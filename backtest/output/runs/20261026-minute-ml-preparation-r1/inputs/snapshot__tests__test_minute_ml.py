"""Synthetic-only behavioral checks; no market data or database fixtures."""
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.minute_ml import (
    FEATURE_COLUMNS, session_grid, build_day_features, delivery_readiness,
    align_feature_groups, sizing_with_fallback, past_control_with_fallback,
)


def synthetic_day(date='2020-01-02'):
    """Four fictional stocks with hand-computable intraday paths."""
    grid = session_grid(date)
    paths = {'SYNTH_A': [100.]*42+[110.]*6,
             'SYNTH_B': [110.]*42+[100.]*6,
             'SYNTH_C': [100.]*48,
             'SYNTH_D': [90.]*48}
    rows, members = [], []
    for n, (symbol, closes) in enumerate(paths.items()):
        previous = 100.
        for end, close in zip(grid, closes):
            rows.append({'symbol': symbol, 'bar_end': end, 'open': previous,
                         'high': max(previous, close), 'low': min(previous, close),
                         'close': close, 'volume': 100., 'amount': 100*(previous+close)/2,
                         'quality_ok': True, 'available_at': end+pd.Timedelta(minutes=1)})
            previous = close
        day = pd.Timestamp(date)
        members.append({'session_date': day, 'pool': '500' if n < 2 else '1000',
                        'symbol': symbol, 'effective_date': day-pd.Timedelta(days=1),
                        'known_at': (day-pd.Timedelta(days=1)).tz_localize('Asia/Shanghai'),
                        'eligible': True, 'exclusion_reason': ''})
    return pd.DataFrame(rows), pd.DataFrame(members)


def test_grid_and_features_have_hand_calculated_values():
    grid = session_grid('2020-01-02')
    assert len(grid) == 48 and str(grid.tz) == 'Asia/Shanghai'
    assert [grid[i].strftime('%H:%M') for i in [0, 23, 24, 47]] == ['09:35', '11:30', '13:05', '15:00']
    bars, members = synthetic_day()
    result = build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    assert result.valid and len(result.features) == 16
    expected = [.5, .5, .5, np.log(1.1)/2, .5, 1/3, 1/8, .5,
                0., 0., 0., abs(np.log(.9))/2, -.5, 1., 1/8, .25]
    np.testing.assert_allclose(result.features, expected, atol=1e-12)
    assert result.coverage.coverage.eq(1).all()


def test_missing_bar_does_not_shrink_membership_denominator_or_fill_zero():
    bars, members = synthetic_day()
    bars = bars.drop(index=0)
    result = build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    assert not result.valid and result.features.isna().all()
    assert result.coverage.set_index('pool').loc['500', 'coverage'] == .5
    assert result.stock_audit.set_index('symbol').loc['SYNTH_A', 'reason'] == 'incomplete_grid'
    with pytest.raises(ValueError, match='member count'):
        build_day_features('2020-01-02', bars, members.iloc[1:], {'500': 2, '1000': 2})


@pytest.mark.parametrize('change, reason', [
    ('duplicate', 'duplicate'), ('naive', 'timezone'), ('other_date', 'session'),
    ('lunch', 'grid'), ('unknown_symbol', 'membership'),
])
def test_ambiguous_or_inconsistent_bar_identity_is_rejected(change, reason):
    bars, members = synthetic_day()
    if change == 'duplicate':
        bars = pd.concat([bars, bars.iloc[:1]])
    elif change == 'naive':
        bars['bar_end'] = bars.bar_end.dt.tz_localize(None)
    elif change == 'other_date':
        bars.loc[0, 'bar_end'] += pd.Timedelta(days=1)
    elif change == 'lunch':
        bars.loc[0, 'bar_end'] = pd.Timestamp('2020-01-02 12:00', tz='Asia/Shanghai')
    else:
        bars.loc[0, 'symbol'] = 'NOT_A_MEMBER'
    with pytest.raises(ValueError, match=reason):
        build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})


@pytest.mark.parametrize('change, reason', [
    ('quality', 'quality_mask'), ('late', 'not_available'), ('ohlc', 'invalid_ohlc'),
    ('negative_volume', 'invalid_volume_amount'), ('amount', 'vwap_incompatible'),
    ('all_zero', 'zero_day_volume'), ('opening_zero', 'opening_unobserved'),
])
def test_bad_stock_days_are_masked_without_modifying_input(change, reason):
    bars, members = synthetic_day()
    if change == 'quality':
        bars.loc[0, 'quality_ok'] = False
    elif change == 'late':
        bars.loc[0, 'available_at'] = pd.Timestamp('2020-01-02 20:31', tz='Asia/Shanghai')
    elif change == 'ohlc':
        bars.loc[0, 'high'] = 1
    elif change == 'negative_volume':
        bars.loc[0, 'volume'] = -1
    elif change == 'amount':
        bars.loc[0, 'amount'] *= 2
    elif change == 'all_zero':
        bars.loc[bars.symbol.eq('SYNTH_A'), ['volume', 'amount']] = 0.
    else:
        bars.loc[0, ['volume', 'amount']] = 0.
    original = bars.copy(deep=True)
    result = build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    assert not result.valid
    assert result.stock_audit.set_index('symbol').loc['SYNTH_A', 'reason'] == reason
    pd.testing.assert_frame_equal(bars, original)


