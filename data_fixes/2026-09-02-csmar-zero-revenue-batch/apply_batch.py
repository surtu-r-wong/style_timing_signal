"""Patch CSMAR income rows that omit revenue keys with Wind-sourced zero revenue.

Targets come from wind_fetch.json (role == "target" and tot_oper_rev is not null).
Rows where Wind also returned null are NOT written. Same in-place method as
data_fixes/2026-09-02-b3-salg-000820-revenue/apply_fix.py. Usage: apply_batch.py [--dry-run]
"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
from signals.common.config import load_db_config
from signals.style_basket.build import _connect
from signals.common.financial_field_map import translate_data

HERE = "data_fixes/2026-09-02-csmar-zero-revenue-batch"
dry = "--dry-run" in sys.argv
fetch = json.load(open(f"{HERE}/wind_fetch.json"))
targets = [r for r in fetch["rows"] if r["role"] == "target" and r["tot_oper_rev"] is not None]
skipped = [r for r in fetch["rows"] if r["role"] == "target" and r["tot_oper_rev"] is None]
print("writable targets:", len(targets), "| wind-null (not written):", len(skipped), "| dry_run:", dry)
db = load_db_config(); S = db["schema"]; print("db host:", db["host"], "schema:", S)
conn = _connect(db); cur = conn.cursor()
sel = f"""SELECT ann_date, data, updated_at FROM {S}.stock_financial
         WHERE ts_code=%s AND statement_type='income' AND data_source='csmar' AND end_date=%s"""
snap = {"after": {}, "before_reconstructed": {}, "written": [], "already_present": []}
for r in targets:
    t, d = r["ts_code"], r["end_date"]
    cur.execute(sel, (t, d)); row = cur.fetchone(); assert row, (t, d)
    if "B001100000" in row[1]:
        snap["already_present"].append(f"{t}|{d}"); continue
    patch = {"B001100000": float(r["tot_oper_rev"]), "B001101000": float(r["oper_rev"]),
             "_backfill_2026_09_02": {"fields": ["B001100000", "B001101000"],
                 "source": "wind:wss:tot_oper_rev,oper_rev;rptType=1;unit=1",
                 "wind_stm_issuingdate": r["stm_issuingdate"],
                 "reason": "CSMAR omits zero revenue keys; breaks revenue TTM/SalG chains",
                 "record": f"{HERE}/README.md"}}
    if not dry:
        cur.execute(f"""UPDATE {S}.stock_financial SET data = data || %s::jsonb, updated_at = now()
            WHERE ts_code=%s AND statement_type='income' AND data_source='csmar' AND end_date=%s
            AND NOT (data ? 'B001100000')""", (json.dumps(patch), t, d))
        assert cur.rowcount == 1, (t, d, cur.rowcount)
    snap["written"].append(f"{t}|{d}")
conn.rollback() if dry else conn.commit()
ok = 0
for r in targets:
    t, d = r["ts_code"], r["end_date"]
    cur.execute(sel, (t, d)); row = cur.fetchone()
    snap["after"][f"{t}|{d}"] = {"ann_date": str(row[0]), "updated_at": str(row[2]), "data": row[1]}
    m = row[1].get("_backfill_2026_09_02")
    if m:
        snap["before_reconstructed"][f"{t}|{d}"] = {k: v for k, v in row[1].items() if k not in set(m["fields"]) | {"_backfill_2026_09_02"}}
    rev = translate_data(row[1], "csmar", "income").get("revenue")
    ok += (rev == 0.0)
print("written:", len(snap["written"]), "| already_present:", len(snap["already_present"]), "| read-side revenue==0.0:", ok, "/", len(targets))
conn.close()
if not dry:
    out = f"{HERE}/db_snapshot_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    json.dump(snap, open(out, "w"), ensure_ascii=False, indent=1, default=str); print("snapshot:", out)
