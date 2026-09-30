"""涨停温度计轴探针：四族因子 × 三关闸门（探针机器全复用 leverage_probe）。

设计：docs/plans/2026-07-08-thermo-probe-design.md。涨停判定用 stock_daily_price
**未复权**价规则重构（限价规则作用于原始价）：limit=四舍五入(pre_close×(1+pct), 2)，
创业板 2020-08-24 起 20%、科创 20%、主板 10%；北交所/基金剔除（whitelist
60/68/00/30 前缀）；ST（5% 限幅）与无涨跌幅日由等值判定自然排除。

族：F1 涨停占比 / F2 炸板率 / F3 涨停溢价（昨日封板股今日收益，日期戳=实现日）
/ F4 成交额水位（log market_turnover）。当日收盘即知 → **无 pit_lag**（与全仓库
信号线同"T 收盘执行"约定；两融因 T+1 公布才需滞后）。快频面 → k∈{3,5,10,20}。

CLI: python3 -m backtest.thermo_probe [--families F1,F2,F3,F4] [--n-perm 1000]
产出: backtest/output/thermo_probe{,_verdicts}.csv + thermometer.csv 缓存。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import (  # noqa: E402
    GRID_LEVEL,
    build_market_turnover,
    level_signal,
    run_families_probe,
)
from backtest.rotation_probe import HALVES  # noqa: E402

GRID_K_THERMO = (3, 5, 10, 20)
CACHE_PATH = ROOT / "backtest" / "output" / "thermometer.csv"
# 回归锚点 (n_sealed, n_burst)：2026-07-08 侦察实测；2024-01-08 主板 31/15
# 与 runbook 前期独立侦察逐数吻合
ANCHORS_THERMO = {"2024-01-08": (33, 16), "2024-09-30": (825, 332)}
_GEM_SWITCH = pd.Timestamp("2020-08-24")  # 创业板 10%→20% 生效日


# ---------------------------------------------------------------- 纯函数
def board_limit_pct(ts_code: pd.Series, trade_date: pd.Series) -> pd.Series:
    """板别涨跌幅限制：科创 20%；创业板 2020-08-24 起 20%、之前 10%；主板 10%。"""
    code = ts_code.astype(str)
    star = code.str.startswith("68").to_numpy()
    gem = code.str.startswith("30").to_numpy()
    after = pd.to_datetime(trade_date).to_numpy() >= np.datetime64(_GEM_SWITCH)
    return pd.Series(np.where(star | (gem & after), 0.20, 0.10), index=ts_code.index)


def classify_limit(pre_close: pd.Series, high: pd.Series, close: pd.Series,
                   pct: pd.Series) -> tuple[pd.Series, pd.Series]:
    """封板/炸板逐行判定（SQL 镜像）。

    限价=四舍五入(pre_close×(1+pct), 2)——交易所 half-up；Python round/np.round
    是银行家舍入且有浮点误差（2.35×1.1=2.585 会错给 2.58），故走整数分位算法：
    cents×(100+100pct) 后 +50 整除 100。触板=|high−limit|<0.005（无涨跌幅日
    high 越过限价 → 自然排除）；封板=触板且收在限价；炸板=触板且回落。
    """
    cents = np.rint(pre_close.to_numpy(dtype=float) * 100).astype(np.int64)
    k = np.rint((1.0 + pct.to_numpy(dtype=float)) * 100).astype(np.int64)
    limit = ((cents * k + 50) // 100) / 100.0
    h = high.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    touched = np.abs(h - limit) < 0.005
    sealed = touched & (np.abs(c - limit) < 0.005)
    burst = touched & (c < limit - 0.005)
    idx = pre_close.index
    return pd.Series(sealed, index=idx), pd.Series(burst, index=idx)


def thermometer_measures(df: pd.DataFrame) -> pd.DataFrame:
    """日度温度计聚合（SQL 镜像，供小样本交叉验证）。

    输入列 ts_code/trade_date/pre_close/high/close（未复权）。输出按日：
    n_sealed/n_burst/n_active/lu_premium(昨日封板股今日均收益，日期戳=实现日
    =PIT 干净)/lu_ratio/burst_rate(0/0→NaN)。停牌缺行时 shift 跨停牌段（已知
    噪声，封板股次日停牌罕见）。
    """
    d = df.sort_values(["ts_code", "trade_date"]).copy()
    pct = board_limit_pct(d["ts_code"], d["trade_date"])
    d["sealed"], d["burst"] = classify_limit(d["pre_close"], d["high"], d["close"], pct)
    d["ret"] = d["close"] / d["pre_close"] - 1.0
    d["sealed_prev"] = d.groupby("ts_code")["sealed"].shift(1)

    g = d.groupby("trade_date")
    out = pd.DataFrame({
        "n_sealed": g["sealed"].sum().astype(int),
        "n_burst": g["burst"].sum().astype(int),
        "n_active": g.size(),
    })
    prem = d[d["sealed_prev"] == True].groupby("trade_date")["ret"].mean()  # noqa: E712
    out["lu_premium"] = prem.reindex(out.index)
    out["lu_ratio"] = out["n_sealed"] / out["n_active"]
    denom = (out["n_sealed"] + out["n_burst"]).astype(float).replace(0.0, np.nan)
    out["burst_rate"] = out["n_burst"].astype(float) / denom
    return out


def build_thermo_signals(thermo: pd.DataFrame, amt: pd.Series
                         ) -> dict[str, dict[str, pd.Series]]:
    """四族×网格装配。当日收盘即知 → 不做 pit_lag。"""
    src = {
        "F1": thermo["lu_ratio"],
        "F2": thermo["burst_rate"],
        "F3": thermo["lu_premium"],
        "F4": np.log(amt),
    }
    return {
        fam: {f"{fam}_lb{lb}zw{zw}": level_signal(series.dropna(), lb, zw)
              for lb, zw in GRID_LEVEL}
        for fam, series in src.items()
    }


# ---------------------------------------------------------------- 数据装载
_ROW_FILTER = """volume > 0 AND pre_close > 0
      AND high IS NOT NULL AND close IS NOT NULL
      AND (ts_code LIKE '60%%' OR ts_code LIKE '68%%'
           OR ts_code LIKE '00%%' OR ts_code LIKE '30%%')"""


def _fetch_rows(db, dates: list[str]) -> pd.DataFrame:
    """锚点日原始行（交叉验证 SQL↔thermometer_measures 用）。"""
    from backtest.data import _connect
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT ts_code, trade_date, pre_close, high, close
                    FROM {db['schema']}.stock_daily_price
                    WHERE trade_date = ANY(%s::date[]) AND {_ROW_FILTER}""",
                (dates,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "pre_close", "high", "close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("pre_close", "high", "close"):
        df[c] = df[c].astype(float)
    return df


def build_thermometer(db=None, force: bool = False) -> pd.DataFrame:
    """全历史日度温度计（单扫 SQL）+ 双重验证（锚点回归 + SQL↔pandas 交叉）+ 缓存。"""
    if CACHE_PATH.exists() and not force:
        t = pd.read_csv(CACHE_PATH, parse_dates=["date"]).set_index("date")
        _check_anchors(t)
        return t
    from signals.common.config import load_db_config
    from backtest.data import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH c AS (
                  SELECT ts_code, trade_date,
                         (close / pre_close - 1.0)::float8 AS ret,
                         ROUND((pre_close * (1 + CASE
                             WHEN ts_code LIKE '68%%' THEN 0.20
                             WHEN ts_code LIKE '30%%' AND trade_date >= DATE '2020-08-24'
                               THEN 0.20
                             ELSE 0.10 END))::numeric, 2) AS lim,
                         high, close
                  FROM {db['schema']}.stock_daily_price
                  WHERE {_ROW_FILTER}
                ), m AS (
                  SELECT ts_code, trade_date, ret,
                         (ABS(high - lim) < 0.005 AND ABS(close - lim) < 0.005) AS sealed,
                         (ABS(high - lim) < 0.005 AND close < lim - 0.005) AS burst
                  FROM c
                ), l AS (
                  SELECT trade_date, ret, sealed, burst,
                         LAG(sealed) OVER (PARTITION BY ts_code ORDER BY trade_date)
                           AS sealed_prev
                  FROM m
                )
                SELECT trade_date,
                       COUNT(*) FILTER (WHERE sealed) AS n_sealed,
                       COUNT(*) FILTER (WHERE burst) AS n_burst,
                       COUNT(*) AS n_active,
                       AVG(ret) FILTER (WHERE sealed_prev) AS lu_premium
                FROM l GROUP BY trade_date ORDER BY trade_date""")
            rows = cur.fetchall()
    finally:
        conn.close()
    t = pd.DataFrame(rows, columns=["date", "n_sealed", "n_burst", "n_active", "lu_premium"])
    t["date"] = pd.to_datetime(t["date"])
    t = t.set_index("date").astype(float)
    t[["n_sealed", "n_burst", "n_active"]] = t[["n_sealed", "n_burst", "n_active"]].astype(int)
    t["lu_ratio"] = t["n_sealed"] / t["n_active"]
    denom = (t["n_sealed"] + t["n_burst"]).astype(float).replace(0.0, np.nan)
    t["burst_rate"] = t["n_burst"].astype(float) / denom

    _check_anchors(t)
    # SQL↔pandas 交叉验证：锚点日原始行重算须逐数一致
    raw = _fetch_rows(db, list(ANCHORS_THERMO))
    pm = thermometer_measures(raw)
    for d in ANCHORS_THERMO:
        ts = pd.Timestamp(d)
        if (int(pm.loc[ts, "n_sealed"]) != int(t.loc[ts, "n_sealed"])
                or int(pm.loc[ts, "n_burst"]) != int(t.loc[ts, "n_burst"])):
            raise ValueError(f"SQL 与 thermometer_measures 在 {d} 不一致："
                             f"pandas {pm.loc[ts, ['n_sealed', 'n_burst']].tolist()} "
                             f"vs SQL {t.loc[ts, ['n_sealed', 'n_burst']].tolist()}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    t.rename_axis("date").reset_index().to_csv(CACHE_PATH, index=False)
    return t


def _check_anchors(t: pd.DataFrame) -> None:
    for d, (n_sealed, n_burst) in ANCHORS_THERMO.items():
        ts = pd.Timestamp(d)
        if ts not in t.index:
            raise ValueError(f"温度计锚点日 {d} 缺失")
        got = (int(t.loc[ts, "n_sealed"]), int(t.loc[ts, "n_burst"]))
        if got != (n_sealed, n_burst):
            raise ValueError(f"温度计锚点 {d}: 实得 {got}, 预期 {(n_sealed, n_burst)}")


# ---------------------------------------------------------------- 编排
def run_probe(families: tuple[str, ...] = ("F1", "F2", "F3", "F4"), n_perm: int = 1000,
              cost_bps: float = 3.0, db=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    thermo = build_thermometer(db)
    amt = build_market_turnover(db)
    sigs_all = build_thermo_signals(thermo, amt)
    return run_families_probe(sigs_all, families, GRID_K_THERMO, n_perm, cost_bps, db)


def main() -> int:
    ap = argparse.ArgumentParser(description="涨停温度计轴探针（四族×三关闸门）")
    ap.add_argument("--families", default="F1,F2,F3,F4")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--rebuild-thermometer", action="store_true",
                    help="强制重建温度计缓存")
    args = ap.parse_args()

    if args.rebuild_thermometer:
        build_thermometer(force=True)
    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    panel, verdicts = run_probe(families, args.n_perm, args.cost_bps)

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out_dir / "thermo_probe.csv", index=False)
    verdicts.to_csv(out_dir / "thermo_probe_verdicts.csv", index=False)

    show = panel[panel["kou_jing"] == "blend"].copy()
    for c in ["ic"] + [f"ic_{h}" for h in HALVES]:
        show[c] = show[c].round(3)
    print("=== IC 面板（blend 口径） ===")
    print(show.drop(columns=["kou_jing"]).to_string(index=False))
    print("\n=== 逐族三关裁决 ===")
    for _, v in verdicts.iterrows():
        print(f"\n-- {v['family']} --")
        for key, val in v.items():
            if key == "family":
                continue
            print(f"  {key}: {val:.4f}" if isinstance(val, float) else f"  {key}: {val}")
    n_pass = int(verdicts["PASS"].sum())
    print(f"\n{'★ PASS：' + str(n_pass) + ' 族过闸 → 进 dual_legs_external_short 装配'
          if n_pass else '✗ STOP：全族停线 → 温度轴第五轴归档'}")
    print(f"→ {out_dir / 'thermo_probe.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
