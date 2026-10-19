import numpy as np
import pandas as pd

from tools.research_equity_bond_short import (
    batched_close_ledger, cycle_short, high_turn_short, schedule, signals, trend_short,
)


def test_no_short_below_floor_or_before_trend_warmup():
    r=pd.Series([1.3,1.25,1.2,1.15,1.04,.95])
    np.testing.assert_array_equal(trend_short(r,1.05,ma=3,momentum=2),[0,0,-1,-1,0,0])


def test_signals_are_causal_under_extreme_future_changes():
    idx=pd.bdate_range("2020-01-01",periods=250)
    r=pd.Series(1.3+np.sin(np.arange(250)/11)*.2,index=idx)
    frame=pd.DataFrame({"ratio":r,"equity":r*2000},index=idx)
    ew=pd.Series(np.where(np.arange(250)%40<20,-1.,1.),index=idx)
    whole,_=signals(frame,ew)
    earlier,_=signals(frame.iloc[:180],ew.iloc[:180])
    pd.testing.assert_frame_equal(whole.iloc[:180],earlier)
    assert whole.le(0).all().all() and whole.ge(-1).all().all()


def test_high_turn_holds_below_entry_threshold_until_trend_exit():
    r=pd.Series(np.r_[np.linspace(1.6,1.4,60),1.3,1.19,1.1,1.02,1.01])
    out=high_turn_short(r)
    assert out.iloc[60:63].tolist()==[-1.,-1.,-1.]
    assert out.iloc[63:].eq(0).all()


def test_cycle_retains_on_trend_recovery_but_exits_on_ratio_stop_or_floor():
    r=pd.Series(np.r_[np.linspace(1.6,1.4,60),1.5,1.6])
    s=cycle_short(r)
    assert s.iloc[59]==-1. and s.iloc[60]==-1. and s.iloc[61]==0.
    r2=pd.Series(np.r_[np.linspace(1.6,1.4,60),1.1,1.02])
    assert cycle_short(r2).iloc[-2:].tolist()==[-1.,0.]
    pd.testing.assert_series_equal(cycle_short(r).iloc[:60],cycle_short(r.iloc[:60]))


def test_schedule_uses_prior_oi_and_rolls_without_price_level_jump():
    dates=pd.to_datetime(["2024-01-02","2024-01-03","2024-01-04"])
    rows=[]
    for d,ois in zip(dates,[(20,10),(10,20),(100,1)]):
        for symbol,price,oi in [("IC2401.CFE",100,ois[0]),("IC2402.CFE",200,ois[1])]:
            rows.append({"date":d,"symbol":symbol,"close":price,"oi":oi})
    sc=schedule(pd.DataFrame(rows),dates,["IC"],np.ones((3,1)))
    assert sc["symbols"].IC.tolist()==["","IC2401.CFE","IC2402.CFE"]
    r,c,t=batched_close_ledger(np.array([-1.,-1.,-1.]),sc,cost_bps=3)
    assert r[0,0]==0 and abs(r[1,0]+.0003)<1e-12
    assert abs(r[2,0]+.0006000900270081024)<1e-12
    assert np.allclose(r,-c)


def test_delay_prevents_earning_signal_day_or_execution_day_move():
    sc={"weights":np.ones((4,1)),"new":np.array([[1],[100],[110],[99]]),
        "old_now":np.array([[1],[1],[110],[99]]),"old_previous":np.array([[1],[1],[100],[110]]),
        "rolled":np.array([[False],[True],[False],[False]])}
    r,_,_=batched_close_ledger(np.array([-1.,0.,0.,0.]),sc,cost_bps=0,delay=1)
    np.testing.assert_allclose(r[:,0],[0.,0.,-.1,0.])
    r,_,_=batched_close_ledger(np.array([-1.,0.,0.,0.]),sc,cost_bps=0,delay=2)
    np.testing.assert_allclose(r[:,0],[0.,0.,0.,.1])
