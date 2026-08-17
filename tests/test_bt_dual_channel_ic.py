"""IC 口径重跑的判例（纯函数，不连库）。

核心是那个**Spearman 对称性**技巧：既有 `paired_ic_bootstrap(x_a, x_b, y)` 测
"两因子 vs 一目标"，而这里要"一因子 vs 两目标"。若对称性不成立，整个 §7 的结论
就建在错的复用上，所以必须钉死。
"""
import numpy as np
import pandas as pd
import pytest
from scipy import stats

from backtest.dual_channel_ic import K_MAIN, K_REPORT, ic_frame
from backtest.fusion_probe import paired_ic_bootstrap


def _series(n=200, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    x = pd.Series(rng.normal(size=n), index=idx)
    y_a = pd.Series(0.3 * x.to_numpy() + rng.normal(size=n), index=idx)
    y_b = pd.Series(0.1 * x.to_numpy() + rng.normal(size=n), index=idx)
    return x, y_a, y_b


def test_spearman_symmetry_underpins_the_reuse():
    """IC(x, y_a) − IC(x, y_b) 必须等于 Spearman(y_a, x) − Spearman(y_b, x)。

    这条成立，才能把两条**目标收益**当"因子"传进 `paired_ic_bootstrap`。
    """
    x, y_a, y_b = _series()
    direct = (float(stats.spearmanr(x, y_a).statistic)
              - float(stats.spearmanr(x, y_b).statistic))
    swapped = (float(stats.spearmanr(y_a, x).statistic)
               - float(stats.spearmanr(y_b, x).statistic))
    assert direct == pytest.approx(swapped, rel=1e-12)


def test_paired_ic_bootstrap_reproduces_the_swapped_point_estimate():
    """按对称性技巧调用既有机器，其 `diff_ic` 应等于直接算的 IC 差。"""
    x, y_a, y_b = _series()
    out = paired_ic_bootstrap(y_a, y_b, x, n=200, seed=0)
    direct = (float(stats.spearmanr(x, y_a).statistic)
              - float(stats.spearmanr(x, y_b).statistic))
    assert out["diff_ic"] == pytest.approx(direct, rel=1e-12)
    assert out["n_obs"] == len(x)


def test_ic_frame_uses_forward_return_and_nonoverlap_grid():
    """k=1 时前瞻收益 = 次日收益，且样本点数 = 有效行数（k=1 无重叠可去）。"""
    idx = pd.date_range("2021-01-04", periods=6, freq="B")
    signal = pd.Series([1.0, 2, 3, 4, 5, 6], index=idx)
    target = pd.Series([0.01, 0.02, -0.01, 0.03, 0.00, 0.05], index=idx)
    x, y = ic_frame(signal, target, k=1)
    # 末行前瞻缺失 → 被 dropna 掉
    assert len(x) == 5 and len(y) == 5
    assert y.iloc[0] == pytest.approx(0.02)     # t 处 = t+1 的收益
    assert x.iloc[0] == 1.0


def test_ic_frame_nonoverlap_thins_for_k_gt_1():
    """k>1 时非重叠网格每 k 行取一个 → 样本数约为 1/k，保证近独立。"""
    idx = pd.date_range("2021-01-04", periods=60, freq="B")
    signal = pd.Series(np.arange(60, dtype=float), index=idx)
    target = pd.Series(np.linspace(-0.01, 0.01, 60), index=idx)
    x1, _ = ic_frame(signal, target, k=1)
    x5, _ = ic_frame(signal, target, k=5)
    assert len(x5) == pytest.approx(len(x1) / 5, abs=2)
    # 相邻样本点间隔 5 个交易日 → 前瞻窗互不重叠
    gaps = np.diff(x5.index.map(lambda t: idx.get_loc(t)).to_numpy())
    assert set(gaps) == {5}


def test_k_main_is_one_and_report_ks_exclude_it():
    """k=1 是唯一主判据；报告用的 k 不得把 k=1 重复进来（防事后选优）。"""
    assert K_MAIN == 1
    assert K_MAIN not in K_REPORT
    assert all(k > 1 for k in K_REPORT)
