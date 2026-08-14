"""④ 条件化有效性探针的 TDD：预登记不变量 + 机器接入三规矩 + 六类必覆盖判例。

预登记 §10 点名必须覆盖的六类（本文件逐节对应）：
1. **三规矩**：8 点合成**一次**调用；统计量**不携带**闸①/闸③ 的一致性约束
   （余弦为负 / 两窗异号时统计量仍是有限值，绝不 −inf）；
   `min_shift = 2k = 40`、`max_shift = n_obs − 40`；
2. **垃圾尾剔除**：两个 committed 缓存的 2026-07-01 垃圾行必须被
   `dashboard.data.trim_incomplete_tail` 剔掉（有效末日 2026-06-30）；
3. **carry NaN 不入桶**：无 carry 日 → 标签 NaN → 不进任何桶
   （严禁并入"低 carry"桶，预登记 §3 S4 / D4）；
4. **每桶最小样本 `-inf`**：选择窗内任一桶 < 100 天 → 该变体统计量 `-inf`；
5. **余弦定义**：去均值后的余弦；二分退化为排序一致；退化输入 → NaN（判不过）；
6. **调节器单点映射**：`w(低效力桶)=0.5`、其余（含状态 NaN 日）`=1.0`，
   乘数集合恰为 {0.5, 1.0}，无第三档、无自由参数。

另加预登记冻结值的守卫（网格 8 点、k=20、250d 分位、闸④ 阈值算术、
选择端不含 2024-2026）与"零平行实现"的源码级守卫（不 import `pair_set_probe`、
不碰 `b3_*`）。

全部判例固定 seed、纯离线（不连库；只读 committed CSV）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest.conditional_probe as cp  # noqa: E402
from backtest.baseline import WINDOWS as BASELINE_WINDOWS  # noqa: E402
from backtest.conditional_probe import (  # noqa: E402
    BINARY_CUT, GATE4_THRESHOLD, GATE_ALPHA, GATE_WORST_TV_LIFT, HOLDOUT,
    INCUMBENT_WORST_TV_SHARPE, IC_OFFSETS, K_FORWARD, MIN_BUCKET_DAYS,
    MIN_CELL_POINTS, N_BUCKETS, N_VARIANTS, PCT_WINDOW, SELECTION, SPLITS,
    STATE_VARS, STAT_WINDOWS, TERCILE_CUTS, TRAIN, VAL, VOL_WINDOW, W_LOW_EFFICACY,
    W_OTHER, adjusted_position, bucket_labels, build_bucket_cells,
    conditional_ic_batch, demeaned_cosine, low_efficacy_bucket, make_batch_stat_fn,
    realized_vol, signed_statistic, variant_grid, window_conditional_ic,
)
from backtest.fusion_probe import nonoverlap_grid, rank_ic  # noqa: E402


def _dates(n):
    return pd.bdate_range("2014-01-02", periods=n)


def _identity(n):
    return np.arange(n, dtype=np.int64)[None, :]


# ================================================================ 0. 预登记冻结值
def test_grid_is_exactly_the_preregistered_8_points():
    grid = variant_grid()
    assert len(grid) == len(set(grid)) == N_VARIANTS == 8
    assert sorted({v[0] for v in grid}) == sorted(STATE_VARS)
    assert sorted({v[1] for v in grid}) == sorted(SPLITS) == ["binary", "tercile"]
    assert len(STATE_VARS) == 4          # ≤4 上限用满；广度不入
    assert N_BUCKETS == {"tercile": 3, "binary": 2}


def test_frozen_scalars_match_prereg():
    assert K_FORWARD == 20 and tuple(IC_OFFSETS) == tuple(range(20))
    assert PCT_WINDOW == 250 and VOL_WINDOW == 20
    assert MIN_BUCKET_DAYS == 100
    assert TERCILE_CUTS == (1.0 / 3.0, 2.0 / 3.0) and BINARY_CUT == 0.5
    assert (W_LOW_EFFICACY, W_OTHER) == (0.5, 1.0)
    assert GATE_ALPHA == 0.05
    assert GATE_WORST_TV_LIFT == 0.10
    assert INCUMBENT_WORST_TV_SHARPE == 1.000931
    assert GATE4_THRESHOLD == pytest.approx(INCUMBENT_WORST_TV_SHARPE
                                            + GATE_WORST_TV_LIFT, abs=1e-12)


def test_windows_come_from_baseline_and_exclude_2024_2026_from_selection():
    assert TRAIN == BASELINE_WINDOWS["2014-2020"]
    assert VAL == BASELINE_WINDOWS["2021-2023"]
    assert HOLDOUT == BASELINE_WINDOWS["2024-2026"]
    assert SELECTION == ("2014-01-01", "2023-12-31")
    assert STAT_WINDOWS == ("train_2014_2020", "val_2021_2023")
    assert all("2024" not in w for w in STAT_WINDOWS)


def test_no_parallel_harness_and_no_forbidden_imports():
    """零平行实现：禁 import `pair_set_probe`（配对集合专用）、禁碰 `b3_*`。"""
    src = (ROOT / "backtest" / "conditional_probe.py").read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines()
               if ln.lstrip().startswith(("import ", "from "))]
    assert imports, "源码解析失败"
    for ln in imports:
        assert "pair_set_probe" not in ln, ln
        assert "b3_" not in ln, ln
    # 运行期也不得偷偷 import（`__import__` / importlib 后门）
    assert "importlib" not in src and "__import__" not in src
    # 同秤件必须是 import 来的同一支实现，不是本地重写
    from backtest.engine import run_strategy
    from backtest.fusion_probe import forward_return, spearman_rows
    from backtest.metrics import sharpe
    from backtest.positions import production_position
    from dashboard.data import rolling_percentile, trim_incomplete_tail
    for fn in (forward_return, nonoverlap_grid, rank_ic, spearman_rows,
               production_position, run_strategy, sharpe, rolling_percentile,
               trim_incomplete_tail):
        assert fn in cp.SAME_SCALE_FUNCTIONS


# ================================================================ 1. 机器接入三规矩
def _machine_fixture(n=600, seed=0):
    """8 变体齐备的合成样本（每桶远超 100 天），供三规矩判例复用。"""
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    sig = rng.normal(size=n)
    fwd = 0.3 * sig + rng.normal(size=n)
    labels = {}
    for j, (sv, sp) in enumerate(variant_grid()):
        nb = N_BUCKETS[sp]
        labels[(sv, sp)] = ((np.arange(n) + j) % nb).astype(float)
    return idx, sig, fwd, labels


def test_rule1_eight_points_are_composed_into_a_single_machine_call(monkeypatch):
    idx, sig, fwd, labels = _machine_fixture()
    calls = []
    real = cp.selection_permutation_test

    def spy(variants, **kw):
        calls.append({"variants": tuple(variants), **kw})
        return real(variants, **kw)

    monkeypatch.setattr(cp, "selection_permutation_test", spy)
    res, _cells, meta = cp.run_machine(sig, fwd, labels, idx, n_perm=5, seed=0)

    assert len(calls) == 1, "8 点必须合成同一次调用，不做 4 次变量级独立调用"
    assert calls[0]["variants"] == tuple(variant_grid())
    assert len(calls[0]["variants"]) == 8
    assert calls[0]["scheme"] == "rotation"
    assert res.p_selected > 0 and np.isfinite(res.observed_best)


def test_rule3_shift_bounds_are_2k_and_n_minus_2k(monkeypatch):
    idx, sig, fwd, labels = _machine_fixture(n=600)
    seen = {}
    real = cp.selection_permutation_test

    def spy(variants, **kw):
        seen.update(kw)
        return real(variants, **kw)

    monkeypatch.setattr(cp, "selection_permutation_test", spy)
    cp.run_machine(sig, fwd, labels, idx, n_perm=3, seed=0)
    assert seen["n_obs"] == 600
    assert seen["min_shift"] == 2 * K_FORWARD == 40
    assert seen["max_shift"] == 600 - 2 * K_FORWARD == 560


def _opposite_halves_fixture(n=800, seed=1):
    """两半窗**有符号统计量反号**（→ 闸① 不过、闸③ 余弦为负）的确定性构造。

    `label = i % 2` → 每条 offset 网格（步长 20）整条落在同一个桶里；
    前半段 fwd = +sig(上桶) / −sig(下桶)，后半段反转 → 桶 IC 向量前后半完全相反。
    """
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    sig = rng.normal(size=n)
    lab = (np.arange(n) % 2).astype(float)
    fwd = np.empty(n)
    first = np.arange(n) < n // 2
    up = lab == 1.0
    fwd[first & up] = sig[first & up]
    fwd[first & ~up] = -sig[first & ~up]
    fwd[~first & up] = -sig[~first & up]
    fwd[~first & ~up] = sig[~first & ~up]
    return idx, sig, fwd, lab


def test_rule2_statistic_carries_no_gate_constraint_and_never_minus_inf():
    """闸①（两窗同号）/ 闸③（余弦>0）失败时统计量仍是**有限值**，不得退化为 −inf。"""
    idx, sig, fwd, lab = _opposite_halves_fixture()
    half = len(idx) // 2
    s = pd.Series(sig, index=idx)
    f = pd.Series(fwd, index=idx)
    L = pd.Series(lab, index=idx)

    early = window_conditional_ic(s.iloc[:half], f.iloc[:half], L.iloc[:half], "binary")
    late = window_conditional_ic(s.iloc[half:], f.iloc[half:], L.iloc[half:], "binary")
    # 构造成立：两半窗有符号统计量精确反号，余弦 = −1（闸①、闸③ 双双不过）
    assert early["signed_stat"] == pytest.approx(2.0)
    assert late["signed_stat"] == pytest.approx(-2.0)
    assert demeaned_cosine(early["bucket_ic"], late["bucket_ic"]) == pytest.approx(-1.0)

    # 而机器统计量照样是有限值：−inf 的唯一来源是"该行无法评分"
    cells = {("X", "binary"): build_bucket_cells(idx, fwd, lab, "binary")}
    stat = make_batch_stat_fn(sig, cells)(("X", "binary"), _identity(len(idx)))
    assert np.isfinite(stat[0]) and stat[0] >= 0.0
    for bc in cells[("X", "binary")]:
        assert bc.n_days >= MIN_BUCKET_DAYS      # 样本充足 → 不该有 −inf 的理由


def test_rule2_minus_inf_only_from_insufficient_sample_not_from_sign_flip():
    """同一份数据：桶样本足 → 有限；人为把一个桶压到 <100 天 → −inf。"""
    idx, sig, fwd, lab = _opposite_halves_fixture()
    thin = lab.copy()
    thin[np.where(lab == 1.0)[0][MIN_BUCKET_DAYS - 1:]] = np.nan   # 上桶只留 99 天
    cells_ok = {("X", "binary"): build_bucket_cells(idx, fwd, lab, "binary")}
    cells_thin = {("X", "binary"): build_bucket_cells(idx, fwd, thin, "binary")}
    ident = _identity(len(idx))
    assert np.isfinite(make_batch_stat_fn(sig, cells_ok)(("X", "binary"), ident)[0])
    assert make_batch_stat_fn(sig, cells_thin)(("X", "binary"), ident)[0] == -np.inf


# ================================================================ 2. 垃圾尾剔除
def test_committed_caches_drop_the_2026_07_01_garbage_row():
    """两个缓存的物理末行是 2026-07-01 垃圾日，读取层必须剔到 2026-06-30。"""
    raw_t = pd.read_csv(ROOT / "backtest/output/thermometer.csv",
                        parse_dates=["date"])["date"]
    raw_a = pd.read_csv(ROOT / "backtest/output/market_turnover.csv",
                        parse_dates=["date"])["date"]
    # 前提：原始 CSV 里确实有那一行（否则本判例是空转）
    assert raw_t.max() == pd.Timestamp("2026-07-01")
    assert raw_a.max() == pd.Timestamp("2026-07-01")

    lu = cp.load_limitup_level()
    amt = cp.load_turnover_level()
    assert lu.index.max() == pd.Timestamp("2026-06-30")
    assert amt.index.max() == pd.Timestamp("2026-06-30")
    assert pd.Timestamp("2026-07-01") not in lu.index
    assert pd.Timestamp("2026-07-01") not in amt.index


def test_limitup_tail_guard_is_byte_identical_to_the_dashboard_house_rule():
    """判据列取 `n_active`（`dashboard/data.py:126` 房规），不是 `lu_ratio` 自身。"""
    from dashboard.data import trim_incomplete_tail
    t = (pd.read_csv(ROOT / "backtest/output/thermometer.csv", parse_dates=["date"])
         .set_index("date").sort_index())
    house = t.loc[trim_incomplete_tail(t["n_active"].astype(float)).index, "lu_ratio"]
    got = cp.load_limitup_level()
    assert got.index.equals(house.index)
    assert np.allclose(got.to_numpy(), house.astype(float).to_numpy(), equal_nan=True)
    assert got.index.max() == pd.Timestamp("2026-06-30")


# ================================================================ 3. carry NaN 不入桶
def test_nan_percentile_never_lands_in_any_bucket():
    pct = pd.Series([np.nan, 0.10, np.nan, 0.50, 0.90, np.nan], index=_dates(6))
    ter = bucket_labels(pct, "tercile")
    bina = bucket_labels(pct, "binary")
    assert ter.isna().tolist() == [True, False, True, False, False, True]
    assert bina.isna().tolist() == [True, False, True, False, False, True]
    assert ter.dropna().tolist() == [0.0, 1.0, 2.0]
    assert bina.dropna().tolist() == [0.0, 1.0, 1.0]


def test_carry_days_before_first_carry_date_are_not_lumped_into_low_bucket():
    """2015-04 前根本没有 carry 值 → 标签 NaN → **不得**并入"低 carry"桶。"""
    idx = pd.bdate_range("2014-01-02", periods=800)
    carry = pd.Series(np.linspace(-0.1, 0.3, 400), index=idx[400:])   # 后半段才有值
    pct = cp.rolling_percentile(carry.dropna(), window=50).reindex(idx)
    lab = bucket_labels(pct, "tercile")

    pre = lab.iloc[:400]
    assert pre.isna().all(), "无 carry 日必须全部是 NaN 标签"
    assert (pre == 0.0).sum() == 0, "严禁把无 carry 日并入低 carry 桶（会造假状态）"

    # 桶总天数之和 == 有标签天数（NaN 日一个都没被算进去）
    fwd = np.zeros(len(idx))
    cells = build_bucket_cells(idx, fwd, lab.to_numpy(dtype=float), "tercile")
    assert sum(c.n_days for c in cells) == int(lab.notna().sum()) < len(idx)
    pre_pos = set(range(400))
    for c in cells:
        for p in c.positions:
            assert not (set(p.tolist()) & pre_pos)


def test_carry_state_series_starts_at_first_carry_day():
    """S4 的原值序列自首个有 carry 日起算（预登记 D4），不 fillna(0) 前推。"""
    und = pd.Series(np.linspace(-0.01, 0.01, 300),
                    index=pd.bdate_range("2014-01-02", periods=300))
    carry = pd.Series(np.linspace(0.0, 0.2, 100),
                      index=pd.bdate_range("2015-04-16", periods=100))
    levels = {"S4_carry": carry.dropna()}
    assert levels["S4_carry"].index[0] == pd.Timestamp("2015-04-16")
    # realized_vol 的暖机也不得回填
    rv = realized_vol(und, VOL_WINDOW)
    assert rv.iloc[:VOL_WINDOW - 1].isna().all() and np.isfinite(rv.iloc[VOL_WINDOW - 1])


# ================================================================ 4. 每桶最小样本 −inf
@pytest.mark.parametrize("n_bucket1, expect_inf", [(MIN_BUCKET_DAYS, False),
                                                   (MIN_BUCKET_DAYS - 1, True)])
def test_min_bucket_days_is_a_hard_floor_at_exactly_100(n_bucket1, expect_inf):
    n = 600
    rng = np.random.default_rng(7)
    idx = _dates(n)
    sig = rng.normal(size=n)
    fwd = rng.normal(size=n)
    lab = np.zeros(n)
    lab[rng.permutation(n)[:n_bucket1]] = 1.0
    cells = {("X", "binary"): build_bucket_cells(idx, fwd, lab, "binary")}
    stat = make_batch_stat_fn(sig, cells)(("X", "binary"), _identity(n))
    assert bool(stat[0] == -np.inf) is expect_inf


def test_min_bucket_floor_applies_to_every_bucket_of_a_tercile():
    n = 900
    rng = np.random.default_rng(8)
    idx = _dates(n)
    sig, fwd = rng.normal(size=n), rng.normal(size=n)
    lab = np.zeros(n)         # 低桶 = 0~299 与 750~899 共 450 天
    lab[300:700] = 1.0
    lab[700:750] = 2.0        # 高桶只有 50 天 < 100 → 整个变体不可评分
    cells = {("X", "tercile"): build_bucket_cells(idx, fwd, lab, "tercile")}
    assert make_batch_stat_fn(sig, cells)(("X", "tercile"),
                                          _identity(n))[0] == -np.inf
    res = window_conditional_ic(pd.Series(sig, index=idx), pd.Series(fwd, index=idx),
                                pd.Series(lab, index=idx), "tercile")
    assert res["feasible_min_bucket"] is False
    assert res["bucket_n_days"] == [450, 400, 50]


def test_minus_inf_is_constant_across_all_resamples_for_an_infeasible_variant():
    n = 400
    rng = np.random.default_rng(9)
    idx = _dates(n)
    sig, fwd = rng.normal(size=n), rng.normal(size=n)
    lab = np.zeros(n)
    lab[:20] = 1.0
    cells = {("X", "binary"): build_bucket_cells(idx, fwd, lab, "binary")}
    im = np.vstack([np.arange(n), np.roll(np.arange(n), 55), np.roll(np.arange(n), 111)])
    out = make_batch_stat_fn(sig, cells)(("X", "binary"), im)
    assert out.shape == (3,) and np.all(out == -np.inf)


def test_thin_offset_cells_are_dropped_but_do_not_produce_minus_inf():
    """cell < `MIN_CELL_POINTS` 点（Spearman 无定义）→ 权重 0，不是闸门、不 −inf。"""
    n = 600
    rng = np.random.default_rng(10)
    idx = _dates(n)
    sig, fwd = rng.normal(size=n), rng.normal(size=n)
    lab = np.zeros(n)
    # 上桶集中在开头 120 天 → 只覆盖前 6 条 offset 网格的少数格点
    lab[:120] = 1.0
    cells = build_bucket_cells(idx, fwd, lab, "binary")
    up = cells[1]
    assert up.n_days == 120 >= MIN_BUCKET_DAYS
    assert len(up.positions) <= len(IC_OFFSETS)
    assert all(len(p) >= MIN_CELL_POINTS for p in up.positions)
    stat = make_batch_stat_fn(sig, {("X", "binary"): cells})(("X", "binary"),
                                                             _identity(n))
    assert np.isfinite(stat[0])


# ================================================================ 5. 余弦定义
def test_demeaned_cosine_is_plus_one_for_proportional_shapes():
    assert demeaned_cosine([0.1, 0.2, 0.3], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    # 去均值 → 加常数不改变结果（"水平"不算形状）
    assert demeaned_cosine([0.1, 0.2, 0.3],
                           np.array([1.0, 2.0, 3.0]) + 7.5) == pytest.approx(1.0)


def test_demeaned_cosine_is_minus_one_for_reversed_shapes():
    assert demeaned_cosine([0.1, 0.2, 0.3], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_demeaned_cosine_of_two_buckets_degenerates_to_order_agreement():
    """二分（2 维）时余弦只能是 ±1 —— 预登记明文接受此退化。"""
    rng = np.random.default_rng(11)
    for _ in range(20):
        a = rng.normal(size=2)
        b = rng.normal(size=2)
        c = demeaned_cosine(a, b)
        same_order = (a[1] - a[0]) * (b[1] - b[0]) > 0
        assert c == pytest.approx(1.0 if same_order else -1.0)


def test_demeaned_cosine_returns_nan_on_degenerate_or_nonfinite_input():
    assert np.isnan(demeaned_cosine([0.2, 0.2, 0.2], [1.0, 2.0, 3.0]))   # 零向量
    assert np.isnan(demeaned_cosine([0.1, np.nan], [1.0, 2.0]))          # NaN 桶 IC
    with pytest.raises(ValueError):
        demeaned_cosine([0.1, 0.2, 0.3], [1.0, 2.0])                     # 维数不符


def test_gate3_cosine_is_computed_on_bucket_ic_vectors_of_the_two_halves():
    """闸③ 的向量就是各桶条件 IC 组成的向量（三分 3 维 / 二分 2 维）。"""
    idx, sig, fwd, lab = _opposite_halves_fixture(n=800)
    s, f = pd.Series(sig, index=idx), pd.Series(fwd, index=idx)
    L = pd.Series(lab, index=idx)
    info = cp.halves_cosine(s, f, L, "binary", idx)
    assert info["n_early"] == info["n_late"] == 400
    assert len(info["early"]["bucket_ic"]) == N_BUCKETS["binary"] == 2
    assert info["cosine"] == pytest.approx(
        demeaned_cosine(info["early"]["bucket_ic"], info["late"]["bucket_ic"]))
    assert info["cosine"] < 0                       # 本构造下闸③ 必不过


# ================================================================ 6. 调节器单点映射
def test_low_efficacy_bucket_follows_the_signed_statistic():
    assert low_efficacy_bucket(+0.2, "tercile") == 0     # IC_高 > IC_低 → 低桶效力低
    assert low_efficacy_bucket(-0.2, "tercile") == 2     # 反号 → 高桶效力低
    assert low_efficacy_bucket(+0.2, "binary") == 0
    assert low_efficacy_bucket(-0.2, "binary") == 1
    assert low_efficacy_bucket(0.0, "tercile") == -1     # 无方向可指
    assert low_efficacy_bucket(float("nan"), "binary") == -1
    with pytest.raises(ValueError):
        low_efficacy_bucket(0.5, "quartile")


def test_adjuster_is_a_single_point_map_with_only_0p5_and_1p0():
    idx = _dates(10)
    pos = pd.Series([1, 1, 1, 0, 1, 0, 1, 1, 0, 1], index=idx, dtype=float)
    lab = pd.Series([0, 1, 2, 0, np.nan, 2, 1, 0, 1, 2], index=idx, dtype=float)
    adj = adjusted_position(pos, lab, low_bucket=0)
    ratio = (adj / pos.replace(0.0, np.nan)).dropna()
    assert set(np.unique(ratio.to_numpy())) <= {W_LOW_EFFICACY, W_OTHER} == {0.5, 1.0}
    # 只有低效力桶（0）被降权；中桶(1)/高桶(2)/NaN 日一律 1.0
    assert adj.tolist() == [0.5, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.5, 0.0, 1.0]


def test_adjuster_leaves_nan_state_days_untouched():
    idx = _dates(6)
    pos = pd.Series(1.0, index=idx)
    lab = pd.Series([np.nan, np.nan, 0.0, 1.0, np.nan, 0.0], index=idx)
    adj = adjusted_position(pos, lab, low_bucket=0)
    assert adj.tolist() == [1.0, 1.0, 0.5, 1.0, 1.0, 0.5]


def test_adjuster_is_longflat_first_then_multiplied():
    """先 long-flat 映射再乘 w → 仓位取值集合 ⊂ {0, 0.5, 1}，永不出现空头。"""
    idx = _dates(200)
    rng = np.random.default_rng(12)
    sig = pd.Series(rng.normal(size=200), index=idx)
    lab = pd.Series((np.arange(200) % 3).astype(float), index=idx)
    pos_lf = cp.production_position(sig).astype(float)
    adj = adjusted_position(pos_lf, lab, low_bucket=2)
    assert set(np.unique(adj.to_numpy())) <= {0.0, 0.5, 1.0}
    assert (adj < 0).sum() == 0
    assert (adj[pos_lf == 0] == 0).all()            # 空仓日不会被调节器"调出"仓位


def test_adjuster_is_identity_when_no_bucket_is_designated():
    idx = _dates(20)
    pos = pd.Series(np.tile([1.0, 0.0], 10), index=idx)
    lab = pd.Series((np.arange(20) % 2).astype(float), index=idx)
    assert adjusted_position(pos, lab, low_bucket=-1).equals(pos)


# ================================================================ 7. 条件 IC 口径一致性
def test_conditional_ic_reduces_to_house_rank_ic_when_k_is_one():
    """k=1 → 只有一条 offset 网格覆盖全窗 → 条件 IC == `fusion_probe.rank_ic`。"""
    n = 300
    rng = np.random.default_rng(13)
    idx = _dates(n)
    sig = rng.normal(size=n)
    fwd = 0.4 * sig + rng.normal(size=n)
    lab = (np.arange(n) % 2).astype(float)
    cells = build_bucket_cells(idx, fwd, lab, "binary", k=1)
    for b, bc in enumerate(cells):
        got = float(conditional_ic_batch(sig, _identity(n), bc)[0])
        m = lab == float(b)
        want = rank_ic(pd.Series(sig[m]), pd.Series(fwd[m]))["ic"]
        assert got == pytest.approx(want, abs=1e-12)


def test_conditional_ic_is_the_sample_weighted_mean_over_offsets():
    """20 个 offset 的 IC 按 cell 样本数加权平均（预登记 §5 主口径）——逐位复算。"""
    n = 600
    rng = np.random.default_rng(14)
    idx = _dates(n)
    sig = rng.normal(size=n)
    fwd = 0.25 * sig + rng.normal(size=n)
    lab = (rng.random(n) < 0.5).astype(float)
    cells = build_bucket_cells(idx, fwd, lab, "binary")
    for bc in cells:
        num = den = 0.0
        for off in IC_OFFSETS:
            g = idx.get_indexer(nonoverlap_grid(idx, K_FORWARD, off))
            sel = g[lab[g] == float(bc.bucket)]
            if len(sel) < MIN_CELL_POINTS:
                continue
            ic = rank_ic(pd.Series(sig[sel]), pd.Series(fwd[sel]))["ic"]
            num += ic * len(sel)
            den += len(sel)
        assert float(conditional_ic_batch(sig, _identity(n), bc)[0]) == \
            pytest.approx(num / den, abs=1e-12)


def test_signed_statistic_signs_are_frozen():
    assert signed_statistic([0.1, 0.5, 0.4], "tercile") == pytest.approx(0.3)  # 高−低
    assert signed_statistic([0.4, 0.1], "binary") == pytest.approx(-0.3)       # 上−下
    with pytest.raises(ValueError):
        signed_statistic([0.1, 0.2], "quartile")


def test_machine_statistic_is_the_absolute_value_of_the_signed_gap():
    """进机器的是 `|有符号值|`（双侧）——同号判定留给闸①，不编码进统计量。"""
    n = 600
    rng = np.random.default_rng(15)
    idx = _dates(n)
    sig = rng.normal(size=n)
    fwd = 0.3 * sig + rng.normal(size=n)
    lab = (np.arange(n) % 2).astype(float)
    cells = build_bucket_cells(idx, fwd, lab, "binary")
    obs = window_conditional_ic(pd.Series(sig, index=idx), pd.Series(fwd, index=idx),
                                pd.Series(lab, index=idx), "binary")
    stat = make_batch_stat_fn(sig, {("X", "binary"): cells})(("X", "binary"),
                                                             _identity(n))
    assert stat[0] == pytest.approx(abs(obs["signed_stat"]), abs=1e-12)


def test_bucket_labels_rejects_unknown_split():
    with pytest.raises(ValueError):
        bucket_labels(pd.Series([0.1, 0.9], index=_dates(2)), "quartile")
