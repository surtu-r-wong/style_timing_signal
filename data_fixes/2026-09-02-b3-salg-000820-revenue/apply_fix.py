"""Backfill zero operating revenue for 000820.SZ 2020-03-31 / 2021-03-31 (CSMAR income rows).

CSMAR omits B001100000/B001101000 when the value is zero; Wind wss (rptType=1)
returns tot_oper_rev = oper_rev = 0.0 for both quarters and matches CSMAR exactly
on the adjacent 2020-06-30 / 2021-06-30 quarters. B3 and the shared financial
reader only read data_source='csmar' for end_date <= CSMAR_END, and the
stock_financial PK is (ts_code, end_date, statement_type), so the fix patches the
existing CSMAR rows in place. Idempotent: rows that already carry B001100000 are
left untouched. Usage: python apply_fix.py [--dry-run]
"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
from signals.common.config import load_db_config
from signals.style_basket.build import _connect
from signals.common.financial_field_map import translate_data as map_fields

TICKER = "000820.SZ"
TARGETS = {  # end_date -> Wind wss values fetched 2026-09-02 (see wind_fetch.json)
    "2020-03-31": {"tot_oper_rev": 0.0, "oper_rev": 0.0, "stm_issuingdate": "2020-04-30"},
    "2021-03-31": {"tot_oper_rev": 0.0, "oper_rev": 0.0, "stm_issuingdate": "2021-04-29"},
}
dry = "--dry-run" in sys.argv
db = load_db_config(); S = db["schema"]
print("db host:", db.get("host"), "schema:", S, "dry_run:", dry)
conn = _connect(db); cur = conn.cursor()
sel = f"""SELECT ts_code, end_date, statement_type, ann_date, data_source, data, updated_at
         FROM {S}.stock_financial WHERE ts_code=%s AND statement_type='income'
         AND data_source='csmar' AND end_date=%s"""
snap = {"before": {}, "after": {}}
for end_date, w in TARGETS.items():
    cur.execute(sel, (TICKER, end_date)); r = cur.fetchone()
    assert r is not None, f"row missing {end_date}"
    data = r[5]
    snap["before"][end_date] = {"ann_date": str(r[3]), "updated_at": str(r[6]), "data": data}
    if "B001100000" in data:
        print(end_date, "already has B001100000 =", data["B001100000"], "-> skip"); continue
    patch = {
        "B001100000": float(w["tot_oper_rev"]),
        "B001101000": float(w["oper_rev"]),
        "_backfill_2026_09_02": {
            "fields": ["B001100000", "B001101000"],
            "source": "wind:wss:tot_oper_rev,oper_rev;rptType=1;unit=1",
            "wind_stm_issuingdate": w["stm_issuingdate"],
            "reason": "CSMAR omits zero revenue keys; B3 SalG TTM chain broke (SALG_FRESHNESS)",
            "record": "data_fixes/2026-09-02-b3-salg-000820-revenue/README.md",
        },
    }
    print(end_date, "patch:", json.dumps(patch))
    if not dry:
        cur.execute(
            f"""UPDATE {S}.stock_financial SET data = data || %s::jsonb, updated_at = now()
                WHERE ts_code=%s AND statement_type='income' AND data_source='csmar'
                AND end_date=%s AND NOT (data ? 'B001100000')""",
            (json.dumps(patch), TICKER, end_date))
        print("  rows updated:", cur.rowcount)
if dry:
    conn.rollback()
else:
    conn.commit()
for end_date in TARGETS:
    cur.execute(sel, (TICKER, end_date)); r = cur.fetchone()
    snap["after"][end_date] = {"ann_date": str(r[3]), "updated_at": str(r[6]), "data": r[5]}
    marker = r[5].get("_backfill_2026_09_02")
    if marker:  # reconstruct pre-patch state exactly: the patch was a pure key merge
        snap["before_reconstructed"] = snap.get("before_reconstructed", {})
        snap["before_reconstructed"][end_date] = {k: v for k, v in r[5].items() if k not in set(marker["fields"]) | {"_backfill_2026_09_02"}}
    rev = map_fields(r[5], "csmar", "income").get("revenue") if not dry else None
    print(end_date, "after: has_rev=", "B001100000" in r[5], "extract revenue=", rev)
conn.close()
if not dry:
    out = f"data_fixes/2026-09-02-b3-salg-000820-revenue/db_snapshot_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    json.dump(snap, open(out, "w"), ensure_ascii=False, indent=2, default=str); print("snapshot:", out)
