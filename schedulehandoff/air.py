"""Classroom air evidence: CO2 samples, venue profiles, and ventilation assessment.

run() keeps the original single-reading intake. The other commands record
local evidence (room volumes, outdoor background, sensor calibration,
return-air links, door counts, fan logs) and pre-registered ventilation
mandates, then assess() combines them with the schedule's room slots into
a provable exchange-ratio range per mandate window. Air evidence never
rewrites schedule or substitute decisions.
"""
import json

from .ingest import capture
from .operations import class_periods, overlap
from .store import connect, decide, save
from . import ventilation

MANDATE_KINDS = ('预防性巡检', '达标公示')
DISPATCH_NOTE = '派查依据为存在低于约定换气比例的相容解释，不等于已证不达标'


def run(path, file):
    source, payload = capture(path, 'air_sensor', file)
    db = connect(path)
    output = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                room = row['room']
                value = float(row['ppm'])
                if value < 0:
                    raise ValueError('negative CO2 reading')
                status = 'elevated' if value > 1000 else 'normal'
                save(db, 'air_sample', key, source, payload['version'], row, status, 'single reading threshold')
                save(db, 'air_case', f'{room}:{key}', source, payload['version'],
                     {'room': room, 'sample_id': key, 'ppm': value}, status, 'single reading threshold')
                output.append(decide(db, 'air', f'{room}:{key}', status, 'single reading threshold', source))
        return output
    finally:
        db.close()


def _rows(path, file, kind):
    return capture(path, kind, file)


def venue(path, file):
    """Record room volumes, activity source ranges, return-air links and
    outdoor background. Profiles are immutable; corrections arrive as new
    rows and the latest capture wins."""
    source, payload = _rows(path, file, 'air_venue')
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                kind = row.get('kind')
                table = {'room': 'air_room', 'activity': 'air_activity',
                         'link': 'air_link', 'outdoor': 'air_outdoor'}.get(kind)
                if table is None:
                    raise ValueError('venue_row_unknown')
                key = str(row['id'])
                if kind == 'room':
                    ok = row.get('room') and row.get('activity') and row.get('sensor') and float(row.get('volume_m3', 0)) > 0
                elif kind == 'activity':
                    ok = 0 <= float(row.get('rate_lo', -1)) <= float(row.get('rate_hi', -1))
                elif kind == 'link':
                    ok = (row.get('from') and row.get('to') and row['from'] != row['to']
                          and 0 <= float(row.get('flow_lo', -1)) <= float(row.get('flow_hi', -1)))
                else:
                    ok = row.get('start') and row.get('end') and row['start'] < row['end'] and float(row.get('ppm', -1)) >= 0
                status, reason = ('已采录', 'venue_captured') if ok else ('拒绝', 'venue_row_invalid')
                save(db, table, key, source, payload['version'], row, status, reason)
                results.append(decide(db, '通风证据', key, status, reason, source))
        return results
    finally:
        db.close()


def calibrate(path, file):
    """Record sensor zero-offset and response-lag ranges. The parameters
    are shared across a sensor's whole continuous series; a late
    correction is a new row and recomputes the affected windows."""
    source, payload = _rows(path, file, 'air_calibration')
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                ok = (row.get('sensor')
                      and float(row.get('zero_lo', 0)) <= float(row.get('zero_hi', -1))
                      and 0 <= float(row.get('lag_lo', -1)) <= float(row.get('lag_hi', -1)) < 1)
                status, reason = ('已采录', 'calibration_captured') if ok else ('拒绝', 'calibration_invalid')
                save(db, 'sensor_cal', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '通风证据', key, status, reason, source))
        return results
    finally:
        db.close()


def doors(path, file):
    """Record door counts. Timestamped entry/exit or absolute counts move
    the occupancy interval; a period total only caps it and never
    fabricates per-minute occupancy."""
    source, payload = _rows(path, file, 'air_door_count')
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                point = row.get('at') and ('present' in row or 'entered' in row or 'exited' in row)
                total = row.get('start') and row.get('end') and 'total' in row
                ok = row.get('room') and (point or total)
                status, reason = ('已采录', 'door_count_captured') if ok else ('拒绝', 'door_row_invalid')
                save(db, 'door_count', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '通风证据', key, status, reason, source))
        return results
    finally:
        db.close()


