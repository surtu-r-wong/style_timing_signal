"""raw/*.csv → stock_selector.index_money_flow（UPSERT），并做入库前体检。

体检（任一失败即不写库，退出码 1）：
  1. 恒等式：main = in − out；main = extra + large；四档之和 = 0（容差 0.01）
  2. 只数：maininflowcount + mainoutflowcount 在成分数 ±5 内（停牌/调样日允许）
  3. 与用户 Wind 终端导出 CSV（/home/elfbob/exchange/20260903/marketmoneyflows.csv，沪深300）
     同日逐列对账：逐字相等或恒为 10^4 倍（记录单位倍率）
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
EXPECTED_COUNT = {"000300.SH": 300, "000905.SH": 500, "000852.SH": 1000, "932000.CSI": 2000}
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
    tol = 0.01
    e1 = (df.maininflowmoney - (df.maininmoney - df.mainoutmoney)).abs().max()
    e2 = (df.maininflowmoney - (df.extrabillinflowmoney + df.largebillinflowmoney)).abs().max()
    e3 = df[["extrabillinflowmoney", "largebillinflowmoney", "middlebillinflowmoney", "smallbillinflowmoney"]].sum(axis=1).abs().max()
    rep["identity_max_err"] = {"in_minus_out": float(e1), "extra_plus_large": float(e2), "four_bills_sum": float(e3)}
    rep["identity_ok"] = bool(max(e1, e2, e3) <= tol)
    cnt = df.maininflowcount.fillna(0) + df.mainoutflowcount.fillna(0)
    exp = df.index_code.map(EXPECTED_COUNT)
    bad = df[(cnt - exp).abs() > 5]
    rep["count_bad_rows"] = int(len(bad)); rep["count_ok"] = bool(len(bad) == 0)
    if len(bad):
        rep["count_bad_sample"] = bad[["index_code", "date", "maininflowcount", "mainoutflowcount"]].head(5).astype(str).values.tolist()
    rep["nan_money_rows"] = int(df[MONEY].isna().any(axis=1).sum())
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
    ok = rep["identity_ok"] and rep["count_ok"] and rep["reconcile_ok"] is not False
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
