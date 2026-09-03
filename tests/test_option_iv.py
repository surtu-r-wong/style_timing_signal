import math
import numpy as np
import pandas as pd
import pytest

from backtest.option_iv import (
    black76, build_metrics_frame, daily_metrics, expiry_date, implied_forward, implied_vol,
    interp_variance_to_cm, month_surface, parse_symbol, third_friday,
)


def test_parse_symbol():
    assert parse_symbol("IO2609-C-4700.CFE") == ("IO", "2609", "C", 4700.0)
    assert parse_symbol("MO2703-P-6200.CFE") == ("MO", "2703", "P", 6200.0)
    with pytest.raises(ValueError):
        parse_symbol("IF2609.CFE")


def test_expiry_third_friday_and_holiday_shift():
    assert third_friday("2609") == pd.Timestamp("2026-09-18")
    days = pd.bdate_range("2026-06-01", "2026-06-30")
    days = days[days != pd.Timestamp("2026-06-19")]  # 端午假日
    assert expiry_date("2606", days) == pd.Timestamp("2026-06-22")
    assert expiry_date("2607", pd.bdate_range("2026-07-01", "2026-07-31")) == pd.Timestamp("2026-07-17")


def test_black76_round_trip_and_parity():
    F, K, T, sig = 4000.0, 4050.0, 0.08, 0.22
    c, p = black76(F, K, T, sig, "C"), black76(F, K, T, sig, "P")
    assert abs((c - p) - (F - K)) < 1e-9  # 平价（无贴现）
    assert abs(implied_vol(c, F, K, T, "C") - sig) < 1e-6
    assert abs(implied_vol(p, F, K, T, "P") - sig) < 1e-6
    assert math.isnan(implied_vol(0.0, F, K, T, "C"))


def _synthetic_month(F, T, sig_atm, skew_slope=0.0, strikes=None):
    strikes = np.array(strikes if strikes is not None else np.arange(0.85, 1.16, 0.025) * F)
    ivs = sig_atm + skew_slope * (F - strikes) / F  # 线性微笑：低行权价隐波更高
    calls = np.array([black76(F, k, T, s, "C") for k, s in zip(strikes, ivs)])
    puts = np.array([black76(F, k, T, s, "P") for k, s in zip(strikes, ivs)])
    return strikes, calls, puts, ivs


def test_implied_forward_and_surface_recover_inputs():
    F, T = 3987.0, 0.06
    ks, c, p, ivs = _synthetic_month(F, T, 0.20, skew_slope=0.4)
    assert abs(implied_forward(ks, c, p, spot=4010.0) - F) < 1e-6
    s = month_surface(ks, c, p, F, T)
    assert abs(s["atm"] - ivs[np.abs(ks - F).argmin()]) < 1e-5
    # 0.95F put 隐波 > 1.05F call 隐波 → 偏度为正，量级 = 斜率 × 0.10
    assert abs(s["skew"] - 0.4 * 0.10) < 2e-3
    assert not s["skew_clipped"]


def test_interp_variance_to_cm():
    assert abs(interp_variance_to_cm(0.20, 10 / 365, 0.20, 40 / 365) - 0.20) < 1e-9
    v = interp_variance_to_cm(0.30, 10 / 365, 0.20, 40 / 365)
    assert 0.20 < v < 0.30
    assert interp_variance_to_cm(0.25, 10 / 365, float("nan"), 40 / 365) == 0.25
    assert math.isnan(interp_variance_to_cm(float("nan"), 0.1, float("nan"), 0.2))


def test_daily_metrics_and_frame_on_synthetic_chain():
    td = pd.Timestamp("2026-09-01"); spot = 4000.0
    trading_days = pd.bdate_range("2026-08-01", "2026-12-31")
    rows = []
    for ym, F, sig in (("2609", 3995.0, 0.24), ("2610", 3990.0, 0.20), ("2612", 3985.0, 0.19)):
        ks, c, p, _ = _synthetic_month(F, (expiry_date(ym, trading_days) - td).days / 365, sig, 0.3)
        for k, cc, pp in zip(ks, c, p):
            rows.append({"symbol": f"IO{ym}-C-{int(round(k))}.CFE", "trade_date": td, "settle": cc, "oi": 100.0})
            rows.append({"symbol": f"IO{ym}-P-{int(round(k))}.CFE", "trade_date": td, "settle": pp, "oi": 150.0})
    opt = pd.DataFrame(rows)
    frame = build_metrics_frame(opt, pd.Series({td: spot}))
    r = frame.iloc[0]
    assert r["near"] == "2609" and r["next"] == "2610"       # 09-18 到期，距 09-01 ≥ 7 天 → 近月仍是 2609
    assert 0.19 < r["iv30"] < 0.25 and r["term"] < 0        # 近月隐波高于次月 → 倒挂
    assert r["skew"] > 0 and abs(r["pcr"] - 1.5) < 1e-9
    # 距到期 < 7 天 → 近月滚到 2610
    late = build_metrics_frame(opt.assign(trade_date=pd.Timestamp("2026-09-15")), pd.Series({pd.Timestamp("2026-09-15"): spot}))
    assert late.iloc[0]["near"] == "2610"
