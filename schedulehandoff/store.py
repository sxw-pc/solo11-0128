import sqlite3
from pathlib import Path
TABLES=['schedule_export', 'room_slot', 'teacher', 'substitution', 'notice_batch', 'notice_receipt', 'withdrawal', 'notice_attempt', 'notice_delivery', 'reschedule_plan', 'reschedule_assignment', 'reschedule_approval', 'schedule_correction', 'power_meter', 'power_case', 'power_topology', 'power_feeder', 'power_breaker', 'power_tie_switch', 'power_room_load', 'power_measurement', 'power_service', 'power_fault', 'power_switching_plan', 'power_switching_execution', 'power_switching_proof', 'power_correction']
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
CREATE TABLE IF NOT EXISTS power_meter(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_case(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_correction(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
-- Distributions: one row per imported topology snapshot.  The payload carries
-- feeder/phase allocation, breakers, tie switches and room loads.
CREATE TABLE IF NOT EXISTS power_topology(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_feeder(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_breaker(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_tie_switch(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_room_load(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
-- Raw measurements with provenance.  Re-imports are versioned; same identity
-- with different content is an identity_conflict issue.
CREATE TABLE IF NOT EXISTS power_measurement(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_service(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_fault(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_switching_plan(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_switching_execution(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_switching_proof(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS power_correction(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def save(db,table,key,source_id,version,body,status,reason):
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if old and (old['version'],old['body'])!=(version,encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,'identity_conflict',f'{table}:{key}'))
        raise ValueError('identity_conflict')
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key,source_id,version,encoded,status,reason))


class IdentityConflict(ValueError):
    """Same identity and version re-imported with different content."""

    def __init__(self, table, key, source_id=None):
        super().__init__('identity_conflict')
        self.table = table
        self.key = key
        self.source_id = source_id


def keyed_save(db,table,base_key,version,body,status,reason,source_id=None,
               recomputable=False):
    """Version-aware save used by the power subsystem.

    The current row lives under ``base_key``; every older revision is kept in
    ``<table>_history`` keyed by version.  Re-importing the *same* version with
    *different* content is an ``identity_conflict`` for raw evidence (the same
    source must not rewrite history).  A *derived* result such as a switching
    proof may be legitimately recomputed when new evidence arrives; pass
    ``recomputable=True`` to archive the superseded conclusion instead of
    raising, while still keeping it in the report history.
    """
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    row=db.execute(f'SELECT * FROM {table} WHERE id=?',(base_key,)).fetchone()
    if row and row['version']==version and row['body']!=encoded and not recomputable:
        # persist the conflict before raising, so the report keeps evidence
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                   (source_id,'identity_conflict',f'{table}:{base_key}@{version}'))
        db.commit()
        raise IdentityConflict(table,base_key,source_id)
    if row and (row['version'],row['body'])==(version,encoded):
        db.execute(f'UPDATE {table} SET status=?,reason=? WHERE id=?',(status,reason,base_key))
        return
    db.execute('CREATE TABLE IF NOT EXISTS '+table+'_history (id TEXT PRIMARY KEY,source_id INT,version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL)')
    # A recomputed conclusion must not overwrite the archived previous body
    # under the same version; key its history row with a content suffix.
    hist_key = base_key+':'+version
    if row and row['version']==version and recomputable:
        import hashlib
        hist_key = base_key+':'+version+':'+hashlib.sha256(encoded.encode()).hexdigest()[:12]
    if row:
        db.execute('INSERT OR IGNORE INTO '+table+"_history(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?)",
                   (base_key+':'+row['version'],row['source_id'],row['version'],row['body'],row['status'],row['reason']))
        db.execute('INSERT OR IGNORE INTO '+table+"_history(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?)",
                   (hist_key,source_id,version,encoded,status,reason))
    else:
        db.execute('INSERT OR IGNORE INTO '+table+"_history(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?)",
                   (hist_key,source_id,version,encoded,status,reason))
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,body=excluded.body,status=excluded.status,reason=excluded.reason',
               (base_key,source_id,version,encoded,status,reason))


def history(db,table,base_key):
    """Return every stored revision of ``base_key`` oldest first."""
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    rows=[dict(x) for x in db.execute(f'SELECT * FROM {table} WHERE id=?',(base_key,))]
    try:
        rows+= [dict(x) for x in db.execute(f'SELECT * FROM {table}_history WHERE id LIKE ?',(base_key+':%',))]
    except Exception:
        pass
    for x in rows:x['body']=json.loads(x['body'])
    return sorted(rows,key=lambda x:(x['version'],x['id']))

def decide(db,subsystem,subject,status,reason,source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem,subject,status,reason,source_id))
    return {'subject':subject,'status':status,'reason':reason}

