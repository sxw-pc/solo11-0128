import hashlib
import json
from pathlib import Path

from .store import EVIDENCE_TABLES, PICKUP_TABLES, connect

CONTRACT_ONLY_GUARDIAN = {'guardian_only', 'registered_guardian_only', 'only_guardian',
                          'guardian', '仅监护人', '监护人本人', '本人接领'}
CONTRACT_ALLOW_DELEGATE = {'delegate', 'delegation', 'designated', 'designated_delegate',
                           'allow_delegate', 'delegate_allowed', '临时代接', '可委托'}


def _allows_delegate(contract):
    mode = str(contract.get('mode') or contract.get('type') or '').lower()
    if mode in CONTRACT_ONLY_GUARDIAN:
        return False
    if mode in CONTRACT_ALLOW_DELEGATE:
        return True
    return bool(contract.get('allows_delegate') or contract.get('allow_delegate'))

PICKUP_EVIDENCE = [
    'pickup_student', 'pickup_guardian', 'pickup_contract', 'pickup_delegation',
    'pickup_revocation', 'pickup_session', 'pickup_credential', 'pickup_action',
    'pickup_delivery', 'pickup_fork', 'pickup_conclusion', 'pickup_attempt',
]


def _loads(row):
    body = json.loads(row['body'])
    body.setdefault('id', row['id'])
    body['_status'] = row['status']
    body['_reason'] = row['reason']
    body['_version'] = row['version']
    return body


def _roster(db, student_id):
    rows = [x for x in db.execute('SELECT * FROM pickup_roster ORDER BY id')
            if json.loads(x['body']).get('student_id') == student_id]
    return [_loads(row) for row in rows]


