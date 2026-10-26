"""Frozen descriptive pilot of intraday flow filters for independent EW legs."""
from __future__ import annotations

import numpy as np
import pandas as pd


def residual_past(y: pd.Series, controls: pd.DataFrame, window: int = 250) -> pd.Series:
    """Full calendar window, intercept included, no current observation in fit."""
    if not y.index.equals(controls.index) or not y.index.is_unique:
        raise ValueError('unaligned or duplicate input calendar')
    if window <= controls.shape[1] + 1:
        raise ValueError('insufficient fitting window')
    xx = np.column_stack([np.ones(len(y)), controls.to_numpy(dtype=float)])
    yy = y.to_numpy(dtype=float)
    out = np.full(len(y), np.nan)
    for i in range(window, len(y)):
        x, target = xx[i-window:i], yy[i-window:i]
        if not (np.isfinite(x).all() and np.isfinite(target).all()
                and np.isfinite(xx[i]).all() and np.isfinite(yy[i])):
            continue
        beta, _, rank, _ = np.linalg.lstsq(x, target, rcond=None)
        if rank == x.shape[1]:
            out[i] = yy[i] - xx[i] @ beta
    return pd.Series(out, index=y.index, name=y.name)


def leg_rules(ew: pd.Series, features: pd.DataFrame):
    if not ew.index.equals(features.index) or not ew.index.is_unique:
        raise ValueError('unaligned or duplicate signal calendar')
    if not np.isfinite(ew).all() or not np.isfinite(features).all().all():
        raise ValueError('signals must be finite')
    base = np.sign(ew.astype(float))
    longs, shorts = {'base': base.clip(lower=0)}, {'base': base.clip(upper=0)}
    for name, values in features.items():
        for direction, mult in [('pos', 1), ('neg', -1)]:
            v = mult * values
            neutral = values.abs().le(1e-12)
            longs[f'{name}_{direction}'] = longs['base'].where(v.gt(0) | neutral, 0.)
            shorts[f'{name}_{direction}'] = shorts['base'].where(v.lt(0) | neutral, 0.)
    return longs, shorts


def attribute_ledger(d: pd.DataFrame):
    """Allocate one pool's dollar PnL/costs; independent compounded legs aren't additive."""
    current = np.sign(d.decision_signal.to_numpy(dtype=float))
    previous = np.r_[0., current[:-1]]
    prior_equity = d.equity.shift(1, fill_value=1.)
    old_value = d.net_notional.shift(1, fill_value=0.) * prior_equity + d.gross_pnl
    new_value = d.net_notional * d.equity
    long_cost, short_cost = np.zeros(len(d)), np.zeros(len(d))
    for i, (old, new) in enumerate(zip(previous, current)):
        cost = float(d.cost.iloc[i])
        if old == new:
            if new > 0:
                long_cost[i] = cost
            elif new < 0:
                short_cost[i] = cost
            elif abs(cost) > 1e-14:
                raise ValueError('flat ledger has transaction cost')
        else:
            total = abs(old_value.iloc[i]) + abs(new_value.iloc[i])
            closing = cost * abs(old_value.iloc[i]) / total if total else 0.
            opening = cost - closing
            if old > 0:
                long_cost[i] += closing
            elif old < 0:
                short_cost[i] += closing
            if new > 0:
                long_cost[i] += opening
            elif new < 0:
                short_cost[i] += opening
    out = pd.DataFrame({'long_pnl': d.gross_pnl.where(previous > 0, 0.),
                        'short_pnl': d.gross_pnl.where(previous < 0, 0.),
                        'long_cost': long_cost, 'short_cost': short_cost}, index=d.index)
    out['long_net'] = out.long_pnl - out.long_cost
    out['short_net'] = out.short_pnl - out.short_cost
    np.testing.assert_allclose(out.long_net + out.short_net, d.equity - prior_equity, atol=1e-12, rtol=1e-10)
    np.testing.assert_allclose(out.long_cost + out.short_cost, d.cost, atol=1e-12, rtol=1e-10)
    trades, active = [], None
    for i, date in enumerate(d.index):
        old, new = previous[i], current[i]
        if active is not None:
            side = 'long' if old > 0 else 'short'
            active['gross_pnl'] += float(out[f'{side}_pnl'].iloc[i])
            active['cost'] += float(out[f'{side}_cost'].iloc[i])
            active['mark_date'] = str(date.date())
            if old != new:
                active['closed'] = True
                trades.append(active)
                active = None
        if new and old != new:
            side = 'long' if new > 0 else 'short'
            active = {'side': side, 'entry_date': str(date.date()), 'mark_date': str(date.date()),
                      'entry_equity': float(prior_equity.iloc[i] + d.gross_pnl.iloc[i]),
                      'gross_pnl': 0., 'cost': float(out[f'{side}_cost'].iloc[i]), 'closed': False}
    if active is not None:
        trades.append(active)
    trades = pd.DataFrame(trades, columns=['side', 'entry_date', 'mark_date', 'entry_equity', 'gross_pnl', 'cost', 'closed'])
    trades['net_pnl'] = trades.gross_pnl - trades.cost
    trades['return_on_entry_equity'] = trades.net_pnl / trades.entry_equity
    np.testing.assert_allclose(trades.net_pnl.sum(), d.equity.iloc[-1]-1., atol=1e-12, rtol=1e-10)
    return out, trades


