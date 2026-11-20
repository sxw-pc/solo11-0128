"""End-to-end custody tests across checkout / handoff / return / report."""
import json
from schedulehandoff import (equipment_checkout as co, equipment_return as rt,
                             equipment_handoff as ho, report)
from schedulehandoff.store import connect, load_rows


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def loan_row(agreement='personal_custody', serials=('CAM1', 'MIC1'), loan_id='K1',
             borrower='T1', team=('T1', 'T2')):
    return {'id': loan_id, 'borrower': borrower, 'agreement': agreement,
            'team': list(team), 'serials': list(serials),
            'station': 'S1', 'time': '2026-01-01T09:00:00', 'clock_skew_seconds': 300}


def handoff(hid, loan_id, giver, receiver, serials, signed, station='S2',
            time='2026-01-01T09:30:00', sig=None, scope='item'):
    row = {'id': hid, 'loan': loan_id, 'giver': giver, 'receiver': receiver,
           'serials': list(serials), 'signed': signed, 'station': station,
           'time': time, 'scope': scope}
    if sig:
        row['signature_id'] = sig
    return row


def return_row(rid, loan_id, serials, station='S3', time='2026-01-01T10:00:00',
               returner=None):
    return {'id': rid, 'loan': loan_id, 'serials': list(serials), 'station': station,
            'time': time, 'returner': returner}


def cases(db):
    return {r['id']: (r['status'], r['reason'], json.loads(r['body']).get('liable'),
                      json.loads(r['body']).get('decision_seq'))
            for r in connect(db).execute('SELECT id,status,reason,body FROM liability_case')}


def notices(db):
    return {r['id']: r['status']
            for r in connect(db).execute('SELECT id,status FROM liability_notice')}


# --- the exact scenario fixed in the task statement -------------------------

def test_personal_agreement_t1_keeps_mic_liability_camera_not_missing(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json',
                          {'version': 'v1', 'rows': [loan_row(serials=('CAM1', 'MIC1'))]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['CAM1'], True, sig='SG1')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'], returner='T2')]}))
    found = cases(db)
    assert 'K1:CAM1' not in found                       # camera returned: not a missing item
    assert found['K1:MIC1'][:3] == ('责任确定', 'personal_holder_liability', ['T1'])
    assert notices(db) == {'K1:MIC1:T1:v1': '已出具'}


def test_team_agreement_emits_joint_t1_t2_notice(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json',
                          {'version': 'v1', 'rows': [loan_row('team_shared')]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['CAM1'], True, sig='SG1')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'], returner='T2')]}))
    assert cases(db)['K1:MIC1'][:3] == ('责任确定', 'team_joint_liability', ['T1', 'T2'])
    assert set(notices(db)) == {'K1:MIC1:T1:v1', 'K1:MIC1:T2:v1'}


def test_late_mic_signature_shifts_personal_liability_to_t2(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H2', 'K1', 'T1', 'T2', ['MIC1'], False)]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', [])]}))
    assert cases(db)['K1:MIC1'][:3] == ('责任确定', 'personal_holder_liability', ['T1'])
    ho.sign(db, write(tmp_path, 'sig.json', {'version': 'v2', 'rows': [
        {'signature_id': 'SG2', 'handoff': 'H2', 'signed_by': 'T2',
         'station': 'S2', 'time': '2026-01-01T09:45:00'}]}))
    assert cases(db)['K1:MIC1'][:3] == ('责任确定', 'personal_holder_liability', ['T2'])
    n = notices(db)
    assert n['K1:MIC1:T1:v1'] == '已冲销'               # historical notice kept, reversed
    assert n['K1:MIC1:T2:v2'] == '已出具'
    adjustments = load_rows(connect(db), 'adjustment_order')
    assert len(adjustments) == 1
    body = json.loads(adjustments[0]['body'])
    assert body['old_liable'] == ['T1'] and body['new_liable'] == ['T2']
    assert body['reversed_notices'] == ['K1:MIC1:T1:v1']


