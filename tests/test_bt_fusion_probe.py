import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.fusion_probe import (  # noqa: E402
    FUSED, INCUMBENT, SUBSTITUTE, build_divergence_report, evaluate_gates,
    forward_return, fuse_equal, nonoverlap_grid, paired_ic_bootstrap, rank_ic,
    spearman_rows, summarize_divergence,
)


# ---------------- 融合口径 ----------------
def test_fuse_equal_is_fixed_half_half():
    idx = pd.bdate_range("2020-01-01", periods=5)
    a = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=idx)
    b = pd.Series([3.0, 0.0, -1.0, 2.0, 1.0], index=idx)
    got = fuse_equal(a, b)
    assert np.allclose(got.to_numpy(), (a.to_numpy() + b.to_numpy()) / 2.0)


def test_fuse_equal_drops_single_leg_days_instead_of_amplifying():
    """缺腿不得按全额计（07-11 勘误 §2 的 blend carry bug 形状）。"""
    idx = pd.bdate_range("2020-01-01", periods=4)
    a = pd.Series([1.0, 1.0, 1.0, 1.0], index=idx)
    b = pd.Series([np.nan, 1.0, np.nan, 3.0], index=idx)
    got = fuse_equal(a, b)
    assert list(got.index) == [idx[1], idx[3]]          # 单腿日被剔除，不是 1.0
    assert np.allclose(got.to_numpy(), [1.0, 2.0])


def test_fuse_equal_has_no_weight_parameter():
    """预登记单点：扫权重必须在实现层面不可达（§7）。"""
    import inspect

    from backtest import fusion_probe

    assert list(inspect.signature(fuse_equal).parameters) == ["a", "b"]
    # 模块级也不得留任何可调的权重旋钮
    assert not [n for n in dir(fusion_probe) if n.isupper() and "WEIGHT" in n]


# ---------------- 前瞻收益与非重叠网格 ----------------
def test_forward_return_uses_t_plus_1_to_t_plus_k():
    idx = pd.bdate_range("2020-01-01", periods=6)
    u = pd.Series([0.0, 0.1, 0.2, -0.1, 0.05, 0.0], index=idx)
    got = forward_return(u, 2)
    # t0 → (1+0.1)(1+0.2)−1
    assert got.iloc[0] == pytest.approx(1.1 * 1.2 - 1.0)
    # t1 → (1+0.2)(1−0.1)−1
    assert got.iloc[1] == pytest.approx(1.2 * 0.9 - 1.0)
    assert np.isnan(got.iloc[-1]) and np.isnan(got.iloc[-2])


def test_forward_return_k_must_be_positive():
    with pytest.raises(ValueError):
        forward_return(pd.Series([0.0, 0.1]), 0)


def test_nonoverlap_grid_spacing_and_offset():
    idx = pd.bdate_range("2020-01-01", periods=100)
    grid = nonoverlap_grid(idx, 20, 0)
    assert len(grid) == 5
    assert list(grid) == [idx[i] for i in (0, 20, 40, 60, 80)]
    assert nonoverlap_grid(idx, 20, 3)[0] == idx[3]
    with pytest.raises(ValueError):
        nonoverlap_grid(idx, 20, 20)


# ---------------- rank IC ----------------
def test_rank_ic_perfect_monotone_is_one():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    y = pd.Series([0.1, 0.2, 0.3, 0.4, 0.9])
    assert rank_ic(x, y)["ic"] == pytest.approx(1.0)


def test_rank_ic_matches_scipy_and_t_test():
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(size=80))
    y = pd.Series(0.3 * x + rng.normal(size=80))
    got = rank_ic(x, y)
    want_r = stats.spearmanr(x, y).statistic
    assert got["ic"] == pytest.approx(want_r)
    assert got["n_obs"] == 80
    t = want_r * np.sqrt(78 / (1 - want_r ** 2))
    assert got["t_stat"] == pytest.approx(t)
    assert got["p_value"] == pytest.approx(2 * stats.t.sf(abs(t), df=78))