def _pickup_view(db):
    sessions = [_loads(x) for x in db.execute('SELECT * FROM pickup_session ORDER BY id')]
    conclusions = {x['id']: _loads(x) for x in db.execute('SELECT * FROM pickup_conclusion')}
    contracts = {x['id']: _loads(x) for x in db.execute('SELECT * FROM pickup_contract')}
    guardians = {x['id']: _loads(x) for x in db.execute('SELECT * FROM pickup_guardian')}
    delegations = {x['id']: _loads(x) for x in db.execute('SELECT * FROM pickup_delegation')}
    revocations = [_loads(x) for x in db.execute('SELECT * FROM pickup_revocation ORDER BY id')]
    credentials = [_loads(x) for x in db.execute('SELECT * FROM pickup_credential ORDER BY id')]
    actions = [_loads(x) for x in db.execute('SELECT * FROM pickup_action ORDER BY id')]
    deliveries = {x['id']: _loads(x) for x in db.execute('SELECT * FROM pickup_delivery')}
    forks = [_loads(x) for x in db.execute('SELECT * FROM pickup_fork ORDER BY id')]
    students = []
    for session in sessions:
        cid = session['id']
        conclusion = conclusions.get(cid)
        student_id = str(session.get('student_id'))
        member_delegations = [d for d in delegations.values()
                              if str(d.get('student_id')) == student_id]
        member_contracts = [c for c in contracts.values()
                            if str(c.get('student_id')) == student_id
                            or (c.get('scope') is not None
                                and student_id in {str(x) for x in c['scope']})]
        member_revocations = [r for r in revocations
                              if str(r.get('delegation_id')) in
                              {str(d['id']) for d in member_delegations}]
        member_credentials = [c for c in credentials if str(c.get('session_id')) == cid]
        credential_ids = {str(c['id']) for c in member_credentials}
        member_actions = [a for a in actions if str(a.get('session_id')) == cid]
        custody_actions = [a for a in member_actions
                           if str(a.get('credential_id') or '') in credential_ids
                           or a.get('credential_id') is None]
        member_forks = [f for f in forks if f['id'].startswith(cid + ':')]
        delivery = deliveries.get(cid)
        chain = []
        for contract in member_contracts:
            guardian = guardians.get(str(contract.get('guardian_id')))
            chain.append({
                'kind': 'contract', 'id': contract['id'],
                'issued_at': contract.get('signed_at') or contract.get('issued_at'),
                'scope': contract.get('scope') or student_id,
                'source_digest': contract.get('source_digest'),
                'mode': contract.get('mode') or contract.get('type'),
                'allows_delegate': _allows_delegate(contract),
                'guardian_id': contract.get('guardian_id'),
                'guardian': guardian,
                'status': contract.get('_status'),
            })
            for delegation in member_delegations:
                if str(delegation.get('contract_id')) != str(contract['id']):
                    continue
                chain.append({
                    'kind': 'delegation', 'id': delegation['id'],
                    'issued_at': delegation.get('issued_at'),
                    'valid_from': delegation.get('valid_from'),
                    'valid_to': delegation.get('valid_to'),
                    'scope': delegation.get('scope') or student_id,
                    'source_digest': delegation.get('source_digest'),
                    'issuer_id': delegation.get('issuer_id'),
                    'delegate_id': delegation.get('delegate_id'),
                    'delegate_name': delegation.get('delegate_name'),
                    'delegate': delegation.get('delegate'),
                    'delegate_id_number': delegation.get('delegate_id_number'),
                    'delegate_id_type': delegation.get('delegate_id_type'),
                    'status': delegation.get('_status'),
                })
                for revocation in member_revocations:
                    if str(revocation.get('delegation_id')) != str(delegation['id']):
                        continue
                    chain.append({
                        'kind': 'revocation', 'id': revocation['id'],
                        'issued_at': revocation.get('issued_at') or revocation.get('at'),
                        'effective_at': revocation.get('effective_at') or revocation.get('at'),
                        'source_digest': revocation.get('source_digest'),
                        'status': revocation.get('_status'),
                    })
        view = {
            'student_id': student_id,
            'session_id': cid,
            'class_id': session.get('class_id') or session.get('class'),
            'lesson_id': session.get('lesson_id') or session.get('lesson'),
            'window': (conclusion or {}).get('window'),
            'authorization_chain': chain,
            'roster': _roster(db, student_id),
            'authorization': (conclusion or {}).get('authorization'),
            'identity_check': (conclusion or {}).get('identity_check'),
            'credentials': (conclusion or {}).get('credentials') or [
                {'id': c['id'], 'serial': c.get('serial'), 'exit': c.get('exit'),
                 'predecessor': c.get('predecessor'), 'voided_at': c.get('voided_at')}
                for c in member_credentials],
            'credential_authorization': (conclusion or {}).get('credential_authorization'),
            'successors': [
                {'credential': c['id'], 'serial': c.get('serial'),
                 'predecessor': c.get('predecessor'), 'exit': c.get('exit')}
                for c in member_credentials],
            'custody': (conclusion or {}).get('custody'),
            'actions': custody_actions,
            'forks': member_forks or (conclusion or {}).get('forks') or [],
            'delivery': delivery,
            'delivered': bool((conclusion or {}).get('delivered')),
            'delivery_at': (conclusion or {}).get('delivery_at'),
            'safe_delivery': bool((conclusion or {}).get('safe_delivery')),
            'decision': (conclusion or {}).get('status'),
            'reason': (conclusion or {}).get('reason'),
            'reasons': (conclusion or {}).get('reasons') or [],
            'current_conclusion': (conclusion or {}).get('current_conclusion'),
            'source_conflicts': (conclusion or {}).get('source_conflicts') or [],
        }
        students.append(view)
    return students


def build(path, out):
    db = connect(path)
    try:
        sources = []
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x = dict(item)
            x.pop('body')
            x['verified'] = hashlib.sha256(item['body']).hexdigest() == item['hash']
            sources.append(x)
        data = {'sources': sources, 'entities': {},
                'issues': [dict(x) for x in db.execute('SELECT * FROM issue')],
                'decisions': [dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in EVIDENCE_TABLES + PICKUP_TABLES + ['entity_history']:
            data['entities'][table] = [dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['pickup'] = _pickup_view(db)
        data['pickup_students'] = data['pickup']
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return out
    finally:
        db.close()
