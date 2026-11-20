"""Offline receipt audit: verify handed-over evidence and probe return boundaries.

Extract only the two return functions by AST. Their SQL runs against an in-memory
SQLite fixture; no project import, configuration, network or shared DB connection.
The corrected interval is a diagnostic only, not an official-weight reconstruction.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class FixtureConnection:
    """Translate PostgreSQL syntax only; preserve WHERE interval and row filters."""

    def __init__(self, db):
        self.db = db
        self.cursor_ = None
        self.queries = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor_.close()

    def execute(self, sql, params):
        self.queries.append(sql)
        codes = params[0]
        sql = sql.replace("::float8", "")
        sql = re.sub(r"\bDATE\s+'([^']+)'", r"'\1'", sql)
        sql = sql.replace("ts_code=ANY(%s)", "ts_code IN (" + ",".join("?" for _ in codes) + ")")
        self.cursor_ = self.db.execute(sql, codes)

    def fetchall(self):
        return self.cursor_.fetchall()

    def close(self):
        # The fixture owner closes the database after all extracted calls finish.
        pass


def return_probe(source: Path, out: Path) -> dict:
    tree = ast.parse(source.read_text())
    names = {"_daily_returns", "_leg_returns"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    db = sqlite3.connect(":memory:")
    conn = FixtureConnection(db)
    namespace = {"pd": pd, "np": np, "_conn": lambda: (conn, "main")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    rows = []
    days = ["2025-06-16", "2025-06-17", "2025-12-12", "2025-12-15", "2025-12-16"]
    for code, returns in (("OLD", [.01, .02, .03, -.04, -.05]),
                          ("NEW", [.10, .20, .30, .40, .50])):
        rows.extend((code, day, 100 * (1 + ret), 100., 1.) for day, ret in zip(days, returns))
    # HALT has a zero-volume row, MISSING has no row: current query/fill conflates them.
    rows.append(("HALT", "2025-12-16", 100., 100., 0.))
    db.execute("CREATE TABLE stock_daily_price (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL, volume REAL)")
    db.executemany("INSERT INTO stock_daily_price VALUES (?, ?, ?, ?, ?)", rows)
    pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "pre_close", "volume"]).to_csv(
        out / "synthetic_prices.csv", index=False)
    try:
        leg = namespace["_leg_returns"]
        first, switch, terminal = map(pd.Timestamp, (days[0], days[3], days[4]))
        old, new = pd.Series({"OLD": 1.}), pd.Series({"NEW": 1.})
        current = pd.concat([leg(old, first, switch), leg(new, switch, terminal)])
        one_day = pd.Timedelta(days=1)
        # Adapt the existing (start,end] reader: [effective,next_effective),
        # with the final observation endpoint still inclusive. Calendar-day offsets
        # include holidays correctly; there is no assumed weekday trading calendar.
        boundary_only = pd.concat([leg(old, first - one_day, switch - one_day),
                                   leg(new, switch - one_day, terminal)])
        expected_current = pd.Series([.02, .03, -.04, .50], index=pd.to_datetime(days[1:]))
        expected_boundary = pd.Series([.01, .02, .03, .40, .50], index=pd.to_datetime(days))
        pd.testing.assert_series_equal(current, expected_current, check_exact=False, atol=1e-12, rtol=0.)
        pd.testing.assert_series_equal(boundary_only, expected_boundary, check_exact=False, atol=1e-12, rtol=0.)
        assert current.index.is_unique and boundary_only.index.is_unique
        pd.concat([current.rename("current"), boundary_only.rename("boundary_only")], axis=1).to_csv(
            out / "synthetic_timing.csv", index_label="date")

        halt = leg(pd.Series({"NEW": .5, "HALT": .5}), switch, terminal)
        missing = leg(pd.Series({"NEW": .5, "MISSING": .5}), switch, terminal)
        pd.testing.assert_series_equal(halt, missing)
        assert np.isclose(halt.loc[terminal], .25)
        absent = leg(pd.Series({"MISSING": 1.}), switch, terminal)
        assert absent.empty
        # Preserve price drift under the existing weighting convention.
        drift = leg(pd.Series({"OLD": .5, "NEW": .5}), switch - one_day, terminal)
        np.testing.assert_allclose(drift.to_numpy(), [.18, (.48 * -.05 + .70 * .50) / 1.18])
        (out / "fixture_queries.json").write_text(json.dumps(conn.queries, indent=2) + "\n")
        return {
            "evidence_kind": "synthetic_mechanism_only",
            "effective_day": str(switch.date()),
            "effective_day_current": float(current.loc[switch]),
            "effective_day_boundary_only": float(boundary_only.loc[switch]),
            "first_effective_day_missing_current": first not in current.index,
            "terminal_preserved": terminal in boundary_only.index,
            "boundary_dates_unique": boundary_only.index.is_unique,
            "halt_and_missing_same_result": True,
            "all_rows_missing_returns_empty": absent.empty,
            "price_drift_checked": True,
            "limits": ["Artificial prices and one-stock period weights; no market attribution.",
                       "Boundary-only example holds input weights fixed, without verifying their official reference date.",
                       "No actual snapshot-age, suspension or corporate-action classification."],
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if not out.is_relative_to(ROOT / "backtest/output/runs") or out.exists():
        parser.error("Use a new directory inside backtest/output/runs/.")
    original = args.handoff_manifest.resolve()
    handed = json.loads(original.read_text())
    # Reuse verified evidence instead of silently presenting it as a new DB run.
    inputs = {**handed["input_sha256"], str(original): sha256(original),
              str(Path(__file__).resolve()): sha256(Path(__file__))}
    for name, digest in handed["outputs_sha256"].items():
        inputs[str(original.parent / name)] = digest
    for path, digest in inputs.items():
        assert sha256(Path(path)) == digest, f"Handoff input/output changed: {path}"
    source = ROOT / "backtest/pure_style_builder.py"
    assert str(source) in inputs, "Handoff must cover the current builder."
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "manifest.json", {"status": "running", "scope": "offline_handoff_receipt"})
    shutil.copyfile(source, out / "pure_style_builder_source.py")
    shutil.copyfile(__file__, out / "audit_script.py")
    shutil.copyfile(original, out / "handoff_manifest.json")
    for name in ("metrics.json", "synthetic_probe.json"):
        shutil.copyfile(original.parent / name, out / ("handoff_" + name))
    probe = return_probe(source, out)
    write_json(out / "return_probe.json", probe)
    assert all(sha256(Path(p)) == digest for p, digest in inputs.items()), "Inputs changed during audit."
    write_json(out / "manifest.json", {
        "status": "complete", "scope": "offline_handoff_receipt_only",
        "overall_replication_status": "pending_official_data_and_validation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__,
        "argv": sys.argv, "input_sha256": inputs, "inputs_unchanged": True,
        "reused_evidence": ["handoff_metrics.json", "handoff_synthetic_probe.json"],
        "new_evidence": ["return_probe.json", "synthetic_prices.csv", "synthetic_timing.csv", "fixture_queries.json"],
        "outputs_sha256": {p.name: sha256(p) for p in sorted(out.iterdir()) if p.name != "manifest.json"},
        "shared_database_accessed": False, "production_changed": False,
    })
    print(json.dumps({"output_dir": str(out), **probe}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
