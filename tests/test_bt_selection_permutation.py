"""候选 ⓪ 选择校正置换机器的校准 TDD。

核心验收（任务书 §2）：
1. 合成 null（无真效应 + 多变体网格）→ 校正 p 近均匀（KS + 分位数容差），
   **且同一 null 上旧做法（只对赢家算 naive p）的 p 显著偏小**——"机器修好了"的对照证据；
2. 合成强效应 → 校正后 p 仍小（功效未被摧毁）；
3. 退化用例：单变体网格退化为普通置换检验，且与 `rotation_probe.shift_permutation_pvalue`
   / `significance.bootstrap_pvalue` **逐位精确一致**（同 seed 同移位流）。

全部判例使用固定 seed → 确定性通过/失败，不存在偶发红灯。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.selection_permutation import (  # noqa: E402
    argmax_select, build_index_matrix, column_pvalues, identity_index,
    make_batch_stat_fn, make_stat_fn, moving_block_index_matrix,
    rotation_index_matrix, selection_permutation_test, statistic_matrix,
)

N = 512


# ---------------------------------------------------------------- 合成数据工具
def circ_smooth(x: np.ndarray, w: int) -> np.ndarray:
    """环形移动平均：保证"移位后的变体 = 移位后源的变体"精确成立（见模块 docstring）。"""
    k = np.zeros(len(x))
    k[:min(w, len(x))] = 1.0 / w
    return np.real(np.fft.ifft(np.fft.fft(x) * np.fft.fft(k)))


def synthetic_grid(src: np.ndarray, n_var: int, causal: bool = False) -> dict:
    """近似真实 lb × zw 网格的带通变换族（跨尺度相关性弱、同尺度相关性高）。"""
    specs = [(lb, zw) for lb in (2, 3, 5, 8, 13, 21, 34, 55) for zw in (1, 2, 3, 5, 8)]
    out = {}
    for lb, zw in specs[:n_var]:
        w2 = min(lb * (zw + 1), len(src) // 4)
        if causal:
            s = pd.Series(src)
            out[(lb, zw)] = (s.rolling(lb, min_periods=1).mean()
                             - s.rolling(w2, min_periods=1).mean()).to_numpy()
        else:
            out[(lb, zw)] = circ_smooth(src, lb) - circ_smooth(src, w2)
    return out


def batch_corr(target: np.ndarray):
    """向量化 Pearson 相关：`(m, n) 信号矩阵 → (m,)`（越大越好，单侧口径）。"""
    y = target - target.mean()
    yn = np.sqrt((y ** 2).sum())

    def _f(mat: np.ndarray, variant) -> np.ndarray:
        x = mat - mat.mean(axis=1, keepdims=True)
        return (x @ y) / (np.sqrt((x ** 2).sum(axis=1)) * yn)
    return _f


def scalar_corr(target: np.ndarray):
    def _f(vals: np.ndarray, variant) -> float:
        return float(np.corrcoef(vals, target)[0, 1])
    return _f


def ks_stat(p: np.ndarray) -> float:
    """经验分布对 Uniform(0,1) 的 KS 距离。"""
    p = np.sort(np.asarray(p, dtype=float))
    m = len(p)
    return float(max(np.max(np.arange(1, m + 1) / m - p), np.max(p - np.arange(m) / m)))


def null_experiment(n_rep: int, n_perm: int, n_var: int, effect: float = 0.0,
                    effect_variant=None, causal: bool = False, seed0: int = 0):
    """重复 n_rep 次"造数据 → 跑机器"，返回 (校正 p, naive p, min-P p) 三条数组。"""
    ps, pn, pm = [], [], []
    for r in range(n_rep):
        rng = np.random.default_rng(seed0 + r)
        src = circ_smooth(rng.normal(size=N), 3)
        tgt = rng.normal(size=N)
        sigs = synthetic_grid(src, n_var, causal=causal)
        if effect:
            drive = sigs[effect_variant]
            tgt = tgt + effect * drive / drive.std()
        res = selection_permutation_test(
            list(sigs), n_obs=N, batch_stat_fn=make_batch_stat_fn(sigs, batch_corr(tgt)),
            n_perm=n_perm, seed=seed0 + 90_000 + r)
        ps.append(res.p_selected)
        pn.append(res.p_naive)
        pm.append(res.p_min_p)
    return np.array(ps), np.array(pn), np.array(pm)


# ---------------------------------------------------------------- 索引采样机制
def test_rotation_index_matrix_reproduces_np_roll():
    rng = np.random.default_rng(0)
    idx = rotation_index_matrix(50, 6, rng, min_shift=1)
    x = np.arange(50) * 1.0
    rng2 = np.random.default_rng(0)
    shifts = rng2.integers(1, 50, size=6)
    for b, s in enumerate(shifts):
        assert np.array_equal(x[idx[b]], np.roll(x, s))


def test_rotation_shift_stream_matches_sequential_house_convention():
    """批量抽 shift 与仓内逐次 `rng.integers(lo, hi)` 是同一条流 —— 精确一致性的前提。"""
    lo, hi, n_perm = 6, 494, 25
    rng_seq = np.random.default_rng(3)
    seq = [int(rng_seq.integers(lo, hi)) for _ in range(n_perm)]
    idx = rotation_index_matrix(500, n_perm, np.random.default_rng(3),
                                min_shift=lo, max_shift=hi)
    x = np.arange(500) * 1.0
    for b, s in enumerate(seq):
        assert np.array_equal(x[idx[b]], np.roll(x, s))


def test_rotation_index_matrix_rejects_bad_bounds():
    rng = np.random.default_rng(0)
    for bad in [dict(min_shift=0), dict(min_shift=10, max_shift=10),
                dict(min_shift=1, max_shift=101)]:
        try:
            rotation_index_matrix(100, 3, rng, **bad)
        except ValueError:
            continue
        raise AssertionError(f"should have raised for {bad}")


def test_moving_block_index_matrix_matches_paired_bootstrap():
    from backtest.paired_bootstrap import moving_block_indices
    mine = moving_block_index_matrix(103, 20, 7, np.random.default_rng(1))
    theirs = moving_block_indices(103, 20, 7, np.random.default_rng(1))
    assert np.array_equal(mine, theirs)
    assert mine.shape == (7, 103)


def test_build_index_matrix_deterministic_and_validated():
    a = build_index_matrix(200, 10, scheme="rotation", seed=5)
    b = build_index_matrix(200, 10, scheme="rotation", seed=5)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, build_index_matrix(200, 10, scheme="rotation", seed=6))
    assert build_index_matrix(200, 10, scheme="block", seed=5, block=20).shape == (10, 200)
    try:
        build_index_matrix(200, 10, scheme="iid", seed=0)
    except ValueError:
        return
    raise AssertionError("unknown scheme should raise")


# ---------------------------------------------------------------- 统计量矩阵
def test_batch_and_scalar_paths_agree():
    rng = np.random.default_rng(7)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 6)
    idx = build_index_matrix(N, 15, seed=2)
    a = statistic_matrix(list(sigs), idx, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)))
    b = statistic_matrix(list(sigs), idx,
                         batch_stat_fn=make_batch_stat_fn(sigs, batch_corr(tgt)))
    assert np.allclose(a, b, atol=1e-12)


def test_observed_row_goes_through_identity_index():
    """观测行必须与置换行走同一条 stat_fn 代码路径（可交换性的前提）。"""
    rng = np.random.default_rng(8)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 4)
    res = selection_permutation_test(
        list(sigs), n_obs=N, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
        n_perm=5, seed=0)
    direct = np.array([scalar_corr(tgt)(np.asarray(v)[identity_index(N)], k)
                       for k, v in sigs.items()])
    assert np.allclose(res.observed, direct, atol=1e-12)


def test_column_pvalues_are_discrete_uniform_ranks():
    pool = np.array([[3.0], [1.0], [2.0], [4.0]])
    got = column_pvalues(pool).ravel()
    assert np.allclose(got, [2 / 4, 4 / 4, 3 / 4, 1 / 4])
    tied = np.array([[1.0], [1.0], [0.0]])
    assert np.allclose(column_pvalues(tied).ravel(), [2 / 3, 2 / 3, 3 / 3])


# ---------------------------------------------------------------- ① null 校准（核心）
def test_null_max_statistic_is_uniform_and_naive_p_is_inflated():
    """合成 null · 16 点网格：校正 p 近均匀；旧做法 naive p 假阳率被选择效应顶高数倍。"""
    ps, pn, pm = null_experiment(n_rep=300, n_perm=199, n_var=16, seed0=1_000)
    rate_corr = float(np.mean(ps <= 0.05))
    rate_naive = float(np.mean(pn <= 0.05))

    # (a) 校正 p 近均匀：名义 5% 附近 + KS 过 α=0.01 临界（1.63/√R）
    assert 0.01 <= rate_corr <= 0.11, rate_corr
    assert 0.05 <= float(np.mean(ps <= 0.10)) <= 0.17
    assert 0.15 <= float(np.mean(ps <= 0.25)) <= 0.36
    assert ks_stat(ps) < 1.63 / np.sqrt(len(ps)), ks_stat(ps)
    assert 0.42 <= ps.mean() <= 0.58, ps.mean()

    # (b) 旧做法在同一 null 上系统性偏小 —— 这是"机器修好了"的对照证据
    assert rate_naive > 0.12, rate_naive
    assert rate_naive > 3.0 * rate_corr, (rate_naive, rate_corr)
    assert pn.mean() < ps.mean() - 0.20, (pn.mean(), ps.mean())
    assert ks_stat(pn) > 2.5 * ks_stat(ps), (ks_stat(pn), ks_stat(ps))

    # (c) min-P 备用口径同样校准
    assert 0.01 <= float(np.mean(pm <= 0.05)) <= 0.11
    assert ks_stat(pm) < 1.63 / np.sqrt(len(pm))


def test_naive_inflation_grows_with_grid_size():
    """选择效应随网格点数单调变重（4 → 16 点），而校正 p 一直守在名义水平。"""
    small = null_experiment(n_rep=200, n_perm=199, n_var=4, seed0=2_000)
    large = null_experiment(n_rep=200, n_perm=199, n_var=16, seed0=2_000)
    naive_small, naive_large = np.mean(small[1] <= 0.05), np.mean(large[1] <= 0.05)
    corr_small, corr_large = np.mean(small[0] <= 0.05), np.mean(large[0] <= 0.05)
    assert naive_large > naive_small + 0.05, (naive_small, naive_large)
    assert corr_small <= 0.12 and corr_large <= 0.12, (corr_small, corr_large)


def test_null_calibration_survives_causal_rolling_variants():
    """真实探针用的是**因果**滚动变换（非环形）→ 循环移位只近似可交换。

    此判例把边缘效应量化：容差放宽，但校正 p 仍守在名义水平附近，naive 仍偏小。
    """
    ps, pn, _ = null_experiment(n_rep=200, n_perm=199, n_var=16, causal=True, seed0=3_000)
    assert float(np.mean(ps <= 0.05)) <= 0.12
    assert ks_stat(ps) < 0.14, ks_stat(ps)
    assert float(np.mean(pn <= 0.05)) > 2.0 * float(np.mean(ps <= 0.05))


# ---------------------------------------------------------------- ② 功效
def test_power_survives_correction():
    """强真效应：校正后 p 仍显著（功效未被校正摧毁）。"""
    effect_variant = list(synthetic_grid(np.zeros(N) + np.arange(N) * 0.0, 16))[7]
    ps, pn, pm = null_experiment(n_rep=60, n_perm=199, n_var=16, effect=0.30,
                                 effect_variant=effect_variant, seed0=4_000)
    assert float(np.mean(ps <= 0.05)) >= 0.90, float(np.mean(ps <= 0.05))
    assert float(np.median(ps)) <= 0.02
    assert float(np.mean(pm <= 0.05)) >= 0.75
    assert float(np.mean(pn <= 0.05)) >= float(np.mean(ps <= 0.05))  # naive 只会更松


def test_weak_effect_correction_costs_power_but_stays_directional():
    effect_variant = list(synthetic_grid(np.zeros(N), 16))[7]
    ps, _, _ = null_experiment(n_rep=60, n_perm=199, n_var=16, effect=0.12,
                               effect_variant=effect_variant, seed0=5_000)
    assert 0.30 <= float(np.mean(ps <= 0.05)) <= 1.0
    assert float(np.median(ps)) < 0.25


# ---------------------------------------------------------------- ③ 退化与口径一致性
def test_single_variant_grid_collapses_to_plain_permutation():
    rng = np.random.default_rng(11)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = {("only",): src}
    res = selection_permutation_test(
        list(sigs), n_obs=N, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
        n_perm=199, seed=12)
    assert res.p_selected == res.p_naive == res.p_min_p
    assert res.selection_inflation == 1.0
    assert res.null_winner_counts.tolist() == [199]


def test_matches_rotation_probe_shift_permutation_pvalue_exactly():
    """单变体 + 同 seed 同移位区间 → 与既有 rotation 口径**逐位精确一致**。"""
    from backtest.rotation_probe import nonoverlap_ic, shift_permutation_pvalue

    idx = pd.bdate_range("2015-01-01", periods=600)
    rng = np.random.default_rng(13)
    sig = pd.Series(circ_smooth(rng.normal(size=600), 20), index=idx)
    ret = pd.Series(rng.normal(0, 0.01, 600), index=idx)
    k, n_perm, seed = 5, 200, 4

    theirs = shift_permutation_pvalue(sig, ret, k, n_perm=n_perm, seed=seed)

    vals = sig.to_numpy()

    def stat(variant, i):
        ic, _ = nonoverlap_ic(pd.Series(vals[i], index=idx), ret, k)
        return abs(ic) if np.isfinite(ic) else -np.inf

    mine = selection_permutation_test(
        ["only"], n_obs=600, stat_fn=stat, n_perm=n_perm, seed=seed,
        scheme="rotation", min_shift=2 * k, max_shift=600 - 2 * k)
    assert mine.p_selected == theirs
    assert mine.p_naive == theirs


def test_matches_significance_bootstrap_pvalue_exactly():
    """单变体 + 默认移位区间 [1, n) → 与 `significance.bootstrap_pvalue` 精确一致。"""
    from backtest.significance import bootstrap_pvalue, strategy_metric

    idx = pd.bdate_range("2016-01-01", periods=400)
    rng = np.random.default_rng(14)
    pos = pd.Series(np.sign(rng.normal(size=400)), index=idx)
    und = pd.Series(rng.normal(0.0003, 0.01, 400), index=idx)
    n_perm, seed = 150, 2

    theirs = bootstrap_pvalue(pos, und, metric="sharpe", n=n_perm, seed=seed)
    vals = pos.to_numpy()

    def stat(variant, i):
        return strategy_metric(pd.Series(vals[i], index=idx), und, "sharpe")

    mine = selection_permutation_test(["only"], n_obs=400, stat_fn=stat,
                                      n_perm=n_perm, seed=seed, scheme="rotation")
    assert mine.p_selected == theirs


# ---------------------------------------------------------------- 选优规则与可行性
def test_select_fn_receives_full_grid_and_infeasible_encodes_as_neg_inf():
    """把"可行性约束"编码成 −inf（leverage_probe 的同号规则即此形），观测无可行 → p=1。"""
    sigs = {"a": np.zeros(64), "b": np.zeros(64)}

    def stat(variant, i):
        return -np.inf

    res = selection_permutation_test(list(sigs), n_obs=64, stat_fn=stat,
                                     n_perm=20, seed=0)
    assert res.best_index == -1 and res.best_variant is None
    assert res.p_selected == 1.0
    assert argmax_select(np.array([-np.inf, -np.inf])) == -1


def test_custom_select_fn_is_applied_to_every_row():
    """自定义选优规则（"取次大"）——观测行与置换行必须走同一条规则。"""
    rng = np.random.default_rng(15)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 5)

    def second_best(stats):
        order = np.argsort(stats)
        return int(order[-2])

    res = selection_permutation_test(
        list(sigs), n_obs=N, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
        n_perm=99, seed=1, select_fn=second_best)
    assert res.observed_best == sorted(res.observed)[-2]
    assert res.null_selected.shape == (99,)
    assert 0 < res.p_selected <= 1


def test_min_p_beats_max_stat_when_variant_null_scales_differ():
    """尺度不齐时 max-statistic 被"大方差变体"霸占，min-P 仍能捞回真信号。

    统计量取**协方差**（非尺度不变）：一个振幅 20× 的噪声变体主导 max，
    真效应藏在安静变体里 → p_min_p 明显小于 p_selected。
    """
    rng = np.random.default_rng(16)
    quiet = circ_smooth(rng.normal(size=N), 4)
    quiet = quiet / quiet.std()
    tgt = rng.normal(size=N) + 0.45 * quiet
    sigs = {"quiet": quiet}
    for j in range(6):
        loud = circ_smooth(rng.normal(size=N), 4)
        sigs[f"loud{j}"] = 20.0 * loud / loud.std()

    def cov_score(vals, variant):
        return float(np.mean((vals - vals.mean()) * (tgt - tgt.mean())))

    res = selection_permutation_test(list(sigs), n_obs=N,
                                     stat_fn=make_stat_fn(sigs, cov_score),
                                     n_perm=399, seed=3)
    assert res.p_min_p < res.p_selected
    assert res.p_min_p < 0.05


# ---------------------------------------------------------------- 结果容器
def test_reproducible_given_seed_and_summary_shape():
    rng = np.random.default_rng(17)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 8)
    kw = dict(n_obs=N, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)), n_perm=99, seed=21)
    r1 = selection_permutation_test(list(sigs), **kw)
    r2 = selection_permutation_test(list(sigs), **kw)
    assert (r1.p_selected, r1.p_naive, r1.p_min_p) == (r2.p_selected, r2.p_naive, r2.p_min_p)
    assert np.array_equal(r1.null_stats, r2.null_stats)

    assert r1.null_stats.shape == (99, 8)
    assert r1.observed.shape == (8,)
    assert int(r1.null_winner_counts.sum()) == 99
    frame = r1.to_frame()
    assert list(frame.columns) == ["variant", "observed",
                                   "p_naive_per_variant_UNCORRECTED",
                                   "null_winner_count", "is_observed_winner"]
    assert len(frame) == 8 and int(frame["is_observed_winner"].sum()) == 1
    summary = r1.summary()
    for key in ("p_selected", "p_min_p", "p_naive_UNCORRECTED", "selection_inflation",
                "n_variants", "n_perm", "scheme"):
        assert key in summary
    assert summary["n_perm"] == 99 and summary["n_variants"] == 8


def test_demo_sidecar_carries_headline_numbers_and_banner(tmp_path):
    """演示 sidecar 必须自带结论数字 + "非裁决"横幅（逐变体 CSV 里没有这些）。"""
    import json

    from backtest.selection_permutation import DEMO_BANNER, write_demo_sidecar

    rng = np.random.default_rng(31)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 5)
    res = selection_permutation_test(
        list(sigs), n_obs=N, stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
        n_perm=99, seed=2, meta={"n_obs": N})
    path = write_demo_sidecar(res, tmp_path, db_host="203.0.113.1")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["WARNING_NOT_A_PROBE"] == DEMO_BANNER
    for key in ("p_selected", "p_min_p", "p_naive_UNCORRECTED", "selection_inflation",
                "best_variant", "n_variants", "n_perm", "scheme"):
        assert key in payload["summary"]
    assert payload["summary"]["p_selected"] == res.p_selected
    assert payload["run"]["db_host"] == "203.0.113.1"
    assert set(payload["null_selected_quantiles"]) == {"p5", "p25", "p50", "p75",
                                                       "p95", "p99"}
    assert payload["null_selected_quantiles"]["p95"] >= \
        payload["null_selected_quantiles"]["p50"]


def test_block_scheme_runs_and_is_deterministic():
    """块重抽样支路：机制可用 + 确定性（**仅作敏感性对照**，不作主口径）。"""
    rng = np.random.default_rng(18)
    src = circ_smooth(rng.normal(size=N), 3)
    tgt = rng.normal(size=N)
    sigs = synthetic_grid(src, 6)
    kw = dict(n_obs=N, batch_stat_fn=make_batch_stat_fn(sigs, batch_corr(tgt)),
              n_perm=99, seed=9, scheme="block", block=20)
    r1 = selection_permutation_test(list(sigs), **kw)
    r2 = selection_permutation_test(list(sigs), **kw)
    assert r1.p_selected == r2.p_selected
    assert r1.scheme == "block"
    assert 0 < r1.p_selected <= 1


def test_injected_index_matrix_overrides_sampling():
    rng = np.random.default_rng(19)
    src = circ_smooth(rng.normal(size=64), 3)
    tgt = rng.normal(size=64)
    sigs = {"a": src, "b": np.roll(src, 7)}
    idx = build_index_matrix(64, 31, seed=77)
    res = selection_permutation_test(list(sigs), n_obs=64,
                                     stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
                                     index_matrix=idx)
    assert res.n_perm == 31
    assert res.null_stats.shape == (31, 2)
    assert res.seed is None          # 索引是注入的 → 不记一个假的复现 seed
    assert res.summary()["seed"] is None
    try:
        selection_permutation_test(list(sigs), n_obs=64,
                                   stat_fn=make_stat_fn(sigs, scalar_corr(tgt)),
                                   index_matrix=idx[:, :10])
    except ValueError:
        return
    raise AssertionError("shape mismatch should raise")


# ---------------------------------------------------------------- adjusted_pvalue
# 关 0 口径显式化（2026-09-03）：四个轴探针原本各自内联 `bool(obs >= q95)`（= max-T）
# 互相克隆，口径从未被选过。`adjusted_pvalue` 强制显式指定，依据写进预登记 §3。

class _FakeRes:
    """只带 adjusted_pvalue 需要的两个字段。"""
    def __init__(self, pool):
        pool = np.asarray(pool, dtype=float)
        self.observed = pool[0]
        self.null_stats = pool[1:]


# 池（首行为观测）：变体 0 是低方差档（0.1~0.5），变体 1 是高方差档（0.7~0.95）
_POOL = [[0.5, 0.90],     # 观测
         [0.1, 0.80],
         [0.2, 0.95],
         [0.3, 0.70]]


def test_adjusted_pvalue_criterion_is_required_and_validated():
    from backtest.selection_permutation import adjusted_pvalue
    res = _FakeRes(_POOL)
    with pytest.raises(TypeError):                     # 无默认值 → 不给就报错
        adjusted_pvalue(res, 0)
    with pytest.raises(ValueError, match="预登记"):     # 拼错的口径必须炸，不许静默回落
        adjusted_pvalue(res, 0, "maxT")


def test_adjusted_pvalue_max_t_hand_computed():
    """max-T：p̃ = #{行 max ≥ 该变体观测值} / m。m=4，行 max = [0.90, 0.80, 0.95, 0.70]。"""
    from backtest.selection_permutation import adjusted_pvalue
    res = _FakeRes(_POOL)
    # 变体 0 观测 0.5：四行的 max 全 ≥ 0.5 → 4/4
    assert adjusted_pvalue(res, 0, "max_t") == pytest.approx(1.0)
    # 变体 1 观测 0.90：只有 0.90 与 0.95 两行 ≥ 0.90 → 2/4
    assert adjusted_pvalue(res, 1, "max_t") == pytest.approx(0.5)


