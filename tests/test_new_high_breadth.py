import numpy as np
import pandas as pd

from backtest import new_high_breadth as nhb
from backtest.new_high_breadth import build_families, eligible, new_high_flags, share


def _close(n=600, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2014-01-01", periods=n)
    return pd.DataFrame(np.exp(np.cumsum(rng.normal(0, 0.01, (n, 6)), axis=0)) * 10, index=idx, columns=[f"S{i}" for i in range(6)])


def test_new_high_flag_requires_full_window_and_holds_20_days():
    idx = pd.bdate_range("2014-01-01", periods=300)
    c = pd.DataFrame({"A": np.linspace(1, 2, 300)}, index=idx)      # 单调上升：每日新高
    nh20 = new_high_flags(c); e = eligible(c)
    assert nh20.iloc[:, 0].all()                                       # 单调上升：每日都是滚动最高（资格另判）
    assert not e.iloc[:249, 0].any() and e.iloc[249:, 0].all()         # 分母资格：累计观测 ≥ 250
    c2 = c.copy(); c2.iloc[260:, 0] = 1.5                            # 260 起回落：新高 20 日内仍记
    n2 = new_high_flags(c2)
    assert n2.iloc[260:279, 0].all() and not n2.iloc[280:, 0].any()


def test_suspension_gap_does_not_drop_stock_and_delisting_stops_ffill():
    idx = pd.bdate_range("2014-01-01", periods=400)
    c = pd.DataFrame({"A": np.linspace(1, 2, 400)}, index=idx)
    c.iloc[300:310, 0] = np.nan                      # 停牌 10 日
    nh20 = new_high_flags(c); e = eligible(c)
    assert e.iloc[310:, 0].all() and nh20.iloc[311:, 0].all()   # 复牌后继续计入且创新高
    assert not nh20.iloc[300:310, 0].any() or nh20.iloc[300:310, 0].all()  # 停牌日 NH20 只沿用此前 20 日状态
    d = c.copy(); d.iloc[350:, 0] = np.nan            # 退市
    assert not eligible(d).iloc[350:, 0].any()


def test_eligible_listing_age():
    idx = pd.bdate_range("2014-01-01", periods=400)
    c = pd.DataFrame({"old": 1.0, "new": np.nan}, index=idx); c.loc[idx[100]:, "new"] = 1.0
    e = eligible(c)
    assert e.iloc[249, 0] and not e.iloc[248, 0]
    # 新股：250 日窗在 100+249 满，但上市 315 自然日 ≈ 225 交易日 → 以两者较晚者为准
    first_ok = e["new"].idxmax()
    assert (first_ok - idx[100]).days >= 315 and e.loc[first_ok:, "new"].all()


def test_share_and_families_with_membership():
    c = _close(); nh20 = new_high_flags(c); e = eligible(c)
    old = nhb.MIN_DENOM; nhb.MIN_DENOM = 3
    try:
        s = share(nh20, e)
        assert s.dropna().between(0, 1).all()
        ic = pd.DataFrame({"index_code": ["000300.SH", "000300.SH", "000905.SH", "000905.SH", "000852.SH", "000852.SH"],
                           "ts_code": ["S0", "S1", "S2", "S3", "S4", "S5"], "effective_date": [c.index[0]] * 6})
        nhb_min = 50
        fam = build_families(c, ic)
        assert list(fam.columns[:1]) == ["N1"] and "N2" in fam and fam["N1"].dropna().between(0, 1).all()
    finally:
        nhb.MIN_DENOM = old
