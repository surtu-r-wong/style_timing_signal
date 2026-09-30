"""期指基差期限结构序列（预登记 docs/plans/2026-09-03-basis-term-structure-axis-prereg.md §2）。

每日每合约年化基差率（与 backtest.data.annualized_basis 同口径），剔除距到期 <7 天合约，
T1 = b 对到期时间的 OLS 斜率，T2 = 最远 − 最近，T3 = T1.diff(20)；500/1000 各算后取可用腿均值。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import _expiry_from_symbol, annualized_basis  # noqa: E402

MIN_DAYS = 7
MIN_CONTRACTS = 3
FUT = {"500": "IC", "1000": "IM"}
SPOT = {"500": "000905.SH", "1000": "000852.SH"}
CACHE = ROOT / "backtest" / "output" / "basis_term_series.csv"
# 横截面复制（预登记 docs/plans/2026-09-09-basis-term-replication-prereg.md）：IF/IH 单品种序列，构造逐字同上
REPL = {"IF": "300", "IH": "50", "IC": "500", "IM": "1000"}   # 品种 → data._SPOT 口径键
REPL_CACHE = ROOT / "backtest" / "output" / "basis_term_replication_series.csv"


def day_term_metrics(rows: pd.DataFrame, spot: float, td) -> dict:
    """rows: symbol/close（单日单品种）。返回 slope / far_near / n_used。"""
    b, t = [], []
    for sym, close in zip(rows["symbol"], rows["close"]):
        days = (_expiry_from_symbol(sym) - td).days
        if days < MIN_DAYS:
            continue
        b.append(annualized_basis(float(close), spot, td, sym)); t.append(days / 365.0)
    if len(b) < MIN_CONTRACTS:
        return {"slope": np.nan, "far_near": np.nan, "n_used": len(b)}
    b, t = np.array(b), np.array(t)
    slope = float(np.polyfit(t, b, 1)[0])
    order = np.argsort(t)
    return {"slope": slope, "far_near": float(b[order[-1]] - b[order[0]]), "n_used": int(len(b))}


def build_index_series(fut: pd.DataFrame, spot: pd.Series) -> pd.DataFrame:
    out = {}
    for td, g in fut.groupby("trade_date"):
        ts = pd.Timestamp(td)
        if ts not in spot.index:
            continue
        out[ts] = day_term_metrics(g, float(spot.loc[ts]), td)
    return pd.DataFrame(out).T.sort_index().astype(float)


def combine(legs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """可用腿均值（信号量合并，不补 0）。"""
    slope = pd.concat([d["slope"] for d in legs.values()], axis=1).mean(axis=1, skipna=True)
    far = pd.concat([d["far_near"] for d in legs.values()], axis=1).mean(axis=1, skipna=True)
    out = pd.DataFrame({"T1": slope, "T2": far})
    out["T3"] = out["T1"].diff(20)
    for name, d in legs.items():
        out[f"slope_{name}"] = d["slope"]; out[f"n_used_{name}"] = d["n_used"]
    out.index.name = "date"
    return out


def build_series(force: bool = False, db=None) -> pd.DataFrame:
    if CACHE.exists() and not force:
        return pd.read_csv(CACHE, parse_dates=["date"]).set_index("date")
    from backtest.data import _connect, load_spot_close
    from signals.common.config import load_db_config
    db = db or load_db_config()
    legs = {}
    for kj, pre in FUT.items():
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT trade_date, symbol, close FROM public.futures_daily WHERE symbol LIKE %s AND close IS NOT NULL", (pre + "%",))
                fut = pd.DataFrame(cur.fetchall(), columns=["trade_date", "symbol", "close"])
        finally:
            conn.close()
        legs[kj] = build_index_series(fut, load_spot_close(kj, None, db))
    out = combine(legs)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(CACHE)
    return out


def _load_fut(prefix: str, db) -> pd.DataFrame:
    from backtest.data import _connect
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trade_date, symbol, close FROM public.futures_daily WHERE symbol LIKE %s AND close IS NOT NULL", (prefix + "%",))
            return pd.DataFrame(cur.fetchall(), columns=["trade_date", "symbol", "close"])
    finally:
        conn.close()


def build_replication_series(force: bool = False, db=None) -> pd.DataFrame:
    """单品种期限结构（不合并腿）：列 T1_<品种> / T2_<品种> / n_used_<品种>，IF/IH/IC/IM 各自对自身现货。

    与 build_series 的唯一差别是不做 500/1000 腿均值——复制对象是单品种，原线的合并
    规则在这里没有对应物。IC/IM 单腿列只用于独立性预检（与原线合并序列对照）。
    """
    if REPL_CACHE.exists() and not force:
        return pd.read_csv(REPL_CACHE, parse_dates=["date"]).set_index("date")
    from backtest.data import load_spot_close
    from signals.common.config import load_db_config
    db = db or load_db_config()
    cols = {}
    for pre, kj in REPL.items():
        d = build_index_series(_load_fut(pre, db), load_spot_close(kj, None, db))
        cols[f"T1_{pre}"] = d["slope"]; cols[f"T2_{pre}"] = d["far_near"]; cols[f"n_used_{pre}"] = d["n_used"]
    out = pd.DataFrame(cols).sort_index(); out.index.name = "date"
    REPL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(REPL_CACHE)
    return out
