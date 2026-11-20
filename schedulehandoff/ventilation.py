"""Bounded discrete mass-balance model for classroom ventilation evidence.

All quantities are volume-normalized (体积已折算). Between two consecutive
readings of one room:

    c_end = c_start + S - k*(c_start - c_out)
            + sum(incoming g*(c_neighbour - c_start))
            - sum(outgoing g*(c_start - c_out))

S     source term (ppm), bounded by the segment occupancy range and the
      activity rate range shared by every room with the same activity
k     fresh-air exchange ratio (换气比例), one value per room and window
c_out outdoor background for the segment
g     shared return-air flow of a link; the SAME g appears in both rooms'
      balances, so a flow counted as leaving A must arrive at B

Sensor zero offset and response lag are shared across a sensor's whole
continuous series; activity rates are shared across rooms with the same
activity. The solver returns the full provable k range per room plus
complete witness explanations; it never collapses evidence to a single
best fit. Sensor parameters are evaluated at the calibration bound
combinations (the corrected series is affine in them, so extrema of this
model family are attained at the bounds).
"""
from fractions import Fraction
from itertools import product

K_CAP = Fraction(50)


def F(value):
    return value if isinstance(value, Fraction) else Fraction(str(value))


# ---------- exact rational simplex (two-phase, Bland's rule) ----------

def _pivot(tab, basis, prow, pcol):
    piv = tab[prow][pcol]
    tab[prow] = [v / piv for v in tab[prow]]
    for i in range(len(tab)):
        if i != prow and tab[i][pcol]:
            f = tab[i][pcol]
            tab[i] = [a - f * b for a, b in zip(tab[i], tab[prow])]
    basis[prow] = pcol


def _optimize(tab, basis, ncols):
    # objective row stores NEGATED reduced costs: z = tab[-1][-1] - sum tab[-1][j] x_j,
    # so pivoting stays uniform with the constraint rows
    while True:
        obj = tab[-1]
        enter = next((j for j in range(ncols) if obj[j] < 0), None)
        if enter is None:
            return 'optimal'
        leave, best = None, None
        for i in range(len(basis)):
            a = tab[i][enter]
            if a > 0:
                key = (tab[i][-1] / a, basis[i])
                if best is None or key < best:
                    best, leave = key, i
        if leave is None:
            return 'unbounded'
        _pivot(tab, basis, leave, enter)


def _objective_row(tab, basis, c):
    ncols = len(tab[0]) - 1 if tab else len(c)
    row = [Fraction(0)] * (ncols + 1)
    for j in range(ncols):
        row[j] = -c[j]
    for i, bv in enumerate(basis):
        if c[bv]:
            for j in range(ncols):
                row[j] += c[bv] * tab[i][j]
            row[-1] += c[bv] * tab[i][-1]
    return row


def simplex_max(rows, nvars, objective):
    """Maximize objective.x subject to lo <= a.x <= hi per row, x >= 0.

    rows: iterable of (coeffs, lo, hi) with Fractions; lo/hi may be None.
    Returns ('optimal', value, assignment) | ('infeasible', None, None)
    | ('unbounded', None, None). Exact rational arithmetic.
    """
    A, b = [], []
    for coeffs, lo, hi in rows:
        if hi is not None:
            A.append(list(coeffs))
            b.append(hi)
        if lo is not None:
            A.append([-v for v in coeffs])
            b.append(-lo)
    m = len(A)
    ncols = nvars + m
    artificial = {}
    for i in range(m):
        if b[i] < 0:
            artificial[i] = ncols
            ncols += 1
    tab, basis = [], []
    for i in range(m):
        row = [Fraction(0)] * (ncols + 1)
        ai, bi = A[i], b[i]
        if i in artificial:
            for j in range(nvars):
                row[j] = -ai[j]
            row[nvars + i] = Fraction(-1)
            basis.append(artificial[i])
            row[-1] = -bi
        else:
            for j in range(nvars):
                row[j] = ai[j]
            basis.append(nvars + i)
            row[-1] = bi
        tab.append(row)
    if artificial:
        c = [Fraction(0)] * ncols
        for col in artificial.values():
            c[col] = Fraction(-1)
        tab.append(_objective_row(tab, basis, c))
        if _optimize(tab, basis, ncols) != 'optimal':
            raise AssertionError('phase-1 problem is bounded by construction')
        if tab[-1][-1] < 0:
            return ('infeasible', None, None)
        tab.pop()
        art = set(artificial.values())
        keep = []
        for i in range(len(basis)):
            if basis[i] in art:
                j = next((j for j in range(ncols) if j not in art and tab[i][j]), None)
                if j is None:
                    continue
                _pivot(tab, basis, i, j)
            keep.append(i)
        tab = [tab[i] for i in keep]
        basis = [basis[i] for i in keep]
        remap = {old: new for new, old in enumerate(j for j in range(ncols) if j not in art)}
        tab = [[row[j] for j in range(ncols) if j not in art] + [row[-1]] for row in tab]
        basis = [remap[bv] for bv in basis]
        ncols -= len(art)
    c = list(objective) + [Fraction(0)] * (ncols - nvars)
    tab.append(_objective_row(tab, basis, c))
    state = _optimize(tab, basis, ncols)
    if state != 'optimal':
        return (state, None, None)
    x = [Fraction(0)] * nvars
    for i, bv in enumerate(basis):
        if bv < nvars:
            x[bv] = tab[i][-1]
    return ('optimal', tab[-1][-1], x)


