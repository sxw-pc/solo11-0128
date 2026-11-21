import json
import sqlite3
from pathlib import Path
TABLES=['schedule_export', 'room_slot', 'teacher', 'substitution', 'notice_batch', 'notice_receipt', 'withdrawal', 'notice_attempt', 'notice_delivery', 'reschedule_plan', 'reschedule_assignment', 'reschedule_approval', 'automation_run', 'automation_recovery', 'automation_lease']
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(subsystem,subject,status,reason,source_id));
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
CREATE TABLE IF NOT EXISTS automation_run(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_recovery(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_lease(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    # BEGIN IMMEDIATE at the first write: the write lock is taken atomically with
    # the first mutation, so two recovery processes serialize instead of both
    # passing the same stale-version check (or deadlocking on lock upgrade).
    db.isolation_level='IMMEDIATE'
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def save(db,table,key,source_id,version,body,status,reason):
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if old and (old['version'],old['body'])!=(version,encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,'identity_conflict',f'{table}:{key}'))
        raise ValueError('identity_conflict')
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key,source_id,version,encoded,status,reason))
def acquire_lease(db,project,run,operator,recovery,source_id,version):
    """Take the exclusive local operation lease for a project, atomically.

    Returns (acquired, holder_body).  The INSERT itself takes the database write
    lock, so two automation processes recovering at the same time cannot both
    pass the same old-version check and publish twice.  Re-entrant for the same
    run and operator so replaying the same recovery evaluation stays idempotent;
    any other holder means the recovery must stop for manual review.
    """
    body={'project':project,'run':run,'operator':operator,'recovery':recovery}
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    cursor=db.execute('INSERT INTO automation_lease(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                      (project,source_id,version,encoded,'持有中','controlled_retry_authorized'))
    if cursor.rowcount==1:return True,body
    held=json.loads(db.execute('SELECT body FROM automation_lease WHERE id=?',(project,)).fetchone()['body'])
    return held.get('operator')==operator and held.get('run')==run,held
def decide(db,subsystem,subject,status,reason,source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem,subject,status,reason,source_id))
    return {'subject':subject,'status':status,'reason':reason}
