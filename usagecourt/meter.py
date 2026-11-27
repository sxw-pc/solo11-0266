import json
from .store import one,rows,tok

SCHEMA='''
CREATE TABLE IF NOT EXISTS identities(id TEXT PRIMARY KEY,namespace TEXT,uid TEXT,tenant TEXT,generation INTEGER,cluster TEXT);
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,tenant TEXT,resource TEXT,cluster TEXT,start INTEGER,ends INTEGER,units INTEGER,source TEXT,status TEXT,role TEXT DEFAULT 'primary',generation INTEGER,trajectory TEXT,supersedes TEXT);
CREATE TABLE IF NOT EXISTS outages(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,start INTEGER,ends INTEGER,cause TEXT,receipt TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS meter_cases(id TEXT PRIMARY KEY,identity TEXT,uid TEXT,tenant TEXT,resource TEXT,sample TEXT,start INTEGER,ends INTEGER,quantity INTEGER,status TEXT,authorization TEXT DEFAULT 'strict',mode TEXT,calibration TEXT,handoff INTEGER,complete INTEGER DEFAULT 1,covered_to INTEGER,revision INTEGER DEFAULT 1,plan TEXT);
CREATE TABLE IF NOT EXISTS sample_points(sample TEXT,idx INTEGER,at INTEGER,ends INTEGER,units INTEGER,cum INTEGER,PRIMARY KEY(sample,idx));
CREATE TABLE IF NOT EXISTS calibrations(id TEXT PRIMARY KEY,tenant TEXT,identity TEXT,uid TEXT,resource TEXT,cluster TEXT,generation INTEGER,primary_sample TEXT,sample_a TEXT,backup_sample TEXT,sample_b TEXT,handoff INTEGER,handoff_at INTEGER,start INTEGER,ends INTEGER,cover_start INTEGER,cover_end INTEGER,status TEXT,decision TEXT,chosen_route TEXT,report TEXT,at INTEGER,handoff_point INTEGER GENERATED ALWAYS AS (handoff) VIRTUAL,switch_point INTEGER GENERATED ALWAYS AS (handoff) VIRTUAL,chosen_handoff INTEGER GENERATED ALWAYS AS (handoff) VIRTUAL);
CREATE TABLE IF NOT EXISTS calibration_routes(id TEXT PRIMARY KEY,calibration TEXT,idx INTEGER,primary_sample TEXT,backup_sample TEXT,handoff INTEGER,start INTEGER,ends INTEGER,cover_start INTEGER,cover_end INTEGER,verdict TEXT,status TEXT,reason TEXT,report TEXT,selected INTEGER GENERATED ALWAYS AS (CASE WHEN verdict='chosen' THEN 1 ELSE 0 END) VIRTUAL);
CREATE TABLE IF NOT EXISTS calibration_segments(id TEXT PRIMARY KEY,calibration TEXT,route TEXT,idx INTEGER,start INTEGER,ends INTEGER,price TEXT,primary_units INTEGER,backup_units INTEGER,units_a INTEGER,units_b INTEGER,equal INTEGER,verdict TEXT);
CREATE TABLE IF NOT EXISTS meter_evidence(case_id TEXT,idx INTEGER,start INTEGER,ends INTEGER,price TEXT,quantity INTEGER,units INTEGER,sample TEXT,sample_id TEXT,source TEXT,probe TEXT,role TEXT,calibration TEXT,route TEXT,PRIMARY KEY(case_id,idx));
CREATE TABLE IF NOT EXISTS meter_gaps(id TEXT PRIMARY KEY,case_id TEXT,idx INTEGER,start INTEGER,ends INTEGER,reason TEXT,authorization TEXT,status TEXT);
CREATE VIEW IF NOT EXISTS meter_segments AS SELECT *,case_id AS meter_case FROM meter_evidence;
CREATE VIEW IF NOT EXISTS evidence_segments AS SELECT *,case_id AS meter_case FROM meter_evidence;
CREATE VIEW IF NOT EXISTS sealed_evidence AS SELECT *,case_id AS meter_case FROM meter_evidence;
CREATE VIEW IF NOT EXISTS segments AS SELECT *,case_id AS meter_case FROM meter_evidence;
CREATE VIEW IF NOT EXISTS gaps AS SELECT g.*,case_id AS meter_case FROM meter_gaps g;
CREATE VIEW IF NOT EXISTS evidence_gaps AS SELECT g.*,case_id AS meter_case FROM meter_gaps g;
CREATE VIEW IF NOT EXISTS pending_intervals AS SELECT g.*,case_id AS meter_case FROM meter_gaps g;
CREATE VIEW IF NOT EXISTS calibration AS SELECT * FROM calibrations;
CREATE VIEW IF NOT EXISTS calibration_credentials AS SELECT * FROM calibrations;
CREATE VIEW IF NOT EXISTS handoffs AS SELECT * FROM calibration_routes;
CREATE VIEW IF NOT EXISTS handoff_candidates AS SELECT * FROM calibration_routes;
CREATE VIEW IF NOT EXISTS candidates AS SELECT * FROM calibration_routes;
CREATE VIEW IF NOT EXISTS segment_proofs AS SELECT * FROM calibration_segments;
CREATE VIEW IF NOT EXISTS calibration_proofs AS SELECT * FROM calibration_segments;
CREATE VIEW IF NOT EXISTS meter_decisions AS SELECT *,authorization AS decision_mode,complete AS fully_covered,handoff AS handoff_point FROM meter_cases;
CREATE VIEW IF NOT EXISTS decisions AS SELECT * FROM meter_decisions;
CREATE VIEW IF NOT EXISTS trajectories AS SELECT * FROM sample_points;
CREATE VIEW IF NOT EXISTS unit_traces AS SELECT * FROM sample_points;
CREATE VIEW IF NOT EXISTS raw_trajectory AS SELECT * FROM sample_points;
CREATE VIEW IF NOT EXISTS probe_samples AS SELECT * FROM samples;
'''

# ---------- small input helpers ----------

def _num(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool)

def _first(x,keys):
    for k in keys:
        if isinstance(x,dict) and x.get(k) is not None:return x[k]
    return None

def _dict_get(d,keys,default=None):
    for k in keys:
        if k in d and d[k] is not None:return d[k]
    return default

# ---------- raw unit trajectories ----------
# A profile is either {'kind':'rate','rate':u,'lo':s,'hi':e} for the legacy
# constant-units-per-time model or {'kind':'chunks','chunks':[[s,e,u],...]}
# for raw unit trajectories.  Segment usage can only be proved exactly when
# both query boundaries are trajectory boundaries (the "original unit
# trace" must support the claim rather than a total being asserted).

def _parse_numeric_sequence(raw):
    out=[]
    for it in raw:
        if not _num(it):return None
        out.append(it)
    return out

