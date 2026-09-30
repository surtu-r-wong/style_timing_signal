"""⑦ 探针：公募持仓拥挤面（⑦B 风格暴露 β 分位 16 点 + ⑦A 季报仓位/板块分位 6 点）。

预登记出处（冻结，一字不改）：`docs/plans/2026-08-14-probe-7-preregistration.md`；
立项与四问裁决：`docs/plans/2026-08-14-fund-crowding-agenda.md` §7。

## ⑦B 族（日频·滚动风格暴露分位·16 点，预登记 §B.1-6）

- 腿（`stock_selector.index_daily`，§E 冻结代码）：`885000.WI` 偏股基金指数 /
  `000300.SH` / `000852.SH` / `399370.SZ` 国证成长 / `399371.SZ` 国证价值；
- 滚动 OLS `r_885000 = α + β_m·r_mkt + β_s·spread`，**W=240 固定**（W=120 仅敏感性报告列，
  不进选优）；size 维 `spread = r_000852 − r_000300`、`r_mkt = 两腿均值`；
  growth-value 维 `spread = r_399370 − r_399371`、`r_mkt = 两腿均值`；
- 信号 = β_s 的 rolling percentile（`dashboard.data.rolling_percentile` 平均秩口径，
  zw ∈ {250, 500}）；持有 k ∈ {5, 10, 20, 40}；
- 网格 = 2 维 × 2 zw × 4 k = **16 点合成单次置换调用**。

## ⑦A 族（季频阶梯·历史分位·6 点，预登记 §B.7-12）

- A1 = 主动权益两类（`ACTIVE_FUND_TYPES`，只读参考 stock_selector
  `fund_selection/picker.py:48`）`prt_stocktonav` **每期中位数**（全量档缓存
  `backtest/output/fund_stock_position.csv`；>100 保留原值、A/C 不去重）；
- A2 = 主动权益 universe 重仓池板块占比 = Σ hold_value(创业板 300/301.SZ + 科创板 688.SH)
  / Σ hold_value(全部 A 股 = 后缀 .SH/.SZ；港股/北交所行剔除)，**聚合在库端 SQL 完成**；
- 分位 = expanding percentile，burn-in 8 期（首个可用值 2018Q1）；k ∈ {20, 40, 60}；
- PIT：期级生效日 = 当期样本 `ann_date` 的 **P95**（`percentile_disc(0.95)` 语义，
  取实际观测日 → ≥95% 样本已披露）；敏感性列 = 报告期末 +30 自然日；
  生效日起 ffill 成日频阶梯；
- 网格 = 2 序列 × 3 k = **6 点合成单次置换调用**；
- **低功效声明**（§B.12）：41 期 − burn-in 8 ≈ 33 个独立观测，结论表述限
  "低功效未检出/检出"，不得写"无价值"。

## 机器接入三规矩（§B.13，`2026-08-12-selection-permutation-machine.md` §6）

1. 两族**分族**各自合成单次 `selection_permutation_test` 调用（16 点 / 6 点）；
   族级 Bonferroni **α = 0.025**；
2. 关1 统计量 = 无约束 `|非重叠 IC|`（关2 独立后置判，不塞进统计量）；
3. `min_shift = 2·max(k)`（⑦B = 80，⑦A = 120），`max_shift = n_obs − min_shift`。

## 三关（§B.14，任一不过 → STOP 归档）

关1 `p_selected < 0.025` **且**关1b 偏 IC（控生产 equal_weight，与 ②/面2 同一目标序列
blend 与同一机器）同号且 `p < 0.025`；关2 两半窗同号（**按各族自身选择样本切半**）；
关3 成本后净 Sharpe > 0（3 bps + blend carry）。方向先验（高拥挤分位 → 风格反向）
只入档，检验双侧。

## 报告列（§B.15，不设闸）

MaxDD/Calmar、corr(⑦B, 现役四对信号+equal_weight)、corr(⑦B size 维, ⑦A 两序列)、
W=120 敏感性、PIT 敏感性（P95 vs +30 天）、2024-26 第二验证窗、concentration_summary。

## 连库纪律

`backtest.data.load_db_config/_connect`，每连先 `SET statement_timeout='120s'`；
拒连即停报告（`_connect` 自带 3 次快速重试后抛出，不滚动重试）。
聚合全部在 SQL 端完成，客户端只收期级/日级小表（**不 fetchall 个股长表**）。

CLI: python3 -m backtest.fund_crowding_probe [--n-perm 1000] [--seed 0] [--db-host ...]
产出: backtest/output/probe_7_fund_crowding_{panel,permutation,diagnostics,verdict,
      sensitivity}.csv + probe_7_fund_crowding_summary.json；
永久资产缓存: fund_crowding_beta_daily.csv / fund_crowding_quarterly.csv

本模块**不改动** `signals/` / `selection_permutation.py` / `divergence_probe.py` /
`rotation_probe.py` / `engine.py` / stock_selector 任何代码 / 任何冻结根。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.data import _connect, load_carry, load_db_config, load_underlying_returns  # noqa: E402
from backtest.divergence_probe import (  # noqa: E402  ← ②/面2 同一机器（复用不重写）
    HOLDOUT, KOU_JING_REPORT, PRIMARY_KOU_JING, SELECTION, TRAIN, VAL, WINDOWS_REPORT,
    ScoreFrame, _slice, abs_partial_batch, abs_spearman_batch, build_score_frames,
    load_pair_signals, make_batch_scorer, net_sharpe_by_window, p_at_threshold,
)
from backtest.rotation_probe import _load_ew_signal, hold_position, nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.yearly import concentration_summary  # noqa: E402
from dashboard.data import rolling_percentile  # noqa: E402  ← 房规平均秩分位（conditional 先例）

__all__ = [
    # 复用面（②/面2 同一批对象，单一入口再导出）
    "SELECTION", "TRAIN", "VAL", "HOLDOUT", "WINDOWS_REPORT", "PRIMARY_KOU_JING",
    "KOU_JING_REPORT", "ScoreFrame", "build_score_frames", "abs_spearman_batch",
    "abs_partial_batch", "make_batch_scorer", "net_sharpe_by_window", "p_at_threshold",
    "nonoverlap_ic", "hold_position", "rolling_percentile", "_slice",
    # ⑦ 净新码
    "FAMILY_ALPHA", "W_MAIN", "W_SENS", "B_DIMS", "B_GRID_ZW", "B_GRID_K",
    "A_SERIES", "A_GRID_K", "A_BURN_IN", "ACTIVE_FUND_TYPES", "INDEX_CODES",
    "FIRST_CLOSE", "b_variant_grid", "a_variant_grid", "family_min_shift",
    "rolling_beta_spread", "dimension_returns", "expanding_percentile",
    "p95_eff_date", "step_to_daily", "board_flags", "a2_ratio_from_rows",
    "a1_median_from_positions", "centered", "build_beta_daily", "build_quarterly",
    "build_b_signals", "build_a_signals", "run_family", "evaluate_family_gates",
    "build_diagnostics", "build_sensitivity_panels", "run_probe", "main",
]

# ---------------------------------------------------------------- 预登记常量（不得扩）
FAMILY_ALPHA = 0.025            # §B.13 族级 Bonferroni（两族 → 0.05/2）
COST_BPS = 3.0

IDX_FUND = "885000.WI"          # 偏股基金指数（Wind）
IDX_300 = "000300.SH"
IDX_1000 = "000852.SH"
IDX_GROWTH = "399370.SZ"        # 国证成长（§E 冻结）
IDX_VALUE = "399371.SZ"         # 国证价值（§E 冻结）
INDEX_CODES = (IDX_FUND, IDX_300, IDX_1000, IDX_GROWTH, IDX_VALUE)
# §A/E 冻结的库内首日锚点：SQL 宇宙被改动/数据被污染时先炸
FIRST_CLOSE = {IDX_FUND: "2015-01-05", IDX_300: "2014-01-02", IDX_1000: "2013-03-06",
               IDX_GROWTH: "2013-01-04", IDX_VALUE: "2013-01-04"}

W_MAIN = 240                    # §B.1：W=240 固定（信达 20220916 先验）
W_SENS = 120                    # §B.1：仅敏感性报告列，不进选优
B_DIMS = ("size", "gv")
B_GRID_ZW = (250, 500)
B_GRID_K = (5, 10, 20, 40)

A_SERIES = ("A1", "A2")
A_GRID_K = (20, 40, 60)
A_BURN_IN = 8                   # §B.9：expanding 分位 burn-in 8 期
PIT_PLUS_DAYS = 30              # §B.10：敏感性口径 = 报告期末 +30 自然日
# §B.7：主动权益两类（只读参考 stock_selector fund_selection/picker.py:48，含"基金"后缀变体）
ACTIVE_FUND_TYPES = frozenset({"普通股票型", "偏股混合型", "普通股票型基金", "偏股混合型基金"})

OUT_DIR = ROOT / "backtest" / "output"
POSITION_CSV = OUT_DIR / "fund_stock_position.csv"       # §C 全量档缓存（永久资产）
BETA_CACHE = OUT_DIR / "fund_crowding_beta_daily.csv"    # ⑦B β 序列（永久资产）
QUARTERLY_CACHE = OUT_DIR / "fund_crowding_quarterly.csv"  # ⑦A 期级序列（永久资产）

A2_ANCHOR_PERIOD = "2020-12-31"   # A2 SQL↔pandas 交叉验证锚点期（建缓存时执行）
FIRST_REPORT_DATE = "2016-03-31"  # §A：41 期连续起点
MIN_PERIODS = 41


def b_variant_grid() -> list[tuple]:
    """⑦B 预登记 16 点（dim × W=240 × zw × k），顺序固定；k 恒在 [3]（机器约定）。"""
    return [(dim, W_MAIN, zw, k) for dim in B_DIMS for zw in B_GRID_ZW for k in B_GRID_K]


def a_variant_grid() -> list[tuple]:
    """⑦A 预登记 6 点（序列 × k），顺序固定；k 恒在 [3]。"""
    return [(ser, "expanding", "annP95", k) for ser in A_SERIES for k in A_GRID_K]


def family_min_shift(grid_k: tuple[int, ...]) -> int:
    """§B.13 规矩 3：`min_shift = 2·max(k)`（⑦B=80，⑦A=120）。"""
    return 2 * max(grid_k)


# ---------------------------------------------------------------- 纯函数：⑦B β 与分位
def rolling_beta_spread(y: pd.Series, x_mkt: pd.Series, x_spr: pd.Series,
                        window: int) -> pd.DataFrame:
    """滚动 OLS `y = α + β_m·x_mkt + β_s·x_spr` 的 (β_s, β_m)（满窗 min_periods=window）。

    二元回归的滚动矩闭式解（对每个窗与逐窗 `lstsq` 逐位一致，判例验到 1e-8）：
        β = [Var(m)·Cov(s,y) − Cov(m,s)·Cov(m,y)] / det，det = Var(m)·Var(s) − Cov(m,s)²
    det ≤ 0（共线/退化）→ NaN，不静默降级。三序列先按交集对齐再 dropna。
    """
    df = pd.concat([y.rename("y"), x_mkt.rename("m"), x_spr.rename("s")],
                   axis=1, join="inner").dropna()
    r = df.rolling(window, min_periods=window)
    e_y, e_m, e_s = r["y"].mean(), r["m"].mean(), r["s"].mean()

    def _em(a: str, b: str) -> pd.Series:
        return (df[a] * df[b]).rolling(window, min_periods=window).mean()

    v_mm = _em("m", "m") - e_m * e_m
    v_ss = _em("s", "s") - e_s * e_s
    c_ms = _em("m", "s") - e_m * e_s
    c_my = _em("m", "y") - e_m * e_y
    c_sy = _em("s", "y") - e_s * e_y
    det = v_mm * v_ss - c_ms ** 2
    beta_s = ((v_mm * c_sy - c_ms * c_my) / det).where(det > 0)
    beta_m = ((v_ss * c_my - c_ms * c_sy) / det).where(det > 0)
    return pd.DataFrame({"beta_s": beta_s, "beta_m": beta_m}, index=df.index)


def dimension_returns(closes: pd.DataFrame, dim: str
                      ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """某维度的 (r_fund, r_mkt, spread)：三腿收盘价先交集再 pct_change（§B.2 原文）。

    - size 维：`spread = r_000852 − r_000300`，`r_mkt = (r_000300 + r_000852)/2`；
    - growth-value 维：`spread = r_399370 − r_399371`，`r_mkt = 两腿均值`。
    """
    legs = {"size": (IDX_300, IDX_1000), "gv": (IDX_GROWTH, IDX_VALUE)}[dim]
    j = closes[[IDX_FUND, legs[0], legs[1]]].dropna()
    rets = j.pct_change().dropna()
    spread = ((rets[legs[1]] - rets[legs[0]]) if dim == "size"
              else (rets[IDX_GROWTH] - rets[IDX_VALUE]))
    mkt = (rets[legs[0]] + rets[legs[1]]) / 2.0
    return rets[IDX_FUND], mkt, spread


def centered(pct_signal: pd.Series) -> pd.Series:
    """分位信号居中（pct − 0.5）——只用于仓位/方向语义（关3 与 `hold_position` 的
    sign 口径）；IC/置换对单调平移不变，完全不受影响。预登记未指明分位→仓位映射，
    此处取与既有 tanh 信号同款的"高于/低于中位"居中语义（偏离记录条目）。"""
    return pct_signal - 0.5


# ---------------------------------------------------------------- 纯函数：⑦A 分位与 PIT
def expanding_percentile(s: pd.Series, burn_in: int = A_BURN_IN) -> pd.Series:
    """expanding 平均秩分位（与 `dashboard.data.rolling_percentile` 同款
    `(less + 0.5·(equal+1)) / n ∈ (0,1]` 口径）；前 `burn_in` 期 = NaN（§B.9）。"""
    if s.isna().any():
        raise ValueError("expanding_percentile 输入不得含 NaN（A1/A2 实测无 NULL）")
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(burn_in, len(vals)):
        w = vals[: i + 1]
        less = int(np.sum(w < vals[i]))
        equal = int(np.sum(w == vals[i]))
        out[i] = (less + 0.5 * (equal + 1)) / len(w)
    return pd.Series(out, index=s.index)


def p95_eff_date(ann_dates) -> pd.Timestamp:
    """当期样本 `ann_date` 的 P95 生效日（§B.10 主口径）。

    取**实际观测日**中最小的满足"累计占比 ≥ 95%"者（PG `percentile_disc(0.95)` 同语义，
    = 排序后 0-based 下标 `ceil(0.95·n) − 1`）——不插值，保证 ≥95% 样本已披露（PIT 保守）。
    """
    a = np.sort(pd.to_datetime(pd.Series(list(ann_dates))).dropna().to_numpy())
    if len(a) == 0:
        raise ValueError("p95_eff_date：空样本")
    return pd.Timestamp(a[int(np.ceil(0.95 * len(a))) - 1])


def step_to_daily(values: pd.Series, eff_dates: pd.Series,
                  calendar: pd.DatetimeIndex) -> pd.Series:
    """期级值 → 日频阶梯：各期自生效日起 ffill 到下一期生效日前（§B.10）。

    burn-in 期（值为 NaN）不产生台阶。**生效日乱序的 PIT 语义（偏离记录 C）**：
    任一日 t 显示的是"生效日 ≤ t 的期中 report_date 最大者"——即**新期一经生效即取代旧期，
    旧期若在更新期生效之后才生效则永不显示**。库内实测恰有一例：2019Q4 年报 P95 =
    2020-04-30（疫情延迟）晚于 2020Q1 季报 P95 = 2020-04-22 → 2019Q4 的台阶被 2020Q1
    抢先取代、从不出现在日频序列里。反向规则（按生效日顺序覆盖 = 旧数据盖新数据）
    经济上不可辩护，故不采。期序输入 = `values` 的行序（report_date 升序）。
    """
    eff = pd.DatetimeIndex(pd.to_datetime(eff_dates.to_numpy()))
    vals = pd.Series(values.to_numpy(dtype=float), index=eff)
    vals = vals[vals.notna()]
    if len(vals) == 0:
        return pd.Series(np.nan, index=calendar)
    # 只保留"生效日严格早于其后所有期生效日"的期（后缀最小值过滤 = as-of by report_date）
    eff_arr = vals.index.to_numpy()
    keep = np.ones(len(vals), dtype=bool)
    running_min = eff_arr[-1]
    for i in range(len(vals) - 2, -1, -1):
        if eff_arr[i] >= running_min:
            keep[i] = False
        else:
            running_min = eff_arr[i]
    vals = vals[keep]
    assert vals.index.is_monotonic_increasing and not vals.index.has_duplicates
    union = calendar.union(vals.index)
    return vals.reindex(union).ffill().reindex(calendar)


# ---------------------------------------------------------------- 纯函数：⑦A 口径镜像
def board_flags(ts_code: pd.Series) -> pd.DataFrame:
    """A2 板块归类（§B.8 冻结字面口径）的 pandas 镜像（SQL FILTER 的交叉验证 + 判例）。

    - `in_den`（全部 A 股）= 后缀 `.SH` 或 `.SZ`（港股/北交所行剔除；689 等非冻结前缀
      只进分母不进分子）；
    - `in_num`（创业板+科创板）= `300/301` 前缀且 `.SZ`，或 `688` 前缀且 `.SH`。
    """
    code = ts_code.astype(str)
    in_den = code.str.endswith(".SZ") | code.str.endswith(".SH")
    in_num = ((code.str.startswith(("300", "301")) & code.str.endswith(".SZ"))
              | (code.str.startswith("688") & code.str.endswith(".SH")))
    return pd.DataFrame({"in_num": in_num, "in_den": in_den}, index=ts_code.index)


def a2_ratio_from_rows(rows: pd.DataFrame) -> tuple[float, float, float]:
    """单期重仓行 → (创业+科创 hold_value, 全 A hold_value, 占比)：SQL 聚合的镜像。"""
    flags = board_flags(rows["ts_code"])
    hv = pd.to_numeric(rows["hold_value"], errors="coerce").astype(float)
    num = float(hv[flags["in_num"]].sum())
    den = float(hv[flags["in_den"]].sum())
    return num, den, (num / den if den > 0 else float("nan"))


def a1_median_from_positions(pos: pd.DataFrame, active_codes: set[str]) -> pd.DataFrame:
    """A1 期级聚合（§B.7）：主动权益过滤后每期 `stock_to_nav_pct` 中位数 + P95 生效日。

    >100 保留原值不截断；A/C 份额不去重（CSV 行即样本）；`ann_date` 复用 CSV 内
    同 (fund_code, report_date) 行。输出按 report_date 索引：
    `a1_median / a1_n_funds / a1_eff_date`。
    """
    sub = pos[pos["fund_code"].isin(active_codes)]
    if sub.empty:
        raise ValueError("A1：主动权益过滤后为空——fund_meta 过滤集或 CSV 有异")
    grp = sub.groupby("report_date")
    out = pd.DataFrame({
        "a1_median": grp["stock_to_nav_pct"].median().astype(float),
        "a1_n_funds": grp["fund_code"].size().astype(int),
        "a1_eff_date": grp["ann_date"].apply(p95_eff_date),
    })
    return out.sort_index()


# ---------------------------------------------------------------- PG（只读，SQL 端聚合）
def _open(db: dict):
    """房规连接（`backtest.data._connect`，自带 3 次快速重试后抛出）+ 120 s 语句超时。"""
    conn = _connect(db)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='120s'")
    return conn


def _load_index_closes(db: dict) -> pd.DataFrame:
    """五腿收盘价宽表（~1.6 万行）+ §A/E 首日锚点断言。"""
    conn = _open(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT index_code, trade_date, close FROM {db['schema']}.index_daily
                    WHERE index_code = ANY(%s) ORDER BY trade_date""",
                (list(INDEX_CODES),))
            rows = cur.fetchall()
    finally:
        conn.close()
    wide = (pd.DataFrame(rows, columns=["code", "date", "close"])
            .pivot(index="date", columns="code", values="close").astype(float))
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    for code, first in FIRST_CLOSE.items():
        if code not in wide.columns:
            raise ValueError(f"index_daily 缺腿 {code}")
        got = str(wide[code].dropna().index[0].date())
        if got != first:
            raise ValueError(f"腿 {code} 首日 {got} ≠ 冻结锚点 {first}（§A/E）")
    return wide


