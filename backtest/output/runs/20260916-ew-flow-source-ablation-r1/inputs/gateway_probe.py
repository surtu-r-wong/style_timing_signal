from pathlib import Path
import json,re,shutil
from datetime import datetime,timezone
import requests,yaml
run=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
shutil.copyfile(__file__,run/'inputs/gateway_probe.py')
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
    (run/'outputs/gateway_probe.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print(label, {k:rec.get(k) for k in ['http_status','status','wind_error_codes','transport_error_type']},'rows',len(rec.get('rows',[])),flush=True)
    return rec
control=call('existing_wset_csi300','/fetch/market_money_flow',{'sector':'csi_300','start':'2026-09-02','end':'2026-09-03'})
if control.get('transport_error_type') or control.get('http_status') in (401,403,502,503,504):
    print('Transport/auth unavailable; stopping vendor probes, no field conclusion.',flush=True)
else:
    codes='000300.SH,000905.SH,000852.SH,932000.CSI,000001.SZ'
    for field in ['mfd_inflow','mfd_inflow_open_m','mfd_inflow_close_m','mfd_buyamt','mfd_sellamt','mfd_buyamt_l','mfd_buyamt_xl']:
        rec=call('wsd_'+field,'/fetch/financial_quarterly',{'codes':codes,'fields':field,'start':'2026-09-02','end':'2026-09-03','options':'Period=D;unit=1'})
        if rec.get('transport_error_type') or rec.get('http_status') in (401,403,404,502,503,504):break
