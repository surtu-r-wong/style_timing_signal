import pandas as pd
import pytest

from backtest.execution_ledger import contract_ledger, combine_pools, futures_weights
from backtest.metrics import max_drawdown


def frame(rows):
    d = pd.DataFrame(rows, columns=['date', 'symbol', 'open', 'close', 'oi'])
    d['date'] = pd.to_datetime(d['date'])
    return d


def test_first_loss_counts_from_initial_capital():
    assert max_drawdown(pd.Series([-.2, .1])) == pytest.approx(-.2)


def test_signal_cannot_earn_previous_overnight_move():
    d = frame([('2020-01-01','IC1',100,100,10),
               ('2020-01-02','IC1',110,121,10),
               ('2020-01-03','IC1',110,100,10)])
    s = pd.Series([1,0,0], index=pd.to_datetime(d.date))
    out = contract_ledger(d, s, {'IC':1.}, fill='open', cost_bps=0)
    assert out.ret.iloc[0] == 0
    assert out.ret.iloc[1] == pytest.approx(.1)
    # The old long bears the next overnight loss before exiting at the open.
    assert out.equity.iloc[-1] == pytest.approx(1.)


def test_close_execution_only_earns_after_next_close():
    d = frame([('2020-01-01','IC1',100,100,10),
               ('2020-01-02','IC1',110,121,10),
               ('2020-01-03','IC1',121,133.1,10)])
    s = pd.Series(1., index=pd.to_datetime(d.date))
    out = contract_ledger(d,s,{'IC':1.},fill='close',cost_bps=0)
    assert out.ret.iloc[1] == 0
    assert out.ret.iloc[2] == pytest.approx(.1)


def test_roll_has_no_cross_contract_price_jump_and_charges_both_sides():
    d=frame([(date,sym,price,price,oi) for date,ois in
             [('2020-01-01',(20,10)),('2020-01-02',(10,20)),('2020-01-03',(10,20))]
             for sym,price,oi in [('IC1',100,ois[0]),('IC2',200,ois[1])]])
    s=pd.Series(1.,index=pd.to_datetime(sorted(d.date.unique())))
    out=contract_ledger(d,s,{'IC':1.},fill='open',cost_bps=3)
    assert out.symbols.iloc[1]=='IC1'  # day 2 OI cannot choose day 2 contract
    assert out.symbols.iloc[2]=='IC2'
    assert out.gross_pnl.abs().max()==0
    assert out.cost.iloc[2] == pytest.approx(.0006, rel=.001)
    assert out.rolls.iloc[2]==1


def test_missing_held_quote_fails_instead_of_switching_to_available_contract():
    d=frame([('2020-01-01','IC1',100,100,20),('2020-01-02','IC1',100,100,20),
             ('2020-01-02','IC2',100,100,10),('2020-01-03','IC2',100,100,30)])
    s=pd.Series(1.,index=pd.to_datetime(sorted(d.date.unique())))
    with pytest.raises(ValueError,match='quote'):
        contract_ledger(d,s,{'IC':1.},fill='open',cost_bps=0)


def test_cash_pools_do_not_implicitly_rebalance_capital():
    a=pd.DataFrame({'ret':[.5,0.]}); b=pd.DataFrame({'ret':[0.,1.]})
    r=combine_pools(a,b)
    assert r.equity.tolist()==pytest.approx([1.25,1.75])
    assert r.ret.iloc[1]==pytest.approx(.4)


def test_no_im_before_first_previous_day_quote():
    idx=pd.to_datetime(['2022-07-21','2022-07-22','2022-07-25'])
    w=futures_weights(idx,pd.Timestamp('2022-07-22'))
    assert w.loc['2022-07-22','IC']==1
    assert w.loc['2022-07-22','IM']==0
    assert w.loc['2022-07-25','IM']==.5


def test_zero_position_does_not_require_an_unavailable_instrument():
    d=frame([('2020-01-01','IC1',100,100,20),('2020-01-02','IC1',100,100,20)])
    s=pd.Series(0.,index=pd.to_datetime(d.date))
    out=contract_ledger(d,s,{'MISSING':1.},fill='open',cost_bps=0)
    assert (out.ret==0).all()


def test_short_to_long_flip_does_not_double_count_overnight():
    d=frame([('2020-01-01','IC1',100,100,20),
             ('2020-01-02','IC1',100,90,20),
             ('2020-01-03','IC1',99,108.9,20)])
    s=pd.Series([-1.,1.,1.],index=pd.to_datetime(d.date))
    out=contract_ledger(d,s,{'IC':1.},fill='open',cost_bps=0)
    assert out.equity.iloc[-1]==pytest.approx(1.111)


def test_expiry_metadata_forces_roll_without_using_today_oi():
    d=frame([('2020-01-01','IC1',100,100,20),('2020-01-01','IC2',200,200,10),
             ('2020-01-02','IC1',100,100,20),('2020-01-02','IC2',200,200,10)])
    s=pd.Series(1.,index=pd.to_datetime(sorted(d.date.unique())))
    out=contract_ledger(d,s,{'IC':1.},fill='open',cost_bps=0,
                        expiries={'IC1':'2020-01-02','IC2':'2020-02-01'})
    assert out.symbols.iloc[-1]=='IC2'
    assert out.equity.iloc[-1]==1.
