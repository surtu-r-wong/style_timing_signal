#!/usr/bin/env python3
"""Verify the CSMAR_END par-anchor rerun recovered the ~639 gap tickers.

Read-only. Does NOT run the backfill — that is a separate, gated CLI command
(`python -m stock_selector.backfill.cli share-capital`). This script only
snapshots and compares state around that rerun.

Root cause / fix context: see ../../stock_selector code fix commit c31104e and
stock_selector/docs/plans/2026-07-25-share-capital-par-contemporaneous-anchor.md.
The ~696 gap tickers are A-share names whose entire CSMAR share history was
flagged par_unknown because par calibration used a 2026 overlap window against a
CSMAR_END(=2025-03-31) A003101000 numerator; cross-period share drift pushed
implied par outside the +/-5% snap tolerance. The fix re-anchors calibration to
[CSMAR_END, CSMAR_END+180d). Probed pre-fix (2026-07-25): same-period anchor
snaps 639/696, the 2026 window snapped 1/696.

Usage (run under the memory guard is unnecessary — this is light, read-only):

    # BEFORE the prod rerun (captures baseline):
    python verify_par_recovery.py --phase before

    # ... then run the rerun once (separate, gated):
    #   cd stock_selector && .venv/bin/python -m stock_selector.backfill.cli share-capital --use-test   # sandbox first
    #   cd stock_selector && .venv/bin/python -m stock_selector.backfill.cli share-capital              # prod

    # AFTER the rerun (verifies recovery + emits the residual tail):
    python verify_par_recovery.py --phase after

Outputs (written next to this script):
  before -> gap_before.csv, valued_tickers_before.csv
  after  -> gap_after.csv, tail.csv, and prints recovered / regression counts.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import psycopg2
import yaml

# B3 preflight window: first required formation month 2014-01, data-end 2023-12-31.
WINDOW_START = "2014-01-01"
WINDOW_END = "2023-12-31"
# The first *required* formation date a ticker is measured against: max(list, window start).
HERE = Path(__file__).resolve().parent
SETTINGS = Path(
    "/home/elfbob/claude-code/style_timing_signal/config/settings.yaml"
)


def connect(schema: str):
    cfg = yaml.safe_load(open(SETTINGS))["database"]
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["name"],
        user=cfg["user"],
        password=cfg["password"],
        connect_timeout=8,
        options=f"-c statement_timeout=180000 -c search_path={schema}",
    )
    return conn


# Window-active SH/SZ tickers whose earliest VALUED (total_shares>0) share-capital
# node postdates their first required formation date -> as-of lookup finds no shares
# at formation -> DATA_MISSING_SHARES. This is the exact gap b3 blocks on.
GAP_SQL = """
WITH per AS (
  SELECT ts_code, min(effective_date) AS first_eff
  FROM {schema}.stock_share_capital
  WHERE total_shares IS NOT NULL AND total_shares > 0
  GROUP BY ts_code)
SELECT m.ts_code, m.list_date, p.first_eff
FROM {schema}.stock_meta m JOIN per p USING (ts_code)
WHERE (m.ts_code LIKE '%%.SH' OR m.ts_code LIKE '%%.SZ')
  AND (m.list_date IS NULL OR m.list_date <= %(win_end)s)
  AND (m.delist_date IS NULL OR m.delist_date >= %(win_start)s)
  AND p.first_eff > GREATEST(COALESCE(m.list_date, %(win_start)s::date), %(win_start)s::date)
ORDER BY m.ts_code
"""

# All tickers with >=1 valued node (for the no-regression diff).
VALUED_SQL = """
SELECT DISTINCT ts_code
FROM {schema}.stock_share_capital
WHERE total_shares IS NOT NULL AND total_shares > 0
ORDER BY ts_code
"""

# Enrich the residual gap: implied par = latest CSMAR A003101000 / earliest 2025
# indicator-implied shares (the same-period anchor). Lets the user eyeball why a
# tail ticker did not snap (non-standard par, share event, data quirk).
TAIL_SQL = """
WITH per AS (
  SELECT ts_code, min(effective_date) AS first_eff
  FROM {schema}.stock_share_capital
  WHERE total_shares IS NOT NULL AND total_shares > 0
  GROUP BY ts_code),
gap AS (
  SELECT m.ts_code, m.list_date
  FROM {schema}.stock_meta m JOIN per p USING (ts_code)
  WHERE (m.ts_code LIKE '%%.SH' OR m.ts_code LIKE '%%.SZ')
    AND (m.list_date IS NULL OR m.list_date <= %(win_end)s)
    AND (m.delist_date IS NULL OR m.delist_date >= %(win_start)s)
    AND p.first_eff > GREATEST(COALESCE(m.list_date, %(win_start)s::date), %(win_start)s::date)),
