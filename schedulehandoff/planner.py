"""Joint room and substitute matching for disrupted class schedules.

搜索组合时除教室/教师时间重叠外，还按教师与班级两条行程链核实相邻课节
之间的版本化通行路线是否赶得上；批准时按最新已生效课节与路线版本重算，
计划后增课或关闭/换版路线一律拒绝旧计划。
"""
import json
from .ingest import capture
from .store import connect, save, decide
from .operations import overlap
from . import travel


def _still_available(db, plan_id, assignments):
    rooms = [json.loads(x['body']) for x in db.execute(
        "SELECT body FROM room_slot WHERE status='可用' AND id NOT LIKE ?", (f'{plan_id}:%',))]
    teachers = [json.loads(x['body']) for x in db.execute(
        "SELECT body FROM substitution WHERE status='已委托' AND id NOT LIKE ?", (f'{plan_id}:%',))]
    for item in assignments:
        if any(x['room'] == item['room'] and overlap(x['start'], x['end'], item['start'], item['end']) for x in rooms):
            return False
        if any(x['teacher'] == item['teacher'] and overlap(x['start'], x['end'], item['start'], item['end']) for x in teachers):
            return False
    return True


def _save_checks(db, plan_id, source, version, records, phase):
    """落行程判定；同一计划同一阶段同键覆盖，重放导入不产生重复占用。"""
    for rec in records:
        key = f"{plan_id}:{phase}:{rec['chain']}:{rec['subject']}:{rec['from_lesson']}->{rec['to_lesson']}"
        db.execute('INSERT INTO itinerary_check(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
                   'body=excluded.body,status=excluded.status,reason=excluded.reason',
                   (key, source, version, json.dumps(rec, sort_keys=True, ensure_ascii=False),
                    rec['status'], rec['reason']))


def _assignment_payload(lesson, room, teacher, capacity):
    return dict(lesson=lesson['id'], room=room['id'], teacher=teacher, capacity=capacity,
                start=lesson['start'], end=lesson['end'], subject=lesson['subject'],
                enrolled=lesson['enrolled'], klass=lesson.get('class'),
                room_location=room.get('location'),
                teaching_location=lesson.get('teaching_location') or room.get('location'),
                remote=bool(lesson.get('remote')))


def _blocking(records, lesson_keys):
    """只有触及本次计划课节的相邻判定才阻塞；已生效课节间的旧问题不重复阻塞。"""
    keys = set(lesson_keys)
    return [r for r in records
            if r['status'] != '可行' and (r['from_lesson'] in keys or r['to_lesson'] in keys)]