def _load_active_fund_codes(db: dict) -> set[str]:
    """主动权益 fund_code 集（`fund_meta.fund_type ∈ ACTIVE_FUND_TYPES`，只读）。"""
    conn = _open(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT fund_code FROM {db['schema']}.fund_meta
                    WHERE fund_type = ANY(%s)""", (sorted(ACTIVE_FUND_TYPES),))
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


_A2_SQL = """
SELECT h.report_date,
       COUNT(DISTINCT h.fund_code)                                    AS a2_n_funds,
       SUM(h.hold_value) FILTER (WHERE h.ts_code LIKE '300%%.SZ'
                                    OR h.ts_code LIKE '301%%.SZ'
                                    OR h.ts_code LIKE '688%%.SH')::float8 AS a2_num_value,
       SUM(h.hold_value) FILTER (WHERE h.ts_code LIKE '%%.SH'
                                    OR h.ts_code LIKE '%%.SZ')::float8   AS a2_den_value
FROM {schema}.fund_stock_holdings h
JOIN {schema}.fund_meta m USING (fund_code)
WHERE m.fund_type = ANY(%s)
GROUP BY h.report_date ORDER BY h.report_date
"""

_A2_EFF_SQL = """
SELECT t.report_date,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY t.ann_date) AS a2_eff_date
FROM (SELECT DISTINCT h.fund_code, h.report_date, h.ann_date
      FROM {schema}.fund_stock_holdings h
      JOIN {schema}.fund_meta m USING (fund_code)
      WHERE m.fund_type = ANY(%s)) t