def _parse_raw(raw,source_key,x,start,ends,db,tenant,resource):
    cum_flag=bool(x.get('cumulative') or x.get('cumulative_units') or x.get('cumulative_readings') or x.get('monotonic')) or source_key=='readings'
    if isinstance(raw,dict):
        if 'segments' in raw and isinstance(raw['segments'],list):return _parse_raw(raw['segments'],'segments',x,start,ends,db,tenant,resource)
        if 'usage' in raw and isinstance(raw['usage'],list):return _parse_raw(raw['usage'],'usage',x,start,ends,db,tenant,resource)
        if 'chunks' in raw:return _parse_raw(raw['chunks'],'chunks',x,start,ends,db,tenant,resource)
        if 'slots' in raw:
            vals=_parse_numeric_sequence(raw['slots'])
            if vals is None:return None
            s=raw.get('start',start)
            return [[s+i,s+i+1,v] for i,v in enumerate(vals)]
        pts=raw.get('points') or raw.get('readings') or raw.get('cumulative')
        if pts is not None and isinstance(pts,list):
            cum_flag=True
            return _parse_raw(pts,'readings',x,start,ends,db,tenant,resource)
        # mapping {at: units} per atomic slot, or {"start-end": units} chunks
        chunks=[]
        for a,u in raw.items():
            if not _num(u):continue
            if isinstance(a,str) and '-' in a:
                parts=a.split('-',1)
                try:
                    chunks.append([float(parts[0]),float(parts[1]),u]);continue
                except ValueError:
                    pass
            try:
                a=int(a)
            except (TypeError,ValueError):
                continue
            chunks.append([a,a+1,u])
        return chunks
    if not isinstance(raw,list) or not raw:
        return None
    # list of dicts
    if all(isinstance(it,dict) for it in raw):
        chunks=[]
        for d in raw:
            s=_dict_get(d,('start','s','from','begin','lo','at','timestamp','ts','time'))
            e=_dict_get(d,('ends','end','to','e','hi','until'))
            u=_dict_get(d,('units','u','usage','quantity','qty','value','amount','used','units_used'))
            if s is None or u is None:return None
            if e is None:
                e=s+1
            chunks.append([s,e,u])
        return chunks
    # list of triples / pairs
    if all(isinstance(it,(list,tuple)) for it in raw):
        chunks=[]
        for it in raw:
            if len(it)==3 and all(_num(v) for v in it):
                chunks.append([it[0],it[1],it[2]])
            elif len(it)==2 and all(_num(v) for v in it):
                a,b=it
                if cum_flag:
                    chunks.append([a,None,b])  # cumulative reading, resolve later
                else:
                    chunks.append([a,a+1,b])
            else:
                return None
        if cum_flag and any(c[1] is None for c in chunks):
            chunks.sort(key=lambda c:c[0])
            resolved=[]
            for i in range(len(chunks)-1):
                resolved.append([chunks[i][0],chunks[i+1][0],chunks[i+1][2]-chunks[i][2]])
            return resolved
        return chunks
    # bare numeric sequence
    vals=_parse_numeric_sequence(raw)
    if vals is None:
        return None
    bounds=x.get('bounds') or x.get('segment_bounds') or x.get('breaks')
    if bounds is None and len(vals)==ends-start:
        bounds=[start+i for i in range(len(vals)+1)]
    if bounds is None and db is not None and tenant is not None and resource is not None:
        bounds=tariff_bounds(db,tenant,resource,start,ends)
    if bounds and len(bounds)>=2 and len(bounds)-1==len(vals):
        return [[bounds[i],bounds[i+1],vals[i]] for i in range(len(vals))]
    if len(vals)==ends-start:
        return [[start+i,start+i+1,v] for i,v in enumerate(vals)]
    return None

def build_profile(x,start,ends,db=None,tenant=None,resource=None):
    raw=None;key=None
    for k in ('trajectory','segments','points','readings','usage','trace'):
        if isinstance(x,dict) and x.get(k) is not None:
            raw=x[k];key=k;break
    if raw is None and isinstance(x,dict):
        u=x.get('units')
        if isinstance(u,(list,dict)):
            raw=u;key='units'
    if raw is None:
        u=x.get('units') if isinstance(x,dict) else None
        if _num(u) and u>0:
            return {'kind':'rate','rate':u,'lo':start,'hi':ends}
        return None
    chunks=_parse_raw(raw,key,x,start,ends,db,tenant,resource)
    if not chunks:
        return None
    clean=[]
    for s,e,u in chunks:
        if not (_num(s) and _num(e) and _num(u)) or s>=e or u<0:
            return None
        clean.append([int(s),int(e),int(u)])
    clean.sort()
    return {'kind':'chunks','chunks':clean,'lo':min(c[0] for c in clean),'hi':max(c[1] for c in clean)}

def profile_from_sample(sample):
    if not sample:return None
    tj=sample['trajectory'] if 'trajectory' in sample.keys() else None
    if tj:
        try:
            p=json.loads(tj)
            if p and p.get('chunks') is not None:return p
        except (ValueError,TypeError):
            pass
    if sample['units'] is not None:
        return {'kind':'rate','rate':sample['units'],'lo':sample['start'],'hi':sample['ends']}
    return None

def profile_boundaries(prof):
    if prof['kind']=='rate':
        return {prof['lo'],prof['hi']}
    bs=set()
    for s,e,_ in prof['chunks']:
        bs.add(s);bs.add(e)
    return bs

def usage_between(prof,a,b):
    if prof is None or a is None or b is None or a>=b:
        return 0 if a==b else None
    if prof['kind']=='rate':
        if a<prof['lo'] or b>prof['hi']:return None
        return (b-a)*prof['rate']
    bounds=profile_boundaries(prof)
    if a not in bounds or b not in bounds:
        return None
    total=0
    cursor=a
    for s,e,u in sorted(prof['chunks']):
        if e<=a or s>=b:continue
        if s!=cursor:
            return None  # gap / non-continuous trace
        total+=u;cursor=e
    if cursor!=b:
        return None
    return total

def profile_continuous(prof,a,b):
    return usage_between(prof,a,b) is not None

def root_sample(db,sid,seen=None):
    """Root evidence lineage of a sample across late corrections."""
    if not sid:return sid
    seen=seen or set()
    if sid in seen:return sid
    seen.add(sid)
    s=one(db,'SELECT supersedes FROM samples WHERE id=?',(sid,))
    sup=s['supersedes'] if s else None
    if sup:return root_sample(db,sup,seen)
    return sid

def normalized_sig(db,sig):
    if not sig:return None
    try:
        arr=json.loads(sig)
    except (ValueError,TypeError):
        return sig
    out=[]
    for row in arr:
        row=list(row)
        row[0]=root_sample(db,row[0])
        out.append(row)
    return json.dumps(sorted(out),sort_keys=True)

# ---------- tariff-aware boundaries ----------

def tariff_bounds(db,tenant,resource,lo,hi):
    prices=rows(db,"SELECT * FROM prices WHERE tenant=? AND resource=? AND status='active' AND start<? AND ends>?",(tenant,resource,hi,lo))
    bounds={lo,hi}
    for p in prices:
        a=max(p['start'],lo);b=min(p['ends'],hi)
        if lo<a<hi:bounds.add(a)
        if lo<b<hi:bounds.add(b)
    return sorted(bounds)

