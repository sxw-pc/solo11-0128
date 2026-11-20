import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from schedulehandoff import air, operations, report, ventilation
from schedulehandoff.store import connect

ROOT = Path(__file__).resolve().parents[1]
W = ('2026-01-01T09:00:00', '2026-01-01T10:00:00')


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


def slot(id, room='R1', start=W[0], end=W[1], capacity=35, enrolled=30):
    return dict(id=id, room=room, start=start, end=end, capacity=capacity, enrolled=enrolled)


def base(tmp_path, rooms=('R1',)):
    """Schedule slot (enrolled 30) + venue profile + calibration + the two
    mandates, all registered before any air sample is imported."""
    db = tmp_path / 'db'
    operations.first(db, write(tmp_path, 'rooms.json', {'version': 'v1', 'rows': [slot('L1')]}))
    venue_rows = [
        {'kind': 'room', 'id': 'PR1', 'room': 'R1', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR1'},
        {'kind': 'room', 'id': 'PR2', 'room': 'R2', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR2'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 7, 'rate_hi': 13},
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': venue_rows}))
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SR1', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0},
        {'id': 'C2', 'sensor': 'SR2', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0}]}))
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '预防性巡检', 'threshold': 0.25},
        {'id': 'M2', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '达标公示', 'threshold': 0.25}]}))
    return db


def samples_r1(tmp_path, name='samples.json'):
    return write(tmp_path, name, {'version': 'v1', 'rows': [
        {'id': 'a1', 'room': 'R1', 'at': '2026-01-01T09:00:00', 'ppm': 800},
        {'id': 'a2', 'room': 'R1', 'at': '2026-01-01T09:30:00', 'ppm': 900}]})


def assess_map(db):
    return {r['mandate']: r for r in air.assess(db)}


def test_simplex_exact_vertex_and_infeasible():
    f = ventilation.F
    state, val, x = ventilation.simplex_max(
        [([f(1), f(2)], None, f(4)), ([f(2), f(1)], None, f(4))], 2, [f(1), f(1)])
    assert (state, val) == ('optimal', Fraction(8, 3))
    assert x == [Fraction(4, 3), Fraction(4, 3)]
    state, _, _ = ventilation.simplex_max([([f(1)], None, f(1)), ([f(1)], f(2), None)], 1, [f(1)])
    assert state == 'infeasible'


def test_correct_series_applies_shared_lag_and_zero():
    series = ventilation.correct_series([('t0', 800), ('t1', 1000)], ventilation.F(0), ventilation.F('0.5'))
    assert series == [('t0', Fraction(800)), ('t1', Fraction(1200))]
    shifted = ventilation.correct_series([('t0', 800), ('t1', 1000)], ventilation.F(50), ventilation.F(0))
    assert shifted == [('t0', Fraction(750)), ('t1', Fraction(950))]


def test_enrolled_thirty_but_actual_occupancy_uncertain(tmp_path):
    db = base(tmp_path)
    air.run(db, samples_r1(tmp_path))
    result = assess_map(db)
    assert result['M1']['status'] == '派查'
    assert result['M2']['status'] == '待证'
    lo, hi = result['M1']['k_range']
    assert (lo, hi) == (0.0, 0.725)  # occupancy anywhere in [0, 30]
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    current = next(m for m in view['ventilation']['mandates'] if m['id'] == 'M1')['current']['body']
    segment = current['segments'][0]
    assert segment['occupancy'] == [0, 30]  # enrolled is only the expected headcount
    assert segment['outdoor'] == 400.0


