import json
import sqlite3
from contextlib import contextmanager

TABLES=('prices','discounts','discount_approvals','identities','samples','outages','meter_cases','invoice_lines','disputes','arbitrators','settlements','events')

def connect(path):
    from . import tariff,meter,bill,carbon,quota,license,inspection,appliance,facility
    db=sqlite3.connect(path,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.executescript('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,subsystem TEXT,payload TEXT,result TEXT);'+tariff.SCHEMA+meter.SCHEMA+bill.SCHEMA+carbon.SCHEMA+quota.SCHEMA+license.SCHEMA+inspection.SCHEMA+appliance.SCHEMA+facility.SCHEMA)
    return db

@contextmanager
def tx(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise

def one(db,sql,args=()):
    r=db.execute(sql,args).fetchone()
    return dict(r) if r else None

def rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args)]

def run(db,subsystem,command):
    from . import tariff,meter,bill,carbon,quota,license,inspection,appliance,facility
    modules={'tariff':tariff,'meter':meter,'bill':bill,'carbon':carbon,'quota':quota,'license':license,'inspection':inspection,'appliance':appliance,'facility':facility}
    if subsystem not in modules or not command.get('event_id'):raise ValueError('subsystem and event_id required')
    payload=json.dumps(command,ensure_ascii=False,sort_keys=True)
    with tx(db):
        old=one(db,'SELECT * FROM events WHERE event_id=?',(command['event_id'],))
        if old:return json.loads(old['result']) if old['payload']==payload and old['subsystem']==subsystem else {'decision':'event_identity_conflict'}
        result=modules[subsystem].handle(db,command)
        db.execute('INSERT INTO events(event_id,subsystem,payload,result) VALUES(?,?,?,?)',(command['event_id'],subsystem,payload,json.dumps(result,ensure_ascii=False,sort_keys=True)))
        return result

def replay(path):
    from . import carbon,quota,license,inspection,appliance,facility
    source=connect(path);target=connect(':memory:');differences=[]
    events=rows(source,'SELECT * FROM events ORDER BY seq')
    for e in events:
        actual=run(target,e['subsystem'],json.loads(e['payload']))
        if actual!=json.loads(e['result']):differences.append({'event':e['event_id']})
    for table in TABLES+carbon.TABLES+quota.TABLES+license.TABLES+inspection.TABLES+appliance.TABLES+facility.TABLES:
        a=[tuple(r) for r in source.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        b=[tuple(r) for r in target.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        if a!=b:differences.append({'table':table})
    source.close();target.close()
    return {'verified':not differences,'decisions':len(events),'divergences':differences}