GROUP BY t.report_date ORDER BY t.report_date
"""


def _load_a2_quarterly(db: dict) -> pd.DataFrame:
    """A2 期级聚合（库端 SQL，客户端只收 41 行）+ 生效日 P95（percentile_disc）。"""
    types = sorted(ACTIVE_FUND_TYPES)
    conn = _open(db)
    try:
        with conn.cursor() as cur:
            cur.execute(_A2_SQL.format(schema=db["schema"]), (types,))
            agg = pd.DataFrame(cur.fetchall(),
                               columns=["report_date", "a2_n_funds",
                                        "a2_num_value", "a2_den_value"])
            cur.execute(_A2_EFF_SQL.format(schema=db["schema"]), (types,))
            eff = pd.DataFrame(cur.fetchall(), columns=["report_date", "a2_eff_date"])
    finally:
        conn.close()
    out = agg.merge(eff, on="report_date", how="inner")
    out["report_date"] = pd.to_datetime(out["report_date"])
    out["a2_eff_date"] = pd.to_datetime(out["a2_eff_date"])
    out["a2_ratio"] = out["a2_num_value"].astype(float) / out["a2_den_value"].astype(float)
    return out.set_index("report_date").sort_index()


def _crosscheck_a2_anchor(db: dict, sql_row: pd.Series) -> None:
    """A2 锚点期 SQL↔pandas 交叉验证（建缓存时执行；唯一一处行级 fetch，~5 万行）。"""
    conn = _open(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT h.fund_code, h.ts_code, h.hold_value
                    FROM {db['schema']}.fund_stock_holdings h
                    JOIN {db['schema']}.fund_meta m USING (fund_code)
                    WHERE m.fund_type = ANY(%s) AND h.report_date = DATE %s""",
                (sorted(ACTIVE_FUND_TYPES), A2_ANCHOR_PERIOD))
            rows = pd.DataFrame(cur.fetchall(),
                                columns=["fund_code", "ts_code", "hold_value"])
    finally:
        conn.close()
    num, den, ratio = a2_ratio_from_rows(rows)
    if abs(ratio - float(sql_row["a2_ratio"])) > 1e-9:
        raise ValueError(f"A2 锚点期 {A2_ANCHOR_PERIOD} SQL↔pandas 占比不一致："
                         f"pandas {ratio:.10f} vs SQL {float(sql_row['a2_ratio']):.10f}")
    n_funds = int(rows["fund_code"].nunique())
    if n_funds != int(sql_row["a2_n_funds"]):
        raise ValueError(f"A2 锚点期 {A2_ANCHOR_PERIOD} 基金数不一致："
                         f"pandas {n_funds} vs SQL {int(sql_row['a2_n_funds'])}")


