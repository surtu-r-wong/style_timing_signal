"""面2 探针：个股截面二阶矩（X1 截面收益离散度 / X2 个股平均相关性；32 点**合成一次**调用）。

预登记出处（冻结，一字不改）：`docs/plans/2026-08-14-probe-mian2-xsection-prereg.md`；
技术前提附录：`docs/plans/2026-08-14-probe-mian2-tech-annex.md`。

> **命题定位**：② `divergence_probe` 测的是**四对配对因子之间**的二阶矩（已 STOP）；
> 本探针测的是当年被"离散度≈广度"**假设**跳过、从未实测的**个股层面**截面二阶矩。
> **X1 与 X2 不是两个独立信息面**（Solnik-Roulet 代数：ρ̄ ≈ 1 − mean(截面方差)/平均个股方差，
> X2 是 X1 被个股波动水平归一化的单调反向变换）—— 故强制报诊断 `corr(X1, X2)`
> （水平量 + 赢家口径信号化后各一版），不许重演"用假设代替实测"。

## 数据与宇宙（预登记 §1，D1/D2）

- 表 `stock_selector.stock_daily_price`（**原表**，不用 `_qfq` 视图：比值对复权缩放不变）；
- 宇宙 = `(ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ') AND close IS NOT NULL
  AND pre_close > 0 AND volume > 0` —— **不含 `.BJ`、不含 `.HK`**（该表自 2025-01-02
  起混入港股，`thermo_probe._ROW_FILTER` 的前缀过滤漏放 49 只；本探针用**后缀**排干净），
  不做市值/流动性/ST/次新任何筛选（零新自由度）；`stock_status` 2023 前极度稀疏，禁用；
- 收益 = `close/pre_close − 1`（`pct_chg` 近端全 NULL，禁用）；**不做**任何异常收益剔除/缩尾；
- SQL 起点 2012-01-01；无 `pit_lag`（当日收盘即知，与 `thermo_probe.py:11-12` 同约定）；
  表内 `available_at` 列**明确不使用**。

## 两族（预登记 §2，D3/D4）

- **X1** = 每日宇宙内个股收益的横截面 `STDDEV_SAMP`（ddof=1，原始有符号收益）；
- **X2** = 方差比隐含平均相关（Solnik & Roulet 2000 / Driessen et al. 2009）：滚动 60 交易日
  （`ROWS BETWEEN 59 PRECEDING`，跨停牌原样接受），**σ_p 用与 σ_i 同一子集**
  （窗内满 60 天的票，`FILTER (WHERE c60=60)`）的等权日收益——否则 ρ̄ 出界。

## 机器接入三规矩（`2026-08-12-selection-permutation-machine.md` §6，违反即作废）

1. **32 点（2 族 × 16 点）合成同一次 `selection_permutation_test` 调用**；
2. **关1 的统计量不携带关2 的同号约束**（无约束 `|非重叠 IC|`，`-inf` 只给无法评分行）；
3. **`min_shift = 2·max(k) = 80`，`max_shift = n_obs − 80`**（n_obs = 2,434）。

## 三关闸门（预登记 §4，全过才 GO）

关1 置换 `p_selected<0.05` **且**关1b 偏 IC（控生产 ew）同号 `p_selected<0.05`；
关2 两半窗同号（train 2014-2020 / val 2021-2023）；关3 成本后净 Sharpe>0（3 bps + blend carry）。

## 大内存红线

**禁止 fetchall 个股长表进内存**（2012 起 ≈2.8 GB 客户端峰值 = OOM 前科路径）：
两族序列全部 SQL 侧聚合，客户端只收 ~3,700 行；PG 用 `connect_timeout=15`
**且显式 `statement_timeout=600000`**，拒连**不滚动重试**。

CLI: python3 -m backtest.xsection_probe [--n-perm 1000] [--seed 0] [--db-host ...]
产出: backtest/output/probe_xsection_{panel,permutation,diagnostics,verdict}.csv
      + probe_xsection_summary.json；缓存 xs_dispersion.csv / avg_correlation.csv

本模块**不改动** `signals/` / `selection_permutation.py` / `rotation_probe.py` /
`leverage_probe.py` / `divergence_probe.py` / `engine.py` / 任何冻结根。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import divergence_probe as _dp  # noqa: E402
from backtest.divergence_probe import (  # noqa: E402  ← 预登记 §3 的 13 个复用名字
    GATE_ALPHA, GRID_K, GRID_LB, GRID_ZW, HOLDOUT, KOU_JING_REPORT, PRIMARY_KOU_JING,
    SELECTION, TRAIN, VAL, WINDOWS_REPORT, ScoreFrame, _slice, abs_partial_batch,
    abs_spearman_batch, build_ic_panel, build_score_frames, build_variant_signals,
    evaluate_gates, load_breadth, make_batch_scorer, net_sharpe_by_window,
    nonoverlap_points, p_at_threshold, run_machines, variant_grid,
    warmup_masked_level_signal,
)
from backtest.rotation_probe import _load_ew_signal  # noqa: E402

# 预登记 §3 的复用面**原样再导出**：面2 与 ② 用的是同一批对象（不是同名副本），
# 判例与下游可以从本模块单一入口拿到全套，任何"另起口径"都会在这里立刻暴露。
__all__ = [
    # —— 预登记 §3 点名的 13 个复用名字（一个不改、一个不少）——
    "variant_grid", "warmup_masked_level_signal", "build_variant_signals", "ScoreFrame",
    "build_score_frames", "abs_spearman_batch", "abs_partial_batch", "make_batch_scorer",
    "build_ic_panel", "run_machines", "p_at_threshold", "net_sharpe_by_window",
    "evaluate_gates",
    # —— 一并复用的窗口/口径常量与小工具 ——
    "SELECTION", "TRAIN", "VAL", "HOLDOUT", "WINDOWS_REPORT", "GATE_ALPHA",
    "PRIMARY_KOU_JING", "KOU_JING_REPORT", "GRID_LB", "GRID_ZW", "GRID_K",
    "nonoverlap_points", "load_breadth", "_slice",
    # —— 面2 净新码 ——
    "FAMILIES", "N_VARIANTS", "SQL_START", "CORR_WINDOW", "UNIVERSE_SQL",
    "ANCHORS_N", "ANCHORS_N_FULL",
    "DISP_CACHE", "CORR_CACHE", "dp_families_bound_to_xsection", "xsection_variant_grid",
    "universe_mask", "xsection_measures", "implied_avg_correlation", "xsection_levels",
    "build_xs_dispersion", "build_avg_correlation", "build_diagnostics", "build_metadata",
    "run_probe", "main",
]

# ---------------------------------------------------------------- 预登记常量（不得扩）
FAMILIES = ("X1", "X2")
N_VARIANTS = len(FAMILIES) * len(GRID_LB) * len(GRID_ZW) * len(GRID_K)   # = 32

SQL_START = "2012-01-01"     # D5：起点；闸门窗 2014-01-02 起满样本
CORR_WINDOW = 60             # D4：X2 的滚动窗（交易日）
STATEMENT_TIMEOUT_MS = 600_000   # D12 硬要求：显式 600 s

DISP_CACHE = ROOT / "backtest" / "output" / "xs_dispersion.csv"
CORR_CACHE = ROOT / "backtest" / "output" / "avg_correlation.csv"

# D1 宇宙：**后缀**过滤（把 .HK/.BJ 真排干净）+ thermo 三条行级有效性条件
UNIVERSE_SQL = """(ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ')
      AND close IS NOT NULL AND pre_close > 0 AND volume > 0"""

# 回归锚点（附录 §1.4 侦察实测的 `.SH+.SZ` 可交易票数）：SQL 宇宙若被改动，这里先炸
ANCHORS_N = {"2014-06-30": 2301, "2018-06-29": 3325, "2026-06-30": 5186}

# X2 侧同款锚点：`c60 = CORR_WINDOW` 的**满窗子集**票数（故严格小于 ANCHORS_N —— 次新股与
# 长停牌票被 c60 硬约束剔掉）。取数口径与 `_CORR_SQL` 逐字一致（全回看，不截断 trade_date
# 下界；截断会把长停牌票的 60 **行**窗切断而系统性少数，2014/2018 尤甚）。
# 溯源：2026-08-18 独立 SQL 现取（非读缓存自证），与当时的 `avg_correlation.csv` 3/3 相等。
ANCHORS_N_FULL = {"2014-06-30": 2297, "2018-06-29": 3298, "2026-06-30": 5166}


# ---------------------------------------------------------------- 复用绑定
@contextmanager
def dp_families_bound_to_xsection():
    """把 `divergence_probe.variant_grid` 的族**临时**绑到 `("X1","X2")`。

    `build_ic_panel` / `run_machines` 内部调用 `variant_grid()` **不带参数**，而该默认值
    在 `def` 时就绑死成 `("D1","D2","D3")`（Python 默认参数的求值时机）——
    预登记 §3 既要求这两台编排器"原样复用"，又禁止改 `divergence_probe.py`，
    故只能在**运行期**换模块属性绑定，退出时无条件还原（单线程、只覆盖编排调用；
    判例 `test_family_binding_is_restored` 守卫还原）。**不改任何口径**：同一批 32 点、
    同一次调用、同一统计量。
    """
    orig = _dp.variant_grid

    def _bound(families: tuple[str, ...] = FAMILIES):
        return orig(families)

    _dp.variant_grid = _bound
    try:
        yield
    finally:
        _dp.variant_grid = orig


def xsection_variant_grid() -> list[tuple]:
    """预登记 32 点（2 族 × lb × zw × k），顺序固定（判例与产物可对表）。"""
    return variant_grid(FAMILIES)


# ---------------------------------------------------------------- 纯函数：两族水平量
def universe_mask(df: pd.DataFrame) -> pd.Series:
    """D1 宇宙的 pandas 镜像（判例 + SQL 交叉验证用）。

    后缀 `.SH/.SZ`（**排除 `.HK`/`.BJ`**）+ `close` 非空 + `pre_close>0` + `volume>0`。
    """
    code = df["ts_code"].astype(str)
    suffix_ok = code.str.endswith(".SH") | code.str.endswith(".SZ")
    close = pd.to_numeric(df["close"], errors="coerce")
    pre = pd.to_numeric(df["pre_close"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce")
    return suffix_ok & close.notna() & (pre > 0) & (vol > 0)


def xsection_measures(df: pd.DataFrame) -> pd.DataFrame:
    """X1 日度聚合的 pandas 镜像（SQL↔pandas 交叉验证 + 判例）。

    输入列 `ts_code/trade_date/close/pre_close/volume`（未复权原表行）。
    输出按日 `n/xs_sd/xs_mean/xs_med`，`xs_sd` = `STDDEV_SAMP`（ddof=1）。
    """
    d = df[universe_mask(df)].copy()
    d["ret"] = (pd.to_numeric(d["close"], errors="coerce").astype(float)
                / pd.to_numeric(d["pre_close"], errors="coerce").astype(float) - 1.0)
    g = d.groupby("trade_date")["ret"]
    out = pd.DataFrame({"n": g.size().astype(int), "xs_sd": g.std(ddof=1),
                        "xs_mean": g.mean(), "xs_med": g.median()})
    return out.sort_index()


def implied_avg_correlation(corr_raw: pd.DataFrame, window: int = CORR_WINDOW) -> pd.Series:
    """X2：方差比隐含平均相关 ρ̄（Solnik-Roulet 2000 / Driessen et al. 2009 代数）。

        ρ̄ = [σ²_p − (1/N²)·Σσ²_i] / [(1/N²)·((Σσ_i)² − Σσ²_i)]

    `σ_p` = **同一子集**（`c60=60` 那批票）等权日收益 `ew_ret_full` 的滚动 `window` 日
    样本标准差（ddof=1，与 σ_i 的 `stddev_samp` 同 ddof）——分子分母同宇宙，
    这正是 D4 那条"否则 ρ̄ 出界"的约束。代数上 ρ̄ 恒等于
    `Σ_{i≠j} ρ_ij σ_i σ_j / Σ_{i≠j} σ_i σ_j`（σ 加权平均逐对相关）⟹ 落在 [−1, 1]，
    前提是窗内子集成员不变；实际成员逐日微变（次新满 60 日入池、长停牌出池），
    是已登记的近似（限制段）。

    分母 ≤0 / N<2 / 数据缺失 → NaN（不静默降级为 0）。
    """
    n = corr_raw["n_full"].astype(float)
    sum_sd = corr_raw["sum_sd"].astype(float)
    sum_var = corr_raw["sum_var"].astype(float)
    sig_p = corr_raw["ew_ret_full"].astype(float).rolling(window).std()
    num = sig_p ** 2 - sum_var / (n ** 2)
    den = (sum_sd ** 2 - sum_var) / (n ** 2)
    rho = (num / den).replace([np.inf, -np.inf], np.nan)
    ok = (n >= 2) & den.notna() & (den > 0) & num.notna()
    return rho.where(ok).rename("X2")


def xsection_levels(disp: pd.DataFrame, corr_raw: pd.DataFrame,
                    window: int = CORR_WINDOW) -> dict[str, pd.Series]:
    """两族原始水平量（预登记 §2 钉死的定义）。

    - **X1 截面离散度** = 当日宇宙内个股收益的横截面 `STDDEV_SAMP`（ddof=1，有符号收益）。
      **明确不采纳**"绝对收益的截面均值"（一阶量，命题错配）。
    - **X2 平均相关性** = `implied_avg_correlation`（方差比隐含，60 日窗，σ_p 同子集）。
    """
    for col in ("n", "xs_sd"):
        if col not in disp.columns:
            raise ValueError(f"xs_dispersion 缺列 {col}：得到 {list(disp.columns)}")
    for col in ("n_full", "sum_sd", "sum_var", "ew_ret_full"):
        if col not in corr_raw.columns:
            raise ValueError(f"avg_correlation 缺列 {col}：得到 {list(corr_raw.columns)}")
    return {"X1": disp["xs_sd"].astype(float).rename("X1"),
            "X2": implied_avg_correlation(corr_raw, window)}


# ---------------------------------------------------------------- PG（缓存构建）
def _connect_ro(db: dict):
    """单次连接（**不滚动重试**）+ `connect_timeout=15` + 显式 `statement_timeout=600 s`。

    预登记 §7 硬要求（吸取 ④ 偏离 G / `ops-tailscale-blackhole-diagnosis`）：
    拒连就抛出并停下讲清楚，不重试。
    """
    import psycopg2
    return psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["name"],
        user=db["user"], password=db["password"],
        connect_timeout=15, keepalives=1, keepalives_idle=30,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


def _query_frame(db: dict, sql: str, columns: list[str]) -> pd.DataFrame:
    """执行按日聚合 SQL 并收 ~3.7k 行（**绝不** fetchall 个股长表）。"""
    conn = _connect_ro(db)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").astype(float).sort_index()


_DISP_SQL = f"""
SELECT trade_date, COUNT(*) AS n,
       STDDEV_SAMP(close/pre_close - 1)::float8 AS xs_sd,
       AVG(close/pre_close - 1)::float8         AS xs_mean,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY close/pre_close - 1)::float8 AS xs_med
