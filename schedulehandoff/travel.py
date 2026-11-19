"""Cross-campus travel chains for teachers and classes.

A desktop export carries locations (lesson class, actual teaching point, room
site), versioned approved travel routes, and the class attendance service that
was already fixed when the order was taken. Both teachers and classes have an
ordered itinerary made of every effective lesson plus the lessons being
arranged. Two non-overlapping lessons are only mutually feasible when the
traveller can move between their sites, over the service-specific approved
route, within the available gap.
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide
from .operations import overlap


def _parse(value):
    return datetime.fromisoformat(value)


def import_sites(path, file):
    """Desktop site directory: every lesson/class/room is bound to a site."""
    source, payload = capture(path, 'desktop_site_directory', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['ref'])
                save(db, 'site_directory', key, source, payload['version'], row, '已采录', 'site_captured')
                results.append(decide(db, '地点目录', key, '已采录', 'site_captured', source))
        return results
    finally:
        db.close()


def import_routes(path, file):
    """Versioned approved routes. Older versions stay on file for tracing."""
    source, payload = capture(path, 'desktop_route_directory', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = f"{row['id']}:{row['version']}"
                reason = 'route_open' if row.get('status', '已核定') == '已核定' else 'route_closed'
                save(db, 'travel_route', key, source, payload['version'], row,
                     '已核定' if reason == 'route_open' else '已关闭', reason)
                results.append(decide(db, '通行路线', key,
                                      '已核定' if reason == 'route_open' else '已关闭', reason, source))
        return results
    finally:
        db.close()


def import_services(path, file):
    """Class attendance service fixed at order intake; planning may not change it."""
    source, payload = capture(path, 'desktop_class_service', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['class'])
                save(db, 'class_service', key, source, payload['version'], row, '已约定', 'service_locked_at_intake')
                results.append(decide(db, '班级服务', key, '已约定', 'service_locked_at_intake', source))
        return results
    finally:
        db.close()


def current_routes(db):
    """Latest on-file version of each route id, only open/approved routes count."""
    latest = {}
    for record in db.execute('SELECT * FROM travel_route'):
        body = json.loads(record['body'])
        rid = body['id']
        if rid not in latest or body['version'] > latest[rid]['version']:
            latest[rid] = body
    return latest


def resolve_route(db, origin, destination, service):
    """Find the approved open route matching sites and service, with its version."""
    for body in current_routes(db).values():
        if body.get('status', '已核定') != '已核定':
            continue
        if body['origin'] == origin and body['destination'] == destination and body['service'] == service:
            return {'ok': True, 'route_id': body['id'], 'route_version': body['version'],
                    'minutes': int(body['minutes']), 'service': service}
    return {'ok': False, 'reason': 'route_missing'}


def _gap_minutes(start, end):
    return int((_parse(start) - _parse(end)).total_seconds() // 60)


def base_stops(db, kind, traveller, plan_id=None):
    """Effective on-file stops for one teacher or one class, time ordered.

    kind='teacher' follows the actual teaching point (remote teachers move from
    their real location); kind='class' follows the room site. Plan-owned rows of
    the given plan_id are excluded because they are re-evaluated as candidates.
    """
    stops = []
    if kind == 'teacher':
        rows = db.execute("SELECT * FROM substitution WHERE status='已委托'")
        for record in rows:
            body = json.loads(record['body'])
            if body.get('teacher') != traveller:
                continue
            if plan_id and record['id'].startswith(f'{plan_id}:'):
                continue
            site = body.get('teaching_site') or body.get('room_site') or body.get('room')
            stops.append({'kind': 'teacher', 'traveller': traveller, 'lesson': body['lesson'],
                          'start': body['start'], 'end': body['end'], 'site': site,
                          'room': body.get('room'), 'class': body.get('class'),
                          'remote': bool(body.get('remote')), 'source': record['id']})
    else:
        rows = db.execute("SELECT * FROM room_slot WHERE status='可用'")
        for record in rows:
            body = json.loads(record['body'])
            if body.get('class') != traveller:
                continue
            if plan_id and record['id'].startswith(f'{plan_id}:'):
                continue
            stops.append({'kind': 'class', 'traveller': traveller, 'lesson': record['id'],
                          'start': body['start'], 'end': body['end'],
                          'site': body.get('room_site') or body.get('room'),
                          'room': body.get('room'), 'class': body.get('class'),
                          'remote': bool(body.get('remote')), 'source': record['id']})
    stops.sort(key=lambda s: (s['start'], s['end'], s['lesson']))
    return stops


def _class_service(db, traveller):
    row = db.execute('SELECT body FROM class_service WHERE id=?', (traveller,)).fetchone()
    return json.loads(row['body']) if row else None


def evaluate_leg(db, prev_stop, next_stop):
    """Adjudicate one adjacent pair of stops on a chain."""
    gap = _gap_minutes(next_stop['start'], prev_stop['end'])
    base = {'prev_lesson': prev_stop['lesson'], 'next_lesson': next_stop['lesson'],
            'origin': prev_stop['site'], 'destination': next_stop['site'],
            'gap_minutes': gap}
    if not prev_stop.get('site') or not next_stop.get('site'):
        return dict(base, feasible=False, reason='site_missing')
    if prev_stop['site'] == next_stop['site']:
        return dict(base, feasible=True, reason='same_site', required_minutes=0,
                    route_id=None, route_version=None, service=None)
    if next_stop['kind'] == 'class':
        agreement = _class_service(db, next_stop['traveller'])
        if not agreement:
            return dict(base, feasible=False, reason='service_missing')
        service = agreement['service']
    else:
        # Teachers move under staff travel arrangements, not the class contract.
        service = 'staff'
    found = resolve_route(db, prev_stop['site'], next_stop['site'], service)
    if not found['ok']:
        return dict(base, feasible=False, reason=found['reason'], service=service)
    leg = dict(base, service=service, route_id=found['route_id'],
               route_version=found['route_version'], required_minutes=found['minutes'])
    if gap < found['minutes']:
        leg.update(feasible=False, reason='travel_gap_insufficient')
    else:
        leg.update(feasible=True, reason='route_ok')
    return leg


def evaluate_chain(db, kind, traveller, stops, plan_id=None):
    """Full ordered itinerary; every adjacent leg must be feasible."""
    ordered = sorted(stops, key=lambda s: (s['start'], s['end'], s['lesson']))
    legs = []
    feasible = True
    for prev, nxt in zip(ordered, ordered[1:]):
        leg = evaluate_leg(db, prev, nxt)
        legs.append(leg)
        if not leg['feasible']:
            feasible = False
    return {'kind': kind, 'traveller': traveller, 'plan_id': plan_id,
            'feasible': feasible, 'stops': ordered, 'legs': legs}


def insertion_feasible(db, kind, traveller, base_chain, stop):
    """Check one candidate stop against the chain it would join."""
    chain = evaluate_chain(db, kind, traveller, base_chain + [stop])
    bad = [leg for leg in chain['legs'] if not leg['feasible']]
    return (not bad), bad


def plan_stops(assignments):
    """Turn plan assignments into candidate stops for both travel dimensions."""
    teacher_stops, class_stops = {}, {}
    for item in assignments:
        tstop = {'kind': 'teacher', 'traveller': item['teacher'], 'lesson': item['lesson'],
                 'start': item['start'], 'end': item['end'],
                 'site': item.get('teaching_site') or item.get('room_site') or item['room'],
                 'room': item['room'], 'class': item.get('class'),
                 'remote': bool(item.get('remote')), 'source': 'plan'}
        cstop = {'kind': 'class', 'traveller': item['class'], 'lesson': item['lesson'],
                 'start': item['start'], 'end': item['end'],
                 'site': item.get('room_site') or item['room'],
                 'room': item['room'], 'class': item.get('class'),
                 'remote': bool(item.get('remote')), 'source': 'plan'}
        teacher_stops.setdefault(item['teacher'], []).append(tstop)
        class_stops.setdefault(item['class'], []).append(cstop)
    return teacher_stops, class_stops


def evaluate_plan_travel(db, assignments, plan_id=None):
    """Recompute every teacher and class chain implied by a plan."""
    teacher_stops, class_stops = plan_stops(assignments)
    chains = []
    for teacher, extra in teacher_stops.items():
        chains.append(evaluate_chain(db, 'teacher', teacher,
                                     base_stops(db, 'teacher', teacher, plan_id) + extra, plan_id))
    for klass, extra in class_stops.items():
        chains.append(evaluate_chain(db, 'class', klass,
                                     base_stops(db, 'class', klass, plan_id) + extra, plan_id))
    return chains
