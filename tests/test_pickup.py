import json
from pathlib import Path

from schedulehandoff import pickup, report
from schedulehandoff.store import connect

ROOT = Path(__file__).resolve().parents[1]


def test_name_roster_pickup_intake(tmp_path):
    db_path = tmp_path / 'pickup.sqlite'
    fixture = ROOT / 'fixtures' / 'pickup-ok.json'
    assert pickup.run(db_path, fixture)[0]['status'] == 'listed'
    assert pickup.run(db_path, fixture)[0]['status'] == 'listed'
    view = json.loads(report.build(db_path, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(view['entities']['pickup_attempt']) == 1


def test_unlisted_person_is_not_listed(tmp_path):
    db_path = tmp_path / 'pickup.sqlite'
    data = {'version': 'v1', 'rows': [{'id': 'pickup-2', 'child_id': 'C1',
            'authorized_names': ['Parent A'], 'person_name': 'Visitor', 'at': '2026-01-01T10:00:00'}]}
    path = tmp_path / 'different.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert pickup.run(db_path, path)[0]['status'] == 'unlisted'
    assert connect(db_path).execute('SELECT COUNT(*) FROM pickup_attempt').fetchone()[0] == 1
