"""Unit tests for the custody-chain engine (pure logic, no SQLite)."""
from schedulehandoff import custody as c


def ev(kind, ref, serial, time=None, station=None, actor=None, giver=None,
       signed=True, scope='item'):
    return c.Event(kind, ref, serial, c.parse_time(time), station, actor, giver,
                   signed, scope)


def loan_ev(serial='MIC1', borrower='T1', t='2026-01-01T09:00:00'):
    return ev('loan', 'loan:K1', serial, t, 'S1', actor=borrower, scope='kit')


def test_loan_only_keeps_borrower_as_sole_holder():
    chains, trunc = c.enumerate_chains([loan_ev()])
    assert trunc is False
    assert len(chains) == 1
    assert chains[0]['final_holder'] == 'T1'


def test_signed_single_item_handoff_moves_custody():
    events = [loan_ev('CAM1'),
              ev('handoff', 'H1', 'CAM1', '2026-01-01T09:30:00', 'S2',
                 actor='T2', giver='T1')]
    chains, _ = c.enumerate_chains(events)
    assert {x['final_holder'] for x in chains} == {'T2'}
    assert all(any(e['ref'] == 'H1' for e in ch['edges']) for ch in chains)


def test_unsigned_handoff_cannot_move_custody():
    events = [loan_ev()]
    excluded = {'MIC1': [{'ref': 'H1', 'kind': 'unsigned_handoff',
                          'giver': 'T1', 'receiver': 'T2', 'why': 'signature_missing'}]}
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', events, excluded, 300, c.PERSONAL, ['T1', 'T2'])
    assert verdict['status'] == '责任确定'
    assert verdict['liable'] == ['T1']
    assert verdict['excluded_evidence'][0]['why'] == 'signature_missing'


def test_same_model_substitute_serial_never_matches_original():
    # MIC9 is scanned back, but the loaned serial is MIC1: the foreign serial
    # never enters MIC1's chain, so MIC1 stays missing under the borrower
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', [loan_ev('MIC1')], {}, 300, c.PERSONAL, ['T1'])
    assert verdict['status'] == '责任确定'
    assert verdict['liable'] == ['T1']


def test_duplicate_return_scan_collapses_to_one_event():
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1'],
                     'time': '2026-01-01T09:00:00', 'station': 'S1'}}
    scans = [
        {'body': {'id': 'R1:MIC1', 'serials': ['MIC1'], 'station': 'S3',
                  'time': '2026-01-01T10:00:00', 'returner': 'T1', 'kind': 'return'}},
        {'body': {'id': 'R2:MIC1', 'serials': ['MIC1'], 'station': 'S3',
                  'time': '2026-01-01T10:00:00', 'returner': 'T1', 'kind': 'return'}},
    ]
    events, excluded = c.build_events(loan, [], scans)
    assert len(events['MIC1']) == 2  # loan + one return
    assert excluded['MIC1'][0]['kind'] == 'duplicate_scan'


def test_checkout_scans_are_not_treated_as_returns():
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1'],
                     'time': '2026-01-01T09:00:00', 'station': 'S1'}}
    scans = [{'body': {'id': 'checkout:K1:MIC1', 'serials': ['MIC1'],
                       'kind': 'checkout', 'station': 'S1', 'time': '2026-01-01T09:00:00'}}]
    events, _ = c.build_events(loan, [], scans)
    assert all(e.kind != 'return' for e in events['MIC1'])


def test_clock_skew_allows_two_orderings_and_forces_pending():
    # the same giver T1 signs two item handoffs inside the 5-minute window,
    # one to T2 and one to T3. A single item can only follow one of them, so
    # both final holders stay feasible — the engine must not pick by row order
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('handoff', 'H1', 'MIC1', '2026-01-01T09:30:00', 'S2', actor='T2', giver='T1'),
              ev('handoff', 'H2', 'MIC1', '2026-01-01T09:33:00', 'S2', actor='T3', giver='T1')]
    chains, _ = c.enumerate_chains(events, skew_seconds=300)
    holders = {ch['final_holder'] for ch in chains}
    assert holders == {'T2', 'T3'}
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', events, {}, 300, c.PERSONAL, ['T1', 'T2', 'T3'])
    assert verdict['status'] == '责任待核'
    assert verdict['reason'] == 'multiple_compatible_holders_pending'
    assert verdict['possible_holders'] == ['T2', 'T3']
    assert verdict['minimum_evidence'], '必须指出区分持有人所需的最少证据'