FROM {{schema}}.stock_daily_price
WHERE trade_date >= DATE '{SQL_START}'
  AND {UNIVERSE_SQL}
GROUP BY trade_date ORDER BY trade_date
"""

_CORR_SQL = f"""
WITH r AS (
  SELECT ts_code, trade_date, (close/pre_close - 1.0)::float8 AS ret
  FROM {{schema}}.stock_daily_price
  WHERE trade_date >= DATE '{SQL_START}'
    AND {UNIVERSE_SQL}
), w AS (
  SELECT ts_code, trade_date, ret,
         stddev_samp(ret) OVER (PARTITION BY ts_code ORDER BY trade_date
                                ROWS BETWEEN {CORR_WINDOW - 1} PRECEDING
                                AND CURRENT ROW) AS sd60,
         count(*)         OVER (PARTITION BY ts_code ORDER BY trade_date
                                ROWS BETWEEN {CORR_WINDOW - 1} PRECEDING
                                AND CURRENT ROW) AS c60
  FROM r
)
SELECT trade_date,
       COUNT(*)       FILTER (WHERE c60 = {CORR_WINDOW})            AS n_full,
       SUM(sd60)      FILTER (WHERE c60 = {CORR_WINDOW})::float8    AS sum_sd,
       SUM(sd60*sd60) FILTER (WHERE c60 = {CORR_WINDOW})::float8    AS sum_var,
       AVG(ret)       FILTER (WHERE c60 = {CORR_WINDOW})::float8    AS ew_ret_full
