"""Pickup authorization, exit custody, and offline handover evidence.

A pickup case is a bundle of evidence for one dismissal window of one student:

* guardians and the pickup contract signed in advance;
* temporary delegations (issuer, delegate, validity window) and revocations;
* the timetable-derived pickup window and the class notice edition;
* the one exit pre-assigned for this dismissal and its numbered paper credential;
* offline actions (identity check, teacher handover, exit receipt, release,
  exit transfer) uploaded by each exit.

The module records every fact immutably and derives a current conclusion.
Replaying an already-used credential never creates a second delivery, and a
late revocation never rewrites a delivery that had already happened.
"""

from datetime import datetime, timezone
import hashlib
import json

from .ingest import capture
from .store import connect, decide, entity, entities, insert_once, issue, save, upsert

ALLOW = '允许交接'
PENDING = '待核'
REJECT = '应拒绝'

CONTRACT_ONLY_GUARDIAN = {'guardian_only', 'registered_guardian_only', 'only_guardian',
                          'guardian', '仅监护人', '监护人本人', '本人接领'}
CONTRACT_ALLOW_DELEGATE = {'delegate', 'delegation', 'designated', 'designated_delegate',
                           'allow_delegate', 'delegate_allowed', '临时代接', '可委托'}

TEACHER_KINDS = {'teacher_handover', 'teacher_release', 'teacher_handed_over'}
RECEIPT_KINDS = {'exit_receipt', 'exit_received', 'duty_receipt'}
RELEASE_KINDS = {'pickup_release', 'person_takeover', 'release', 'handover_release'}
TRANSFER_OUT_KINDS = {'exit_transfer_out', 'double_sign_out'}
TRANSFER_IN_KINDS = {'exit_transfer_in', 'double_sign_in'}
VOID_KINDS = {'credential_void', 'void_credential'}


def parse_time(value):
    if value in (None, ''):
        return None
    text = str(value).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def version_key(value):
    """Natural ordering for version labels such as v9 < v10."""
    import re
    text = str(value or '')
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', text)]


def _stamp(item):
    """Every evidence item carries the digest of the bytes it came from."""
    if not isinstance(item, dict):
        return None
    declared = item.get('source_digest')
    if declared:
        return str(declared)
    return digest(item)


def _text(value):
    return None if value is None else str(value)


def _same_identity(left, right):
    """Identity is (document type, document number), never the display name."""
    if not left or not right:
        return False
    ln = _text(left.get('id_number') or left.get('document_number'))
    rn = _text(right.get('id_number') or right.get('document_number'))
    if not ln or not rn:
        return False
    lt = _text(left.get('id_type') or left.get('document_type') or 'national_id')
    rt = _text(right.get('id_type') or right.get('document_type') or 'national_id')
    return ln == rn and lt == rt


def _identity_of(item):
    if not item:
        return None
    return {'id_type': item.get('id_type') or item.get('document_type') or 'national_id',
            'id_number': item.get('id_number') or item.get('document_number')}


def _allows_delegate(contract):
    mode = str(contract.get('mode') or contract.get('type') or '').lower()
    if mode in CONTRACT_ONLY_GUARDIAN:
        return False
    if mode in CONTRACT_ALLOW_DELEGATE:
        return True
    return bool(contract.get('allows_delegate') or contract.get('allow_delegate'))


def _save_safely(db, table, key, source_id, version, body, status, reason):
    try:
        save(db, table, key, source_id, version, body, status, reason)
        return True
    except ValueError as exc:
        if str(exc) != 'identity_conflict':
            raise
        return False


def _load_case(db, payload, source_id):
    """Persist every case section.  Offline replays are idempotent."""
    state = {'conflicts': []}
    conflict_keys = set()
    sections = {
        'students': 'pickup_student',
        'guardians': 'pickup_guardian',
        'contracts': 'pickup_contract',
        'delegations': 'pickup_delegation',
        'revocations': 'pickup_revocation',
        'sessions': 'pickup_session',
        'credentials': 'pickup_credential',
    }
    for section, table in sections.items():
        for row in payload.get(section, []) or []:
            if not isinstance(row, dict) or row.get('id') in (None, ''):
                continue
            key = str(row['id'])
            body = dict(row)
            body['source_digest'] = _stamp(row)
            body.setdefault('student_id', row.get('student_id'))
            if not _save_safely(db, table, key, source_id, payload['version'], body,
                                '已登记', 'case_evidence'):
                previous = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
                previous_body = json.loads(previous['body']) if previous else {}
                first_source = previous_body.get('source_digest') or previous_body.get('source')
                conflicts_key = (table, key, str(previous['source_id']) if previous else None,
                                 str(source_id))
                if conflicts_key not in conflict_keys:
                    conflict_keys.add(conflicts_key)
                    state['conflicts'].append({
                        'table': table, 'id': key,
                        'first_source': previous['source_id'] if previous else None,
                        'second_source': source_id,
                        'first_digest': previous_body.get('source_digest'),
                        'second_digest': body.get('source_digest')})
                    issue(db, source_id, 'source_content_conflict', f'{table}:{key}')
    for row in payload.get('events', []) or []:
        if not isinstance(row, dict) or row.get('id') in (None, ''):
            continue
        key = str(row['id'])
        body = dict(row)
        body['source_digest'] = _stamp(row)
        old = insert_once(db, 'pickup_action', key, source_id, payload['version'], body,
                          '已采录', 'offline_upload')
        stored = json.loads(old['body'])
        incoming = json.loads(canonical(body))
        stored_content = {k: v for k, v in stored.items() if k != 'source_digest'}
        incoming_content = {k: v for k, v in incoming.items() if k != 'source_digest'}
        if stored_content != incoming_content:
            # Same event id with different content: refuse to let file arrival
            # order decide which one is real.
            insert_once(db, 'pickup_action', f'{key}#conflict:{source_id}', source_id,
                        payload['version'], body, '冲突待核', 'source_content_conflict')
            issue(db, source_id, 'source_content_conflict', f'pickup_action:{key}')
            state['conflicts'].append({'table': 'pickup_action', 'id': key,
                                       'first_source': old['source_id'],
                                       'second_source': source_id})
    return state


