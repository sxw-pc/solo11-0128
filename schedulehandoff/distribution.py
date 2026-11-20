"""Offline low-voltage distribution model and switching-sequence checks.

The model is deliberately small and explicit:

* two source buses (the normal branch and a backup feeder), breakers between
  a bus/segment and a segment, phase-specific tie switches between segments;
* room loads assigned to one segment and one phase, split into frozen load
  classes: ``basic`` lighting, ``deferrable`` projector, ``critical`` exam
  equipment;
* currents are amperes, tracked per phase and for the shared neutral.

Every switching action is checked against four hard rules:

1. a source may not newly back-feed a segment inside an active fault's
   isolation scope (closing a tie while the fault-side device is still closed
   is rejected, even if the numbers look acceptable);
2. a tie may not be closed while both sides are already live (parallel
   sources / make-before-break);
3. phase and neutral ampacities are checked on the *instantaneous* current,
   i.e. steady current plus the start-up surge of every load class energised
   by the step.  A safe end state does not prove a safe intermediate state;
4. a critical load is never switched off, and a deferrable load may only be
   shed while its room's frozen service agreement permits a brief outage.

Shedding is driven by the signed service agreement and the load class
captured in the topology.  The planner never reclassifies a load in order to
make a sequence feasible.
"""
import heapq
import itertools

PHASES = ('A', 'B', 'C', 'N')
ABC = ('A', 'B', 'C')
CLASSES = ('basic', 'deferrable', 'critical')
# Per-action execution time, used only for *planned* outage estimates.  Real
# outage durations are always measured from execution timestamps.
ACTION_MINUTES = {'open': 0.2, 'close': 0.2, 'shed': 0.1, 'ramp': 0.1}

# Frozen contract types.  ``max_interruption_min`` is a ceiling; a signed
# value may be stricter but can never be looser.
CONTRACTS = {
    '普通授课': {'max_interruption_min': 1.0, 'brief_outage_ok': True},
    '在线考试': {'max_interruption_min': 0.0, 'brief_outage_ok': False},
}

LABELS = {
    'open': '断开', 'close': '合上', 'shed': '暂卸', 'ramp': '恢复',
    'basic': '基本照明', 'deferrable': '可延迟投影机', 'critical': '不可中断考试设备',
}


