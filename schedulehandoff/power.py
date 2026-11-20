"""Offline distribution evidence: topology, fault, switching plan and proof.

Public commands (wired in ``__main__``):

``power-topology``  import feeders, phase allocation, breakers, tie switches,
                    room loads and raw measurements with their provenance.
``power-fault``     record the faulted section and the devices that tripped.
``power-service``   record the service agreements frozen *before* the fault.
``power-plan``      recompute a building-wide switching sequence from the
                    current ``room_slot`` table and the frozen agreements.
``power-execution`` import device receipts / observed currents for a plan.
``power-correct``   import a late correction of an earlier current reading.
``power-proof``     decide whether a completed transfer is proven, from the
                    execution evidence (never from the plan).
``power-import``    legacy single-circuit reading comparison, kept intact.

Circuit evidence is never written back into the timetable, teacher or notice
tables.  The dependency runs the other way: a schedule change or correction
invalidates an existing plan/proof.
"""
import hashlib
import json

from . import distribution as dist
from .ingest import capture
from .store import connect, decide, keyed_save

POWER_TABLES = (
    'power_topology', 'power_feeder', 'power_breaker', 'power_tie_switch',
    'power_room_load', 'power_measurement', 'power_service', 'power_fault',
    'power_switching_plan', 'power_switching_execution', 'power_switching_proof',
    'power_correction',
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _current(db, table, key):
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    return None if row is None else {**dict(row), 'body': json.loads(row['body'])}


def _all(db, table):
    return [dict(x) | {'body': json.loads(x['body'])}
            for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]


def _fingerprint(db):
    """Fingerprint of the whole timetable snapshot a plan was computed from.

    Every ``room_slot`` row is included, not just the ones currently marked
    ``可用``: a new lesson that is pending coordination, or a superseded row,
    is still a timetable change that must force a replan before a proof can
    be issued.  Unrelated imports therefore also force a replan - the
    conservative direction for evidence.
    """
    rows = []
    for item in db.execute("SELECT * FROM room_slot ORDER BY id"):
        body = json.loads(item['body'])
        rows.append([item['id'], item['version'], item['status'],
                     body.get('room'), body.get('start'), body.get('end'),
                     body.get('capacity'), body.get('enrolled')])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _active_slots(db):
    out = []
    for item in db.execute("SELECT * FROM room_slot WHERE status='可用' ORDER BY id"):
        body = json.loads(item['body'])
        out.append({'slot': item['id'], 'version': item['version'], **body})
    return out


def _occupied_at(slots, at):
    """Slots covering ``at`` (ISO string); malformed timestamps count as active."""
    from datetime import datetime
    try:
        when = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return list(slots)
    out = []
    for slot in slots:
        try:
            start = datetime.fromisoformat(slot['start'])
            end = datetime.fromisoformat(slot['end'])
        except (KeyError, TypeError, ValueError):
            out.append(slot)
            continue
        if start <= when < end:
            out.append(slot)
    return out


def _store_topology(db, source, version, topo):
    key = topo['id']
    keyed_save(db, 'power_topology', key, version, dist.topology_body(topo),
               '已采录', 'topology_captured', source)
    for feeder in topo['sources'].values():
        keyed_save(db, 'power_feeder', f"{key}:{feeder['id']}", version,
                   dict(feeder), '已采录', 'feeder_captured', source)
    for breaker in topo['breakers']:
        keyed_save(db, 'power_breaker', f"{key}:{breaker['id']}", version,
                   dict(breaker), '已采录', 'breaker_captured', source)
    for tie in topo['ties']:
        keyed_save(db, 'power_tie_switch', f"{key}:{tie['id']}", version,
                   dict(tie), '已采录', 'tie_captured', source)
    for load in topo['loads'].values():
        keyed_save(db, 'power_room_load', f"{key}:{load['room']}", version,
                   dict(load), '已采录', 'room_load_captured', source)


def _import_measurements(db, source, version, rows):
    saved = []
    for row in rows:
        key = str(row['id'])
        amps = float(row['amps'])
        if amps < 0:
            raise ValueError('invalid_current')
        body = {'feeder': str(row.get('feeder', '')), 'circuit': row.get('circuit'),
                'room': row.get('room'), 'phase': row.get('phase'),
                'amps': amps, 'at': row.get('at'),
                'rated_amp': row.get('rated_amp'),
                'source': row.get('source') or source}
        keyed_save(db, 'power_measurement', key, version, body,
                   '已采录', 'raw_measurement', source)
        saved.append(key)
    return saved


# ---------------------------------------------------------------------------
# imports
# ---------------------------------------------------------------------------

def topology(path, file):
    source, payload = capture(path, 'power_topology', file)
    db = connect(path)
    try:
        with db:
            topo = dist.parse_topology(payload)
            _store_topology(db, source, payload['version'], topo)
            evidence = _import_measurements(db, source, payload['version'],
                                            payload.get('measurements', []))
            decide(db, '配电拓扑', topo['id'], '已采录', 'topology_and_measurements', source)
        return {'topology_id': topo['id'], 'feeders': sorted(topo['sources']),
                'breakers': len(topo['breakers']), 'ties': len(topo['ties']),
                'rooms': sorted(topo['loads']), 'measurements': evidence}
    finally:
        db.close()


def fault(path, file):
    source, payload = capture(path, 'power_fault', file)
    db = connect(path)
    try:
        with db:
            item = payload.get('fault') or payload
            fid = str(item['id'])
            segments = [str(x) for x in item.get('segments', [])]
            if not segments:
                raise ValueError('fault_segments_required')
            body = {'id': fid, 'at': item.get('at'),
                    'segments': segments,
                    'tripped': [str(x) for x in item.get('tripped', [])],
                    'isolated': bool(item.get('isolated', True)),
                    'source': item.get('source') or source}
            if not body['isolated']:
                raise ValueError('fault_not_isolated')
            keyed_save(db, 'power_fault', fid, payload['version'], body,
                       '已采录', 'fault_scope_captured', source)
            decide(db, '故障隔离', fid, '已采录', 'fault_scope_captured', source)
        return body
    finally:
        db.close()


def service(path, file):
    """Import service agreements that were frozen *before* the fault.

    ``fault_at`` is the fault timestamp as known when the agreements were
    captured.  An agreement signed at or after that time is kept as evidence
    but marked stale, so a post-fault promise cannot be presented as a
    pre-fault commitment.
    """
    source, payload = capture(path, 'power_service', file)
    db = connect(path)
    try:
        from datetime import datetime
        with db:
            fault_at = payload.get('fault_at')
            saved = []
            for row in payload.get('rows', []):
                key = str(row['id'])
                contract = row['contract']
                if contract not in dist.CONTRACTS:
                    raise ValueError('unknown_contract')
                for cls in row.get('load_classes', []):
                    if cls not in dist.CLASSES:
                        raise ValueError('unknown_load_class')
                signed_after = False
                if fault_at and row.get('signed_at'):
                    try:
                        signed_after = (datetime.fromisoformat(row['signed_at'])
                                        >= datetime.fromisoformat(fault_at))
                    except ValueError:
                        raise ValueError('invalid_timestamp')
                body = {'id': key, 'room': str(row['room']),
                        'lesson': str(row.get('lesson', '')),
                        'contract': contract,
                        'load_classes': list(row.get('load_classes', [])),
                        'max_interruption_min': row.get('max_interruption_min'),
                        'signed_at': row.get('signed_at'),
                        'fault_at': fault_at,
                        'signed_after_fault': signed_after,
                        'source': row.get('source') or source}
                keyed_save(db, 'power_service', key, payload['version'], body,
                           '不能履约' if signed_after else '已登记',
                           'contract_not_frozen_before_fault' if signed_after
                           else 'service_contract_frozen', source)
                saved.append(key)
            decide(db, '服务约定', 'agreements', '已登记',
                   'service_contracts_captured', source)
        return {'services': saved}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _load_services(db):
    return [_current(db, 'power_service', x['id']) for x in _all(db, 'power_service')]


def _service_for(services, room, lesson):
    matches = [s for s in services if s and s['body']['room'] == room]
    if lesson:
        exact = [s for s in matches if s['body'].get('lesson') == lesson]
        if exact:
            return exact[0]
    return matches[0] if matches else None


def _fault_scope(fault_body):
    return {'id': fault_body.get('id'), 'at': fault_body.get('at'),
            'segments': list(fault_body.get('segments', [])),
            'tripped': list(fault_body.get('tripped', []))}


def _plan_inputs(db, fault_body):
    topo_rows = _all(db, 'power_topology')
    if not topo_rows:
        raise ValueError('topology_missing')
    topo = dist.from_body(topo_rows[-1]['body'])
    services = _load_services(db)
    slots = _occupied_at(_active_slots(db), fault_body.get('at'))
    in_use = {}
    for slot in slots:
        in_use.setdefault(slot['room'], []).append(slot)
    protected = sorted(set(in_use) & set(topo['loads']))
    missing = sorted(set(in_use) - set(topo['loads']))
    sheddable = set()
    service_map = {}
    problems = []
    for room in protected:
        matching = None
        for slot in in_use[room]:
            service = _service_for(services, room, slot.get('id'))
            if service:
                matching = service
                break
        if matching is None:
            service_map[room] = None
            problems.append('service_contract_missing:' + room)
            continue
        body = matching['body']
        service_map[room] = body
        if body.get('signed_after_fault'):
            problems.append('contract_not_frozen_before_fault:' + room)
        rule = dist.service_rule(body.get('contract'), body.get('max_interruption_min'))
        load = topo['loads'][room]
        # Load-class membership comes from the pre-fault service agreement,
        # not from the planner.  A load may be shed only if that agreement
        # explicitly declares the room's deferrable class, the contract allows
        # a brief outage, and neither the agreement nor the topology carries
        # non-interruptible exam equipment.
        promised = set(body.get('load_classes', []))
        if (rule and rule['brief_outage_ok'] and rule['max_interruption_min'] > 0
                and 'deferrable' in promised and 'deferrable' in load['classes']
                and 'critical' not in promised and 'critical' not in load['classes']):
            sheddable.add(room)
    return topo, protected, sheddable, service_map, in_use, missing, sorted(set(problems)), services


def plan(path, file):
    """Recompute the candidate sequence from the *current* room_slot table."""
    source, payload = capture(path, 'power_switching_request', file)
    db = connect(path)
    try:
        with db:
            fault_id = str(payload['fault_id'])
            fault_row = _current(db, 'power_fault', fault_id)
            if fault_row is None:
                raise ValueError('fault_missing')
            fault_body = fault_row['body']
            topo, protected, sheddable, service_map, in_use, missing, problems, _services = \
                _plan_inputs(db, fault_body)
            fault_spec = _fault_scope(fault_body)
            result = dist.plan_sequence(topo, fault_spec, protected, sheddable,
                                        fault_body['segments'])
            if missing:
                problems = list(problems) + ['room_not_in_topology:' + r for r in missing]
            if problems:
                # A missing service agreement, a post-fault agreement or a
                # room absent from the topology means the plan cannot rest on
                # frozen evidence, regardless of what the switch search finds.
                result = dict(result, feasible=False, steps=[], outage_minutes={},
                              deferred_minutes={},
                              reasons=dist.minimal_reasons(
                                  list(result.get('reasons', [])) + problems))
            plan_id = str(payload.get('plan_id') or f"plan:{fault_id}")
            body = {'plan_id': plan_id, 'fault_id': fault_id, 'topology_id': topo['id'],
                    'topology': dist.topology_body(topo),
                    'fault': fault_spec, 'fault_segments': fault_body['segments'],
                    'protected_rooms': protected,
                    'sheddable_rooms': sorted(sheddable),
                    'in_use_lessons': {r: [s['id'] for s in in_use[r]] for r in sorted(in_use)},
                    'missing_topology_rooms': missing,
                    'schedule_fingerprint': _fingerprint(db),
                    'feasible': result['feasible'],
                    'steps': result['steps'],
                    'outage_minutes': result['outage_minutes'],
                    'deferred_minutes': result.get('deferred_minutes', {}),
                    'reasons': result.get('reasons', []),
                    'individual_feasibility': result.get('individual_feasibility', {}),
                    'scenario_state': dist.state_body(dist.scenario_state(topo, fault_spec)),
                    'elapsed_minutes': result.get('elapsed_minutes')}
            if not result['feasible']:
                body['reasons'] = dist.minimal_reasons(problems + result['reasons'])
            keyed_save(db, 'power_switching_plan', plan_id, payload['version'], body,
                       '可执行' if result['feasible'] else '不可执行',
                       'joint_sequence_verified' if result['feasible'] else ';'.join(body['reasons']),
                       source)
            verdicts = _plan_verdicts(protected, service_map, body, problems)
            decide(db, '切换方案', plan_id,
                   '可执行' if result['feasible'] else '不可执行',
                   'joint_sequence_verified' if result['feasible']
                   else ';'.join(body['reasons']), source)
        return {'plan_id': plan_id, 'feasible': result['feasible'],
                'steps': body['steps'], 'outage_minutes': body['outage_minutes'],
                'protected_rooms': protected, 'sheddable_rooms': sorted(sheddable),
                'reasons': body['reasons'], 'services': verdicts,
                'individual_feasibility': body['individual_feasibility'],
                'schedule_fingerprint': body['schedule_fingerprint']}
    finally:
        db.close()


def _plan_verdicts(protected, service_map, plan_body, problems):
    verdicts = []
    base_reasons = list(problems)
    if not plan_body['feasible']:
        base_reasons = dist.minimal_reasons(base_reasons + plan_body.get('reasons', []))
    for room in protected:
        service = service_map.get(room)
        if service is None:
            verdicts.append({'room': room, 'lesson': plan_body['in_use_lessons'].get(room),
                             'status': '不能履约', 'reason': 'service_contract_missing',
                             'minimal_reasons': ['service_contract_missing'],
                             'basis': 'planned'})
            continue
        verdicts.append(dist.screen_service(
            {'room': room, 'lesson': service.get('lesson'),
             'contract': service.get('contract'),
             'max_interruption_min': service.get('max_interruption_min'),
             'signed_after_fault': service.get('signed_after_fault')},
            'planned', plan_body['outage_minutes'], plan_body['feasible'], base_reasons))
    return verdicts


# ---------------------------------------------------------------------------
# execution, corrections, proof
# ---------------------------------------------------------------------------

def execution(path, file):
    source, payload = capture(path, 'power_switching_execution', file)
    db = connect(path)
    try:
        with db:
            item = payload.get('execution') or payload
            eid = str(item['id'])
            body = {'id': eid, 'plan_id': str(item['plan_id']),
                    'topology_id': item.get('topology_id'),
                    'fault_id': item.get('fault_id'),
                    'started_at': item.get('started_at'),
                    'fault_at': item.get('fault_at'),
                    'meter_evidence': item.get('meter_evidence'),
                    'steps': list(item.get('steps', [])),
                    'source': item.get('source') or source}
            plan_row = _current(db, 'power_switching_plan', body['plan_id'])
            if plan_row is None:
                status, reason = '拒绝', 'plan_missing'
            else:
                status, reason = '已采录', 'execution_receipts_captured'
            keyed_save(db, 'power_switching_execution', eid, payload['version'], body,
                       status, reason, source)
            decide(db, '切换执行', eid, status, reason, source)
        return {'execution_id': eid, 'status': status, 'reason': reason}
    finally:
        db.close()


def correct(path, file):
    """Late correction of a current reading already used by an execution."""
    source, payload = capture(path, 'power_current_correction', file)
    db = connect(path)
    try:
        with db:
            saved = []
            for row in payload.get('rows', []):
                key = str(row['id'])
                observed = row.get('observed_amps') or row.get('amps')
                if not isinstance(observed, dict):
                    raise ValueError('corrected_amps_required')
                body = {'id': key, 'execution': str(row['execution']),
                        'step': int(row['step']), 'observed_amps': observed,
                        'at': row.get('at'), 'reason': row.get('reason'),
                        'source': row.get('source') or source}
                keyed_save(db, 'power_correction', key, payload['version'], body,
                           '已更正', 'late_current_correction', source)
                saved.append(key)
            decide(db, '电流更正', 'corrections', '已更正',
                   'late_current_correction', source)
        return {'corrections': saved}
    finally:
        db.close()


def _corrections(db, execution_id):
    out = {}
    for row in _all(db, 'power_correction'):
        body = row['body']
        if body['execution'] == execution_id:
            out[(execution_id, int(body['step']))] = body['observed_amps']
    return out


def proof(path, file):
    """Issue or refuse a completed-transfer proof from execution evidence."""
    source, payload = capture(path, 'power_switching_proof', file)
    db = connect(path)
    try:
        with db:
            plan_id = str(payload['plan_id'])
            execution_id = str(payload['execution_id'])
            plan_row = _current(db, 'power_switching_plan', plan_id)
            exec_row = _current(db, 'power_switching_execution', execution_id)
            if plan_row is None:
                result = {'status': '拒绝', 'reason': 'plan_missing',
                          'reasons': ['plan_missing'], 'executed_outage_minutes': {}}
            else:
                body = plan_row['body']
                topo = dist.from_body(body['topology'])
                fault_spec = body['fault']
                execution_body = exec_row['body'] if exec_row else None
                current_fault = _current(db, 'power_fault', body['fault_id'])
                current_topology = _all(db, 'power_topology')
                current_topology = (dist.topology_body(dist.from_body(current_topology[-1]['body']))
                                    if current_topology else None)
                result = dist.proof_check(
                    topo, fault_spec, body, execution_body,
                    corrections=_corrections(db, execution_id),
                    current_fingerprint=_fingerprint(db),
                    current_fault=_fault_scope(current_fault['body']) if current_fault else None,
                    current_topology=current_topology)
            proof_id = str(payload.get('proof_id') or f"proof:{execution_id}")
            keyed_save(db, 'power_switching_proof', proof_id, payload['version'], result,
                       result['status'], result['reason'], source, recomputable=True)
            decide(db, '切换证明', proof_id, result['status'], result['reason'], source)
        return result
    finally:
        db.close()


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

def demo(root):
    """Reproduce the synthetic acceptance floor with a one-minute outage."""
    import json
    from pathlib import Path
    root = Path(root)
    db = root / 'ledger.db'

    def write(name, obj):
        p = root / name
        p.write_text(json.dumps(obj), encoding='utf-8')
        return p

    from . import operations
    operations.first(db, write('rooms.json', {'version': 'v1', 'rows': [
        {'id': 'L2', 'room': 'R2', 'start': '2026-01-01T09:00:00',
         'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20},
        {'id': 'L1', 'room': 'R1', 'start': '2026-01-01T09:00:00',
         'end': '2026-01-01T10:00:00', 'capacity': 30, 'enrolled': 20}]}))
    topology(db, write('topology.json', {
        'version': 'v1', 'topology': {'id': 'T1'},
        'feeders': [{'id': 'A', 'bus': 'busA', 'rated_amp': 10},
                    {'id': 'B', 'bus': 'busB', 'rated_amp': 10}],
        'breakers': [
            {'id': 'BR2', 'from': 'bus:busA', 'to': 'seg:F', 'phase': 'A', 'initial': 'closed'},
            {'id': 'BR2R', 'from': 'seg:F', 'to': 'seg:S2', 'phase': 'A', 'initial': 'closed'},
            {'id': 'BR1', 'from': 'bus:busB', 'to': 'seg:S1', 'phase': 'A', 'initial': 'closed'}],
        'tie_switches': [{'id': 'T1', 'from': 'seg:S2', 'to': 'seg:S1', 'phase': 'A', 'initial': 'open'}],
        'room_loads': [
            {'room': 'R2', 'segment': 'S2', 'phase': 'A',
             'classes': {'basic': 6}, 'startup': {'basic': 8}},
            {'room': 'R1', 'segment': 'S1', 'phase': 'A',
             'classes': {'deferrable': 3}, 'startup': {'deferrable': 3}}],
        'measurements': [{'id': 'm-B', 'feeder': 'B', 'phase': 'A', 'amps': 3,
                          'at': '2026-01-01T08:59:00', 'source': 'panel-B'}]}))
    fault(db, write('fault.json', {'version': 'v1', 'fault': {
        'id': 'F1', 'at': '2026-01-01T09:10:00', 'segments': ['F']}}))
    service(db, write('services.json', {'version': 'v1',
                                        'fault_at': '2026-01-01T09:10:00', 'rows': [
        {'id': 'S2', 'room': 'R2', 'lesson': 'L2', 'contract': '普通授课',
         'load_classes': ['basic'], 'signed_at': '2026-01-01T08:00:00'},
        {'id': 'S1', 'room': 'R1', 'lesson': 'L1', 'contract': '普通授课',
         'load_classes': ['basic', 'deferrable'], 'signed_at': '2026-01-01T08:00:00'}]}))
    planned = plan(db, write('plan.json', {'version': 'v1', 'plan_id': 'P1', 'fault_id': 'F1'}))
    stamps = iter(['2026-01-01T09:10:10', '2026-01-01T09:10:20',
                   '2026-01-01T09:10:40', '2026-01-01T09:11:00',
                   '2026-01-01T09:11:20'])
    receipt = []
    for step in planned['steps']:
        spec = step['action_spec']
        item = {'at': next(stamps), 'receipt': True}
        if spec['kind'] in ('open', 'close'):
            item[spec['kind']] = spec['id']
            item['phase'] = spec['phase']
        else:
            item[spec['kind']] = spec['room']
            item['class'] = spec['class']
        if spec['kind'] in ('close', 'ramp'):
            item['observed_amps'] = {'A': step['currents']['B']['A']}
        receipt.append(item)
    execution(db, write('execution.json', {'version': 'v1', 'execution': {
        'id': 'E1', 'plan_id': 'P1', 'topology_id': 'T1', 'fault_id': 'F1',
        'fault_at': '2026-01-01T09:10:00', 'started_at': '2026-01-01T09:10:05',
        'meter_evidence': {'m-B': 'panel-B'}, 'steps': receipt}}))
    proven = proof(db, write('proof.json', {'version': 'v1',
                                            'plan_id': 'P1', 'execution_id': 'E1'}))
    return {'plan': planned, 'proof': proven}


def run(path, file):
    """Legacy: compare one current reading per circuit against its nameplate."""
    source, payload = capture(path, 'power_meter', file)
    db = connect(path)
    result = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                amps = float(row['amps'])
                limit = float(row['rated_amp'])
                if amps < 0 or limit <= 0:
                    raise ValueError('invalid circuit current')
                status = 'within_limit' if amps <= limit else 'over_limit'
                keyed_save(db, 'power_meter', key, payload['version'], row, status,
                           'single circuit current limit', source)
                keyed_save(db, 'power_case', key, payload['version'],
                           {'circuit': row['circuit'], 'room': row['room'], 'amps': amps},
                           status, 'single circuit current limit', source)
                result.append(decide(db, 'power', key, status,
                                     'single circuit current limit', source))
        return result
    finally:
        db.close()
