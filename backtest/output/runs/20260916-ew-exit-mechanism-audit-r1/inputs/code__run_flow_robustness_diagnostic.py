"""Post-hoc attribution of frozen P1 flow results; no new trading rule search."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers, attribute_ledger
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'backtest/output/runs/20260916-ew-flow-robustness-diagnostic-r1'
PRIOR = ROOT/'backtest/output/runs/20260916-ew-constituent-flow-extension-r1'
EVENTS = ['2018-03-23','2018-06-19','2019-05-06','2019-08-02','2024-09-24','2025-04-03','2025-05-12']
SCENARIOS = [('close_3bps',3.),('close_10bps',10.),('second_close_3bps',3.)]


def capture():
    if (RUN/'inputs/source_receipts.json').exists():
        raise ValueError('inputs already frozen')
    files = ['inputs/futures.csv','inputs/spot.csv','inputs/flow.csv','inputs/prices.csv',
             'inputs/wset_continued_scores.csv','outputs/scores.csv']
    for scenario,_ in SCENARIOS:
        files += [f'outputs/targets_core_common_{scenario}.csv',f'outputs/ledgers_core_common_{scenario}.csv.gz']
    receipts = []
    for rel in files:
        src = PRIOR/rel
        shutil.copyfile(src,RUN/'inputs'/src.name)
        receipts.append(artifact_record(src,ROOT))
    old = ROOT/'backtest/output/runs/20260916-ew-flow-source-ablation-r1/inputs/money_flow.csv'
    shutil.copyfile(old,RUN/'inputs/wset_money_flow.csv')
    receipts.append(artifact_record(old,ROOT))
    (RUN/'inputs/source_receipts.json').write_text(json.dumps(receipts,indent=2))
    for filename in ['run_flow_robustness_diagnostic.py','intraday_flow_pilot.py','execution_ledger.py','run_intraday_flow_pilot.py','metrics.py']:
        shutil.copyfile(ROOT/'backtest'/filename,RUN/'inputs'/('code__'+filename))


def episodes(base):
    """Executed baseline short periods, including entry and exit cost dates."""
    neg = base.decision_signal.lt(0).to_numpy()
    labels = pd.Series(-1,index=base.index,dtype=int)
    records = []
    starts = np.flatnonzero(neg & ~np.r_[False,neg[:-1]])
    for number,start in enumerate(starts):
        ends = np.flatnonzero(~neg[start:])
        closed = bool(len(ends))
        stop = start+ends[0] if closed else len(base)-1
        assert labels.iloc[start:stop+1].eq(-1).all()
        labels.iloc[start:stop+1] = number
        records.append({'episode':number,'entry_fill_date':str(base.index[start].date()),
            'exit_or_mark_date':str(base.index[stop].date()),'closed':closed,'days':stop-start+1})
    return labels,pd.DataFrame(records)


def ret_metrics(ret):
    equity = (1+ret).cumprod()
    return {'n':len(ret),'cagr':float(np.expm1(np.log1p(ret).sum()*245/len(ret))),
        'sharpe':float(ret.mean()/ret.std(ddof=1)*np.sqrt(245)),
        'maxdd':float((equity/equity.cummax().clip(lower=1)-1).min())}


def main():
    capture()
    fut = pd.read_csv(RUN/'inputs/futures.csv',parse_dates=['date'])
    spot = pd.read_csv(RUN/'inputs/spot.csv',parse_dates=['date'])
    full_calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    expiries = {}
    for sym in fut.symbol.unique():
        exp = pd.Timestamp(_expiry_from_symbol(sym)); after = full_calendar[full_calendar>=exp]
        expiries[sym] = after[0] if len(after) else exp
    inputs0 = pd.read_csv(RUN/'inputs/targets_core_common_close_3bps.csv',index_col=0,parse_dates=True)
    idx = inputs0.index
    q = float(inputs0.S__CSI300.abs().sum()/inputs0.S__base.abs().sum())
    assert np.isclose(q,488/1131)
    weights = futures_weights(idx,fut.loc[fut.symbol.str.startswith('IM'),'date'].min())
    market = prepare_close_market(fut,idx,weights,expiries)
    annual, ep_rows, daily_rows, event_rows, summaries, stress, matched_metrics, checks = [],[],[],[],[],[],[],[]
    main_book = None
    for scenario,cost in SCENARIOS:
        print(scenario,flush=True)
        target = pd.read_csv(RUN/f'inputs/targets_core_common_{scenario}.csv',index_col=0,parse_dates=True)
        old = pd.read_csv(RUN/f'inputs/ledgers_core_common_{scenario}.csv.gz',index_col=['strategy','date'],parse_dates=['date'])
        sb = target.S__base; lb = target.C__base-sb
        signals = pd.DataFrame({'C__matched300':lb+q*sb,'S__matched300':q*sb},index=idx)
        fresh = batch_close_ledgers(market,signals,cost_bps=cost)
        for name,ledger in fresh.items():
            ref = contract_ledger(fut,signals[name],weights,expiries=expiries,cost_bps=cost)
            np.testing.assert_allclose(ledger.ret,ref.ret,rtol=1e-10,atol=1e-12)
            attribute_ledger(ledger)
            checks.append({'scenario':scenario,'strategy':name,'max_engine_error':float((ledger.ret-ref.ret).abs().max())})
            matched_metrics.append({'scenario':scenario,'strategy':name,'short_scale':q,**describe(ledger)})
        pd.concat(fresh,names=['strategy','date']).to_csv(RUN/f'outputs/matched_ledgers_{scenario}.csv.gz')
        book = {name:old.loc[name] for name in ['C__base','C__half','C__CSI300','C__POOL']}
        book.update(fresh)
        pairs = [('new300_vs_base','C__CSI300','C__base'),('new300_vs_half','C__CSI300','C__half'),
                 ('new300_vs_matched','C__CSI300','C__matched300'),('new300_vs_pool','C__CSI300','C__POOL')]
        if scenario=='close_3bps':
            book['WSET__CSI300'] = old.loc['WSET__CSI300']
            pairs.append(('old300_vs_new300','WSET__CSI300','C__CSI300'))
            main_book = book
        labels,episode_table = episodes(book['C__base'])
        episode_table.to_csv(RUN/f'outputs/baseline_short_episodes_{scenario}.csv',index=False)
        event_mask = pd.Series(False,index=idx)
        event_dates = {}
        for date in EVENTS:
            dates = idx[idx>=date][:10]
            assert len(dates)==10
            event_dates[date] = dates
            event_mask.loc[dates] = True
        assert event_mask.sum()==70
        for name,left,right in pairs:
            ra,rb = book[left].ret,book[right].ret
            delta = np.log1p(ra)-np.log1p(rb)
            total = float(delta.sum())
            daily = pd.DataFrame({'date':idx,'delta_log':delta.values,'left_return':ra.values,'right_return':rb.values,
                'left_filled_target':book[left].decision_signal.values,'right_filled_target':book[right].decision_signal.values,
                'baseline_short_episode':labels.values,'event_window':event_mask.values})
            daily_rows.append(daily.assign(scenario=scenario,comparison=name))
            ep = episode_table.copy()
            ep['delta_log'] = [float(delta[labels.eq(k)].sum()) for k in ep.episode]
            ep_rows.append(ep.assign(scenario=scenario,comparison=name))
            remainder = float(delta[labels.eq(-1)].sum())
            np.testing.assert_allclose(ep.delta_log.sum()+remainder,total,atol=1e-12,rtol=0)
            year_values = delta.groupby(idx.year).sum()
            np.testing.assert_allclose(year_values.sum(),total,atol=1e-12,rtol=0)
            for year,value in year_values.items():
                annual.append({'scenario':scenario,'comparison':name,'year':int(year),'delta_log':value})
            inside = float(delta[event_mask].sum()); outside = float(delta[~event_mask].sum())
            np.testing.assert_allclose(inside+outside,total,atol=1e-12,rtol=0)
            for date,dates in event_dates.items():
                event_rows.append({'scenario':scenario,'comparison':name,'event':date,'start':str(dates[0].date()),
                    'end':str(dates[-1].date()),'delta_log':float(delta.loc[dates].sum())})
            top1 = delta.nlargest(1).index; top5 = delta.nlargest(5).index
            summaries.append({'scenario':scenario,'comparison':name,'left':left,'right':right,'total_delta_log':total,
                'episode_delta_log':float(ep.delta_log.sum()),'outside_episodes_delta_log':remainder,
                'positive_episodes':int(ep.delta_log.gt(1e-12).sum()),'negative_episodes':int(ep.delta_log.lt(-1e-12).sum()),
                'episode_count':len(ep),'top_episode_delta_log':float(ep.delta_log.max()),
                'top5_episodes_delta_log':float(ep.delta_log.nlargest(5).sum()),'event_inside_delta_log':inside,
                'event_outside_delta_log':outside,'best_year':int(year_values.idxmax()),'best_year_delta_log':float(year_values.max()),
                'outside_best_year_delta_log':total-float(year_values.max()),'top_day_delta_log':float(delta.loc[top1].sum()),
                'top5_days_delta_log':float(delta.loc[top5].sum())})
            for mode,zero in [('full',pd.Index([])),('seven_event_windows',idx[event_mask]),('best_one_day',top1),('best_five_days',top5)]:
                a,b = ra.copy(),rb.copy();a.loc[zero]=0.;b.loc[zero]=0.
                aa,bb = ret_metrics(a),ret_metrics(b)
                stress.append({'scenario':scenario,'comparison':name,'mode':mode,'zeroed_days':len(zero),
                    'left_cagr':aa['cagr'],'right_cagr':bb['cagr'],'left_sharpe':aa['sharpe'],'right_sharpe':bb['sharpe'],
                    'sharpe_difference':aa['sharpe']-bb['sharpe'],'left_maxdd':aa['maxdd'],'right_maxdd':bb['maxdd'],
                    'delta_log':float((np.log1p(a)-np.log1p(b)).sum())})
    pd.DataFrame(matched_metrics).to_csv(RUN/'outputs/matched_metrics.csv',index=False)
    pd.DataFrame(summaries).to_csv(RUN/'outputs/comparison_summary.csv',index=False)
    pd.DataFrame(annual).to_csv(RUN/'outputs/annual_attribution.csv',index=False)
    pd.concat(ep_rows,ignore_index=True).to_csv(RUN/'outputs/episode_attribution.csv',index=False)
    pd.concat(daily_rows,ignore_index=True).to_csv(RUN/'outputs/daily_attribution.csv.gz',index=False)
    pd.DataFrame(event_rows).to_csv(RUN/'outputs/event_attribution.csv',index=False)
    pd.DataFrame(stress).to_csv(RUN/'outputs/stress_summary.csv',index=False)
    # Decision-time versus execution-time source disagreements.
    scores = pd.read_csv(RUN/'inputs/scores.csv',index_col=0,parse_dates=True)
    oldscore = pd.read_csv(RUN/'inputs/wset_continued_scores.csv',index_col=0,parse_dates=True)
    diff = inputs0.C__CSI300.ne(inputs0.WSET__CSI300)
    assert diff.sum()==18
    dates = idx[diff]
    amounts = pd.read_csv(RUN/'inputs/flow.csv',parse_dates=['trade_date']).query("index_code=='000300.SH'").set_index('trade_date')
    amounts['I'] = amounts.xlarge_buy_money+amounts.large_buy_money
    amounts['U'] = amounts.xlarge_sell_money+amounts.large_sell_money
    original = pd.read_csv(RUN/'inputs/wset_money_flow.csv',parse_dates=['trade_date']).query("index_code=='000300.SH'").set_index('trade_date')
    merged = amounts.join(original[['open_main_inflow_money','end_main_inflow_money','main_in_money','main_out_money']],rsuffix='_wset',how='inner')
    mismatches = pd.DataFrame(index=merged.index)
    for new,oldfield in [('open_main_inflow_money','open_main_inflow_money_wset'),('end_main_inflow_money','end_main_inflow_money_wset'),('I','main_in_money'),('U','main_out_money')]:
        mismatches[new] = ~np.isclose(merged[new],merged[oldfield]*10000,rtol=1e-10,atol=1e-4)
    mismatches.to_csv(RUN/'outputs/source_amount_mismatches.csv',index_label='date')
    any_diff = mismatches.any(axis=1).astype(float).reindex(scores.index)
    counts = any_diff.rolling(270,min_periods=270).sum()
    source_rows=[]
    for date in dates:
        i=idx.get_loc(date)
        source_rows.append({'decision_date':str(date.date()),'fill_date':str(idx[i+1].date()) if i+1<len(idx) else None,
            'first_holding_return_date':str(idx[i+2].date()) if i+2<len(idx) else None,
            'new_score':float(scores.loc[date,'CSI300']),'old_score':float(oldscore.loc[date,'CSI300']),
            'new_target':float(inputs0.loc[date,'C__CSI300']),'old_target':float(inputs0.loc[date,'WSET__CSI300']),
            'amount_mismatch_days_in_270d_dependency':float(counts.loc[date]),
            'new_abs_score_over_prior250_std':float(abs(scores.loc[date,'CSI300'])/scores.CSI300.shift(1).rolling(250).std().loc[date])})
    pd.DataFrame(source_rows).to_csv(RUN/'outputs/source_decision_disagreements.csv',index=False)
    oldb,newb=main_book['WSET__CSI300'],main_book['C__CSI300']
    filled = oldb.decision_signal.ne(newb.decision_signal)
    support = filled | filled.shift(1,fill_value=False)
    delta = np.log1p(oldb.ret)-np.log1p(newb.ret)
    changed_returns = delta.abs().gt(1e-12)
    support_report={'target_difference_days':int(diff.sum()),'filled_position_difference_days':int(filled.sum()),
        'nonnegligible_relative_return_days':int(changed_returns.sum()),
        'filled_or_previous_filled_difference_days':int(support.sum()),
        'delta_log_inside_execution_support':float(delta[support].sum()),
        'delta_log_outside_execution_support':float(delta[~support].sum()),
        'note':'Small tails outside position-disagreement support may arise from cost/equity rebalancing; all dates retained.'}
    (RUN/'outputs/source_execution_support.json').write_text(json.dumps(support_report,indent=2))
    verify={'source_disagreement_days':18,'matched_short_scale':q,'new_ledgers':6,'reference_checks':checks,
        'episode_year_event_additivity_passed':True,'all_new_account_leg_trade_identities_passed':True,
        'all_previous_ledgers_reused_without_reexecution':True,'no_new_strategy_or_threshold_selected':True}
    (RUN/'outputs/verification.json').write_text(json.dumps(verify,indent=2))
    print(pd.DataFrame(matched_metrics)[['scenario','strategy','cagr','sharpe','maxdd']].round(4).to_string(index=False),flush=True)
    print(pd.DataFrame(summaries).query("scenario=='close_3bps'").round(5).to_string(index=False),flush=True)
    print('SOURCE DIFFERENCES',support_report,flush=True)


if __name__=='__main__':
    main()
