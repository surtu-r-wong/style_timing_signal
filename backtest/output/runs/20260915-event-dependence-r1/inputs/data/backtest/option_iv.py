"""股指期权隐波序列构造（预登记 docs/plans/2026-09-03-index-option-iv-axis-prereg.md §2，冻结规则）。

源：public.option_daily（symbol/trade_date/settle/oi），底层指数收盘 stock_selector.index_daily。
全历史自算（不用厂商 implied_vol）：合约解析 → 近/次月（≥7 自然日）→ 平价隐含远期 → Black-76 ATM 隐波
→ IV30（方差按自然日插值到 30 天）/ 期限斜率 / 偏度（0.95F put − 1.05F call）/ 持仓 PCR。
纯函数不连库；`build_series(prefix)` 连库并缓存到 backtest/output/option_iv_<prefix>.csv。
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPOT_CODE = {"IO": "000300.SH", "MO": "000852.SH", "HO": "000016.SH"}
MIN_DAYS_TO_EXPIRY = 7
CM_DAYS = 30
SKEW_MONEYNESS = (0.95, 1.05)
_SYM = re.compile(r"^(?P<root>[A-Z]{2})(?P<ym>\d{4})-(?P<cp>[CP])-(?P<k>\d+(?:\.\d+)?)\.CFE$")


# ---------------- 纯函数 ----------------
def parse_symbol(symbol: str) -> tuple[str, str, str, float]:
    m = _SYM.match(symbol)
    if not m:
        raise ValueError(f"unrecognised option symbol: {symbol}")
    return m["root"], m["ym"], m["cp"], float(m["k"])


def third_friday(ym: str) -> pd.Timestamp:
    y, mth = 2000 + int(ym[:2]), int(ym[2:])
    d = pd.Timestamp(y, mth, 1)
    return d + pd.Timedelta(days=(4 - d.weekday()) % 7 + 14)


def expiry_date(ym: str, trading_days: pd.DatetimeIndex) -> pd.Timestamp:
    """合约月第三个周五；非交易日顺延到下一交易日（2602→02-24、2606→06-22 数据核实）。"""
    d = third_friday(ym)
    later = trading_days[trading_days >= d]
    return later[0] if len(later) else d


def black76(F: float, K: float, T: float, sigma: float, cp: str) -> float:
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if cp == "C":
        return F * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - F * norm.cdf(-d1)


def implied_vol(price: float, F: float, K: float, T: float, cp: str) -> float:
    intrinsic = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
    if not (np.isfinite(price) and price > intrinsic and T > 0):
        return float("nan")
    try:
        return float(brentq(lambda s: black76(F, K, T, s, cp) - price, 1e-4, 5.0, xtol=1e-8))
    except ValueError:
        return float("nan")


def implied_forward(strikes: np.ndarray, calls: np.ndarray, puts: np.ndarray, spot: float, n: int = 3) -> float:
    """平价 F = K + C − P（短期限忽略贴现），取距 spot 最近 n 个行权价的中位数。"""
    order = np.argsort(np.abs(strikes - spot))[:n]
    return float(np.median(strikes[order] + calls[order] - puts[order]))


def month_surface(strikes: np.ndarray, calls: np.ndarray, puts: np.ndarray, F: float, T: float) -> dict:
    """单合约月：ATM 隐波（call/put 均值）+ 偏度（0.95F put − 1.05F call，行权价线性插值，越界取端点）。"""
    k0 = strikes[np.abs(strikes - F).argmin()]
    ic = implied_vol(float(calls[strikes == k0][0]), F, k0, T, "C")
    ip = implied_vol(float(puts[strikes == k0][0]), F, k0, T, "P")
    atm = float(np.nanmean([ic, ip])) if (np.isfinite(ic) or np.isfinite(ip)) else float("nan")
    # 偏度只需 0.95F/1.05F 两点：只对 [0.90F, 1.10F] 带内的行权价反推（带外置 NaN，不参与插值）
    band = (strikes >= 0.90 * F) & (strikes <= 1.10 * F)
    iv_p = np.array([implied_vol(float(p), F, float(k), T, "P") if b else np.nan for k, p, b in zip(strikes, puts, band)])
    iv_c = np.array([implied_vol(float(c), F, float(k), T, "C") if b else np.nan for k, c, b in zip(strikes, calls, band)])
    def interp(ks, ivs, target):
        ok = np.isfinite(ivs)
        if ok.sum() < 2:
            return float("nan"), True
        ks2, iv2 = ks[ok], ivs[ok]
        clipped = not (ks2.min() <= target <= ks2.max())
        return float(np.interp(target, ks2, iv2)), clipped
    sp, cp_ = interp(strikes, iv_p, SKEW_MONEYNESS[0] * F)
    sc, cc_ = interp(strikes, iv_c, SKEW_MONEYNESS[1] * F)
    return {"atm": atm, "atm_call": ic, "atm_put": ip, "skew": sp - sc, "skew_clipped": bool(cp_ or cc_), "k0": float(k0)}


def interp_variance_to_cm(iv_near: float, t_near: float, iv_next: float, t_next: float, cm_days: int = CM_DAYS) -> float:
    """两个期限的 ATM 方差按自然日线性插值到 cm 天（VIX 式）；只有一个可用时直接用它。"""
    tc = cm_days / 365.0
    a, b = np.isfinite(iv_near), np.isfinite(iv_next)
    if a and b and t_next > t_near:
        w = (t_next - tc) / (t_next - t_near)
        w = min(max(w, 0.0), 1.0)
        var = w * iv_near ** 2 * t_near + (1 - w) * iv_next ** 2 * t_next
        return float(math.sqrt(max(var, 0.0) / tc))
    if a:
        return float(iv_near)
    if b:
        return float(iv_next)
    return float("nan")


def daily_metrics(day: pd.DataFrame, td: pd.Timestamp, spot: float, trading_days: pd.DatetimeIndex) -> dict:
    """单日：day 含 ym/cp/K/settle/oi。返回 iv30/term/skew/pcr 及诊断列。"""
    out = {"iv30": np.nan, "term": np.nan, "skew": np.nan, "pcr": np.nan, "near": None, "next": None,
           "atm_near": np.nan, "atm_next": np.nan, "F_near": np.nan, "skew_clipped": False, "n_rows": int(len(day))}
    put_oi, call_oi = day.loc[day.cp == "P", "oi"].sum(), day.loc[day.cp == "C", "oi"].sum()
    out["pcr"] = float(put_oi / call_oi) if call_oi > 0 else np.nan
    months = []
    for ym in sorted(day.ym.unique()):
        exp = expiry_date(ym, trading_days)
        days = (exp - td).days
        if days >= MIN_DAYS_TO_EXPIRY:
            months.append((ym, days / 365.0))
    if not months or not np.isfinite(spot):
        return out
    surfaces = []
    for ym, T in months[:2]:
        sub = day[day.ym == ym].pivot_table(index="K", columns="cp", values="settle", aggfunc="first").dropna()
        if len(sub) < 3:
            surfaces.append(None); continue
        ks = sub.index.to_numpy(dtype=float); c = sub["C"].to_numpy(dtype=float); p = sub["P"].to_numpy(dtype=float)
        F = implied_forward(ks, c, p, spot)
        if not np.isfinite(F) or F <= 0:
            surfaces.append(None); continue
        surfaces.append((ym, T, F, month_surface(ks, c, p, F, T)))
    if surfaces and surfaces[0] is not None:
        ym, T, F, s = surfaces[0]
        out.update({"near": ym, "atm_near": s["atm"], "F_near": F, "skew": s["skew"], "skew_clipped": s["skew_clipped"]})
        t_near, iv_near = T, s["atm"]
    else:
        t_near, iv_near = np.nan, np.nan
    if len(surfaces) > 1 and surfaces[1] is not None:
        ym2, T2, F2, s2 = surfaces[1]
        out.update({"next": ym2, "atm_next": s2["atm"]})
        t_next, iv_next = T2, s2["atm"]
    else:
        t_next, iv_next = np.nan, np.nan
    out["iv30"] = interp_variance_to_cm(iv_near, t_near, iv_next, t_next) if np.isfinite(t_near) or np.isfinite(t_next) else np.nan
    out["term"] = float(iv_next - iv_near) if np.isfinite(iv_near) and np.isfinite(iv_next) else np.nan
    return out


def build_metrics_frame(opt: pd.DataFrame, spot: pd.Series) -> pd.DataFrame:
    """opt: columns symbol/trade_date/settle/oi（单一品种）；spot: date→close。返回按日的指标表。"""
    df = opt.copy()
    parsed = df["symbol"].map(parse_symbol)
    df["ym"] = [p[1] for p in parsed]; df["cp"] = [p[2] for p in parsed]; df["K"] = [p[3] for p in parsed]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["settle"] = df["settle"].astype(float); df["oi"] = df["oi"].astype(float)
    trading_days = pd.DatetimeIndex(sorted(set(df["trade_date"]).union(set(pd.to_datetime(spot.index)))))
    rows = []
    for td, day in df.groupby("trade_date", sort=True):
        sp = float(spot.get(td, np.nan))
        rows.append({"date": td, "spot": sp, **daily_metrics(day, td, sp, trading_days)})
    return pd.DataFrame(rows).set_index("date").sort_index()


# ---------------- 连库 / 缓存 ----------------
def cache_path(prefix: str) -> Path:
    return ROOT / "backtest" / "output" / f"option_iv_{prefix}.csv"


def load_option_daily(prefix: str, db=None) -> pd.DataFrame:
    from signals.common.config import load_db_config
    from signals.style_basket.build import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        return pd.read_sql("SELECT symbol, trade_date, settle, oi FROM public.option_daily WHERE symbol LIKE %(p)s ORDER BY trade_date, symbol",
                           conn, params={"p": f"{prefix}%"})
    finally:
        conn.close()


def load_spot(prefix: str, db=None) -> pd.Series:
    from signals.common.config import load_db_config
    from signals.style_basket.build import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        df = pd.read_sql(f"SELECT trade_date, close FROM {db['schema']}.index_daily WHERE index_code = %(c)s ORDER BY trade_date",
                         conn, params={"c": SPOT_CODE[prefix]})
    finally:
        conn.close()
    s = df.set_index(pd.to_datetime(df["trade_date"]))["close"].astype(float)
    return s


def build_series(prefix: str, force: bool = False, db=None) -> pd.DataFrame:
    path = cache_path(prefix)
    if path.exists() and not force:
        return pd.read_csv(path, parse_dates=["date"]).set_index("date")
    frame = build_metrics_frame(load_option_daily(prefix, db), load_spot(prefix, db))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)
    return frame