# ---------------------------------------------------------------- 缓存构建（永久资产）
def build_beta_daily(db=None, force: bool = False) -> pd.DataFrame:
    """⑦B β 日频序列缓存：2 维 × W∈{240(主), 120(敏感性)} 的 β_s/β_m 宽表。"""
    if BETA_CACHE.exists() and not force:
        d = pd.read_csv(BETA_CACHE, parse_dates=["date"]).set_index("date")
        need = [f"beta_s_{dim}_w{w}" for dim in B_DIMS for w in (W_MAIN, W_SENS)]
        missing = [c for c in need if c not in d.columns]
        if missing:
            raise ValueError(f"β 缓存缺列 {missing}，请 --rebuild-cache")
        return d
    db = db or load_db_config()
    t0 = time.time()
    closes = _load_index_closes(db)
    cols = {}
    for dim in B_DIMS:
        y, mkt, spr = dimension_returns(closes, dim)
        for w in (W_MAIN, W_SENS):
            b = rolling_beta_spread(y, mkt, spr, w)
            cols[f"beta_s_{dim}_w{w}"] = b["beta_s"]
            cols[f"beta_m_{dim}_w{w}"] = b["beta_m"]
    d = pd.DataFrame(cols).sort_index()
    d = d[d.notna().any(axis=1)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d.rename_axis("date").reset_index().to_csv(BETA_CACHE, index=False)
    print(f"[cache] fund_crowding_beta_daily 建成：{len(d)} 行 / "
          f"{time.time() - t0:.1f} s → {BETA_CACHE}")
    return d


def build_quarterly(db=None, force: bool = False) -> pd.DataFrame:
    """⑦A 期级序列缓存：A1 中位数 + A2 板块占比 + 生效日（P95 与 +30 天）+ expanding 分位。"""
    if QUARTERLY_CACHE.exists() and not force:
        q = pd.read_csv(QUARTERLY_CACHE,
                        parse_dates=["report_date", "a1_eff_date", "a2_eff_date",
                                     "eff_plus30"]).set_index("report_date")
        _check_quarterly(q)
        return q
    db = db or load_db_config()
    t0 = time.time()
    pos = pd.read_csv(POSITION_CSV, parse_dates=["report_date", "ann_date"])
    active = _load_active_fund_codes(db)
    n_missing_meta = int((~pos["fund_code"].isin(active)).sum())
    a1 = a1_median_from_positions(pos, active)
    a2 = _load_a2_quarterly(db)
    _crosscheck_a2_anchor(db, a2.loc[pd.Timestamp(A2_ANCHOR_PERIOD)])
    q = a1.join(a2[["a2_n_funds", "a2_num_value", "a2_den_value", "a2_ratio",
                    "a2_eff_date"]], how="inner")
    q["eff_plus30"] = q.index + pd.Timedelta(days=PIT_PLUS_DAYS)
    q["a1_pct"] = expanding_percentile(q["a1_median"])
    q["a2_pct"] = expanding_percentile(q["a2_ratio"])
    q.attrs["n_rows_not_in_active_meta"] = n_missing_meta
    _check_quarterly(q)
    q.rename_axis("report_date").reset_index().to_csv(QUARTERLY_CACHE, index=False)
    print(f"[cache] fund_crowding_quarterly 建成：{len(q)} 期 / "
          f"{time.time() - t0:.1f} s → {QUARTERLY_CACHE}")
    return q


def _check_quarterly(q: pd.DataFrame) -> None:
    if len(q) < MIN_PERIODS:
        raise ValueError(f"⑦A 期数 {len(q)} < 冻结实测 {MIN_PERIODS}（§A）")
    first = str(q.index[0].date())
    if first != FIRST_REPORT_DATE:
        raise ValueError(f"⑦A 首期 {first} ≠ 冻结实测 {FIRST_REPORT_DATE}（§A）")


# ---------------------------------------------------------------- 信号装配
def build_b_signals(beta: pd.DataFrame, w: int = W_MAIN) -> dict[tuple, pd.Series]:
    """⑦B 16 个变体信号（k 不改信号本身，同 (dim,zw) 的四个 k 共用同一条序列）。"""
    base = {(dim, zw): rolling_percentile(beta[f"beta_s_{dim}_w{w}"].dropna(), zw)
            for dim in B_DIMS for zw in B_GRID_ZW}
    return {(dim, w, zw, k): base[(dim, zw)]
            for dim in B_DIMS for zw in B_GRID_ZW for k in B_GRID_K}


def build_a_signals(q: pd.DataFrame, calendar: pd.DatetimeIndex,
                    pit: str = "annP95") -> dict[tuple, pd.Series]:
    """⑦A 6 个变体信号：expanding 分位 → 生效日阶梯（pit ∈ {annP95, plus30}）。"""
    eff_cols = ({"A1": "a1_eff_date", "A2": "a2_eff_date"} if pit == "annP95"
                else {"A1": "eff_plus30", "A2": "eff_plus30"})
    base = {ser: step_to_daily(q[f"{ser.lower()}_pct"], q[eff_cols[ser]], calendar)
            for ser in A_SERIES}
    return {(ser, "expanding", pit, k): base[ser]
            for ser in A_SERIES for k in A_GRID_K}


# ---------------------------------------------------------------- 分族机器（⓪ 接入）
def family_halves(common_sel: pd.DatetimeIndex) -> dict[str, tuple[str, str]]:
    """关2 两半窗（§B.14：按各族自身选择样本切半）：前 n//2 天 / 其余。"""
    n = len(common_sel)
    split = n // 2
    return {"half1": (str(common_sel[0].date()), str(common_sel[split - 1].date())),
            "half2": (str(common_sel[split].date()), str(common_sel[-1].date()))}


def build_family_panel(variants: list[tuple], signals: dict[tuple, pd.Series],
                       und: dict[str, pd.Series], common: pd.DatetimeIndex,
                       halves: dict[str, tuple[str, str]]) -> pd.DataFrame:
    """变体 × 三口径 × 五窗 + 两半窗的**有符号** IC 面板（房规 `nonoverlap_ic`）。"""
    windows = {**WINDOWS_REPORT, **halves}
    rows = []
    for v in variants:
        fam, mode, zw, k = v
        sig = signals[v].reindex(common)
        for kj, u in und.items():
            row = {"family": fam, "mode": str(mode), "zw": str(zw), "k": k,
                   "kou_jing": kj}
            for wname, win in windows.items():
                ic, n = nonoverlap_ic(_slice(sig, win), _slice(u, win), k)
                row[f"ic_{wname}"] = ic
                row[f"n_{wname}"] = n
            rows.append(row)
    return pd.DataFrame(rows)


def run_family(name: str, variants: list[tuple], signals: dict[tuple, pd.Series],
               grid_k: tuple[int, ...], und: dict[str, pd.Series], ew: pd.Series,
               carry: pd.Series, n_perm: int, seed: int,
               cost_bps: float = COST_BPS) -> dict:
    """单族端到端：共用索引 → 合成单次 ⓪ 机器（×2 统计量）→ 三关判定。"""
    common = None
    for v in variants:
        idx = signals[v].dropna().index
        common = idx if common is None else common.intersection(idx)
    common = (common.intersection(und[PRIMARY_KOU_JING].index)
              .intersection(ew.dropna().index).sort_values())
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]
    n_obs = len(common_sel)
    min_shift = family_min_shift(grid_k)
    max_shift = n_obs - min_shift
    halves = family_halves(common_sel)
    meta = {"family_group": name, "n_obs": n_obs, "min_shift": min_shift,
            "max_shift": max_shift, "first_date": str(common_sel[0].date()),
            "last_date": str(common_sel[-1].date()),
            "half_split": {k: list(v) for k, v in halves.items()}}

    frames = build_score_frames(und[PRIMARY_KOU_JING], ew, common_sel,
                                grid_k=tuple(grid_k))
    arrs = {v: signals[v].reindex(common_sel).to_numpy(dtype=float) for v in variants}
    res_ic = selection_permutation_test(
        variants, n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="abs_nonoverlap_ic", meta=meta)
    res_pic = selection_permutation_test(
        variants, n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_partial_batch),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="abs_partial_ic_vs_equal_weight", meta=meta)

    panel = build_family_panel(variants, signals,
                               {k: v.reindex(common).dropna() for k, v in und.items()},
                               common, halves)
    winner = res_ic.best_variant
    j_win = res_ic.best_index

    # 运行期口径校验：向量化统计量 == 房规 nonoverlap_ic 的 |·|（selection 窗、blend）
    panel_abs = (panel[panel["kou_jing"] == PRIMARY_KOU_JING]
                 .set_index(["family", "mode", "zw", "k"])["ic_selection_2014_2023"].abs())
    key = [(v[0], str(v[1]), str(v[2]), v[3]) for v in variants]
    obs_check = float(np.nanmax(np.abs(
        np.array([panel_abs.loc[kk] for kk in key]) - res_ic.observed)))
    if not (obs_check < 1e-9):
        raise AssertionError(f"向量化 IC 与房规 nonoverlap_ic 不一致：max|diff|={obs_check:.3e}")

    # 关1b：赢家的**有符号**偏 IC + 在关1b 零分布上的保守 p（面2 同款读法）
    sf = frames[winner[3]]
    x = signals[winner].reindex(common_sel).to_numpy(dtype=float)[None, sf.pts]
    rx = rankdata(x, axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    beta = (rx @ sf.ctrl_rank_c) / sf.ctrl_ss
    rs = rx - beta[:, None] * sf.ctrl_rank_c[None, :]
    den = np.sqrt((rs * rs).sum(axis=1) * sf.fwd_resid_ss)
    pic_signed = float((rs @ sf.fwd_resid)[0] / den[0]) if den[0] >= 1e-9 else 0.0
    p_pic_at_winner = p_at_threshold(res_pic.null_selected, res_pic.observed[j_win])

    wrow = panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                 & (panel["family"] == winner[0]) & (panel["mode"] == str(winner[1]))
                 & (panel["zw"] == str(winner[2])) & (panel["k"] == winner[3])].iloc[0]
    ic_sel_signed = float(wrow["ic_selection_2014_2023"])
    direction = float(np.sign(ic_sel_signed))

    # 关3 + 报告：居中信号（pct−0.5）交房规 hold/run_strategy（IC 不受居中影响）
    csig = centered(signals[winner]).reindex(common).dropna()
    sharpe_by_win = net_sharpe_by_window(csig, direction, winner[3],
                                         und[PRIMARY_KOU_JING], carry, cost_bps)
    verdict = evaluate_family_gates(name, winner, wrow, res_ic, res_pic, pic_signed,
                                    p_pic_at_winner, sharpe_by_win)

    # 集中度摘要（报告列）：赢家选择窗净收益序列
    s_sel = _slice(csig, SELECTION)
    pos = hold_position(s_sel * direction, winner[3])
    from backtest.engine import run_strategy
    strat = run_strategy(pos, und[PRIMARY_KOU_JING].reindex(pos.index), cost_bps,
                         carry.reindex(pos.index))
    conc = concentration_summary(strat["ret"].dropna())

    overall = bool(verdict[verdict["gate"] == "OVERALL"]["pass"].iloc[0])
    summary = {
        "verdict": "GO" if overall else "STOP",
        "winner_variant": {"family": winner[0], "mode": str(winner[1]),
                           "zw": str(winner[2]), "k": winner[3]},
        "gate1_ic_machine": res_ic.summary(),
        "gate1b_partial_ic_machine": {
            **res_pic.summary(),
            "observed_at_gate1_winner": float(res_pic.observed[j_win]),
            "p_selected_at_gate1_winner": p_pic_at_winner,
            "partial_ic_signed_at_gate1_winner": pic_signed,
            "same_sign_as_ic": bool(np.sign(pic_signed) == direction
                                    and pic_signed != 0.0),
        },
        "null_selected_quantiles": {
            nm: {f"p{q}": float(np.percentile(r.null_selected[np.isfinite(r.null_selected)], q))
                 for q in (5, 25, 50, 75, 95, 99)}
            for nm, r in (("ic", res_ic), ("partial_ic", res_pic))
        },
        "winner_ic_by_window": {w: float(wrow[f"ic_{w}"])
                                for w in (list(WINDOWS_REPORT) + ["half1", "half2"])},
        "winner_net_by_window": sharpe_by_win,
        "concentration_summary": {k2: (str(v2) if isinstance(v2, pd.Timestamp) else v2)
                                  for k2, v2 in conc.items()},
        "run": {**meta, "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
                "family_alpha": FAMILY_ALPHA, "n_variants": len(variants),
                "vectorized_vs_house_ic_max_abs_diff": obs_check},
    }
    perm_frame = pd.concat([
        res_ic.to_frame().assign(machine="abs_nonoverlap_ic", family_group=name),
        res_pic.to_frame().assign(machine="abs_partial_ic_vs_equal_weight",
                                  family_group=name),
    ], ignore_index=True)
    return {"panel": panel.assign(family_group=name), "permutation": perm_frame,
            "verdict": verdict, "summary": summary, "winner": winner,
            "common": common, "common_sel": common_sel, "halves": halves,
            "res_ic": res_ic, "res_pic": res_pic}