csmar AS (
  SELECT DISTINCT ON (f.ts_code) f.ts_code, (f.data->>'A003101000')::numeric AS a003
  FROM {schema}.stock_financial f JOIN gap g USING (ts_code)
  WHERE f.data_source='csmar' AND f.statement_type='balance'
    AND f.data ? 'A003101000' AND (f.data->>'A003101000')::numeric > 0
  ORDER BY f.ts_code, f.end_date DESC),
anchor AS (
  SELECT DISTINCT ON (s.ts_code) s.ts_code, s.total_shares AS anchor_shares
  FROM {schema}.stock_share_capital s JOIN gap g USING (ts_code)
  WHERE s.source='indicator_implied' AND s.total_shares > 0
  ORDER BY s.ts_code, s.effective_date)
SELECT g.ts_code, g.list_date, c.a003 AS csmar_latest_a003101000,
       a.anchor_shares AS anchor_2025_shares,
       round((c.a003 / NULLIF(a.anchor_shares, 0))::numeric, 4) AS implied_par
FROM gap g
LEFT JOIN csmar c USING (ts_code)
LEFT JOIN anchor a USING (ts_code)
ORDER BY implied_par NULLS LAST, g.ts_code
"""


def _rows(conn, sql, schema):
    with conn.cursor() as cur:
        cur.execute(
            sql.format(schema=schema),
            {"win_start": WINDOW_START, "win_end": WINDOW_END},
        )
        return cur.fetchall()


def _write_csv(path: Path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path.name}: {len(rows)} rows")


def phase_before(conn, schema):
    gap = _rows(conn, GAP_SQL, schema)
    valued = _rows(conn, VALUED_SQL, schema)
    _write_csv(HERE / "gap_before.csv", ["ts_code", "list_date", "first_eff"], gap)
    _write_csv(HERE / "valued_tickers_before.csv", ["ts_code"], valued)
    print(f"BEFORE: gap={len(gap)} tickers | valued={len(valued)} tickers")
    print("  expected pre-fix gap ~= 696 (probed 2026-07-25).")


def phase_after(conn, schema):
    gap = _rows(conn, GAP_SQL, schema)
    valued = _rows(conn, VALUED_SQL, schema)
    _write_csv(HERE / "gap_after.csv", ["ts_code", "list_date", "first_eff"], gap)

    tail = _rows(conn, TAIL_SQL, schema)
    _write_csv(
        HERE / "tail.csv",
        ["ts_code", "list_date", "csmar_latest_a003101000",
         "anchor_2025_shares", "implied_par", "note"],
        [list(r) + [""] for r in tail],
    )

    # Recovery + regression, computed against the BEFORE snapshots on disk.
    gap_before = _load_set(HERE / "gap_before.csv")
    valued_before = _load_set(HERE / "valued_tickers_before.csv")
    gap_after = {r[0] for r in gap}
    valued_after = {r[0] for r in valued}

    recovered = gap_before - gap_after
    regressed = valued_before - valued_after  # MUST be empty
    new_historical_gaps = gap_after - gap_before

    print(f"AFTER: gap={len(gap_after)} tickers (was {len(gap_before)})")
    print(f"  recovered (dropped out of gap): {len(recovered)}  (expected ~639)")
    print(f"  residual tail still gapped:     {len(gap_after)}  (expected ~57)")
    print(f"  REGRESSION (valued->unvalued):  {len(regressed)}  (MUST be 0)")
    print(
        "  NEW HISTORICAL GAP REGRESSION: "
        f"{len(new_historical_gaps)}  (MUST be 0)"
    )
    if regressed:
        print("  !! regressed tickers:", sorted(regressed)[:20], file=sys.stderr)
        print("  !! forward-window skew (reviewer M1) may have flipped these; "
              "consider a centered window before accepting the rerun.",
              file=sys.stderr)
    if new_historical_gaps:
        print(
            "  !! new historical gap tickers:",
            sorted(new_historical_gaps)[:20],
            file=sys.stderr,
        )
    return 1 if regressed or new_historical_gaps else 0


def _load_set(path: Path) -> set[str]:
    if not path.exists():
        print(f"  missing {path.name} — run --phase before first", file=sys.stderr)
        sys.exit(2)
    with open(path) as fh:
        r = csv.reader(fh)
        next(r, None)  # header
        return {row[0] for row in r if row}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=["before", "after"], required=True)
    ap.add_argument("--use-test", action="store_true",
                    help="query stock_selector_test schema (sandbox)")
    args = ap.parse_args(argv)
    schema = "stock_selector_test" if args.use_test else "stock_selector"

    conn = connect(schema)
    try:
        if args.phase == "before":
            phase_before(conn, schema)
            return 0
        return phase_after(conn, schema)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
