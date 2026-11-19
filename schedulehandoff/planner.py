"""Joint room and substitute matching for disrupted class schedules."""
import json
from .ingest import capture
from .store import connect,save,decide
from .operations import overlap


def _still_available(db, plan_id, assignments):
    rooms=[json.loads(x['body']) for x in db.execute("SELECT body FROM room_slot WHERE status='可用' AND id NOT LIKE ?",(f'{plan_id}:%',))]
    teachers=[json.loads(x['body']) for x in db.execute("SELECT body FROM substitution WHERE status='已委托' AND id NOT LIKE ?",(f'{plan_id}:%',))]
    for item in assignments:
        if any(x['room']==item['room'] and overlap(x['start'],x['end'],item['start'],item['end']) for x in rooms):return False
        if any(x['teacher']==item['teacher'] and overlap(x['start'],x['end'],item['start'],item['end']) for x in teachers):return False
    return True


def plan(path,file):
    source,payload=capture(path,'desktop_reschedule_request',file)
    lessons=payload.get('rows',[]);rooms=payload.get('rooms',[])
    if not lessons or len({x['id'] for x in lessons})!=len(lessons):raise ValueError('invalid_lesson_scope')
    db=connect(path)
    try:
        plan_id=str(payload['plan_id'])
        existing=db.execute('SELECT * FROM reschedule_plan WHERE id=?',(plan_id,)).fetchone()
        if existing and existing['status']=='已应用':
            if existing['source_id']!=source:raise ValueError('plan_identity_conflict')
            return {'plan_id':plan_id,'status':'已应用','reason':'approved',
                    'assignments':json.loads(existing['body'])['assignments']}
        occupied=[json.loads(x['body']) for x in db.execute("SELECT body FROM room_slot WHERE status='可用'")]
        roster={x['id']:json.loads(x['body']) for x in db.execute('SELECT * FROM teacher')}
        assigned=[json.loads(x['body']) for x in db.execute("SELECT body FROM substitution WHERE status='已委托'")]
        options=[]
        for lesson in lessons:
            choices=[]
            for room in rooms:
                if room['id'] not in lesson['candidate_rooms'] or int(room['capacity'])<int(lesson['enrolled']):continue
                if any(x['room']==room['id'] and overlap(x['start'],x['end'],lesson['start'],lesson['end']) for x in occupied):continue
                for teacher_id in lesson['candidate_teachers']:
                    teacher=roster.get(teacher_id)
                    if not teacher or lesson['subject'] not in teacher.get('qualified',[]):continue
                    if any(overlap(a,b,lesson['start'],lesson['end']) for a,b in teacher.get('busy',[])):continue
                    if any(x['teacher']==teacher_id and overlap(x['start'],x['end'],lesson['start'],lesson['end']) for x in assigned):continue
                    rank=lesson['candidate_rooms'].index(room['id'])+lesson['candidate_teachers'].index(teacher_id)
                    score=rank*100+int(room['capacity'])-int(lesson['enrolled'])
                    choices.append((score,room['id'],teacher_id,int(room['capacity'])))
            options.append(sorted(choices))
        best=None
        def search(index,chosen,cost):
            nonlocal best
            if index==len(lessons):
                candidate=(len(chosen),-cost,list(chosen))
                if best is None or candidate[:2]>best[:2]:best=candidate
                return
            search(index+1,chosen,cost)
            lesson=lessons[index]
            for score,room,teacher,capacity in options[index]:
                if any((p['room']==room or p['teacher']==teacher) and
                       overlap(p['start'],p['end'],lesson['start'],lesson['end']) for p in chosen):continue
                chosen.append(dict(lesson=lesson['id'],room=room,teacher=teacher,capacity=capacity,
                                   start=lesson['start'],end=lesson['end'],subject=lesson['subject'],
                                   enrolled=lesson['enrolled']))
                search(index+1,chosen,cost+score)
                chosen.pop()
        search(0,[],0)
        chosen=best[2] if best else []
        status,reason=('可确认','all_lessons_assigned') if len(chosen)==len(lessons) else ('待协调','unassigned_lessons')
        with db:
            save(db,'reschedule_plan',plan_id,source,payload['version'],
                 {'lessons':lessons,'rooms':rooms,'assignments':chosen},status,reason)
            for assignment in chosen:
                save(db,'reschedule_assignment',f"{plan_id}:{assignment['lesson']}",source,payload['version'],
                     assignment,'已规划','joint_match')
            decide(db,'联合调课',plan_id,status,reason,source)
        return {'plan_id':plan_id,'status':status,'reason':reason,'assignments':chosen}
    finally:db.close()


def approve(path,file):
    source,payload=capture(path,'desktop_reschedule_approval',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                plan_id=str(row['plan_id']);plan_row=db.execute('SELECT * FROM reschedule_plan WHERE id=?',(plan_id,)).fetchone()
                old=db.execute('SELECT * FROM reschedule_approval WHERE id=?',(plan_id,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('approval_identity_conflict')
                    results.append(decide(db,'联合调课',plan_id,old['status'],old['reason'],source));continue
                if not plan_row or plan_row['status']!='可确认':status,reason='拒绝','plan_incomplete'
                elif not row.get('approver') or row.get('version')!=plan_row['version']:status,reason='拒绝','approval_invalid'
                elif not _still_available(db,plan_id,json.loads(plan_row['body'])['assignments']):
                    status,reason='拒绝','resource_changed_since_plan'
                else:
                    status,reason='已应用','joint_plan_applied'
                    plan_body=json.loads(plan_row['body'])
                    for assignment in plan_body['assignments']:
                        key=f"{plan_id}:{assignment['lesson']}"
                        save(db,'room_slot',key,source,payload['version'],assignment,'可用','planned_room')
                        save(db,'substitution',key,source,payload['version'],assignment,'已委托','planned_substitute')
                    db.execute("UPDATE reschedule_plan SET status='已应用',reason='approved' WHERE id=?",(plan_id,))
                save(db,'reschedule_approval',plan_id,source,payload['version'],row,status,reason)
                results.append(decide(db,'联合调课',plan_id,status,reason,source))
        return results
    finally:db.close()