# ---------- evidence helpers ----------

def correct_series(readings, zero, lag):
    """Apply one shared zero offset and first-order response lag to a raw
    sensor series: m_t = (1-lag)*c_t + lag*m_{t-1}. The same (zero, lag)
    pair is used for every reading of the series."""
    out = []
    prev = None
    for at, raw in readings:
        raw = F(raw)
        c = raw - zero if prev is None else (raw - lag * prev) / (1 - lag) - zero
        out.append((at, c))
        prev = raw
    return out


def occupancy_range(point_events, totals, enrolled, t0, t1):
    """Possible occupancy range during [t0, t1] from door evidence.

    Point events (timestamped entries/exits or absolute counts) move an
    interval cursor; a period total only lowers the upper bound of the
    periods it covers — totals never fabricate per-minute occupancy.
    """
    lo, hi = 0, int(enrolled)
    pieces, start = [], None
    for ev in sorted(point_events, key=lambda e: e['at']):
        pieces.append((start, ev['at'], lo, hi))
        if 'present' in ev:
            lo = hi = int(ev['present'])
        else:
            delta = int(ev.get('entered', 0)) - int(ev.get('exited', 0))
            lo, hi = max(0, lo + delta), hi + delta
        start = ev['at']
    pieces.append((start, None, lo, hi))
    sel_lo = sel_hi = None
    for a, b, plo, phi in pieces:
        if (a is None or a < t1) and (b is None or t0 < b):
            sel_lo = plo if sel_lo is None else min(sel_lo, plo)
            sel_hi = phi if sel_hi is None else max(sel_hi, phi)
    if sel_lo is None:
        sel_lo, sel_hi = 0, int(enrolled)
    for tot in totals:
        if tot['start'] < t1 and t0 < tot['end']:
            sel_hi = min(sel_hi, int(tot['total']))
    return sel_lo, sel_hi


def _value_at(series, t):
    """Corrected value of a series at-or-before t, else its first value."""
    before = [c for at, c in series if at <= t]
    if before:
        return before[-1]
    return series[0][1] if series else None


# ---------- joint bounded solve over one assessment window ----------

