"""Synthetic-only minute-feature rehearsal. No market input option, model fit, or returns."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.minute_ml import (session_grid, build_day_features, align_feature_groups,
                                sizing_with_fallback, past_control_with_fallback, delivery_readiness)
from backtest.run_manifest import create_run_dir, artifact_record, write_manifest

ROOT = Path(__file__).resolve().parents[1]
PREP_FILES = [
    'backtest/minute_ml.py', 'backtest/prepare_minute_ml.py', 'tests/test_minute_ml.py',
    'docs/plans/2026-10-26-minute-ml-preparation.md', 'examples/minute_ml/delivery.template.json',
    'docs/requests/2026-10-26-stock-minute-ml-preparation/request-draft.md',
    'backtest/run_manifest.py',
]


def synthetic_inputs(date, scenario):
    """Fictional symbols and prices, intentionally unrelated to real securities."""
    grid = session_grid(date)
    paths = {'SYNTH_A': [100.]*42+[110.]*6, 'SYNTH_B': [110.]*42+[100.]*6,
             'SYNTH_C': [100.]*48, 'SYNTH_D': [90.]*48}
    bars, members = [], []
    for n, (symbol, closes) in enumerate(paths.items()):
        previous = 100.
        for end, close in zip(grid, closes):
            bars.append({'symbol': symbol, 'bar_end': end, 'open': previous,
                         'high': max(previous, close), 'low': min(previous, close), 'close': close,
                         'volume': 100., 'amount': 100*(previous+close)/2,
                         'quality_ok': True, 'available_at': end+pd.Timedelta(minutes=1)})
            previous = close
        members.append({'session_date': date, 'pool': '500' if n < 2 else '1000', 'symbol': symbol,
                        'effective_date': date-pd.Timedelta(days=1),
                        'known_at': (date-pd.Timedelta(days=1)).tz_localize('Asia/Shanghai'),
                        'eligible': True, 'exclusion_reason': ''})
    bars = pd.DataFrame(bars)
    if scenario == 'missing_bar':
        bars = bars.iloc[1:]
    elif scenario == 'late_delivery':
        bars.loc[0, 'available_at'] = date.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=21)
    elif scenario == 'quality_mask':
        bars.loc[0, 'quality_ok'] = False
    elif scenario != 'complete':
        raise ValueError('unknown synthetic scenario')
    return bars, pd.DataFrame(members)


def rehearse(run):
    for name in PREP_FILES:
        shutil.copyfile(ROOT/name, run/'inputs'/('snapshot__'+name.replace('/', '__')))
    dates = pd.bdate_range('2020-01-02', periods=5, name='date')
    scenarios = ['complete', 'missing_bar', 'late_delivery', 'quality_mask', 'complete']
    rows, audits, coverage, all_bars, all_members, validity = [], [], [], [], [], []
    for date, scenario in zip(dates, scenarios):
        bars, members = synthetic_inputs(date, scenario)
        result = build_day_features(date, bars, members, {'500': 2, '1000': 2})
        rows.append(result.features)
        audits.append(result.stock_audit.assign(scenario=scenario))
        coverage.append(result.coverage.assign(scenario=scenario))
        all_bars.append(bars)
        all_members.append(members)
        validity.append(result.valid)
    minute = pd.DataFrame(rows, index=dates)
    valid = pd.Series(validity, index=dates, name='valid')
    # Shape-only placeholders: these are not reconstructed historical EW features.
    daily = pd.DataFrame({f'synthetic_daily_{i:02d}': np.arange(5)/100+i/100
                          for i in range(14)}, index=dates)
    groups = align_feature_groups(daily, minute, valid)
    # Illustrative scores, not model predictions. No fitting occurs in this command.
    scores = pd.DataFrame({'daily_placeholder': [.2, np.nan, np.nan, np.nan, .1],
                           'augmented_placeholder': [.4, np.nan, np.nan, np.nan, -.2]}, index=dates)
    direction = pd.Series([1., 1., -1., -1., 1.], index=dates)
    targets = pd.DataFrame({'synthetic_ew': direction})
    for name in scores:
        targets[name] = sizing_with_fallback(scores[name], direction, valid)
        targets['past_'+name] = past_control_with_fallback(targets[name], direction, valid)
        np.testing.assert_array_equal(targets.loc[~valid, name], direction.loc[~valid])
        np.testing.assert_array_equal(targets.loc[~valid, 'past_'+name], direction.loc[~valid])
    assert validity == [True, False, False, False, True]
    assert len(groups.training_rows) == 2
    pd.concat(all_bars, ignore_index=True).to_csv(run/'inputs/SYNTHETIC_bars.csv', index=False)
    pd.concat(all_members, ignore_index=True).to_csv(run/'inputs/SYNTHETIC_memberships.csv', index=False)
    pd.DataFrame({'date': dates, 'scenario': scenarios, 'valid': validity}).to_csv(
        run/'outputs/SYNTHETIC_calendar.csv', index=False)
    minute.to_csv(run/'outputs/SYNTHETIC_minute_features.csv')
    groups.daily.to_csv(run/'outputs/SYNTHETIC_daily_group.csv')
    groups.combined.to_csv(run/'outputs/SYNTHETIC_augmented_group.csv')
    pd.concat(audits, ignore_index=True).to_csv(run/'outputs/SYNTHETIC_stock_audit.csv', index=False)
    pd.concat(coverage, ignore_index=True).to_csv(run/'outputs/SYNTHETIC_coverage.csv', index=False)
    scores.to_csv(run/'outputs/SYNTHETIC_illustrative_scores.csv')
    targets.to_csv(run/'outputs/SYNTHETIC_targets.csv')
    pending = delivery_readiness(json.loads((ROOT/'examples/minute_ml/delivery.template.json').read_text()))
    assert not pending['ready']
    summary = {'mode': 'synthetic', 'research_stage': 'preparation_only',
               'real_minute_data_read': False, 'real_models_fitted': 0, 'returns_calculated': False,
               'synthetic_sessions': 5, 'synthetic_stock_days': 20,
               'valid_sessions': 2, 'fallback_sessions': 3,
               'minute_features': len(minute.columns),
               'daily_feature_count': groups.daily.shape[1], 'augmented_feature_count': groups.combined.shape[1],
               'complete_calendar_preserved': True, 'symmetric_fallback_checked': True,
               'delivery_template_readiness': pending,
               'next_step': 'Wait for cleaning delivery; validate real input versions and canonical interval semantics before research.'}
    (run/'outputs/verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    (run/'outputs/README.md').write_text(
        '# 合成演练，非行情或策略业绩\n\n'
        '5个虚构交易日、4只SYNTH股票；包含完整、缺格、迟到和质量屏蔽场景。\n'
        '只验证16列分钟结构、14/30列输入形状、完整日历与同可用性回退。\n'
        'illustrative_scores为手写占位分数，没有训练模型，也没有计算收益。\n'
        '正式研究尚未开始，等待清洗完成后的真实数据交付与验收。\n')
    for name in PREP_FILES:
        frozen = run/'inputs'/('snapshot__'+name.replace('/', '__'))
        assert artifact_record(frozen, run)['sha256'] == artifact_record(ROOT/name, ROOT)['sha256']
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', default='20261026-minute-ml-preparation-r1')
    args = parser.parse_args()
    run = create_run_dir(ROOT/'backtest/output/runs', args.run_id)
    metadata = {'run_id': args.run_id, 'mode': 'synthetic', 'research_stage': 'preparation_only',
                'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
    write_manifest(run, metadata)
    try:
        summary = rehearse(run)
        metadata.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat(),
                        artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*'))
                                   if p.is_file() and p.name != 'manifest.json'])
        write_manifest(run, metadata)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as exc:
        metadata.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        write_manifest(run, metadata)
        raise


if __name__ == '__main__':
    main()
