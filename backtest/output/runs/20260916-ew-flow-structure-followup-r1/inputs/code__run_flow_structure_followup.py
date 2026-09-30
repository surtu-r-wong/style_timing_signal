"""Frozen three-part descriptive follow-up; all inputs are local snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.flow_structure_followup import structure_scores, short_modes, stable_short, daily_phase
from backtest.intraday_flow_pilot import leg_rules, prepare_close_market, batch_close_ledgers, attribute_ledger
from backtest.run_flow_robustness_diagnostic import episodes, ret_metrics
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest, git_state

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT/'backtest/output/runs/20260916-ew-constituent-flow-extension-r1'
ROBUST = ROOT/'backtest/output/runs/20260916-ew-flow-robustness-diagnostic-r1'
RUN_ID = '20260916-ew-flow-structure-followup-r1'
SPEC = ROOT/'docs/plans/2026-09-16-ew-flow-structure-followup.md'
SCENARIOS = [('close_3bps', 3., 0), ('close_10bps', 10., 0), ('second_close_3bps', 3., 1)]


def capture(run):
    receipts = []
    files = ['inputs/flow.csv', 'inputs/prices.csv', 'inputs/calendar.csv', 'inputs/futures.csv',
             'inputs/spot.csv', 'inputs/equal_weight.csv', 'inputs/wset_continued_scores.csv', 'outputs/scores.csv']
    files += [f'outputs/{kind}_core_common_{scenario}.{suffix}' for scenario, _, _ in SCENARIOS
              for kind, suffix in [('targets', 'csv'), ('ledgers', 'csv.gz')]]
    for rel in files:
        src = PRIOR/rel
        shutil.copyfile(src, run/'inputs'/src.name)
        receipts.append(artifact_record(src, ROOT))
    src = ROBUST/'inputs/wset_money_flow.csv'
    shutil.copyfile(src, run/'inputs'/src.name)
    receipts.append(artifact_record(src, ROOT))
    src = ROOT/'backtest/output/runs/20260916-ew-flow-score-continuation-r1/inputs/p0_added_rows.csv'
    shutil.copyfile(src, run/'inputs'/src.name)
    receipts.append(artifact_record(src, ROOT))
    shutil.copyfile(SPEC, run/'inputs/prereg.md')
    for rel in ['backtest/run_flow_structure_followup.py', 'backtest/flow_structure_followup.py',
                'backtest/intraday_flow_pilot.py', 'backtest/execution_ledger.py', 'backtest/run_manifest.py',
                'backtest/run_intraday_flow_pilot.py', 'backtest/run_flow_robustness_diagnostic.py',
                'backtest/data.py', 'backtest/metrics.py', 'tests/test_flow_structure_followup.py']:
        shutil.copyfile(ROOT/rel, run/'inputs'/('code__'+Path(rel).name))
    office = Path('/home/elfbob/claude-code/data_manager/requests/2026-09-16-style-timing-signal-index-money-flow-backfill')
    for name in ['response-08-office-2026-09-16.md', 'response-08-style-timing-signal-2026-09-16.md']:
        shutil.copyfile(office/name, run/'inputs'/name)
    (run/'inputs/source_receipts.json').write_text(json.dumps(receipts, indent=2))


def thresholds(flow, oldflow, calendar):
    d = flow.query("index_code=='000300.SH'").set_index('trade_date').reindex(calendar)
    gross = d[['xlarge_buy_money', 'xlarge_sell_money', 'large_buy_money', 'large_sell_money']].sum(axis=1, min_count=4)
    raw_new = (d.end_main_inflow_money-d.open_main_inflow_money)/gross
    old = oldflow.query("index_code=='000300.SH'").set_index('trade_date').reindex(calendar)
    raw_old = (old.end_main_inflow_money-old.open_main_inflow_money)/(old.main_in_money+old.main_out_money)
    raw = pd.DataFrame({'new': raw_new, 'old': raw_old}, index=calendar)
    return .25*raw.shift(1).rolling(250, min_periods=250).std(ddof=1)/np.sqrt(20), raw


def execute(run):
    read = lambda name, **kw: pd.read_csv(run/'inputs'/name, **kw)
    flow = read('flow.csv', parse_dates=['trade_date'])
    prices = read('prices.csv', parse_dates=['date'])
    calendar = pd.DatetimeIndex(read('calendar.csv', parse_dates=['date']).date, name='date')
    prior_target = read('targets_core_common_close_3bps.csv', index_col=0, parse_dates=True)
    idx = prior_target.index
    score, details = structure_scores(flow, prices, calendar)
    assert score.loc[idx, ['PAIR', 'POOL']].notna().all().all()
    changed = flow.copy(); cutoff = pd.Timestamp('2026-08-17')
    changed.loc[changed.trade_date.ge(cutoff), 'xlarge_buy_money'] *= 3
    perturbed, _ = structure_scores(changed, prices, calendar)
    np.testing.assert_allclose(score.loc[score.index<cutoff], perturbed.loc[perturbed.index<cutoff], atol=0, rtol=0, equal_nan=True)
    score.to_csv(run/'outputs/structure_scores.csv', index_label='date')
    details.to_csv(run/'outputs/structure_details.csv.gz', index=False)
    detail_valid = details.loc[details.date.isin(idx)]
    detail_valid.groupby(['source', 'quadrant']).size().rename('days').to_csv(run/'outputs/structure_quadrants.csv')
    correlations = []
    for name, d in detail_valid.groupby('source'):
        for variable in ['total_imbalance', 'price_return']:
            correlations.append({'source': name, 'control': variable,
                                 'raw_corr': d.difference.corr(d[variable]),
                                 'residual_corr': d.residual.corr(d[variable])})
    pd.DataFrame(correlations).to_csv(run/'outputs/structure_control_correlations.csv', index=False)
    ew = read('equal_weight.csv', index_col='date', parse_dates=True).factor_value.reindex(idx)
    longs, shorts = leg_rules(ew, score.loc[idx, ['PAIR', 'POOL']])
    lb, sb = longs['base'], shorts['base']
    np.testing.assert_array_equal(lb+sb, prior_target.C__base)
    candidates, definitions = {}, []
    for source in ['PAIR', 'POOL']:
        for side, target in [('L', longs[source+'_pos']), ('S', shorts[source+'_pos'])]:
            name = 'A_'+source+'_'+side
            candidates[name] = (side, target)
            definitions.append({'name': name, 'stage': 'A', 'side': side, 'source': source, 'mapping': 'positive_contrast'})
    bscore = read('scores.csv', index_col=0, parse_dates=True).CSI300.reindex(idx)
    modes = short_modes(sb, bscore)
    np.testing.assert_array_equal(modes.daily, prior_target.S__CSI300)
    for mode in modes:
        name = 'B_'+mode
        candidates[name] = ('S', modes[mode])
        definitions.append({'name': name, 'stage': 'B', 'side': 'S', 'source': 'new', 'mapping': mode})
    oldscore = read('wset_continued_scores.csv', index_col=0, parse_dates=True).CSI300.reindex(idx)
    oldflow = pd.concat([read('wset_money_flow.csv', parse_dates=['trade_date']),
                         read('p0_added_rows.csv', parse_dates=['trade_date'])], ignore_index=True)
    assert not oldflow.duplicated(['index_code', 'trade_date']).any()
    h, raw = thresholds(flow, oldflow, calendar)
    assert h.loc[idx].gt(0).all().all()
    h.to_csv(run/'outputs/buffer_thresholds.csv', index_label='date')
    raw.to_csv(run/'outputs/raw_B.csv', index_label='date')
    # The buffer uses only past raw values, independent of future source changes.
    oldchanged = oldflow.copy()
    oldchanged.loc[oldchanged.trade_date.ge(cutoff), 'end_main_inflow_money'] *= 7
    h2, _ = thresholds(changed, oldchanged, calendar)
    np.testing.assert_allclose(h.loc[h.index<=cutoff], h2.loc[h.index<=cutoff], atol=0, rtol=0, equal_nan=True)
    for source, values in [('new', bscore), ('old', oldscore)]:
        stable = stable_short(sb, values, h.loc[idx, source])
        if source == 'old':
            stable['binary'] = short_modes(sb, values).daily
        for mode in stable:
            name = 'C_'+source+'_'+mode
            candidates[name] = ('S', stable[mode])
            definitions.append({'name': name, 'stage': 'C', 'side': 'S', 'source': source, 'mapping': mode})
    # All strategy and diagnostic exposure targets are frozen before performance.
    signals0 = pd.DataFrame({'C__base': lb+sb, 'S__base': sb, 'L__base': lb,
                            'C__halfS': lb+.5*sb, 'S__halfS': .5*sb,
                            'C__halfL': .5*lb+sb, 'L__halfL': .5*lb}, index=idx)
    for definition in definitions:
        name = definition['name']; side, target = candidates[name]
        base, other = (lb, sb) if side == 'L' else (sb, lb)
        q = float(target.abs().sum()/base.abs().sum())
        definition.update(short_or_long_scale=q, baseline_days=int(base.ne(0).sum()),
                          kept_equivalent_days=float(target.abs().sum()),
                          full_days=int(target.abs().eq(1).sum()), half_days=int(target.abs().eq(.5).sum()))
        signals0['C__'+name] = other+target
        signals0[side+'__'+name] = target
        signals0['Q__'+name] = other+q*base
        signals0['Q'+side+'__'+name] = q*base
    pd.DataFrame(definitions).to_csv(run/'outputs/candidate_definitions.csv', index=False)
    signals0.to_csv(run/'outputs/decision_targets.csv', index_label='date')
    futures = read('futures.csv', parse_dates=['date'])
    spot = read('spot.csv', parse_dates=['date'])
    full_calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol)); after = full_calendar[full_calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    weights = futures_weights(idx, futures.loc[futures.symbol.str.startswith('IM'), 'date'].min())
    market = prepare_close_market(futures, idx, weights, expiries)
    mid = len(idx)//2
    windows = {'full': idx, 'early_half': idx[:mid], 'late_half': idx[mid:]}
    windows.update({f'year_{year}': idx[idx.year==year] for year in sorted(set(idx.year))})
    metrics, comparisons, stress, daily, checks, episode_rows, phase_rows, source_rows = [], [], [], [], [], [], [], []
    total_new = total_reused = 0
    for scenario, cost, lag in SCENARIOS:
        print('Running', scenario, flush=True)
        signals = signals0.shift(lag, fill_value=0.)
        old = read(f'ledgers_core_common_{scenario}.csv.gz', index_col=['strategy', 'date'], parse_dates=['date'])
        oldtargets = read(f'targets_core_common_{scenario}.csv', index_col=0, parse_dates=True)
        reuse = {'C__base': 'C__base', 'S__base': 'S__base', 'C__halfS': 'C__half', 'S__halfS': 'S__half',
                 'C__B_daily': 'C__CSI300', 'S__B_daily': 'S__CSI300'}
        if scenario == 'close_3bps':
            reuse['C__C_old_binary'] = 'WSET__CSI300'
        book = {}
        for new, prior in reuse.items():
            np.testing.assert_array_equal(signals[new], oldtargets[prior])
            book[new] = old.loc[prior]
        fresh = batch_close_ledgers(market, signals.drop(columns=list(reuse)), cost_bps=cost)
        total_new += len(fresh); total_reused += len(reuse)
        for name in ['C__A_PAIR_L', 'C__B_entry_exit', 'C__C_new_tier']:
            ref = contract_ledger(futures, signals[name], weights, expiries=expiries, cost_bps=cost)
            np.testing.assert_allclose(fresh[name].ret, ref.ret, atol=1e-12, rtol=1e-10)
            checks.append({'scenario': scenario, 'strategy': name, 'max_reference_error': float((fresh[name].ret-ref.ret).abs().max())})
        trades = []
        for name, ledger in fresh.items():
            np.testing.assert_allclose(ledger.gross_pnl-ledger.cost, ledger.equity.diff().fillna(ledger.equity.iloc[0]-1.), atol=1e-12, rtol=1e-10)
            _, t = attribute_ledger(ledger)
            trades.append(t.assign(strategy=name))
        book.update(fresh)
        pd.concat(book, names=['strategy', 'date']).to_csv(run/f'outputs/ledgers_{scenario}.csv.gz')
        pd.concat(trades, ignore_index=True).to_csv(run/f'outputs/new_trades_{scenario}.csv', index=False)
        for name, ledger in book.items():
            for window, dates in windows.items():
                metrics.append({'scenario': scenario, 'strategy': name, 'window': window,
                                'start': str(dates[0].date()), 'end': str(dates[-1].date()), **describe(ledger.loc[dates])})
        labels, ep = episodes(book['C__base'])
        ep['left_censored'] = False
        if signals0.S__base.iloc[0] < 0 and len(ep):
            ep.loc[ep.index[0], 'left_censored'] = True
        ep.to_csv(run/f'outputs/baseline_episodes_{scenario}.csv', index=False)
        for definition in definitions:
            name, side = definition['name'], definition['side']
            a = book['C__'+name].ret
            for comparison, right in [('base', 'C__base'), ('half', 'C__half'+side), ('matched', 'Q__'+name)]:
                b = book[right].ret
                delta = np.log1p(a)-np.log1p(b)
                top = delta.nlargest(5)
                comparisons.append({'scenario': scenario, 'candidate': name, 'comparison': comparison,
                                    'delta_log': float(delta.sum()), 'top_day': str(top.index[0].date()),
                                    'top_day_delta_log': float(top.iloc[0]), 'top5_delta_log': float(top.sum()),
                                    'sharpe_difference': ret_metrics(a)['sharpe']-ret_metrics(b)['sharpe']})
                daily.append(pd.DataFrame({'date': idx, 'scenario': scenario, 'candidate': name,
                                           'comparison': comparison, 'delta_log': delta.values}))
                for k in [0, 1, 5]:
                    aa, bb = a.copy(), b.copy(); dates = top.index[:k]
                    aa.loc[dates] = 0.; bb.loc[dates] = 0.
                    ma, mb = ret_metrics(aa), ret_metrics(bb)
                    stress.append({'scenario': scenario, 'candidate': name, 'comparison': comparison, 'best_days_zeroed': k,
                                   'left_cagr': ma['cagr'], 'right_cagr': mb['cagr'],
                                   'sharpe_difference': ma['sharpe']-mb['sharpe']})
                if definition['stage'] == 'B':
                    e = ep.copy()
                    e['delta_log'] = [float(delta.loc[labels.eq(i)].sum()) for i in e.episode]
                    remainder = float(delta.loc[labels.eq(-1)].sum())
                    np.testing.assert_allclose(e.delta_log.sum()+remainder, delta.sum(), atol=1e-12, rtol=0)
                    episode_rows.append(e.assign(scenario=scenario, candidate=name, comparison=comparison))
                    episode_rows.append(pd.DataFrame([{'scenario': scenario, 'candidate': name, 'comparison': comparison,
                                                       'episode': -1, 'delta_log': remainder}]))
        phase = daily_phase(sb, modes.daily)
        holding_phase = phase.shift(lag+2, fill_value='outside_short')
        filling_phase = phase.shift(lag+1, fill_value='outside_short')
        attribution_phase = holding_phase.where(holding_phase.eq(filling_phase), 'transition')
        delta = np.log1p(book['C__B_daily'].ret)-np.log1p(book['C__base'].ret)
        grouped = delta.groupby(attribution_phase).sum()
        np.testing.assert_allclose(grouped.sum(), delta.sum(), atol=1e-12, rtol=0)
        for phase_name, value in grouped.items():
            phase_rows.append({'scenario': scenario, 'phase': phase_name, 'days': int(attribution_phase.eq(phase_name).sum()),
                               'delta_log': float(value)})
        pd.DataFrame({'decision_phase': phase, 'holding_phase': holding_phase, 'fill_phase': filling_phase,
                      'attribution_phase': attribution_phase, 'delta_log': delta}).to_csv(run/f'outputs/daily_phase_{scenario}.csv', index_label='date')
        for mapping, newname, oldname in [('binary', 'B_daily', 'C_old_binary'), ('tier', 'C_new_tier', 'C_old_tier'),
                                         ('hysteresis', 'C_new_hysteresis', 'C_old_hysteresis')]:
            new, oldledger = book['C__'+newname], book['C__'+oldname]
            source_rows.append({'scenario': scenario, 'mapping': mapping,
                                'decision_disagreements': int(signals0['C__'+newname].ne(signals0['C__'+oldname]).sum()),
                                'decision_absolute_gap': float((signals0['C__'+newname]-signals0['C__'+oldname]).abs().sum()),
                                'old_minus_new_log': float((np.log1p(oldledger.ret)-np.log1p(new.ret)).sum()),
                                'old_minus_new_sharpe': describe(oldledger)['sharpe']-describe(new)['sharpe']})
    for name, rows in [('metrics', metrics), ('comparisons', comparisons), ('stress', stress),
                       ('daily_phase_summary', phase_rows), ('source_sensitivity', source_rows)]:
        pd.DataFrame(rows).to_csv(run/f'outputs/{name}.csv', index=False)
    pd.concat(daily, ignore_index=True).to_csv(run/'outputs/relative_daily.csv.gz', index=False)
    pd.concat(episode_rows, ignore_index=True).to_csv(run/'outputs/episode_attribution.csv', index=False)
    verification = {'new_ledgers': total_new, 'reused_ledgers': total_reused, 'reference_checks': checks,
                    'future_structure_perturbation_invariant': True, 'past_only_buffer_invariant': True,
                    'account_leg_trade_identities': True, 'phase_episode_additivity': True,
                    'execution_start': str(idx[0].date()), 'execution_end': str(idx[-1].date()), 'days': len(idx),
                    'candidate_count_including_two_prior_binary_benchmarks': len(definitions),
                    'no_database_access': True, 'completed_utc': datetime.now(timezone.utc).isoformat()}
    (run/'outputs/verification.json').write_text(json.dumps(verification, indent=2))
    selected = pd.DataFrame(metrics).query("scenario=='close_3bps' and window=='full'")
    print(selected.loc[selected.strategy.str.startswith('C__'), ['strategy', 'cagr', 'sharpe', 'maxdd']].round(5).to_string(index=False), flush=True)
    return verification


def main():
    run = create_run_dir(ROOT/'backtest/output/runs', RUN_ID)
    capture(run)
    payload = {'run_id': RUN_ID, 'status': 'running', 'git': git_state(ROOT),
               'stage': 'order_size_structure_entry_exit_position_stability',
               'prereg_sha256': artifact_record(run/'inputs/prereg.md', run)['sha256']}
    write_manifest(run, payload)
    try:
        verification = execute(run)
    except Exception as exc:
        payload.update(status='failed', error_type=type(exc).__name__)
        write_manifest(run, payload)
        raise
    payload.update(status='computed_pending_report', verification=verification,
                   artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*')) if p.is_file() and p.name != 'manifest.json'])
    write_manifest(run, payload)


if __name__ == '__main__':
    main()
