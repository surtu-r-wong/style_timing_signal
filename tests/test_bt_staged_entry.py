"""两笔分批建仓映射的判例（不连库）。

钉死三件事：规则本身、两个结构性后果（路径依赖 / 卡死）、以及自由度决议
（首日不算穿越、同日冲突先平后开）。后两类尤其要有判例——它们是规则的性质
而非 bug，将来有人"顺手修掉"就等于换了策略。
"""
import pandas as pd
import pytest

from backtest.positions import crossings, production_position, staged_position

W1, W2 = 0.4, 0.6


def _s(values: list[float]) -> pd.Series:
    idx = pd.date_range("2026-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


# ─────────────────────────── crossings ───────────────────────────

def test_crossings_detect_up_and_down():
    up, down = crossings(_s([-1, 1, 1, -1]))
    assert list(up) == [False, True, False, False]
    assert list(down) == [False, False, False, True]


def test_first_row_is_never_a_crossing():
    """开局就在线上不算入场信号——要求真实穿越事件。"""
    up, down = crossings(_s([1, 1]))
    assert list(up) == [False, False]
    up, down = crossings(_s([-1, -1]))
    assert list(down) == [False, False]


def test_exactly_threshold_counts_as_below():
    """θ=0 时信号恰为 0 → 不算在线上（与 production_position 的严格大于一致）。"""
    up, down = crossings(_s([-1, 0, 1]))
    assert list(up) == [False, False, True]
    assert list(down) == [False, False, False]
    assert list(production_position(_s([-1, 0, 1]))) == [0, 0, 1]


# ─────────────────────────── 规则主路径 ───────────────────────────

def test_canonical_lifecycle():
    """用户设计的完整一轮：raw 先探 0.4 → smooth 确认补满 1.0
    → raw 先撤笔2 剩 0.4 → smooth 收尾清零。"""
    raw = _s([0, +1, +1, +1, -1, -1])
    smooth = _s([0, -1, +1, +1, +1, -1])
    pos = staged_position(raw, smooth, W1, W2)
    assert list(pos) == [0.0, 0.4, 1.0, 1.0, 0.4, 0.0]


def test_leg2_can_be_held_alone():
    """smooth 先上穿建笔2、随后 smooth 下穿平笔1（本无持仓）→ 0.6 单独存在。

    这一格证明仓位不是 {0, w1, w1+w2} 三值，w2 单腿状态真实可达。
    """
    raw = _s([0, -1, -1, -1])
    smooth = _s([0, +1, +1, +1])
    pos = staged_position(raw, smooth, W1, W2)
    assert list(pos) == [0.0, 0.6, 0.6, 0.6]


def test_weights_are_respected():
    raw = _s([0, +1, +1])
    smooth = _s([0, -1, +1])
    pos = staged_position(raw, smooth, w1=0.25, w2=0.75)
    assert list(pos) == [0.0, 0.25, 1.0]


def test_long_flat_only_never_shorts():
    """负信号一律不建仓，仓位恒非负（沿用 long-flat 既有裁决）。"""
    raw = _s([0, -1, -2, -3])
    smooth = _s([0, -1, -2, -3])
    pos = staged_position(raw, smooth, W1, W2)
    assert (pos >= 0).all() and pos.sum() == 0


# ──────────────── 结构性后果一：卡死（不是 bug，是规则性质）────────────────

def test_leg2_stays_stuck_when_smooth_never_recrosses():
    """笔2 被 raw 下穿平掉后，smooth 若全程不下穿就再无上穿事件 → 笔2 空置。

    t4/t5 上 raw>0 且 smooth>0（两笔的水平条件都成立），仓位却只有 0.4。
    """
    raw = _s([0, +1, +1, -1, +1, +1])
    smooth = _s([0, -1, +1, +1, +1, +1])
    pos = staged_position(raw, smooth, W1, W2)
    assert list(pos) == [0.0, 0.4, 1.0, 0.4, 0.4, 0.4]
    assert raw.iloc[4] > 0 and smooth.iloc[4] > 0, "水平条件都成立"
    assert pos.iloc[4] == 0.4, "但笔2 卡死，补不回来"


# ──────────────── 结构性后果二：路径依赖 ────────────────

def test_same_levels_different_position_depending_on_path():
    """同一组 (raw>0, smooth>0) 水平，仓位可以是 1.0 也可以是 0.4。

    事件驱动的直接后果：仓位不是 (raw, smooth) 的函数。
    """
    normal = staged_position(_s([0, +1, +1]), _s([0, -1, +1]), W1, W2)
    stuck = staged_position(_s([0, +1, +1, -1, +1]), _s([0, -1, +1, +1, +1]), W1, W2)
    assert normal.iloc[-1] == 1.0
    assert stuck.iloc[-1] == 0.4
    # 末日两者的水平完全一样
    assert normal.index is not stuck.index  # 只是提示这是两条独立路径


# ──────────────── 自由度：同日冲突先平后开 ────────────────

def test_same_day_close_then_open_nets_to_held():
    """raw 上穿（开笔1）与 smooth 下穿（平笔1）同日 → 先平后开，净效果持有。"""
    raw = _s([0, -1, +1])
    smooth = _s([0, +1, -1])
    up_raw, _ = crossings(raw)
    _, down_sm = crossings(smooth)
    assert bool(up_raw.iloc[2]) and bool(down_sm.iloc[2]), "构造出同日冲突"
    pos = staged_position(raw, smooth, W1, W2)
    assert pos.iloc[2] == 1.0, "笔1 先平后开=持有；笔2 只由 raw 下穿平，故仍在"


# ─────────────────────────── 入参校验 ───────────────────────────

def test_mismatched_index_raises():
    raw = _s([0, 1, 1])
    smooth = _s([0, 1, 1]).iloc[:2]
    with pytest.raises(ValueError, match="索引必须逐位一致"):
        staged_position(raw, smooth, W1, W2)


def test_negative_weight_raises():
    raw = smooth = _s([0, 1])
    with pytest.raises(ValueError, match="必须非负"):
        staged_position(raw, smooth, w1=-0.1, w2=0.6)


# ─────────────── 与真实 committed 信号对齐（锚定 08-10~08-14）───────────────

def test_matches_real_signal_window_2026_08():
    """真实数据锚：raw 08-10 转正、smooth 08-13 才转正（滞后 3 个交易日），
    分批规则应在 08-10 就有 0.4、08-13 补满 1.0，而现役 long-flat 到 08-13 才有仓位。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    df = pd.read_csv(
        root / "output/equal_weight/equal_weight_signal_20d40z.csv",
        parse_dates=["date"]).set_index("date").sort_index()
    raw, smooth = df["factor_value_raw"], df["factor_value"]
    staged = staged_position(raw, smooth, W1, W2)
    incumbent = production_position(smooth).astype(float)

    win = slice("2026-08-07", "2026-08-14")
    assert list(staged[win]) == [0.0, 0.4, 0.4, 0.4, 1.0, 1.0]
    assert list(incumbent[win]) == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0]


# ─────────────── 方向 A：快腿平滑窗（smooth_series / build_fast_sweep）───────────────

def test_smooth_series_matches_production_definition():
    """平滑必须与生产逐字一致：rolling(N, min_periods=1).mean()。

    min_periods=1 是关键——开头不产生 NaN，而是用可得行数先平均
    （signals/equal_weight/generate_signal.py:230）。
    """
    from backtest.staged_entry_probe import smooth_series

    raw = _s([1.0, 3.0, 5.0, 7.0])
    assert list(smooth_series(raw, 1)) == [1.0, 3.0, 5.0, 7.0]      # 不平滑
    assert list(smooth_series(raw, 2)) == [1.0, 2.0, 4.0, 6.0]      # 首行 min_periods=1
    assert list(smooth_series(raw, 3)) == [1.0, 2.0, 3.0, 5.0]
    assert not smooth_series(raw, 3).isna().any(), "min_periods=1 不该产生 NaN"


def test_smooth_series_reproduces_committed_factor_value():
    """自算 sm5 应复现 committed 的 factor_value（差异只应来自 CSV 的 round(4)）。"""
    from pathlib import Path

    from backtest.staged_entry_probe import SLOW_WINDOW, smooth_series

    root = Path(__file__).resolve().parents[1]
    df = pd.read_csv(
        root / "output/equal_weight/equal_weight_signal_20d40z.csv",
        parse_dates=["date"]).set_index("date").sort_index()
    delta = (smooth_series(df["factor_value_raw"], SLOW_WINDOW)
             - df["factor_value"]).abs().max()
    assert delta < 2e-4, f"max|Δ|={delta:.3e} 超出 round(4) 能解释的范围"


def test_fast_sweep_asserts_when_smoothing_definition_drifts():
    """口径自证要真的会拦人：喂一个假的 committed_smooth 必须抛错。

    这条断言是整张扫描表「与现役同秤」的唯一保证，不能只写不测。
    """
    from backtest.staged_entry_probe import build_fast_sweep

    raw = _s([0.1] * 10)
    bogus = _s([9.9] * 10)          # 与任何平滑窗都对不上
    with pytest.raises(AssertionError, match="平滑口径对不上"):
        build_fast_sweep(raw, bogus, W1, W2)


def test_fast_sweep_emits_paired_candidates():
    """每个快腿窗必须同时出 lf_smN 与 staged_smN_5——少了前者就分不清
    「分批结构的贡献」与「smN 这个信号本身更好」。"""
    from backtest.staged_entry_probe import build_fast_sweep, smooth_series

    raw = _s([0.0, 0.2, -0.1, 0.3, 0.4, -0.2, 0.5, 0.1])
    slow = smooth_series(raw, 5)
    cands = build_fast_sweep(raw, slow, W1, W2, fast_windows=(1, 2))
    assert set(cands) == {"lf_sm5_incumbent", "lf_sm1", "staged_sm1_5",
                          "lf_sm2", "staged_sm2_5"}
    for pos in cands.values():
        assert (pos >= 0).all() and (pos <= 1.0 + 1e-12).all()