def test_zero_trade_bar_is_retained_only_with_consistent_carried_price():
    bars, members = synthetic_day()
    bars.loc[5, ['volume', 'amount']] = 0.
    result = build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    assert result.valid
    bars.loc[5, 'high'] = 101.
    result = build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    assert not result.valid
    assert result.stock_audit.set_index('symbol').loc['SYNTH_A', 'reason'] == 'zero_volume_price'


def test_membership_must_be_historical_and_exclusions_explicit():
    bars, members = synthetic_day()
    members.loc[0, 'known_at'] = pd.Timestamp('2020-01-03', tz='Asia/Shanghai')
    with pytest.raises(ValueError, match='known'):
        build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    bars, members = synthetic_day()
    members.loc[0, 'effective_date'] = pd.Timestamp('2020-01-03')
    with pytest.raises(ValueError, match='effective'):
        build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    bars, members = synthetic_day()
    members.loc[0, ['eligible', 'exclusion_reason']] = [False, 'missing_bars']
    with pytest.raises(ValueError, match='exclusion'):
        build_day_features('2020-01-02', bars, members, {'500': 2, '1000': 2})
    members.loc[0, 'exclusion_reason'] = 'suspended'
    result = build_day_features('2020-01-02', bars[bars.symbol.ne('SYNTH_A')], members,
                                {'500': 2, '1000': 2})
    assert result.valid
    assert result.coverage.set_index('pool').loc['500', 'eligible'] == 1


def test_quality_threshold_is_inclusive_at_95_percent():
    bars, members = synthetic_day()
    expanded_bars, expanded_members = [], []
    for pool, symbol in [('500', 'SYNTH_A'), ('1000', 'SYNTH_C')]:
        for n in range(20):
            name = f'{symbol}_{n}'
            expanded_members.append(members.loc[members.symbol.eq(symbol)].assign(symbol=name))
            sample = bars.loc[bars.symbol.eq(symbol)].assign(symbol=name)
            if n == 0:
                sample = sample.iloc[1:]
            expanded_bars.append(sample)
    result = build_day_features('2020-01-02', pd.concat(expanded_bars),
                                pd.concat(expanded_members), {'500': 20, '1000': 20})
    assert result.valid and result.coverage.coverage.eq(.95).all()


def test_alignment_preserves_complete_calendar_and_symmetric_fallback():
    idx = pd.bdate_range('2020-01-01', periods=7)
    daily = pd.DataFrame({'daily_a': np.arange(7)}, index=idx)
    minute = pd.DataFrame(1., index=idx, columns=FEATURE_COLUMNS)
    valid = pd.Series([True, True, False, True, True, True, True], index=idx)
    minute.loc[idx[2]] = np.nan
    groups = align_feature_groups(daily, minute, valid)
    assert groups.daily.index.equals(idx) and groups.combined.index.equals(idx)
    assert groups.training_rows.equals(idx.delete(2))
    direction = pd.Series([1., 1., -1., -1., 1., 1., 1.], index=idx)
    pred = pd.Series([1., 1., np.nan, 1., 1., 1., 1.], index=idx)
    target = sizing_with_fallback(pred, direction, valid)
    assert target.iloc[2] == -1
    high = .75+.25*np.tanh(1.)
    assert target.iloc[3] == pytest.approx(-(high*3+.75)/4)
    changed = pred.copy()
    changed.iloc[-1] = -100
    pd.testing.assert_series_equal(target.iloc[:-1], sizing_with_fallback(changed, direction, valid).iloc[:-1])
    with pytest.raises(ValueError, match='calendar'):
        align_feature_groups(daily, minute.iloc[1:], valid)
    with pytest.raises(ValueError, match='finite'):
        align_feature_groups(daily, minute, pd.Series(True, index=idx))


def test_past_control_matches_fallback_and_ignores_invalid_days_in_estimation():
    idx = pd.bdate_range('2020-01-01', periods=5)
    target = pd.Series([.6, 1., .8, -.9, -1.], index=idx)
    direction = pd.Series([1., 1., 1., -1., -1.], index=idx)
    valid = pd.Series([True, False, True, True, False], index=idx)
    result = past_control_with_fallback(target, direction, valid)
    np.testing.assert_allclose(result, [.75, 1., .6, -.75, -1.])


def test_delivery_template_cannot_be_mistaken_for_ready_data():
    path = Path(__file__).resolve().parents[1]/'examples/minute_ml/delivery.template.json'
    template = json.loads(path.read_text())
    assert not delivery_readiness(template)['ready']
    ready = copy.deepcopy(template)
    ready.update(state='ready', cleaning_complete=True, dataset_version='synthetic-v1',
                 cleaning_receipt='synthetic-receipt', normalization_evidence='synthetic-mapping',
                 membership_source='synthetic-members', quality_mask_version='synthetic-quality',
                 start_date='2020-01-01', end_date='2020-01-31', availability_basis='documented_assumption')
    ready['files'] = [{'role': role, 'path': role+'.csv', 'sha256': 'a'*64, 'size_bytes': 1, 'rows': 1}
                      for role in ['bars', 'memberships', 'calendar', 'quality_exclusions']]
    report = delivery_readiness(ready)
    assert report['ready'] and report['warnings']
    ready['files'][0]['path'] = '../escape.csv'
    assert not delivery_readiness(ready)['ready']
    ready['files'][0]['path'] = 'bars.csv'
    ready['cleaning_complete'] = False
    assert not delivery_readiness(ready)['ready']
