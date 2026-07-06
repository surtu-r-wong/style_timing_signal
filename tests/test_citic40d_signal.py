"""citic40d 参数化 + scan 因子函数的单元测试。

目标：把硬编码的 N_LIST=[20] / Z_WINDOW=40 暴露为可传参、并新增带平滑的
等权均值因子（compute_mean_factor），供 walk-forward 参数扫描（设计稿 §3.2）调用。
护栏：默认签名产出必须与现有生产输出逐值一致（字节回归）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import signals.citic40d.generate_signal as cs  # noqa: E402

COLS = ["stability", "growth", "finance", "cycle", "consumption"]
EXPECTED_FACTOR_NAMES = [
    "growth_stability",
    "cycle_consumption",
    "finance_stability",
    "offensive_defensive",
    "wide_off_def",
]


def _synthetic_style(n_rows: int = 90, seed: int = 0) -> pd.DataFrame:
    """五列随机游走指数点位（模拟中信风格 5 序列）。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n_rows)
    data = {}
    for i, col in enumerate(COLS):
        rets = rng.normal(0.0003, 0.012, n_rows)
        data[col] = 100.0 * np.exp(np.cumsum(rets)) * (1 + 0.1 * i)
    return pd.DataFrame(data, index=idx)


def test_build_all_factors_accepts_custom_n_list_and_z_window():
    style = _synthetic_style()
    factors = cs.build_all_factors(style, n_list=[5], z_window=10)

    assert list(factors.columns) == [f"{name}_5" for name in EXPECTED_FACTOR_NAMES]
    expected = cs.compute_spread_factor(style["growth"], style["stability"], 5, 10)
    pd.testing.assert_series_equal(factors["growth_stability_5"], expected, check_names=False)


def test_build_all_factors_default_signature_unchanged():
    """无参调用产出 _20 后缀、m=40 —— 保证生产默认路径字节不变。"""
    style = _synthetic_style()
    factors = cs.build_all_factors(style)

    assert list(factors.columns) == [f"{name}_20" for name in EXPECTED_FACTOR_NAMES]
    expected = cs.compute_spread_factor(style["growth"], style["stability"], 20, 40)
    pd.testing.assert_series_equal(factors["growth_stability_20"], expected, check_names=False)


def test_compute_mean_factor_equals_unsmoothed_mean_of_five_factors():
    style = _synthetic_style()
    mean = cs.compute_mean_factor(style, n=20, z_window=40, smoothing=0)
    expected = cs.build_all_factors(style, n_list=[20], z_window=40).mean(axis=1)
    pd.testing.assert_series_equal(mean, expected, check_names=False)


def test_compute_mean_factor_applies_smoothing():
    style = _synthetic_style()
    raw = cs.compute_mean_factor(style, n=5, z_window=10, smoothing=0)
    smoothed = cs.compute_mean_factor(style, n=5, z_window=10, smoothing=3)
    expected = raw.rolling(3, min_periods=1).mean()
    pd.testing.assert_series_equal(smoothed, expected, check_names=False)


def test_compute_mean_factor_default_reproduces_build_output_factor20():
    """默认参数下 compute_mean_factor 复现生产默认输出的 factor_20（四舍五入逐值一致）。"""
    style = _synthetic_style()
    out = cs.build_output(style)  # 默认 N_LIST=[20]/Z_WINDOW=40/dropna/round4
    mean = cs.compute_mean_factor(style).round(4).reindex(out.index)
    pd.testing.assert_series_equal(mean, out["factor_20"], check_names=False)
