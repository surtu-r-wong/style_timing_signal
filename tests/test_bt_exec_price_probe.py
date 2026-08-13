"""Batch 12 执行价口径审计探针的单测（纯函数，不连库）。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.exec_price_probe import (  # noqa: E402
    READING_PAIRS, READINGS, blend_futures_frame, build_legs, effective_position,
    held_contract_frame, main_contract_series, overnight_gap_diagnostics,
    roll_cost_estimate, run_strategy_dual, run_strategy_shift,
)

IDX = pd.bdate_range("2020-01-01", periods=60)


def _rand(seed=0, n=60, mu=0.0, sd=0.01):
    return pd.Series(np.random.default_rng(seed).normal(mu, sd, n), index=IDX[:n])


def _pos(pattern):
    """按字符串模式造仓位：'+'=1 '-'=-1 '0'=0。"""
    vals = {"+": 1.0, "-": -1.0, "0": 0.0}
    return pd.Series([vals[ch] for ch in pattern], index=IDX[:len(pattern)], dtype=float)


# ---------------------------------------------------------------- 路线 B
def test_shift1_variant_is_bitwise_identical_to_production_engine():
    pos = pd.Series(np.tile([1.0, 1.0, 0.0, -1.0], 15), index=IDX)
    und, car = _rand(1), _rand(2, mu=0.05, sd=0.02)
    want = run_strategy(pos, und, 3.0, car)
    got = run_strategy_shift(pos, und, 3.0, car, shift=1)
    pd.testing.assert_frame_equal(got, want)


def test_shift1_variant_identical_without_carry():
    pos = pd.Series(np.tile([1.0, 0.0], 30), index=IDX)
    und = _rand(3)
    pd.testing.assert_frame_equal(run_strategy_shift(pos, und, 5.0, None, shift=1),
                                  run_strategy(pos, und, 5.0, None))


def test_shift2_delays_position_and_cost_by_one_more_day():
    pos = _pos("00++000000")
    und = pd.Series(0.01, index=IDX[:10])
    r1 = run_strategy_shift(pos, und, 3.0, None, shift=1)
    r2 = run_strategy_shift(pos, und, 3.0, None, shift=2)
    assert list(r1["pos_eff"]) == [0, 0, 0, 1, 1, 0, 0, 0, 0, 0]
    assert list(r2["pos_eff"]) == [0, 0, 0, 0, 1, 1, 0, 0, 0, 0]
    # 成本落在**新生效日**：shift2 的建仓成本比 shift1 晚一个交易日，总额不变
    assert list(r1["cost"]).index(r1["cost"].max()) + 1 == \
        list(r2["cost"]).index(r2["cost"].max())
    assert np.isclose(r1["cost"].sum(), r2["cost"].sum())


def test_shift2_carry_follows_pos_eff():
    pos = _pos("0++++00000")
    und = pd.Series(0.0, index=IDX[:10])
    car = pd.Series(0.098, index=IDX[:10])
    r2 = run_strategy_shift(pos, und, 0.0, car, shift=2)
    assert np.allclose(r2["carry"], r2["pos_eff"] * 0.098 / 245)


def test_shift_negative_rejected_and_empty_input_safe():
    with pytest.raises(ValueError):
        run_strategy_shift(_pos("0"), pd.Series(0.0, index=IDX[:1]), shift=-1)
    empty = pd.Series(dtype=float)
    assert len(run_strategy_shift(empty, empty, shift=2)) == 0


def test_symmetric_long_clip_equals_longflat_at_theta_zero():
    raw = pd.Series([0.3, -0.2, 0.0, 1.1, -0.5], index=IDX[:5])
    legs = build_legs(raw)
    pd.testing.assert_series_equal(legs["long_seg"], legs["longflat"], check_names=False)


# ---------------------------------------------------------------- 主力合约 / 价格系
def _fut_rows(rows):
    return pd.DataFrame(rows, columns=["trade_date", "symbol", "open", "close", "oi"])


def test_main_contract_picks_max_oi():
    df = _fut_rows([
        ("2020-01-02", "IC2001.CFE", 1, 100, 50), ("2020-01-02", "IC2002.CFE", 1, 110, 90),
        ("2020-01-03", "IC2001.CFE", 1, 101, 95), ("2020-01-03", "IC2002.CFE", 1, 111, 40),
    ])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    m = main_contract_series(df)
    assert m.loc[pd.Timestamp("2020-01-02")] == "IC2002.CFE"
    assert m.loc[pd.Timestamp("2020-01-03")] == "IC2001.CFE"


def test_held_contract_frame_uses_prev_day_main_no_fake_roll():
    """d1/d2 主力 = A，d3 起 B 接棒。d3 的收益必须仍按 A 算（持有的是 A），
    d4 才切到 B —— 两张合约价差 100 vs 200 若被跨合约衔接会造出 +100% 假损益。"""
    rows = []
    prices = {"IC2001.CFE": [100, 101, 102, 103], "IC2002.CFE": [200, 202, 204, 206]}
    ois = {"IC2001.CFE": [99, 99, 10, 10], "IC2002.CFE": [10, 10, 99, 99]}
    for i, d in enumerate(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]):
        for sym in prices:
            rows.append((d, sym, prices[sym][i] * 0.995, prices[sym][i], ois[sym][i]))
    df = _fut_rows(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    f = held_contract_frame(df)
    assert list(f["symbol_held"]) == ["IC2001.CFE", "IC2001.CFE", "IC2002.CFE"]
    assert np.isclose(f["ret_cc"].iloc[1], 102 / 101 - 1)      # 换月日仍用 A
    assert np.isclose(f["ret_cc"].iloc[2], 206 / 204 - 1)      # 次日才是 B
    assert f["ret_cc"].abs().max() < 0.05                       # 无虚假换月跳空


def test_held_contract_frame_identity_cc_eq_gap_times_oc():
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2020-01-01", periods=30)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, 30))
    rows = [(d, "IC2001.CFE", c * (1 + rng.normal(0, 0.003)), c, 100)
            for d, c in zip(dates, close)]
    df = _fut_rows(rows)
    f = held_contract_frame(df)
    assert np.allclose(1 + f["ret_cc"], (1 + f["gap"]) * (1 + f["ret_oc"]))


def test_blend_futures_frame_averages_on_intersection():
    a = pd.DataFrame({"ret_cc": [0.02, 0.04], "ret_oc": [0.01, 0.02], "gap": [0.01, 0.02],
                      "symbol_held": ["IC1", "IC2"]}, index=pd.bdate_range("2022-08-01", periods=2))
    b = pd.DataFrame({"ret_cc": [0.0, 0.0, 0.0], "ret_oc": [0.0, 0.0, 0.0], "gap": [0.0, 0.0, 0.0],
                      "symbol_held": ["IM1", "IM2", "IM3"]},
                     index=pd.bdate_range("2022-08-01", periods=3))
    out = blend_futures_frame(a, b)
    assert len(out) == 2                       # 交集，不放大单腿
    assert np.allclose(out["ret_cc"], [0.01, 0.02])
    assert out["symbol_held"].iloc[0] == "IC1|IM1"


# ---------------------------------------------------------------- 路线 A：四读法
def _fut_frame(n, cc, oc, gap):
    idx = IDX[:n]
    return pd.DataFrame({"ret_cc": np.full(n, cc), "ret_oc": np.full(n, oc),
                         "gap": np.full(n, gap), "symbol_held": "X"}, index=idx)


def test_reading_i_is_bitwise_identical_to_production_engine():
    pos = pd.Series(np.tile([1.0, -1.0, 0.0, 1.0], 15), index=IDX)
    und, car = _rand(11), _rand(12, mu=0.05, sd=0.02)
    got = run_strategy_dual(pos, und, None, 3.0, car, **READINGS["i_spot_close"])
    want = run_strategy(pos, und, 3.0, car)
    pd.testing.assert_frame_equal(got[want.columns], want)


def test_long_only_position_invariant_across_all_four_readings():
    pos = pd.Series(np.tile([1.0, 1.0, 0.0], 20), index=IDX)
    und, car = _rand(13), _rand(14, mu=0.05, sd=0.02)
    fut = _fut_frame(60, 0.05, 0.03, 0.02)     # 期货收益与现货完全不同
    base = None
    for kw in READINGS.values():
        r = run_strategy_dual(pos, und, fut, 3.0, car, **kw)["ret"]
        base = r if base is None else base
        pd.testing.assert_series_equal(r, base)


def test_reading_ii_swaps_only_short_days_to_futures_and_drops_short_carry():
    pos = _pos("0+-000")
    und = pd.Series(0.01, index=IDX[:6])
    car = pd.Series(2.45, index=IDX[:6])       # carry/245 = 0.01/日
    fut = _fut_frame(6, -0.05, 0.0, 0.0)
    r = run_strategy_dual(pos, und, fut, 0.0, car, **READINGS["ii_fut_close"])
    # pos_eff: [0,0,+1,-1,0,0]；多头日走现货+carry，空头日走期货且无 carry
    assert np.isclose(r["gross"].iloc[2], 0.01) and np.isclose(r["carry"].iloc[2], 0.01)
    assert np.isclose(r["gross"].iloc[3], 0.05) and r["carry"].iloc[3] == 0.0


def test_reading_iii_changes_only_the_short_open_day():
    pos = _pos("0--- 00".replace(" ", ""))
    und = pd.Series(0.0, index=IDX[:6])
    fut = _fut_frame(6, 0.02, 0.005, 0.01493)
    r2 = run_strategy_dual(pos, und, fut, 0.0, None, **READINGS["ii_fut_close"])
    r3 = run_strategy_dual(pos, und, fut, 0.0, None, **READINGS["iii_fut_open_entry"])
    d = (r3["ret"] - r2["ret"])
    nz = d[np.abs(d) > 1e-12]
    assert len(nz) == 1                                    # 只有开仓日不同
    assert nz.index[0] == IDX[2]                           # pos_eff 首个空头日
    assert np.isclose(nz.iloc[0], -(0.005 - 0.02))         # −1 ×(ret_oc − ret_cc)


def test_reading_iv_adds_exit_gap_pnl_on_the_flat_day():
    pos = _pos("0--000")
    und = pd.Series(0.0, index=IDX[:6])
    fut = _fut_frame(6, 0.02, 0.005, 0.01493)
    r3 = run_strategy_dual(pos, und, fut, 0.0, None, **READINGS["iii_fut_open_entry"])
    r4 = run_strategy_dual(pos, und, fut, 0.0, None, **READINGS["iv_fut_open_both"])
    d = (r4["ret"] - r3["ret"])
    nz = d[np.abs(d) > 1e-12]
    assert len(nz) == 1 and nz.index[0] == IDX[4]          # pos_eff 转 0 的那天
    assert np.isclose(nz.iloc[0], -0.01493)                # 空头多持一段隔夜 = −gap


def test_dual_engine_rejects_spot_open_and_bad_args():
    pos, und = _pos("0-0"), pd.Series(0.0, index=IDX[:3])
    with pytest.raises(ValueError):                        # 现货无开盘价
        run_strategy_dual(pos, und, None, short_source="spot", short_entry="open")
    with pytest.raises(ValueError):
        run_strategy_dual(pos, und, None, short_source="futures")
    with pytest.raises(ValueError):                        # fut 未提供
        run_strategy_dual(pos, und, None, short_source="fut")


def test_cost_is_unchanged_across_readings():
    pos = pd.Series(np.tile([1.0, -1.0, 0.0], 20), index=IDX)
    und = _rand(15)
    fut = _fut_frame(60, 0.01, 0.005, 0.005)
    costs = [run_strategy_dual(pos, und, fut, 3.0, None, **kw)["cost"].sum()
             for kw in READINGS.values()]
    assert np.allclose(costs, costs[0])


# ---------------------------------------------------------------- 隔夜缺口诊断
def test_gap_diagnostics_counts_only_short_open_days_and_signs():
    pos = _pos("0--0-0")                       # pos_eff 空头日 = idx2,3 与 idx5 → 两次开仓
    fut = pd.DataFrame({"ret_cc": 0.0, "ret_oc": 0.0,
                        "gap": [0.0, 0.0, 0.02, 0.0, 0.0, -0.01], "symbol_held": "X"},
                       index=IDX[:6])
    d = overnight_gap_diagnostics(pos, fut)
    assert d["n_short_open"] == 2
    assert np.isclose(d["gap_mean"], 0.005)
    assert np.isclose(d["pct_gap_up_short_avoids_loss"], 0.5)
    assert np.isclose(d["pct_gap_down_short_misses_gain"], 0.5)
    assert np.isclose(d["missed_pnl_sum"], -0.01)          # 净「躲过」而非错过
    assert d["flip_short_to_long"] == 0


def test_gap_diagnostics_flags_direct_short_to_long_flip():
    pos = _pos("0-+000")
    fut = _fut_frame(6, 0.0, 0.0, 0.01)
    assert overnight_gap_diagnostics(pos, fut)["flip_short_to_long"] == 1


def test_gap_diagnostics_empty_when_no_shorts():
    pos = _pos("0++000")
    assert overnight_gap_diagnostics(pos, _fut_frame(6, 0.0, 0.0, 0.01))["n_short_open"] == 0


# ---------------------------------------------------------------- 读法 (i.5)：拆 carry
def test_reading_i5_removes_only_short_carry_keeps_spot_prices():
    pos = _pos("0+-000")
    und = pd.Series(0.01, index=IDX[:6])
    car = pd.Series(2.45, index=IDX[:6])       # carry/245 = 0.01/日
    r1 = run_strategy_dual(pos, und, None, 0.0, car, **READINGS["i_spot_close"])
    r15 = run_strategy_dual(pos, und, None, 0.0, car, **READINGS["i5_spot_close_nocarry"])
    pd.testing.assert_series_equal(r1["gross"], r15["gross"])          # 价格系没变
    assert np.isclose(r1["carry"].iloc[2], 0.01) and np.isclose(r15["carry"].iloc[2], 0.01)
    assert np.isclose(r1["carry"].iloc[3], -0.01) and r15["carry"].iloc[3] == 0.0


def test_decomposition_is_additive_on_returns():
    """(ii)−(i) 必须逐日等于 [(i.5)−(i)] + [(ii)−(i.5)]（收益可加，Sharpe 不可加）。"""
    pos = pd.Series(np.tile([1.0, -1.0, 0.0, -1.0], 15), index=IDX)
    und, car = _rand(21), _rand(22, mu=0.15, sd=0.03)
    fut = _fut_frame(60, 0.008, 0.004, 0.004)
    r = {k: run_strategy_dual(pos, und, fut, 3.0, car, **kw)["ret"] for k, kw in READINGS.items()}
    lhs = r["ii_fut_close"] - r["i_spot_close"]
    rhs = ((r["i5_spot_close_nocarry"] - r["i_spot_close"])
           + (r["ii_fut_close"] - r["i5_spot_close_nocarry"]))
    pd.testing.assert_series_equal(lhs, rhs)


# ---------------------------------------------------------------- 窗口边界伪开仓（M1）
def test_effective_position_uses_full_series_to_kill_boundary_pseudo_open():
    full = _pos("0----0")                       # 全序列：一次开仓（idx2）
    win = full.iloc[3:]                         # 窗口从已持空的中段切起
    naive = effective_position(win)             # 切片后 shift → idx3 被误判成新开仓
    fixed = effective_position(win, position_full=full)
    assert naive.iloc[0] == 0.0 and fixed.iloc[0] == -1.0


def test_gap_diagnostics_windows_reconcile_with_full_when_given_full_series():
    full = _pos("0-----0---")                    # 两次开仓：pos_eff 的 idx2 与 idx8
    fut = _fut_frame(10, 0.0, 0.0, 0.01)
    cuts = ((0, 4), (4, 7), (7, 10))             # 中间窗从"已持空 2 天以上"处切起
    whole = overnight_gap_diagnostics(full, fut, position_full=full)["n_short_open"]
    parts = sum(overnight_gap_diagnostics(full.iloc[a:b], fut.iloc[a:b],
                                          position_full=full)["n_short_open"]
                for a, b in cuts)
    assert whole == parts == 2                   # 分窗合计与 full 对账
    naive = sum(overnight_gap_diagnostics(full.iloc[a:b], fut.iloc[a:b])["n_short_open"]
                for a, b in cuts)
    assert naive == 3                            # 未修口径多记一次窗界伪开仓


def test_gap_diagnostics_reports_significance_fields():
    pos = _pos("0-0-0-0-0-")                     # pos_eff 的 idx2/4/6/8 各一次开仓
    fut = pd.DataFrame({"ret_cc": 0.0, "ret_oc": 0.0,
                        "gap": [0, 0, .01, 0, .02, 0, -.005, 0, .015, 0.0],
                        "symbol_held": "X"}, index=IDX[:10])
    d = overnight_gap_diagnostics(pos, fut, position_full=pos)
    assert d["n_short_open"] == 4 and d["n_gap_up"] == 3 and d["n_gap_down"] == 1
    assert 0 < d["binom_p_up"] <= 1 and 0 < d["t_p_mean"] <= 1
    assert np.isclose(d["binom_p_up"], 0.625)    # 3/4 双侧二项，远不显著


# ---------------------------------------------------------------- 换月成本（I7）
def test_roll_cost_counts_only_rolls_while_short_and_doubles_the_leg():
    pos = _pos("0--0--")                          # pos_eff = [0,0,-1,-1,0,-1]
    fut = pd.DataFrame({"ret_cc": 0.0, "ret_oc": 0.0, "gap": 0.0,
                        "symbol_held": ["A", "B", "C", "C", "C", "D"]}, index=IDX[:6])
    d = roll_cost_estimate(pos, fut, cost_bps=3.0, position_full=pos)
    assert d["n_legs"] == 1 and d["n_roll_days"] == 3       # idx1/idx2/idx5
    assert d["n_roll_days_short"] == 2                      # idx1 非持空日 → 不计
    assert np.isclose(d["roll_cost_total"], 2 * (2 * 3.0 / 1e4))  # 每次 = 平旧+开新


def test_roll_cost_splits_blend_legs_by_half_weight():
    pos = _pos("0---")
    fut = pd.DataFrame({"ret_cc": 0.0, "ret_oc": 0.0, "gap": 0.0,
                        "symbol_held": ["A|X", "A|X", "B|X", "B|Y"]}, index=IDX[:4])
    d = roll_cost_estimate(pos, fut, cost_bps=3.0, position_full=pos)
    assert d["n_legs"] == 2 and d["n_roll_days"] == 2 and d["n_roll_days_short"] == 2
    assert np.isclose(d["roll_cost_total"], 2 * (2 * 3.0 / 1e4) * 0.5)


# ---------------------------------------------------------------- 预登记完整性
def test_prereg_readings_and_pairs_are_frozen():
    assert list(READINGS) == ["i_spot_close", "i5_spot_close_nocarry", "ii_fut_close",
                              "iii_fut_open_entry", "iv_fut_open_both"]
    assert ("iii_fut_open_entry", "ii_fut_close") in READING_PAIRS   # 纯开仓时点效应 ★
    assert ("i5_spot_close_nocarry", "i_spot_close") in READING_PAIRS  # 去 carry 贡献
    assert ("ii_fut_close", "i5_spot_close_nocarry") in READING_PAIRS  # 换价格系贡献
    assert READINGS["i_spot_close"]["short_carry"] is True
    assert all(not v["short_carry"] for k, v in READINGS.items() if k != "i_spot_close")
    assert READINGS["i5_spot_close_nocarry"]["short_source"] == "spot"
