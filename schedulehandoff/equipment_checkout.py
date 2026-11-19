"""Capture local classroom equipment checkout scans."""
from .ingest import capture
from .store import connect, save, decide


def checkout(path, file):
    source, payload = capture(path, 'desktop_equipment_checkout', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if not row.get('borrower') or not row.get('serials'):
                    status, reason = '待核', 'borrower_or_serial_missing'
                else:
                    status, reason = '已借出', 'checkout_recorded'
                save(db, 'equipment_loan', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '课堂设备借出', key, status, reason, source))
        return results
    finally:
        db.close()
