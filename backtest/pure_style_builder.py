"""中证「纯风格」（P 族）编制方案的自建实现 —— 可指向任意市值带。

预登记 = `docs/plans/2026-08-18-fifth-bucket-preregistration.md`（跑前冻结）。
**Gate 0 与尾部桶共用本模块**（预登记 §1 要求"用完全相同的代码与参数"）。

## 方法论出处

`/home/elfbob/exchange/20260702/指数编制方案/中证2000纯成长（纯价值）指数编制方案.pdf`
（2025-01 V1.0）。规则逐条照抄，见预登记 §3。

## 已登记的口径替代（预登记附录 A.2）

1. **中信一级行业代替中证行业分类**（库内无 CSI 类型）。金融 = 银行 / 非银行金融 / 综合金融；
   **房地产不算金融**（中证一级行业里房地产是独立板块）。
2. **`circ_mv`（流通市值）代替自由流通市值**（logistic 平滑的 25/50/75% 分位点）。
3. 样本空间用 **`total_mv` 排名带**代替官方成分（库内无四母指数历史成分）。

## PIT

只取 `ann_date <= 调样日` 的财务行。⚠️ `stock_financial` 96.5% 来自 CSMAR，其 `ann_date`
是数据集批次日而非首披日 → **approximate PIT，结论须标 provisional**（同 B3）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.common.factors import growth_slope, pit_ttm_with_known  # noqa: E402

#: 中信一级行业 → 视作「金融」（价值分剔 CF/P、除 √3）。房地产**不**计入。
FINANCE_INDUSTRIES = frozenset({"银行", "非银行金融", "综合金融"})
WINSOR = (0.05, 0.95)          # 极值调整（上证全指方案明文；纯风格方案称"极值调整"未给分位）
GROWTH_QUARTERS = 12           # SalG/ProG：过去三年季度滚动
FLOAT_QUANTILES = (0.25, 0.50, 0.75)
WEIGHT_CAP, TOP5_CAP = 0.15, 0.60


def _conn():
    import psycopg2
    db = load_db_config()
    c = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                         user=db["user"], password=db["password"], connect_timeout=15)
    return c, db["schema"]


# ---------------------------------------------------------------- 调样日
def rebalance_dates(start: str, end: str) -> list[pd.Timestamp]:
    """每年 6 月和 12 月**第二个星期五的下一交易日**（中证口径）。

    交易日取 `index_daily` 里 000300.SH 的日期集合（该指数全历史连续）。
    """
    c, schema = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT trade_date FROM {schema}.index_daily
                            WHERE index_code='000300.SH' ORDER BY trade_date""")
            days = pd.DatetimeIndex([r[0] for r in cur.fetchall()])
    finally:
        c.close()
    out = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        for m in (6, 12):
            fridays = pd.date_range(f"{y}-{m:02d}-01", f"{y}-{m:02d}-28", freq="W-FRI")
            if len(fridays) < 2:
                continue
            nxt = days[days > fridays[1]]
            if len(nxt):
                out.append(nxt[0])
    return [d for d in out if pd.Timestamp(start) <= d <= pd.Timestamp(end)]


