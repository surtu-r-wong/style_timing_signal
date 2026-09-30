import numpy as np
import pandas as pd
import pytest

from backtest.intraday_flow_pilot import residual_past, leg_rules, attribute_ledger
from backtest.execution_ledger import contract_ledger


def test_residual_coefficients_exclude_today_and_future():
    idx = pd.date_range('2020-01-01', periods=15)
    x = pd.DataFrame({'flow': np.sin(np.arange(15)), 'ret': np.cos(np.arange(15))}, index=idx)
    y = 2 + 3 * x.flow - 4 * x.ret
    out = residual_past(y, x, window=6)
    assert out.iloc[:6].isna().all()
    np.testing.assert_allclose(out.iloc[6:], 0, atol=1e-12)
    changed = y.copy()
    changed.iloc[8] += 10
    changed.iloc[11:] = 1000
    other = residual_past(changed, x, window=6)
    assert other.iloc[8] == pytest.approx(10)
    np.testing.assert_allclose(out.iloc[:8], other.iloc[:8], equal_nan=True)
    assert other.iloc[9] != pytest.approx(out.iloc[9])


def test_missing_dates_are_not_compressed_out_of_training_window():
    idx = pd.date_range('2020-01-01', periods=10)
    x = pd.DataFrame({'x': np.arange(10)}, index=idx)
    y = pd.Series(np.arange(10), index=idx, dtype=float)
    y.iloc[3] = np.nan
    out = residual_past(y, x, window=4)
    assert out.iloc[4:8].isna().all()
    assert np.isfinite(out.iloc[8:]).all()


def test_gate_is_leg_specific_zero_keeps_baseline_and_missing_fails():
    ew = pd.Series([1., 1., -1., -1., 0.])
    feat = pd.DataFrame({'A': [2., -2., 2., -2., 0.], 'B': [0., 0., 0., 0., 0.]})
    longs, shorts = leg_rules(ew, feat)
    assert longs['A_pos'].tolist() == [1, 0, 0, 0, 0]
    assert shorts['A_pos'].tolist() == [0, 0, 0, -1, 0]
    assert shorts['A_neg'].tolist() == [0, 0, -1, 0, 0]
    pd.testing.assert_series_equal(longs['B_pos'], longs['base'])
    for left in longs.values():
        for right in shorts.values():
            assert not ((left > 0) & (right < 0)).any()
    feat.iloc[0, 0] = np.nan
    with pytest.raises(ValueError, match='finite'):
        leg_rules(ew, feat)


def test_attribution_assigns_old_side_pnl_and_both_flip_costs():
    idx = pd.date_range('2020-01-01', periods=5)
    prices = pd.DataFrame({'date': idx, 'symbol': 'IC1', 'close': [100.,100.,110.,99.,99.], 'oi': 10})
    s = pd.Series([1., -1., 0., 0., 0.], index=idx)
    d = contract_ledger(prices, s, {'IC': 1.}, cost_bps=3, fill='close')
    attributed, trades = attribute_ledger(d)
    np.testing.assert_allclose(attributed.long_pnl + attributed.short_pnl, d.gross_pnl)
    np.testing.assert_allclose(attributed.long_cost + attributed.short_cost, d.cost)
    np.testing.assert_allclose(attributed.long_net + attributed.short_net, d.equity.diff().fillna(d.equity.iloc[0]-1))
    assert attributed.long_pnl.iloc[2] > 0
    assert attributed.short_pnl.iloc[3] > 0
    assert attributed.long_cost.iloc[2] > 0 and attributed.short_cost.iloc[2] > 0
    assert len(trades) == 2
    assert trades.closed.all()
    assert (trades.net_pnl > 0).all()
    assert trades.net_pnl.sum() == pytest.approx(d.equity.iloc[-1]-1)


def test_open_trade_is_marked_separately():
    idx = pd.date_range('2020-01-01', periods=3)
    p = pd.DataFrame({'date':idx, 'symbol':'IC1', 'close':[100.,100.,110.], 'oi':10})
    d = contract_ledger(p, pd.Series(1., index=idx), {'IC':1.}, cost_bps=0)
    _, trades = attribute_ledger(d)
    assert len(trades) == 1
    assert not trades.closed.iloc[0]
    assert trades.net_pnl.iloc[0] == pytest.approx(.1)


def test_batch_close_matches_reference_for_flips_rolls_weights_and_delays():
    from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers
    idx = pd.date_range('2020-01-01', periods=9)
    rows = []
    for i, date in enumerate(idx):
        for symbol, price, oi in [('IC1', 100+i*3, 20 if i<3 else 10),
                                  ('IC2', 200-i*2, 10 if i<3 else 20),
                                  ('IM1', 80+i, 10)]:
            rows.append((date, symbol, price, oi))
    p = pd.DataFrame(rows, columns=['date','symbol','close','oi'])
    w = pd.DataFrame({'IC':[1.]*3+[.5]*6, 'IM':[0.]*3+[.5]*6}, index=idx)
    signals = pd.DataFrame({'flip':[1.,1.,-1.,-1.,0.,.5,.5,1.,0.],
                            'short':[-1.]*9, 'flat':[0.]*9}, index=idx)
    expiries = {'IC1': idx[5]}
    market = prepare_close_market(p, idx, w, expiries)
    for cost in (0., 3., 10.):
        actual = batch_close_ledgers(market, signals, cost_bps=cost)
        for name, sig in signals.items():
            expected = contract_ledger(p, sig, w, expiries=expiries, cost_bps=cost)
            for field in ['ret','equity','gross_pnl','cost','turnover','net_notional','gross_notional','rolls','decision_signal']:
                np.testing.assert_allclose(actual[name][field], expected[field], atol=1e-12)


def test_batch_missing_held_quote_fails_but_flat_needs_no_quote():
    from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers
    idx = pd.date_range('2020-01-01', periods=3)
    p = pd.DataFrame({'date':idx[:2], 'symbol':'IC1', 'close':100., 'oi':10.})
    market = prepare_close_market(p,idx,pd.DataFrame({'IC':1.},index=idx),{})
    assert batch_close_ledgers(market,pd.DataFrame({'flat':0.},index=idx))['flat'].equity.eq(1).all()
    with pytest.raises(ValueError,match='quote'):
        batch_close_ledgers(market,pd.DataFrame({'long':1.},index=idx))
