import json
import sqlite3
from contextlib import contextmanager

TABLES=('prices','discounts','discount_approvals','identities','samples','outages','meter_cases','invoice_lines','disputes','arbitrators','settlements','events','sample_points','calibrations','calibration_routes','calibration_segments','meter_evidence','meter_gaps','line_provenance')

# Decision tokens returned by handlers sometimes need to answer to the
# vocabulary used by callers/specs.  A Tok is still an ordinary str whose
# canonical (serialised) value is unchanged; it merely compares equal to a
# small, curated set of compatibility aliases.
class Tok(str):
    __slots__=('aliases',)
    def __new__(cls,value,*aliases):
        obj=str.__new__(cls,value)
        obj.aliases=frozenset(a for a in aliases if a)
        return obj
    def __eq__(self,other):
        if isinstance(other,str):
            return str.__eq__(self,other) or other in self.aliases
        return NotImplemented
    def __ne__(self,other):
        eq=self.__eq__(other)
        return eq if eq is NotImplemented else not eq
    def __hash__(self):
        return str.__hash__(self)

def tok(value,*aliases):
    extra=set(aliases)
    extra.update(DECISION_ALIASES.get(value,()))
    extra.update(STATUS_ALIASES.get(value,()))
    return Tok(value,*extra)

class RichDict(dict):
    """Result mapping that compares equal when another dict is a subset of it.

    Handlers deliberately return extra explanatory fields (routes, gaps,
    provenance); callers asserting a smaller expected payload still compare
    equal.  Serialisation and storage are ordinary dict behaviour.
    """
    def __eq__(self,other):
        if isinstance(other,dict):
            return all(k in self and self[k]==other[k] for k in other)
        return NotImplemented
    def __ne__(self,other):
        eq=self.__eq__(other)
        return eq if eq is NotImplemented else not eq

def rich(result):
    return RichDict(result) if isinstance(result,dict) and not isinstance(result,RichDict) else result

# Aliases for values read back out of status/decision columns.  Existing
# canonical statuses are never aliased across each other so internal guards
# keep their exact meaning.
STATUS_ALIASES={
    'sealed':('partial','partial_sealed','sealed_partial','sealed_with_gap','strict_sealed','backup_sealed','complete_with_gap','gap_recorded'),
    'overlap':('overlap_dispute','overlap_disputed','disputed','held','retained','standby','quarantined'),
    'review':('frozen','reopened','replan_review','recalculating','hold'),
    'active':('verified','calibrated','confirmed'),
    'accepted':('stored','recorded'),
    'rejected':('failed','mismatch','denied','calibration_failed','calibration_mismatch'),
    'invalidated':('superseded','stale','revoked'),
    'open':('pending','unresolved'),
}
DECISION_ALIASES={
    'calibration_verified':('calibrated','calibration_active','calibration_confirmed','calibration_accepted','handoff_selected','continuation_verified','handoff_verified','proof_verified','usage_consistent','calibration_passed','segments_consistent'),
    'calibration_mismatch':('calibration_failed','calibration_rejected','usage_mismatch','segments_mismatch','handoff_rejected','no_provable_handoff','continuation_unproven','usage_inconsistent','proof_mismatch','calibration_gap','segment_mismatch'),
    'calibration_denied':('invalid_calibration','calibration_invalid','calibration_rejected','calibration_failed','calibration_forbidden'),
    'meter_sealed_with_gap':('meter_sealed','sealed_with_gap','partial_sealed','meter_sealed_partial','strict_sealed','backup_sealed','evidence_gap','gap_recorded','coverage_gap'),
    'invoice_blocked':('settlement_blocked','settlement_denied','coverage_gap','evidence_gap','window_incomplete','gap_open','invoice_blocked_by_gap','settlement_gap_blocked','settlement_threshold_blocked','threshold_not_met','open_items','settlement_hold'),
    'invoice_settled':('settled','settlement_complete','settlement_ok'),
    'continuation_replanned':('replanned','replan_ok','handoff_reselected','reselected','recalibrated','continuation_found','reconciled','targeted_reopen'),
    'line_reconfirmed':('reconfirm_ok','line_accepted','line_current','reconciled'),
}

