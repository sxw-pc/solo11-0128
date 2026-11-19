"""Receive a simple authorized-person roster and pickup attempt."""

from .ingest import capture
from .store import connect, decide, save


def run(path, file):
    source, payload = capture(path, 'pickup_desk', file)
    db = connect(path)
    result = []
    try:
        with db:
            for row in payload.get('rows', []):
                child = str(row['child_id'])
                authorized = list(row['authorized_names'])
                person = row['person_name']
                if not authorized or not person:
                    raise ValueError('pickup identity required')
                save(db, 'pickup_roster', f'{child}:{payload["version"]}', source,
                     payload['version'], {'child_id': child, 'authorized_names': authorized},
                     'recorded', 'name roster')
                status = 'listed' if person in authorized else 'unlisted'
                save(db, 'pickup_attempt', str(row['id']), source, payload['version'],
                     row, status, 'name equality')
                result.append(decide(db, 'pickup', str(row['id']), status, 'name equality', source))
        return result
    finally:
        db.close()
