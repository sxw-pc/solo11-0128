"""Teacher withdrawal and targeted class notice recovery."""
import json
from .ingest import capture
from .store import connect,save,decide


def withdraw(path,file):
    source,payload=capture(path,'desktop_teacher_withdrawal',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);lesson=str(row['lesson'])
                current=db.execute('SELECT * FROM substitution WHERE id=?',(lesson,)).fetchone()
                old=db.execute('SELECT * FROM withdrawal WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('withdrawal_identity_conflict')
                    results.append(decide(db,'教师代课',key,old['status'],old['reason'],source));continue
                if not current or current['status']!='已委托':status,reason='待核','assignment_not_active'
                elif json.loads(current['body'])['teacher']!=row['teacher']:status,reason='拒绝','teacher_mismatch'
                elif not row.get('reason'):status,reason='待核','reason_missing'
                else:
                    status,reason='已撤回','teacher_withdrew'
                    db.execute("UPDATE substitution SET status='已撤回',reason='teacher_withdrew' WHERE id=?",(lesson,))
                    for notice in db.execute('SELECT * FROM notice_batch'):
                        if json.loads(notice['body'])['lesson']==lesson:
                            db.execute("UPDATE notice_batch SET status='撤回待通知',reason='teacher_withdrew' WHERE id=?",(notice['id'],))
                save(db,'withdrawal',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'教师代课',key,status,reason,source))
        return results
    finally:db.close()


def resend(path,file):
    source,payload=capture(path,'desktop_notice_retry',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);batch=db.execute('SELECT * FROM notice_batch WHERE id=?',(str(row['batch']),)).fetchone()
                if not batch:status,reason='拒绝','batch_missing'
                elif row['class'] not in json.loads(batch['body'])['required']:status,reason='拒绝','class_out_of_scope'
                elif row.get('version')!=batch['version']:status,reason='待核','stale_notice_version'
                elif not row.get('ack_token'):status,reason='待核','ack_missing'
                elif batch['status']=='撤回待通知':status,reason='待核','assignment_withdrawn'
                else:status,reason='已送达','recovered_notice'
                save(db,'notice_attempt',key,source,payload['version'],row,status,reason)
                if status=='已送达':
                    save(db,'notice_delivery',f"{row['batch']}:{row['class']}",source,payload['version'],
                         {'batch':row['batch'],'class':row['class'],'ack_token':row['ack_token']},status,reason)
                    required=set(json.loads(batch['body'])['required'])
                    delivered={json.loads(x['body'])['class'] for x in db.execute("SELECT body FROM notice_delivery WHERE status='已送达'") if json.loads(x['body'])['batch']==row['batch']}
                    original={json.loads(x['body'])['class'] for x in db.execute('SELECT body FROM notice_receipt WHERE id LIKE ?', (f"{row['batch']}:%",)) if json.loads(x['body']).get('version')==batch['version']}
                    if required.issubset(delivered|original):
                        db.execute("UPDATE notice_batch SET status='换班生效',reason='resend_recovered' WHERE id=?",(batch['id'],))
                results.append(decide(db,'通知交付',key,status,reason,source))
        return results
    finally:db.close()
