"""Synthetic-floor acceptance tests for offline distribution evidence.

The floor is deliberately tiny and fully recomputable:

* branch A feeds the faulted segment ``F``; room R2 (6 A basic lighting)
  hangs behind the faulted segment on segment S2, so R2 is dark at fault
  time;
* backup feeder B is rated 10 A, phase A, and already carries R1's 3 A
  deferrable projector (its 3 A already includes restart maximum);
* the tie switch T1 connects S2 to B's segment S1 but is open;
* R2's lamps draw an 8 A start-up surge from cold, then 6 A steady.

Closing the tie directly gives 3 + 8 = 11 A instantaneous (over B's 10 A)
and back-feeds F because BR2/BR2R are still closed.  Opening the fault side,
shedding R1's 3 A and then closing T1 gives 8 A instantaneous / 6 A steady;
restoring R1 gives 9 A steady.  R2 is briefly dark.
"""
import json

import pytest

from schedulehandoff import distribution as dist
from schedulehandoff import operations, power, report
from schedulehandoff.store import connect

ROOT_FLOOR = {
    'version': 'v1', 'topology': {'id': 'T1'},
    'feeders': [
        {'id': 'A', 'bus': 'busA', 'rated_amp': 10, 'source': 'panel-A'},
        {'id': 'B', 'bus': 'busB', 'rated_amp': 10, 'source': 'panel-B'}],
    'breakers': [
        {'id': 'BR2', 'from': 'bus:busA', 'to': 'seg:F', 'phase': 'A',
         'initial': 'closed', 'rated_amp': 10},
        {'id': 'BR2R', 'from': 'seg:F', 'to': 'seg:S2', 'phase': 'A',
         'initial': 'closed', 'rated_amp': 10},
        {'id': 'BR1', 'from': 'bus:busB', 'to': 'seg:S1', 'phase': 'A',
         'initial': 'closed', 'rated_amp': 10}],
    'tie_switches': [
        {'id': 'T1', 'from': 'seg:S2', 'to': 'seg:S1', 'phase': 'A',
         'initial': 'open'}],
    'room_loads': [
        {'room': 'R2', 'segment': 'S2', 'phase': 'A',
         'classes': {'basic': 6}, 'startup': {'basic': 8},
         'source': 'panel-A/R2'},
        {'room': 'R1', 'segment': 'S1', 'phase': 'A',
         'classes': {'deferrable': 3}, 'startup': {'deferrable': 3},
         'source': 'panel-B/R1'}],
    'measurements': [
        {'id': 'm-B', 'feeder': 'B', 'phase': 'A', 'amps': 3,
         'at': '2026-01-01T08:59:00', 'source': 'panel-B'},
        {'id': 'm-R2', 'feeder': 'A', 'phase': 'A', 'amps': 6,
         'at': '2026-01-01T08:59:00', 'rated_amp': 10, 'source': 'panel-A'}],
}

FAULT = {'version': 'v1', 'fault': {
    'id': 'F1', 'at': '2026-01-01T09:10:00', 'segments': ['F'],
    'tripped': [], 'source': 'panel-A'}}

