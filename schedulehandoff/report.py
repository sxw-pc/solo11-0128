import hashlib,json
from pathlib import Path
from .store import connect,TABLES,loads

EQUIPMENT_TABLES=['equipment_kit','custody_contract','equipment_handoff',
                  'equipment_signature','equipment_scan','serial_chain',
                  'liability_case','liability_notice','adjustment_order']


def _body(row):
    return row['body'] if isinstance(row['body'], dict) else loads(row['body'])


def _explain(chain):
    body = _body(chain)
    serial = body['serial']
    status, reason = body['status'], body['reason']
    chains = body.get('chains', [])
    holders = body.get('possible_holders', [])
    lines = [f"序列号 {serial}（借用单 {body['loan_id']}）当前结论：{status}（{reason}）。"]
    lines.append(f"与已签交接、站点和时间范围相容的保管链共 {len(chains)} 条；"
                 f"末端相容持有人：{holders or '无'}。")
    for ev in body.get('excluded_evidence', []):
        if ev['kind'] == 'unsigned_handoff':
            lines.append(f"交接 {ev['ref']}（{ev.get('giver')}→{ev.get('receiver')}）"
                         f"缺少签收，不能转移保管责任。")
        elif ev['kind'] == 'duplicate_scan':
            lines.append(f"站点 {ev.get('station')} 的重复扫码 {ev['ref']} 只计一次。")
    if status == '责任确定':
        lines.append(f"依据借出时固定的协议，赔偿责任人为：{body.get('liable', [])}。")
    elif status == '责任待核':
        need = body.get('minimum_evidence') or []
        if need:
            lines.append('责任待核，最少还需以下证据才能区分：'
                         + '；'.join(x.get('need', json.dumps(x, ensure_ascii=False)) for x in need) + '。')
        else:
            lines.append('责任待核：存在多条把缺件放在不同持有人手中的相容链，'
                         '不能把最后一条扫码的人直接记为赔偿人。')
    elif status == '证据冲突':
        core = body.get('conflict_core') or {}
        refs = [e.get('ref') for e in core.get('events', [])]
        lines.append('无可行保管链；最小冲突证据：' + '、'.join(refs) + '。'
                     + '；'.join(core.get('contradictions', [])))
    elif status == '已归还':
        lines.append('归还站点扫码已独立结清该序列号，同一扫码不会重复结清其他借用单。')
    return '\n'.join(lines)


def _equipment_section(db):
    section = {}
    for table in EQUIPMENT_TABLES:
        section[table] = [dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        for row in section[table]:
            row['body'] = loads(row['body'])
    section['serial_explanations'] = [
        {'serial': _body(x)['serial'], 'loan_id': _body(x)['loan_id'],
         'status': x['status'], 'reason': x['reason'], 'narrative': _explain(x)}
        for x in section['serial_chain']]
    section['case_versions'] = {}
    for case in section['liability_case']:
        body = _body(case)
        section['case_versions'].setdefault(f"{body['loan_id']}:{body['serial']}", []).append(
            {'decision_seq': body.get('decision_seq'), 'status': case['status'],
             'reason': case['reason'], 'liable': body.get('liable', []),
             'supersedes': body.get('supersedes'),
             'minimum_evidence': body.get('minimum_evidence', [])})
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
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
              'equipment':_equipment_section(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
