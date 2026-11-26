from .store import one,rows

SCHEMA='''
CREATE TABLE IF NOT EXISTS prices(id TEXT PRIMARY KEY,tenant TEXT,resource TEXT,generation INTEGER,start INTEGER,ends INTEGER,rate INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS discounts(id TEXT PRIMARY KEY,tenant TEXT,resource TEXT,threshold INTEGER,percent INTEGER,start INTEGER,ends INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS discount_approvals(id TEXT PRIMARY KEY,discount TEXT,actor TEXT,role TEXT,expires INTEGER);
'''

def handle(db,x):
    op=x['op']
    if op=='price':
        if x['start']>=x['ends'] or x['rate']<0 or x['resource'] not in ('cpu','memory'):
            return {'decision':'invalid_price'}
        previous=one(db,'SELECT MAX(generation) n FROM prices WHERE tenant=? AND resource=?',(x['tenant'],x['resource']))
        if previous['n'] is not None and x['generation']<=previous['n']:
            return {'decision':'stale_price'}
        overlap=one(db,'SELECT id FROM prices WHERE tenant=? AND resource=? AND start<? AND ends>? AND status=?',(x['tenant'],x['resource'],x['ends'],x['start'],'active'))
        if overlap:return {'decision':'overlapping_price','conflict':overlap['id']}
        db.execute('INSERT INTO prices VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['tenant'],x['resource'],x['generation'],x['start'],x['ends'],x['rate'],'active'))
        return {'decision':'price_active'}
    if op=='discount':
        if x['threshold']<1 or not 0<x['percent']<100 or x['start']>=x['ends']:
            return {'decision':'invalid_discount'}
        db.execute('INSERT INTO discounts VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['tenant'],x['resource'],x['threshold'],x['percent'],x['start'],x['ends'],'pending'))
        return {'decision':'discount_pending'}
    if op=='approve':
        d=one(db,'SELECT * FROM discounts WHERE id=?',(x['discount'],))
        if not d or d['status']!='pending' or x['role'] not in ('finance','owner') or x['expires']<d['ends']:
            return {'decision':'approval_denied'}
        db.execute('INSERT INTO discount_approvals VALUES(?,?,?,?,?)',(x['id'],x['discount'],x['actor'],x['role'],x['expires']))
        votes=rows(db,'SELECT actor,role FROM discount_approvals WHERE discount=?',(x['discount'],))
        if len({v['role'] for v in votes})==2 and len({v['actor'] for v in votes})==2:
            db.execute("UPDATE discounts SET status='active' WHERE id=?",(x['discount'],))
            return {'decision':'discount_active'}
        return {'decision':'approval_recorded'}
    if op=='revoke_discount':
        d=one(db,'SELECT * FROM discounts WHERE id=?',(x['id'],))
        if not d or d['status']!='active':return {'decision':'not_active'}
        db.execute("UPDATE discounts SET status='revoked' WHERE id=?",(x['id'],))
        db.execute("UPDATE invoice_lines SET status='review' WHERE discount=? AND status='issued'",(x['id'],))
        db.execute("UPDATE settlements SET status='review' WHERE invoice IN (SELECT invoice FROM invoice_lines WHERE discount=? AND status='review') AND status='settled'",(x['id'],))
        return {'decision':'discount_revoked'}
    return {'decision':'unknown_operation'}
