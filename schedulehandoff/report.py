import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def macro_replay(db):
    """Reconstruct each frozen macro run with its lease holder and recovery decisions.

    This is the replay of "why there was no second click": the frozen contract
    and versions, who (if anyone) holds the exclusive operation lease, and every
    recovery decision with the version check it was based on.
    """
    runs={row['id']:json.loads(row['body']) for row in db.execute('SELECT * FROM automation_run')}
    leases={row['id']:json.loads(row['body']) for row in db.execute('SELECT * FROM automation_lease')}
    recoveries=[dict(row) for row in db.execute('SELECT * FROM automation_recovery ORDER BY id')]
    story={}
    for run_id,body in runs.items():
        related=[r for r in recoveries if json.loads(r['body']).get('run')==run_id]
        story[run_id]={
            'project':body.get('project'),
            'contract':body.get('contract'),
            'base_version':body.get('base_version'),
            'target_summary':body.get('target_summary'),
            'lease_holder':(leases.get(body.get('project')) or {}).get('operator'),
            'recoveries':[{'id':r['id'],'status':r['status'],'reason':r['reason'],
                           'version_check':json.loads(r['body']).get('version_check')} for r in related],
        }
    return story


def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
              'macro_replay':macro_replay(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
