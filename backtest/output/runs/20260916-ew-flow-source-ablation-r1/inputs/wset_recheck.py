from pathlib import Path
import json,re,shutil
from datetime import datetime,timezone
import requests,yaml
run=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
shutil.copyfile(__file__,run/'inputs/wset_recheck.py')
cfg=yaml.safe_load(Path('config/settings.yaml').read_text())['wind_gateway']
url=cfg['url'].rstrip('/'); token=cfg['token']
s=requests.Session();s.trust_env=False
out=[]
def call(label,path,params):
    rec={'label':label,'path':path,'params':params,'observed_utc':datetime.now(timezone.utc).isoformat()}
    try:
        r=s.get(url+path,params=params,headers={'Authorization':'Bearer '+token},timeout=(5,30))
        rec['http_status']=r.status_code
        try:j=r.json()
        except ValueError:j={}
        rec['status']=j.get('status') if isinstance(j,dict) else None
        rec['wind_error_codes']=sorted(set(re.findall(r'-405\d+',r.text)))
        if r.status_code==200 and isinstance(j,dict) and j.get('status')=='ok':
            rec['columns']=j.get('columns',[]);rec['rows']=j.get('rows',[])
        elif r.status_code==404:
            rec['meaning']='HTTP route missing; not a vendor field result'
    except requests.RequestException as e:
        rec['transport_error_type']=type(e).__name__
    out.append(rec)
    (run/'outputs/wset_recheck.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print(label, {k:rec.get(k) for k in ['http_status','status','wind_error_codes','transport_error_type']},'rows',len(rec.get('rows',[])),flush=True)
    return rec

for sector in ['chinext','star']:
    rec=call('wset_'+sector,'/fetch/market_money_flow',{'sector':sector,'start':'2026-09-02','end':'2026-09-03'})
    if rec.get('transport_error_type') or rec.get('http_status') in (401,403,404,429,502,503,504):break
