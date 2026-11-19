import json
from pathlib import Path

from schedulehandoff import power, report
from schedulehandoff.store import connect

ROOT = Path(__file__).resolve().parents[1]


def test_single_circuit_power_readings(tmp_path):
    db_path = tmp_path / 'power.sqlite'
    fixture = ROOT / 'fixtures' / 'power-ok.json'
    assert [row['status'] for row in power.run(db_path, fixture)] == ['within_limit', 'over_limit']
    assert len(power.run(db_path, fixture)) == 2
    view = json.loads(report.build(db_path, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(view['entities']['power_meter']) == 2


def test_invalid_current_rolls_back_cases(tmp_path):
    db_path = tmp_path / 'power.sqlite'
    payload = {'version': 'v1', 'rows': [{'id': 'bad', 'room': 'R1', 'circuit': 'C1',
               'amps': -1, 'rated_amp': 10}]}
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    try:
        power.run(db_path, path)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid reading accepted')
    assert connect(db_path).execute('SELECT COUNT(*) FROM power_case').fetchone()[0] == 0
