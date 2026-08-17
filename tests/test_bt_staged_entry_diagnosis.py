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


def pytest_approx(v):
    import pytest
    return pytest.approx(v, rel=1e-9, abs=1e-12)
