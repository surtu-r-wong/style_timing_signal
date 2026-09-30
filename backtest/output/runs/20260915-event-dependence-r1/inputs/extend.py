"""Additional original representatives; no new parameter search."""
import importlib.util
from pathlib import Path
import sys,json,hashlib
import numpy as np
import pandas as pd
import audit as a
from backtest.rotation_probe import hold_position
from backtest.leverage_probe import level_signal
from signals.equal_weight.generate_signal import load_pair_configs
ROOT,RUN,BT,SOURCE=a.ROOT,a.RUN,a.BT,a.SOURCE
MAIN=a.OUT
EXTRA=RUN/'outputs/extra';EXTRA.mkdir(exist_ok=True)
market=pd.read_csv(MAIN/'market.csv',index_col='date',parse_dates=True)
u,carry=market.underlying,market['carry'];cal=u.index
refs={k:v for k,v in pd.read_csv(MAIN/'reference_positions.csv',index_col='date',parse_dates=True).items()}
ew=a.csv(SOURCE/'inputs/equal_weight.csv')
prices=a.csv(SOURCE/'inputs/style_prices.csv')
positions,records,issues={},[],[]

def add(name,study,pos,mapping='sym',detail='',role='frozen_representative'):
    aligned=pos.astype(float).sort_index().reindex(cal)
    support=aligned.dropna()
    if len(support)<100:issues.append(dict(study=study,name=name,reason='short sample'));return
    native=aligned.loc[support.index.min():support.index.max()]
    if native.isna().any():
        issues.append(dict(study=study,name=name,reason=f'internal missing dates {int(native.isna().sum())}; no reconstruction performed'));return
    positions[name]=native
    configs=[('carry','ew_sym')] if mapping=='sym' else [('carry','ew_short')] if mapping=='short' else [('carry','ew_lf'),('spot','slope_lf')]
    for model,reference in configs:
        records.append(dict(candidate=f'{name}__{model}__{reference}',name=name,study=study,mapping=mapping,model=model,reference=reference,
                            role=role,detail=detail,start=str(native.index.min().date()),end=str(native.index.max().date()),n=len(native),
                            position_hash=hashlib.sha256(native.to_csv().encode()).hexdigest()))

def guarded(label,fn):
    try: fn();print('built',label,flush=True)
    except Exception as e:
        # Error types only: database exceptions may contain connection details.
        issues.append(dict(study=label,name='',reason=type(e).__name__))
        print('BUILD ISSUE',label,type(e).__name__,flush=True)

# Same-target original threshold grid; other target variants remain separate coverage gaps.
from backtest.threshold_by_underlying_probe import THETAS
for theta in THETAS:
    if theta!=0:add(f'threshold_{theta}','threshold-and-microcap-grids',(ew.factor_value>theta).astype(float),'lf',detail='original blend threshold subgrid; carry columns supplemental',role='rejected_variant')
from backtest.dual import assemble_dual,dual_legs_external_short
add('dual_v1','dual-engine-v1',assemble_dual(ew.factor_value.reindex(cal),carry,.1,.3,.06),detail='frozen v1 long=.1 short=.3 carry=.06')
from backtest.breadth import breadth_divergence
b=a.csv(BT/'breadth.csv')
s=breadth_divergence((1+u).cumprod(),b.pct_above_ma20.reindex(cal),20,20,'deteriorating',.2,10)
add('breadth_dual_default','breadth-divergence',dual_legs_external_short(ew.factor_value.reindex(cal),s,carry,.1,.06)[2],detail='original default; 168-grid not substituted for fixed default')
from backtest.conditional_probe import realized_vol,build_state_percentiles,bucket_labels,adjusted_position
labels=bucket_labels(build_state_percentiles({'S1':realized_vol(u).dropna()})['S1'],'binary')
add('conditional_vol_binary','conditional-modulation',adjusted_position((ew.factor_value>0).astype(float),labels,1).loc[:'2026-06-30'],'lf',detail='frozen S1 binary upper bucket 0.5 weight; initial rolling-history differs from original full market history')

