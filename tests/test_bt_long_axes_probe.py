"""多头增强轴探针（backtest/long_axes_probe）：装配纯函数 TDD。

探针机器（level_signal/pick_representative/run_families_probe/三关）已在
杠杆/温度轴测试覆盖，此处只测本模块唯一新增件 build_long_signals。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.long_axes_probe import build_long_signals  # noqa: E402


def _fixture(n=400, seed=0):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    carry = pd.Series(0.08 + rng.normal(0, 0.01, n).cumsum() * 0.02, index=idx)
    breadth = pd.DataFrame({
        "pct_above_ma20": np.clip(0.5 + rng.normal(0, 0.03, n).cumsum() * 0.05, 0, 1),
        "pct_above_ma60": np.clip(0.5 + rng.normal(0, 0.03, n).cumsum() * 0.05, 0, 1),
        "hi_lo_diff20": rng.normal(0, 0.05, n),
        "hi_lo_diff60": rng.normal(0, 0.05, n),
    }, index=idx)
    return carry, breadth


def test_build_long_signals_families_and_forms():
    """五族 × lb{5,20}×zw{60,250} 各 4 形态；命名 <族>_lb<lb>zw<zw>。"""
    carry, breadth = _fixture()
    sigs = build_long_signals(carry, breadth)
    assert set(sigs) == {"C1", "C2", "B1", "B2", "B3"}
    assert all(len(v) == 4 for v in sigs.values())
    assert set(sigs["C1"]) == {"C1_lb5zw60", "C1_lb5zw250", "C1_lb20zw60", "C1_lb20zw250"}


def test_build_long_signals_composition_no_lag():
    """逐族组合恒等：C1=carry 水平、C2=carry.diff(20)、B1=ma20 水平、
    B2=ma20.diff(20)、B3=hi_lo_diff20——全走 level_signal，无 PIT 滞后（末日在）。"""
    carry, breadth = _fixture(seed=1)
    sigs = build_long_signals(carry, breadth)

    pd.testing.assert_series_equal(
        sigs["C1"]["C1_lb5zw60"], level_signal(carry.dropna(), 5, 60),
        check_names=False)
    pd.testing.assert_series_equal(
        sigs["C2"]["C2_lb20zw250"],
        level_signal(carry.diff(20).dropna(), 20, 250), check_names=False)
    pd.testing.assert_series_equal(
        sigs["B1"]["B1_lb5zw250"],
        level_signal(breadth["pct_above_ma20"].dropna(), 5, 250), check_names=False)
    pd.testing.assert_series_equal(
        sigs["B2"]["B2_lb5zw60"],
        level_signal(breadth["pct_above_ma20"].diff(20).dropna(), 5, 60),
        check_names=False)
    pd.testing.assert_series_equal(
        sigs["B3"]["B3_lb20zw60"],
        level_signal(breadth["hi_lo_diff20"].dropna(), 20, 60), check_names=False)
    # 无滞后：末日保留
    assert sigs["C1"]["C1_lb5zw60"].index[-1] == carry.index[-1]


def test_build_long_signals_tolerates_nan_head():
    """breadth 缓存头部有未满窗 NaN（真实文件形态）→ dropna 后正常装配。"""
    carry, breadth = _fixture(seed=2)
    breadth.iloc[:30] = np.nan
    sigs = build_long_signals(carry, breadth)
    b1 = sigs["B1"]["B1_lb5zw60"]
    assert b1.index[0] == breadth.index[30]
    assert not b1.isna().all()
