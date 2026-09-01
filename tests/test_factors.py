"""方向 C · B1-T2 因子构造 TDD。

财务事实（signals.common.financial_reader.fetch_financial_facts 归一化输出）→
YTD→TTM → 成长斜率 / 价值比率 → 截面标准化合成。纯函数、合成数据可测。
"""
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
    extract_statement_field,
    filter_quarter_ends,
    growth_slope,
    latest_pit,
    pit_ttm_series,
    quarterize_ytd,
    stock_style_factors,
    ttm_from_quarterly,
    winsorize,
)


def _income_ytd(rows):
    """rows: list of (end_date, ann_date, ytd_value) → DataFrame（含 pit_ttm_series 期望列）。"""
    return pd.DataFrame(
        {
            "end_date": pd.to_datetime([r[0] for r in rows]),
            "ann_date": pd.to_datetime([r[1] for r in rows]),
            "value": [r[2] for r in rows],
        }
    )


def _five_quarter_ytd_with_provenance():
    """2019Q1..2020Q1 的 YTD 明细，默认全部依赖已验证。"""
    end_dates = pd.date_range("2019-03-31", periods=5, freq="QE")
    return pd.DataFrame(
        {
            "end_date": end_dates,
            "ann_date": end_dates + pd.Timedelta(days=30),
            "value": [10.0, 30.0, 60.0, 100.0, 12.0],
            "true_first_disclosure_verified": [True] * 5,
            "unverified_dependency_keys": [()] * 5,
        }
    )


def test_quarterize_ytd_differences_within_year_and_resets():
    """YTD 累计 → 单季：同年后续季度差分，跨年 Q1 重置为 YTD 本身。"""
    ytd = pd.Series(
        {
            pd.Timestamp("2019-03-31"): 10.0,
            pd.Timestamp("2019-06-30"): 25.0,
            pd.Timestamp("2019-09-30"): 45.0,
            pd.Timestamp("2019-12-31"): 70.0,
            pd.Timestamp("2020-03-31"): 12.0,
        }
    )
    got = quarterize_ytd(ytd)
    assert got[pd.Timestamp("2019-03-31")] == 10.0  # Q1 = YTD
    assert got[pd.Timestamp("2019-06-30")] == 15.0  # 25 - 10
    assert got[pd.Timestamp("2019-09-30")] == 20.0  # 45 - 25
    assert got[pd.Timestamp("2019-12-31")] == 25.0  # 70 - 45
    assert got[pd.Timestamp("2020-03-31")] == 12.0  # 新年 Q1 = YTD


def test_filter_quarter_ends_drops_01_01_pseudo_and_daily_ttm_rows():
    """滤掉 CSMAR 01-01 伪行（=上年年报重复）与 Wind 日频 TTM 行，只留自然季末。"""
    s = pd.Series(
        {
            pd.Timestamp("2019-01-01"): 999.0,  # 伪行：上年年报重复
            pd.Timestamp("2019-03-31"): 10.0,  # Q1（真季末）
            pd.Timestamp("2019-06-30"): 25.0,  # H1（真季末）
            pd.Timestamp("2025-04-28"): 888.0,  # Wind 日频 TTM 行
        }
    )
    got = filter_quarter_ends(s)
    assert list(got.index) == [pd.Timestamp("2019-03-31"), pd.Timestamp("2019-06-30")]
    assert got[pd.Timestamp("2019-03-31")] == 10.0


def test_ttm_sums_four_consecutive_quarters():
    """TTM = 滚动 4 个连续单季之和；不足 4 季 → NaN。"""
    q = pd.Series(
        {
            pd.Timestamp("2019-03-31"): 10.0,
            pd.Timestamp("2019-06-30"): 15.0,
            pd.Timestamp("2019-09-30"): 20.0,
            pd.Timestamp("2019-12-31"): 25.0,
            pd.Timestamp("2020-03-31"): 12.0,
        }
    )
    got = ttm_from_quarterly(q)
    assert pd.isna(got[pd.Timestamp("2019-03-31")])  # 不足 4 季
    assert pd.isna(got[pd.Timestamp("2019-09-30")])
    assert got[pd.Timestamp("2019-12-31")] == 70.0  # 10+15+20+25
    assert got[pd.Timestamp("2020-03-31")] == 72.0  # 15+20+25+12


