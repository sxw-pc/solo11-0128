"""Custody-chain reasoning for classroom equipment.

Pure, I/O-free logic: given one loan, the signed/unsigned handoffs that touch
its serials and the station return scans, enumerate *every* custody chain that
is compatible with the signed evidence, station records and the allowed clock
skew. Same-model substitutes never match another serial, duplicate scans are a
single event, and a handoff without a signature cannot move custody.

The engine never picks a chain by row order: when several chains remain
possible and they end with different holders, liability stays 待核 and the
minimum extra signature/station scan that would separate them is reported.
"""
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations

MAX_CHAINS = 500
DEFAULT_SKEW_SECONDS = 300

PERSONAL = 'personal_custody'      # 个人保管协议
TEAM = 'team_shared'               # 团队共用协议


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _body(row):
    return row['body'] if isinstance(row['body'], dict) else json.loads(row['body'])


@dataclass(frozen=True)
class Event:
    kind: str                 # loan | handoff | return
    ref: str                  # source row id
    serial: str
    time: object = None       # datetime or None
    station: str = None
    actor: str = None         # borrower / receiver / returner
    giver: str = None         # handoff giver only
    signed: bool = True
    scope: str = 'item'       # item | kit
    duplicate: bool = False

    def as_dict(self):
        return {'kind': self.kind, 'ref': self.ref, 'serial': self.serial,
                'time': self.time.isoformat() if self.time else None,
                'station': self.station, 'actor': self.actor, 'giver': self.giver,
                'signed': self.signed, 'scope': self.scope, 'duplicate': self.duplicate}


def _within_skew(a, b, skew):
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= skew


def build_events(loan, handoff_rows, scan_rows):
    """Turn stored rows into serial-keyed events plus excluded evidence.

    Duplicate scans (same serial/station/time/kind) collapse to one event;
    unsigned handoffs stay out of the chain but are kept as evidence so the
    report can name the signature that is missing.
    """
    body = _body(loan)
    loan_id = str(body['id'])
    events = {s: [] for s in body['serials']}
    excluded = {s: [] for s in body['serials']}
    for serial in body['serials']:
        events[serial].append(Event('loan', f'loan:{loan_id}', serial,
                                    parse_time(body.get('time')), body.get('station'),
                                    actor=body['borrower'], scope='kit'))
    for row in handoff_rows:
        rb = _body(row)
        if str(rb.get('loan')) != loan_id:
            continue
        for serial in rb.get('serials', []):
            if serial not in events:
                continue
            ev = Event('handoff', str(rb['id']), serial, parse_time(rb.get('time')),
                       rb.get('station'), actor=rb.get('receiver'), giver=rb.get('giver'),
                       signed=bool(rb.get('signed', False)),
                       scope=rb.get('scope', 'item'))
            if not ev.signed:
                excluded[serial].append({'ref': ev.ref, 'kind': 'unsigned_handoff',
                                         'station': ev.station,
                                         'time': ev.time.isoformat() if ev.time else None,
                                         'giver': ev.giver, 'receiver': ev.actor,
                                         'why': 'signature_missing'})
            else:
                events[serial].append(ev)
    seen = set()
    for row in scan_rows:
        rb = _body(row)
        if rb.get('kind', 'return') != 'return':
            continue
        for serial in rb.get('serials', []):
            if serial not in events:
                continue
            dedup_key = (serial, rb.get('station'), rb.get('time'), 'return')
            ev = Event('return', str(rb['id']), serial, parse_time(rb.get('time')),
                       rb.get('station'), actor=rb.get('returner'))
            if dedup_key in seen:
                excluded[serial].append({'ref': ev.ref, 'kind': 'duplicate_scan',
                                         'station': ev.station,
                                         'time': ev.time.isoformat() if ev.time else None,
                                         'why': 'duplicate_scan_counts_once'})
                continue
            seen.add(dedup_key)
            events[serial].append(ev)
    return events, excluded


REJECT = object()  # sentinel: event contradicts the current holder state


def _apply(ev, holder):
    """Return post-event holder, None for a successful return, or REJECT."""
    if ev.kind == 'loan':
        return ev.actor if holder is None else REJECT
    if ev.kind == 'handoff':
        if holder is None or not ev.actor:
            return REJECT
        if ev.giver is not None and holder != ev.giver:
            return REJECT
        return ev.actor
    # return: the person handing it back must currently hold it; an unstaffed
    # station scan does not name a returner and simply closes the custody.
    if holder is None:
        return REJECT
    if ev.actor is not None and holder != ev.actor:
        return REJECT
    return None


