from pathlib import Path
from datetime import datetime,timezone
import json,re,shutil
import requests,yaml
run=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
shutil.copyfile(__file__,run/'inputs/wss_history_probe.py')
cfg=yaml.safe_load(Path('config/settings.yaml').read_text())['wind_gateway']
s=requests.Session();s.trust_env=False
records=[]
def fetch(codes,fields,date):
    params={'codes':codes,'fields':fields,'rpt_date':date,'options':'tradeDate='+date.replace('-','')+';cycle=D;unit=1'}
    rec={'params':params,'observed_utc':datetime.now(timezone.utc).isoformat()}
    try:
        r=s.get(cfg['url'].rstrip('/')+'/fetch/financial_snapshot',params=params,headers={'Authorization':'Bearer '+cfg['token']},timeout=(5,30))
        rec['http_status']=r.status_code;rec['wind_error_codes']=sorted(set(re.findall(r'-405\d+',r.text)))
        try:j=r.json()
        except ValueError:j={}
        rec['status']=j.get('status') if isinstance(j,dict) else None
        if r.status_code==200 and rec['status']=='ok':rec.update(columns=j.get('columns',[]),rows=j.get('rows',[]))
    except requests.RequestException as e:rec['transport_error_type']=type(e).__name__
    records.append(rec)
    (run/'outputs/wss_history_probe.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False))
    print(date,fields,{k:rec.get(k) for k in ['http_status','status','wind_error_codes','transport_error_type']},'rows',len(rec.get('rows',[])),flush=True)
    return rec
codes='000300.SH,000905.SH,000852.SH,932000.CSI,399102.SZ,000680.SH,000016.SH,000906.SH'
for field in ['mfd_buyamt_m','mfd_sellamt_m','mfd_inflow_m']:
    rec=fetch('000300.SH,000905.SH,000852.SH,000001.SZ',field,'2026-09-03')
    if rec.get('http_status') in (401,403,404,429,502,503,504) or rec.get('transport_error_type'):raise SystemExit(0)
for date in ['2015-01-05','2018-06-19','2020-07-01','2022-07-25','2024-09-24','2026-09-02','2026-09-03']:
    rec=fetch(codes,'mfd_inflow_open_m,mfd_inflow_close_m',date)
    if rec.get('http_status') in (401,403,404,429,502,503,504) or rec.get('transport_error_type'):break
