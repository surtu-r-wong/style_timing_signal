import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.metrics import sharpe  # noqa: E402
from backtest.paired_bootstrap import (  # noqa: E402
    moving_block_indices, paired_block_bootstrap_sharpe_diff, sharpe_matrix,
)


def test_sharpe_matrix_matches_metrics_sharpe():
    rng = np.random.default_rng(0)
    mat = rng.normal(0, 0.01, (5, 300))
    got = sharpe_matrix(mat)
    want = np.array([sharpe(pd.Series(r)) for r in mat])
    assert np.allclose(got, want)


def test_sharpe_matrix_zero_variance_row_is_zero():
    mat = np.zeros((2, 50))
    mat[1] = 0.01
    assert np.allclose(sharpe_matrix(mat), 0.0)


def test_moving_block_indices_shape_and_bounds():
    rng = np.random.default_rng(1)
    idx = moving_block_indices(103, 20, 7, rng)
    assert idx.shape == (7, 103)
    assert idx.min() >= 0 and idx.max() <= 102
    # 块内连续：每 20 个一段递增 1
    assert np.all(np.diff(idx[:, :20], axis=1) == 1)


def test_identical_series_give_zero_diff_and_p_one():
    idx = pd.bdate_range("2020-01-01", periods=400)
    rng = np.random.default_rng(2)
    a = pd.Series(rng.normal(0.0004, 0.01, 400), index=idx)
    out = paired_block_bootstrap_sharpe_diff(a, a.copy(), block=20, n=200, seed=0)
    assert out["diff_sharpe"] == 0.0
    assert np.isclose(out["boot_sd"], 0.0)
    assert out["p_value"] == 1.0  # 全部 |d*−d̂| ≥ 0


def test_pairing_preserved_constant_shift_has_deterministic_sign():
    """b = a 的 0.5 倍 → Sharpe 恒等（尺度不变），配对下差恒为 0。"""
    idx = pd.bdate_range("2020-01-01", periods=400)
    rng = np.random.default_rng(3)
    a = pd.Series(rng.normal(0.0004, 0.01, 400), index=idx)
    out = paired_block_bootstrap_sharpe_diff(a, a * 0.5, block=20, n=200, seed=0)
    assert abs(out["diff_sharpe"]) < 1e-12
    assert abs(out["boot_mean"]) < 1e-12


def test_reproducible_given_seed():
    idx = pd.bdate_range("2020-01-01", periods=400)
    rng = np.random.default_rng(4)
    a = pd.Series(rng.normal(0.0006, 0.01, 400), index=idx)
    b = pd.Series(rng.normal(0.0001, 0.01, 400), index=idx)
    o1 = paired_block_bootstrap_sharpe_diff(a, b, block=20, n=300, seed=7)
    o2 = paired_block_bootstrap_sharpe_diff(a, b, block=20, n=300, seed=7)
    assert o1 == o2
    assert 0 < o1["p_value"] <= 1
    assert o1["ci_lo"] < o1["ci_hi"]          # 分位区间非退化
    assert o1["boot_sd"] > 0


def test_strongly_different_series_reject_null():
    idx = pd.bdate_range("2015-01-01", periods=1500)
    rng = np.random.default_rng(5)
    common = rng.normal(0, 0.01, 1500)
    a = pd.Series(common + 0.002, index=idx)   # 明显更高的均值
    b = pd.Series(common, index=idx)
    out = paired_block_bootstrap_sharpe_diff(a, b, block=20, n=1000, seed=0)
    assert out["diff_sharpe"] > 0
    assert out["p_value"] < 0.05