def enumerate_chains(events, skew_seconds=DEFAULT_SKEW_SECONDS):
    """All feasible custody chains as linear extensions under clock skew.

    The loan is always the root. Two timestamped events whose gap is within
    the skew may appear in either order; every ordering is actually walked,
    so the result cannot depend on file row order.

    Each path applies every event compatible with its current holder state and
    consumes the rest; a return scan that never applies on a path kills that
    path (a scan cannot simply be ignored). Ambiguous final holders arise from
    unsigned handoffs — they never enter a chain, so every signed ordering
    stays feasible. A signed handoff whose giver can never hold the item is a
    hard contradiction reported by :func:`unapplied_signed_events`.
    """
    ordered = sorted(events, key=lambda e: (e.time is None, e.time or datetime.min, e.ref))
    n = len(ordered)
    pre = [set() for _ in range(n)]
    loan_idx = next((i for i, e in enumerate(ordered) if e.kind == 'loan'), 0)
    for j in range(n):
        if j != loan_idx:
            pre[j].add(loan_idx)
        for i in range(n):
            a, b = ordered[i], ordered[j]
            if a.time is not None and b.time is not None and a.time < b.time \
                    and not _within_skew(a.time, b.time, skew_seconds):
                pre[j].add(i)
    return_refs = {e.ref for e in ordered if e.kind == 'return'}
    chains, seen_keys, truncated = [], set(), False

    def walk(remaining, used_mask, holder, edges, unapplied_returns):
        nonlocal truncated
        if not remaining:
            if unapplied_returns:
                return
            key = tuple((e['ref'], e['to_holder']) for e in edges)
            if key in seen_keys:
                return
            seen_keys.add(key)
            if len(chains) >= MAX_CHAINS:
                truncated = True
                return
            chains.append({'edges': list(edges), 'final_holder': holder})
            return
        for pos in list(range(len(remaining))):
            idx = remaining[pos]
            if not pre[idx].issubset({i for i in range(n) if used_mask & (1 << i)}):
                continue
            ev = ordered[idx]
            can_apply = False
            if ev.kind == 'loan':
                can_apply = holder is None
                next_holder = ev.actor if can_apply else REJECT
            elif holder is not None:
                next_holder = _apply(ev, holder)
                can_apply = next_holder is not REJECT
            else:
                next_holder = REJECT
            remaining.pop(pos)
            if can_apply:
                edges.append({'ref': ev.ref, 'kind': ev.kind, 'scope': ev.scope,
                              'time': ev.time.isoformat() if ev.time else None, 'station': ev.station,
                              'from_holder': holder, 'to_holder': next_holder,
                              'actor': ev.actor, 'giver': ev.giver, 'signed': ev.signed})
                walk(remaining, used_mask | (1 << idx), next_holder, edges,
                     unapplied_returns - {ev.ref})
                edges.pop()
            else:
                # the event contradicts this ordering; another root within the
                # skew window may make it apply, so consume and continue
                walk(remaining, used_mask | (1 << idx), holder, edges,
                     unapplied_returns | ({ev.ref} if ev.kind == 'return' else set()))
            remaining.insert(pos, idx)

    walk(list(range(n)), 0, None, [], set(return_refs))
    return chains, truncated


def holder_ranges(events, chains, skew_seconds=DEFAULT_SKEW_SECONDS):
    """Union, over all feasible chains, of compatible holders per time window.

    The timeline is split at every event timestamp. A segment's holders are
    the distinct states the feasible chains can be in during that segment;
    more than one holder (or a segment no wider than the skew) is marked
    ambiguous. The first segment holds the post-loan borrower and the last
    holds the possible final holders.
    """
    times = sorted({e.time for e in events if e.time is not None})
    if not times:
        holders = sorted({c['final_holder'] for c in chains if c['final_holder']})
        return [{'start': None, 'end': None, 'holders': holders,
                 'ambiguous': len(holders) > 1}]

    def state_after(chain, t):
        holder = None
        for edge in chain['edges']:
            et = parse_time(edge['time'])
            if et is not None and et <= t:
                holder = edge['to_holder']
        return holder

    bounds = [None] + times + [None]
    ranges = []
    for start, end in zip(bounds, bounds[1:]):
        holders = set()
        for chain in chains:
            if start is None:
                holder = chain['edges'][0]['to_holder'] if chain['edges'] else None
            else:
                holder = state_after(chain, start)
            if holder:
                holders.add(holder)
        width = (end - start).total_seconds() if start is not None and end is not None else None
        ambiguous = len(holders) > 1 or (width is not None and width <= skew_seconds)
        ranges.append({'start': start.isoformat() if start else None,
                       'end': end.isoformat() if end else None,
                       'holders': sorted(holders), 'ambiguous': ambiguous})
    return ranges


