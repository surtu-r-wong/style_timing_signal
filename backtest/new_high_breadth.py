"""创新高参与度序列（预登记 docs/plans/2026-09-03-new-high-breadth-axis-prereg.md §2）。
NH_t = close_hfq_t ≥ 250 日滚动最高（窗须满）；NH20 = 过去 20 日内任一日新高；上市满 15 个月。
N1 = 全市场 NH20 占比；N2 = mean(share_500, share_1000) − share_300（PIT 成分）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.consensus_revision import membership  # noqa: E402

HIGH_WINDOW = 250
RECENT = 20
MIN_LISTED_DAYS = 315   # 15 个月（自然日）
MIN_DENOM = 500
INDEX_CODES = ("000300.SH", "000905.SH", "000852.SH")
CACHE = ROOT / "backtest" / "output" / "new_high_breadth_series.csv"


def new_high_flags(close: pd.DataFrame) -> pd.DataFrame:
    """close: date × ticker 后复权收盘。返回 NH20 布尔表（NaN → False），以及可用性掩码在 eligible()。"""
    roll_max = close.rolling(HIGH_WINDOW, min_periods=HIGH_WINDOW).max()
    nh = (close >= roll_max) & roll_max.notna()
    nh20 = nh.rolling(RECENT, min_periods=1).max().fillna(0).astype(bool)
    return nh20


def eligible(close: pd.DataFrame) -> pd.DataFrame:
    """当日可计入分母：250 日窗已满 且 上市满 15 个月（以首个非空 close 日起算自然日）。"""
    has = close.notna()
    first = has.idxmax().where(has.any())
    listed_days = pd.DataFrame({c: (close.index - first[c]).days for c in close.columns}, index=close.index)
    full_window = close.rolling(HIGH_WINDOW, min_periods=HIGH_WINDOW).max().notna()
    return full_window & (listed_days >= MIN_LISTED_DAYS)


def share(nh20: pd.DataFrame, elig: pd.DataFrame, member: pd.DataFrame | None = None) -> pd.Series:
    m = elig if member is None else (elig & member.reindex(index=elig.index, columns=elig.columns).fillna(False))
    denom = m.sum(axis=1)
    num = (nh20 & m).sum(axis=1)
    return (num / denom).where(denom >= (MIN_DENOM if member is None else 50))


def build_families(close: pd.DataFrame, ic: pd.DataFrame) -> pd.DataFrame:
    nh20 = new_high_flags(close); elig = eligible(close)
    out = pd.DataFrame(index=close.index)
    out["N1"] = share(nh20, elig)
    shares = {}
    for code in INDEX_CODES:
        mem = membership(ic, (code,), pd.DatetimeIndex(close.index))
        shares[code] = share(nh20, elig, mem)
        out[f"share_{code[:6]}"] = shares[code]
    out["N2"] = (shares["000905.SH"] + shares["000852.SH"]) / 2 - shares["000300.SH"]
    out["n_market"] = elig.sum(axis=1)
    out.index.name = "date"
    return out


def build_series(force: bool = False, db=None) -> pd.DataFrame:
    if CACHE.exists() and not force:
        return pd.read_csv(CACHE, parse_dates=["date"]).set_index("date")
    from signals.common.config import load_db_config
    from signals.style_basket.build import _connect
    db = db or load_db_config(); S = db["schema"]; conn = _connect(db)
    try:
        px = pd.read_sql(f"SELECT ts_code, trade_date, close_hfq FROM {S}.stock_daily_price WHERE close_hfq IS NOT NULL AND trade_date >= '2012-06-01'", conn)
        ic = pd.read_sql(f"SELECT index_code, ts_code, effective_date FROM {S}.index_constituent WHERE index_code IN ('000300.SH','000905.SH','000852.SH')", conn)
    finally:
        conn.close()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    close = px.pivot(index="trade_date", columns="ts_code", values="close_hfq").astype(float).sort_index()
    out = build_families(close, ic)
    CACHE.parent.mkdir(parents=True, exist_ok=True); out.to_csv(CACHE)
    return out