def evaluate_family_gates(name: str, winner: tuple, wrow: pd.Series, res_ic, res_pic,
                          pic_signed: float, p_pic_at_winner: float,
                          sharpe_by_win: dict) -> pd.DataFrame:
    """§B.14 三关逐条判定（族级 α=0.025），任一不过 → STOP。

    关2 = 两半窗（各族自身选择样本切半）IC 与选择窗方向同号；
    `pass=None` 的行是对照量（`p_naive`），按纪律不得进任何闸门。
    """
    ic_sel = float(wrow["ic_selection_2014_2023"])
    ic_h1 = float(wrow["ic_half1"])
    ic_h2 = float(wrow["ic_half2"])
    direction = float(np.sign(ic_sel))

    g1 = bool(np.isfinite(res_ic.p_selected) and res_ic.p_selected < FAMILY_ALPHA)
    same_sign = bool(pic_signed != 0.0 and np.isfinite(pic_signed)
                     and np.sign(pic_signed) == direction)
    g1b_p = bool(np.isfinite(p_pic_at_winner) and p_pic_at_winner < FAMILY_ALPHA)
    g2_h1 = bool(direction != 0 and np.isfinite(ic_h1) and np.sign(ic_h1) == direction)
    g2_h2 = bool(direction != 0 and np.isfinite(ic_h2) and np.sign(ic_h2) == direction)
    sh_sel = float(sharpe_by_win.get("selection_2014_2023", {}).get("net_sharpe", np.nan))
    g3 = bool(np.isfinite(sh_sel) and sh_sel > 0)
    overall = bool(g1 and same_sign and g1b_p and g2_h1 and g2_h2 and g3)

    rows = [
        {"gate": "1_permutation_significant", "metric": "p_selected",
         "value": res_ic.p_selected, "threshold": FAMILY_ALPHA, "pass": g1},
        {"gate": "1_permutation_significant", "metric": "p_naive_UNCORRECTED(对照·不入闸)",
         "value": res_ic.p_naive, "threshold": float("nan"), "pass": None},
        {"gate": "1b_partial_ic_same_sign", "metric": "partial_ic_signed_at_winner",
         "value": pic_signed, "threshold": 0.0, "pass": same_sign},
        {"gate": "1b_partial_ic_same_sign", "metric": "p_selected_at_winner",
         "value": p_pic_at_winner, "threshold": FAMILY_ALPHA, "pass": g1b_p},
        {"gate": "2_stable_halves", "metric": "ic_half1",
         "value": ic_h1, "threshold": 0.0, "pass": g2_h1},
        {"gate": "2_stable_halves", "metric": "ic_half2",
         "value": ic_h2, "threshold": 0.0, "pass": g2_h2},
        {"gate": "3_net_positive", "metric": "net_sharpe_selection_2014_2023",
         "value": sh_sel, "threshold": 0.0, "pass": g3},
        {"gate": "OVERALL", "metric": "GO(三关全过)" if overall else "STOP(任一关不过)",
         "value": float(overall), "threshold": float("nan"), "pass": overall},
    ]
    return pd.DataFrame(rows).assign(family_group=name)