def test_ttm_is_nan_across_missing_quarter_and_recovers():
    """缺季 → 跨该缺口的 TTM 窗口为 NaN；越过缺口后恢复。"""
    q = pd.Series(
        {
            pd.Timestamp("2019-03-31"): 10.0,
            # 2019-06-30 缺失
            pd.Timestamp("2019-09-30"): 20.0,
            pd.Timestamp("2019-12-31"): 25.0,
            pd.Timestamp("2020-03-31"): 12.0,
            pd.Timestamp("2020-06-30"): 18.0,
        }
    )
    got = ttm_from_quarterly(q)
    assert pd.isna(got[pd.Timestamp("2020-03-31")])  # 窗口含缺失的 2019-06-30
    assert got[pd.Timestamp("2020-06-30")] == 75.0  # 20+25+12+18，越过缺口


def test_growth_slope_is_ols_slope_over_abs_mean():
    """成长分 = 最近 n 季 TTM 对时间的 OLS 斜率 ÷ |均值|（设计稿 §2.5）。"""
    ttm = pd.Series([100.0 + 10.0 * i for i in range(12)])  # 完美线性 +10/季
    got = growth_slope(ttm, n=12)
    assert got == pytest.approx(10.0 / 155.0)  # 斜率 10 / 均值 155


def test_growth_slope_uses_trailing_n_quarters():
    """只用尾部 n 季：更早的历史不参与。"""
    ttm = pd.Series([50.0 * i for i in range(4)] + [200.0] * 12)  # 尾部 12 季持平
    assert growth_slope(ttm, n=12) == pytest.approx(0.0)  # 持平 → 斜率 0


def test_growth_slope_nan_when_fewer_than_n_valid():
    """有效 TTM 不足 n 季 → NaN（不外推）。"""
    ttm = pd.Series([100.0, 110.0, np.nan, 120.0])
    assert np.isnan(growth_slope(ttm, n=12))


def test_latest_pit_picks_most_recent_period_announced_by_as_of():
    """PIT：只用 ann_date ≤ as_of 的行；同披露日取最新 end_date（年报/Q1 常同日披露）。"""
    df = pd.DataFrame(
        {
            "end_date": pd.to_datetime(["2019-12-31", "2020-03-31", "2020-06-30"]),
            "ann_date": pd.to_datetime(["2020-04-30", "2020-04-30", "2020-08-31"]),
            "value": [100.0, 110.0, 120.0],
        }
    )
    # 06-01：2020-06-30 未披露（08-31），已披露最新期=2020-03-31
    assert latest_pit(df, pd.Timestamp("2020-06-01")) == 110.0
    # 09-01：2020-06-30 已披露
    assert latest_pit(df, pd.Timestamp("2020-09-01")) == 120.0
    # 01-01：无任何行已披露 → NaN
    assert np.isnan(latest_pit(df, pd.Timestamp("2020-01-01")))


def test_winsorize_clips_tails_to_quantiles():
    """截面缩尾：低于下分位截到下分位、高于上分位截到上分位（设计稿 §2.5 5%/95%）。"""
    s = pd.Series([float(i) for i in range(11)])  # 0..10
    got = winsorize(s, 0.10, 0.90)  # q10=1, q90=9
    assert got.iloc[0] == 1.0  # 0 → 上抬到 1
    assert got.iloc[-1] == 9.0  # 10 → 下压到 9
    assert got.iloc[5] == 5.0  # 中间不动


def test_cross_section_zscore_standardizes_and_ignores_nan():
    """截面 Z：(x−均值)/标准差；NaN 不参与统计且原样保留。"""
    got = cross_section_zscore(pd.Series([10.0, 20.0, 30.0, np.nan]))
    assert got.iloc[0] == pytest.approx(-1.0)  # 均值 20、样本标准差 10
    assert got.iloc[1] == pytest.approx(0.0)
    assert got.iloc[2] == pytest.approx(1.0)
    assert np.isnan(got.iloc[3])


def test_composite_sums_zscores_over_sqrt_n():
    """合成 = Σz / √n（n=因子数），独立因子下保持单位方差量级（设计稿 §2.5）。"""
    z = pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, 0.0], "c": [1.0, 4.0]})
    got = composite_score(z)
    assert got.iloc[0] == pytest.approx(3.0 / np.sqrt(3))  # (1+1+1)/√3
    assert got.iloc[1] == pytest.approx(6.0 / np.sqrt(3))  # (2+0+4)/√3