def _detect_same_source_conflicts(db, known=None):
    """Same declared source digest with different content is not a replay."""
    seen = {}
    conflicts = list(known or [])
    for table in ('pickup_contract', 'pickup_delegation', 'pickup_revocation',
                  'pickup_credential', 'pickup_action'):
        for row in db.execute(f'SELECT * FROM {table}'):
            if '#' in str(row['id']):
                continue
            body = json.loads(row['body'])
            declared = body.get('source_digest')
            if not declared:
                continue
            content = {k: v for k, v in body.items() if k not in ('source_digest', 'source')}
            previous = seen.get(declared)
            if previous is not None and previous[1] != canonical(content):
                conflicts.append({'source_digest': declared, 'first': previous[0],
                                  'second': f'{table}:{row["id"]}'})
            else:
                seen.setdefault(declared, (f'{table}:{row["id"]}', canonical(content)))
    if conflicts:
        issue(db, None, 'source_content_conflict',
              ','.join(sorted({str(x.get('source_digest') or x) if isinstance(x, dict) else str(x)
                               for x in conflicts})))
    return conflicts


# ---------------------------------------------------------------------------
# timetable / notice window
# ---------------------------------------------------------------------------


def _notice_for(db, lesson_id):
    notices = []
    for row in db.execute('SELECT * FROM notice_batch'):
        body = json.loads(row['body'])
        if str(body.get('lesson')) == str(lesson_id):
            notices.append((row, body))
    return notices


def _window(db, session):
    lesson_id = _text(session.get('lesson_id') or session.get('lesson'))
    class_id = _text(session.get('class_id') or session.get('class') or session.get('student_id'))
    room = db.execute('SELECT * FROM room_slot WHERE id=?', (lesson_id,)).fetchone() if lesson_id else None
    notices = _notice_for(db, lesson_id) if lesson_id else []
    data = {'lesson_id': lesson_id, 'class_id': class_id,
            'schedule_version': None, 'schedule_status': None, 'lesson_end': None,
            'notice_id': None, 'notice_version': None, 'notice_status': None,
            'class_notice_version': None, 'effective': False, 'reason': None,
            'source': 'session_declared'}
    if room:
        body = json.loads(room['body'])
        data['lesson_end'] = body.get('end')
        data['schedule_version'] = room['version']
        data['schedule_status'] = room['status']
        data['source'] = 'desktop_schedule'
        if room['status'] != '可用':
            data['reason'] = 'schedule_unresolved'
            return data
    elif session.get('lesson_end'):
        data['lesson_end'] = session.get('lesson_end')
        data['schedule_status'] = 'declared'
    else:
        data['reason'] = 'schedule_missing'
        return data

    if not notices:
        data['reason'] = 'notice_missing'
        return data
    effective = []
    for row, body in notices:
        class_version = (body.get('class_versions') or {}).get(class_id)
        if row['status'] == '换班生效' and (not data['schedule_version']
                                            or body.get('schedule_version') == data['schedule_version']):
            effective.append((row, body, class_version))
    if effective:
        # Prefer the edition that belongs to the current schedule version, then
        # the most recent declared version/id.  Arrival order is irrelevant.
        effective.sort(key=lambda item: (
            str(item[1].get('schedule_version') or '') == str(data['schedule_version'] or ''),
            version_key(item[0]['version']), str(item[0]['id'])))
        row, body, class_version = effective[-1]
        data.update(notice_id=row['id'], notice_version=row['version'],
                    notice_status=row['status'], class_notice_version=class_version)
        if class_version != row['version']:
            data['reason'] = 'class_notice_stale'
        else:
            data['effective'] = True
            data['reason'] = None
        return data
    notices.sort(key=lambda item: (version_key(item[0]['version']), str(item[0]['id'])))
    row, body = notices[-1]
    data.update(notice_id=row['id'], notice_version=row['version'],
                notice_status=row['status'],
                class_notice_version=(body.get('class_versions') or {}).get(class_id))
    if data['schedule_version'] and body.get('schedule_version') != data['schedule_version']:
        data['reason'] = 'schedule_revised'
    elif data['schedule_version'] and body.get('schedule_version') is None:
        data['reason'] = 'schedule_revised'
    elif row['status'] != '换班生效':
        data['reason'] = row['reason'] or 'notice_not_effective'
    elif (body.get('class_versions') or {}).get(class_id) != row['version']:
        data['reason'] = 'class_notice_stale'
    else:
        data['reason'] = 'notice_not_effective'
    return data


# ---------------------------------------------------------------------------
# authorization chain
# ---------------------------------------------------------------------------


def _contracts_for(db, student_id, contract_id=None):
    contracts = []
    for contract in entities(db, 'pickup_contract'):
        if contract_id and str(contract['id']) != str(contract_id):
            continue
        if contract_id:
            contracts.append(contract)
            continue
        applies = str(contract.get('student_id')) == str(student_id)
        if contract.get('scope') is not None:
            applies = applies or str(student_id) in {str(x) for x in contract['scope']}
        if applies:
            contracts.append(contract)
    return contracts


def _delegations_for(db, student_id):
    return [d for d in entities(db, 'pickup_delegation')
            if str(d.get('student_id')) == str(student_id)]


def _revocations_for(db, delegation_id):
    return [r for r in entities(db, 'pickup_revocation')
            if str(r.get('delegation_id')) == str(delegation_id)]


def _claimant(session, release, identity_event):
    release = release or {}
    identity_event = identity_event or {}
    source = release or identity_event
    claimant = {
        'person_id': _text(source.get('person_id') or source.get('delegate_id') or
                           identity_event.get('person_id')),
        'person_name': source.get('person_name') or source.get('name') or identity_event.get('person_name'),
        'id_type': source.get('id_type') or source.get('document_type') or
                   identity_event.get('id_type') or identity_event.get('document_type'),
        'id_number': source.get('id_number') or source.get('document_number') or
                     identity_event.get('id_number') or identity_event.get('document_number'),
        'at': source.get('at') or session.get('pickup_at'),
    }
    # The person the desk verified and the person who took the child must be
    # the same document.  Disagreeing records are a conflict, not a pick-one.
    if release and identity_event:
        presented = _identity_of(identity_event)
        released = _identity_of(release)
        if presented and released and presented.get('id_number') and released.get('id_number') \
                and not _same_identity(presented, released):
            claimant['document_conflict'] = {'verified': presented, 'released': released}
    return claimant


def _identity_verdict(claimant, expected, role):
    if expected is None:
        return REJECT, 'no_valid_authorization', []
    findings = []
    if not claimant.get('id_number'):
        return PENDING, 'identity_unverified', ['identity_document_missing']
    if _same_identity(claimant, expected):
        if claimant.get('person_id') and str(claimant['person_id']) != str(expected.get('id')):
            findings.append('identity_number_matches_but_subject_id_differs')
        return ALLOW, 'identity_verified', findings
    same_name = (claimant.get('person_name') and expected.get('name')
                 and str(claimant['person_name']) == str(expected.get('name')))
    if same_name:
        findings.append('same_name_different_document')
        return REJECT, 'identity_mismatch', findings
    findings.append('document_not_registered')
    return REJECT, 'identity_mismatch', findings


