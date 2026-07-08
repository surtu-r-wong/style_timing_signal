"""杠杆轴空头信号探针（backtest/leverage_probe）：新纯函数 TDD。

复用件（series_signal/nonoverlap_ic/置换/偏IC/hold_position）已在
test_bt_rotation_probe 覆盖，此处只测本模块新增件。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import (  # noqa: E402
    build_signals,
    level_signal,
    pick_representative,
    pit_lag,
    unit_scale,
    validate_anchors,
)
from backtest.rotation_probe import series_signal  # noqa: E402
from signals.equal_weight.generate_signal import STD_FLOOR  # noqa: E402


def _level(n=400, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(
        0.10 + rng.normal(0, 0.005, n).cumsum() * 0.01,
        index=pd.bdate_range("2020-01-01", periods=n),
    )


# ---------------------------------------------------------------- level_signal
def test_level_signal_mirrors_production_z_tanh():
    """水平序列信号化 = rolling mean(lb) → z(zw) → tanh(z/2)，与生产 z 口径逐值一致。"""
    level = _level()
    lb, zw = 5, 60
    sm = level.rolling(lb, min_periods=1).mean()
    mu = sm.rolling(zw, min_periods=zw).mean()
    sd = sm.rolling(zw, min_periods=zw).std()
    sd = pd.Series(np.where(sd < STD_FLOOR, STD_FLOOR, sd), index=sd.index)
    z = ((sm - mu) / sd).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    expected = pd.Series(np.tanh(z / 2.0), index=level.index)
    got = level_signal(level, lookback=lb, z_window=zw)
    pd.testing.assert_series_equal(got, expected, check_names=False)


def test_level_signal_extreme_high_is_positive_and_bounded():
    """水平量长期平稳后跳升 → 信号显著为正；全程有界 [−1,1]。"""
    idx = pd.bdate_range("2020-01-01", periods=300)
    level = pd.Series(1.0, index=idx) + pd.Series(
        np.random.default_rng(1).normal(0, 0.01, 300), index=idx
    )
    level.iloc[-10:] += 1.0  # 末端跳升
    sig = level_signal(level, lookback=5, z_window=250)
    assert sig.iloc[-1] > 0.5
    assert sig.abs().max() <= 1.0


# ---------------------------------------------------------------- unit_scale
def test_unit_scale_discriminates_row_units():
    """amount/(volume×close)≈0.1 →（千元/手）段 ×1000；≈1 → 元/股段 ×1。"""
    close = pd.Series([244.0, 1185.5])
    volume = pd.Series([61506.0, 3960779.0])          # 手 / 股
    amount = pd.Series([1_530_836.13, 4_684_236_159.0])  # 千元 / 元
    got = unit_scale(amount, volume, close)
    assert list(got) == [1000.0, 1.0]


def test_unit_scale_zero_or_missing_volume_is_nan():
    """停牌/坏行（volume 0 或缺失、close 缺失）→ NaN（聚合时剔除）。"""
    got = unit_scale(
        pd.Series([100.0, 100.0, 100.0]),
        pd.Series([0.0, np.nan, 10.0]),
        pd.Series([5.0, 5.0, np.nan]),
    )
    assert got.isna().all()


# ---------------------------------------------------------------- pit_lag
def test_pit_lag_shifts_one_trading_day():
    """T 日两融 T+1 早公布 → 信号后移一格：新序列在 t 的值 = 原 t−1 值，首日剔除。"""
    idx = pd.bdate_range("2024-01-01", periods=3)
    sig = pd.Series([1.0, 2.0, 3.0], index=idx)
    got = pit_lag(sig)
    assert got.index.equals(idx[1:])
    assert list(got) == [1.0, 2.0]


# ---------------------------------------------------------------- pick_representative
def _panel(rows):
    return pd.DataFrame(
        rows, columns=["form", "k", "ic", "ic_2014-2019", "ic_2020-2026"]
    )


def test_pick_representative_prefers_sign_consistent_worst_half():
    """同号约束下取 worst-half |IC| 最大者；全窗 IC 更高但半窗变号的形态出局。"""
    panel = _panel([
        ("a", 5, 0.30, 0.25, -0.05),   # 半窗变号 → 出局
        ("b", 10, 0.20, 0.15, 0.10),   # worst 0.10
        ("c", 20, 0.18, 0.12, 0.14),   # worst 0.12 ← 代表
    ])
    row, ok = pick_representative(panel)
    assert ok is True
    assert row["form"] == "c"


def test_pick_representative_handles_negative_family():
    """全负族（干柴先验方向）按 |IC| 逻辑同样成立。"""
    panel = _panel([
        ("a", 5, -0.30, -0.25, 0.05),   # 变号出局
        ("b", 10, -0.20, -0.15, -0.10),
        ("c", 20, -0.18, -0.12, -0.14),  # worst |0.12| ← 代表
    ])
    row, ok = pick_representative(panel)
    assert ok is True
    assert row["form"] == "c"
    assert row["ic"] < 0


def test_pick_representative_no_consistent_candidate_flags_fail():
    """无同号形态 → ok=False，返回 |IC| 最大行仅作存档。"""
    panel = _panel([
        ("a", 5, 0.30, 0.25, -0.05),
        ("b", 10, -0.20, 0.15, -0.10),
    ])
    row, ok = pick_representative(panel)
    assert ok is False
    assert row["form"] == "a"


# ---------------------------------------------------------------- validate_anchors
def test_validate_anchors_pass_within_tolerance():
    s = pd.Series(
        [1.46e12, 3.25e12],
        index=pd.to_datetime(["2015-06-18", "2026-06-30"]),
    )
    validate_anchors(s, {"2015-06-18": 1.4e12, "2026-06-30": 3.4e12}, rel_tol=0.15)


def test_validate_anchors_raises_on_deviation_or_missing():
    s = pd.Series([1.46e9], index=pd.to_datetime(["2015-06-18"]))  # 千倍错位
    with pytest.raises(ValueError):
        validate_anchors(s, {"2015-06-18": 1.4e12}, rel_tol=0.15)
    with pytest.raises(ValueError):
        validate_anchors(s, {"2015-06-19": 1.0}, rel_tol=0.15)


# ---------------------------------------------------------------- build_signals
def test_build_signals_composition_and_pit():
    """三族装配 = 文档定义的逐步组合，且全部 PIT 后移一格。"""
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(9)
    bal = pd.Series(1e8 * (1 + rng.normal(0.001, 0.01, 300)).cumprod(), index=idx)
    buy = pd.Series(1e6 * (1 + rng.normal(0, 0.05, 300)).cumprod(), index=idx)
    amt = pd.Series(1e12 * (1 + rng.normal(0, 0.03, 300)).cumprod(), index=idx)

    sigs = build_signals(bal, buy, amt)
    assert set(sigs) == {"L1", "L2", "L3p"}

    # L1：余额 pct_change 喂 series_signal（zw=2lb）再 PIT
    exp_l1 = pit_lag(series_signal(bal.pct_change().dropna(), 5, 10, 0))
    pd.testing.assert_series_equal(
        sigs["L1"]["L1_lb5sm0"], exp_l1, check_names=False
    )

    # L2：买入额(万元→元)/成交额 → level_signal 再 PIT
    ratio = (buy * 1e4 / amt).dropna()
    exp_l2 = pit_lag(level_signal(ratio, 5, 60))
    pd.testing.assert_series_equal(
        sigs["L2"]["L2_lb5zw60"], exp_l2, check_names=False
    )

    # L3'：余额(万元→元)/20日平均成交额 → level_signal 再 PIT
    ratio3 = (bal * 1e4 / amt.rolling(20).mean()).dropna()
    exp_l3 = pit_lag(level_signal(ratio3, 20, 250))
    pd.testing.assert_series_equal(
        sigs["L3p"]["L3p_lb20zw250"], exp_l3, check_names=False
    )

    # 形态数：L1 = 4lb×2sm = 8；L2/L3' 各 = 2lb×2zw = 4
    assert len(sigs["L1"]) == 8 and len(sigs["L2"]) == 4 and len(sigs["L3p"]) == 4
