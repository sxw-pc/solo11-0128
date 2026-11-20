"""双校区行程链、版本化路线与冻结到课服务的验收测试。"""
import json
import pytest
from schedulehandoff import operations, planner, travel, report
from schedulehandoff.store import connect

EAST, WEST = '东校区', '西校区'
HOME = '教师居家点'


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return p


def import_routes(db, root, rows, version='v1', name='routes.json'):
    return travel.import_routes(db, write(root, name, {'version': version, 'rows': rows}))


def import_services(db, root, rows, version='v1', name='services.json'):
    return travel.import_services(db, write(root, name, {'version': version, 'rows': rows}))


def import_lesson(db, root, id, klass, location, start, end, room='R1', remote=False,
                  capacity=35, enrolled=30, name='rooms.json'):
    row = {'id': id, 'room': room, 'start': start, 'end': end, 'capacity': capacity,
           'enrolled': enrolled, 'class': klass, 'location': location, 'remote': remote}
    operations.first(db, write(root, name, {'version': 'v1', 'rows': [row]}))
    return row


def import_staff(db, root, lesson, teacher, location, start, end, subject='math',
                 qualified=None, remote=False, name='staff.json', staff_id=None):
    sid = staff_id or f'S-{lesson}'
    row = {'id': sid, 'lesson': lesson, 'teacher': teacher, 'subject': subject,
           'qualified': qualified or [subject], 'busy': [], 'accepted': True,
           'teaching_location': location, 'remote': remote, 'start': start, 'end': end}
    operations.second(db, write(root, name, {'version': 'v1', 'rows': [row]}))
    return row


def reschedule_request(plan_id, lessons, rooms, version='v1'):
    return {'version': version, 'plan_id': plan_id, 'rooms': rooms, 'rows': lessons}


def disrupted(klass, start='2026-01-01T10:10:00', end='2026-01-01T11:10:00',
              teachers=('T1',), rooms=('RW',), remote=False, teaching_location=None):
    return {'id': 'LD', 'class': klass, 'subject': 'math', 'start': start, 'end': end,
            'enrolled': 20, 'candidate_rooms': list(rooms), 'candidate_teachers': list(teachers),
            'remote': remote, 'teaching_location': teaching_location}


WEST_ROOM = {'id': 'RW', 'capacity': 30, 'location': WEST}


def two_campus_routes(walk=8, bus=20):
    return [
        {'id': 'RT-EW-WALK', 'from': EAST, 'to': WEST, 'service': '步行', 'minutes': walk},
        {'id': 'RT-EW-BUS', 'from': EAST, 'to': WEST, 'service': '校车接送', 'minutes': bus},
    ]


def seed_two_campus(tmp_path, service='步行'):
    """同一班级原课 10:00 在东校区结束，受扰课 10:10 在西校区开始；T1 已在西校区。"""
    db = tmp_path / 'db'
    import_routes(db, tmp_path, two_campus_routes())
    import_services(db, tmp_path, [{'class': 'C1', 'service': service}])
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00')
    # T1 已在西校区的已委托安排（与受扰课不重叠，仅决定行程链接入点）
    import_lesson(db, tmp_path, 'LW', 'CW', WEST, '2026-01-01T08:00:00', '2026-01-01T08:50:00',
                  room='RW', name='rooms-w.json')
    import_staff(db, tmp_path, 'LW', 'T1', WEST, '2026-01-01T08:00:00', '2026-01-01T08:50:00',
                 name='staff-w.json')
    return db


def approve_file(tmp_path, plan_id='P1', version='v1', name='approval.json'):
    return write(tmp_path, name, {'version': version,
                                  'rows': [{'plan_id': plan_id, 'version': version, 'approver': 'principal'}]})


def test_walk_class_contract_confirms_approves_and_notice_takes_effect(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    request = reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])
    result = planner.plan(db, write(tmp_path, 'plan.json', request))
    assert result['status'] == '可确认'
    assert result['assignments'][0]['room_location'] == WEST
    assert result['assignments'][0]['teaching_location'] == WEST
    pair = next(r for r in result['itinerary'] if r['chain'] == 'class')
    assert pair['route_id'] == 'RT-EW-WALK' and pair['route_version'] == 'v1'
    assert pair['gap_minutes'] == 10 and pair['required_minutes'] == 8
    assert planner.approve(db, approve_file(tmp_path))[0]['status'] == '已应用'
    notices = {'version': 'v1', 'rows': [{'id': 'N1', 'lesson': 'P1:LD', 'required_classes': ['C1'],
                                          'receipts': [{'class': 'C1', 'version': 'v1'}]}]}
    assert operations.third(db, write(tmp_path, 'notices.json', notices))[0]['status'] == '换班生效'


