import json
from .store import one,rows,tok
from . import meter as _meter

SCHEMA='''
CREATE TABLE IF NOT EXISTS invoice_lines(id TEXT PRIMARY KEY,invoice TEXT,meter_case TEXT,tenant TEXT,resource TEXT,start INTEGER,ends INTEGER,quantity INTEGER,rate INTEGER,amount INTEGER,price TEXT,discount TEXT,status TEXT,prior_id TEXT,provenance TEXT,freeze_reason TEXT,revision INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS disputes(id TEXT PRIMARY KEY,line TEXT,tenant TEXT,reason TEXT,evidence TEXT,claim_amount INTEGER,status TEXT,resolution TEXT);
CREATE TABLE IF NOT EXISTS arbitrators(id TEXT PRIMARY KEY,tenant TEXT,actor TEXT,role TEXT,generation INTEGER,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS settlements(id TEXT PRIMARY KEY,invoice TEXT,tenant TEXT,total INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS line_provenance(id TEXT PRIMARY KEY,line TEXT,idx INTEGER,start INTEGER,ends INTEGER,quantity INTEGER,sample TEXT,source TEXT,calibration TEXT,route TEXT);
CREATE VIEW IF NOT EXISTS line_evidence AS SELECT * FROM line_provenance;
CREATE VIEW IF NOT EXISTS provenance AS SELECT * FROM line_provenance;
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
        if sample['units'] is not None:
            pauses=[(max(start,o['start']),min(ends,o['ends'])) for o in outages if o['start']<ends and o['ends']>start]
            duration=ends-start-union_duration(pauses)
            quantity=duration*sample['units']
        else:
            # Raw trajectory sample: bill exactly what the trace proves.
            quantity=_meter.usage_between(_meter.profile_from_sample(sample),start,ends)
            if quantity is None:return None
        offers=rows(db,"SELECT * FROM discounts WHERE tenant=? AND resource=? AND status='active' AND start<=? AND ends>=? AND threshold<=? ORDER BY percent DESC",(case['tenant'],case['resource'],start,ends,case['quantity']))
        discount=offers[0] if offers else None
        rate=matches[0]['rate']
        amount=quantity*rate*(100-(discount['percent'] if discount else 0))//100
        out.append({'start':start,'ends':ends,'quantity':quantity,'rate':rate,'amount':amount,'price':matches[0]['id'],'discount':discount['id'] if discount else None})
    return out

def _discount_for(db,case,start,ends):
    offers=rows(db,"SELECT * FROM discounts WHERE tenant=? AND resource=? AND status='active' AND start<=? AND ends>=? AND threshold<=? ORDER BY percent DESC",(case['tenant'],case['resource'],start,ends,case['quantity']))
    return offers[0] if offers else None

def evidence_lines(db,case):
    """Price lines aggregated from sealed segment/provenance evidence."""
    ev=rows(db,'SELECT * FROM meter_evidence WHERE case_id=? ORDER BY start,idx',(case['id'],))
    if not ev:return None
    groups={}
    order=[]
    for e in ev:
        price=one(db,'SELECT * FROM prices WHERE id=?',(e['price'],)) if e['price'] else None
        if not price:
            matches=rows(db,"SELECT * FROM prices WHERE tenant=? AND resource=? AND status='active' AND start<=? AND ends>=?",(case['tenant'],case['resource'],e['start'],e['ends']))
            price=matches[0] if len(matches)==1 else None
        if not price:return None
        key=price['id']
        g=groups.get(key)
        if g is None:
            g={'price':price,'chunks':[]};groups[key]=g;order.append(key)
        g['chunks'].append(e)
    out=[]
    for key in order:
        price=groups[key]['price'];chunks=sorted(groups[key]['chunks'],key=lambda c:c['start'])
        # merge contiguous chunks of one price version into one price line
        spans=[]
        for c in chunks:
            if spans and c['start']<=spans[-1][1]:
                spans[-1][1]=max(spans[-1][1],c['ends'])
                spans[-1][2].append(c)
            else:
                spans.append([c['start'],c['ends'],[c]])
        for start,ends,cs in spans:
            quantity=sum(c['quantity'] for c in cs)
            discount=_discount_for(db,case,start,ends)
            rate=price['rate']
            amount=quantity*rate*(100-(discount['percent'] if discount else 0))//100
            prov=[{'sample':c['sample'],'start':c['start'],'ends':c['ends'],'quantity':c['quantity'],'role':c['role'],'calibration':c['calibration']} for c in cs]
            sig=json.dumps(sorted([[c['sample'],c['start'],c['ends'],c['quantity']] for c in cs]),sort_keys=True)
            out.append({'start':start,'ends':ends,'quantity':quantity,'rate':rate,'amount':amount,'price':price['id'],'discount':discount['id'] if discount else None,'provenance':prov,'provenance_sig':sig})
    out.sort(key=lambda p:p['start'])
    return out

def build_lines(db,case):
    has=one(db,'SELECT 1 AS n FROM meter_evidence WHERE case_id=? LIMIT 1',(case['id'],))
    if has:
        return evidence_lines(db,case)
    return segments(db,case)

def write_line_provenance(db,line_id,p):
    for i,c in enumerate(p.get('provenance') or []):
        pid=f"{line_id}:prov:{i}"
        db.execute('INSERT OR REPLACE INTO line_provenance VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (pid,line_id,i,c['start'],c['ends'],c['quantity'],c['sample'],c.get('role'),c.get('calibration'),None))

def replace_line_provenance(db,line_id,p):
    db.execute('DELETE FROM line_provenance WHERE line=?',(line_id,))
    write_line_provenance(db,line_id,p)

def open_gaps(db,invoice):
    return rows(db,"SELECT g.* FROM meter_gaps g WHERE g.status='open' AND g.case_id IN (SELECT meter_case FROM invoice_lines WHERE invoice=?) ORDER BY g.start",(invoice,))

def handle(db,x):
    op=x['op']
    # Calibration / replanning operations are metering evidence concerns but
    # callers may address them through either subsystem.
    if op in ('calibrate','calibration','verify_calibration','calibration_proof','prove','proof','handoff','select_handoff','authorize_backup','backup_authorization','replan','reseal','recalibrate','reselect','gap','record_gap','pending_interval'):
        from . import meter
        return meter.handle(db,x)
    if op=='issue':
        case=one(db,'SELECT * FROM meter_cases WHERE id=?',(x['case'],))
        if not case or case['status']!='sealed' or case['tenant']!=x['tenant']:
            return {'decision':'meter_unavailable'}
        if one(db,'SELECT 1 FROM invoice_lines WHERE invoice=?',(x['invoice'],)):
            return {'decision':'invoice_identity_conflict'}
        if one(db,'SELECT 1 FROM invoice_lines WHERE meter_case=? AND prior_id IS NULL',(case['id'],)):
            return {'decision':'meter_already_billed'}
        parts=build_lines(db,case)
        if parts is None:return {'decision':'price_gap_or_conflict'}
        for i,p in enumerate(parts,1):
            lid=f"{x['invoice']}:{i}"
            db.execute('INSERT INTO invoice_lines(id,invoice,meter_case,tenant,resource,start,ends,quantity,rate,amount,price,discount,status,prior_id,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (lid,x['invoice'],case['id'],case['tenant'],case['resource'],p['start'],p['ends'],p['quantity'],p['rate'],p['amount'],p['price'],p['discount'],'issued',None,p.get('provenance_sig')))
            write_line_provenance(db,lid,p)
        return {'decision':'invoice_issued','lines':len(parts),'total':sum(p['amount'] for p in parts)}
    if op in ('reconfirm','accept_line','unfreeze','reconcile_line','confirm_replan'):
        line=one(db,'SELECT * FROM invoice_lines WHERE id=?',(x['line'],))
        if not line or line['tenant']!=x['tenant']:
            return {'decision':'reconfirm_denied'}
        if line['status']=='issued':
            return {'decision':tok('line_reconfirmed'),'line':line['id'],'unchanged':True}
        if line['status']!='review':
            return {'decision':'reconfirm_denied'}
        case=one(db,'SELECT * FROM meter_cases WHERE id=?',(line['meter_case'],))
        parts=build_lines(db,case) if case else None
        p=next((p for p in (parts or []) if p['start']==line['start'] and p['ends']==line['ends']),None)
        if not p:
            return {'decision':'reconfirm_unsubstantiated'}
        if p['amount']!=line['amount'] and x.get('approved_amount')!=p['amount']:
            return {'decision':tok('reconfirm_amount_mismatch','approval_required'),'current_amount':p['amount'],'line_amount':line['amount']}
        db.execute("UPDATE invoice_lines SET status='issued',freeze_reason=NULL,quantity=?,rate=?,amount=?,price=?,discount=?,provenance=? WHERE id=?",
                   (p['quantity'],p['rate'],p['amount'],p['price'],p['discount'],p.get('provenance_sig'),line['id']))
        replace_line_provenance(db,line['id'],p)
        return {'decision':tok('line_reconfirmed'),'line':line['id'],'amount':p['amount']}
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
        parts=build_lines(db,case) if case else None
        current=next((p for p in parts if p['start']==line['start'] and p['ends']==line['ends']),None) if parts else None
        if not current or x['approved_amount']!=d['claim_amount'] or x['approved_amount']!=current['amount']:
            return {'decision':'resolution_unsubstantiated'}
        if current['amount']!=line['amount'] and not one(db,"SELECT 1 FROM outages WHERE identity=? AND uid=? AND status='accepted' AND receipt=? AND start<? AND ends>?",(case['identity'],case['uid'],d['evidence'],line['ends'],line['start'])):
            return {'decision':'resolution_evidence_unmatched'}
        reverse=f"{x['id']}:reverse";replacement=f"{x['id']}:new"
        db.execute('INSERT INTO invoice_lines(id,invoice,meter_case,tenant,resource,start,ends,quantity,rate,amount,price,discount,status,prior_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(reverse,line['invoice'],line['meter_case'],line['tenant'],line['resource'],line['start'],line['ends'],-line['quantity'],line['rate'],-line['amount'],line['price'],line['discount'],'reversal',line['id']))
        db.execute('INSERT INTO invoice_lines(id,invoice,meter_case,tenant,resource,start,ends,quantity,rate,amount,price,discount,status,prior_id,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(replacement,line['invoice'],line['meter_case'],line['tenant'],line['resource'],line['start'],line['ends'],current['quantity'],current['rate'],current['amount'],current['price'],current['discount'],'issued',line['id'],current.get('provenance_sig')))
        if current.get('provenance'):write_line_provenance(db,replacement,current)
        db.execute("UPDATE invoice_lines SET status='reversed' WHERE id=?",(line['id'],))
        db.execute("UPDATE disputes SET status='resolved',resolution=? WHERE id=?",(x['id'],d['id']))
        return {'decision':'line_reissued','difference':x['approved_amount']-line['amount']}
    if op=='settle':
        lines=rows(db,'SELECT * FROM invoice_lines WHERE invoice=? AND tenant=?',(x['invoice'],x['tenant']))
        if not lines or any(l['status'] in ('disputed','review') for l in lines):return {'decision':'invoice_blocked'}
        gaps=open_gaps(db,x['invoice'])
        if gaps:
            return {'decision':tok('invoice_blocked','settlement_blocked','coverage_gap','evidence_gap','window_incomplete','gap_open','invoice_blocked_by_gap'),'reason':'coverage_gap','gaps':[{'start':g['start'],'ends':g['ends']} for g in gaps]}
        if one(db,"SELECT 1 FROM settlements WHERE invoice=? AND status='settled'",(x['invoice'],)):
            return {'decision':'already_settled'}
        total=sum(l['amount'] for l in lines)
        db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?)',(x['id'],x['invoice'],x['tenant'],total,x['at'],'settled'))
        return {'decision':tok('invoice_settled'),'total':total}
    return {'decision':'unknown_operation'}
