"""Reconcile a simulated macro event log and advise whether to retry."""
from .ingest import capture
from .store import connect, save, decide


def reconcile(path, file):
    source, payload = capture(path, 'desktop_macro_events', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                run = db.execute('SELECT * FROM automation_run WHERE id=?',
                                 (str(row['run']),)).fetchone()
                events = row.get('events', [])
                if not run or run['status'] != '已计划':
                    status, reason = '待核', 'run_missing'
                elif any(e.get('kind') == 'publish_ack' for e in events):
                    status, reason = '已完成', 'publish_ack_seen'
                else:
                    status, reason = '可重试', 'ack_missing'
                save(db, 'automation_recovery', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '桌面发布恢复', key, status, reason, source))
        return results
    finally:
        db.close()