def test_adjusted_pvalue_min_p_hand_computed():
    """min-P：先逐列转 p，再比 min_j p_j。

    列 0 值 [0.5,0.1,0.2,0.3] → p = [0.25, 1.0, 0.75, 0.5]
    列 1 值 [0.9,0.8,0.95,0.7] → p = [0.5, 0.75, 0.25, 1.0]
    逐行 min p = [0.25, 0.75, 0.25, 0.5]
    """
    from backtest.selection_permutation import adjusted_pvalue
    res = _FakeRes(_POOL)
    # 变体 0 的观测列 p = 0.25；min p ≤ 0.25 的有第 0、2 行 → 2/4
    assert adjusted_pvalue(res, 0, "min_p") == pytest.approx(0.5)
    # 变体 1 的观测列 p = 0.50；min p ≤ 0.50 的有第 0、2、3 行 → 3/4
    assert adjusted_pvalue(res, 1, "min_p") == pytest.approx(0.75)


def test_max_t_is_blind_to_the_low_variance_variant_min_p_is_not():
    """论证的核心形态：低方差档的变体在 max-T 下拿到最差的 p，在 min-P 下明显更好。

    变体 0 是这个池里**观测值相对自身零分布最极端**的那个（列 p = 0.25，比变体 1 的 0.50 更极端），
    但它的绝对值 0.5 够不到高方差档的水位，于是 max-T 给它 p=1.0（最差可能值）。
    """
    from backtest.selection_permutation import adjusted_pvalue
    res = _FakeRes(_POOL)
    assert adjusted_pvalue(res, 0, "max_t") == pytest.approx(1.0)      # 完全看不见
    assert adjusted_pvalue(res, 0, "min_p") < adjusted_pvalue(res, 0, "max_t")


def test_max_t_matches_the_inlined_form_used_by_existing_probes():
    """等价性：现有四个探针内联的 `count(null_selected >= obs)` 与 max-T 口径同源。

    差别只在探针版用的是 `obs >= q95` 的阈值形式、本函数给的是 p 值形式；
    此处钉住 p 值形式与「池内行 max ≥ 观测」的计数完全一致（含观测行本身）。
    """
    from backtest.selection_permutation import adjusted_pvalue
    pool = np.asarray(_POOL, dtype=float)
    res = _FakeRes(pool)
    for j in (0, 1):
        manual = np.count_nonzero(pool.max(axis=1) >= pool[0, j]) / pool.shape[0]
        assert adjusted_pvalue(res, j, "max_t") == pytest.approx(manual)
