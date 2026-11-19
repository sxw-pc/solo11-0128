"""Cross-campus travel-chain acceptance tests.

Covers the two real intake contracts on the same timetable (walk service
confirmable, accessible shuttle pending), dual teacher/class itinerary
checks, remote teaching points, exact-fit and cross-day gaps, missing and
closed routes, approval-time late lessons and route version changes, plus
idempotent re-import.
"""
import json
import pytest
from schedulehandoff import operations, planner, travel, report
from schedulehandoff.store import connect


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return p


EAST, WEST = '东校区', '西校区'


def routes_payload(status_walk='已核定', walk_version='r1', status_bus='已核定'):
    return {'version': 'v1', 'rows': [
        {'id': 'RT-EW-WALK', 'version': walk_version, 'origin': EAST, 'destination': WEST,
         'service': 'walk', 'minutes': 8, 'status': status_walk},
        {'id': 'RT-WE-WALK', 'version': 'r1', 'origin': WEST, 'destination': EAST,
         'service': 'walk', 'minutes': 8, 'status': '已核定'},
        {'id': 'RT-EW-BUS', 'version': 'r1', 'origin': EAST, 'destination': WEST,
         'service': 'shuttle', 'minutes': 20, 'status': status_bus},
        {'id': 'RT-EW-STAFF', 'version': 'r1', 'origin': EAST, 'destination': WEST,
         'service': 'staff', 'minutes': 20, 'status': '已核定'},
        {'id': 'RT-WE-STAFF', 'version': 'r1', 'origin': WEST, 'destination': EAST,
         'service': 'staff', 'minutes': 20, 'status': '已核定'}]}


def seed_two_campus(root, db, walk_service='walk', bus_service='shuttle',
                    walk_status='已核定', walk_version='r1', bus_status='已核定'):
    """Original 10:00 East lesson, disrupted 10:10 West lesson, both campuses."""
    travel.import_sites(db, write(root, 'sites.json', {'version': 'v1', 'rows': [
        {'id': 'S-EAST', 'kind': 'campus', 'ref': EAST, 'site': EAST},
        {'id': 'S-WEST', 'kind': 'campus', 'ref': WEST, 'site': WEST},
        {'id': 'S-RE', 'kind': 'room', 'ref': 'RE1', 'site': EAST},
        {'id': 'S-RE2', 'kind': 'room', 'ref': 'RE2', 'site': EAST},
        {'id': 'S-RE3', 'kind': 'room', 'ref': 'RE3', 'site': EAST},
        {'id': 'S-RE4', 'kind': 'room', 'ref': 'RE4', 'site': EAST},
        {'id': 'S-RE5', 'kind': 'room', 'ref': 'RE5', 'site': EAST},
        {'id': 'S-RE6', 'kind': 'room', 'ref': 'RE6', 'site': EAST},
        {'id': 'S-RW', 'kind': 'room', 'ref': 'RW1', 'site': WEST}]}))
    travel.import_routes(db, write(root, 'routes.json',
                                   routes_payload(walk_status, walk_version, bus_status)))
    travel.import_services(db, write(root, 'services.json', {'version': 'v1', 'rows': [
        {'class': 'C-WALK', 'service': walk_service, 'agreement_id': 'A-WALK'},
        {'class': 'C-BUS', 'service': bus_service, 'agreement_id': 'A-BUS'}]}))
    operations.first(db, write(root, 'rooms.json', {'version': 'v1', 'rows': [
        {'id': 'L0', 'room': 'RE1', 'class': 'C-WALK', 'room_site': EAST,
         'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00', 'capacity': 35, 'enrolled': 30},
        {'id': 'L0B', 'room': 'RE2', 'class': 'C-BUS', 'room_site': EAST,
         'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00', 'capacity': 35, 'enrolled': 12}]}))


