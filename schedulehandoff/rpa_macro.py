"""Plan a simulated offline desktop schedule publication macro.

The plan phase freezes the operation contract before any execution: each
recorded step is classified as repeatable input (可重复), an overwritable draft
save (可覆盖), or the irreversible formal publish (不可逆), and the run freezes
the project identity, the window it is allowed to operate in, the pre-publish
project version and this round's target summary.  Once a run id is captured its
frozen body cannot be overwritten by a later recording of another project or
contract (store.save raises identity_conflict), so a failed formal publish can
never be relabelled as a draft save after the fact.
"""
from .ingest import capture
from .store import connect, save, decide

STEP_KINDS = {'open', 'type', 'click', 'verify'}
CLICK_EFFECTS = {'save_draft': '可覆盖', 'publish': '不可逆'}
CONTRACTS = {'draft', 'publish'}


def classify(steps):
    """Classify each recorded step, or return None when a step cannot be classified."""
    classified = []
    for step in steps:
        kind = step.get('kind')
        if kind not in STEP_KINDS:
            return None
        effect = '可重复'
        if kind == 'click':
            action = step.get('action')
            if action not in CLICK_EFFECTS:
                return None
            effect = CLICK_EFFECTS[action]
        classified.append(dict(step, effect=effect))
    return classified


def plan(path, file):
    source, payload = capture(path, 'desktop_macro_plan', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                steps = row.get('steps', [])
                classified = classify(steps) if steps else None
                contract = row.get('contract')
                publishes = [s for s in (classified or []) if s['effect'] == '不可逆']
                saves = [s for s in (classified or []) if s['effect'] == '可覆盖']
                if not row.get('project') or not row.get('window'):
                    status, reason = '待核', 'identity_missing'
                elif not row.get('base_version') or not row.get('target_summary'):
                    status, reason = '待核', 'version_unfrozen'
                elif contract not in CONTRACTS:
                    status, reason = '待核', 'contract_invalid'
                elif classified is None:
                    status, reason = '待核', 'invalid_steps'
                elif any(s.get('window') not in (None, row['window']) for s in classified):
                    status, reason = '待核', 'invalid_steps'
                elif contract == 'draft' and (publishes or not saves):
                    status, reason = '待核', 'contract_step_mismatch'
                elif contract == 'publish' and len(publishes) != 1:
                    status, reason = '待核', 'contract_step_mismatch'
                else:
                    status, reason = '已计划', 'macro_recorded'
                body = dict(row)
                if classified is not None:
                    body['classified'] = classified
                save(db, 'automation_run', key, source, payload['version'], body, status, reason)
                results.append(decide(db, '桌面发布脚本', key, status, reason, source))
        return results
    finally:
        db.close()