def test_two_mandates_same_readings_different_decisions(tmp_path):
    """The minimal counterexample: 400 outside, 800 -> 900 inside, source
    term in [140, 260]; exchange ratio provably in [0.1, 0.4], so the
    pre-agreed 0.25 is neither refuted nor proven."""
    db = base(tmp_path)
    air.run(db, samples_r1(tmp_path))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'at': '2026-01-01T08:55:00', 'present': 20}]}))
    result = assess_map(db)
    assert result['M1']['status'] == '派查'       # an explanation below 0.25 exists
    assert result['M2']['status'] == '待证'       # not every explanation reaches 0.25
    assert result['M1']['k_range'] == [0.1, 0.4]
    assert result['M2']['k_range'] == [0.1, 0.4]
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    current = next(m for m in view['ventilation']['mandates'] if m['id'] == 'M1')['current']['body']
    low, high = current['explanations']['low'], current['explanations']['high']
    assert low['k']['R1'] == pytest.approx(0.1) and low['segments'][0]['source'] == pytest.approx(140)
    assert high['k']['R1'] == pytest.approx(0.4) and high['segments'][0]['source'] == pytest.approx(260)
    assert low['segments'][0]['occupancy'] == pytest.approx(20) and low['r']['lecture'] == pytest.approx(7)
    assert high['r']['lecture'] == pytest.approx(13)
    # dispatch is not proof of non-compliance: compliant explanations exist
    assert current['k_range'][1] >= 0.25
    assert any('不等于已证不达标' in note for note in current['notes'])


def test_shared_activity_rate_from_other_room_narrows_range(tmp_path):
    """Door counts confirming actual occupancy plus the shared source-rate
    calibration from another room with the same activity narrow the range;
    conclusions are recomputed on the new evidence."""
    db = base(tmp_path)
    air.run(db, samples_r1(tmp_path))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'at': '2026-01-01T08:55:00', 'present': 20}]}))
    assert assess_map(db)['M1']['k_range'] == [0.1, 0.4]
    air.run(db, write(tmp_path, 'samples-r2.json', {'version': 'v1', 'rows': [
        {'id': 'b1', 'room': 'R2', 'at': '2026-01-01T09:00:00', 'ppm': 800},
        {'id': 'b2', 'room': 'R2', 'at': '2026-01-01T09:30:00', 'ppm': 1000}]}))
    air.doors(db, write(tmp_path, 'doors-r2.json', {'version': 'v1', 'rows': [
        {'id': 'D2', 'room': 'R2', 'at': '2026-01-01T08:55:00', 'present': 30}]}))
    air.fanlog(db, write(tmp_path, 'fan.json', {'version': 'v1', 'rows': [
        {'id': 'F1', 'room': 'R2', 'start': W[0], 'end': W[1], 'k_lo': 0.25, 'k_hi': 0.25}]}))
    result = assess_map(db)
    assert result['M1']['k_range'] == [0.25, 0.25]  # shared rate pinned to 10 ppm/person
    assert result['M1']['status'] == '免查'
    assert result['M2']['status'] == '可签发'
    conn = connect(db)
    decisions = conn.execute(
        "SELECT status FROM decision_log WHERE subsystem='通风委托' AND subject='M1' ORDER BY id").fetchall()
    assert [d[0] for d in decisions] == ['已登记', '派查', '免查']  # original decision preserved


def test_late_door_count_recomputes_only_affected_window(tmp_path):
    db = tmp_path / 'db'
    operations.first(db, write(tmp_path, 'rooms.json', {'version': 'v1', 'rows': [slot('L1')]}))
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': [
        {'kind': 'room', 'id': 'PR1', 'room': 'R1', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR1'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 7, 'rate_hi': 13},
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]}))
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SR1', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0}]}))
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '预防性巡检', 'threshold': 0.25},
        {'id': 'M3', 'room': 'R1', 'start': '2026-01-01T10:00:00', 'end': '2026-01-01T11:00:00',
         'kind': '预防性巡检', 'threshold': 0.25}]}))
    air.run(db, write(tmp_path, 'samples.json', {'version': 'v1', 'rows': [
        {'id': 'a1', 'room': 'R1', 'at': '2026-01-01T09:00:00', 'ppm': 800},
        {'id': 'a2', 'room': 'R1', 'at': '2026-01-01T09:30:00', 'ppm': 900},
        {'id': 'a3', 'room': 'R1', 'at': '2026-01-01T10:00:00', 'ppm': 1000},
        {'id': 'a4', 'room': 'R1', 'at': '2026-01-01T10:30:00', 'ppm': 940}]}))
    first = assess_map(db)
    assert first['M1']['k_range'] == [0.0, 0.58] and first['M3']['k_range'] == [0.1, 0.1]
    # a late period-total door record for the first window only
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'start': W[0], 'end': W[1], 'total': 25}]}))
    second = assess_map(db)
    assert second['M1']['changed'] and second['M1']['k_range'] == [0.0, 0.45]
    assert not second['M3']['changed']  # unaffected window not recomputed
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM air_assessment WHERE mandate_id='M1'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM air_assessment WHERE mandate_id='M1' AND superseded=1").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM air_assessment WHERE mandate_id='M3'").fetchone()[0] == 1
    kept = conn.execute("SELECT body FROM air_assessment WHERE mandate_id='M1' AND superseded=1").fetchone()[0]
    assert json.loads(kept)['k_range'] == [0.0, 0.58]  # old parameter range retained


