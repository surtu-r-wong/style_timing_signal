"""Independently check frozen ML artifacts without training or changing the run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.run_manifest import artifact_record


def verify(run):
    manifest = json.loads((run/'manifest.json').read_text())
    assert manifest['status'] == 'complete'
    for record in manifest['artifacts']:
        assert artifact_record(run/record['path'], run) == record
    x = pd.read_csv(run/'outputs/features.csv', index_col='date', parse_dates=True)
    labels = pd.read_csv(run/'outputs/labels.csv', index_col='date', parse_dates=['date', 'label_end'])
    ew = pd.read_csv(run/'inputs/equal_weight.csv', index_col='date', parse_dates=True)
    raw_oi = pd.read_csv(run/'inputs/oi_features.csv', parse_dates=['date'])
    oi = raw_oi.loc[raw_oi.source.eq('IC')].set_index('date')
    raw_px = pd.read_csv(run/'inputs/index.csv', parse_dates=['date'])
    price = raw_px.loc[raw_px.index_code.eq('000905.SH')].set_index('date').close.sort_index()
    calendar = pd.read_csv(run/'inputs/calendar.csv', parse_dates=['date'])
    calendar = pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1), 'date'])
    feature_calendar = calendar[(calendar >= price.index.min()) & (calendar <= x.index[-1])]
    px = price.loc[feature_calendar].to_numpy()
    returns = np.r_[np.nan, px[1:]/px[:-1]-1]
    factors = ew.reindex(feature_calendar)
    signs = np.sign(factors.factor_value.to_numpy())
    pairs = [f'pair_0{i}_factor_20' for i in range(1, 5)]
    independent = []
    age = 0
    for i, date in enumerate(feature_calendar):
        age = age+1 if i and signs[i] == signs[i-1] else 1
        if date not in x.index:
            continue
        row = factors.loc[date, pairs].tolist()
        vol20 = np.std(returns[i-19:i+1], ddof=1)*np.sqrt(245)
        vol60 = np.std(returns[i-59:i+1], ddof=1)*np.sqrt(245)
        row += [factors.factor_value.iloc[i], factors.factor_value.iloc[i]-factors.factor_value.iloc[i-5],
                np.std(row, ddof=1), signs[i], np.log1p(min(age, 60)),
                px[i]/px[i-5]-1, px[i]/px[i-20]-1, vol20, vol20/vol60,
                px[i]/max(px[i-59:i+1])-1,
                oi.loc[feature_calendar[i-1], 'g5'], oi.loc[feature_calendar[i-1], 'v5']]
        independent.append(row)
    np.testing.assert_allclose(x, independent, atol=1e-10, rtol=1e-10)

    futures = pd.read_csv(run/'inputs/futures.csv', parse_dates=['date'])
    first_im = futures.loc[futures.symbol.str.startswith('IM'), 'date'].min()
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol))
        following = calendar[calendar >= expiry]
        expiries[symbol] = following[0] if len(following) else expiry
    unit = contract_ledger(futures, pd.Series(1., index=labels.index),
                           futures_weights(labels.index, first_im), expiries=expiries, cost_bps=0.).ret
    expected_y = []
    for i, date in enumerate(labels.index):
        if i+6 >= len(labels) or date not in x.index:
            expected_y.append(np.nan)
            continue
        scale = x.loc[date, 'rv20']*np.sqrt(5/245)
        expected_y.append(np.sign(ew.loc[date, 'factor_value'])*unit.iloc[i+2:i+7].sum()/scale)
        assert labels.label_end.iloc[i] == labels.index[i+6]
    np.testing.assert_allclose(labels.y, expected_y, equal_nan=True, atol=1e-10, rtol=1e-10)

    pred = pd.read_csv(run/'outputs/predictions.csv', index_col='date', parse_dates=True)
    targets = pd.read_csv(run/'outputs/targets.csv', index_col='date', parse_dates=True)
    base = np.sign(ew.factor_value.reindex(pred.index).to_numpy())
    np.testing.assert_array_equal(targets.ew, base)
    np.testing.assert_allclose(targets.fixed75, .75*base)
    np.testing.assert_allclose(targets.half_short, np.where(base < 0, .5*base, base))
    np.testing.assert_allclose(targets.vol20, base*np.clip(.20/x.rv20.reindex(pred.index), .5, 1))
    for name in pred.columns:
        raw = .75+.25*np.tanh(pred[name].to_numpy())
        smooth = np.array([np.mean(raw[max(0, i-4):i+1]) for i in range(len(raw))])
        np.testing.assert_allclose(targets[name], base*smooth, atol=1e-12)
        counts, sums, control = {-1: 0, 1: 0}, {-1: 0., 1: 0.}, []
        for side, q in zip(base, smooth):
            control.append(side*(sums[side]/counts[side] if side and counts[side] else .75))
            if side:
                counts[side] += 1
                sums[side] += q
        np.testing.assert_allclose(targets['past_'+name], control, atol=1e-12)

    audits = json.loads((run/'outputs/training_audit.json').read_text())
    for a in audits:
        test_start = pd.Timestamp(a['test_start'])
        eligible = labels.reindex(x.index)
        train = eligible.loc[eligible.label_end.lt(test_start) & eligible.y.notna()]
        val = train.iloc[-245:]
        inner = train.loc[train.label_end.lt(val.index[0])]
        assert len(train) == a['train_n'] and len(inner) == a['inner_train_n']
        assert str(train.label_end.max().date()) == a['train_label_end']
        assert str(inner.label_end.max().date()) == a['inner_train_label_end']
        assert pd.Timestamp(a['validation_label_end']) < test_start
        assert pd.Timestamp(a['inner_train_label_end']) < pd.Timestamp(a['validation_start'])
        assert a['selected_parameter'] == min(a['tuning'], key=lambda t: t['validation_mse'])['parameter']
    scores = pd.read_csv(run/'outputs/prediction_errors.csv', parse_dates=['date'])
    for a in audits:
        rows = scores.loc[scores.strategy.eq(a['strategy']) & scores.year.eq(a['year'])]
        np.testing.assert_allclose(rows.squared_error, (rows.prediction-rows.label)**2, atol=1e-12)
        np.testing.assert_allclose(rows.mean_squared_error, (a['train_mean']-rows.label)**2, atol=1e-12)

    metrics = pd.read_csv(run/'outputs/metrics.csv')
    metric_checks = 0
    for scenario in metrics.scenario.unique():
        book = pd.read_csv(run/f'outputs/ledgers_{scenario}.csv.gz', parse_dates=['date'])
        for row in metrics.loc[metrics.scenario.eq(scenario)].itertuples(index=False):
            d = book.loc[book.strategy.eq(row.strategy)].set_index('date')
            if row.period == '2019-2022':
                d = d.loc[d.index.year <= 2022]
            elif row.period == '2023-2026':
                d = d.loc[d.index.year >= 2023]
            elif row.period != 'full':
                d = d.loc[d.index.year == int(row.period)]
            r = d.ret.to_numpy()
            wealth = np.cumprod(1+r)
            peak = np.maximum.accumulate(np.r_[1., wealth])[1:]
            expected = [np.expm1(np.log1p(r).sum()*245/len(r)),
                        np.mean(r)/np.std(r, ddof=1)*np.sqrt(245), min(wealth/peak-1),
                        np.mean(d.turnover)*245, np.mean(d.cost_return)*245,
                        np.mean(np.abs(d.decision_signal))]
            np.testing.assert_allclose([row.cagr, row.sharpe, row.maxdd, row.turnover_ann,
                                        row.cost_ann, row.exposure], expected, atol=1e-10, rtol=1e-10)
            metric_checks += 1
    return {'status': 'passed', 'run': run.name,
            'source_manifest': artifact_record(run/'manifest.json', ROOT),
            'verifier': artifact_record(Path(__file__).resolve(), ROOT),
            'artifact_hashes_checked': len(manifest['artifacts']),
            'independent_feature_rows': len(x), 'independent_feature_columns': len(x.columns),
            'independent_label_calendar_rows': len(labels), 'training_boundaries_checked': len(audits),
            'independent_target_columns': len(targets.columns), 'outer_prediction_days': len(pred),
            'independent_metric_rows': metric_checks,
            'all_future_leakage_assertions_passed': True,
            'interpretation': 'Checks cover the stated timing and numerical invariants; not historical PIT proof or statistical validation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT/'backtest/output/runs/20261026-ew-ml-r1')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('verification outputs are append-only; choose a new path')
    result = verify(args.run)
    with args.output.open('x') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
