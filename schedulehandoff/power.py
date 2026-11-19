"""Receive classroom circuit current readings and simple local limits."""

from .ingest import capture
from .store import connect, decide, save


def run(path, file):
    source, payload = capture(path, 'power_meter', file)
    db = connect(path)
    result = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                amps = float(row['amps'])
                limit = float(row['rated_amp'])
                if amps < 0 or limit <= 0:
                    raise ValueError('invalid circuit current')
                status = 'within_limit' if amps <= limit else 'over_limit'
                save(db, 'power_meter', key, source, payload['version'], row, status,
                     'single circuit current limit')
                save(db, 'power_case', key, source, payload['version'],
                     {'circuit': row['circuit'], 'room': row['room'], 'amps': amps},
                     status, 'single circuit current limit')
                result.append(decide(db, 'power', key, status,
                                     'single circuit current limit', source))
        return result
    finally:
        db.close()
