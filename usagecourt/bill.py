from .store import one,rows

SCHEMA='''
CREATE TABLE IF NOT EXISTS invoice_lines(id TEXT PRIMARY KEY,invoice TEXT,meter_case TEXT,tenant TEXT,resource TEXT,start INTEGER,ends INTEGER,quantity INTEGER,rate INTEGER,amount INTEGER,price TEXT,discount TEXT,status TEXT,prior_id TEXT);
CREATE TABLE IF NOT EXISTS disputes(id TEXT PRIMARY KEY,line TEXT,tenant TEXT,reason TEXT,evidence TEXT,claim_amount INTEGER,status TEXT,resolution TEXT);
CREATE TABLE IF NOT EXISTS arbitrators(id TEXT PRIMARY KEY,tenant TEXT,actor TEXT,role TEXT,generation INTEGER,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS settlements(id TEXT PRIMARY KEY,invoice TEXT,tenant TEXT,total INTEGER,at INTEGER,status TEXT);
'''

def union_duration(intervals):
    merged=[]
    for start,ends in sorted(intervals):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],ends))
        else:merged.append((start,ends))
    return sum(b-a for a,b in merged)

def segments(db,case):
    prices=rows(db,"SELECT * FROM prices WHERE tenant=? AND resource=? AND status='active' AND start<? AND ends>?",(case['tenant'],case['resource'],case['ends'],case['start']))
    boundaries={case['start'],case['ends']}
    for p in prices:boundaries.update((max(p['start'],case['start']),min(p['ends'],case['ends'])))
    out=[]
    bounds=sorted(boundaries)
    sample=one(db,'SELECT * FROM samples WHERE id=?',(case['sample'],))
    outages=rows(db,"SELECT * FROM outages WHERE identity=? AND uid=? AND status='accepted' AND start<? AND ends>?",(case['identity'],case['uid'],case['ends'],case['start']))
    for start,ends in zip(bounds,bounds[1:]):
        matches=[p for p in prices if p['start']<=start and p['ends']>=ends]
        if len(matches)!=1:return None
        pauses=[(max(start,o['start']),min(ends,o['ends'])) for o in outages if o['start']<ends and o['ends']>start]
        duration=ends-start-union_duration(pauses)
        quantity=duration*sample['units']
        offers=rows(db,"SELECT * FROM discounts WHERE tenant=? AND resource=? AND status='active' AND start<=? AND ends>=? AND threshold<=? ORDER BY percent DESC",(case['tenant'],case['resource'],start,ends,case['quantity']))
        discount=offers[0] if offers else None
        rate=matches[0]['rate']
        amount=quantity*rate*(100-(discount['percent'] if discount else 0))//100
        out.append({'start':start,'ends':ends,'quantity':quantity,'rate':rate,'amount':amount,'price':matches[0]['id'],'discount':discount['id'] if discount else None})
    return out