def test_team_agreement_stays_joint_after_late_signature(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row('team_shared')]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H2', 'K1', 'T1', 'T2', ['MIC1'], False)]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'])]}))
    ho.sign(db, write(tmp_path, 'sig.json', {'version': 'v2', 'rows': [
        {'signature_id': 'SG2', 'handoff': 'H2', 'signed_by': 'T2',
         'station': 'S2', 'time': '2026-01-01T09:45:00'}]}))
    assert cases(db)['K1:MIC1'][2] == ['T1', 'T2']     # joint clause unchanged


# --- whole-kit vs single-item handoff --------------------------------------

def test_kit_scope_handoff_expands_all_serials(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {
        'version': 'v1',
        'kits': [{'id': 'KIT1', 'items': ['CAM1', 'MIC1']}],
        'rows': [loan_row(serials=('CAM1', 'MIC1'))]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', [], True, sig='SG1', scope='kit')]}))
    stored = json.loads(load_rows(connect(db), 'equipment_handoff')[0]['body'])
    assert stored['serials'] == ['CAM1', 'MIC1'] and stored['scope'] == 'kit'
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', [])]}))
    # whole kit signed over: T2 holds everything
    assert cases(db)['K1:MIC1'][2] == ['T2']
    assert cases(db)['K1:CAM1'][2] == ['T2']


def test_single_item_handoff_does_not_move_the_rest(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['CAM1'], True, sig='SG1')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'], returner='T2')]}))
    assert cases(db)['K1:MIC1'][2] == ['T1']


# --- substitutes, cross-kit, duplicate scans --------------------------------

def test_same_model_substitute_cannot_stand_in_for_original_serial(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1', 'MIC9'])]}))
    assert cases(db)['K1:MIC1'][2] == ['T1']           # MIC9 != MIC1
    row = load_rows(connect(db), 'equipment_return')[0]
    assert json.loads(row['body'])['serials'] == ['CAM1', 'MIC9']


def test_cross_kit_mixed_return_checks_both_loans_without_double_settlement(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [
        loan_row(loan_id='K1', borrower='T1', serials=('CAM1', 'MIC1')),
        loan_row(loan_id='K2', borrower='T3', team=('T3',), serials=('CAM2', 'MIC9'))]}))
    # K1's box contains MIC9 (K2's) instead of MIC1 (K1's)
    rt.receive(db, write(tmp_path, 'r1.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1', 'MIC9'], returner='T1')]}))
    after_first = cases(db)
    assert after_first['K1:MIC1'][2] == ['T1']
    assert 'K2:MIC9' not in after_first                # K2 not yet in return process
    # K2 now returns its own box; MIC9 must settle K2 exactly once
    rt.receive(db, write(tmp_path, 'r2.json',
                         {'version': 'v1', 'rows': [return_row('R2', 'K2', ['CAM2', 'MIC9'], returner='T3')]}))
    conn = connect(db)
    loan_status = {r['id']: r['status'] for r in conn.execute('SELECT id,status FROM equipment_loan')}
    assert loan_status['K2'] == '已结清'
    chain = json.loads(conn.execute("SELECT body FROM serial_chain WHERE id='K2:MIC9'").fetchone()['body'])
    assert chain['status'] == '已归还'
    # the single MIC9 scan never created two settlements
    assert sum(1 for _ in conn.execute("SELECT 1 FROM equipment_scan WHERE body LIKE '%MIC9%'")) >= 1


