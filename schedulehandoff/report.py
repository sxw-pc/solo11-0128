import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def _rows(db, table):
    out = []
    for row in db.execute(f'SELECT * FROM {table} ORDER BY id'):
        item = dict(row)
        item['body'] = json.loads(item['body'])
        out.append(item)
    return out


def reschedule_review(db):
    """汇总每个调课计划的方案、淘汰原因、行程判定与批准时变化。"""
    review = []
    for plan in _rows(db, 'reschedule_plan'):
        body = plan['body']
        plan_checks = [x for x in _rows(db, 'itinerary_check')
                       if x['id'].startswith(f"{plan['id']}:plan:")]
        approval_checks = [x for x in _rows(db, 'itinerary_check')
                           if x['id'].startswith(f"{plan['id']}:approval:")]
        approval = db.execute('SELECT * FROM reschedule_approval WHERE id=?',
                              (plan['id'],)).fetchone()
        review.append({
            'plan_id': plan['id'], 'status': plan['status'], 'reason': plan['reason'],
            'version': plan['version'],
            'assignments': body.get('assignments', []),
            'eliminations': body.get('eliminations', []),
            'plan_itinerary': [x['body'] for x in plan_checks],
            'approval_itinerary': [x['body'] for x in approval_checks],
            'approval': dict(approval) if approval else None,
            'approval_change': _diff_itinerary(
                [x['body'] for x in plan_checks], [x['body'] for x in approval_checks]),
        })
    return review


def _diff_itinerary(plan_records, approval_records):
    """对比计划与批准两阶段的相邻课节判定，列出批准时发生的变化。"""
    def key(r):
        return (r['chain'], r['subject'], r['from_lesson'], r['to_lesson'])
    before = {key(r): r for r in plan_records}
    changes = []
    for rec in approval_records:
        old = before.get(key(rec))
        if old is None:
            changes.append({'pair': key(rec), 'change': 'added_at_approval', 'now': rec})
        elif (old['status'], old.get('route_id'), old.get('route_version')) != \
                (rec['status'], rec.get('route_id'), rec.get('route_version')):
            changes.append({'pair': key(rec), 'change': 'changed_at_approval',
                            'before': {'status': old['status'], 'route_id': old.get('route_id'),
                                       'route_version': old.get('route_version')},
                            'after': {'status': rec['status'], 'route_id': rec.get('route_id'),
                                      'route_version': rec.get('route_version')}})
    return changes


def build(path, out):
    db = connect(path)
    try:
        sources = []
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x = dict(item)
            x.pop('body')
            x['verified'] = hashlib.sha256(item['body']).hexdigest() == item['hash']
            sources.append(x)
        data = {'sources': sources, 'entities': {},
                'issues': [dict(x) for x in db.execute('SELECT * FROM issue')],
                'decisions': [dict(x) for x in db.execute('SELECT * FROM decision_log')],
                'reschedule_review': reschedule_review(db)}
        for table in TABLES:
            data['entities'][table] = [dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return out
    finally:
        db.close()
