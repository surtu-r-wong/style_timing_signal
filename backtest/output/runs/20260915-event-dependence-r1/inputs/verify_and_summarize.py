"""Independent engine/mask checks and review tables. Does not rebuild or optimize candidates."""
from pathlib import Path
import sys,json,hashlib
import numpy as np,pandas as pd,yaml
ROOT=Path.cwd();sys.path.insert(0,str(ROOT));RUN=Path(__file__).resolve().parents[1];O=RUN/'outputs'
from backtest.engine import run_strategy
cat=pd.read_csv(O/'candidate_catalog.csv');pos=pd.read_csv(O/'candidate_positions.csv',index_col=0,parse_dates=True)
refs=pd.read_csv(O/'reference_positions.csv',index_col=0,parse_dates=True);market=pd.read_csv(O/'market.csv',index_col=0,parse_dates=True)
masks=pd.read_csv(O/'event_masks.csv',index_col=0,parse_dates=True);events=pd.read_csv(O/'event_windows.csv');a=pd.read_csv(O/'event_attribution.csv')
assert cat.candidate.is_unique and pos.columns.is_unique
assert set(cat.name)==set(pos.columns)
assert not a.duplicated(['candidate','lag','period','mask']).any()
assert np.allclose(a.total_relative_log,a.event_relative_log+a.nonevent_relative_log,atol=1e-12)
for n in [1,5,10,20]:
 expected=pd.Series(False,index=market.index)
 for row in events[events.window==n].itertuples():
  i=market.index.searchsorted(pd.Timestamp(row.anchor));expected.iloc[i:i+n]=True
 assert np.array_equal(expected,masks[f'all_{n}'])
for n in [5,10,20]:
 expected=set(market.underlying.abs().nlargest(n).index)
 assert expected==set(masks.index[masks[f'abs_market_top{n}']])
none=a[a['mask']=='none'];assert np.allclose(none.delta_sharpe,none.neutral_delta_sharpe,equal_nan=True)
response=pd.read_csv(O/'event_position_response.csv')
assert np.allclose(response.event_gross_sum,response.frozen_position_gross_sum+response.response_gross_sum)
# Independently calculate via the repository's Series-based engine and metrics.
def sharpe(s):return s.mean()/s.std(ddof=1)*245**.5
def dd(s):
 nav=np.r_[1.,(1+s).cumprod().to_numpy()];return float((nav/np.maximum.accumulate(nav)-1).min())
primary=a[(a.period=='full')&(a['mask']=='all_10')].set_index(['candidate','lag'])
risk=[];identities={};maxerr=0
for i,r in enumerate(cat.itertuples()):
 p=pos[r.name].dropna();idx=p.index
 assert len(p)==r.n and idx.equals(market.loc[idx.min():idx.max()].index)
 identities[r.name]=hashlib.sha256(idx.asi8.tobytes()+p.to_numpy().tobytes()).hexdigest()
 c=market['carry'].reindex(idx) if r.model=='carry' else None
 for lag in [1,2]:
  q=p if lag==1 else p.shift(1).fillna(0)
  ref=refs[r.reference].reindex(idx);ref=ref if lag==1 else ref.shift(1).fillna(0)
  x=run_strategy(q,market.underlying,carry=c).ret;y=run_strategy(ref,market.underlying,carry=c).ret
  ev=masks.all_10.reindex(idx);xx=x.mask(ev,0);yy=y.mask(ev,0);rr=primary.loc[(r.candidate,lag)]
  vals=[sharpe(x),sharpe(y),sharpe(xx),sharpe(yy),np.log1p(x).sum()-np.log1p(y).sum(),np.log1p(xx).sum()-np.log1p(yy).sum()]
  stored=rr[['sharpe_candidate','sharpe_reference','neutral_sharpe_candidate','neutral_sharpe_reference','total_relative_log','nonevent_relative_log']].to_numpy(float)
  error=np.max(np.abs(vals-stored));maxerr=max(maxerr,error);assert error<1e-9,(r.candidate,lag,error)
  risk.append(dict(candidate=r.candidate,lag=lag,total_return_candidate=(1+x).prod()-1,total_return_reference=(1+y).prod()-1,
    neutral_return_candidate=(1+xx).prod()-1,neutral_return_reference=(1+yy).prod()-1,maxdd_candidate=dd(x),maxdd_reference=dd(y),
    neutral_maxdd_candidate=dd(xx),neutral_maxdd_reference=dd(yy)))
 if i%300==0:print('verified',i,'/',len(cat),flush=True)