def test_events_outside_skew_have_a_single_ordering():
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('handoff', 'H1', 'MIC1', '2026-01-01T09:30:00', 'S2', actor='T2', giver='T1'),
              ev('handoff', 'H2', 'MIC1', '2026-01-01T10:00:00', 'S2', actor='T1', giver='T2')]
    chains, _ = c.enumerate_chains(events, skew_seconds=300)
    assert {ch['final_holder'] for ch in chains} == {'T1'}


def test_handoff_from_someone_who_never_held_it_is_a_conflict():
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('handoff', 'H1', 'MIC1', '2026-01-01T09:30:00', 'S2',
                 actor='T2', giver='T9')]
    chains, _ = c.enumerate_chains(events, 300)
    # the loan-only chain survives, but the signed handoff fits no chain
    unapplied = c.unapplied_signed_events(events, chains)
    assert [x['event']['ref'] for x in unapplied] == ['H1']
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', events, {}, 300, c.PERSONAL, ['T1', 'T2'])
    assert verdict['status'] == '证据冲突'
    assert verdict['reason'] == 'signed_evidence_contradicts_custody'
    core = verdict['conflict_core']
    assert any('T9' in x for x in core['contradictions'])


def test_return_named_by_non_holder_contradicts_the_chain():
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('return', 'R1', 'MIC1', '2026-01-01T10:00:00', 'S3', actor='T9')]
    chains, _ = c.enumerate_chains(events, 300)
    assert c.unapplied_signed_events(events, chains)
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', events, {}, 300, c.PERSONAL, ['T1'])
    assert verdict['status'] == '证据冲突'


def test_unstaffed_return_scan_closes_custody_without_naming_holder():
    events = [loan_ev(),
              ev('return', 'R1', 'MIC1', '2026-01-01T10:00:00', 'S3', actor=None)]
    chains, _ = c.enumerate_chains(events, 300)
    assert len(chains) == 1
    assert chains[0]['final_holder'] is None


def test_personal_contract_pins_liability_to_unique_holder():
    liable, code = c.apply_contract(c.PERSONAL, ['T1', 'T2'], 'T1', ['T2'])
    assert liable == ['T2'] and code == 'personal_holder_liability'


def test_personal_contract_goes_pending_with_multiple_holders():
    liable, code = c.apply_contract(c.PERSONAL, ['T1', 'T2'], 'T1', ['T1', 'T2'])
    assert liable is None and code == 'multiple_compatible_holders_pending'


def test_team_contract_is_joint_even_when_one_holder_is_certain():
    liable, code = c.apply_contract(c.TEAM, ['T1', 'T2'], 'T1', ['T2'])
    assert liable == ['T1', 'T2'] and code == 'team_joint_liability'


def test_team_contract_pending_when_holder_is_outside_team():
    liable, code = c.apply_contract(c.TEAM, ['T1', 'T2'], 'T1', ['T3'])
    assert liable is None and code == 'holder_outside_team_pending'


def test_holder_ranges_cover_full_loan_window():
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('handoff', 'H1', 'MIC1', '2026-01-01T09:30:00', 'S2', actor='T2', giver='T1')]
    chains, _ = c.enumerate_chains(events, 300)
    ranges = c.holder_ranges(events, chains, 300)
    assert ranges[0]['holders'] == ['T1']
    assert ranges[-1]['holders'] == ['T2']


def test_no_solution_reports_minimal_conflict_core():
    events = [loan_ev(t='2026-01-01T09:00:00'),
              ev('handoff', 'H1', 'MIC1', '2026-01-01T09:30:00', 'S2', actor='T2', giver='T9'),
              ev('return', 'R1', 'MIC1', '2026-01-01T10:00:00', 'S3', actor='T9')]
    loan = {'body': {'id': 'K1', 'borrower': 'T1', 'serials': ['MIC1']}}
    verdict = c.analyze_serial(loan, 'MIC1', events, {}, 300, c.PERSONAL, ['T1'])
    assert verdict['status'] == '证据冲突'
    core = verdict['conflict_core']
    refs = [e['ref'] for e in core['events']]
    assert 'loan:K1' in refs and 'H1' in refs          # smallest rooted subset
    assert any('T9' in line for line in core['contradictions'])


def test_chain_enumeration_respects_max_chains(monkeypatch):
    monkeypatch.setattr(c, 'MAX_CHAINS', 2)
    events = [loan_ev(t='2026-01-01T09:00:00')]
    for i in range(6):
        events.append(ev('handoff', f'H{i}', 'MIC1', f'2026-01-01T09:{30 + i:02d}:00',
                         'S2', actor=f'T{i + 2}', giver='T1'))
    chains, truncated = c.enumerate_chains(events, 300)
    assert len(chains) == 2 and truncated is True