def _choose_contract(db, session, student_id, claimant, attempt_at):
    """Lock the contract that was in force; never pick a friendlier one later."""
    candidates = _contracts_for(db, student_id)
    explicit = session.get('contract_id')
    if explicit:
        locked = [c for c in candidates if str(c['id']) == str(explicit)]
        if not locked:
            return None, 'contract_missing', []
        return locked[0], 'contract_locked_by_session', []
    if len({str(c['id']) for c in candidates}) > 1:
        # More than one contract exists for the student.  Picking the one that
        # happens to authorize today's claimant is exactly the "switch after
        # the fact" failure; the session must name the contract it was opened
        # under.
        return None, 'contract_not_locked', []
    if len(candidates) == 1:
        return candidates[0], 'single_contract', []
    return None, 'contract_missing', []


def _delegate_identity(delegation):
    if delegation.get('delegate'):
        return delegation['delegate']
    if delegation.get('delegate_id_number') or delegation.get('delegate_id_type'):
        return {'id': delegation.get('delegate_id'), 'name': delegation.get('delegate_name'),
                'id_type': delegation.get('delegate_id_type'),
                'id_number': delegation.get('delegate_id_number')}
    return None


def _authorize(db, session, student_id, claimant, attempt_at, window):
    result = {'status': PENDING, 'reason': 'authorization_incomplete', 'reasons': [],
              'contract': None, 'delegation': None, 'revocations': [],
              'claimant': claimant, 'identity': None, 'late_revocations': []}
    if claimant.get('document_conflict'):
        result.update(status=PENDING, reason='identity_document_conflict')
        result['reasons'].append('identity_document_conflict')
        return result
    contract, reason, _ = _choose_contract(db, session, student_id, claimant, attempt_at)
    if not contract:
        result.update(status=REJECT, reason=reason)
        result['reasons'].append(reason)
        return result
    result['contract'] = contract
    result['reasons'].append('contract_selected')
    signed = parse_time(contract.get('signed_at') or contract.get('issued_at'))
    if signed is None:
        result.update(status=PENDING, reason='contract_issued_at_missing')
        return result
    if attempt_at and signed > attempt_at:
        result.update(status=REJECT, reason='contract_signed_after_pickup')
        result['reasons'].append('contract_signed_after_pickup')
        return result
    valid_from = parse_time(contract.get('effective_from'))
    valid_to = parse_time(contract.get('effective_to'))
    if valid_from and attempt_at and attempt_at < valid_from:
        result.update(status=REJECT, reason='contract_not_yet_effective')
        return result
    if valid_to and attempt_at and attempt_at > valid_to:
        result.update(status=REJECT, reason='contract_expired')
        return result
    if contract.get('scope') is not None and str(student_id) not in {str(x) for x in contract['scope']}:
        result.update(status=REJECT, reason='contract_out_of_scope')
        return result

    guardian = entity(db, 'pickup_guardian', str(contract.get('guardian_id')))
    if not guardian:
        result.update(status=REJECT, reason='registered_guardian_missing')
        return result
    student = entity(db, 'pickup_student', str(student_id))
    guardian_scope = guardian.get('student_ids')
    if guardian_scope is not None and str(student_id) not in {str(x) for x in guardian_scope}:
        result.update(status=REJECT, reason='guardian_not_registered_for_student')
        return result
    if student and student.get('guardian_id') and str(student['guardian_id']) != str(guardian['id']):
        result.update(status=REJECT, reason='guardian_not_registered_for_student')
        return result

    if _same_identity(claimant, guardian):
        identity_status, identity_reason, findings = _identity_verdict(claimant, guardian, 'guardian')
        result['identity'] = {'role': 'guardian', 'status': identity_status,
                              'reason': identity_reason, 'findings': findings,
                              'registered': _identity_of(guardian), 'presented': _identity_of(claimant)}
        result['status'] = ALLOW if identity_status == ALLOW else identity_status
        result['reason'] = identity_reason
        result['reasons'].extend(findings or [])
        return result

    if not _allows_delegate(contract):
        result.update(status=REJECT, reason='contract_guardian_only')
        result['reasons'].append('contract_guardian_only')
        result['identity'] = {'role': 'delegate_attempt', 'status': REJECT,
                              'reason': 'contract_guardian_only',
                              'presented': _identity_of(claimant)}
        return result

    if not claimant.get('id_number'):
        result.update(status=PENDING, reason='identity_unverified')
        result['reasons'].append('identity_document_missing')
        return result

    candidates = []
    for delegation in _delegations_for(db, student_id):
        if str(delegation.get('contract_id')) != str(contract['id']):
            continue
        delegate = _delegate_identity(delegation)
        if delegate and _same_identity(claimant, delegate):
            candidates.append(delegation)
        elif (_text(delegation.get('delegate_id')) and claimant.get('person_id')
              and str(delegation.get('delegate_id')) == str(claimant.get('person_id'))):
            candidates.append(delegation)
    if not candidates:
        names = {str(d.get('delegate_name') or (d.get('delegate') or {}).get('name'))
                 for d in _delegations_for(db, student_id)}
        if claimant.get('person_name') and str(claimant['person_name']) in names:
            result.update(status=REJECT, reason='identity_mismatch')
            result['reasons'].append('same_name_different_document')
        else:
            result.update(status=REJECT, reason='no_valid_delegation')
            result['reasons'].append('no_valid_delegation')
        return result
    if len({str(d['id']) for d in candidates}) > 1:
        result.update(status=PENDING, reason='ambiguous_delegation')
        return result
    delegation = candidates[0]
    result['delegation'] = delegation
    delegate = _delegate_identity(delegation)
    identity_status, identity_reason, findings = _identity_verdict(claimant, delegate, 'delegate')
    expected = dict(delegate, id=delegation.get('delegate_id'))
    result['identity'] = {'role': 'delegate', 'status': identity_status,
                          'reason': identity_reason, 'findings': findings,
                          'registered': _identity_of(expected), 'presented': _identity_of(claimant)}
    if identity_status != ALLOW:
        result.update(status=identity_status, reason=identity_reason)
        result['reasons'].extend(findings)
        return result

    issued = parse_time(delegation.get('issued_at'))
    if issued is None:
        result.update(status=PENDING, reason='delegation_issued_at_missing')
        return result
    if attempt_at and issued > attempt_at:
        result.update(status=REJECT, reason='delegation_issued_after_pickup')
        return result

    issuer = str(delegation.get('issuer_id') or delegation.get('authorizer_id') or '')
    if issuer != str(contract.get('guardian_id')):
        # A delegate may only re-delegate when the contract says so and a
        # parent delegation is itself valid for this student.
        if not contract.get('sub_delegation_allowed'):
            result.update(status=REJECT, reason='delegation_out_of_scope')
            result['reasons'].append('delegation_issuer_not_guardian')
            return result
        parent_id = delegation.get('parent_delegation_id')
        parent = next((d for d in _delegations_for(db, student_id)
                       if str(d['id']) == str(parent_id)), None)
        parent_issuer = _text((parent or {}).get('issuer_id'))
        if not parent or parent_issuer != str(contract.get('guardian_id')):
            result.update(status=REJECT, reason='delegation_out_of_scope')
            result['reasons'].append('re_delegation_out_of_scope')
            return result
        if parent.get('scope') is not None and str(student_id) not in {str(x) for x in parent['scope']}:
            result.update(status=REJECT, reason='delegation_out_of_scope')
            result['reasons'].append('re_delegation_out_of_scope')
            return result
        if parse_time(parent.get('issued_at')) is None:
            result.update(status=PENDING, reason='delegation_issued_at_missing')
            return result
        if attempt_at and parse_time(parent.get('issued_at')) > attempt_at:
            result.update(status=REJECT, reason='delegation_issued_after_pickup')
            return result

    valid_from = parse_time(delegation.get('valid_from') or delegation.get('from'))
    valid_to = parse_time(delegation.get('valid_to') or delegation.get('to'))
    if delegation.get('scope') is not None and str(student_id) not in {str(x) for x in delegation['scope']}:
        result.update(status=REJECT, reason='delegation_out_of_scope')
        return result
    if valid_from is None or valid_to is None:
        result.update(status=PENDING, reason='delegation_window_missing')
        return result
    if attempt_at and attempt_at < valid_from:
        result.update(status=REJECT, reason='before_delegation_window')
        return result
    if attempt_at and attempt_at > valid_to:
        result.update(status=REJECT, reason='outside_delegation_window')
        result['reasons'].append('delegation_valid_to_exceeded')
        return result

    revoked = False
    for revocation in _revocations_for(db, delegation['id']):
        result['revocations'].append(revocation)
        issued_at = parse_time(revocation.get('issued_at') or revocation.get('at'))
        effective_at = parse_time(revocation.get('effective_at') or revocation.get('issued_at')
                                  or revocation.get('at'))
        if issued_at is None:
            result['late_revocations'].append(revocation)
            continue
        if attempt_at and issued_at > attempt_at:
            result['late_revocations'].append(revocation)
            continue
        if effective_at is None:
            result['late_revocations'].append(revocation)
            continue
        if attempt_at and effective_at <= attempt_at:
            revoked = True
        elif attempt_at is None:
            revoked = True
    if revoked:
        result.update(status=REJECT, reason='delegation_revoked_before_pickup')
        result['reasons'].append('revocation_effective_before_pickup')
        return result

    result.update(status=ALLOW, reason='delegation_valid')
    result['reasons'].append('delegation_verified')
    return result