def _solve_combo(room_list, segments, links, activities, fan, k_cap):
    """One LP for one sensor-parameter combination. Returns per-room
    (k_min, witness_min, k_max, witness_max) or None if inconsistent."""
    idx = {}
    for r in room_list:
        idx[('k', r)] = len(idx)
    for link in links:
        idx[('g', link['id'])] = len(idx)
    for act in activities:
        idx[('r', act)] = len(idx)
    for i in range(len(segments)):
        idx[('S', i)] = len(idx)
    nvars = len(idx)
    lo, hi = [Fraction(0)] * nvars, [Fraction(0)] * nvars
    for r in room_list:
        flo, fhi = fan.get(r, (Fraction(0), k_cap))
        lo[idx[('k', r)]], hi[idx[('k', r)]] = flo, min(fhi, k_cap)
    for link in links:
        lo[idx[('g', link['id'])]], hi[idx[('g', link['id'])]] = link['lo'], link['hi']
    for act, (rlo, rhi) in activities.items():
        lo[idx[('r', act)]], hi[idx[('r', act)]] = rlo, rhi
    for i, seg in enumerate(segments):
        hi[idx[('S', i)]] = seg['o_hi'] * activities[seg['act']][1]

    rows = []

    def shifted(coeffs, const):
        # variables are x = value - lo; move the lower bounds to the rhs
        adj = const
        for j, a in coeffs.items():
            adj -= a * lo[j]
        return [coeffs.get(j, Fraction(0)) for j in range(nvars)], adj

    for i, seg in enumerate(segments):
        coeffs = {idx[('S', i)]: Fraction(1), idx[('k', seg['room'])]: seg['cout'] - seg['c0']}
        for lid, coef in seg['in_terms'] + seg['out_terms']:
            j = idx[('g', lid)]
            coeffs[j] = coeffs.get(j, Fraction(0)) + coef
        a, rhs = shifted(coeffs, seg['c1'] - seg['c0'])
        rows.append((a, rhs, rhs))
        r_var = idx[('r', seg['act'])]
        s_var = idx[('S', i)]
        a, bound = shifted({s_var: Fraction(1), r_var: -seg['o_lo']}, Fraction(0))
        rows.append((a, bound, None))
        a, bound = shifted({s_var: Fraction(1), r_var: -seg['o_hi']}, Fraction(0))
        rows.append((a, None, bound))
    for j in range(nvars):
        a = [Fraction(0)] * nvars
        a[j] = Fraction(1)
        rows.append((a, None, hi[j] - lo[j]))

    def witness(x):
        values = [x[j] + lo[j] for j in range(nvars)]
        w = {'k': {}, 'g': {}, 'r': {}, 'segments': []}
        for r in room_list:
            w['k'][r] = values[idx[('k', r)]]
        for link in links:
            w['g'][link['id']] = values[idx[('g', link['id'])]]
        for act in activities:
            w['r'][act] = values[idx[('r', act)]]
        for i, seg in enumerate(segments):
            s = values[idx[('S', i)]]
            rate = w['r'][seg['act']]
            occ = s / rate if rate > 0 else seg['o_lo']
            w['segments'].append({'room': seg['room'], 'start': seg['start'], 'end': seg['end'],
                                  'source': s, 'occupancy': occ})
        return w

    out = {}
    for r in room_list:
        j = idx[('k', r)]
        for sign, slot in ((-1, 'low'), (1, 'high')):
            objective = [Fraction(0)] * nvars
            objective[j] = Fraction(sign)
            state, value, x = simplex_max(rows, nvars, objective)
            if state != 'optimal':
                return None
            out.setdefault(r, {})[slot] = (x[j] + lo[j], witness(x))
    return out


