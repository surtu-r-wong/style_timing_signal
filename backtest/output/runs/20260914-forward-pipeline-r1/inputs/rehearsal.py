from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import shutil
import numpy as np
import pandas as pd
from backtest.run_manifest import create_run_dir,artifact_record,write_manifest,git_state
from backtest.forward_market import read_pg,validate_snapshot
from backtest.forward_scoring import score_frames
from backtest.execution_audit import stats

root=Path.cwd(); source=root/'backtest/output/runs/20260914-fixed-candidates-r1'
run=create_run_dir(root/'backtest/output/runs','20260914-forward-pipeline-r1')
shutil.copyfile(root/'docs/superpowers/plans/2026-09-14-forward-completion-plan.md',run/'inputs/prereg.md')
write_manifest(run,{'status':'running','git':git_state(root),'created_utc':datetime.now(timezone.utc).isoformat(),'mode':'historical_rehearsal_not_forward'})
manifest=json.loads((source/'manifest.json').read_text())
for name in ('spot.csv','futures.csv','equal_weight.csv','slope20.csv'):
    path=source/'inputs'/name; actual=artifact_record(path,source)
    assert actual==next(a for a in manifest['artifacts'] if a['path']==actual['path'])
    shutil.copyfile(path,run/'inputs'/name)
spot=pd.read_csv(run/'inputs/spot.csv',parse_dates=['date'])
fut=pd.read_csv(run/'inputs/futures.csv',parse_dates=['date'])
idx=pd.DatetimeIndex(sorted(spot.date.unique()))[-505:]
spot=spot[spot.date.isin(idx)];fut=fut[fut.date.isin(idx)]
sigs={name:pd.read_csv(run/f'inputs/{file}.csv',index_col='date',parse_dates=True).factor_value.reindex(idx)
      for name,file in [('equal_weight','equal_weight'),('slope20','slope20')]}
# Historical generator output ONLY: this is not an append to any live campaign.
receipts={d.date().isoformat():{'signals':{name:float(s.at[d]) for name,s in sigs.items()}} for d in idx}
rows=[];comparisons=[];checks=[]
for cost in (3.,10.):
    ledgers,quality=score_frames(spot,fut,idx,receipts,cost=cost)
    for name,ledger in ledgers.items():
        ledger.to_csv(run/f'outputs/rehearsal_{name}_{cost:g}bps.csv',index_label='date')
        rows.append({'name':name,'cost_bps':cost,**stats(ledger.ret.iloc[1:])})
    for c,a,b in [('C1','futures_ew','futures_always_long'),('C2','current_two_pool','ew_two_pool')]:
        comparisons.append({'comparison':c,'cost_bps':cost,'sharpe_difference':stats(ledgers[a].ret.iloc[1:])['sharpe']-stats(ledgers[b].ret.iloc[1:])['sharpe']})
    checks.append({'cost_bps':cost,'sessions':len(idx)-1,'first_ret_is_initialization_zero':all(x.ret.iloc[0]==0 for x in ledgers.values()),
                   'all_daily_returns_finite':all(np.isfinite(x.ret).all() for x in ledgers.values())})
pd.DataFrame(rows).to_csv(run/'outputs/historical_rehearsal_metrics.csv',index=False)
pd.DataFrame(comparisons).to_csv(run/'outputs/historical_rehearsal_comparisons.csv',index=False)
(run/'outputs/rehearsal_quality.json').write_text(json.dumps({'historical_rehearsal':True,'contemporaneous_receipts':False,**quality},indent=2))
# Real adapter smoke test on an already-consumed historical date, stored outside live evidence.
s,f,c,p=read_pg('2026-09-11','2026-09-01')
for name,data in [('spot',s),('futures',f),('calendar',c)]: data.to_csv(run/f'inputs/adapter_historical_{name}.csv',index=False)
next_day,contracts=validate_snapshot('2026-09-11',s,f,c)
(run/'outputs/adapter_validation.json').write_text(json.dumps({'mode':'historical_adapter_smoke','provenance':p,'spot_rows':len(s),'futures_rows':len(f),'calendar_rows':len(c),'next_session':next_day,'next_contracts':contracts},indent=2))
(run/'outputs/rehearsal_checks.json').write_text(json.dumps(checks,indent=2))
shutil.copyfile(__file__,run/'inputs/rehearsal.py')
print(json.dumps({'run':str(run),'historical_sessions':len(idx)-1,'checks':checks,'adapter_next_session':next_day},indent=2),flush=True)
