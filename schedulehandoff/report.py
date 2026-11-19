import hashlib,json
from pathlib import Path
from .store import connect,TABLES
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
        data['travel']=_travel_summary(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()


def _travel_summary(db):
    """Read back plans, elimination reasons and approval-time travel changes."""
    summary={'routes':[],'services':[],'plans':[],'chains':[]}
    for record in db.execute('SELECT * FROM travel_route ORDER BY id'):
        body=json.loads(record['body'])
        summary['routes'].append({'id':body['id'],'version':body['version'],'status':record['status'],
                                  'origin':body['origin'],'destination':body['destination'],
                                  'service':body['service'],'minutes':body['minutes']})
    for record in db.execute('SELECT * FROM class_service ORDER BY id'):
        body=json.loads(record['body'])
        summary['services'].append({'class':body['class'],'service':body['service'],
                                    'agreement_id':body.get('agreement_id'),'version':record['version']})
    for record in db.execute('SELECT * FROM reschedule_plan ORDER BY id'):
        body=json.loads(record['body'])
        summary['plans'].append({'plan_id':record['id'],'status':record['status'],'reason':record['reason'],
                                 'assignments':[{'lesson':a['lesson'],'class':a.get('class'),'room':a['room'],
                                                 'teacher':a['teacher'],'room_site':a.get('room_site'),
                                                 'teaching_site':a.get('teaching_site'),
                                                 'remote':a.get('remote',False)} for a in body.get('assignments',[])],
                                 'eliminations':body.get('eliminations',{})})
    for record in db.execute('SELECT * FROM travel_chain ORDER BY id'):
        body=json.loads(record['body'])
        summary['chains'].append({'id':record['id'],'status':record['status'],'reason':record['reason'],
                                  'kind':body['kind'],'traveller':body['traveller'],
                                  'feasible':body['feasible'],
                                  'legs':[{'prev_lesson':leg['prev_lesson'],'next_lesson':leg['next_lesson'],
                                           'origin':leg.get('origin'),'destination':leg.get('destination'),
                                           'service':leg.get('service'),'route_id':leg.get('route_id'),
                                           'route_version':leg.get('route_version'),
                                           'gap_minutes':leg.get('gap_minutes'),
                                           'required_minutes':leg.get('required_minutes'),
                                           'feasible':leg['feasible'],'reason':leg['reason']}
                                          for leg in body['legs']]})
    return summary
