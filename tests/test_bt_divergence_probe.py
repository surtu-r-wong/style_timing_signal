"""② 分歧度/离散度探针的 TDD：口径一致性 + 预登记不变量 + 机器接入三规矩。

验收要点：
1. **三族定义**按 ②(f) 原文（D1=300−1000 差、D2=四对截面标准差、D3=多少对同向），
   且 D3 的写法与 `|Σ sign|` 仿射等价 → `level_signal` 后逐位相同；
2. **向量化统计量 == 房规**：`abs_spearman_batch` 与 `rotation_probe.nonoverlap_ic`、
   `abs_partial_batch` 与 `rotation_probe.partial_rank_ic` 逐位一致（不是另起口径）；
3. **机器接入三规矩**：48 点合成**一次**调用、统计量**不携带**关2 的同号约束
   （不可能因为"两半窗不同号"而返回 −inf）、`min_shift=2·max(k)=80` 且
   `max_shift=n_obs−80`；
4. **预登记不扩网格**：变体恰为 3×2×2×4=48 点，且 lb/zw/k 取值集合与 ②(f) 逐字相同；
5. **选择端不含 2024-2026**：选择窗常量与勘误 §3 一致。

全部判例固定 seed、纯离线（不连库）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.divergence_probe import (  # noqa: E402
    FAMILIES, GRID_K, GRID_LB, GRID_ZW, HOLDOUT, N_VARIANTS, PAIR_NAMES, SELECTION,
    TRAIN, VAL, abs_partial_batch, abs_spearman_batch, build_score_frames,
    build_variant_signals, divergence_levels, evaluate_gates, make_batch_scorer,
    nonoverlap_points, p_at_threshold, variant_grid, warmup_masked_level_signal,
)
from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.rotation_probe import (  # noqa: E402
    _nonoverlap_frame, nonoverlap_ic, partial_rank_ic,
)
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

N = 900


def _dates(n=N):
    return pd.bdate_range("2014-01-02", periods=n)


def _fake_pairs(seed=0, n=N) -> pd.DataFrame:
    """四条相关的 tanh 型配对信号（形似生产口径：有界、自相关）。"""
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    common = pd.Series(rng.normal(size=n)).rolling(20, min_periods=1).mean().to_numpy()
    cols = {}
    for i, name in enumerate(PAIR_NAMES):
        own = pd.Series(rng.normal(size=n)).rolling(10, min_periods=1).mean().to_numpy()
        cols[name] = np.tanh(2.0 * (common + 0.8 * own))
    return pd.DataFrame(cols, index=idx)


# ---------------------------------------------------------------- 1. 三族定义
def test_d1_is_300_minus_1000_pair():
    pairs = _fake_pairs()
    lv = divergence_levels(pairs)
    assert np.allclose(lv["D1"].to_numpy(),
                       (pairs["300"] - pairs["1000"]).to_numpy())


def test_d2_is_cross_sectional_std_of_four_pairs():
    pairs = _fake_pairs()
    lv = divergence_levels(pairs)
    assert np.allclose(lv["D2"].to_numpy(), pairs.std(axis=1, ddof=1).to_numpy())
    # 只有四对进入截面：删一列后数值必变
    assert not np.allclose(lv["D2"].to_numpy(),
                           pairs[["300", "500", "1000"]].std(axis=1, ddof=1).to_numpy())


def test_d3_counts_pairs_pointing_the_same_way():
    pairs = pd.DataFrame([[0.5, 0.4, 0.3, 0.2],      # 4 正 → 4
                          [0.5, 0.4, -0.3, -0.2],    # 2/2 → 2
                          [-0.5, 0.4, -0.3, -0.2]],  # 3 负 → 3
                         columns=list(PAIR_NAMES), index=_dates(3))
    assert divergence_levels(pairs)["D3"].tolist() == [4.0, 2.0, 3.0]


def test_d3_is_affine_equivalent_to_abs_sum_of_signs_after_level_signal():
    """`max(#正,#负)` 与 `|Σ sign|` 差一个仿射变换 → 滚动 z 化后**逐位相同**。"""
    pairs = _fake_pairs(seed=3)
    d3 = divergence_levels(pairs)["D3"]
    alt = pd.Series(np.abs(np.sign(pairs.to_numpy()).sum(axis=1)).astype(float),
                    index=pairs.index)
    a = level_signal(d3, 20, 60)
    b = level_signal(alt, 20, 60)
    assert np.allclose(a.to_numpy(), b.to_numpy(), atol=1e-12)


def test_d2_level_signal_is_scale_invariant_so_ddof_choice_cannot_matter():
    pairs = _fake_pairs(seed=4)
    d2 = divergence_levels(pairs)["D2"]
    scaled = d2 * np.sqrt(3.0 / 4.0)          # ddof=1 → ddof=0 的精确比例
    assert np.allclose(level_signal(d2, 5, 60).to_numpy(),
                       level_signal(scaled, 5, 60).to_numpy(), atol=1e-12)


def test_divergence_levels_rejects_wrong_column_order():
    pairs = _fake_pairs()[["500", "300", "1000", "2000"]]
    with pytest.raises(ValueError):
        divergence_levels(pairs)


# ---------------------------------------------------------------- 2. 预登记网格
def test_grid_is_exactly_the_preregistered_48_points():
    grid = variant_grid()
    assert len(grid) == N_VARIANTS == 48
    assert len(set(grid)) == 48
    assert sorted({v[0] for v in grid}) == sorted(FAMILIES) == ["D1", "D2", "D3"]
    assert sorted({v[1] for v in grid}) == [5, 20] == sorted(GRID_LB)
    assert sorted({v[2] for v in grid}) == [60, 250] == sorted(GRID_ZW)
    assert sorted({v[3] for v in grid}) == [5, 10, 20, 40] == sorted(GRID_K)


def test_selection_windows_exclude_2024_2026():
    assert SELECTION == ("2014-01-01", "2023-12-31")
    assert TRAIN[1] == "2020-12-31" and VAL == ("2021-01-01", "2023-12-31")
    assert HOLDOUT[0] == "2024-01-01"
    assert pd.Timestamp(SELECTION[1]) < pd.Timestamp(HOLDOUT[0])


def test_warmup_is_masked_to_nan_not_left_as_constant_zero():
    """房规 `level_signal` 把暖机段填 0（常数假信号）；本探针必须置 NaN 让交集丢掉它。"""
    lv = divergence_levels(_fake_pairs())["D2"]
    for lb in GRID_LB:
        for zw in GRID_ZW:
            raw = level_signal(lv, lb, zw)
            masked = warmup_masked_level_signal(lv, lb, zw)
            n_warm = zw + lb - 2
            assert (raw.iloc[:zw - 1] == 0.0).all()          # 房规行为存证
            assert masked.iloc[:n_warm].isna().all()
            assert masked.iloc[n_warm:].notna().all()
            assert np.allclose(masked.iloc[n_warm:].to_numpy(),
                               raw.iloc[n_warm:].to_numpy(), atol=1e-12)


def test_shared_index_starts_after_the_longest_warmup():
    """全网格共用索引 = 各变体非 NaN 日期的交集 → 起点由 zw=250 的暖机决定。"""
    lv = divergence_levels(_fake_pairs())
    sigs = build_variant_signals(lv)
    common = None
    for v in variant_grid():
        idx = sigs[v].dropna().index
        common = idx if common is None else common.intersection(idx)
    longest = max(zw + lb - 2 for lb in GRID_LB for zw in GRID_ZW)
    assert common[0] == lv["D1"].index[longest]


def test_variant_signals_share_one_series_per_lb_zw_and_k_does_not_move_the_signal():
    lv = divergence_levels(_fake_pairs())
    sigs = build_variant_signals(lv)
    assert len(sigs) == 48
    for fam in FAMILIES:
        for lb in GRID_LB:
            for zw in GRID_ZW:
                base = sigs[(fam, lb, zw, GRID_K[0])]
                for k in GRID_K[1:]:
                    assert sigs[(fam, lb, zw, k)] is base


# ---------------------------------------------------------------- 3. 向量化 == 房规
def _fixture(seed=0, n=N):
    lv = divergence_levels(_fake_pairs(seed=seed, n=n))
    sigs = build_variant_signals(lv)
    idx = None
    for v in variant_grid():
        s = sigs[v].dropna().index
        idx = s if idx is None else idx.intersection(s)
    rng = np.random.default_rng(seed + 100)
    und = pd.Series(rng.normal(scale=0.012, size=len(idx)), index=idx)
    ctrl = pd.Series(np.tanh(pd.Series(rng.normal(size=len(idx))).rolling(
        20, min_periods=1).mean().to_numpy()), index=idx)
    return sigs, idx, und, ctrl


def test_abs_spearman_batch_matches_house_nonoverlap_ic_bitwise():
    sigs, idx, und, ctrl = _fixture()
    frames = build_score_frames(und, ctrl, idx)
    for v in variant_grid():
        fam, lb, zw, k = v
        s = sigs[v].reindex(idx)
        house, _ = nonoverlap_ic(s, und, k)
        fast = abs_spearman_batch(s.to_numpy()[None, frames[k].pts], frames[k])[0]
        assert np.isclose(fast, abs(house), atol=1e-12), (v, fast, house)


def test_abs_partial_batch_matches_house_partial_rank_ic_bitwise():
    sigs, idx, und, ctrl = _fixture(seed=1)
    frames = build_score_frames(und, ctrl, idx)
    for v in variant_grid():
        fam, lb, zw, k = v
        s = sigs[v].reindex(idx)
        frame = _nonoverlap_frame(s, und, k)
        house = partial_rank_ic(frame["sig"], frame["fwd"], ctrl.reindex(frame.index))
        fast = abs_partial_batch(s.to_numpy()[None, frames[k].pts], frames[k])[0]
        assert np.isclose(fast, abs(house), atol=1e-12), (v, fast, house)


def test_nonoverlap_points_matches_house_block_end_positions():
    for k in GRID_K:
        assert np.array_equal(nonoverlap_points(N, k), np.arange(k - 1, N, k))


def test_batch_scorer_applies_the_variant_own_k():
    """变体元组第 4 位是 k：换 k 必须换 ScoreFrame，否则口径静默串台。"""
    sigs, idx, und, ctrl = _fixture(seed=2)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in variant_grid()}
    fn = make_batch_scorer(arrs, frames, abs_spearman_batch)
    ident = np.arange(len(idx))[None, :]
    for k in GRID_K:
        v = ("D2", 20, 60, k)
        house, _ = nonoverlap_ic(sigs[v].reindex(idx), und, k)
        assert np.isclose(fn(v, ident)[0], abs(house), atol=1e-12)


# ---------------------------------------------------------------- 4. 机器接入三规矩
def test_single_call_covers_all_48_variants_no_per_family_calls():
    """规矩 1：族级多重比较必须被同一次调用吃掉（48 点合成一次）。"""
    sigs, idx, und, ctrl = _fixture(seed=5)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in variant_grid()}
    res = selection_permutation_test(
        variant_grid(), n_obs=len(idx),
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=30, seed=0, scheme="rotation",
        min_shift=2 * max(GRID_K), max_shift=len(idx) - 2 * max(GRID_K))
    assert len(res.variants) == 48
    assert sorted({v[0] for v in res.variants}) == ["D1", "D2", "D3"]
    assert res.null_stats.shape == (30, 48)


def test_statistic_never_encodes_gate2_sign_constraint():
    """规矩 2：`-inf` 只能来自"无法评分"。构造一个两半窗**反号**的变体，统计量仍有限。"""
    sigs, idx, und, ctrl = _fixture(seed=6)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in variant_grid()}
    fn = make_batch_scorer(arrs, frames, abs_spearman_batch)
    ident = np.arange(len(idx))[None, :]
    half = len(idx) // 2
    saw_flip = False
    for v in variant_grid():
        s = sigs[v].reindex(idx)
        a, _ = nonoverlap_ic(s.iloc[:half], und.iloc[:half], v[3])
        b, _ = nonoverlap_ic(s.iloc[half:], und.iloc[half:], v[3])
        if np.isfinite(a) and np.isfinite(b) and np.sign(a) != np.sign(b):
            saw_flip = True
            assert np.isfinite(fn(v, ident)[0]), v      # 同号约束没有混进统计量
    assert saw_flip, "构造失败：没有任何变体两半窗反号，本判例失去意义"


def test_shift_bounds_follow_house_rule_two_k():
    """规矩 3：min_shift = 2·max(k) = 80，max_shift = n_obs − 80。"""
    assert 2 * max(GRID_K) == 80
    sigs, idx, und, ctrl = _fixture(seed=7)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in variant_grid()}
    n_obs = len(idx)
    res = selection_permutation_test(
        variant_grid(), n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=20, seed=0, scheme="rotation",
        min_shift=80, max_shift=n_obs - 80)
    assert res.n_perm == 20
    with pytest.raises(ValueError):        # 越界的移位区间必须炸，不许静默退化
        selection_permutation_test(
            variant_grid(), n_obs=n_obs,
            batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
            n_perm=5, seed=0, scheme="rotation", min_shift=n_obs + 1)


def test_run_is_deterministic_under_fixed_seed():
    sigs, idx, und, ctrl = _fixture(seed=8)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in variant_grid()}

    def _run():
        return selection_permutation_test(
            variant_grid(), n_obs=len(idx),
            batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
            n_perm=25, seed=11, scheme="rotation",
            min_shift=80, max_shift=len(idx) - 80)

    a, b = _run(), _run()
    assert a.p_selected == b.p_selected and a.best_index == b.best_index
    assert np.array_equal(a.null_selected, b.null_selected)


# ---------------------------------------------------------------- 5. 闸门逻辑
def test_p_at_threshold_follows_house_count_over_b_plus_one():
    null = np.array([0.1, 0.2, 0.3, 0.4], dtype=float)
    assert p_at_threshold(null, 0.35) == pytest.approx(2 / 5)   # (1 + 1) / (4 + 1)
    assert p_at_threshold(null, 0.05) == pytest.approx(5 / 5)
    assert np.isnan(p_at_threshold(null, np.nan))


class _FakeRes:
    def __init__(self, p_selected, p_naive):
        self.p_selected = p_selected
        self.p_naive = p_naive


def _panel_row(ic_sel, ic_tr, ic_val):
    return pd.DataFrame([{
        "family": "D2", "lb": 20, "zw": 60, "k": 20, "kou_jing": "blend",
        "ic_selection_2014_2023": ic_sel, "ic_train_2014_2020": ic_tr,
        "ic_val_2021_2023": ic_val}])


def test_gates_go_only_when_all_four_pass():
    v = evaluate_gates(("D2", 20, 60, 20), _panel_row(0.12, 0.10, 0.14),
                       _FakeRes(0.01, 0.001), _FakeRes(0.02, 0.002),
                       pic_signed=0.09, p_pic_at_winner=0.02,
                       sharpe_by_win={"selection_2014_2023": {"net_sharpe": 0.6}})
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is True


@pytest.mark.parametrize("kw", [
    {"res_ic": _FakeRes(0.20, 0.01)},                       # 关1 不过
    {"pic_signed": -0.09},                                  # 关1b 反号
    {"p_pic_at_winner": 0.30},                              # 关1b p 不过
    {"panel": _panel_row(0.12, -0.03, 0.14)},               # 关2 半窗反号
    {"sharpe_by_win": {"selection_2014_2023": {"net_sharpe": -0.2}}},   # 关3 不过
])
def test_any_single_gate_failure_forces_stop(kw):
    base = dict(winner=("D2", 20, 60, 20), panel=_panel_row(0.12, 0.10, 0.14),
                res_ic=_FakeRes(0.01, 0.001), res_pic=_FakeRes(0.02, 0.002),
                pic_signed=0.09, p_pic_at_winner=0.02,
                sharpe_by_win={"selection_2014_2023": {"net_sharpe": 0.6}})
    base.update(kw)
    v = evaluate_gates(**base)
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is False
    assert "STOP" in str(v[v["gate"] == "OVERALL"]["metric"].iloc[0])


def test_naive_p_row_is_marked_as_non_gate():
    v = evaluate_gates(("D2", 20, 60, 20), _panel_row(0.12, 0.10, 0.14),
                       _FakeRes(0.01, 0.001), _FakeRes(0.02, 0.002),
                       pic_signed=0.09, p_pic_at_winner=0.02,
                       sharpe_by_win={"selection_2014_2023": {"net_sharpe": 0.6}})
    naive = v[v["metric"].str.contains("naive")]
    assert len(naive) == 1 and naive["pass"].iloc[0] is None
