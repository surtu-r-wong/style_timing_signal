"""Fixed B_neg source ablation. Existing immutable input runs are read-only."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect, _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import residual_past, leg_rules, prepare_close_market, batch_close_ledgers
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'backtest/output/runs/20260916-ew-flow-source-ablation-r1'
PRIOR = ROOT/'backtest/output/runs/20260916-ew-intraday-flow-pilot-r1'
CODES = {'CSI300':'000300.SH','CHINEXT':'399102.SZ','STAR':'000680.SH'}


def capture():
    receipts = {}
    for filename in ['money_flow.csv','board_prices.csv','futures.csv','spot.csv','equal_weight.csv']:
        source = PRIOR/'inputs'/filename
        shutil.copyfile(source,RUN/'inputs'/filename)
        receipts[filename] = artifact_record(source,ROOT)
    for filename in ['features.csv','verification.json','contract_schedule.csv']:
        source = PRIOR/'outputs'/filename
        shutil.copyfile(source,RUN/'inputs'/('prior_'+filename))
        receipts['prior_'+filename] = artifact_record(source,ROOT)
    conn = _connect(load_db_config())
    inventory = {}
    try:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute('SELECT transaction_timestamp()::text')
            inventory['observed_at'] = q.fetchone()[0]
            q.execute("SELECT trade_date,index_code,close FROM stock_selector.index_daily WHERE index_code='000680.SH' AND trade_date BETWEEN '2014-01-01' AND '2026-09-03' ORDER BY trade_date")
            star = pd.DataFrame(q.fetchall(),columns=['date','index_code','close'])
            q.execute("SELECT table_schema,table_name,column_name FROM information_schema.columns WHERE table_schema IN ('stock_selector','public') AND (table_name ILIKE '%money%flow%' OR column_name ILIKE '%inflow%' OR column_name ILIKE '%outflow%' OR column_name ILIKE 'mfd_%') ORDER BY 1,2,ordinal_position")
            inventory['money_flow_catalog'] = q.fetchall()
            q.execute("SELECT index_code,min(effective_date),max(effective_date),count(DISTINCT effective_date),count(*) FROM stock_selector.index_constituent WHERE index_code IN ('000300.SH','000905.SH','000852.SH','932000.CSI') GROUP BY 1 ORDER BY 1")
            inventory['constituent_coverage'] = q.fetchall()
        conn.rollback()
    finally:
        conn.close()
    star.to_csv(RUN/'inputs/star_prices.csv',index=False)
    (RUN/'inputs/database_inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2,default=str))
    (RUN/'inputs/source_receipts.json').write_text(json.dumps(receipts,indent=2))
    for path in ['backtest/run_flow_source_ablation.py','backtest/intraday_flow_pilot.py',
                 'backtest/run_intraday_flow_pilot.py','backtest/execution_ledger.py','backtest/data.py',
                 'backtest/metrics.py','backtest/run_manifest.py']:
        shutil.copyfile(ROOT/path,RUN/'inputs'/('code__'+path.replace('/','__')))


def source_scores(flow,board,calendar):
    px = board.pivot(index='date',columns='index_code',values='close').reindex(calendar)
    smoothed, residuals = {}, {}
    for name,code in CODES.items():
        d = flow[flow.index_code.eq(code)].set_index('trade_date').reindex(calendar)
        gross = (d.main_in_money+d.main_out_money).where(lambda x:x>0)
        y = (d.end_main_inflow_money-d.open_main_inflow_money)/gross
        controls = pd.DataFrame({'flow':(d.main_in_money-d.main_out_money)/gross,
                                 'ret':px[code].pct_change(fill_method=None)},index=calendar)
        residuals[name] = residual_past(y,controls,window=250)
        smoothed[name] = residuals[name].rolling(20,min_periods=20).mean()
    scores = pd.DataFrame(smoothed,index=calendar)
    # Linear averaging commutes with smoothing only on complete common windows.
    scores['PAIR'] = scores[['CSI300','CHINEXT']].mean(axis=1).where(scores[['CSI300','CHINEXT']].notna().all(axis=1))
    scores['TRIPLE'] = scores[['CSI300','CHINEXT','STAR']].mean(axis=1).where(scores[['CSI300','CHINEXT','STAR']].notna().all(axis=1))
    return scores,pd.DataFrame(residuals,index=calendar)


def main():
    capture()
    flow = pd.read_csv(RUN/'inputs/money_flow.csv',parse_dates=['trade_date'])
    board = pd.concat([pd.read_csv(RUN/'inputs/board_prices.csv',parse_dates=['date']),
                       pd.read_csv(RUN/'inputs/star_prices.csv',parse_dates=['date'])],ignore_index=True)
    spot = pd.read_csv(RUN/'inputs/spot.csv',parse_dates=['date'])
    futures = pd.read_csv(RUN/'inputs/futures.csv',parse_dates=['date'])
    ew = pd.read_csv(RUN/'inputs/equal_weight.csv',index_col='date',parse_dates=True).factor_value
    calendar = pd.DatetimeIndex(sorted(spot.loc[spot.date<='2026-09-03','date'].unique()),name='date')
    scores,residuals = source_scores(flow,board,calendar)
    old = pd.read_csv(RUN/'inputs/prior_features.csv',header=[0,1],index_col=0,parse_dates=True)[('score','B')].reindex(calendar)
    np.testing.assert_allclose(scores.PAIR,old,rtol=0,atol=1e-12,equal_nan=True)
    prior_info = json.loads((RUN/'inputs/prior_verification.json').read_text())
    long_idx = calendar[calendar>=prior_info['sample_start']]
    all_valid = scores.notna().all(axis=1)
    if not all_valid.any():
        raise ValueError('no three-source common window')
    short_idx = calendar[calendar>=all_valid[all_valid].index[0]]
    if not all_valid.reindex(short_idx).all():
        raise ValueError('gap in three-source common window')
    scores.to_csv(RUN/'outputs/source_scores.csv',index_label='date')
    residuals.to_csv(RUN/'outputs/source_residuals.csv',index_label='date')
    expiries = {}
    full_calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol)); following = full_calendar[full_calendar>=expiry]
        expiries[symbol] = following[0] if len(following) else expiry
    first_im = futures.loc[futures.symbol.str.startswith('IM'),'date'].min()
    rows, overlap, checks, definitions = [], [], [], []
    for scope,idx,names in [('long_window',long_idx,['CSI300','CHINEXT','PAIR']),
                            ('common_short',short_idx,['CSI300','CHINEXT','STAR','PAIR','TRIPLE'])]:
        feat = scores.loc[idx,names]
        if feat.isna().any().any() or ew.reindex(idx).isna().any():
            raise ValueError('feature or incumbent gap')
        longs,shorts = leg_rules(ew.reindex(idx),feat)
        sbase,lbase = shorts['base'],longs['base']
        targets = {'C__base':lbase+sbase,'C__half':lbase+.5*sbase,'S__base':sbase,'S__half':.5*sbase}
        for name in names:
            selected = shorts[name+'_neg']
            targets['C__'+name] = lbase+selected
            targets['S__'+name] = selected
            short_days = sbase.lt(0)
            overlap.append({'scope':scope,'source':name,'original_short_decision_days':int(short_days.sum()),
                            'kept_short_decision_days':int(selected.lt(0).sum()),
                            'fraction_kept':float(selected.lt(0).sum()/short_days.sum()),
                            'score_mean':float(feat[name].mean()),'score_sd':float(feat[name].std()),
                            'corr_with_pair':float(feat[name].corr(feat.PAIR)),
                            'disagreement_with_pair_on_short_days':int(((selected!=shorts['PAIR_neg'])&short_days).sum())})
        weights = futures_weights(idx,first_im)
        market = prepare_close_market(futures,idx,weights,expiries)
        signals0 = pd.DataFrame(targets,index=idx)
        split = len(idx)//2
        windows = {'full':idx,'early_half':idx[:split],'late_half':idx[split:],
                   '2024-2026':idx[idx>='2024-01-01']}
        windows.update({f'year_{year}':idx[idx.year==year] for year in sorted(set(idx.year))})
        definitions.append({'scope':scope,'start':str(idx[0].date()),'end':str(idx[-1].date()),'n':len(idx),'sources':names,'half_split_after':str(idx[split-1].date())})
        feat.corr().to_csv(RUN/f'outputs/score_correlation_{scope}.csv')
        for scenario,cost,lag in [('close_3bps',3.,0),('close_10bps',10.,0),('second_close_3bps',3.,1)]:
            print(scope,scenario,flush=True)
            signals = signals0.shift(lag,fill_value=0.)
            ledgers = batch_close_ledgers(market,signals,cost_bps=cost)
            if scope=='long_window':
                prior = pd.read_csv(PRIOR/f'outputs/ledgers_{scenario}.csv.gz',index_col=['strategy','date'],parse_dates=['date'])
                for name,prior_name in [('C__base','C__base__base'),('C__PAIR','C__base__B_neg'),('S__PAIR','S__B_neg')]:
                    np.testing.assert_allclose(ledgers[name].ret,prior.loc[prior_name,'ret'],atol=1e-12,rtol=1e-10)
                checks.append({'scope':scope,'scenario':scenario,'prior_frozen_base_pair_reproduced':True})
            elif scenario=='close_3bps':
                reference = contract_ledger(futures,signals['C__STAR'],weights,expiries=expiries,cost_bps=cost)
                np.testing.assert_allclose(reference.ret,ledgers['C__STAR'].ret,atol=1e-12,rtol=1e-10)
                checks.append({'scope':scope,'scenario':scenario,'original_engine_star_reference':True})
            for name,ledger in ledgers.items():
                np.testing.assert_allclose(ledger.gross_pnl-ledger.cost,ledger.equity.diff().fillna(ledger.equity.iloc[0]-1),atol=1e-12,rtol=1e-10)
                for window,dates in windows.items():
                    if len(dates):
                        rows.append({'scope':scope,'scenario':scenario,'strategy':name,'window':window,
                                     'start':str(dates[0].date()),'end':str(dates[-1].date()),**describe(ledger.loc[dates])})
            signals.to_csv(RUN/f'outputs/targets_{scope}_{scenario}.csv',index_label='date')
            pd.concat(ledgers,names=['strategy','date']).to_csv(RUN/f'outputs/ledgers_{scope}_{scenario}.csv.gz')
    pd.DataFrame(rows).to_csv(RUN/'outputs/metrics.csv',index=False)
    pd.DataFrame(overlap).to_csv(RUN/'outputs/source_overlap.csv',index=False)
    verification = {'sample_definitions':definitions,'checks':checks,'prior_score_reproduced_atol':1e-12,
                    'all_72_account_pnl_identities_passed':True,'rules_and_directions_unchanged':True}
    (RUN/'outputs/research_verification.json').write_text(json.dumps(verification,indent=2))
    print(pd.DataFrame(rows).query("scenario=='close_3bps' and window=='full'")[['scope','strategy','cagr','sharpe','maxdd']].round(4).to_string(index=False),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Connection details may include secrets; report only the class.
        raise SystemExit('Source ablation failed: '+type(exc).__name__) from None
