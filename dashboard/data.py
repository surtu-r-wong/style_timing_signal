"""仪表盘数据层：committed 产物 + PG 两融 → 面板数据。

展示层，零新信号：全部序列来自已闭环研究的 committed CSV（output/ 与
backtest/output/）+ PG edb_daily 两融（复用 leverage_probe._load_margin）。
统一读数口径 = 250d 滚动分位（rank-based，对单调变换不变）。
设计：docs/plans/2026-07-08-style-dashboard-design.md。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "output"
BT_OUT = ROOT / "backtest" / "output"

# 状态条三线：(名称, 信号 CSV, 因子列)；推荐持仓 = output/recommended/<name>_longflat.csv
SIGNALS = (
    ("equal_weight", OUT / "equal_weight/equal_weight_signal_20d40z.csv", "factor_value"),
    ("hybrid20", OUT / "hybrid20/confirmed_signal.csv", "factor_20"),
    ("citic40d", OUT / "citic40d/citic_style_signal_40d.csv", "factor_20"),
)


# ---------------------------------------------------------------- 纯函数
def rolling_percentile(s: pd.Series, window: int = 250) -> pd.Series:
    """末值在过去 window 日内的平均秩分位（∈(0,1]，窗口未满 NaN）。

    向量化（sliding window + 计数），与 pandas rank(pct=True) 平均秩口径一致；
    页面刷新即重算，rolling.apply 太慢。含 NaN 的序列先 dropna 再喂。
    """
    a = s.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    if len(a) >= window:
        w = sliding_window_view(a, window)
        last = w[:, -1][:, None]
        less = (w < last).sum(axis=1)
        equal = (w == last).sum(axis=1)
        out[window - 1:] = (less + 0.5 * (equal + 1)) / window
    return pd.Series(out, index=s.index)


def freshness(named: dict[str, pd.Series]) -> dict[str, pd.Timestamp]:
    """各数据源最后有效日期（尾部 NaN 不算新鲜）。"""
    return {k: v.dropna().index.max() for k, v in named.items()}


def trim_incomplete_tail(s: pd.Series, floor: float = 0.3) -> pd.Series:
    """从尾部剔除"活跃度崩塌"的不完整日（上游只灌了部分股票，如 2026-07-01
    仅 11 只入库导致成交额/涨停数全是垃圾）。

    从末日回走，剔除 值 < floor×近20日中位 的日子，遇到首个健康日即停——
    只作用于尾部，历史中段的真实低活跃日不受影响。floor=0.3：垃圾日是
    0.2% 量级、真实极端缩量日也不会低于中位的 30%。
    """
    med = s.rolling(20, min_periods=5).median()
    keep = len(s)
    for i in range(len(s) - 1, -1, -1):
        m = med.iloc[i]
        if np.isfinite(m) and s.iloc[i] < floor * m:
            keep = i
        else:
            break
    return s.iloc[:keep]


def trim_zero_return_tail(df: pd.DataFrame, ret_cols: list[str],
                          tol: float = 1e-4) -> pd.DataFrame:
    """从尾部剔除"全腿收益≈0"的占位日（build 时点上游未更、qfq 前值复制）。

    等权 400+ 只篮子的日收益不可能全腿同时 |ret|<1e-4，占位日则精确如此；
    只走尾部，遇首个正常日即停。
    """
    keep = len(df)
    for i in range(len(df) - 1, -1, -1):
        if all(abs(float(df[c].iloc[i])) < tol for c in ret_cols):
            keep = i
        else:
            break
    return df.iloc[:keep]


def rebase_indices(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """指数列归一到窗口首个有效值=1（切窗后两腿相对走势可比）；返回副本。"""
    out = df.copy()
    for c in cols:
        base = out[c].dropna()
        if len(base):
            out[c] = out[c] / base.iloc[0]
    return out


_RANGE_YEARS = {"1y": 1, "3y": 3, "5y": 5}


def slice_range(obj, key: str):
    """按时间范围键（1y/3y/5y/all）从末日回溯切片（Series/DataFrame 通吃）。"""
    if key == "all" or obj.empty:
        return obj
    cutoff = obj.index.max() - pd.DateOffset(years=_RANGE_YEARS[key])
    return obj[obj.index >= cutoff]


# ---------------------------------------------------------------- committed 产物装载
def load_style_meter() -> pd.DataFrame:
    """② 风格测量仪：U2 中性纯风格累计价差 + 信号化位置。"""
    spread = pd.read_csv(OUT / "style_basket/spread_U2_neutral.csv",
                         parse_dates=["date"]).set_index("date").sort_index()
    spread = trim_zero_return_tail(spread, ["growth_ret", "value_ret"])
    sig = pd.read_csv(OUT / "style_basket/signal_pure_style_U2.csv",
                      parse_dates=["date"]).set_index("date").sort_index()
    return spread[["growth_index", "value_index", "spread"]].join(
        sig["factor_value"].rename("signal"), how="left")


def load_thermometer() -> pd.DataFrame:
    """③ 涨停温度计 + 各指标 250d 分位列（尾部不完整日守卫按 n_active）。"""
    t = pd.read_csv(BT_OUT / "thermometer.csv",
                    parse_dates=["date"]).set_index("date").sort_index()
    t = t.loc[trim_incomplete_tail(t["n_active"].astype(float)).index]
    for c in ("lu_ratio", "burst_rate", "lu_premium"):
        t[f"{c}_pct"] = rolling_percentile(t[c].dropna()).reindex(t.index)
    return t


def load_turnover() -> pd.Series:
    s = pd.read_csv(BT_OUT / "market_turnover.csv",
                    parse_dates=["date"]).set_index("date")["amt_yuan"].sort_index()
    return trim_incomplete_tail(s)


def load_breadth() -> pd.DataFrame:
    return pd.read_csv(BT_OUT / "breadth.csv",
                       parse_dates=["trade_date"]).set_index("trade_date").sort_index()


def load_signals_status() -> list[dict]:
    """① 状态条：三线最新因子值 + long-flat 推荐持仓 + 各自截止日。"""
    rows = []
    for name, path, col in SIGNALS:
        fac = (pd.read_csv(path, parse_dates=["date"]).set_index("date")
               .sort_index()[col].dropna())
        pos = (pd.read_csv(OUT / f"recommended/{name}_longflat.csv",
                           parse_dates=["date"]).set_index("date")
               .sort_index()["position"].dropna())
        rows.append({
            "name": name, "factor": float(fac.iloc[-1]), "date": fac.index[-1],
            "position": int(pos.iloc[-1]), "pos_date": pos.index[-1], "series": fac,
        })
    return rows


# ---------------------------------------------------------------- PG 两融
_MARGIN_CACHE: dict = {}   # {"t": epoch, "df": DataFrame}
_MARGIN_TTL = 300.0        # PG 查询 ~7s 是页面瓶颈；两融日更，5 分钟 TTL 足够


def load_margin(db=None, ttl: float = _MARGIN_TTL) -> pd.DataFrame:
    """④ 杠杆：两融余额/买入额（PG edb_daily）+ 占成交比 + 20d 增速 + 分位。

    进程内 TTL 缓存：TTL 内 range 切换/刷新复用，过期重查（重读语义保留）。
    """
    import time as _time
    import backtest.leverage_probe as _lp
    now = _time.time()
    if _MARGIN_CACHE.get("df") is not None and now - _MARGIN_CACHE["t"] < ttl:
        return _MARGIN_CACHE["df"]
    bal, buy = _lp._load_margin(db)
    amt = load_turnover()
    df = pd.DataFrame({"balance": bal, "buy": buy})
    df["buy_ratio"] = (buy * 1e4 / amt).reindex(df.index)  # 万元 → 元，同分母
    df["bal_growth20"] = bal.pct_change(20)
    df["buy_ratio_pct"] = rolling_percentile(df["buy_ratio"].dropna()).reindex(df.index)
    _MARGIN_CACHE.update(t=now, df=df)
    return df


# ---------------------------------------------------------------- bundle（回调一次装齐）
def data_bundle(include_margin: bool = True, db=None) -> dict:
    style = load_style_meter()
    thermo = load_thermometer()
    amt = load_turnover()
    breadth = load_breadth()
    signals = load_signals_status()
    margin = None
    if include_margin:
        try:
            margin = load_margin(db)
        except Exception:
            margin = None  # PG 不可达 → 杠杆面板降级为提示，其余照常

    sources = {
        "风格测量仪": style["spread"],
        "涨停温度计": thermo["lu_ratio"],
        "成交额": amt,
        "广度": breadth["pct_above_ma20"],
    }
    if margin is not None:
        sources["两融"] = margin["balance"]

    return {
        "signals": signals,
        "style": style,
        "thermo": thermo,
        "turnover": amt,
        "turnover_pct": rolling_percentile(amt),
        "breadth": breadth,
        "margin": margin,
        "freshness": freshness(sources),
    }