# ---------------------------------------------------------------- 样本空间
def sample_space(asof: pd.Timestamp, rank_lo: int, rank_hi: int | None,
                 min_list_years: float = 1.0, liq_drop_pct: float = 0.20) -> pd.DataFrame:
    """调样日的样本空间：市值排名带 → 剔新股 → 剔流动性尾部。

    `rank_lo`/`rank_hi` 为 1-based 闭区间（`rank_hi=None` 表示到最后）。
    返回 index=ts_code，列 `total_mv / circ_mv / avg_mv_1y / avg_close_1y / adv_1y / industry`。
    """
    d = asof.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, total_mv::float8, circ_mv::float8
                            FROM {s}.stock_indicator
                            WHERE trade_date=(SELECT max(trade_date) FROM {s}.stock_indicator
                                              WHERE trade_date<=DATE '{d}')
                              AND total_mv IS NOT NULL
                              AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ')""")
            snap = pd.DataFrame(cur.fetchall(), columns=["ts_code", "total_mv", "circ_mv"])
            snap = snap.sort_values("total_mv", ascending=False).set_index("ts_code")
            band = snap.iloc[rank_lo - 1: rank_hi].copy()
            codes = list(band.index)
            if not codes:
                return band.assign(avg_mv_1y=np.nan)

            cur.execute(f"""SELECT ts_code, avg(total_mv)::float8 FROM {s}.stock_indicator
                            WHERE ts_code=ANY(%s) AND trade_date BETWEEN DATE '{d}'-INTERVAL '365 days'
                              AND DATE '{d}' GROUP BY 1""", (codes,))
            band["avg_mv_1y"] = pd.Series(dict(cur.fetchall()))

            cur.execute(f"""SELECT ts_code, avg(close)::float8, avg(amount)::float8, count(*)
                            FROM {s}.stock_daily_price
                            WHERE ts_code=ANY(%s) AND trade_date BETWEEN DATE '{d}'-INTERVAL '365 days'
                              AND DATE '{d}' AND close IS NOT NULL AND pre_close>0 AND volume>0
                            GROUP BY 1""", (codes,))
            px = pd.DataFrame(cur.fetchall(),
                              columns=["ts_code", "avg_close_1y", "adv_1y", "n_days"]).set_index("ts_code")
            band = band.join(px)

            cur.execute(f"""SELECT ts_code, list_date FROM {s}.stock_meta WHERE ts_code=ANY(%s)""", (codes,))
            band["list_date"] = pd.to_datetime(pd.Series(dict(cur.fetchall())), errors="coerce")

            cur.execute(f"""SELECT DISTINCT ON (ts_code) ts_code, level_1_name
                            FROM {s}.industry_classification
                            WHERE ts_code=ANY(%s) AND classification_type='CITIC'
                              AND effective_date<=DATE '{d}'
                            ORDER BY ts_code, effective_date DESC""", (codes,))
            band["industry"] = pd.Series(dict(cur.fetchall()))
    finally:
        c.close()

    band = band[band["list_date"].notna()]
    band = band[(asof - band["list_date"]).dt.days >= min_list_years * 365]   # 剔新股
    band = band[band["adv_1y"].notna()]
    band = band[band["adv_1y"] >= band["adv_1y"].quantile(liq_drop_pct)]       # 剔流动性尾部
    return band


# ---------------------------------------------------------------- 因子面板
_STMT_FIELD = {
    "income": ("B001100000", "revenue"),
    "balance": ("A003000000", "equity"),
    "cashflow_direct": ("C001000000", "cfo"),
    "disclosed_indicators": ("F020102", "profit_ex"),      # 归母扣非净利润
    "profitability": ("F050501B", "roe"),                  # 净利润/股东权益余额
    "dividend": ("F100801A", "dps"),                       # 占位，见 _dividend_field
}
#: `dividend` 的每股税前现金红利在项目白名单里叫 `cash_dividend_ps_pre_tax`；
#: 其 CSMAR 编码从 `financial_field_map` 反查，避免在此处硬编码错。
def _dividend_field() -> str:
    from signals.common.financial_field_map import CSMAR_FIELD_MAPS
    for code, friendly in CSMAR_FIELD_MAPS.get("dividend", {}).items():
        if friendly == "cash_dividend_ps_pre_tax":
            return code
    raise KeyError("financial_field_map 里找不到 cash_dividend_ps_pre_tax")