FROM w GROUP BY trade_date ORDER BY trade_date
"""


def _fetch_anchor_rows(db: dict, dates: list[str]) -> pd.DataFrame:
    """锚点日**原始行**（无宇宙过滤 → 让 pandas 镜像自己排 .HK/.BJ）。

    3 天 ≈1.2 万行，是本模块唯一一处 `fetchall` 个股行，量级安全。
    """
    lits = ", ".join(f"DATE '{d}'" for d in dates)
    conn = _connect_ro(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT ts_code, trade_date, close, pre_close, volume
                            FROM {db['schema']}.stock_daily_price
                            WHERE trade_date IN ({lits})""")
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "pre_close", "volume"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def build_xs_dispersion(db=None, force: bool = False) -> pd.DataFrame:
    """X1 缓存（`n/xs_sd/xs_mean/xs_med`）+ 锚点回归 + SQL↔pandas 交叉验证。"""
    if DISP_CACHE.exists() and not force:
        d = pd.read_csv(DISP_CACHE, parse_dates=["date"]).set_index("date")
        _check_anchor_n(d)
        return d
    from signals.common.config import load_db_config
    db = db or load_db_config()
    t0 = time.time()
    d = _query_frame(db, _DISP_SQL.format(schema=db["schema"]),
                     ["date", "n", "xs_sd", "xs_mean", "xs_med"])
    elapsed = time.time() - t0
    d["n"] = d["n"].astype(int)
    _check_anchor_n(d)

    # SQL↔pandas 交叉验证（宇宙过滤 + 统计量都要逐数对得上）
    raw = _fetch_anchor_rows(db, list(ANCHORS_N))
    mirror = xsection_measures(raw)
    for day in ANCHORS_N:
        ts = pd.Timestamp(day)
        if int(mirror.loc[ts, "n"]) != int(d.loc[ts, "n"]):
            raise ValueError(f"SQL 与 xsection_measures 在 {day} 的宇宙口径不一致："
                             f"pandas {int(mirror.loc[ts, 'n'])} vs SQL {int(d.loc[ts, 'n'])}")
        if abs(float(mirror.loc[ts, "xs_sd"]) - float(d.loc[ts, "xs_sd"])) > 1e-9:
            raise ValueError(f"SQL 与 xsection_measures 在 {day} 的 xs_sd 不一致")

    DISP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    d.rename_axis("date").reset_index().to_csv(DISP_CACHE, index=False)
    print(f"[cache] xs_dispersion 建成：{len(d)} 行 / {elapsed:.1f} s → {DISP_CACHE}")
    return d


