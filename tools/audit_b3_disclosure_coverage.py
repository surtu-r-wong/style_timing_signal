from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import yaml

from backtest.b3_eval import DataBlocked, compute_true_disclosure_coverage
from backtest.run_manifest import artifact_record, git_state
from signals.style_basket.b3_config import CONFIG_PATH, config_hash, load_b3_config


ROOT = Path(__file__).resolve().parents[1]
MISSING_COLUMNS = ["pit_policy", "formation_date", "ticker"]


def _group_stats(frame: pd.DataFrame, keys: list[str]) -> list[dict]:
    rows: list[dict] = []
    grouped = frame.groupby(keys, sort=True, dropna=False)["verified"]
    for group_key, values in grouped:
        group_values = group_key if isinstance(group_key, tuple) else (group_key,)
        numerator = int(values.sum())
        denominator = int(values.size)
        rows.append({
            **dict(zip(keys, group_values)),
            "numerator": numerator,
            "denominator": denominator,
            "ratio": float(numerator / denominator),
        })
    return rows


def _assert_breakdown(rows: list[dict], coverage: dict, label: str) -> None:
    denominator = sum(row["denominator"] for row in rows)
    numerator = sum(row["numerator"] for row in rows)
    if denominator != coverage["required_denominator"]:
        raise DataBlocked(f"{label} breakdown does not add to total coverage")
    if numerator != coverage["verified_numerator"]:
        raise DataBlocked(f"{label} breakdown does not add to verified coverage")


def audit_frame(
    frame: pd.DataFrame,
    policies: list[str],
) -> tuple[dict, pd.DataFrame]:
    coverage = compute_true_disclosure_coverage(frame, policies)
    model = frame.loc[frame["universe_role"].eq("model")].copy()
    dates = pd.to_datetime(model["formation_date"], errors="raise")
    periods = dates.dt.to_period("M")
    required_periods = pd.period_range("2014-01", "2023-12", freq="M")
    required = model.loc[periods.isin(required_periods)].copy()
    required["formation_date"] = dates.loc[required.index].dt.strftime("%Y-%m-%d")
    required["formation_month"] = periods.loc[required.index].astype(str)
    required["verified"] = [
        bool(value) for value in required["true_first_disclosure_verified"]
    ]

    by_policy = _group_stats(required, ["pit_policy"])
    by_month = _group_stats(required, ["formation_month"])
    by_policy_month = _group_stats(required, ["pit_policy", "formation_month"])
    _assert_breakdown(by_policy, coverage, "policy")
    _assert_breakdown(by_month, coverage, "formation month")
    _assert_breakdown(by_policy_month, coverage, "policy/formation month")

    missing = required.loc[~required["verified"], MISSING_COLUMNS]
    missing = missing.sort_values(MISSING_COLUMNS).reset_index(drop=True)
    summary = {
        "schema_version": 1,
        "coverage_ready": (
            coverage["required_denominator"] > 0
            and coverage["verified_numerator"] == coverage["required_denominator"]
        ),
        "coverage": coverage,
        "by_policy": by_policy,
        "by_formation_month": by_month,
        "by_policy_formation_month": by_policy_month,
    }
    return summary, missing


def _read_input(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"input does not exist: {path}")
    if not (path.name.endswith(".csv") or path.name.endswith(".csv.gz")):
        raise ValueError("input must be .csv or .csv.gz")
    return pd.read_csv(path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit B3 true first-disclosure coverage")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.output_dir.exists():
        print(f"output directory already exists: {args.output_dir}", file=sys.stderr)
        return 2
    try:
        cfg = load_b3_config(args.config)
        frame = _read_input(args.input)
        summary, missing = audit_frame(frame, cfg["pit"]["policies"])
        provenance = {
            "input_artifact": artifact_record(args.input, args.input.parent),
            "config_path": str(args.config),
            "config_sha256": config_hash(cfg),
            "git": git_state(ROOT),
        }
    except (
        DataBlocked,
        OSError,
        UnicodeError,
        ValueError,
        pd.errors.ParserError,
        subprocess.CalledProcessError,
        yaml.YAMLError,
    ) as exc:
        print(f"coverage audit failed: {exc}", file=sys.stderr)
        return 2

    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        missing_path = args.output_dir / "uncovered_model_rows.csv"
        missing.to_csv(missing_path, index=False)
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        summary["provenance"] = provenance
        summary["artifacts"] = {
            "uncovered_model_rows": artifact_record(missing_path, args.output_dir),
        }
        _write_json_atomic(args.output_dir / "coverage_audit.json", summary)
    except OSError as exc:
        print(f"coverage audit output failed: {exc}", file=sys.stderr)
        return 2
    return 0 if summary["coverage_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
