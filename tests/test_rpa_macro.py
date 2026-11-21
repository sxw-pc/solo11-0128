import json
import threading

import pytest

from schedulehandoff import rpa_macro, rpa_execute, report
from schedulehandoff.store import connect

TARGET = '三年级数学调课-2026春'
PUBLISH_STEPS = [
    {'kind': 'open'},
    {'kind': 'type', 'text': TARGET},
    {'kind': 'click', 'action': 'save_draft'},
    {'kind': 'click', 'action': 'publish'},
    {'kind': 'verify'},
]
DRAFT_STEPS = [
    {'kind': 'open'},
    {'kind': 'type', 'text': TARGET},
    {'kind': 'click', 'action': 'save_draft'},
    {'kind': 'verify'},
]


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def plan_row(contract='publish', **over):
    row = {'id': 'M1', 'project': 'P1', 'window': 'W1', 'base_version': 'v3',
           'target_summary': TARGET, 'contract': contract,
           'steps': list(PUBLISH_STEPS if contract == 'publish' else DRAFT_STEPS)}
    row.update(over)
    return row


def plan_file(root, rows, name='macro.json'):
    return write(root, name, {'version': 'v1', 'rows': rows})


def event_row(events, export=None, run='M1', operator='op-1', rid='E1'):
    row = {'id': rid, 'run': run, 'operator': operator, 'events': events}
    if export is not None:
        row['export'] = export
    return row


def events_file(root, rows, name='events.json'):
    return write(root, name, {'version': 'v1', 'rows': rows})


def publish_click(at='2026-01-01T09:00:00', **over):
    event = {'kind': 'publish_click', 'window': 'W1', 'project': 'P1',
             'state': 'edit:v3', 'at': at}
    event.update(over)
    return event


def save_click(at='2026-01-01T09:00:00', **over):
    event = {'kind': 'save_click', 'window': 'W1', 'project': 'P1',
             'state': 'draft:v3', 'at': at}
    event.update(over)
    return event


def export_row(**over):
    export = {'project': 'P1', 'published_version': 'v3', 'published_summary': '旧版课表',
              'generated_at': '2026-01-01T09:05:00', 'holder': None}
    export.update(over)
    return export


def stored(db, table, key):
    row = connect(db).execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    return json.loads(row['body']) if row else None


def test_plan_classifies_steps_and_freezes_contract(tmp_path):
    db = tmp_path / 'ledger.db'
    result = rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    assert result[0]['status'] == '已计划' and result[0]['reason'] == 'macro_recorded'
    body = stored(db, 'automation_run', 'M1')
    assert body['contract'] == 'publish' and body['base_version'] == 'v3'
    assert body['target_summary'] == TARGET and body['window'] == 'W1'
    assert [s['effect'] for s in body['classified']] == ['可重复', '可重复', '可覆盖', '不可逆', '可重复']


def test_plan_requires_frozen_identity_version_and_contract(tmp_path):
    db = tmp_path / 'ledger.db'
    rows = [plan_row(id='M1', window=None),
            plan_row(id='M2', base_version=None),
            plan_row(id='M3', target_summary=None),
            plan_row(id='M4', contract=None),
            plan_row(id='M5', contract='save')]
    result = rpa_macro.plan(db, plan_file(tmp_path, rows))
    assert [x['status'] for x in result] == ['待核'] * 5
    assert [x['reason'] for x in result] == ['identity_missing', 'version_unfrozen',
                                             'version_unfrozen', 'contract_invalid', 'contract_invalid']


def test_plan_rejects_unclassifiable_or_contract_mismatched_steps(tmp_path):
    db = tmp_path / 'ledger.db'
    rows = [plan_row(id='M1', steps=[{'kind': 'open'}, {'kind': 'drag'}]),
            plan_row(id='M2', steps=[{'kind': 'open'}, {'kind': 'click'}]),
            plan_row(id='M3', contract='draft', steps=PUBLISH_STEPS),
            plan_row(id='M4', contract='publish', steps=DRAFT_STEPS),
            plan_row(id='M5', contract='publish',
                     steps=[{'kind': 'click', 'action': 'publish'}, {'kind': 'click', 'action': 'publish'}]),
            plan_row(id='M6', contract='draft', steps=[{'kind': 'open'}, {'kind': 'type'}])]
    result = rpa_macro.plan(db, plan_file(tmp_path, rows))
    assert [x['reason'] for x in result] == ['invalid_steps', 'invalid_steps',
                                             'contract_step_mismatch', 'contract_step_mismatch',
                                             'contract_step_mismatch', 'contract_step_mismatch']