def build_avg_correlation(db=None, force: bool = False) -> pd.DataFrame:
    """X2 原料缓存（`n_full/sum_sd/sum_var/ew_ret_full`）。全历史 SQL ≈4~5 分钟。"""
    if CORR_CACHE.exists() and not force:
        c = pd.read_csv(CORR_CACHE, parse_dates=["date"]).set_index("date")
        _check_anchor_n_full(c)
        return c
    from signals.common.config import load_db_config
    db = db or load_db_config()
    t0 = time.time()
    c = _query_frame(db, _CORR_SQL.format(schema=db["schema"]),
                     ["date", "n_full", "sum_sd", "sum_var", "ew_ret_full"])
    elapsed = time.time() - t0
    c["n_full"] = c["n_full"].astype(int)
    _check_anchor_n_full(c)          # 锚点不过 → 绝不落盘（坏数据不得进缓存）
    CORR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    c.rename_axis("date").reset_index().to_csv(CORR_CACHE, index=False)
    print(f"[cache] avg_correlation 建成：{len(c)} 行 / {elapsed:.1f} s → {CORR_CACHE}")
    return c


def _check_anchor_n(d: pd.DataFrame) -> None:
    for day, n_expect in ANCHORS_N.items():
        ts = pd.Timestamp(day)
        if ts not in d.index:
            raise ValueError(f"X1 锚点日 {day} 缺失")
        got = int(d.loc[ts, "n"])
        if got != n_expect:
            raise ValueError(f"X1 宇宙锚点 {day}: 实得 {got}, 预期 {n_expect}（附录 §1.4）")