pd.DataFrame(risk).to_csv(O/'risk_and_returns.csv',index=False)
cat['path_id']=cat.name.map(identities);cat['has_short']=cat.name.map((pos.min()<0).to_dict())
cat['comparison_scope']=np.select([cat.reference.eq('ew_lf'),cat.reference.eq('ew_short'),cat.model.eq('spot')&cat.has_short,cat.model.eq('spot'),cat.mapping.eq('lf')],['historical_EW_longflat','current_EW_short_leg','hypothetical_spot_with_shorts','current_spot_slope_longflat','current_EW_cross_mapping'],default='current_EW_symmetric')
cat.to_csv(O/'candidate_catalog.csv',index=False)
# Verify frozen source records and copies against source files, without printing contents.
sources=json.loads((O/'input_sources.json').read_text())
for key,rec in sources.items():
 f=RUN/rec['path'];assert hashlib.sha256(f.read_bytes()).hexdigest()==rec['sha256']
 assert f.read_bytes()==(ROOT/key).read_bytes(),key
# Archive ledgers start Jan 3; regenerate using their own calendar rather than reset elsewhere.
ledger_checks=[]
for mapping,prefix in [('symmetric','fixed_'),('longflat','fixed_lf_')]:
 for name in ['raw','DEMA5','DEMA7','CUSUM','Hamilton']:
  path=ROOT/f'backtest/output/runs/20260914-fixed-candidates-r1/outputs/{mapping}_{name}_ledger.csv'
  ledger=pd.read_csv(path,index_col=0,parse_dates=True)
  idx=ledger.index.intersection(market.index);p=pos[prefix+name].reindex(idx)
  z=run_strategy(p,market.underlying,carry=market['carry']).ret
  err=float((z-ledger.ret.reindex(idx)).abs().max())
  ledger_checks.append(dict(mapping=mapping,name=name,max_abs_error=err,n=len(idx)))
  assert err<1e-9,(mapping,name,err)