# ---------------------------------------------------------------------------
# exit credential chain and custody actions
# ---------------------------------------------------------------------------

def _event_kind(event):
    return str(event.get('kind') or event.get('action') or '')


def _signatures(event):
    signatures = event.get('signatures') or event.get('signed_by') or []
    if isinstance(signatures, str):
        signatures = [signatures]
    return {str(x) for x in signatures if x not in (None, '')}


def _credential_map(db, session_id):
    creds = [c for c in entities(db, 'pickup_credential')
             if str(c.get('session_id')) == str(session_id)]
    return {str(c['id']): c for c in creds}


def _successors(db, session_id):
    """Credential successor graph built from paper serials and transfer actions."""
    creds = _credential_map(db, session_id)
    successors = {}
    for cred in creds.values():
        pred = cred.get('predecessor') or cred.get('previous_credential_id')
        if pred:
            successors.setdefault(str(pred), {})[str(cred['id'])] = {
                'credential': str(cred['id']), 'serial': cred.get('serial'),
                'exit': cred.get('exit'), 'source_digest': cred.get('source_digest'),
                'source': f"pickup_credential:{cred['id']}"}
    for event in _events(db, session_id):
        kind = _event_kind(event)
        pred = event.get('predecessor') or event.get('previous_credential_id')
        succ = event.get('successor') or event.get('next_credential_id') or event.get('credential_id')
        if kind in TRANSFER_OUT_KINDS and pred and succ and str(pred) != str(succ):
            # A paper hand-over may appear as a join of exit-transfer records
            # that name the successor only through the new credential row.
            for value in (succ, event.get('credential_id')):
                if not value:
                    continue
                successors.setdefault(str(pred), {}).setdefault(str(value), {
                    'credential': str(value), 'exit': event.get('target_exit') or event.get('exit'),
                    'source_digest': event.get('source_digest'), 'source': f"pickup_action:{event['id']}"})
    return successors, creds


def _fork_details(db, session_id):
    successors, creds = _successors(db, session_id)
    forks = []
    for pred, options in successors.items():
        if len(options) > 1:
            forks.append({'kind': 'mutually_exclusive_successors', 'predecessor': pred,
                          'successors': sorted(options.values(), key=lambda x: x['credential']),
                          'sources': sorted({x['source'] for x in options.values()})})
    by_serial = {}
    for cred in creds.values():
        by_serial.setdefault(str(cred.get('serial')), []).append(cred)
    for serial, items in by_serial.items():
        if len({str(x['id']) for x in items}) > 1:
            forks.append({'kind': 'duplicate_serial', 'serial': serial,
                          'successors': [{'credential': x['id'], 'exit': x.get('exit'),
                                          'source_digest': x.get('source_digest'),
                                          'source': f"pickup_credential:{x['id']}"} for x in items],
                          'sources': sorted(f"pickup_credential:{x['id']}" for x in items)})
    releases = [e for e in _events(db, session_id) if _event_kind(e) in RELEASE_KINDS]
    by_ticket = {}
    for event in releases:
        ticket = str(event.get('credential_id') or event.get('token') or event.get('serial') or event['id'])
        by_ticket.setdefault(ticket, []).append(event)
    for ticket, items in by_ticket.items():
        exits = {str(x.get('exit')) for x in items if x.get('exit')}
        distinct = False
        if len(exits) > 1:
            # Two exits may legitimately be named only when the paper
            # credential was transferred with a predecessor/successor relation.
            for first in items:
                for second in items:
                    if first is second:
                        continue
                    if str(first.get('exit')) == str(second.get('exit')):
                        continue
                    if (second.get('predecessor') or second.get('previous_credential_id')):
                        continue
                    distinct = True
        if distinct:
            forks.append({'kind': 'same_ticket_two_exits', 'credential': ticket,
                          'successors': [{'credential': ticket, 'exit': x.get('exit'),
                                          'event': x['id'], 'source_digest': x.get('source_digest')}
                                         for x in items],
                          'sources': sorted(f"pickup_action:{x['id']}" for x in items)})
    return forks, successors, creds


