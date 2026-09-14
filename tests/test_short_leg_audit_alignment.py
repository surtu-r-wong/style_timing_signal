import pandas as pd
import pytest
from backtest.short_leg_audit import carry_income


def test_carry_uses_effective_position_and_keeps_pre_window_signal():
    dates=pd.date_range('2020-01-01',periods=3)
    signal=pd.Series([1.,-1.,1.],index=dates)
    carry=pd.Series([.1,.2,.3],index=dates)
    assert carry_income(signal,carry,dates[1],dates[2])==pytest.approx(-.05)
