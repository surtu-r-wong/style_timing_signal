"""deploy/failover/failover_legs.py 的灾备件单测（不连库）。

灾备的命题：**官方腿停发时，官方级复刻腿顶上、系统层面几乎无损**（§7.10 实测）。
这里覆盖可离线验证的三层：净值适配器、自建 vs 官方的对比口径、演练判定阈值。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.failover.failover_legs import (  # noqa: E402
    BAND_SPEC,
    READY_POSITION_DIFF,
    READY_SIGNAL_CORR,
    compare_signals,
    nav_from_returns,
    position_of,
    signal_from_legs,
    verdict_of,
)


# ─────────────────────────────── 净值适配器 ───────────────────────────────
def test_nav_from_returns_hand_case():
    r = pd.Series([0.1, -0.1], index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
    nav = nav_from_returns(r)
    assert np.isclose(nav.iloc[0], 1.1)
    assert np.isclose(nav.iloc[1], 1.1 * 0.9)


def test_nav_from_returns_empty_is_safe():
    assert nav_from_returns(pd.Series(dtype=float)).empty


def test_nav_from_returns_drops_na():
    r = pd.Series([0.1, np.nan, 0.1],
                  index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]))
    assert len(nav_from_returns(r)) == 2


# ─────────────────────────────── 仓位口径 ───────────────────────────────
def test_position_is_long_flat_at_theta_zero():
    s = pd.Series([-0.5, 0.0, 0.5])
    assert list(position_of(s)) == [0, 0, 1]        # θ=0 严格大于才持多


def test_position_respects_custom_theta():
    s = pd.Series([0.05, 0.15])
    assert list(position_of(s, theta=0.1)) == [0, 1]


# ─────────────────────────────── 对比口径 ───────────────────────────────
def _sig(vals, start="2026-01-01"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)))


def test_compare_signals_identical_series():
    s = _sig(list(np.linspace(-1, 1, 60)))
    got = compare_signals(s, s)
    assert got["n_obs"] == 60
    assert np.isclose(got["signal_corr"], 1.0)
    assert got["position_diff_ratio"] == 0.0
    assert got["position_last_mine"] == got["position_last_official"] == 1


def test_compare_signals_counts_position_divergence():
    a = _sig([0.5] * 40 + [-0.5] * 20)
    b = _sig([0.5] * 50 + [-0.5] * 10)
    got = compare_signals(a, b)
    assert got["position_diff_ratio"] == round(10 / 60, 4)   # 10 天符号不同


def test_compare_signals_uses_common_window_only():
    a = _sig(list(np.linspace(-1, 1, 60)))
    b = a.iloc[10:]                                          # 官方侧短 10 天
    got = compare_signals(a, b)
    assert got["n_obs"] == 50


def test_compare_signals_refuses_short_window():
    a = _sig([0.1] * 10)
    got = compare_signals(a, a)
    assert got["signal_corr"] is None and "不判" in got["note"]


# ─────────────────────────────── 演练判定 ───────────────────────────────
def test_verdict_ready_matches_section_7_10_readings():
    """§7.10 单带实测（corr 0.886~0.892、分歧 13.1%~15.9%）必须判 READY。"""
    for corr, diff in [(0.892, 0.131), (0.886, 0.159)]:
        got = verdict_of({"signal_corr": corr, "position_diff_ratio": diff})
        assert got["status"] == "READY"


def test_verdict_degraded_when_corr_too_low():
    got = verdict_of({"signal_corr": 0.70, "position_diff_ratio": 0.10})
    assert got["status"] == "DEGRADED" and "复刻管线" in got["reason"]


def test_verdict_degraded_when_position_diff_too_high():
    got = verdict_of({"signal_corr": 0.95, "position_diff_ratio": 0.35})
    assert got["status"] == "DEGRADED"


def test_verdict_inconclusive_on_missing_readings():
    got = verdict_of({"signal_corr": None, "position_diff_ratio": None,
                      "note": "公共窗不足 30 日，不判"})
    assert got["status"] == "INCONCLUSIVE"


def test_thresholds_are_the_registered_values():
    """阈值取自 §7.10 已登记区间，改动须同步改文档与依据。"""
    assert READY_SIGNAL_CORR == 0.85
    assert READY_POSITION_DIFF == 0.20


# ─────────────────────────────── 带定义 ───────────────────────────────
def test_band_spec_matches_production_pairs():
    """灾备必须覆盖生产四对，且官方码与 INDEX_PAIRS 同源。"""
    from signals.style_basket.validate import INDEX_PAIRS
    prod = {name.replace("pair", ""): codes for name, codes in INDEX_PAIRS.items()}
    assert set(BAND_SPEC) == set(prod)
    for band, spec in BAND_SPEC.items():
        assert spec["official"] == prod[band]


def test_signal_from_legs_matches_production_function():
    """信号构造必须逐字复用生产管线的 _signal，不得另造。"""
    from signals.style_basket.decompose import _signal
    idx = pd.bdate_range("2026-01-01", periods=80)
    g = pd.Series(np.linspace(0.001, 0.004, 80), index=idx)
    v = pd.Series(np.linspace(0.004, 0.001, 80), index=idx)
    got = signal_from_legs(g, v)
    want = _signal(nav_from_returns(g), nav_from_returns(v))
    pd.testing.assert_series_equal(got, want)
