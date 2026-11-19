"""Plan a simulated offline desktop schedule publication macro."""
from .ingest import capture
from .store import connect, save, decide


def plan(path, file):
    source, payload = capture(path, 'desktop_macro_plan', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                steps = row.get('steps', [])
                if not steps or any(s.get('kind') not in {'open', 'type', 'click', 'verify'} for s in steps):
                    status, reason = '待核', 'invalid_steps'
                else:
                    status, reason = '已计划', 'macro_recorded'
                save(db, 'automation_run', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '桌面发布脚本', key, status, reason, source))
        return results
    finally:
        db.close()
