"""⑤ 配对集合探针（`backtest/pair_set_probe.py`）的单元判例。

覆盖五类要害：
1. **预登记完整性**：集合恰 4 个且成员逐字对表；A 与冻结根 `config_4pairs.csv` 同构；
   885000.WI 不出现在任何集合；参数锁死在现役生产值；D_rev 是诊断变体、不进闸门集合；
2. **等权、无权重优化**：子集因子 == 该子集逐对因子的**算术平均**（没有任何权重自由度）；
3. **闸门判定**：先 ⓪ 后收益层、边界含等号、任一条失手翻 STOP、诊断行不进 OVERALL、
   红利同源判定短路（后续判据不再评）；
4. **机器接口 == 房规**：`make_sharpe_scorer` / `make_ic_scorer` 的观测行分别等于
   `engine.run_strategy`+`metrics.sharpe` 与 `fusion_probe.rank_ic`；
5. **循环移位精确保持换手**（"变体 = 已算好的仓位序列"这条写法成立的前提）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.fusion_probe import forward_return, nonoverlap_grid, rank_ic  # noqa: E402
from backtest.metrics import sharpe, turnover  # noqa: E402
from backtest.pair_set_probe import (  # noqa: E402
    ALPHA, BONFERRONI_M, CHALLENGERS, DIAG_SET, DIV_CORR_GATE, DIVIDEND_CODES,
    DIVIDEND_SET, GATE_WORST_TV_LIFT, INCUMBENT, K_FORWARD, LOOKBACK, PAIR_LEGS,
    SETS, SMOOTHING, STAT_WINDOWS, STYLE_NAMES, Z_WINDOW, build_factor,
    build_pair_factors, dividend_gate, evaluate_gates, make_ic_scorer,
    make_sharpe_scorer, map_position, pair_configs_for, shift_bounds,
)
from backtest.positions import production_position, to_position  # noqa: E402
from backtest.selection_permutation import build_index_matrix  # noqa: E402
from signals.equal_weight.generate_signal import (  # noqa: E402
    calculate_contrast_equal_weight_signal, load_pair_configs,
)

ALL_COLUMNS = STYLE_NAMES + list(PAIR_LEGS["dividend"])


@pytest.fixture
def prices() -> pd.DataFrame:
    """合成价格宽表（10 列 = 八条风格 + 红利两码），随机游走、恒正。"""
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2015-01-01", periods=520)
    out = {}
    for i, col in enumerate(ALL_COLUMNS):
        steps = rng.normal(0.0002, 0.011, len(idx))
        out[col] = 1000.0 * np.exp(np.cumsum(steps)) * (1 + 0.01 * i)
    return pd.DataFrame(out, index=idx)


# ---------------------------------------------------------------- 1. 预登记完整性
def test_registered_sets_are_exactly_four_with_exact_membership():
    assert list(SETS) == ["A_four_pairs_incumbent", "B_500_1000_2000",
                          "C_1000_only", "D_four_plus_dividend"]
    assert SETS["A_four_pairs_incumbent"] == ("300", "500", "1000", "2000")
    assert SETS["B_500_1000_2000"] == ("500", "1000", "2000")
    assert SETS["C_1000_only"] == ("1000",)
    assert SETS["D_four_plus_dividend"] == ("300", "500", "1000", "2000", "dividend")
    assert INCUMBENT == "A_four_pairs_incumbent"
    assert set(CHALLENGERS) == set(SETS) - {INCUMBENT} and len(CHALLENGERS) == 3


def test_diagnostic_reversed_set_is_not_a_registered_set():
    """D_rev 只是方向反转的诊断变体：不在 SETS、不进机器变体、不进闸门。"""
    assert DIAG_SET not in SETS
    assert DIAG_SET not in CHALLENGERS


def test_885000_fund_index_is_never_used_as_a_data_code():
    """885000.WI 不是任何集合的成员、不进任何 `PairConfig`、不在被读取的代码表里。

    ⚠️ **名实对表**：模块源码里**确实出现** "885000" 字样——模块 docstring 的预登记声明、
    `summary.json` 的未测项登记、CLI 末行的提醒。那些是**文档串**。
    本判例断言的是"**从不作为数据码被使用**"，不是"字符串从不出现"。
    """
    blob = repr(SETS) + repr(PAIR_LEGS) + repr(STYLE_NAMES) + repr(DIVIDEND_CODES)
    assert "885000" not in blob and "偏股基金" not in blob
    for name in list(SETS) + [DIAG_SET]:
        cols = [c for p in pair_configs_for(name)
                for c in (p.left_column, p.right_column)]
        assert not any(("885000" in c) or ("偏股基金" in c) for c in cols)


def test_parameters_are_locked_to_production_values():
    assert (LOOKBACK, Z_WINDOW, SMOOTHING) == (20, 40, 5)
    assert K_FORWARD == 20
    assert GATE_WORST_TV_LIFT == 0.15
    assert DIV_CORR_GATE == 0.9
    assert (ALPHA, BONFERRONI_M) == (0.05, 3)


def test_incumbent_set_matches_frozen_config_4pairs_file():
    frozen = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    mine = pair_configs_for(INCUMBENT)
    assert [(p.group, p.left_column, p.right_column, p.direction) for p in mine] == \
           [(p.group, p.left_column, p.right_column, p.direction) for p in frozen]


def test_incumbent_factor_identical_to_frozen_root_call(prices):
    frozen = calculate_contrast_equal_weight_signal(
        prices, lookback=LOOKBACK, z_window=Z_WINDOW, smoothing_window=SMOOTHING,
        pair_configs=load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv"),
    )["factor_value"].astype(float)
    pd.testing.assert_series_equal(build_factor(prices, INCUMBENT), frozen,
                                   check_names=False)


def test_subset_configs_are_renumbered_from_one():
    for name in SETS:
        groups = [p.group for p in pair_configs_for(name)]
        assert groups == list(range(1, len(groups) + 1))


def test_unknown_set_is_rejected():
    with pytest.raises(KeyError):
        pair_configs_for("E_five_pairs")


def test_reversed_diagnostic_flips_only_the_dividend_leg():
    base = {p.group: (p.left_column, p.right_column) for p in pair_configs_for(DIVIDEND_SET)}
    rev = {p.group: (p.left_column, p.right_column) for p in pair_configs_for(DIAG_SET)}
    assert [base[g] for g in (1, 2, 3, 4)] == [rev[g] for g in (1, 2, 3, 4)]
    assert rev[5] == tuple(reversed(base[5])) == tuple(reversed(PAIR_LEGS["dividend"]))


# ---------------------------------------------------------------- 2. 等权、无权重自由度
@pytest.mark.parametrize("name", list(SETS))
def test_set_factor_is_plain_mean_of_its_pair_factors(prices, name):
    """组内等权 = 算术平均（平滑是线性算子，可与平均交换）——**没有权重参数可扫**。"""
    legs = build_pair_factors(prices)
    want = sum(legs[k] for k in SETS[name]) / len(SETS[name])
    pd.testing.assert_series_equal(build_factor(prices, name), want, check_names=False)


def test_single_pair_set_equals_that_pair_alone(prices):
    legs = build_pair_factors(prices)
    pd.testing.assert_series_equal(build_factor(prices, "C_1000_only"), legs["1000"],
                                   check_names=False)


def test_dropping_a_pair_actually_changes_the_factor(prices):
    a = build_factor(prices, INCUMBENT)
    b = build_factor(prices, "B_500_1000_2000")
    assert float((a - b).abs().max()) > 1e-6


# ---------------------------------------------------------------- 3. 红利腿诊断闸
def _div_diag(max_corr: float) -> dict:
    return {"same_source_verdict": bool(max_corr > DIV_CORR_GATE),
            "max_abs_corr_vs_incumbent_family": max_corr}


def test_dividend_gate_fires_on_high_correlation():
    idx = pd.bdate_range("2015-01-01", periods=300)
    rng = np.random.default_rng(3)
    base = pd.Series(rng.normal(size=len(idx)), index=idx)
    twin = base * 0.99 + 0.01 * pd.Series(rng.normal(size=len(idx)), index=idx)
    prices = pd.DataFrame({c: 100.0 + np.arange(len(idx)) * 0.1 for c in ALL_COLUMNS},
                          index=idx)
    got = dividend_gate(twin, base, {"300": base, "dividend": twin}, prices)
    assert got["same_source_verdict"] is True
    assert got["max_abs_corr_vs_incumbent_family"] > DIV_CORR_GATE


def test_dividend_gate_passes_on_low_correlation():
    idx = pd.bdate_range("2015-01-01", periods=300)
    rng = np.random.default_rng(4)
    a = pd.Series(rng.normal(size=len(idx)), index=idx)
    b = pd.Series(rng.normal(size=len(idx)), index=idx)
    prices = pd.DataFrame({c: 100.0 + np.arange(len(idx)) * 0.1 for c in ALL_COLUMNS},
                          index=idx)
    got = dividend_gate(b, a, {"300": a, "dividend": b}, prices)
    assert got["same_source_verdict"] is False


def test_dividend_gate_reports_price_vs_total_return_evidence(prices):
    """诊断字段齐备（覆盖天数 + 两码日收益 corr + 净值比漂移）——同源判别的取证链。"""
    legs = build_pair_factors(prices)
    got = dividend_gate(legs["dividend"], build_factor(prices, INCUMBENT), legs, prices)
    for key in ("n_days_aligned_panel", "daily_return_corr_between_two_legs",
                "level_ratio_first", "level_ratio_last", "implied_annual_drift_of_ratio",
                "relative_return_annualized", "relative_return_share_negative_days"):
        assert key in got and got[key] is not None
    # 字段名与语义对表：这是**对齐后面板**长度，不是红利腿自身行数（M-5）
    assert got["n_days_aligned_panel"] == len(prices)
    assert "n_days_dividend_leg" not in got


# ---------------------------------------------------------------- 4. 闸门判定
def _panel(worst=1.0, turn=10.0, ex1=1.0,
           inc_worst=1.0, inc_turn=10.0, inc_ex1=1.0) -> pd.DataFrame:
    return pd.DataFrame([
        {"pair_set": INCUMBENT, "worst_tv_sharpe": inc_worst,
         "turnover_selection_2014_2023": inc_turn,
         "sharpe_ex_top1_selection_2014_2023": inc_ex1,
         "sharpe_ex_top2_selection_2014_2023": inc_ex1 - 0.1},
        {"pair_set": "B_500_1000_2000", "worst_tv_sharpe": worst,
         "turnover_selection_2014_2023": turn,
         "sharpe_ex_top1_selection_2014_2023": ex1,
         "sharpe_ex_top2_selection_2014_2023": ex1 - 0.1},
    ])


def _ic(cand_ic=0.3, inc_ic=0.2, excl=True) -> tuple:
    rows, diffs = [], []
    for w in STAT_WINDOWS:
        rows.append({"pair_set": INCUMBENT, "window": w, "ic": inc_ic})
        rows.append({"pair_set": "B_500_1000_2000", "window": w, "ic": cand_ic})
        diffs.append({"challenger": "B_500_1000_2000", "window": w,
                      "ci_excludes_zero": excl})
    return pd.DataFrame(rows), pd.DataFrame(diffs)


def _judge(**kw) -> pd.DataFrame:
    panel = _panel(**{k: v for k, v in kw.items()
                      if k in ("worst", "turn", "ex1", "inc_worst", "inc_turn", "inc_ex1")})
    ic_rep, ic_diff = _ic(**{k: v for k, v in kw.items()
                             if k in ("cand_ic", "inc_ic", "excl")})
    return evaluate_gates(panel, ic_rep, ic_diff, ("B_500_1000_2000",), _div_diag(0.1))


def _overall(rep: pd.DataFrame, name="B_500_1000_2000") -> bool:
    row = rep[(rep["pair_set"] == name) & (rep["gate"] == "OVERALL")]
    return bool(row["pass"].iloc[0])


def test_all_gates_pass_at_exact_boundaries():
    rep = _judge(worst=1.0 + GATE_WORST_TV_LIFT, turn=10.0, ex1=1.0)
    assert _overall(rep) is True


@pytest.mark.parametrize("kw", [
    {"worst": 1.0 + GATE_WORST_TV_LIFT - 1e-9},          # 判据①：差一点点
    {"worst": 1.0 + GATE_WORST_TV_LIFT, "turn": 10.001},  # 判据②：换手上升
    {"worst": 1.0 + GATE_WORST_TV_LIFT, "ex1": 0.999},    # 判据③：集中度恶化
    {"worst": 1.0 + GATE_WORST_TV_LIFT, "cand_ic": 0.1},  # 闸门⓪：IC 不高于现役
    {"worst": 1.0 + GATE_WORST_TV_LIFT, "excl": False},   # 闸门⓪：与现役平手
])
def test_each_single_failure_flips_overall_to_stop(kw):
    assert _overall(_judge(**kw)) is False


def test_gate0_requires_both_selection_windows_higher():
    panel = _panel(worst=1.0 + GATE_WORST_TV_LIFT)
    ic_rep, ic_diff = _ic()
    # 只让 val 窗高于现役，train 窗打平 → 闸门 ⓪ 必须不过
    ic_rep.loc[(ic_rep["pair_set"] == "B_500_1000_2000")
               & (ic_rep["window"] == STAT_WINDOWS[0]), "ic"] = 0.2
    rep = evaluate_gates(panel, ic_rep, ic_diff, ("B_500_1000_2000",), _div_diag(0.1))
    assert _overall(rep) is False


def test_holdout_window_never_appears_in_any_gate_row():
    rep = _judge(worst=1.0 + GATE_WORST_TV_LIFT)
    assert not rep["metric"].astype(str).str.contains("holdout|2024").any()


def test_diagnostic_rows_never_enter_overall():
    rep = _judge(worst=1.0 + GATE_WORST_TV_LIFT)
    diag = rep[rep["pass"].isna()]
    assert len(diag) >= 1
    assert _overall(rep) is True


def test_dividend_same_source_short_circuits_all_later_gates():
    panel = _panel().assign()
    panel = pd.concat([panel, panel.iloc[[1]].assign(pair_set=DIVIDEND_SET)],
                      ignore_index=True)
    ic_rep, ic_diff = _ic()
    rep = evaluate_gates(panel, ic_rep, ic_diff, (DIVIDEND_SET,), _div_diag(0.95))
    assert _overall(rep, DIVIDEND_SET) is False
    gates = set(rep[rep["pair_set"] == DIVIDEND_SET]["gate"])
    assert gates == {"PRE_dividend_corr", "OVERALL"}      # 后续判据一条都没评


# ---------------------------------------------------------------- 5. 机器接口 == 房规
@pytest.fixture
def toy():
    rng = np.random.default_rng(5)
    n = 400
    idx = pd.bdate_range("2016-01-01", periods=n)
    fac = pd.Series(rng.normal(0, 0.4, n), index=idx).rolling(5).mean().fillna(0.0)
    und = pd.Series(rng.normal(0.0003, 0.012, n), index=idx)
    car = pd.Series(rng.normal(0.05, 0.02, n), index=idx)
    masks = {"train_2014_2020": np.arange(n) < 250, "val_2021_2023": np.arange(n) >= 250}
    return idx, fac, und, car, masks


def test_sharpe_scorer_observed_row_matches_house_engine(toy):
    idx, fac, und, car, masks = toy
    pos = map_position(fac, "longflat")
    scorer = make_sharpe_scorer({"v": pos.to_numpy(dtype=float)},
                                und.to_numpy(dtype=float), car.to_numpy(dtype=float),
                                masks, cost_bps=3.0)
    got = float(scorer("v", np.arange(len(idx))[None, :])[0])
    house = []
    for w in ("train_2014_2020", "val_2021_2023"):
        m = masks[w]
        house.append(sharpe(run_strategy(pos[m], und[m], 3.0, car[m])["ret"]))
    assert got == pytest.approx(min(house), abs=1e-12)


def test_ic_scorer_observed_row_matches_house_rank_ic(toy):
    idx, fac, und, car, masks = toy
    fwd = forward_return(und, K_FORWARD)
    ok = fwd.notna().to_numpy()
    masks = {w: m & ok for w, m in masks.items()}
    grid_pos = {w: np.flatnonzero(masks[w])[0::K_FORWARD] for w in masks}
    scorer = make_ic_scorer({"v": fac.to_numpy(dtype=float)},
                            fwd.to_numpy(dtype=float), grid_pos)
    got = float(scorer("v", np.arange(len(idx))[None, :])[0])
    house = [rank_ic(fac.iloc[g], fwd.iloc[g])["ic"] for g in grid_pos.values()]
    assert got == pytest.approx(min(house), abs=1e-12)


def test_ic_scorer_is_signed_not_absolute(toy):
    """有符号统计量：因子取反 → 逐窗 IC 变号，故 worst 从 min(ic) 变成 −max(ic)。

    若统计量写成 |IC|（② 的做法），取反前后必然**相等** —— 本判例正是为了钉死"不是 |IC|"。
    """
    idx, fac, und, car, masks = toy
    fwd = forward_return(und, K_FORWARD)
    ok = fwd.notna().to_numpy()
    masks = {w: m & ok for w, m in masks.items()}
    grid_pos = {w: np.flatnonzero(masks[w])[0::K_FORWARD] for w in masks}
    arrays = {"v": fac.to_numpy(dtype=float), "neg": -fac.to_numpy(dtype=float)}
    scorer = make_ic_scorer(arrays, fwd.to_numpy(dtype=float), grid_pos)
    one = np.arange(len(idx))[None, :]
    house = [rank_ic(fac.iloc[g], fwd.iloc[g])["ic"] for g in grid_pos.values()]
    assert float(scorer("v", one)[0]) == pytest.approx(min(house), abs=1e-12)
    assert float(scorer("neg", one)[0]) == pytest.approx(-max(house), abs=1e-12)
    assert float(scorer("v", one)[0]) != pytest.approx(float(scorer("neg", one)[0]))


def test_nonoverlap_grid_positions_match_fusion_probe(toy):
    """机器里的整数位置抽样与 ③ 的 `nonoverlap_grid` 逐点相同（同一张网格才叫同秤）。"""
    idx, *_ = toy
    m = np.arange(len(idx)) < 250
    want = nonoverlap_grid(idx[m], K_FORWARD, 0)
    got = idx[np.flatnonzero(m)[0::K_FORWARD]]
    pd.testing.assert_index_equal(pd.DatetimeIndex(got), pd.DatetimeIndex(want))


# ---------------------------------------------------------------- 6. 移位保持换手
def test_rotation_preserves_turnover_of_position_series(toy):
    idx, fac, *_ = toy
    pos = map_position(fac, "longflat").to_numpy(dtype=float)
    mat = build_index_matrix(len(pos), 30, scheme="rotation", seed=1,
                             min_shift=2 * K_FORWARD, max_shift=len(pos) - 2 * K_FORWARD)
    base = np.abs(np.diff(pos)).sum() + np.abs(pos[0] - pos[-1])   # 环上总变化
    for row in mat:
        rot = pos[row]
        assert np.abs(np.diff(rot)).sum() + abs(rot[0] - rot[-1]) == pytest.approx(base)


def test_shift_bounds_is_the_path_run_probe_actually_uses():
    """`run_probe` 的移位区间**唯一**来自 `shift_bounds`：正式跑 n_obs=2434 → (40, 2394)。

    这条判例检验的是**真实代码路径**（`run_probe` 里就是 `shift_bounds(len(common_sel))`），
    不是把 `2*K_FORWARD == 40` 再抄一遍的重言式。
    """
    assert shift_bounds(2434) == (40, 2394)          # 正式跑的选择窗长度
    assert shift_bounds(2434)[0] == 2 * K_FORWARD
    assert shift_bounds(500) == (40, 460)
    src = (ROOT / "backtest/pair_set_probe.py").read_text(encoding="utf-8")
    assert "min_shift, max_shift = shift_bounds(len(common_sel))" in src


def test_shift_bounds_rejects_samples_too_short_for_house_rule():
    for n in (0, 40, 80, 4 * K_FORWARD):
        with pytest.raises(ValueError):
            shift_bounds(n)


def test_shift_bounds_feed_index_matrix_within_range():
    """真跑一遍机器的索引矩阵：全部位移落在 [min_shift, max_shift) 内。"""
    n = 2434
    lo, hi = shift_bounds(n)
    mat = build_index_matrix(n, 200, scheme="rotation", seed=0,
                             min_shift=lo, max_shift=hi)
    base = np.arange(n)
    shifts = (base[0] - mat[:, 0]) % n
    assert shifts.min() >= lo and shifts.max() < hi


# ---------------------------------------------------------------- 7. 映射
def test_map_position_reuses_house_mappings(toy):
    idx, fac, *_ = toy
    pd.testing.assert_series_equal(map_position(fac, "longflat"),
                                   production_position(fac).astype(float),
                                   check_names=False)
    pd.testing.assert_series_equal(map_position(fac, "symmetric"),
                                   to_position(fac, mode="discrete").astype(float),
                                   check_names=False)
    with pytest.raises(ValueError):
        map_position(fac, "proportional")


def test_longflat_turnover_is_not_higher_than_symmetric(toy):
    idx, fac, *_ = toy
    assert turnover(map_position(fac, "longflat")) <= turnover(map_position(fac, "symmetric"))