def fanlog(path, file):
    """Record fan logs; they bound the exchange ratio of a room for the
    covered period."""
    source, payload = _rows(path, file, 'air_fan_log')
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                ok = (row.get('room') and row.get('start') and row.get('end')
                      and 0 <= float(row.get('k_lo', -1)) <= float(row.get('k_hi', -1)))
                status, reason = ('已采录', 'fan_log_captured') if ok else ('拒绝', 'fan_log_invalid')
                save(db, 'fan_log', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '通风证据', key, status, reason, source))
        return results
    finally:
        db.close()


def mandate(path, file):
    """Register a ventilation mandate. Mandates are fixed before sampling:
    a new mandate is rejected once samples for its room and window exist,
    and its kind/threshold cannot be changed afterwards."""
    source, payload = _rows(path, file, 'air_mandate_file')
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                old = db.execute('SELECT * FROM air_mandate WHERE id=?', (key,)).fetchone()
                if old:
                    save(db, 'air_mandate', key, source, payload['version'], row, old['status'], old['reason'])
                    results.append(decide(db, '通风委托', key, old['status'], old['reason'], source))
                    continue
                sampled = any(json.loads(x['body']).get('room') == row.get('room')
                              and row['start'] <= json.loads(x['body']).get('at', '') <= row['end']
                              for x in db.execute('SELECT body FROM air_sample')) if row.get('start') and row.get('end') else False
                if not row.get('room') or not row.get('start') or not row.get('end') or row['start'] >= row['end']:
                    status, reason = '拒绝', 'window_invalid'
                elif row.get('kind') not in MANDATE_KINDS:
                    status, reason = '拒绝', 'kind_unknown'
                elif not float(row.get('threshold', 0)) > 0:
                    status, reason = '拒绝', 'threshold_invalid'
                elif sampled:
                    status, reason = '拒绝', 'samples_before_mandate'
                else:
                    status, reason = '已登记', 'mandate_fixed_before_sampling'
                save(db, 'air_mandate', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '通风委托', key, status, reason, source))
        return results
    finally:
        db.close()


def _latest(db, table, keyfn):
    rows = {}
    for x in db.execute(f"SELECT * FROM {table} WHERE status='已采录'"):
        body = json.loads(x['body'])
        key = keyfn(body)
        if key not in rows or rows[key]['source_id'] < x['source_id']:
            rows[key] = {'body': body, 'source_id': x['source_id']}
    return rows


