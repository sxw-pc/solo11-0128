"""Joint room and substitute matching for disrupted class schedules.

Beyond room/teacher time overlaps, every candidate combination must keep both
the teacher's and the class's travel chains feasible: adjacent lessons have to
be reachable over the service-specific approved route within the available
gap. A class chain without a solution turns the plan from 可确认 into 待协调,
so approve cannot create room_slot/substitution rows and notice-evaluate can
never mark the lesson 换班生效.
"""
import json
from .ingest import capture
from .store import connect, save, decide
from .operations import overlap
from . import travel


def _room_sites(db, payload):
    sites = {r['id']: r['site'] for r in payload.get('rooms', []) if r.get('site')}
    for record in db.execute('SELECT * FROM site_directory'):
        body = json.loads(record['body'])
        if body.get('kind') == 'room' and body.get('ref') and body.get('site'):
            sites.setdefault(body['ref'], body['site'])
    return sites


def _candidate_stops(lesson, room_id, teacher_id, room_sites):
    room_site = room_sites.get(room_id)
    teaching_site = lesson.get('teaching_site') or room_site
    common = {'lesson': lesson['id'], 'start': lesson['start'], 'end': lesson['end'],
              'room': room_id, 'class': lesson['class'], 'room_site': room_site,
              'teaching_site': teaching_site, 'remote': bool(lesson.get('remote'))}
    teacher_stop = {'kind': 'teacher', 'traveller': teacher_id, 'lesson': lesson['id'],
                    'start': lesson['start'], 'end': lesson['end'], 'site': teaching_site,
                    'room': room_id, 'class': lesson['class'],
                    'remote': bool(lesson.get('remote')), 'source': 'plan'}
    class_stop = {'kind': 'class', 'traveller': lesson['class'], 'lesson': lesson['id'],
                  'start': lesson['start'], 'end': lesson['end'], 'site': room_site,
                  'room': room_id, 'class': lesson['class'],
                  'remote': bool(lesson.get('remote')), 'source': 'plan'}
    return teacher_stop, class_stop, common


def _still_available(db, plan_id, assignments):
    rooms = [json.loads(x['body']) for x in db.execute("SELECT body FROM room_slot WHERE status='可用' AND id NOT LIKE ?", (f'{plan_id}:%',))]
    teachers = [json.loads(x['body']) for x in db.execute("SELECT body FROM substitution WHERE status='已委托' AND id NOT LIKE ?", (f'{plan_id}:%',))]
    for item in assignments:
        if any(x['room'] == item['room'] and overlap(x['start'], x['end'], item['start'], item['end']) for x in rooms):
            return False
        if any(x['teacher'] == item['teacher'] and overlap(x['start'], x['end'], item['start'], item['end']) for x in teachers):
            return False
    return True


