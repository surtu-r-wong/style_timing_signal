"""Run the frozen EW ML pilot from local immutable inputs, never from a database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil

import numpy as np
import pandas as pd
import sklearn

from backtest.data import _expiry_from_symbol
from backtest.ew_ml import (OI_COLUMNS, build_features, make_labels, fit_predict,
                            position_scale, past_same_side_scale)
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers
from backtest.metrics import sharpe, max_drawdown
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest, git_state

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'backtest/output/runs/20260917-ew-oi-research-r2'
SPEC = 'docs/plans/2026-10-26-ew-ml-pilot-prereg.md'
SOURCES = {
    'equal_weight.csv': 'inputs/equal_weight.csv',
    'futures.csv': 'inputs/futures.csv',
    'oi_features.csv': 'inputs/office/features.csv',
    'index.csv': 'inputs/office/raw_inputs/index.csv',
    'calendar.csv': 'inputs/office/calendar.csv',
}
CODE = ['backtest/ew_ml.py', 'backtest/run_ew_ml.py', 'tests/test_ew_ml.py',
        'backtest/execution_ledger.py', 'backtest/intraday_flow_pilot.py',
        'backtest/data.py', 'backtest/metrics.py', 'backtest/run_manifest.py',
        'requirements-ml-research.txt']
SCENARIOS = [('close_3bps', 3., 0), ('close_10bps', 10., 0), ('second_close_3bps', 3., 1)]
MODELS = ['ridge_core', 'tree_core', 'ridge_oi', 'tree_oi']


def stamp():
    return datetime.now(timezone.utc).isoformat()


def dump(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def freeze(run):
    original = json.loads((SOURCE/'manifest.json').read_text())
    if original.get('status') != 'complete':
        raise ValueError('source run is not complete')
    records = {r['path']: r for r in original['artifacts']}
    receipts = []
    for target, rel in SOURCES.items():
        actual = artifact_record(SOURCE/rel, SOURCE)
        if actual != records[rel]:
            raise ValueError(f'source hash mismatch: {rel}')
        shutil.copyfile(SOURCE/rel, run/'inputs'/target)
        receipts.append(dict(source_run=SOURCE.name, destination=target, **actual))
    shutil.copyfile(SOURCE/'manifest.json', run/'inputs/source_manifest.json')
    shutil.copyfile(ROOT/SPEC, run/'inputs/prereg.md')
    for rel in CODE:
        shutil.copyfile(ROOT/rel, run/'inputs'/('code__'+rel.replace('/', '__')))
    dump(run/'inputs/source_receipts.json', receipts)
    frozen = {'frozen_at': stamp(), 'spec': artifact_record(run/'inputs/prereg.md', run),
              'inputs': [artifact_record(p, run) for p in sorted((run/'inputs').iterdir())],
              'git': git_state(ROOT),
              'runtime': {'python': platform.python_version(), 'numpy': np.__version__,
                          'pandas': pd.__version__, 'sklearn': sklearn.__version__},
              'protocol': '2019-2026 annual expanding fits; purged inner 245-day validation',
              'historical_reuse': True, 'production_modified': False}
    dump(run/'inputs/freeze.json', frozen)
    return frozen


def load_inputs(run):
    ew = pd.read_csv(run/'inputs/equal_weight.csv', index_col='date', parse_dates=True)
    futures = pd.read_csv(run/'inputs/futures.csv', parse_dates=['date'])
    oi = pd.read_csv(run/'inputs/oi_features.csv', parse_dates=['date'])
    oi = oi.loc[oi.source.eq('IC')].set_index('date')
    prices = pd.read_csv(run/'inputs/index.csv', parse_dates=['date'])
    price = prices.loc[prices.index_code.eq('000905.SH')].set_index('date').close.sort_index()
    cal = pd.read_csv(run/'inputs/calendar.csv', parse_dates=['date'])
    calendar = pd.DatetimeIndex(cal.loc[cal.sfe.eq(1), 'date'], name='date')
    if not calendar.is_unique or not calendar.is_monotonic_increasing:
        raise ValueError('invalid office calendar')
    idx = calendar[(calendar >= futures.date.min()) & (calendar <= '2026-09-16')]
    if idx[-1] != pd.Timestamp('2026-09-16') or futures.date.max() != idx[-1]:
        raise ValueError('unexpected endpoint')
    if not ew.index.is_unique or not price.index.is_unique or not oi.index.is_unique:
        raise ValueError('duplicate input date')
    if ew.reindex(idx).isna().any().any() or price.reindex(idx).isna().any():
        raise ValueError('missing required prices or incumbent')
    price_calendar = calendar[(calendar >= price.index.min()) & (calendar <= idx[-1])]
    if price.reindex(price_calendar).isna().any():
        raise ValueError('price warmup has a calendar gap')
    x = build_features(ew, price.reindex(price_calendar), oi).reindex(idx)
    valid = np.isfinite(x.to_numpy()).all(axis=1)
    first = np.flatnonzero(valid)[0]
    if not valid[first:].all():
        raise ValueError('internal feature gap; do not silently drop trading days')
    x = x.iloc[first:]
    expiries = {}
    for symbol in futures.symbol.unique():
        nominal = pd.Timestamp(_expiry_from_symbol(symbol))
        following = calendar[calendar >= nominal]
        expiries[symbol] = following[0] if len(following) else nominal
    first_im = futures.loc[futures.symbol.str.startswith('IM'), 'date'].min()
    weights = futures_weights(idx, first_im)
    market = prepare_close_market(futures, idx, weights, expiries)
    unit = batch_close_ledgers(market, pd.DataFrame({'long': 1.}, index=idx), cost_bps=0.)['long'].ret
    labels = make_labels(unit, np.sign(ew.factor_value.reindex(idx)), x.rv20.reindex(idx))
    x.to_csv(run/'outputs/features.csv', index_label='date')
    labels.to_csv(run/'outputs/labels.csv', index_label='date')
    dump(run/'outputs/input_acceptance.json', {
        'source_hashes_passed': True, 'features': list(x.columns), 'feature_rows': len(x),
        'feature_start': str(x.index[0].date()), 'end': str(idx[-1].date()),
        'execution_rows': len(futures), 'execution_calendar_rows': len(idx),
        'oi_lag_sessions': 1, 'historical_publication_times_proven': False,
        'internal_feature_gaps': 0,
    })
    return x, labels, futures, expiries, first_im


def walk_forward(x, labels, run):
    predictions, audits, score_rows = {}, [], []
    for name in MODELS:
        family, group = name.split('_')
        features = x if group == 'oi' else x.drop(columns=OI_COLUMNS)
        chunks = []
        for year in range(2019, 2027):
            test = features.index[features.index.year == year]
            if not len(test):
                raise ValueError(f'missing outer year {year}')
            prediction, audit, _ = fit_predict(features, labels, test, family)
            audit.update(strategy=name, year=year)
            audits.append(audit)
            chunks.append(prediction)
            observed = labels.y.reindex(test)
            use = observed.notna()
            error = (prediction.loc[use]-observed.loc[use])**2
            null_error = (audit['train_mean']-observed.loc[use])**2
            score_rows += [{'strategy': name, 'date': str(date.date()), 'year': year,
                            'prediction': float(prediction.loc[date]), 'label': float(observed.loc[date]),
                            'squared_error': float(error.loc[date]), 'mean_squared_error': float(null_error.loc[date])}
                           for date in test[use]]
            print(f'{name} {year}: train={audit["train_n"]} test={len(test)} parameter={audit["selected_parameter"]}', flush=True)
        predictions[name] = pd.concat(chunks)
    pred = pd.DataFrame(predictions)
    if not pred.index.equals(x.index[x.index >= '2019-01-01']) or pred.isna().any().any():
        raise ValueError('outer predictions are not a complete calendar')
    pred.to_csv(run/'outputs/predictions.csv', index_label='date')
    dump(run/'outputs/training_audit.json', audits)
    scores = pd.DataFrame(score_rows)
    scores.to_csv(run/'outputs/prediction_errors.csv', index=False)
    scoring = scores.groupby(['strategy', 'year'])[['squared_error', 'mean_squared_error']].mean()
    scoring['r2_vs_training_mean'] = 1-scoring.squared_error/scoring.mean_squared_error
    scoring.to_csv(run/'outputs/prediction_metrics_yearly.csv')
    return pred


def describe(ledger):
    r = ledger.ret
    return {'n': len(r), 'cagr': float(np.expm1(np.log1p(r).sum()*245/len(r))),
            'sharpe': sharpe(r), 'maxdd': max_drawdown(r),
            'turnover_ann': float(ledger.turnover.mean()*245),
            'cost_ann': float(ledger.cost_return.mean()*245),
            'exposure': float(ledger.decision_signal.abs().mean()),
            'cumulative': float((1+r).prod()-1)}


def execute_ledgers(x, pred, futures, expiries, first_im, run):
    idx = pred.index
    sign = x.direction.reindex(idx)
    targets = pd.DataFrame({'ew': sign, 'fixed75': .75*sign,
                            'half_short': sign.clip(lower=0)+.5*sign.clip(upper=0),
                            'vol20': sign*(.20/x.rv20.reindex(idx)).clip(.5, 1.)})
    for name in MODELS:
        q = position_scale(pred[name])
        targets[name] = sign*q
        targets['past_'+name] = sign*past_same_side_scale(q, sign)
    targets.to_csv(run/'outputs/targets.csv', index_label='date')
    weights = futures_weights(idx, first_im)
    market = prepare_close_market(futures, idx, weights, expiries)
    windows = {'full': idx, '2019-2022': idx[idx.year <= 2022], '2023-2026': idx[idx.year >= 2023]}
    windows.update({str(y): idx[idx.year == y] for y in sorted(set(idx.year))})
    metrics, comparisons, stress, checks, attribution = [], [], [], [], []
    books = {}
    for scenario, cost, lag in SCENARIOS:
        print(f'Executing {scenario}: {len(targets.columns)} strategies', flush=True)
        signals = targets.shift(lag, fill_value=0.)
        book = batch_close_ledgers(market, signals, cost_bps=cost)
        books[scenario] = book
        # Two model families across all three execution scenarios; existing independent engine.
        for name in ['ridge_core', 'tree_oi']:
            ref = contract_ledger(futures, signals[name], weights, expiries=expiries, cost_bps=cost)
            fields = ['ret', 'equity', 'gross_pnl', 'cost', 'turnover', 'decision_signal']
            maximum = 0.
            for field in fields:
                np.testing.assert_allclose(book[name][field], ref[field], rtol=1e-10, atol=1e-12)
                maximum = max(maximum, float((book[name][field]-ref[field]).abs().max()))
            checks.append({'scenario': scenario, 'strategy': name, 'max_error': maximum})
        for name, ledger in book.items():
            np.testing.assert_allclose(ledger.decision_signal, targets[name].shift(lag+1, fill_value=0.))
            np.testing.assert_allclose(ledger.gross_pnl-ledger.cost,
                                       ledger.equity.diff().fillna(ledger.equity.iloc[0]-1), atol=1e-12, rtol=1e-10)
            before = ledger.equity.shift(1, fill_value=1.)
            held_side = np.sign(ledger.decision_signal.shift(1, fill_value=0.))
            gross_return = ledger.gross_pnl/before
            long_gross = float(gross_return.where(held_side > 0, 0).sum())
            short_gross = float(gross_return.where(held_side < 0, 0).sum())
            costs = float(ledger.cost_return.sum())
            np.testing.assert_allclose(long_gross+short_gross-costs, ledger.ret.sum(), atol=1e-10)
            attribution.append({'scenario': scenario, 'strategy': name,
                                'long_gross_arithmetic_sum': long_gross,
                                'short_gross_arithmetic_sum': short_gross,
                                'cost_arithmetic_sum': costs, 'net_arithmetic_sum': float(ledger.ret.sum())})
            for period, dates in windows.items():
                metrics.append({'scenario': scenario, 'strategy': name, 'period': period,
                                **describe(ledger.loc[dates])})
        pd.concat(book, names=['strategy', 'date']).to_csv(run/f'outputs/ledgers_{scenario}.csv.gz')
        for name in MODELS:
            for baseline in ['ew', 'fixed75', 'half_short', 'vol20', 'past_'+name]:
                left, right = book[name], book[baseline]
                for period, dates in windows.items():
                    a, b = describe(left.loc[dates]), describe(right.loc[dates])
                    comparisons.append({'scenario': scenario, 'strategy': name, 'baseline': baseline,
                                        'period': period, 'sharpe_difference': a['sharpe']-b['sharpe'],
                                        'cagr_difference': a['cagr']-b['cagr'],
                                        'maxdd_difference': a['maxdd']-b['maxdd']})
                delta = np.log1p(left.ret)-np.log1p(right.ret)
                best = delta.nlargest(5).index
                a, b = left.ret.copy(), right.ret.copy()
                a.loc[best], b.loc[best] = 0., 0.
                stress.append({'scenario': scenario, 'strategy': name, 'baseline': baseline,
                               'best5_zero_sharpe_difference': sharpe(a)-sharpe(b),
                               'total_log_difference': float(delta.sum()),
                               'best5_log_difference': float(delta.loc[best].sum()),
                               'dates': '|'.join(str(d.date()) for d in best)})
    metrics, comparisons, stress = pd.DataFrame(metrics), pd.DataFrame(comparisons), pd.DataFrame(stress)
    metrics.to_csv(run/'outputs/metrics.csv', index=False)
    comparisons.to_csv(run/'outputs/comparisons.csv', index=False)
    stress.to_csv(run/'outputs/stress.csv', index=False)
    pd.DataFrame(attribution).to_csv(run/'outputs/leg_attribution.csv', index=False)
    dump(run/'outputs/ledger_verification.json', {'crosschecks': checks, 'ledger_count': 36,
                                                'pnl_identity': True, 'decision_lag': True,
                                                'arithmetic_leg_identity': True})
    return metrics, comparisons, stress


def economic_screen(metrics, comparisons, stress):
    result = []
    for name in MODELS:
        comp = comparisons.loc[comparisons.strategy.eq(name)]
        base = comp.loc[comp.baseline.eq('ew')].set_index(['scenario', 'period'])
        main = base.loc[('close_3bps', 'full')]
        other = comp.loc[comp.scenario.eq('close_3bps') & comp.period.eq('full') & ~comp.baseline.eq('ew')]
        s = stress.loc[stress.strategy.eq(name) & stress.baseline.eq('ew') & stress.scenario.eq('close_3bps')].iloc[0]
        checks = {
            'sharpe_gain_at_least_010': bool(main.sharpe_difference >= .10),
            'beats_simple_and_past_controls': bool(other.sharpe_difference.gt(0).all()),
            'cagr_loss_at_most_2pp': bool(main.cagr_difference >= -.02),
            'drawdown_not_worse': bool(main.maxdd_difference >= 0),
            'both_subperiods_positive': all(base.loc[('close_3bps', period)].sharpe_difference > 0
                                            for period in ['2019-2022', '2023-2026']),
            'high_cost_and_delay_positive': all(base.loc[(scenario, 'full')].sharpe_difference > 0
                                                for scenario in ['close_10bps', 'second_close_3bps']),
            'best5_zero_positive': bool(s.best5_zero_sharpe_difference > 0),
        }
        result.append({'strategy': name, 'economic_screen_passed': bool(all(checks.values())),
                       'checks': {k: bool(v) for k, v in checks.items()},
                       'statistical_go': False})
    return result


def report(run, metrics, screen):
    main = metrics.loc[metrics.scenario.eq('close_3bps') & metrics.period.eq('full')].set_index('strategy')
    lines = ['# EW机器学习首轮结果', '',
             '固定规格，复用历史上的逐年滚动预测。不是未触碰确认样本，不发统计GO。', '',
             '| 策略 | CAGR | Sharpe | 最大回撤 | 平均目标暴露 | 年换手 |',
             '|---|---:|---:|---:|---:|---:|']
    for name in ['ew', 'fixed75', 'half_short', 'vol20']+MODELS:
        row = main.loc[name]
        lines.append(f'| {name} | {row.cagr:.2%} | {row.sharpe:.3f} | {row.maxdd:.2%} | {row.exposure:.3f} | {row.turnover_ann:.2f} |')
    lines += ['', '经济筛选：']
    for verdict in screen:
        failed = [k for k, value in verdict['checks'].items() if not value]
        lines.append(f'- {verdict["strategy"]}: '+('通过，仅值得进一步观察' if not failed else '未通过：'+', '.join(failed)))
    lines += ['', '完整逐年、分段、成本、延迟、历史仓位对照和最好五日压力分别见metrics/comparisons/stress.csv。',
              'leg_attribution为逐日收益算术归因，多空毛收益减费用相加等于日收益之和，不是可相加的CAGR。',
              'OI固定额外滞后一天；历史发布时间及修订未独立证明。实际合约回测为小数名义仓位，',
              '未建模保证金、整数手、涨跌停成交和市场冲击，不能当实际账户收益。',
              '未修改生产、共享库、自动调度或既有前瞻协议。']
    (run/'outputs/REPORT.md').write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', default='20261026-ew-ml-r1')
    args = parser.parse_args()
    run = create_run_dir(ROOT/'backtest/output/runs', args.run_id)
    metadata = {'run_id': args.run_id, 'status': 'running', 'started_at': stamp()}
    write_manifest(run, metadata)
    try:
        frozen = freeze(run)
        metadata['frozen_at'] = frozen['frozen_at']
        write_manifest(run, metadata)
        x, labels, futures, expiries, first_im = load_inputs(run)
        predictions = walk_forward(x, labels, run)
        metrics, comparisons, stress = execute_ledgers(x, predictions, futures, expiries, first_im, run)
        screen = economic_screen(metrics, comparisons, stress)
        dump(run/'outputs/economic_screen.json', screen)
        report(run, metrics, screen)
        # Catch accidental changes during execution, including all captured dependencies.
        for item in frozen['inputs']:
            if artifact_record(run/item['path'], run) != item:
                raise ValueError('frozen artifact changed during execution')
        for rel in CODE:
            frozen_file = run/'inputs'/('code__'+rel.replace('/', '__'))
            if artifact_record(ROOT/rel, ROOT)['sha256'] != artifact_record(frozen_file, run)['sha256']:
                raise ValueError(f'code changed during execution: {rel}')
        metadata.update(status='complete', finished_at=stamp(),
                        artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*'))
                                   if p.is_file() and p.name != 'manifest.json'])
        write_manifest(run, metadata)
        print((run/'outputs/REPORT.md').read_text(), flush=True)
    except Exception as exc:
        metadata.update(status='failed', finished_at=stamp(), error=f'{type(exc).__name__}: {exc}')
        write_manifest(run, metadata)
        raise


if __name__ == '__main__':
    main()
