"""Read-only money-flow field audit. No returns, candidate selection or DB writes.

python -m backtest.flow_field_readiness --run-id UNIQUE_ID
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs/plans/2026-09-16-equal-weight-independent-legs.md"
FIELDS = (
    "main_inflow_count", "main_outflow_count", "main_in_money", "main_out_money",
    "main_inflow_money", "open_main_inflow_money", "end_main_inflow_money",
    "extra_bill_inflow_money", "large_bill_inflow_money",
    "middle_bill_inflow_money", "small_bill_inflow_money",
)


def capture(run):
    connection = _connect(load_db_config())
    try:
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with connection.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute("SELECT transaction_timestamp()::text")
            stamp = q.fetchone()[0]
            q.execute("SELECT index_code,trade_date,wind_sector," + ",".join(FIELDS)
                      + ",src,fetched_at,updated_at FROM stock_selector.index_money_flow"
                      " ORDER BY index_code,trade_date")
            frame = pd.DataFrame(q.fetchall(), columns=[x[0] for x in q.description])
            q.execute("SELECT trade_date FROM stock_selector.index_daily"
                      " WHERE index_code='000300.SH' ORDER BY trade_date")
            calendar = pd.DataFrame(q.fetchall(), columns=["trade_date"])
            q.execute("SELECT a.attname,col_description(a.attrelid,a.attnum)"
                      " FROM pg_attribute a"
                      " WHERE a.attrelid='stock_selector.index_money_flow'::regclass"
                      " AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum")
            comments = dict(q.fetchall())
        connection.rollback()
    finally:
        connection.close()
    frame.to_csv(run / "inputs/money_flow.csv", index=False)
    calendar.to_csv(run / "inputs/calendar.csv", index=False)
    (run / "inputs/metadata.json").write_text(json.dumps(
        {"observed_at": stamp, "column_comments": comments}, ensure_ascii=False, indent=2))


def audit(frame, calendar):
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    calendar = pd.DatetimeIndex(pd.to_datetime(calendar.trade_date))
    for field in FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    checks = {"nonempty": len(frame) > 0,
              "unique_keys": not frame.duplicated(["index_code", "trade_date"]).any(),
              "finite_fields": bool(np.isfinite(frame[list(FIELDS)]).all().all()),
              "valid_dates": bool(frame.trade_date.notna().all()),
              "unique_calendar": not calendar.has_duplicates}
    counts = frame[["main_inflow_count", "main_outflow_count"]]
    checks["nonnegative_integer_counts"] = bool(((counts >= 0) & (counts == np.floor(counts))).all().all())
    checks["positive_count_sum"] = bool(counts.sum(axis=1).gt(0).all())
    checks["csi300_count_cap"] = bool(counts.sum(axis=1)[frame.index_code.eq("000300.SH")].le(300).all())
    checks["nonnegative_gross_money"] = bool(frame[["main_in_money", "main_out_money"]].ge(0).all().all())
    checks["positive_money_denominator"] = bool((frame.main_in_money + frame.main_out_money).gt(0).all())
    net = frame.main_in_money - frame.main_out_money
    errors = {
        "net_equals_extra_plus_large": net - frame.extra_bill_inflow_money - frame.large_bill_inflow_money,
        "vendor_main_equals_net": frame.main_inflow_money - net,
        "four_buckets_zero_sum": frame[["extra_bill_inflow_money", "large_bill_inflow_money",
                                        "middle_bill_inflow_money", "small_bill_inflow_money"]].sum(axis=1),
    }
    identities = {name: {"violations_gt_0_001": int(values.abs().gt(.001).sum()),
                         "max_abs_error": float(values.abs().max())}
                  for name, values in errors.items()}
    checks["net_equals_extra_plus_large"] = identities["net_equals_extra_plus_large"]["violations_gt_0_001"] == 0
    coverage, field_stats, yearly = [], [], []
    for code, group in frame.groupby("index_code"):
        dates = pd.DatetimeIndex(group.trade_date)
        expected = calendar[(calendar >= dates.min()) & (calendar <= dates.max())]
        missing, extra = expected.difference(dates), dates.difference(calendar)
        coverage.append({"index_code": code, "rows": len(group), "first": str(dates.min().date()),
                         "last": str(dates.max().date()), "missing_dates": missing.strftime("%Y-%m-%d").tolist(),
                         "off_calendar_dates": extra.strftime("%Y-%m-%d").tolist(),
                         "calendar_days_after_last": int((calendar > dates.max()).sum()),
                         "first_fetch": str(group.fetched_at.min()), "last_fetch": str(group.fetched_at.max()),
                         "last_update": str(group.updated_at.max())})
        for field in FIELDS:
            x = group[field]
            nonzero = group.loc[x.notna() & x.ne(0), "trade_date"]
            field_stats.append({"index_code": code, "field": field, "rows": len(x),
                                "missing": int(x.isna().sum()), "zero": int(x.eq(0).sum()),
                                "negative": int(x.lt(0).sum()), "min": x.min(), "max": x.max(),
                                "first_nonzero": str(nonzero.min().date()) if len(nonzero) else None})
        for year, segment in group.groupby(group.trade_date.dt.year):
            for field in FIELDS:
                x = segment[field]
                yearly.append({"index_code": code, "year": int(year), "field": field,
                               "rows": len(x), "zero": int(x.eq(0).sum()), "missing": int(x.isna().sum())})
    checks["calendar_coverage"] = all(not row["missing_dates"] and not row["off_calendar_dates"] for row in coverage)
    summary = {"rows": len(frame), "checks": {k: bool(v) for k, v in checks.items()},
               "numerical_checks_pass": all(checks.values()), "coverage": coverage, "identities": identities,
               "definition_status": "definition_pending", "historical_availability": "pit_unverified",
               "returns_examined": False,
               "limits": "Numeric checks do not prove vendor definitions, publication timing, membership PIT or revision policy."}
    return summary, pd.DataFrame(field_stats), pd.DataFrame(yearly)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run = create_run_dir(ROOT / "backtest/output/runs", args.run_id)
    write_manifest(run, {"status": "running", "created_utc": datetime.now(timezone.utc).isoformat()})
    try:
        shutil.copyfile(PLAN, run / "inputs/scope.md")
        for path in (Path(__file__), ROOT / "backtest/run_manifest.py", ROOT / "backtest/data.py",
                     ROOT / "signals/common/config.py"):
            shutil.copyfile(path, run / "inputs" / ("code__" + path.relative_to(ROOT).as_posix().replace("/", "__")))
        capture(run)
        # Compute from saved inputs, so the outputs can be reproduced offline.
        frame = pd.read_csv(run / "inputs/money_flow.csv")
        summary, fields, yearly = audit(frame, pd.read_csv(run / "inputs/calendar.csv"))
        raw = ROOT / "data_fixes/2026-09-03-index-money-flow/raw"
        raw_records = [artifact_record(p, ROOT) for p in sorted(raw.glob("*.csv"))]
        (run / "inputs/raw_source_hashes.json").write_text(json.dumps(raw_records, indent=2))
        (run / "outputs/summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
        fields.to_csv(run / "outputs/field_statistics.csv", index=False)
        yearly.to_csv(run / "outputs/yearly_field_coverage.csv", index=False)
        # Caller writes the human report and final verification before sealing the run.
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as exc:
        write_manifest(run, {"status": "failed", "error_type": type(exc).__name__})
        # Connection exceptions can contain service details. Never print their text.
        raise SystemExit("Field audit failed: " + type(exc).__name__) from None


if __name__ == "__main__":
    main()
