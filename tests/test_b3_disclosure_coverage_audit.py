import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.b3_eval import DataBlocked, compute_true_disclosure_coverage
from signals.style_basket.b3_config import CONFIG_PATH
from tools.audit_b3_disclosure_coverage import audit_frame, main


POLICIES = ["legal_deadline", "legal_deadline_plus_one_month_end"]


def exposure_grid(verified=True):
    rows = []
    for policy in POLICIES:
        for period in pd.period_range("2014-01", "2023-12", freq="M"):
            rows.append({
                "universe_role": "model",
                "pit_policy": policy,
                "formation_date": period.end_time.normalize(),
                "ticker": "000001.SZ",
                "true_first_disclosure_verified": verified,
            })
    return pd.DataFrame(rows)


def test_audit_total_is_the_existing_coverage_contract():
    frame = exposure_grid()
    frame = pd.concat([frame, pd.DataFrame([{
        "universe_role": "size_only",
        "pit_policy": POLICIES[0],
        "formation_date": "2014-01-31",
        "ticker": "000002.SZ",
        "true_first_disclosure_verified": False,
    }, {
        "universe_role": "model",
        "pit_policy": POLICIES[0],
        "formation_date": "2024-01-31",
        "ticker": "000003.SZ",
        "true_first_disclosure_verified": False,
    }])], ignore_index=True)
    summary, missing = audit_frame(frame, POLICIES)
    assert summary["coverage"] == compute_true_disclosure_coverage(frame, POLICIES)
    assert summary["coverage_ready"] is True
    assert summary["coverage"]["required_denominator"] == 240
    assert len(summary["by_policy"]) == 2
    assert len(summary["by_formation_month"]) == 120
    assert len(summary["by_policy_formation_month"]) == 240
    assert missing.empty


def test_partial_coverage_reports_exact_model_key():
    frame = exposure_grid()
    frame.loc[0, "true_first_disclosure_verified"] = False
    summary, missing = audit_frame(frame, POLICIES)
    assert summary["coverage_ready"] is False
    assert summary["coverage"]["verified_numerator"] == 239
    assert missing[["pit_policy", "formation_date", "ticker"]].to_dict("records") == [{
        "pit_policy": POLICIES[0],
        "formation_date": "2014-01-31",
        "ticker": "000001.SZ",
    }]
    assert summary["by_formation_month"][0]["formation_month"] == "2014-01"
    assert summary["by_policy_formation_month"][0]["formation_month"] == "2014-01"


@pytest.mark.parametrize("mutation", ["empty-model", "duplicate", "missing-column", "integer-bool"])
def test_audit_rejects_invalid_contract(mutation):
    frame = exposure_grid()
    if mutation == "empty-model":
        frame["universe_role"] = "size_only"
    elif mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif mutation == "missing-column":
        frame = frame.drop(columns="ticker")
    else:
        frame["true_first_disclosure_verified"] = 1
    with pytest.raises(DataBlocked):
        audit_frame(frame, POLICIES)


def test_cli_writes_ready_and_partial_audits_with_fixed_artifacts(tmp_path):
    ready_input = tmp_path / "ready.csv.gz"
    exposure_grid().to_csv(ready_input, index=False)
    ready_dir = tmp_path / "ready-audit"
    assert main([
        "--input", str(ready_input),
        "--config", str(CONFIG_PATH),
        "--output-dir", str(ready_dir),
    ]) == 0
    ready = json.loads((ready_dir / "coverage_audit.json").read_text(encoding="utf-8"))
    assert ready["coverage_ready"] is True
    assert ready["provenance"]["input_artifact"]["sha256"]
    assert ready["provenance"]["config_sha256"]
    assert ready["provenance"]["git"]["commit"]
    assert ready["artifacts"]["uncovered_model_rows"]["path"] == "uncovered_model_rows.csv"
    assert (ready_dir / "uncovered_model_rows.csv").read_text(encoding="utf-8") == (
        "pit_policy,formation_date,ticker\n"
    )

    partial = exposure_grid()
    partial.loc[0, "true_first_disclosure_verified"] = False
    partial_input = tmp_path / "partial.csv"
    partial.to_csv(partial_input, index=False)
    partial_dir = tmp_path / "partial-audit"
    assert main([
        "--input", str(partial_input),
        "--config", str(CONFIG_PATH),
        "--output-dir", str(partial_dir),
    ]) == 1
    payload = json.loads((partial_dir / "coverage_audit.json").read_text(encoding="utf-8"))
    assert payload["coverage_ready"] is False
    assert len(pd.read_csv(partial_dir / "uncovered_model_rows.csv")) == 1


def test_cli_fails_closed_without_overwriting_or_leaving_success_artifacts(tmp_path):
    input_path = tmp_path / "invalid.csv"
    invalid = exposure_grid()
    invalid["true_first_disclosure_verified"] = 1
    invalid.to_csv(input_path, index=False)
    output_dir = tmp_path / "invalid-audit"
    assert main([
        "--input", str(input_path),
        "--config", str(CONFIG_PATH),
        "--output-dir", str(output_dir),
    ]) == 2
    assert not output_dir.exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    valid_path = tmp_path / "valid.csv"
    exposure_grid().to_csv(valid_path, index=False)
    assert main([
        "--input", str(valid_path),
        "--config", str(CONFIG_PATH),
        "--output-dir", str(existing),
    ]) == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in existing.iterdir()) == ["keep.txt"]


def test_cli_requires_explicit_output_directory():
    with pytest.raises(SystemExit) as excinfo:
        main(["--input", "monthly_exposures.csv"])
    assert excinfo.value.code == 2