def test_rank_ic_too_few_points_is_nan():
    got = rank_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0]))
    assert np.isnan(got["ic"]) and got["n_obs"] == 2


# ---------------- 向量化 Spearman + 配对 bootstrap ----------------
def test_spearman_rows_matches_scipy_including_ties():
    rng = np.random.default_rng(1)
    x = rng.integers(0, 5, size=(6, 40)).astype(float)   # 大量并列
    y = rng.normal(size=(6, 40))
    got = spearman_rows(x, y)
    want = np.array([stats.spearmanr(x[i], y[i]).statistic for i in range(6)])
    assert np.allclose(got, want)


def test_paired_ic_bootstrap_identical_factors_give_zero_diff():
    rng = np.random.default_rng(2)
    x = pd.Series(rng.normal(size=60))
    y = pd.Series(rng.normal(size=60))
    out = paired_ic_bootstrap(x, x.copy(), y, n=200, seed=0)
    assert out["diff_ic"] == pytest.approx(0.0)
    assert out["boot_sd"] == pytest.approx(0.0, abs=1e-12)
    assert out["ci_excludes_zero"] is False


def test_paired_ic_bootstrap_reproducible_and_detects_real_gap():
    rng = np.random.default_rng(3)
    y = pd.Series(rng.normal(size=200))
    good = pd.Series(y.to_numpy() + rng.normal(scale=0.3, size=200))   # 强预测
    weak = pd.Series(rng.normal(size=200))                             # 无预测
    a = paired_ic_bootstrap(good, weak, y, n=500, seed=0)
    b = paired_ic_bootstrap(good, weak, y, n=500, seed=0)
    assert a == b                                  # 同 seed 逐位复现
    assert a["diff_ic"] > 0.5 and a["ci_lo"] > 0 and a["ci_excludes_zero"] is True


# ---------------- 闸门判定 ----------------
def _ic_frames(ic_inc, ic_fus, ci_lo=0.05, ci_hi=0.2):
    rows = []
    for win, inc, fus in (("2014-2020", ic_inc[0], ic_fus[0]),
                          ("2021-2023", ic_inc[1], ic_fus[1])):
        rows += [{"window": win, "factor": INCUMBENT, "ic": inc},
                 {"window": win, "factor": FUSED, "ic": fus},
                 {"window": win, "factor": SUBSTITUTE, "ic": inc}]
    diff = pd.DataFrame([
        {"window": w, "challenger": FUSED, "ci_lo": ci_lo, "ci_hi": ci_hi,
         "ci_excludes_zero": bool(ci_lo > 0 or ci_hi < 0)}
        for w in ("2014-2020", "2021-2023")])
    return pd.DataFrame(rows), diff


def _ret_frame(sharpe_inc, sharpe_fus, turn_inc=1.0, turn_fus=1.0):
    rows = []
    wins = ["2014-2020", "2021-2023", "2024-2026", "full"]
    for name, sh, tn in ((INCUMBENT, sharpe_inc, turn_inc), (FUSED, sharpe_fus, turn_fus)):
        for w, s in zip(wins, sh):
            rows.append({"signal": f"{name}_lf", "kou_jing": "blend", "window": w,
                         "seg": "full", "sharpe": s, "turnover": tn})
    return pd.DataFrame(rows)


def test_gates_all_pass_when_fusion_dominates():
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.09, 0.08))
    ret = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.7, 1.55, 1.1, 1.8], 1.0, 0.9)
    v = evaluate_gates(ic_rep, ic_diff, ret)
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is True


def test_gate0_blocks_even_when_revenue_gates_pass():
    """⓪ 先于收益层：IC 平手/更低 → 总裁决 STOP，哪怕收益全过。"""
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.05, 0.03))
    ret = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.7, 1.55, 1.1, 1.8], 1.0, 0.9)
    v = evaluate_gates(ic_rep, ic_diff, ret)
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is False
    g0 = v[(v["gate"] == "0_rank_ic") & (v["metric"] == "verdict")]["pass"].iloc[0]
    assert bool(g0) is False