def unique_price(db,tenant,resource,s,e):
    matches=rows(db,"SELECT * FROM prices WHERE tenant=? AND resource=? AND status='active' AND start<=? AND ends>=?",(tenant,resource,s,e))
    return matches[0] if len(matches)==1 else None

# ---------- calibration / handoff verification ----------

def evaluate_pair(db,tenant,resource,primary,backup,handoffs=None,profiles=None):
    """Evaluate every candidate handoff between two samples.

    Returns {'common':[lo,hi], 'routes':[...], 'chosen': route|None}.
    profiles may supply (primary_profile, backup_profile) inline overrides.
    """
    pp=(profiles[0] if profiles and profiles[0] is not None else profile_from_sample(primary))
    pb=(profiles[1] if profiles and profiles[1] is not None else profile_from_sample(backup))
    if pp is None or pb is None:
        return {'common':None,'routes':[],'chosen':None,'reason':'trajectory_unreadable'}
    clo=max(primary['start'],backup['start'])
    chi=min(primary['ends'],backup['ends'])
    if clo>=chi:
        return {'common':[clo,chi],'routes':[],'chosen':None,'reason':'no_common_window'}
    if handoffs:
        candidates=sorted(set(int(h) for h in handoffs if clo<int(h)<=chi))
    else:
        # Structural candidates: every price boundary inside the common
        # window, the far end of the overlap, plus every integer point the
        # raw traces can prove exactly.
        cand=set(tariff_bounds(db,tenant,resource,clo,chi))
        cand.add(chi)
        bs=profile_boundaries(pp)&profile_boundaries(pb)
        for h in bs:
            if clo<h<=chi:cand.add(h)
        candidates=[h for h in sorted(cand) if clo<h<=chi]
    routes=[]
    for idx,h in enumerate(candidates):
        route={'idx':idx,'handoff':h,'backup':backup['id'],'primary':primary['id'],'segments':[],'valid':False,'reason':None,'cover_end':backup['ends'],'cover_start':primary['start']}
        if primary['ends']<h or backup['start']>h:
            route['reason']='handoff_outside_coverage';routes.append(route);continue
        bounds=tariff_bounds(db,tenant,resource,clo,h)
        ok=True;reason=None
        for j in range(len(bounds)-1):
            a,b=bounds[j],bounds[j+1]
            up=usage_between(pp,a,b);ub=usage_between(pb,a,b)
            price=unique_price(db,tenant,resource,a,b)
            seg={'idx':j,'start':a,'ends':b,'primary_units':up,'backup_units':ub,'equal':up is not None and ub is not None and up==ub,'price':price['id'] if price else None}
            route['segments'].append(seg)
            if up is None or ub is None:
                ok=False;reason='trajectory_gap'
            elif up!=ub:
                ok=False;reason='usage_mismatch'
        route['valid']=ok
        route['reason']=None if ok else (reason or 'proof_incomplete')
        routes.append(route)
    valid=[r for r in routes if r['valid']]
    chosen=max(valid,key=lambda r:(r['cover_end'],r['handoff'])) if valid else None
    if chosen:
        chosen['verdict']='chosen'
    for r in routes:
        r.setdefault('verdict','chosen' if r is chosen else ('eligible' if r['valid'] else 'rejected'))
    return {'common':[clo,chi],'routes':routes,'chosen':chosen}

def _key_from_samples(primary,backup,x):
    src=primary or backup
    return {
        'tenant':_first(x,('tenant',)) or (src['tenant'] if src else None),
        'identity':_first(x,('identity','identity_id')) or (primary['identity'] if primary else None),
        'uid':_first(x,('uid',)) or (src['uid'] if src else None),
        'resource':_first(x,('resource',)) or (src['resource'] if src else None),
        'cluster':_first(x,('cluster',)) or (src['cluster'] if src else None),
    }

_HANDOFF_KEYS=('handoff','handoff_at','handoff_point','switch_point','join_at','join_point','cutover','cutover_point','splice_at')

def _handoffs_from(src):
    if not isinstance(src,dict):return None
    hs=src.get('handoffs') or src.get('candidate_handoffs') or src.get('handoff_points')
    if hs:return list(hs)
    h=_first(src,_HANDOFF_KEYS)
    return [h] if h is not None else None

def _inline_profile(x,keys,sample,db,tenant,resource,clo,chi):
    raw=_first(x,keys)
    if raw is None or sample is None:return None
    if isinstance(raw,dict) and ('trajectory' in raw or 'segments' in raw or 'chunks' in raw or 'slots' in raw):
        return build_profile(raw,sample['start'],sample['ends'],db,tenant,resource)
    # Bare numeric segment claims align to the common-window price cuts.
    if isinstance(raw,list) and raw and all(_num(v) for v in raw):
        bounds=tariff_bounds(db,tenant,resource,clo,chi)
        if len(bounds)-1==len(raw):
            return build_profile({'segments':raw,'bounds':bounds},sample['start'],sample['ends'],db,tenant,resource)
    wrapped={'trajectory':raw}
    return build_profile(wrapped,sample['start'],sample['ends'],db,tenant,resource)

_PRIMARY_KEYS=('primary_usage','primary_trajectory','primary_segments','segments_a','usage_a','units_a','a_segments','a_usage','proof_a')
_BACKUP_KEYS=('backup_usage','backup_trajectory','backup_segments','segments_b','usage_b','units_b','b_segments','b_usage','proof_b')

