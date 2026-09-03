"""分析师一致预期修正序列构造（预登记 docs/plans/2026-09-03-consensus-revision-axis-prereg.md §2，冻结规则）。

个股：FY1 净利润日变化 c_t=(FY1_t−FY1_{t−1})/|FY1_{t−1}| 裁剪 ±0.5，年报滚动日（|FY1_t−FY2_{t−1}|/|FY2_{t−1}|<1% 且 FY1 变化）置 0，
断档 >10 交易日不前向填充；rev20 = 20 日和（有效日 <10 记 NaN）。
宇宙：index_constituent 最新快照（effective_date ≤ t）；R1 广度=(#rev20>+0.5% − #rev20<−0.5%)/#覆盖；R2 幅度=10% 截尾均值（中位数 86% 日子为 0，退化）；
R3=R1.diff(20)；R4=R1(000300)−R1(000852)。缓存 backtest/output/consensus_revision_series.csv。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CLIP = 0.5
ROLL_TOL = 0.01
DEADBAND = 0.005
TRIM = 0.10
WINDOW = 20
MIN_VALID = 10
MAX_GAP_DAYS = 10
MIN_MEMBERS = 200
UNIVERSES = {"U": ("000905.SH", "000852.SH"), "300": ("000300.SH",), "1000": ("000852.SH",)}
CACHE = ROOT / "backtest" / "output" / "consensus_revision_series.csv"


# ---------------- 纯函数 ----------------
def daily_change(fy1: pd.DataFrame, fy2: pd.DataFrame, trading_days: pd.DatetimeIndex) -> pd.DataFrame:
    """fy1/fy2: date × ticker 宽表（快照日）。返回按交易日索引的日变化表 c（滚动日 0、断档 NaN、裁剪 ±CLIP）。"""
    f1 = fy1.reindex(trading_days)
    f2 = fy2.reindex(trading_days)
    # 断档：上一有效快照距今 > MAX_GAP_DAYS 个交易日 → 当日无效
    has = f1.notna()
    pos = pd.Series(np.arange(len(trading_days)), index=trading_days)
    last_valid = has.apply(lambda col: pos.where(col).ffill())
    gap = pos.to_numpy()[:, None] - last_valid.to_numpy()
    ff1 = f1.ffill().where(gap <= MAX_GAP_DAYS)
    ff2 = f2.ffill().where(gap <= MAX_GAP_DAYS)
    prev1, prev2 = ff1.shift(1), ff2.shift(1)
    c = (ff1 - prev1) / prev1.abs()
    c = c.clip(-CLIP, CLIP)
    roll = prev2.notna() & (prev2 != 0) & ((ff1 - prev2).abs() / prev2.abs() < ROLL_TOL) & (ff1 != prev1)
    c = c.mask(roll, 0.0)
    return c


def rev20(c: pd.DataFrame, window: int = WINDOW, min_valid: int = MIN_VALID) -> pd.DataFrame:
    valid = c.notna().rolling(window, min_periods=1).sum()
    s = c.fillna(0.0).rolling(window, min_periods=1).sum()
    return s.where(valid >= min_valid)


def membership(ic: pd.DataFrame, codes: tuple[str, ...], trading_days: pd.DatetimeIndex) -> pd.DataFrame:
    """PIT 成员矩阵（date × ticker，bool）：每个交易日取 effective_date ≤ t 的最新快照，多指数取并集。"""
    ic = ic[ic["index_code"].isin(codes)].copy()
    ic["effective_date"] = pd.to_datetime(ic["effective_date"])
    tickers = sorted(ic["ts_code"].unique())
    out = pd.DataFrame(False, index=trading_days, columns=tickers)
    for code in codes:
        sub = ic[ic["index_code"] == code]
        snaps = sorted(sub["effective_date"].unique())
        for i, d in enumerate(snaps):
            members = sub.loc[sub["effective_date"] == d, "ts_code"].to_numpy()
            end = snaps[i + 1] if i + 1 < len(snaps) else trading_days[-1] + pd.Timedelta(days=1)
            rows = (trading_days >= d) & (trading_days < end)
            if rows.any():
                out.loc[rows, members] = True
    return out


def trimmed_mean(r: pd.DataFrame, trim: float) -> pd.Series:
    """逐行 10% 截尾均值（两侧各去 trim 比例后取均值）；行内有效值不足 3 个记 NaN。"""
    def _row(x: np.ndarray) -> float:
        x = x[np.isfinite(x)]
        if len(x) < 3:
            return np.nan
        x = np.sort(x); k = int(np.floor(len(x) * trim))
        return float(x[k:len(x) - k].mean()) if len(x) - 2 * k > 0 else np.nan
    return pd.Series([_row(row) for row in r.to_numpy(dtype=float)], index=r.index)


def aggregate(rev: pd.DataFrame, member: pd.DataFrame) -> pd.DataFrame:
    """按日聚合：breadth / magnitude / n_covered（覆盖成员 < MIN_MEMBERS → NaN）。"""
    cols = rev.columns.intersection(member.columns)
    r = rev[cols].where(member[cols])
    n = r.notna().sum(axis=1)
    up = (r > DEADBAND).sum(axis=1); dn = (r < -DEADBAND).sum(axis=1)
    out = pd.DataFrame({"breadth": (up - dn) / n, "magnitude": trimmed_mean(r, TRIM), "n_covered": n}, index=rev.index)
    return out.where(n >= MIN_MEMBERS)


def build_families(u: pd.DataFrame, b300: pd.DataFrame, b1000: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"R1": u["breadth"], "R2": u["magnitude"], "R3": u["breadth"].diff(WINDOW),
                         "R4": b300["breadth"] - b1000["breadth"], "n_U": u["n_covered"], "n_300": b300["n_covered"], "n_1000": b1000["n_covered"]})


# ---------------- 连库 / 缓存 ----------------
def _conn(db=None):
    from signals.common.config import load_db_config
    from signals.style_basket.build import _connect
    return _connect(db or load_db_config()), (db or load_db_config())["schema"]


def load_inputs(db=None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    conn, S = _conn(db)
    try:
        fy = pd.read_sql(f"SELECT ts_code, report_date, forecast_horizon AS h, net_profit AS np FROM {S}.stock_consensus_fy WHERE forecast_horizon IN (1,2)", conn)
        ic = pd.read_sql(f"SELECT index_code, ts_code, effective_date FROM {S}.index_constituent WHERE index_code IN ('000300.SH','000905.SH','000852.SH')", conn)
        cal = pd.read_sql(f"SELECT trade_date FROM {S}.index_daily WHERE index_code='000300.SH' AND trade_date >= '2019-09-01' ORDER BY trade_date", conn)
    finally:
        conn.close()
    fy["report_date"] = pd.to_datetime(fy["report_date"]); fy["np"] = fy["np"].astype(float)
    fy1 = fy[fy.h == 1].pivot(index="report_date", columns="ts_code", values="np").sort_index()
    fy2 = fy[fy.h == 2].pivot(index="report_date", columns="ts_code", values="np").sort_index()
    days = pd.DatetimeIndex(pd.to_datetime(cal["trade_date"]))
    days = days[days >= fy1.index.min()]
    return fy1, fy2, ic, days


def build_series(force: bool = False, db=None) -> pd.DataFrame:
    if CACHE.exists() and not force:
        return pd.read_csv(CACHE, parse_dates=["date"]).set_index("date")
    fy1, fy2, ic, days = load_inputs(db)
    c = daily_change(fy1, fy2, days)
    rv = rev20(c)
    agg = {name: aggregate(rv, membership(ic, codes, days)) for name, codes in UNIVERSES.items()}
    fam = build_families(agg["U"], agg["300"], agg["1000"])
    fam.index.name = "date"
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    fam.to_csv(CACHE)
    return fam