def _events(db, session_id):
    return [e for e in entities(db, 'pickup_action')
            if str(e.get('session_id')) == str(session_id)]


def _base_credential(session, creds):
    explicit = session.get('credential_id')
    if explicit and str(explicit) in creds:
        return creds[str(explicit)]
    roots = [c for c in creds.values() if not (c.get('predecessor') or c.get('previous_credential_id'))]

    def order(cred):
        try:
            serial = int(cred.get('serial'))
        except (TypeError, ValueError):
            serial = 1 << 30
        return serial, str(cred['id'])
    roots.sort(key=order)
    return roots[0] if roots else None


def _credential_authorization(db, session, cred_id, creds, at_time):
    """Walk the pre-assigned credential forward; every transfer must be signed."""
    events = _events(db, session['id'])
    roots = [c for c in creds.values()
             if not (c.get('predecessor') or c.get('previous_credential_id'))]
    if len(roots) > 1:
        # Two exits claim the root paper serial.  Neither is automatically the
        # holder of the delivery right.
        return {'authorized': False, 'exit': session.get('assigned_exit'), 'credential': None,
                'reason': 'credential_fork', 'missing': ['single_root_credential'],
                'chain': [], 'roots': sorted(str(x['id']) for x in roots)}
    base = _base_credential(session, creds)
    if base is None:
        return {'authorized': False, 'exit': session.get('assigned_exit'), 'credential': None,
                'reason': 'credential_missing', 'missing': ['credential'], 'chain': []}
    assigned_exit = session.get('assigned_exit') or base.get('exit')
    if not base.get('serial'):
        return {'authorized': False, 'exit': assigned_exit, 'credential': str(base['id']),
                'reason': 'credential_serial_missing', 'missing': ['serial'], 'chain': []}
    if session.get('assigned_exit') and base.get('exit') \
            and str(base.get('exit')) != str(session['assigned_exit']):
        return {'authorized': False, 'exit': base.get('exit'), 'credential': str(base['id']),
                'reason': 'credential_exit_not_assigned', 'missing': ['assigned_exit'],
                'chain': [], 'assigned_exit': session['assigned_exit']}
    target_id = str(cred_id) if cred_id is not None else str(base['id'])
    if target_id not in creds:
        # A paper serial is the physical identity of a credential.
        serial_holder = None
        for cid, cred in creds.items():
            if str(cred.get('serial')) != target_id:
                continue
            if serial_holder is not None:
                return {'authorized': False, 'exit': session.get('assigned_exit'),
                        'credential': target_id, 'reason': 'duplicate_serial',
                        'missing': ['unique_serial'], 'chain': []}
            serial_holder = cid
        if serial_holder is not None:
            target_id = serial_holder
    chain = []
    target = creds.get(target_id)
    if target is None:
        return {'authorized': False, 'exit': session.get('assigned_exit'), 'credential': target_id,
                'reason': 'credential_unknown', 'missing': ['credential'], 'chain': []}
    if target.get('session_id') is not None and str(target.get('session_id')) != str(session['id']):
        return {'authorized': False, 'exit': target.get('exit'), 'credential': target_id,
                'reason': 'credential_other_session', 'missing': ['session_match'], 'chain': []}
    current = target
    guard = 0
    while current is not None and str(current['id']) != str(base['id']) and guard < 20:
        guard += 1
        pred_id = current.get('predecessor') or current.get('previous_credential_id')
        if not pred_id or str(pred_id) not in creds:
            return {'authorized': False, 'exit': current.get('exit'), 'credential': target_id,
                    'reason': 'credential_chain_broken', 'missing': ['predecessor'], 'chain': chain}
        out = [e for e in events if _event_kind(e) in TRANSFER_OUT_KINDS
               and str(e.get('predecessor') or e.get('previous_credential_id')) == str(pred_id)
               and str(e.get('successor') or e.get('next_credential_id') or e.get('credential_id')) == str(current['id'])
               and _exit_matches(e, current)]
        inn = [e for e in events if _event_kind(e) in TRANSFER_IN_KINDS
               and str(e.get('credential_id')) == str(current['id'])
               and _exit_matches(e, current)]
        pred_void_events = [e for e in events if _event_kind(e) in VOID_KINDS
                            and str(e.get('credential_id')) == str(pred_id)]
        predecessor = creds.get(str(pred_id), {})
        voided = bool(pred_void_events) or bool(predecessor.get('voided_at')) \
            or bool(current.get('voids_predecessor'))
        current_void = bool(current.get('voided_at')) or any(
            _event_kind(e) in VOID_KINDS and str(e.get('credential_id')) == str(current['id'])
            for e in events)
        out_ok = [e for e in out if len(_signatures(e)) >= 2]
        in_ok = [e for e in inn if len(_signatures(e)) >= 2]
        chain.append({'from': str(pred_id), 'to': str(current['id']),
                      'exit': current.get('exit'), 'voided': voided,
                      'out_signatures': sorted(_signatures(out[0])) if out else [],
                      'in_signatures': sorted(_signatures(inn[0])) if inn else [],
                      'out_source': out[0].get('source_digest') if out else None,
                      'in_source': inn[0].get('source_digest') if inn else None,
                      'void_source': pred_void_events[0].get('source_digest') if pred_void_events else None})
        if current_void:
            return {'authorized': False, 'exit': current.get('exit'), 'credential': target_id,
                    'reason': 'credential_voided', 'missing': ['valid_credential'], 'chain': chain}
        if not out_ok or not in_ok or not voided:
            return {'authorized': False, 'exit': current.get('exit'), 'credential': target_id,
                    'reason': 'exit_transfer_incomplete', 'chain': chain,
                    'missing': [x for x, ok in (('transfer_out_double_sign', bool(out_ok)),
                                                ('transfer_in_double_sign', bool(in_ok)),
                                                ('previous_credential_voided', voided)) if not ok]}
        current = creds[str(pred_id)]
    base_void = bool(base.get('voided_at')) or any(
        _event_kind(e) in VOID_KINDS and str(e.get('credential_id')) == str(base['id'])
        for e in events)
    if base_void and str(base['id']) == target_id:
        return {'authorized': False, 'exit': base.get('exit'), 'credential': target_id,
                'reason': 'credential_voided', 'missing': ['valid_credential'], 'chain': chain}
    active_exit = target.get('exit') or assigned_exit
    chain_credential_ids = {str(base['id']), target_id} | {
        item['from'] for item in chain} | {item['to'] for item in chain}
    return {'authorized': True, 'exit': active_exit, 'credential': target_id,
            'base_credential': str(base['id']), 'assigned_exit': assigned_exit,
            'chain_credentials': sorted(chain_credential_ids, key=str),
            'reason': 'credential_authorized', 'chain': chain}


