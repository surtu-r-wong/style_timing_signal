"""Fixed order-size contrast and causal position-state rules for research."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.intraday_flow_pilot import residual_past

CODES = {'CSI300': '000300.SH', 'CHINEXT': '399102.SZ',
         'CSI500': '000905.SH', 'CSI1000': '000852.SH'}


def structure_scores(flow, prices, calendar):
    px = prices.pivot(index='date', columns='index_code', values='close').reindex(calendar)
    scores, details = {}, []
    for name, code in CODES.items():
        d = flow.loc[flow.index_code.eq(code)].set_index('trade_date').reindex(calendar)
        xgross = d.xlarge_buy_money + d.xlarge_sell_money
        lgross = d.large_buy_money + d.large_sell_money
        present = d.index_code.notna()
        if not (xgross[present].gt(0).all() and lgross[present].gt(0).all()):
            raise ValueError('nonpositive size-specific gross amount')
        x = (d.xlarge_buy_money - d.xlarge_sell_money) / xgross
        large = (d.large_buy_money - d.large_sell_money) / lgross
        total = (x * xgross + large * lgross) / (xgross + lgross)
        control = pd.DataFrame({'flow': total, 'ret': px[code].pct_change(fill_method=None)})
        resid = residual_past(x - large, control, window=250)
        scores[name] = resid.rolling(20, min_periods=20).mean()
        detail = pd.DataFrame({'xlarge_imbalance': x, 'large_imbalance': large,
                               'difference': x - large, 'total_imbalance': total,
                               'price_return': control.ret, 'xlarge_gross_share': xgross/(xgross+lgross),
                               'residual': resid, 'score': scores[name]}, index=calendar)
        detail['quadrant'] = np.select([x.ge(0) & large.ge(0), x.ge(0) & large.lt(0),
                                       x.lt(0) & large.ge(0), x.lt(0) & large.lt(0)],
                                      ['both_buy', 'x_buy_l_sell', 'x_sell_l_buy', 'both_sell'], default='missing')
        details.append(detail.assign(source=name))
    scores = pd.DataFrame(scores, index=calendar)
    for group, members in {'PAIR': ['CSI300', 'CHINEXT'], 'POOL': ['CSI500', 'CSI1000']}.items():
        scores[group] = scores[members].mean(axis=1).where(scores[members].notna().all(axis=1))
    return scores, pd.concat(details, names=None).rename_axis('date').reset_index()


def _aligned(base, values):
    if not base.index.equals(values.index) or not base.index.is_unique:
        raise ValueError('unaligned inputs')
    if not np.isfinite(base).all() or not np.isfinite(values).all():
        raise ValueError('nonfinite inputs')
    if not base.isin([-1., 0.]).all():
        raise ValueError('expected baseline short leg')


def short_modes(base, score):
    """At baseline episode start evaluate entry once; exits never reenter."""
    _aligned(base, score)
    gate = score.gt(0) | score.abs().le(1e-12)
    result = {name: np.zeros(len(base)) for name in ['daily', 'entry_hold', 'exit_only', 'entry_exit']}
    previous_short = False
    entry = exit_live = both = False
    for i, (short, allowed) in enumerate(zip(base.lt(0), gate)):
        if not short:
            entry = exit_live = both = False
        elif not previous_short:
            entry = bool(allowed)
            exit_live = True
            both = bool(allowed)
        else:
            exit_live = exit_live and bool(allowed)
            both = both and bool(allowed)
        result['daily'][i] = -float(short and allowed)
        result['entry_hold'][i] = -float(short and entry)
        result['exit_only'][i] = -float(short and exit_live)
        result['entry_exit'][i] = -float(short and both)
        previous_short = short
    return pd.DataFrame(result, index=base.index)


def stable_short(base, score, threshold):
    _aligned(base, score)
    if not base.index.equals(threshold.index) or not np.isfinite(threshold).all() or not threshold.gt(0).all():
        raise ValueError('invalid threshold')
    tier = base * pd.Series(np.where(score.gt(threshold), 1., np.where(score.lt(-threshold), 0., .5)), index=base.index)
    state, out = 0., []
    for b, value, h in zip(base, score, threshold):
        if not b:
            state = 0.
        elif value > h:
            state = -1.
        elif value < -h:
            state = 0.
        out.append(state)
    return pd.DataFrame({'tier': tier, 'hysteresis': out}, index=base.index)


def daily_phase(base, selected):
    """Ex-post labels only. Never call this to construct a position."""
    _aligned(base, selected)
    result = pd.Series('outside_short', index=base.index)
    mask = base.lt(0).to_numpy()
    starts = np.flatnonzero(mask & ~np.r_[False, mask[:-1]])
    for start in starts:
        ends = np.flatnonzero(~mask[start:])
        stop = start + ends[0] if len(ends) else len(base)
        kept = np.flatnonzero(selected.iloc[start:stop].lt(0))
        if not len(kept):
            result.iloc[start:stop] = 'whole_episode_rejected'
        else:
            first, last = start + kept[0], start + kept[-1]
            result.iloc[start:first] = 'before_first_entry'
            result.iloc[first:last+1] = 'between_entries'
            result.iloc[last+1:stop] = 'after_last_exit'
            result.iloc[start+kept] = 'kept'
    return result