def minimum_evidence(events, chains, excluded, skew_seconds=DEFAULT_SKEW_SECONDS):
    """Smallest signature/station-scan set that forces a unique final holder."""
    if len({c['final_holder'] for c in chains}) <= 1:
        return []
    candidates = []
    for item in excluded:
        if item['kind'] == 'unsigned_handoff':
            ref, receiver = item['ref'], item['receiver']

            def pred(c, ref=ref, receiver=receiver):
                return any(e['ref'] == ref and e['to_holder'] == receiver for e in c['edges'])

            candidates.append({'kind': 'signature', 'ref': ref, 'station': item.get('station'),
                               'time': item.get('time'), 'giver': item.get('giver'),
                               'receiver': receiver, 'predicate': pred,
                               'need': f"签收单 {ref}（{item.get('giver')}→{receiver}）"})
    timed = sorted((e for e in events if e.time is not None), key=lambda e: e.time)
    # only adjacent events whose gap fits inside the clock-skew window are
    # genuinely order-ambiguous; a scan in that window naming one holder is
    # the cheapest evidence that can separate the chains
    for a, b in zip(timed, timed[1:]):
        gap = (b.time - a.time).total_seconds()
        if gap > skew_seconds:
            continue
        window = (a.time.isoformat(), b.time.isoformat())
        names = {x for x in (a.actor, b.actor, a.giver, b.giver) if x}
        for holder in sorted(names):
            def pred(c, window=window, holder=holder):
                lo, hi = parse_time(window[0]), parse_time(window[1])
                return any((t := parse_time(e['time'])) is not None and lo <= t <= hi
                           and (e['to_holder'] == holder or e['from_holder'] == holder)
                           for e in c['edges'])

            candidates.append({'kind': 'station_scan', 'station': a.station or b.station,
                               'window': window, 'holder': holder, 'predicate': pred,
                               'need': f"站点 {a.station or b.station or '?'} 在 {window[0]}~{window[1]} 对持有人 {holder} 的扫码"})
    for cand in candidates:
        kept = [c for c in chains if cand['predicate'](c)]
        if kept and len({c['final_holder'] for c in kept}) == 1:
            return [_public(cand)]
    if len(candidates) <= 12:
        for size in range(2, len(candidates) + 1):
            for combo in combinations(candidates, size):
                kept = [c for c in chains if all(x['predicate'](c) for x in combo)]
                if kept and len({c['final_holder'] for c in kept}) == 1:
                    return [_public(x) for x in combo]
    return [_public(x) for x in candidates] or [{'kind': 'manual_review',
            'need': '现有扫码与签名无法区分，需人工核对实物去向'}]


def _public(cand):
    return {k: v for k, v in cand.items() if k != 'predicate'}


def unapplied_signed_events(events, chains):
    """Signed handoffs/returns that no feasible chain can incorporate.

    A chain legitimately ignores an event only when another ordering fits.
    The event is a *hard contradiction* only when its acting holder (giver for
    a handoff, returner for a scan) never holds the item in any feasible
    chain. If that person does hold it somewhere, the event fits one of the
    competing chains and the situation is genuine ambiguity (handled as
    责任待核), not a conflict.
    """
    holders_ever = set()
    for c in chains:
        for e in c['edges']:
            for name in (e['to_holder'], e['from_holder'], e['actor'], e['giver']):
                if name:
                    holders_ever.add(name)
    applied_refs = {e['ref'] for c in chains for e in c['edges']}
    out = []
    for ev in events:
        if ev.kind == 'loan' or ev.ref in applied_refs:
            continue
        if ev.kind == 'handoff' and ev.signed and ev.giver not in holders_ever:
            out.append({'event': ev.as_dict(),
                        'why': f"已签交接 {ev.ref}（{ev.giver}→{ev.actor}）的移交人在任何相容链中均未持有该序列号"})
        elif ev.kind == 'return' and ev.actor is not None and ev.actor not in holders_ever:
            out.append({'event': ev.as_dict(),
                        'why': f"归还扫码 {ev.ref} 的归还人在任何相容链中均未持有该序列号"})
    return out


