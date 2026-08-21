"""Fail-closed evidence revalidation orchestrator for the P0 experiments."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from backtest.run_manifest import (
    artifact_record,
    create_run_dir,
    git_state,
    query_table_cutoffs,
    write_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "p0-revalidation"
SEED = 0
CUTOFF_CONTRACT = {
    "index_daily": "trade_date",
    "stock_daily_price": "trade_date",
    "stock_indicator": "trade_date",
    "stock_financial": "end_date",
    "index_constituent": "effective_date",
}
LEGACY_FILES = {
    "gate0r": "gate0r_result.json",
    "fifth": "fifth_bucket_verdict.json",
    "geo": "geo5_verdict.json",
}
OUTPUT_FILES = LEGACY_FILES.copy()


def build_commands(run_dir: Path, python: str = sys.executable) -> list[tuple[str, list[str]]]:
    """Return the frozen five-command P0 execution plan."""
    outputs = Path(run_dir) / "outputs"
    return [
        ("gate0r", [python, "-m", "backtest.gate0_runner", "0r",
                    "--output-dir", str(outputs)]),
        ("tail", [python, "-m", "backtest.tail_pair_runner",
                  "--output-dir", str(outputs)]),
        ("fifth", [python, "-m", "backtest.fifth_bucket_formal",
                   "--tail-csv", str(outputs / "tail_pair_daily.csv"),
                   "--output-dir", str(outputs), "--start", "2022-12-12",
                   "--n-perm", "1000", "--seed", "0"]),
        ("geo_pairs", [python, "-m", "backtest.geometric_pairs_runner",
                       "--output-dir", str(outputs)]),
        ("geo_formal", [python, "-m", "backtest.geometric_5b_formal",
                        "--geo-csv", str(outputs / "geo5_pairs_daily.csv"),
                        "--output-dir", str(outputs), "--n-perm", "1000",
                        "--seed", "0"]),
    ]


def subprocess_runner(command: list[str], log_path: Path) -> int:
    """Run one command with its combined output captured in a binary log."""
    with Path(log_path).open("wb") as handle:
        completed = subprocess.run(
            command, stdout=handle, stderr=subprocess.STDOUT, check=False
        )
    return completed.returncode


def execute_steps(run_dir: Path, commands: list[tuple[str, list[str]]],
                  runner=subprocess_runner) -> None:
    logs = Path(run_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for number, (step, command) in enumerate(commands, start=1):
        log_path = logs / f"step-{number}-{step}.log"
        code = runner(command, log_path)
        if code != 0:
            raise RuntimeError(f"step {step} failed with exit code {code}")


def _reject_nonfinite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON value")
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)


def _reject_constant(_value):
    raise ValueError("non-finite JSON value")


def load_json(path: Path) -> dict:
    """Read UTF-8 JSON and reject non-finite values at every nesting level."""
    payload = json.loads(
        Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant
    )
    _reject_nonfinite(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def validate_gate0(payload: dict) -> dict:
    if payload.get("pass") is not True:
        raise ValueError("gate0 pass must be True")
    return payload


def validate_verdict(payload: dict) -> dict:
    if payload.get("OVERALL") not in {"STOP", "GO"}:
        raise ValueError("OVERALL must be STOP or GO")
    for field in (
        "sharpe_diff",
        "p_selected",
        "sharpe_incumbent_full",
        "sharpe_candidate_full",
        "position_diff_ratio",
    ):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a finite number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{field} must be a finite number")
    return payload


def _passed(payload: dict) -> bool:
    if "OVERALL" in payload:
        return payload["OVERALL"] == "GO"
    return payload.get("pass") is True


def compare_verdicts(old: dict[str, dict], new: dict[str, dict]) -> dict[str, dict]:
    """Compare every logical verdict, preserving the original payloads."""
    if set(old) != set(new):
        raise ValueError("old and new verdict keys must match")
    comparison = {}
    for logical in old:
        old_passed, new_passed = _passed(old[logical]), _passed(new[logical])
        comparison[logical] = {
            "old": old[logical],
            "new": new[logical],
            "maintained": old_passed == new_passed,
            "flipped": old_passed != new_passed,
        }
    return comparison


def database_cutoffs() -> dict[str, str]:
    """Query the frozen source-table cutoff contract without import-time I/O."""
    from backtest.pure_style_builder import _conn

    connection, _schema = _conn()
    try:
        return query_table_cutoffs(connection, "stock_selector", CUTOFF_CONTRACT)
    finally:
        connection.close()


def _artifact_records(run_dir: Path) -> list[dict]:
    records = []
    for path in sorted(Path(run_dir).rglob("*")):
        if (
            path.is_file()
            and path.name != "manifest.json"
            and ".tmp" not in path.name
        ):
            records.append(artifact_record(path, run_dir))
    return records


def _write_comparison(path: Path, payload: dict) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_revalidation(run_root: Path, run_id: str, *, root: Path = ROOT,
                     metadata: dict | None = None, python: str = sys.executable,
                     runner=subprocess_runner) -> Path:
    """Create a sealed evidence run, re-execute P0, and preserve failure state."""
    run_dir = create_run_dir(Path(run_root), run_id)
    commands = build_commands(run_dir, python)
    manifest = {
        "metadata": {} if metadata is None else metadata,
        "experiment": EXPERIMENT,
        "seed": SEED,
        "status": "running",
        "commands": [{"step": step, "command": command} for step, command in commands],
        "inputs": {},
        "artifacts": [],
    }
    write_manifest(run_dir, manifest)
    print(f"RUN_DIR {run_dir}", flush=True)

    try:
        legacy_dir = Path(root) / "backtest" / "output"
        old = {}
        for logical, filename in LEGACY_FILES.items():
            source = legacy_dir / filename
            target = run_dir / "inputs" / filename
            shutil.copy2(source, target)
            payload = load_json(target)
            old[logical] = (
                validate_gate0(payload) if logical == "gate0r" else validate_verdict(payload)
            )
            manifest["inputs"][logical] = artifact_record(target, run_dir)

        execute_steps(run_dir, commands, runner=runner)

        new = {}
        for logical, filename in OUTPUT_FILES.items():
            payload = load_json(run_dir / "outputs" / filename)
            new[logical] = (
                validate_gate0(payload) if logical == "gate0r" else validate_verdict(payload)
            )
        _write_comparison(run_dir / "comparison.json", compare_verdicts(old, new))

        manifest["status"] = "complete"
        manifest["artifacts"] = _artifact_records(run_dir)
        write_manifest(run_dir, manifest)
        return run_dir
    except BaseException as exc:
        manifest["status"] = (
            "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        )
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
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
    parser = argparse.ArgumentParser(description="Run sealed P0 evidence revalidation")
    parser.add_argument("--run-root", type=Path, default=ROOT / "backtest" / "output" / "runs")
    args = parser.parse_args(argv)

    git = git_state(ROOT)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S-p0-revalidation-") + git["commit"][:7]
    metadata = {"git": git, "database_cutoffs": database_cutoffs()}
    run_revalidation(args.run_root, run_id, metadata=metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