def plan(path, file):
    source, payload = capture(path, 'desktop_reschedule_request', file)
    lessons = payload.get('rows', [])
    rooms = payload.get('rooms', [])
    if not lessons or len({x['id'] for x in lessons}) != len(lessons):
        raise ValueError('invalid_lesson_scope')
    db = connect(path)
    try:
        plan_id = str(payload['plan_id'])
        existing = db.execute('SELECT * FROM reschedule_plan WHERE id=?', (plan_id,)).fetchone()
        if existing and existing['status'] == '已应用':
            if existing['source_id'] != source:
                raise ValueError('plan_identity_conflict')
            return {'plan_id': plan_id, 'status': '已应用', 'reason': 'approved',
                    'assignments': json.loads(existing['body'])['assignments']}
        occupied = [json.loads(x['body']) for x in db.execute("SELECT body FROM room_slot WHERE status='可用'")]
        roster = {x['id']: json.loads(x['body']) for x in db.execute('SELECT * FROM teacher')}
        assigned = [json.loads(x['body']) for x in db.execute("SELECT body FROM substitution WHERE status='已委托'")]
        services = travel.class_service_map(db)
        itinerary_mode = travel.active_mode(db)
        eliminations = {}

        def reject(lesson, room, teacher, reason, detail=None):
            key = (lesson['id'], room, teacher, reason)
            eliminations.setdefault(key, {'lesson': lesson['id'], 'room': room, 'teacher': teacher,
                                          'reason': reason, 'detail': detail or {}})

        options = []
        for lesson in lessons:
            choices = []
            for room in rooms:
                if room['id'] not in lesson['candidate_rooms']:
                    reject(lesson, room['id'], None, 'room_not_candidate')
                    continue
                if int(room['capacity']) < int(lesson['enrolled']):
                    reject(lesson, room['id'], None, 'capacity_exceeded')
                    continue
                if any(x['room'] == room['id'] and overlap(x['start'], x['end'], lesson['start'], lesson['end']) for x in occupied):
                    reject(lesson, room['id'], None, 'room_time_overlap')
                    continue
                for teacher_id in lesson['candidate_teachers']:
                    teacher = roster.get(teacher_id)
                    if not teacher:
                        reject(lesson, room['id'], teacher_id, 'teacher_unknown')
                        continue
                    if lesson['subject'] not in teacher.get('qualified', []):
                        reject(lesson, room['id'], teacher_id, 'qualification_missing')
                        continue
                    if any(overlap(a, b, lesson['start'], lesson['end']) for a, b in teacher.get('busy', [])):
                        reject(lesson, room['id'], teacher_id, 'teacher_busy')
                        continue
                    if any(x['teacher'] == teacher_id and overlap(x['start'], x['end'], lesson['start'], lesson['end']) for x in assigned):
                        reject(lesson, room['id'], teacher_id, 'teacher_time_overlap')
                        continue
                    rank = lesson['candidate_rooms'].index(room['id']) + lesson['candidate_teachers'].index(teacher_id)
                    score = rank * 100 + int(room['capacity']) - int(lesson['enrolled'])
                    choices.append((score, room['id'], teacher_id, int(room['capacity'])))
            options.append(sorted(choices))

        best = None
        # 每个课节的首选组合（按搜索排序）；行程无解时用它生成完整相邻课节判定证据
        preferred = []
        for index in range(len(lessons)):
            if options[index]:
                score, room_id, teacher_id, capacity = options[index][0]
                preferred.append(_assignment_payload(
                    lessons[index], {r['id']: r for r in rooms}[room_id], teacher_id, capacity))

        def search(index, chosen, cost):
            nonlocal best
            if index == len(lessons):
                candidate = (len(chosen), -cost, list(chosen))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
                return
            search(index + 1, chosen, cost)
            lesson = lessons[index]
            room_by_id = {r['id']: r for r in rooms}
            for score, room_id, teacher_id, capacity in options[index]:
                if any((p['room'] == room_id or p['teacher'] == teacher_id) and
                       overlap(p['start'], p['end'], lesson['start'], lesson['end']) for p in chosen):
                    reject(lesson, room_id, teacher_id, 'plan_internal_overlap')
                    continue
                candidate = _assignment_payload(lesson, room_by_id[room_id], teacher_id, capacity)
                if itinerary_mode:
                    _, records = travel.evaluate_assignments(db, chosen + [candidate], 'plan', services)
                    blockers = _blocking(records, [a['lesson'] for a in chosen] + [lesson['id']])
                    if blockers:
                        bad = blockers[0]
                        reject(lesson, room_id, teacher_id, bad['reason'],
                               {'chain': bad['chain'], 'subject': bad['subject'],
                                'from_lesson': bad['from_lesson'], 'to_lesson': bad['to_lesson'],
                                'gap_minutes': bad['gap_minutes'], 'required_minutes': bad['required_minutes'],
                                'route_id': bad['route_id'], 'route_version': bad['route_version']})
                        continue
                chosen.append(candidate)
                search(index + 1, chosen, cost + score)
                chosen.pop()

        search(0, [], 0)
        chosen = best[2] if best else []
        evidence_combo = chosen if len(chosen) == len(lessons) else preferred
        _, itinerary_records = travel.evaluate_assignments(db, evidence_combo, 'plan', services)
        if itinerary_mode and _blocking(itinerary_records, [x['id'] for x in lessons]):
            chosen = []
        status, reason = ('可确认', 'all_lessons_assigned') if len(chosen) == len(lessons) else ('待协调', 'unassigned_lessons')
        with db:
            save(db, 'reschedule_plan', plan_id, source, payload['version'],
                 {'lessons': lessons, 'rooms': rooms, 'assignments': chosen,
                  'eliminations': list(eliminations.values()),
                  'itinerary': itinerary_records if itinerary_mode else []},
                 status, reason)
            for assignment in chosen:
                save(db, 'reschedule_assignment', f"{plan_id}:{assignment['lesson']}", source, payload['version'],
                     assignment, '已规划', 'joint_match')
            if itinerary_mode:
                _save_checks(db, plan_id, source, payload['version'], itinerary_records, 'plan')
            decide(db, '联合调课', plan_id, status, reason, source)
        return {'plan_id': plan_id, 'status': status, 'reason': reason,
                'assignments': chosen,
                'eliminations': list(eliminations.values()),
                'itinerary': itinerary_records if itinerary_mode else []}
    finally:
        db.close()