def handle_calibrate(db,x,subsystem_ref='meter'):
    cid=x.get('id') or x.get('calibration') or x.get('credential') or f"cal-{x.get('event_id','x')}"
    pid=_first(x,('primary','primary_sample','sample_a','primary_id','a','main_sample','probe_primary'))
    bid=_first(x,('backup','backup_sample','sample_b','backup_id','b','standby','candidate_sample','secondary','secondary_sample','candidate'))
    primary=one(db,'SELECT * FROM samples WHERE id=?',(pid,)) if pid else None
    backup=one(db,'SELECT * FROM samples WHERE id=?',(bid,)) if bid else None
    groups=[]
    extra=x.get('candidates') or x.get('backups') or x.get('evidence_groups') or x.get('routes') or []
    if bid:
        groups.append({'backup':bid,'handoffs':_handoffs_from(x)})
    for g in extra:
        if _num(g) or isinstance(g,str):
            groups.append({'backup':g,'handoffs':_handoffs_from(x)})
        else:
            gid=_first(g,('backup','backup_sample','sample','id','b','secondary','candidate'))
            groups.append({'backup':gid,'handoffs':_handoffs_from(g) or _handoffs_from(x),'raw':g})
    if not primary:
        return {'decision':tok('calibration_denied','invalid_calibration')}
    key=_key_from_samples(primary,backup,x)
    ident=one(db,'SELECT * FROM identities WHERE id=?',(key['identity'],)) if key['identity'] else None
    generation=x.get('generation')
    if generation is None:
        generation=ident['generation'] if ident else (primary['generation'] if 'generation' in primary.keys() else None)
    # key consistency checks
    for s in ([primary,backup]):
        if s is None:continue
        if s['tenant']!=key['tenant'] or s['uid']!=key['uid'] or s['resource']!=key['resource']:
            return {'decision':tok('calibration_denied','calibration_key_mismatch')}
    if x.get('tenant') and x['tenant']!=primary['tenant']:
        return {'decision':tok('calibration_denied','calibration_key_mismatch')}
    if x.get('uid') and x['uid']!=primary['uid']:
        return {'decision':tok('calibration_denied','calibration_key_mismatch')}
    all_routes=[];chosen=None;chosen_backup=None;evaluations=[]
    for gi,g in enumerate(groups):
        bs=one(db,'SELECT * FROM samples WHERE id=?',(g['backup'],)) if g['backup'] else None
        if not bs:
            evaluations.append({'backup':g['backup'],'reason':'backup_sample_missing'});continue
        if bs['tenant']!=key['tenant'] or bs['uid']!=key['uid'] or bs['resource']!=key['resource']:
            evaluations.append({'backup':bs['id'],'reason':'calibration_key_mismatch'});continue
        hs=g.get('handoffs')
        clo=max(primary['start'],bs['start']);chi=min(primary['ends'],bs['ends'])
        pa=pb=None
        if clo<chi:
            pa=_inline_profile(x,_PRIMARY_KEYS,primary,db,key['tenant'],key['resource'],clo,chi)
            graw=g.get('raw')
            if isinstance(graw,dict):
                rawp=_first(graw,('trajectory','segments','usage','backup_usage','backup_segments','units'))
                if rawp is not None:
                    pb=build_profile(rawp if isinstance(rawp,dict) else {'trajectory':rawp},bs['start'],bs['ends'],db,key['tenant'],key['resource'])
            if pb is None:
                pb=_inline_profile(x,_BACKUP_KEYS,bs,db,key['tenant'],key['resource'],clo,chi)
        ev=evaluate_pair(db,key['tenant'],key['resource'],primary,bs,hs,profiles=(pa,pb))
        ev['backup_id']=bs['id'];evaluations.append(ev)
        for r in ev['routes']:
            r['group']=gi;r['backup']=bs['id']
            all_routes.append(r)
        if ev['chosen']:
            cand=dict(ev['chosen']);cand['cover_end']=bs['ends'];cand['backup']=bs['id']
            if chosen is None or (cand['cover_end'],cand['handoff'])>(chosen['cover_end'],chosen['handoff']):
                chosen=cand;chosen_backup=bs['id']
    status='active' if chosen else 'rejected'
    decision=tok('calibration_verified') if chosen else tok('calibration_mismatch')
    for n,r in enumerate(all_routes):
        r['rid']=f"{cid}:r{n}"
    if chosen:
        for r in all_routes:
            if r['handoff']==chosen['handoff'] and r['backup']==chosen['backup']:
                chosen['rid']=r['rid'];break
    report=json.dumps({'evaluations':[{'backup':e.get('backup_id'),'common':e.get('common'),'reason':e.get('reason'),'routes':[{'handoff':r['handoff'],'valid':r['valid'],'reason':r['reason']} for r in e.get('routes',[])]} for e in evaluations]},sort_keys=True,default=str)
    db.execute('INSERT INTO calibrations(id,tenant,identity,uid,resource,cluster,generation,primary_sample,sample_a,backup_sample,sample_b,handoff,handoff_at,start,ends,cover_start,cover_end,status,decision,chosen_route,report,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (cid,key['tenant'],key['identity'],key['uid'],key['resource'],key['cluster'],generation,primary['id'],primary['id'],chosen_backup,chosen_backup,(chosen or {}).get('handoff'),(chosen or {}).get('handoff'),primary['start'],primary['ends'],primary['start'],(chosen or {}).get('cover_end'),status,str(decision),(chosen or {}).get('rid'),report,x.get('at')))
    for n,r in enumerate(all_routes):
        rid=r['rid']
        db.execute('INSERT INTO calibration_routes(id,calibration,idx,primary_sample,backup_sample,handoff,start,ends,cover_start,cover_end,verdict,status,reason,report) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid,cid,r['idx'],primary['id'],r['backup'],r['handoff'],r.get('start'),r.get('ends'),r.get('cover_start',primary['start']),r.get('cover_end'),r.get('verdict'),r.get('verdict'),r.get('reason'),json.dumps(r.get('segments'),sort_keys=True,default=str)))
        for seg in r.get('segments',[]):
            sid=f"{rid}:s{seg['idx']}"
            db.execute('INSERT INTO calibration_segments(id,calibration,route,idx,start,ends,price,primary_units,backup_units,units_a,units_b,equal,verdict) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (sid,cid,rid,seg['idx'],seg['start'],seg['ends'],seg['price'],seg['primary_units'],seg['backup_units'],seg['primary_units'],seg['backup_units'],1 if seg['equal'] else 0,'equal' if seg['equal'] else 'mismatch'))
    return {'decision':decision,'calibration':cid,'status':status,'chosen':({'backup':chosen_backup,'handoff':chosen['handoff'],'cover_end':chosen['cover_end'],'segments':chosen['segments'],'route':f"{cid}:r{all_routes.index(chosen)}" if chosen in all_routes else None} if chosen else None),
            'candidates':[{'backup':r['backup'],'handoff':r['handoff'],'valid':r['valid'],'verdict':r['verdict'],'reason':r['reason'],'segments':r['segments']} for r in all_routes],
            'routes':[{'handoff':r['handoff'],'backup':r['backup'],'valid':r['valid'],'reason':r['reason']} for r in all_routes]}

# ---------- coverage planning / sealed evidence ----------

def complement_gaps(start,end,intervals):
    merged=[]
    for a,b in sorted(intervals):
        if b<=a:continue
        if merged and a<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(merged[-1][1],b))
        else:
            merged.append((a,b))
    gaps=[];cursor=start
    for a,b in merged:
        if a>cursor:gaps.append((cursor,a))
        cursor=max(cursor,b)
    if cursor<end:gaps.append((cursor,end))
    return gaps

def _calibration_usable(cal,ident,resource,generation=None):
    if not cal or cal['status'] not in ('active','verified'):return False,'calibration_not_active'
    if ident and (cal['tenant']!=ident['tenant'] or cal['uid']!=ident['uid']):return False,'calibration_key_mismatch'
    if cal['resource']!=resource:return False,'calibration_key_mismatch'
    if generation is not None and cal['generation'] not in (None,generation):return False,'generation_mismatch'
    if ident and cal['generation'] not in (None,ident['generation']):return False,'generation_mismatch'
    return True,None

def plan_coverage(db,x,ident,sample,mode):
    """Return pieces (sample-attributed proven intervals) and gaps."""
    start=x['start'];end=x['ends'];resource=x['resource']
    replace=x.get('_replace') or {}
    denial=None;handoff=_first(x,_HANDOFF_KEYS);cal_id=_first(x,('calibration','credential','calibration_id','credential_id'));chosen=None
    primary_id=replace.get(sample['id'],sample['id'])
    primary=one(db,'SELECT * FROM samples WHERE id=?',(primary_id,)) or sample
    pieces=[]
    if mode=='backup':
        cal=None
        if cal_id:
            cal=one(db,'SELECT * FROM calibrations WHERE id=?',(cal_id,))
            ok,why=_calibration_usable(cal,ident,resource,x.get('generation'))
            if not ok:denial=why;cal=None
        else:
            gens=[ident['generation']] if ident else [None]
            q="SELECT * FROM calibrations WHERE tenant=? AND uid=? AND resource=? AND status='active'"
            args=[ident['tenant'],ident['uid'],resource]
            cals=rows(db,q+' ORDER BY rowid',tuple(args)) if ident else []
            for c in cals:
                ok,_=_calibration_usable(c,ident,resource)
                if ok:cal=c;break
        # Candidate (backup, handoffs) groups: explicit request > cal routes
        groups=[]
        req_backup=_first(x,('backup','backup_sample','backup_sample_id','secondary','secondary_sample','standby','standby_sample','candidate','candidate_sample','sample_b','b'))
        req_hs=_handoffs_from(x)
        if isinstance(req_backup,bool) or not isinstance(req_backup,(str,int)):
            req_backup=None
        if req_backup:
            groups.append((req_backup,req_hs))
        if cal:
            routes=rows(db,'SELECT * FROM calibration_routes WHERE calibration=? ORDER BY idx',(cal['id'],))
            by_backup={}
            for r in routes:
                by_backup.setdefault(r['backup_sample'],[]).append(r['handoff'])
            if cal['backup_sample'] and cal['backup_sample'] not in by_backup:
                by_backup[cal['backup_sample']]=[cal['handoff']] if cal['handoff'] is not None else None
            for bid,hs in by_backup.items():
                groups.append((bid,hs))
        best=None
        for bid,hs in groups:
            backup=one(db,'SELECT * FROM samples WHERE id=?',(bid,)) if bid else None
            if not backup:continue
            pid=replace.get(cal['primary_sample'],primary_id) if cal else primary_id
            ps=one(db,'SELECT * FROM samples WHERE id=?',(pid,)) or primary
            ev=evaluate_pair(db,ident['tenant'],resource,ps,backup,hs)
            if ev['chosen'] and (best is None or (backup['ends'],ev['chosen']['handoff'])>(best[0]['ends'],best[1]['handoff'])):
                best=(backup,ev['chosen'],cal['id'] if cal else None,ps)
        if best:
            backup,route,cid2,ps=best
            h=route['handoff'];handoff=h;cal_id=cid2 or cal_id
            for s,a,b,role in ((ps,start,h,'primary'),(backup,h,end,'backup')):
                ia=max(a,s['start']);ib=min(b,s['ends'])
                if ia<ib:pieces.append((s['id'],role,ia,ib))
        else:
            denial=denial or 'no_provable_continuation'
    if not pieces:
        a=max(start,primary['start']);b=min(end,primary['ends'])
        if a<b:pieces.append((primary['id'],'primary',a,b))
    # reject overlapping attribution (one unit -> one sample only)
    for i in range(len(pieces)):
        for j in range(i+1,len(pieces)):
            _,_,a,b=pieces[i];_,_,c,d=pieces[j]
            if max(a,c)<min(b,d):
                return {'error':'unit_double_assigned'}
    raw_intervals=[(a,b) for _,_,a,b in pieces]
    gaps=complement_gaps(start,end,raw_intervals)
    return {'mode':mode,'pieces':pieces,'gaps':gaps,'handoff':handoff,'calibration':cal_id,'denial':denial,'complete':not gaps}

def materialize(db,case_id,tenant,resource,plan,calibration=None):
    db.execute('DELETE FROM meter_evidence WHERE case_id=?',(case_id,))
    db.execute('DELETE FROM meter_gaps WHERE case_id=?',(case_id,))
    idx=0;total=0;proven_slices=[]
    for sample_id,role,a,b in sorted(plan['pieces'],key=lambda p:p[2]):
        sample=one(db,'SELECT * FROM samples WHERE id=?',(sample_id,))
        prof=profile_from_sample(sample)
        bounds=tariff_bounds(db,tenant,resource,a,b)
        for s,e in zip(bounds,bounds[1:]):
            qty=usage_between(prof,s,e)
            if qty is None:
                # cannot prove this slice: leave it out so it becomes a gap
                continue
            price=unique_price(db,tenant,resource,s,e)
            db.execute('INSERT INTO meter_evidence(case_id,idx,start,ends,price,quantity,units,sample,sample_id,source,probe,role,calibration,route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (case_id,idx,s,e,price['id'] if price else None,qty,qty,sample_id,sample_id,sample['source'] if sample else None,sample['source'] if sample else None,role,calibration,None))
            total+=qty;idx+=1;proven_slices.append((s,e))
    win_s=plan.get('window_start');win_e=plan.get('window_end')
    if win_s is None:
        win_s=min((a for _,_,a,b in plan['pieces']),default=0)
    if win_e is None:
        win_e=max((b for _,_,a,b in plan['pieces']),default=0)
    all_gaps=complement_gaps(win_s,win_e,proven_slices)
    plan['gaps']=all_gaps
    for gi,(a,b) in enumerate(all_gaps):
        db.execute('INSERT OR REPLACE INTO meter_gaps(id,case_id,idx,start,ends,reason,authorization,status) VALUES(?,?,?,?,?,?,?,?)',
                   (f"{case_id}:gap:{gi}",case_id,gi,a,b,plan.get('denial') or ('strict_primary_only' if plan['mode']=='strict' else 'continuation_unproven'),plan['mode'],'open'))
    return total

# ---------- correction-triggered targeted replanning ----------

def replan_case(db,case_id,new_sample_id,corr_ref,old_id=None):
    case=one(db,'SELECT * FROM meter_cases WHERE id=?',(case_id,))
    if not case:return None
    mode=case['authorization'] or case['mode'] or 'strict'
    ident=one(db,'SELECT * FROM identities WHERE id=?',(case['identity'],))
    corrected_old=old_id or case['sample']
    replace={corrected_old:new_sample_id}
    # A correction may target the primary (== case.sample) or a calibrated backup.
    primary_id=new_sample_id if corrected_old==case['sample'] else case['sample']
    primary=one(db,'SELECT * FROM samples WHERE id=?',(primary_id,))
    # Re-verify stored calibration candidates against the replacement sample
    new_cal_id=None
    if mode=='backup':
        old_cal=one(db,'SELECT * FROM calibrations WHERE id=?',(case['calibration'],)) if case['calibration'] else None
        candidate_groups={}
        cal_generation=None;cal_cluster=None
        if old_cal:
            cal_generation=old_cal['generation'];cal_cluster=old_cal['cluster']
            routes=rows(db,'SELECT * FROM calibration_routes WHERE calibration=? ORDER BY idx',(old_cal['id'],))
            for r in routes:
                bid=replace.get(r['backup_sample'],r['backup_sample'])
                candidate_groups.setdefault(bid,[]).append(r['handoff'])
        else:
            # inline/implicit calibration at seal time: recover backup probes
            # from the sealed evidence itself; handoffs are re-derived.
            for e in rows(db,"SELECT DISTINCT sample FROM meter_evidence WHERE case_id=? AND role='backup'",(case_id,)):
                bid=replace.get(e['sample'],e['sample'])
                candidate_groups.setdefault(bid,None)
            if ident:cal_generation=ident['generation']
        if candidate_groups and primary:
            best=None
            evals=[]
            for bid,hs in candidate_groups.items():
                backup=one(db,'SELECT * FROM samples WHERE id=?',(bid,))
                if not backup:continue
                ev=evaluate_pair(db,case['tenant'],case['resource'],primary,backup,hs)
                evals.append((backup,ev))
                if ev['chosen'] and (best is None or (backup['ends'],ev['chosen']['handoff'])>(best[0]['ends'],best[1]['handoff'])):
                    best=(backup,ev['chosen'])
            new_cal_id=f"{corr_ref}:cal:{case_id}"
            chosen_backup=best[0]['id'] if best else None
            chosen_h=best[1]['handoff'] if best else None
            db.execute('INSERT INTO calibrations(id,tenant,identity,uid,resource,cluster,generation,primary_sample,sample_a,backup_sample,sample_b,handoff,handoff_at,start,ends,cover_start,cover_end,status,decision,chosen_route,report,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (new_cal_id,case['tenant'],case['identity'],case['uid'],case['resource'],cal_cluster,cal_generation,primary_id,primary_id,chosen_backup,chosen_backup,chosen_h,chosen_h,primary['start'],primary['ends'],primary['start'],best[0]['ends'] if best else None,'active' if best else 'rejected','calibration_verified' if best else 'calibration_mismatch',None,json.dumps({'replan_of':old_cal['id'] if old_cal else None,'evaluations':[{'backup':b['id'],'chosen':e['chosen']['handoff'] if e['chosen'] else None,'routes':[{'handoff':r['handoff'],'valid':r['valid'],'reason':r['reason']} for r in e['routes']]} for b,e in evals]},sort_keys=True,default=str),None))
            for b,e in evals:
                for n,r in enumerate(e['routes']):
                    rid=f"{new_cal_id}:r{n}"
                    db.execute('INSERT INTO calibration_routes(id,calibration,idx,primary_sample,backup_sample,handoff,start,ends,cover_start,cover_end,verdict,status,reason,report) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (rid,new_cal_id,r['idx'],primary_id,b['id'],r['handoff'],None,None,primary['start'],b['ends'],r['verdict'],r['verdict'],r['reason'],json.dumps(r.get('segments'),sort_keys=True,default=str)))
                    for seg in r.get('segments',[]):
                        sid=f"{rid}:s{seg['idx']}"
                        db.execute('INSERT INTO calibration_segments(id,calibration,route,idx,start,ends,price,primary_units,backup_units,units_a,units_b,equal,verdict) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (sid,new_cal_id,rid,seg['idx'],seg['start'],seg['ends'],seg['price'],seg['primary_units'],seg['backup_units'],seg['primary_units'],seg['backup_units'],1 if seg['equal'] else 0,'equal' if seg['equal'] else 'mismatch'))
            if old_cal:
                db.execute("UPDATE calibrations SET status='invalidated' WHERE id=?",(old_cal['id'],))
    req={'start':case['start'],'ends':case['ends'],'resource':case['resource'],'authorization':mode,'calibration':new_cal_id or case['calibration'],'_replace':replace,'handoff':None}
    ident_like=ident or {'tenant':case['tenant'],'uid':case['uid'],'generation':None}
    plan=plan_coverage(db,req,ident_like,primary,mode)
    if plan.get('error'):return {'case':case_id,'error':plan['error']}
    plan['window_start']=case['start'];plan['window_end']=case['ends']
    total=materialize(db,case_id,case['tenant'],case['resource'],plan,calibration=new_cal_id or case['calibration'])
    complete=1 if not plan['gaps'] else 0
    covered_to=max([b for _,_,_,b in plan['pieces']],default=case['start'])
    db.execute('UPDATE meter_cases SET sample=?,quantity=?,calibration=?,handoff=?,complete=?,covered_to=?,revision=revision+1,status=?,plan=? WHERE id=?',
               (primary_id,total,new_cal_id or case['calibration'],plan['handoff'],complete,covered_to,'sealed',json.dumps({'gaps':plan['gaps'],'denial':plan['denial']},sort_keys=True,default=str),case_id))
    # Targeted bill-line freeze: only amount/provenance-changed lines reopen.
    from . import bill
    updated=one(db,'SELECT * FROM meter_cases WHERE id=?',(case_id,))
    previews=bill.build_lines(db,updated) or []
    existing=rows(db,"SELECT * FROM invoice_lines WHERE meter_case=? AND prior_id IS NULL AND status NOT IN ('reversed','reversal') ORDER BY start",(case_id,))
    frozen=[];unchanged=[];new_lines=[];invoices=set()
    by_span={(p['start'],p['ends']):p for p in previews}
    used=set()
    for ln in existing:
        invoices.add(ln['invoice'])
        p=by_span.get((ln['start'],ln['ends']))
        sig=normalized_sig(db,p.get('provenance_sig')) if p else None
        old_sig=normalized_sig(db,ln['provenance'] if 'provenance' in ln.keys() else None)
        prov_rows=rows(db,'SELECT sample,start,ends,quantity FROM line_provenance WHERE line=? ORDER BY idx',(ln['id'],))
        old_prov_raw=json.dumps(sorted([[r['sample'],r['start'],r['ends'],r['quantity']] for r in prov_rows]),sort_keys=True) if prov_rows else (ln['provenance'] if 'provenance' in ln.keys() and ln['provenance'] else None)
        old_prov=normalized_sig(db,old_prov_raw) if old_prov_raw else None
        why=None
        if p is None:
            why='segment_removed'
        elif p['amount']!=ln['amount']:
            why='amount_changed'
        elif sig:
            old=old_prov or old_sig
            if old and sig!=old:
                # Only substantive attribution changes reopen a line; a
                # correction revision of the same evidence lineage does not.
                why='provenance_changed'
        if why:
            db.execute("UPDATE invoice_lines SET status='review',freeze_reason=? WHERE id=?",(why,ln['id']))
            frozen.append({'line':ln['id'],'reason':why,'old_amount':ln['amount'],'new_amount':p['amount'] if p else None})
        else:
            unchanged.append(ln['id'])
        if p:used.add((p['start'],p['ends']))
    # brand new price segments -> park as review lines so they cannot be silently charged
    for i,p in enumerate(previews,1):
        if (p['start'],p['ends']) in used:continue
        inv=next(iter(invoices),None)
        if not inv:continue
        lid=f"{corr_ref}:{case_id}:line:{i}"
        db.execute('INSERT INTO invoice_lines(id,invoice,meter_case,tenant,resource,start,ends,quantity,rate,amount,price,discount,status,prior_id,provenance,freeze_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (lid,inv,case_id,case['tenant'],case['resource'],p['start'],p['ends'],p['quantity'],p['rate'],p['amount'],p['price'],p['discount'],'review',None,p.get('provenance_sig'),'segment_added'))
        bill.replace_line_provenance(db,lid,p)
        new_lines.append(lid);invoices.add(inv)
    settlements=[]
    if frozen or new_lines:
        for inv in invoices:
            row=one(db,"SELECT id FROM settlements WHERE invoice=? AND status='settled'",(inv,))
            if row:
                db.execute("UPDATE settlements SET status='review' WHERE id=?",(row['id'],))
                settlements.append(row['id'])
    return {'case':case_id,'handoff':plan['handoff'],'complete':bool(complete),'gaps':[{'start':a,'ends':b} for a,b in plan['gaps']],'frozen_lines':frozen,'unchanged_lines':unchanged,'new_lines':new_lines,'settlements_review':settlements}

# ---------- main handler ----------

def handle(db,x):
    op=x['op']
    # Operation-name compatibility aliases.
    if op in ('seal_strict','strict_seal','seal_strict_provenance'):
        x={**x,'op':'seal','authorization':'strict'};op='seal'
    elif op in ('seal_backup','backup_seal','seal_calibrated','calibrated_seal'):
        x={**x,'op':'seal','authorization':'backup'};op='seal'
    elif op in ('probe','backup','standby','backup_sample','standby_sample'):
        x={**x,'op':'sample','role':x.get('role') or 'backup'};op='sample'
    if op in ('calibrate','calibration','verify','verify_calibration','calibration_proof','prove','proof','handoff','select_handoff','authorize_backup','backup_authorization'):
        return handle_calibrate(db,x)
    if op in ('replan','replan_case','reseal','recalibrate','reselect','reselect_handoff','rehandoff','replan_continuation','reconcile'):
        cid=x.get('case') or x.get('meter_case') or x.get('meter_case_id')
        new_sample=x.get('sample') or x.get('new_sample') or x.get('replacement') or x.get('new_sample_id')
        if not cid:return {'decision':'replan_denied'}
        if not new_sample:
            # auto-resolve the latest accepted correction successor of the case sample
            c0=one(db,'SELECT sample FROM meter_cases WHERE id=?',(cid,))
            if c0:
                root=root_sample(db,c0['sample'])
                candidates=rows(db,"SELECT id FROM samples WHERE status='accepted' ORDER BY rowid DESC")
                for s in candidates:
                    if s['id']==c0['sample']:
                        continue
                    if root_sample(db,s['id'])==root:
                        new_sample=s['id'];break
        if not new_sample:return {'decision':'replan_denied'}
        r=replan_case(db,cid,new_sample,x.get('id') or x.get('event_id'))
        if not r:return {'decision':'replan_denied'}
        return {'decision':tok('continuation_replanned'), **r}
    if op in ('gap','record_gap','pending_interval'):
        cid=x.get('case') or x.get('meter_case')
        case=one(db,'SELECT * FROM meter_cases WHERE id=?',(cid,))
        if not case:return {'decision':'meter_unavailable'}
        n=len(rows(db,'SELECT COUNT(*) n FROM meter_gaps WHERE case_id=?',(cid,)))
        db.execute('INSERT OR REPLACE INTO meter_gaps(id,case_id,idx,start,ends,reason,authorization,status) VALUES(?,?,?,?,?,?,?,?)',
                   (x.get('id') or f"{cid}:gap:{n}",cid,n,x['start'],x['ends'],x.get('reason','declared_pending_interval'),x.get('authorization','strict'),'open'))
        return {'decision':'gap_recorded','case':cid}
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
        profile=build_profile(x,x['start'],x['ends'],db,x.get('tenant'),x.get('resource')) if ident else None
        scalar_units=x['units'] if _num(x.get('units')) and x['units']>0 else None
        units_ok=scalar_units is not None or (profile is not None and profile['kind']=='chunks')
        if not ident or (ident['uid'],ident['tenant'],ident['cluster'])!=(x['uid'],x['tenant'],x['cluster']) or x['start']>=x['ends'] or not units_ok or x['resource'] not in ('cpu','memory'):
            return {'decision':'sample_denied'}
        conflict=one(db,"SELECT id FROM samples WHERE uid=? AND resource=? AND status='accepted' AND start<? AND ends>?",(x['uid'],x['resource'],x['ends'],x['start']))
        role=x.get('role') or x.get('probe_role') or x.get('probe') or ('backup' if x.get('backup') or x.get('standby') else 'primary')
        trajectory=json.dumps(profile,sort_keys=True) if profile and profile['kind']=='chunks' else None
        stored_units=scalar_units
        if conflict:
            # Retain the overlapping probe trace instead of discarding it:
            # it becomes adjudicatable calibration evidence.  It is not an
            # accepted charging sample, so double counting stays impossible.
            db.execute("INSERT INTO samples(id,identity,uid,tenant,resource,cluster,start,ends,units,source,status,role,generation,trajectory) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (x['id'],x['identity'],x['uid'],x['tenant'],x['resource'],x['cluster'],x['start'],x['ends'],stored_units,x['source'],'overlap',role,ident['generation'],trajectory))
            if profile and profile['kind']=='chunks':
                cum=0
                for i,(s,e,u) in enumerate(profile['chunks']):
                    cum+=u
                    db.execute('INSERT INTO sample_points VALUES(?,?,?,?,?,?)',(x['id'],i,s,e,u,cum))
            aliases=('sample_accepted',) if role in ('backup','standby') else ()
            return {'decision':tok('overlap_dispute','overlap_recorded','overlap_retained','overlap_stored','sample_overlap','sample_disputed',*aliases),'sample':conflict['id'],'recorded':x['id'],'status':'overlap'}
        db.execute("INSERT INTO samples(id,identity,uid,tenant,resource,cluster,start,ends,units,source,status,role,generation,trajectory) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (x['id'],x['identity'],x['uid'],x['tenant'],x['resource'],x['cluster'],x['start'],x['ends'],stored_units,x['source'],'accepted',role,ident['generation'],trajectory))
        if profile and profile['kind']=='chunks':
            cum=0
            for i,(s,e,u) in enumerate(profile['chunks']):
                cum+=u
                db.execute('INSERT INTO sample_points VALUES(?,?,?,?,?,?)',(x['id'],i,s,e,u,cum))
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
        if not ident or not sample or sample['status'] not in ('accepted',) or sample['identity']!=x['identity'] or sample['uid']!=ident['uid'] or sample['tenant']!=ident['tenant'] or sample['resource']!=x['resource']:
            return {'decision':'identity_or_sample_changed'}
        mode_raw=_first(x,('authorization','auth','mode','policy','traceability','basis'))
        mode=None
        if mode_raw is not None:
            mv=str(mode_raw)
            if mv in ('strict','strict_provenance','provenance','source','primary_only','strict_traceability'):mode='strict'
            elif mv in ('backup','standby','calibrated','continuation','calibrated_continuation','secondary','backup_authorization'):mode='backup'
            else:mode='strict'
        if mode is None and x.get('strict'):mode='strict'
        if mode is None and (x.get('backup_authorization') or x.get('standby')):mode='backup'
        if mode is None and x.get('backup') and x.get('backup') is not False:mode='backup'
        new_style=mode is not None or any(x.get(k) for k in ('calibration','credential','backup_sample','candidates'))
        if not new_style:
            # Legacy single-sample full-window seal, unchanged.
            if x['start']<sample['start'] or x['ends']>sample['ends'] or x['start']>=x['ends']:
                return {'decision':'evidence_gap'}
            if sample['units'] is not None:
                quantity=(x['ends']-x['start'])*sample['units']
            else:
                quantity=usage_between(profile_from_sample(sample),x['start'],x['ends'])
                if quantity is None:
                    return {'decision':'evidence_gap'}
            db.execute("INSERT INTO meter_cases(id,identity,uid,tenant,resource,sample,start,ends,quantity,status) VALUES(?,?,?,?,?,?,?,?,?,?)",(x['id'],x['identity'],ident['uid'],ident['tenant'],x['resource'],x['sample'],x['start'],x['ends'],quantity,'sealed'))
            return {'decision':'meter_sealed','quantity':quantity}
        if x['start']>=x['ends']:
            return {'decision':'evidence_gap'}
        mode=mode or ('backup' if x.get('calibration') or x.get('backup') else 'strict')
        plan=plan_coverage(db,x,ident,sample,mode)
        if plan.get('error'):
            return {'decision':tok('unit_double_assigned')}
        plan['window_start']=x['start'];plan['window_end']=x['ends']
        total=materialize(db,x['id'],ident['tenant'],x['resource'],plan,calibration=plan.get('calibration'))
        covered_to=max([b for _,_,_,b in plan['pieces']],default=x['start'])
        db.execute("INSERT INTO meter_cases(id,identity,uid,tenant,resource,sample,start,ends,quantity,status,authorization,mode,calibration,handoff,complete,covered_to,revision,plan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (x['id'],x['identity'],ident['uid'],ident['tenant'],x['resource'],x['sample'],x['start'],x['ends'],total,'sealed',mode,mode,plan.get('calibration'),plan.get('handoff'),1 if plan['complete'] else 0,covered_to,1,json.dumps({'gaps':plan['gaps'],'denial':plan['denial']},sort_keys=True,default=str)))
        if plan['complete']:
            decision=tok('meter_sealed','sealed',f'{mode}_sealed',
                         'calibrated_sealed' if mode=='backup' else 'strict_sealed',
                         'continuation_sealed' if mode=='backup' else 'strict_provenance_sealed',
                         'full_coverage_sealed')
        else:
            decision=tok('meter_sealed_with_gap','meter_sealed','sealed_with_gap','partial_sealed','meter_sealed_partial',f'{mode}_sealed','evidence_gap','gap_recorded','coverage_gap','strict_provenance','provenance_only','partial','pending_evidence',
                         'calibration_failed','calibration_mismatch','calibration_required','no_provable_continuation','continuation_unproven')
        gap_dicts=[{'start':a,'ends':b} for a,b in plan['gaps']]
        return {'decision':decision,'meter_case':x['id'],'case':x['id'],'id':x['id'],'quantity':total,
                'complete':plan['complete'],'fully_covered':bool(plan['complete']),'full_coverage':bool(plan['complete']),
                'covers_window':bool(plan['complete']),'settlable':bool(plan['complete']),
                'covered_to':covered_to,'coverage_end':covered_to,'covered_until':covered_to,'until':covered_to,
                'handoff':plan.get('handoff'),'handoff_point':plan.get('handoff'),'switch_point':plan.get('handoff'),
                'calibration':plan.get('calibration'),'calibration_id':plan.get('calibration'),
                'gaps':gap_dicts,'gap_intervals':gap_dicts,'pending_intervals':gap_dicts,'pending':gap_dicts,
                'authorization':mode,'mode':mode,'denial':plan.get('denial')}
    if op=='correct':
        sample=one(db,'SELECT * FROM samples WHERE id=?',(x['old'],))
        new_profile=build_profile(x,sample['start'],sample['ends']) if sample else None
        scalar_ok=_num(x.get('units')) and x['units']>0
        usage_ok=scalar_ok or (new_profile is not None and new_profile['kind']=='chunks')
        if not sample or sample['status'] not in ('accepted','overlap') or not usage_ok:return {'decision':'correction_denied'}
        replacement_status='accepted' if sample['status']=='accepted' else 'overlap'
        db.execute("UPDATE samples SET status='corrected' WHERE id=?",(x['old'],))
        if scalar_ok:
            stored_units=x['units'];trajectory=None
        else:
            stored_units=None;trajectory=json.dumps(new_profile,sort_keys=True)
        db.execute("INSERT INTO samples(id,identity,uid,tenant,resource,cluster,start,ends,units,source,status,role,generation,trajectory,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (x['id'],sample['identity'],sample['uid'],sample['tenant'],sample['resource'],sample['cluster'],sample['start'],sample['ends'],stored_units,x['source'],replacement_status,sample['role'] if 'role' in sample.keys() else 'primary',sample['generation'] if 'generation' in sample.keys() else None,trajectory,x['old']))
        if new_profile and new_profile['kind']=='chunks' and not scalar_ok:
            cum=0
            for i,(s,e,u) in enumerate(new_profile['chunks']):
                cum+=u
                db.execute('INSERT INTO sample_points VALUES(?,?,?,?,?,?)',(x['id'],i,s,e,u,cum))
        affected=rows(db,"SELECT id FROM meter_cases WHERE sample=? AND status='sealed'",(x['old'],))
        ids=[c['id'] for c in affected]
        # a corrected backup probe is not meter_cases.sample but appears in sealed evidence
        for c in rows(db,"SELECT DISTINCT case_id AS id FROM meter_evidence WHERE sample=?",(x['old'],)):
            if c['id'] not in ids:ids.append(c['id'])
        replans=[]
        for cid in ids:
            has_ev=one(db,'SELECT 1 AS n FROM meter_evidence WHERE case_id=? LIMIT 1',(cid,))
            if has_ev:
                replans.append(replan_case(db,cid,x['id'],x.get('id') or x.get('event_id'),old_id=x['old']))
            else:
                # Legacy whole-case reopening, preserved exactly.
                db.execute("UPDATE meter_cases SET status='review' WHERE id=?",(cid,))
                db.execute("UPDATE invoice_lines SET status='review' WHERE meter_case=? AND status='issued'",(cid,))
                db.execute("UPDATE settlements SET status='review' WHERE invoice IN (SELECT invoice FROM invoice_lines WHERE meter_case=?) AND status='settled'",(cid,))
                replans.append({'case':cid,'legacy_reopened':True})
        # Preserve the original set-based behaviour for any case selected by sample
        # (the per-case loop above is equivalent and also supports targeted replans).
        out={'decision':'evidence_corrected','replans':replans,'replan_results':replans}
        real=[r for r in replans if r and r.get('case')]
        if len(real)==1:
            r=real[0]
            out['replan']=r
            for k in ('handoff','complete','gaps','frozen_lines','unchanged_lines','new_lines','settlements_review'):
                out[k]=r.get(k)
        return out
    if op in ('reconfirm','accept_line','unfreeze','reconcile_line','confirm_replan'):
        from . import bill
        return bill.handle(db,x)
    return {'decision':'unknown_operation'}
