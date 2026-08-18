"""纯风格编制管线的纯函数层（`backtest/pure_style_builder`）—— 不连库。

覆盖 2026-08-18 三处修复里可离线验证的两处：
① PIT 可知日的法定披露截止日映射；② `avg_mv_1y` 的日频口径（股本前向填充）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import (  # noqa: E402
    _avg_mv_1y_daily,
    _cap_weights,
    _statutory_deadline,
)


# ---------------------------------------------------------------- 法定披露截止日
def test_statutory_deadline_maps_all_four_report_periods():
    """Q1→当年4/30、半年报→当年8/31、Q3→当年10/31、年报→**次年**4/30。"""
    got = _statutory_deadline(pd.Series(pd.to_datetime(
        ["2018-03-31", "2018-06-30", "2018-09-30", "2018-12-31"])))
    assert list(got) == list(pd.to_datetime(
        ["2018-04-30", "2018-08-31", "2018-10-31", "2019-04-30"]))


def test_statutory_deadline_returns_nat_for_pseudo_rows():
    """CSMAR 的 `01-01` 伪行不是自然季末 → NaT（下游按 is_quarter_end 剔除）。"""
    got = _statutory_deadline(pd.Series(pd.to_datetime(["2016-01-01", "2016-03-31"])))
    assert pd.isna(got.iloc[0]) and got.iloc[1] == pd.Timestamp("2016-04-30")


def test_statutory_deadline_is_an_upper_bound_on_true_disclosure():
    """截止日 ≥ 报告期末，且年报跨年 —— 这是"保守而非前视"论证的算术前提。"""
    ends = pd.Series(pd.to_datetime(["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"]))
    dl = _statutory_deadline(ends)
    assert (dl > ends).all()


# ---------------------------------------------------------------- 日频日均总市值
def _px(rows):
    return [(c, pd.Timestamp(d), v) for c, d, v in rows]


def test_avg_mv_1y_daily_forward_fills_share_capital():
    """股本按 `effective_date` 前向填充：换股本前后各按当时的在外股本计市值。"""
    px = _px([("A.SH", "2024-01-02", 10.0), ("A.SH", "2024-06-03", 10.0)])
    sh = [("A.SH", pd.Timestamp("2023-01-01"), 100.0),
          ("A.SH", pd.Timestamp("2024-03-01"), 200.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == (10.0 * 100 + 10.0 * 200) / 2      # 1000 与 2000 的均值


def test_avg_mv_1y_daily_drops_days_before_first_share_record():
    """首条股本记录之前的交易日无法定价 → 不计入均值（而非当 0 处理）。"""
    px = _px([("A.SH", "2023-01-02", 10.0), ("A.SH", "2024-01-02", 20.0)])
    sh = [("A.SH", pd.Timestamp("2023-06-01"), 50.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == 20.0 * 50                          # 只剩 2024-01-02 那天


def test_avg_mv_1y_daily_keeps_codes_independent():
    """`merge_asof(by=ts_code)` 不得让 A 的股本泄漏到 B。"""
    px = _px([("A.SH", "2024-01-02", 10.0), ("B.SZ", "2024-01-02", 10.0)])
    sh = [("A.SH", pd.Timestamp("2023-01-01"), 100.0),
          ("B.SZ", pd.Timestamp("2023-01-01"), 300.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == 1000.0 and got["B.SZ"] == 3000.0


def test_avg_mv_1y_daily_empty_inputs_are_safe():
    """无价格或无股本 → 空序列，由调用方回退到月末口径（不得抛）。"""
    assert _avg_mv_1y_daily([], []).empty
    assert _avg_mv_1y_daily(_px([("A.SH", "2024-01-02", 10.0)]), []).empty


# ---------------------------------------------------------------- 双帽权重
def test_cap_weights_enforces_single_and_top5_caps():
    """单样本 ≤15%、前五合计 ≤60%，且归一化到 1。"""
    w = _cap_weights(pd.Series([100.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]))
    assert np.isclose(w.sum(), 1.0)
    assert w.max() <= 0.15 + 1e-9
    assert w.nlargest(5).sum() <= 0.60 + 1e-9