def _check_anchor_n_full(c: pd.DataFrame) -> None:
    """X2 缓存的同级护栏（面2 backlog B1）：读/建两条路径都过，防陈旧或损坏的 CSV 被静默采信。"""
    for day, n_expect in ANCHORS_N_FULL.items():
        ts = pd.Timestamp(day)
        if ts not in c.index:
            raise ValueError(f"X2 锚点日 {day} 缺失（缓存陈旧或被截断？）")
        got = int(c.loc[ts, "n_full"])
        if got != n_expect:
            raise ValueError(f"X2 宇宙锚点 {day}: 实得 {got}, 预期 {n_expect}"
                             f"（口径见 ANCHORS_N_FULL 注释）")


# ---------------------------------------------------------------- 诊断（净新码）
def build_diagnostics(levels: dict[str, pd.Series], signals: dict[tuple, pd.Series],
                      ew: pd.Series, breadth: pd.DataFrame, sizes: pd.DataFrame,
                      common_sel: pd.DatetimeIndex, winner: tuple) -> pd.DataFrame:
    """面2 强制诊断（不进闸门，**一律限选择窗**）：

    - `corr(X1, breadth.pct_above_ma20)` —— 当年"离散度≈广度"跳过理由的第一个数字（§5）；
    - `corr(X1, X2)` **两版**：水平量 + 赢家 lb/zw 信号化后（§2 D7 的硬要求）；
    - `corr(·, N_t)` —— 两族水平量对当日截面票数的相关（§2 D6 的估计噪声趋势）；
    - 附：corr(族, 生产 ew) 与赢家信号 vs 广度信号（沿用 ② `build_diagnostics` 的口径）。
    """
    rows = []
    b_cols = ["pct_above_ma20", "pct_above_ma60", "hi_lo_diff20", "hi_lo_diff60"]

    def _pair(name_a, a, name_b, b, kind):
        j = pd.concat([a.rename("a"), b.rename("b")], axis=1, join="inner").dropna()
        j = j[j.index.isin(common_sel)]          # 诊断限窗：只在选择窗内算
        if len(j) < 30:
            return
        rows.append({"kind": kind, "left": name_a, "right": name_b,
                     "pearson": float(j["a"].corr(j["b"])),
                     "spearman": float(j["a"].corr(j["b"], method="spearman")),
                     "n_obs": int(len(j))})

    for fam, lvl in levels.items():
        for c in b_cols:
            _pair(fam, lvl, f"breadth_{c}", breadth[c], "level_vs_breadth")
        _pair(fam, lvl, "equal_weight_factor", ew, "level_vs_production")
        _pair(fam, lvl, "abs_equal_weight_factor", ew.abs(), "level_vs_abs_production")
        for c in sizes.columns:
            _pair(fam, lvl, c, sizes[c], "level_vs_universe_size")

    # 两族重合性（§2.3 的硬要求）：水平量一版 + 赢家口径信号化一版
    _pair("X1", levels["X1"], "X2", levels["X2"], "family_overlap_level")
    fam, lb, zw, k = winner
    x1_sig = warmup_masked_level_signal(levels["X1"].dropna(), lb, zw)
    x2_sig = warmup_masked_level_signal(levels["X2"].dropna(), lb, zw)
    _pair(f"signal_X1_lb{lb}zw{zw}", x1_sig, f"signal_X2_lb{lb}zw{zw}", x2_sig,
          "family_overlap_signal")

    # 赢家变体口径下的同面判定（同 lb/zw 信号化后再比，最锋利的一版）
    wsig = signals[winner]
    for c in b_cols:
        _pair(f"signal_{fam}_lb{lb}zw{zw}", wsig,
              f"signal_breadth_{c}_lb{lb}zw{zw}",
              warmup_masked_level_signal(breadth[c].dropna(), lb, zw),
              "winner_signal_vs_breadth_signal")
    _pair(f"signal_{fam}_lb{lb}zw{zw}", wsig, "equal_weight_factor", ew,
          "winner_signal_vs_production")
    return pd.DataFrame(rows)


