"""方向 C · B1-T2/T3：截面打分装配 + 分桶 + 篮子价差（signals/style_basket）。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.factors import (  # noqa: E402
    composite_score,
    cross_section_zscore,
    winsorize,
)
from signals.style_basket.scoring import (  # noqa: E402
    basket_spread_returns,
    select_baskets,
    style_scores,
    universe_mask,
)


def _factor_frame():
    """8 股截面：含一个极端离群（s7 的 ep）与一只金融股（s8 cfp=NaN）。"""
    rng = np.random.default_rng(7)
    df = pd.DataFrame(
        rng.normal(size=(8, 6)),
        index=[f"s{i}" for i in range(1, 9)],
        columns=["sal_g", "pro_g", "ep", "bp", "cfp", "dp"],
    )
    df.loc["s7", "ep"] = 50.0  # 离群，必须被缩尾抑制
    df.loc["s8", "cfp"] = np.nan  # 金融股剔 CF/P
    return df


def test_style_scores_assembles_winsorize_z_composite():
    """装配正确性：逐列 缩尾→截面z，成长=[sal_g,pro_g]、价值=[ep,bp,cfp,dp] 各自 /√n 合成。"""
    factors = _factor_frame()
    got = style_scores(factors, lower=0.10, upper=0.90)

    z = pd.DataFrame(
        {c: cross_section_zscore(winsorize(factors[c], 0.10, 0.90)) for c in factors}
    )
    expected_growth = composite_score(z[["sal_g", "pro_g"]])
    expected_value = composite_score(z[["ep", "bp", "cfp", "dp"]])

    pd.testing.assert_series_equal(got["growth_score"], expected_growth, check_names=False)
    pd.testing.assert_series_equal(got["value_score"], expected_value, check_names=False)
    pd.testing.assert_series_equal(
        got["style_score"], expected_growth - expected_value, check_names=False
    )


def test_style_scores_outlier_is_capped_and_financial_uses_three_value_factors():
    """行为断言：离群 ep 缩尾后 z 有界；金融股价值分按 3 因子 /√3 归一非 NaN。"""
    factors = _factor_frame()
    got = style_scores(factors, lower=0.10, upper=0.90)
    # 8 个样本缩尾后 z 不可能超过 √7（全距界），远小于不缩尾时离群的 z≈2.6+
    assert got["value_score"].abs().max() < 4.0
    assert pd.notna(got.loc["s8", "value_score"])  # 金融股仍有价值分


def test_select_baskets_top_bottom_30pct_dropping_nan():
    """style_score 降序 Top 30% = 成长篮、Bottom 30% = 价值篮；NaN 不入任何篮。"""
    scores = pd.Series(
        {f"s{i}": float(i) for i in range(1, 11)} | {"s_nan": np.nan}
    )  # 1..10 + NaN，30% → 各 3 只
    growth, value = select_baskets(scores, pct=0.3)
    assert set(growth) == {"s10", "s9", "s8"}
    assert set(value) == {"s1", "s2", "s3"}


def test_select_baskets_empty_when_too_few():
    """有效样本不足（各桶不足 1 只）→ 两篮皆空。"""
    growth, value = select_baskets(pd.Series({"a": 1.0}), pct=0.3)
    assert len(growth) == 0 and len(value) == 0


def test_universe_mask_rank_bands():
    """U0 全市场 / U1 剔前300 / U2 301-1800 / U3 301-3800 / U4 1801-3800（按市值排名）。"""
    mv = pd.Series({f"s{i}": float(5000 - i) for i in range(1, 4001)})  # s1 最大
    rank = mv.rank(ascending=False)
    assert universe_mask(rank, "U0").all()
    assert not universe_mask(rank, "U1")["s300"] and universe_mask(rank, "U1")["s301"]
    u2 = universe_mask(rank, "U2")
    assert u2["s301"] and u2["s1800"] and not u2["s1801"]
    u3 = universe_mask(rank, "U3")
    assert u3["s301"] and u3["s3800"] and not u3["s3801"]
    u4 = universe_mask(rank, "U4")
    assert not u4["s1800"] and u4["s1801"] and u4["s3800"] and not u4["s3801"]


def _ticker_facts():
    """一只票的合成财务事实：income 15 季 YTD + CSMAR cfo YTD + Wind cfo TTM 行 + balance + dividend。

    15 季 → TTM 有效 12 期（2019Q4..2022Q3）→ 12 窗斜率恰在 2022Q3 首次可算。
    """
    rows = []
    # income：2019Q1..2022Q3 共 15 季，营收单季恒 100、归母净利单季恒 40
    q_ends = pd.date_range("2019-03-31", periods=15, freq="QE")
    for d in q_ends:
        m = (d.month - 1) // 3 + 1
        ann = d + pd.Timedelta(days=30)
        rows.append((d, ann, "income", {"revenue": 100.0 * m, "net_profit_parent_ytd": 40.0 * m}))
        if d <= pd.Timestamp("2021-12-31"):  # CSMAR 段 cfo YTD
            rows.append((d, ann, "cashflow_direct", {"cfo_net": 30.0 * m}))
        else:  # Wind 段：cfo 以 TTM 直接值出现在 statement_type='cashflow'
            rows.append((d, ann, "cashflow", {"cfo_ttm": 120.0}))
        rows.append((d, ann, "balance", {"equity_parent": 1000.0 + d.year - 2019}))
    rows.append((pd.Timestamp("2021-12-31"), pd.Timestamp("2022-04-30"), "dividend",
                 {"cash_dividend_ps_pre_tax": 5.0}))
    return pd.DataFrame(
        {
            "ts_code": "X1",
            "end_date": [r[0] for r in rows],
            "ann_date": [r[1] for r in rows],
            "statement_type": [r[2] for r in rows],
            "data": [r[3] for r in rows],
        }
    )


def test_ticker_financial_rows_builds_pools_with_wind_cfo_splice():
    """单票 facts → pooled 行：TTM/斜率/事件三类；CFO 在 CSMAR YTD 链后拼 Wind 直接 TTM。"""
    from signals.style_basket.build import ticker_financial_rows

    pools = ticker_financial_rows(_ticker_facts(), growth_n=12)
    ttm, slope, event = pools["ttm"], pools["slope"], pools["event"]

    # 营收 TTM：2019Q4 起 = 400（单季 100×4）
    rev = ttm[(ttm["field"] == "rev")].set_index("end_date")
    assert rev.loc[pd.Timestamp("2019-12-31"), "ttm"] == 400.0
    assert rev.loc[pd.Timestamp("2022-09-30"), "ttm"] == 400.0

    # CFO：CSMAR 链 2019Q4..2021Q4 = 120；Wind 段 2022Q1/Q2 直接 TTM=120
    cfo = ttm[(ttm["field"] == "cfo")].set_index("end_date")
    assert cfo.loc[pd.Timestamp("2021-12-31"), "ttm"] == 120.0
    assert cfo.loc[pd.Timestamp("2022-03-31"), "ttm"] == 120.0  # 来自 Wind 行
    assert cfo.loc[pd.Timestamp("2022-06-30"), "ttm"] == 120.0
    # Wind 行无差分依赖：known = 自身 ann
    assert cfo.loc[pd.Timestamp("2022-03-31"), "known_date"] == pd.Timestamp("2022-03-31") + pd.Timedelta(days=30)

    # 斜率：15 季 TTM 恒定 → 2022Q3 处 slope=0（12 窗全有效，首次可算）
    np_slope = slope[slope["field"] == "np"].set_index("end_date")
    assert np_slope.loc[pd.Timestamp("2022-09-30"), "slope"] == pytest.approx(0.0)

    # 事件：equity 与 dps
    eq = event[event["field"] == "equity"].set_index("end_date")
    assert eq.loc[pd.Timestamp("2022-09-30"), "value"] == 1003.0
    dps = event[event["field"] == "dps"]
    assert dps["value"].iloc[0] == 5.0
    # 所有 pooled 行都带 ts_code
    assert (ttm["ts_code"] == "X1").all() and (slope["ts_code"] == "X1").all()


def test_basket_spread_returns_equal_weight_and_schedule_switch():
    """formation 收盘建仓：次日起等权日收益计入，至下一 formation（含）；停牌 NaN 跳过。"""
    dates = pd.date_range("2020-01-01", periods=6, freq="B")
    returns = pd.DataFrame(
        {
            "g1": [0.01, 0.01, 0.02, 0.00, 0.03, 0.01],
            "g2": [0.02, 0.00, 0.04, 0.02, np.nan, 0.05],
            "v1": [0.00, 0.01, 0.01, 0.02, 0.01, 0.00],
        },
        index=dates,
    )
    schedule = [
        (dates[1], ["g1", "g2"], ["v1"]),  # d2 收盘建仓
        (dates[3], ["g2"], ["v1"]),  # d4 收盘换仓
    ]
    got = basket_spread_returns(returns, schedule)
    assert list(got.index) == list(dates[2:])  # 首个 formation 次日起
    assert got.loc[dates[2], "growth_ret"] == pytest.approx(0.03)  # (0.02+0.04)/2
    assert got.loc[dates[2], "spread"] == pytest.approx(0.02)
    assert got.loc[dates[3], "spread"] == pytest.approx(0.01 - 0.02)  # 换仓日仍旧仓
    assert np.isnan(got.loc[dates[4], "growth_ret"])  # 新仓 g2 当日停牌 → NaN
    assert got.loc[dates[5], "spread"] == pytest.approx(0.05 - 0.00)