def _num(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('invalid_' + name)
    if value < 0:
        raise ValueError('invalid_' + name)
    return float(value)


def _phase(value):
    value = str(value)
    if value not in ABC:
        raise ValueError('invalid_phase')
    return value


def tie_key(tie_id, phase):
    return f'{tie_id}|{phase}'


# ---------------------------------------------------------------------------
# topology
# ---------------------------------------------------------------------------

def parse_topology(payload):
    """Validate a topology payload and build the internal snapshot."""
    topo = payload.get('topology') or {}
    topo_id = str(topo.get('id') or payload.get('id') or 'floor')
    sources = {}
    for item in payload.get('feeders', []):
        fid = str(item['id'])
        rated = _num(item['rated_amp'], 'rated_amp')
        phases = {p: _num(v, 'amps') for p, v in (item.get('phases') or {}).items()}
        for p in phases:
            if p not in PHASES:
                raise ValueError('invalid_phase')
        sources[fid] = {'id': fid, 'bus': str(item['bus']),
                        'rated_amp': rated,
                        'phases': phases or {p: rated for p in PHASES},
                        'source': str(item.get('source') or topo_id)}
    if len(sources) < 2:
        raise ValueError('two_sources_required')
    breakers = []
    for item in payload.get('breakers', []):
        frm, to = str(item['from']), str(item['to'])
        if not (frm.startswith('bus:') or frm.startswith('seg:')):
            raise ValueError('invalid_from')
        if not to.startswith('seg:'):
            raise ValueError('invalid_to')
        phases = item.get('phases') or [item.get('phase', 'A')]
        breaker = {'id': str(item['id']), 'from': frm, 'to': to,
                   'phases': [_phase(p) for p in phases],
                   'initial': str(item.get('initial', 'closed')),
                   'source': str(item.get('source') or topo_id)}
        if item.get('rated_amp') is not None:
            breaker['rated_amp'] = _num(item['rated_amp'], 'rated_amp')
        breakers.append(breaker)
    ties = []
    for item in payload.get('tie_switches', []):
        phases = item.get('phases') or [item.get('phase', 'A')]
        ties.append({'id': str(item['id']), 'from': str(item['from']),
                     'to': str(item['to']),
                     'phases': [_phase(p) for p in phases],
                     'initial': str(item.get('initial', 'open')),
                     'source': str(item.get('source') or topo_id)})
    loads = {}
    for item in payload.get('room_loads', []):
        room = str(item['room'])
        classes = {}
        for cls, amps in (item.get('classes') or {}).items():
            if cls not in CLASSES:
                raise ValueError('invalid_load_class')
            classes[cls] = _num(amps, 'amps')
        if not classes:
            raise ValueError('room_load_required')
        startup = {}
        for cls, amps in (item.get('startup') or {}).items():
            if cls not in CLASSES:
                raise ValueError('invalid_load_class')
            startup[cls] = _num(amps, 'startup')
        loads[room] = {'room': room, 'segment': str(item['segment']),
                       'phase': _phase(item.get('phase', 'A')),
                       'classes': classes,
                       'startup': {c: max(startup.get(c, classes[c]), classes[c])
                                   for c in classes},
                       'source': str(item.get('source') or topo_id)}
    return {'id': topo_id, 'sources': sources, 'breakers': breakers,
            'ties': ties, 'loads': loads,
            'source': str(topo.get('source') or 'topology')}


def topology_body(topo):
    """JSON-serialisable snapshot stored in the ledger and inside plans."""
    return {'id': topo['id'],
            'feeders': [dict(f) for f in sorted(topo['sources'].values(), key=lambda x: x['id'])],
            'breakers': [dict(b) for b in topo['breakers']],
            'tie_switches': [dict(t) for t in topo['ties']],
            'room_loads': [{'room': x['room'], 'segment': x['segment'],
                            'phase': x['phase'], 'classes': dict(x['classes']),
                            'startup': dict(x['startup']), 'source': x['source']}
                           for x in sorted(topo['loads'].values(), key=lambda x: x['room'])],
            'source': topo['source']}


def from_body(body):
    """Rebuild an internal topology from a stored ``topology_body``."""
    return parse_topology(body)


def initial_state(topo):
    switches = {b['id']: b['initial'] for b in topo['breakers']}
    for tie in topo['ties']:
        for p in tie['phases']:
            switches[tie_key(tie['id'], p)] = tie['initial']
    classes = {room: {c: True for c in load['classes']}
               for room, load in topo['loads'].items()}
    return {'switches': switches, 'classes': classes}


def scenario_state(topo, fault=None):
    """Fault-time switch state: topology defaults plus tripped devices."""
    state = initial_state(topo)
    for sid in (fault or {}).get('tripped', []):
        sid = str(sid)
        if sid in state['switches']:
            state['switches'][sid] = 'open'
        else:
            for key in [k for k in state['switches'] if k.startswith(sid + '|')]:
                state['switches'][key] = 'open'
    return state


def state_body(state):
    return {'switches': dict(sorted(state['switches'].items())),
            'classes': {r: dict(sorted(c.items())) for r, c in sorted(state['classes'].items())}}


# ---------------------------------------------------------------------------
# electrical evaluation
# ---------------------------------------------------------------------------

def _node(name, phase):
    if name.startswith('bus:'):
        return ('bus', name[4:], phase)
    return ('seg', name[4:] if name.startswith('seg:') else name, phase)


def _graph(topo, state):
    adj = {}

    def link(a, b):
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    for source in topo['sources'].values():
        for p in ABC:
            adj.setdefault(('bus', source['bus'], p), [])
    for b in topo['breakers']:
        if state['switches'].get(b['id'], b['initial']) == 'closed':
            for p in b['phases']:
                link(_node(b['from'], p), _node(b['to'], p))
    for tie in topo['ties']:
        for p in tie['phases']:
            if state['switches'].get(tie_key(tie['id'], p), tie['initial']) == 'closed':
                link(_node(tie['from'], p), _node(tie['to'], p))
    return adj


def _reachable(topo, state, fault_segments=()):
    """Per-source reachable nodes; a faulted section is a dead end.

    The faulted segment itself stays reachable (that is where fault current
    flows) but nothing beyond it is: the fault is a short, not a conductor.
    A room behind the fault is therefore dead while the faulted section stays
    live until its fault-side device opens.
    """
    adj = _graph(topo, state)
    faults = set(fault_segments)
    per = {}
    for feeder, source in topo['sources'].items():
        seen = set()
        for p in ABC:
            start = ('bus', source['bus'], p)
            if start in seen:
                continue
            seen.add(start)
            queue = [start]
            while queue:
                cur = queue.pop(0)
                if cur[0] == 'seg' and cur[1] in faults:
                    continue
                for nxt in adj.get(cur, []):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    queue.append(nxt)
        per[feeder] = seen
    reach = {}
    for feeder, seen in per.items():
        for node in seen:
            reach.setdefault(node, feeder)
    segment_feeds = {}
    for feeder, seen in per.items():
        for node in seen:
            if node[0] == 'seg':
                segment_feeds.setdefault(node[1], set()).add(feeder)
    parallel = sorted(seg for seg, feeds in segment_feeds.items() if len(feeds) > 1)
    fault_sources = {seg: sorted(feeds) for seg, feeds in segment_feeds.items()
                     if seg in faults}
    return reach, parallel, fault_sources


def evaluate(topo, state, fault_segments=()):
    """Return served rooms, currents (per phase and neutral) and limits."""
    reach, parallel, fault_sources = _reachable(topo, state, fault_segments)
    currents = {fid: {p: 0.0 for p in PHASES} for fid in topo['sources']}
    on = {}
    for room, load in topo['loads'].items():
        seg_node = ('seg', load['segment'], load['phase'])
        feeder = reach.get(seg_node)
        on[room] = {}
        for cls, amps in load['classes'].items():
            enabled = bool(state['classes'].get(room, {}).get(cls, True))
            on[room][cls] = bool(feeder and enabled and amps > 0)
            if on[room][cls]:
                currents[feeder][load['phase']] += amps
    for feeder in currents:
        currents[feeder]['N'] = sum(currents[feeder][p] for p in ABC)
    limits = {}
    for feeder, source in topo['sources'].items():
        limits[feeder] = {}
        for p in PHASES:
            limits[feeder][p] = min(source['phases'].get(p, source['rated_amp']),
                                    source['rated_amp'])
        for b in topo['breakers']:
            if b.get('rated_amp') is not None:
                for p in b['phases']:
                    limits[feeder][p] = min(limits[feeder][p], b['rated_amp'])
    served = {room: any(on[room].values()) for room in topo['loads']}
    # A service interruption is the loss of the room's supply, or of a load
    # class the lesson cannot do without (basic lighting / non-interruptible
    # exam equipment).  A deferrable projector may be switched off under a
    # contract that allows it: that is deferred time, tracked separately, not
    # an interruption.  A room that is dark is an interruption regardless of
    # which classes it has.
    essential = {}
    for room, load in topo['loads'].items():
        live = ('seg', load['segment'], load['phase']) in reach
        required = [c for c in load['classes'] if c in ('basic', 'critical')]
        if required:
            essential[room] = bool(live and all(on[room].get(c, False)
                                                for c in required))
        else:
            essential[room] = bool(live)
    return {'reach': reach, 'parallel': parallel, 'currents': currents,
            'limits': limits, 'on': on, 'served': served,
            'essential_served': essential,
            'fault_live': sorted(fault_sources),
            'fault_sources': fault_sources,
            'switch_states': dict(state['switches'])}


def _overload(currents, limits):
    out = []
    for feeder in currents:
        for p in PHASES:
            if currents[feeder][p] > limits[feeder][p] + 1e-9:
                out.append({'feeder': feeder, 'phase': p,
                            'amps': round(currents[feeder][p], 3),
                            'limit': limits[feeder][p]})
    return out


def instantaneous(topo, before, after):
    """Steady state plus the start-up surge of newly energised classes."""
    amps = {f: dict(v) for f, v in after['currents'].items()}
    surges = []
    for room, load in topo['loads'].items():
        for cls, steady in load['classes'].items():
            was = before['on'].get(room, {}).get(cls, False)
            now = after['on'].get(room, {}).get(cls, False)
            if now and not was:
                surge = max(0.0, load['startup'].get(cls, steady) - steady)
                if surge:
                    feeder = after['reach'].get(('seg', load['segment'], load['phase']))
                    if feeder:
                        amps[feeder][load['phase']] += surge
                        amps[feeder]['N'] += surge
                        surges.append({'room': room, 'class': cls,
                                       'surge_amp': round(surge, 3)})
    return amps, surges


def apply_action(state, action):
    new = {'switches': dict(state['switches']),
           'classes': {r: dict(c) for r, c in state['classes'].items()}}
    kind = action['kind']
    if kind in ('open', 'close'):
        sid = str(action['id'])
        want = 'open' if kind == 'open' else 'closed'
        if sid in new['switches']:
            new['switches'][sid] = want
            return new
        cover = [k for k in new['switches'] if k.startswith(sid + '|')]
        if not cover:
            raise ValueError('unknown_switch')
        for key in cover:
            new['switches'][key] = want
        return new
    if kind in ('shed', 'ramp'):
        room = str(action['room'])
        cls = str(action.get('class', 'deferrable'))
        if room not in state['classes'] or cls not in state['classes'][room]:
            raise ValueError('unknown_load_class')
        new['classes'][room][cls] = kind == 'ramp'
        return new
    raise ValueError('unknown_action')


def check_action(topo, state, action, fault_segments=()):
    """Validate one action; record every hazard even when it is refused."""
    before = evaluate(topo, state, fault_segments)
    try:
        new = apply_action(state, action)
    except ValueError as exc:
        return state, {'action': action, 'rejected': str(exc)}, str(exc)
    after = evaluate(topo, new, fault_segments)
    amps, surges = instantaneous(topo, before, after)
    over = _overload(amps, after['limits'])
    # Back-feed: a faulted section gains a source it did not have before.
    # This is what catches a tie close made while the fault-side device is
    # still closed, even if the amp numbers alone would pass.
    backfeed = []
    for seg, feeders in after['fault_sources'].items():
        old = set(before['fault_sources'].get(seg, []))
        if set(feeders) - old:
            backfeed.append(seg)
    backfeed = sorted(backfeed)
    risks = []
    if backfeed:
        risks.append('backfeed_fault_section')
    if after['parallel'] and not before['parallel']:
        risks.append('make_before_break')
    if surges:
        risks.append('startup_surge')
    if over:
        risks.append('instantaneous_overload')
    log = {'action': action, 'before': before, 'after': after,
           'instantaneous_amps': amps, 'surges': surges,
           'overload': over, 'risks': risks,
           'surge_amp': round(sum(s['surge_amp'] for s in surges), 3)}
    if backfeed:
        return state, dict(log, rejected='backfeed_fault_section',
                           backfeed_sections=backfeed), 'backfeed_fault_section'
    if after['parallel'] and not before['parallel']:
        return state, dict(log, rejected='make_before_break'), 'make_before_break'
    if over:
        code = 'startup_surge' if surges else 'phase_overload'
        return state, dict(log, rejected=code), code
    return new, log, None


# ---------------------------------------------------------------------------
# global planning
# ---------------------------------------------------------------------------

def _action_spec(action):
    return {'kind': action['kind'], 'id': action.get('id'),
            'phase': action.get('phase'), 'room': action.get('room'),
            'class': action.get('class')}


def _label(action):
    if action['kind'] in ('open', 'close'):
        name = action['id'] if not action.get('phase') else f"{action['id']}|{action['phase']}"
        return f"{LABELS[action['kind']]} {name}"
    return f"{LABELS[action['kind']]} {action['room']} {LABELS.get(action['class'], action['class'])}"


def _actions(topo, state, fault_segments, sheddable):
    acts = []
    for b in topo['breakers']:
        if state['switches'].get(b['id'], b['initial']) != 'closed':
            continue
        if any(b['to'] == 'seg:' + seg or b['from'] == 'seg:' + seg
               for seg in fault_segments):
            acts.append({'kind': 'open', 'id': b['id'], 'phase': b['phases'][0]})
    for tie in topo['ties']:
        for p in tie['phases']:
            if state['switches'].get(tie_key(tie['id'], p), tie['initial']) == 'open':
                acts.append({'kind': 'close', 'id': tie['id'], 'phase': p})
    for room in sorted(state['classes']):
        load = topo['loads'].get(room)
        if not load:
            continue
        for cls in ('deferrable', 'critical'):
            if cls not in state['classes'][room]:
                continue
            if state['classes'][room][cls]:
                if cls == 'deferrable' and room in sheddable:
                    acts.append({'kind': 'shed', 'room': room, 'class': cls})
            else:
                acts.append({'kind': 'ramp', 'room': room, 'class': cls})
    return acts


def _fully_served(topo, state, ev, room):
    load = topo['loads'].get(room)
    if load is None:
        return True
    return all(ev['on'].get(room, {}).get(c, False) for c in load['classes'])


def _goal(topo, state, fault_segments, protected, ev):
    if ev['fault_live'] or ev['parallel']:
        return False
    if _overload(ev['currents'], ev['limits']):
        return False
    return all(_fully_served(topo, state, ev, room) for room in protected)


def _step_record(index, action, log, elapsed, outage, deferred):
    after = log['after']
    live_feeders = sorted(f for f, c in after['currents'].items()
                          if any(v > 0 for v in c.values()))
    return {'index': index, 'kind': action['kind'],
            'action_spec': _action_spec(action),
            'label': _label(action),
            'elapsed_minutes': round(elapsed, 3),
            'switch_states': dict(after['switch_states']),
            'feeders': live_feeders,
            'currents': {f: {p: round(v, 3) for p, v in c.items()}
                         for f, c in after['currents'].items()},
            'instantaneous_amps': {f: {p: round(v, 3) for p, v in c.items()}
                                   for f, c in log['instantaneous_amps'].items()},
            'limits': {f: {p: v for p, v in c.items()} for f, c in after['limits'].items()},
            'surges': log['surges'],
            'served': dict(after['served']),
            'essential_served': dict(after['essential_served']),
            'outage_minutes': {r: round(v, 3) for r, v in outage.items()},
            'deferred_minutes': {r: round(v, 3) for r, v in deferred.items()}}


def sub_topology(topo, rooms):
    """A copy carrying only ``rooms`` loads; used for per-room feasibility.

    This is how a shared-feeder trap becomes visible: each room looks feasible
    on its own copy, while the joint state overloads the same phase.
    """
    keep = set(rooms)
    return {'id': topo['id'], 'sources': topo['sources'],
            'breakers': topo['breakers'], 'ties': topo['ties'],
            'loads': {r: load for r, load in topo['loads'].items() if r in keep},
            'source': topo['source']}


def plan_sequence(topo, fault, protected, sheddable, fault_segments=None,
                  max_minutes=30.0, max_steps=10):
    """Dijkstra over switch/load states for one building-wide sequence.

    The search is global: every room sharing a backup feeder is evaluated in
    one state, so per-room feasible paths cannot be stitched into a same-phase
    overload.  Returns the feasible sequence, or the minimal causal reasons.
    """
    fault_segments = tuple(fault_segments if fault_segments is not None
                           else fault.get('segments', []))
    start = scenario_state(topo, fault)
    protected = tuple(sorted(set(protected)))
    sheddable = set(sheddable)
    start_ev = evaluate(topo, start, fault_segments)
    off_start = {room: (0.0 if not start_ev['essential_served'].get(room) else None)
                 for room in topo['loads']}
    outage = {room: 0.0 for room in topo['loads']}
    deferred_start = {room: (0.0 if not start_ev['served'].get(room) else None)
                      for room in topo['loads']}
    deferred = {room: 0.0 for room in topo['loads']}
    counter = itertools.count()
    heap = [(0.0, next(counter), 0, start,
             {'off_start': off_start, 'outage': outage,
              'deferred_start': deferred_start, 'deferred': deferred, 'log': []})]
    seen = set()
    failures = {}
    while heap:
        elapsed, _, depth, state, meta = heapq.heappop(heap)
        key = (tuple(sorted(state['switches'].items())),
               tuple((r, tuple(sorted(c.items()))) for r, c in sorted(state['classes'].items())))
        if key in seen:
            continue
        seen.add(key)
        ev = evaluate(topo, state, fault_segments)
        if _goal(topo, state, fault_segments, protected, ev):
            final_outage = {}
            final_deferred = {}
            for room, start_at in meta['off_start'].items():
                total = meta['outage'].get(room, 0.0)
                if start_at is not None:
                    total += elapsed - start_at
                final_outage[room] = round(total, 3)
            for room, start_at in meta['deferred_start'].items():
                total = meta['deferred'].get(room, 0.0)
                if start_at is not None:
                    total += elapsed - start_at
                final_deferred[room] = round(total, 3)
            return {'feasible': True, 'steps': meta['log'],
                    'outage_minutes': final_outage,
                    'deferred_minutes': final_deferred,
                    'final_state': state_body(state),
                    'final_switches': dict(state['switches']),
                    'final_currents': {f: {p: round(v, 3) for p, v in c.items()}
                                       for f, c in ev['currents'].items()},
                    'elapsed_minutes': round(elapsed, 3),
                    'protected': list(protected),
                    'fault_segments': list(fault_segments)}
        if depth >= max_steps or elapsed >= max_minutes:
            continue
        for action in _actions(topo, state, fault_segments, sheddable):
            new_state, log, error = check_action(topo, state, action, fault_segments)
            if error:
                failures[error] = failures.get(error, 0) + 1
                continue
            new_ev = log['after']
            new_off = dict(meta['off_start'])
            new_outage = dict(meta['outage'])
            new_deferred_start = dict(meta['deferred_start'])
            new_deferred = dict(meta['deferred'])
            for room in topo['loads']:
                served = bool(new_ev['essential_served'].get(room))
                if served:
                    if new_off.get(room) is not None:
                        new_outage[room] = new_outage.get(room, 0.0) + (elapsed - new_off[room])
                        new_off[room] = None
                elif new_off.get(room) is None:
                    new_off[room] = elapsed
                full = bool(new_ev['served'].get(room))
                if full:
                    if new_deferred_start.get(room) is not None:
                        new_deferred[room] = new_deferred.get(room, 0.0) + (elapsed - new_deferred_start[room])
                        new_deferred_start[room] = None
                elif new_deferred_start.get(room) is None:
                    new_deferred_start[room] = elapsed
            step = _step_record(len(meta['log']) + 1, action, log, elapsed,
                                new_outage, new_deferred)
            heapq.heappush(heap, (elapsed + ACTION_MINUTES.get(action['kind'], 0.2),
                                  next(counter), depth + 1, new_state,
                                  {'off_start': new_off, 'outage': new_outage,
                                   'deferred_start': new_deferred_start,
                                   'deferred': new_deferred,
                                   'log': meta['log'] + [step]}))
    reasons = set(failures) or {'no_feasible_path'}
    # Exploratory rejections inside the search (e.g. a tie close attempted
    # before the fault side was opened) are not causal conclusions.  Diagnose
    # the failure from per-room feasibility instead: if every protected room
    # can be served on its own but not together, the cause is the shared
    # backup feeder; if the shared feeder already carries load that the
    # frozen agreements do not allow shedding, name that directly.
    individual = {}
    if len(protected) > 1:
        for room in protected:
            alone = sub_topology(topo, [room])
            individual[room] = bool(plan_sequence(
                alone, fault, [room], sheddable & {room},
                fault_segments, max_minutes, max_steps)['feasible'])
    if len(protected) > 1 and individual and all(individual.values()):
        reasons = {'shared_backup_same_phase_overload'}
        start_ev = evaluate(topo, start, fault_segments)
        for room, load in topo['loads'].items():
            if room in sheddable or not start_ev['served'].get(room):
                continue
            if any(load['classes'].get(c, 0) > 0 for c in ('basic', 'critical')):
                reasons = {'non_sheddable_shared_feeder_load'}
                break
    elif len(protected) == 1:
        room = protected[0]
        if room not in topo['loads']:
            reasons = {'room_not_in_topology'}
        else:
            load = topo['loads'][room]
            if 'critical' in load['classes'] and room not in sheddable:
                # a critical load may never be switched off, so a sequence
                # that would need to do so is impossible by contract
                if 'startup_surge' in failures or 'phase_overload' in failures:
                    reasons = {'critical_load_not_sheddable'}
    return {'feasible': False, 'steps': [], 'outage_minutes': {},
            'deferred_minutes': {},
            'protected': list(protected), 'reasons': minimal_reasons(reasons),
            'failure_counts': failures, 'individual_feasibility': individual,
            'fault_segments': list(fault_segments)}


def minimal_reasons(codes):
    """Collapse causal codes to the smallest explanatory set."""
    result = set(codes)
    if 'critical_load_not_sheddable' in result:
        result.discard('phase_overload')
        result.discard('startup_surge')
    if 'non_sheddable_shared_feeder_load' in result:
        result.discard('phase_overload')
        result.discard('startup_surge')
        result.discard('shared_backup_same_phase_overload')
    if 'shared_backup_same_phase_overload' in result:
        result.discard('phase_overload')
        result.discard('startup_surge')
    if 'startup_surge' in result:
        result.discard('phase_overload')
    if 'backfeed_fault_section' in result:
        result.discard('no_feasible_path')
    return sorted(result) or ['no_feasible_path']


# ---------------------------------------------------------------------------
# service screening
# ---------------------------------------------------------------------------

def service_rule(contract, max_interruption_min=None):
    rule = CONTRACTS.get(contract)
    if rule is None:
        return None
    limit = rule['max_interruption_min']
    if max_interruption_min is not None:
        limit = min(limit, float(max_interruption_min))
    return {'contract': contract, 'max_interruption_min': limit,
            'brief_outage_ok': rule['brief_outage_ok']}


def screen_service(service, basis, outage_minutes, plan_feasible, reasons):
    """Judge one frozen promise against planned or executed evidence."""
    rule = service_rule(service.get('contract'), service.get('max_interruption_min'))
    base = {'room': service.get('room'), 'lesson': service.get('lesson'),
            'contract': service.get('contract'), 'basis': basis,
            'max_interruption_min': rule['max_interruption_min'] if rule else None}
    if rule is None:
        return dict(base, status='不能履约', reason='unknown_contract',
                    minimal_reasons=['unknown_contract'])
    if service.get('signed_after_fault'):
        return dict(base, status='不能履约',
                    reason='contract_not_frozen_before_fault',
                    minimal_reasons=['contract_not_frozen_before_fault'])
    if not plan_feasible:
        return dict(base, status='不能履约', reason=';'.join(reasons),
                    minimal_reasons=list(reasons))
    duration = float(outage_minutes.get(service.get('room'), 0.0))
    base['outage_minutes'] = round(duration, 3)
    if duration > rule['max_interruption_min'] + 1e-9:
        if rule['max_interruption_min'] == 0:
            return dict(base, status='不能履约', reason='zero_interruption_breached',
                        minimal_reasons=['zero_interruption_breached'],
                        recommendation='stop_exam_or_independent_ups_with_its_own_evidence')
        return dict(base, status='不能履约', reason='interruption_time_exceeded',
                    minimal_reasons=['interruption_time_exceeded'])
    return dict(base, status='可履约', reason='within_contract_limit',
                minimal_reasons=[])


# ---------------------------------------------------------------------------
# completed-transfer proof
# ---------------------------------------------------------------------------

def norm_step(item):
    """Normalise a plan spec or a receipt step to a comparable tuple."""
    kind = item.get('kind')
    if kind is None:
        for key in ('open', 'close', 'shed', 'ramp'):
            if key in item:
                kind = key
                break
    sid = item.get('id')
    room = item.get('room')
    if sid is None and kind in ('open', 'close'):
        sid = item.get(kind)
    if room is None and kind in ('shed', 'ramp'):
        room = item.get(kind)
    return (kind, None if sid is None else str(sid),
            None if item.get('phase') is None else str(item.get('phase')),
            None if room is None else str(room),
            None if item.get('class') is None else str(item.get('class')))


def _channels_for(topo, after, spec, before=None):
    """(feeder, phase) channels a step's measured currents belong to.

    For an open action the segment is de-energised afterwards, so the channel
    is resolved against the state *before* the step; for a close/ramp the
    state after the step is authoritative.
    """
    kind = spec['kind']
    if kind in ('shed', 'ramp'):
        load = topo['loads'].get(str(spec.get('room')))
        if not load:
            return []
        feeder = (after['reach'].get(('seg', load['segment'], load['phase']))
                  or (before or {}).get('reach', {}).get(('seg', load['segment'], load['phase'])))
        return [(feeder, load['phase'])] if feeder else []
    if kind in ('open', 'close'):
        sid = str(spec.get('id'))
        views = [before, after] if before is not None else [after]
        for b in topo['breakers']:
            if b['id'] == sid:
                for view in views:
                    for name in (b['from'], b['to']):
                        if name.startswith('seg:'):
                            feeder = view['reach'].get(('seg', name[4:], b['phases'][0]))
                            if feeder:
                                return [(feeder, b['phases'][0])]
        for tie in topo['ties']:
            if tie['id'] == sid:
                p = spec.get('phase') or tie['phases'][0]
                for view in views:
                    for name in (tie['to'], tie['from']):
                        if name.startswith('seg:'):
                            feeder = view['reach'].get(('seg', name[4:], p))
                            if feeder:
                                return [(feeder, p)]
    return []


def _observed_by_feeder(step, observed, channels=()):
    """Normalise a receipt's observed amps to {feeder: {phase: amps}}.

    A phase map (``{'A': 8}``) is bound to the step's own electrical
    channels; an explicit ``{feeder: {phase: amps}}`` map is used as-is.
    """
    if not observed:
        return {}
    scalar = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                 for v in observed.values())
    if scalar and all(k in PHASES for k in observed):
        if channels:
            return {feeder: {k: float(v) for k, v in observed.items()}
                    for feeder, _ in channels if feeder}
        return {'?': {k: float(v) for k, v in observed.items()}}
    if all(isinstance(v, dict) for v in observed.values()):
        return {str(f): {str(p): float(v) for p, v in vals.items()}
                for f, vals in observed.items()}
    raise ValueError('invalid_observed_amps')


