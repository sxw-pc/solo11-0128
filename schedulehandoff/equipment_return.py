"""Compare a local return scan against its checkout list."""
import json
from .ingest import capture
from .store import connect, save, decide


def receive(path, file):
    source, payload = capture(path, 'desktop_equipment_return', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                loan = db.execute('SELECT * FROM equipment_loan WHERE id=?',
                                  (str(row['loan']),)).fetchone()
                if not loan or loan['status'] != '已借出':
                    status, reason = '待核', 'loan_missing'
                elif set(row.get('serials', [])) != set(json.loads(loan['body'])['serials']):
                    status, reason = '缺件', 'serial_mismatch'
                else:
                    status, reason = '已归还', 'all_serials_returned'
                save(db, 'equipment_return', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '课堂设备归还', key, status, reason, source))
        return results
    finally:
        db.close()