def test_gate0_tie_on_ci_is_not_clearly_better():
    """IC 双窗更高但配对差 CI 双窗都含 0 → 平手，不算"明确优于"。"""
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.055, 0.045), ci_lo=-0.05, ci_hi=0.06)
    ret = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.7, 1.55, 1.1, 1.8], 1.0, 0.9)
    v = evaluate_gates(ic_rep, ic_diff, ret)
    assert bool(v[(v["gate"] == "0_rank_ic")
                  & (v["metric"] == "verdict")]["pass"].iloc[0]) is False


def test_worst_tv_lift_threshold_is_010():
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.09, 0.08))
    ret = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.55, 1.49, 1.1, 1.7])   # worst 提升 0.09
    v = evaluate_gates(ic_rep, ic_diff, ret)
    lift = v[v["gate"] == "2_worst_tv_lift"].iloc[0]
    assert lift["delta"] == pytest.approx(0.09)
    assert bool(lift["pass"]) is False


def test_turnover_increase_fails_gate3():
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.09, 0.08))
    ret = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.7, 1.55, 1.1, 1.8], 1.0, 1.2)
    v = evaluate_gates(ic_rep, ic_diff, ret)
    assert not v[v["gate"] == "3_turnover_not_up"]["pass"].any()
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is False


# ---------------- 分歧日分析 ----------------
def _synthetic_factors(ew_vals, sl_vals, start="2015-01-01"):
    idx = pd.bdate_range(start, periods=len(ew_vals))
    ew = pd.Series(ew_vals, index=idx, dtype=float)
    sl = pd.Series(sl_vals, index=idx, dtype=float)
    return {INCUMBENT: ew, SUBSTITUTE: sl, FUSED: fuse_equal(ew, sl)}


def test_divergence_follows_the_larger_magnitude_leg(monkeypatch):
    """合成判例：分歧日融合必然跟随 |因子值| 较大的那一腿（并列 → 跟空仓腿）。"""
    from backtest import fusion_probe

    #        d0: ew 大且多 → 跟 ew     d1: sl 大且多 → 跟 sl
    #        d2: 不分歧（同多）        d3: |ew|=|sl| 并列 → 跟空仓腿
    ew = [+0.30, -0.10, +0.20, +0.15]
    sl = [-0.10, +0.30, +0.05, -0.15]
    factors = _synthetic_factors(ew, sl)
    und = pd.Series([0.01, -0.02, 0.03, 0.004],
                    index=factors[INCUMBENT].index)
    monkeypatch.setattr(fusion_probe, "load_underlying_returns",
                        lambda *a, **k: und)
    div = build_divergence_report(factors)

    assert div["divergent"].tolist() == [True, True, False, True]
    d = div[div["divergent"]]
    assert d["follows"].tolist() == [INCUMBENT, SUBSTITUTE, SUBSTITUTE]
    assert (d["follows"] == d["larger_abs_leg"]).all()
    # 分歧日两腿仓位严格互补（long-flat 下 1/0）
    assert ((d["pos_incumbent"] + d["pos_slope20"]) == 1).all()
    # 融合仓位 = 被跟随那一腿的仓位（d0 跟 ew 的多、d1 跟 sl 的多、d3 跟 sl 的空）
    assert d["pos_fused"].tolist() == [1, 1, 0]
    assert (d["pos_fused"].to_numpy()
            == np.where(d["follows"] == INCUMBENT, d["pos_incumbent"],
                        d["pos_slope20"])).all()


def test_divergence_fwd_return_is_t_plus_1(monkeypatch):
    from backtest import fusion_probe

    factors = _synthetic_factors([+0.3, -0.1, +0.2], [-0.1, +0.3, +0.05])
    und = pd.Series([0.01, -0.02, 0.03], index=factors[INCUMBENT].index)
    monkeypatch.setattr(fusion_probe, "load_underlying_returns", lambda *a, **k: und)
    div = build_divergence_report(factors)
    assert div["fwd_ret_t1"].iloc[0] == pytest.approx(-0.02)   # t0 → t1 的收益
    assert div["fwd_ret_t1"].iloc[1] == pytest.approx(0.03)
    assert np.isnan(div["fwd_ret_t1"].iloc[2])                 # 末日无 T+1


