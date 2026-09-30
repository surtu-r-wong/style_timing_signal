"""Descriptive, hindsight age-matched controls for six frozen flow rules."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers, attribute_ledger
from backtest.run_flow_robustness_diagnostic import episodes, ret_metrics
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest, git_state

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT/'backtest/output/runs/20260916-ew-exit-mechanism-audit-r1'
RUN_ID = '20260917-ew-holding-age-audit-r1'
SPEC = ROOT/'docs/plans/2026-09-17-ew-holding-age-audit.md'
SCENARIOS = [('close_3bps', 3., 0), ('close_10bps', 10., 0), ('second_close_3bps', 3., 1)]
NAMES = [s+'_'+m for s in ['new', 'old'] for m in ['daily', 'exit_only', 'entry_exit']]


def age_matched_target(base, selected):
    """Full-sample exposure projection on EW short-episode age, never a live rule."""
    if not base.index.equals(selected.index) or not base.index.is_unique or not base.index.is_monotonic_increasing:
        raise ValueError('invalid or mismatched index')
    if len(base) == 0 or not base.isin([0., -1.]).all():
        raise ValueError('base must be a nonempty binary short target')
    if base.iloc[0] < 0:
        raise ValueError('unknown left-censored episode age')
    if not np.isfinite(selected).all() or not (selected.ge(base) & selected.le(0)).all():
        raise ValueError('candidate must be finite and contained in base')
    active = base.lt(0)
    starts = active & ~active.shift(1, fill_value=False)
    episode = starts.cumsum().where(active, -1).astype(int)
    age = pd.Series(0, index=base.index, dtype=int)
    age.loc[active] = episode.loc[active].groupby(episode.loc[active]).cumcount()+1
    metadata = pd.DataFrame({'episode': episode, 'age': age, 'right_censored': False})
    if active.iloc[-1]:
        metadata.loc[episode.eq(episode.iloc[-1]), 'right_censored'] = True
    profile = selected.loc[active].abs().groupby(age.loc[active]).agg(['size', 'sum', 'mean'])
    profile.columns = ['n_days', 'selected_exposure', 'mean_exposure']
    profile.index.name = 'age'
    target = -age.map(profile.mean_exposure).where(active, 0.)
    np.testing.assert_allclose(target.loc[active].abs().groupby(age.loc[active]).sum(),
                               profile.selected_exposure, atol=1e-12, rtol=0)
    return target, metadata, profile


def capture(run):
    manifest = json.loads((PRIOR/'manifest.json').read_text())
    assert manifest['status'] == 'complete'
    records = {r['path']: r for r in manifest['artifacts']}
    files = ['inputs/futures.csv', 'inputs/spot.csv', 'outputs/decision_targets.csv']
    files += [f'outputs/ledgers_{s}.csv.gz' for s, _, _ in SCENARIOS]
    receipts = []
    for rel in files:
        source = PRIOR/rel
        actual = artifact_record(source, PRIOR)
        assert actual == records[rel], f'prior artifact changed: {rel}'
        destination = run/'inputs'/source.name
        shutil.copyfile(source, destination)
        assert artifact_record(destination, run)['sha256'] == actual['sha256']
        receipts.append(artifact_record(source, ROOT))
    shutil.copyfile(PRIOR/'manifest.json', run/'inputs/source_manifest.json')
    (run/'inputs/source_receipts.json').write_text(json.dumps(receipts, indent=2))
    shutil.copyfile(SPEC, run/'inputs/prereg.md')
    files = ['backtest/run_holding_age_audit.py', 'tests/test_holding_age_audit.py',
             'backtest/intraday_flow_pilot.py', 'backtest/execution_ledger.py', 'backtest/data.py',
             'backtest/metrics.py', 'backtest/run_manifest.py', 'backtest/run_flow_robustness_diagnostic.py',
             'backtest/run_intraday_flow_pilot.py']
    for rel in files:
        shutil.copyfile(ROOT/rel, run/'inputs'/('code__'+Path(rel).name))


def execute(run):
    def read(name, **kw):
        return pd.read_csv(run/'inputs'/name, **kw)
    prior = read('decision_targets.csv', index_col=0, parse_dates=True)
    idx = prior.index
    targets = pd.DataFrame(index=idx)
    profiles, definitions = [], []
    for name in NAMES:
        control, metadata, profile = age_matched_target(prior.S__base, prior['S__'+name])
        targets['A__'+name] = prior.L__base+control
        targets['AS__'+name] = control
        profiles.append(profile.reset_index().assign(candidate=name))
        definitions.append({'candidate': name, 'selected_exposure_days': float(prior['S__'+name].abs().sum()),
                            'age_reference_exposure_days': float(control.abs().sum()),
                            'sparse_age_days_n_lt_5': int(profile.loc[profile.n_days.lt(5), 'n_days'].sum()),
                            'max_age': int(profile.index.max())})
    metadata.to_csv(run/'outputs/decision_ages.csv', index_label='date')
    pd.concat(profiles, ignore_index=True).to_csv(run/'outputs/age_profiles.csv', index=False)
    pd.DataFrame(definitions).to_csv(run/'outputs/definitions.csv', index=False)
    targets.to_csv(run/'outputs/age_targets.csv', index_label='date')
    futures = read('futures.csv', parse_dates=['date'])
    spot = read('spot.csv', parse_dates=['date'])
    calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol))
        after = calendar[calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    weights = futures_weights(idx, futures.loc[futures.symbol.str.startswith('IM'), 'date'].min())
    market = prepare_close_market(futures, idx, weights, expiries)
    windows = {'full': idx, 'early_half': idx[:len(idx)//2], 'late_half': idx[len(idx)//2:]}
    windows.update({f'year_{y}': idx[idx.year==y] for y in sorted(set(idx.year))})
    reused_names = ['C__base', 'S__base', 'C__half']+[p+'__'+n for n in NAMES for p in ['C', 'S']]
    metrics, comparisons, stresses, attribution, daily, checks, exposure = [], [], [], [], [], [], []
    for scenario, cost, lag in SCENARIOS:
        print(scenario, flush=True)
        old = read(f'ledgers_{scenario}.csv.gz', index_col=['strategy', 'date'], parse_dates=['date'])
        book = {name: old.loc[name] for name in reused_names}
        for name, ledger in book.items():
            assert ledger.index.equals(idx)
            np.testing.assert_allclose(ledger.decision_signal, prior[name].shift(lag+1, fill_value=0.), atol=1e-12)
        signals = targets.shift(lag, fill_value=0.)
        fresh = batch_close_ledgers(market, signals, cost_bps=cost)
        for name in ['A__new_exit_only', 'AS__old_daily']:
            reference = contract_ledger(futures, signals[name], weights, expiries=expiries, cost_bps=cost)
            np.testing.assert_allclose(fresh[name].ret, reference.ret, atol=1e-12, rtol=1e-10)
            checks.append({'scenario': scenario, 'strategy': name,
                           'max_engine_error': float((fresh[name].ret-reference.ret).abs().max())})
        trades = []
        for name, ledger in fresh.items():
            np.testing.assert_allclose(ledger.decision_signal, targets[name].shift(lag+1, fill_value=0.), atol=1e-12)
            np.testing.assert_allclose(ledger.gross_pnl-ledger.cost,
                                       ledger.equity.diff().fillna(ledger.equity.iloc[0]-1.), atol=1e-12, rtol=1e-10)
            _, t = attribute_ledger(ledger)
            trades.append(t.assign(strategy=name))
        book.update(fresh)
        pd.concat(book, names=['strategy', 'date']).to_csv(run/f'outputs/ledgers_{scenario}.csv.gz')
        pd.concat(trades, ignore_index=True).to_csv(run/f'outputs/new_trades_{scenario}.csv', index=False)
        labels, ep = episodes(book['C__base'])
        ep.to_csv(run/f'outputs/baseline_episodes_{scenario}.csv', index=False)
        for name, ledger in book.items():
            for window, dates in windows.items():
                metrics.append({'scenario': scenario, 'strategy': name, 'window': window, **describe(ledger.loc[dates])})
        for name in NAMES:
            for window, dates in windows.items():
                exposure.append({'scenario': scenario, 'candidate': name, 'window': window,
                                 'selected_decision_exposure': float(prior.loc[dates, 'S__'+name].abs().mean()),
                                 'age_decision_exposure': float(targets.loc[dates, 'AS__'+name].abs().mean()),
                                 'selected_fill_exposure': float(book['S__'+name].loc[dates].decision_signal.abs().mean()),
                                 'age_fill_exposure': float(book['AS__'+name].loc[dates].decision_signal.abs().mean())})
            for side, left, right in [('combined', 'C__', 'A__'), ('short', 'S__', 'AS__')]:
                a, b = book[left+name].ret, book[right+name].ret
                delta = np.log1p(a)-np.log1p(b)
                daily.append(pd.DataFrame({'date': idx, 'scenario': scenario, 'candidate': name,
                                           'side': side, 'delta_log': delta.values}))
                for window, dates in windows.items():
                    ma, mb = ret_metrics(a.loc[dates]), ret_metrics(b.loc[dates])
                    comparisons.append({'scenario': scenario, 'candidate': name, 'side': side, 'window': window,
                                        'sharpe_difference': ma['sharpe']-mb['sharpe'],
                                        'candidate_cagr': ma['cagr'], 'age_reference_cagr': mb['cagr'],
                                        'delta_log': float(delta.loc[dates].sum())})
                grouped = delta.groupby(labels).sum()
                np.testing.assert_allclose(grouped.sum(), delta.sum(), atol=1e-12, rtol=0)
                np.testing.assert_allclose(delta.groupby(idx.year).sum().sum(), delta.sum(), atol=1e-12, rtol=0)
                for episode, value in grouped.items():
                    attribution.append({'scenario': scenario, 'candidate': name, 'side': side,
                                        'episode': int(episode), 'delta_log': float(value)})
                top = delta.nlargest(5).index
                for count in [0, 1, 5]:
                    aa, bb = a.copy(), b.copy()
                    aa.loc[top[:count]] = 0.; bb.loc[top[:count]] = 0.
                    stresses.append({'scenario': scenario, 'candidate': name, 'side': side,
                                     'best_days_zeroed': count,
                                     'sharpe_difference': ret_metrics(aa)['sharpe']-ret_metrics(bb)['sharpe']})
    for name, rows in [('metrics', metrics), ('comparisons', comparisons), ('stress', stresses),
                       ('episode_attribution', attribution), ('exposure_by_window', exposure)]:
        pd.DataFrame(rows).to_csv(run/f'outputs/{name}.csv', index=False)
    pd.concat(daily, ignore_index=True).to_csv(run/'outputs/relative_daily.csv.gz', index=False)
    verification = {'new_ledgers': 36, 'reused_ledgers': len(reused_names)*3, 'reference_checks': checks,
                    'source_hashes_verified': True, 'account_leg_trade_identities': True,
                    'episode_year_additivity': True, 'target_delays_verified': True,
                    'age_exposure_matching': True, 'samples': len(idx),
                    'decision_episodes': int(metadata.episode.loc[metadata.age.gt(0)].nunique()),
                    'right_censored_decision_days': int(metadata.right_censored.sum()),
                    'start': str(idx[0].date()), 'end': str(idx[-1].date()),
                    'hindsight_reference_not_deployable': True,
                    'completed_utc': datetime.now(timezone.utc).isoformat()}
    (run/'outputs/verification.json').write_text(json.dumps(verification, indent=2))
    print(pd.DataFrame(comparisons).query("scenario=='close_3bps' and window=='full'").round(6).to_string(index=False), flush=True)
    return verification


def main():
    run = create_run_dir(ROOT/'backtest/output/runs', RUN_ID)
    manifest = {'run_id': RUN_ID, 'status': 'running', 'git': git_state(ROOT),
                'stage': 'descriptive_hindsight_holding_age_audit'}
    write_manifest(run, manifest)
    try:
        capture(run)
        manifest['prereg_sha256'] = artifact_record(run/'inputs/prereg.md', run)['sha256']
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
