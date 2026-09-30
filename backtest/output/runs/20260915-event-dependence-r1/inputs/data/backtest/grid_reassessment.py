"""Frozen original-grid reassessment; descriptive research, no deployment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import annualized_basis
from backtest.engine import run_strategy
from backtest.execution_audit import ROOT, RUN_ROOT, END, inputs, panel
from backtest.execution_ledger import contract_ledger, combine_pools
from backtest.metrics import sharpe
from backtest.momentum_scan import momentum_grid, momentum_pair_factor
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff
from backtest.run_manifest import artifact_record, create_run_dir, git_state, write_manifest
from backtest.scan import default_grid
from signals.equal_weight.generate_signal import (
    calculate_contrast_equal_weight_signal, load_pair_configs,
)

PLAN = ROOT / 'docs/superpowers/plans/2026-09-14-grid-reassessment-plan.md'
WINDOWS = {'train': ('2014-01-01', '2020-12-31'),
           'val': ('2021-01-01', '2023-12-31'),
           'reused': ('2024-01-01', END)}
INCUMBENTS = {'ew': 'ew_L20_zw40_sm5', 'slope': 'slope_L20s0_zw120_sm0'}


def blend_carry(legs):
    """Return old skip-missing blend and repaired fixed-half blend."""
    return legs.mean(axis=1).dropna(), legs.fillna(0).mean(axis=1)


def rank_table(table):
    table = table.copy()
    if not np.isfinite(table[['sharpe_train', 'sharpe_val', 'sharpe_reused']]).all().all():
        raise ValueError('nonfinite scan statistic')
    table['score'] = table[['sharpe_train', 'sharpe_val']].min(axis=1)
    table['old_three_window_score'] = table[['sharpe_train', 'sharpe_val', 'sharpe_reused']].min(axis=1)
    table['rank_grid'] = table.groupby('grid').score.rank(method='first', ascending=False).astype(int)
    table['rank_family'] = table.groupby('family').score.rank(method='first', ascending=False).astype(int)
    return table


def reconstruct_carry(spot, fut):
    closes = spot.pivot(index='date', columns='symbol', values='close')
    legs = {}
    for group, code in [('IC', '000905.SH'), ('IM', '000852.SH')]:
        quotes = fut[fut.symbol.str.startswith(group)].sort_values(['date', 'symbol'])
        main = quotes.loc[quotes.groupby('date').oi.idxmax()]
        legs[group] = pd.Series({r.date: annualized_basis(
            r.close, closes.at[r.date, code], r.date.date(), r.symbol) for r in main.itertuples()})
    frame = pd.concat(legs, axis=1)
    old, fixed = blend_carry(frame)
    return frame.assign(old=old, fixed=fixed)


def build_factors(prices, configs):
    factors, parameters = {}, []
    for p in default_grid():
        name = f"ew_L{p['lookback']}_zw{p['z_window']}_sm{p['smoothing']}"
        factors[name] = calculate_contrast_equal_weight_signal(
            prices, lookback=p['lookback'], z_window=p['z_window'],
            smoothing_window=p['smoothing'], pair_configs=configs).factor_value
        parameters.append({'name': name, 'grid': 'ew40', 'family': 'ew', **p})
    pairs = [p.effective_columns() for p in configs]
    for p in momentum_grid():
        name = f"{p['family']}_L{p['length']}s{p['skip']}_zw{p['z_window']}_sm{p['smoothing']}"
        factors[name] = momentum_pair_factor(prices, pairs, **p)
        parameters.append({'name': name, 'grid': 'momentum174', **p})
    assert len(factors) == 214 and len(parameters) == 214
    return factors, parameters


def scan(factors, parameters, underlying, carry_frame):
    output = []
    for regime in ('old_sym', 'fixed_sym', 'spot_lf'):
        rows = []
        carry = None if regime == 'spot_lf' else carry_frame['old' if regime == 'old_sym' else 'fixed']
        for p in parameters:
            factor = factors[p['name']]
            pos = (factor > 0).astype(float) if regime == 'spot_lf' else np.sign(factor)
            row = dict(p, regime=regime)
            for win, (a, b) in WINDOWS.items():
                idx = pos.loc[a:b].index.intersection(underlying.dropna().loc[a:b].index)
                returns = run_strategy(pos.reindex(idx), underlying.reindex(idx), 3.,
                                       None if carry is None else carry.reindex(idx)).ret
                row[f'sharpe_{win}'] = sharpe(returns)
                row[f'n_{win}'] = len(returns)
            rows.append(row)
        table = rank_table(pd.DataFrame(rows))
        output.append(table)
        print(table[table.rank_family.eq(1)][['regime', 'name', 'score']].to_string(index=False), flush=True)
    return pd.concat(output, ignore_index=True)


def execute_selected(run, table, factors, spot, fut, idx, weights, expiries):
    def winner(regime, family):
        return table.loc[table.regime.eq(regime) & table.family.eq(family) & table.rank_family.eq(1), 'name'].item()

    ew = [INCUMBENTS['ew'], winner('fixed_sym', 'ew')]
    slopes = [INCUMBENTS['slope'], winner('fixed_sym', 'slope'), winner('spot_lf', 'slope')]
    ew, slopes = list(dict.fromkeys(ew)), list(dict.fromkeys(slopes))
    selection = {'ew': ew, 'slope': slopes, 'rule': 'frozen plan, train/val only'}
    (run / 'outputs/selected.json').write_text(json.dumps(selection, indent=2))
    pd.DataFrame({k: factors[k].round(4) for k in ew + slopes}).to_csv(run / 'outputs/selected_factors.csv', index_label='date')
    all_ledgers, comparisons = {}, []
    for cost in (3., 10.):
        legs_ew, legs_slope = {}, {}
        for name in ew:
            legs_ew[name] = contract_ledger(fut, np.sign(factors[name].round(4)).reindex(idx),
                weights, fill='close', cost_bps=cost, expiries=expiries)
        for name in slopes:
            legs_slope[name] = contract_ledger(spot, (factors[name].round(4) > 0).astype(float).reindex(idx),
                {'000905.SH': .5, '000852.SH': .5}, select_main=False, fill='close', cost_bps=cost)
        combos = {}
        for e, el in legs_ew.items():
            all_ledgers[f'futures_{e}_{cost:g}bps'] = el
            for s, sl in legs_slope.items():
                name = f'two_pool_{e}__{s}_{cost:g}bps'
                combos[name] = combine_pools(sl, el)
        for s, sl in legs_slope.items():
            all_ledgers[f'spot_{s}_{cost:g}bps'] = sl
        reference = f"two_pool_{INCUMBENTS['ew']}__{INCUMBENTS['slope']}_{cost:g}bps"
        for name, ledger in combos.items():
            if name == reference:
                continue
            for block in (20, 60):
                comparisons.append({'name': name, 'reference': reference, 'cost_bps': cost,
                    **paired_block_bootstrap_sharpe_diff(ledger.ret, combos[reference].ret,
                                                       block=block, n=2000, seed=20260914)})
        all_ledgers.update(combos)
        print(f'Executed cost {cost:g} bps: {len(ew)} EW, {len(slopes)} slope, {len(combos)} combinations', flush=True)
    for name, ledger in all_ledgers.items():
        ledger.to_csv(run / f'outputs/ledger_{name}.csv', index_label='date')
    panel(all_ledgers).to_csv(run / 'outputs/execution_panel.csv', index=False)
    pd.DataFrame(comparisons).to_csv(run / 'outputs/paired_descriptive.csv', index=False)
    return selection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--snapshot-from', type=Path, required=True)
    args = parser.parse_args()
    source = args.snapshot_from
    prior = json.loads((source / 'manifest.json').read_text())
    run = create_run_dir(RUN_ROOT, args.run_id)
    shutil.copyfile(PLAN, run / 'inputs/prereg.md')
    write_manifest(run, {'status': 'running', 'created_utc': datetime.now(timezone.utc).isoformat(), 'git': git_state(ROOT)})
    verified = []
    for filename in ('spot.csv', 'futures.csv', 'equal_weight.csv', 'slope20.csv',
                     'snapshot.json', 'style_prices.csv', 'config_4pairs.csv'):
        path = source / 'inputs' / filename
        record = artifact_record(path, source)
        expected = next(a for a in prior['artifacts'] if a['path'] == record['path'])
        if record != expected:
            raise ValueError(f'input hash changed: {filename}')
        shutil.copyfile(path, run / 'inputs' / filename)
        verified.append(record)
    code = run / 'inputs/code'
    code.mkdir()
    for path in ('backtest/grid_reassessment.py', 'backtest/scan.py', 'backtest/momentum_scan.py',
                 'backtest/engine.py', 'backtest/metrics.py', 'backtest/data.py',
                 'backtest/execution_audit.py', 'backtest/execution_ledger.py',
                 'backtest/paired_bootstrap.py', 'backtest/run_manifest.py',
                 'signals/equal_weight/generate_signal.py', 'signals/common/factors.py'):
        shutil.copyfile(ROOT / path, code / path.replace('/', '__'))
    for filename in ('scan_equal_weight.csv', 'scan_momentum.csv'):
        shutil.copyfile(ROOT / 'backtest/output' / filename, run / 'inputs' / f'historical_{filename}')
    spot, fut, idx, signals, weights, expiries, und, fixed = inputs(run)
    carries = reconstruct_carry(spot, fut)
    pd.testing.assert_series_equal(carries.fixed, fixed, check_names=False)
    carries.to_csv(run / 'outputs/carry_comparison.csv', index_label='date')
    prices = pd.read_csv(run / 'inputs/style_prices.csv', index_col='date', parse_dates=True)
    if prices.index.max() > pd.Timestamp(END):
        raise ValueError('future input beyond frozen endpoint')
    factors, parameters = build_factors(prices, load_pair_configs(run / 'inputs/config_4pairs.csv'))
    checks = []
    for family, name in INCUMBENTS.items():
        filename = 'equal_weight' if family == 'ew' else 'slope20'
        prod = pd.read_csv(run / f'inputs/{filename}.csv', index_col='date', parse_dates=True).factor_value
        rebuilt = factors[name].round(4).reindex(prod.index)
        if rebuilt.isna().any() or not np.allclose(rebuilt, prod, atol=1e-10, rtol=0):
            raise ValueError(f'production reconstruction mismatch: {name}')
        checks.append({'name': name, 'n': len(prod), 'matches_rounded_production': True,
                       'rounding_sign_changes': int((np.sign(factors[name]) != np.sign(factors[name].round(4))).sum())})
    table = scan(factors, parameters, und, carries)
    table.to_csv(run / 'outputs/full_grid.csv', index=False)
    table[table.name.isin(INCUMBENTS.values())].to_csv(run / 'outputs/incumbent_ranks.csv', index=False)
    table[table.rank_family.eq(1)].to_csv(run / 'outputs/family_winners.csv', index=False)
    table[table.family.eq('slope') & table.length.eq(20)].to_csv(run / 'outputs/slope20_neighborhood.csv', index=False)
    selection = execute_selected(run, table, factors, spot, fut, idx, weights, expiries)
    (run / 'outputs/validation.json').write_text(json.dumps(checks, indent=2))
    artifacts = [artifact_record(p, run) for sub in ('inputs', 'outputs', 'logs')
                 for p in sorted((run / sub).rglob('*')) if p.is_file()]
    write_manifest(run, {'status': 'complete', 'completed_utc': datetime.now(timezone.utc).isoformat(),
                        'git': git_state(ROOT), 'artifacts': artifacts, 'source_verified': verified,
                        'selected': selection, 'inference': 'descriptive, reused history; no selection-adjusted PASS'})
    print(run, flush=True)


if __name__ == '__main__':
    main()
