import hashlib,json
from pathlib import Path
from .store import connect,TABLES

METHOD=('有界离散质量守恒（体积已折算）：c_end = c_start + S - k*(c_start - c_out) + 回风流入 - 回风流出；'
        'S 由每段实际占用范围与同场各教室共用的活动系数界定；传感器零点与响应滞后对同一连续序列共用；'
        '相邻房间共享同一股回风流量；换气比例 k 取所有相容解释的完整可证范围，不取单一最优拟合值。'
        '预防性巡检委托在存在低于约定值的相容解释时派查（派查不等于已证不达标）；'
        '达标公示委托只在所有相容解释都不低于约定值时可签发。')

def _ventilation(db):
    mandates=[]
    for m in db.execute('SELECT * FROM air_mandate ORDER BY id'):
        assessments=[]
        for a in db.execute('SELECT * FROM air_assessment WHERE mandate_id=? ORDER BY id',(m['id'],)):
            row=dict(a);row['body']=json.loads(row['body']);assessments.append(row)
        current=next((a for a in reversed(assessments) if not a['superseded']),None)
        decisions=[dict(x) for x in db.execute(
            "SELECT * FROM decision_log WHERE subsystem='通风委托' AND subject=? ORDER BY id",(m['id'],))]
        mandates.append({'id':m['id'],'registration_status':m['status'],'mandate':json.loads(m['body']),
                         'current':current,'history':assessments,'decisions':decisions})
    return {'method':METHOD,'mandates':mandates}

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
              'ventilation':_ventilation(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
