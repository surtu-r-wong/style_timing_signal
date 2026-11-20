"""Pure preparation helpers for canonical 5-minute inputs; no data access or training."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

import numpy as np
import pandas as pd

TIMEZONE = 'Asia/Shanghai'
POOLS = ('500', '1000')
MIN_COVERAGE = .95
MEASURES = ('up_breadth', 'late_up_breadth', 'early_up_late_down', 'return_dispersion',
            'path_efficiency', 'downside_variance_share', 'late_volume_share', 'close_location')
FEATURE_COLUMNS = [f'm{pool}_{name}' for pool in POOLS for name in MEASURES]
BAR_FIELDS = {'symbol', 'bar_end', 'open', 'high', 'low', 'close', 'volume', 'amount',
              'quality_ok', 'available_at'}
MEMBER_FIELDS = {'session_date', 'pool', 'symbol', 'effective_date', 'known_at',
                 'eligible', 'exclusion_reason'}


@dataclass
class DayFeatures:
    features: pd.Series
    valid: bool
    coverage: pd.DataFrame
    stock_audit: pd.DataFrame


@dataclass
class FeatureGroups:
    daily: pd.DataFrame
    combined: pd.DataFrame
    training_rows: pd.DatetimeIndex
    valid: pd.Series


def _date(value):
    date = pd.Timestamp(value)
    if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
        raise ValueError('session date must be a timezone-free calendar date')
    return date


def _times(values, name):
    converted = []
    for value in values:
        time = pd.Timestamp(value)
        if pd.isna(time) or time.tzinfo is None:
            raise ValueError(f'{name} requires explicit timezone and nonmissing timestamps')
        converted.append(time.tz_convert(TIMEZONE))
    return pd.DatetimeIndex(converted, tz=TIMEZONE)


def _booleans(values, name):
    if not all(isinstance(v, (bool, np.bool_)) for v in values):
        raise ValueError(f'{name} must contain explicit booleans')


def session_grid(session_date):
    date = _date(session_date).tz_localize(TIMEZONE)
    morning = pd.date_range(date+pd.Timedelta(hours=9, minutes=35), periods=24, freq='5min')
    afternoon = pd.date_range(date+pd.Timedelta(hours=13, minutes=5), periods=24, freq='5min')
    return morning.append(afternoon)


def _stock_summary(stock, grid, cutoff):
    if len(stock) != 48 or not pd.DatetimeIndex(stock.bar_end).equals(grid):
        return None, 'incomplete_grid'
    if not stock.quality_ok.all():
        return None, 'quality_mask'
    if (stock.available_at < stock.bar_end).any() or (stock.available_at > cutoff).any():
        return None, 'not_available'
    values = stock[['open', 'high', 'low', 'close', 'volume', 'amount']].apply(
        pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    if not np.isfinite(values).all():
        return None, 'nonfinite_values'
    open_, high, low, close, volume, amount = values.T
    if (np.any(values[:, :4] <= 0) or np.any(high < low) or
            np.any(high < np.maximum(open_, close)) or np.any(low > np.minimum(open_, close))):
        return None, 'invalid_ohlc'
    if np.any(volume < 0) or np.any(amount < 0) or np.any((volume == 0) != (amount == 0)):
        return None, 'invalid_volume_amount'
    if volume.sum() == 0:
        return None, 'zero_day_volume'
    if volume[0] == 0:
        return None, 'opening_unobserved'
    no_trade = volume == 0
    previous = np.r_[open_[0], close[:-1]]
    if no_trade.any() and not np.allclose(values[no_trade, :4], previous[no_trade, None], atol=1e-10, rtol=0):
        return None, 'zero_volume_price'
    active = ~no_trade
    vwap = amount[active]/volume[active]
    if np.any(vwap < low[active]-.010000001) or np.any(vwap > high[active]+.010000001):
        return None, 'vwap_incompatible'
    returns = np.log(close/previous)
    # Endpoint ratios preserve exact flat-day signs; summed logs can leave a tiny
    # positive residual after a round trip and falsely count an advancing stock.
    total = float(np.log(close[-1]/open_[0]))
    travel = float(np.abs(returns).sum())
    span = float(high.max()-low.min())
    return {
        'day': total, 'early': float(np.log(close[5]/open_[0])),
        'late': float(np.log(close[-1]/close[-7])),
        'efficiency': total/travel if travel else 0.,
        'down_var': float(np.square(np.minimum(returns, 0)).sum()),
        'total_var': float(np.square(returns).sum()),
        'late_volume': float(volume[-6:].sum()/volume.sum()),
        'close_location': float((close[-1]-low.min())/span) if span else .5,
    }, 'ok'


def build_day_features(session_date, bars, members, expected_member_counts):
    """Validate one supplied day, retain missing-member denominators, aggregate 16 features.

    Inputs must already have explicit canonical interval semantics. This function does
    not infer a vendor timestamp convention, retrieve data, or repair any source row.
    """
    date = _date(session_date)
    grid = session_grid(date)
    cutoff = date.tz_localize(TIMEZONE)+pd.Timedelta(hours=20, minutes=30)
    if not BAR_FIELDS.issubset(bars.columns) or not MEMBER_FIELDS.issubset(members.columns):
        raise ValueError('missing input contract columns')
    if not bars.columns.is_unique or not members.columns.is_unique:
        raise ValueError('duplicate input column')
    b, m = bars.copy(), members.copy()
    _booleans(b.quality_ok, 'quality_ok')
    _booleans(m.eligible, 'eligible')
    if m.empty or m[['pool', 'symbol']].isna().any().any():
        raise ValueError('missing membership identity')
    if not all(isinstance(v, str) and v for v in m.symbol):
        raise ValueError('invalid membership symbol')
    if m.duplicated(['pool', 'symbol']).any() or m.symbol.duplicated().any():
        raise ValueError('duplicate or overlapping membership')
    if set(m.pool) != set(POOLS) or set(expected_member_counts) != set(POOLS):
        raise ValueError('membership pools must be 500 and 1000')
    for pool in POOLS:
        count = expected_member_counts[pool]
        if (isinstance(count, bool) or not isinstance(count, (int, np.integer)) or count <= 0
                or int(m.pool.eq(pool).sum()) != count):
            raise ValueError(f'independent member count mismatch: {pool}')
    if any(_date(d) != date for d in m.session_date):
        raise ValueError('membership session mismatch')
    if any(_date(d) > date for d in m.effective_date):
        raise ValueError('membership effective date is in the future')
    if (_times(m.known_at, 'membership known_at') > cutoff).any():
        raise ValueError('membership was not known by the decision cutoff')
    reasons = m.exclusion_reason.fillna('')
    if (m.eligible & reasons.ne('')).any() or (~m.eligible & ~reasons.isin(['suspended', 'not_yet_listed'])).any():
        raise ValueError('invalid eligibility exclusion reason')
    b['bar_end'] = _times(b.bar_end, 'bar_end')
    b['available_at'] = _times(b.available_at, 'available_at')
    if b.duplicated(['symbol', 'bar_end']).any():
        raise ValueError('duplicate bar identity')
    if not b.symbol.isin(m.symbol).all():
        raise ValueError('bar symbol outside supplied membership')
    if not all(t.date() == date.date() for t in b.bar_end):
        raise ValueError('bar from a different session')
    if not b.bar_end.isin(grid).all():
        raise ValueError('bar timestamp outside canonical grid')
    by_stock = {symbol: group.sort_values('bar_end') for symbol, group in b.groupby('symbol')}
    summaries = {pool: [] for pool in POOLS}
    audit, coverage = [], []
    for member in m.itertuples(index=False):
        if not member.eligible:
            audit.append({'date': date, 'pool': member.pool, 'symbol': member.symbol,
                          'eligible': False, 'valid': False, 'reason': member.exclusion_reason})
            continue
        stock = by_stock.get(member.symbol, b.iloc[:0])
        summary, reason = _stock_summary(stock, grid, cutoff)
        if summary is not None:
            summaries[member.pool].append(summary)
        audit.append({'date': date, 'pool': member.pool, 'symbol': member.symbol,
                      'eligible': True, 'valid': summary is not None, 'reason': reason})
    output = {}
    for pool in POOLS:
        denominator = int((m.pool.eq(pool) & m.eligible).sum())
        selected = summaries[pool]
        fraction = len(selected)/denominator if denominator else 0.
        coverage.append({'date': date, 'pool': pool, 'members': expected_member_counts[pool],
                         'eligible': denominator, 'valid_stocks': len(selected),
                         'coverage': fraction, 'passed': bool(denominator and fraction >= MIN_COVERAGE)})
        if not selected:
            values = [np.nan]*8
        else:
            s = pd.DataFrame(selected)
            variance = s.total_var.sum()
            values = [float(s.day.gt(0).mean()), float(s.late.gt(0).mean()),
                      float((s.early.gt(0) & s.late.lt(0)).mean()), float(s.day.std(ddof=0)),
                      float(s.efficiency.mean()), float(s.down_var.sum()/variance) if variance else 0.,
                      float(s.late_volume.mean()), float(s.close_location.mean())]
        output.update(zip([f'm{pool}_{key}' for key in MEASURES], values))
    valid = all(row['passed'] for row in coverage)
    features = pd.Series(output, index=FEATURE_COLUMNS, name=date, dtype=float)
    if not valid:
        features[:] = np.nan
    return DayFeatures(features, valid, pd.DataFrame(coverage), pd.DataFrame(audit))


def _same_calendar(*objects):
    idx = objects[0].index
    if (not isinstance(idx, pd.DatetimeIndex) or not idx.is_unique or
            not idx.is_monotonic_increasing or not idx.equals(idx.normalize()) or
            any(not obj.index.equals(idx) for obj in objects[1:])):
        raise ValueError('requires the same complete, unique, sorted daily calendar')


def align_feature_groups(daily, minute, valid):
    """Preserve the supplied full calendar; caller uses training_rows only after labels exist."""
    _same_calendar(daily, minute, valid)
    _booleans(valid, 'valid')
    if list(minute.columns) != FEATURE_COLUMNS or not daily.columns.is_unique:
        raise ValueError('unexpected feature columns')
    if set(daily.columns) & set(FEATURE_COLUMNS):
        raise ValueError('feature groups overlap')
    combined = pd.concat([daily, minute], axis=1)
    if not np.isfinite(combined.loc[valid].to_numpy(dtype=float)).all():
        raise ValueError('declared valid inputs must be finite')
    base = daily.astype(float).copy()
    base.loc[~valid] = np.nan
    combined.loc[~valid] = np.nan
    return FeatureGroups(base, combined, daily.index[valid], valid.copy())


def sizing_with_fallback(prediction, direction, valid):
    _same_calendar(prediction, direction, valid)
    _booleans(valid, 'valid')
    if not direction.isin([-1., 0., 1.]).all() or not np.isfinite(prediction.loc[valid]).all():
        raise ValueError('invalid direction or nonfinite prediction on valid days')
    smooth = (.75+.25*np.tanh(prediction.where(valid, 0.))).rolling(5, min_periods=1).mean()
    return (direction*smooth.where(valid, 1.)).rename('target')


def past_control_with_fallback(target, direction, valid):
    """Past same-side mean on valid days; invalid days retain the exact incumbent."""
    _same_calendar(target, direction, valid)
    _booleans(valid, 'valid')
    if (not np.isfinite(target).all() or not direction.isin([-1., 0., 1.]).all()
            or (target.abs() > 1).any() or (target*direction < 0).any()
            or target.loc[direction.eq(0)].ne(0).any()):
        raise ValueError('invalid sizing control inputs')
    result = direction.astype(float).copy()
    for side in (-1., 1.):
        eligible = valid & direction.eq(side)
        sizes = target.abs().where(eligible)
        count = sizes.notna().cumsum().shift(1, fill_value=0)
        total = sizes.fillna(0).cumsum().shift(1, fill_value=0)
        mean = (total/count.where(count > 0)).fillna(.75)
        result.loc[eligible] = side*mean.loc[eligible]
    return result.rename('past_control')


def delivery_readiness(manifest):
    """Validate declarations only. Never open referenced files or trigger research."""
    if not isinstance(manifest, dict):
        return {'ready': False, 'issues': ['manifest must be an object'], 'warnings': []}
    issues, warnings = [], ['Declarations only: source files and receipt evidence have not been verified.']
    fixed = {'schema_version': 'stock-minute-ml-input-v1', 'state': 'ready',
             'timezone': TIMEZONE, 'frequency_minutes': 5, 'session_grid': 'cn_equity_48x5m_v1',
             'auction_policy': 'open_excluded_close_in_final_bar', 'price_basis': 'unadjusted_intraday',
             'volume_unit': 'shares', 'amount_unit': 'CNY', 'membership_basis': 'historical_effective'}
    for key, expected in fixed.items():
        if manifest.get(key) != expected:
            issues.append(f'{key}: expected {expected}')
    if manifest.get('cleaning_complete') is not True:
        issues.append('cleaning_complete must be true')
    for key in ['dataset_version', 'cleaning_receipt', 'normalization_evidence', 'membership_source', 'quality_mask_version']:
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            issues.append(f'{key}: required')
    try:
        if _date(manifest.get('start_date')) > _date(manifest.get('end_date')):
            issues.append('start_date is after end_date')
    except (ValueError, TypeError):
        issues.append('valid start_date and end_date required')
    availability = manifest.get('availability_basis')
    if availability not in ['observed', 'documented_assumption']:
        issues.append('availability_basis must be observed or documented_assumption')
    if availability == 'documented_assumption':
        warnings.append('Historical publication availability is assumed, not independently observed.')
    files = manifest.get('files')
    required_roles = {'bars', 'memberships', 'calendar', 'quality_exclusions'}
    seen, paths = set(), set()
    row_counts = {role: 0 for role in required_roles}
    if not isinstance(files, list):
        files = []
        issues.append('files must be a list')
    for item in files:
        if not isinstance(item, dict) or item.get('role') not in required_roles:
            issues.append('invalid file role')
            continue
        role = item['role']
        seen.add(role)
        path = item.get('path')
        if (not isinstance(path, str) or not path or '\\' in path or '\x00' in path or
                PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or
                path in paths or path == '.'):
            issues.append(f'{role}: invalid or duplicate relative path')
        else:
            paths.add(path)
        if not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', ''))):
            issues.append(f'{role}: valid SHA256 required')
        for field in ['rows', 'size_bytes']:
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(f'{role}: nonnegative {field} required')
            elif field == 'rows':
                row_counts[role] += value
    if seen != required_roles:
        issues.append('all four input roles required')
    for role in required_roles-{'quality_exclusions'}:
        if not row_counts[role]:
            issues.append(f'{role}: no declared data rows')
    return {'ready': not issues, 'issues': issues, 'warnings': warnings,
            'files_verified': False, 'research_started': False}