def build_metadata(summary: dict) -> pd.DataFrame:
    """裁决 CSV 的 META 行：赢家全参数 + 样本 + 溯源锚点（CSV 单看即可复述结论）。"""
    w = summary["winner_variant"]
    run = summary["run"]
    g1, g1b = summary["gate1_ic_machine"], summary["gate1b_partial_ic_machine"]
    items = {
        "verdict": summary["verdict"],
        "winner_family": w["family"], "winner_lb": w["lb"], "winner_zw": w["zw"],
        "winner_k": w["k"],
        "winner_abs_ic": g1["observed_best"],
        "winner_ic_signed_selection": summary["winner_ic_by_window"]["selection_2014_2023"],
        "p_selected_ic": g1["p_selected"],
        "p_naive_ic_UNCORRECTED": g1["p_naive_UNCORRECTED"],
        "p_min_p_ic": g1["p_min_p"],
        "selection_inflation_ic": g1["selection_inflation"],
        "partial_ic_signed_at_winner": g1b["partial_ic_signed_at_gate1_winner"],
        "p_selected_partial_at_winner": g1b["p_selected_at_gate1_winner"],
        "p_selected_partial_own_winner": g1b["p_selected"],
        "partial_machine_own_winner": g1b["best_variant"],
        "null_selected_p95_ic": summary["null_selected_quantiles"]["ic"]["p95"],
        "n_distinct_null_winners_ic": g1["n_distinct_null_winners"],
        **{f"winner_ic_{k}": v for k, v in summary["winner_ic_by_window"].items()},
        **{f"winner_net_sharpe_{k}": v["net_sharpe"]
           for k, v in summary["winner_net_by_window"].items()},
        **{k: run[k] for k in (
            "n_perm", "seed", "cost_bps", "n_obs_selection", "sample_first_date",
            "sample_last_date", "min_shift", "max_shift", "n_variants",
            "sql_start", "corr_window", "universe", "x2_min", "x2_max",
            "n_universe_first", "n_universe_last",
            "vectorized_vs_house_ic_max_abs_diff", "db_host", "holdout_policy")},
    }
    return pd.DataFrame([{"gate": "META", "metric": k, "value": v,
                          "threshold": float("nan"), "pass": None}
                         for k, v in items.items()])