pd.DataFrame(ledger_checks).to_csv(O/'ledger_verification.csv',index=False)
# Training/validation comparison uses each strategy's worst Sharpe over the two periods.
tv=a[(a.period.isin(['train','val']))&(a['mask'].isin(['all_5','all_10','all_20']))]
g=tv.groupby(['candidate','lag','mask']);w=g[['sharpe_candidate','sharpe_reference','neutral_sharpe_candidate','neutral_sharpe_reference']].min()
w['period_count']=g.period.nunique();valid=g[['sharpe_candidate','sharpe_reference','neutral_sharpe_candidate','neutral_sharpe_reference']].count().min(axis=1).eq(2);w=w[(w.period_count==2)&valid].copy()
w['delta_worst_tv']=w.sharpe_candidate-w.sharpe_reference;w['neutral_delta_worst_tv']=w.neutral_sharpe_candidate-w.neutral_sharpe_reference
w['rank_flip']=(w.delta_worst_tv<0)&(w.neutral_delta_worst_tv>0);w.reset_index().to_csv(O/'worst_tv_comparisons.csv',index=False)
# One row for each candidate and sensitivity; preserve exact denominator and feasibility labels.
f=a[a.period=='full'].merge(cat[['candidate','comparison_scope','path_id','has_short']],on='candidate',validate='many_to_one')
f.to_csv(O/'full_sensitivity.csv',index=False)
summary=f.groupby(['comparison_scope','lag','mask']).agg(comparisons=('candidate','size'),originally_lagging=('delta_sharpe',lambda s:int((s<0).sum())),sharpe_flips=('rank_flip','sum'),both_flips=('both_metrics_flip','sum')).reset_index()
summary.to_csv(O/'comparison_counts.csv',index=False)
sel=f[f['mask'].isin(['all_5','all_10','all_20'])]
rob=sel.groupby('candidate').agg(tests=('rank_flip','size'),sharpe_flips=('rank_flip','sum'),both_flips=('both_metrics_flip','sum'),min_neutral_delta=('neutral_delta_sharpe','min'))
rob['robust_sharpe_flip']=(rob.tests==6)&(rob.sharpe_flips==6);rob['robust_both_flip']=(rob.tests==6)&(rob.both_flips==6)
rob.reset_index().merge(cat,on='candidate').to_csv(O/'robustness.csv',index=False)
# Inventory every registry record; group count is not a count of unique trading signals.
reg=yaml.safe_load((RUN/'inputs/research_registry.yaml').read_text())['studies']
negative=[r for r in reg if r['outcome'] in ['stop','all_fail']]
notes={
 'mian2-cross-section':('evidence_gap','冻结代表存在1个内部交易日缺口，未擅自填补；未做数值归因。'),
 'dual-channel':('different_instruments','改变现货2000/期货500执行配比；不等于同blend信号替换，未纳入同标的排名。'),
 'tail-fifth-bucket':('structural_gate','原研究为桶结构/增量可辨认性；本轮未重建股票级篮子和可交易日持仓。'),
 'geometric-five-bucket':('structural_gate','原研究为五桶全替换及结构可辨认性；本轮未重建股票级篮子日持仓。'),
 'axes-batch-1':('structural_gate','四轴entry ticket为结构/IC检验；没有直接沿用的单一日频交易规格。'),
 'axes-batch-2-quality':('structural_gate','质量轴entry ticket为结构/IC检验；没有直接沿用的单一日频交易规格。'),
 'b3-continuous-style-state':('evidence_gap','紧凑档案无完整逐日持仓；结构闸亦失败，2021—2023收益闸已失败；本轮未重建全股票级管线。'),
 'basis-term-cross-section-replication':('other_target_IC_gate','原复制目标300/50，原闸为IC；未用blend代理冒充原检验。'),
 'sharpe-difference-calibration-2026-09-14':('statistical_method','统计检验校准失败，不是交易信号。'),
 'robust-sharpe-inference-2026-09-14':('statistical_method','统计推断规则校准失败，不是交易信号。'),
 'basis-c1-retest':('alias_replay','与long-axes-probes的C1代表共用回放；不重复计候选。'),
 'threshold-and-microcap-grids':('partial_grid','仅重放blend阈值子网格；其他标的及微盘联合网格未全量覆盖。'),
 'fund-crowding':('representatives','重放A2与B两个冻结代表，不是22规格全网格。'),
 'divergence-probe':('representatives','重放原冻结赢家，不是48规格全网格。'),
 'breadth-divergence':('representatives','重放原固定默认规则，不是168规格扫描全量。'),
 'signal-generator-ewma-std':('partial_grid','重放equal_weight固定EWMA分母规格；未重放citic/hybrid对象。'),
 'money-flow-cross-section-replication':('diagnostic_translation','原闸为IC；将冻结代表映射成交易仅作补充诊断，不视作原收益闸重跑。'),
 'index-option-implied-vol':('representatives','O1—O5冻结代表；另含O6描述性对照，不能把O6列为原否决主规格。'),
 'analyst-revision':('representatives_with_gaps','R1—R4缓存有190/210个内部缺日，本轮使用最长连续段；未将缺日填为持仓。'),
 'pair-set':('fixed_candidates','B/C/D及红利价格伙伴版本；跨版本报告的其他规格未宣称全覆盖。'),
 'long-axes-probes':('representatives','C1/C2/B1/B2/B3/E1/E2七个冻结代表。'),
 'rotation-short-window':('representatives','原平台区代表lb20/sm3/k20；不是全网格。'),
 'conditional-modulation':('representatives','固定S1二元调制；早期预热从2014开始，非完整原网格。'),
}
rows=[]
for r in reg:
 sub=cat[cat.study==r['id']]
 default='grid_or_fixed_replay' if len(sub) else 'outside_negative_audit'
 status,note=notes.get(r['id'],(default,'按原网格或已存档固定规格回放；不同映射分别列行。' if len(sub) else '未被登记为STOP/ALL_FAIL，不计入否决研究分母。'))
 rows.append(dict(study=r['id'],title=r['title'],outcome=r['outcome'],coverage=status,tested_specifications=sub.name.nunique(),comparison_rows=len(sub),note=note,original_scope=r.get('scope',''),original_claim=r.get('claim',''),documents=json.dumps(r.get('documents',{}),ensure_ascii=False)))
pd.DataFrame(rows).to_csv(O/'registry_coverage.csv',index=False)
neg_ids={r['id'] for r in negative};covered=neg_ids&set(cat.study)
verification=dict(status='passed',comparison_rows=len(cat),named_specifications=cat.name.nunique(),unique_position_paths=len(set(identities.values())),
 registry_records=len(reg),negative_records=len(negative),negative_signal_records=len(negative)-2,
 negative_records_with_direct_replay=len(covered),negative_records_with_replay_including_alias=len(covered)+1,
 independent_engine_comparisons=len(risk),max_primary_metric_error=maxerr,frozen_source_hashes_checked=len(sources),archived_ledgers_checked=len(ledger_checks),
 attribution_rows=len(a),robust_sharpe_flips=int(rob.robust_sharpe_flip.sum()),robust_both_flips=int(rob.robust_both_flip.sum()))
(O/'verification.json').write_text(json.dumps(verification,indent=2));print(json.dumps(verification,indent=2))
print(summary[(summary.lag==1)&summary['mask'].eq('all_10')].to_string(index=False))
print('ROBUST',cat[cat.candidate.isin(rob.index[rob.robust_sharpe_flip])][['name','comparison_scope']].to_string(index=False))