def test_composite_divides_by_sqrt_of_available_factors():
    """缺因子（如金融股剔 CF/P）：按可用因子数 √ 归一，不被缺失稀释。"""
    z = pd.DataFrame(
        {
            "ep": [1.0, 1.0],
            "bp": [1.0, 1.0],
            "cfp": [1.0, np.nan],  # 金融股：CF/P 剔除
            "dp": [1.0, 1.0],
        }
    )
    got = composite_score(z)
    assert got.iloc[0] == pytest.approx(4.0 / np.sqrt(4))  # 四因子齐
    assert got.iloc[1] == pytest.approx(3.0 / np.sqrt(3))  # 仅三因子


def test_composite_nan_when_no_factor_available():
    """一行全缺 → NaN（min_factors 兜底）。"""
    z = pd.DataFrame({"a": [1.0, np.nan], "b": [2.0, np.nan]})
    got = composite_score(z, min_factors=1)
    assert got.iloc[0] == pytest.approx(3.0 / np.sqrt(2))
    assert np.isnan(got.iloc[1])


def test_pit_ttm_no_leak_when_q1_announced_before_annual():
    """PIT 依赖窗：TTM(Q1-20) = Q1ytd-20 + 年报-19 − Q1ytd-19，须等年报披露才可知。

    年报 5-10 晚于 Q1 的 4-25 披露（法定上限同为 4-30 前后，实务存在乱序）：
    as_of=4-30 时 Q1 行自身 ann≤as_of，但其 TTM 用到未公开的年报 → 必须剔除。
    """
    income = _income_ytd(
        [
            ("2019-03-31", "2019-04-30", 10.0),
            ("2019-06-30", "2019-08-31", 25.0),
            ("2019-09-30", "2019-10-31", 45.0),
            ("2019-12-31", "2020-05-10", 70.0),  # 年报延迟披露
            ("2020-03-31", "2020-04-25", 12.0),  # Q1 先披露
        ]
    )
    # 4-30：年报未出 → TTM(Q4-19)=70 不可知；TTM(Q1-20)=72 依赖年报也不可知 → 空
    got = pit_ttm_series(income, pd.Timestamp("2020-04-30"))
    assert len(got) == 0
    # 5-15：年报已出 → 两期都可知
    got2 = pit_ttm_series(income, pd.Timestamp("2020-05-15"))
    assert list(got2.to_numpy()) == [70.0, 72.0]


def test_pit_ttm_with_known_exposes_grid_and_known_dates():
    """批量接口：整段季度网格 + 每期 TTM 的可知日（依赖窗内 ann 的最大值）。"""
    from signals.common.factors import pit_ttm_with_known

    income = _income_ytd(
        [
            ("2019-03-31", "2019-04-30", 10.0),
            ("2019-06-30", "2019-08-31", 25.0),
            ("2019-09-30", "2019-10-31", 45.0),
            ("2019-12-31", "2020-05-10", 70.0),
            ("2020-03-31", "2020-04-25", 12.0),
        ]
    )
    got = pit_ttm_with_known(income)
    assert list(got.columns) == ["ttm", "known_date"]
    # 网格完整（2019Q1..2020Q1 共 5 行），前三期 TTM 不足 4 季 → NaN
    assert len(got) == 5
    assert pd.isna(got["ttm"].iloc[0])
    assert got.loc[pd.Timestamp("2019-12-31"), "ttm"] == 70.0
    assert got.loc[pd.Timestamp("2019-12-31"), "known_date"] == pd.Timestamp("2020-05-10")
    assert got.loc[pd.Timestamp("2020-03-31"), "ttm"] == 72.0
    assert got.loc[pd.Timestamp("2020-03-31"), "known_date"] == pd.Timestamp("2020-05-10")