# ---------------------------------------------------------------- 编排
def run_probe(n_perm: int = 1000, seed: int = 0, cost_bps: float = 3.0, db=None,
              force_cache: bool = False) -> dict:
    """端到端：两族 SQL 缓存 → 32 点合成一次机器（×2 统计量）→ 三关判定 → 产物字典。"""
    from backtest.data import load_carry, load_underlying_returns

    disp = build_xs_dispersion(db=db, force=force_cache)
    corr_raw = build_avg_correlation(db=db, force=force_cache)
    levels = xsection_levels(disp, corr_raw)
    signals = build_variant_signals(levels, FAMILIES)
    ew = _load_ew_signal()
    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    carry = load_carry(PRIMARY_KOU_JING, db=db)
    breadth = load_breadth()
    sizes = pd.DataFrame({"n_universe": disp["n"].astype(float),
                          "n_full_corr_subset": corr_raw["n_full"].astype(float)})

    # 全变体共用一条时间索引（机器硬要求）；zw=250 的暖机 + X2 的 60 日窗决定起点
    common = None
    for v in xsection_variant_grid():
        idx = signals[v].dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.intersection(und[PRIMARY_KOU_JING].index).intersection(ew.dropna().index)
    common = common.sort_values()
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]

    with dp_families_bound_to_xsection():
        panel = build_ic_panel(signals,
                               {k: v.reindex(common).dropna() for k, v in und.items()},
                               common)
        frames = build_score_frames(und[PRIMARY_KOU_JING], ew, common_sel)
        res_ic, res_pic = run_machines(signals, frames, common_sel, n_perm, seed)

    winner = res_ic.best_variant
    j_win = res_ic.best_index
    # 运行期口径校验：向量化统计量 == 房规 nonoverlap_ic 的 |·|（selection 窗、blend）
    panel_abs = (panel[(panel["kou_jing"] == PRIMARY_KOU_JING)]
                 .set_index(["family", "lb", "zw", "k"])["ic_selection_2014_2023"].abs())
    obs_check = float(np.nanmax(np.abs(
        np.array([panel_abs.loc[v] for v in xsection_variant_grid()]) - res_ic.observed)))
    if not (obs_check < 1e-9):
        raise AssertionError(f"向量化 IC 与房规 nonoverlap_ic 不一致：max|diff|={obs_check:.3e}")

    # 关1b：赢家的**有符号**偏 IC（向量化口径只给 |·|）+ 在关1b 零分布上的保守 p
    sf = frames[winner[3]]
    x = signals[winner].reindex(common_sel).to_numpy(dtype=float)[None, sf.pts]
    rx = rankdata(x, axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    beta = (rx @ sf.ctrl_rank_c) / sf.ctrl_ss
    rs = rx - beta[:, None] * sf.ctrl_rank_c[None, :]
    den = np.sqrt((rs * rs).sum(axis=1) * sf.fwd_resid_ss)
    pic_signed = float((rs @ sf.fwd_resid)[0] / den[0]) if den[0] >= 1e-9 else 0.0
    ic_sel_signed = float(panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                                & (panel["family"] == winner[0]) & (panel["lb"] == winner[1])
                                & (panel["zw"] == winner[2]) & (panel["k"] == winner[3])
                                ]["ic_selection_2014_2023"].iloc[0])
    same_sign = bool(np.sign(pic_signed) == np.sign(ic_sel_signed) and pic_signed != 0.0)
    p_pic_at_winner = p_at_threshold(res_pic.null_selected, res_pic.observed[j_win])

    direction = float(np.sign(ic_sel_signed))
    sharpe_by_win = net_sharpe_by_window(
        signals[winner].reindex(common).dropna(), direction, winner[3],
        und[PRIMARY_KOU_JING], carry, cost_bps)

    verdict = evaluate_gates(winner, panel, res_ic, res_pic, pic_signed,
                             p_pic_at_winner, sharpe_by_win)
    overall = bool(verdict[verdict["gate"] == "OVERALL"]["pass"].iloc[0])

    diagnostics = build_diagnostics(levels, signals, ew, breadth, sizes, common_sel, winner)

    perm_frame = pd.concat([
        res_ic.to_frame().assign(machine="abs_nonoverlap_ic"),
        res_pic.to_frame().assign(machine="abs_partial_ic_vs_equal_weight"),
    ], ignore_index=True)

    x2 = levels["X2"].dropna()
    summary = {
        "PROBE": "面2 个股截面二阶矩（X1 离散度 / X2 平均相关性；预登记 32 点，合成一次调用）",
        "verdict": "GO" if overall else "STOP",
        "winner_variant": {"family": winner[0], "lb": winner[1], "zw": winner[2],
                           "k": winner[3]},
        "gate1_ic_machine": res_ic.summary(),
        "gate1b_partial_ic_machine": {
            **res_pic.summary(),
            "observed_at_gate1_winner": float(res_pic.observed[j_win]),
            "p_selected_at_gate1_winner": p_pic_at_winner,
            "partial_ic_signed_at_gate1_winner": pic_signed,
            "same_sign_as_ic": same_sign,
        },
        "null_selected_quantiles": {
            name: {f"p{q}": float(np.percentile(r.null_selected[np.isfinite(r.null_selected)], q))
                   for q in (5, 25, 50, 75, 95, 99)}
            for name, r in (("ic", res_ic), ("partial_ic", res_pic))
        },
        "winner_ic_by_window": {w: float(panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                                               & (panel["family"] == winner[0])
                                               & (panel["lb"] == winner[1])
                                               & (panel["zw"] == winner[2])
                                               & (panel["k"] == winner[3])][f"ic_{w}"].iloc[0])
                               for w in WINDOWS_REPORT},
        "winner_net_by_window": sharpe_by_win,
        "run": {
            "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
            "n_obs_selection": len(common_sel),
            "selection_window": list(SELECTION),
            "sample_first_date": str(common_sel[0].date()),
            "sample_last_date": str(common_sel[-1].date()),
            "common_full_first": str(common[0].date()),
            "common_full_last": str(common[-1].date()),
            "min_shift": 2 * max(GRID_K), "max_shift": len(common_sel) - 2 * max(GRID_K),
            "n_variants": len(xsection_variant_grid()),
            "sql_start": SQL_START, "corr_window": CORR_WINDOW,
            "universe": "suffix .SH/.SZ + close NOT NULL + pre_close>0 + volume>0"
                        "（无 .BJ/.HK、无市值/流动性/ST/次新筛选）",
            "x1_first_date": str(levels["X1"].dropna().index[0].date()),
            "x2_first_date": str(x2.index[0].date()),
            "x2_min": float(x2.min()), "x2_max": float(x2.max()),
            "n_universe_first": int(disp["n"].iloc[0]),
            "n_universe_last": int(disp["n"].iloc[-1]),
            "vectorized_vs_house_ic_max_abs_diff": obs_check,
            "db_host": (db or {}).get("host", "settings.yaml default"),
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门（勘误 §3 / 议程 §7.3）",
        },
    }
    return {"panel": panel, "permutation": perm_frame, "diagnostics": diagnostics,
            "verdict": verdict, "summary": summary, "res_ic": res_ic, "res_pic": res_pic,
            "levels": levels, "winner": winner}


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="面2 个股截面二阶矩探针（32 点合成一次调用）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--db-host", default=None, help="覆盖库主机（主库拒连时走只读备端）")
    ap.add_argument("--rebuild-cache", action="store_true", help="强制重建两份 SQL 缓存")
    ap.add_argument("--cache-only", action="store_true", help="只建缓存不跑探针")
    args = ap.parse_args()

    db = None
    if args.db_host:
        from signals.common.config import load_db_config
        db = dict(load_db_config())
        db["host"] = args.db_host

    if args.cache_only:
        d = build_xs_dispersion(db=db, force=args.rebuild_cache)
        c = build_avg_correlation(db=db, force=args.rebuild_cache)
        print(f"xs_dispersion: {len(d)} 行 {d.index[0].date()}~{d.index[-1].date()}")
        print(f"avg_correlation: {len(c)} 行 {c.index[0].date()}~{c.index[-1].date()}")
        return 0

    out = run_probe(n_perm=args.n_perm, seed=args.seed, cost_bps=args.cost_bps, db=db,
                    force_cache=args.rebuild_cache)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out["panel"].round(6).to_csv(out_dir / "probe_xsection_panel.csv", index=False)
    out["permutation"].round(6).to_csv(out_dir / "probe_xsection_permutation.csv", index=False)
    out["diagnostics"].round(6).to_csv(out_dir / "probe_xsection_diagnostics.csv", index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        out_dir / "probe_xsection_verdict.csv", index=False)
    side = out_dir / "probe_xsection_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    fam, lb, zw, k = out["winner"]
    blend = out["panel"][out["panel"]["kou_jing"] == PRIMARY_KOU_JING].copy()
    blend["abs_ic_sel"] = blend["ic_selection_2014_2023"].abs()
    top = blend.sort_values("abs_ic_sel", ascending=False).head(10)
    print("=== 选择窗（2014-2023 · blend）|IC| 前 10（32 点全表见 CSV） ===")
    print(top[["family", "lb", "zw", "k", "ic_selection_2014_2023",
               "ic_train_2014_2020", "ic_val_2021_2023", "ic_holdout_2024_2026",
               "n_selection_2014_2023"]].round(4).to_string(index=False))
    print(f"\n=== 赢家：{fam} lb{lb} zw{zw} k{k} ===")
    for key, val in out["summary"]["gate1_ic_machine"].items():
        print(f"  [关1] {key}: {val}")
    for key in ("observed_at_gate1_winner", "p_selected_at_gate1_winner",
                "partial_ic_signed_at_gate1_winner", "same_sign_as_ic",
                "p_selected", "p_naive_UNCORRECTED", "best_variant"):
        print(f"  [关1b] {key}: {out['summary']['gate1b_partial_ic_machine'][key]}")
    print("\n=== 三关裁决 ===")
    print(out["verdict"].round(6).to_string(index=False))
    print("\n=== 赢家逐窗净表现（3bps + blend carry） ===")
    print(pd.DataFrame(out["summary"]["winner_net_by_window"]).T.round(4).to_string())
    print("\n=== 诊断（限选择窗；面2 强制列） ===")
    dg = out["diagnostics"]
    print(dg[dg["kind"].isin(["family_overlap_level", "family_overlap_signal",
                              "level_vs_breadth", "level_vs_universe_size"])]
          [["kind", "left", "right", "pearson", "spearman", "n_obs"]].round(3)
          .to_string(index=False))
    verdict = out["summary"]["verdict"]
    print(f"\n{'★ GO：三关全过' if verdict == 'GO' else '✗ STOP：任一关不过 → 归档'}")
    print(f"→ {out_dir}/probe_xsection_{{panel,permutation,diagnostics,verdict}}.csv")
    print(f"→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