def plan(path, file):
    source, payload = capture(path, 'desktop_reschedule_request', file)
    lessons = payload.get('rows', [])
    rooms = payload.get('rooms', [])
    if not lessons or len({x['id'] for x in lessons}) != len(lessons):
        raise ValueError('invalid_lesson_scope')
    if any(not x.get('class') for x in lessons):
        raise ValueError('class_required')
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
        room_sites = _room_sites(db, payload)
        base_teacher = {t: travel.base_stops(db, 'teacher', t, plan_id)
                        for t in {t for x in lessons for t in x['candidate_teachers']}}
        base_class = {x['class']: travel.base_stops(db, 'class', x['class'], plan_id) for x in lessons}
        eliminations = {}

        def reject(lesson, room, teacher, leg):
            eliminations.setdefault(lesson['id'], []).append(
                {'room': room, 'teacher': teacher, 'reason': leg['reason'],
                 'origin': leg.get('origin'), 'destination': leg.get('destination'),
                 'gap_minutes': leg.get('gap_minutes'), 'required_minutes': leg.get('required_minutes'),
                 'route_id': leg.get('route_id'), 'route_version': leg.get('route_version'),
                 'service': leg.get('service')})

        options = []
        for lesson in lessons:
            choices = []
            for room in rooms:
                if room['id'] not in lesson['candidate_rooms'] or int(room['capacity']) < int(lesson['enrolled']):
                    continue
                if any(x['room'] == room['id'] and overlap(x['start'], x['end'], lesson['start'], lesson['end']) for x in occupied):
                    continue
                for teacher_id in lesson['candidate_teachers']:
                    teacher = roster.get(teacher_id)
                    if not teacher or lesson['subject'] not in teacher.get('qualified', []):
                        continue
                    if any(overlap(a, b, lesson['start'], lesson['end']) for a, b in teacher.get('busy', [])):
                        continue
                    if any(x['teacher'] == teacher_id and overlap(x['start'], x['end'], lesson['start'], lesson['end']) for x in assigned):
                        continue
                    teacher_stop, class_stop, _ = _candidate_stops(lesson, room['id'], teacher_id, room_sites)
                    t_ok, t_bad = travel.insertion_feasible(db, 'teacher', teacher_id, base_teacher[teacher_id], teacher_stop)
                    if not t_ok:
                        reject(lesson, room['id'], teacher_id, t_bad[0])
                        continue
                    c_ok, c_bad = travel.insertion_feasible(db, 'class', lesson['class'], base_class[lesson['class']], class_stop)
                    if not c_ok:
                        reject(lesson, room['id'], teacher_id, c_bad[0])
                        continue
                    rank = lesson['candidate_rooms'].index(room['id']) + lesson['candidate_teachers'].index(teacher_id)
                    score = rank * 100 + int(room['capacity']) - int(lesson['enrolled'])
                    choices.append((score, room['id'], teacher_id, int(room['capacity'])))
            options.append(sorted(choices))
        best = None

        def search(index, chosen, cost):
            nonlocal best
            if index == len(lessons):
                candidate = (len(chosen), -cost, list(chosen))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
                return
            search(index + 1, chosen, cost)
            lesson = lessons[index]
            for score, room, teacher, capacity in options[index]:
                if any((p['room'] == room or p['teacher'] == teacher) and
                       overlap(p['start'], p['end'], lesson['start'], lesson['end']) for p in chosen):
                    continue
                teacher_stop, class_stop, common = _candidate_stops(lesson, room, teacher, room_sites)
                t_chain = base_teacher[teacher] + [p['_tstop'] for p in chosen if p['teacher'] == teacher]
                t_ok, t_bad = travel.insertion_feasible(db, 'teacher', teacher, t_chain, teacher_stop)
                if not t_ok:
                    reject(lesson, room, teacher, t_bad[0])
                    continue
                c_chain = base_class[lesson['class']] + [p['_cstop'] for p in chosen if p['class'] == lesson['class']]
                c_ok, c_bad = travel.insertion_feasible(db, 'class', lesson['class'], c_chain, class_stop)
                if not c_ok:
                    reject(lesson, room, teacher, c_bad[0])
                    continue
                entry = dict(common)
                entry.update(room=room, teacher=teacher, capacity=capacity,
                             subject=lesson['subject'], enrolled=lesson['enrolled'])
                entry['_tstop'] = teacher_stop
                entry['_cstop'] = class_stop
                chosen.append(entry)
                search(index + 1, chosen, cost + score)
                chosen.pop()
        search(0, [], 0)
        raw_chosen = best[2] if best else []
        chosen = []
        for entry in raw_chosen:
            entry = dict(entry)
            entry.pop('_tstop', None)
            entry.pop('_cstop', None)
            chosen.append(entry)
        chains = travel.evaluate_plan_travel(db, chosen, plan_id) if chosen else []
        travel_blocked = any(
            next((e['reason'] for e in eliminations.get(lesson['id'], [])
                  if e['reason'] in ('route_missing', 'travel_gap_insufficient', 'service_missing', 'site_missing')), None)
            for lesson in lessons)
        if len(chosen) == len(lessons) and all(c['feasible'] for c in chains):
            status, reason = '可确认', 'all_lessons_assigned'
        elif travel_blocked:
            status, reason = '待协调', 'travel_unresolved'
        else:
            status, reason = '待协调', 'unassigned_lessons'
        deduped = {}
        for key, items in eliminations.items():
            seen, uniq = set(), []
            for item in items:
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig not in seen:
                    seen.add(sig)
                    uniq.append(item)
            deduped[key] = uniq
        with db:
            save(db, 'reschedule_plan', plan_id, source, payload['version'],
                 {'lessons': lessons, 'rooms': rooms, 'assignments': chosen,
                  'travel_chains': chains, 'eliminations': deduped}, status, reason)
            for assignment in chosen:
                save(db, 'reschedule_assignment', f"{plan_id}:{assignment['lesson']}", source, payload['version'],
                     assignment, '已规划', 'joint_match')
            for chain in chains:
                cid = f"{plan_id}:plan:{chain['kind']}:{chain['traveller']}"
                save(db, 'travel_chain', cid, source, payload['version'], chain,
                     '行程可行' if chain['feasible'] else '待协调',
                     'all_legs_feasible' if chain['feasible'] else 'leg_infeasible')
            decide(db, '联合调课', plan_id, status, reason, source)
        return {'plan_id': plan_id, 'status': status, 'reason': reason,
                'assignments': chosen, 'travel_chains': chains, 'eliminations': deduped}
    finally:
        db.close()