def _exit_matches(event, credential):
    """If an event names an exit it must agree with the paper credential."""
    declared = event.get('exit') or event.get('target_exit')
    expected = credential.get('exit')
    if declared is None or expected is None:
        return True
    return str(declared) == str(expected)


def _first_event(events, kinds, credential_id=None, credential_ids=None,
                 before=None, after=None, exit=None):
    allowed = {str(x) for x in (credential_ids or []) if x not in (None, '')}
    matches = []
    for event in events:
        if _event_kind(event) not in kinds:
            continue
        event_cred = event.get('credential_id')
        if credential_id is not None and event_cred is not None \
                and str(event_cred) != str(credential_id):
            continue
        if allowed and event_cred is not None and str(event_cred) not in allowed:
            continue
        if exit is not None and event.get('exit') is not None and str(event.get('exit')) != str(exit):
            continue
        at = parse_time(event.get('at'))
        # Unparseable timestamps must not be allowed to satisfy a time-bound
        # step; they cannot be ordered against the dismissal window.
        if at is None and (before is not None or after is not None):
            continue
        if before is not None and at is not None and at > before:
            continue
        if after is not None and at is not None and at < after:
            continue
        matches.append((at or datetime.min, event))
    matches.sort(key=lambda pair: (pair[0] is None, pair[0]))
    return matches[0][1] if matches else None


def _custody(db, session, credential, release, window_start, exit_id):
    events = _events(db, session['id'])
    release_at = parse_time(release.get('at')) if release else None
    credential_ids = {str(x) for x in (credential.get('chain_credentials')
                                       or [credential.get('credential')])
                      if x not in (None, '')}
    # The teacher's hand-over must precede the final release; when no release
    # has been recorded yet, look across the whole window rather than silently
    # claiming the teacher never handed the child over.
    teacher_before = release_at if release_at is not None else None
    teacher = _first_event(events, TEACHER_KINDS,
                           credential_ids=credential_ids, before=teacher_before)
    if teacher is None:
        # A paper transfer moves custody; after a signed hand-off at the
        # original exit the teacher's action belongs to the base credential.
        teacher = _first_event(events, TEACHER_KINDS, before=teacher_before)
    receipt = _first_event(events, RECEIPT_KINDS,
                           credential_ids=credential_ids,
                           before=release_at, after=parse_time(teacher.get('at')) if teacher else None,
                           exit=exit_id)
    missing = []
    if not teacher:
        missing.append('teacher_handover')
    if not receipt:
        missing.append('exit_receipt')
    if not release:
        missing.append('pickup_release')
    checks = {'teacher_handover': teacher['id'] if teacher else None,
              'exit_receipt': receipt['id'] if receipt else None,
              'pickup_release': release['id'] if release else None,
              'distinct_actions': len({x for x in (teacher['id'] if teacher else None,
                                                   receipt['id'] if receipt else None,
                                                   release['id'] if release else None)
                                       if x}) == 3,
              'missing': missing}
    if missing:
        return {'complete': False, 'reason': 'custody_incomplete', 'missing': missing,
                'checks': checks}
    if not checks['distinct_actions']:
        return {'complete': False, 'reason': 'single_scan_is_not_delivery',
                'missing': ['separate_actions'], 'checks': checks}
    if window_start and parse_time(teacher.get('at')) and parse_time(teacher.get('at')) < window_start:
        return {'complete': False, 'reason': 'custody_before_dismissal',
                'missing': ['teacher_handover_after_dismissal'], 'checks': checks}
    if parse_time(teacher.get('at')) and parse_time(receipt.get('at')) \
            and parse_time(receipt.get('at')) < parse_time(teacher.get('at')):
        return {'complete': False, 'reason': 'custody_order_invalid',
                'missing': ['exit_receipt_after_teacher'], 'checks': checks}
    if parse_time(receipt.get('at')) and release_at and release_at < parse_time(receipt.get('at')):
        return {'complete': False, 'reason': 'custody_order_invalid',
                'missing': ['release_after_exit_receipt'], 'checks': checks}
    return {'complete': True, 'reason': 'three_actions_confirmed', 'missing': [],
            'checks': checks}


# ---------------------------------------------------------------------------
# per-session evaluation
# ---------------------------------------------------------------------------

def _roster_identities(db, student_id, contract):
    """The identity records behind the roster, so names alone prove nothing."""
    out = []
    guardian = entity(db, 'pickup_guardian',
                      str((contract or {}).get('guardian_id')))
    if guardian:
        out.append({'role': 'guardian', 'id': guardian['id'], 'name': guardian.get('name'),
                    'id_type': (_identity_of(guardian) or {}).get('id_type'),
                    'id_number': (_identity_of(guardian) or {}).get('id_number')})
    for delegation in _delegations_for(db, student_id):
        if contract and str(delegation.get('contract_id')) != str(contract['id']):
            continue
        delegate = _delegate_identity(delegation)
        if not delegate:
            continue
        out.append({'role': 'delegate', 'id': delegation.get('delegate_id'),
                    'delegation_id': delegation['id'], 'name': delegate.get('name'),
                    'id_type': (_identity_of(delegate) or {}).get('id_type'),
                    'id_number': (_identity_of(delegate) or {}).get('id_number'),
                    'valid_from': delegation.get('valid_from'),
                    'valid_to': delegation.get('valid_to')})
    return out

