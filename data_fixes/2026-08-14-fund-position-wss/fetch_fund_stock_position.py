#!/usr/bin/env python3
"""Probe 7: pull fund real stock position (Wind wss ``prt_stocktonav``) for
41 quarter-ends 2016-03-31..2026-03-31, universe = per-period DISTINCT
fund_code from stock_selector.fund_stock_holdings.

Gateway surface: GET /fetch/financial_snapshot (caller-specified fields —
bills codes x fields = 1 cell per fund x period; the fund_holder_structure
endpoint would bill 5 fields, 5x the approved budget, so it is NOT used).
Gateway URL + token from stock_selector/config/settings.yaml (read-only).

Discipline: serial batches <=500 codes, sleep >=1s between batches, per-batch
max 2 retries (10s/30s), 3 consecutive failed batches -> save progress and
abort. Per-period append to CSV + progress.json for resume.

Budget guard: hard cap on requested cells (approved ~110,353 + trial margin).
"""
import json
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(PROJECT_ROOT))
from backtest.data import load_db_config, _connect  # noqa: E402

GATEWAY = "http://100.120.152.1:8080"
TOKEN = "mysecret_2026_04_27_xxx"  # stock_selector/config/settings.yaml wind_gateway.token
FIELD = "prt_stocktonav"
CHUNK = 500
SLEEP_BETWEEN_BATCHES = 1.0
RETRY_SLEEPS = [10, 30]          # max 2 retries per batch
MAX_CONSEC_BATCH_FAILURES = 3
CELL_HARD_CAP = 112000           # approved ~110,353 + trial/margin

EVIDENCE_DIR = Path("/home/elfbob/claude-code/style_timing_signal/data_fixes/2026-08-14-fund-position-wss")
OUT_CSV = PROJECT_ROOT / "backtest" / "output" / "fund_stock_position.csv"
PROGRESS = EVIDENCE_DIR / "progress.json"
LOG = EVIDENCE_DIR / "run.log"

START, END = date(2016, 3, 31), date(2026, 3, 31)

# Local Clash proxy (HTTP_PROXY=127.0.0.1:7897) mangles Tailscale-IP requests
# into non-JSON error pages — bypass env proxies entirely.
SESSION = requests.Session()
SESSION.trust_env = False


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {"done_periods": [], "cells_requested": 0, "rows_written": 0,
            "period_stats": {}}


def save_progress(p: dict) -> None:
    tmp = PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2))
    tmp.replace(PROGRESS)


def gateway_get(params: dict) -> dict:
    r = SESSION.get(
        f"{GATEWAY}/fetch/financial_snapshot", params=params,
        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=180,
    )
    body = r.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"gateway error: {json.dumps(body)[:300]}")
    return body


def quota_used() -> int:
    try:
        r = SESSION.get(f"{GATEWAY}/quota",
                        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        return r.json().get("used", -1)
    except Exception:
        return -1


def main() -> int:
    conn = _connect(load_db_config())
    cur = conn.cursor()
    cur.execute("SET statement_timeout='120s'")
    cur.execute(
        """
        SELECT report_date, fund_code, MAX(ann_date) AS ann_date
        FROM stock_selector.fund_stock_holdings
        WHERE report_date BETWEEN %s AND %s
        GROUP BY 1, 2
        ORDER BY 1, 2
        """, (START, END))
    universe: dict[str, list[tuple[str, str]]] = {}
    for rd, fc, ad in cur.fetchall():
        universe.setdefault(rd.isoformat(), []).append(
            (fc, ad.isoformat() if ad else ""))
    conn.close()

    periods = sorted(universe)
    total_universe = sum(len(v) for v in universe.values())
    prog = load_progress()
    log(f"START run: {len(periods)} periods, universe {total_universe} fund x period; "
        f"already done: {len(prog['done_periods'])}; gateway quota used={quota_used()}")

    if not OUT_CSV.exists():
        OUT_CSV.write_text("fund_code,report_date,stock_to_nav_pct,ann_date\n")

    consec_failures = 0
    for period in periods:
        if period in prog["done_periods"]:
            continue
        entries = universe[period]
        codes = [c for c, _ in entries]
        ann_map = dict(entries)
        values: dict[str, object] = {}
        period_cells = 0
        n_batches = (len(codes) + CHUNK - 1) // CHUNK
        for bi in range(n_batches):
            chunk = codes[bi * CHUNK:(bi + 1) * CHUNK]
            if prog["cells_requested"] + period_cells + len(chunk) > CELL_HARD_CAP:
                log(f"ABORT: cell hard cap {CELL_HARD_CAP} would be exceeded "
                    f"(requested so far {prog['cells_requested'] + period_cells})")
                save_progress(prog)
                return 2
            ok = False
            for attempt in range(1 + len(RETRY_SLEEPS)):
                try:
                    body = gateway_get({
                        "codes": ",".join(chunk), "fields": FIELD,
                        "rpt_date": period, "options": "unit=1",
                    })
                    period_cells += len(chunk)  # billed per attempt reaching Wind
                    cols = body["columns"]
                    vi = cols.index(FIELD)
                    for row in body["rows"]:
                        values[row[0]] = row[vi]
                    ok = True
                    break
                except Exception as e:
                    log(f"batch fail period={period} batch={bi + 1}/{n_batches} "
                        f"attempt={attempt + 1}: {type(e).__name__}: {str(e)[:200]}")
                    if attempt < len(RETRY_SLEEPS):
                        time.sleep(RETRY_SLEEPS[attempt])
            if not ok:
                consec_failures += 1
                log(f"batch EXHAUSTED period={period} batch={bi + 1}/{n_batches} "
                    f"(consecutive failures {consec_failures})")
                if consec_failures >= MAX_CONSEC_BATCH_FAILURES:
                    log("STOP: 3 consecutive batch failures — saving progress, aborting")
                    prog["cells_requested"] += period_cells
                    save_progress(prog)
                    return 1
            else:
                consec_failures = 0
            time.sleep(SLEEP_BETWEEN_BATCHES)
        # period complete only if every code got a response row
        missing = [c for c in codes if c not in values]
        if missing:
            log(f"period={period} INCOMPLETE: {len(missing)} codes missing from "
                f"responses — not marking done")
            prog["cells_requested"] += period_cells
            save_progress(prog)
            return 1
        # append rows
        lines = []
        nulls = 0
        oob = 0
        vals = []
        for c in codes:
            v = values[c]
            if v is None or (isinstance(v, float) and v != v):
                nulls += 1
                sval = ""
            else:
                vals.append(float(v))
                if v < 0 or v > 100:
                    oob += 1
                sval = f"{float(v):.6f}"
            lines.append(f"{c},{period},{sval},{ann_map[c]}")
        with OUT_CSV.open("a") as f:
            f.write("\n".join(lines) + "\n")
        med = round(statistics.median(vals), 2) if vals else None
        prog["done_periods"].append(period)
        prog["cells_requested"] += period_cells
        prog["rows_written"] += len(codes)
        prog["period_stats"][period] = {
            "universe": len(codes), "rows": len(codes), "cells": period_cells,
            "nulls": nulls, "out_of_range": oob, "median": med,
        }
        save_progress(prog)
        log(f"period={period} DONE rows={len(codes)} cells={period_cells} "
            f"nulls={nulls} oob={oob} median={med}")
    log(f"ALL DONE: rows={prog['rows_written']} cells_requested={prog['cells_requested']} "
        f"gateway quota used={quota_used()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
