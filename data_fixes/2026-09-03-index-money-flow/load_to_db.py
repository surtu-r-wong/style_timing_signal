"""raw/*.csv → stock_selector.index_money_flow（UPSERT），并做入库前体检。

体检（硬闸任一失败即不写库，退出码 1）：
  硬闸 1 恒等式：main_in − main_out == extra_bill + large_bill（2026-09-03 实测 7403 行零违反，
     这是唯一机械成立的关系；Wind 另给的 maininflowmoney 字段在 25% 的行与之不符，
     四档净流入零和在 41% 的行不成立——两者都只报数不设闸）
  硬闸 2 交易日历：与 index_daily 的 000300.SH 日期集合比，各板块自身区间内零缺日
  硬闸 3 只数上界：固定成分指数 ≤ 成分数（停牌股不计入，故只有上界是硬的；
     2015-07 千股停牌期沪深300 实测低到 204，属真实事件，不设下界）
  硬闸 4 对账：与用户 Wind 终端导出 CSV（沪深300 65 天）逐列比值恒定，记单位倍率
  硬闸 5 金额列无 NaN
用法：python load_to_db.py [--dry-run] [--exchange-csv PATH]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backtest.data import _connect, load_db_config  # noqa: E402

WIND2DB = {
    "maininflowcount": "main_inflow_count", "mainoutflowcount": "main_outflow_count",
    "maininmoney": "main_in_money", "mainoutmoney": "main_out_money",
    "maininflowmoney": "main_inflow_money", "openmaininflowmoney": "open_main_inflow_money",
    "endmaininflowmoney": "end_main_inflow_money", "extrabillinflowmoney": "extra_bill_inflow_money",
    "largebillinflowmoney": "large_bill_inflow_money", "middlebillinflowmoney": "middle_bill_inflow_money",
    "smallbillinflowmoney": "small_bill_inflow_money",
}
# 2026-09-03 实测：wset marketmoneyflows 只支持 沪深300/创业板/科创板 三个板块，
# index_code 取该板块对应的收益率指数（沪深300 / 创业板综 / 科创综指）。
COUNT_CAP = {"000300.SH": 300}   # 固定成分数的指数才有硬上界；板块无
SECTOR_OF = {"000300.SH": "csi_300", "399102.SZ": "chinext", "000680.SH": "star"}
MONEY = [c for c in WIND2DB if c.endswith("money")]


def load_raw() -> pd.DataFrame:
    parts = [pd.read_csv(p) for p in sorted((HERE / "raw").glob("*.csv"))]
    if not parts:
        raise SystemExit("raw/ 为空")
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in MONEY:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    dup = df.duplicated(["index_code", "date"]).sum()
    if dup:
        raise SystemExit(f"重复键 {dup} 行")
    return df.sort_values(["index_code", "date"]).reset_index(drop=True)


def health(df: pd.DataFrame, exchange_csv: Path | None) -> dict:
    rep: dict = {"rows": len(df), "by_index": df.groupby("index_code")["date"].agg(["min", "max", "count"]).astype(str).to_dict("index")}
    # 硬闸 1：唯一机械恒等式
    e_main = (df.maininmoney - df.mainoutmoney - df.extrabillinflowmoney - df.largebillinflowmoney).abs()
    rep["identity_main_max_err"] = float(e_main.max())
    rep["identity_ok"] = bool(e_main.max() <= 1e-3)
    # 只报数不设闸：Wind 的 maininflowmoney 字段与恒等式的偏离、四档零和偏离
    e_field = (df.maininflowmoney - (df.maininmoney - df.mainoutmoney)).abs()
    e_bills = df[["extrabillinflowmoney", "largebillinflowmoney",
                  "middlebillinflowmoney", "smallbillinflowmoney"]].sum(axis=1).abs()
    rep["reported_only"] = {
        "maininflowmoney_vs_identity": {"n_violations": int((e_field > 1e-3).sum()),
                                        "p99": float(e_field.quantile(0.99)), "max": float(e_field.max())},
        "four_bills_sum": {"n_violations": int((e_bills > 1e-3).sum()),
                           "p99": float(e_bills.quantile(0.99)), "max": float(e_bills.max())},
    }
    rep["nan_money_rows"] = int(df[MONEY].isna().any(axis=1).sum())
    # 硬闸 3：只数上界（固定成分指数才有硬上界；板块股票数随 IPO 增长，无上界）
    df = df.copy()
    df["cnt"] = df.maininflowcount.fillna(0) + df.mainoutflowcount.fillna(0)
    cap = df.index_code.map(COUNT_CAP)
    over = df[cap.notna() & (df.cnt > cap)]
    rep["count_over_cap_rows"] = int(len(over))
    rep["count_range"] = {k: [int(v.min()), int(v.max())] for k, v in df.groupby("index_code").cnt}
    if len(over):
        rep["count_over_sample"] = over[["index_code", "date", "cnt"]].head(5).astype(str).values.tolist()
    # 只数日环比跳变只报数：2015-07 千股停牌、科创板早期 IPO 爬坡都会造成大跳，与截断不可区分；
    # 截断已由日历闸（硬闸 2）直接覆盖。
    jump = df.groupby("index_code").cnt.transform(lambda x: (x / x.shift(1) - 1).abs())
    rep["count_jump_gt5pct_days"] = int((jump > 0.05).sum())
    rep["count_ok"] = bool(len(over) == 0 and (df.cnt > 0).all())
    # 硬闸 2：交易日历覆盖（各板块自身区间内不得缺日）
    db = load_db_config()
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT trade_date FROM {db['schema']}.index_daily "
                    "WHERE index_code='000300.SH' ORDER BY trade_date")
        cal_set = {r[0] for r in cur.fetchall()}
    cal_max = max(cal_set)
    cov = {}
    for k, v in df.groupby("index_code"):
        have = set(v.date)
        want = {d for d in cal_set if v.date.min() <= d <= min(v.date.max(), cal_max)}
        missing = sorted(want - have)
        cov[k] = {"days": len(have), "n_missing": len(missing),
                  "missing_sample": [str(d) for d in missing[:5]]}
    rep["calendar"] = cov
    rep["calendar_ok"] = bool(all(c["n_missing"] == 0 for c in cov.values()))
    rep["reconcile_ok"] = None
    if exchange_csv and exchange_csv.exists():
        ex = pd.read_csv(exchange_csv, encoding="gb18030", skipfooter=3, engine="python")
        ex["date"] = pd.to_datetime(ex["date"]).dt.date
        m = df[df.index_code == "000300.SH"].merge(ex, on="date", suffixes=("", "_ex"))
        if len(m):
            ratios = {}
            for c in MONEY:
                r = (m[c] / m[f"{c}_ex"]).replace([float("inf")], float("nan")).dropna()
                ratios[c] = (float(r.min()), float(r.max()))
            cnt_eq = bool((m.maininflowcount == m.maininflowcount_ex).all() and (m.mainoutflowcount == m.mainoutflowcount_ex).all())
            lo = min(v[0] for v in ratios.values()); hi = max(v[1] for v in ratios.values())
            unit = 1.0 if abs(lo - 1) < 1e-6 and abs(hi - 1) < 1e-6 else (1e4 if abs(lo - 1e4) < 1 and abs(hi - 1e4) < 1 else None)
            rep["reconcile"] = {"overlap_days": int(len(m)), "count_equal": cnt_eq, "money_ratio_range": (lo, hi), "unit_multiplier": unit}
            rep["reconcile_ok"] = bool(cnt_eq and unit is not None)
    return rep


def upsert(df: pd.DataFrame, fetched: date) -> int:
    from psycopg2.extras import execute_values
    db = load_db_config(); conn = _connect(db)
    cols = ["index_code", "trade_date", "wind_sector", *WIND2DB.values(), "fetched_at"]
    rows = [[r.index_code, r.date, r.wind_sector, *[None if pd.isna(getattr(r, w)) else (int(getattr(r, w)) if w.endswith("count") else float(getattr(r, w))) for w in WIND2DB], fetched] for r in df.itertuples()]
    sql = (f"INSERT INTO {db['schema']}.index_money_flow ({','.join(cols)}) VALUES %s "
           f"ON CONFLICT (index_code, trade_date) DO UPDATE SET "
           + ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("index_code", "trade_date"))
           + ", updated_at=NOW()")
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=2000)
            cur.execute(f"SELECT count(*) FROM {db['schema']}.index_money_flow")
            n = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return int(n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--exchange-csv", default="/home/elfbob/exchange/20260903/marketmoneyflows.csv")
    a = p.parse_args(argv)
    df = load_raw()
    rep = health(df, Path(a.exchange_csv))
    import json; print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    ok = (rep["identity_ok"] and rep["count_ok"] and rep["calendar_ok"]
          and rep["reconcile_ok"] is not False and rep["nan_money_rows"] == 0)
    if not ok:
        print("HEALTH FAIL — 不写库"); return 1
    if a.dry_run:
        print("dry-run，不写库"); return 0
    n = upsert(df, date.today())
    print(f"UPSERT 完成，表内总行数 {n}")
    (HERE / "load_receipt.json").write_text(json.dumps({"loaded_rows": len(df), "table_rows": n, "health": rep, "fetched_at": str(date.today())}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
