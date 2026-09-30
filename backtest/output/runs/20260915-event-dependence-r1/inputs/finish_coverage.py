"""Close remaining readily replayable source gaps without changing candidate specs."""
import audit as a
from pathlib import Path
import json,sys,hashlib
import numpy as np
import pandas as pd
from backtest.rotation_probe import hold_position
ROOT,RUN,BT,SOURCE=a.ROOT,a.RUN,a.BT,a.SOURCE
MAIN=a.OUT
EXTRA=MAIN/'extra2';EXTRA.mkdir(exist_ok=True)
m=pd.read_csv(MAIN/'market.csv',index_col='date',parse_dates=True);u,carry=m.underlying,m['carry'];cal=u.index
refs=dict(pd.read_csv(MAIN/'reference_positions.csv',index_col='date',parse_dates=True).items())
records,positions,issues=[],{},[]
def add(name,study,pos,mapping='sym',detail='',role='frozen_representative'):
    s=pos.astype(float).reindex(cal).dropna()
    if len(s)<100:return
    if not s.index.equals(cal[(cal>=s.index.min())&(cal<=s.index.max())]):
        issues.append({'study':study,'name':name,'reason':'internal calendar gaps; not silently filled'});return
    positions[name]=s
    for model,ref in ([('carry','ew_sym')] if mapping=='sym' else [('carry','ew_lf'),('spot','slope_lf')]):
        records.append(dict(candidate=f'{name}__{model}__{ref}',name=name,study=study,mapping=mapping,model=model,reference=ref,
            role=role,detail=detail,start=str(s.index.min().date()),end=str(s.index.max().date()),n=len(s),position_hash=hashlib.sha256(s.to_csv().encode()).hexdigest()))
def guarded(study,fn):
    try:fn();print('built',study,flush=True)
    except Exception as e:issues.append(dict(study=study,name='',reason=type(e).__name__));print('issue',study,type(e).__name__,flush=True)

def overnight():
    from backtest.overnight_probe import load_pg_ohlc,STYLE_NAMES,build_candidates,INCUMBENT
    op,cp=RUN/'inputs/ohl_open.csv',RUN/'inputs/ohl_close.csv'
    if op.exists():o,c=pd.read_csv(op,index_col=0,parse_dates=True),pd.read_csv(cp,index_col=0,parse_dates=True)
    else:
        o,c=load_pg_ohlc(STYLE_NAMES);o,c=o.loc[:'2026-09-11'],c.loc[:'2026-09-11'];o.to_csv(op,index_label='date');c.to_csv(cp,index_label='date')
    fs=build_candidates(o,c)
    for name,f in fs.items():
        if name==INCUMBENT:continue
        add('overnight_'+name,'overnight-intraday-decomposition',np.sign(f),detail='original citic40d transform; compared to current EW; original citic benchmark gate not reevaluated')
        add('overnight_lf_'+name,'overnight-intraday-decomposition',(f>0).astype(float),'lf',detail='original citic40d transform, current EW/slope references')
guarded('overnight-intraday-decomposition',overnight)

def erp():
    from backtest.erp_probe import _load_edb_series,build_erp_signals,_PE_CODE,_Y10_CODE
    path=RUN/'inputs/erp_sources.csv'
    if path.exists():d=pd.read_csv(path,index_col=0,parse_dates=True)
    else:
        d=pd.concat([_load_edb_series(_PE_CODE).rename('pe'),_load_edb_series(_Y10_CODE).rename('y10')],axis=1).loc[:'2026-09-11'];d.to_csv(path,index_label='date')
    sigs=build_erp_signals(d.pe.dropna(),d.y10.dropna())
    v=pd.read_csv(a.freeze(BT/'erp_probe_verdicts.csv'))
    for r in v.itertuples():add('erp_'+r.family,'long-axes-probes',hold_position(sigs[r.family][r.best_form]*r.direction,int(r.best_k)),detail=f'ERP original {r.best_form}; k={r.best_k}')
guarded('long-axes-probes',erp)

def dividend():
    from backtest.pair_set_probe import load_dividend_closes,build_factor as original
    from backtest.pair_set_probe_5b import build_factor,CANDIDATE
    from signals.common.config import load_db_config
    path=RUN/'inputs/dividend_prices.csv'
    if path.exists():d=pd.read_csv(path,index_col=0,parse_dates=True)
    else:
        d=load_dividend_closes(load_db_config(),codes={'000300.SH':'沪深300','000922.CSI':'中证红利','H00922.CSI':'中证红利全收益'}).loc[:'2026-09-11'];d.to_csv(path,index_label='date')
    prices=a.csv(SOURCE/'inputs/style_prices.csv').join(d)
    for name,f in [('original_dividend',original(prices,'D_four_plus_dividend')),('price_partner',build_factor(prices,CANDIDATE))]:
        add('dividend_'+name,'pair-set',(f>0).astype(float),'lf',detail='fixed original dividend variant; original correlation/IC gate remains separate')
guarded('pair-set',dividend)

pd.DataFrame(records).to_csv(EXTRA/'candidate_catalog.csv',index=False)
pd.DataFrame(positions).to_csv(EXTRA/'candidate_positions.csv',index_label='date')
pd.DataFrame(issues).to_csv(EXTRA/'build_issues.csv',index=False)
a.OUT=EXTRA
a.analyze(pd.DataFrame(records),positions,refs,u,carry)
for filename in ['candidate_catalog.csv','event_attribution.csv','event_position_response.csv','primary_comparisons.csv','rank_flips.csv']:
    pd.concat([pd.read_csv(MAIN/filename),pd.read_csv(EXTRA/filename)],ignore_index=True).to_csv(MAIN/filename,index=False)
pd.concat([pd.read_csv(MAIN/'candidate_positions.csv',index_col='date',parse_dates=True),pd.DataFrame(positions)],axis=1).to_csv(MAIN/'candidate_positions.csv',index_label='date')
merged=json.loads((MAIN/'input_sources.json').read_text());merged.update(a.SOURCES)
(MAIN/'input_sources.json').write_text(json.dumps(merged,indent=2))
print('FINISH COVERAGE',len(records),issues,flush=True)
