"""ERP 轴探针（backtest/erp_probe）：装配纯函数 TDD。

探针机器已在杠杆/温度/多头轴测试覆盖，此处只测 ERP 构造与装配：
erp = 1/PE_TTM − 10Y/100（比例口径对齐）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.erp_probe import build_erp, build_erp_signals  # noqa: E402
from backtest.leverage_probe import level_signal  # noqa: E402


def _fixture(n=400, seed=0):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    pe = pd.Series(18.0 + rng.normal(0, 0.3, n).cumsum(), index=idx).clip(8, 40)
    y10 = pd.Series(2.8 + rng.normal(0, 0.02, n).cumsum() * 0.1, index=idx).clip(1.5, 4.5)
    return pe, y10


def test_build_erp_units_and_alignment():
    """ERP = 1/PE − 10Y/100：PE 23.5x + 10Y 1.65% → ERP ≈ 2.60%；只留共同日。"""
    idx = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])
    pe = pd.Series([23.5, 23.5, 23.5], index=idx)
    y10 = pd.Series([1.65, 1.65], index=idx[:2])  # 百分点口径（EDB 原样）
    erp = build_erp(pe, y10)
    assert len(erp) == 2
    assert erp.iloc[0] == pytest.approx(1 / 23.5 - 0.0165, abs=1e-9)


def test_build_erp_drops_nonpositive_pe():
    """PE ≤ 0（亏损期静态口径可能出现）→ 剔除不产生荒谬倒数。"""
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    erp = build_erp(pd.Series([-5.0, 20.0], index=idx),
                    pd.Series([1.6, 1.6], index=idx))
    assert len(erp) == 1 and erp.index[0] == idx[1]


def test_build_erp_signals_families_and_composition():
    """两族 × lb{5,20}×zw{60,250}：E1=水平、E2=20d 变化；无 pit_lag（末日在）。"""
    pe, y10 = _fixture()
    sigs = build_erp_signals(pe, y10)
    assert set(sigs) == {"E1", "E2"}
    assert all(len(v) == 4 for v in sigs.values())

    erp = build_erp(pe, y10)
    pd.testing.assert_series_equal(
        sigs["E1"]["E1_lb5zw60"], level_signal(erp.dropna(), 5, 60), check_names=False)
    pd.testing.assert_series_equal(
        sigs["E2"]["E2_lb20zw250"],
        level_signal(erp.diff(20).dropna(), 20, 250), check_names=False)
    assert sigs["E1"]["E1_lb5zw60"].index[-1] == erp.index[-1]