def test_pit_ttm_with_known_dedups_same_quarter_keeping_first_announced():
    """同一季末出现两行（重述）：PIT 取先披露那行的值，不吃后来的重述。"""
    from signals.common.factors import pit_ttm_with_known

    income = _income_ytd(
        [
            ("2019-03-31", "2019-04-30", 10.0),
            ("2019-06-30", "2019-08-31", 25.0),
            ("2019-09-30", "2019-10-31", 45.0),
            ("2019-12-31", "2020-04-30", 70.0),
            ("2019-12-31", "2020-06-30", 90.0),  # 重述行，披露更晚
        ]
    )
    got = pit_ttm_with_known(income)
    assert got.loc[pd.Timestamp("2019-12-31"), "ttm"] == 70.0  # 用原始首披值


def test_ttm_provenance_propagates_actual_ytd_dependencies():
    """2020Q1 TTM 的真实差分链包含 2019Q1，故精确传播其未验证依赖。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    key = "X|2019-03-31|income|csmar"
    income.loc[0, "true_first_disclosure_verified"] = False
    income.at[0, "unverified_dependency_keys"] = (key,)

    got = pit_ttm_with_known(income)

    row = got.loc[pd.Timestamp("2020-03-31")]
    assert row["true_first_disclosure_verified"] is False
    assert row["unverified_dependency_keys"] == (key,)
    assert got.loc[pd.Timestamp("2019-09-30"), "unverified_dependency_keys"] is None


def test_ttm_provenance_dependency_union_is_sorted_and_deduplicated():
    """多个实际 YTD 依赖的 keys 在 TTM 行上稳定排序并去重。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income.loc[[0, 1], "true_first_disclosure_verified"] = False
    income.at[0, "unverified_dependency_keys"] = ("z", "a", "z")
    income.at[1, "unverified_dependency_keys"] = ("m", "a")

    got = pit_ttm_with_known(income)

    assert got.loc[pd.Timestamp("2020-03-31"), "unverified_dependency_keys"] == (
        "a",
        "m",
        "z",
    )


def test_ttm_provenance_rejects_inconsistent_flag_and_keys():
    """verified flag 必须与依赖 tuple 是否为空严格一致。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income.loc[0, "true_first_disclosure_verified"] = False

    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(income)


@pytest.mark.parametrize(
    ("flag", "keys"),
    [
        (1, ()),
        (True, []),
        (False, ("",)),
        (False, (1,)),
    ],
)
def test_ttm_provenance_rejects_invalid_types(flag, keys):
    """flag、tuple 容器及其中 key 都执行 strict provenance 类型校验。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income["true_first_disclosure_verified"] = income[
        "true_first_disclosure_verified"
    ].astype(object)
    income.at[0, "true_first_disclosure_verified"] = flag
    income.at[0, "unverified_dependency_keys"] = keys

    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(income)


def test_unselected_restatement_does_not_contaminate_ttm_provenance():
    """同季晚披露重述未被数值路径选中，也不应成为 TTM 的 provenance 依赖。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    restatement = income.iloc[[0]].copy()
    restatement["ann_date"] = restatement["ann_date"] + pd.Timedelta(days=10)
    restatement["value"] = 999.0
    restatement["true_first_disclosure_verified"] = False
    restatement["unverified_dependency_keys"] = [
        ("X|2019-03-31|income|later-restatement",)
    ]
    income = pd.concat([income, restatement], ignore_index=True)

    got = pit_ttm_with_known(income)

    row = got.loc[pd.Timestamp("2020-03-31")]
    assert row["ttm"] == 102.0
    assert row["true_first_disclosure_verified"] is True
    assert row["unverified_dependency_keys"] == ()


def test_unselected_restatement_cannot_fill_invalid_first_provenance():
    """晚重述的有效 pair 不得填补首披 null provenance 并绕过 strict 校验。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income["true_first_disclosure_verified"] = income[
        "true_first_disclosure_verified"
    ].astype(object)
    income.at[0, "true_first_disclosure_verified"] = None
    income.at[0, "unverified_dependency_keys"] = None
    restatement = income.iloc[[0]].copy()
    restatement["ann_date"] = restatement["ann_date"] + pd.Timedelta(days=10)
    restatement["value"] = 999.0
    restatement["true_first_disclosure_verified"] = True
    restatement["unverified_dependency_keys"] = [()]
    income = pd.concat([income, restatement], ignore_index=True)

    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(income)


