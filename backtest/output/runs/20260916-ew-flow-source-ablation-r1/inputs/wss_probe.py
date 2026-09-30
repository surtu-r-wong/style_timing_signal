from pathlib import Path
from datetime import datetime,timezone
import json,re,shutil
import requests,yaml
run=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
shutil.copyfile(__file__,run/'inputs/wss_probe.py')
cfg=yaml.safe_load(Path('config/settings.yaml').read_text())['wind_gateway']
s=requests.Session();s.trust_env=False
records=[]
for field in ['mfd_inflow_open_m','mfd_inflow_close_m']:
    params={'codes':'000300.SH,000905.SH,000852.SH,932000.CSI,000001.SZ','fields':field,'rpt_date':'2026-09-03','options':'tradeDate=20260903;cycle=D;unit=1'}
    record={'field':field,'endpoint':'/fetch/financial_snapshot','params':params,'observed_utc':datetime.now(timezone.utc).isoformat()}
    try:
        r=s.get(cfg['url'].rstrip('/')+'/fetch/financial_snapshot',params=params,headers={'Authorization':'Bearer '+cfg['token']},timeout=(5,30))
        record['http_status']=r.status_code
        record['wind_error_codes']=sorted(set(re.findall(r'-405\d+',r.text)))
        try:j=r.json()
        except ValueError:j={}
        record['status']=j.get('status') if isinstance(j,dict) else None
        if r.status_code==200 and record['status']=='ok':
            record['columns']=j.get('columns',[]);record['rows']=j.get('rows',[])
    except requests.RequestException as e:record['transport_error_type']=type(e).__name__
    records.append(record)
    (run/'outputs/wss_probe.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False))
    print(field,{k:record.get(k) for k in ['http_status','status','wind_error_codes','transport_error_type']},'rows',len(record.get('rows',[])),flush=True)
    if record.get('http_status') in (401,403,404,429,502,503,504) or record.get('transport_error_type'):break
