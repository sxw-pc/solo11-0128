"""Versioned travel routes, frozen class attendance services and dual itinerary chains.

教师行程按“教师实际授课点”行走，班级行程按“教室地点”行走；相邻课节之间
按照已核定路线的服务与所需分钟数判断能否赶上。路线带版本，批准时按最新
已生效版本重算；班级服务在接单时冻结，不允许临时改服务放行。
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide

WALK = '步行'
ROUTE_ACTIVE = '已核定'
ROUTE_CLOSED = '已关闭'


def _parse(ts):
    return datetime.fromisoformat(ts)


def _save_route(db, rid, source, version, row, status, reason):
    """路线按 id 续版本：同版本必须逐字一致，新版本覆盖并保留全部导出字节。"""
    encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
    db.execute('INSERT INTO travel_route(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
               'body=excluded.body,status=excluded.status,reason=excluded.reason',
               (rid, source, version, encoded, status, reason))


def import_routes(path, file):
    """导入带版本的通行路线；同 id 新版本可续发，status='已关闭' 关闭旧路。"""
    source, payload = capture(path, 'desktop_travel_route', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                rid = str(row['id'])
                old = db.execute('SELECT * FROM travel_route WHERE id=?', (rid,)).fetchone()
                if old:
                    if old['version'] == payload['version']:
                        if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                            raise ValueError('identity_conflict')
                        results.append(decide(db, '通行路线', rid, old['status'], old['reason'], source))
                        continue
                    if _version_key(old['version']) >= _version_key(payload['version']):
                        raise ValueError('route_version_regression')
                if row.get('status', ROUTE_ACTIVE) == ROUTE_CLOSED:
                    status, reason = ROUTE_CLOSED, 'route_closed'
                else:
                    status, reason = ROUTE_ACTIVE, 'route_approved'
                _save_route(db, rid, source, payload['version'], row, status, reason)
                results.append(decide(db, '通行路线', rid, status, reason, source))
        return results
    finally:
        db.close()


def import_services(path, file):
    """导入接单时已确定的班级到课服务；合同冻结，重复导入必须逐字一致。"""
    source, payload = capture(path, 'desktop_class_service', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                cid = str(row['class'])
                old = db.execute('SELECT * FROM class_service WHERE id=?', (cid,)).fetchone()
                if old:
                    if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                                   (source, 'identity_conflict', f'class_service:{cid}'))
                        raise ValueError('identity_conflict')
                    results.append(decide(db, '到课服务', cid, old['status'], old['reason'], source))
                    continue
                save(db, 'class_service', cid, source, payload['version'], row, '已约定', 'service_frozen_at_order')
                results.append(decide(db, '到课服务', cid, '已约定', 'service_frozen_at_order', source))
        return results
    finally:
        db.close()


def _version_key(version):
    # 版本按字符串逐段比较；无法拆段时退化为原字符串，保证全序可排序。
    parts = []
    for part in str(version).split('.'):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return parts


def active_mode(db):
    """存在任一已核定路线即进入双校区行程约束模式；否则保留旧排课行为。"""
    return db.execute("SELECT 1 FROM travel_route WHERE status=?", (ROUTE_ACTIVE,)).fetchone() is not None


def current_route(db, origin, destination, service):
    """取起终点+服务下最新版本的已核定路线；同点移动不需要路线。"""
    if origin == destination:
        return {'id': None, 'version': None, 'minutes': 0, 'service': service, 'same_site': True}
    rows = [dict(x) for x in db.execute(
        "SELECT * FROM travel_route WHERE status=?", (ROUTE_ACTIVE,))]
    best = None
    for row in rows:
        body = json.loads(row['body'])
        ends = {body.get('from'), body.get('to')}
        if ends == {origin, destination} and body.get('service') == service:
            if best is None or _version_key(row['version']) > _version_key(best['version']):
                best = {'id': row['id'], 'version': row['version'],
                        'minutes': int(body['minutes']), 'service': service, 'same_site': False,
                        'body': body}
    return best


def class_service_map(db):
    return {row['id']: json.loads(row['body'])
            for row in db.execute('SELECT * FROM class_service')}


def _teacher_chain_entries(db):
    entries = []
    for row in db.execute("SELECT * FROM substitution WHERE status='已委托'"):
        body = json.loads(row['body'])
        location = body.get('teaching_location')
        if location is None:
            slot = db.execute('SELECT body FROM room_slot WHERE id=?', (str(body['lesson']),)).fetchone()
            location = json.loads(slot['body']).get('location') if slot else None
        entries.append({'key': str(body['lesson']), 'teacher': body['teacher'],
                        'start': body['start'], 'end': body['end'],
                        'location': location, 'remote': bool(body.get('remote')),
                        'source': 'substitution'})
    return entries


def _class_chain_entries(db):
    entries = []
    for row in db.execute("SELECT * FROM room_slot WHERE status='可用'"):
        body = json.loads(row['body'])
        if not body.get('class'):
            continue
        entries.append({'key': str(body['id']), 'class': body['class'],
                        'start': body['start'], 'end': body['end'],
                        'location': body.get('location'),
                        'remote': bool(body.get('remote')), 'source': 'room_slot'})
    return entries


def _planned_entries(assignments):
    teachers, classes = [], []
    for a in assignments:
        teachers.append({'key': str(a['lesson']), 'teacher': a['teacher'],
                         'start': a['start'], 'end': a['end'],
                         'location': a.get('teaching_location') or a.get('room_location'),
                         'remote': bool(a.get('remote')), 'source': 'plan'})
        if a.get('klass'):
            classes.append({'key': str(a['lesson']), 'class': a['klass'],
                            'start': a['start'], 'end': a['end'],
                            'location': a.get('room_location'),
                            'remote': bool(a.get('remote')), 'source': 'plan'})
    return teachers, classes


def teacher_chain(db, assignments):
    chain = {}
    for e in _teacher_chain_entries(db):
        chain.setdefault(e['teacher'], []).append(e)
    planned, _ = _planned_entries(assignments)
    for e in planned:
        chain.setdefault(e['teacher'], []).append(e)
    for items in chain.values():
        items.sort(key=lambda x: _parse(x['start']))
    return chain


def class_chain(db, assignments):
    chain = {}
    for e in _class_chain_entries(db):
        chain.setdefault(e['class'], []).append(e)
    _, planned = _planned_entries(assignments)
    for e in planned:
        if e['class']:
            chain.setdefault(e['class'], []).append(e)
    for items in chain.values():
        items.sort(key=lambda x: _parse(x['start']))
    return chain


def _pair_record(kind, subject, first, second, service, route, gap, required, status, reason, phase):
    return {'chain': kind, 'subject': subject, 'phase': phase,
            'from_lesson': first['key'], 'to_lesson': second['key'],
            'from_location': first.get('location'), 'to_location': second.get('location'),
            'service': service, 'route_id': route.get('id') if route else None,
            'route_version': route.get('version') if route else None,
            'gap_minutes': gap, 'required_minutes': required,
            'status': status, 'reason': reason,
            'first_end': first['end'], 'second_start': second['start']}


def evaluate_chain(db, kind, subject, entries, service_lookup, phase):
    """评估一条行程链上所有相邻课节。返回 (可行?, 判定记录列表)。

    service_lookup(entry)->service 名称；缺服务在行程模式下直接判待协调，
    不允许临时改服务。每条记录带来源路线 id/版本与分钟数，可追溯。
    """
    records = []
    feasible = True
    for first, second in zip(entries, entries[1:]):
        gap = int((_parse(second['start']) - _parse(first['end'])).total_seconds() // 60)
        service = service_lookup(second)
        if first.get('location') is None or second.get('location') is None:
            rec = _pair_record(kind, subject, first, second, service, None, gap, None,
                               '待协调', 'itinerary_location_missing', phase)
        elif service is None:
            rec = _pair_record(kind, subject, first, second, None, None, gap, None,
                               '待协调', 'service_not_frozen', phase)
        else:
            route = current_route(db, first['location'], second['location'], service)
            if route is None:
                rec = _pair_record(kind, subject, first, second, service, None, gap, None,
                                   '待协调', 'route_missing', phase)
            elif gap < route['minutes']:
                rec = _pair_record(kind, subject, first, second, service, route, gap,
                                   route['minutes'], '待协调', 'cannot_reach_next_lesson', phase)
            else:
                rec = _pair_record(kind, subject, first, second, service, route, gap,
                                   route['minutes'], '可行', 'itinerary_feasible', phase)
        if rec['status'] != '可行':
            feasible = False
        records.append(rec)
    return feasible, records


def evaluate_assignments(db, assignments, phase, services=None):
    """分别核实教师与班级两条行程链。返回 (全部可行?, 全部相邻课节记录)。"""
    if not active_mode(db):
        return True, []
    services = services or class_service_map(db)
    records = []
    ok = True
    tchain = teacher_chain(db, assignments)
    for teacher, entries in sorted(tchain.items()):
        feasible, recs = evaluate_chain(
            db, 'teacher', teacher, entries,
            lambda e: e.get('service') or WALK, phase)
        ok = ok and feasible
        records.extend(recs)
    cchain = class_chain(db, assignments)
    for klass, entries in sorted(cchain.items()):
        contract = services.get(klass)
        service_name = contract['service'] if contract else None

        def lookup(e, _name=service_name):
            return e.get('service') or _name
        feasible, recs = evaluate_chain(db, 'class', klass, entries, lookup, phase)
        ok = ok and feasible
        records.extend(recs)
    return ok, records