# ---------------------------------------------------------------- 诊断与敏感性（不设闸）
def build_diagnostics(b_signals: dict[tuple, pd.Series], a_signals: dict[tuple, pd.Series],
                      beta: pd.DataFrame, q: pd.DataFrame, pairs: pd.DataFrame,
                      ew: pd.Series, b_common_sel: pd.DatetimeIndex,
                      a_common_sel: pd.DatetimeIndex) -> pd.DataFrame:
    """§B.15 报告列：corr(⑦B, 四对+ew) / corr(⑦B 两维, ⑦A) / 期级 corr(A1, A2)。"""
    rows = []

    def _pair(name_a, a, name_b, b, kind, window_idx):
        j = pd.concat([a.rename("a"), b.rename("b")], axis=1, join="inner").dropna()
        if window_idx is not None:
            j = j[j.index.isin(window_idx)]
        if len(j) < 12:
            return
        rows.append({"kind": kind, "left": name_a, "right": name_b,
                     "pearson": float(j["a"].corr(j["b"])),
                     "spearman": float(j["a"].corr(j["b"], method="spearman")),
                     "n_obs": int(len(j))})

    b_series = {(dim, zw): b_signals[(dim, W_MAIN, zw, B_GRID_K[0])]
                for dim in B_DIMS for zw in B_GRID_ZW}
    # corr(⑦B, 现役四对信号 + equal_weight)（限 ⑦B 选择样本）
    for (dim, zw), sig in b_series.items():
        for col in pairs.columns:
            _pair(f"signal_B_{dim}_zw{zw}", sig, f"pair_{col}", pairs[col],
                  "b_signal_vs_pair", b_common_sel)
        _pair(f"signal_B_{dim}_zw{zw}", sig, "equal_weight_factor", ew,
              "b_signal_vs_production", b_common_sel)
    # ⑦B 两维水平量互相关（β_s 层）
    _pair("beta_s_size_w240", beta["beta_s_size_w240"], "beta_s_gv_w240",
          beta["beta_s_gv_w240"], "b_level_overlap", b_common_sel)
    # corr(⑦B, ⑦A 两序列)（§B.15 原文全 ⑦B 两维；限 ⑦A 选择样本；日频信号层。
    # QA F1 返修：首版只交付 size 维 4 行，此处补齐 gv 维同法 4 行）
    a_series = {ser: a_signals[(ser, "expanding", "annP95", A_GRID_K[0])]
                for ser in A_SERIES}
    for dim in B_DIMS:
        for zw in B_GRID_ZW:
            for ser, asig in a_series.items():
                _pair(f"signal_B_{dim}_zw{zw}", b_series[(dim, zw)],
                      f"signal_{ser}", asig, f"b_{dim}_vs_a_signal", a_common_sel)
    # ⑦A 期级互相关（raw 与分位各一版；期级样本量 ~41/33，如实报 n）
    _pair("A1_raw_median", q["a1_median"], "A2_raw_ratio", q["a2_ratio"],
          "a_quarterly_overlap_raw", None)
    _pair("A1_pct", q["a1_pct"].dropna(), "A2_pct", q["a2_pct"].dropna(),
          "a_quarterly_overlap_pct", None)
    return pd.DataFrame(rows)