def test_frozen_plan_cannot_be_relabeled_or_moved_to_another_project(tmp_path):
    db = tmp_path / 'ledger.db'
    assert rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))[0]['status'] == '已计划'
    with pytest.raises(ValueError, match='identity_conflict'):
        rpa_macro.plan(db, plan_file(tmp_path, [plan_row(contract='draft')], 'relabel.json'))
    with pytest.raises(ValueError, match='identity_conflict'):
        rpa_macro.plan(db, plan_file(tmp_path, [plan_row(project='P2')], 'moved.json'))
    replay = rpa_macro.plan(db, plan_file(tmp_path, [plan_row()], 'same.json'))
    assert replay[0]['status'] == '已计划'


def test_draft_contract_click_without_ack_allows_controlled_retry(tmp_path):
    """Acceptance: click-only recording, no ack, no export — draft contract may re-save."""
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row(contract='draft')]))
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([save_click()])]))
    assert result[0]['status'] == '可重试'
    assert result[0]['reason'] == 'save_ack_missing_screen_confirms_draft'
    assert result[0]['next'] == '继续'


def test_publish_contract_click_without_ack_or_export_must_not_click_again(tmp_path):
    """Acceptance: click-only recording, no ack, no export — publish contract must not click."""
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()])]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'export_required'
    assert result[0]['next'] == '待复核'
    assert connect(db).execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 0


def test_publish_ack_completes_the_run(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [publish_click(), {'kind': 'publish_ack', 'window': 'W1', 'project': 'P1',
                                'at': '2026-01-01T09:00:30'}]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '已完成' and result[0]['reason'] == 'publish_ack_seen'
    assert result[0]['next'] == '结束'


def test_export_showing_target_published_completes_with_zero_retries(tmp_path):
    """Same local log as the click-only world; only the read-only export tells them apart."""
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    export = export_row(published_version='v4', published_summary=TARGET)
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()], export)]))
    assert result[0]['status'] == '已完成' and result[0]['reason'] == 'target_already_published'
    assert connect(db).execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 0


def test_old_version_with_exclusive_rights_allows_exactly_one_controlled_retry(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    file = events_file(tmp_path, [event_row([publish_click()], export_row())])
    result = rpa_execute.reconcile(db, file)
    assert result[0]['status'] == '可重试' and result[0]['reason'] == 'controlled_retry_authorized'
    lease = connect(db).execute('SELECT * FROM automation_lease').fetchall()
    assert len(lease) == 1 and json.loads(lease[0]['body'])['operator'] == 'op-1'
    replay = rpa_execute.reconcile(db, file)
    assert replay[0]['status'] == '可重试' and replay[0]['reason'] == 'controlled_retry_authorized'
    assert connect(db).execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 1


def test_old_version_but_another_operator_holds_publish_rights_goes_to_review(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    export = export_row(holder='op-2')
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()], export)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'publish_rights_held_by_other'
    assert connect(db).execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 0