def test_accessible_bus_contract_same_slot_must_remain_pending(tmp_path):
    db = seed_two_campus(tmp_path, service='校车接送')
    request = reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])
    result = planner.plan(db, write(tmp_path, 'plan.json', request))
    assert result['status'] == '待协调'
    assert result['assignments'] == []
    reason = next(e for e in result['eliminations'] if e['reason'] == 'cannot_reach_next_lesson')
    assert reason['detail']['chain'] == 'class' and reason['detail']['required_minutes'] == 20
    # 不能靠临时改服务放行：服务合同冻结，改后再导直接身份冲突
    with pytest.raises(ValueError, match='identity_conflict'):
        import_services(db, tmp_path, [{'class': 'C1', 'service': '步行'}], name='services-changed.json')
    assert planner.approve(db, approve_file(tmp_path))[0]['reason'] == 'plan_incomplete'


def test_joint_planning_prefers_teacher_already_on_campus(tmp_path):
    db = tmp_path / 'db'
    # 验收口径：东西校区步行需要二十分钟
    import_routes(db, tmp_path, two_campus_routes(walk=20, bus=30))
    import_services(db, tmp_path, [{'class': 'C1', 'service': '步行'}])
    # T1 东校区 10:00 下课
    import_lesson(db, tmp_path, 'L0', 'C0', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00')
    # T2 已在西校区且具同科资质
    import_lesson(db, tmp_path, 'LW', 'CW', WEST, '2026-01-01T08:00:00', '2026-01-01T08:50:00',
                  room='RW', name='rooms-w.json')
    import_staff(db, tmp_path, 'LW', 'T2', WEST, '2026-01-01T08:00:00', '2026-01-01T08:50:00',
                 name='staff-w.json')
    lesson = disrupted('C1', start='2026-01-01T10:05:00', end='2026-01-01T11:05:00',
                       teachers=('T1', 'T2'))
    result = planner.plan(db, write(tmp_path, 'plan.json', reschedule_request('P1', [lesson], [WEST_ROOM])))
    assert result['status'] == '可确认'
    assert result['assignments'][0]['teacher'] == 'T2'
    blocked = next(e for e in result['eliminations']
                   if e['teacher'] == 'T1' and e['reason'] == 'cannot_reach_next_lesson')
    assert blocked['detail']['chain'] == 'teacher' and blocked['detail']['gap_minutes'] == 5
    assert blocked['detail']['required_minutes'] == 20


def test_exactly_enough_minutes_is_feasible(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    # 课间恰好 8 分钟，等于路线所需
    lesson = disrupted('C1', start='2026-01-01T10:08:00', end='2026-01-01T11:08:00')
    result = planner.plan(db, write(tmp_path, 'plan.json', reschedule_request('P1', [lesson], [WEST_ROOM])))
    assert result['status'] == '可确认'
    pair = next(r for r in result['itinerary'] if r['chain'] == 'class')
    assert pair['gap_minutes'] == pair['required_minutes'] == 8


def test_cross_day_adjacency_uses_full_timestamps(tmp_path):
    db = tmp_path / 'db'
    import_routes(db, tmp_path, two_campus_routes())
    import_services(db, tmp_path, [{'class': 'C1', 'service': '步行'}])
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T23:55:00', '2026-01-02T00:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T23:55:00', '2026-01-02T00:00:00')
    lesson = disrupted('C1', start='2026-01-02T00:05:00', end='2026-01-02T01:05:00',
                       teachers=('T1',))
    result = planner.plan(db, write(tmp_path, 'plan.json', reschedule_request('P1', [lesson], [WEST_ROOM])))
    pair = next(r for r in result['itinerary'] if r['chain'] == 'class' and r['from_lesson'] == 'L0')
    assert pair['gap_minutes'] == 5 and pair['reason'] == 'cannot_reach_next_lesson'
    assert result['status'] == '待协调'


def test_remote_teacher_walks_from_actual_teaching_point_class_walks_from_room(tmp_path):
    db = tmp_path / 'db'
    import_routes(db, tmp_path, two_campus_routes() + [
        {'id': 'RT-HOME-WEST', 'from': HOME, 'to': WEST, 'service': '步行', 'minutes': 6}])
    import_services(db, tmp_path, [{'class': 'C1', 'service': '步行'}])
    # 教师在居家点远程授课，班级在东校区教室
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', HOME, '2026-01-01T09:00:00', '2026-01-01T10:00:00', remote=True)
    # 受扰课为线下：教师从居家点走 6 分钟（10 分钟间隙，可行）；班级从东校区走 8 分钟（可行）
    lesson = disrupted('C1', teachers=('T1',))
    result = planner.plan(db, write(tmp_path, 'plan.json', reschedule_request('P1', [lesson], [WEST_ROOM])))
    assert result['status'] == '可确认'
    teacher_pair = next(r for r in result['itinerary'] if r['chain'] == 'teacher')
    class_pair = next(r for r in result['itinerary'] if r['chain'] == 'class')
    assert teacher_pair['from_location'] == HOME and teacher_pair['route_id'] == 'RT-HOME-WEST'
    assert class_pair['from_location'] == EAST and class_pair['route_id'] == 'RT-EW-WALK'


def test_missing_route_keeps_lesson_pending_with_traceable_reason(tmp_path):
    db = tmp_path / 'db'
    import_routes(db, tmp_path, [{'id': 'RT-EW-WALK', 'from': EAST, 'to': WEST,
                                  'service': '步行', 'minutes': 8}])
    import_services(db, tmp_path, [{'class': 'C1', 'service': '校车接送'}])
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00')
    result = planner.plan(db, write(tmp_path, 'plan.json',
                                    reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])))
    assert result['status'] == '待协调'
    assert any(e['reason'] == 'route_missing' for e in result['eliminations'])
    body = json.loads(connect(db).execute("SELECT body FROM reschedule_plan WHERE id='P1'").fetchone()['body'])
    assert any(r['reason'] == 'route_missing' and r['service'] == '校车接送' for r in body['itinerary'])


