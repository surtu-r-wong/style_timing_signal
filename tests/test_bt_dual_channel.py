"""双通道执行探针的判例（纯函数/口径，不连库）。

重点钉三件事：名义权重的加权语义、单对信号必须走同一道平滑（否则口径不可比）、
以及"两腿分别跑引擎再加权"不能退化成"仓位加权后跑一次"。
"""
import pandas as pd
import pytest

from backtest.dual_channel_probe import PAIR_COLS, combine, matched_signal


def _mkt(n=200):
    idx = pd.date_range("2021-01-04", periods=n, freq="B")
    spot = pd.Series([0.003] * n, index=idx)      # 现货腿标的
    fut = pd.Series([0.002] * n, index=idx)       # 期货腿标的（不同标的）
    carry = pd.Series([0.245] * n, index=idx)     # 年化 24.5% 贴水 → 每日 0.1%
    pos = pd.Series(1.0, index=idx)
    return pos, spot, fut, carry


def test_notional_weights_are_applied_to_returns():
    """组合收益 = w_spot·现货腿 + w_fut·期货腿（名义加权，非仓位加权）。"""
    pos, spot, fut, carry = _mkt()
    ret, pos_eff = combine(pos, spot, fut, None, 0.4, 0.6, cost_bps=0.0)
    # 首日 pos_eff=0（shift(1)），从第二日起两腿都满仓 → 加权仓位 = 1.0
    assert pos_eff.iloc[0] == pytest.approx(0.0)
    assert pos_eff.iloc[5] == pytest.approx(1.0)
    # 第 6 日收益 = 0.4×0.003 + 0.6×0.002 = 0.0024
    assert ret.iloc[5] == pytest.approx(0.4 * 0.003 + 0.6 * 0.002)


def test_carry_only_enters_the_futures_leg():
    """carry 只加在期货腿上，且按名义权重缩放——现货腿 ETF 没有贴水收益。"""
    pos, spot, fut, carry = _mkt()
    no_c, _ = combine(pos, spot, fut, None, 0.4, 0.6, cost_bps=0.0)
    with_c, _ = combine(pos, spot, fut, carry, 0.4, 0.6, cost_bps=0.0)
    delta = with_c.iloc[5] - no_c.iloc[5]
    assert delta == pytest.approx(0.6 * 0.245 / 245)      # w_fut × carry/ANN


def test_weighted_positions_run_once_is_not_equivalent():
    """反证：把仓位加权后只跑一次引擎，与两腿分别跑再加权**不等价**。

    两腿标的不同（收益 0.003 vs 0.002），单跑一次无论用哪个标的都得不到
    0.4/0.6 的混合收益 —— 这条判例守住 §4 决议 7。
    """
    from backtest.engine import run_strategy

    pos, spot, fut, carry = _mkt()
    ret, _ = combine(pos, spot, fut, None, 0.4, 0.6, cost_bps=0.0)
    single_spot = run_strategy(pos, spot, 0.0, None)["ret"]
    single_fut = run_strategy(pos, fut, 0.0, None)["ret"]
    assert ret.iloc[5] != pytest.approx(single_spot.iloc[5])
    assert ret.iloc[5] != pytest.approx(single_fut.iloc[5])


def test_zero_weight_leg_drops_out():
    pos, spot, fut, carry = _mkt()
    only_fut, _ = combine(pos, spot, fut, None, 0.0, 1.0, cost_bps=0.0)
    assert only_fut.iloc[5] == pytest.approx(0.002)


