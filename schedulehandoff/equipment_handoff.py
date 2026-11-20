"""Capture signed and pending equipment handoffs between teachers.

A handoff may move one serial (scope=item) or a whole kit (scope=kit, the
serial set is expanded from the checkout configuration). Only a signed
handoff can move custody; an unsigned one is retained as evidence and becomes
effective when a matching signature is imported later. Re-importing the same
handoff with ``signed`` switched from false to true is the supported late
signature path; changing the parties or serials of an existing handoff is an
identity conflict.
"""
from .ingest import capture
from .store import connect, decide, get_row, upsert, record_issue
from . import lifecycle


def _expanded_serials(loan_body, row):
    if row.get('scope') == 'kit':
        return list(loan_body['serials'])
    return list(row.get('serials', []))


def handoff(path, file):
    source, payload = capture(path, 'desktop_equipment_handoff', file)
    db = connect(path)
    results = []
    try:
        with db:
            affected = set()
            for row in payload.get('rows', []):
                key = str(row['id'])
                loan = get_row(db, 'equipment_loan', str(row.get('loan')))
                status, reason = '已采录', 'handoff_recorded'
                effective = dict(row)
                if not loan or loan['status'] not in (lifecycle.ACTIVE_LOAN, lifecycle.SETTLED_LOAN):
                    status, reason = '待核', 'handoff_loan_missing'
                else:
                    loan_body = __import__('json').loads(loan['body'])
                    serials = _expanded_serials(loan_body, row)
                    if not serials or any(s not in loan_body['serials'] for s in serials):
                        status, reason = '待核', 'serial_not_in_kit'
                    elif not row.get('giver') or not row.get('receiver'):
                        status, reason = '待核', 'handoff_parties_missing'
                    effective['serials'] = serials
                    if row.get('scope') == 'kit':
                        effective['scope'] = 'kit'
                    else:
                        effective.setdefault('scope', 'item')
                    if not row.get('signed', False):
                        status, reason = ('待签', 'awaiting_signature') if status == '已采录' else (status, reason)
                old = get_row(db, 'equipment_handoff', key)
                if old:
                    old_body = __import__('json').loads(old['body'])
                    immutable = ('loan', 'giver', 'receiver', 'station', 'time', 'scope', 'serials')
                    if any(old_body.get(f) != effective.get(f) for f in immutable):
                        record_issue(db, source, 'identity_conflict', f'equipment_handoff:{key}')
                        raise ValueError('identity_conflict')
                upsert(db, 'equipment_handoff', key, source, payload['version'],
                       effective, status, reason)
                if row.get('signed') and row.get('signature_id'):
                    upsert(db, 'equipment_signature', str(row['signature_id']), source,
                           payload['version'],
                           {'id': row['signature_id'], 'handoff': key,
                            'loan': row.get('loan'), 'signed_by': row.get('receiver'),
                            'time': row.get('signature_time') or row.get('time'),
                            'station': row.get('station')},
                           '有效', 'signature_received')
                results.append(decide(db, '器材交接', key, status, reason, source))
            if affected:
                # New handoff evidence may revise a case already opened at
                # return time; refresh chains and version the decision.
                for item in lifecycle.reanalyze(db, affected, source, payload['version']):
                    for serial, verdict in item['serials'].items():
                        decide(db, '器材责任链', f"{item['loan_id']}:{serial}",
                               verdict['status'], verdict['reason'], source)
            return results
    finally:
        db.close()


def sign(path, file):
    """Standalone late signature import: ``{handoff, signature_id, signed_by}``."""
    source, payload = capture(path, 'desktop_equipment_signature', file)
    db = connect(path)
    results = []
    try:
        with db:
            affected = set()
            for row in payload.get('rows', []):
                key = str(row['signature_id'])
                handoff_row = get_row(db, 'equipment_handoff', str(row['handoff']))
                if not handoff_row:
                    status, reason = '待核', 'handoff_missing_for_signature'
                else:
                    hb = __import__('json').loads(handoff_row['body'])
                    upsert(db, 'equipment_signature', key, source, payload['version'],
                           {'id': key, 'handoff': row['handoff'], 'loan': hb.get('loan'),
                            'signed_by': row.get('signed_by', hb.get('receiver')),
                            'time': row.get('time') or hb.get('time'),
                            'station': row.get('station') or hb.get('station')},
                           '有效', 'signature_received')
                    hb['signed'] = True
                    hb['signature_id'] = key
                    upsert(db, 'equipment_handoff', hb['id'], source, payload['version'],
                           hb, '已采录', 'late_signature_completed')
                    status, reason = '已采录', 'late_signature_completed'
                    affected.add(str(hb.get('loan')))
                results.append(decide(db, '器材签收', key, status, reason, source))
            if affected:
                for item in lifecycle.reanalyze(db, affected, source, payload['version']):
                    for serial, verdict in item['serials'].items():
                        decide(db, '器材责任链', f"{item['loan_id']}:{serial}",
                               verdict['status'], verdict['reason'], source)
            return results
    finally:
        db.close()
