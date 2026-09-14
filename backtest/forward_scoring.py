"""Frozen C1/C2 model scoring, with no interim prospective return release."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, combine_pools
from backtest.execution_audit import stats
from backtest.forward_market import verify_market, metadata, _implementation
from backtest.forward_observation import verify_campaign


def score_frames(spot,futures,sessions,receipts,*,cost=3.):
    """Pure historical/test harness. Receipts contain only timely recorded signals."""
    idx=pd.DatetimeIndex(sessions)
    if len(idx)<2 or not idx.is_unique or not idx.is_monotonic_increasing: raise ValueError('invalid scoring calendar')
    spot=spot.copy();futures=futures.copy()
    for d in (spot,futures): d['date']=pd.to_datetime(d.date)
    for symbol in ('000905.SH','000852.SH'):
        days=pd.DatetimeIndex(spot.loc[spot.symbol.eq(symbol),'date']).sort_values()
        if not days.equals(idx): raise ValueError('spot quote calendar incomplete or duplicated')
    for group in ('IC','IM'):
        days=pd.DatetimeIndex(futures.loc[futures.symbol.str.startswith(group),'date'].unique()).sort_values()
        if not days.equals(idx): raise ValueError('futures quote calendar incomplete')
    values={}
    for name in ('equal_weight','slope20'):
        raw=pd.Series({pd.Timestamp(d):float(r['signals'][name]) for d,r in receipts.items()},dtype=float)
        if not np.isfinite(raw).all() or raw.abs().gt(1).any(): raise ValueError('invalid receipt signal')
        # Only record-day observations within the scoring interval can set targets.
        values[name]=raw.reindex(idx).ffill().fillna(0.)
    signals={'futures_ew':np.sign(values['equal_weight']),
             'futures_always_long':pd.Series(1.,index=idx),
             'spot_slope':values['slope20'].gt(0).astype(float),
             'spot_ew':values['equal_weight'].gt(0).astype(float)}
    expiry={s:pd.Timestamp(_expiry_from_symbol(s)) for s in futures.symbol.unique()}
    ledgers={}
    for name,s in signals.items():
        is_fut=name.startswith('futures')
        ledgers[name]=contract_ledger(futures if is_fut else spot,s,
             {'IC':.5,'IM':.5} if is_fut else {'000905.SH':.5,'000852.SH':.5},
             fill='close',cost_bps=cost,select_main=is_fut,expiries=expiry if is_fut else None)
    ledgers['current_two_pool']=combine_pools(ledgers['spot_slope'],ledgers['futures_ew'])
    ledgers['ew_two_pool']=combine_pools(ledgers['spot_ew'],ledgers['futures_ew'])
    missing=[d.date().isoformat() for d in idx if d.date().isoformat() not in receipts]
    streak=longest=0
    for d in idx:
        streak=streak+1 if d.date().isoformat() in missing else 0
        longest=max(longest,streak)
    quality={'missing_signal_sessions':missing,'signal_coverage':1-len(missing)/len(idx),
             'longest_signal_gap':longest,'calendar_sessions':len(idx),'completed_fill_sessions':len(idx)-1,
             'observation_start':idx[1].date().isoformat(),'observation_end':idx[-1].date().isoformat(),
             'actual_account_returns':False,'go_enabled':False}
    return ledgers,quality


def quality_report(market):
    meta=metadata(market); state=verify_market(market)
    signal=verify_campaign(meta['campaign'])
    dates={p.name for p in (Path(meta['campaign'])/'records').iterdir()}
    state['missing_signal_sessions']=sorted(set(state['sessions'])-dates)
    state['signal_records']=signal['records']
    state['returns_released']=False
    state['observation_sessions_required']=meta['observation_sessions']
    observation_dates=set(state['sessions'][:meta['observation_sessions']+1])
    state['score_ready']=not (set(state['missing_market_sessions']) & observation_dates) and state['completed_fill_sessions']>=meta['observation_sessions']
    state['go_enabled']=False
    return state


def score_campaign(market,output):
    meta=metadata(market); quality=quality_report(market)
    if _implementation()!=meta['implementation_sha256']: raise ValueError('scoring implementation changed')
    if not quality['score_ready']: raise ValueError('interim quality only: need 504 complete fill sessions and no market gaps')
    idx=pd.to_datetime(quality['sessions'][:meta['observation_sessions']+1])
    market=Path(market)
    spot=pd.concat([pd.read_csv(market/'records'/d.date().isoformat()/'spot.csv') for d in idx],ignore_index=True)
    fut=pd.concat([pd.read_csv(market/'records'/d.date().isoformat()/'futures.csv') for d in idx],ignore_index=True)
    receipts={p.name:json.loads((p/'record.json').read_text()) for p in (Path(meta['campaign'])/'records').iterdir()}
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    rows=[]; comparisons=[]
    for cost in (3.,10.):
        ledgers,q=score_frames(spot,fut,idx,receipts,cost=cost)
        for name,ledger in ledgers.items():
            ledger.to_csv(output/f'{name}_{cost:g}bps.csv',index_label='date')
            rows.append({'name':name,'cost_bps':cost,**stats(ledger.ret.iloc[1:])})
        for name,a,b in [('C1','futures_ew','futures_always_long'),('C2','current_two_pool','ew_two_pool')]:
            comparisons.append({'comparison':name,'cost_bps':cost,
                'sharpe_difference':stats(ledgers[a].ret.iloc[1:])['sharpe']-stats(ledgers[b].ret.iloc[1:])['sharpe']})
    pd.DataFrame(rows).to_csv(output/'metrics.csv',index=False)
    pd.DataFrame(comparisons).to_csv(output/'comparisons_descriptive.csv',index=False)
    (output/'quality.json').write_text(json.dumps(q,indent=2))
    # No statistical test/GO: its independent protocol remains unfrozen.
    return output


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['quality','score']);ap.add_argument('--market',type=Path,required=True);ap.add_argument('--output',type=Path)
    a=ap.parse_args()
    if a.action=='score':
        if a.output is None: ap.error('--output required')
        print(score_campaign(a.market,a.output))
    else: print(json.dumps(quality_report(a.market),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