def test_duplicate_scans_count_once_and_replay_opens_no_new_case(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    # CAM1 scanned twice (gun double-trigger) while MIC1 is genuinely missing
    payload = {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1', 'CAM1'])]}
    rt.receive(db, write(tmp_path, 'r.json', payload))
    conn = connect(db)
    return_scans = [s for s in load_rows(conn, 'equipment_scan')
                    if json.loads(s['body']).get('kind') == 'return']
    # two physical scans of one serial collapse to a single event
    assert [json.loads(s['body'])['serial'] for s in return_scans] == ['CAM1']
    before_cases = cases(db)
    assert before_cases['K1:MIC1'][2] == ['T1']           # one case, not two
    before_notices = load_rows(connect(db), 'liability_notice')
    before_adjustments = load_rows(connect(db), 'adjustment_order')
    # re-import the identical file (same bytes -> same hash -> idempotent capture)
    rt.receive(db, write(tmp_path, 'r_copy.json', payload))
    assert cases(db) == before_cases
    assert load_rows(connect(db), 'liability_notice') == before_notices
    assert load_rows(connect(db), 'adjustment_order') == before_adjustments
    # the source file is stored only once despite two imports
    assert connect(db).execute(
        "SELECT count(*) FROM export_file WHERE kind='desktop_equipment_return'").fetchone()[0] == 1


# --- clock skew, late evidence, versioned decisions -------------------------

def test_ambiguous_chain_is_pending_not_last_scanner_liability(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json',
                          {'version': 'v1', 'rows': [loan_row(serials=('MIC1',),
                                                              team=('T1', 'T2', 'T3'))]}))
    # the same giver signs over the item to two different receivers in the
    # skew window: either chain is feasible, so liability cannot be assigned
    ho.handoff(db, write(tmp_path, 'h1.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['MIC1'], True, sig='SG1',
                time='2026-01-01T09:30:00')]}))
    ho.handoff(db, write(tmp_path, 'h2.json', {'version': 'v2', 'rows': [
        handoff('H2', 'K1', 'T1', 'T3', ['MIC1'], True, sig='SG2',
                time='2026-01-01T09:33:00')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', [])]}))
    status, reason, liable, seq = cases(db)['K1:MIC1']
    assert status == '责任待核' and liable == []
    body = json.loads(connect(db).execute(
        "SELECT body FROM serial_chain WHERE id='K1:MIC1'").fetchone()['body'])
    assert body['possible_holders'] == ['T2', 'T3']
    assert body['minimum_evidence'], '待核必须给出最少补证清单'
    assert not load_rows(connect(db), 'liability_notice'), '待核不得出具赔偿通知'


def test_later_evidence_resolves_pending_case_with_a_new_version(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json',
                          {'version': 'v1', 'rows': [loan_row(serials=('MIC1',),
                                                              team=('T1', 'T2', 'T3'))]}))
    ho.handoff(db, write(tmp_path, 'h1.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['MIC1'], True, sig='SG1',
                time='2026-01-01T09:30:00')]}))
    ho.handoff(db, write(tmp_path, 'h2.json', {'version': 'v2', 'rows': [
        handoff('H2', 'K1', 'T1', 'T3', ['MIC1'], True, sig='SG2',
                time='2026-01-01T09:33:00')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', [])]}))
    assert cases(db)['K1:MIC1'][0] == '责任待核'
    # decisive new evidence: a station return scan inside the skew window
    # naming T2 forces the T1->T2 chain and closes the item as returned
    rt.receive(db, write(tmp_path, 'r2.json', {'version': 'v3', 'rows': [
        return_row('R2', 'K1', ['MIC1'], station='S2',
                   time='2026-01-01T09:31:30', returner='T2')]}))
    status, reason, liable, seq = cases(db)['K1:MIC1']
    assert status == '已撤销'
    chain = json.loads(connect(db).execute(
        "SELECT body FROM serial_chain WHERE id='K1:MIC1'").fetchone()['body'])
    assert chain['status'] == '已归还'
    # no compensation notice was ever issued for a case that stayed pending
    assert not load_rows(connect(db), 'liability_notice')


# --- agreements are frozen at checkout --------------------------------------

def test_agreement_cannot_be_changed_after_loss(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row('personal_custody')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'])]}))
    contracts = {r['id']: json.loads(r['body'])['agreement']
                 for r in load_rows(connect(db), 'custody_contract')}
    assert contracts['K1'] == 'personal_custody'
    # re-importing the same loan keeps the frozen agreement (identity preserved)
    co.checkout(db, write(tmp_path, 'loan2.json',
                          {'version': 'v1', 'rows': [loan_row('personal_custody')]}))
    assert cases(db)['K1:MIC1'][2] == ['T1']


def test_unknown_agreement_is_rejected_at_checkout(tmp_path):
    db = tmp_path / 'ledger.db'
    bad = loan_row('verbal_sharing')
    result = co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [bad]}))
    assert result[0]['status'] == '待核' and result[0]['reason'] == 'unknown_agreement'


def test_missing_borrower_or_duplicate_serial_blocks_checkout(tmp_path):
    db = tmp_path / 'ledger.db'
    rows = [
        {'id': 'KB', 'borrower': '', 'agreement': 'personal_custody',
         'serials': ['CAM1'], 'station': 'S1', 'time': '2026-01-01T09:00:00'},
        {'id': 'KC', 'borrower': 'T1', 'agreement': 'personal_custody',
         'serials': ['CAM1', 'CAM1'], 'station': 'S1', 'time': '2026-01-01T09:00:00'},
    ]
    result = co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': rows}))
    assert [(r['status'], r['reason']) for r in result] == [
        ('待核', 'borrower_or_serial_missing'), ('待核', 'duplicate_serial_in_kit')]
    # blocked loans never seed a contract or open a case
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM custody_contract").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM liability_case").fetchone()[0] == 0


# --- report and schedule independence ---------------------------------------

def test_report_explains_why_holder_is_or_is_not_determined(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    ho.handoff(db, write(tmp_path, 'h.json', {'version': 'v1', 'rows': [
        handoff('H1', 'K1', 'T1', 'T2', ['CAM1'], True, sig='SG1')]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'], returner='T2')]}))
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    eq = data['equipment']
    assert {'serial_chain', 'liability_case', 'liability_notice',
            'adjustment_order', 'custody_contract'} <= set(eq)
    explanations = {x['serial']: x['narrative'] for x in eq['serial_explanations']}
    assert 'MIC1' in explanations and 'T1' in explanations['MIC1']
    assert 'CAM1' in explanations and '已归还' in explanations['CAM1']


def test_equipment_liability_does_not_depend_on_schedule_notice_state(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json', {'version': 'v1', 'rows': [loan_row()]}))
    rt.receive(db, write(tmp_path, 'r.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['CAM1'])]}))
    assert cases(db)['K1:MIC1'][0] == '责任确定'
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM notice_batch").fetchone()[0] == 0


def test_return_against_a_settled_loan_is_held_for_review(tmp_path):
    db = tmp_path / 'ledger.db'
    co.checkout(db, write(tmp_path, 'loan.json',
                          {'version': 'v1', 'rows': [loan_row(serials=('MIC1',))]}))
    rt.receive(db, write(tmp_path, 'r1.json',
                         {'version': 'v1', 'rows': [return_row('R1', 'K1', ['MIC1'])]}))
    assert connect(db).execute(
        "SELECT status FROM equipment_loan WHERE id='K1'").fetchone()[0] == '已结清'
    out = rt.receive(db, write(tmp_path, 'r2.json',
                               {'version': 'v2', 'rows': [return_row('R2', 'K1', ['MIC1'])]}))
    assert out[0] == {'subject': 'R2', 'status': '待核', 'reason': 'loan_missing'}


def test_backward_compatible_simple_checkout_still_reports_missing(tmp_path):
    # the original enhanced-material shape (no agreement/station) still works
    db = tmp_path / 'ledger.db'
    loan = {'version': 'v1', 'rows': [{'id': 'K1', 'borrower': 'T1',
                                       'serials': ['CAM1', 'MIC1']}]}
    assert co.checkout(db, write(tmp_path, 'loan.json', loan))[0]['status'] == '已借出'
    out = rt.receive(db, write(tmp_path, 'return.json', {
        'version': 'v1', 'rows': [{'id': 'R1', 'loan': 'K1', 'serials': ['CAM1']}]}))
    assert out[0]['status'] == '缺件'
    # the decision is no longer a bare serial_mismatch: liability is derived
    # from the actual custody chain and stored against the serial
    assert all(d['reason'] != 'serial_mismatch' for d in out)
    assert cases(db)['K1:MIC1'][2] == ['T1']