def _gather(db, mandate_row, body):
    """Collect every evidence source relevant to one mandate window."""
    ws, we = body['start'], body['end']
    sources = {mandate_row['source_id']}
    profiles = _latest(db, 'air_room', lambda b: b['room'])
    activities = _latest(db, 'air_activity', lambda b: b['id'])
    links = _latest(db, 'air_link', lambda b: b['id'])
    outdoor = _latest(db, 'air_outdoor', lambda b: b['id'])
    profile = profiles.get(body['room'])
    involved = {body['room']}
    if profile:
        act = profile['body'].get('activity')
        involved |= {r for r, p in profiles.items() if p['body'].get('activity') == act}
        for link in links.values():
            other = None
            if link['body'].get('from') == body['room']:
                other = link['body'].get('to')
            elif link['body'].get('to') == body['room']:
                other = link['body'].get('from')
            if other in profiles:
                involved.add(other)
    involved = {r for r in involved if r in profiles}
    used_links = [x for x in links.values()
                  if x['body'].get('from') in involved and x['body'].get('to') in involved]
    for room in involved:
        sources.add(profiles[room]['source_id'])
    for link in used_links:
        sources.add(link['source_id'])
    used_acts = {profiles[r]['body'].get('activity') for r in involved}
    for act in used_acts:
        if act in activities:
            sources.add(activities[act]['source_id'])

    samples = {}
    sample_rows = [(x['source_id'], json.loads(x['body'])) for x in db.execute('SELECT source_id,body FROM air_sample')]
    for room in sorted(involved):
        series = sorted((r for r in sample_rows if r[1].get('room') == room), key=lambda r: r[1]['at'])
        before = [r for r in series if r[1]['at'] < ws]
        inside = [r for r in series if ws <= r[1]['at'] <= we]
        used = ([before[-1]] if before else []) + inside
        if used:
            samples[room] = [(r[1]['at'], r[1]['ppm']) for r in used]
            for source_id, _ in used:
                sources.add(source_id)

    door_map = {}
    for x in db.execute("SELECT * FROM door_count WHERE status='已采录'"):
        b = json.loads(x['body'])
        if b.get('room') not in involved:
            continue
        relevant = ('at' in b and b['at'] <= we) or ('start' in b and overlap(b['start'], b['end'], ws, we))
        if not relevant:
            continue
        sources.add(x['source_id'])
        entry = door_map.setdefault(b['room'], {'points': [], 'totals': []})
        entry['points' if 'at' in b else 'totals'].append(b)

    fan = {}
    for x in db.execute("SELECT * FROM fan_log WHERE status='已采录'"):
        b = json.loads(x['body'])
        if b.get('room') in involved and overlap(b['start'], b['end'], ws, we):
            sources.add(x['source_id'])
            lo, hi = fan.get(b['room'], (0.0, float(ventilation.K_CAP)))
            fan[b['room']] = (max(lo, float(b['k_lo'])), min(hi, float(b['k_hi'])))

    cals = {}
    missing_cal = False
    cal_rows = _latest(db, 'sensor_cal', lambda b: b['sensor'])
    for room in involved:
        sensor = profiles[room]['body'].get('sensor')
        cal = cal_rows.get(sensor)
        if cal is None:
            missing_cal = True
            continue
        sources.add(cal['source_id'])
        cals[sensor] = {'zero': (cal['body']['zero_lo'], cal['body']['zero_hi']),
                        'lag': (cal['body']['lag_lo'], cal['body']['lag_hi'])}

    enrolled = {}
    for room in involved:
        slots = class_periods(db, room, ws, we)
        for slot in slots:
            sources.add(slot['source_id'])
        enrolled[room] = max([int(s['body']['enrolled']) for s in slots], default=0)

    outdoor_periods = []
    for x in outdoor.values():
        b = x['body']
        if overlap(b['start'], b['end'], ws, we):
            sources.add(x['source_id'])
            outdoor_periods.append((b['start'], b['end'], b['ppm']))

    missing = None
    if profile is None:
        missing = 'profile_missing'
    elif any(act not in activities for act in used_acts):
        missing = 'activity_missing'
    elif missing_cal:
        missing = 'cal_missing'
    elif not outdoor_periods:
        missing = 'outdoor_missing'
    evidence = {
        'rooms': {r: profiles[r]['body'] for r in sorted(involved)},
        'links': [{'id': x['body']['id'], 'from': x['body']['from'], 'to': x['body']['to'],
                   'lo': x['body']['flow_lo'], 'hi': x['body']['flow_hi']} for x in used_links],
        'activities': {a: (activities[a]['body']['rate_lo'], activities[a]['body']['rate_hi'])
                       for a in used_acts if a in activities},
        'samples': samples, 'cals': cals, 'doors': door_map, 'fan': fan,
        'enrolled': enrolled, 'outdoor': outdoor_periods,
        'sources': sorted(sources),
    }
    return evidence, missing


def _decide_mandate(kind, k_range, threshold, missing, feasible):
    if missing:
        return '待补证据', missing
    if not feasible:
        return '证据冲突', 'no_consistent_explanation'
    k_lo, k_hi = k_range
    if kind == '预防性巡检':
        return ('派查', 'exists_subthreshold_explanation') if k_lo < threshold else ('免查', 'all_explanations_adequate')
    if k_lo >= threshold:
        return '可签发', 'all_explanations_adequate'
    if k_hi < threshold:
        return '不签发', 'all_explanations_below'
    return '待证', 'exists_subthreshold_explanation'


