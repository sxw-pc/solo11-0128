"""Evidence-driven lifecycle of equipment liability cases.

Every entry module (checkout / handoff / return) funnels its captured rows
here. The whole evidence base is re-read from SQLite and the custody engine
re-runs for the affected loans, so late signatures and scans from another
station always produce a new *versioned* decision instead of mutating the one
already issued to a teacher.
"""
from . import custody
from .store import get_row, load_rows, upsert, dumps, basis_hash, record_issue

PERSONAL = custody.PERSONAL
TEAM = custody.TEAM
VALID_AGREEMENTS = {PERSONAL, TEAM}
ACTIVE_LOAN = '已借出'
SETTLED_LOAN = '已结清'


def _body(row):
    return row['body'] if isinstance(row['body'], dict) else __import__('json').loads(row['body'])


def analyze_loan(db, loan):
    """Reconstruct every serial verdict for one loan from stored evidence."""
    body = _body(loan)
    loan_id = str(body['id'])
    handoffs = [r for r in load_rows(db, 'equipment_handoff') if str(_body(r).get('loan')) == loan_id]
    scans = scans_for_loan(db, loan_id)
    events, excluded = custody.build_events(loan, handoffs, scans)
    agreement = body.get('agreement', PERSONAL)
    team = body.get('team') or [body['borrower']]
    skew = int(body.get('clock_skew_seconds') or custody.DEFAULT_SKEW_SECONDS)
    verdicts = {}
    for serial in body['serials']:
        verdicts[serial] = custody.analyze_serial(
            loan, serial, events[serial], excluded, skew, agreement, team)
    return verdicts, events


def _return_scans(db):
    return [r for r in load_rows(db, 'equipment_scan')
            if _body(r).get('kind', 'return') == 'return']


def assign_return_scans(db):
    """Resolve which outstanding loan each return scan settles.

    A physical serial can settle at most one loan. Scans are classified first:
    a scan whose explicit loan link matches a kit containing the serial (or an
    unlinked scan whose serial has a unique owner) is *consistent*; a link
    pointing at a kit that does not contain the serial is a mix-in and never
    settles anything. Consistent scans win over mix-ins, and two conflicting
    consistent owners for one serial block both and ask for review.
    Returns (serial -> loan_id, evidence list for the report).
    """
    # ownership comes from the recorded loans themselves, independent of the
    # derived open/settled status, to avoid a circular dependency
    owners = {}
    for loan in load_rows(db, 'equipment_loan'):
        for serial in _body(loan).get('serials', []):
            owners.setdefault(serial, set()).add(str(_body(loan)['id']))
    evidence = []
    consistent = {}   # serial -> set of loan ids with a matching scan
    for scan in _return_scans(db):
        sb = _body(scan)
        linked = str(sb['loan']) if sb.get('loan') is not None else None
        for serial in sb.get('serials', []):
            possible = owners.get(serial, set())
            if linked and linked in possible:
                consistent.setdefault(serial, set()).add(linked)
            elif linked and not possible:
                evidence.append({'serial': serial, 'scan': sb.get('id'), 'linked_loan': linked,
                                 'kind': 'foreign_serial_no_open_loan',
                                 'why': '该序列号不属于任何借用单，疑似同型号替代件'})
            elif linked and possible and linked not in possible:
                # belongs to another kit but dropped into this box
                evidence.append({'serial': serial, 'scan': sb.get('id'), 'linked_loan': linked,
                                 'owner_loans': sorted(possible),
                                 'kind': 'foreign_serial_mixed_kit',
                                 'why': f'该序列号属于借用单 {sorted(possible)}，混入 {linked} 归还盒，'
                                        f'不能据此结清原借用单'})
            elif not linked and len(possible) == 1:
                consistent.setdefault(serial, set()).add(next(iter(possible)))
            else:
                evidence.append({'serial': serial, 'scan': sb.get('id'), 'linked_loan': linked,
                                 'candidate_loans': sorted(possible),
                                 'kind': 'ambiguous_scan_owner',
                                 'why': '多套借用单均含该序列号，需站点签收区分'})
    assignment = {}
    for serial, loan_ids in consistent.items():
        if len(loan_ids) == 1:
            assignment[serial] = next(iter(loan_ids))
        else:
            evidence.append({'serial': serial, 'candidate_loans': sorted(loan_ids),
                             'kind': 'double_settlement_blocked',
                             'why': '同一序列号的一致性扫码指向多份借用，不能同时结清，需站点签收区分'})
    return assignment, evidence


def _consistent_scan_keys(db, loan_id):
    """Scan refs that genuinely settle this loan, excluding foreign mix-ins."""
    loan_id = str(loan_id)
    keys = set()
    for scan in _return_scans(db):
        sb = _body(scan)
        linked = str(sb['loan']) if sb.get('loan') is not None else None
        for serial in sb.get('serials', []):
            if linked == loan_id and serial in _loan_serials(db, loan_id):
                keys.add((sb.get('id'), serial))
    return keys


