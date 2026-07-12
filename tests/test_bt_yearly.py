"""backtest/yearly.py 年度集中度分解——纯函数测试（全离线合成）。

锚定设计 docs/plans/2026-07-12-yearly-concentration-design.md §5。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.metrics import ann_return, sharpe  # noqa: E402
from backtest.yearly import concentration_summary, yearly_table  # noqa: E402

YEARLY_COLUMNS = ["days", "lf_ann", "lf_sharpe", "sym_ann", "sym_sharpe",
                  "bh_ann", "excess", "pct_long", "log_contrib_pct"]
CONC_KEYS = ["sharpe_full", "sharpe_ex_top1", "sharpe_ex_top2",
             "ex_top1_year", "ex_top2_years",
             "roll3y_min", "roll3y_min_date", "roll3y_median", "roll3y_neg_share"]


def _two_year_series():
    """2020 全年交替 [+0.002, 0] 、2021 全年交替 [−0.001, +0.003]。"""
    idx = pd.bdate_range("2020-01-01", "2021-12-31")
    r = pd.Series(0.0, index=idx)
    y0, y1 = idx.year == 2020, idx.year == 2021
    r[y0] = [0.002 if i % 2 == 0 else 0.0 for i in range(y0.sum())]
    r[y1] = [-0.001 if i % 2 == 0 else 0.003 for i in range(y1.sum())]
    return r


def test_yearly_table_slices_by_calendar_year_exact():
    ret_lf = _two_year_series()
    ret_sym = ret_lf * 2.0
    bh = ret_lf * -0.5
    pos_lf = pd.Series(1.0, index=ret_lf.index)

    tab = yearly_table(ret_lf, ret_sym, bh, pos_lf)

    assert list(tab.index) == [2020, 2021]
    assert list(tab.columns) == YEARLY_COLUMNS
    for y in (2020, 2021):
        sl = ret_lf[ret_lf.index.year == y]
        assert tab.loc[y, "days"] == len(sl)
        assert abs(tab.loc[y, "lf_ann"] - ann_return(sl)) < 1e-12
        assert abs(tab.loc[y, "lf_sharpe"] - sharpe(sl)) < 1e-12
        assert abs(tab.loc[y, "sym_ann"] - ann_return(sl * 2.0)) < 1e-12
        assert abs(tab.loc[y, "sym_sharpe"] - sharpe(sl * 2.0)) < 1e-12
        assert abs(tab.loc[y, "bh_ann"] - ann_return(sl * -0.5)) < 1e-12
        assert abs(tab.loc[y, "excess"]
                   - (ann_return(sl) - ann_return(sl * -0.5))) < 1e-12
    # 语义锚：ann = mean × 245
    sl20 = ret_lf[ret_lf.index.year == 2020]
    assert abs(tab.loc[2020, "lf_ann"] - sl20.mean() * 245) < 1e-12


def test_yearly_table_pct_long_uses_effective_tplus1_position():
    idx = pd.DatetimeIndex(["2020-12-29", "2020-12-30", "2020-12-31",
                            "2021-01-04", "2021-01-05"])
    zero = pd.Series(0.0, index=idx)
    pos_lf = pd.Series([0, 1, 1, 1, 0], index=idx, dtype=float)
    tab = yearly_table(zero, zero, zero, pos_lf)
    # pos_eff = shift(1) = [0,0,1,1,1] → 2020: 1/3 · 2021: 2/2
    assert abs(tab.loc[2020, "pct_long"] - 1 / 3) < 1e-12
    assert abs(tab.loc[2021, "pct_long"] - 1.0) < 1e-12


def test_yearly_table_log_contrib_sums_to_100():
    ret_lf = _two_year_series()
    zero = pd.Series(0.0, index=ret_lf.index)
    tab = yearly_table(ret_lf, zero, zero, zero)

    logc = np.log1p(ret_lf).groupby(ret_lf.index.year).sum()
    expect = logc / logc.sum() * 100
    assert abs(tab["log_contrib_pct"].sum() - 100.0) < 1e-9
    for y in (2020, 2021):
        assert abs(tab.loc[y, "log_contrib_pct"] - expect[y]) < 1e-9


def test_yearly_table_log_contrib_nan_when_total_nonpositive():
    idx = pd.bdate_range("2020-01-01", "2020-06-30")
    ret_lf = pd.Series(-0.001, index=idx)
    zero = pd.Series(0.0, index=idx)
    tab = yearly_table(ret_lf, zero, zero, zero)
    assert np.isnan(tab.loc[2020, "log_contrib_pct"])


def test_yearly_table_zero_std_year_sharpe_zero():
    idx = pd.bdate_range("2020-01-01", "2020-12-31")
    zero = pd.Series(0.0, index=idx)
    tab = yearly_table(zero, zero, zero, zero)
    assert tab.loc[2020, "lf_sharpe"] == 0.0
    assert tab.loc[2020, "lf_ann"] == 0.0


def test_concentration_ex_top_drops_by_yearly_log_contribution():
    idx = pd.bdate_range("2019-01-01", "2021-12-31")
    r = pd.Series(0.0, index=idx)
    y19, y20, y21 = (idx.year == y for y in (2019, 2020, 2021))
    r[y19] = [0.0005 if i % 2 == 0 else -0.0003 for i in range(y19.sum())]
    r[y20] = [0.004 if i % 2 == 0 else 0.001 for i in range(y20.sum())]   # 最大贡献
    r[y21] = [0.002 if i % 2 == 0 else -0.0005 for i in range(y21.sum())]  # 次大

    out = concentration_summary(r)
    assert abs(out["sharpe_full"] - sharpe(r)) < 1e-12
    assert out["ex_top1_year"] == 2020
    assert abs(out["sharpe_ex_top1"] - sharpe(r[~y20])) < 1e-12
    assert out["ex_top2_years"] == "2020,2021"
    assert abs(out["sharpe_ex_top2"] - sharpe(r[y19])) < 1e-12


def test_concentration_roll3y_full_window_only():
    # 980 日 > 735:交替正收益,无一个满窗为负
    idx = pd.bdate_range("2019-01-01", periods=980)
    r = pd.Series([0.0015 if i % 2 == 0 else 0.0005 for i in range(980)], index=idx)
    out = concentration_summary(r)
    assert np.isfinite(out["roll3y_min"]) and np.isfinite(out["roll3y_median"])
    assert out["roll3y_min"] <= out["roll3y_median"]
    assert out["roll3y_min"] > 0
    assert out["roll3y_neg_share"] == 0.0
    assert out["roll3y_min_date"] in idx


def test_concentration_roll3y_nan_when_sample_short():
    idx = pd.bdate_range("2020-01-01", periods=200)   # < 735
    r = pd.Series(0.001, index=idx)
    out = concentration_summary(r)
    assert np.isnan(out["roll3y_min"])
    assert np.isnan(out["roll3y_median"])
    assert np.isnan(out["roll3y_neg_share"])
    assert out["roll3y_min_date"] is None


def test_schema_contract():
    """列/键合同守卫——下游(CSV/仪表盘)若消费,改名必炸这里。"""
    idx = pd.bdate_range("2020-01-01", periods=10)
    s = pd.Series(0.001, index=idx)
    tab = yearly_table(s, s, s, s)
    assert list(tab.columns) == YEARLY_COLUMNS
    assert tab.index.name == "year"
    out = concentration_summary(s)
    assert list(out.keys()) == CONC_KEYS