def prepare_close_market(prices, index, weights, expiries):
    """Cache previous-day OI selection and same-contract marks, independent of signal."""
    index = pd.DatetimeIndex(index)
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError('invalid market calendar')
    w = weights.reindex(index)
    if w.isna().any().any() or (w < 0).any().any() or (w.sum(axis=1) > 1.0000001).any():
        raise ValueError('invalid market weights')
    p = prices.copy()
    p['date'] = pd.to_datetime(p.date)
    if p.duplicated(['date','symbol']).any():
        raise ValueError('duplicate quote')
    days = {date: g.set_index('symbol') for date, g in p.groupby('date')}
    quotes = p.set_index(['date','symbol']).close.to_dict()
    shape = (len(index),len(w.columns))
    symbols = np.full(shape, '', dtype=object)
    new_price, old_price = np.full(shape,np.nan), np.full(shape,np.nan)
    for i in range(1,len(index)):
        date, prev = index[i], index[i-1]
        for j, group in enumerate(w.columns):
            if symbols[i-1,j]:
                old_price[i,j] = quotes.get((date,symbols[i-1,j]),np.nan)
            if w.iloc[i,j] == 0 or prev not in days:
                continue
            candidates = days[prev]
            candidates = candidates[candidates.index.str.startswith(group)].copy()
            candidates = candidates[pd.to_numeric(candidates.oi,errors='coerce').notna()]
            candidates = candidates.loc[[sym for sym in candidates.index
                                         if sym not in expiries or pd.Timestamp(expiries[sym]) > date]].sort_index()
            if len(candidates):
                sym = candidates.oi.astype(float).idxmax()
                symbols[i,j] = sym
                new_price[i,j] = quotes.get((date,sym),np.nan)
    return {'index':index,'weights':w.to_numpy(dtype=float),'groups':list(w.columns),
            'symbols':symbols,'new_price':new_price,'old_price':old_price}


def batch_close_ledgers(market, signals, cost_bps=3.):
    """Vectorize only across strategies; algebra matches contract_ledger(close)."""
    if not signals.index.equals(market['index']) or not signals.index.is_unique:
        raise ValueError('unaligned signal calendar')
    s = signals.to_numpy(dtype=float)
    if not np.isfinite(s).all() or (np.abs(s)>1).any() or cost_bps < 0:
        raise ValueError('invalid signals or cost')
    n, k = s.shape
    g = market['weights'].shape[1]
    equity, quantities, marks = np.ones(k), np.zeros((k,g)), np.ones(g)
    keys = ['ret','equity','gross_pnl','pre_fill_pnl','post_fill_pnl','cost','cost_return',
            'trade_notional','turnover','net_notional','gross_notional','rolls','decision_signal']
    out = {key:np.zeros((n,k)) for key in keys}
    for i in range(n):
        before = equity.copy()
        old = market['old_price'][i]
        valid_old = np.isfinite(old) & (old>0)
        if np.any((quantities != 0) & ~valid_old):
            raise ValueError('missing or invalid held quote')
        old = np.where(valid_old,old,1.)
        pnl = (quantities*(old-marks)).sum(axis=1)
        at_fill = equity+pnl
        if (at_fill<=0).any():
            raise ValueError('pool insolvent')
        decision = s[i-1] if i else np.zeros(k)
        target = decision[:,None]*market['weights'][i]*at_fill[:,None]
        new = market['new_price'][i]
        valid_new = np.isfinite(new) & (new>0)
        if np.any((target != 0) & ~valid_new):
            raise ValueError('missing or invalid target quote')
        new = np.where(valid_new,new,1.)
        desired = target/new
        same = market['symbols'][i] == (market['symbols'][i-1] if i else np.full(g,''))
        traded = np.where(same,np.abs(desired-quantities)*new,
                          np.abs(quantities)*old+np.abs(desired)*new).sum(axis=1)
        cost = traded*cost_bps/1e4
        equity = at_fill-cost
        if (equity<=0).any():
            raise ValueError('pool insolvent after fill')
        row = {'ret':equity/before-1.,'equity':equity,'gross_pnl':pnl,'pre_fill_pnl':pnl,
               'post_fill_pnl':np.zeros(k),'cost':cost,'cost_return':cost/before,
               'trade_notional':traded,'turnover':traded/before,
               'net_notional':target.sum(axis=1)/equity,'gross_notional':np.abs(target).sum(axis=1)/equity,
               'rolls':((quantities!=0)&(desired!=0)&~same).sum(axis=1),'decision_signal':decision}
        for key in keys:
            out[key][i] = row[key]
        quantities, marks = desired, new
    return {name:pd.DataFrame({key:value[:,j] for key,value in out.items()},index=signals.index)
            for j,name in enumerate(signals.columns)}