def test_lesson_added_after_plan_is_rejected_at_approval(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    request = reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])
    assert planner.plan(db, write(tmp_path, 'plan.json', request))['status'] == '可确认'
    # 计划后给 T1 增排一节东校区课（与 L0 不重叠），下课时点使其只剩 1 分钟赶往西校区
    import_lesson(db, tmp_path, 'LATE', 'C9', EAST, '2026-01-01T10:01:00', '2026-01-01T10:09:00',
                  room='RE2', name='rooms-late.json')
    import_staff(db, tmp_path, 'LATE', 'T1', EAST, '2026-01-01T10:01:00', '2026-01-01T10:09:00',
                 name='staff-late.json', staff_id='S-LATE')
    outcome = planner.approve(db, approve_file(tmp_path))[0]
    assert outcome['status'] == '拒绝' and outcome['reason'] == 'lesson_added_since_plan'
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM room_slot WHERE id='P1:LD'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM substitution WHERE id='P1:LD'").fetchone()[0] == 0


def test_route_closed_after_plan_is_rejected_at_approval(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    request = reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])
    assert planner.plan(db, write(tmp_path, 'plan.json', request))['status'] == '可确认'
    # 计划后以新版本关闭步行路线
    travel.import_routes(db, write(tmp_path, 'routes-close.json', {'version': 'v2', 'rows': [
        {'id': 'RT-EW-WALK', 'status': '已关闭', 'from': EAST, 'to': WEST,
         'service': '步行', 'minutes': 8}]}))
    outcome = planner.approve(db, approve_file(tmp_path))[0]
    assert outcome['status'] == '拒绝' and outcome['reason'] == 'route_closed_since_plan'


def test_route_version_changed_after_plan_is_rejected_at_approval(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    request = reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])
    assert planner.plan(db, write(tmp_path, 'plan.json', request))['status'] == '可确认'
    # 计划后发布同路线新版本，耗时拉长到 30 分钟
    travel.import_routes(db, write(tmp_path, 'routes-v2.json', {'version': 'v2', 'rows': [
        {'id': 'RT-EW-WALK', 'from': EAST, 'to': WEST, 'service': '步行', 'minutes': 30}]}))
    outcome = planner.approve(db, approve_file(tmp_path))[0]
    assert outcome['status'] == '拒绝'
    assert outcome['reason'] in ('route_version_changed_since_plan', 'itinerary_infeasible_since_plan')
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    review = next(x for x in data['reschedule_review'] if x['plan_id'] == 'P1')
    assert review['approval_change'], '批准时的路线版本变化必须进入报告'