def test_second_recovery_process_cannot_pass_the_same_old_version_check(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    first = rpa_execute.reconcile(db, events_file(
        tmp_path, [event_row([publish_click()], export_row(), operator='op-a', rid='EA')], 'ea.json'))
    assert first[0]['status'] == '可重试'
    second = rpa_execute.reconcile(db, events_file(
        tmp_path, [event_row([publish_click()], export_row(), operator='op-b', rid='EB')], 'eb.json'))
    assert second[0]['status'] == '待复核' and second[0]['reason'] == 'lease_held_by_other'
    assert connect(db).execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 1


def test_simultaneous_recovery_processes_do_not_publish_twice(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    results = []

    def recover(operator, rid, name):
        file = events_file(tmp_path, [event_row([publish_click()], export_row(),
                                                operator=operator, rid=rid)], name)
        try:
            results.append(rpa_execute.reconcile(db, file)[0])
        except Exception as exc:  # surfaced by the assertions below
            results.append(exc)

    threads = [threading.Thread(target=recover, args=('op-a', 'EA', 'ea.json')),
               threading.Thread(target=recover, args=('op-b', 'EB', 'eb.json'))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not any(isinstance(x, Exception) for x in results)
    assert sorted(x['status'] for x in results) == ['可重试', '待复核']
    review = [x for x in results if x['status'] == '待复核'][0]
    assert review['reason'] == 'lease_held_by_other'
    leases = connect(db).execute('SELECT * FROM automation_lease').fetchall()
    assert len(leases) == 1
    assert json.loads(leases[0]['body'])['operator'] in ('op-a', 'op-b')


def test_stale_export_cannot_clear_the_click_goes_to_review(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    export = export_row(generated_at='2026-01-01T08:59:00')
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()], export)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'export_stale'


def test_version_advanced_to_someone_elses_revision_goes_to_review(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    export = export_row(published_version='v7', published_summary='别人的修订')
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()], export)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'version_advanced_by_other'


def test_focus_jump_to_same_named_projects_other_window_blocks_clicks(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [{'kind': 'window_focus', 'window': 'W2', 'project': 'P1', 'at': '2026-01-01T08:59:00'},
              publish_click(window='W2')]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'window_mismatch'


def test_events_from_another_project_block_clicks(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [publish_click(project='P2')]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'project_mismatch'


def test_frame_gap_and_popup_leave_explainable_review_state(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    rows = [event_row([publish_click(), {'kind': 'frame_gap', 'at': '2026-01-01T09:00:10'}], rid='E1'),
            event_row([publish_click(), {'kind': 'popup', 'at': '2026-01-01T09:00:10'}], rid='E2')]
    result = rpa_execute.reconcile(db, events_file(tmp_path, rows))
    assert [(x['status'], x['reason']) for x in result] == [('待复核', 'recording_incomplete'),
                                                           ('待复核', 'recording_incomplete')]


def test_local_export_file_landing_before_ack_stays_explainable(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [publish_click(),
              {'kind': 'local_export', 'window': 'W1', 'project': 'P1', 'at': '2026-01-01T09:00:20'},
              {'kind': 'publish_ack', 'window': 'W1', 'project': 'P1', 'at': '2026-01-01T09:00:30'}]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '已完成' and result[0]['reason'] == 'publish_ack_seen'
    body = stored(db, 'automation_recovery', 'E1')
    assert any(e['kind'] == 'local_export' for e in body['events'])


def test_draft_save_ack_completes_and_missing_screen_state_goes_to_review(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row(contract='draft')]))
    acked = [save_click(), {'kind': 'save_ack', 'window': 'W1', 'project': 'P1',
                            'at': '2026-01-01T09:00:30'}]
    blind = [{'kind': 'save_click', 'window': 'W1', 'project': 'P1', 'at': '2026-01-01T09:00:00'}]
    rows = [event_row(acked, rid='E1'), event_row(blind, rid='E2')]
    result = rpa_execute.reconcile(db, events_file(tmp_path, rows))
    assert [(x['status'], x['reason']) for x in result] == [('已完成', 'save_ack_seen'),
                                                           ('待复核', 'screen_state_unknown')]


def test_draft_contract_flags_unexpected_publish_event(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row(contract='draft')]))
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()])]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'unexpected_publish_event'


def test_run_missing_or_not_planned_is_rejected(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row(id='M2', contract='save')]))
    rows = [event_row([publish_click()], run='M9', rid='E1'),
            event_row([publish_click()], run='M2', rid='E2')]
    result = rpa_execute.reconcile(db, events_file(tmp_path, rows))
    assert [(x['status'], x['reason']) for x in result] == [('待核', 'run_missing'),
                                                           ('待核', 'run_not_planned')]


def test_publish_not_attempted_continues_the_macro(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [{'kind': 'window_focus', 'window': 'W1', 'project': 'P1', 'at': '2026-01-01T08:59:00'},
              {'kind': 'screen_state', 'window': 'W1', 'project': 'P1', 'state': 'edit:v3',
               'at': '2026-01-01T08:59:30'}]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '可重试' and result[0]['reason'] == 'publish_not_attempted'


def test_click_without_window_context_cannot_be_trusted(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    events = [{'kind': 'publish_click', 'at': '2026-01-01T09:00:00'}]
    result = rpa_execute.reconcile(db, events_file(tmp_path, [event_row(events)]))
    assert result[0]['status'] == '待复核' and result[0]['reason'] == 'click_context_unverified'


def test_replaying_the_same_recording_is_idempotent(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    file = events_file(tmp_path, [event_row([publish_click()], export_row())])
    first = rpa_execute.reconcile(db, file)
    second = rpa_execute.reconcile(db, file)
    assert first == second
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM decision_log WHERE subject='E1'").fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM automation_lease').fetchone()[0] == 1


def test_report_replays_why_there_was_no_second_click(tmp_path):
    db = tmp_path / 'ledger.db'
    rpa_macro.plan(db, plan_file(tmp_path, [plan_row()]))
    rpa_execute.reconcile(db, events_file(tmp_path, [event_row([publish_click()])], 'blind.json'))
    rpa_execute.reconcile(db, events_file(
        tmp_path, [event_row([publish_click()], export_row(), operator='op-a', rid='E2')], 'lease.json'))
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    story = data['macro_replay']['M1']
    assert story['contract'] == 'publish' and story['base_version'] == 'v3'
    assert story['target_summary'] == TARGET
    assert story['lease_holder'] == 'op-a'
    recoveries = {r['id']: (r['status'], r['reason']) for r in story['recoveries']}
    assert recoveries == {'E1': ('待复核', 'export_required'),
                          'E2': ('可重试', 'controlled_retry_authorized')}
    check = {r['id']: r['version_check'] for r in story['recoveries']}
    assert check['E2']['published_version'] == 'v3' and check['E2']['lease_holder'] == 'op-a'
