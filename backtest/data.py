"""数据层：标的日收益（PG index_daily）+ 主力合约年化基差 carry（PG futures_daily）。

口径（三口径 500 / 1000 / blend）：
- 500 → 中证500 现货 000905.SH，期货 IC（2015-04+）
- 1000 → 中证1000 现货 000852.SH，期货 IM（2022-07+）
- blend → 两者日收益等权 / carry 等权（有一个用一个）

carry = 当期主力合约（oi 最大）年化基差率 = (spot-futures)/spot × 365/到期天数（正=贴水）。
无期货数据的日期（上市前 / futures_daily 止于 2026-04-29 之后）→ carry 缺失，引擎按 0 处理。
"""
import calendar
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from signals.common.config import load_db_config  # noqa: E402

_SPOT = {"500": "000905.SH", "1000": "000852.SH"}
_FUT = {"500": "IC", "1000": "IM"}


# ---------------- 纯函数 ----------------
def blend_returns(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a + b) / 2.0


def pick_main_contract(day_df: pd.DataFrame) -> str:
    return day_df.loc[day_df["oi"].idxmax(), "symbol"]


def _third_friday(year: int, month: int) -> date:
    fridays = [d for d in calendar.Calendar().itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]
    return fridays[2]


def _expiry_from_symbol(symbol: str) -> date:
    yymm = symbol.split(".")[0][2:]  # IC2606.CFE → 2606
    return _third_friday(2000 + int(yymm[:2]), int(yymm[2:]))


def annualized_basis(futures: float, spot: float, trade_date: date, symbol: str) -> float:
    days = max((_expiry_from_symbol(symbol) - trade_date).days, 1)
    return (spot - futures) / spot * 365.0 / days  # 正=贴水


# ---------------- PG 读取 ----------------
def _connect(db: dict):
    import psycopg2
    for _ in range(3):
        try:
            return psycopg2.connect(
                host=db["host"], port=db["port"], dbname=db["name"],
                user=db["user"], password=db["password"],
                connect_timeout=15, keepalives=1, keepalives_idle=30,
            )
        except psycopg2.OperationalError:
            time.sleep(3)
    raise RuntimeError("PG unreachable after retries")


def load_spot_close(kou_jing: str, start=None, db=None) -> pd.Series:
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT trade_date, close FROM {db['schema']}.index_daily
                    WHERE index_code=%s AND (%s::date IS NULL OR trade_date>=%s::date)
                    ORDER BY trade_date""",
                (_SPOT[kou_jing], start, start),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.Series({pd.Timestamp(d): float(c) for d, c in rows}).sort_index()


def load_underlying_returns(kou_jing: str, start=None, db=None) -> pd.Series:
    db = db or load_db_config()
    if kou_jing == "blend":
        both = pd.concat([load_underlying_returns("500", start, db),
                          load_underlying_returns("1000", start, db)], axis=1).dropna()
        return blend_returns(both.iloc[:, 0], both.iloc[:, 1])
    return load_spot_close(kou_jing, start, db).pct_change().dropna()


def load_carry(kou_jing: str, start=None, db=None) -> pd.Series:
    """年化基差率序列（正=贴水）。缺期货的日期不在序列里。"""
    db = db or load_db_config()
    if kou_jing == "blend":
        both = pd.concat([load_carry("500", start, db),
                          load_carry("1000", start, db)], axis=1)
        return both.mean(axis=1).dropna()  # 有一个用一个
    spot = load_spot_close(kou_jing, start, db)
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT trade_date, symbol, close, oi FROM public.futures_daily
                   WHERE symbol LIKE %s AND (%s::date IS NULL OR trade_date>=%s::date)""",
                (_FUT[kou_jing] + "%", start, start),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    fdf = pd.DataFrame(rows, columns=["trade_date", "symbol", "close", "oi"]).dropna(subset=["oi", "close"])
    out = {}
    for td, g in fdf.groupby("trade_date"):
        ts = pd.Timestamp(td)
        if ts not in spot.index:
            continue
        sym = pick_main_contract(g)
        fut = float(g.loc[g["symbol"] == sym, "close"].iloc[0])
        out[ts] = annualized_basis(fut, float(spot.loc[ts]), td, sym)
    return pd.Series(out).sort_index()
