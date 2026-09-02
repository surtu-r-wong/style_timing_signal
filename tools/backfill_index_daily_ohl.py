"""Fill open/high/low (and NULL volume/amount) of stock_selector.index_daily from the
Wind gateway /fetch/index_daily. Never touches close, never inserts rows.

Why a dedicated tool: the regular topup path (stock_selector backfill CLI) upserts every
column including close; re-fetching a decade of history that way could silently rewrite
the close series that Gate 0 anchors and the co-movement sentinel are registered on.
This tool only COALESCEs NULL cells. Rows that do not exist in the table are skipped and
counted, not inserted.

Usage:
  python tools/backfill_index_daily_ohl.py --codes 000918.CSI,000919.CSI \
      --start 2014-01-02 --end 2026-09-01 --receipt data_fixes/.../receipt.json [--dry-run]
"""
from __future__ import annotations
import argparse, json, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import requests, yaml
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from signals.common.config import load_db_config
from signals.style_basket.build import _connect

GW_SETTINGS = Path("/home/elfbob/claude-code/stock_selector/config/settings.yaml")
CHUNK_DAYS = 120  # 2 codes × 6 fields × ~85 trading days ≈ 1,000 cells per wsd call


def gateway():
    g = yaml.safe_load(open(GW_SETTINGS))["wind_gateway"]
    s = requests.Session(); s.trust_env = False
    s.headers["Authorization"] = f"Bearer {g['token']}"
    return g["url"].rstrip("/"), s


def fetch(url, s, codes, start, end):
    r = s.get(f"{url}/fetch/index_daily", params={"indices": codes, "start": start.isoformat(), "end": end.isoformat()}, timeout=120)
    r.raise_for_status(); b = r.json()
    if b.get("status") != "ok":
        raise RuntimeError(b)
    return b["columns"], b["rows"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True); ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True); ap.add_argument("--receipt", required=True)
    ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args()
    url, s = gateway(); db = load_db_config(); S = db["schema"]; conn = _connect(db); cur = conn.cursor()
    q0 = s.get(f"{url}/quota", timeout=30).json()
    stats = {"fetched_rows": 0, "wind_open_null": 0, "updated": 0, "row_missing": 0, "already_filled": 0, "chunks": 0}
    per_code = {}
    t0 = time.time(); cur_start = a.start
    while cur_start <= a.end:
        cur_end = min(cur_start + timedelta(days=CHUNK_DAYS - 1), a.end)
        cols, rows = fetch(url, s, a.codes, cur_start, cur_end); stats["chunks"] += 1
        ix = {c: i for i, c in enumerate(cols)}
        for r in rows:
            stats["fetched_rows"] += 1
            code, td = r[ix["ts_code"]], r[ix["trade_date"]]
            o, h, l = r[ix["open"]], r[ix["high"]], r[ix["low"]]
            v, amt = r[ix.get("volume", -1)] if "volume" in ix else None, r[ix.get("amt", -1)] if "amt" in ix else None
            pc = per_code.setdefault(code, {"fetched": 0, "updated": 0, "wind_open_null": 0, "row_missing": 0, "first_open": None, "last_open": None})
            pc["fetched"] += 1
            if o is None:
                stats["wind_open_null"] += 1; pc["wind_open_null"] += 1; continue
            if pc["first_open"] is None: pc["first_open"] = td
            pc["last_open"] = td
            if a.dry_run:
                continue
            cur.execute(f"""UPDATE {S}.index_daily SET open = COALESCE(open, %s), high = COALESCE(high, %s), low = COALESCE(low, %s),
                            volume = COALESCE(volume, %s), amount = COALESCE(amount, %s), updated_at = now()
                            WHERE index_code = %s AND trade_date = %s AND open IS NULL""", (o, h, l, v, amt, code, td))
            if cur.rowcount == 1:
                stats["updated"] += 1; pc["updated"] += 1
            else:
                cur.execute(f"SELECT 1 FROM {S}.index_daily WHERE index_code=%s AND trade_date=%s", (code, td))
                if cur.fetchone(): stats["already_filled"] += 1
                else: stats["row_missing"] += 1; pc["row_missing"] += 1
        cur_start = cur_end + timedelta(days=1)
    conn.rollback() if a.dry_run else conn.commit()
    q1 = s.get(f"{url}/quota", timeout=30).json()
    receipt = {"generated_at": datetime.now(timezone.utc).isoformat(), "codes": a.codes, "start": a.start.isoformat(), "end": a.end.isoformat(),
               "dry_run": a.dry_run, "db_host": db["host"], "stats": stats, "per_code": per_code,
               "gateway_quota_before": q0, "gateway_quota_after": q1, "gateway_cells_used": (q1.get("used", 0) - q0.get("used", 0)),
               "elapsed_s": round(time.time() - t0, 1)}
    Path(a.receipt).parent.mkdir(parents=True, exist_ok=True)
    Path(a.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: receipt[k] for k in ("stats", "gateway_cells_used", "elapsed_s")}, ensure_ascii=False)); print(json.dumps(per_code, default=str))
    conn.close()


if __name__ == "__main__":
    main()