def _div_frame(pos_inc, pos_fused, fwd):
    n = len(fwd)
    return pd.DataFrame({
        "date": pd.bdate_range("2015-01-01", periods=n), "divergent": [True] * n,
        "pos_incumbent": pos_inc, "pos_slope20": [1 - p for p in pos_inc],
        "pos_fused": pos_fused, "abs_incumbent": [0.2] * n, "abs_slope20": [0.1] * n,
        "follows": [INCUMBENT] * n, "larger_abs_leg": [INCUMBENT] * n,
        "fwd_ret_t1": fwd, "window": ["2014-2020"] * n})


def test_capture_accounting_两腿互补则合计等于可得收益():
    fwd = [0.01, -0.02, 0.03, -0.01, 0.02]
    div = _div_frame([1, 0, 1, 0, 1], [1, 0, 1, 0, 1], fwd)
    s = summarize_divergence(div, n_boot=200, seed=0)
    assert s["available_return"] == pytest.approx(sum(fwd))
    assert s["capture_incumbent"] + s["capture_slope20"] == pytest.approx(sum(fwd))
    assert s["random_expectation"] == pytest.approx(sum(fwd) / 2)


def test_capture_ratio_reflects_the_followed_leg():
    """捕获率 = 被跟随腿收益 / 可得收益；随机仲裁的期望是 50%，好的仲裁可 >100%。"""
    fwd = [0.01, -0.02, 0.03, -0.01, 0.02]
    pos_inc = [1, 0, 1, 0, 1]                        # 现任每天都站对
    div = _div_frame(pos_inc, pos_inc, fwd)          # 融合恒等于现任
    s = summarize_divergence(div, n_boot=200, seed=0)
    assert s["capture_fused"] == pytest.approx(0.06)
    assert s["available_return"] == pytest.approx(0.03)
    assert s["capture_ratio_fused"] == pytest.approx(2.0)   # 远优于随机的 50%
    assert s["correct_choice_rate"] == pytest.approx(1.0)


def test_adverse_selection_shows_up_as_low_capture_and_small_p():
    """恒跟错边 → 捕获率 0、随机化检验左尾 p 极小（反向选择的判例锚）。"""
    fwd = [0.02] * 20 + [-0.01] * 5
    pos_inc = [1] * 20 + [0] * 5                     # 现任每天都站对
    pos_fused = [0] * 20 + [1] * 5                   # 融合每天都站反
    div = _div_frame(pos_inc, pos_fused, fwd)
    s = summarize_divergence(div, n_boot=2000, seed=0)
    assert s["capture_fused"] < 0
    assert s["capture_ratio_fused"] < 0
    assert s["random_p_left"] < 0.01
    assert s["correct_choice_rate"] == pytest.approx(0.0)
    assert s["correct_binomial_p"] < 1e-6


def test_summarize_divergence_ignores_nonvergent_and_nan_rows():
    div = _div_frame([1, 0, 1], [1, 0, 1], [0.01, -0.02, np.nan])
    div.loc[1, "divergent"] = False
    s = summarize_divergence(div, n_boot=100, seed=0)
    assert s["n_days_divergent"] == 1                # 剔除非分歧日与无 T+1 的末日
    assert s["n_days_total"] == 3
    assert s["capture_fused"] == pytest.approx(0.01)


def test_holdout_window_never_enters_any_gate():
    """2024-26 仅报告：改 holdout 数字不得改变任何一格判定（§4 C2）。"""
    ic_rep, ic_diff = _ic_frames((0.05, 0.04), (0.09, 0.08))
    base = _ret_frame([1.5, 1.4, 1.0, 1.6], [1.7, 1.55, 1.1, 1.8], 1.0, 0.9)
    moved = base.copy()
    moved.loc[moved["window"] == "2024-2026", "sharpe"] = -5.0
    v1 = evaluate_gates(ic_rep, ic_diff, base)
    v2 = evaluate_gates(ic_rep, ic_diff, moved)
    assert v1["pass"].tolist() == v2["pass"].tolist()
