from datetime import date
import numpy as np
import pandas as pd

from backtest.basis_term import combine, day_term_metrics
from backtest.data import _expiry_from_symbol


def test_day_term_metrics_slope_and_exclusion():
    td = date(2026, 8, 25); spot = 7700.0
    syms = ["IC2609.CFE", "IC2610.CFE", "IC2612.CFE", "IC2703.CFE"]
    # 构造：年化贴水率随到期线性增加 b = 0.05 + 0.10·T
    rows = []
    for s in syms:
        T = (_expiry_from_symbol(s) - td).days / 365.0; b = 0.05 + 0.10 * T
        rows.append({"symbol": s, "close": spot * (1 - b * T)})
    m = day_term_metrics(pd.DataFrame(rows), spot, td)
    assert m["n_used"] == 4 and abs(m["slope"] - 0.10) < 1e-6
    assert m["far_near"] > 0
    # 距到期 <7 天的合约剔除：09-18 到期，09-14 只剩 3 个 → 仍可算；09-15 起近月剔除后只剩 3
    m2 = day_term_metrics(pd.DataFrame(rows), spot, date(2026, 9, 14))
    assert m2["n_used"] == 3
    m3 = day_term_metrics(pd.DataFrame(rows[:2]), spot, td)
    assert np.isnan(m3["slope"]) and m3["n_used"] == 2


def test_combine_uses_available_legs_without_zero_fill():
    idx = pd.bdate_range("2022-06-01", periods=60)
    a = pd.DataFrame({"slope": 0.2, "far_near": 0.1, "n_used": 4}, index=idx)
    b = pd.DataFrame({"slope": 0.4, "far_near": 0.3, "n_used": 4}, index=idx[30:])
    out = combine({"500": a, "1000": b})
    assert abs(out["T1"].iloc[0] - 0.2) < 1e-12          # 只有 IC 时不补 0
    assert abs(out["T1"].iloc[-1] - 0.3) < 1e-12         # 两腿平均
    assert out["T3"].iloc[:20].isna().all() and abs(out["T3"].iloc[-1]) < 1e-12
