"""Frozen execution sensitivity research. No production mapping changes.

python -m backtest.execution_audit --run-id ID [--snapshot-from PRIOR_RUN]
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect, _expiry_from_symbol, annualized_basis
from backtest.engine import run_strategy
from backtest.execution_ledger import contract_ledger, combine_pools, futures_weights
from backtest.metrics import ann_return, sharpe, max_drawdown
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff
from backtest.run_manifest import create_run_dir, artifact_record, git_state, write_manifest
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT/'backtest/output/runs'
START, END = '2015-04-16', '2026-09-11'
WINDOWS = {'full':(START,END),'dual_listed':('2022-07-25',END),
           '2015-2020':(START,'2020-12-31'),'2021-2023':('2021-01-01','2023-12-31'),
           '2024-2026':('2024-01-01',END)}
PLAN = ROOT/'docs/superpowers/plans/2026-09-14-research-followthrough.md'


def begin(run_id):
    run=create_run_dir(RUN_ROOT,run_id)
    shutil.copyfile(PLAN,run/'inputs/prereg.md')
    write_manifest(run,{'status':'running','created_utc':datetime.now(timezone.utc).isoformat(),'git':git_state(ROOT)})
    return run


def finish(run, stage, extra=None):
    code=run/'inputs/code'; code.mkdir(exist_ok=True)
    modules=('execution_audit','execution_ledger','metrics','engine','data','paired_bootstrap',
             'research_statistics','candidate_completion','selection_permutation','rotation_probe',
             'leverage_probe','gate0_criterion_study','research_data_inventory','short_leg_audit')
    for name in modules:
        src=ROOT/f'backtest/{name}.py'
        if src.exists(): shutil.copyfile(src,code/src.name)
    for path in ('signals/equal_weight/generate_signal.py','signals/common/factors.py'):
        src=ROOT/path
        if src.exists(): shutil.copyfile(src,code/(path.replace('/','__')))
    files=[p for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
    write_manifest(run,{'status':'complete','stage':stage,'completed_utc':datetime.now(timezone.utc).isoformat(),
                        'git':git_state(ROOT),'artifacts':[artifact_record(p,run) for p in files],**(extra or {})})


def capture(run):
    cfg=load_db_config(); conn=_connect(cfg)
    try:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='60s'")
            q.execute('SELECT transaction_timestamp()::text'); stamp=q.fetchone()[0]
            q.execute("SELECT trade_date,index_code,open,close FROM stock_selector.index_daily WHERE index_code IN ('000905.SH','000852.SH') AND trade_date BETWEEN '2014-01-01' AND %s ORDER BY 1,2",(END,))
            spot=pd.DataFrame(q.fetchall(),columns=['date','symbol','open','close'])
            q.execute("SELECT trade_date,symbol,open,close,oi,settle,volume FROM public.futures_daily WHERE (symbol LIKE %s OR symbol LIKE %s) AND trade_date<=%s ORDER BY 1,2",('IC%','IM%',END))
            fut=pd.DataFrame(q.fetchall(),columns=['date','symbol','open','close','oi','settle','volume'])
        conn.rollback()
    finally: conn.close()
    spot['oi']=1.
    spot.to_csv(run/'inputs/spot.csv',index=False); fut.to_csv(run/'inputs/futures.csv',index=False)
    for name,path in [('equal_weight','output/equal_weight/equal_weight_signal_20d40z.csv'),
                      ('slope20','output/slope20/slope20_signal_L20zw120.csv')]:
        d=pd.read_csv(ROOT/path); d=d[d.date<=END]; d.to_csv(run/f'inputs/{name}.csv',index=False)
    (run/'inputs/snapshot.json').write_text(json.dumps({'transaction_timestamp':stamp,'end':END,
        'spot_rows':len(spot),'futures_rows':len(fut),'spot_open_valid':int(pd.to_numeric(spot.open,errors='coerce').gt(0).sum()),
        'signal_note':'local production CSV frozen alongside repeatable-read price snapshot'},indent=2))


def copy_snapshot(source, run):
    for f in ('spot.csv','futures.csv','equal_weight.csv','slope20.csv','snapshot.json'):
        shutil.copyfile(Path(source)/'inputs'/f,run/'inputs'/f)


def inputs(run):
    spot=pd.read_csv(run/'inputs/spot.csv',parse_dates=['date'])
    fut=pd.read_csv(run/'inputs/futures.csv',parse_dates=['date'])
    closes=spot.pivot(index='date',columns='symbol',values='close')
    if closes.isna().any().any(): raise ValueError('spot calendar differs between legs')
    calendar=closes.index
    index=calendar[(calendar>=START)&(calendar<=END)]
    signals={}
    for name in ('equal_weight','slope20'):
        d=pd.read_csv(run/f'inputs/{name}.csv',index_col='date',parse_dates=True)
        s=d.factor_value.reindex(index)
        if s.isna().any(): raise ValueError(f'missing {name} signal on market calendar')
        signals[name]=np.sign(s)
    dates=set(fut.date)
    if any(t not in dates for t in index): raise ValueError('futures market day missing')
    first_im=fut.loc[fut.symbol.str.startswith('IM'),'date'].min()
    if pd.isna(first_im): raise ValueError('IM missing')
    w=futures_weights(index,first_im)
    expiries={}
    for sym in fut.symbol.unique():
        expiry=pd.Timestamp(_expiry_from_symbol(sym))
        next_dates=calendar[calendar>=expiry]
        expiries[sym]=next_dates[0] if len(next_dates) else expiry
    und=closes.pct_change().mean(axis=1)
    carry_legs={}
    for group,code in [('IC','000905.SH'),('IM','000852.SH')]:
        ff=fut[fut.symbol.str.startswith(group)].sort_values(['date','symbol'])
        main=ff.loc[ff.groupby('date').oi.idxmax()]
        carry_legs[group]=pd.Series({r.date:annualized_basis(r.close,closes.at[r.date,code],r.date.date(),r.symbol)
                                     for r in main.itertuples()})
    carry=pd.concat(carry_legs,axis=1).fillna(0).mean(axis=1)
    return spot,fut,index,signals,w,expiries,und,carry


def stats(ret):
    r=ret.astype(float)
    if r.isna().any(): raise ValueError('missing daily return')
    q=r.quantile(.05)
    return {'n':len(r),'ann':ann_return(r),'cagr':float(np.expm1(np.log1p(r).sum()*245/len(r))),
            'sharpe':sharpe(r),'maxdd':max_drawdown(r),'vol':float(r.std(ddof=1)*np.sqrt(245)),
            'es05':float(r[r<=q].mean()),'worst_day':float(r.min())}


def panel(ledgers):
    rows=[]
    for name,d in ledgers.items():
        for window,(a,b) in WINDOWS.items():
            x=d.loc[a:b]
            if len(x)<60: continue
            row={'name':name,'window':window,'start':str(x.index[0].date()),'end':str(x.index[-1].date()),**stats(x.ret)}
            if 'cost_return' in x:
                row.update(cost_ann=float(x.cost_return.mean()*245),turnover_ann=float(x.turnover.mean()*245),
                           rolls=int(x.rolls.sum()),gross_notional_max=float(x.gross_notional.max()))
            rows.append(row)
    return pd.DataFrame(rows)


def execute(run):
    spot,fut,idx,s,w,exp,und,carry=inputs(run)
    ledgers={}
    for cost in (3.,10.):
        spot_ledger=contract_ledger(spot,s['slope20'].clip(lower=0),{'000905.SH':.5,'000852.SH':.5},
                                   select_main=False,fill='close',cost_bps=cost)
        ledgers[f'spot_close_{cost:g}bps']=spot_ledger
        for fill in ('close','open'):
            f=contract_ledger(fut,s['equal_weight'],w,fill=fill,cost_bps=cost,expiries=exp)
            ledgers[f'futures_{fill}_{cost:g}bps']=f
            for mode in ('separate','daily_rebalanced'):
                ledgers[f'two_pool_spotclose_fut{fill}_{cost:g}bps_{mode}']=combine_pools(spot_ledger,f,rebalance=mode=='daily_rebalanced')
    old_spot=run_strategy(s['slope20'].clip(lower=0),und,3.,None)
    old_fut=run_strategy(s['equal_weight'],und,3.,carry)
    ledgers['legacy_spotcarry_daily_rebalanced']=combine_pools(old_spot,old_fut,rebalance=True)
    ledgers['legacy_spotcarry_separate']=combine_pools(old_spot,old_fut)
    for name,d in ledgers.items(): d.to_csv(run/f'outputs/ledger_{name}.csv',index_label='date')
    report=panel(ledgers); report.to_csv(run/'outputs/execution_panel.csv',index=False)
    pairs=[]
    for name in ('two_pool_spotclose_futclose_3bps_separate','two_pool_spotclose_futopen_3bps_separate'):
        for window in ('full','dual_listed'):
            a,b=WINDOWS[window]
            pair=paired_block_bootstrap_sharpe_diff(ledgers[name].ret.loc[a:b],ledgers['legacy_spotcarry_separate'].ret.loc[a:b],block=20,n=2000,seed=20260914)
            pairs.append({'name':name,'reference':'legacy_spotcarry_separate','window':window,**pair})
    pd.DataFrame(pairs).to_csv(run/'outputs/execution_paired.csv',index=False)
    (run/'outputs/limitations.json').write_text(json.dumps({
        'spot_proxy':True,'actual_spot_account_verified':False,'fractional_contracts':True,'mark':'close, not settlement cash balance',
        'cash_interest':0,'margin_and_limit_locks_simulated':False,'integer_lots':False,
        'pool_rule':'initial half, no transfers; daily half only sensitivity',
        'pre_IM_rule':'100% IC futures; spot remains 500/1000 index proxy',
        'first_trading_day':'initialize flat; first decision uses next market day'},indent=2))
    print(report[(report.window.isin(['full','dual_listed'])) & (~report.name.str.contains('10bps|daily_rebalanced'))][['name','window','ann','sharpe','maxdd']].round(4).to_string(index=False),flush=True)
    return ledgers


def risk(run):
    spot,fut,idx,s,w,exp,und,carry=inputs(run)
    vol=und.rolling(60,min_periods=60).std()*np.sqrt(245)
    scale=(.15/vol).clip(upper=1).reindex(idx)
    if scale.isna().any(): raise ValueError('risk volatility burn-in missing')
    variants={'current':(s['slope20'].clip(lower=0),s['equal_weight']),
              'half_short':(s['slope20'].clip(lower=0),s['equal_weight'].where(s['equal_weight']>=0,s['equal_weight']*.5)),
              'vol15_cap1':(s['slope20'].clip(lower=0)*scale,s['equal_weight']*scale)}
    ledgers={}; risk_positions=[]
    for name,(sp,fp) in variants.items():
        a=contract_ledger(spot,sp,{'000905.SH':.5,'000852.SH':.5},select_main=False,fill='close',cost_bps=3)
        b=contract_ledger(fut,fp,w,fill='close',cost_bps=3,expiries=exp)
        d=combine_pools(a,b); ledgers[name]=d
        capital=.5*a.equity+.5*b.equity
        futures_share=.5*b.equity/capital
        d['futures_capital_share']=futures_share
        d['futures_notional_over_total']=futures_share*b.gross_notional
        d['total_gross_notional']=(.5*a.equity*a.gross_notional+.5*b.equity*b.gross_notional)/capital
        d['cost_return']=(.5*a.cost+.5*b.cost)/capital.shift(1,fill_value=1)
        for year,g in d.groupby(d.index.year):
            risk_positions.append({'name':name,'year':year,**stats(g.ret),
                                   'gross_notional_max':float(g.total_gross_notional.max()),
                                   'futures_capital_share_end':float(g.futures_capital_share.iloc[-1]),
                                   'cost_ann':float(g.cost_return.mean()*245)})
        d.to_csv(run/f'outputs/risk_ledger_{name}.csv',index_label='date')
    # panel expects cost/turnover fields only when all are present.
    p=panel({name:d[['ret','equity']] for name,d in ledgers.items()}); p.to_csv(run/'outputs/risk_panel.csv',index=False)
    pd.DataFrame(risk_positions).to_csv(run/'outputs/risk_yearly.csv',index=False)
    pairs=[]
    for name in ('half_short','vol15_cap1'):
        for window in ('full','dual_listed','2024-2026'):
            a,b=WINDOWS[window]
            for block in (20,60):
                pairs.append({'name':name,'window':window,**paired_block_bootstrap_sharpe_diff(ledgers[name].ret.loc[a:b],ledgers['current'].ret.loc[a:b],block=block,n=2000,seed=20260914)})
    pd.DataFrame(pairs).to_csv(run/'outputs/risk_paired.csv',index=False)
    print(p[p.window.isin(['full','dual_listed'])][['name','window','ann','sharpe','maxdd','es05']].round(4).to_string(index=False),flush=True)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--run-id',required=True); ap.add_argument('--snapshot-from',type=Path)
    ap.add_argument('--stage',choices=['execution','risk'],default='execution'); a=ap.parse_args()
    run=begin(a.run_id)
    if a.snapshot_from: copy_snapshot(a.snapshot_from,run)
    else: capture(run)
    if a.stage=='execution': execute(run)
    else: risk(run)
    finish(run,a.stage)
    print(run.relative_to(ROOT))


if __name__=='__main__': main()