def assess_window(window, rooms, links, activities, samples, cals, doors, fan,
                  enrolled, outdoor, k_cap=K_CAP):
    """Solve the provable ventilation range for every involved room.

    window: (start, end) ISO strings. rooms: {room: {activity, sensor,
    volume_m3}}. links: [{id, from, to, lo, hi}]. activities: {id: (lo, hi)}.
    samples: {room: [(at, ppm)]} including one predecessor before the window
    when available. cals: {sensor: {zero: (lo, hi), lag: (lo, hi)}}.
    doors: {room: {'points': [...], 'totals': [...]}}. fan: {room: (lo, hi)}.
    enrolled: {room: int}. outdoor: [(start, end, ppm)].
    """
    ws, we = window
    k_cap = F(k_cap)
    room_list = sorted(rooms)
    acts = {a: (F(lo), F(hi)) for a, (lo, hi) in activities.items()}
    link_list = [{'id': l['id'], 'from': l['from'], 'to': l['to'],
                  'lo': F(l['lo']), 'hi': F(l['hi'])} for l in links]
    fan_bounds = {r: (F(a), F(b)) for r, (a, b) in (fan or {}).items()}
    notes = []

    def outdoor_at(t0, t1):
        mid_options = [F(ppm) for start, end, ppm in outdoor if start < t1 and t0 < end]
        return mid_options[0] if mid_options else None

    # segment skeletons (raw evidence; independent of the sensor combo)
    skeleton = {}
    for room in room_list:
        inside = [(at, F(ppm)) for at, ppm in samples.get(room, []) if ws <= at <= we]
        segs = []
        for (t0, m0), (t1, m1) in zip(inside, inside[1:]):
            cout = outdoor_at(t0, t1)
            if cout is None:
                return {'status': 'evidence_missing', 'detail': 'outdoor_missing',
                        'segments': [], 'notes': notes}
            ev = doors.get(room, {'points': [], 'totals': []})
            o_lo, o_hi = occupancy_range(ev['points'], ev['totals'],
                                         enrolled.get(room, 0), t0, t1)
            segs.append({'room': room, 'start': t0, 'end': t1, 'raw': (m0, m1),
                         'cout': cout, 'o_lo': o_lo, 'o_hi': o_hi,
                         'act': rooms[room]['activity']})
        skeleton[room] = segs
    if not any(skeleton.values()):
        notes.append('insufficient_readings')

    report_segments = [
        {'room': s['room'], 'start': s['start'], 'end': s['end'],
         'raw_start': float(s['raw'][0]), 'raw_end': float(s['raw'][1]),
         'occupancy': [s['o_lo'], s['o_hi']],
         'source': [float(s['o_lo'] * acts[s['act']][0]), float(s['o_hi'] * acts[s['act']][1])],
         'outdoor': float(s['cout'])}
        for room in room_list for s in skeleton[room]]

    sensors = {room: rooms[room]['sensor'] for room in room_list}
    vertices = []
    for room in room_list:
        cal = cals.get(sensors[room], {'zero': (0, 0), 'lag': (0, 0)})
        vertices.append([(F(z), F(l)) for z in cal['zero'] for l in cal['lag']])

    best = {}
    feasible = False
    unresolved_links = set()
    for combo in product(*vertices):
        choice = {sensors[room]: combo[i] for i, room in enumerate(room_list)}
        series = {room: correct_series(samples.get(room, []), *choice[sensors[room]])
                  for room in room_list}
        segments = []
        dropped = set()
        for room in room_list:
            for seg in skeleton[room]:
                s = dict(seg)
                s['c0'] = _value_at(series[room], seg['start'])
                s['c1'] = _value_at(series[room], seg['end'])
                s['in_terms'], s['out_terms'] = [], []
                for link in link_list:
                    if link['to'] == room:
                        neighbour = _value_at(series.get(link['from'], []), seg['start'])
                        if neighbour is None:
                            dropped.add(link['id'])
                            continue
                        s['in_terms'].append((link['id'], neighbour - s['c0']))
                    elif link['from'] == room:
                        if not series.get(link['to']):
                            dropped.add(link['id'])
                            continue
                        s['out_terms'].append((link['id'], s['cout'] - s['c0']))
                segments.append(s)
        unresolved_links |= dropped
        active_links = [l for l in link_list if l['id'] not in dropped]
        solved = _solve_combo(room_list, segments, active_links, acts, fan_bounds, k_cap)
        if solved is None:
            continue
        feasible = True
        for room in room_list:
            k_lo, w_lo = solved[room]['low']
            k_hi, w_hi = solved[room]['high']
            for w in (w_lo, w_hi):
                w['sensor'] = {sid: {'zero': z, 'lag': l} for sid, (z, l) in choice.items()}
            entry = best.setdefault(room, {'low': (k_lo, w_lo), 'high': (k_hi, w_hi)})
            if k_lo < entry['low'][0]:
                entry['low'] = (k_lo, w_lo)
            if k_hi > entry['high'][0]:
                entry['high'] = (k_hi, w_hi)
    for lid in sorted(unresolved_links):
        notes.append(f'link_unresolved:{lid}')
    if not feasible:
        return {'status': 'infeasible', 'segments': report_segments, 'notes': notes}
    return {'status': 'ok',
            'k': {room: (best[room]['low'][0], best[room]['high'][0]) for room in room_list},
            'witnesses': {room: {'low': best[room]['low'][1], 'high': best[room]['high'][1]}
                          for room in room_list},
            'segments': report_segments, 'notes': notes,
            'at_cap': {room: best[room]['high'][0] >= k_cap for room in room_list}}
