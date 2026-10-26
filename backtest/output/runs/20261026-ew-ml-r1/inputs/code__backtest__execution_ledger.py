"""Fractional-notional, close-marked ledger; signals and contract choice lag fills.

No synthetic carry. Cash interest, integer lots, margin calls, market impact and
limit-lock feasibility are outside this price-based execution sensitivity model.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def futures_weights(index, first_im_date):
    index = pd.DatetimeIndex(index)
    active = index > pd.Timestamp(first_im_date)
    return pd.DataFrame({'IC': np.where(active, .5, 1.),
                         'IM': np.where(active, .5, 0.)}, index=index)


def contract_ledger(prices, signal, weights, *, fill='close', cost_bps=3.,
                    select_main=True, expiries=None):
    """One cash pool with retained quantities and mark-to-market equity.

    Each day: mark yesterday's quantities to today's fill, choose targets using
    yesterday's signal/OI, charge both sides of rolls, mark new quantities to
    close. Weights are fractions of pre-cost fill-time pool equity; fee-induced
    excess over target is exposed as gross_notional. Missing required quotes
    raise. The first calendar day initializes flat.
    """
    if fill not in ('open', 'close') or cost_bps < 0:
        raise ValueError('invalid fill or cost')
    s = signal.astype(float).sort_index()
    if not s.index.is_unique or s.isna().any() or not np.isfinite(s).all():
        raise ValueError('signal must have unique dates and finite values')
    if (s.abs() > 1).any():
        raise ValueError('signal exposure exceeds 1')
    if isinstance(weights, dict):
        weights = pd.DataFrame(weights, index=s.index)
    w = weights.reindex(s.index)
    if w.isna().any().any() or (w < 0).any().any() or (w.sum(axis=1) > 1.0000001).any():
        raise ValueError('invalid instrument weights')
    p = prices.copy()
    p['date'] = pd.to_datetime(p['date'])
    if p.duplicated(['date','symbol']).any():
        raise ValueError('duplicate price quote')
    quotes = p.set_index(['date','symbol'])
    daily = {dt: g.set_index('symbol') for dt,g in p.groupby('date')}
    expiries = expiries or {}

    def price(day, sym, col):
        try:
            value = float(quotes.at[(day,sym),col])
        except KeyError:
            raise ValueError(f'missing quote {day.date()} {sym} {col}') from None
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f'invalid quote {day.date()} {sym} {col}')
        return value

    equity = 1.
    holdings, marks, held_groups = {}, {}, {}
    rows = []
    for i,t in enumerate(s.index):
        prior_equity = equity
        overnight = sum(q * (price(t,sym,fill) - marks[sym])
                        for sym,q in holdings.items())
        at_fill = equity + overnight
        if at_fill <= 0:
            raise ValueError('pool insolvent before fill')
        desired, selected = {}, {}
        if i and s.iloc[i-1] != 0:
            prev = s.index[i-1]
            for group, weight in w.loc[t].items():
                if weight == 0:
                    continue
                if select_main:
                    if prev not in daily:
                        raise ValueError(f'missing prior OI {prev.date()}')
                    candidates = daily[prev]
                    candidates = candidates[candidates.index.str.startswith(group)].copy()
                    candidates = candidates[pd.to_numeric(candidates.oi,errors='coerce').notna()]
                    eligible = [sym for sym in candidates.index
                                if sym not in expiries or pd.Timestamp(expiries[sym]) > t]
                    candidates = candidates.loc[eligible].sort_index()
                    if candidates.empty:
                        raise ValueError(f'no prior-day contract {group} {t.date()}')
                    sym = candidates.oi.astype(float).idxmax()
                else:
                    sym = group
                selected[group] = sym
                desired[sym] = desired.get(sym,0.) + float(s.iloc[i-1] * weight * at_fill / price(t,sym,fill))
        trade_notional = sum(abs(desired.get(sym,0.)-holdings.get(sym,0.))*price(t,sym,fill)
                             for sym in sorted(set(holdings)|set(desired)))
        cost = trade_notional * cost_bps / 1e4
        intraday = sum(q*(price(t,sym,'close')-price(t,sym,fill)) for sym,q in desired.items())
        equity = at_fill - cost + intraday
        if equity <= 0:
            raise ValueError('pool insolvent after fill')
        rolls = sum(group in selected and sym != selected[group]
                    for group,sym in held_groups.items())
        notionals = {sym:q*price(t,sym,'close') for sym,q in desired.items()}
        rows.append({'date':t,'ret':equity/prior_equity-1,'equity':equity,
                     'gross_pnl':overnight+intraday,'pre_fill_pnl':overnight,
                     'post_fill_pnl':intraday,'cost':cost,'cost_return':cost/prior_equity,
                     'trade_notional':trade_notional,'turnover':trade_notional/prior_equity,
                     'net_notional':sum(notionals.values())/equity,
                     'gross_notional':sum(abs(x) for x in notionals.values())/equity,
                     'rolls':rolls,'symbols':'|'.join(sorted(desired)),
                     'decision_signal':float(s.iloc[i-1]) if i else 0.})
        holdings = desired
        marks = {sym:price(t,sym,'close') for sym in desired}
        held_groups = selected
    return pd.DataFrame(rows).set_index('date')


def combine_pools(a, b, weight=.5, rebalance=False):
    if not a.index.equals(b.index):
        raise ValueError('cash pools require identical calendars')
    if not 0 <= weight <= 1:
        raise ValueError('invalid pool weight')
    if rebalance:
        r = weight*a.ret+(1-weight)*b.ret
        equity = (1+r).cumprod()
    else:
        ea, eb = (1+a.ret).cumprod(), (1+b.ret).cumprod()
        equity = weight*ea+(1-weight)*eb
        r = equity/equity.shift(1,fill_value=1.)-1
    return pd.DataFrame({'ret':r,'equity':equity})