def _itinerary_recheck_reason(current, planned):
    """对比计划时与批准时的相邻课节判定，给出可追溯的拒绝原因。"""
    prior = {(r['chain'], r['subject'], r['from_lesson'], r['to_lesson']): r for r in planned}
    for rec in current:
        if rec['status'] == '可行':
            continue
        key = (rec['chain'], rec['subject'], rec['from_lesson'], rec['to_lesson'])
        old = prior.get(key)
        if old is None:
            return 'lesson_added_since_plan'
        if old.get('route_id') and not rec.get('route_id'):
            return 'route_closed_since_plan'
        if rec['reason'] == 'route_missing':
            return 'route_closed_since_plan'
        if old.get('route_id') and rec.get('route_id') and old['route_id'] == rec['route_id'] \
                and old.get('route_version') != rec.get('route_version'):
            return 'route_version_changed_since_plan'
        return 'itinerary_infeasible_since_plan'
    return 'itinerary_infeasible_since_plan'


def approve(path, file):
    source, payload = capture(path, 'desktop_reschedule_approval', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                plan_id = str(row['plan_id'])
                plan_row = db.execute('SELECT * FROM reschedule_plan WHERE id=?', (plan_id,)).fetchone()
                old = db.execute('SELECT * FROM reschedule_approval WHERE id=?', (plan_id,)).fetchone()
                if old:
                    if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        raise ValueError('approval_identity_conflict')
                    results.append(decide(db, '联合调课', plan_id, old['status'], old['reason'], source))
                    continue
                if not plan_row or plan_row['status'] != '可确认':
                    status, reason = '拒绝', 'plan_incomplete'
                elif not row.get('approver') or row.get('version') != plan_row['version']:
                    status, reason = '拒绝', 'approval_invalid'
                elif not _still_available(db, plan_id, json.loads(plan_row['body'])['assignments']):
                    status, reason = '拒绝', 'resource_changed_since_plan'
                else:
                    plan_body = json.loads(plan_row['body'])
                    assignments = plan_body['assignments']
                    itinerary_mode = travel.active_mode(db)
                    if itinerary_mode:
                        _, evaluated = travel.evaluate_assignments(db, assignments, 'approval')
                        plan_lesson_keys = [str(x['id']) for x in plan_body.get('lessons', [])]
                        current_records = _blocking(evaluated, plan_lesson_keys)
                        _save_checks(db, plan_id, source, payload['version'], current_records, 'approval')
                        if current_records:
                            status = '拒绝'
                            reason = _itinerary_recheck_reason(current_records, plan_body.get('itinerary', []))
                        else:
                            status, reason = '已应用', 'joint_plan_applied'
                    else:
                        status, reason = '已应用', 'joint_plan_applied'
                    if status == '已应用':
                        for assignment in assignments:
                            key = f"{plan_id}:{assignment['lesson']}"
                            save(db, 'room_slot', key, source, payload['version'], assignment, '可用', 'planned_room')
                            save(db, 'substitution', key, source, payload['version'], assignment, '已委托', 'planned_substitute')
                        db.execute("UPDATE reschedule_plan SET status='已应用',reason='approved' WHERE id=?", (plan_id,))
                save(db, 'reschedule_approval', plan_id, source, payload['version'], row, status, reason)
                results.append(decide(db, '联合调课', plan_id, status, reason, source))
        return results
    finally:
        db.close()