def _approval_travel_rejection(plan_chains, latest_chains):
    """Map an approval-time infeasible leg to a rejection reason with evidence."""
    plan_index = {(c['kind'], c['traveller']): {
        (leg['prev_lesson'], leg['next_lesson']): leg for leg in c['legs']}
        for c in plan_chains}
    plan_stops = {(c['kind'], c['traveller']): {s['lesson']: s for s in c['stops']}
                  for c in plan_chains}
    for chain in latest_chains:
        if chain['feasible']:
            continue
        old_legs = plan_index.get((chain['kind'], chain['traveller']), {})
        old_stops = plan_stops.get((chain['kind'], chain['traveller']), {})
        for leg in chain['legs']:
            if leg['feasible']:
                continue
            key = (leg['prev_lesson'], leg['next_lesson'])
            old = old_legs.get(key)
            if leg['reason'] == 'service_missing':
                return 'travel_service_missing', leg
            if leg['reason'] in ('route_missing',):
                if old and old.get('route_id'):
                    return 'travel_route_closed', leg
                return 'travel_route_missing', leg
            if leg['reason'] == 'site_missing':
                return 'travel_site_missing', leg
            if leg['reason'] == 'travel_gap_insufficient':
                if old and old.get('route_version') and old.get('route_version') != leg.get('route_version'):
                    return 'travel_route_version_changed', leg
                if any(s['lesson'] not in old_stops for s in chain['stops']):
                    return 'travel_late_lesson', leg
                return 'travel_gap_insufficient', leg
            return 'travel_unresolved', leg
    return 'travel_unresolved', None


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
                latest_chains = []
                if not plan_row or plan_row['status'] != '可确认':
                    status, reason, evidence = '拒绝', 'plan_incomplete', None
                elif not row.get('approver') or row.get('version') != plan_row['version']:
                    status, reason, evidence = '拒绝', 'approval_invalid', None
                elif not _still_available(db, plan_id, json.loads(plan_row['body'])['assignments']):
                    status, reason, evidence = '拒绝', 'resource_changed_since_plan', None
                else:
                    plan_body = json.loads(plan_row['body'])
                    assignments = plan_body['assignments']
                    latest_chains = travel.evaluate_plan_travel(db, assignments, plan_id)
                    if all(c['feasible'] for c in latest_chains):
                        status, reason, evidence = '已应用', 'joint_plan_applied', None
                    else:
                        reason, evidence = _approval_travel_rejection(
                            plan_body.get('travel_chains', []), latest_chains)
                        status = '拒绝'
                save(db, 'reschedule_approval', plan_id, source, payload['version'], row, status, reason)
                if plan_row and status in ('拒绝', '已应用'):
                    plan_body = json.loads(plan_row['body'])
                    if status == '已应用':
                        approval_chains = travel.evaluate_plan_travel(db, plan_body['assignments'], plan_id)
                    else:
                        approval_chains = latest_chains or travel.evaluate_plan_travel(
                            db, plan_body.get('assignments', []), plan_id)
                    for chain in approval_chains:
                        cid = f"{plan_id}:approval:{chain['kind']}:{chain['traveller']}"
                        save(db, 'travel_chain', cid, source, payload['version'],
                             chain, '行程可行' if chain['feasible'] else '待协调', reason)
                if status == '已应用':
                    plan_body = json.loads(plan_row['body'])
                    for assignment in plan_body['assignments']:
                        key = f"{plan_id}:{assignment['lesson']}"
                        save(db, 'room_slot', key, source, payload['version'], assignment, '可用', 'planned_room')
                        save(db, 'substitution', key, source, payload['version'], assignment, '已委托', 'planned_substitute')
                    db.execute("UPDATE reschedule_plan SET status='已应用',reason='approved' WHERE id=?", (plan_id,))
                results.append(decide(db, '联合调课', plan_id, status, reason, source))
        return results
    finally:
        db.close()
