"""方向 C · B1 管线：财务池 → 月末截面打分 → 分桶 → 自建风格篮子价差。

三阶段（各自缓存，可断点重跑）：
  pool    — 全市场财务事实 → pooled 长表（TTM/斜率/事件，含 known_date PIT 可知日）
  scores  — 逐月末截面：asof 提取 + 市值 → 六因子 → 打分（全市场）→ 排名
  baskets — 按 U0-U4 universe 分桶（Top/Bottom pct）→ 日度等权两腿 + 价差序列

产出：output/style_basket/spread_<U>.csv（提交）；cache/ 下中间物（gitignored）。
纯计算在 signals.common.factors 与 .scoring（全部 TDD）；本文件是 IO 编排。
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.common.factors import (  # noqa: E402
    asof_latest,
    extract_statement_field,
    pit_ttm_with_known,
    rolling_growth_slope,
)
from signals.common.financial_reader import _connect, fetch_financial_facts  # noqa: E402
from signals.style_basket.scoring import (  # noqa: E402
    UNIVERSE_BANDS,
    basket_spread_returns,
    select_baskets,
    style_scores,
    universe_mask,
)

OUT_DIR = ROOT / "output" / "style_basket"
CACHE_DIR = OUT_DIR / "cache"

FIN_STATEMENT_TYPES = ["income", "cashflow_direct", "cashflow", "balance", "dividend"]
# 借 Gen3 规则剔金融 CF/P（金融企业现金流量表无经济含义）；地产现金流有意义，不剔
FINANCIAL_INDUSTRIES = {"银行", "非银行金融", "综合金融"}
MIN_LISTED_DAYS = 180  # U0 轻过滤：上市 <6 个月剔除


# ---------------------------------------------------------------- pool（纯）
def ticker_financial_rows(facts: pd.DataFrame, growth_n: int = 12) -> dict[str, pd.DataFrame]:
    """单票财务事实 → pooled 行（ttm / slope / event 三类，均含 known_date）。

    - rev/np：income YTD → pit_ttm_with_known（跨 CSMAR/Wind，YTD 语义连续）
    - cfo：CSMAR cfo_net YTD 链 + Wind cashflow.cfo_ttm 直接-TTM 行拼接
      （Wind 行无差分依赖 → known=自身 ann；与 CSMAR 链重叠期以 CSMAR 为准）
    - slope：rev/np 的 TTM 网格 rolling 斜率（窗内可知日最大值为 known）
    - event：equity_parent（balance）与 cash_dividend_ps_pre_tax（dividend），
      known=ann_date（无差分依赖）
    """
    ts = facts["ts_code"].iloc[0]
    ttm_parts: list[pd.DataFrame] = []
    slope_parts: list[pd.DataFrame] = []
    event_parts: list[pd.DataFrame] = []

    def _ttm_rows(field: str, grid: pd.DataFrame) -> pd.DataFrame:
        valid = grid[grid["ttm"].notna()]
        return pd.DataFrame(
            {
                "ts_code": ts,
                "field": field,
                "end_date": valid.index,
                "known_date": valid["known_date"].to_numpy(),
                "ttm": valid["ttm"].to_numpy(),
            }
        )

    # rev / np：YTD → TTM 网格 + 斜率
    for field, stmt, col in [
        ("rev", "income", "revenue"),
        ("np", "income", "net_profit_parent_ytd"),
    ]:
        grid = pit_ttm_with_known(extract_statement_field(facts, stmt, col))
        if grid.empty:
            continue
        ttm_parts.append(_ttm_rows(field, grid))
        sl = rolling_growth_slope(grid["ttm"], grid["known_date"], n=growth_n)
        sl = sl[sl["slope"].notna()]
        if not sl.empty:
            slope_parts.append(
                pd.DataFrame(
                    {
                        "ts_code": ts,
                        "field": field,
                        "end_date": sl.index,
                        "known_date": sl["known_date"].to_numpy(),
                        "slope": sl["slope"].to_numpy(),
                    }
                )
            )

    # cfo：CSMAR YTD 链 + Wind 直接 TTM
    cfo_grid = pit_ttm_with_known(extract_statement_field(facts, "cashflow_direct", "cfo_net"))
    csmar_max = cfo_grid.index.max() if not cfo_grid.empty else pd.Timestamp.min
    if not cfo_grid.empty:
        ttm_parts.append(_ttm_rows("cfo", cfo_grid))
    wind_cfo = extract_statement_field(facts, "cashflow", "cfo_ttm")
    wind_cfo = wind_cfo[wind_cfo["value"].notna() & (wind_cfo["end_date"] > csmar_max)]
    if not wind_cfo.empty:
        ttm_parts.append(
            pd.DataFrame(
                {
                    "ts_code": ts,
                    "field": "cfo",
                    "end_date": wind_cfo["end_date"].to_numpy(),
                    "known_date": wind_cfo["ann_date"].to_numpy(),
                    "ttm": wind_cfo["value"].to_numpy(),
                }
            )
        )

    # events：equity / dps
    for field, stmt, col in [
        ("equity", "balance", "equity_parent"),
        ("dps", "dividend", "cash_dividend_ps_pre_tax"),
    ]:
        ev = extract_statement_field(facts, stmt, col)
        ev = ev[ev["value"].notna()]
        if not ev.empty:
            event_parts.append(
                pd.DataFrame(
                    {
                        "ts_code": ts,
                        "field": field,
                        "end_date": ev["end_date"].to_numpy(),
                        "known_date": ev["ann_date"].to_numpy(),
                        "value": ev["value"].to_numpy(),
                    }
                )
            )

    empty_cols = {
        "ttm": ["ts_code", "field", "end_date", "known_date", "ttm"],
        "slope": ["ts_code", "field", "end_date", "known_date", "slope"],
        "event": ["ts_code", "field", "end_date", "known_date", "value"],
    }
    return {
        "ttm": pd.concat(ttm_parts, ignore_index=True) if ttm_parts else pd.DataFrame(columns=empty_cols["ttm"]),
        "slope": pd.concat(slope_parts, ignore_index=True) if slope_parts else pd.DataFrame(columns=empty_cols["slope"]),
        "event": pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame(columns=empty_cols["event"]),
    }


# ---------------------------------------------------------------- pool（IO）
def _all_tickers(db) -> list[str]:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT ts_code FROM {db['schema']}.stock_financial ORDER BY ts_code"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def build_pool(db=None, chunk: int = 400, start: str = "2003-01-01",
               end: str | None = None, growth_n: int = 12) -> None:
    """阶段 1：全市场财务池 → cache/pool_{ttm,slope,event}.csv。"""
    db = db or load_db_config()
    end = end or str(pd.Timestamp.today().date())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tickers = _all_tickers(db)
    print(f"[pool] {len(tickers)} tickers, chunk={chunk}, window={start}..{end}")
    pools: dict[str, list[pd.DataFrame]] = {"ttm": [], "slope": [], "event": []}
    t0 = time.time()
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        facts = fetch_financial_facts(
            batch, start, end, db=db, statement_types=FIN_STATEMENT_TYPES
        )
        for _, tf in facts.groupby("ts_code", sort=False):
            rows = ticker_financial_rows(tf, growth_n=growth_n)
            for key in pools:
                if not rows[key].empty:
                    pools[key].append(rows[key])
        print(f"[pool] {i + len(batch)}/{len(tickers)} done, {time.time() - t0:.0f}s")
    for key, parts in pools.items():
        out = pd.concat(parts, ignore_index=True)
        out.to_csv(CACHE_DIR / f"pool_{key}.csv", index=False)
        print(f"[pool] pool_{key}.csv: {len(out)} rows")


# ---------------------------------------------------------------- scores
def _read_pool(key: str) -> pd.DataFrame:
    df = pd.read_csv(
        CACHE_DIR / f"pool_{key}.csv", parse_dates=["end_date", "known_date"]
    )
    return df


def _fetch_shares_pool(db) -> pd.DataFrame:
    """股本事件表：end_date=effective_date, known_date=available_date。"""
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"""SELECT ts_code, effective_date AS end_date,
                       available_date AS known_date, total_shares
                FROM {db['schema']}.stock_share_capital
                WHERE total_shares IS NOT NULL AND total_shares > 0""",
            conn,
        )
    finally:
        conn.close()
    df["end_date"] = pd.to_datetime(df["end_date"])
    df["known_date"] = pd.to_datetime(df["known_date"]).fillna(df["end_date"])
    return df


def _fetch_financial_set(db) -> set[str]:
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"""SELECT DISTINCT ts_code FROM {db['schema']}.industry_classification
                WHERE classification_type='CITIC' AND level_1_name = ANY(%(inds)s)""",
            conn,
            params={"inds": list(FINANCIAL_INDUSTRIES)},
        )
    finally:
        conn.close()
    return set(df["ts_code"])


def _fetch_list_dates(db) -> pd.Series:
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"SELECT ts_code, list_date FROM {db['schema']}.stock_meta", conn
        )
    finally:
        conn.close()
    return pd.Series(pd.to_datetime(df["list_date"]).to_numpy(), index=df["ts_code"])


def _fetch_close_snapshot(db, dates: list[pd.Timestamp]) -> pd.DataFrame:
    """月末未复权收盘快照（真市值口径）→ wide（index=date, columns=ts_code）。"""
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"""SELECT ts_code, trade_date, close FROM {db['schema']}.stock_daily_price
                WHERE trade_date = ANY(%(ds)s) AND close IS NOT NULL""",
            conn,
            params={"ds": [d.date() for d in dates]},
        )
    finally:
        conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")


def _month_end_trading_days(trading_days: pd.DatetimeIndex,
                            start: str, end: str) -> list[pd.Timestamp]:
    s = pd.Series(trading_days, index=trading_days)
    month_ends = s.groupby(s.index.to_period("M")).max()
    return [d for d in month_ends if pd.Timestamp(start) <= d <= pd.Timestamp(end)]


def _fetch_trading_days(db, start: str) -> pd.DatetimeIndex:
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"""SELECT DISTINCT trade_date FROM {db['schema']}.index_daily
                WHERE index_code='000905.SH' AND trade_date >= %(s)s ORDER BY trade_date""",
            conn,
            params={"s": start},
        )
    finally:
        conn.close()
    return pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))


def build_scores(db=None, start: str = "2013-05-01", end: str | None = None) -> None:
    """阶段 2：逐月末截面打分 → cache/scores.csv（长表：date × ts_code × 因子/分数/排名）。"""
    db = db or load_db_config()
    end = end or str(pd.Timestamp.today().date())
    pool_ttm = _read_pool("ttm")
    pool_slope = _read_pool("slope")
    pool_event = _read_pool("event")
    shares_pool = _fetch_shares_pool(db)
    fin_set = _fetch_financial_set(db)
    list_dates = _fetch_list_dates(db)

    trading = _fetch_trading_days(db, start)
    month_ends = _month_end_trading_days(trading, start, end)
    print(f"[scores] {len(month_ends)} month-ends {month_ends[0].date()}..{month_ends[-1].date()}")
    close_wide = _fetch_close_snapshot(db, month_ends)

    # 按 field 预切分，避免每次全表过滤
    ttm_by = {f: g for f, g in pool_ttm.groupby("field")}
    slope_by = {f: g for f, g in pool_slope.groupby("field")}
    event_by = {f: g for f, g in pool_event.groupby("field")}

    records: list[pd.DataFrame] = []
    t0 = time.time()
    for k, d in enumerate(month_ends):
        def _asof(pool: pd.DataFrame, col: str) -> pd.Series:
            got = asof_latest(pool, d)
            if got.empty:
                return pd.Series(dtype=float)
            return pd.Series(got[col].to_numpy(), index=got["ts_code"])

        sal_g = _asof(slope_by.get("rev", pool_slope.iloc[:0]), "slope")
        pro_g = _asof(slope_by.get("np", pool_slope.iloc[:0]), "slope")
        np_ttm = _asof(ttm_by.get("np", pool_ttm.iloc[:0]), "ttm")
        cfo_ttm = _asof(ttm_by.get("cfo", pool_ttm.iloc[:0]), "ttm")
        equity = _asof(event_by.get("equity", pool_event.iloc[:0]), "value")
        dps = _asof(event_by.get("dps", pool_event.iloc[:0]), "value")
        shares = _asof(shares_pool, "total_shares")

        if d not in close_wide.index:
            continue
        close = close_wide.loc[d].dropna()
        # 轻过滤：有价、有股本、上市 ≥ MIN_LISTED_DAYS
        base = close.index.intersection(shares.index)
        listed = list_dates.reindex(base)
        ok = base[(listed + pd.Timedelta(days=MIN_LISTED_DAYS) <= d) | listed.isna()]
        mv = shares.reindex(ok) * close.reindex(ok)
        mv = mv[mv > 0]

        factors = pd.DataFrame(index=mv.index)
        factors["sal_g"] = sal_g.reindex(mv.index)
        factors["pro_g"] = pro_g.reindex(mv.index)
        factors["ep"] = np_ttm.reindex(mv.index) / mv
        factors["bp"] = equity.reindex(mv.index) / mv
        factors["cfp"] = cfo_ttm.reindex(mv.index) / mv
        factors.loc[factors.index.isin(fin_set), "cfp"] = np.nan
        factors["dp"] = dps.reindex(mv.index) * shares.reindex(mv.index) / mv

        scored = style_scores(factors)
        scored["mv"] = mv
        scored["mv_rank"] = mv.rank(ascending=False, method="first")
        scored.insert(0, "date", d)
        scored.insert(1, "ts_code", scored.index)
        records.append(scored.reset_index(drop=True))
        if (k + 1) % 20 == 0:
            print(f"[scores] {k + 1}/{len(month_ends)}, {time.time() - t0:.0f}s")

    out = pd.concat(records, ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(CACHE_DIR / "scores.csv", index=False)
    print(f"[scores] scores.csv: {len(out)} rows, {out['date'].nunique()} dates")


# ---------------------------------------------------------------- baskets
def _fetch_qfq_returns(db, start: str) -> pd.DataFrame:
    """全市场前复权日收益矩阵（date × ts_code）。停牌 ffill≤20 日（指数口径）。"""
    conn = _connect(db)
    buf = io.StringIO()
    try:
        with conn.cursor() as cur:
            cur.copy_expert(
                f"""COPY (SELECT ts_code, trade_date, close
                          FROM {db['schema']}.stock_daily_price_qfq
                          WHERE trade_date >= '{start}' AND close IS NOT NULL)
                    TO STDOUT WITH CSV HEADER""",
                buf,
            )
    finally:
        conn.close()
    buf.seek(0)
    df = pd.read_csv(buf, dtype={"ts_code": "category", "close": "float32"},
                     parse_dates=["trade_date"])
    wide = df.pivot_table(index="trade_date", columns="ts_code", values="close",
                          aggfunc="last", observed=True)
    wide = wide.ffill(limit=20)
    return wide.pct_change(fill_method=None)


def build_baskets(db=None, pct: float = 0.3,
                  universes: tuple[str, ...] = ("U0", "U1", "U2", "U3", "U4"),
                  ret_start: str = "2013-01-01") -> None:
    """阶段 3：读 scores 缓存 → 各 universe 分桶 → 日度两腿/价差 → output CSV。"""
    db = db or load_db_config()
    scores = pd.read_csv(CACHE_DIR / "scores.csv", parse_dates=["date"])
    returns = _fetch_qfq_returns(db, ret_start)
    print(f"[baskets] returns matrix {returns.shape}, scores {len(scores)} rows")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for uni in universes:
        schedule = []
        for d, snap in scores.groupby("date"):
            snap = snap.set_index("ts_code")
            mask = universe_mask(snap["mv_rank"], uni)
            growth, value = select_baskets(snap.loc[mask, "style_score"], pct=pct)
            if growth and value:
                schedule.append((d, growth, value))
        if not schedule:
            print(f"[baskets] {uni}: empty schedule, skip")
            continue
        spread = basket_spread_returns(returns, schedule)
        spread = spread.dropna(how="all")
        # 两腿累计净值（=自建"指数"，供信号管线像消费指数对一样消费）
        spread["growth_index"] = (1.0 + spread["growth_ret"].fillna(0)).cumprod()
        spread["value_index"] = (1.0 + spread["value_ret"].fillna(0)).cumprod()
        n_members = int(np.mean([len(g) for _, g, _ in schedule]))
        out_file = OUT_DIR / f"spread_{uni}.csv"
        spread.to_csv(out_file, index_label="date")
        print(f"[baskets] {uni}: {len(schedule)} rebalances, ~{n_members}/basket, "
              f"{len(spread)} days -> {out_file.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="自建风格篮子构建（B1）")
    parser.add_argument("--stage", choices=["pool", "scores", "baskets", "all"],
                        default="all")
    parser.add_argument("--chunk", type=int, default=400)
    parser.add_argument("--start", default="2013-05-01", help="打分起点（月末评估）")
    parser.add_argument("--end", default=None)
    parser.add_argument("--pct", type=float, default=0.3)
    args = parser.parse_args()
    if args.stage in ("pool", "all"):
        build_pool(chunk=args.chunk, end=args.end)
    if args.stage in ("scores", "all"):
        build_scores(start=args.start, end=args.end)
    if args.stage in ("baskets", "all"):
        build_baskets(pct=args.pct)
    return 0


if __name__ == "__main__":
    sys.exit(main())
