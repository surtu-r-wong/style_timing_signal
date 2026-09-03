import numpy as np
import pandas as pd

from backtest.consensus_revision import (
    CLIP, DEADBAND, aggregate, build_families, daily_change, membership, rev20, trimmed_mean,
)


def test_trimmed_mean_drops_tails():
    r = pd.DataFrame([[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0]])
    assert abs(trimmed_mean(r, 0.10).iloc[0] - 0.0) < 1e-12          # 去掉两端各 1 个 → 极端值被截
    assert abs(trimmed_mean(r, 0.0).iloc[0] - 1.0) < 1e-12
    assert np.isnan(trimmed_mean(pd.DataFrame([[1.0, np.nan]]), 0.1).iloc[0])


def _days(n=60):
    return pd.bdate_range("2023-01-02", periods=n)


def test_daily_change_roll_day_is_zero_and_clipped():
    days = _days(6)
    fy1 = pd.DataFrame({"A": [100, 101, 200, 202, 202, 500]}, index=days, dtype=float)
    fy2 = pd.DataFrame({"A": [190, 200, 240, 240, 240, 240]}, index=days, dtype=float)
    c = daily_change(fy1, fy2, days)
    assert np.isnan(c.iloc[0, 0])
    assert abs(c.iloc[1, 0] - 0.01) < 1e-12
    assert c.iloc[2, 0] == 0.0            # 200 ≈ 前一日 FY2 200 → 年报滚动日，置 0
    assert abs(c.iloc[3, 0] - 0.01) < 1e-12
    assert c.iloc[4, 0] == 0.0
    assert c.iloc[5, 0] == CLIP           # +147% 裁剪到 +0.5


def test_daily_change_gap_not_forward_filled_beyond_limit():
    days = _days(30)
    vals = pd.Series(100.0, index=days); vals.iloc[5:25] = np.nan   # 20 个交易日无快照
    fy1 = pd.DataFrame({"A": vals}); fy2 = pd.DataFrame({"A": vals * 1.1})
    c = daily_change(fy1, fy2, days)
    assert (c.iloc[5:15, 0] == 0.0).all()      # 最后快照后第 1~10 个交易日前向填充（变化 0）
    assert c.iloc[15:26, 0].isna().all()        # 第 11 日起 NaN；恢复首日（位置 25）无前一有效值亦 NaN
    assert c.iloc[26, 0] == 0.0


def test_rev20_requires_min_valid_days():
    days = _days(40)
    c = pd.DataFrame({"A": 0.01, "B": np.nan}, index=days)
    r = rev20(c)
    assert abs(r.iloc[-1]["A"] - 0.20) < 1e-12
    assert r["B"].isna().all()
    assert r.iloc[8]["A"] != r.iloc[8]["A"]     # 第 9 日仅 9 个有效日 → NaN


def test_membership_pit_and_union():
    days = _days(10)
    ic = pd.DataFrame({"index_code": ["X", "X", "X", "Y"], "ts_code": ["A", "B", "C", "D"],
                       "effective_date": [days[0], days[0], days[5], days[0]]})
    m = membership(ic, ("X", "Y"), days)
    assert m.loc[days[2], ["A", "B", "D"]].all() and not m.loc[days[2], "C"]
    assert m.loc[days[7], "C"] and not m.loc[days[7], "A"]   # 新快照替换旧名单


def test_aggregate_breadth_and_magnitude():
    days = _days(3)
    rev = pd.DataFrame(np.array([[0.02, -0.02, 0.0, 0.03], [0.01, 0.01, 0.01, -0.01], [np.nan] * 4]), index=days, columns=list("ABCD"))
    member = pd.DataFrame(True, index=days, columns=list("ABCD"))
    from backtest import consensus_revision as cr
    cr_min = cr.MIN_MEMBERS; cr.MIN_MEMBERS = 2
    try:
        a = aggregate(rev, member)
    finally:
        cr.MIN_MEMBERS = cr_min
    assert abs(a.iloc[0]["breadth"] - (2 - 1) / 4) < 1e-12 and abs(a.iloc[0]["magnitude"] - 0.0075) < 1e-12  # 4 个值截尾 10% 不去点 → 均值
    assert abs(a.iloc[1]["breadth"] - (3 - 1) / 4) < 1e-12
    assert np.isnan(a.iloc[2]["breadth"])
    fam = build_families(a, a, a)
    assert list(fam.columns[:4]) == ["R1", "R2", "R3", "R4"] and (fam["R4"].dropna() == 0).all()
