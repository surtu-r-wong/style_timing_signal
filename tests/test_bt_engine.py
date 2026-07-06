import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy, segment_returns  # noqa: E402


def test_tplus1_fill_and_full_calendar():
    idx = pd.bdate_range("2020-01-01", periods=4)
    underlying = pd.Series([0.0, 0.10, -0.05, 0.02], index=idx)
    pos = pd.Series([1, 1, 0, -1], index=idx)  # T-close signal
    out = run_strategy(pos, underlying, cost_bps=0, carry=None)
    # position effective T+1: ret[1]=pos_eff[1]*u[1]=1*0.10; ret[3]=0*0.02=0
    assert abs(out["ret"].iloc[1] - 0.10) < 1e-12
    assert abs(out["ret"].iloc[3] - 0.0) < 1e-12


def test_turnover_cost_on_effective_change():
    idx = pd.bdate_range("2020-01-01", periods=3)
    u = pd.Series([0.0, 0.0, 0.0], index=idx)
    pos = pd.Series([0, 1, 1], index=idx)
    out = run_strategy(pos, u, cost_bps=3, carry=None)
    # pos_eff=[0,0,1]; effective change lands day2 → 3bp on iloc[2]
    assert abs(out["ret"].iloc[2] - (-0.0003)) < 1e-12
    assert abs(out["ret"].iloc[1] - 0.0) < 1e-12


def test_carry_long_earns_in_discount():
    idx = pd.bdate_range("2020-01-01", periods=2)
    u = pd.Series([0.0, 0.0], index=idx)
    pos = pd.Series([1, -1], index=idx)
    carry = pd.Series([0.08, 0.08], index=idx)  # 年化贴水率 8% (正=贴水)
    out = run_strategy(pos, u, cost_bps=0, carry=carry)
    assert abs(out["ret"].iloc[1] - 0.08 / 245) < 1e-12  # 持多 earns


def test_carry_short_pays_in_discount():
    idx = pd.bdate_range("2020-01-01", periods=3)
    u = pd.Series([0.0, 0.0, 0.0], index=idx)
    pos = pd.Series([-1, -1, -1], index=idx)
    carry = pd.Series([0.08, 0.08, 0.08], index=idx)
    out = run_strategy(pos, u, cost_bps=0, carry=carry)
    assert abs(out["ret"].iloc[1] - (-0.08 / 245)) < 1e-12  # 持空 pays


def test_segment_returns_split_long_and_short():
    idx = pd.bdate_range("2020-01-01", periods=4)
    u = pd.Series([0.0, 0.10, 0.10, 0.10], index=idx)
    pos = pd.Series([1, -1, 1, 1], index=idx)
    long_ret, short_ret = segment_returns(pos, u, cost_bps=0, carry=None)
    assert abs(long_ret.iloc[1] - 0.10) < 1e-12   # long-only holds day1
    assert abs(long_ret.iloc[2] - 0.0) < 1e-12     # long-only flat day2
    assert abs(short_ret.iloc[2] - (-0.10)) < 1e-12  # short-only holds day2
