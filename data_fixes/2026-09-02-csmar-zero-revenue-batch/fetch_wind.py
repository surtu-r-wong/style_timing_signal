"""Read-only: list CSMAR income quarter rows that lack B001100000 (but have net profit),
fetch Wind wss revenue for each target quarter plus one CSMAR-valued control quarter per
ticker, cross-check stm_issuingdate with stock_first_disclosure. Writes wind_fetch.json."""
import json, sys
from datetime import date, datetime, timezone
sys.path.insert(0, "/home/elfbob/claude-code/stock_selector")
from stock_selector.config import load_config, resolve_settings_path
from stock_selector.data.wind_source import WindDataSource
from stock_selector.db.connection import get_connection, pg_config_from

cfg = load_config(resolve_settings_path()); pg = pg_config_from(cfg)
print("db host:", pg.get("host"), "schema:", pg.get("schema") or pg.get("options"))
S = "stock_selector"; _cm = get_connection(pg); conn = _cm.__enter__(); cur = conn.cursor()
cur.execute(f"""SELECT ts_code, end_date FROM {S}.stock_financial
 WHERE statement_type='income' AND data_source='csmar' AND NOT (data ? 'B001100000') AND data ? 'B001000000'
 AND end_date BETWEEN '2013-01-01' AND '2025-03-31' AND extract(month from end_date) IN (3,6,9,12)
 AND extract(day from end_date) >= 28 ORDER BY 1,2""")
targets = [(r[0], r[1]) for r in cur.fetchall()]
tickers = sorted({t for t, _ in targets})
print("targets:", len(targets), "tickers:", len(tickers))
controls = {}
for t in tickers:
    cur.execute(f"""SELECT end_date, (data->>'B001100000')::float FROM {S}.stock_financial
      WHERE ts_code=%s AND statement_type='income' AND data_source='csmar' AND data ? 'B001100000'
      AND end_date BETWEEN '2016-01-01' AND '2025-03-31' AND extract(day from end_date) >= 28
      ORDER BY abs(end_date - %s) LIMIT 1""", (t, min(d for x, d in targets if x == t)))
    r = cur.fetchone(); controls[t] = (r[0], r[1]) if r else None
cur.execute(f"SELECT ts_code, end_date, first_disclosure_date FROM {S}.stock_first_disclosure WHERE ts_code = ANY(%s)", (tickers,))
fd = {(r[0], r[1]): r[2] for r in cur.fetchall()}
_cm.__exit__(None, None, None)

w = WindDataSource.from_settings(cfg)
by_q = {}
for t, d in targets: by_q.setdefault(d, set()).add(t)
for t, c in controls.items():
    if c: by_q.setdefault(c[0], set()).add(t)
rows = []
for q in sorted(by_q):
    df = w.fetch_financial_snapshot(sorted(by_q[q]), ["tot_oper_rev", "oper_rev", "stm_issuingdate"], q)
    for rec in df.to_dict("records"):
        t = rec["ts_code"]; is_target = (t, q) in set(targets)
        rows.append({"ts_code": t, "end_date": q.isoformat(),
                     "tot_oper_rev": None if rec["tot_oper_rev"] != rec["tot_oper_rev"] else rec["tot_oper_rev"],
                     "oper_rev": None if rec["oper_rev"] != rec["oper_rev"] else rec["oper_rev"],
                     "stm_issuingdate": str(rec["stm_issuingdate"])[:10],
                     "role": "target" if is_target else "control",
                     "csmar_B001100000": None if is_target else controls[t][1],
                     "db_first_disclosure": str(fd.get((t, q))) if (t, q) in fd else None})
w.close()
out = {"fetched_at": datetime.now(timezone.utc).isoformat(), "endpoint": "/fetch/financial_snapshot", "options": "rptType=1;unit=1",
       "n_targets": len(targets), "n_tickers": len(tickers), "rows": rows}
json.dump(out, open("/home/elfbob/claude-code/style_timing_signal/data_fixes/2026-09-02-csmar-zero-revenue-batch/wind_fetch.json", "w"), ensure_ascii=False, indent=1, default=str)
import pandas as pd; pd.set_option("display.width", 250); pd.set_option("display.max_rows", 100)
df = pd.DataFrame(rows); print(df.to_string(index=False))
tg = df[df.role == "target"]
print("\ntargets: wind null:", tg.tot_oper_rev.isna().sum(), "| zero:", (tg.tot_oper_rev == 0).sum(), "| nonzero:", ((tg.tot_oper_rev != 0) & tg.tot_oper_rev.notna()).sum())
ct = df[df.role == "control"]; print("controls match CSMAR:", ((ct.tot_oper_rev - ct.csmar_B001100000).abs() < 0.01).sum(), "/", len(ct))
print("target stm_issuingdate == db first disclosure:", (tg.stm_issuingdate == tg.db_first_disclosure).sum(), "/", len(tg), "| db missing:", tg.db_first_disclosure.isna().sum())