# Self-built style signals: exact committed artifacts.
for name in ['pure_style_U2','self_mixed_U2']:
    f=a.csv(ROOT/f'output/style_basket/signal_{name}.csv').factor_value
    add('b2_'+name,'b2-industry-neutral',(f>0).astype(float),'lf')
    add('b2_sym_'+name,'b2-industry-neutral',np.sign(f),detail='original symmetric reference')

# Divergence and xsection frozen winners, with family-wide common warmup.
from backtest.divergence_probe import divergence_levels,build_variant_signals
from backtest.xsection_probe import xsection_levels
pairs=ew[[f'pair_0{i}_factor_20' for i in range(1,5)]].copy();pairs.columns=['300','500','1000','2000']
def add_dp(study,levels,summary,families):
    signals=build_variant_signals(levels,tuple(families));meta=a.js(BT/summary)
    w=meta['winner_variant'];key=(w['family'],w['lb'],w['zw'],w['k'])
    common=cal
    for s in signals.values():common=common.intersection(s.dropna().index)
    direction=np.sign(meta['winner_ic_by_window']['selection_2014_2023'])
    add(study+'_winner',study,hold_position(signals[key].reindex(common)*direction,w['k']),detail=f'{key}; fixed selection-window direction {direction}; original full-window hold start')
add_dp('divergence-probe',divergence_levels(pairs),'probe_2_divergence_summary.json',['D1','D2','D3'])
add_dp('mian2-cross-section',xsection_levels(a.csv(BT/'xs_dispersion.csv'),a.csv(BT/'avg_correlation.csv')),'probe_xsection_summary.json',['X1','X2'])

from backtest.fund_crowding_probe import build_b_signals,build_a_signals,centered
q=a.csv(BT/'fund_crowding_quarterly.csv')
for col in ['a1_eff_date','a2_eff_date','eff_plus30']:q[col]=pd.to_datetime(q[col])
fmeta=a.js(BT/'probe_7_fund_crowding_summary.json')
for tag,signals in [('family_B',build_b_signals(a.csv(BT/'fund_crowding_beta_daily.csv'))),('family_A',build_a_signals(q,cal))]:
    common=cal
    for s in signals.values():common=common.intersection(s.dropna().index)
    w=fmeta[tag]['winner_variant']
    key=(w['family'],int(w['mode']),int(w['zw']),w['k']) if tag=='family_B' else (w['family'],w['mode'],w['zw'],w['k'])
    direction=np.sign(fmeta[tag]['winner_ic_by_window']['selection_2014_2023'])
    s=centered(signals[key]).reindex(common)
    add('crowding_'+tag,'fund-crowding',hold_position(s*direction,w['k']),detail=str(key))

# Frozen EWMA denominator substitute: core EW definition stays identical.
p=a.freeze(ROOT/'docs/plans/2026-08-26-signal-generator-prescreens/ewma_std_prescreen.py')
spec=importlib.util.spec_from_file_location('frozen_ewma_event',p);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
configs=load_pair_configs(a.freeze(SOURCE/'inputs/config_4pairs.csv'))
f=mod.equal_weight_factor(prices,configs,'ewm').round(4)
add('ewma_std_ew','signal-generator-ewma-std',np.sign(f),detail='EW only; citic/hybrid original variants listed separately as coverage gap')
add('ewma_std_ew_lf','signal-generator-ewma-std',(f>0).astype(float),'lf')

from backtest.rotation_probe import series_signal
mixed=a.csv(ROOT/'output/style_basket/spread_U2.csv').spread
pure=a.csv(ROOT/'output/style_basket/spread_U2_neutral.csv').spread
rot=(mixed-pure).dropna()
add('rotation_lb20sm3k20','rotation-short-window',hold_position(series_signal(rot,20,40,3),20),detail='original documented plateau representative')