def _fetch_series(codes: list[str], asof: pd.Timestamp, stmt: str, field: str,
                  years: int = 6) -> pd.DataFrame:
    """PIT 拉取 (ts_code, end_date, ann_date, value)：只取 `ann_date <= asof` 的行。

    `years=6`：SalG/ProG 需 12 个 TTM 点，而 TTM 要 4 季暖机 + CSMAR 的 01-01 伪行被剔除，
    4 年只剩 11 个非空 TTM（实测 600110.SH），故必须拉够 6 年。
    """
    d = asof.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, end_date, ann_date, (data->>%s)::float8
                            FROM {s}.stock_financial
                            WHERE ts_code=ANY(%s) AND statement_type=%s
                              AND ann_date <= DATE '{d}'
                              AND end_date >= DATE '{d}' - INTERVAL '{years} years'
                              AND data ? %s""", (field, codes, stmt, field))
            rows = cur.fetchall()
    finally:
        c.close()
    return pd.DataFrame(rows, columns=["ts_code", "end_date", "ann_date", "value"])


def _ttm_latest(df: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """每只股票的最新可知 TTM 值（走 `factors.pit_ttm_with_known`，零平行实现）。"""
    out = {}
    for code, g in df.groupby("ts_code"):
        t = pit_ttm_with_known(g.rename(columns={"value": "value"}))
        t = t[t["known_date"].notna() & (t["known_date"] <= asof)]
        if len(t) and pd.notna(t["ttm"].iloc[-1]):
            out[code] = float(t["ttm"].iloc[-1])
    return pd.Series(out, dtype=float)


def _growth(df: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """SalG / ProG：过去 12 季 TTM 对时间的 OLS 斜率 ÷ |均值|（`factors.growth_slope`）。"""
    out = {}
    for code, g in df.groupby("ts_code"):
        t = pit_ttm_with_known(g)
        t = t[t["known_date"].notna() & (t["known_date"] <= asof)]
        if len(t) >= GROWTH_QUARTERS:
            v = growth_slope(t["ttm"], n=GROWTH_QUARTERS)
            if np.isfinite(v):
                out[code] = v
    return pd.Series(out, dtype=float)


def _delta_roe(codes: list[str], asof: pd.Timestamp) -> pd.Series:
    """ΔROE = 当期季报 ROE − 去年同期季报 ROE（CSI 要的是**差**，不是 `F080702B` 那个比率）。"""
    df = _fetch_series(codes, asof, "profitability", "F050501B", years=3)
    out = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("end_date")
        s = pd.Series(g["value"].to_numpy(dtype=float),
                      index=pd.DatetimeIndex(g["end_date"]))
        s = s[~s.index.duplicated(keep="last")]
        s = s[pd.DatetimeIndex(s.index).is_quarter_end]
        if len(s) < 5:
            continue
        cur_end = s.index[-1]
        prev = s.index[(s.index.month == cur_end.month) & (s.index.year == cur_end.year - 1)]
        if len(prev) and np.isfinite(s.iloc[-1]) and np.isfinite(s[prev[0]]):
            out[code] = float(s.iloc[-1] - s[prev[0]])
    return pd.Series(out, dtype=float)


def factor_panel(sp: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """七个 CSI 因子的原始值面板（index=ts_code）。

    价值四项分母统一用**过去 1 年日均总市值**（原文："与过去 1 年日均总市值的比值"）；
    D/P 用每股税前现金红利 ÷ 过去 1 年日均收盘价（等价形式）。
    """
    codes = list(sp.index)
    amv = sp["avg_mv_1y"]
    panel = pd.DataFrame(index=sp.index)
    panel["EP"] = _ttm_latest(_fetch_series(codes, asof, "income", "B002000000"), asof) / amv
    panel["CFP"] = _ttm_latest(_fetch_series(codes, asof, "cashflow_direct", "C001000000"), asof) / amv
    eq = _fetch_series(codes, asof, "balance", "A003000000")
    eq_latest = (eq.sort_values("end_date").groupby("ts_code")["value"].last())
    panel["BP"] = eq_latest / amv
    dps = _ttm_latest(_fetch_series(codes, asof, "dividend", _dividend_field()), asof)
    panel["DP"] = (dps / sp["avg_close_1y"]).reindex(panel.index).fillna(0.0)   # 未分红 → 0
    panel["SalG"] = _growth(_fetch_series(codes, asof, "income", "B001100000"), asof)
    panel["ProG"] = _growth(_fetch_series(codes, asof, "disclosed_indicators", "F020102"), asof)
    panel["dROE"] = _delta_roe(codes, asof)
    panel["industry"] = sp["industry"]
    panel["circ_mv"] = sp["circ_mv"]
    return panel


def _winsor_z(s: pd.Series) -> pd.Series:
    lo, hi = s.quantile(WINSOR[0]), s.quantile(WINSOR[1])
    w = s.clip(lo, hi)
    sd = w.std(ddof=1)
    return (w - w.mean()) / sd if np.isfinite(sd) and sd > 0 else pd.Series(0.0, index=s.index)


def style_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """价值得分 / 成长得分（区分金融·非金融；缺失用同行业均值代替）。"""
    p = panel.copy()
    for c in ("EP", "CFP", "BP", "DP", "SalG", "ProG", "dROE"):
        ind_mean = p.groupby("industry")[c].transform("mean")
        p[c] = p[c].fillna(ind_mean).fillna(p[c].mean())          # 同行业均值 → 全场均值兜底
    z = pd.DataFrame({c: _winsor_z(p[c]) for c in
                      ("EP", "CFP", "BP", "DP", "SalG", "ProG", "dROE")}, index=p.index)
    fin = p["industry"].isin(FINANCE_INDUSTRIES)
    value = pd.Series(np.nan, index=p.index)
    value[fin] = (z.loc[fin, "DP"] + z.loc[fin, "BP"] + z.loc[fin, "EP"]) / np.sqrt(3)
    value[~fin] = (z.loc[~fin, "DP"] + z.loc[~fin, "BP"]
                   + z.loc[~fin, "CFP"] + z.loc[~fin, "EP"]) / 2.0
    growth = (z["SalG"] + z["ProG"] + z["dROE"]) / np.sqrt(3)
    return pd.DataFrame({"value_score": value, "growth_score": growth,
                         "circ_mv": p["circ_mv"], "industry": p["industry"]})


def _logistic_prob(x: pd.Series, xl: float, xm: float, xu: float) -> pd.Series:
    """原文平滑函数：X≤X_M 用 (X_M−X_L) 作尺度，X>X_M 用 (X_U−X_M)。"""
    x = x.astype(float)
    scale = np.where(x <= xm, max(xm - xl, 1e-12), max(xu - xm, 1e-12))
    y = 1.0 / (1.0 + np.exp(4.0 * (xm - x) / scale))
    return pd.Series(y, index=x.index)


def style_probabilities(sc: pd.DataFrame) -> pd.DataFrame:
    """得分 → 综合价值/成长概率（含 Y≤0.1→0、Y≥0.9→1 的截断）。"""
    d = sc.dropna(subset=["value_score", "growth_score"]).copy()
    # 步骤(3)：先把两个得分平滑成 0~1 概率 —— 原文未给函数，此处用**经验秩分位**（自由裁量，已登记）
    d["value_prob"] = d["value_score"].rank(pct=True)
    d["growth_prob"] = d["growth_score"].rank(pct=True)
    d["comb_value_score"] = (d["value_prob"] + (1.0 - d["growth_prob"])) / 2.0
    d["comb_growth_score"] = 1.0 - d["comb_value_score"]
    # 步骤(4)：X_L/X_M/X_U = 按综合价值得分升序、累计自由流通市值达 25/50/75% 处的得分
    o = d.sort_values("comb_value_score")
    cw = o["circ_mv"].fillna(0).cumsum() / max(o["circ_mv"].fillna(0).sum(), 1e-12)
    xs = [float(o["comb_value_score"].iloc[int(np.searchsorted(cw.to_numpy(), q))])
          for q in FLOAT_QUANTILES]
    y = _logistic_prob(d["comb_value_score"], *xs)
    y = y.mask(y <= 0.1, 0.0).mask(y >= 0.9, 1.0)
    d["comb_value_prob"] = y
    d["comb_growth_prob"] = 1.0 - y
    return d


def _cap_weights(score: pd.Series) -> pd.Series:
    """综合风格得分加权 + 单样本 ≤15%、前五合计 ≤60%（迭代压顶再归一）。"""
    w = score.clip(lower=0)
    if w.sum() <= 0:
        w = pd.Series(1.0, index=score.index)
    w = w / w.sum()
    for _ in range(100):
        over = w > WEIGHT_CAP
        if over.any():
            excess = (w[over] - WEIGHT_CAP).sum()
            w[over] = WEIGHT_CAP
            free = ~over
            if free.any() and w[free].sum() > 0:
                w[free] += excess * w[free] / w[free].sum()
        top5 = w.nlargest(min(5, len(w)))
        if top5.sum() > TOP5_CAP:
            w[top5.index] *= TOP5_CAP / top5.sum()
            w = w / w.sum()
            continue
        if not over.any():
            break
    return w / w.sum()


def select_legs(prob: pd.DataFrame, take_top_half: bool) -> tuple[pd.Series, pd.Series]:
    """→ (成长腿权重, 价值腿权重)。`take_top_half` = 2000 带的"再取前 50%"附加条款。"""
    legs = {}
    for side, pcol, scol in (("growth", "comb_growth_prob", "comb_growth_score"),
                             ("value", "comb_value_prob", "comb_value_score")):
        sel = prob[prob[pcol] >= 1.0]
        if take_top_half and len(sel) > 1:
            sel = sel.nlargest(max(1, len(sel) // 2), scol)
        legs[side] = _cap_weights(sel[scol]) if len(sel) else pd.Series(dtype=float)
    return legs["growth"], legs["value"]


def _daily_returns(codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """窗口内成分股日收益宽表（close/pre_close − 1，沿用 UNIVERSE_SQL 的行级有效性）。"""
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, trade_date, (close/pre_close-1.0)::float8
                            FROM {s}.stock_daily_price
                            WHERE ts_code=ANY(%s) AND trade_date> DATE '{start.date()}'
                              AND trade_date<=DATE '{end.date()}'
                              AND close IS NOT NULL AND pre_close>0 AND volume>0""", (codes,))
            rows = cur.fetchall()
    finally:
        c.close()
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows, columns=["ts_code", "trade_date", "ret"])
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    return d.pivot(index="trade_date", columns="ts_code", values="ret").sort_index()