def _session_evaluate(db, session, conflicts):
    student_id = str(session.get('student_id'))
    session_id = str(session['id'])
    window = _window(db, session)
    window_start = parse_time(window.get('lesson_end'))
    events = _events(db, session_id)
    releases = sorted([e for e in events if _event_kind(e) in RELEASE_KINDS],
                      key=lambda e: (parse_time(e.get('at')) is None,
                                     parse_time(e.get('at')) or datetime.min))
    identity_events = sorted([e for e in events if _event_kind(e) in ('identity_check', 'id_check')],
                             key=lambda e: (parse_time(e.get('at')) is None,
                                            parse_time(e.get('at')) or datetime.min))
    release = releases[0] if releases else None
    identity_event = identity_events[0] if identity_events else None
    attempt_at = parse_time(release.get('at')) if release else parse_time(session.get('pickup_at'))
    if attempt_at is None:
        attempt_at = parse_time(session.get('at'))
    if attempt_at is None:
        # Without a time the clock-based authorization and custody checks
        # (validity windows, delays, revocations) cannot be proven.
        upsert(db, 'pickup_conclusion', session_id, session.get('_source'),
               session.get('_version') or '', {
                   'student_id': student_id, 'session_id': session_id,
                   'window': window, 'status': PENDING, 'reason': 'pickup_time_missing',
                   'delivered': False, 'safe_delivery': False,
                   'current_conclusion': None}, PENDING, 'pickup_time_missing')
        decide(db, '接领', session_id, PENDING, 'pickup_time_missing', session.get('_source'))
        return {'student_id': student_id, 'session_id': session_id, 'window': window,
                'status': PENDING, 'reason': 'pickup_time_missing',
                'delivered': False, 'safe_delivery': False, 'current_conclusion': None,
                'authorization': None, 'identity_check': None, 'credentials': [],
                'credential_authorization': {}, 'custody': {}, 'forks': [],
                'replay_of': [], 'source_conflicts': conflicts}

    forks, successors, creds = _fork_details(db, session_id)
    seen_forks = set()
    for fork in forks:
        key = f"{session_id}:{fork['kind']}:{fork.get('predecessor') or fork.get('serial') or fork.get('credential')}"
        upsert(db, 'pickup_fork', key, session.get('_source'), session.get('_version') or '',
               fork, '分叉待核', 'mutually_exclusive_successors')
        issue_detail = f"{fork['kind']}:{fork.get('predecessor') or fork.get('serial') or fork.get('credential')}"
        if issue_detail not in seen_forks:
            issue(db, session.get('_source'), 'credential_fork', issue_detail)
            seen_forks.add(issue_detail)

    claimant = _claimant(session, release, identity_event)
    auth = _authorize(db, session, student_id, claimant, attempt_at, window)
    cred_state = _credential_authorization(
        db, session, release.get('credential_id') if release else None, creds, attempt_at)
    exit_id = cred_state.get('exit') or session.get('assigned_exit')
    custody = _custody(db, session, cred_state, release, window_start, exit_id)

    reasons = list(auth['reasons'])
    status, reason = auth['status'], auth['reason']
    if status == ALLOW:
        if not window['effective']:
            status, reason = PENDING, window.get('reason') or 'schedule_not_effective'
            reasons.append(reason)
        elif window_start and attempt_at and attempt_at < window_start:
            status, reason = PENDING, 'before_current_dismissal'
            reasons.append('schedule_delay')
        elif not cred_state.get('authorized'):
            missing = cred_state.get('missing') or [cred_state.get('reason')]
            if cred_state.get('reason') == 'credential_voided':
                status, reason = REJECT, 'credential_voided'
            elif cred_state.get('reason') in ('multiple_assigned_credentials',
                                              'credential_fork',
                                              'credential_exit_not_assigned',
                                              'credential_serial_missing'):
                status, reason = PENDING, cred_state['reason']
            else:
                status, reason = PENDING, cred_state.get('reason') or 'credential_not_authorized'
            reasons.extend(x for x in missing if x)
        elif forks:
            status, reason = PENDING, 'credential_fork'
            reasons.append('credential_fork')
        elif (release and release.get('exit') and cred_state.get('exit')
              and str(release['exit']) != str(cred_state['exit'])):
            # A release at an exit other than the currently authorized one is
            # only allowed after a signed paper transfer that names it.
            status, reason = PENDING, 'exit_not_preassigned'
            reasons.append('exit_not_preassigned')
        elif custody.get('complete'):
            status, reason = ALLOW, 'complete_custody_chain'
        else:
            status, reason = PENDING, custody.get('reason') or 'custody_incomplete'
            reasons.extend(custody.get('missing') or [])

    if conflicts:
        status = PENDING
        reason = 'source_content_conflict'
        reasons.append('source_content_conflict')

    # A delivery is a fact.  It is written once, from the first complete valid
    # chain, and is never overwritten by replays or late documents.
    eligible = (custody.get('complete') and auth['status'] == ALLOW and window['effective']
                and not forks and cred_state.get('authorized')
                and not (window_start and attempt_at and attempt_at < window_start)
                and (not release.get('exit') or not cred_state.get('exit')
                     or str(release['exit']) == str(cred_state['exit'])))
    existing = db.execute('SELECT * FROM pickup_delivery WHERE id=?', (session_id,)).fetchone()
    replay = []
    if eligible:
        delivery = insert_once(db, 'pickup_delivery', session_id, session.get('_source'),
                               session.get('_version') or '', {
                                   'session_id': session_id, 'student_id': student_id,
                                   'credential_id': cred_state.get('credential'),
                                   'exit': exit_id,
                                   'person_id': claimant.get('person_id'),
                                   'person_name': claimant.get('person_name'),
                                   'at': release.get('at') if release else None,
                                   'identity_check': auth.get('identity'),
                                   'actions': custody['checks'],
                                   'window': window,
                               }, '已交付', 'complete_custody_chain')
        original = json.loads(delivery['body'])
        for extra in releases[1:]:
            replay.append(extra['id'])
            same_ticket = (str(extra.get('credential_id') or extra.get('token'))
                           == str(original.get('credential_id')))
            content_same = (_text(extra.get('person_id')) == _text(original.get('person_id'))
                            and _text(extra.get('person_name')) == _text(original.get('person_name'))
                            and _text(extra.get('id_number')) ==
                            _text((original.get('identity_check') or {}).get('presented', {}).get('id_number')))
            if same_ticket and content_same:
                db.execute("UPDATE pickup_action SET status='重复重放',reason='duplicate_after_delivery' WHERE id=?",
                           (extra['id'],))
            else:
                db.execute("UPDATE pickup_action SET status='冲突待核',reason='replay_content_conflict' WHERE id=?",
                           (extra['id'],))
                issue(db, session.get('_source'), 'replay_content_conflict', extra['id'])
                reasons.append('replay_content_conflict')
                if reason != 'credential_fork':
                    status, reason = PENDING, 'replay_content_conflict'
    elif existing:
        original = json.loads(existing['body'])
        for extra in releases[1:]:
            replay.append(extra['id'])
            same_ticket = (str(extra.get('credential_id') or extra.get('token'))
                           == str(original.get('credential_id')))
            content_same = (_text(extra.get('person_id')) == _text(original.get('person_id'))
                            and _text(extra.get('person_name')) == _text(original.get('person_name')))
            if same_ticket and content_same:
                db.execute("UPDATE pickup_action SET status='重复重放',reason='duplicate_after_delivery' WHERE id=?",
                           (extra['id'],))
            else:
                db.execute("UPDATE pickup_action SET status='冲突待核',reason='replay_content_conflict' WHERE id=?",
                           (extra['id'],))
                issue(db, session.get('_source'), 'replay_content_conflict', extra['id'])
                reasons.append('replay_content_conflict')
                if reason != 'credential_fork':
                    status, reason = PENDING, 'replay_content_conflict'

    delivered = existing is not None or eligible
    delivery_row = db.execute('SELECT * FROM pickup_delivery WHERE id=?',
                              (session_id,)).fetchone()
    delivery_id = delivery_row['id'] if delivery_row else None
    # The historical delivery keeps the facts of the first valid chain.
    delivered_facts = json.loads(delivery_row['body']) if delivery_row else {}

    late = auth.get('late_revocations') or []
    if delivered and late and any(parse_time(x.get('issued_at') or x.get('at')) and attempt_at and
                                  parse_time(x.get('issued_at') or x.get('at')) > attempt_at
                                  for x in late):
        reasons.append('late_revocation_after_delivery')

    if delivered and status == ALLOW:
        current = '已发生交付且可证安全'
    elif delivered:
        current = '已发生交付，当前结论待核'
    else:
        current = None

    body = {
        'student_id': student_id, 'session_id': session_id,
        'class_id': window.get('class_id'), 'lesson_id': window.get('lesson_id'),
        'window': window,
        'authorization': {
            'status': auth['status'], 'reason': auth['reason'],
            'claimant': claimant,
            'contract': auth.get('contract'),
            'delegation': auth.get('delegation'),
            'revocations': auth.get('revocations'),
            'late_revocations': late,
            'identity': auth.get('identity'),
        },
        'credentials': [
            {'id': cid, 'serial': cred.get('serial'), 'exit': cred.get('exit'),
             'predecessor': cred.get('predecessor'), 'voided_at': cred.get('voided_at'),
             'status': cred.get('_status'), 'source_digest': cred.get('source_digest'),
             'successors': sorted(successors.get(cid, {}).keys()),
             'successor_sources': [v.get('source') for v in successors.get(cid, {}).values()],
             'successor_details': sorted(successors.get(cid, {}).values(),
                                         key=lambda x: x['credential'])}
            for cid, cred in sorted(creds.items())],
        'credential_authorization': cred_state,
        'identity_check': auth.get('identity'),
        'custody': custody,
        'forks': forks,
        'delivered': delivered, 'delivery_id': delivery_id,
        'delivery_at': delivered_facts.get('at') if delivered else None,
        'delivered_exit': delivered_facts.get('exit') if delivered else None,
        'delivered_person': delivered_facts.get('person_name') if delivered else None,
        'safe_delivery': delivered and status == ALLOW,
        'replay_of': replay,
        'status': status, 'reason': reason, 'reasons': reasons,
        'current_conclusion': current,
        'source_conflicts': conflicts,
    }
    upsert(db, 'pickup_conclusion', session_id, session.get('_source'),
           session.get('_version') or '', body, status, reason)

    authorized_names = []
    for delegation in _delegations_for(db, student_id):
        if delegation.get('delegate_name'):
            authorized_names.append(delegation['delegate_name'])
    guardian = entity(db, 'pickup_guardian',
                      str((auth.get('contract') or {}).get('guardian_id')))
    if guardian and guardian.get('name'):
        authorized_names.append(guardian['name'])
    _save_safely(db, 'pickup_roster', f'{student_id}:{session.get("_version")}',
                 session.get('_source'), session.get('_version') or '', {
                     'student_id': student_id,
                     'authorized_names': sorted(set(authorized_names)),
                     'authorized_identities': _roster_identities(db, student_id,
                                                                 auth.get('contract')),
                     'contract_id': (auth.get('contract') or {}).get('id'),
                     'window': window,
                 }, '已登记', 'authorization_chain')
    upsert(db, 'pickup_attempt', session_id, session.get('_source'),
           session.get('_version') or '', {
               'session_id': session_id, 'student_id': student_id,
               'claimant': claimant, 'at': release.get('at') if release else session.get('pickup_at'),
               'delivered': delivered, 'delivery_id': delivery_id,
               'authorization': auth['status'], 'credential': cred_state.get('credential'),
               'exit': exit_id, 'replay_of': replay,
           }, status, reason)
    decide(db, '接领', session_id, status, reason, session.get('_source'))
    return body


