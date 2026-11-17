import json
from schedulehandoff import operations,report,changes
from schedulehandoff.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p
def slot(id,room='R1',start='2026-01-01T09:00:00',end='2026-01-01T10:00:00',capacity=35,enrolled=30):
    return dict(id=id,room=room,start=start,end=end,capacity=capacity,enrolled=enrolled)

def test_room_teacher_notice_full_chain(tmp_path):
    result=operations.demo(tmp_path)
    assert [x[0]['status'] for x in result]==['可用','已委托','换班生效']
    data=json.loads(report.build(tmp_path/'ledger.db',tmp_path/'report.json').read_text(encoding='utf-8'))
    assert len(data['sources'])==3 and all(x['verified'] for x in data['sources'])

def test_room_overlap_capacity_and_bad_interval(tmp_path):
    db=tmp_path/'db';rows=[slot('L1'),slot('L2',start='2026-01-01T09:30:00',end='2026-01-01T10:30:00'),
                            slot('L3',room='R2',capacity=10),slot('L4',room='R3',start='2026-01-01T10:00:00',end='2026-01-01T09:00:00')]
    result=operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['room_available','room_overlap','capacity_exceeded','bad_interval']

def test_substitute_qualification_and_parallel_teaching(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1'),slot('L2',room='R2')]}))
    rows=[{'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True},
          {'id':'S2','lesson':'L2','teacher':'T2','subject':'math','qualified':['history'],'busy':[],'accepted':True}]
    result=operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':rows}))
    assert [x['status'] for x in result]==['已委托','拒绝']
    assert connect(db).execute('SELECT count(*) FROM teacher').fetchone()[0]==2

def test_missing_and_stale_receipts_block_effective_schedule(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1')]}))
    operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':[
        {'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]}))
    rows=[{'id':'N1','lesson':'L1','required_classes':['C1','C2'],'receipts':[{'class':'C1','version':'v1'}]},
          {'id':'N2','lesson':'L1','required_classes':['C1'],'receipts':[{'class':'C1','version':'old'}]}]
    result=operations.third(db,write(tmp_path,'notice.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['receipt_missing','receipt_missing']

def test_notice_cannot_override_room_failure(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1',capacity=1)]}))
    operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':[
        {'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]}))
    result=operations.third(db,write(tmp_path,'notice.json',{'version':'v1','rows':[
        {'id':'N1','lesson':'L1','required_classes':['C1'],'receipts':[{'class':'C1','version':'v1'}]}]}))
    assert result[0]['reason']=='schedule_unresolved'

def test_teacher_parallel_assignment_and_withdrawal_revoke_effective_notice(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1'),slot('L2',room='R2')]}))
    rows=[{'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True},
          {'id':'S2','lesson':'L2','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]
    assert [x['reason'] for x in operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':rows}))]==['qualified_and_available','teacher_busy']
    operations.third(db,write(tmp_path,'notice.json',{'version':'v1','rows':[
        {'id':'N1','lesson':'L1','required_classes':['C1'],'receipts':[{'class':'C1','version':'v1'}]}]}))
    assert connect(db).execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()[0]=='换班生效'
    file=write(tmp_path,'withdraw.json',{'version':'v1','rows':[{'id':'W1','lesson':'L1','teacher':'T1','reason':'illness'}]})
    assert changes.withdraw(db,file)==changes.withdraw(db,file)
    assert connect(db).execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()[0]=='撤回待通知'

def test_targeted_notice_retry_recovers_missing_class(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1')]}))
    operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':[
        {'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]}))
    operations.third(db,write(tmp_path,'notice.json',{'version':'v1','rows':[
        {'id':'N1','lesson':'L1','required_classes':['C1','C2'],'receipts':[{'class':'C1','version':'v1'}]}]}))
    stale={'version':'v1','rows':[{'id':'A0','batch':'N1','class':'C2','version':'old','ack_token':'ack'}]}
    assert changes.resend(db,write(tmp_path,'stale.json',stale))[0]['reason']=='stale_notice_version'
    valid={'version':'v1','rows':[{'id':'A1','batch':'N1','class':'C2','version':'v1','ack_token':'ack'}]}
    assert changes.resend(db,write(tmp_path,'retry.json',valid))[0]['status']=='已送达'
    assert connect(db).execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()[0]=='换班生效'

def test_cross_day_room_interval_is_a_real_overlap(tmp_path):
    db=tmp_path/'db';rows=[slot('L1',start='2026-01-01T23:30:00',end='2026-01-02T00:30:00'),
                          slot('L2',start='2026-01-02T00:10:00',end='2026-01-02T01:00:00')]
    assert [x['reason'] for x in operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':rows}))]==['room_available','room_overlap']

def test_notice_rejects_new_version_without_room_snapshot(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1')]}))
    operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':[
        {'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]}))
    result=operations.third(db,write(tmp_path,'notice.json',{'version':'v2','rows':[
        {'id':'N1','lesson':'L1','required_classes':['C1'],'receipts':[{'class':'C1','version':'v2'}]}]}))
    assert result[0]['reason']=='stale_schedule_version'

def test_resend_cannot_expand_frozen_class_scope(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'rooms.json',{'version':'v1','rows':[slot('L1')]}))
    operations.second(db,write(tmp_path,'staff.json',{'version':'v1','rows':[
        {'id':'S1','lesson':'L1','teacher':'T1','subject':'math','qualified':['math'],'busy':[],'accepted':True}]}))
    operations.third(db,write(tmp_path,'notice.json',{'version':'v1','rows':[
        {'id':'N1','lesson':'L1','required_classes':['C1'],'receipts':[]}]}))
    wrong={'version':'v1','rows':[{'id':'A1','batch':'N1','class':'C2','version':'v1','ack_token':'ack'}]}
    assert changes.resend(db,write(tmp_path,'wrong.json',wrong))[0]['reason']=='class_out_of_scope'
    assert connect(db).execute("SELECT status FROM notice_batch WHERE id='N1'").fetchone()[0]=='通知不全'
