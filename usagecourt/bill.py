from .store import one,rows

SCHEMA='''
CREATE TABLE IF NOT EXISTS invoice_lines(id TEXT PRIMARY KEY,invoice TEXT,meter_case TEXT,tenant TEXT,resource TEXT,start INTEGER,ends INTEGER,quantity INTEGER,rate INTEGER,amount INTEGER,price TEXT,discount TEXT,status TEXT,prior_id TEXT);
CREATE TABLE IF NOT EXISTS disputes(id TEXT PRIMARY KEY,line TEXT,tenant TEXT,reason TEXT,evidence TEXT,claim_amount INTEGER,status TEXT,resolution TEXT);
CREATE TABLE IF NOT EXISTS arbitrators(id TEXT PRIMARY KEY,tenant TEXT,actor TEXT,role TEXT,generation INTEGER,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS settlements(id TEXT PRIMARY KEY,invoice TEXT,tenant TEXT,total INTEGER,at INTEGER,status TEXT,voucher_total INTEGER,net_due INTEGER);
CREATE TABLE IF NOT EXISTS vouchers(id TEXT PRIMARY KEY,batch TEXT,tenant TEXT,resource TEXT,price TEXT,face INTEGER,remaining INTEGER,group_id TEXT,starts INTEGER,ends INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS commitments(id TEXT PRIMARY KEY,tenant TEXT,resource TEXT,price TEXT,amount INTEGER,starts INTEGER,ends INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS voucher_redemptions(id TEXT PRIMARY KEY,invoice TEXT,line TEXT,voucher TEXT,batch TEXT,amount INTEGER,kind TEXT,status TEXT,prior_id TEXT);
CREATE TABLE IF NOT EXISTS voucher_reservations(id TEXT PRIMARY KEY,commitment TEXT,voucher TEXT,batch TEXT,amount INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS voucher_quarantine(id TEXT PRIMARY KEY,invoice TEXT,voucher TEXT,amount INTEGER,status TEXT);
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

# ----- prepaid voucher helpers -----

def effective_lines(db,invoice,tenant):
    return rows(db,"SELECT * FROM invoice_lines WHERE invoice=? AND tenant=? AND status='issued' AND amount>0",(invoice,tenant))

def invoice_blocked(db,invoice,tenant):
    lines=rows(db,'SELECT * FROM invoice_lines WHERE invoice=? AND tenant=?',(invoice,tenant))
    if not lines:return True
    if any(l['status'] in ('disputed','review') for l in lines):return True
    if one(db,"SELECT 1 FROM settlements WHERE invoice=? AND status='review'",(invoice,)):return True
    return False

def debit_voucher(db,voucher_id,amount):
    db.execute('UPDATE vouchers SET remaining=remaining-? WHERE id=?',(amount,voucher_id))

def credit_voucher(db,voucher_id,amount):
    db.execute('UPDATE vouchers SET remaining=remaining+? WHERE id=?',(amount,voucher_id))

def reserved_open(db,tenant,resource,at):
    r=one(db,"""SELECT COALESCE(SUM(r.amount),0) n FROM voucher_reservations r
                 JOIN commitments c ON c.id=r.commitment
                 WHERE r.status='reserved' AND c.tenant=? AND c.resource=? AND c.status='open'
                   AND c.ends>?""",(tenant,resource,at))
    return r['n']

def renewal_remaining(db,tenant,resource,at):
    # spendable uncommitted balance, plus reservations still earmarked for live future commitments
    free=sum(v['remaining'] for v in voucher_inventory(db,tenant,resource,at))
    return free+reserved_open(db,tenant,resource,at)

def voucher_inventory(db,tenant,resource,at):
    out=[]
    for v in rows(db,"SELECT * FROM vouchers WHERE tenant=? AND resource=? AND status='active' AND starts<=? AND ends>?",(tenant,resource,at,at)):
        if v['remaining']>0:out.append({**v,'free':v['remaining']})
    return out

def open_commitments(db,tenant,resource,at):
    # registered future usage is reservable as long as it has not yet expired
    return rows(db,"SELECT * FROM commitments WHERE tenant=? AND resource=? AND status='open' AND ends>?",(tenant,resource,at))

def quarantine_map(db,invoice=None):
    q="SELECT voucher,SUM(amount) amount FROM voucher_quarantine WHERE status='open'"
    args=[]
    if invoice is not None:
        q+=' AND invoice=?';args.append(invoice)
    return {r['voucher']:r['amount'] for r in rows(db,q+' GROUP BY voucher',args)}

def voucher_suits(v,line):
    return v['price'] is None or v['price']==line['price']

def solve_reduce(inventory,lines):
    """Maximize this-invoice deduction; tie-break on remaining renewable capacity.
    Ungrouped vouchers may split; grouped sub-vouchers move as one whole group and
    every sub-voucher must land wholly on a single fitting line, else the group is retained."""
    singles=sorted((v for v in inventory if not v['group_id']),key=lambda v:(v['ends'],v['id']))
    groupmap={}
    for v in inventory:
        if v['group_id']:groupmap.setdefault(v['group_id'],[]).append(v)
    groups=[sorted(gs,key=lambda v:v['id']) for gs in groupmap.values()]
    groups.sort(key=lambda g:g[0]['group_id'])
    base_caps={l['id']:l['amount'] for l in lines}
    base_free={v['id']:v['free'] for v in inventory}
    best={'deduction':-1,'renewable':-1,'plan':None}

    def fill_singles(caps,free,plan):
        extra=[]
        for v in singles:
            for line in lines:
                if caps[line['id']]<=0 or free[v['id']]<=0:continue
                if not voucher_suits(v,line):continue
                take=min(free[v['id']],caps[line['id']])
                caps[line['id']]-=take;free[v['id']]-=take
                extra.append((v,line,take))
        return plan+extra,free

    def evaluate(caps,free,plan):
        caps2=dict(caps);free2=dict(free)
        full,final_free=fill_singles(caps2,free2,plan)
        deduction=sum(a for _,_,a in full)
        renewable=sum(final_free.values())
        if deduction>best['deduction'] or (deduction==best['deduction'] and renewable>best['renewable']):
            best.update({'deduction':deduction,'renewable':renewable,'plan':list(full)})

    def try_groups(gi,caps,free,plan):
        if gi==len(groups):
            evaluate(caps,free,plan);return
        g=groups[gi]
        # option 1: leave the whole group retained (its balance stays committed to renewal)
        try_groups(gi+1,caps,free,plan)
        # option 2: every sub-voucher is placed wholly on one fitting line; sub-vouchers may
        # land on different lines but a sub-voucher can never be split. If any sub-voucher has
        # no line able to absorb it whole, the entire group must be retained.
        def place_sub(si,caps2,free2,acc):
            if si==len(g):
                try_groups(gi+1,caps2,free2,plan+acc);return
            v=g[si]
            for line in lines:
                if not voucher_suits(v,line):continue
                # grouped sub-voucher is redeemed for its FULL face; the line must still cover it
                if caps2[line['id']] < free2[v['id']]:continue
                nc=dict(caps2);nf=dict(free2)
                nc[line['id']]-=free2[v['id']];nf[v['id']]=0
                place_sub(si+1,nc,nf,acc+[(v,line,free2[v['id']])])
        place_sub(0,dict(caps),dict(free),[])

    try_groups(0,dict(base_caps),dict(base_free),[])
    if not best['plan'] or best['deduction']<=0:return {'feasible':False,'reason':'no_redeemable_voucher'}
    return {'feasible':True,'plan':best['plan'],'deduction':best['deduction']}

def solve_commitment_cover(inventory,commitment):
    """Reserve enough free voucher balance to fully cover one future commitment.
    Ungrouped vouchers may split; a group may pool its sub-vouchers but the group stays intact.
    Different commitments can never share the same voucher balance because each reservation
    debits the voucher's free balance before the next commitment is evaluated."""
    need=commitment['amount']
    def fits(v):
        # version identity must match and the voucher must still be valid when the commitment starts
        return (v['price'] is None or v['price']==commitment['price']) and v['ends']>commitment['starts']
    singles=sorted((v for v in inventory if not v['group_id'] and fits(v)),key=lambda v:(v['ends'],v['id']))
    groupmap={}
    for v in inventory:
        if v['group_id'] and fits(v):groupmap.setdefault(v['group_id'],[]).append(v)
    groups=sorted(groupmap.values(),key=lambda g:g[0]['group_id'])
    result=[]
    # single vouchers first (they may split, shortest-lived first)
    for v in singles:
        if need<=0:break
        take=min(v['free'],need)
        if take<=0:continue
        result.append((v,take));v['free']-=take;need-=take
    if need<=0:return result
    # then whole groups only: every sub-voucher is reserved at its full face, so a group is
    # used solely when its total intact balance can cover the remaining need; never split a group.
    for g in groups:
        if need<=0:break
        total=sum(v['free'] for v in g)
        if total<need:continue
        for v in g:
            result.append((v,v['free']));v['free']=0
        need-=total
    return result if need<=0 and result else None

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
    if op=='voucher_buy':
        faces=x.get('faces') or [x['face']]*x.get('count',1)
        if not faces or any(f<=0 for f in faces) or x['starts']>=x['ends'] or x['resource'] not in ('cpu','memory'):
            return {'decision':'voucher_denied'}
        if x.get('price') is not None and not one(db,'SELECT 1 FROM prices WHERE id=? AND tenant=?',(x['price'],x['tenant'])):
            return {'decision':'voucher_denied'}
        for i,face in enumerate(faces,1):
            vid=f"{x['id']}:{i}" if len(faces)>1 else x['id']
            if one(db,'SELECT 1 FROM vouchers WHERE id=?',(vid,)):return {'decision':'voucher_denied'}
            db.execute('INSERT INTO vouchers VALUES(?,?,?,?,?,?,?,?,?,?,?)',(vid,x['batch'],x['tenant'],x['resource'],x.get('price'),face,face,x.get('group_id'),x['starts'],x['ends'],'active'))
        return {'decision':'voucher_active','vouchers':len(faces),'face_total':sum(faces)}
    if op=='commitment_register':
        if x['amount']<=0 or x['starts']>=x['ends']:return {'decision':'commitment_denied'}
        if x.get('price') is not None and not one(db,'SELECT 1 FROM prices WHERE id=? AND tenant=?',(x['price'],x['tenant'])):
            return {'decision':'commitment_denied'}
        if one(db,'SELECT 1 FROM commitments WHERE id=?',(x['id'],)):return {'decision':'commitment_denied'}
        db.execute('INSERT INTO commitments VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['tenant'],x['resource'],x.get('price'),x['amount'],x['starts'],x['ends'],'open'))
        return {'decision':'commitment_registered'}
    if op=='authorize':
        if x['mode'] not in ('reduce','renew'):return {'decision':'authorization_denied'}
        if invoice_blocked(db,x['invoice'],x['tenant']):return {'decision':'invoice_under_review'}
        lines=effective_lines(db,x['invoice'],x['tenant'])
        if not lines:return {'decision':'authorization_denied'}
        if one(db,"SELECT 1 FROM voucher_redemptions WHERE invoice=? AND status='active'",(x['invoice'],)):
            return {'decision':'voucher_invoice_already_redeemed'}
        if one(db,"SELECT 1 FROM settlements WHERE invoice=? AND status='settled'",(x['invoice'],)):
            return {'decision':'already_settled'}
        resource=lines[0]['resource'];at=x['at']
        inventory=voucher_inventory(db,x['tenant'],resource,at)
        # vouchers quarantined for other invoices cannot be touched by this authorization
        blocked={vid for vid,amt in quarantine_map(db).items() if amt>0}
        inventory=[v for v in inventory if v['id'] not in blocked]
        if not inventory:return {'decision':'voucher_allocation_denied','reason':'no_redeemable_voucher'}
        gross=sum(l['amount'] for l in lines)
        reservations=[];uncovered=[]
        if x['mode']=='renew':
            for c in open_commitments(db,x['tenant'],resource,at):
                cover=solve_commitment_cover(inventory,c)
                if cover is None:
                    uncovered.append(c['id']);continue
                for v,amt in cover:reservations.append((c,v,amt))
            if uncovered:
                return {'decision':'voucher_allocation_denied','reason':'commitment_uncoverable','uncovered':uncovered,'payable':gross}
        caps={l['id']:l['amount'] for l in lines}
        sol=solve_reduce(inventory,lines)
        if not sol['feasible']:
            return {'decision':'voucher_allocation_denied','reason':sol['reason'],'payable':gross}
        # persist reservations first so redemptions cannot double-spend the same balance
        for c,v,amt in reservations:
            rid=f"{x['invoice']}-auth:{x['id']}:res:{c['id']}:{v['id']}"
            db.execute('INSERT INTO voucher_reservations VALUES(?,?,?,?,?,?)',(rid,c['id'],v['id'],v['batch'],amt,'reserved'))
            debit_voucher(db,v['id'],amt)
        redemptions=[]
        for v,line,amt in sol['plan']:
            caps[line['id']]-=amt
            rid=f"{x['id']}:red:{v['id']}:{line['id']}"
            db.execute('INSERT INTO voucher_redemptions VALUES(?,?,?,?,?,?,?,?,?)',(rid,x['invoice'],line['id'],v['id'],v['batch'],amt,x['mode'],'active',None))
            debit_voucher(db,v['id'],amt)
            redemptions.append({'voucher':v['id'],'line':line['id'],'amount':amt})
        deduction=sum(r['amount'] for r in redemptions)
        if deduction<=0:return {'decision':'voucher_allocation_denied','reason':'no_redeemable_voucher','payable':gross}
        renewable=renewal_remaining(db,x['tenant'],resource,at)
        result={'decision':'voucher_authorized','mode':x['mode'],'gross':gross,'voucher_deduction':deduction,
                'payable':gross-deduction,'renewal_remaining':renewable,'redemptions':redemptions,
                'reservations':[{'commitment':c['id'],'voucher':v['id'],'amount':amt} for c,v,amt in reservations]}
        if uncovered:result['uncovered']=uncovered
        return result
    if op=='release_redemption':
        reds=rows(db,"SELECT * FROM voucher_redemptions WHERE invoice=? AND status='active'",(x['invoice'],))
        # reservations created by a renew authorization on this invoice back a commitment on the same voucher pool
        res=rows(db,"SELECT r.* FROM voucher_reservations r WHERE r.status='reserved' AND r.id LIKE ?",(f"{x['invoice']}-auth:%",))
        if not reds and not res:return {'decision':'release_denied'}
        if not invoice_blocked(db,x['invoice'],x['tenant']):
            return {'decision':'release_denied'}
        def quarantine(voucher,amount):
            q=one(db,"SELECT * FROM voucher_quarantine WHERE invoice=? AND voucher=? AND status='open'",(x['invoice'],voucher))
            if q:db.execute('UPDATE voucher_quarantine SET amount=? WHERE id=?',(q['amount']+amount,q['id']))
            else:db.execute('INSERT INTO voucher_quarantine VALUES(?,?,?,?,?)',(f"{x['id']}:q:{voucher}",x['invoice'],voucher,amount,'open'))
        restored=0
        for r in reds:
            db.execute('INSERT INTO voucher_redemptions VALUES(?,?,?,?,?,?,?,?,?)',(f"{x['id']}:{r['id']}:reverse",r['invoice'],r['line'],r['voucher'],r['batch'],-r['amount'],r['kind'],'reversal',r['id']))
            db.execute("UPDATE voucher_redemptions SET status='reversed' WHERE id=?",(r['id'],))
            credit_voucher(db,r['voucher'],r['amount'])
            quarantine(r['voucher'],r['amount']);restored+=r['amount']
        released_reservations=0
        for rv in res:
            db.execute("INSERT INTO voucher_reservations VALUES(?,?,?,?,?,?)",(f"{x['id']}:{rv['id']}:reverse",rv['commitment'],rv['voucher'],rv['batch'],-rv['amount'],'reversal'))
            db.execute("UPDATE voucher_reservations SET status='reversed' WHERE id=?",(rv['id'],))
            credit_voucher(db,rv['voucher'],rv['amount'])
            quarantine(rv['voucher'],rv['amount']);released_reservations+=rv['amount']
        return {'decision':'redemption_reversed','invoice':x['invoice'],'restored':restored,'reservations_released':released_reservations}
    if op=='reauthorize':
        if x['mode'] not in ('reduce','renew'):return {'decision':'authorization_denied'}
        if invoice_blocked(db,x['invoice'],x['tenant']):return {'decision':'invoice_under_review'}
        lines=effective_lines(db,x['invoice'],x['tenant'])
        if not lines:return {'decision':'authorization_denied'}
        if one(db,"SELECT 1 FROM voucher_redemptions WHERE invoice=? AND status='active'",(x['invoice'],)):
            return {'decision':'voucher_invoice_already_redeemed'}
        resource=lines[0]['resource'];at=x['at']
        qpool=quarantine_map(db,x['invoice'])
        # the only balance this invoice may re-bind is exactly what its reversal quarantined;
        # active usage is already zero for these vouchers, so the pool alone is the available free.
        vmap={v['id']:v for v in voucher_inventory(db,x['tenant'],resource,at)}
        inventory=[{**vmap[vid],'free':pool} for vid,pool in qpool.items() if vid in vmap and pool>0]
        if not inventory:return {'decision':'voucher_allocation_denied','reason':'quarantine_empty'}
        gross=sum(l['amount'] for l in lines);reservations=[];uncovered=[]
        if x['mode']=='renew':
            for c in open_commitments(db,x['tenant'],resource,at):
                cover=solve_commitment_cover(inventory,c)
                if cover is None:uncovered.append(c['id']);continue
                for v,amt in cover:reservations.append((c,v,amt))
            if uncovered:
                return {'decision':'voucher_allocation_denied','reason':'commitment_uncoverable','uncovered':uncovered,'payable':gross}
        sol=solve_reduce(inventory,lines)
        if not sol['feasible']:
            return {'decision':'voucher_allocation_denied','reason':sol['reason'],'payable':gross}
        caps={l['id']:l['amount'] for l in lines}
        for c,v,amt in reservations:
            db.execute('INSERT INTO voucher_reservations VALUES(?,?,?,?,?,?)',(f"{x['invoice']}-auth:{x['id']}:res:{c['id']}:{v['id']}",c['id'],v['id'],v['batch'],amt,'reserved'))
            debit_voucher(db,v['id'],amt)
        redemptions=[]
        for v,line,amt in sol['plan']:
            caps[line['id']]-=amt
            db.execute('INSERT INTO voucher_redemptions VALUES(?,?,?,?,?,?,?,?,?)',(f"{x['id']}:red:{v['id']}:{line['id']}",x['invoice'],line['id'],v['id'],v['batch'],amt,x['mode'],'active',None))
            debit_voucher(db,v['id'],amt)
            redemptions.append({'voucher':v['id'],'line':line['id'],'amount':amt})
        deduction=sum(r['amount'] for r in redemptions)
        # consume the quarantine pool as the restored balance is re-bound to this invoice
        for vid,amt in qpool.items():
            q=one(db,"SELECT * FROM voucher_quarantine WHERE invoice=? AND voucher=? AND status='open'",(x['invoice'],vid))
            if q:db.execute("UPDATE voucher_quarantine SET status='closed' WHERE id=?",(q['id'],))
        renewable=renewal_remaining(db,x['tenant'],resource,at)
        return {'decision':'voucher_reauthorized','mode':x['mode'],'gross':gross,'voucher_deduction':deduction,
                'payable':gross-deduction,'renewal_remaining':renewable,'redemptions':redemptions,
                'reservations':[{'commitment':c['id'],'voucher':v['id'],'amount':amt} for c,v,amt in reservations]}
    if op=='reissue_reviewed':
        # form a new settleable version of an invoice under review; original lines and any
        # prior voucher mapping stay intact, and reviewed lines are reversed and replaced.
        reviewed=rows(db,"SELECT * FROM invoice_lines WHERE invoice=? AND tenant=? AND status='review'",(x['invoice'],x['tenant']))
        if not reviewed:return {'decision':'reissue_denied'}
        for line in reviewed:
            case=one(db,'SELECT * FROM meter_cases WHERE id=?',(line['meter_case'],))
            parts=segments(db,case) if case else None
            current=next((p for p in parts if p['start']==line['start'] and p['ends']==line['ends']),None) if parts else None
            if not current:return {'decision':'reissue_denied'}
            rev=f"{x['id']}:{line['id']}:reverse";new=f"{x['id']}:{line['id']}:new"
            db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(rev,line['invoice'],line['meter_case'],line['tenant'],line['resource'],line['start'],line['ends'],-line['quantity'],line['rate'],-line['amount'],line['price'],line['discount'],'reversal',line['id']))
            db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(new,line['invoice'],line['meter_case'],line['tenant'],line['resource'],current['start'],current['ends'],current['quantity'],current['rate'],current['amount'],current['price'],current['discount'],'issued',line['id']))
            db.execute("UPDATE invoice_lines SET status='reversed' WHERE id=?",(line['id'],))
        db.execute("UPDATE settlements SET status='superseded' WHERE invoice=? AND status='review'",(x['invoice'],))
        return {'decision':'invoice_reissued','lines':len(reviewed)}
    if op=='settle':
        lines=rows(db,'SELECT * FROM invoice_lines WHERE invoice=? AND tenant=?',(x['invoice'],x['tenant']))
        if not lines or any(l['status'] in ('disputed','review') for l in lines):return {'decision':'invoice_blocked'}
        if one(db,"SELECT 1 FROM settlements WHERE invoice=? AND status='settled'",(x['invoice'],)):
            return {'decision':'already_settled'}
        active_reds=rows(db,"SELECT * FROM voucher_redemptions WHERE invoice=? AND status='active'",(x['invoice'],))
        effective=[l for l in lines if l['status']=='issued' and l['amount']>0]
        valid={l['id'] for l in effective}
        if any(r['line'] not in valid for r in active_reds):return {'decision':'invoice_blocked'}
        total=sum(l['amount'] for l in effective)
        voucher_total=sum(r['amount'] for r in active_reds)
        db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['invoice'],x['tenant'],total,x['at'],'settled',voucher_total,total-voucher_total))
        db.execute("UPDATE voucher_quarantine SET status='closed' WHERE invoice=? AND status='open'",(x['invoice'],))
        return {'decision':'invoice_settled','total':total,'voucher_deduction':voucher_total,'net_due':total-voucher_total}
    return {'decision':'unknown_operation'}