def test_repeated_imports_do_not_duplicate_occupancy_or_checks(tmp_path):
    db = seed_two_campus(tmp_path, service='步行')
    plan_file = write(tmp_path, 'plan.json', reschedule_request('P1', [disrupted('C1')], [WEST_ROOM]))
    first = planner.plan(db, plan_file)
    second = planner.plan(db, plan_file)
    assert first['status'] == second['status'] == '可确认'
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM reschedule_assignment WHERE id LIKE 'P1:%'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM itinerary_check").fetchone()[0] == \
        len(set(r['id'] if False else (r['chain'], r['subject'], r['from_lesson'], r['to_lesson'])
                for r in first['itinerary']))
    approval = approve_file(tmp_path)
    assert planner.approve(db, approval)[0]['status'] == '已应用'
    assert planner.approve(db, approval)[0]['status'] == '已应用'
    assert conn.execute("SELECT count(*) FROM room_slot WHERE id='P1:LD'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM substitution WHERE id='P1:LD'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM reschedule_approval WHERE id='P1'").fetchone()[0] == 1


def test_report_reads_back_plan_eliminations_and_approval_change(tmp_path):
    db = tmp_path / 'db'
    import_routes(db, tmp_path, two_campus_routes())
    import_services(db, tmp_path, [{'class': 'C1', 'service': '校车接送'}])
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00')
    planner.plan(db, write(tmp_path, 'plan.json', reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])))
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    review = next(x for x in data['reschedule_review'] if x['plan_id'] == 'P1')
    assert review['status'] == '待协调'
    assert any(e['reason'] == 'cannot_reach_next_lesson' for e in review['eliminations'])
    assert {'travel_route', 'class_service', 'itinerary_check'} <= set(data['entities'])


def test_missing_frozen_service_blocks_class_chain(tmp_path):
    db = tmp_path / 'db'
    import_routes(db, tmp_path, two_campus_routes())
    # 刻意不导入 C1 的到课服务合同
    import_lesson(db, tmp_path, 'L0', 'C1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00', room='RE')
    import_staff(db, tmp_path, 'L0', 'T1', EAST, '2026-01-01T09:00:00', '2026-01-01T10:00:00')
    result = planner.plan(db, write(tmp_path, 'plan.json',
                                    reschedule_request('P1', [disrupted('C1')], [WEST_ROOM])))
    assert result['status'] == '待协调'
    body = json.loads(connect(db).execute("SELECT body FROM reschedule_plan WHERE id='P1'").fetchone()['body'])
    assert any(r['reason'] == 'service_not_frozen' and r['chain'] == 'class' for r in body['itinerary'])


def test_route_import_is_idempotent_and_rejects_version_regression(tmp_path):
    db = tmp_path / 'db'
    rows = two_campus_routes()
    f1 = write(tmp_path, 'routes.json', {'version': 'v1', 'rows': rows})
    travel.import_routes(db, f1)
    travel.import_routes(db, f1)  # 同文件重放
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM travel_route").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM export_file WHERE kind='desktop_travel_route'").fetchone()[0] == 1
    # 同版本不同内容拒绝
    with pytest.raises(ValueError, match='identity_conflict'):
        travel.import_routes(db, write(tmp_path, 'routes-bad.json', {
            'version': 'v1', 'rows': [dict(rows[0], minutes=99)]}))
    # 版本号回退拒绝
    with pytest.raises(ValueError, match='route_version_regression'):
        travel.import_routes(db, write(tmp_path, 'routes-old.json', {
            'version': 'v0', 'rows': rows}))
    # 新版本续发后查询取最新版本
    travel.import_routes(db, write(tmp_path, 'routes-v2.json', {
        'version': 'v2', 'rows': [dict(rows[0], minutes=12)]}))
    route = travel.current_route(conn, EAST, WEST, '步行')
    assert route['version'] == 'v2' and route['minutes'] == 12


def test_service_import_idempotent_and_frozen(tmp_path):
    db = tmp_path / 'db'
    f1 = write(tmp_path, 'services.json', {'version': 'v1', 'rows': [{'class': 'C1', 'service': '校车接送'}]})
    travel.import_services(db, f1)
    travel.import_services(db, f1)
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM class_service").fetchone()[0] == 1
    with pytest.raises(ValueError, match='identity_conflict'):
        travel.import_services(db, write(tmp_path, 'services-changed.json', {
            'version': 'v2', 'rows': [{'class': 'C1', 'service': '步行'}]}))