def conflict_core(events, skew_seconds=DEFAULT_SKEW_SECONDS):
    """Smallest event subset (rooted at the loan) that has no feasible chain."""
    ordered = sorted(events, key=lambda e: (e.time is None, e.time or datetime.min, e.ref))
    loan = next(e for e in ordered if e.kind == 'loan')
    rest = [e for e in ordered if e.kind != 'loan']
    contradictions = []
    for ev in rest:
        known = {e.actor for e in ordered if e.kind in ('loan', 'handoff')}
        if ev.kind == 'handoff' and ev.giver and ev.giver not in known:
            contradictions.append(f"{ev.ref}: 移交人 {ev.giver} 从未持有该序列号")
        if ev.kind == 'return' and ev.actor and ev.actor not in known:
            contradictions.append(f"{ev.ref}: 归还扫码人 {ev.actor} 无任何持有记录")
    for size in range(0, len(rest) + 1):
        for subset in combinations(rest, size):
            sub_events = [loan] + list(subset)
            sub_chains, _ = enumerate_chains(sub_events, skew_seconds)
            if not sub_chains or unapplied_signed_events(sub_events, sub_chains):
                return {'events': [e.as_dict() for e in sub_events],
                        'contradictions': contradictions or ['事件组合与唯一持有人约束互斥']}
    return {'events': [e.as_dict() for e in ordered], 'contradictions': contradictions}


def apply_contract(agreement, team, borrower, possible):
    """Map feasible final holders to a liability decision under the fixed contract."""
    if not possible:
        return None, 'no_feasible_chain'
    if agreement == TEAM:
        team_set = set(team or [borrower])
        if set(possible) <= team_set:
            return sorted(team_set), 'team_joint_liability'
        return None, 'holder_outside_team_pending'
    if len(possible) == 1:
        return possible, 'personal_holder_liability'
    return None, 'multiple_compatible_holders_pending'


def analyze_serial(loan, serial, serial_events, excluded, skew_seconds, agreement, team):
    """Full per-serial verdict: chains, holders, evidence needs and contract result."""
    body = _body(loan)
    chains, truncated = enumerate_chains(serial_events, skew_seconds)
    returned = any(e['kind'] == 'return' for c in chains for e in c['edges'])
    possible = sorted({c['final_holder'] for c in chains if c['final_holder']})
    result = {'serial': serial, 'loan_id': str(body['id']),
              'chains_truncated': truncated,
              'chains': [{'final_holder': c['final_holder'], 'edges': c['edges']} for c in chains],
              'possible_holders': possible,
              'holder_ranges': holder_ranges(serial_events, chains, skew_seconds),
              'excluded_evidence': excluded.get(serial, [])}
    if not chains:
        result.update(status='证据冲突', reason='no_feasible_chain', liable=[],
                      conflict_core=conflict_core(serial_events, skew_seconds),
                      minimum_evidence=minimum_evidence(serial_events, chains,
                                                        excluded.get(serial, []), skew_seconds))
        return result
    unapplied = unapplied_signed_events(serial_events, chains)
    if unapplied:
        result.update(status='证据冲突', reason='signed_evidence_contradicts_custody', liable=[],
                      conflict_core=conflict_core(serial_events, skew_seconds),
                      unapplied_signed_events=unapplied,
                      minimum_evidence=[])
        return result
    if returned:
        result.update(status='已归还', reason='return_scan_settles_loan', liable=[],
                      minimum_evidence=[])
        return result
    if truncated:
        result.update(status='责任待核', reason='chain_enumeration_truncated', liable=[],
                      minimum_evidence=minimum_evidence(serial_events, chains,
                                                        excluded.get(serial, []), skew_seconds))
        return result
    liable, code = apply_contract(agreement, team, body['borrower'], possible)
    if liable is None:
        result.update(status='责任待核', reason=code, liable=[],
                      minimum_evidence=minimum_evidence(serial_events, chains,
                                                        excluded.get(serial, []), skew_seconds))
    else:
        result.update(status='责任确定', reason=code, liable=liable, minimum_evidence=[])
    return result