LESSONS = {'version': 'v1', 'rows': [
    {'id': 'L2', 'room': 'R2', 'start': '2026-01-01T09:00:00',
     'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20},
    {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T09:00:00',
     'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20}]}

ORDINARY = {
    'R2': {'id': 'S2', 'room': 'R2', 'lesson': 'L2', 'contract': '普通授课',
           'load_classes': ['basic'], 'signed_at': '2026-01-01T08:00:00'},
    'R1': {'id': 'S1', 'room': 'R1', 'lesson': 'L1', 'contract': '普通授课',
           'load_classes': ['basic', 'deferrable'],
           'signed_at': '2026-01-01T08:00:00'},
}
EXAM = {
    'R2': {'id': 'S2', 'room': 'R2', 'lesson': 'L2', 'contract': '在线考试',
           'load_classes': ['basic', 'critical'],
           'signed_at': '2026-01-01T08:00:00'},
    'R1': {'id': 'S1', 'room': 'R1', 'lesson': 'L1', 'contract': '普通授课',
           'load_classes': ['basic', 'deferrable'],
           'signed_at': '2026-01-01T08:00:00'},
}


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


def services_payload(mapping, fault_at='2026-01-01T09:10:00'):
    return {'version': 'v1', 'fault_at': fault_at,
            'rows': [json.loads(json.dumps(row)) for row in mapping.values()]}


def make_floor(tmp_path, services=None, lessons=LESSONS, floor=ROOT_FLOOR,
               fault=FAULT):
    db = tmp_path / 'ledger.db'
    operations.first(db, write(tmp_path, 'rooms.json', lessons))
    power.topology(db, write(tmp_path, 'floor.json', floor))
    power.fault(db, write(tmp_path, 'fault.json', fault))
    if services:
        power.service(db, write(tmp_path, 'services.json',
                                services_payload(services)))
    return db


def do_plan(db, tmp_path, plan_id='P1'):
    return power.plan(db, write(tmp_path, f'{plan_id}.json',
                                {'version': 'v1', 'plan_id': plan_id,
                                 'fault_id': 'F1'}))


def receipt_for(plan, times, observed=None):
    """Build an execution receipt in the plan's own order."""
    steps = []
    for step, at in zip(plan['steps'], times):
        spec = step['action_spec']
        item = {'at': at, 'receipt': True}
        if spec['kind'] in ('open', 'close'):
            item[spec['kind']] = spec['id']
            item['phase'] = spec['phase']
        else:
            item[spec['kind']] = spec['room']
            item['class'] = spec['class']
        if observed and spec['kind'] in ('close', 'ramp'):
            item['observed_amps'] = observed
        steps.append(item)
    return steps


def run_execution(db, tmp_path, plan, times, observed=None, execution_id='E1',
                  fault_at='2026-01-01T09:10:00'):
    payload = {'version': 'v1', 'execution': {
        'id': execution_id, 'plan_id': plan['plan_id'],
        'topology_id': 'T1', 'fault_id': 'F1',
        'fault_at': fault_at, 'started_at': '2026-01-01T09:10:05',
        'meter_evidence': {'m-B': 'panel-B'},
        'steps': receipt_for(plan, times, observed)}}
    return power.execution(db, write(tmp_path, f'{execution_id}.json', payload))


def run_proof(db, tmp_path, execution_id='E1', plan_id='P1'):
    return power.proof(db, write(tmp_path, f'{execution_id}-proof.json',
                                 {'version': 'v1', 'plan_id': plan_id,
                                  'execution_id': execution_id}))


# ---------------------------------------------------------------------------
# topology and hazards
# ---------------------------------------------------------------------------

def test_direct_tie_close_backfeeds_and_startup_surge_overloads():
    topo = dist.parse_topology(ROOT_FLOOR)
    fault = {'segments': ['F']}
    state = dist.scenario_state(topo, fault)
    _, log, error = dist.check_action(topo, state,
                                      {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                      ['F'])
    assert error == 'backfeed_fault_section'
    assert 'F' in log['backfeed_sections']
    # even with the fault side isolated, the direct close still exceeds 10 A
    opened, _, _ = dist.check_action(topo, state,
                                     {'kind': 'open', 'id': 'BR2', 'phase': 'A'},
                                     ['F'])
    opened, _, _ = dist.check_action(topo, opened,
                                     {'kind': 'open', 'id': 'BR2R', 'phase': 'A'},
                                     ['F'])
    _, log2, error2 = dist.check_action(topo, opened,
                                        {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                        ['F'])
    assert error2 == 'startup_surge'
    assert log2['instantaneous_amps']['B']['A'] == pytest.approx(11.0)
    assert log2['instantaneous_amps']['B']['N'] == pytest.approx(11.0)
    assert log2['overload'][0]['limit'] == 10.0


def test_end_state_within_limit_does_not_prove_intermediate_state():
    topo = dist.parse_topology(ROOT_FLOOR)
    state = dist.scenario_state(topo, {'segments': ['F']})
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2', 'phase': 'A'},
                                    ['F'])
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2R', 'phase': 'A'},
                                    ['F'])
    # 9 A steady state after the tie close (3 A + 6 A) is within 10 A...
    opened_r2 = dist.evaluate(topo, state, ['F'])
    assert opened_r2['currents']['B']['A'] == pytest.approx(3.0)
    _, log, error = dist.check_action(topo, state,
                                      {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                      ['F'])
    assert error == 'startup_surge'
    assert log['after']['currents']['B']['A'] == pytest.approx(9.0)  # safe end state
    assert log['instantaneous_amps']['B']['A'] == pytest.approx(11.0)  # unsafe moment


def test_neutral_carries_the_sum_of_phases():
    topo = dist.parse_topology(ROOT_FLOOR)
    fault = {'segments': ['F']}
    state = dist.scenario_state(topo, fault)
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2', 'phase': 'A'},
                                    ['F'])
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2R', 'phase': 'A'},
                                    ['F'])
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'shed', 'room': 'R1', 'class': 'deferrable'},
                                    ['F'])
    state, log, error = dist.check_action(topo, state,
                                          {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                          ['F'])
    assert error is None
    assert log['after']['currents']['B']['A'] == pytest.approx(6.0)
    assert log['after']['currents']['B']['N'] == pytest.approx(6.0)
    assert log['instantaneous_amps']['B']['A'] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# planning the full sequence and service contracts
# ---------------------------------------------------------------------------

def test_plan_sheds_deferrable_then_switches_then_restores(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    planned = do_plan(db, tmp_path)
    assert planned['feasible'] is True
    labels = [s['label'] for s in planned['steps']]
    assert labels == ['暂卸 R1 可延迟投影机', '断开 BR2|A', '断开 BR2R|A',
                      '合上 T1|A', '恢复 R1 可延迟投影机']
    close = planned['steps'][3]
    assert close['instantaneous_amps']['B']['A'] == pytest.approx(8.0)
    assert close['currents']['B']['A'] == pytest.approx(6.0)
    assert close['currents']['B']['N'] == pytest.approx(6.0)
    assert close['surges'] == [{'room': 'R2', 'class': 'basic', 'surge_amp': 2.0}]
    ramp = planned['steps'][4]
    assert ramp['currents']['B']['A'] == pytest.approx(9.0)
    assert ramp['currents']['B']['N'] == pytest.approx(9.0)
    # R2 is dark for a short time, R1's essential service never stops
    assert planned['outage_minutes']['R2'] > 0
    assert planned['outage_minutes']['R1'] == 0
    verdicts = {v['room']: v for v in planned['services']}
    assert verdicts['R2']['status'] == '可履约'
    assert verdicts['R2']['reason'] == 'within_contract_limit'
    assert verdicts['R1']['status'] == '可履约'


def test_zero_interruption_exam_cannot_continue_on_same_circuit_evidence(tmp_path):
    db = make_floor(tmp_path, EXAM)
    planned = do_plan(db, tmp_path)
    verdict = [v for v in planned['services'] if v['room'] == 'R2'][0]
    assert planned['feasible'] is True          # the *switching* works
    assert verdict['status'] == '不能履约'       # the *exam promise* does not
    assert verdict['reason'] == 'zero_interruption_breached'
    assert verdict['minimal_reasons'] == ['zero_interruption_breached']
    assert 'independent_ups' in verdict['recommendation']


def test_contract_signed_after_fault_cannot_be_used(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    late = services_payload(ORDINARY)
    for row in late['rows']:
        row['signed_at'] = '2026-01-01T09:30:00'
    late['version'] = 'v2'
    power.service(db, write(tmp_path, 'late.json', late))
    planned = do_plan(db, tmp_path)
    verdicts = {v['room']: v for v in planned['services']}
    assert verdicts['R2']['status'] == '不能履约'
    assert 'contract_not_frozen_before_fault' in verdicts['R2']['minimal_reasons']


def test_non_sheddable_third_room_blocks_the_shared_backup(tmp_path):
    """If B's 3 A were another exam's non-sheddable load, the plan must fail."""
    floor = json.loads(json.dumps(ROOT_FLOOR))
    floor['room_loads'][1]['classes'] = {'critical': 3}
    floor['room_loads'][1]['startup'] = {'critical': 3}
    services = {
        'R2': {'id': 'S2', 'room': 'R2', 'lesson': 'L2', 'contract': '普通授课',
               'load_classes': ['basic'], 'signed_at': '2026-01-01T08:00:00'},
        'R1': {'id': 'S1', 'room': 'R1', 'lesson': 'L1', 'contract': '在线考试',
               'load_classes': ['critical'], 'signed_at': '2026-01-01T08:00:00'}}
    db = make_floor(tmp_path, services, floor=floor)
    planned = do_plan(db, tmp_path)
    assert planned['feasible'] is False
    assert planned['steps'] == []
    assert 'non_sheddable_shared_feeder_load' in planned['reasons']
    assert planned['sheddable_rooms'] == []
    verdicts = {v['room']: v for v in planned['services']}
    assert verdicts['R1']['status'] == '不能履约'
    assert verdicts['R2']['status'] == '不能履约'


def test_shared_backup_overload_is_detected_globally(tmp_path):
    """Two rooms individually fine on B but together over its same phase."""
    floor = json.loads(json.dumps(ROOT_FLOOR))
    # R2 needs the tie onto B; R1 is already on B.  With R2 at 8 A steady
    # (9 A surge) and R1's 3 A, the joint final state is 11 A > 10 A even
    # though each room alone fits.
    floor['room_loads'][0]['classes'] = {'basic': 8}
    floor['room_loads'][0]['startup'] = {'basic': 9}
    services = dict(ORDINARY)
    services['R1'] = {'id': 'S1', 'room': 'R1', 'lesson': 'L1',
                      'contract': '普通授课',
                      'load_classes': ['basic', 'deferrable'],
                      'signed_at': '2026-01-01T08:00:00'}
    db = make_floor(tmp_path, services, floor=floor)
    planned = do_plan(db, tmp_path)
    assert planned['feasible'] is False
    assert planned['sheddable_rooms'] == ['R1']
    assert planned['reasons'] == ['shared_backup_same_phase_overload']
    result = dist.plan_sequence(
        dist.parse_topology(floor), {'segments': ['F']},
        ['R2', 'R1'], {'R1'}, ['F'])
    assert result['individual_feasibility'] == {'R1': True, 'R2': True}


def test_shared_phase_with_non_sheddable_third_room_is_named(tmp_path):
    """R1, R2, R3 share B; only R1 is shedable, so the joint plan fails."""
    floor = json.loads(json.dumps(ROOT_FLOOR))
    floor['room_loads'].append(
        {'room': 'R3', 'segment': 'S1', 'phase': 'A',
         'classes': {'basic': 6}, 'startup': {'basic': 8}})
    lessons = {'version': 'v1', 'rows': LESSONS['rows'] + [
        {'id': 'L3', 'room': 'R3', 'start': '2026-01-01T09:00:00',
         'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20}]}
    services = dict(ORDINARY)
    services['R3'] = {'id': 'S3', 'room': 'R3', 'lesson': 'L3',
                      'contract': '在线考试', 'load_classes': ['basic'],
                      'signed_at': '2026-01-01T08:00:00'}
    db = make_floor(tmp_path, services, lessons=lessons, floor=floor)
    planned = do_plan(db, tmp_path)
    assert planned['feasible'] is False
    assert 'non_sheddable_shared_feeder_load' in planned['reasons']
    result = dist.plan_sequence(
        dist.parse_topology(floor), {'segments': ['F']},
        ['R2', 'R1', 'R3'], {'R1'}, ['F'])
    assert result['individual_feasibility']['R2'] is True


def test_lesson_outside_fault_time_does_not_need_powering(tmp_path):
    lessons = {'version': 'v1', 'rows': [
        {'id': 'L2', 'room': 'R2', 'start': '2026-01-01T11:00:00',
         'end': '2026-01-01T12:00:00', 'capacity': 30, 'enrolled': 20},
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T09:00:00',
         'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20}]}
    db = make_floor(tmp_path, ORDINARY, lessons=lessons)
    planned = do_plan(db, tmp_path)
    assert planned['protected_rooms'] == ['R1']
    assert all('R2' not in v['room'] for v in planned['services'])


def test_time_correction_changes_which_rooms_are_protected(tmp_path):
    lessons = {'version': 'v1', 'rows': [
        {'id': 'L2', 'room': 'R2', 'start': '2026-01-01T11:00:00',
         'end': '2026-01-01T12:00:00', 'capacity': 30, 'enrolled': 20},
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T09:00:00',
         'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20}]}
    db = make_floor(tmp_path, ORDINARY, lessons=lessons)
    assert do_plan(db, tmp_path)['protected_rooms'] == ['R1']
    operations.correct(db, write(tmp_path, 'correction.json', {'version': 'v2',
        'rows': [{'lesson': 'L2', 'room_slot': 'L2',
                  'start': '2026-01-01T09:00:00',
                  'end': '2026-01-01T10:00:00'}]}))
    planned = do_plan(db, tmp_path, plan_id='P2')
    assert planned['protected_rooms'] == ['R1', 'R2']
    assert len(planned['steps']) >= 5


def test_circuit_evidence_never_writes_back_to_schedule_teacher_or_notice(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    operations.second(db, write(tmp_path, 'staff.json', {'version': 'v1', 'rows': [
        {'id': 'S2', 'lesson': 'L2', 'teacher': 'T2', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True},
        {'id': 'S1', 'lesson': 'L1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True}]}))
    operations.third(db, write(tmp_path, 'notice.json', {'version': 'v1', 'rows': [
        {'id': 'N1', 'lesson': 'L1', 'required_classes': ['C1'],
         'receipts': [{'class': 'C1', 'version': 'v1'}]}]}))
    tables = ('room_slot', 'substitution', 'notice_batch', 'notice_receipt',
              'teacher')
    conn = connect(db)
    before = {t: [dict(x) for x in conn.execute(f'SELECT * FROM {t} ORDER BY id')]
              for t in tables}
    conn.close()
    planned = do_plan(db, tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    run_proof(db, tmp_path)
    conn = connect(db)
    after = {t: [dict(x) for x in conn.execute(f'SELECT * FROM {t} ORDER BY id')]
             for t in tables}
    assert before == after
    # the effective notice is untouched by any circuit conclusion
    assert conn.execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()[0] == '换班生效'


def test_backfeed_evidence_is_reported_even_when_action_is_refused(tmp_path):
    topo = dist.parse_topology(ROOT_FLOOR)
    state = dist.scenario_state(topo, {'segments': ['F']})
    _, log, error = dist.check_action(topo, state,
                                      {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                      ['F'])
    assert error == 'backfeed_fault_section'
    assert log['risks'] == ['backfeed_fault_section', 'make_before_break',
                            'startup_surge', 'instantaneous_overload']
    assert log['backfeed_sections'] == ['F']
    assert log['instantaneous_amps']['B']['A'] == pytest.approx(11.0)


def test_plan_and_execution_are_separate_versions(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    conn = connect(db)
    plan = json.loads(conn.execute(
        "SELECT body FROM power_switching_plan WHERE id='P1'").fetchone()[0])
    execution = json.loads(conn.execute(
        "SELECT body FROM power_switching_execution WHERE id='E1'").fetchone()[0])
    # the plan records no device receipt; the execution records no verdict
    assert all('receipt' not in step for step in plan['steps'])
    assert execution['steps'][0]['receipt'] is True
    assert 'status' not in execution or execution.get('status') is None
    assert plan['plan_id'] == execution['plan_id']


# ---------------------------------------------------------------------------
# execution evidence and proof
# ---------------------------------------------------------------------------

def _standard_floor(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    planned = do_plan(db, tmp_path)
    return db, planned


def test_proof_needs_a_device_receipt_not_just_the_plan(tmp_path):
    db, planned = _standard_floor(tmp_path)
    # no execution at all
    missing = run_proof(db, tmp_path, execution_id='nope')
    assert missing['status'] == '未完成'
    assert missing['reason'] == 'execution_missing'
    # an execution without device receipts, in the plan's own order
    receipt = receipt_for(planned, ['2026-01-01T09:10:10',
                                    '2026-01-01T09:10:20',
                                    '2026-01-01T09:10:40',
                                    '2026-01-01T09:11:00',
                                    '2026-01-01T09:11:20'],
                          observed={'A': 6})
    for step in receipt:
        step.pop('receipt', None)
    payload = {'version': 'v1', 'execution': {
        'id': 'E1', 'plan_id': 'P1', 'fault_id': 'F1',
        'fault_at': '2026-01-01T09:10:00',
        'meter_evidence': {'m-B': 'panel-B'},
        'steps': receipt}}
    power.execution(db, write(tmp_path, 'exec.json', payload))
    result = run_proof(db, tmp_path, execution_id='E1')
    assert result['status'] == '拒绝'
    assert any(r.startswith('device_receipt_missing') for r in result['reasons'])


def test_plan_is_not_accepted_as_execution(tmp_path):
    db, planned = _standard_floor(tmp_path)
    # a receipt that skips the ramp step
    receipt = receipt_for(planned, ['2026-01-01T09:10:10',
                                    '2026-01-01T09:10:20',
                                    '2026-01-01T09:10:40',
                                    '2026-01-01T09:11:00'])
    payload = {'version': 'v1', 'execution': {
        'id': 'E2', 'plan_id': 'P1', 'fault_id': 'F1',
        'fault_at': '2026-01-01T09:10:00', 'meter_evidence': {'m-B': 'panel-B'},
        'steps': receipt}}
    power.execution(db, write(tmp_path, 'exec2.json', payload))
    result = run_proof(db, tmp_path, execution_id='E2')
    assert result['status'] == '拒绝'
    assert 'step_not_executed:5' in result['reasons']


def test_outage_duration_is_measured_from_execution_timestamps(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:15:00',
                   '2026-01-01T09:15:20'],
                  observed={'A': 6})
    result = run_proof(db, tmp_path)
    assert result['status'] == '已证明'
    # plan says 0.5 min, execution says 5.0 min (R2 restored only when the
    # tie was actually closed at 09:15:00, five minutes after the fault)
    assert result['plan_outage_minutes']['R2'] == pytest.approx(0.5)
    assert result['executed_outage_minutes']['R2'] == pytest.approx(5.0)


def test_executed_long_outage_breaks_the_ordinary_contract(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:15:00',
                   '2026-01-01T09:15:20'], observed={'A': 6})
    run_proof(db, tmp_path)
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    proof = view['power']['proofs'][0]
    verdicts = {v['room']: v for v in proof['services']}
    assert verdicts['R2']['basis'] == 'executed'
    assert verdicts['R2']['status'] == '不能履约'
    assert verdicts['R2']['reason'] == 'interruption_time_exceeded'


def test_late_correction_can_invalidate_a_proof(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    assert run_proof(db, tmp_path)['status'] == '已证明'
    # a late reading says the tie-closing step actually drew 12 A
    power.correct(db, write(tmp_path, 'correction.json', {'version': 'v2', 'rows': [
        {'id': 'C1', 'execution': 'E1', 'step': 4,
         'observed_amps': {'B': {'A': 12.0, 'N': 12.0}},
         'at': '2026-01-01T09:20:00', 'reason': 'meter recalibration'}]}))
    result = run_proof(db, tmp_path)
    assert result['status'] == '拒绝'
    assert any('over_limit:4:late_correction' in r for r in result['reasons'])


def test_late_correction_below_limit_keeps_proof(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    assert run_proof(db, tmp_path)['status'] == '已证明'
    power.correct(db, write(tmp_path, 'correction.json', {'version': 'v2', 'rows': [
        {'id': 'C1', 'execution': 'E1', 'step': 4,
         'observed_amps': {'B': {'A': 8.0, 'N': 8.0}},
         'at': '2026-01-01T09:20:00'}]}))
    assert run_proof(db, tmp_path)['status'] == '已证明'


def test_make_before_break_execution_is_refused(tmp_path):
    """A receipt that closed the tie before opening the fault side is refused."""
    db, planned = _standard_floor(tmp_path)
    # deliberately wrong order: tie close first
    wrong = [
        {'shed': 'R1', 'class': 'deferrable',
         'at': '2026-01-01T09:10:10', 'receipt': True},
        {'close': 'T1', 'phase': 'A', 'at': '2026-01-01T09:10:15',
         'receipt': True, 'observed_amps': {'A': 8}},
        {'open': 'BR2', 'phase': 'A', 'at': '2026-01-01T09:10:20',
         'receipt': True},
        {'open': 'BR2R', 'phase': 'A', 'at': '2026-01-01T09:10:40',
         'receipt': True},
        {'ramp': 'R1', 'class': 'deferrable', 'at': '2026-01-01T09:11:20',
         'receipt': True, 'observed_amps': {'A': 9}}]
    payload = {'version': 'v1', 'execution': {
        'id': 'E1', 'plan_id': 'P1', 'fault_id': 'F1',
        'fault_at': '2026-01-01T09:10:00', 'meter_evidence': {'m': 'x'},
        'steps': wrong}}
    power.execution(db, write(tmp_path, 'wrong.json', payload))
    result = run_proof(db, tmp_path)
    assert result['status'] == '拒绝'
    assert any(r.startswith('unexpected_actions') for r in result['reasons'])


def test_schedule_change_after_plan_invalidates_the_proof(tmp_path):
    db, planned = _standard_floor(tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    assert run_proof(db, tmp_path)['status'] == '已证明'
    operations.first(db, write(tmp_path, 'late-rooms.json', {'version': 'v9',
        'rows': [{'id': 'L9', 'room': 'R2', 'start': '2026-01-01T09:15:00',
                  'end': '2026-01-01T10:15:00', 'capacity': 30,
                  'enrolled': 20}]}))
    result = run_proof(db, tmp_path)
    assert result['status'] == '拒绝'
    assert result['reason'] == 'schedule_changed_since_plan'


# ---------------------------------------------------------------------------
# replay, versioning and conflicts
# ---------------------------------------------------------------------------

def test_same_feeder_different_phases_share_the_neutral(tmp_path):
    """Phases are independent conductors, but the neutral is shared."""
    floor = json.loads(json.dumps(ROOT_FLOOR))
    floor['feeders'][1]['phases'] = {'A': 10, 'B': 10, 'C': 10, 'N': 10}
    # R1 now hangs on segment S1 phase B, fed by its own phase-B breaker
    floor['breakers'].append(
        {'id': 'BR1B', 'from': 'bus:busB', 'to': 'seg:S1', 'phase': 'B',
         'initial': 'closed', 'rated_amp': 10})
    floor['room_loads'][1]['phase'] = 'B'
    db = make_floor(tmp_path, dict(ORDINARY), floor=floor)
    planned = do_plan(db, tmp_path)
    assert planned['feasible'] is True
    restore = planned['steps'][-1]
    # R2's 6 A is on phase A, R1's 3 A is on phase B; neither phase is over
    assert restore['currents']['B']['A'] == pytest.approx(6.0)
    assert restore['currents']['B']['B'] == pytest.approx(3.0)
    # the shared neutral is the sum of both phase currents
    assert restore['currents']['B']['N'] == pytest.approx(9.0)
    # without shedding, R2's 8 A inrush + R1's 3 A would put 11 A on N
    topo = dist.parse_topology(floor)
    state = dist.scenario_state(topo, {'segments': ['F']})
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2', 'phase': 'A'},
                                    ['F'])
    state, _, _ = dist.check_action(topo, state,
                                    {'kind': 'open', 'id': 'BR2R', 'phase': 'A'},
                                    ['F'])
    _, log, error = dist.check_action(topo, state,
                                      {'kind': 'close', 'id': 'T1', 'phase': 'A'},
                                      ['F'])
    assert error == 'startup_surge'
    assert log['instantaneous_amps']['B']['A'] == pytest.approx(8.0)
    assert log['instantaneous_amps']['B']['B'] == pytest.approx(3.0)
    assert log['instantaneous_amps']['B']['N'] == pytest.approx(11.0)


def test_replaying_the_same_plan_and_execution_is_idempotent(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    first = do_plan(db, tmp_path)
    second = do_plan(db, tmp_path)
    assert first == second
    counts = {t: connect(db).execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('power_switching_plan', 'power_switching_execution')}
    assert counts['power_switching_plan'] == 1
    run_execution(db, tmp_path, first,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    run_execution(db, tmp_path, first,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    count = connect(db).execute('SELECT COUNT(*) FROM power_switching_execution').fetchone()[0]
    assert count == 1


def test_same_version_different_content_is_an_identity_conflict(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    do_plan(db, tmp_path)
    changed = json.loads(json.dumps(ROOT_FLOOR))
    changed['feeders'][1]['rated_amp'] = 20
    with pytest.raises(ValueError):
        power.topology(db, write(tmp_path, 'changed.json', changed))
    issues = [dict(x) for x in connect(db).execute('SELECT * FROM issue')]
    assert any(x['code'] == 'identity_conflict' for x in issues)


def test_new_version_keeps_previous_plan_history(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    first = do_plan(db, tmp_path)
    operations.correct(db, write(tmp_path, 'correction.json', {'version': 'v2',
        'rows': [{'lesson': 'L2', 'room_slot': 'L2',
                  'start': '2026-01-01T09:00:00',
                  'end': '2026-01-01T09:30:00'}]}))
    second = power.plan(db, write(tmp_path, 'p2.json',
                                  {'version': 'v2', 'plan_id': 'P1',
                                   'fault_id': 'F1'}))
    assert second['feasible'] is True
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM power_switching_plan WHERE id='P1'").fetchone()[0] == 1
    history = [json.loads(x['body']) for x in conn.execute(
        "SELECT body FROM power_switching_plan_history WHERE id LIKE 'P1:%'")]
    assert len(history) >= 2
    assert any(h['schedule_fingerprint'] == first['schedule_fingerprint'] for h in history)


# ---------------------------------------------------------------------------
# report contents
# ---------------------------------------------------------------------------

def test_report_lists_each_step_phase_neutral_surge_and_minimal_reasons(tmp_path):
    db = make_floor(tmp_path, ORDINARY)
    planned = do_plan(db, tmp_path)
    run_execution(db, tmp_path, planned,
                  ['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'], observed={'A': 6})
    run_proof(db, tmp_path)
    view = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    plan = view['power']['plans'][0]
    assert len(plan['steps']) == 5
    close = plan['steps'][3]
    assert close['switch_states']['T1|A'] == 'closed'
    assert close['feeder_phase_amps']['B']['A'] == pytest.approx(6.0)
    assert close['neutral_amps']['B'] == pytest.approx(6.0)
    assert close['instantaneous_amps']['B']['A'] == pytest.approx(8.0)
    assert close['surges'][0]['surge_amp'] == pytest.approx(2.0)
    assert plan['services'][0]['minimal_reasons'] == []
    assert plan['planned_outage_minutes']['R2'] > 0


def test_report_for_infeasible_plan_names_the_minimal_reason(tmp_path):
    floor = json.loads(json.dumps(ROOT_FLOOR))
    floor['room_loads'][1]['classes'] = {'critical': 3}
    floor['room_loads'][1]['startup'] = {'critical': 3}
    services = {
        'R2': ORDINARY['R2'],
        'R1': {'id': 'S1', 'room': 'R1', 'lesson': 'L1',
               'contract': '在线考试', 'load_classes': ['critical'],
               'signed_at': '2026-01-01T08:00:00'}}
    db = make_floor(tmp_path, services, floor=floor)
    do_plan(db, tmp_path)
    view = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    plan = view['power']['plans'][0]
    assert plan['feasible'] is False
    assert plan['reasons'] == ['non_sheddable_shared_feeder_load']
    for verdict in plan['services']:
        assert verdict['status'] == '不能履约'
        assert verdict['minimal_reasons']