def build_sensitivity_panels(beta: pd.DataFrame, q: pd.DataFrame,
                             und_blend: pd.Series, ew: pd.Series) -> pd.DataFrame:
    """两组敏感性报告列（不进选优）：⑦B W=120 的 16 点面板 + ⑦A PIT=+30 天的 6 点面板。

    各自在**自己的**共用索引上算（W=120 β 起点更早 / +30 生效日更晚），blend 口径，
    五窗有符号 IC；只报告，无任何选择或闸门。
    """
    rows = []

    def _panel(tag, variants, signals):
        common = None
        for v in variants:
            idx = signals[v].dropna().index
            common = idx if common is None else common.intersection(idx)
        common = (common.intersection(und_blend.index)
                  .intersection(ew.dropna().index).sort_values())
        for v in variants:
            fam, mode, zw, k = v
            sig = signals[v].reindex(common)
            row = {"kind": tag, "family": fam, "mode": str(mode), "zw": str(zw), "k": k,
                   "first_date": str(common[0].date()), "last_date": str(common[-1].date())}
            for wname, win in WINDOWS_REPORT.items():
                ic, n = nonoverlap_ic(_slice(sig, win), _slice(und_blend, win), k)
                row[f"ic_{wname}"] = ic
                row[f"n_{wname}"] = n
            rows.append(row)

    w120_variants = [(dim, W_SENS, zw, k)
                     for dim in B_DIMS for zw in B_GRID_ZW for k in B_GRID_K]
    _panel("B_w120_sensitivity", w120_variants, build_b_signals(beta, w=W_SENS))
    plus30_variants = [(ser, "expanding", "plus30", k)
                       for ser in A_SERIES for k in A_GRID_K]
    _panel("A_pit_plus30_sensitivity", plus30_variants,
           build_a_signals(q, und_blend.index, pit="plus30"))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 编排
def run_probe(n_perm: int = 1000, seed: int = 0, cost_bps: float = COST_BPS, db=None,
              force_cache: bool = False) -> dict:
    """端到端：两缓存 → 两族分跑 ⓪ 机器 → 三关 → 诊断/敏感性 → 产物字典。"""
    beta = build_beta_daily(db=db, force=force_cache)
    q = build_quarterly(db=db, force=force_cache)
    ew = _load_ew_signal()
    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    carry = load_carry(PRIMARY_KOU_JING, db=db)

    b_signals = build_b_signals(beta, w=W_MAIN)
    a_signals = build_a_signals(q, und[PRIMARY_KOU_JING].index, pit="annP95")

    fam_b = run_family("B", b_variant_grid(), b_signals, B_GRID_K, und, ew, carry,
                       n_perm, seed, cost_bps)
    fam_a = run_family("A", a_variant_grid(), a_signals, A_GRID_K, und, ew, carry,
                       n_perm, seed, cost_bps)

    pairs, _ew_rebuilt, max_diff_vs_prod = load_pair_signals(db=db)
    diagnostics = build_diagnostics(b_signals, a_signals, beta, q, pairs, ew,
                                    fam_b["common_sel"], fam_a["common_sel"])
    sensitivity = build_sensitivity_panels(beta, q, und[PRIMARY_KOU_JING], ew)

    summary = {
        "PROBE": "⑦ 公募持仓拥挤面（⑦B 风格暴露 β 分位 16 点 + ⑦A 季报仓位/板块分位 6 点，"
                 "分族合成单次调用，族级 α=0.025）",
        "family_B": fam_b["summary"],
        "family_A": fam_a["summary"],
        "direction_prior_archived": "高拥挤分位 → 风格反向（§B.5 入档；检验双侧，不改闸）",
        "low_power_statement_A": "⑦A 41 期 − burn-in 8 ≈ 33 个独立观测；结论表述限"
                                 "'低功效未检出/检出'，不得写'无价值'（§B.12）",
        "run": {
            "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
            "family_alpha": FAMILY_ALPHA,
            "w_main": W_MAIN, "w_sensitivity": W_SENS,
            "index_codes": list(INDEX_CODES),
            "active_fund_types": sorted(ACTIVE_FUND_TYPES),
            "a_burn_in": A_BURN_IN, "pit_main": "ann_date P95 (percentile_disc)",
            "pit_sensitivity": f"report_date + {PIT_PLUS_DAYS} calendar days",
            "n_quarters": int(len(q)),
            "beta_first_dates": {c: str(beta[c].dropna().index[0].date())
                                 for c in beta.columns if c.startswith("beta_s")},
            "max_abs_diff_rebuilt_ew_vs_production_csv": max_diff_vs_prod,
            "db_host": (db or {}).get("host", "settings.yaml default"),
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门",
        },
    }
    return {"panel": pd.concat([fam_b["panel"], fam_a["panel"]], ignore_index=True),
            "permutation": pd.concat([fam_b["permutation"], fam_a["permutation"]],
                                     ignore_index=True),
            "verdict": pd.concat([fam_b["verdict"], fam_a["verdict"]],
                                 ignore_index=True),
            "diagnostics": diagnostics, "sensitivity": sensitivity,
            "summary": summary, "fam_b": fam_b, "fam_a": fam_a}


