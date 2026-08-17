"""分批候选机制诊断的判例（不连库，只测纯函数）。

这些函数的输出直接支撑「STOP」结论（top1_share=0.547、优势 85% 来自减仓），
算错就结论错，所以语义必须钉死。`build_pair` 连库，不在此测。
"""
import pandas as pd

from backtest.staged_entry_diagnosis import (
    diff_concentration, position_crosstab, yearly_compare,
)


def _frame(rets: list[float], pos: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2022-01-03", periods=len(rets), freq="B")
    return pd.DataFrame({"ret": rets, "pos_eff": pos}, index=idx)


def test_top_share_is_signed_and_relative_to_total():
    """top-N 份额 = 带符号和 / 累计和。份额 >1 表示其余天在反向抵消——
    这正是 val 窗实测的形态（top5_share=1.049）。"""
    inc = _frame([0.0, 0.0, 0.0, 0.0], [1.0] * 4)
    stg = _frame([0.10, -0.02, -0.02, -0.02], [0.4] * 4)   # 累计 +0.04，一天撑起 250%
    out = diff_concentration(inc, stg)
    assert out["total_diff_sum"] == pytest_approx(0.04)
    assert out["top1_share"] == pytest_approx(0.10 / 0.04)      # 2.5
    assert out["days_stg_better"] == 1 and out["days_stg_worse"] == 3
    assert out["n_days"] == 4


def test_identical_days_are_counted_separately():
    """两者仓位相同的日子 diff 恒 0，必须单独记数而不是算进"更好/更差"。"""
    inc = _frame([0.01, 0.02, 0.03], [1.0] * 3)
    stg = _frame([0.01, 0.02, 0.03], [1.0] * 3)
    out = diff_concentration(inc, stg)
    assert out["days_identical"] == 3
    assert out["days_stg_better"] == 0 and out["days_stg_worse"] == 0


def test_nav_uses_compounding_not_sum():
    inc = _frame([0.10, 0.10], [1.0, 1.0])
    stg = _frame([0.0, 0.0], [0.0, 0.0])
    out = diff_concentration(inc, stg)
    assert out["nav_inc"] == pytest_approx(1.21)     # 1.1×1.1，不是 1.20
    assert out["nav_stg"] == pytest_approx(1.0)


def test_crosstab_classifies_exposure_direction():
    """交叉表要能分开「提前减仓」与「提前进场」——Q4 的全部答案就在这个分类上。"""
    inc = _frame([-0.05, 0.0, 0.02], [1.0, 0.0, 1.0])
    stg = _frame([-0.02, 0.01, 0.02], [0.4, 0.4, 1.0])
    und = pd.Series([-0.05, 0.025, 0.02], index=inc.index)
    ct = position_crosstab(inc, stg, und)

    lower = ct[(ct.index.get_level_values("inc_pos") == 1.0)
               & (ct.index.get_level_values("stg_pos") == 0.4)]
    assert lower["kind"].iloc[0] == "分批暴露更低（提前/多减）"
    assert lower["days"].iloc[0] == 1
    assert lower["diff_sum"].iloc[0] == pytest_approx(0.03)   # 跌时少亏

    higher = ct[(ct.index.get_level_values("inc_pos") == 0.0)
                & (ct.index.get_level_values("stg_pos") == 0.4)]
    assert higher["kind"].iloc[0] == "分批暴露更高（提前/多进）"

    same = ct[ct["kind"] == "相同"]
    assert same["diff_sum"].iloc[0] == pytest_approx(0.0)
    # 份额应加总到 1（浮点容差内）
    assert ct["diff_share"].sum() == pytest_approx(1.0)


def test_yearly_compare_splits_by_calendar_year():
    idx = pd.to_datetime(["2021-06-01", "2021-06-02", "2022-06-01", "2022-06-02"])
    inc = pd.DataFrame({"ret": [0.01, 0.01, -0.01, -0.01], "pos_eff": 1.0}, index=idx)
    stg = pd.DataFrame({"ret": [0.02, 0.02, 0.00, 0.00], "pos_eff": 0.4}, index=idx)
    yc = yearly_compare(inc, stg)
    assert list(yc.index) == [2021, 2022]
    assert yc.loc[2021, "n"] == 2 and yc.loc[2022, "n"] == 2
    assert yc.loc[2022, "inc_ann"] < 0 < yc.loc[2021, "inc_ann"]


# ─────────── 回撤维度：等暴露对照 / 缩仓阶梯（含二次 shift 的防回归）───────────

def _mkt(n: int = 300):
    """合成标的收益 + 零 carry：前段涨、中段跌、后段涨，便于造择时。"""
    import numpy as np
    idx = pd.date_range("2021-01-04", periods=n, freq="B")
    r = np.concatenate([
        np.full(n // 3, 0.004), np.full(n // 3, -0.006),
        np.full(n - 2 * (n // 3), 0.003)])
    return pd.Series(r, index=idx), pd.Series(0.0, index=idx)


def test_scaling_ladder_k1_reproduces_direct_run():
    """k=1.0 那行必须与「直接跑信号仓位」逐位一致。

    **这条是二次 shift 的防回归**：若误把结果表里的 `pos_eff`（已 shift(1)）
    传进来，`run_strategy` 会再 shift 一次 → 等于偷偷换成 shift(2) 口径，
    这条断言立刻失败。2026-08-17 实际踩过。
    """
    from backtest.engine import run_strategy
    from backtest.metrics import max_drawdown, sharpe
    from backtest.staged_entry_diagnosis import scaling_ladder

    und, car = _mkt()
    pos = pd.Series(1.0, index=und.index)
    pos.iloc[100:200] = 0.0
    direct = run_strategy(pos, und, 3.0, car)["ret"]
    lad = scaling_ladder(pos, und, car, 3.0, ks=(1.0,))
    assert lad.loc[1.0, "sharpe"] == pytest_approx(sharpe(direct))
    assert lad.loc[1.0, "maxdd"] == pytest_approx(max_drawdown(direct))

    # 反证：传 shift 过的仓位会得到不同结果（即坑真的存在，断言不是空转）
    shifted = scaling_ladder(pos.shift(1).fillna(0.0), und, car, 3.0, ks=(1.0,))
    assert shifted.loc[1.0, "sharpe"] != pytest_approx(sharpe(direct))


def test_scaling_preserves_sharpe_exactly():
    """等比缩仓 Sharpe 严格不变——这是"缩仓不是 alpha"的数学依据。"""
    from backtest.staged_entry_diagnosis import scaling_ladder

    und, car = _mkt()
    pos = pd.Series(1.0, index=und.index)
    pos.iloc[120:180] = 0.0
    lad = scaling_ladder(pos, und, car, 3.0, ks=(1.0, 0.8, 0.5, 0.25))
    base = lad.loc[1.0, "sharpe"]
    for k in (0.8, 0.5, 0.25):
        assert lad.loc[k, "sharpe"] == pytest_approx(base)
    # 回撤则应随 k 单调变浅
    assert lad["maxdd"].is_monotonic_increasing


def test_exposure_scaled_flags_pure_exposure_effect():
    """候选 = 现役 × 常数 → 回撤差异应归零（纯暴露效应）。"""
    from backtest.staged_entry_diagnosis import exposure_scaled_compare

    und, car = _mkt()
    inc = pd.Series(1.0, index=und.index)
    inc.iloc[100:160] = 0.0
    cand = inc * 0.6
    out = exposure_scaled_compare(inc, cand, und, car, 3.0,
                                  {"full": (None, None)})
    assert out.loc["full", "k"] == pytest_approx(0.6)
    assert abs(out.loc["full", "gap_pp"]) < 0.5
    assert out.loc["full", "verdict"] == "纯暴露效应"


def test_exposure_scaled_detects_real_timing():
    """候选择时性地在跌段降暴露 → 必须判「择时有贡献」。"""
    from backtest.staged_entry_diagnosis import exposure_scaled_compare

    und, car = _mkt()
    inc = pd.Series(1.0, index=und.index)
    cand = inc.copy()
    down = und < 0
    cand[down] = 0.0                      # 精准躲掉整个跌段
    out = exposure_scaled_compare(inc, cand, und, car, 3.0, {"full": (None, None)})
    assert out.loc["full", "k"] < 1.0
    assert out.loc["full", "gap_pp"] > 1.5
    assert out.loc["full", "verdict"] == "择时有贡献"


def pytest_approx(v):
    import pytest
    return pytest.approx(v, rel=1e-9, abs=1e-12)
