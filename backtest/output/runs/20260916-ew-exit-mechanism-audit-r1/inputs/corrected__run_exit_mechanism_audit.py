"""Fixed source sensitivity and attribution of already-defined short exit rules."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.flow_structure_followup import short_modes
from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers, attribute_ledger
from backtest.run_flow_robustness_diagnostic import episodes, ret_metrics, EVENTS
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest, git_state

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT/'backtest/output/runs/20260916-ew-flow-structure-followup-r1'
RUN_ID = '20260916-ew-exit-mechanism-audit-r1'
SPEC = ROOT/'docs/plans/2026-09-16-ew-exit-mechanism-audit.md'
SCENARIOS = [('close_3bps', 3., 0), ('close_10bps', 10., 0), ('second_close_3bps', 3., 1)]
MODES = ['exit_only', 'entry_exit']


def capture(run):
    files = ['inputs/scores.csv', 'inputs/wset_continued_scores.csv', 'inputs/futures.csv', 'inputs/spot.csv',
             'outputs/decision_targets.csv', 'outputs/response-09-office-2026-09-16.md']
    files += [f'outputs/ledgers_{s}.csv.gz' for s, _, _ in SCENARIOS]
    receipts = []
    for rel in files:
        path = PRIOR/rel
        shutil.copyfile(path, run/'inputs'/path.name)
        receipts.append(artifact_record(path, ROOT))
    (run/'inputs/source_receipts.json').write_text(json.dumps(receipts, indent=2))
    shutil.copyfile(SPEC, run/'inputs/prereg.md')
    shutil.copyfile(ROOT/'tests/test_exit_mechanism_audit.py', run/'inputs/code__test_exit_mechanism_audit.py')
    for name in ['run_exit_mechanism_audit.py', 'flow_structure_followup.py', 'intraday_flow_pilot.py',
                 'run_flow_robustness_diagnostic.py', 'execution_ledger.py', 'run_intraday_flow_pilot.py',
                 'data.py', 'metrics.py', 'run_manifest.py']:
        shutil.copyfile(ROOT/'backtest'/name, run/'inputs'/('code__'+name))


def decision_inventory(base, score, modes, source):
    """Labels are for accounting only; they never generate a position."""
    negative = base.lt(0).to_numpy()
    starts = np.flatnonzero(negative & ~np.r_[False, negative[:-1]])
    labels = pd.DataFrame('same', index=base.index, columns=MODES)
    records = []
    gate = score.gt(0) | score.abs().le(1e-12)
    for number, start in enumerate(starts):
        stops = np.flatnonzero(~negative[start:])
        stop = start+stops[0] if len(stops) else len(base)
        days = base.index[start:stop]
        daily = modes.daily.loc[days]
        for mode in MODES:
            target = modes[mode].loc[days]
            forced = target.lt(daily)
            blocked = target.gt(daily)
            reason = 'entry_rejected' if mode == 'entry_exit' and not gate.iloc[start] else 'suppressed_reentry'
            labels.loc[days[forced], mode] = 'forced_entry'
            labels.loc[days[blocked], mode] = reason
            held = np.flatnonzero(target.lt(0))
            flat = np.flatnonzero(target.eq(0) & (np.arange(len(target)) > (held[0] if len(held) else -1)))
            daily_entries = daily.lt(0) & ~daily.shift(1, fill_value=0).lt(0)
            records.append({'source': source, 'mode': mode, 'decision_episode': number,
                            'start': str(days[0].date()), 'last_decision': str(days[-1].date()),
                            'left_censored': bool(start == 0), 'right_censored': bool(stop == len(base)),
                            'entry_gate': bool(gate.iloc[start]), 'decision_days': len(days),
                            'held_days': int(target.lt(0).sum()), 'daily_held_days': int(daily.lt(0).sum()),
                            'first_flat_after_entry_or_rejection': str(days[flat[0]].date()) if len(flat) else None,
                            'daily_reentries': max(0, int(daily_entries.sum())-1),
                            'daily_entries_after_baseline_start': int(daily_entries.iloc[1:].sum()),
                            'forced_entry_days': int(forced.sum()), 'blocked_daily_holding_days': int(blocked.sum()),
                            'blocked_daily_entry_count': int((blocked & daily_entries).sum())})
    for mode in MODES:
        np.testing.assert_array_equal(labels[mode].ne('same'), modes[mode].ne(modes.daily))
        assert (modes[mode].ge(base) & modes[mode].le(0)).all()
        if mode == 'entry_exit':
            assert not labels[mode].eq('forced_entry').any()
    return labels, pd.DataFrame(records)


def execute(run):
    def read(name, **kw):
        return pd.read_csv(run/'inputs'/name, **kw)
    prior_target = read('decision_targets.csv', index_col=0, parse_dates=True)
    idx = prior_target.index
    lb, sb = prior_target.L__base, prior_target.S__base
    scores = {'new': read('scores.csv', index_col=0, parse_dates=True).CSI300.reindex(idx),
              'old': read('wset_continued_scores.csv', index_col=0, parse_dates=True).CSI300.reindex(idx)}
    modes = {source: short_modes(sb, values) for source, values in scores.items()}
    reuse = {'C__base': 'C__base', 'S__base': 'S__base', 'L__base': 'L__base', 'C__half': 'C__halfS'}
    targets = pd.DataFrame({'C__base': lb+sb, 'S__base': sb, 'L__base': lb, 'C__half': lb+.5*sb}, index=idx)
    definitions, inventories, phase = [], [], {}
    for source in ['new', 'old']:
        mapping = 'B_daily' if source == 'new' else 'C_old_binary'
        for prefix in ['C', 'S']:
            name = prefix+'__'+source+'_daily'
            reuse[name] = prefix+'__'+mapping
            targets[name] = (lb if prefix == 'C' else 0)+modes[source].daily
        phase[source], inventory = decision_inventory(sb, scores[source], modes[source], source)
        inventories.append(inventory)
        for mode in MODES:
            name = source+'_'+mode
            selected = modes[source][mode]
            q = float(selected.abs().sum()/sb.abs().sum())
            r = float(selected.abs().sum()/modes[source].daily.abs().sum())
            if not 0 <= r <= 1:
                raise ValueError('scaled daily reference would add leverage')
            for prefix, target in [('C', lb+selected), ('S', selected), ('Q', lb+q*sb), ('QS', q*sb),
                                   ('D', lb+r*modes[source].daily), ('DS', r*modes[source].daily)]:
                key = prefix+'__'+name
                targets[key] = target
                if source == 'new' and prefix in ['C', 'S', 'Q', 'QS']:
                    reuse[key] = prefix+'__B_'+mode
                    np.testing.assert_allclose(target, prior_target[reuse[key]], atol=1e-12, rtol=0)
            definitions.append({'candidate': name, 'source': source, 'mode': mode,
                                'q_baseline': q, 'r_daily': r, 'held_days': int(selected.lt(0).sum())})
    pd.DataFrame(definitions).to_csv(run/'outputs/definitions.csv', index=False)
    pd.concat(inventories, ignore_index=True).to_csv(run/'outputs/decision_episode_inventory.csv', index=False)
    targets.to_csv(run/'outputs/decision_targets.csv', index_label='date')
    futures = read('futures.csv', parse_dates=['date'])
    spot = read('spot.csv', parse_dates=['date'])
    calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol)); after = calendar[calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    weights = futures_weights(idx, futures.loc[futures.symbol.str.startswith('IM'), 'date'].min())
    market = prepare_close_market(futures, idx, weights, expiries)
    windows = {'full': idx, 'early_half': idx[:len(idx)//2], 'late_half': idx[len(idx)//2:]}
    windows.update({f'year_{year}': idx[idx.year==year] for year in sorted(set(idx.year))})
    event_mask = pd.Series(False, index=idx)
    for date in EVENTS:
        event_mask.loc[idx[idx>=date][:10]] = True
    assert event_mask.sum() == 70
    metrics, checks, comparison_rows, annual, ep_rows, daily_rows, stress, phases, source_rows, phase_daily = [], [], [], [], [], [], [], [], [], []
    total_new = 0
    for scenario, cost, lag in SCENARIOS:
        print(scenario, flush=True)
        prior = read(f'ledgers_{scenario}.csv.gz', index_col=['strategy', 'date'], parse_dates=['date'])
        signals = targets.shift(lag, fill_value=0.)
        book = {new: prior.loc[old] for new, old in reuse.items()}
        for name, ledger in book.items():
            np.testing.assert_allclose(ledger.decision_signal, signals[name].shift(1, fill_value=0.), atol=1e-12)
        fresh = batch_close_ledgers(market, signals.drop(columns=list(reuse)), cost_bps=cost)
        assert len(fresh) == 16
        total_new += len(fresh)
        for name in ['C__old_exit_only', 'D__new_entry_exit']:
            reference = contract_ledger(futures, signals[name], weights, expiries=expiries, cost_bps=cost)
            np.testing.assert_allclose(fresh[name].ret, reference.ret, atol=1e-12, rtol=1e-10)
            checks.append({'scenario': scenario, 'strategy': name, 'max_engine_error': float((fresh[name].ret-reference.ret).abs().max())})
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
        ep.to_csv(run/f'outputs/baseline_episodes_{scenario}.csv', index=False)
        for definition in definitions:
            name, source, mode = definition['candidate'], definition['source'], definition['mode']
            a = book['C__'+name].ret
            for comparison, right in [('daily', 'C__'+source+'_daily'), ('scaled_daily', 'D__'+name),
                                      ('matched_base', 'Q__'+name), ('base', 'C__base'), ('half', 'C__half')]:
                b = book[right].ret
                delta = np.log1p(a)-np.log1p(b)
                year_values = delta.groupby(idx.year).sum()
                np.testing.assert_allclose(year_values.sum(), delta.sum(), atol=1e-12, rtol=0)
                for year, value in year_values.items():
                    annual.append({'scenario': scenario, 'candidate': name, 'comparison': comparison, 'year': int(year), 'delta_log': float(value)})
                e = ep.copy(); e['delta_log'] = [float(delta.loc[labels.eq(i)].sum()) for i in e.episode]
                remainder = float(delta.loc[labels.eq(-1)].sum())
                np.testing.assert_allclose(e.delta_log.sum()+remainder, delta.sum(), atol=1e-12, rtol=0)
                ep_rows.append(e.assign(scenario=scenario, candidate=name, comparison=comparison))
                ep_rows.append(pd.DataFrame([{'episode': -1, 'delta_log': remainder, 'scenario': scenario, 'candidate': name, 'comparison': comparison}]))
                daily_rows.append(pd.DataFrame({'date': idx, 'scenario': scenario, 'candidate': name,
                                                'comparison': comparison, 'delta_log': delta.values}))
                top = delta.nlargest(5)
                comparison_rows.append({'scenario': scenario, 'candidate': name, 'comparison': comparison,
                                        'delta_log': float(delta.sum()), 'sharpe_difference': ret_metrics(a)['sharpe']-ret_metrics(b)['sharpe'],
                                        'positive_episodes': int(e.delta_log.gt(1e-12).sum()), 'negative_episodes': int(e.delta_log.lt(-1e-12).sum()),
                                        'event_inside_log': float(delta[event_mask].sum()), 'event_outside_log': float(delta[~event_mask].sum()),
                                        'top_day': str(top.index[0].date()), 'top_day_log': float(top.iloc[0]), 'top5_log': float(top.sum())})
                if comparison in ['daily', 'scaled_daily']:
                    removals = [('full', pd.Index([])), ('best_one_day', top.index[:1]), ('best_five_days', top.index),
                                ('seven_events', idx[event_mask])]
                    removals += [(f'episode_{i}', idx[labels.eq(i)]) for i in ep.episode]
                    for zero_type, dates in removals:
                        aa, bb = a.copy(), b.copy(); aa.loc[dates] = 0.; bb.loc[dates] = 0.
                        ma, mb = ret_metrics(aa), ret_metrics(bb)
                        stress.append({'scenario': scenario, 'candidate': name, 'comparison': comparison, 'zero_type': zero_type,
                                       'zeroed_days': len(dates), 'left_cagr': ma['cagr'], 'right_cagr': mb['cagr'],
                                       'sharpe_difference': ma['sharpe']-mb['sharpe']})
                if comparison == 'daily':
                    decision = phase[source][mode]
                    holding = decision.shift(lag+2, fill_value='same')
                    fill = decision.shift(lag+1, fill_value='same')
                    group = holding+' -> '+fill
                    grouped = delta.groupby(group).sum()
                    np.testing.assert_allclose(grouped.sum(), delta.sum(), atol=1e-12, rtol=0)
                    for label, value in grouped.items():
                        phases.append({'scenario': scenario, 'candidate': name, 'transition': label,
                                       'days': int(group.eq(label).sum()), 'delta_log': float(value)})
                    phase_daily.append(pd.DataFrame({'date': idx, 'scenario': scenario, 'candidate': name,
                                                     'decision_label': decision.values, 'holding_label': holding.values,
                                                     'fill_label': fill.values, 'delta_log': delta.values}))
        for mode in ['daily', *MODES]:
            new = book['C__new_'+mode]; old = book['C__old_'+mode]
            different = targets['C__new_'+mode].ne(targets['C__old_'+mode])
            source_rows.append({'scenario': scenario, 'mode': mode, 'decision_differences': int(different.sum()),
                                'filled_differences': int(new.decision_signal.ne(old.decision_signal).sum()),
                                'old_minus_new_log': float((np.log1p(old.ret)-np.log1p(new.ret)).sum()),
                                'old_minus_new_sharpe': describe(old)['sharpe']-describe(new)['sharpe']})
            if scenario == 'close_3bps':
                pd.DataFrame({'new_score': scores['new'], 'old_score': scores['old'],
                              'new_target': targets['C__new_'+mode], 'old_target': targets['C__old_'+mode]}).loc[different].to_csv(run/f'outputs/source_differences_{mode}.csv', index_label='date')
    for name, rows in [('metrics', metrics), ('comparisons', comparison_rows), ('annual_attribution', annual),
                       ('stress', stress), ('phase_attribution', phases), ('source_sensitivity', source_rows)]:
        pd.DataFrame(rows).to_csv(run/f'outputs/{name}.csv', index=False)
    for name, parts in [('episode_attribution.csv', ep_rows), ('daily_attribution.csv.gz', daily_rows), ('phase_daily.csv.gz', phase_daily)]:
        pd.concat(parts, ignore_index=True).to_csv(run/'outputs'/name, index=False)
    report = {'new_ledgers': total_new, 'reused_ledgers': len(reuse)*3, 'reference_checks': checks,
              'account_leg_trade_identities': True, 'phase_episode_year_additivity': True,
              'all_reused_targets_match': True, 'new_state_rules': False,
              'samples': len(idx), 'start': str(idx[0].date()), 'end': str(idx[-1].date()),
              'completed_utc': datetime.now(timezone.utc).isoformat()}
    (run/'outputs/verification.json').write_text(json.dumps(report, indent=2))
    print(pd.DataFrame(metrics).query("scenario=='close_3bps' and window=='full' and strategy.str.startswith('C__')", engine='python')[['strategy', 'cagr', 'sharpe', 'maxdd']].round(5).to_string(index=False), flush=True)
    return report


def main():
    run = create_run_dir(ROOT/'backtest/output/runs', RUN_ID)
    capture(run)
    manifest = {'run_id': RUN_ID, 'status': 'running', 'git': git_state(ROOT),
                'stage': 'fixed_exit_mechanism_source_and_attribution_audit',
                'prereg_sha256': artifact_record(run/'inputs/prereg.md', run)['sha256']}
    write_manifest(run, manifest)
    try:
        verification = execute(run)
    except Exception as exc:
        manifest.update(status='failed', error_type=type(exc).__name__)
        write_manifest(run, manifest)
        raise
    manifest.update(status='computed_pending_report', verification=verification,
                    artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*')) if p.is_file() and p.name!='manifest.json'])
    write_manifest(run, manifest)


if __name__ == '__main__':
    main()
