from pathlib import Path
from datetime import datetime, timezone
import ast
import json
import shutil
import sys
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path.cwd()))
from backtest.intraday_flow_pilot import batch_close_ledgers
from backtest.metrics import sharpe, max_drawdown
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import create_run_dir,artifact_record,write_manifest,git_state

root=Path.cwd()
source=root/'backtest/output/runs/20260916-ew-intraday-flow-pilot-r1'
run=create_run_dir(root/'backtest/output/runs','20260916-ew-intraday-flow-attribution-r1')
prereg='''# 首轮结果后固定的归因补充

已看到首轮8个分腿中S__B_neg改善，25组合中C__base__B_neg的完整样本Sharpe最高。本次是对已选中案例的事后归因，不是新确认，不重新选择窗口/方向/候选。

先固定两个组合参照：原多头+固定半仓原空头；原多头+首轮同平均空头暴露比例的原空头（比例取已冻结exposure_matched_scales.csv）。原组合与被选中组合直接读取首轮账本，不重复跑其模拟。三执行情景与首轮一致。

复用09-15已经固定的七个事件起始日，各取连续10个交易日；计算候选相对原组合、固定半空组合的逐日对数收益差，按事件窗/窗外和年度归因。另列将七窗净收益置零后的指标，保留原持仓路径，不解释为事件未发生的反事实或显著性检验。不得以事后事件剔除优化候选。无新数据、无统计GO、无部署。
'''
(run/'inputs/prereg.md').write_text(prereg)
write_manifest(run,{'status':'running','created_utc':datetime.now(timezone.utc).isoformat(),'selection_is_posthoc':True})
shutil.copyfile(__file__,run/'inputs/attribution.py')
for filename in ['contract_schedule.csv','exposure_matched_scales.csv','metrics.csv']:
    shutil.copyfile(source/'outputs'/filename,run/'inputs'/filename)
for scenario in ['close_3bps','close_10bps','second_close_3bps']:
    for filename in [f'targets_{scenario}.csv',f'ledgers_{scenario}.csv.gz']:
        shutil.copyfile(source/'outputs'/filename,run/'inputs'/filename)
for filename in ['backtest/intraday_flow_pilot.py','backtest/run_intraday_flow_pilot.py','backtest/metrics.py','backtest/run_manifest.py']:
    shutil.copyfile(root/filename,run/'inputs'/('code__'+filename.replace('/','__')))
event_source=root/'backtest/output/runs/20260915-event-dependence-r1/inputs/audit.py'
for node in ast.parse(event_source.read_text()).body:
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='EVENTS' for t in node.targets):
        events=ast.literal_eval(node.value)
(run/'inputs/events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
(run/'inputs/source_manifest.json').write_text(json.dumps({'first_run_manifest':artifact_record(source/'manifest.json',root),'event_definitions':artifact_record(event_source,root)},indent=2))
schedule=pd.read_csv(run/'inputs/contract_schedule.csv',index_col='date',parse_dates=True)
idx=schedule.index
w=np.column_stack([np.where(schedule.IM_symbol.notna(),.5,1.),np.where(schedule.IM_symbol.notna(),.5,0.)])
market={'index':idx,'groups':['IC','IM'],'weights':w,
        'symbols':schedule[['IC_symbol','IM_symbol']].fillna('').to_numpy(),
        'new_price':schedule[['IC_new_close','IM_new_close']].to_numpy(),
        'old_price':schedule[['IC_held_close','IM_held_close']].to_numpy()}
scales=pd.read_csv(run/'inputs/exposure_matched_scales.csv')
rows,attribution,yearly,zeroed=[],[],[],[]
for scenario,cost in [('close_3bps',3.),('close_10bps',10.),('second_close_3bps',3.)]:
    targets=pd.read_csv(run/f'inputs/targets_{scenario}.csv',index_col='date',parse_dates=True)
    scale=float(scales[(scales.scenario==scenario)&(scales.side=='S')&(scales.rule=='B_neg')].scale.iloc[0])
    extra=pd.DataFrame({'fixed_half_short':targets.L__base+.5*targets.S__base,
                        'matched_short_exposure':targets.L__base+scale*targets.S__base},index=idx)
    added=batch_close_ledgers(market,extra,cost_bps=cost)
    original=pd.read_csv(run/f'inputs/ledgers_{scenario}.csv.gz',index_col=['strategy','date'],parse_dates=['date'])
    curves={'incumbent':original.loc['C__base__base'],'filtered_short':original.loc['C__base__B_neg'],**added}
    for name,ledger in curves.items():
        rows.append({'scenario':scenario,'strategy':name,'matched_scale':scale,**describe(ledger)})
    pd.concat(added,names=['strategy','date']).to_csv(run/f'outputs/control_ledgers_{scenario}.csv.gz')
    masks={}
    for event in events:
        start=idx.searchsorted(pd.Timestamp(event[1]))
        mask=np.zeros(len(idx),dtype=bool);mask[start:start+10]=True
        masks[event[0]]=mask
    union=np.logical_or.reduce(list(masks.values()))
    for reference in ['incumbent','fixed_half_short']:
        delta=np.log1p(curves['filtered_short'].ret)-np.log1p(curves[reference].ret)
        for event,mask in {**masks,'all_seven_windows':union,'outside_seven_windows':~union}.items():
            attribution.append({'scenario':scenario,'reference':reference,'event':event,'n_days':int(mask.sum()),
                                'relative_log_return':float(delta[mask].sum()),'full_relative_log_return':float(delta.sum())})
        for year in sorted(set(idx.year)):
            yearly.append({'scenario':scenario,'reference':reference,'year':year,'relative_log_return':float(delta[idx.year==year].sum())})
        a=curves['filtered_short'].ret.where(~union,0.)
        b=curves[reference].ret.where(~union,0.)
        zeroed.append({'scenario':scenario,'reference':reference,'zeroed_days':int(union.sum()),
                       'candidate_sharpe':sharpe(a),'reference_sharpe':sharpe(b),
                       'candidate_cagr':float(np.expm1(np.log1p(a).sum()*245/len(a))),
                       'reference_cagr':float(np.expm1(np.log1p(b).sum()*245/len(b))),
                       'candidate_maxdd':max_drawdown(a),'reference_maxdd':max_drawdown(b)})
        np.testing.assert_allclose(delta[union].sum()+delta[~union].sum(),delta.sum(),atol=1e-12)
pd.DataFrame(rows).to_csv(run/'outputs/control_comparison.csv',index=False)
pd.DataFrame(attribution).to_csv(run/'outputs/event_attribution.csv',index=False)
pd.DataFrame(yearly).to_csv(run/'outputs/yearly_attribution.csv',index=False)
pd.DataFrame(zeroed).to_csv(run/'outputs/net_returns_zeroed.csv',index=False)
(run/'outputs/verification.json').write_text(json.dumps({'seven_event_windows':7,'union_days':int(union.sum()),'disjoint_event_union':sum(m.sum() for m in masks.values())==int(union.sum()),'log_attribution_identity_passed':True,'posthoc_selected_case':True},indent=2))
print(pd.DataFrame(rows)[['scenario','strategy','cagr','sharpe','maxdd']].round(5).to_string(index=False))
print(pd.DataFrame(attribution).query("scenario == 'close_3bps'").round(5).to_string(index=False))
print(pd.DataFrame(zeroed).round(5).to_string(index=False))
files=[p for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
write_manifest(run,{'status':'complete','stage':'posthoc_control_and_event_attribution','completed_utc':datetime.now(timezone.utc).isoformat(),'git':git_state(root),'artifacts':[artifact_record(p,run) for p in files]})
