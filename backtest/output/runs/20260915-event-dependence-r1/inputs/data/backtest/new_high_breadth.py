"""创新高参与度序列（预登记 docs/plans/2026-09-03-new-high-breadth-axis-prereg.md §2）。
NH_t = 当日实际收盘 ≥ 过去 250 行滚动最高（停牌日前向填充，累计观测 ≥250 日才计入）；NH20 = 过去 20 日内任一日新高；上市满 15 个月。
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


def _ffill_within_life(close: pd.DataFrame) -> pd.DataFrame:
    """停牌日（无行）前向填充，但退市/最后观测日之后保持 NaN。"""
    has = close.notna()
    last = has[::-1].idxmax()[::1]
    alive = pd.DataFrame({c: close.index <= last[c] for c in close.columns}, index=close.index)
    return close.ffill().where(alive)


def new_high_flags(close: pd.DataFrame) -> pd.DataFrame:
    """NH_t = 当日有实际收盘 且 收盘 ≥ 过去 250 行（停牌日前向填充后）的最高；NH20 = 过去 20 日内任一日 NH。
    2026-09-03 实现更正（冻结后披露）：原实现 rolling(min_periods=250) 会把窗内任一停牌日的股票整只剔除，
    2015~2016 千股停牌期误剔近三分之二样本；现以前向填充后的序列取滚动最高，资格另由 eligible() 按累计观测日数判定。
    """
    filled = _ffill_within_life(close)
    roll_max = filled.rolling(HIGH_WINDOW, min_periods=1).max()
    nh = close.notna() & (close >= roll_max)
    nh20 = nh.rolling(RECENT, min_periods=1).max().fillna(0).astype(bool)
    return nh20


def eligible(close: pd.DataFrame) -> pd.DataFrame:
    """当日可计入分母：累计实际观测日 ≥ 250 且 上市满 15 个月（自然日），且尚未退市（最后观测日之前）。"""
    has = close.notna()
    first = has.idxmax().where(has.any())
    listed_days = pd.DataFrame({c: (close.index - first[c]).days for c in close.columns}, index=close.index)
    obs = has.cumsum()
    last = has[::-1].idxmax()
    alive = pd.DataFrame({c: close.index <= last[c] for c in close.columns}, index=close.index)
    return (obs >= HIGH_WINDOW) & (listed_days >= MIN_LISTED_DAYS) & alive


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
        # 后复权 = close × adj_factor（与 close_hfq 逐日相等，比值恒 1.0；close_hfq 2025 年起不全、2026-06-02 后为空）
        px = pd.read_sql(f"SELECT ts_code, trade_date, close * adj_factor AS close_hfq FROM {S}.stock_daily_price WHERE close IS NOT NULL AND adj_factor IS NOT NULL AND trade_date >= '2012-06-01'", conn)
        ic = pd.read_sql(f"SELECT index_code, ts_code, effective_date FROM {S}.index_constituent WHERE index_code IN ('000300.SH','000905.SH','000852.SH')", conn)
    finally:
        conn.close()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    close = px.pivot(index="trade_date", columns="ts_code", values="close_hfq").astype(float).sort_index()
    out = build_families(close, ic)
    CACHE.parent.mkdir(parents=True, exist_ok=True); out.to_csv(CACHE)
    return out
