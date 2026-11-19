"""Room occupation, substitute authorization, and class notice handoff."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide


def overlap(a,b,c,d):
    return datetime.fromisoformat(a)<datetime.fromisoformat(d) and datetime.fromisoformat(c)<datetime.fromisoformat(b)


def first(path,file):
    source,payload=capture(path,'desktop_schedule',file)
    db=connect(path);results=[]
    try:
        with db:
            save(db,'schedule_export',f'export:{source}',source,payload['version'],
                 {'version':payload['version'],'count':len(payload.get('rows',[]))},'已采录','snapshot_frozen')
            for row in payload.get('rows',[]):
                key=str(row['id']);prior=[json.loads(x['body']) for x in db.execute("SELECT body FROM room_slot WHERE status='可用' AND id<>?",(key,))]
                if datetime.fromisoformat(row['start'])>=datetime.fromisoformat(row['end']):status,reason='待协调','bad_interval'
                elif int(row['enrolled'])>int(row['capacity']):status,reason='待协调','capacity_exceeded'
                elif any(x['room']==row['room'] and overlap(x['start'],x['end'],row['start'],row['end']) for x in prior):status,reason='待协调','room_overlap'
                else:status,reason='可用','room_available'
                save(db,'room_slot',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'教室占用',key,status,reason,source))
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'desktop_staff_roster',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                lesson=str(row['lesson']);teacher=str(row['teacher'])
                slot=db.execute('SELECT * FROM room_slot WHERE id=?',(lesson,)).fetchone()
                lesson_data=json.loads(slot['body']) if slot else {}
                busy=any(overlap(start,end,lesson_data.get('start',start),lesson_data.get('end',end))
                         for start,end in row.get('busy',[]))
                another=[json.loads(x['body']) for x in db.execute("SELECT body FROM substitution WHERE status='已委托' AND id<>?",(lesson,))]
                parallel=any(x['teacher']==teacher and overlap(x['start'],x['end'],lesson_data.get('start',x['start']),lesson_data.get('end',x['end'])) for x in another)
                if not slot or slot['status']!='可用':status,reason='待协调','room_unavailable'
                elif row['subject'] not in row.get('qualified',[]):status,reason='拒绝','qualification_missing'
                elif busy or parallel:status,reason='待协调','teacher_busy'
                elif not row.get('accepted',False):status,reason='待协调','teacher_not_accepted'
                else:status,reason='已委托','qualified_and_available'
                save(db,'teacher',teacher,source,payload['version'],{'qualified':row.get('qualified',[]),'busy':row.get('busy',[])},'已登记','roster_captured')
                detail=dict(row,start=lesson_data.get('start'),end=lesson_data.get('end'))
                save(db,'substitution',lesson,source,payload['version'],detail,status,reason)
                results.append(decide(db,'教师代课',lesson,status,reason,source))
        return results
    finally:db.close()


def third(path,file):
    source,payload=capture(path,'desktop_notice_receipt',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);room=db.execute('SELECT status,version FROM room_slot WHERE id=?',(str(row['lesson']),)).fetchone()
                teacher=db.execute('SELECT status FROM substitution WHERE id=?',(str(row['lesson']),)).fetchone()
                required=set(row['required_classes']);received={r['class'] for r in row.get('receipts',[]) if r.get('version')==payload['version']}
                if not room or room['status']!='可用' or not teacher or teacher['status']!='已委托':status,reason='拒绝生效','schedule_unresolved'
                elif room['version']!=payload['version']:status,reason='通知不全','stale_schedule_version'
                elif not required or not required.issubset(received):status,reason='通知不全','receipt_missing'
                else:status,reason='换班生效','all_parties_confirmed'
                save(db,'notice_batch',key,source,payload['version'],{'lesson':row['lesson'],'required':sorted(required)},status,reason)
                for receipt in row.get('receipts',[]):
                    save(db,'notice_receipt',f"{key}:{receipt['class']}",source,payload['version'],receipt,
                         '有效' if receipt.get('version')==payload['version'] else '旧版','version_check')
                results.append(decide(db,'通知交付',key,status,reason,source))
        return results
    finally:db.close()


def demo(root):
    import json
    db=root/'ledger.db';files=[
        {'version':'v1','rows':[{'id':'L1','room':'R1','start':'2026-01-01T09:00:00','end':'2026-01-01T10:00:00','capacity':35,'enrolled':30}]},
        {'version':'v1','rows':[{'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]},
        {'version':'v1','rows':[{'id':'N1','lesson':'L1','required_classes':['C1','C2'],'receipts':[{'class':'C1','version':'v1'},{'class':'C2','version':'v1'}]}]}]
    results=[]
    for i,(fn,item) in enumerate(zip((first,second,third),files)):
        p=root/f'export{i}.json';p.write_text(json.dumps(item),encoding='utf-8');results.append(fn(db,p))
    return results