def _leg_returns(w0: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """一条腿在 (start, end] 的日收益：权重随价格漂移（调样日固定权重因子，期间不再平衡）。"""
    if not len(w0):
        return pd.Series(dtype=float)
    r = _daily_returns(list(w0.index), start, end)
    if r.empty:
        return pd.Series(dtype=float)
    r = r.reindex(columns=w0.index).fillna(0.0)          # 停牌/缺行 → 当日 0 收益
    w = w0.reindex(r.columns).fillna(0.0).to_numpy(dtype=float)
    out = {}
    for dt, row in zip(r.index, r.to_numpy(dtype=float)):
        tot = w.sum()
        out[dt] = float((w * row).sum() / tot) if tot > 0 else 0.0
        w = w * (1.0 + row)                               # 权重按持有漂移
    return pd.Series(out).sort_index()


@dataclass
class PairResult:
    growth: pd.Series          # 日收益
    value: pd.Series
    n_growth: dict
    n_value: dict


def build_pair(rank_lo: int, rank_hi: int | None, dates: list[pd.Timestamp],
               take_top_half: bool, verbose: bool = True) -> PairResult:
    """按调样日滚动构建一对纯风格腿的**日收益序列**。"""
    gs, vs, ng, nv = [], [], {}, {}
    for i, d in enumerate(dates[:-1]):
        sp = sample_space(d, rank_lo, rank_hi)
        if not len(sp):
            continue
        prob = style_probabilities(style_scores(factor_panel(sp, d)))
        wg, wv = select_legs(prob, take_top_half)
        ng[str(d.date())], nv[str(d.date())] = len(wg), len(wv)
        nxt = dates[i + 1]
        gs.append(_leg_returns(wg, d, nxt))
        vs.append(_leg_returns(wv, d, nxt))
        if verbose:
            print(f"  {d.date()} → {nxt.date()}: 样本空间 {len(sp)}，"
                  f"成长 {len(wg)} / 价值 {len(wv)}", flush=True)
    return PairResult(pd.concat(gs).sort_index() if gs else pd.Series(dtype=float),
                      pd.concat(vs).sort_index() if vs else pd.Series(dtype=float), ng, nv)
