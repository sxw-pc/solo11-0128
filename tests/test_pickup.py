import json
from pathlib import Path

from schedulehandoff import operations, pickup, report
from schedulehandoff.store import connect

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write(root, name, obj):
    path = root / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return path


def lesson_ledger(root, end='10:30', version='v1'):
    """Room + teacher + class notice so the effective lesson ends at ``end``."""
    db = root / 'ledger.db'
    operations.first(db, write(root, f'rooms-{version}.json', {'version': version, 'rows': [
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': f'2026-01-01T{end}:00',
         'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(root, f'staff-{version}.json', {'version': version, 'rows': [
        {'id': 'S1', 'lesson': 'L1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True}]}))
    operations.third(db, write(root, f'notice-{version}.json', {'version': version, 'rows': [
        {'id': 'N1', 'lesson': 'L1', 'required_classes': ['C1'],
         'receipts': [{'class': 'C1', 'version': version}]}]}))
    return db


def pickup_case(contract_mode='delegate', contract_id='K-normal', revocations=None,
                delegate_id_number='IDB-777', delegate_name='Parent B', release_at='10:35',
                events=None, credentials=None, extra_delegations=None):
    data = {
        'version': 'v1',
        'students': [{'id': 'C1', 'name': 'Student C1', 'class_id': 'C1', 'guardian_id': 'G-A'}],
        'guardians': [{'id': 'G-A', 'name': 'Parent A', 'id_type': 'national_id',
                       'id_number': 'IDA-001', 'student_ids': ['C1']}],
        'contracts': [{'id': contract_id, 'student_id': 'C1', 'guardian_id': 'G-A',
                       'mode': contract_mode, 'signed_at': '2026-01-01T09:00:00',
                       'scope': ['C1']}],
        'delegations': [{'id': 'D1', 'student_id': 'C1', 'contract_id': contract_id,
                         'issuer_id': 'G-A', 'delegate_id': 'G-B', 'delegate_name': delegate_name,
                         'delegate_id_type': 'national_id',
                         'delegate_id_number': delegate_id_number,
                         'valid_from': '2026-01-01T10:30:00',
                         'valid_to': '2026-01-01T10:45:00',
                         'issued_at': '2026-01-01T10:25:00', 'scope': ['C1']}],
        'sessions': [{'id': 'S-C1', 'student_id': 'C1', 'class_id': 'C1', 'lesson_id': 'L1',
                      'pickup_at': f'2026-01-01T{release_at}:00', 'assigned_exit': 'east',
                      'contract_id': contract_id}],
        'credentials': credentials or [{'id': 'K1', 'session_id': 'S-C1', 'serial': 1,
                                        'exit': 'east'}],
        'events': events or [
            {'id': 'e-id', 'session_id': 'S-C1', 'kind': 'identity_check',
             'at': f'2026-01-01T{release_at}:00', 'person_id': 'G-B', 'person_name': delegate_name,
             'id_type': 'national_id', 'id_number': delegate_id_number},
            {'id': 'e-teach', 'session_id': 'S-C1', 'kind': 'teacher_handover',
             'at': '2026-01-01T10:31:00', 'credential_id': 'K1', 'exit': 'east'},
            {'id': 'e-recv', 'session_id': 'S-C1', 'kind': 'exit_receipt',
             'at': '2026-01-01T10:33:00', 'credential_id': 'K1', 'exit': 'east'},
            {'id': 'e-rel', 'session_id': 'S-C1', 'kind': 'pickup_release',
             'at': f'2026-01-01T{release_at}:00', 'credential_id': 'K1', 'exit': 'east',
             'person_id': 'G-B', 'person_name': delegate_name, 'id_type': 'national_id',
             'id_number': delegate_id_number},
        ],
    }
    if revocations:
        data['revocations'] = revocations
    if extra_delegations:
        data['delegations'].extend(extra_delegations)
    return data


def run_case(root, data, name='case.json', end='10:30'):
    db = lesson_ledger(root, end=end)
    return db, pickup.run(db, write(root, name, data))[0]


# ---------------------------------------------------------------------------
# acceptance scenario: two contracts signed before pickup
# ---------------------------------------------------------------------------


def test_normal_contract_allows_designated_temporary_delegate(tmp_path):
    db, result = run_case(tmp_path, pickup_case('delegate'))
    assert result['status'] == '允许交接'
    assert result['reason'] == 'complete_custody_chain'
    assert result['delivered'] is True and result['safe_delivery'] is True
    assert result['window']['lesson_end'] == '2026-01-01T10:30:00'
    assert result['window']['effective'] is True
    assert result['authorization']['delegation']['id'] == 'D1'
    assert result['identity_check']['reason'] == 'identity_verified'
    assert result['custody']['checks']['distinct_actions'] is True
    assert result['current_conclusion'] == '已发生交付且可证安全'


def test_guardian_only_contract_refuses_delegate(tmp_path):
    db, result = run_case(tmp_path, pickup_case('guardian_only', contract_id='K-guardian-only'))
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'contract_guardian_only'
    assert result['delivered'] is False


def test_contract_cannot_be_switched_after_the_fact(tmp_path):
    """Both contracts exist; the session is locked to the guardian-only one."""
    db = lesson_ledger(tmp_path)
    data = pickup_case('guardian_only', contract_id='K-guardian-only')
    data['contracts'].append({'id': 'K-normal', 'student_id': 'C1', 'guardian_id': 'G-A',
                              'mode': 'delegate', 'signed_at': '2026-01-01T09:00:00',
                              'scope': ['C1']})
    result = pickup.run(db, write(tmp_path, 'case.json', data))[0]
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'contract_guardian_only'
    assert result['authorization']['contract']['id'] == 'K-guardian-only'


def test_unlocked_contract_is_never_chosen_by_identity(tmp_path):
    db = lesson_ledger(tmp_path)
    data = pickup_case('delegate')
    data['sessions'][0].pop('contract_id')
    data['contracts'].append({'id': 'K-guardian-only', 'student_id': 'C1', 'guardian_id': 'G-A',
                              'mode': 'guardian_only', 'signed_at': '2026-01-01T09:00:00',
                              'scope': ['C1']})
    result = pickup.run(db, write(tmp_path, 'case.json', data))[0]
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'contract_not_locked'


# ---------------------------------------------------------------------------
# identity evidence
# ---------------------------------------------------------------------------


def test_same_name_different_document_is_rejected(tmp_path):
    data = pickup_case('delegate', delegate_id_number='IDB-777')
    # Another person with the same display name presents a different document.
    for event in data['events']:
        if event['id'] in ('e-id', 'e-rel'):
            event['id_number'] = 'IDB-999'
    db, result = run_case(tmp_path, data)
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'identity_mismatch'
    assert 'same_name_different_document' in result['reasons']
    assert result['delivered'] is False
    assert result['identity_check']['presented']['id_number'] == 'IDB-999'
    assert result['identity_check']['registered']['id_number'] == 'IDB-777'
    # The roster remembers the *registered* delegate, not the presented name.
    roster = json.loads(connect(db).execute(
        "SELECT body FROM pickup_roster WHERE id LIKE 'C1:%'").fetchone()[0])
    assert roster['authorized_names'] == ['Parent A', 'Parent B']


def test_missing_document_pends_instead_of_listing_by_name(tmp_path):
    db, result = run_case(tmp_path, pickup_case('delegate', delegate_id_number=''))
    assert result['status'] == '待核'
    assert result['reason'] == 'identity_unverified'
    assert result['delivered'] is False


# ---------------------------------------------------------------------------
# delegation scope, revocation and timing
# ---------------------------------------------------------------------------


def test_out_of_scope_redelegation_is_rejected(tmp_path):
    data = pickup_case('delegate')
    data['delegations'][0].update(issuer_id='G-X')
    db, result = run_case(tmp_path, data)
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'delegation_out_of_scope'


def test_revocation_effective_before_arrival_blocks_delivery(tmp_path):
    data = pickup_case('delegate', revocations=[
        {'id': 'RV1', 'delegation_id': 'D1', 'issued_at': '2026-01-01T10:20:00',
         'effective_at': '2026-01-01T10:21:00'}])
    db, result = run_case(tmp_path, data)
    assert result['status'] == '应拒绝'
    assert result['reason'] == 'delegation_revoked_before_pickup'
    assert result['delivered'] is False


def test_revocation_issued_after_delivery_does_not_erase_the_fact(tmp_path):
    db = lesson_ledger(tmp_path)
    first = pickup.run(db, write(tmp_path, 'case.json', pickup_case('delegate')))[0]
    assert first['delivered'] is True
    late = pickup_case('delegate', revocations=[
        {'id': 'RV-late', 'delegation_id': 'D1', 'issued_at': '2026-01-01T10:40:00',
         'effective_at': '2026-01-01T10:41:00'}])
    late['version'] = 'v2'
    result = pickup.run(db, write(tmp_path, 'late.json', late))[0]
    # Delivery already happened; the late revocation is recorded, and the
    # historical delivery remains.
    assert result['delivered'] is True
    assert len(result['authorization']['late_revocations']) == 1
    delivery = connect(db).execute("SELECT body FROM pickup_delivery WHERE id='S-C1'").fetchone()
    assert delivery is not None


# ---------------------------------------------------------------------------
# timetable revision and notice edition
# ---------------------------------------------------------------------------


def test_schedule_delay_makes_old_delegation_window_not_current(tmp_path):
    db = lesson_ledger(tmp_path, end='10:30')
    assert pickup.run(db, write(tmp_path, 'case.json', pickup_case('delegate')))[0]['status'] == '允许交接'
    # Desktop correction: lesson now ends at 11:00.  The old delegation only
    # covered 10:30-10:45.
    operations.first(db, write(tmp_path, 'rooms-v2.json', {'version': 'v2', 'rows': [
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': '2026-01-01T11:00:00',
         'capacity': 35, 'enrolled': 30}]}))
    result = pickup.evaluate(db)[0]
    assert result['window']['lesson_end'] == '2026-01-01T11:00:00'
    assert result['window']['effective'] is False
    assert result['window']['reason'] == 'schedule_revised'
    assert result['status'] == '待核'
    assert result['delivered'] is True  # the earlier handover still happened
    assert result['safe_delivery'] is False
    assert result['current_conclusion'] == '已发生交付，当前结论待核'
    # The notice for the old edition is reported but no longer effective.
    batch = connect(db).execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()
    assert batch['status'] in ('待重新通知', '换班生效')


def test_old_credential_is_not_a_current_dismissal_after_delay(tmp_path):
    db = lesson_ledger(tmp_path, end='10:30')
    operations.first(db, write(tmp_path, 'rooms-v2.json', {'version': 'v2', 'rows': [
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': '2026-01-01T11:00:00',
         'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(tmp_path, 'staff-v2.json', {'version': 'v2', 'rows': [
        {'id': 'S1', 'lesson': 'L1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True}]}))
    # New notice edition for the corrected timetable.
    operations.third(db, write(tmp_path, 'notice-v2.json', {'version': 'v2', 'rows': [
        {'id': 'N1', 'lesson': 'L1', 'required_classes': ['C1'],
         'receipts': [{'class': 'C1', 'version': 'v2'}]}]}))
    data = pickup_case('delegate')
    data['version'] = 'v2'
    result = pickup.run(db, write(tmp_path, 'case.json', data))[0]
    # Arrival at 10:35 predates the corrected 11:00 dismissal.
    assert result['window']['lesson_end'] == '2026-01-01T11:00:00'
    assert result['window']['effective'] is True
    assert result['status'] == '待核'
    assert result['reason'] == 'before_current_dismissal'
    assert result['delivered'] is False


# ---------------------------------------------------------------------------
# custody actions
# ---------------------------------------------------------------------------


def test_single_scan_is_not_delivery(tmp_path):
    data = pickup_case('delegate')
    data['events'] = [e for e in data['events'] if e['id'] != 'e-teach']
    db, result = run_case(tmp_path, data)
    assert result['status'] == '待核'
    assert result['reason'] == 'custody_incomplete'
    assert 'teacher_handover' in result['custody']['missing']
    assert result['delivered'] is False


def test_exit_receipt_before_teacher_handover_is_invalid(tmp_path):
    data = pickup_case('delegate')
    for event in data['events']:
        if event['id'] == 'e-recv':
            event['at'] = '2026-01-01T10:30:30'
    db, result = run_case(tmp_path, data)
    assert result['status'] == '待核'
    assert result['reason'] == 'custody_incomplete'
    assert result['delivered'] is False


# ---------------------------------------------------------------------------
# exits, credentials and offline merge
# ---------------------------------------------------------------------------


def _east_transfer(events, credentials, serial=2, successor='K2'):
    events = [e for e in events if e['id'] != 'e-rel']
    events += [
        {'id': 'e-out-east', 'session_id': 'S-C1', 'kind': 'exit_transfer_out',
         'at': '2026-01-01T10:34:00', 'predecessor': 'K1', 'successor': successor,
         'target_exit': 'east', 'signatures': ['teacher', 'duty-east']},
        {'id': 'e-in-east', 'session_id': 'S-C1', 'kind': 'exit_transfer_in',
         'at': '2026-01-01T10:34:30', 'credential_id': successor, 'exit': 'east',
         'signatures': ['duty-east', 'lead-east']},
        {'id': 'e-rel-east', 'session_id': 'S-C1', 'kind': 'pickup_release',
         'at': '2026-01-01T10:35:00', 'credential_id': successor, 'exit': 'east',
         'person_id': 'G-B', 'person_name': 'Parent B', 'id_type': 'national_id',
         'id_number': 'IDB-777'},
    ]
    credentials = credentials + [{'id': successor, 'session_id': 'S-C1', 'serial': serial,
                                  'exit': 'east', 'predecessor': 'K1',
                                  'voids_predecessor': True}]
    return events, credentials


def test_exit_change_requires_double_sign_and_voids_previous_credential(tmp_path):
    events, credentials = _east_transfer(pickup_case()['events'], pickup_case()['credentials'])
    data = pickup_case('delegate', events=events, credentials=credentials)
    db, result = run_case(tmp_path, data)
    assert result['status'] == '允许交接'
    assert result['credential_authorization']['exit'] == 'east'
    assert result['credential_authorization']['chain'][0]['from'] == 'K1'
    assert result['credential_authorization']['chain'][0]['voided'] is True


def test_exit_change_without_double_sign_is_pending(tmp_path):
    events, credentials = _east_transfer(pickup_case()['events'], pickup_case()['credentials'])
    for event in events:
        if event['id'] == 'e-out-east':
            event['signatures'] = ['teacher']
    data = pickup_case('delegate', events=events, credentials=credentials)
    db, result = run_case(tmp_path, data)
    assert result['status'] == '待核'
    assert result['reason'] == 'exit_transfer_incomplete'


def test_two_exits_uploading_same_serial_fork_requires_review(tmp_path):
    """East and west each upload a mutually-exclusive successor for serial 1."""
    db = lesson_ledger(tmp_path)
    base = pickup_case()
    east_events, east_creds = _east_transfer(base['events'], base['credentials'],
                                             successor='K2')
    east = pickup_case('delegate', events=east_events, credentials=east_creds)
    first = pickup.run(db, write(tmp_path, 'east.json', east))[0]
    assert first['status'] == '允许交接'

    west_events, west_creds = _east_transfer(base['events'], base['credentials'],
                                             successor='K3')
    for event in west_events:
        if event['id'] == 'e-out-east':
            event.update(id='e-out-west', target_exit='west',
                         signatures=['teacher', 'duty-west'])
        elif event['id'] == 'e-in-east':
            event.update(id='e-in-west', exit='west', signatures=['duty-west', 'lead-west'])
        elif event['id'] == 'e-rel-east':
            event.update(id='e-rel-west', exit='west')
    west_creds[-1].update(id='K3', exit='west')
    west = pickup_case('delegate', events=west_events, credentials=west_creds)
    west['version'] = 'v2'
    result = pickup.run(db, write(tmp_path, 'west.json', west))[0]

    assert result['status'] == '待核'
    assert result['reason'] == 'credential_fork'
    assert result['safe_delivery'] is False
    # The fork lists both origins; arrival order does not pick a winner.
    kinds = {fork['kind'] for fork in result['forks']}
    assert 'mutually_exclusive_successors' in kinds
    fork = next(fork for fork in result['forks']
                if fork['kind'] == 'mutually_exclusive_successors')
    assert fork['predecessor'] == 'K1'
    assert {successor['exit'] for successor in fork['successors']} == {'east', 'west'}
    assert {successor['credential'] for successor in fork['successors']} == {'K2', 'K3'}
    assert all(successor.get('source_digest') for successor in fork['successors'])
    # Delivery already happened at the first exit and is still on file.
    assert result['delivered'] is True
    assert result['current_conclusion'] == '已发生交付，当前结论待核'


def test_same_ticket_scanned_at_two_exits_is_a_fork_not_two_authorizations(tmp_path):
    db = lesson_ledger(tmp_path)
    base = pickup_case()
    pickup.run(db, write(tmp_path, 'case.json', base))
    replay = pickup_case()
    replay['version'] = 'v2'
    replay['events'] = [dict(e) for e in replay['events'] if e['id'] == 'e-rel']
    replay['events'][0].update(id='e-rel-west', exit='west')
    result = pickup.run(db, write(tmp_path, 'replay.json', replay))[0]
    assert result['status'] == '待核'
    assert result['reason'] == 'credential_fork'
    assert result['safe_delivery'] is False


def test_replay_after_complete_delivery_does_not_create_a_second_release(tmp_path):
    db = lesson_ledger(tmp_path)
    first = pickup.run(db, write(tmp_path, 'case.json', pickup_case()))[0]
    assert first['delivered'] is True
    replay = pickup_case()
    replay['version'] = 'v2'
    replay['events'] = [dict(e) for e in replay['events'] if e['id'] == 'e-rel']
    replay['events'][0]['id'] = 'e-rel-replay'
    result = pickup.run(db, write(tmp_path, 'replay.json', replay))[0]
    assert len(connect(db).execute('SELECT * FROM pickup_delivery').fetchall()) == 1
    assert result['delivered'] is True
    assert 'e-rel-replay' in result['replay_of']


def test_same_source_digest_with_different_content_is_pending(tmp_path):
    db = lesson_ledger(tmp_path)
    first = pickup_case('delegate')
    first['contracts'][0]['source_digest'] = 'shared-source'
    assert pickup.run(db, write(tmp_path, 'first.json', first))[0]['status'] == '允许交接'
    second = pickup_case('guardian_only')
    second['version'] = 'v2'
    second['contracts'][0]['source_digest'] = 'shared-source'
    result = pickup.run(db, write(tmp_path, 'second.json', second))[0]
    assert result['status'] == '待核'
    assert result['reason'] == 'source_content_conflict'
    assert result['source_conflicts']


def test_offline_files_merge_into_one_custody_chain(tmp_path):
    """Two exits upload disjoint offline files; together they prove delivery."""
    db = lesson_ledger(tmp_path)
    base = pickup_case()
    east = {**base, 'events': [e for e in base['events'] if e['id'] != 'e-rel']}
    west = {**base, 'version': 'v2',
            'events': [e for e in base['events'] if e['id'] == 'e-rel']}
    result = pickup.run_many(db, [write(tmp_path, 'east.json', east),
                                  write(tmp_path, 'west.json', west)])[0]
    assert result['status'] == '允许交接'
    assert result['delivered'] is True
    assert result['custody']['checks']['distinct_actions'] is True


def test_exit_reassignment_to_another_exit_transfers_custody(tmp_path):
    data = pickup_case('delegate')
    data['events'] = [e for e in data['events'] if e['id'] not in ('e-recv', 'e-rel')]
    data['events'] += [
        {'id': 'e-out', 'session_id': 'S-C1', 'kind': 'exit_transfer_out',
         'at': '2026-01-01T10:32:00', 'predecessor': 'K1', 'successor': 'K2',
         'target_exit': 'west', 'signatures': ['duty-east', 'lead-east']},
        {'id': 'e-in', 'session_id': 'S-C1', 'kind': 'exit_transfer_in',
         'at': '2026-01-01T10:33:00', 'credential_id': 'K2', 'exit': 'west',
         'signatures': ['duty-west', 'lead-west']},
        {'id': 'e-recv', 'session_id': 'S-C1', 'kind': 'exit_receipt',
         'at': '2026-01-01T10:34:00', 'credential_id': 'K2', 'exit': 'west'},
        {'id': 'e-rel', 'session_id': 'S-C1', 'kind': 'pickup_release',
         'at': '2026-01-01T10:35:00', 'credential_id': 'K2', 'exit': 'west',
         'person_id': 'G-B', 'person_name': 'Parent B', 'id_type': 'national_id',
         'id_number': 'IDB-777'}]
    data['credentials'].append({'id': 'K2', 'session_id': 'S-C1', 'serial': 2,
                                'exit': 'west', 'predecessor': 'K1',
                                'voids_predecessor': True})
    db, result = run_case(tmp_path, data)
    assert result['status'] == '允许交接'
    assert result['delivered_exit'] == 'west'
    assert result['credential_authorization']['assigned_exit'] == 'east'


def test_verified_and_released_documents_must_agree(tmp_path):
    data = pickup_case('delegate')
    for event in data['events']:
        if event['id'] == 'e-rel':
            event['id_number'] = 'IDB-999'
    db, result = run_case(tmp_path, data)
    assert result['status'] == '待核'
    assert result['reason'] == 'identity_document_conflict'
    assert result['delivered'] is False


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


def test_report_lists_chain_windows_successors_and_delivery(tmp_path):
    db, result = run_case(tmp_path, pickup_case('delegate'))
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(view['pickup']) == 1
    student = view['pickup'][0]
    assert student['student_id'] == 'C1'
    assert student['window']['lesson_end'] == '2026-01-01T10:30:00'
    assert student['window']['notice_id'] == 'N1'
    kinds = [item['kind'] for item in student['authorization_chain']]
    assert 'contract' in kinds and 'delegation' in kinds
    assert student['credentials'][0]['id'] == 'K1'
    assert student['credentials'][0]['successors'] == []
    assert student['custody']['checks']['distinct_actions'] is True
    assert student['delivery'] is not None
    assert student['decision'] == '允许交接'
    # The legacy name-only attempt table is not used to prove anything.
    assert view['entities']['pickup_attempt'][0]['status'] == '允许交接'


def test_report_keeps_old_documents_after_timetable_correction(tmp_path):
    db = lesson_ledger(tmp_path, end='10:30')
    pickup.run(db, write(tmp_path, 'case.json', pickup_case('delegate')))
    operations.first(db, write(tmp_path, 'rooms-v2.json', {'version': 'v2', 'rows': [
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': '2026-01-01T11:00:00',
         'capacity': 35, 'enrolled': 30}]}))
    pickup.evaluate(db)
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    student = view['pickup'][0]
    assert student['window']['lesson_end'] == '2026-01-01T11:00:00'
    assert student['delivered'] is True
    assert student['safe_delivery'] is False
    history = view['entities']['entity_history']
    assert any(row['entity_id'] == 'L1' and row['version'] == 'v1' for row in history)


# ---------------------------------------------------------------------------
# legacy name-only intake
# ---------------------------------------------------------------------------


def test_name_only_roster_cannot_prove_authorization(tmp_path):
    db_path = tmp_path / 'ledger.db'
    fixture = ROOT / 'fixtures' / 'pickup-ok.json'
    operations.first(db_path, write(tmp_path, 'rooms.json', {'version': 'v1', 'rows': [
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': '2026-01-01T10:30:00',
         'capacity': 35, 'enrolled': 30}]}))
    operations.second(db_path, write(tmp_path, 'staff.json', {'version': 'v1', 'rows': [
        {'id': 'S1', 'lesson': 'L1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True}]}))
    operations.third(db_path, write(tmp_path, 'notice.json', {'version': 'v1', 'rows': [
        {'id': 'N1', 'lesson': 'L1', 'required_classes': ['C1'],
         'receipts': [{'class': 'C1', 'version': 'v1'}]}]}))
    assert pickup.run(db_path, fixture)[0]['status'] == '允许交接'
    assert pickup.run(db_path, fixture)[0]['status'] == '允许交接'
    view = json.loads(report.build(db_path, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(view['entities']['pickup_attempt']) == 1


def test_legacy_name_only_file_is_pending(tmp_path):
    db_path = tmp_path / 'pickup.sqlite'
    data = {'version': 'v1', 'rows': [{'id': 'pickup-2', 'child_id': 'C1',
            'authorized_names': ['Parent A'], 'person_name': 'Parent A',
            'at': '2026-01-01T10:00:00'}]}
    result = pickup.run(db_path, write(tmp_path, 'legacy.json', data))[0]
    assert result['status'] == '待核'
    assert result['reason'] == 'insufficient_identity_evidence'
    assert connect(db_path).execute('SELECT COUNT(*) FROM pickup_attempt').fetchone()[0] == 1