def test_period_total_does_not_fabricate_per_minute_occupancy(tmp_path):
    db = base(tmp_path)
    air.run(db, samples_r1(tmp_path))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'start': W[0], 'end': W[1], 'total': 25}]}))
    result = assess_map(db)
    assert result['M1']['k_range'] == [0.0, 0.5625]
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    current = next(m for m in view['ventilation']['mandates'] if m['id'] == 'M1')['current']['body']
    assert current['segments'][0]['occupancy'] == [0, 25]  # an interval, not invented minutes


def test_shared_return_air_raises_neighbor_without_fault(tmp_path):
    db = tmp_path / 'db'
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': [
        {'kind': 'room', 'id': 'PA', 'room': 'RA', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SA'},
        {'kind': 'room', 'id': 'PB', 'room': 'RB', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SB'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 7, 'rate_hi': 13},
        {'kind': 'link', 'id': 'L1', 'from': 'RA', 'to': 'RB', 'flow_lo': 0, 'flow_hi': 0.5},
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]}))
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SA', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0},
        {'id': 'C2', 'sensor': 'SB', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0}]}))
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'MB', 'room': 'RB', 'start': W[0], 'end': W[1], 'kind': '达标公示', 'threshold': 0.25}]}))
    air.run(db, write(tmp_path, 'samples.json', {'version': 'v1', 'rows': [
        {'id': 'a1', 'room': 'RA', 'at': '2026-01-01T09:00:00', 'ppm': 1200},
        {'id': 'a2', 'room': 'RA', 'at': '2026-01-01T09:30:00', 'ppm': 1200},
        {'id': 'b1', 'room': 'RB', 'at': '2026-01-01T09:00:00', 'ppm': 500},
        {'id': 'b2', 'room': 'RB', 'at': '2026-01-01T09:30:00', 'ppm': 700}]}))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'RA', 'at': '2026-01-01T08:55:00', 'present': 30}]}))
    result = assess_map(db)
    # RB's rise is explainable by RA's return air: RB is not judged faulty
    assert result['MB']['status'] == '待证'
    lo, hi = result['MB']['k_range']
    assert lo == 0.0 and hi == pytest.approx(113 / 80)
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    current = next(m for m in view['ventilation']['mandates'] if m['id'] == 'MB')['current']['body']
    assert current['k_rooms']['RA'][1] == pytest.approx(113 / 560)
    high = current['explanations']['high']
    g = high['g']['L1']
    assert g == pytest.approx(39 / 80) and g > 0
    # conservation: the same shared flow closes both rooms' balances
    seg_a = next(s for s in high['segments'] if s['room'] == 'RA')
    assert seg_a['source'] - 800 * high['k']['RA'] - 800 * g == pytest.approx(0)
    assert -100 * high['k']['RB'] + 700 * g == pytest.approx(200)
    assert current['links'][0]['conservation']