def _evaluate(db, conflicts):
    conclusions = []
    sessions = sorted(entities(db, 'pickup_session'),
                      key=lambda s: (str(s.get('student_id')), str(s['id'])))
    for session in sessions:
        conclusions.append(_session_evaluate(db, session, conflicts))
    return conclusions


def evaluate(path):
    """Re-derive current conclusions from evidence already in the ledger."""
    db = connect(path)
    try:
        with db:
            conflicts = _detect_same_source_conflicts(db)
            return _evaluate(db, conflicts)
    finally:
        db.close()


def _run_files(path, files):
    # Capture each offline file first (its own short transaction), then load
    # them into a single ledger transaction so merging is atomic.
    captured = [(source, payload) for source, payload in
                (capture(path, 'pickup_desk', file) for file in files)]
    loaded = []
    state_conflicts = []
    db = connect(path)
    try:
        with db:
            for source, payload in captured:
                if payload.get('rows') and not any(
                        payload.get(k) for k in ('students', 'guardians', 'contracts',
                                                 'delegations', 'sessions', 'credentials',
                                                 'events')):
                    loaded.append({'legacy': payload, 'source': source})
                    continue
                state = _load_case(db, payload, source)
                state_conflicts.extend(state['conflicts'])
            legacy_results = []
            for item in loaded:
                legacy_results.extend(_legacy_rows(db, item['legacy'], item['source']))
            conflicts = list(state_conflicts)
            for item in _detect_same_source_conflicts(db):
                if item not in conflicts:
                    conflicts.append(item)
            conclusions = _evaluate(db, conflicts)
            return conclusions if conclusions else legacy_results
    finally:
        db.close()


def _legacy_rows(db, payload, source):
    """Old name-only files are kept as evidence, but names cannot authorize."""
    results = []
    for row in payload.get('rows', []):
        child = str(row.get('child_id') or row.get('student_id'))
        person = row.get('person_name')
        if not child or not person:
            raise ValueError('pickup identity required')
        _save_safely(db, 'pickup_roster', f'{child}:{payload["version"]}', source,
                     payload['version'], {'child_id': child,
                                          'authorized_names': list(row.get('authorized_names') or []),
                                          'source': 'legacy_name_roster'},
                     '待核', 'name-only roster cannot prove identity')
        save(db, 'pickup_attempt', str(row['id']), source, payload['version'], row,
             '待核', 'insufficient_identity_evidence')
        results.append(decide(db, 'pickup', str(row['id']), '待核',
                              'insufficient_identity_evidence', source))
    return results


def run(path, file):
    return _run_files(path, [file])


def run_many(path, files):
    return _run_files(path, list(files))