def handle(db,x):
    op=x['op']
    if op=='issue':
        case=one(db,'SELECT * FROM meter_cases WHERE id=?',(x['case'],))
        if not case or case['status']!='sealed' or case['tenant']!=x['tenant']:
            return {'decision':'meter_unavailable'}
        if one(db,'SELECT 1 FROM invoice_lines WHERE invoice=?',(x['invoice'],)):
            return {'decision':'invoice_identity_conflict'}
        if one(db,'SELECT 1 FROM invoice_lines WHERE meter_case=? AND prior_id IS NULL',(case['id'],)):
            return {'decision':'meter_already_billed'}
        parts=segments(db,case)
        if parts is None:return {'decision':'price_gap_or_conflict'}
        for i,p in enumerate(parts,1):
            db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(f"{x['invoice']}:{i}",x['invoice'],case['id'],case['tenant'],case['resource'],p['start'],p['ends'],p['quantity'],p['rate'],p['amount'],p['price'],p['discount'],'issued',None))
        return {'decision':'invoice_issued','lines':len(parts),'total':sum(p['amount'] for p in parts)}
    if op=='arbitrator':
        old=one(db,'SELECT * FROM arbitrators WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']:return {'decision':'stale_arbitrator'}
        if x['role']!='auditor' or x['expires']<=x['at']:return {'decision':'invalid_arbitrator'}
        db.execute('INSERT INTO arbitrators VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET tenant=excluded.tenant,actor=excluded.actor,role=excluded.role,generation=excluded.generation,expires=excluded.expires,status=excluded.status',(x['id'],x['tenant'],x['actor'],x['role'],x['generation'],x['expires'],'active'))
        return {'decision':'arbitrator_active'}
    if op=='dispute':
        line=one(db,'SELECT * FROM invoice_lines WHERE id=?',(x['line'],))
        if not line or line['tenant']!=x['tenant'] or line['status'] not in ('issued','review') or not x.get('evidence'):
            return {'decision':'dispute_denied'}
        db.execute('INSERT INTO disputes VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['line'],x['tenant'],x['reason'],x['evidence'],x['claim_amount'],'open',None))
        db.execute("UPDATE invoice_lines SET status='disputed' WHERE id=?",(x['line'],))
        db.execute("UPDATE settlements SET status='review' WHERE invoice=? AND status='settled'",(line['invoice'],))
        return {'decision':'line_frozen'}
    if op=='resolve':
        d=one(db,'SELECT * FROM disputes WHERE id=?',(x['dispute'],))
        line=one(db,'SELECT * FROM invoice_lines WHERE id=?',(d['line'],)) if d else None
        arb=one(db,'SELECT * FROM arbitrators WHERE id=?',(x['arbitrator'],))
        if not d or d['status']!='open' or not line or line['status']!='disputed' or not arb or arb['status']!='active' or arb['tenant']!=d['tenant'] or arb['actor']!=x['actor'] or arb['expires']<x['at'] or x['approved_amount']<0:
            return {'decision':'resolution_denied'}
        case=one(db,'SELECT * FROM meter_cases WHERE id=?',(line['meter_case'],))
        parts=segments(db,case) if case else None
        current=next((p for p in parts if p['start']==line['start'] and p['ends']==line['ends']),None) if parts else None
        if not current or x['approved_amount']!=d['claim_amount'] or x['approved_amount']!=current['amount']:
            return {'decision':'resolution_unsubstantiated'}
        if current['amount']!=line['amount'] and not one(db,"SELECT 1 FROM outages WHERE identity=? AND uid=? AND status='accepted' AND receipt=? AND start<? AND ends>?",(case['identity'],case['uid'],d['evidence'],line['ends'],line['start'])):
            return {'decision':'resolution_evidence_unmatched'}
        reverse=f"{x['id']}:reverse";replacement=f"{x['id']}:new"
        db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(reverse,line['invoice'],line['meter_case'],line['tenant'],line['resource'],line['start'],line['ends'],-line['quantity'],line['rate'],-line['amount'],line['price'],line['discount'],'reversal',line['id']))
        db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(replacement,line['invoice'],line['meter_case'],line['tenant'],line['resource'],line['start'],line['ends'],current['quantity'],current['rate'],current['amount'],current['price'],current['discount'],'issued',line['id']))
        db.execute("UPDATE invoice_lines SET status='reversed' WHERE id=?",(line['id'],))
        db.execute("UPDATE disputes SET status='resolved',resolution=? WHERE id=?",(x['id'],d['id']))
        return {'decision':'line_reissued','difference':x['approved_amount']-line['amount']}
    if op=='settle':
        lines=rows(db,'SELECT * FROM invoice_lines WHERE invoice=? AND tenant=?',(x['invoice'],x['tenant']))
        if not lines or any(l['status'] in ('disputed','review') for l in lines):return {'decision':'invoice_blocked'}
        if one(db,"SELECT 1 FROM settlements WHERE invoice=? AND status='settled'",(x['invoice'],)):
            return {'decision':'already_settled'}
        total=sum(l['amount'] for l in lines)
        db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?)',(x['id'],x['invoice'],x['tenant'],total,x['at'],'settled'))
        return {'decision':'invoice_settled','total':total}
    return {'decision':'unknown_operation'}
