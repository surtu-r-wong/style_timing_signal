"""rotation 短窗本质验证（backtest/rotation_probe）：五个纯函数 TDD。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.rotation_probe import (  # noqa: E402
    hold_position,
    nonoverlap_ic,
    partial_rank_ic,
    series_signal,
    shift_permutation_pvalue,
)
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402


def _ret(n=120, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0, 0.01, n), index=pd.bdate_range("2020-01-01", periods=n))


def test_series_signal_matches_pair_signal_on_nav_vs_ones():
    """单序列信号化 ≡ 生产管线 _compute_pair_signal(nav, 常数腿)——零口径漂移锚。"""
    ret = _ret()
    nav = (1.0 + ret).cumprod()
    ones = pd.Series(1.0, index=ret.index)
    expected = _compute_pair_signal(nav, ones, lookback=10, z_window=20)
    got = series_signal(ret, lookback=10, z_window=20, smoothing=0)
    pd.testing.assert_series_equal(got, expected, check_names=False)


def test_series_signal_smoothing_is_rolling_mean():
    ret = _ret(seed=1)
    raw = series_signal(ret, lookback=5, z_window=10, smoothing=0)
    sm = series_signal(ret, lookback=5, z_window=10, smoothing=3)
    pd.testing.assert_series_equal(
        sm, raw.rolling(3, min_periods=1).mean(), check_names=False
    )


def test_nonoverlap_ic_perfect_foresight_is_one():
    """信号=下一块收益和 → 非重叠窗 Spearman = 1；返回 (ic, n_windows)。"""
    idx = pd.bdate_range("2020-01-01", periods=40)
    rng = np.random.default_rng(2)
    ret = pd.Series(rng.normal(0, 0.01, 40), index=idx)
    k = 5
    fwd = ret.rolling(k).sum().shift(-k)
    sig = fwd.fillna(0.0)  # 完美前视信号（测试专用）
    ic, n = nonoverlap_ic(sig, ret, k)
    assert ic == pytest.approx(1.0)
    # 块末采样点 i=k−1, 2k−1, ...，末块无下块 → 40/5 − 1 = 7 个有效窗
    assert n == 7


def test_nonoverlap_ic_windows_do_not_overlap():
    """采样点间隔恰为 k：n_windows = floor(len/k) − 1（末块无前瞻）。"""
    idx = pd.bdate_range("2020-01-01", periods=63)
    rng = np.random.default_rng(5)
    ret = pd.Series(rng.normal(0.001, 0.01, 63), index=idx)
    sig = pd.Series(np.arange(63, dtype=float), index=idx)
    _, n = nonoverlap_ic(sig, ret, 10)
    assert n == 5  # floor(63/10)=6 块 − 1


def test_shift_permutation_detects_perfect_signal():
    """完美前视信号 → 置换 p 极小（≈1/(n+1)）；p∈(0,1]。"""
    idx = pd.bdate_range("2020-01-01", periods=250)
    rng = np.random.default_rng(7)
    ret = pd.Series(rng.normal(0, 0.01, 250), index=idx)
    sig = ret.rolling(5).sum().shift(-5).fillna(0.0)
    p = shift_permutation_pvalue(sig, ret, k=5, n_perm=99, seed=0)
    assert p <= 2.0 / 100.0


def test_shift_permutation_random_signal_not_significant():
    """独立随机信号 → p 大（固定 seed 确定性断言）。"""
    idx = pd.bdate_range("2020-01-01", periods=250)
    rng = np.random.default_rng(11)
    ret = pd.Series(rng.normal(0, 0.01, 250), index=idx)
    sig = pd.Series(rng.normal(0, 1, 250), index=idx)
    p = shift_permutation_pvalue(sig, ret, k=5, n_perm=99, seed=1)
    assert p > 0.05


def test_partial_rank_ic_zero_when_control_explains_signal():
    """sig ≡ control → 控后残差无信息 → 偏 IC ≈ 0。"""
    rng = np.random.default_rng(3)
    control = pd.Series(rng.normal(0, 1, 300))
    fwd = control * 0.5 + pd.Series(rng.normal(0, 1, 300))  # fwd 由 control 驱动
    got = partial_rank_ic(control.copy(), fwd, control)
    assert abs(got) < 1e-6


def test_partial_rank_ic_survives_when_signal_orthogonal():
    """sig ⊥ control 且预测 fwd → 偏 IC ≈ 原始 IC（控不掉）。"""
    rng = np.random.default_rng(4)
    control = pd.Series(rng.normal(0, 1, 500))
    sig = pd.Series(rng.normal(0, 1, 500))  # 与 control 独立
    fwd = sig * 0.5 + pd.Series(rng.normal(0, 1, 500))
    raw = sig.corr(fwd, method="spearman")
    got = partial_rank_ic(sig, fwd, control)
    assert got == pytest.approx(raw, abs=0.05)
    assert got > 0.3


def test_hold_position_rebalances_every_k_days_by_sign():
    """每 k 日按信号符号换仓、其间保持；引擎另做 T+1，不在此重复滞后。"""
    idx = pd.bdate_range("2020-01-01", periods=9)
    sig = pd.Series([0.5, -0.2, 0.1, -0.4, -0.3, 0.2, 0.6, 0.1, -0.9], index=idx)
    got = hold_position(sig, k=3)
    # 换仓点 = 位置 0,3,6：sign(0.5)=+1、sign(-0.4)=−1、sign(0.6)=+1
    assert list(got) == [1, 1, 1, -1, -1, -1, 1, 1, 1]
    assert got.index.equals(idx)


def test_hold_position_zero_signal_means_flat():
    idx = pd.bdate_range("2020-01-01", periods=4)
    got = hold_position(pd.Series([0.0, 1.0, -1.0, 1.0], index=idx), k=2)
    assert list(got) == [0, 0, -1, -1]  # sign(0)=0 平仓两日，再按位置 2 换仓