def test_calibration_parameters_cannot_switch_per_reading(tmp_path):
    """A zero offset shared across the whole series forces k = 0 here;
    switching calibration per reading would admit k = 0.25."""
    db = tmp_path / 'db'
    operations.first(db, write(tmp_path, 'rooms.json', {'version': 'v1', 'rows': [
        slot('L1', enrolled=10)]}))
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': [
        {'kind': 'room', 'id': 'PR1', 'room': 'R1', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR1'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 10, 'rate_hi': 10},
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]}))
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SR1', 'zero_lo': 0, 'zero_hi': 100, 'lag_lo': 0, 'lag_hi': 0}]}))
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '达标公示', 'threshold': 0.25}]}))
    air.run(db, write(tmp_path, 'samples.json', {'version': 'v1', 'rows': [
        {'id': 'a1', 'room': 'R1', 'at': '2026-01-01T09:00:00', 'ppm': 900},
        {'id': 'a2', 'room': 'R1', 'at': '2026-01-01T09:20:00', 'ppm': 1000},
        {'id': 'a3', 'room': 'R1', 'at': '2026-01-01T09:40:00', 'ppm': 1100}]}))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'at': '2026-01-01T08:55:00', 'present': 10}]}))
    result = assess_map(db)
    assert result['M1']['k_range'] == [0.0, 0.0]  # shared zero offset, not per-reading
    assert result['M1']['status'] == '不签发'
    view = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    current = next(m for m in view['ventilation']['mandates'] if m['id'] == 'M1')['current']['body']
    assert current['sensor_cal']['SR1']['zero'] == [0, 100]
    assert current['sensor_cal']['SR1']['shared'] is True


def test_replay_and_same_identity_different_content_conflict(tmp_path):
    db = base(tmp_path)
    samples = samples_r1(tmp_path)
    first = air.run(db, samples)
    assert air.run(db, samples) == first  # replay is stable
    before = assess_map(db)
    after = assess_map(db)
    assert all(not r['changed'] for r in after.values())
    assert before['M1']['status'] == after['M1']['status']
    conn = connect(db)
    counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('air_assessment', 'decision_log', 'export_file', 'air_sample')}
    with pytest.raises(ValueError):
        air.run(db, write(tmp_path, 'conflict.json', {'version': 'v2', 'rows': [
            {'id': 'a1', 'room': 'R1', 'at': '2026-01-01T09:00:00', 'ppm': 850}]}))
    with pytest.raises(ValueError):
        air.mandate(db, write(tmp_path, 'mandate2.json', {'version': 'v2', 'rows': [
            {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '达标公示', 'threshold': 0.3}]}))
    assert conn.execute("SELECT COUNT(*) FROM issue WHERE code='identity_conflict'").fetchone()[0] == 2
    assert json.loads(conn.execute("SELECT body FROM air_sample WHERE id='a1'").fetchone()[0])['ppm'] == 800
    assert json.loads(conn.execute("SELECT body FROM air_mandate WHERE id='M1'").fetchone()[0])['kind'] == '预防性巡检'
    # the conflicting sources are kept as evidence; nothing else changed
    assert conn.execute('SELECT COUNT(*) FROM export_file').fetchone()[0] == counts['export_file'] + 2
    for table in ('air_assessment', 'decision_log', 'air_sample'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == counts[table]


def test_mandate_cannot_be_fixed_after_sampling(tmp_path):
    db = base(tmp_path)
    air.run(db, samples_r1(tmp_path))
    result = air.mandate(db, write(tmp_path, 'late.json', {'version': 'v1', 'rows': [
        {'id': 'M9', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '达标公示', 'threshold': 0.25}]}))
    assert result[0]['reason'] == 'samples_before_mandate'
    # replaying an already registered mandate after sampling stays accepted
    again = air.mandate(db, tmp_path / 'mandates.json')
    assert [r['status'] for r in again] == ['已登记', '已登记']


def test_contradictory_evidence_is_a_conflict_not_a_verdict(tmp_path):
    db = tmp_path / 'db'
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': [
        {'kind': 'room', 'id': 'PR1', 'room': 'R1', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR1'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 7, 'rate_hi': 13},
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]}))
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SR1', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0}]}))
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '预防性巡检', 'threshold': 0.25}]}))
    # empty room (no slot, nobody counted) yet the concentration rises
    air.run(db, samples_r1(tmp_path))
    result = assess_map(db)
    assert result['M1']['status'] == '证据冲突'
    assert result['M1']['reason'] == 'no_consistent_explanation'