def _loan_serials(db, loan_id):
    for loan in load_rows(db, 'equipment_loan'):
        if str(_body(loan)['id']) == str(loan_id):
            return set(_body(loan).get('serials', []))
    return set()


def scans_for_loan(db, loan_id):
    """Return scans that actually settle this loan (cross-kit safe).

    A scan row may bundle several serials; only consistent serials — those
    whose scan is explicitly linked to this loan and belong to its kit — are
    kept. A foreign item mixed into another kit's box (linked elsewhere) never
    enters this loan's custody chain even though the assignment resolves it.
    """
    loan_id = str(loan_id)
    assignment, _ = assign_return_scans(db)
    consistent = _consistent_scan_keys(db, loan_id)
    wanted = {s for s, lid in assignment.items() if lid == loan_id}
    rows = []
    for scan in _return_scans(db):
        sb = _body(scan)
        kept = [s for s in sb.get('serials', [])
                if s in wanted and assignment.get(s) == loan_id
                and (sb.get('id'), s) in consistent]
        if kept:
            row = dict(scan)
            row['body'] = dict(sb, serials=kept)
            rows.append(row)
    return rows


def settle_returns(db, affected_loan_ids):
    """Cross-kit reconciliation: match each return scan to exactly one open loan.

    A serial found inside another kit's return box is checked against every
    outstanding loan that lists it; the same scan can never close two loans.
    """
    assignment, evidence = assign_return_scans(db)
    notes = []
    affected = {str(x) for x in affected_loan_ids}
    foreign_by_scan = {}
    for item in evidence:
        foreign_by_scan.setdefault(item['serial'], item)
    for loan in load_rows(db, 'equipment_loan'):
        lb = _body(loan)
        loan_id = str(lb['id'])
        if loan_id not in affected and not any(assignment.get(s) == loan_id
                                               for s in lb.get('serials', [])):
            continue
        returned = sorted(s for s in lb['serials'] if assignment.get(s) == loan_id)
        missing = sorted(s for s in lb['serials'] if assignment.get(s) != loan_id)
        foreign = [v for s, v in foreign_by_scan.items()
                   if v.get('linked_loan') == loan_id and s not in lb['serials']]
        if returned and not missing:
            db.execute("UPDATE equipment_loan SET status=?, reason=? WHERE id=?",
                       (SETTLED_LOAN, 'all_serials_independently_returned', loan['id']))
        elif returned:
            db.execute("UPDATE equipment_loan SET status=?, reason=? WHERE id=?",
                       (ACTIVE_LOAN, 'partial_return_pending_liability', loan['id']))
        notes.append({'loan_id': loan_id, 'returned': returned, 'missing': missing,
                      'foreign_serials': foreign, 'evidence': evidence})
    return notes


def _case_exists(db, loan_id, serial):
    return get_row(db, 'liability_case', _case_key(loan_id, serial)) is not None


def reanalyze(db, affected_loan_ids, source_id, version):
    """Re-run verdicts and evolve cases/notices/adjustments for affected loans.

    A missing-item case can only be opened once the loan has entered the
    return process (an equipment_return row exists); before that the item is
    merely still in circulation and no teacher is named as compensator.
    """
    affected = {str(x) for x in affected_loan_ids}
    assignment, _ = assign_return_scans(db)
    explicit_return_loans = {str(_body(r)['loan']) for r in load_rows(db, 'equipment_return')
                             if _body(r).get('loan') is not None}
    result = []
    for loan in load_rows(db, 'equipment_loan'):
        loan_id = str(_body(loan)['id'])
        if loan_id not in affected:
            continue
        # The loan enters the return process only through its own evidence:
        # an explicit equipment_return row naming it, or a scan settling one
        # of its serials onto it. Another kit's mixed-in box does not count.
        in_return = (loan_id in explicit_return_loans
                     or any(lid == loan_id for lid in assignment.values())
                     or loan['status'] == SETTLED_LOAN)
        verdicts, _ = analyze_loan(db, loan)
        cross_kit = [item for item in assign_return_scans(db)[1]
                     if item.get('linked_loan') == loan_id
                     or loan_id in item.get('owner_loans', [])
                     or loan_id in item.get('candidate_loans', [])]
        for serial, verdict in verdicts.items():
            if cross_kit:
                verdict['cross_kit_evidence'] = cross_kit
            chain_key = f'{loan_id}:{serial}'
            upsert(db, 'serial_chain', chain_key, source_id, version,
                   verdict, verdict['status'], verdict['reason'])
            if verdict['status'] == '已归还':
                _close_case(db, loan_id, serial, verdict, source_id, version)
                continue
            if not _case_exists(db, loan_id, serial) and not in_return:
                continue  # not yet in the return process: no premature liability
            _evolve_case(db, loan, serial, verdict, source_id, version)
        result.append({'loan_id': loan_id,
                       'serials': {s: {'status': v['status'], 'reason': v['reason'],
                                       'liable': v['liable'],
                                       'minimum_evidence': v['minimum_evidence']}
                                   for s, v in verdicts.items()}})
    return result


