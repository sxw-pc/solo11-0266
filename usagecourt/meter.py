from .store import one,rows

SCHEMA='''
CREATE TABLE IF NOT EXISTS identities(id TEXT PRIMARY KEY,namespace TEXT,uid TEXT,tenant TEXT,generation INTEGER,cluster TEXT);
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,tenant TEXT,resource TEXT,cluster TEXT,start INTEGER,ends INTEGER,units INTEGER,source TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS outages(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,start INTEGER,ends INTEGER,cause TEXT,receipt TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS meter_cases(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,tenant TEXT,resource TEXT,sample TEXT,start INTEGER,ends INTEGER,quantity INTEGER,status TEXT);
'''

def handle(db,x):
    op=x['op']
    if op=='identity':
        old=one(db,'SELECT * FROM identities WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']:
            return {'decision':'stale_identity'}
        db.execute('INSERT INTO identities VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET namespace=excluded.namespace,uid=excluded.uid,tenant=excluded.tenant,generation=excluded.generation,cluster=excluded.cluster',(x['id'],x['namespace'],x['uid'],x['tenant'],x['generation'],x['cluster']))
        if old:
            db.execute("UPDATE meter_cases SET status='review' WHERE identity=? AND uid<>? AND status='sealed'",(x['id'],x['uid']))
            db.execute("UPDATE invoice_lines SET status='review' WHERE meter_case IN (SELECT id FROM meter_cases WHERE identity=? AND uid<>?) AND status='issued'",(x['id'],x['uid']))
            db.execute("UPDATE settlements SET status='review' WHERE invoice IN (SELECT invoice FROM invoice_lines WHERE meter_case IN (SELECT id FROM meter_cases WHERE identity=? AND uid<>?)) AND status='settled'",(x['id'],x['uid']))
        return {'decision':'identity_recorded'}
    if op=='sample':
        ident=one(db,'SELECT * FROM identities WHERE id=?',(x['identity'],))
        if not ident or (ident['uid'],ident['tenant'],ident['cluster'])!=(x['uid'],x['tenant'],x['cluster']) or x['start']>=x['ends'] or x['units']<=0 or x['resource'] not in ('cpu','memory'):
            return {'decision':'sample_denied'}
        conflict=one(db,"SELECT id FROM samples WHERE uid=? AND resource=? AND status='accepted' AND start<? AND ends>?",(x['uid'],x['resource'],x['ends'],x['start']))
        if conflict:return {'decision':'overlap_dispute','sample':conflict['id']}
        db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],x['identity'],x['uid'],x['tenant'],x['resource'],x['cluster'],x['start'],x['ends'],x['units'],x['source'],'accepted'))
        return {'decision':'sample_accepted'}
    if op=='outage':
        ident=one(db,'SELECT * FROM identities WHERE id=?',(x['identity'],))
        if not ident or ident['uid']!=x['uid'] or x['start']>=x['ends'] or x['cause']!='platform' or not x.get('receipt'):
            return {'decision':'outage_denied'}
        db.execute('INSERT INTO outages VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['identity'],x['uid'],x['start'],x['ends'],x['cause'],x['receipt'],'accepted'))
        db.execute("UPDATE meter_cases SET status='review' WHERE identity=? AND uid=? AND start<? AND ends>? AND status='sealed'",(x['identity'],x['uid'],x['ends'],x['start']))
        db.execute("UPDATE invoice_lines SET status='review' WHERE meter_case IN (SELECT id FROM meter_cases WHERE identity=? AND uid=? AND status='review') AND status='issued'",(x['identity'],x['uid']))
        db.execute("UPDATE settlements SET status='review' WHERE invoice IN (SELECT invoice FROM invoice_lines WHERE meter_case IN (SELECT id FROM meter_cases WHERE identity=? AND uid=? AND status='review')) AND status='settled'",(x['identity'],x['uid']))
        return {'decision':'outage_accepted'}
    if op=='seal':
        ident=one(db,'SELECT * FROM identities WHERE id=?',(x['identity'],))
        sample=one(db,'SELECT * FROM samples WHERE id=?',(x['sample'],))
        if not ident or not sample or sample['status']!='accepted' or sample['identity']!=x['identity'] or sample['uid']!=ident['uid'] or sample['tenant']!=ident['tenant'] or sample['resource']!=x['resource']:
            return {'decision':'identity_or_sample_changed'}
        if x['start']<sample['start'] or x['ends']>sample['ends'] or x['start']>=x['ends']:
            return {'decision':'evidence_gap'}
        quantity=(x['ends']-x['start'])*sample['units']
        db.execute('INSERT INTO meter_cases VALUES(?,?,?,?,?,?,?,?,?,?)',(x['id'],x['identity'],ident['uid'],ident['tenant'],x['resource'],x['sample'],x['start'],x['ends'],quantity,'sealed'))
        return {'decision':'meter_sealed','quantity':quantity}
    if op=='correct':
        sample=one(db,'SELECT * FROM samples WHERE id=?',(x['old'],))
        if not sample or sample['status']!='accepted' or x['units']<=0:return {'decision':'correction_denied'}
        db.execute("UPDATE samples SET status='corrected' WHERE id=?",(x['old'],))
        db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],sample['identity'],sample['uid'],sample['tenant'],sample['resource'],sample['cluster'],sample['start'],sample['ends'],x['units'],x['source'],'accepted'))
        db.execute("UPDATE meter_cases SET status='review' WHERE sample=? AND status='sealed'",(x['old'],))
        db.execute("UPDATE invoice_lines SET status='review' WHERE meter_case IN (SELECT id FROM meter_cases WHERE sample=?) AND status='issued'",(x['old'],))
        db.execute("UPDATE settlements SET status='review' WHERE invoice IN (SELECT invoice FROM invoice_lines WHERE meter_case IN (SELECT id FROM meter_cases WHERE sample=?)) AND status='settled'",(x['old'],))
        return {'decision':'evidence_corrected'}
    return {'decision':'unknown_operation'}
