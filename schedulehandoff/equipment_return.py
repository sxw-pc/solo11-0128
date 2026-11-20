"""Compare station return scans against the full custody evidence.

The old set comparison could not say which physical item was lost or who held
it. Returns now (1) collapse duplicate scans of one serial, (2) match each
returned serial across every outstanding kit so a serial can settle at most
one loan, and (3) report the actual contractual liability — T1/T2/joint/待核 —
using the custody chains instead of a plain serial_mismatch.
"""
from .ingest import capture
from .store import connect, save, decide, get_row, load_rows, upsert
from . import lifecycle


def _dedup_serials(row):
    seen, unique = set(), []
    for serial in row.get('serials', []):
        if serial in seen:
            continue
        seen.add(serial)
        unique.append(serial)
    return unique


def receive(path, file):
    source, payload = capture(path, 'desktop_equipment_return', file)
    db = connect(path)
    results = []
    try:
        with db:
            affected = set()
            for row in payload.get('rows', []):
                key = str(row['id'])
                loan = get_row(db, 'equipment_loan', str(row.get('loan')))
                serials = _dedup_serials(row)
                stored = dict(row, serials=serials)
                if not loan or loan['status'] != lifecycle.ACTIVE_LOAN:
                    status, reason = '待核', 'loan_missing'
                else:
                    loan_body = __import__('json').loads(loan['body'])
                    if len(row.get('serials', [])) != len(serials):
                        reason_note = 'duplicate_scan_counts_once'
                    else:
                        reason_note = None
                    unknown = [s for s in serials if s not in loan_body['serials']]
                    for serial in serials:
                        scan_body = {'id': f'{key}:{serial}', 'return_row': key,
                                     'loan': str(row['loan']), 'serial': serial,
                                     'serials': [serial], 'station': row.get('station'),
                                     'time': row.get('time'), 'returner': row.get('returner'),
                                     'kind': 'return'}
                        upsert(db, 'equipment_scan', scan_body['id'], source,
                               payload['version'], scan_body, '已采录',
                               reason_note or 'return_station_scan')
                    affected.add(str(row['loan']))
                    for other in load_rows(db, 'equipment_loan'):
                        ob = __import__('json').loads(other['body'])
                        if any(s in ob.get('serials', []) for s in serials):
                            affected.add(str(ob['id']))
                    if unknown and not any(s in loan_body['serials'] for s in serials):
                        status, reason = '待核', 'substitute_serial_not_in_any_kit_position'
                    else:
                        status = '已归还' if not unknown and set(serials) >= set(loan_body['serials']) else '缺件'
                        reason = reason_note or 'return_evidence_captured'
                save(db, 'equipment_return', key, source, payload['version'],
                     stored, status, reason)
                results.append(decide(db, '课堂设备归还', key, status, reason, source))
            if affected:
                # run custody reasoning while every touched loan is still open
                # so scans resolve onto them; only then settle loan status
                loan_results = lifecycle.reanalyze(db, affected, source, payload['version'])
                lifecycle.settle_returns(db, affected)
                for item in loan_results:
                    for serial, verdict in item['serials'].items():
                        decide(db, '器材责任链', f"{item['loan_id']}:{serial}",
                               verdict['status'], verdict['reason'], source)
            return results
    finally:
        db.close()