def _case_key(loan_id, serial):
    return f'{loan_id}:{serial}'


def _close_case(db, loan_id, serial, verdict, source_id, version):
    case = get_row(db, 'liability_case', _case_key(loan_id, serial))
    if case:
        db.execute("UPDATE liability_case SET status=?, reason=?, body=?, source_id=?, version=? WHERE id=?",
                   ('已撤销', 'serial_returned_after_case',
                    dumps(_case_body(db, loan_id, serial, verdict)), source_id, version, case['id']))


def _case_body(db, loan_id, serial, verdict):
    return {'loan_id': loan_id, 'serial': serial, 'status': verdict['status'],
            'reason': verdict['reason'], 'liable': verdict['liable'],
            'possible_holders': verdict['possible_holders'],
            'minimum_evidence': verdict['minimum_evidence'],
            'conflict_core': verdict.get('conflict_core'),
            'basis_hash': basis_hash(verdict['chains'])}


def _evolve_case(db, loan, serial, verdict, source_id, version):
    lb = _body(loan)
    key = _case_key(str(lb['id']), serial)
    existing = get_row(db, 'liability_case', key)
    new_body = _case_body(db, str(lb['id']), serial, verdict)
    if existing:
        old_body = _body(existing)
        if old_body.get('basis_hash') == new_body['basis_hash'] \
                and old_body.get('reason') == new_body['reason']:
            return  # identical evidence replay: no new case, notice or adjustment
        seq = int(old_body.get('decision_seq', 1)) + 1
        new_body['decision_seq'] = seq
        new_body['supersedes'] = old_body.get('decision_seq', 1)
        _issue_adjustment(db, loan, serial, old_body, new_body, source_id, version, seq)
    else:
        new_body['decision_seq'] = 1
    db.execute('INSERT INTO liability_case(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
               'body=excluded.body,status=excluded.status,reason=excluded.reason',
               (key, source_id, version, dumps(new_body), verdict['status'], verdict['reason']))
    if verdict['status'] == '责任确定':
        for teacher in verdict['liable']:
            notice_id = f'{key}:{teacher}:v{new_body["decision_seq"]}'
            notice_body = {'loan_id': str(lb['id']), 'serial': serial, 'teacher': teacher,
                           'agreement': lb.get('agreement', PERSONAL),
                           'decision_seq': new_body['decision_seq'],
                           'basis_hash': new_body['basis_hash']}
            upsert(db, 'liability_notice', notice_id, source_id, version,
                   notice_body, '已出具', 'liability_notice_issued')


def _issue_adjustment(db, loan, serial, old_body, new_body, source_id, version, seq):
    """Old notices stay frozen; the reversal is carried by a separate 调整单."""
    old_notices = [r for r in load_rows(db, 'liability_notice')
                   if r['id'].startswith(f'{_case_key(old_body["loan_id"], serial)}:')
                   and r['status'] == '已出具'
                   and _body(r).get('decision_seq') == old_body.get('decision_seq')]
    for notice in old_notices:
        db.execute("UPDATE liability_notice SET status=? WHERE id=?", ('已冲销', notice['id']))
    adj_id = f'ADJ:{old_body["loan_id"]}:{serial}:v{seq}'
    adj_body = {'loan_id': old_body['loan_id'], 'serial': serial,
                'decision_seq': seq, 'supersedes': old_body.get('decision_seq'),
                'reversed_notices': [n['id'] for n in old_notices],
                'old_status': old_body['status'], 'old_reason': old_body['reason'],
                'old_liable': old_body.get('liable', []),
                'new_status': new_body['status'], 'new_reason': new_body['reason'],
                'new_liable': new_body['liable'],
                'minimum_evidence': new_body.get('minimum_evidence', []),
                'basis_hash': new_body['basis_hash']}
    upsert(db, 'adjustment_order', adj_id, source_id, version, adj_body,
           '已调整', 'new_evidence_versioned_decision')
    record_issue(db, source_id, 'liability_decision_revised',
                 f'{old_body["loan_id"]}:{serial} v{old_body.get("decision_seq")}->v{seq}')


def refresh_contracts(db, source_id, version):
    """Persist the agreement fixed at checkout time; it cannot change later."""
    for loan in load_rows(db, 'equipment_loan'):
        if loan['status'] != ACTIVE_LOAN:
            continue  # a loan that failed validation freezes no contract
        lb = _body(loan)
        agreement = lb.get('agreement', PERSONAL)
        if agreement not in VALID_AGREEMENTS:
            record_issue(db, source_id, 'unknown_agreement', f'{lb["id"]}:{agreement}')
            agreement = PERSONAL
        body = {'loan_id': str(lb['id']), 'agreement': agreement,
                'borrower': lb['borrower'],
                'team': lb.get('team') or [lb['borrower']],
                'frozen_at_checkout': True}
        upsert(db, 'custody_contract', str(lb['id']), source_id, version, body,
               '已固定', 'agreement_fixed_at_checkout')
