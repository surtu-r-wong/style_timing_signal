"""涨停温度计轴探针（backtest/thermo_probe）：新纯函数 TDD。

探针机器（level_signal/pick_representative/三关）已在 leverage/rotation 测试
覆盖，此处只测本模块新增件：板别限幅规则、封板/炸板判定（half-up 舍入）、
日度聚合（溢价 PIT 日期戳）、四族装配（无 pit_lag）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.thermo_probe import (  # noqa: E402
    board_limit_pct,
    build_thermo_signals,
    classify_limit,
    thermometer_measures,
)


# ---------------------------------------------------------------- board_limit_pct
def test_board_limit_pct_rules():
    """创业板 2020-08-24 切 10%→20%；科创恒 20%；主板恒 10%。"""
    codes = pd.Series(["300750.SZ", "300750.SZ", "688111.SH", "600519.SH", "000001.SZ"])
    dates = pd.Series(pd.to_datetime(
        ["2020-08-21", "2020-08-24", "2015-01-05", "2024-01-08", "2024-01-08"]
    ))
    got = board_limit_pct(codes, dates)
    assert list(got) == [0.10, 0.20, 0.20, 0.10, 0.10]


# ---------------------------------------------------------------- classify_limit
def test_classify_limit_seal_burst_notouch():
    """封板=收在限价；炸板=触限回落；未触=双 False。"""
    pre = pd.Series([10.0, 10.0, 10.0])
    high = pd.Series([11.0, 11.0, 10.8])
    close = pd.Series([11.0, 10.5, 10.8])
    pct = pd.Series([0.10, 0.10, 0.10])
    sealed, burst = classify_limit(pre, high, close, pct)
    assert list(sealed) == [True, False, False]
    assert list(burst) == [False, True, False]


def test_classify_limit_half_up_rounding():
    """交易所四舍五入：2.35×1.1=2.585 → 限价 2.59（Python banker 舍入会错给 2.58）。"""
    pre = pd.Series([2.35])
    high = pd.Series([2.59])
    close = pd.Series([2.59])
    sealed, burst = classify_limit(pre, high, close, pd.Series([0.10]))
    assert bool(sealed.iloc[0]) is True
    # 反向守卫：2.58 不是限价，不得误判封板
    sealed2, _ = classify_limit(pre, pd.Series([2.58]), pd.Series([2.58]), pd.Series([0.10]))
    assert bool(sealed2.iloc[0]) is False


def test_classify_limit_unlimited_day_excluded():
    """无涨跌幅日（上市首日等）high 越过理论限价 → 等值判定自然排除。"""
    sealed, burst = classify_limit(
        pd.Series([10.0]), pd.Series([14.4]), pd.Series([13.0]), pd.Series([0.10])
    )
    assert not bool(sealed.iloc[0]) and not bool(burst.iloc[0])


# ---------------------------------------------------------------- thermometer_measures
def _fixture():
    """A/B 两主板股 × 3 日：A d1 封板、d2 炸板；B d2 封板。"""
    d1, d2, d3 = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    rows = [
        # ts_code, date, pre_close, high, close
        ("600001.SH", d1, 10.00, 11.00, 11.00),   # A 封板
        ("600001.SH", d2, 11.00, 12.10, 11.44),   # A 触 12.10 回落 → 炸板；ret=+4%
        ("600001.SH", d3, 11.44, 11.60, 11.50),
        ("000002.SZ", d1, 10.00, 10.50, 10.20),   # B 未触
        ("000002.SZ", d2, 10.20, 11.22, 11.22),   # B 封板
        ("000002.SZ", d3, 11.22, 12.00, 11.78),   # ret≈+4.99%
    ]
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "pre_close", "high", "close"])


def test_thermometer_measures_counts_and_rates():
    out = thermometer_measures(_fixture())
    d1, d2, d3 = out.index
    assert out.loc[d1, "n_sealed"] == 1 and out.loc[d1, "n_burst"] == 0
    assert out.loc[d2, "n_sealed"] == 1 and out.loc[d2, "n_burst"] == 1
    assert out.loc[d2, "n_active"] == 2
    assert out.loc[d1, "lu_ratio"] == pytest.approx(0.5)
    assert out.loc[d2, "burst_rate"] == pytest.approx(0.5)  # 1/(1+1)
    assert out.loc[d3, "n_sealed"] == 0


def test_thermometer_measures_premium_dated_on_realization_day():
    """涨停溢价 PIT：昨日封板股的今日收益，日期戳=今日（实现日）。"""
    out = thermometer_measures(_fixture())
    d1, d2, d3 = out.index
    assert np.isnan(out.loc[d1, "lu_premium"])          # 无前日封板
    assert out.loc[d2, "lu_premium"] == pytest.approx(0.04)          # A: 11.44/11−1
    assert out.loc[d3, "lu_premium"] == pytest.approx(11.78 / 11.22 - 1)  # B
    # 关键：A 的 d2 收益绝不能出现在 d1（前视）
    assert not np.isnan(out.loc[d2, "lu_premium"])


def test_thermometer_measures_burst_rate_nan_when_no_touch():
    """全日无触板 → 炸板率 0/0 = NaN（非 0，避免污染 z）。"""
    d = pd.to_datetime(["2024-01-02"])
    df = pd.DataFrame(
        [("600001.SH", d[0], 10.0, 10.2, 10.1)],
        columns=["ts_code", "trade_date", "pre_close", "high", "close"],
    )
    out = thermometer_measures(df)
    assert np.isnan(out["burst_rate"].iloc[0])


# ---------------------------------------------------------------- build_thermo_signals
def test_build_thermo_signals_composition_without_pit_lag():
    """四族×4 形态；当日收盘即知 → 不做 pit_lag（末日保留、逐值=level_signal 直出）。"""
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(3)
    thermo = pd.DataFrame({
        "lu_ratio": np.clip(rng.normal(0.01, 0.005, 300), 0, 1),
        "burst_rate": np.clip(rng.normal(0.3, 0.1, 300), 0, 1),
        "lu_premium": rng.normal(0.01, 0.02, 300),
    }, index=idx)
    amt = pd.Series(1e12 * (1 + rng.normal(0, 0.03, 300)).cumprod(), index=idx)

    sigs = build_thermo_signals(thermo, amt)
    assert set(sigs) == {"F1", "F2", "F3", "F4"}
    assert all(len(v) == 4 for v in sigs.values())

    exp_f1 = level_signal(thermo["lu_ratio"].dropna(), 5, 60)
    pd.testing.assert_series_equal(sigs["F1"]["F1_lb5zw60"], exp_f1, check_names=False)
    assert sigs["F1"]["F1_lb5zw60"].index[-1] == idx[-1]  # 无滞后：末日在

    exp_f4 = level_signal(np.log(amt).dropna(), 20, 250)
    pd.testing.assert_series_equal(sigs["F4"]["F4_lb20zw250"], exp_f4, check_names=False)
