"""Reconcile a simulated macro event log and decide the next safe step.

A publish click whose acknowledgement is missing is ambiguous: the local event
log alone cannot distinguish "the click landed but the ack recording was lost"
from "the click never landed".  Reconcile therefore refuses to guess.  For the
irreversible publish contract it asks the versioned read-only publish export
for evidence and authorizes at most one controlled retry, and only while
holding the exclusive local lease for the project.  For the draft contract a
lost save ack is recoverable because re-saving overwrites the same draft
version, so a screen-read that confirms the same draft is enough.

Every evaluation is stored with its version check so the report can replay why
a second click was or was not authorized.
"""
import json
from .ingest import capture
from .store import connect, save, decide, acquire_lease

EVENT_KINDS = {'window_focus', 'screen_state', 'step_ok', 'save_click', 'publish_click',
               'save_ack', 'publish_ack', 'frame_gap', 'popup', 'local_export'}
EXPORT_FIELDS = ('project', 'published_version', 'published_summary')
NEXT_STEP = {'可重试': '继续', '已完成': '结束', '待复核': '待复核', '待核': '待核'}


def reconcile(path, file):
    source, payload = capture(path, 'desktop_macro_events', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                run = db.execute('SELECT * FROM automation_run WHERE id=?', (str(row.get('run')),)).fetchone()
                if not run:
                    status, reason, check = '待核', 'run_missing', {}
                elif run['status'] != '已计划':
                    status, reason, check = '待核', 'run_not_planned', {}
                else:
                    status, reason, check = evaluate(db, json.loads(run['body']), row, source, payload['version'])
                save(db, 'automation_recovery', key, source, payload['version'],
                     dict(row, version_check=check), status, reason)
                result = decide(db, '桌面发布恢复', key, status, reason, source)
                result['next'] = NEXT_STEP[status]
                results.append(result)
        return results
    finally:
        db.close()


def evaluate(db, plan, row, source, version):
    events = row.get('events', [])
    export = row.get('export')
    operator = row.get('operator')
    contract = plan.get('contract')
    check = {'contract': contract, 'base_version': plan.get('base_version'),
             'target_summary': plan.get('target_summary'),
             'published_version': export.get('published_version') if isinstance(export, dict) else None,
             'published_summary': export.get('published_summary') if isinstance(export, dict) else None,
             'export_generated_at': export.get('generated_at') if isinstance(export, dict) else None,
             'lease_holder': None}
    if not all(isinstance(e, dict) for e in events) or not {e.get('kind') for e in events} <= EVENT_KINDS:
        return '待核', 'invalid_events', check
    if not operator:
        return '待核', 'operator_missing', check
    ack = 'publish_ack' if contract == 'publish' else 'save_ack'
    if any(e.get('kind') == ack for e in events):
        return '已完成', f'{ack}_seen', check
    # Every step's precondition is checked against the frozen window and project:
    # once focus jumps to another window of the same-named project (or to another
    # project), no further click may be trusted.
    for event in events:
        if event.get('project') is not None and event.get('project') != plan.get('project'):
            return '待复核', 'project_mismatch', check
        if event.get('window') is not None and event.get('window') != plan.get('window'):
            return '待复核', 'window_mismatch', check
    # Frame gaps and popup occlusion leave explainable state: the recording can no
    # longer prove what happened, so the recovery stops for manual review.
    if any(e.get('kind') in ('frame_gap', 'popup') for e in events):
        return '待复核', 'recording_incomplete', check
    if contract == 'draft':
        return evaluate_draft(plan, events, check)
    return evaluate_publish(db, plan, row, events, export, operator, source, version, check)


def evaluate_draft(plan, events, check):
    if any(e.get('kind') in ('publish_click', 'publish_ack') for e in events):
        return '待复核', 'unexpected_publish_event', check
    clicks = [e for e in events if e.get('kind') == 'save_click']
    if not clicks:
        return '可重试', 'save_not_attempted', check
    last = clicks[-1]
    if last.get('window') != plan.get('window') or last.get('project') != plan.get('project'):
        return '待复核', 'click_context_unverified', check
    # A lost save ack is safe to retry only while the screen still shows the same
    # draft; re-saving then just overwrites the same draft version.
    expected = f"draft:{plan.get('base_version')}"
    if any(e.get('state') == expected for e in events[events.index(last):]):
        return '可重试', 'save_ack_missing_screen_confirms_draft', check
    return '待复核', 'screen_state_unknown', check


def evaluate_publish(db, plan, row, events, export, operator, source, version, check):
    clicks = [e for e in events if e.get('kind') == 'publish_click']
    if len(clicks) > 1:
        return '待复核', 'multiple_publish_clicks', check
    if not clicks:
        return '可重试', 'publish_not_attempted', check
    click = clicks[0]
    if click.get('window') != plan.get('window') or click.get('project') != plan.get('project'):
        return '待复核', 'click_context_unverified', check
    # The click may or may not have landed; only the read-only publish export can
    # tell the two worlds apart.  Without it, never click again.
    if export is None:
        return '待复核', 'export_required', check
    if not isinstance(export, dict) or any(field not in export for field in EXPORT_FIELDS):
        return '待复核', 'export_invalid', check
    if export['project'] != plan.get('project'):
        return '待复核', 'export_project_mismatch', check
    if export['published_summary'] == plan.get('target_summary'):
        return '已完成', 'target_already_published', check
    if not click.get('at') or not export.get('generated_at') or export['generated_at'] <= click['at']:
        return '待复核', 'export_stale', check
    if export['published_version'] != plan.get('base_version'):
        return '待复核', 'version_advanced_by_other', check
    holder = export.get('holder')
    if holder and holder != operator:
        return '待复核', 'publish_rights_held_by_other', check
    # Old version still published and the export grants no one else's rights:
    # authorize exactly one controlled retry under the exclusive local lease.
    acquired, held = acquire_lease(db, plan.get('project'), str(plan.get('id')), operator,
                                   str(row.get('id')), source, version)
    check['lease_holder'] = held.get('operator') if isinstance(held, dict) else operator
    if not acquired:
        return '待复核', 'lease_held_by_other', check
    return '可重试', 'controlled_retry_authorized', check
