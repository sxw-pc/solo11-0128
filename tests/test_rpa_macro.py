import json
from schedulehandoff import rpa_macro, rpa_execute


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_simulated_macro_recovery_entry(tmp_path):
    db = tmp_path / 'ledger.db'
    plan = {'version': 'v1', 'rows': [{'id': 'M1', 'project': 'P1', 'steps': [
        {'kind': 'open'}, {'kind': 'click'}, {'kind': 'verify'}]}]}
    assert rpa_macro.plan(db, write(tmp_path, 'macro.json', plan))[0]['status'] == '已计划'
    events = {'version': 'v1', 'rows': [{'id': 'E1', 'run': 'M1', 'events': [
        {'kind': 'publish_click', 'at': '2026-01-01T09:00:00'}]}]}
    assert rpa_execute.reconcile(db, write(tmp_path, 'events.json', events))[0]['status'] == '可重试'