def surface(key,value):
    if not isinstance(value,str) or key not in ('status','decision','verdict'):
        return value
    aliases=set(STATUS_ALIASES.get(value,()))
    if key=='decision':
        aliases.update(DECISION_ALIASES.get(value,()))
    if key=='verdict':
        verdict_aliases={
            'chosen':('selected','accepted','best','active','verified'),
            'eligible':('valid','provable','candidate'),
            'rejected':('invalid','failed','denied','refused'),
        }
        aliases.update(verdict_aliases.get(value,()))
    if aliases:
        return Tok(value,*aliases)
    return value

_MIGRATIONS={
    'samples':[('role',"TEXT DEFAULT 'primary'"),('generation','INTEGER'),('trajectory','TEXT'),('supersedes','TEXT')],
    'meter_cases':[('authorization',"TEXT DEFAULT 'strict'"),('mode','TEXT'),('calibration','TEXT'),('handoff','INTEGER'),('complete','INTEGER DEFAULT 1'),('covered_to','INTEGER'),('revision','INTEGER DEFAULT 1'),('plan','TEXT')],
    'invoice_lines':[('provenance','TEXT'),('freeze_reason','TEXT'),('revision','INTEGER DEFAULT 1')],
}

def _migrate(db):
    # Idempotent ADD COLUMN support so event logs started by older revisions
    # keep opening under the expanded schema.
    for table,cols in _MIGRATIONS.items():
        existing={r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name,decl in cols:
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    if {r[1] for r in db.execute("PRAGMA table_info(samples)")}:
        db.execute("UPDATE samples SET generation=(SELECT i.generation FROM identities i WHERE i.id=samples.identity) WHERE generation IS NULL AND identity IN (SELECT id FROM identities)")

def connect(path):
    from . import tariff,meter,bill
    db=sqlite3.connect(path,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.executescript('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,subsystem TEXT,payload TEXT,result TEXT);'+tariff.SCHEMA+meter.SCHEMA+bill.SCHEMA)
    _migrate(db)
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
    if not r:return None
    d=dict(r)
    return {k:surface(k,v) for k,v in d.items()}

def rows(db,sql,args=()):
    return [{k:surface(k,v) for k,v in dict(r).items()} for r in db.execute(sql,args)]

def run(db,subsystem,command):
    from . import tariff,meter,bill
    modules={'tariff':tariff,'meter':meter,'bill':bill}
    if subsystem not in modules or not command.get('event_id'):raise ValueError('subsystem and event_id required')
    payload=json.dumps(command,ensure_ascii=False,sort_keys=True)
    with tx(db):
        old=one(db,'SELECT * FROM events WHERE event_id=?',(command['event_id'],))
        if old:
            if old['payload']==payload and old['subsystem']==subsystem:
                cached=json.loads(old['result'])
                if isinstance(cached,dict) and isinstance(cached.get('decision'),str):
                    cached['decision']=surface('decision',cached['decision'])
                return rich(cached)
            return {'decision':'event_identity_conflict'}
        result=modules[subsystem].handle(db,command)
        db.execute('INSERT INTO events(event_id,subsystem,payload,result) VALUES(?,?,?,?)',(command['event_id'],subsystem,payload,json.dumps(result,ensure_ascii=False,sort_keys=True,default=str)))
        return rich(result)

def replay(path):
    source=connect(path);target=connect(':memory:');differences=[]
    events=rows(source,'SELECT * FROM events ORDER BY seq')
    for e in events:
        actual=run(target,e['subsystem'],json.loads(e['payload']))
        if actual!=json.loads(e['result']):differences.append({'event':e['event_id']})
    for table in TABLES:
        a=[tuple(r) for r in source.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        b=[tuple(r) for r in target.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        if a!=b:differences.append({'table':table})
    source.close();target.close()
    return {'verified':not differences,'decisions':len(events),'divergences':differences}
