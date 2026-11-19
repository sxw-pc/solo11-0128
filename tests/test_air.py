import json
from pathlib import Path

from schedulehandoff import air, report
from schedulehandoff.store import connect

ROOT = Path(__file__).resolve().parents[1]


def test_single_reading_air_intake(tmp_path):
    db_path = tmp_path / 'air.sqlite'
    source = ROOT / 'fixtures' / 'air-ok.json'
    output = air.run(db_path, source)
    assert [row['status'] for row in output] == ['normal', 'elevated']
    assert air.run(db_path, source) == output
    view = json.loads(report.build(db_path, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(view['entities']['air_sample']) == 2
    assert connect(db_path).execute('SELECT COUNT(*) FROM export_file').fetchone()[0] == 1


def test_invalid_air_reading_does_not_write_case(tmp_path):
    db_path = tmp_path / 'air.sqlite'
    payload = {'version': 'v1', 'rows': [{'id': 'bad', 'room': 'R1', 'ppm': -2}]}
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    try:
        air.run(db_path, path)
    except ValueError:
        pass
    else:
        raise AssertionError('negative reading accepted')
    assert connect(db_path).execute('SELECT COUNT(*) FROM air_case').fetchone()[0] == 0