def test_missing_profile_or_outdoor_waits_for_evidence(tmp_path):
    db = tmp_path / 'db'
    air.mandate(db, write(tmp_path, 'mandates.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'room': 'R1', 'start': W[0], 'end': W[1], 'kind': '预防性巡检', 'threshold': 0.25}]}))
    air.run(db, samples_r1(tmp_path))
    result = assess_map(db)
    assert result['M1']['status'] == '待补证据' and result['M1']['reason'] == 'profile_missing'
    air.venue(db, write(tmp_path, 'venue.json', {'version': 'v1', 'rows': [
        {'kind': 'room', 'id': 'PR1', 'room': 'R1', 'volume_m3': 180, 'activity': 'lecture', 'sensor': 'SR1'},
        {'kind': 'activity', 'id': 'lecture', 'rate_lo': 7, 'rate_hi': 13}]}))
    assert assess_map(db)['M1']['reason'] == 'cal_missing'
    air.calibrate(db, write(tmp_path, 'cal.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'sensor': 'SR1', 'zero_lo': 0, 'zero_hi': 0, 'lag_lo': 0, 'lag_hi': 0}]}))
    assert assess_map(db)['M1']['reason'] == 'outdoor_missing'
    air.venue(db, write(tmp_path, 'outdoor.json', {'version': 'v1', 'rows': [
        {'kind': 'outdoor', 'id': 'O1', 'start': '2026-01-01T08:00:00', 'end': '2026-01-01T12:00:00', 'ppm': 400}]}))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'at': '2026-01-01T08:55:00', 'present': 20}]}))
    result = assess_map(db)
    assert result['M1']['status'] == '派查'  # evidence complete, range now computable
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM air_assessment WHERE mandate_id='M1'").fetchone()[0] == 4


def test_air_evidence_never_rewrites_schedule_or_substitute(tmp_path):
    db = base(tmp_path)
    operations.second(db, write(tmp_path, 'staff.json', {'version': 'v1', 'rows': [
        {'id': 'S1', 'lesson': 'L1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True}]}))
    air.run(db, samples_r1(tmp_path))
    air.doors(db, write(tmp_path, 'doors.json', {'version': 'v1', 'rows': [
        {'id': 'D1', 'room': 'R1', 'at': '2026-01-01T08:55:00', 'present': 20}]}))
    assess_map(db)
    conn = connect(db)
    assert conn.execute("SELECT status FROM room_slot WHERE id='L1'").fetchone()[0] == '可用'
    assert conn.execute("SELECT status FROM substitution WHERE id='L1'").fetchone()[0] == '已委托'
    assert conn.execute("SELECT COUNT(*) FROM decision_log WHERE subsystem IN ('教室占用','教师代课')").fetchone()[0] == 2


def test_cli_air_workflow(tmp_path):
    db_path = tmp_path / 'ledger.db'
    base(tmp_path)
    # base() wrote its own db; rerun the chain through the CLI on a fresh db
    for name, command in [('rooms.json', 'room-import'), ('venue.json', 'air-venue'),
                          ('cal.json', 'air-calibrate'), ('mandates.json', 'air-mandate')]:
        subprocess.run([sys.executable, '-m', 'schedulehandoff', command, '--db', str(db_path),
                        str(tmp_path / name)], check=True, capture_output=True, cwd=ROOT)
    air_file = samples_r1(tmp_path)
    subprocess.run([sys.executable, '-m', 'schedulehandoff', 'air-import', '--db', str(db_path),
                    str(air_file)], check=True, capture_output=True, cwd=ROOT)
    out = subprocess.run([sys.executable, '-m', 'schedulehandoff', 'air-assess', '--db', str(db_path)],
                         check=True, capture_output=True, cwd=ROOT, text=True)
    result = {r['mandate']: r for r in json.loads(out.stdout)}
    assert result['M1']['status'] == '派查' and result['M2']['status'] == '待证'
    out = subprocess.run([sys.executable, '-m', 'schedulehandoff', 'report', '--db', str(db_path),
                          '--out', str(tmp_path / 'report.json')], check=True, capture_output=True, cwd=ROOT)
    view = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert view['ventilation']['method']
    assert len(view['ventilation']['mandates']) == 2
