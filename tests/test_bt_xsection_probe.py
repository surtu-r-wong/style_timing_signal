"""面2 个股截面二阶矩探针的 TDD：预登记不变量 + 机器接入三规矩 + 面2 自己的六类判例。

预登记 §7 点名的六类（本文件逐类有段落标题）：

1. **机器接入三规矩**——32 点合成**一次**调用、统计量**不携带**关2 的同号约束、
   `min_shift=2·max(k)=80` 且 `max_shift=n_obs−80`；外加"族绑定用完必还原"守卫；
2. **宇宙过滤（含港股排除）**——后缀 `.SH/.SZ`，`.HK`/`.BJ` 全排；并**存证**
   `thermo_probe` 的前缀写法会放行 4 位港股代码（附录 §1.3 的发现）；
3. **X2 分母子集一致**——σ_p 与 σ_i 同子集时 ρ̄ 恒等于 σ 加权平均逐对相关 ⟹ ∈[−1,1]；
   并**反证**：σ_p 换成不同子集会把 ρ̄ 顶出 1；
4. **暖机掩码**——两族都按位置置 NaN（房规 `level_signal` 的 `fillna(0)` 会造常数假信号）；
5. **样本下限**——`len(pts)<10` 抛异常 / 诊断 `n_obs<30` 不出行 / 净值窗 `len(s)<60` 跳过；
6. **诊断限窗**——诊断一律只在选择窗内计算（`breadth.csv` 2025 后含港股污染 + 止于 2026-06-30）。

全部判例固定 seed、**纯离线**（不连库、不读 SQL 缓存）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import divergence_probe as _dp  # noqa: E402
from backtest.divergence_probe import (  # noqa: E402
    build_ic_panel, build_score_frames, build_variant_signals, net_sharpe_by_window,
    run_machines, warmup_masked_level_signal,
)
from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.rotation_probe import nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.xsection_probe import (  # noqa: E402
    ANCHORS_N, ANCHORS_N_FULL, CORR_WINDOW, FAMILIES, GRID_K, GRID_LB, GRID_ZW, HOLDOUT,
    N_VARIANTS,
    SELECTION, SQL_START, TRAIN, UNIVERSE_SQL, VAL, build_diagnostics,
    dp_families_bound_to_xsection, implied_avg_correlation, universe_mask,
    xsection_levels, xsection_measures, xsection_variant_grid,
)

N = 900
LONGEST_WARMUP = max(zw + lb - 2 for lb in GRID_LB for zw in GRID_ZW)   # 268


def _dates(n=N, start="2012-01-04"):
    return pd.bdate_range(start, periods=n)


def _fake_levels(seed=0, n=N) -> dict[str, pd.Series]:
    """形似真序列的两族水平量：X1 = 正的截面标准差（无上界）、X2 = (0,1) 内的平均相关。"""
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    base = pd.Series(rng.normal(size=n)).rolling(20, min_periods=1).mean().to_numpy()
    x1 = pd.Series(0.022 + 0.004 * base + 0.001 * rng.normal(size=n), index=idx,
                   name="X1").abs()
    x2 = pd.Series(1.0 / (1.0 + np.exp(-(0.4 + 1.5 * base + 0.2 * rng.normal(size=n)))),
                   index=idx, name="X2")
    return {"X1": x1, "X2": x2}


def _fixture(seed=0, n=N):
    """两族 → 32 信号 → 共用索引 + 合成的标的收益/控制变量（口径与 ② 判例同形）。"""
    levels = _fake_levels(seed=seed, n=n)
    sigs = build_variant_signals(levels, FAMILIES)
    idx = None
    for v in xsection_variant_grid():
        s = sigs[v].dropna().index
        idx = s if idx is None else idx.intersection(s)
    rng = np.random.default_rng(seed + 100)
    und = pd.Series(rng.normal(scale=0.012, size=len(idx)), index=idx)
    ctrl = pd.Series(np.tanh(pd.Series(rng.normal(size=len(idx))).rolling(
        20, min_periods=1).mean().to_numpy()), index=idx)
    return levels, sigs, idx, und, ctrl


# ================================================================ 0. 预登记常量
def test_grid_is_exactly_the_preregistered_32_points():
    grid = xsection_variant_grid()
    assert len(grid) == N_VARIANTS == 32
    assert len(set(grid)) == 32
    assert sorted({v[0] for v in grid}) == sorted(FAMILIES) == ["X1", "X2"]
    assert sorted({v[1] for v in grid}) == [5, 20] == sorted(GRID_LB)
    assert sorted({v[2] for v in grid}) == [60, 250] == sorted(GRID_ZW)
    assert sorted({v[3] for v in grid}) == [5, 10, 20, 40] == sorted(GRID_K)


def test_selection_windows_exclude_2024_2026():
    assert SELECTION == ("2014-01-01", "2023-12-31")
    assert TRAIN[1] == "2020-12-31" and VAL == ("2021-01-01", "2023-12-31")
    assert HOLDOUT[0] == "2024-01-01"
    assert pd.Timestamp(SELECTION[1]) < pd.Timestamp(HOLDOUT[0])


def test_frozen_data_constants():
    """D5 起点 2012-01-01、D4 窗 60 日、附录 §1.4 的三个宇宙锚点——冻结项。"""
    assert SQL_START == "2012-01-01"
    assert CORR_WINDOW == 60
    assert ANCHORS_N == {"2014-06-30": 2301, "2018-06-29": 3325, "2026-06-30": 5186}
    # X2 侧锚点（`c60 = 60` 的满窗子集，故严格小于 X1 的 N）——独立 SQL 取数，非读缓存自证
    assert ANCHORS_N_FULL == {"2014-06-30": 2297, "2018-06-29": 3298, "2026-06-30": 5166}
    assert all(ANCHORS_N_FULL[d] < ANCHORS_N[d] for d in ANCHORS_N)


# ================================================================ 1. 机器接入三规矩
def test_single_call_covers_all_32_variants_no_per_family_calls():
    """规矩 1：族级多重比较必须被同一次调用吃掉（32 点合成一次）。"""
    _, sigs, idx, und, ctrl = _fixture(seed=5)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in xsection_variant_grid()}
    from backtest.divergence_probe import abs_spearman_batch, make_batch_scorer
    res = selection_permutation_test(
        xsection_variant_grid(), n_obs=len(idx),
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=30, seed=0, scheme="rotation",
        min_shift=2 * max(GRID_K), max_shift=len(idx) - 2 * max(GRID_K))
    assert len(res.variants) == 32
    assert sorted({v[0] for v in res.variants}) == ["X1", "X2"]
    assert res.null_stats.shape == (30, 32)


def test_statistic_never_encodes_gate2_sign_constraint():
    """规矩 2：`-inf` 只能来自"无法评分"。两半窗**反号**的变体统计量仍须有限。"""
    from backtest.divergence_probe import abs_spearman_batch, make_batch_scorer
    _, sigs, idx, und, ctrl = _fixture(seed=6)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in xsection_variant_grid()}
    fn = make_batch_scorer(arrs, frames, abs_spearman_batch)
    ident = np.arange(len(idx))[None, :]
    half = len(idx) // 2
    saw_flip = False
    for v in xsection_variant_grid():
        s = sigs[v].reindex(idx)
        a, _ = nonoverlap_ic(s.iloc[:half], und.iloc[:half], v[3])
        b, _ = nonoverlap_ic(s.iloc[half:], und.iloc[half:], v[3])
        if np.isfinite(a) and np.isfinite(b) and np.sign(a) != np.sign(b):
            saw_flip = True
            assert np.isfinite(fn(v, ident)[0]), v
    assert saw_flip, "构造失败：没有任何变体两半窗反号，本判例失去意义"


def test_shift_bounds_follow_house_rule_two_k():
    """规矩 3：min_shift = 2·max(k) = 80，max_shift = n_obs − 80；越界必须炸。"""
    from backtest.divergence_probe import abs_spearman_batch, make_batch_scorer
    assert 2 * max(GRID_K) == 80
    _, sigs, idx, und, ctrl = _fixture(seed=7)
    frames = build_score_frames(und, ctrl, idx)
    arrs = {v: sigs[v].reindex(idx).to_numpy(dtype=float) for v in xsection_variant_grid()}
    n_obs = len(idx)
    res = selection_permutation_test(
        xsection_variant_grid(), n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=20, seed=0, scheme="rotation", min_shift=80, max_shift=n_obs - 80)
    assert res.n_perm == 20
    with pytest.raises(ValueError):
        selection_permutation_test(
            xsection_variant_grid(), n_obs=n_obs,
            batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
            n_perm=5, seed=0, scheme="rotation", min_shift=n_obs + 1)


def test_house_run_machines_uses_two_k_bounds_and_one_call_per_statistic():
    """复用的 `run_machines` 在面2 族上仍是"每个统计量恰一次调用 + 房规移位区间"。"""
    _, sigs, idx, und, ctrl = _fixture(seed=9)
    frames = build_score_frames(und, ctrl, idx)
    with dp_families_bound_to_xsection():
        res_ic, res_pic = run_machines(sigs, frames, idx, n_perm=12, seed=0)
    for res in (res_ic, res_pic):
        assert len(res.variants) == 32
        assert sorted({v[0] for v in res.variants}) == ["X1", "X2"]
        assert res.meta["min_shift"] == 80
        assert res.meta["max_shift"] == len(idx) - 80
        assert res.meta["n_obs"] == len(idx)


def test_family_binding_is_restored_and_does_not_leak_into_divergence_probe():
    """族绑定是运行期临时改名：进出各查一次，divergence_probe 必须原样回到 48 点。"""
    before = _dp.variant_grid()
    assert len(before) == 48 and sorted({v[0] for v in before}) == ["D1", "D2", "D3"]
    with dp_families_bound_to_xsection():
        inside = _dp.variant_grid()
        assert len(inside) == 32 and sorted({v[0] for v in inside}) == ["X1", "X2"]
    assert _dp.variant_grid() == before
    # 异常路径也必须还原
    with pytest.raises(RuntimeError):
        with dp_families_bound_to_xsection():
            raise RuntimeError("boom")
    assert _dp.variant_grid() == before


def test_ic_panel_covers_32_variants_times_three_kou_jing():
    _, sigs, idx, und, _ = _fixture(seed=10)
    with dp_families_bound_to_xsection():
        panel = build_ic_panel(sigs, {"500": und, "1000": und, "blend": und}, idx)
    assert len(panel) == 32 * 3
    assert sorted(panel["family"].unique()) == ["X1", "X2"]


# ================================================================ 2. 宇宙过滤（含港股排除）
def _raw_rows() -> pd.DataFrame:
    """一天的原始行：沪深 + 港股（含会被前缀过滤放行的 4 位码）+ 北交所 + 三种无效行。"""
    return pd.DataFrame({
        "ts_code": ["600000.SH", "688111.SH", "000001.SZ", "300750.SZ",
                    "0700.HK", "3008.HK", "6030.HK", "0001.HK",
                    "830799.BJ", "920008.BJ",
                    "600001.SH", "600002.SH", "600003.SH"],
        "trade_date": [pd.Timestamp("2026-06-30")] * 13,
        "close": [10.0, 20.0, 11.0, 30.0, 300.0, 5.0, 40.0, 50.0, 8.0, 9.0,
                  np.nan, 12.0, 13.0],
        "pre_close": [9.9, 20.4, 11.1, 29.0, 299.0, 5.1, 41.0, 49.0, 8.1, 8.9,
                      10.0, 0.0, 13.1],
        "volume": [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 100, 100, 0],
    })


def test_universe_mask_excludes_hk_and_bj_by_suffix():
    df = _raw_rows()
    keep = df[universe_mask(df)]["ts_code"].tolist()
    assert keep == ["600000.SH", "688111.SH", "000001.SZ", "300750.SZ"]
    assert not any(c.endswith(".HK") for c in keep)
    assert not any(c.endswith(".BJ") for c in keep)


def test_thermo_prefix_filter_would_leak_hk_codes_suffix_filter_does_not():
    """附录 §1.3 的发现存证：4 位港股码会被 `LIKE '00%%'/'30%%'/'60%%'` 放行。"""
    code = _raw_rows()["ts_code"].astype(str)
    prefix_ok = code.str.startswith(("60", "68", "00", "30"))
    leaked = sorted(c for c in code[prefix_ok] if c.endswith(".HK"))
    assert leaked == ["0001.HK", "3008.HK", "6030.HK"]   # 00/30/60 开头的港股码被前缀写法放行
    assert "0700.HK" not in leaked                        # 07 开头的碰巧躲过，故"漏网"是随机的
    suffix_ok = code.str.endswith((".SH", ".SZ"))
    assert not (suffix_ok & code.str.endswith(".HK")).any()          # 后缀写法不漏


def test_universe_mask_drops_null_close_nonpositive_preclose_and_zero_volume():
    df = _raw_rows()
    dropped = df[~universe_mask(df)]["ts_code"].tolist()
    for bad in ("600001.SH", "600002.SH", "600003.SH"):
        assert bad in dropped


def test_universe_sql_uses_suffix_not_prefix():
    assert "LIKE '%.SH'" in UNIVERSE_SQL and "LIKE '%.SZ'" in UNIVERSE_SQL
    assert ".BJ" not in UNIVERSE_SQL and ".HK" not in UNIVERSE_SQL
    for banned in ("LIKE '60%'", "LIKE '68%'", "LIKE '00%'", "LIKE '30%'"):
        assert banned not in UNIVERSE_SQL
    for cond in ("close IS NOT NULL", "pre_close > 0", "volume > 0"):
        assert cond in UNIVERSE_SQL


def test_xsection_measures_is_stddev_samp_over_the_universe_only():
    df = _raw_rows()
    got = xsection_measures(df)
    kept = df[universe_mask(df)]
    ret = kept["close"].astype(float) / kept["pre_close"].astype(float) - 1.0
    assert int(got.loc[pd.Timestamp("2026-06-30"), "n"]) == 4
    assert got.loc[pd.Timestamp("2026-06-30"), "xs_sd"] == pytest.approx(ret.std(ddof=1))
    assert got.loc[pd.Timestamp("2026-06-30"), "xs_mean"] == pytest.approx(ret.mean())
    # 港股若混进来，数值必变（=宇宙不是摆设）
    hk = df[df["ts_code"].str.endswith(".HK")]
    ret_polluted = pd.concat([ret, hk["close"] / hk["pre_close"] - 1.0])
    assert got.loc[pd.Timestamp("2026-06-30"), "xs_sd"] != pytest.approx(
        ret_polluted.std(ddof=1))


# ================================================================ 3. X2 分母子集一致
def _sql_side_aggregates(rets: np.ndarray, window=CORR_WINDOW) -> pd.DataFrame:
    """固定成员面板 → SQL 四列（`n_full/sum_sd/sum_var/ew_ret_full`）的 pandas 等价物。"""
    df = pd.DataFrame(rets, index=_dates(len(rets)))
    sd = df.rolling(window).std(ddof=1)
    return pd.DataFrame({
        "n_full": sd.notna().sum(axis=1).astype(float),
        "sum_sd": sd.sum(axis=1, min_count=1),
        "sum_var": (sd ** 2).sum(axis=1, min_count=1),
        "ew_ret_full": df.mean(axis=1),
    })


def _direct_avg_pairwise_corr(rets: np.ndarray, t: int, window=CORR_WINDOW) -> float:
    """σ 加权平均逐对相关（Solnik-Roulet 的被估量），直接从协方差阵算。"""
    w = rets[t - window + 1: t + 1]
    cov = np.cov(w, rowvar=False, ddof=1)
    sd = np.sqrt(np.diag(cov))
    off = ~np.eye(cov.shape[0], dtype=bool)
    return float(cov[off].sum() / np.outer(sd, sd)[off].sum())


def test_implied_rho_equals_sigma_weighted_pairwise_correlation_exactly():
    """分母子集一致 ⟹ ρ̄ **恒等于** σ 加权平均逐对相关（不是近似）。"""
    rng = np.random.default_rng(4)
    n_stock, n_day = 6, 200
    common = rng.normal(size=(n_day, 1))
    rets = 0.01 * (0.7 * common + rng.normal(size=(n_day, n_stock)))
    raw = _sql_side_aggregates(rets)
    rho = implied_avg_correlation(raw)
    for t in (2 * CORR_WINDOW - 2, 150, n_day - 1):
        assert rho.iloc[t] == pytest.approx(_direct_avg_pairwise_corr(rets, t), abs=1e-10)


def test_implied_rho_stays_inside_minus_one_and_one():
    for seed in (0, 1, 2, 3):
        rng = np.random.default_rng(seed)
        n_stock, n_day = 8, 240
        common = rng.normal(size=(n_day, 1))
        rets = 0.01 * (rng.uniform(0.0, 1.5) * common + rng.normal(size=(n_day, n_stock)))
        rho = implied_avg_correlation(_sql_side_aggregates(rets)).dropna()
        assert len(rho) > 0
        assert rho.min() >= -1.0 - 1e-9 and rho.max() <= 1.0 + 1e-9


def test_implied_rho_is_one_when_every_stock_moves_identically():
    rng = np.random.default_rng(7)
    n_day = 150
    one = 0.01 * rng.normal(size=(n_day, 1))
    rets = np.repeat(one, 5, axis=1)
    rho = implied_avg_correlation(_sql_side_aggregates(rets)).dropna()
    assert np.allclose(rho.to_numpy(), 1.0, atol=1e-9)


def test_mismatched_sigma_p_subset_pushes_rho_out_of_bounds():
    """D4 反证：σ_p 若取**不同子集**（多一只高波动票）的等权收益，ρ̄ 会顶出 [−1,1]。"""
    rng = np.random.default_rng(11)
    n_day = 150
    common = rng.normal(size=(n_day, 1))
    subset = 0.005 * np.repeat(common, 3, axis=1) + 0.0002 * rng.normal(size=(n_day, 3))
    loud = 0.06 * rng.normal(size=(n_day, 1))          # 未进 c60=60 子集的高波动票
    raw_ok = _sql_side_aggregates(subset)
    assert implied_avg_correlation(raw_ok).dropna().max() <= 1.0 + 1e-9
    raw_bad = raw_ok.copy()
    raw_bad["ew_ret_full"] = np.hstack([subset, loud]).mean(axis=1)   # 分母子集不一致
    assert implied_avg_correlation(raw_bad).dropna().max() > 1.0


def test_implied_rho_is_nan_when_denominator_degenerates_not_silently_zero():
    idx = _dates(5)
    raw = pd.DataFrame({"n_full": [0.0, 1.0, 1.0, 2.0, 2.0],
                        "sum_sd": [np.nan, 0.02, 0.02, 0.0, 0.04],
                        "sum_var": [np.nan, 4e-4, 4e-4, 0.0, 1.6e-3],
                        "ew_ret_full": [0.0, 0.001, 0.002, 0.001, 0.002]}, index=idx)
    rho = implied_avg_correlation(raw, window=2)
    assert rho.isna().all()          # N<2 / 分母 0 / 缺料 → 一律 NaN，不静默给 0


def test_xsection_levels_takes_xs_sd_and_rejects_missing_columns():
    idx = _dates(80)
    disp = pd.DataFrame({"n": np.full(80, 2000.0), "xs_sd": np.linspace(0.01, 0.03, 80),
                         "xs_mean": np.zeros(80), "xs_med": np.zeros(80)}, index=idx)
    raw = pd.DataFrame({"n_full": np.full(80, 100.0), "sum_sd": np.full(80, 2.0),
                        "sum_var": np.full(80, 0.05), "ew_ret_full": np.zeros(80)},
                       index=idx)
    lv = xsection_levels(disp, raw)
    assert list(lv) == ["X1", "X2"]
    assert np.allclose(lv["X1"].to_numpy(), disp["xs_sd"].to_numpy())
    with pytest.raises(ValueError):
        xsection_levels(disp.drop(columns=["xs_sd"]), raw)
    with pytest.raises(ValueError):
        xsection_levels(disp, raw.drop(columns=["ew_ret_full"]))


def test_x1_is_not_the_cross_sectional_mean_absolute_return():
    """D3 明确不采纳的一阶量：X1 必须是 STDDEV_SAMP，不能是 |收益| 的截面均值。"""
    df = pd.DataFrame({"ts_code": ["600000.SH", "600001.SH", "600002.SH"],
                       "trade_date": [pd.Timestamp("2020-01-02")] * 3,
                       "close": [11.0, 12.0, 13.0], "pre_close": [10.0, 10.0, 10.0],
                       "volume": [1, 1, 1]})
    got = float(xsection_measures(df)["xs_sd"].iloc[0])
    ret = np.array([0.1, 0.2, 0.3])
    assert got == pytest.approx(ret.std(ddof=1))
    assert got != pytest.approx(np.abs(ret).mean())


# ================================================================ 4. 暖机掩码
def test_warmup_is_masked_to_nan_for_both_families():
    """房规 `level_signal` 把暖机段填 0（常数假信号）；本探针必须置 NaN 让交集丢掉它。"""
    levels = _fake_levels()
    for fam in FAMILIES:
        lv = levels[fam]
        for lb in GRID_LB:
            for zw in GRID_ZW:
                raw = level_signal(lv, lb, zw)
                masked = warmup_masked_level_signal(lv, lb, zw)
                n_warm = zw + lb - 2
                assert (raw.iloc[:zw - 1] == 0.0).all()
                assert masked.iloc[:n_warm].isna().all()
                assert masked.iloc[n_warm:].notna().all()
                assert np.allclose(masked.iloc[n_warm:].to_numpy(),
                                   raw.iloc[n_warm:].to_numpy(), atol=1e-12)


def test_shared_index_starts_after_the_longest_warmup():
    levels = _fake_levels()
    sigs = build_variant_signals(levels, FAMILIES)
    common = None
    for v in xsection_variant_grid():
        idx = sigs[v].dropna().index
        common = idx if common is None else common.intersection(idx)
    assert common[0] == levels["X1"].index[LONGEST_WARMUP]


def test_x2_leading_nan_from_the_60d_window_is_dropped_before_warmup_masking():
    """X2 前 60 日无 c60=60 → NaN；`build_variant_signals` 先 dropna 再按位置掩暖机。"""
    levels = _fake_levels(seed=2)
    levels["X2"] = levels["X2"].copy()
    levels["X2"].iloc[:CORR_WINDOW] = np.nan
    sigs = build_variant_signals(levels, FAMILIES)
    first_valid = levels["X2"].dropna().index[LONGEST_WARMUP]
    assert sigs[("X2", 20, 250, 5)].dropna().index[0] == first_valid
    assert sigs[("X1", 20, 250, 5)].dropna().index[0] == levels["X1"].index[LONGEST_WARMUP]


def test_variant_signals_share_one_series_per_lb_zw():
    levels = _fake_levels()
    sigs = build_variant_signals(levels, FAMILIES)
    assert len(sigs) == 32
    for fam in FAMILIES:
        for lb in GRID_LB:
            for zw in GRID_ZW:
                base = sigs[(fam, lb, zw, GRID_K[0])]
                for k in GRID_K[1:]:
                    assert sigs[(fam, lb, zw, k)] is base


# ================================================================ 5. 样本下限
def test_score_frames_raise_when_fewer_than_ten_nonoverlap_blocks():
    """`len(pts) < 10` 直接抛异常（不是静默降级）——k=40 在短样本上最先撞线。"""
    _, sigs, idx, und, ctrl = _fixture(seed=12)
    ok = idx[:500]                       # k=40 → 12 块（末块前瞻 NaN）→ 11 ≥ 10
    frames = build_score_frames(und.reindex(ok), ctrl.reindex(ok), ok)
    assert len(frames[40].pts) >= 10
    tiny = idx[:300]                     # k=40 → 7 块 < 10 → 必须抛，不许静默降级
    with pytest.raises(ValueError, match="不足以评分"):
        build_score_frames(und.reindex(tiny), ctrl.reindex(tiny), tiny)


def test_diagnostics_skip_pairs_with_fewer_than_thirty_common_days():
    idx = _dates(400)
    levels = {"X1": pd.Series(np.linspace(0.01, 0.03, 400), index=idx, name="X1"),
              "X2": pd.Series(np.linspace(0.2, 0.6, 400), index=idx, name="X2")}
    breadth = pd.DataFrame({c: np.linspace(0.3, 0.7, 400) for c in
                            ("pct_above_ma20", "pct_above_ma60", "hi_lo_diff20",
                             "hi_lo_diff60")}, index=idx)
    sizes = pd.DataFrame({"n_universe": np.linspace(2300.0, 5200.0, 400)}, index=idx)
    ew = pd.Series(np.tanh(np.linspace(-1, 1, 400)), index=idx)
    winner = ("X1", 5, 60, 20)
    sigs = build_variant_signals(levels, FAMILIES)
    dg = build_diagnostics(levels, sigs, ew, breadth, sizes, idx[:20], winner)
    assert dg.empty                                    # 共同日 <30 → 一行都不出
    dg2 = build_diagnostics(levels, sigs, ew, breadth, sizes, idx[:120], winner)
    assert not dg2.empty and (dg2["n_obs"] <= 120).all()


def test_net_sharpe_window_shorter_than_sixty_days_is_skipped():
    idx = pd.bdate_range("2014-01-02", periods=40)
    sig = pd.Series(np.tanh(np.linspace(-1, 1, 40)), index=idx)
    und = pd.Series(np.full(40, 0.001), index=idx)
    carry = pd.Series(np.full(40, 0.02), index=idx)
    assert net_sharpe_by_window(sig, 1.0, 5, und, carry, 3.0) == {}


# ================================================================ 6. 诊断限窗
def _diag_fixture():
    idx = pd.bdate_range("2014-01-02", periods=3000)      # 跨到 2025 年，覆盖选择窗以外
    rng = np.random.default_rng(21)
    levels = {"X1": pd.Series(0.02 + 0.003 * rng.normal(size=3000), index=idx, name="X1"),
              "X2": pd.Series(0.4 + 0.05 * rng.normal(size=3000), index=idx, name="X2")}
    breadth = pd.DataFrame({c: rng.uniform(0.2, 0.8, size=3000) for c in
                            ("pct_above_ma20", "pct_above_ma60", "hi_lo_diff20",
                             "hi_lo_diff60")}, index=idx)
    sizes = pd.DataFrame({"n_universe": np.linspace(2300.0, 5200.0, 3000),
                          "n_full_corr_subset": np.linspace(2000.0, 4800.0, 3000)},
                         index=idx)
    ew = pd.Series(np.tanh(rng.normal(size=3000)), index=idx)
    sigs = build_variant_signals(levels, FAMILIES)
    sel = idx[(idx >= pd.Timestamp(SELECTION[0])) & (idx <= pd.Timestamp(SELECTION[1]))]
    return levels, sigs, ew, breadth, sizes, sel


def test_diagnostics_never_look_outside_the_selection_window():
    """breadth.csv 2025 后含港股污染且止于 2026-06；诊断一律限选择窗（§5）。"""
    levels, sigs, ew, breadth, sizes, sel = _diag_fixture()
    dg = build_diagnostics(levels, sigs, ew, breadth, sizes, sel, ("X1", 20, 60, 10))
    assert not dg.empty
    assert (dg["n_obs"] <= len(sel)).all()
    assert sel[-1] <= pd.Timestamp(SELECTION[1])
    # 同一对在全窗上的相关必须与限窗版不同（否则"限窗"没生效）
    full = pd.concat([levels["X1"].rename("a"), breadth["pct_above_ma20"].rename("b")],
                     axis=1).dropna()
    row = dg[(dg["kind"] == "level_vs_breadth") & (dg["left"] == "X1")
             & (dg["right"] == "breadth_pct_above_ma20")].iloc[0]
    assert int(row["n_obs"]) < len(full)
    assert row["pearson"] != pytest.approx(float(full["a"].corr(full["b"])), abs=1e-12)


def test_diagnostics_carry_the_three_mandatory_families_of_columns():
    """面2 强制诊断：corr(X1,X2) 两版、corr(X1,广度)、corr(·,N_t)。"""
    levels, sigs, ew, breadth, sizes, sel = _diag_fixture()
    dg = build_diagnostics(levels, sigs, ew, breadth, sizes, sel, ("X1", 20, 60, 10))
    kinds = set(dg["kind"])
    assert {"family_overlap_level", "family_overlap_signal", "level_vs_breadth",
            "level_vs_universe_size"} <= kinds
    assert len(dg[(dg["kind"] == "level_vs_breadth") & (dg["left"] == "X1")
                  & (dg["right"] == "breadth_pct_above_ma20")]) == 1
    size_rows = dg[dg["kind"] == "level_vs_universe_size"]
    assert set(size_rows["left"]) == {"X1", "X2"}
    assert set(size_rows["right"]) == {"n_universe", "n_full_corr_subset"}
    assert len(dg[dg["kind"] == "family_overlap_level"]) == 1
    assert len(dg[dg["kind"] == "family_overlap_signal"]) == 1


def test_family_overlap_signal_uses_the_winner_lb_zw():
    levels, sigs, ew, breadth, sizes, sel = _diag_fixture()
    dg = build_diagnostics(levels, sigs, ew, breadth, sizes, sel, ("X2", 5, 250, 40))
    row = dg[dg["kind"] == "family_overlap_signal"].iloc[0]
    assert row["left"] == "signal_X1_lb5zw250" and row["right"] == "signal_X2_lb5zw250"


# ================================================================ X2 缓存锚点护栏（backlog B1）
def _anchor_frame() -> pd.DataFrame:
    """最小合法 X2 缓存：三个锚点日 + 正确的 `n_full`。"""
    return pd.DataFrame({
        "date": [pd.Timestamp(d) for d in ANCHORS_N_FULL],
        "n_full": list(ANCHORS_N_FULL.values()),
        "sum_sd": [1.0, 1.0, 1.0],
        "sum_var": [1.0, 1.0, 1.0],
        "ew_ret_full": [0.0, 0.0, 0.0],
    })


def test_avg_correlation_cache_read_is_anchor_checked(tmp_path, monkeypatch):
    """B1：X2 读缓存路径与 X1 同级设防——陈旧/损坏的 CSV 不得被静默采信。"""
    import backtest.xsection_probe as XP

    path = tmp_path / "avg_correlation.csv"
    monkeypatch.setattr(XP, "CORR_CACHE", path)

    _anchor_frame().to_csv(path, index=False)
    got = XP.build_avg_correlation()                     # 命中缓存分支，不连库
    assert len(got) == len(ANCHORS_N_FULL)

    bad = _anchor_frame()
    bad.loc[0, "n_full"] = int(bad.loc[0, "n_full"]) - 1  # 少一只票 = 宇宙口径漂了
    bad.to_csv(path, index=False)
    with pytest.raises(ValueError, match="X2 宇宙锚点"):
        XP.build_avg_correlation()

    _anchor_frame().iloc[1:].to_csv(path, index=False)    # 锚点日整行缺失（截断/陈旧）
    with pytest.raises(ValueError, match="X2 锚点日"):
        XP.build_avg_correlation()


def test_avg_correlation_fresh_build_is_anchor_checked_too(tmp_path, monkeypatch):
    """建缓存路径同样过闸——否则坏数据会被写进 CSV，下次读时才炸（或永不炸）。"""
    import backtest.xsection_probe as XP

    path = tmp_path / "avg_correlation.csv"
    monkeypatch.setattr(XP, "CORR_CACHE", path)
    bad = _anchor_frame().set_index("date")
    bad.loc[pd.Timestamp("2014-06-30"), "n_full"] = 999
    monkeypatch.setattr(XP, "_query_frame", lambda db, sql, columns: bad.copy())

    with pytest.raises(ValueError, match="X2 宇宙锚点"):
        XP.build_avg_correlation(db={"schema": "fake"}, force=True)
    assert not path.exists(), "锚点不过时绝不能把坏数据落盘"
