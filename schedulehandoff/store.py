import sqlite3
from pathlib import Path

EVIDENCE_TABLES = [
    'schedule_export', 'room_slot', 'teacher', 'substitution', 'notice_batch',
    'notice_receipt', 'withdrawal', 'notice_attempt', 'notice_delivery',
    'reschedule_plan', 'reschedule_assignment', 'reschedule_approval',
]
PICKUP_TABLES = [
    'pickup_student', 'pickup_guardian', 'pickup_contract', 'pickup_delegation',
    'pickup_revocation', 'pickup_session', 'pickup_assignment', 'pickup_credential',
    'pickup_action', 'pickup_identity_check', 'pickup_custody', 'pickup_window',
    'pickup_delivery', 'pickup_fork', 'pickup_conclusion', 'pickup_roster',
    'pickup_attempt',
]
DERIVED_TABLES = ['entity_history']
TABLES = EVIDENCE_TABLES + PICKUP_TABLES + DERIVED_TABLES

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(subsystem,subject,status,reason,source_id));
CREATE TABLE IF NOT EXISTS entity_history(id INTEGER PRIMARY KEY,table_name TEXT NOT NULL,entity_id TEXT NOT NULL,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,archived TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS schedule_export(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS room_slot(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS teacher(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS substitution(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_batch(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_receipt(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS withdrawal(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_attempt(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_delivery(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reschedule_plan(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reschedule_assignment(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reschedule_approval(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_student(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_guardian(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_contract(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_delegation(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_revocation(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_session(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_assignment(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_credential(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_action(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_identity_check(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_custody(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_window(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_delivery(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_fork(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_conclusion(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_roster(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_attempt(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
"""


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _archive(db, table, old):
    db.execute(
        'INSERT INTO entity_history(table_name,entity_id,source_id,version,body,status,reason) '
        'VALUES (?,?,?,?,?,?,?)',
        (table, old['id'], old['source_id'], old['version'], old['body'], old['status'], old['reason']))


def save(db, table, key, source_id, version, body, status, reason, allow_revision=False):
    """Persist one evidence entity.

    A different body under the same identity and version is an identity conflict.
    With ``allow_revision`` a different *version* supersedes the old row, but the
    old body is kept in ``entity_history`` instead of being erased.
    """
    import json
    if table not in TABLES:
        raise ValueError('unknown_table')
    if table == 'entity_history':
        raise ValueError('derived_table')
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
    old = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if old and old['body'] != encoded:
        if allow_revision:
            _archive(db, table, old)
        else:
            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                       (source_id, 'identity_conflict', f'{table}:{key}'))
            raise ValueError('identity_conflict')
    if old and old['body'] == encoded:
        # The same immutable fact arriving again (even under a later file
        # version) is a replay; keep the original capture untouched.
        return
    db.execute(
        f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET version=excluded.version,body=excluded.body,'
        'status=excluded.status,reason=excluded.reason',
        (key, source_id, version, encoded, status, reason))


def upsert(db, table, key, source_id, version, body, status, reason):
    """Persist derived state (current windows/forks) without identity conflicts.

    Derived state may change whenever new evidence or a corrected timetable
    arrives.  Revisions are archived so the report can still show history.
    """
    import json
    if table not in TABLES:
        raise ValueError('unknown_table')
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
    old = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if old and old['body'] != encoded:
        _archive(db, table, old)
    db.execute(
        f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
        'body=excluded.body,status=excluded.status,reason=excluded.reason',
        (key, source_id, version, encoded, status, reason))


def insert_once(db, table, key, source_id, version, body, status, reason):
    """Insert an immutable fact.  A replay never overwrites the original."""
    import json
    if table not in TABLES:
        raise ValueError('unknown_table')
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
    old = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if old:
        return old
    db.execute(
        f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?)',
        (key, source_id, version, encoded, status, reason))
    return db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()


def issue(db, source_id, code, detail):
    db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)', (source_id, code, detail))


def decide(db, subsystem, subject, status, reason, source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem, subject, status, reason, source_id))
    return {'subject': subject, 'status': status, 'reason': reason}


def entity(db, table, key):
    import json
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if not row:
        return None
    body = json.loads(row['body'])
    body.setdefault('id', row['id'])
    body['_status'] = row['status']
    body['_reason'] = row['reason']
    body['_version'] = row['version']
    body['_source'] = row['source_id']
    return body


def entities(db, table):
    import json
    out = []
    for row in db.execute(f'SELECT * FROM {table} ORDER BY id'):
        body = json.loads(row['body'])
        body.setdefault('id', row['id'])
        body['_status'] = row['status']
        body['_reason'] = row['reason']
        body['_version'] = row['version']
        body['_source'] = row['source_id']
        out.append(body)
    return out