def build_metadata(summary: dict) -> pd.DataFrame:
    """裁决 CSV 的 META 行（两族 + 共同运行参数；CSV 单看即可复述结论）。"""
    rows = []
    for fam_key in ("family_B", "family_A"):
        s = summary[fam_key]
        g1, g1b = s["gate1_ic_machine"], s["gate1b_partial_ic_machine"]
        items = {
            "verdict": s["verdict"],
            "winner": str(tuple(s["winner_variant"].values())),
            "winner_abs_ic": g1["observed_best"],
            "p_selected_ic": g1["p_selected"],
            "p_naive_ic_UNCORRECTED": g1["p_naive_UNCORRECTED"],
            "p_min_p_ic": g1["p_min_p"],
            "selection_inflation_ic": g1["selection_inflation"],
            "partial_ic_signed_at_winner": g1b["partial_ic_signed_at_gate1_winner"],
            "p_selected_partial_at_winner": g1b["p_selected_at_gate1_winner"],
            "null_selected_p95_ic": s["null_selected_quantiles"]["ic"]["p95"],
            "n_distinct_null_winners_ic": g1["n_distinct_null_winners"],
            **{f"winner_ic_{k}": v for k, v in s["winner_ic_by_window"].items()},
            **{f"winner_net_sharpe_{k}": v["net_sharpe"]
               for k, v in s["winner_net_by_window"].items()},
            **{f"run_{k}": v for k, v in s["run"].items()
               if k in ("n_obs", "min_shift", "max_shift", "first_date", "last_date")},
        }
        rows += [{"family_group": fam_key[-1], "gate": "META", "metric": k, "value": v,
                  "threshold": float("nan"), "pass": None} for k, v in items.items()]
    rows += [{"family_group": "共同", "gate": "META", "metric": k, "value": str(v),
              "threshold": float("nan"), "pass": None}
             for k, v in summary["run"].items()]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(
        description="⑦ 公募持仓拥挤面探针（⑦B 16 点 + ⑦A 6 点，分族合成单次调用）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap.add_argument("--db-host", default=None, help="覆盖库主机（主库拒连时走只读备端）")
    ap.add_argument("--rebuild-cache", action="store_true", help="强制重建两份缓存")
    ap.add_argument("--cache-only", action="store_true", help="只建缓存不跑探针")
    args = ap.parse_args()

    db = None
    if args.db_host:
        db = dict(load_db_config())
        db["host"] = args.db_host

    if args.cache_only:
        beta = build_beta_daily(db=db, force=args.rebuild_cache)
        q = build_quarterly(db=db, force=args.rebuild_cache)
        print(f"beta_daily: {len(beta)} 行 {beta.index[0].date()}~{beta.index[-1].date()}")
        print(f"quarterly: {len(q)} 期 {q.index[0].date()}~{q.index[-1].date()}")
        return 0

    out = run_probe(n_perm=args.n_perm, seed=args.seed, cost_bps=args.cost_bps, db=db,
                    force_cache=args.rebuild_cache)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out["panel"].round(6).to_csv(OUT_DIR / "probe_7_fund_crowding_panel.csv", index=False)
    out["permutation"].round(6).to_csv(OUT_DIR / "probe_7_fund_crowding_permutation.csv",
                                       index=False)
    out["diagnostics"].round(6).to_csv(OUT_DIR / "probe_7_fund_crowding_diagnostics.csv",
                                       index=False)
    out["sensitivity"].round(6).to_csv(OUT_DIR / "probe_7_fund_crowding_sensitivity.csv",
                                       index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        OUT_DIR / "probe_7_fund_crowding_verdict.csv", index=False)
    side = OUT_DIR / "probe_7_fund_crowding_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    for name, fam in (("⑦B", out["fam_b"]), ("⑦A", out["fam_a"])):
        s = fam["summary"]
        blend = fam["panel"][fam["panel"]["kou_jing"] == PRIMARY_KOU_JING].copy()
        blend["abs_ic_sel"] = blend["ic_selection_2014_2023"].abs()
        top = blend.sort_values("abs_ic_sel", ascending=False).head(8)
        print(f"\n=== {name} 选择窗 |IC| 前 8（全表见 CSV） ===")
        print(top[["family", "zw", "k", "ic_selection_2014_2023", "ic_half1", "ic_half2",
                   "ic_holdout_2024_2026", "n_selection_2014_2023"]]
              .round(4).to_string(index=False))
        print(f"=== {name} 赢家：{s['winner_variant']} ===")
        for key in ("p_selected", "p_naive_UNCORRECTED", "p_min_p", "observed_best",
                    "selection_inflation"):
            print(f"  [关1] {key}: {s['gate1_ic_machine'][key]}")
        for key in ("partial_ic_signed_at_gate1_winner", "p_selected_at_gate1_winner",
                    "same_sign_as_ic"):
            print(f"  [关1b] {key}: {s['gate1b_partial_ic_machine'][key]}")
        print(fam["verdict"].round(6).to_string(index=False))
        print(pd.DataFrame(s["winner_net_by_window"]).T.round(4).to_string())
        print(f"→ {name} verdict: {s['verdict']}")

    print("\n=== 诊断（§B.15 报告列；全表见 CSV） ===")
    dg = out["diagnostics"]
    print(dg[["kind", "left", "right", "pearson", "spearman", "n_obs"]]
          .round(3).to_string(index=False))
    print(f"\n→ {OUT_DIR}/probe_7_fund_crowding_*.csv\n→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
