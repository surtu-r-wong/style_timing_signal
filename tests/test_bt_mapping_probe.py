"""①b 持仓映射 32 点网格探针（`backtest/mapping_probe.py`）的单元判例。

覆盖三类要害：
1. **映射退化性**：h=0 时滞回状态机 == 既有无记忆映射（`positions.py`，只读复用）；
   (0,0,0) == 现役 `production_position`；(0,0,−1) == ①a 的对称口径；
2. **向量化引擎 == 房规引擎**：`net_return_matrix` / `sharpe_rows` 与
   `engine.run_strategy` / `metrics.sharpe` 逐值一致；
3. **移位精确保持换手**（本探针"变体=已算好的仓位序列"这条写法成立的前提）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.mapping_probe import (  # noqa: E402
    GATE3_MAXDD, INCUMBENT, N_VARIANTS, SYMMETRIC, W_SHORT, autocorr,
    choose_min_shift, evaluate_gates, hysteresis_position, net_return_matrix,
    sharpe_rows, variant_grid,
)
from backtest.metrics import sharpe, turnover  # noqa: E402
from backtest.positions import production_position, to_position, to_position_asym  # noqa: E402
from backtest.selection_permutation import build_index_matrix  # noqa: E402


@pytest.fixture
def signal():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2015-01-01", periods=600)
    x = pd.Series(rng.normal(0, 0.35, len(idx)), index=idx).rolling(5).mean().fillna(0.0)
    return x


def _loop_state_machine(s: pd.Series, theta_l: float, h: float, w_s: float) -> pd.Series:
    """朴素逐日状态机（参照实现）——与向量化 `_sticky` 写法互为交叉判例。"""
    out, state = [], 0
    for v in s.to_numpy(dtype=float):
        if v > theta_l + h:
            state = 1
        elif v < -(theta_l + h):
            state = -1
        elif state == 1 and v > theta_l:
            state = 1
        elif state == -1 and v < -theta_l:
            state = -1
        else:
            state = 0
        out.append(1.0 if state == 1 else (w_s if state == -1 else 0.0))
    return pd.Series(out, index=s.index)


# ---------------------------------------------------------------- 网格
def test_grid_is_32_points_and_contains_known_anchors():
    g = variant_grid()
    assert len(g) == N_VARIANTS == 32
    assert len(set(g)) == 32
    assert INCUMBENT in g and SYMMETRIC in g
    assert 0.0 in W_SHORT and set(W_SHORT) >= {-0.25, -0.5, -1.0}


# ---------------------------------------------------------------- 映射退化性
def test_vectorized_state_machine_matches_naive_loop(signal):
    for v in variant_grid():
        pd.testing.assert_series_equal(hysteresis_position(signal, *v),
                                       _loop_state_machine(signal, *v),
                                       check_names=False)


def test_h_zero_degenerates_to_existing_memoryless_mapping(signal):
    for theta in (0.0, 0.1, 0.2, 0.3):
        got = hysteresis_position(signal, theta, 0.0, -1.0)
        want = to_position_asym(signal, theta, theta).astype(float)
        pd.testing.assert_series_equal(got, want, check_names=False, check_dtype=False)


def test_incumbent_point_equals_production_position(signal):
    got = hysteresis_position(signal, *INCUMBENT)
    want = production_position(signal, threshold=0.0).astype(float)
    pd.testing.assert_series_equal(got, want, check_names=False, check_dtype=False)


def test_symmetric_point_equals_1a_symmetric_mapping(signal):
    got = hysteresis_position(signal, *SYMMETRIC)
    want = to_position(signal, mode="discrete", threshold=0.0).astype(float)
    pd.testing.assert_series_equal(got, want, check_names=False, check_dtype=False)


def test_partial_short_weight_only_scales_short_leg(signal):
    full = hysteresis_position(signal, 0.1, 0.1, -1.0)
    half = hysteresis_position(signal, 0.1, 0.1, -0.5)
    assert (half[full > 0] == full[full > 0]).all()          # 多头腿不受 w_S 影响
    assert np.allclose(half[full < 0], 0.5 * full[full < 0])  # 空头腿按权重缩放
    assert (half[full == 0] == 0).all()


def test_hysteresis_reduces_turnover(signal):
    for theta in (0.0, 0.1, 0.2, 0.3):
        for w in (0.0, -1.0):
            no_h = hysteresis_position(signal, theta, 0.0, w)
            with_h = hysteresis_position(signal, theta, 0.1, w)
            assert turnover(with_h) <= turnover(no_h) + 1e-12


def test_long_and_short_states_are_mutually_exclusive(signal):
    for v in variant_grid():
        pos = hysteresis_position(signal, *v)
        assert set(np.unique(pos.to_numpy())) <= {-1.0, -0.5, -0.25, 0.0, 1.0}


def test_negative_parameters_rejected(signal):
    with pytest.raises(ValueError):
        hysteresis_position(signal, -0.1, 0.0, 0.0)
    with pytest.raises(ValueError):
        hysteresis_position(signal, 0.1, -0.1, 0.0)


# ---------------------------------------------------------------- 向量化引擎 == 房规引擎
def test_net_return_matrix_matches_house_engine(signal):
    rng = np.random.default_rng(3)
    und = pd.Series(rng.normal(0, 0.01, len(signal)), index=signal.index)
    carry = pd.Series(rng.normal(0.08, 0.02, len(signal)), index=signal.index)
    variants = variant_grid()
    mat = np.vstack([hysteresis_position(signal, *v).to_numpy() for v in variants])
    got = net_return_matrix(mat, und.to_numpy(), carry.to_numpy(), 3.0)
    for i, v in enumerate(variants):
        want = run_strategy(hysteresis_position(signal, *v), und, 3.0, carry)["ret"]
        assert np.abs(got[i] - want.to_numpy()).max() < 1e-15


def test_sharpe_rows_matches_house_sharpe(signal):
    rng = np.random.default_rng(11)
    mat = rng.normal(0, 0.01, (7, 300))
    got = sharpe_rows(mat)
    want = np.array([sharpe(pd.Series(r)) for r in mat])
    assert np.abs(got - want).max() < 1e-12


def test_sharpe_rows_zero_variance_follows_house_convention():
    assert sharpe_rows(np.zeros((2, 50)))[0] == 0.0     # 房规：std=0 → 0.0（不是 -inf）


def test_carry_and_cost_terms_are_both_live(signal):
    """成本口径不是摆设：改 cost_bps 与去掉 carry 都必须改变结果。"""
    rng = np.random.default_rng(5)
    und = rng.normal(0, 0.01, len(signal))
    carry = np.full(len(signal), 0.10)
    pos = hysteresis_position(signal, 0.0, 0.0, -1.0).to_numpy()[None, :]
    a = net_return_matrix(pos, und, carry, 3.0)
    b = net_return_matrix(pos, und, carry, 30.0)
    c = net_return_matrix(pos, und, np.zeros_like(carry), 3.0)
    assert not np.allclose(a, b) and not np.allclose(a, c)
    assert a.mean() > b.mean()          # 成本更高 → 净收益更低


# ---------------------------------------------------------------- 移位保持换手
def _circular_abs_diff(pos: np.ndarray) -> np.ndarray:
    """环形逐日 |Δpos|（首日与末日相接），长度 = len(pos)。"""
    return np.abs(pos - np.roll(pos, 1))


def test_rotation_rolls_the_daily_turnover_sequence(signal):
    """本探针"变体=已算好的仓位序列"的成立前提（**逐日版**，强于总量版）：

    移位后的**逐日** |Δpos| 序列 == 原逐日 |Δpos| 序列的同幅循环移位
    → 成本结构不被移位改造，只是整体搬家；总换手（环形）随之逐位不变。
    """
    for v in ((0.1, 0.1, -0.5), (0.0, 0.0, -1.0), (0.3, 0.1, -0.25)):
        pos = hysteresis_position(signal, *v).to_numpy()
        base = _circular_abs_diff(pos)
        for shift in (1, 37, 250):
            rolled = np.roll(pos, shift)
            assert np.array_equal(_circular_abs_diff(rolled), np.roll(base, shift))
            assert abs(_circular_abs_diff(rolled).sum() - base.sum()) < 1e-12


def test_rotation_index_matrix_applies_as_roll(signal):
    """机器索引矩阵作用在仓位上 == np.roll（口径一致性，防止移错方向/错侧）。"""
    pos = hysteresis_position(signal, 0.2, 0.1, -1.0).to_numpy()
    n = len(pos)
    idx = build_index_matrix(n, 5, scheme="rotation", seed=1, min_shift=10, max_shift=n - 10)
    rolled = pos[idx]
    for r in range(idx.shape[0]):
        shift = int((np.arange(n)[0] - idx[r, 0]) % n)
        assert np.array_equal(rolled[r], np.roll(pos, shift))


# ---------------------------------------------------------------- 三条判据（出裁决的函数）
class _FakeRes:
    p_selected = 0.01
    p_naive = 0.01


def _rows(worst, turnover, maxdd, inc_worst=1.0, inc_turnover=10.0, inc_maxdd=-0.13):
    win = pd.Series({"worst_tv_sharpe": worst,
                     "turnover_selection_2014_2023": turnover,
                     "maxdd_selection_2014_2023": maxdd})
    inc = pd.Series({"worst_tv_sharpe": inc_worst,
                     "turnover_selection_2014_2023": inc_turnover,
                     "maxdd_selection_2014_2023": inc_maxdd})
    return win, inc


def _judge(**kw):
    v = evaluate_gates(*_rows(**kw), _FakeRes())
    g = {row.gate + "|" + row.metric: row["pass"] for _, row in v.iterrows()}
    overall = bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0])
    return overall, g


def test_gates_boundary_values_are_inclusive():
    """三条判据都是**闭区间**：margin 恰好 +0.10 / 换手恰好持平 / MaxDD 恰好 −16.7% 都算过。"""
    overall, g = _judge(worst=1.10, turnover=10.0, maxdd=GATE3_MAXDD)
    assert overall is True
    assert g["1_worst_tv_sharpe|winner_worst_tv_sharpe"] is True
    assert g["2_turnover|winner_turnover_selection"] is True
    assert g["3_maxdd|winner_maxdd_selection"] is True


def test_gates_each_single_failure_flips_overall_to_stop():
    """三缺一 → OVERALL 必 False（逐条各测一次，防止 AND 写成 OR / 漏接某条）。"""
    assert _judge(worst=1.0999, turnover=10.0, maxdd=GATE3_MAXDD)[0] is False   # ① 差一点
    assert _judge(worst=1.10, turnover=10.0001, maxdd=GATE3_MAXDD)[0] is False  # ② 差一点
    assert _judge(worst=1.10, turnover=10.0, maxdd=GATE3_MAXDD - 1e-6)[0] is False  # ③ 差一点


def test_gates_diagnostic_rows_never_enter_overall():
    """`pass=None` 的诊断/对照行（含两个 p）不得参与 OVERALL。"""
    v = evaluate_gates(*_rows(worst=1.10, turnover=10.0, maxdd=GATE3_MAXDD), _FakeRes())
    diag = v[v["pass"].isna()]
    assert {"p_selected(机器主口径·非判据)",
            "p_naive_UNCORRECTED(对照·不得入闸)"} <= set(diag["metric"])
    assert bool(v[v["gate"] == "OVERALL"]["pass"].iloc[0]) is True   # p 再差也不改 OVERALL


# ---------------------------------------------------------------- 移位下界规则
def test_autocorr_lag0_is_one_and_matches_pandas(signal):
    x = signal.to_numpy()
    ac = autocorr(x, np.arange(0, 5))
    assert ac[0] == 1.0
    for k in (1, 2, 3, 4):
        xc = x - x.mean()
        assert abs(ac[k] - float(xc[k:] @ xc[:-k]) / float(xc @ xc)) < 1e-12


def test_choose_min_shift_picks_first_ladder_rung_within_white_noise_band():
    """规则语义：选中的梯级必须达标，且它**之前**的梯级都不达标（第一个达标者）。"""
    from backtest.mapping_probe import SHIFT_LADDER
    rng = np.random.default_rng(1)
    n = 2000
    lags = np.arange(0, 400)
    series = {("iid",): rng.normal(size=n)}
    pick, frame, rule = choose_min_shift(series)
    ac = np.abs(autocorr(series[("iid",)], lags))
    band = rule["white_noise_band_2_over_sqrt_n"]
    assert pick in SHIFT_LADDER and len(frame) == 1 and rule["chosen_min_shift"] == pick
    assert ac[pick] < band
    assert all(ac[rung] >= band for rung in SHIFT_LADDER if rung < pick)
    assert rule["max_abs_acf_at_chosen_lag_across_variants"] < band


def test_choose_min_shift_raises_when_no_rung_qualifies():
    n = 2000
    t = np.arange(n)
    series = {("cyc",): np.sin(2 * np.pi * t / 13.0)}   # 周期 13 → 全梯级都相关
    with pytest.raises(AssertionError):
        choose_min_shift(series, ladder=(20, 40))