def _over_observed(observed_by_feeder, limits):
    out = []
    for feeder, vals in observed_by_feeder.items():
        for p, amps in vals.items():
            if p not in PHASES or feeder not in limits:
                out.append({'feeder': feeder, 'phase': p, 'amps': amps,
                            'reason': 'unknown_channel'})
                continue
            if amps > limits[feeder][p] + 1e-9:
                out.append({'feeder': feeder, 'phase': p, 'amps': round(amps, 3),
                            'limit': limits[feeder][p]})
    return out


def _minutes_between(start, end):
    from datetime import datetime
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60.0, 3)
    except (TypeError, ValueError):
        return None


def proof_check(topo, fault, plan, execution, corrections=None,
                current_fingerprint=None, current_fault=None,
                current_topology=None):
    """Check an execution receipt and issue or refuse a completion proof.

    The proof is never derived from the plan.  It is derived from device
    receipts, monotonic timestamps and measured currents; a missing receipt,
    a skipped step, an over-limit reading or a late correction that alters a
    reading all refuse the proof.  ``corrections`` maps ``(execution_id,
    step_index)`` to corrected ``observed_amps``.
    """
    corrections = corrections or {}
    if plan.get('feasible') is False:
        return {'status': '拒绝', 'reason': 'plan_not_feasible',
                'reasons': ['plan_not_feasible'], 'executed_outage_minutes': {}}
    if not execution:
        return {'status': '未完成', 'reason': 'execution_missing',
                'reasons': ['execution_missing'], 'executed_outage_minutes': {}}
    if execution.get('plan_id') != plan.get('plan_id'):
        return {'status': '拒绝', 'reason': 'execution_plan_mismatch',
                'reasons': ['execution_plan_mismatch'], 'executed_outage_minutes': {}}
    if (execution.get('topology_id') not in (None, plan.get('topology_id'))
            or execution.get('fault_id') not in (None, plan.get('fault_id'))):
        return {'status': '拒绝', 'reason': 'evidence_mismatch',
                'reasons': ['evidence_mismatch'], 'executed_outage_minutes': {}}
    if current_topology is not None and plan.get('topology') != current_topology:
        return {'status': '拒绝', 'reason': 'topology_changed_since_plan',
                'reasons': ['topology_changed_since_plan'],
                'executed_outage_minutes': {}}
    if current_fault is not None and plan.get('fault') != current_fault:
        return {'status': '拒绝', 'reason': 'fault_scope_changed_since_plan',
                'reasons': ['fault_scope_changed_since_plan'],
                'executed_outage_minutes': {}}
    if current_fingerprint is not None and plan.get('schedule_fingerprint') not in (None, current_fingerprint):
        return {'status': '拒绝', 'reason': 'schedule_changed_since_plan',
                'reasons': ['schedule_changed_since_plan'],
                'executed_outage_minutes': {}}
    receipt = execution.get('steps', [])
    if not receipt:
        return {'status': '未完成', 'reason': 'receipt_missing',
                'reasons': ['receipt_missing'], 'executed_outage_minutes': {}}
    state = plan.get('scenario_state') or state_body(scenario_state(topo, fault))
    state = {'switches': dict(state['switches']),
             'classes': {r: dict(c) for r, c in state['classes'].items()}}
    fault_segments = tuple(plan.get('fault_segments', fault.get('segments', [])))
    position = 0
    problems = []
    executed_log = []
    served_before = evaluate(topo, state, fault_segments)['essential_served']
    off_at = {room: (execution.get('fault_at') or execution.get('started_at'))
              for room, served in served_before.items() if not served}
    restored_at = {}
    previous_at = None
    last_at = None
    for index, step in enumerate(plan.get('steps', []), 1):
        spec = step.get('action_spec') or {'kind': step['kind']}
        match = None
        for pos in range(position, len(receipt)):
            if norm_step(receipt[pos]) == norm_step(spec):
                match = pos
                break
        if match is None:
            problems.append(f'step_not_executed:{index}')
            break
        if match > position:
            problems.append(f'unexpected_actions:{position}:{match}')
            break
        position = match + 1
        item = receipt[match]
        at = item.get('at')
        if spec['kind'] in ('open', 'close') and not item.get('receipt'):
            problems.append(f'device_receipt_missing:{index}')
        if item.get('ok') is False:
            problems.append(f'executed_failure:{index}')
        if at:
            if previous_at is not None and at < previous_at:
                problems.append(f'non_monotonic_time:{index}')
            previous_at = at
            last_at = at
        new_state, log, error = check_action(topo, state, spec, fault_segments)
        if error:
            problems.append(f'unsafe_execution:{index}:{error}')
            break
        state = new_state
        observed = item.get('observed_amps')
        corrected_flag = False
        corrected = corrections.get((execution.get('id'), index))
        if corrected is not None:
            observed = corrected
            corrected_flag = True
        if spec['kind'] in ('close', 'ramp') and not observed:
            problems.append(f'meter_evidence_missing:{index}')
        # Modelled instantaneous current (steady + start-up surge) is checked
        # as-is: a measured steady reading must not erase the surge that the
        # topology says occurs at the moment of energisation.  The observed
        # reading is checked separately against the same limits.
        amps = {f: dict(v) for f, v in log['instantaneous_amps'].items()}
        channels = _channels_for(topo, log['after'], spec, log['before'])
        observed_by_feeder = _observed_by_feeder(item, observed, channels)
        over = _overload(amps, log['after']['limits'])
        over += _over_observed(observed_by_feeder, log['after']['limits'])
        if over:
            suffix = ':late_correction' if corrected_flag else ''
            problems.append(f"over_limit:{index}{suffix}:"
                            + ','.join(f"{o['feeder']}{o['phase']}={o['amps']}"
                                       for o in over))
        after = log['after']
        for room, served in after['essential_served'].items():
            if served and room in off_at and room not in restored_at:
                restored_at[room] = at
        executed_log.append({'index': index, 'kind': spec['kind'],
                             'label': step.get('label'),
                             'action': _action_spec(spec),
                             'switch_states': dict(state['switches']),
                             'currents': {f: {p: round(v, 3) for p, v in c.items()}
                                          for f, c in after['currents'].items()},
                             'instantaneous_amps': {f: {p: round(v, 3) for p, v in c.items()}
                                                    for f, c in amps.items()},
                             'surges': log.get('surges', []),
                             'at': at,
                             'observed_amps': observed,
                             'late_correction': corrected_flag})
    if position < len(receipt):
        problems.append('execution_diverged')
    if not execution.get('meter_evidence'):
        problems.append('meter_evidence_missing')
    executed_outage = {}
    for room, load in topo['loads'].items():
        if room not in off_at:
            executed_outage[room] = 0.0
            continue
        minutes = _minutes_between(off_at.get(room), restored_at.get(room))
        executed_outage[room] = minutes
        if minutes is None and room in restored_at:
            problems.append(f'outage_evidence_incomplete:{room}')
    reasons = problems or ['receipt_and_currents_verified']
    return {'status': '已证明' if not problems else '拒绝',
            'reasons': problems,
            'reason': reasons[0] if problems else 'receipt_and_currents_verified',
            'executed_steps': executed_log,
            'executed_outage_minutes': executed_outage,
            'plan_outage_minutes': plan.get('outage_minutes', {}),
            'last_evidence_at': last_at,
            'plan_id': plan.get('plan_id'),
            'execution_id': execution.get('id'),
            'schedule_fingerprint': plan.get('schedule_fingerprint')}
