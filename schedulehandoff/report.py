import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def _row(db,table,key):
    item=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    return None if item is None else {**dict(item),'body':json.loads(item['body'])}


def _rows(db,table):
    return [dict(x)|{'body':json.loads(x['body'])} for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]


def _history(db,table,key):
    """All revisions the ledger still holds, oldest first."""
    out=[]
    for item in db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)):
        out.append(dict(item)|{'body':json.loads(item['body'])})
    try:
        for item in db.execute(f'SELECT * FROM {table}_history WHERE id LIKE ?',(key+':%',)):
            out.append(dict(item)|{'body':json.loads(item['body'])})
    except Exception:
        pass
    return out


def _steps(plan):
    lessons = plan.get('in_use_lessons', {})
    out=[]
    for step in plan.get('steps',[]):
        currents=step.get('currents',{})
        instantaneous=step.get('instantaneous_amps',currents)
        served=step.get('served',{})
        affected=[]
        for room,on in served.items():
            affected.append({'room':room,'lesson':lessons.get(room),
                             'served':bool(on)})
        out.append({
            'index':step.get('index'),
            'action':step.get('label'),
            'switch_states':step.get('switch_states',{}),
            'feeder_phase_amps':currents,
            'neutral_amps':{f:c.get('N') for f,c in currents.items()},
            'instantaneous_amps':instantaneous,
            'surges':step.get('surges',[]),
            'served_rooms':served,
            'affected_lessons':affected,
            'outage_minutes':step.get('outage_minutes',{}),
            'deferred_minutes':step.get('deferred_minutes',{}),
            'elapsed_minutes':step.get('elapsed_minutes'),
        })
    return out


def _minimal(reasons):
    """Smallest explanatory set for a refused proof."""
    codes = {str(r).split(':')[0] for r in reasons}
    if 'over_limit' in codes:
        return sorted(codes)
    if 'meter_evidence_missing' in codes:
        codes.discard('receipt_and_currents_verified')
    return sorted(codes)


def _power_section(db):
    """Step-by-step switching evidence plus the service verdicts."""
    section={'plans':[],'executions':[],'proofs':[],'faults':[],'services':[],
             'corrections':[],'topologies':[],'measurements':[],'history':{}}
    for item in _rows(db,'power_topology'):
        section['topologies'].append({**item['body'],'version':item['version'],
                                      'source_id':item['source_id'],'status':item['status']})
    for item in _rows(db,'power_measurement'):
        section['measurements'].append({**item['body'],'version':item['version'],
                                        'source_id':item['source_id']})
    for item in _rows(db,'power_fault'):
        section['faults'].append(item['body'])
    for item in _rows(db,'power_service'):
        section['services'].append(item['body'])
    for item in _rows(db,'power_correction'):
        section['corrections'].append(item['body'])
    for item in _rows(db,'power_switching_plan'):
        body=item['body']
        planned=body.get('outage_minutes',{})
        verdicts=[]
        services={s['room']:s for s in section['services']}
        from . import distribution as dist
        for room in body.get('protected_rooms',[]):
            service=services.get(room)
            if service:
                verdicts.append(dist.screen_service(
                    {'room':room,'lesson':service.get('lesson'),
                     'contract':service.get('contract'),
                     'max_interruption_min':service.get('max_interruption_min'),
                     'signed_after_fault':service.get('signed_after_fault')},
                    'planned',planned,body.get('feasible'),body.get('reasons',[])))
            else:
                verdicts.append({'room':room,'status':'不能履约',
                                 'reason':'service_contract_missing',
                                 'minimal_reasons':['service_contract_missing'],
                                 'basis':'planned'})
        section['plans'].append({'plan_id':item['id'],'version':item['version'],
                                 'status':item['status'],'reason':item['reason'],
                                 'feasible':body.get('feasible'),
                                 'protected_rooms':body.get('protected_rooms'),
                                 'sheddable_rooms':body.get('sheddable_rooms'),
                                 'in_use_lessons':body.get('in_use_lessons'),
                                 'missing_topology_rooms':body.get('missing_topology_rooms'),
                                 'schedule_fingerprint':body.get('schedule_fingerprint'),
                                 'steps':_steps(body),
                                 'planned_outage_minutes':planned,
                                 'reasons':body.get('reasons',[]),
                                 'services':verdicts,
                                 'individual_feasibility':body.get('individual_feasibility')})
        section['history'].setdefault('power_switching_plan',{})[item['id']]=_history(db,'power_switching_plan',item['id'])
    for item in _rows(db,'power_switching_execution'):
        section['executions'].append({'execution_id':item['id'],'version':item['version'],
                                      'status':item['status'],'reason':item['reason'],
                                      'plan_id':item['body'].get('plan_id'),
                                      'started_at':item['body'].get('started_at'),
                                      'fault_at':item['body'].get('fault_at'),
                                      'steps':item['body'].get('steps',[])})
        section['history'].setdefault('power_switching_execution',{})[item['id']]=_history(db,'power_switching_execution',item['id'])
    for item in _rows(db,'power_switching_proof'):
        body=item['body']
        executed=body.get('executed_outage_minutes',{}) or {}
        plan_row=_row(db,'power_switching_plan',body.get('plan_id'))
        verdicts=[]
        if plan_row:
            services={s['room']:s for s in section['services']}
            from . import distribution as dist
            for room in plan_row['body'].get('protected_rooms',[]):
                service=services.get(room)
                if service:
                    verdicts.append(dist.screen_service(
                        {'room':room,'lesson':service.get('lesson'),
                         'contract':service.get('contract'),
                         'max_interruption_min':service.get('max_interruption_min'),
                         'signed_after_fault':service.get('signed_after_fault')},
                        'executed',executed,body.get('status')=='已证明',
                        body.get('reasons',[])))
                else:
                    verdicts.append({'room':room,'status':'不能履约',
                                     'reason':'service_contract_missing',
                                     'minimal_reasons':['service_contract_missing'],
                                     'basis':'executed'})
        section['proofs'].append({'proof_id':item['id'],'version':item['version'],
                                  'status':item['status'],'reason':item['reason'],
                                  'plan_id':body.get('plan_id'),
                                  'execution_id':body.get('execution_id'),
                                  'reasons':body.get('reasons',[]),
                                  'minimal_reasons':_minimal(body.get('reasons',[])),
                                  'steps':body.get('executed_steps',[]),
                                  'executed_outage_minutes':executed,
                                  'plan_outage_minutes':body.get('plan_outage_minutes'),
                                  'services':verdicts})
        section['history'].setdefault('power_switching_proof',{})[item['id']]=_history(db,'power_switching_proof',item['id'])
    return section


def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['power']=_power_section(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
