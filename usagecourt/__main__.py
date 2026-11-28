import argparse,json
from pathlib import Path
from .store import connect,run,replay,rows

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['tariff','meter','bill','die','patterns','delivery','demo','report','replay'])
    p.add_argument('--db',default='demo.sqlite');p.add_argument('--fixture')
    a=p.parse_args()
    if a.command=='replay':result=replay(a.db)
    else:
        db=connect(a.db)
        if a.command=='report':result=rows(db,'SELECT * FROM events ORDER BY seq')
        else:
            if a.command=='demo':files=[(sub,f'fixtures/{sub}_{kind}.json') for sub,kind in [('tariff','normal'),('meter','normal'),('bill','normal'),('tariff','failure'),('meter','failure'),('bill','failure')]]
            elif a.fixture:files=[(a.command,a.fixture)]
            else:p.error('--fixture required')
            result=[{'fixture':file,'results':[run(db,x.get('subsystem',sub),x) for x in json.loads(Path(file).read_text(encoding='utf-8'))]} for sub,file in files]
        db.close()
    print(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True))
    if a.command=='replay' and not result['verified']:raise SystemExit(2)

if __name__=='__main__':main()
