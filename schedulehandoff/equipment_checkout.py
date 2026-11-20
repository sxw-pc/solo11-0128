"""Capture local classroom equipment checkout scans.

Beyond the original borrower/serial existence check, a checkout now freezes
the kit configuration, the custody agreement (personal vs team, fixed at
checkout time), the lending station and the device clock skew. No liability
case exists before return evidence arrives; checkout only records the root of
each serial's custody chain.
"""
from .ingest import capture
from .store import connect, save, decide, upsert
from . import lifecycle


def _validate(row):
    if not row.get('borrower') or not row.get('serials'):
        return '待核', 'borrower_or_serial_missing'
    if row.get('agreement') and row['agreement'] not in lifecycle.VALID_AGREEMENTS:
        return '待核', 'unknown_agreement'
    if len(row['serials']) != len(set(row['serials'])):
        return '待核', 'duplicate_serial_in_kit'
    return '已借出', 'checkout_recorded'


def checkout(path, file):
    source, payload = capture(path, 'desktop_equipment_checkout', file)
    db = connect(path)
    results = []
    try:
        with db:
            for kit in payload.get('kits', []):
                save(db, 'equipment_kit', str(kit['id']), source, payload['version'],
                     kit, '已采录', 'kit_configuration_captured')
            for row in payload.get('rows', []):
                key = str(row['id'])
                status, reason = _validate(row)
                save(db, 'equipment_loan', key, source, payload['version'],
                     row, status, reason)
                if status == '已借出':
                    for serial in row['serials']:
                        upsert(db, 'equipment_scan', f'checkout:{key}:{serial}',
                               source, payload['version'],
                               {'id': f'checkout:{key}:{serial}', 'loan': key,
                                'serial': serial, 'serials': [serial],
                                'station': row.get('station'), 'time': row.get('time'),
                                'returner': None, 'kind': 'checkout'},
                               '已采录', 'checkout_station_scan')
                results.append(decide(db, '课堂设备借出', key, status, reason, source))
            lifecycle.refresh_contracts(db, source, payload['version'])
            return results
    finally:
        db.close()