# STAR residual original replication had only an IC test, no trading rule: label inferred mapping.
f=a.csv(BT/'money_flow_series.csv')['e_000680.SH'].dropna()
s=level_signal(f,5,250).iloc[249:]
add('money_flow_star','money-flow-cross-section-replication',hold_position(s,20),detail='original positive direction and k20; trading mapping diagnostic only; original rejection was IC replication',role='diagnostic_translation')
# Cross-section basis tests used 300/50 targets; do not mislabel blend replay as original performance test.

# Read-only small PG input additions, frozen after load; no source refresh service.
def more_prices():
    from signals.common.data_source import load_pg_closes
    from backtest.family_unification_formal import CANDIDATE_NAMES
    from signals.equal_weight.generate_signal import PairConfig,calculate_contrast_equal_weight_signal
    path=RUN/'inputs/new_style_prices.csv'
    if path.exists():d=pd.read_csv(path,index_col=0,parse_dates=True)
    else:
        d=load_pg_closes(CANDIDATE_NAMES,end='2026-09-11',trim_ragged_tail=True);d.to_csv(path,index_label='date')
    pcs=[PairConfig(group=i+1,left_column=CANDIDATE_NAMES[2*i],right_column=CANDIDATE_NAMES[2*i+1],direction='forward') for i in range(4)]
    f=calculate_contrast_equal_weight_signal(d,lookback=20,z_window=40,smoothing_window=5,pair_configs=pcs).factor_value
    add('family_pure','family-unification',(f>0).astype(float),'lf',detail='fixed original candidate, fresh read-only PG historical snapshot; not byte-identical old archive guarantee')
guarded('family-unification',more_prices)

def leverage():
    from backtest.leverage_probe import _load_margin,build_signals
    path=RUN/'inputs/margin.csv'
    if path.exists():d=pd.read_csv(path,index_col=0,parse_dates=True);bal,buy=d.bal,d.buy
    else:
        bal,buy=_load_margin();d=pd.DataFrame({'bal':bal,'buy':buy}).loc[:'2026-09-11'];d.to_csv(path,index_label='date');bal,buy=d.bal,d.buy
    signals=build_signals(bal,buy,a.csv(BT/'market_turnover.csv').amt_yuan)
    v=pd.read_csv(a.freeze(BT/'leverage_probe_verdicts.csv'))
    for r in v.itertuples():add('leverage_'+r.family,'leverage-probe',hold_position(signals[r.family][r.best_form]*r.direction,int(r.best_k)),detail=f'{r.best_form}; fixed k={r.best_k}; small PG read frozen')
guarded('leverage-probe',leverage)

pd.DataFrame(records).to_csv(EXTRA/'candidate_catalog.csv',index=False)
pd.DataFrame(positions).to_csv(EXTRA/'candidate_positions.csv',index_label='date')
pd.DataFrame(issues).to_csv(EXTRA/'build_issues.csv',index=False)
a.OUT=EXTRA
a.analyze(pd.DataFrame(records),positions,refs,u,carry)
# Merge without recomputing the unchanged first-batch results.
for filename in ['candidate_catalog.csv','event_attribution.csv','event_position_response.csv','primary_comparisons.csv','rank_flips.csv']:
    old=pd.read_csv(MAIN/filename);new=pd.read_csv(EXTRA/filename)
    pd.concat([old,new],ignore_index=True).to_csv(MAIN/filename,index=False)
old=pd.read_csv(MAIN/'candidate_positions.csv',index_col='date',parse_dates=True)
pd.concat([old,pd.DataFrame(positions)],axis=1).to_csv(MAIN/'candidate_positions.csv',index_label='date')
merged=json.loads((MAIN/'input_sources.json').read_text());merged.update(a.SOURCES)
(MAIN/'input_sources.json').write_text(json.dumps(merged,indent=2))
print('EXTENSION DONE',len(records),issues,flush=True)
