"""新轮动轴入场券·immutable run 编排器（fail-closed，镜像 p0_revalidation 纪律）。

设计冻结：docs/plans/2026-08-24-new-rotation-axes-entry-ticket.md §3。
两步：① axis_rotation_builder 构建腿对 → ② axis_entry_ticket 判定。
manifest 只有 status=complete 才构成证据；interrupted/failed 状态原样保全。

CLI: python3 -m backtest.axis_ticket_runner [--n-perm 2000]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from backtest.p0_revalidation import (
    _now_iso,
    execute_steps,
    subprocess_runner,
)
from backtest.run_manifest import (
    artifact_record,
    capture_input_state,
    create_run_dir,
    git_state,
    input_drift_report,
    query_table_cutoffs,
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "axes-entry-ticket"
SPEC = "docs/plans/2026-08-24-new-rotation-axes-entry-ticket.md"
SEED = 0
AXIS_END = "2026-08-24"
CUTOFF_CONTRACT = {
    "index_daily": "trade_date",
    "stock_daily_price": "trade_date",
    "stock_indicator": "trade_date",
    "stock_financial": "end_date",
    "stock_share_capital": "effective_date",
}
REQUIRED_OUTPUTS = (
    "axis_legs_daily.csv",
    "axis_build.json",
    "axis_ticket_panel.csv",
    "axis_ticket_verdict.json",
)
STEP_NAMES = ("build", "ticket")


def database_input_state() -> dict:
    from backtest.pure_style_builder import _conn

    connection, schema = _conn()
    try:
        return capture_input_state(connection, schema, CUTOFF_CONTRACT)
    finally:
        connection.close()


def database_cutoffs() -> dict[str, str]:
    from backtest.pure_style_builder import _conn

    connection, schema = _conn()
    try:
        return query_table_cutoffs(connection, schema, CUTOFF_CONTRACT)
    finally:
        connection.close()


def database_input_drift(start: dict, terminal: str) -> dict:
    from backtest.pure_style_builder import _conn

    connection, schema = _conn()
    try:
        return input_drift_report(
            connection, schema, CUTOFF_CONTRACT, start["write_marks"],
            start["database_time"], terminal,
        )
    finally:
        connection.close()


def build_commands(run_dir: Path, n_perm: int, python: str = sys.executable,
                   axes: str | None = None, end: str = AXIS_END) -> list[tuple[str, list[str]]]:
    out = str(run_dir / "outputs")
    build_cmd = [python, "-m", "backtest.axis_rotation_builder", "--output-dir", out,
                 "--end", end]
    if axes:
        build_cmd += ["--axes", axes]
    return [
        ("build", build_cmd),
        ("ticket", [python, "-m", "backtest.axis_entry_ticket",
                    "--legs-csv", str(run_dir / "outputs" / "axis_legs_daily.csv"),
                    "--output-dir", out, "--n-perm", str(n_perm),
                    "--seed", str(SEED)]),
    ]


def _number(value):
    if value in (None, ""):
        return None
    return float(value)


def _axis_snapshot(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    outputs = run_dir / "outputs"
    verdict = json.loads(
        (outputs / "axis_ticket_verdict.json").read_text(encoding="utf-8")
    )
    halves = {}
    with (outputs / "axis_ticket_panel.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row.get("row") != "ref_half":
                continue
            halves.setdefault(row["axis"], {})[row["window"]] = {
                "partial_ic": _number(row.get("partial_ic")),
                "n_windows": int(float(row["n_windows"])),
            }
    axes = {}
    for axis, payload in verdict.get("axes", {}).items():
        axes[axis] = {
            "partial_ic": payload.get("partial_ic"),
            "permutation_p": payload.get("partial_ic_pvalue"),
            "pass": payload.get("pass"),
            "halves": halves.get(axis, {}),
        }
    return {
        "run": str(run_dir),
        "legs": artifact_record(outputs / "axis_legs_daily.csv", run_dir),
        "overall": verdict.get("OVERALL"),
        "anchors_ok": verdict.get("anchors_ok"),
        "axes": axes,
    }


def compare_axis_evidence(baseline_run: Path, new_run: Path) -> dict:
    old = _axis_snapshot(baseline_run)
    new = _axis_snapshot(new_run)
    all_axes = set(old["axes"]) | set(new["axes"])
    flipped = sorted(
        axis for axis in all_axes
        if old["axes"].get(axis, {}).get("pass")
        != new["axes"].get(axis, {}).get("pass")
    )
    return {
        "old": old,
        "new": new,
        "flipped_axes": flipped,
        "verdicts_maintained": not flipped and set(old["axes"]) == set(new["axes"]),
    }


def _write_comparison(path: Path, payload: dict) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate_complete_evidence(run_dir: Path) -> None:
    run_dir = Path(run_dir)
    for filename in REQUIRED_OUTPUTS:
        path = run_dir / "outputs" / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"required evidence missing: outputs/{filename}")
    verdict = json.loads((run_dir / "outputs" / "axis_ticket_verdict.json")
                         .read_text(encoding="utf-8"))
    if "OVERALL" not in verdict or "anchors_ok" not in verdict:
        raise RuntimeError("verdict json missing OVERALL/anchors_ok")
    for number, step in enumerate(STEP_NAMES, start=1):
        if not (run_dir / "logs" / f"step-{number}-{step}.log").is_file():
            raise RuntimeError(f"missing log for step {step}")


def _artifact_records(run_dir: Path) -> list[dict]:
    return [artifact_record(p, run_dir)
            for p in sorted(Path(run_dir).rglob("*"))
            if p.is_file() and p.name != "manifest.json" and ".tmp" not in p.name]


def run_ticket(run_root: Path, run_id: str, *, n_perm: int = 2000,
               python: str = sys.executable, runner=subprocess_runner,
               axes: str | None = None, spec: str = SPEC, end: str = AXIS_END,
               metadata: dict | None = None, drift_check=None,
               baseline_run: Path | None = None) -> Path:
    if metadata is None:
        git = git_state(ROOT)
        database_start = database_input_state()
        metadata = {
            "spec": spec, "git": git,
            "database_cutoffs": database_start["cutoffs"],
            "database_input_start": database_start,
        }
        if drift_check is None:
            drift_check = lambda: database_input_drift(database_start, end)
    else:
        metadata = dict(metadata)
        metadata.setdefault("spec", spec)
    if baseline_run is not None:
        metadata["baseline_run"] = str(Path(baseline_run))
    run_dir = create_run_dir(Path(run_root), run_id)
    commands = build_commands(run_dir, n_perm, python, axes, end)
    manifest = {
        "metadata": metadata,
        "experiment": EXPERIMENT,
        "seed": SEED,
        "status": "running",
        "commands": [{"step": step, "command": command} for step, command in commands],
        "started_at": _now_iso(),
        "inputs": {},
        "artifacts": [],
    }
    write_manifest(run_dir, manifest)
    print(f"RUN_DIR={run_dir}", flush=True)

    try:
        execute_steps(run_dir, commands, runner=runner)
        _validate_complete_evidence(run_dir)
        if baseline_run is not None:
            _write_comparison(
                run_dir / "comparison.json",
                compare_axis_evidence(Path(baseline_run), run_dir),
            )
        if drift_check is not None:
            drift = drift_check()
            manifest["metadata"]["input_drift"] = drift
            if drift.get("inputs_moved_in_window"):
                raise RuntimeError("input drift touched the axis analysis window")
        manifest["status"] = "complete"
        manifest["artifacts"] = _artifact_records(run_dir)
        manifest["finished_at"] = _now_iso()
        write_manifest(run_dir, manifest)
        return run_dir
    except BaseException as exc:
        manifest["status"] = (
            "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "failed"
        )
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        manifest["finished_at"] = _now_iso()
        try:
            manifest["artifacts"] = _artifact_records(run_dir)
        except BaseException:
            manifest["artifacts"] = []
        try:
            write_manifest(run_dir, manifest)
        except BaseException:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="新轮动轴入场券 immutable run")
    ap.add_argument("--n-perm", type=int, default=2000)
    ap.add_argument("--axes", default=None, help="逗号分隔轴集（默认批次一四轴）")
    ap.add_argument("--spec", default=SPEC, help="本批冻结设计稿路径")
    ap.add_argument("--end", default=AXIS_END)
    ap.add_argument("--baseline-run", type=Path, required=True)
    ap.add_argument("--run-root", type=Path,
                    default=ROOT / "backtest" / "output" / "runs")
    args = ap.parse_args(argv)

    git = git_state(ROOT)
    if git.get("tracked_dirty", git["dirty"]):
        raise RuntimeError("formal run refused: tracked changes are present")
    database_start = database_input_state()
    metadata = {
        "spec": args.spec, "git": git,
        "database_cutoffs": database_start["cutoffs"],
        "database_input_start": database_start,
    }
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S-axes-ticket-") + git["commit"][:7]
    run_dir = run_ticket(
        args.run_root, run_id, n_perm=args.n_perm, axes=args.axes,
        spec=args.spec, end=args.end, metadata=metadata,
        baseline_run=args.baseline_run,
        drift_check=lambda: database_input_drift(database_start, args.end),
    )
    print(f"COMPLETE {run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
