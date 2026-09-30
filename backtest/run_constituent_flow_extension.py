"""Read-only P1 acceptance and preregistered B_neg constituent-source comparison."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect, _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import residual_past, leg_rules, prepare_close_market, batch_close_ledgers, attribute_ledger
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'backtest/output/runs/20260916-ew-constituent-flow-extension-r1'
PRIOR = ROOT/'backtest/output/runs/20260916-ew-flow-source-ablation-r1'
OFFICE = Path('/home/elfbob/claude-code/data_manager/requests/2026-09-16-style-timing-signal-index-money-flow-backfill')
EVIDENCE = Path('/home/elfbob/claude-code/.dbm-evidence/money-flow-p1-20260916.F8Veyt')
CODES = {'CSI300':'000300.SH','CSI500':'000905.SH','CSI1000':'000852.SH','CHINEXT':'399102.SZ','STAR':'000680.SH'}
VALUES = ['open_main_inflow_money','end_main_inflow_money','main_inflow_money',
          'xlarge_buy_money','xlarge_sell_money','large_buy_money','large_sell_money']
GROUPS = {'PAIR':['CSI300','CHINEXT'],'POOL':['CSI500','CSI1000'],
          'CORE':['CSI300','CSI500','CSI1000','CHINEXT'],
          'ALL':['CSI300','CSI500','CSI1000','CHINEXT','STAR']}
MAIN = ['CSI300','CSI500','CSI1000','CHINEXT','PAIR','POOL','CORE']
END = pd.Timestamp('2026-09-11')


def capture():
    if (RUN/'inputs/flow.csv').exists():
        raise ValueError('input snapshot already exists; do not overwrite')
    for name in ['futures.csv','spot.csv','equal_weight.csv']:
        shutil.copyfile(PRIOR/'inputs'/name,RUN/'inputs'/name)
    shutil.copyfile(PRIOR/'outputs/source_scores.csv',RUN/'inputs/wset_scores.csv')
    for name in ['response-03-office-2026-09-16.md','response-06-office-2026-09-16.md']:
        shutil.copyfile(OFFICE/name,RUN/'inputs'/name)
    for name in ['snapshot.csv','identity-outliers.csv']:
        shutil.copyfile(EVIDENCE/name,RUN/'inputs'/('office_'+name))
    for name in ['run_constituent_flow_extension.py','intraday_flow_pilot.py','run_intraday_flow_pilot.py','execution_ledger.py','data.py','metrics.py']:
        shutil.copyfile(ROOT/'backtest'/name,RUN/'inputs'/('code__'+name))
    cols = ['index_code','trade_date',*VALUES,'source_unit','src','fetched_at','updated_at']
    conn = _connect(load_db_config())
    try:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute('SELECT transaction_timestamp()::text'); stamp = q.fetchone()[0]
            q.execute('SELECT '+','.join(cols)+" FROM stock_selector.index_constituent_money_flow WHERE trade_date<='2026-09-15' ORDER BY index_code,trade_date")
            rows = [dict(zip(cols,r)) for r in q.fetchall()]
            q.execute("SELECT trade_date,index_code,close FROM stock_selector.index_daily WHERE index_code=ANY(%s) AND trade_date BETWEEN '2014-01-01' AND '2026-09-15' ORDER BY 1,2",(list(CODES.values()),))
            prices = pd.DataFrame(q.fetchall(),columns=['date','index_code','close'])
            q.execute("SELECT calendar_date FROM public.trading_calendar WHERE sfe=true AND calendar_date BETWEEN '2014-01-01' AND '2026-09-15' ORDER BY 1")
            calendar = pd.DataFrame(q.fetchall(),columns=['date'])
        conn.rollback()
    finally:
        conn.close()
    # Decimal comparison of every source amount; blank all-null rows are not data.
    with (RUN/'inputs/office_snapshot.csv').open() as h:
        snapshot = list(csv.DictReader(h))
    snap = {(r['index_code'],r['trade_date']):r for r in snapshot if any(r[k] for k in VALUES)}
    assert len(rows)==len(snap)==10806
    assert set(CODES.values())=={r['index_code'] for r in rows}
    assert len({(r['index_code'],r['trade_date']) for r in rows})==len(rows)
    for row in rows:
        orig = snap[(row['index_code'],str(row['trade_date']))]
        assert row['source_unit']=='元' and row['src']=='wind:wsd:mfd'
        for name in VALUES:
            assert row[name] is not None and row[name].is_finite()
            assert Decimal(orig[name])==row[name]
    pd.DataFrame(rows).to_csv(RUN/'inputs/flow.csv',index=False)
    prices.to_csv(RUN/'inputs/prices.csv',index=False)
    calendar.to_csv(RUN/'inputs/calendar.csv',index=False)
    (RUN/'inputs/capture.json').write_text(json.dumps({'observed_at':stamp,'database_read_only':True,
        'office_snapshot_decimal_exact':True,'source_values_compared':len(rows)*len(VALUES),
        'source_receipts':[artifact_record(EVIDENCE/n,EVIDENCE) for n in ['snapshot.csv','identity-outliers.csv']]},indent=2))
    print('Captured and reconciled',len(rows),'rows',flush=True)


def build_scores(flow,prices,calendar,vendor_control=False):
    px = prices.pivot(index='date',columns='index_code',values='close').reindex(calendar)
    out, residuals = {}, {}
    for name,code in CODES.items():
        d = flow.loc[flow.index_code.eq(code)].set_index('trade_date').reindex(calendar)
        inflow = d.xlarge_buy_money+d.large_buy_money
        outflow = d.xlarge_sell_money+d.large_sell_money
        gross = (inflow+outflow).where(lambda x:x>0)
        y = (d.end_main_inflow_money-d.open_main_inflow_money)/gross
        net = d.main_inflow_money if vendor_control else inflow-outflow
        controls = pd.DataFrame({'flow':net/gross,'ret':px[code].pct_change(fill_method=None)},index=calendar)
        residuals[name] = residual_past(y,controls,window=250)
        out[name] = residuals[name].rolling(20,min_periods=20).mean()
    scores = pd.DataFrame(out,index=calendar)
    for name,members in GROUPS.items():
        scores[name] = scores[members].mean(axis=1).where(scores[members].notna().all(axis=1))
    return scores,pd.DataFrame(residuals,index=calendar)


def audit(flow,prices,calendar):
    assert not flow.duplicated(['index_code','trade_date']).any()
    assert not prices.duplicated(['index_code','date']).any()
    assert np.isfinite(flow[VALUES]).all().all()
    buysells = ['xlarge_buy_money','xlarge_sell_money','large_buy_money','large_sell_money']
    assert flow[buysells].ge(0).all().all()
    gross = flow[buysells].sum(axis=1)
    assert gross.gt(0).all()
    net = flow.xlarge_buy_money+flow.large_buy_money-flow.xlarge_sell_money-flow.large_sell_money
    diff = flow.main_inflow_money-net
    outliers = flow.loc[(diff.abs()/gross).gt(1e-5),['index_code','trade_date']].copy()
    outliers['difference'] = diff.loc[outliers.index]
    outliers['relative_to_gross'] = diff.abs().div(gross).loc[outliers.index]
    assert len(outliers)==23 and outliers.trade_date.nunique()==12
    supplied = pd.read_csv(RUN/'inputs/office_identity-outliers.csv',parse_dates=['trade_date'])
    assert set(map(tuple,outliers[['index_code','trade_date']].to_numpy()))==set(map(tuple,supplied[['index_code','trade_date']].to_numpy()))
    coverage = []
    for code,d in flow.groupby('index_code'):
        expected = calendar[(calendar>=d.trade_date.min())&(calendar<=d.trade_date.max())]
        assert set(expected)==set(d.trade_date)
        p = prices[prices.index_code.eq(code)].set_index('date').close.reindex(expected)
        assert np.isfinite(p).all() and p.gt(0).all()
        coverage.append({'index_code':code,'rows':len(d),'start':str(d.trade_date.min().date()),'end':str(d.trade_date.max().date()),'internal_missing_days':0,'missing_control_prices':0})
    outliers.to_csv(RUN/'outputs/identity_outliers.csv',index=False)
    report = {'coverage':coverage,'all_seven_values_finite':True,'buy_sell_nonnegative':True,
              'positive_gross_denominator':True,'identity_outliers_gt_1e_5':len(outliers),
              'identity_outlier_dates':int(outliers.trade_date.nunique()),
              'max_identity_gap_over_gross':float((diff.abs()/gross).max()),
              'outlier_keys_match_office':True,'source_unit':'元','src':'wind:wsd:mfd'}
    (RUN/'outputs/data_verification.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    return report


def execute():
    flow = pd.read_csv(RUN/'inputs/flow.csv',parse_dates=['trade_date'])
    prices = pd.read_csv(RUN/'inputs/prices.csv',parse_dates=['date'])
    calendar = pd.DatetimeIndex(pd.read_csv(RUN/'inputs/calendar.csv',parse_dates=['date']).date,name='date')
    audit(flow,prices,calendar)
    scores,residuals = build_scores(flow,prices,calendar)
    vendor,_ = build_scores(flow,prices,calendar,vendor_control=True)
    # Future perturbation cannot alter older scores, including rolling fits.
    changed = flow.copy(); cut = pd.Timestamp('2026-08-17')
    changed.loc[changed.trade_date.ge(cut),'end_main_inflow_money'] *= -3
    future,_ = build_scores(changed,prices,calendar)
    np.testing.assert_allclose(scores.loc[scores.index<cut],future.loc[future.index<cut],atol=0,rtol=0,equal_nan=True)
    scores.to_csv(RUN/'outputs/scores.csv',index_label='date')
    residuals.to_csv(RUN/'outputs/residuals.csv',index_label='date')
    vendor.to_csv(RUN/'outputs/vendor_control_scores.csv',index_label='date')
    futures = pd.read_csv(RUN/'inputs/futures.csv',parse_dates=['date'])
    spot = pd.read_csv(RUN/'inputs/spot.csv',parse_dates=['date'])
    ew = pd.read_csv(RUN/'inputs/equal_weight.csv',index_col='date',parse_dates=True).factor_value
    expired_calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol)); after = expired_calendar[expired_calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    first_im = futures.loc[futures.symbol.str.startswith('IM'),'date'].min()
    rows,overlap,diagnostic,trades_all,definitions,checks = [],[],[],[],[],[]
    total = 0
    for scope,names in [('core_common',MAIN),('star_common',MAIN+['STAR','ALL'])]:
        valid = scores[names].notna().all(axis=1)
        start = valid[valid].index[0]
        idx = calendar[(calendar>=start)&(calendar<=END)]
        assert len(idx)>0 and valid.reindex(idx).all() and ew.reindex(idx).notna().all()
        feat = scores.loc[idx,names]
        longs,shorts = leg_rules(ew.reindex(idx),feat)
        lb,sb = longs['base'],shorts['base']
        targets = {'C__base':lb+sb,'C__half':lb+.5*sb,'S__base':sb,'S__half':.5*sb}
        for name in names:
            selected = shorts[name+'_neg']
            targets['C__'+name] = lb+selected; targets['S__'+name] = selected
            overlap.append({'scope':scope,'source':name,'original_short_days':int(sb.lt(0).sum()),
                'kept_short_days':int(selected.lt(0).sum()),'fraction_kept':float(selected.lt(0).sum()/max(1,sb.lt(0).sum())),
                'score_corr_with_PAIR':float(feat[name].corr(feat.PAIR)),
                'disagreement_with_PAIR_on_short_days':int(((selected!=shorts['PAIR_neg'])&sb.lt(0)).sum())})
        if scope=='core_common':
            _,vs = leg_rules(ew.reindex(idx),vendor.loc[idx,names])
            for name in names:
                diagnostic.append({'source':name,'max_abs_score_difference':float((vendor.loc[idx,name]-feat[name]).abs().max()),
                    'corr':float(vendor.loc[idx,name].corr(feat[name])),
                    'short_decision_disagreements':int((vs[name+'_neg']!=shorts[name+'_neg']).sum())})
        weights = futures_weights(idx,first_im)
        market = prepare_close_market(futures,idx,weights,expiries)
        signals0 = pd.DataFrame(targets,index=idx)
        middle = len(idx)//2
        windows = {'full':idx,'early_half':idx[:middle],'late_half':idx[middle:], '2024-2026':idx[idx>='2024-01-01']}
        windows.update({f'year_{y}':idx[idx.year==y] for y in sorted(set(idx.year))})
        definitions.append({'scope':scope,'start':str(idx[0].date()),'end':str(idx[-1].date()),'n':len(idx),'sources':names})
        for scenario,cost,lag in [('close_3bps',3.,0),('close_10bps',10.,0),('second_close_3bps',3.,1)]:
            print(scope,scenario,len(idx),'days',flush=True)
            signals = signals0.shift(lag,fill_value=0.)
            # Separate WSET benchmark, identical calendar/account initialization.
            # WSET prior scores end Sep03; extend them from the already-frozen continuation run.
            if scope=='core_common' and scenario=='close_3bps':
                old = pd.read_csv(RUN/'inputs/wset_continued_scores.csv',index_col=0,parse_dates=True)
                _,ws = leg_rules(ew.reindex(idx),old.reindex(idx)[['CSI300','PAIR']])
                for name in ['CSI300','PAIR']:
                    signals['WSET__'+name] = lb+ws[name+'_neg']
            ledgers = batch_close_ledgers(market,signals,cost_bps=cost)
            total += len(ledgers)
            refs = ['C__base','C__POOL'] if scope=='core_common' else ['C__STAR']
            for name in refs:
                ref = contract_ledger(futures,signals[name],weights,expiries=expiries,cost_bps=cost)
                err = float((ref.ret-ledgers[name].ret).abs().max())
                np.testing.assert_allclose(ref.ret,ledgers[name].ret,rtol=1e-10,atol=1e-12)
                checks.append({'scope':scope,'scenario':scenario,'strategy':name,'max_reference_error':err})
            for name,ledger in ledgers.items():
                np.testing.assert_allclose(ledger.gross_pnl-ledger.cost,ledger.equity.diff().fillna(ledger.equity.iloc[0]-1),rtol=1e-10,atol=1e-12)
                _,trades = attribute_ledger(ledger)
                trades_all.append(trades.assign(scope=scope,scenario=scenario,strategy=name))
                for window,dates in windows.items():
                    if len(dates):
                        rows.append({'scope':scope,'scenario':scenario,'strategy':name,'window':window,
                            'start':str(dates[0].date()),'end':str(dates[-1].date()),**describe(ledger.loc[dates])})
            signals.to_csv(RUN/f'outputs/targets_{scope}_{scenario}.csv',index_label='date')
            pd.concat(ledgers,names=['strategy','date']).to_csv(RUN/f'outputs/ledgers_{scope}_{scenario}.csv.gz')
    assert total==122
    pd.DataFrame(rows).to_csv(RUN/'outputs/metrics.csv',index=False)
    pd.DataFrame(overlap).to_csv(RUN/'outputs/source_overlap.csv',index=False)
    pd.DataFrame(diagnostic).to_csv(RUN/'outputs/vendor_net_sensitivity.csv',index=False)
    pd.concat(trades_all,ignore_index=True).to_csv(RUN/'outputs/trades.csv',index=False)
    verification = {'sample_definitions':definitions,'checks':checks,'total_ledgers':total,
        'all_account_leg_and_trade_pnl_identities_passed':True,'future_perturbation_invariant':True,
        'execution_end':'2026-09-11','feature_end':'2026-09-15','unchanged_B_neg_rule':True,
        'completed_utc':datetime.now(timezone.utc).isoformat()}
    (RUN/'outputs/research_verification.json').write_text(json.dumps(verification,indent=2))
    print(pd.DataFrame(rows).query("scenario=='close_3bps' and window=='full'")[['scope','strategy','cagr','sharpe','maxdd']].round(4).to_string(index=False),flush=True)


if __name__=='__main__':
    import sys
    mode = sys.argv[1]
    if mode=='capture':
        try:
            capture()
        except Exception as exc:
            raise SystemExit('Capture failed: '+type(exc).__name__) from None
    elif mode=='execute':
        source = ROOT/'backtest/output/runs/20260916-ew-flow-score-continuation-r1/outputs/source_scores.csv'
        shutil.copyfile(source,RUN/'inputs/wset_continued_scores.csv')
        execute()
    else:
        raise SystemExit('expected capture or execute')