def _floatify(value):
    if isinstance(value, dict):
        return {k: _floatify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_floatify(v) for v in value]
    return float(value) if isinstance(value, ventilation.Fraction) else value


def _assess_one(db, mandate_row):
    body = json.loads(mandate_row['body'])
    ws, we = body['start'], body['end']
    evidence, missing = _gather(db, mandate_row, body)
    last = db.execute('SELECT * FROM air_assessment WHERE mandate_id=? AND superseded=0',
                      (mandate_row['id'],)).fetchone()
    if last and json.loads(last['body'])['sources'] == evidence['sources']:
        kept = json.loads(last['body'])
        return {'mandate': mandate_row['id'], 'status': last['status'], 'reason': last['reason'],
                'k_range': kept.get('k_range'), 'changed': False}

    threshold = ventilation.F(body['threshold'])
    if missing:
        result, feasible, k_range, witnesses = None, False, None, None
    else:
        result = ventilation.assess_window(
            (ws, we), evidence['rooms'], evidence['links'], evidence['activities'],
            evidence['samples'], evidence['cals'], evidence['doors'], evidence['fan'],
            evidence['enrolled'], evidence['outdoor'])
        if result['status'] == 'evidence_missing':
            missing = result['detail']
            result, feasible, k_range, witnesses = None, False, None, None
        else:
            feasible = result['status'] == 'ok'
            k_range = result['k'].get(body['room']) if feasible else None
            witnesses = result.get('witnesses', {}).get(body['room']) if feasible else None
    status, reason = _decide_mandate(body['kind'], k_range, threshold, missing, feasible)
    notes = list(result['notes']) if result else []
    if feasible and result['at_cap'].get(body['room']):
        notes.append('k_range_upper_at_cap')
    if status == '派查':
        notes.append(DISPATCH_NOTE)
    record = {
        'mandate': mandate_row['id'], 'room': body['room'], 'window': [ws, we],
        'kind': body['kind'], 'threshold': float(threshold),
        'segments': result['segments'] if result else [],
        'outdoor': [{'start': s, 'end': e, 'ppm': p} for s, e, p in evidence['outdoor']],
        'sensor_cal': {sid: {'zero': list(c['zero']), 'lag': list(c['lag']), 'shared': True}
                       for sid, c in evidence['cals'].items()},
        'links': [{'id': l['id'], 'from': l['from'], 'to': l['to'], 'flow': [l['lo'], l['hi']],
                   'conservation': '同一股回风在两侧房间平衡中共用'} for l in evidence['links']],
        'activities': {a: {'rate': list(r), 'shared_across_rooms': True}
                       for a, r in evidence['activities'].items()},
        'k_range': [float(k_range[0]), float(k_range[1])] if k_range else None,
        'k_rooms': {r: [float(kr[0]), float(kr[1])] for r, kr in result['k'].items()} if feasible else {},
        'explanations': _floatify(witnesses) if witnesses else None,
        'notes': notes,
        'sources': evidence['sources'],
    }
    db.execute('UPDATE air_assessment SET superseded=1 WHERE mandate_id=?', (mandate_row['id'],))
    source_id = max(evidence['sources'])
    db.execute('INSERT INTO air_assessment(mandate_id,body,status,reason,source_id) VALUES (?,?,?,?,?)',
               (mandate_row['id'], json.dumps(record, ensure_ascii=False, sort_keys=True),
                status, reason, source_id))
    decide(db, '通风委托', mandate_row['id'], status, reason, source_id)
    return {'mandate': mandate_row['id'], 'status': status, 'reason': reason,
            'k_range': record['k_range'], 'changed': True}


def assess(path, only=None):
    """Recompute the current ventilation conclusion of every mandate whose
    evidence changed. Superseded assessments, old parameter ranges,
    conflicts and earlier mandate decisions are kept."""
    db = connect(path)
    results = []
    try:
        with db:
            rows = db.execute("SELECT * FROM air_mandate WHERE status='已登记' ORDER BY id").fetchall()
            for row in rows:
                if only and row['id'] != only:
                    continue
                results.append(_assess_one(db, row))
        return results
    finally:
        db.close()
