"""Record independent classroom CO2 sensor readings."""

from .ingest import capture
from .store import connect, decide, save


def run(path, file):
    source, payload = capture(path, 'air_sensor', file)
    db = connect(path)
    output = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                room = row['room']
                value = float(row['ppm'])
                if value < 0:
                    raise ValueError('negative CO2 reading')
                status = 'elevated' if value > 1000 else 'normal'
                save(db, 'air_sample', key, source, payload['version'], row, status, 'single reading threshold')
                save(db, 'air_case', f'{room}:{key}', source, payload['version'],
                     {'room': room, 'sample_id': key, 'ppm': value}, status, 'single reading threshold')
                output.append(decide(db, 'air', f'{room}:{key}', status, 'single reading threshold', source))
        return output
    finally:
        db.close()