def test_ttm_provenance_rejects_value_row_with_later_announcement():
    """value 实际来自晚披露行时，冻结 known_date 无法准确表达，必须 fail closed。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income.loc[0, "value"] = np.nan
    value_row = income.iloc[[0]].copy()
    value_row["ann_date"] = value_row["ann_date"] + pd.Timedelta(days=10)
    value_row["value"] = 10.0
    value_row["true_first_disclosure_verified"] = False
    value_row["unverified_dependency_keys"] = [("later-value",)]
    income = pd.concat([income, value_row], ignore_index=True)

    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(income)


def test_ttm_provenance_uses_value_row_when_announcement_matches():
    """同披露日首行 value 为空时，依赖绑定实际提供 value 的同行。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance()
    income.loc[0, "value"] = np.nan
    value_row = income.iloc[[0]].copy()
    value_row["value"] = 10.0
    value_row["true_first_disclosure_verified"] = False
    value_row["unverified_dependency_keys"] = [("same-day-value",)]
    income = pd.concat([income, value_row], ignore_index=True)

    got = pit_ttm_with_known(income)

    row = got.loc[pd.Timestamp("2020-03-31")]
    assert row["ttm"] == 102.0
    assert row["true_first_disclosure_verified"] is False
    assert row["unverified_dependency_keys"] == ("same-day-value",)


def test_legacy_groupby_first_value_and_known_date_remain_frozen():
    """无 provenance 的同构输入仍沿用逐列 groupby.first 数值与 known_date 语义。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance().drop(
        columns=["true_first_disclosure_verified", "unverified_dependency_keys"]
    )
    income.loc[0, "value"] = np.nan
    value_row = income.iloc[[0]].copy()
    value_row["ann_date"] = value_row["ann_date"] + pd.Timedelta(days=10)
    value_row["value"] = 10.0
    income = pd.concat([income, value_row], ignore_index=True)

    got = pit_ttm_with_known(income)

    assert list(got.columns) == ["ttm", "known_date"]
    assert got.loc[pd.Timestamp("2019-12-31"), "ttm"] == 100.0
    assert got.loc[pd.Timestamp("2019-12-31"), "known_date"] == pd.Timestamp(
        "2020-01-30"
    )
    assert got.loc[pd.Timestamp("2020-03-31"), "ttm"] == 102.0
    assert got.loc[pd.Timestamp("2020-03-31"), "known_date"] == pd.Timestamp(
        "2020-04-30"
    )


def test_ttm_without_provenance_preserves_legacy_schema():
    """公共 B1 facts 不含 provenance 时，输出 schema 必须完全不变。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance().drop(
        columns=["true_first_disclosure_verified", "unverified_dependency_keys"]
    )

    got = pit_ttm_with_known(income)

    assert list(got.columns) == ["ttm", "known_date"]


@pytest.mark.parametrize(
    "missing_column",
    ["true_first_disclosure_verified", "unverified_dependency_keys"],
)
def test_partial_provenance_is_rejected(missing_column):
    """成对 provenance 列只出现一列时不得静默降级。"""
    from signals.common.factors import pit_ttm_with_known

    income = _five_quarter_ytd_with_provenance().drop(columns=[missing_column])

    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(income)


def test_rolling_growth_slope_matches_growth_slope_on_contiguous_windows():
    """滚动斜率 = 逐季调用 growth_slope 的向量化等价（连续窗口逐值恒等）。"""
    from signals.common.factors import rolling_growth_slope

    idx = pd.date_range("2016-03-31", periods=14, freq="QE")
    ttm = pd.Series(100.0 + 7.0 * np.arange(14) + 3.0 * np.sin(np.arange(14)), index=idx)
    known = pd.Series(idx + pd.Timedelta(days=30), index=idx)
    got = rolling_growth_slope(ttm, known, n=12)
    assert list(got.columns) == ["slope", "known_date"]
    # 前 11 期不足窗 → NaN
    assert got["slope"].iloc[:11].isna().all()
    for i in [11, 12, 13]:
        expected = growth_slope(ttm.iloc[: i + 1], n=12)
        assert got["slope"].iloc[i] == pytest.approx(expected)
    # 斜率可知日 = 窗内 12 期 TTM 可知日的最大值（最后一期 +30 天）
    assert got["known_date"].iloc[11] == known.iloc[11]