def west_teacher(root, db, teacher='T1', lesson_west='LW', end='2026-01-01T08:50:00'):
    """Teacher already teaching on the West campus before the 10:10 lesson."""
    travel.import_services(db, write(root, f'west-svc-{teacher}.json', {'version': 'v1', 'rows': [
        {'class': f'C-W0-{teacher}', 'service': 'walk', 'agreement_id': f'A-W0-{teacher}'}]}))
    operations.first(db, write(root, f'west-room-{teacher}.json', {'version': 'v1', 'rows': [
        {'id': f'LW0-{teacher}', 'room': 'RW1', 'class': f'C-W0-{teacher}', 'room_site': WEST,
         'start': '2026-01-01T08:00:00', 'end': end, 'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(root, f'west-staff-{teacher}.json', {'version': 'v1', 'rows': [
        {'id': f'SW0-{teacher}', 'lesson': f'LW0-{teacher}', 'teacher': teacher, 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True, 'teaching_site': WEST,
         'room_site': WEST, 'class': f'C-W0-{teacher}', 'room': 'RW1'}]}))



def west_request(plan_id, klass, teachers=('T1',)):
    return {'version': 'v1', 'plan_id': plan_id,
            'rooms': [{'id': 'RW1', 'capacity': 30, 'site': WEST}],
            'rows': [{'id': 'LD', 'subject': 'math', 'class': klass,
                      'start': '2026-01-01T10:10:00', 'end': '2026-01-01T11:00:00',
                      'enrolled': 20, 'candidate_rooms': ['RW1'],
                      'candidate_teachers': list(teachers)}]}


def approval_file(root, plan_id, version='v1'):
    return write(root, f'approval-{plan_id}.json',
                 {'version': 'v1', 'rows': [{'plan_id': plan_id, 'version': version, 'approver': 'principal'}]})


def test_walk_service_contract_is_confirmable_approved_and_effective(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    result = planner.plan(db, write(tmp_path, 'plan.json', west_request('PW', 'C-WALK')))
    assert result['status'] == '可确认'
    chain = next(c for c in result['travel_chains'] if c['kind'] == 'class')
    leg = chain['legs'][0]
    assert (leg['origin'], leg['destination'], leg['service'], leg['required_minutes']) == (
        EAST, WEST, 'walk', 8)
    assert leg['gap_minutes'] == 10 and leg['feasible'] and leg['route_version'] == 'r1'
    assert planner.approve(db, approval_file(tmp_path, 'PW'))[0]['status'] == '已应用'
    conn = connect(db)
    assert conn.execute("SELECT status FROM room_slot WHERE id='PW:LD'").fetchone()[0] == '可用'
    assert conn.execute("SELECT status FROM substitution WHERE id='PW:LD'").fetchone()[0] == '已委托'
    notices = {'version': 'v1', 'rows': [{'id': 'N1', 'lesson': 'PW:LD', 'required_classes': ['C-WALK'],
                                          'receipts': [{'class': 'C-WALK', 'version': 'v1'}]}]}
    assert operations.third(db, write(tmp_path, 'notices.json', notices))[0]['status'] == '换班生效'


def test_accessible_shuttle_contract_stays_pending_and_blocks_effectiveness(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    result = planner.plan(db, write(tmp_path, 'plan.json', west_request('PB', 'C-BUS')))
    assert result['status'] == '待协调' and result['reason'] == 'travel_unresolved'
    assert result['assignments'] == []
    eliminated = result['eliminations']['LD'][0]
    assert eliminated['reason'] == 'travel_gap_insufficient'
    assert (eliminated['service'], eliminated['required_minutes'], eliminated['gap_minutes']) == (
        'shuttle', 20, 10)
    # planner cannot be bypassed by pretending another service applies
    conn = connect(db)
    assert planner.approve(db, approval_file(tmp_path, 'PB'))[0]['reason'] == 'plan_incomplete'
    assert conn.execute("SELECT count(*) FROM room_slot WHERE id='PB:LD'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM substitution WHERE id='PB:LD'").fetchone()[0] == 0
    notices = {'version': 'v1', 'rows': [{'id': 'N2', 'lesson': 'PB:LD', 'required_classes': ['C-BUS'],
                                          'receipts': [{'class': 'C-BUS', 'version': 'v1'}]}]}
    assert operations.third(db, write(tmp_path, 'notices.json', notices))[0]['reason'] == 'schedule_unresolved'


def test_teacher_chain_prefers_teacher_already_on_west_campus(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db, teacher='T2', end='2026-01-01T09:55:00')
    # T1 finishes an East lesson at 10:00; the West lesson starts 10:05, staff route needs 20 min.
    operations.first(db, write(tmp_path, 'east-t1.json', {'version': 'v1', 'rows': [
        {'id': 'LE-T1', 'room': 'RE3', 'class': 'C-ET1', 'room_site': EAST,
         'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00', 'capacity': 35, 'enrolled': 30}]}))
    travel.import_services(db, write(tmp_path, 'svc-et1.json', {'version': 'v1', 'rows': [
        {'class': 'C-ET1', 'service': 'walk', 'agreement_id': 'A-ET1'}]}))
    operations.second(db, write(tmp_path, 'east-t1-staff.json', {'version': 'v1', 'rows': [
        {'id': 'SE-T1', 'lesson': 'LE-T1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True, 'teaching_site': EAST,
         'room_site': EAST, 'class': 'C-ET1', 'room': 'RE3'}]}))
    # The disrupted class is already on West campus, so only the teacher itineraries decide.
    travel.import_services(db, write(tmp_path, 'svc-pt.json', {'version': 'v1', 'rows': [
        {'class': 'C-PT', 'service': 'walk', 'agreement_id': 'A-PT'}]}))
    operations.first(db, write(tmp_path, 'west-pt.json', {'version': 'v1', 'rows': [
        {'id': 'LPT0', 'room': 'RW1', 'class': 'C-PT', 'room_site': WEST,
         'start': '2026-01-01T08:30:00', 'end': '2026-01-01T09:30:00', 'capacity': 35, 'enrolled': 30}]}))
    request = west_request('PT', 'C-PT', teachers=('T1', 'T2'))
    request['rows'][0]['start'] = '2026-01-01T10:05:00'
    result = planner.plan(db, write(tmp_path, 'plan.json', request))
    assert result['status'] == '可确认'
    assert result['assignments'][0]['teacher'] == 'T2'
    t1 = next(e for e in result['eliminations']['LD'] if e['teacher'] == 'T1')
    assert t1['reason'] == 'travel_gap_insufficient' and t1['required_minutes'] == 20
    teacher_chain = next(c for c in result['travel_chains'] if c['kind'] == 'teacher')
    assert teacher_chain['traveller'] == 'T2' and teacher_chain['feasible']


def test_exact_fit_gap_is_feasible_and_cross_day_chain_uses_real_datetimes(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    request = west_request('PE', 'C-WALK')
    request['rows'][0]['start'] = '2026-01-01T10:08:00'  # exactly the 8-minute walk
    exact = planner.plan(db, write(tmp_path, 'plan-exact.json', request))
    assert exact['status'] == '可确认'
    class_leg = next(c for c in exact['travel_chains'] if c['kind'] == 'class')['legs'][0]
    assert class_leg['gap_minutes'] == 8 and class_leg['required_minutes'] == 8 and class_leg['feasible']
    # Cross-day: East lesson ends 23:55, West lesson starts 00:05 next day.
    night = {'version': 'v1', 'plan_id': 'PN',
             'rooms': [{'id': 'RW1', 'capacity': 30, 'site': WEST}],
             'rows': [{'id': 'LN', 'subject': 'math', 'class': 'C-WALK',
                       'start': '2026-01-02T00:05:00', 'end': '2026-01-02T00:50:00',
                       'enrolled': 20, 'candidate_rooms': ['RW1'], 'candidate_teachers': ['T1']}]}
    operations.first(db, write(tmp_path, 'night-east.json', {'version': 'v1', 'rows': [
        {'id': 'LNE', 'room': 'RE2', 'class': 'C-WALK', 'room_site': EAST,
         'start': '2026-01-01T23:00:00', 'end': '2026-01-01T23:55:00', 'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(tmp_path, 'night-staff.json', {'version': 'v1', 'rows': [
        {'id': 'SNE', 'lesson': 'LNE', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True, 'teaching_site': EAST,
         'room_site': EAST, 'class': 'C-WALK', 'room': 'RE2'}]}))
    night_result = planner.plan(db, write(tmp_path, 'plan-night.json', night))
    assert night_result['status'] == '可确认'
    night_leg = next(c for c in night_result['travel_chains'] if c['kind'] == 'class')['legs'][-1]
    assert night_leg['gap_minutes'] == 10 and night_leg['feasible']


def test_remote_teacher_walks_from_actual_teaching_point(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    travel.import_services(db, write(tmp_path, 'svc-remote.json', {'version': 'v1', 'rows': [
        {'class': 'C-REM', 'service': 'walk', 'agreement_id': 'A-REM'}]}))
    # Remote lesson: the class sits East, but T1 actually teaches from a West studio.
    operations.first(db, write(tmp_path, 'remote-room.json', {'version': 'v1', 'rows': [
        {'id': 'LR', 'room': 'RE2', 'class': 'C-REM', 'room_site': EAST, 'remote': True,
         'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00', 'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(tmp_path, 'remote-staff.json', {'version': 'v1', 'rows': [
        {'id': 'SR', 'lesson': 'LR', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True, 'remote': True,
         'teaching_site': WEST, 'room_site': EAST, 'class': 'C-REM', 'room': 'RE2'}]}))
    request = west_request('PR', 'C-REM')
    request['rows'][0]['class'] = 'C-REM'
    result = planner.plan(db, write(tmp_path, 'plan-remote.json', request))
    # Teacher stays West (same site); the class must walk East->West and needs the walk route.
    teacher_chain = next(c for c in result['travel_chains'] if c['kind'] == 'teacher')
    class_chain = next(c for c in result['travel_chains'] if c['kind'] == 'class')
    assert teacher_chain['feasible'] and teacher_chain['legs'][0]['reason'] == 'same_site'
    assert class_chain['feasible'] and class_chain['legs'][0]['route_id'] == 'RT-EW-WALK'


def test_missing_route_keeps_plan_pending_with_traceable_reason(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db, walk_service='walk')
    # A class whose intake service has no approved route on file.
    travel.import_services(db, write(tmp_path, 'svc-other.json', {'version': 'v1', 'rows': [
        {'class': 'C-NOROUTE', 'service': 'accessible-cart', 'agreement_id': 'A-OTHER'}]}))
    operations.first(db, write(tmp_path, 'noroute-room.json', {'version': 'v1', 'rows': [
        {'id': 'LNR', 'room': 'RE2', 'class': 'C-NOROUTE', 'room_site': EAST,
         'start': '2026-01-01T09:00:00', 'end': '2026-01-01T10:00:00', 'capacity': 35, 'enrolled': 12}]}))
    west_teacher(tmp_path, db)
    result = planner.plan(db, write(tmp_path, 'plan.json', west_request('PM', 'C-NOROUTE')))
    assert result['status'] == '待协调'
    assert result['eliminations']['LD'][0]['reason'] == 'route_missing'


def test_route_closed_after_plan_is_rejected_at_approval(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    plan_file = write(tmp_path, 'plan.json', west_request('PC', 'C-WALK'))
    assert planner.plan(db, plan_file)['status'] == '可确认'
    travel.import_routes(db, write(tmp_path, 'routes-closed.json', {'version': 'v1', 'rows': [
        {'id': 'RT-EW-WALK', 'version': 'r2', 'origin': EAST, 'destination': WEST,
         'service': 'walk', 'minutes': 8, 'status': '已关闭'}]}))
    decision = planner.approve(db, approval_file(tmp_path, 'PC'))[0]
    assert decision['status'] == '拒绝' and decision['reason'] == 'travel_route_closed'
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM room_slot WHERE id='PC:LD'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM reschedule_plan WHERE id='PC'").fetchone()[0] == '可确认'


def test_route_version_change_that_breaks_gap_is_rejected_at_approval(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    assert planner.plan(db, write(tmp_path, 'plan.json', west_request('PV', 'C-WALK')))['status'] == '可确认'
    # New approved version of the same route now takes 12 minutes; gap is only 10.
    travel.import_routes(db, write(tmp_path, 'routes-v2.json', {'version': 'v1', 'rows': [
        {'id': 'RT-EW-WALK', 'version': 'r2', 'origin': EAST, 'destination': WEST,
         'service': 'walk', 'minutes': 12, 'status': '已核定'}]}))
    decision = planner.approve(db, approval_file(tmp_path, 'PV'))[0]
    assert decision['status'] == '拒绝' and decision['reason'] == 'travel_route_version_changed'


def test_late_lesson_added_after_plan_is_rejected_at_approval(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    assert planner.plan(db, write(tmp_path, 'plan.json', west_request('PL', 'C-WALK')))['status'] == '可确认'
    # After planning, T1 picks up an East lesson ending 10:05, stranding them from the 10:10 West class.
    operations.first(db, write(tmp_path, 'late-room.json', {'version': 'v1', 'rows': [
        {'id': 'LATE1', 'room': 'RE2', 'class': 'C-LATE', 'room_site': EAST,
         'start': '2026-01-01T09:30:00', 'end': '2026-01-01T10:05:00', 'capacity': 35, 'enrolled': 30}]}))
    operations.second(db, write(tmp_path, 'late-staff.json', {'version': 'v1', 'rows': [
        {'id': 'SLATE', 'lesson': 'LATE1', 'teacher': 'T1', 'subject': 'math',
         'qualified': ['math'], 'busy': [], 'accepted': True, 'teaching_site': EAST,
         'room_site': EAST, 'class': 'C-LATE', 'room': 'RE2'}]}))
    decision = planner.approve(db, approval_file(tmp_path, 'PL'))[0]
    assert decision['status'] == '拒绝' and decision['reason'] == 'travel_late_lesson'


def test_repeated_imports_are_idempotent_and_do_not_double_occupy(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    route_file = tmp_path / 'routes.json'
    first = travel.import_routes(db, route_file)
    again = travel.import_routes(db, route_file)
    assert first == again
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM travel_route').fetchone()[0] == 5
    plan_file = write(tmp_path, 'plan.json', west_request('PI', 'C-WALK'))
    assert planner.plan(db, plan_file)['status'] == '可确认'
    assert planner.approve(db, approval_file(tmp_path, 'PI'))[0]['status'] == '已应用'
    assert planner.approve(db, approval_file(tmp_path, 'PI'))[0]['status'] == '已应用'
    assert conn.execute("SELECT count(*) FROM room_slot WHERE id='PI:LD'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM substitution WHERE id='PI:LD'").fetchone()[0] == 1
    # Replaying the plan after approval returns the applied state without duplicating anything.
    replayed = planner.plan(db, plan_file)
    assert replayed['status'] == '已应用' and len(replayed['assignments']) == 1


def test_report_reads_back_plans_eliminations_and_approval_changes(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    west_teacher(tmp_path, db)
    planner.plan(db, write(tmp_path, 'plan.json', west_request('PR2', 'C-WALK')))
    travel.import_routes(db, write(tmp_path, 'routes-closed.json', {'version': 'v1', 'rows': [
        {'id': 'RT-EW-WALK', 'version': 'r2', 'origin': EAST, 'destination': WEST,
         'service': 'walk', 'minutes': 8, 'status': '已关闭'}]}))
    planner.approve(db, approval_file(tmp_path, 'PR2'))
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    summary = data['travel']
    assert any(r['id'] == 'RT-EW-WALK' and r['status'] == '已关闭' for r in summary['routes'])
    plan = next(p for p in summary['plans'] if p['plan_id'] == 'PR2')
    assert plan['assignments'][0]['room_site'] == WEST
    approval_chain = next(c for c in summary['chains'] if c['id'].startswith('PR2:approval'))
    assert approval_chain['reason'] == 'travel_route_closed'
    assert any(leg['reason'] == 'route_missing' for leg in approval_chain['legs'])
    assert 'travel_chain' in data['entities'] and data['entities']['travel_chain']


def test_service_cannot_be_silently_changed_on_reimport(tmp_path):
    db = tmp_path / 'db'
    seed_two_campus(tmp_path, db)
    with pytest.raises(ValueError, match='identity_conflict'):
        travel.import_services(db, write(tmp_path, 'svc-tamper.json', {'version': 'v2', 'rows': [
            {'class': 'C-BUS', 'service': 'walk', 'agreement_id': 'A-BUS'}]}))