def test_pair_cols_cover_four_config_groups():
    """PAIR_COLS 必须与 config_4pairs.csv 的 group 顺序一致。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = pd.read_csv(root / "signals/equal_weight/config_4pairs.csv")
    assert len(cfg) == 4
    # group 1..4 ↔ 沪深300/中证500/中证1000/中证2000
    expect = {1: "300", 2: "500", 3: "1000", 4: "2000"}
    for _, r in cfg.iterrows():
        key = expect[int(r["group"])]
        assert key in r["left_column"], \
            f"group {r['group']} 的左腿 {r['left_column']} 与 PAIR_COLS 的 {key} 对不上"
        assert PAIR_COLS[key] == f"pair_0{int(r['group'])}_factor_20"


# ─────────────── 换月成本（口径对齐 Batch 12 §7 exec-price-audit）───────────────

def _held(symbols: list[str]) -> pd.Series:
    idx = pd.date_range("2021-01-04", periods=len(symbols), freq="B")
    return pd.Series(symbols, index=idx)


def test_roll_cost_charges_two_sides_on_symbol_change():
    """换月日 = symbol 变化日；单次 = 平旧+开新 = 2×cost_bps × 有效仓位。"""
    from backtest.dual_channel_probe import roll_cost_series

    held = _held(["IC2101", "IC2101", "IC2102", "IC2102"])
    pos = pd.Series(1.0, index=held.index)
    rc = roll_cost_series(pos, held, cost_bps=3.0)
    assert rc.iloc[0] == 0.0, "首日无前值 → 不算换月"
    assert rc.iloc[1] == 0.0
    assert rc.iloc[2] == pytest.approx(2 * 3.0 / 1e4)      # 换月日
    assert rc.iloc[3] == 0.0


def test_roll_cost_only_on_held_days():
    """空仓日无仓可换 → 零成本（Batch 12 的"只计落在持仓日的"）。"""
    from backtest.dual_channel_probe import roll_cost_series

    held = _held(["IC2101", "IC2102", "IC2103"])
    pos = pd.Series([1.0, 0.0, 1.0], index=held.index)
    rc = roll_cost_series(pos, held, cost_bps=3.0)
    assert rc.iloc[1] == 0.0, "换月日但空仓 → 不计"
    assert rc.iloc[2] == pytest.approx(2 * 3.0 / 1e4)


def test_roll_cost_scales_with_notional_weight():
    """名义 0.6 的期货腿只承担 0.6 倍换月成本——这是"只给一边扣会做偏"的关键。"""
    from backtest.dual_channel_probe import roll_cost_series

    held = _held(["IC2101", "IC2102"])
    full = roll_cost_series(pd.Series(1.0, index=held.index), held, 3.0)
    partial = roll_cost_series(pd.Series(0.6, index=held.index), held, 3.0)
    assert partial.iloc[1] == pytest.approx(0.6 * full.iloc[1])


def test_roll_cost_reindexes_by_ffill_not_interpolation():
    """仓位索引比 held 密时按 ffill 对齐，且不因 object dtype 触发降级警告。"""
    import warnings

    from backtest.dual_channel_probe import roll_cost_series

    held = pd.Series(["IC2101", "IC2102"],
                     index=pd.to_datetime(["2021-01-04", "2021-01-06"]))
    pos = pd.Series(1.0, index=pd.to_datetime(
        ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"]))
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        rc = roll_cost_series(pos, held, 3.0)
    assert rc.loc["2021-01-05"] == 0.0          # ffill 保持 IC2101
    assert rc.loc["2021-01-06"] == pytest.approx(2 * 3.0 / 1e4)
    assert rc.loc["2021-01-07"] == 0.0


def test_matched_signal_is_smoothed_like_production():
    """单对信号必须走与部署同一道 5 日平滑，否则拿未平滑比平滑、口径不可比。"""
    from pathlib import Path

    from backtest.staged_entry_probe import SIGNAL_FILE, smooth_series

    root = Path(__file__).resolve().parents[1]
    df = pd.read_csv(root / SIGNAL_FILE, parse_dates=["date"]).set_index("date")
    raw_pair = df["pair_04_factor_20"]
    got = matched_signal("2000")
    assert got.equals(smooth_series(raw_pair, 5))
    # 平滑真的起了作用（不是恒等）
    assert not got.equals(raw_pair)