def test_rolling_growth_slope_nan_across_gap():
    """窗口含缺季（TTM NaN）→ 斜率 NaN（严于 growth_slope 的 dropna 语义）。"""
    from signals.common.factors import rolling_growth_slope

    idx = pd.date_range("2016-03-31", periods=14, freq="QE")
    vals = 100.0 + 7.0 * np.arange(14)
    vals[6] = np.nan  # 中间缺一季
    ttm = pd.Series(vals, index=idx)
    known = pd.Series(idx + pd.Timedelta(days=30), index=idx)
    got = rolling_growth_slope(ttm, known, n=12)
    # 14 期缺 i=6：任何 12 长窗口 [i−11..i]（i=11,12,13）都覆盖缺口 → 全 NaN
    assert got["slope"].isna().all()


def test_asof_latest_picks_freshest_known_row_per_ticker():
    """pooled 长表 → as_of 时每股 known_date≤as_of 的最新（end_date 最大）一行。"""
    from signals.common.factors import asof_latest

    pooled = pd.DataFrame(
        {
            "ts_code": ["A", "A", "B", "B", "C"],
            "end_date": pd.to_datetime(
                ["2020-03-31", "2020-06-30", "2020-03-31", "2020-06-30", "2020-06-30"]
            ),
            "known_date": pd.to_datetime(
                ["2020-04-25", "2020-08-20", "2020-04-28", "2020-09-20", "2020-10-30"]
            ),
            "value": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    got = asof_latest(pooled, pd.Timestamp("2020-08-31"))
    # A：两期都已知 → 取 end_date 更晚的 2.0；B：仅 Q1 已知 → 3.0；C：全未知 → 不出现
    assert dict(zip(got["ts_code"], got["value"])) == {"A": 2.0, "B": 3.0}


def test_pit_ttm_series_composes_chain_and_restricts_to_announced():
    """端到端：YTD 明细（含 01-01 伪行）→ 过滤/单季化/TTM → 只保留 ann_date≤as_of。"""
    income = _income_ytd(
        [
            ("2019-03-31", "2019-04-30", 10.0),  # Q1
            ("2019-06-30", "2019-08-31", 25.0),  # H1
            ("2019-09-30", "2019-10-31", 45.0),  # Q3
            ("2019-12-31", "2020-04-30", 70.0),  # 年报
            ("2020-01-01", "2020-05-01", 70.0),  # 伪行：须被滤掉，否则污染 2020 单季化
            ("2020-03-31", "2020-04-30", 12.0),  # Q1
            ("2020-06-30", "2020-08-31", 30.0),  # H1
            ("2020-09-30", "2020-10-31", 52.0),  # Q3
        ]
    )
    got = pit_ttm_series(income, pd.Timestamp("2020-09-01"))
    # TTM: 2019-12-31=70, 2020-03-31=72(15+20+25+12), 2020-06-30=75(20+25+12+18)
    # 2020-09-30 的 ann=10-31 > as_of，剔除
    assert list(got.index) == [
        pd.Timestamp("2019-12-31"),
        pd.Timestamp("2020-03-31"),
        pd.Timestamp("2020-06-30"),
    ]
    assert list(got.to_numpy()) == [70.0, 72.0, 75.0]


def test_extract_statement_field_pulls_field_and_keeps_missing_as_nan():
    """从 data JSONB 列按 statement_type 抽某字段 → end_date/ann_date/value 表；缺键留 NaN。"""
    facts = pd.DataFrame(
        {
            "statement_type": ["income", "income", "balance"],
            "end_date": pd.to_datetime(["2019-03-31", "2019-06-30", "2019-06-30"]),
            "ann_date": pd.to_datetime(["2019-04-30", "2019-08-31", "2019-08-31"]),
            "data": [
                {"revenue": 10.0, "net_profit_ytd": 5.0},
                {"revenue": 25.0},  # 缺 net_profit_ytd
                {"equity_parent": 100.0},
            ],
        }
    )
    rev = extract_statement_field(facts, "income", "revenue")
    assert list(rev["value"]) == [10.0, 25.0]  # 只取 income 两行
    assert list(rev["end_date"]) == [pd.Timestamp("2019-03-31"), pd.Timestamp("2019-06-30")]

    npf = extract_statement_field(facts, "income", "net_profit_ytd")
    assert npf["value"].iloc[0] == 5.0
    assert np.isnan(npf["value"].iloc[1])  # 第二季缺该字段 → NaN（保留季结构）


def test_extract_statement_field_preserves_provenance_in_source_order():
    """成对 provenance 列原值、原行序透传到字段明细，不做 bool 强转。"""
    facts = pd.DataFrame(
        {
            "statement_type": ["income", "balance", "income"],
            "end_date": pd.to_datetime(["2019-03-31", "2019-03-31", "2019-06-30"]),
            "ann_date": pd.to_datetime(["2019-04-30", "2019-04-30", "2019-08-31"]),
            "data": [{"revenue": 10.0}, {"equity_parent": 50.0}, {"revenue": 30.0}],
            "true_first_disclosure_verified": [np.bool_(True), True, False],
            "unverified_dependency_keys": [(), (), ("second",)],
        }
    )

    got = extract_statement_field(facts, "income", "revenue")

    assert list(got.columns) == [
        "end_date",
        "ann_date",
        "value",
        "true_first_disclosure_verified",
        "unverified_dependency_keys",
    ]
    assert got["true_first_disclosure_verified"].tolist() == [np.bool_(True), False]
    assert got["unverified_dependency_keys"].tolist() == [(), ("second",)]


def test_extract_statement_field_rejects_partial_provenance():
    """字段抽取入口同样拒绝只出现一列的 provenance 合同。"""
    facts = pd.DataFrame(
        {
            "statement_type": ["income"],
            "end_date": pd.to_datetime(["2019-03-31"]),
            "ann_date": pd.to_datetime(["2019-04-30"]),
            "data": [{"revenue": 10.0}],
            "true_first_disclosure_verified": [True],
        }
    )

    with pytest.raises(ValueError, match="provenance"):
        extract_statement_field(facts, "income", "revenue")


def _seven_quarter_facts():
    """构造 7 季（2018Q1..2019Q3）平坦 YTD 财务事实 + 期末 balance/dividend。

    单季平坦：营收 100/季、归母净利 40/季、CFO 30/季 → TTM 恒定（成长斜率=0）；
    归母权益 1000（latest balance），每股税前股利 5（2018 年报分红）。
    """
    q_ends = ["2018-03-31", "2018-06-30", "2018-09-30", "2018-12-31",
              "2019-03-31", "2019-06-30", "2019-09-30"]
    q_ann = ["2018-04-30", "2018-08-31", "2018-10-31", "2019-04-30",
             "2019-04-30", "2019-08-31", "2019-10-31"]
    ytd_mult = [1, 2, 3, 4, 1, 2, 3]  # 年内累计、跨年重置
    rows = []
    for qe, qa, m in zip(q_ends, q_ann, ytd_mult):
        rows.append((qe, qa, "income", {"revenue": 100.0 * m, "net_profit_parent_ytd": 40.0 * m}))
        rows.append((qe, qa, "cashflow_direct", {"cfo_net": 30.0 * m}))
    rows.append(("2019-09-30", "2019-10-31", "balance", {"equity_parent": 1000.0}))
    rows.append(("2018-12-31", "2019-04-30", "dividend", {"cash_dividend_ps_pre_tax": 5.0}))
    return pd.DataFrame(
        {
            "end_date": pd.to_datetime([r[0] for r in rows]),
            "ann_date": pd.to_datetime([r[1] for r in rows]),
            "statement_type": [r[2] for r in rows],
            "data": [r[3] for r in rows],
        }
    )


def test_stock_style_factors_composes_growth_and_value_vector():
    """单票风格因子向量：成长斜率 + EP/BP/CFP/DP（÷市值），PIT 对齐。"""
    facts = _seven_quarter_facts()
    got = stock_style_factors(
        facts, pd.Timestamp("2019-11-30"), shares=10.0, price=50.0, growth_n=4
    )
    # 平坦 → 成长斜率 0；mv = 10×50 = 500
    assert got["sal_g"] == pytest.approx(0.0)
    assert got["pro_g"] == pytest.approx(0.0)
    assert got["ep"] == pytest.approx(160.0 / 500.0)  # 归母净利 TTM 160 / mv
    assert got["bp"] == pytest.approx(1000.0 / 500.0)  # 归母权益 1000 / mv
    assert got["cfp"] == pytest.approx(120.0 / 500.0)  # CFO TTM 120 / mv
    assert got["dp"] == pytest.approx(5.0 * 10.0 / 500.0)  # 每股股利×股本 / mv = dps/price


def test_stock_style_factors_excludes_cfp_for_financials():
    """金融行业剔 CF/P（借 Gen3 规则）：cfp=NaN，其余照算。"""
    facts = _seven_quarter_facts()
    got = stock_style_factors(
        facts, pd.Timestamp("2019-11-30"), shares=10.0, price=50.0,
        is_financial=True, growth_n=4,
    )
    assert np.isnan(got["cfp"])
    assert got["bp"] == pytest.approx(1000.0 / 500.0)  # 其它价值因子不受影响


def test_rolling_growth_slope_survives_an_all_unknown_window():
    """整窗可知日缺失时应返回 NaT，而不是崩溃。

    NaT 转 datetime64[D] 再转 float 得到的是哨兵值 −9.22e18，不是 NaN；
    rolling max 取到它之后 pd.to_datetime(..., unit="D") 会在乘算中溢出。
    2026-08-07 回填退市股票、universe 变宽后由真实数据触发（整段缺披露日的
    财务事实），此前从未走到。函数本就允许 known_date 为 NaT——
    末尾 slope.where(known_date.notna()) 正是为此写的。
    """
    from signals.common.factors import rolling_growth_slope

    n = 4
    ttm = pd.Series(
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        index=pd.to_datetime(
            [
                "2020-03-31",
                "2020-06-30",
                "2020-09-30",
                "2020-12-31",
                "2021-03-31",
                "2021-06-30",
            ]
        ),
    )
    known = pd.Series([pd.NaT] * len(ttm), index=ttm.index)

    got = rolling_growth_slope(ttm, known, n=n)

    assert got["known_date"].isna().all()
    assert got["slope"].isna().all()


def test_rolling_growth_slope_refuses_a_window_with_an_unknown_disclosure_date():
    """窗内任一期披露日未知，斜率就不可算——文档语义如此。

    NaT 转 datetime64[D] 再转 float 得到的是哨兵 −9.22e18，而它是个合法浮点数，
    于是 rolling(min_periods=n) 把它算作有效观测、max() 又把它忽略，结果是
    "少了一期披露日却照样宣布斜率可知"——PIT 语义被破坏。同一个缺陷在
    Windows 上还会让 pd.to_datetime 溢出崩溃（2026-08-07 回填退市股后触发）。
    """
    from signals.common.factors import rolling_growth_slope

    n = 4
    index = pd.to_datetime(
        ["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"]
    )
    ttm = pd.Series([1.0, 2.0, 3.0, 4.0], index=index)
    known = pd.Series(
        [
            pd.Timestamp("2020-05-01"),
            pd.NaT,  # 这一期的披露日未知
            pd.Timestamp("2020-11-01"),
            pd.Timestamp("2021-02-01"),
        ],
        index=index,
    )

    got = rolling_growth_slope(ttm, known, n=n)

    assert pd.isna(got["known_date"].iloc[-1])
    assert pd.isna(got["slope"].iloc[-1])


def test_rolling_growth_slope_never_divides_by_a_near_zero_window_mean():
    """窗口均值近零时不得真的执行除法。

    np.where(cond, nan, slopes / means) 会先把整个除法算完再选，于是 means≈0
    的条目照样产生 inf 并置上浮点错误标志。默认 errstate 下只是 RuntimeWarning，
    但 pandas 的 cast_from_unit_vectorized 工作在 errstate(over="raise") 里，
    那个待决标志会在随后一次完全无辜的 to_datetime 上炸成 FloatingPointError
    ——2026-08-07 回填退市股票后由真实数据触发（TTM 序列跨零、量级 1e9）。
    这里用 errstate 把当时的条件复现出来。
    """
    from signals.common.factors import rolling_growth_slope

    n = 12
    index = pd.date_range("2015-03-31", periods=n + 2, freq="QE")
    ttm = pd.Series([1e9, -1e9] * ((n + 2) // 2), index=index)
    known = pd.Series(index + pd.Timedelta(days=30), index=index)

    with np.errstate(over="raise", divide="raise", invalid="raise"):
        got = rolling_growth_slope(ttm, known, n=n)

    assert len(got) == len(ttm)
    # 均值≈0 的窗口本就不该有相对斜率，结果应为 NaN 而非 inf。
    assert pd.isna(got["slope"].iloc[-1])
