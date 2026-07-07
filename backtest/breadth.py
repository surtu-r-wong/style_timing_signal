"""广度序列构建：从复权 close 长表算每日市场广度度量族（Phase 4 空头引擎信号 T1）。

纯函数 `compute_breadth`（可测、单一真源）+ `build_breadth`（PG stock_daily_price_qfq 拉数 + 缓存）。
广度是"下跌确认"类脆弱性信号的素材——标的创新高但广度不确认 = 顶背离火花（见 T2）。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def compute_breadth(prices: pd.DataFrame, ma_windows=(20, 60), hilo_windows=(20, 60),
                    min_active: int = 1) -> pd.DataFrame:
    """长表 (ts_code, trade_date, close) → 每日广度度量（index=trade_date）。

    - `pct_above_ma{L}`：close 严格 > 自身 L 日均线的股票占比，分母=当日有满 L 窗的股票数。
    - `hi_lo_diff{N}`：(N 日新高家数 − N 日新低家数) / 活跃家数（活跃=有满 N 窗）。
    - `min_active`：当日成交股票数 < 此值的交易日剔除（防未灌全的当日出伪广度）。
    """
    wide = prices.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    out = {}
    for L in ma_windows:
        ma = wide.rolling(L, min_periods=L).mean()
        denom = ma.notna().sum(axis=1)
        above = (wide > ma).sum(axis=1)
        out[f"pct_above_ma{L}"] = above / denom.where(denom > 0)
    for N in hilo_windows:
        roll_max = wide.rolling(N, min_periods=N).max()
        roll_min = wide.rolling(N, min_periods=N).min()
        active = roll_max.notna().sum(axis=1)
        highs = ((wide >= roll_max) & roll_max.notna()).sum(axis=1)
        lows = ((wide <= roll_min) & roll_min.notna()).sum(axis=1)
        out[f"hi_lo_diff{N}"] = (highs - lows) / active.where(active > 0)
    result = pd.DataFrame(out)
    xs_size = wide.notna().sum(axis=1)
    return result[xs_size.reindex(result.index) >= min_active]


def breadth_divergence(underlying: pd.Series, breadth: pd.Series, new_high_window: int = 20,
                       div_lookback: int = 20, form: str = "deteriorating",
                       threshold: float = 0.2, hold: int = 1) -> pd.Series:
    """标的创 P 日新高 + 广度不确认 → 空-触发短信号 {−1, 0}（顶背离火花，T2）。

    `form`（"广度不确认"三形式，全部进 T4 扫描）：
      - deteriorating (F1)：breadth_t < breadth_{t−Q}（价创新高时广度走弱）
      - nonconfirm  (F2)：breadth_t < 其自身 P 日滚动高（广度未同步创新高）
      - low_pct     (F3)：breadth_t < 其 Q 窗 threshold 分位（广度处低分位）
    `hold`：触发后持有 short 的天数（事件型对冲须持有穿越下跌；hold=1 为触发日当日）。
    """
    u = underlying.astype(float)
    is_new_high = u >= u.rolling(new_high_window, min_periods=new_high_window).max()
    if form == "deteriorating":
        not_confirm = breadth < breadth.shift(div_lookback)
    elif form == "nonconfirm":
        not_confirm = breadth < breadth.rolling(new_high_window, min_periods=new_high_window).max()
    elif form == "low_pct":
        q = breadth.rolling(div_lookback, min_periods=div_lookback).quantile(threshold)
        not_confirm = breadth < q
    else:
        raise ValueError(f"unknown form: {form!r}")
    trigger = is_new_high & not_confirm
    if hold > 1:
        trigger = trigger.astype(int).rolling(hold, min_periods=1).max() > 0
    return pd.Series(0, index=underlying.index, dtype=int).mask(trigger, -1)


def _fetch_prices_pg(db, start):
    """从 PG 拉全市场复权 close 长表（含退市票 → survivorship 可控）。"""
    from backtest.data import _connect
    from signals.common.config import load_db_config
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT ts_code, trade_date, close
                    FROM {db['schema']}.stock_daily_price_qfq
                    WHERE close IS NOT NULL
                      AND (%s::date IS NULL OR trade_date >= %s::date)""",
                (start, start),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["close"] = df["close"].astype(float)
    return df


CACHE_PATH = ROOT / "backtest" / "output" / "breadth.csv"


def build_breadth(db=None, start="2012-06-01", cache_path=CACHE_PATH,
                  ma_windows=(20, 60), hilo_windows=(20, 60), min_active=500,
                  fetch=None) -> pd.DataFrame:
    """拉 PG → compute_breadth → 缓存 CSV（一次性、下游复用）。

    `fetch(db, start) -> 长表` 可注入（默认 `_fetch_prices_pg`）便于测试与替换数据源；
    `start` 早于信号窗（2014）以让滚动窗在窗口起点已满；`min_active=500` 剔未灌全的当日
    （A 股实际交易日 ≥2400 只，500 是安全下限）。
    """
    fetch = fetch or _fetch_prices_pg
    prices = fetch(db, start)
    breadth = compute_breadth(prices, ma_windows, hilo_windows, min_active=min_active)
    if cache_path is not None:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        breadth.to_csv(cache_path)
    return breadth
