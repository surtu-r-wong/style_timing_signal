"""Task 7 tests: the downstream post-write verifier and the guarded B3 runner.

The verifier must fail closed on every kind of damaged evidence — malformed,
duplicate-keyed, partial, stale or hash-mismatched — and must copy B3's own
verdicts verbatim instead of preselecting them.  The runner must execute the
three frozen stages in order under the 8 GiB guard, stop on a failed
preflight/build, and keep an eval exit code of 2 as evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


_CAMPAIGN = (
    Path(__file__).resolve().parents[1]
    / "data_fixes"
    / "2026-08-01-b3-wind-share-capital"
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CAMPAIGN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_post_write = _load("verify_post_write")
run_guarded_b3 = _load("run_guarded_b3")

VerificationError = verify_post_write.VerificationError
POLICIES = verify_post_write.POLICIES


# ------------------------------------------------------------------ builders


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_GRID_LAST = (pd.Timestamp("2014-01-31") + pd.offsets.MonthEnd(119)).date()


def _coverage_frame(
    *,
    missing_shares: int = 0,
    missing_close: int = 0,
    formations: int = 120,
    blocked: bool = False,
) -> pd.DataFrame:
    rows: list[dict] = []
    for policy in POLICIES:
        for index in range(formations):
            formation_date = (
                pd.Timestamp("2014-01-31") + pd.offsets.MonthEnd(index)
            ).date()
            rows.append(
                {
                    "pit_policy": policy,
                    "formation_date": formation_date,
                    "required_formation": True,
                    "check": "size_exclusion",
                    "side": "ELIGIBLE",
                    "eligible_count": 1_000,
                    "status": "REPORT_ONLY",
                    "reason_code": "",
                }
            )
        if missing_shares:
            rows.append(
                {
                    "pit_policy": policy,
                    "formation_date": _GRID_LAST,
                    "required_formation": True,
                    "check": "size_exclusion",
                    "side": "DATA_MISSING_SHARES",
                    "eligible_count": missing_shares,
                    "status": "REPORT_ONLY",
                    "reason_code": "DATA_MISSING_SHARES",
                }
            )
        if missing_close:
            rows.append(
                {
                    "pit_policy": policy,
                    "formation_date": _GRID_LAST,
                    "required_formation": True,
                    "check": "size_exclusion",
                    "side": "DATA_MISSING_CLOSE",
                    "eligible_count": missing_close,
                    "status": "REPORT_ONLY",
                    "reason_code": "DATA_MISSING_CLOSE",
                }
            )
        if blocked:
            rows.append(
                {
                    "pit_policy": policy,
                    "formation_date": _GRID_LAST,
                    "required_formation": True,
                    "check": "monthly_exposure",
                    "side": "",
                    "eligible_count": None,
                    "status": "DATA_BLOCKED",
                    "reason_code": "DATA_CONTRACT",
                }
            )
    return pd.DataFrame(rows)


def _run_manifest(
    *,
    candidates: dict | None = None,
    family: object = "NOT_SIGNIFICANT",
    final: object = "DATA_BLOCKED",
    invalid: list | None = None,
) -> dict:
    return {
        "requested_data_end": "2023-12-31",
        "candidate_statistical_verdicts": (
            {"size_l20": "NOT_SIGNIFICANT"} if candidates is None else candidates
        ),
        "family_statistical_verdict": family,
        "final_verdict": final,
        "invalid_formation_months": [] if invalid is None else invalid,
    }


@pytest.fixture
def evidence(tmp_path):
    """A complete, internally consistent evidence tree that passes every gate."""

    research = tmp_path / "run" / "research"
    logs = tmp_path / "run" / "logs"
    research.mkdir(parents=True)
    logs.mkdir(parents=True)

    coverage_path = research / "coverage_audit.csv"
    _coverage_frame().to_csv(coverage_path, index=False)
    diagnostics_path = research / "exposure_diagnostics.csv"
    diagnostics_path.write_text("pit_policy,formation_date,scope\n", encoding="utf-8")

    preflight_path = _write_json(
        research / "manifests" / "preflight.json",
        {
            "stage": "preflight",
            "data_end": "2023-12-31",
            "status": "OK",
            "blockers": [],
            "outputs": {
                "coverage_audit.csv": _sha(coverage_path),
                "exposure_diagnostics.csv": _sha(diagnostics_path),
            },
        },
    )

    stages = []
    for name in ("preflight", "build", "eval"):
        files = {}
        for label in ("stdout", "stderr", "time"):
            path = logs / f"{name}.{label}.log"
            path.write_text(f"{name} {label}\n", encoding="utf-8")
            files[label] = {"path": str(path), "sha256": _sha(path)}
        stages.append(
            {
                "name": name,
                "command": ["systemd-run", "--user", "--scope"],
                "exit_code": 2 if name == "eval" else 0,
                "allowed_exit_codes": [0, 2] if name == "eval" else [0],
                "wall_seconds": 1.0,
                "peak_rss_kib": 3_600_000,
                "files": files,
            }
        )
    receipt_path = _write_json(
        tmp_path / "run" / "b3_execution_receipt.json",
        {
            "schema": "b3-wind-share-capital-execution",
            "version": 1,
            "data_end": "2023-12-31",
            "complete": True,
            "stopped_at": None,
            "stages": stages,
        },
    )

    run_manifest_path = _write_json(
        tmp_path / "run" / "backtest" / "run_manifest.json", _run_manifest()
    )

    proposal_path = _write_json(
        tmp_path / "proposal" / "proposal_manifest.json", {"schema": "proposal"}
    )
    proposal_hash = _sha(proposal_path)
    apply_path = _write_json(
        tmp_path / "proposal" / "apply_receipt.json",
        {"proposal_manifest_sha256": proposal_hash, "readback_mismatches": 0},
    )
    canonical_path = _write_json(
        tmp_path / "proposal" / "post_write_canonical_verification.json",
        {
            "proposal_manifest_sha256": proposal_hash,
            "mismatches": 0,
            "coordinates": 5_781,
            "wind_sourced_coordinates": 5_781,
        },
    )

    return {
        "root": tmp_path,
        "execution_receipt": receipt_path,
        "preflight_manifest": preflight_path,
        "coverage_audit": coverage_path,
        "run_manifest": run_manifest_path,
        "proposal_manifest": proposal_path,
        "apply_receipt": apply_path,
        "canonical_verification": canonical_path,
    }


def _summarize(evidence, **overrides):
    kwargs = {
        key: evidence[key]
        for key in (
            "execution_receipt",
            "preflight_manifest",
            "coverage_audit",
            "run_manifest",
            "proposal_manifest",
            "apply_receipt",
            "canonical_verification",
        )
    }
    kwargs.update(overrides)
    return verify_post_write.build_summary(**kwargs)


# ------------------------------------------------------------------ verifier


def test_verifier_accepts_a_complete_evidence_tree(evidence):
    summary = _summarize(evidence)

    assert summary["accepted"] is True
    assert summary["data_missing_shares"] == {"all": 0, "required": 0}
    assert summary["data_missing_close"] == {"all": 0, "required": 0}
    assert summary["required_formations_by_policy"] == {
        "legal_deadline": 120,
        "legal_deadline_plus_one_month_end": 120,
    }
    assert summary["candidate_statistical_verdicts"] == {
        "size_l20": "NOT_SIGNIFICANT"
    }
    assert summary["family_statistical_verdict"] == "NOT_SIGNIFICANT"
    assert summary["invalid_formation_months"] == []
    assert summary["proposal_chain"]["coordinates_verified"] == 5_781


def test_verifier_copies_b3_verdicts_verbatim_including_data_blocked(evidence):
    summary = _summarize(evidence)

    assert summary["final_verdict"] == "DATA_BLOCKED"

    _write_json(
        evidence["run_manifest"], _run_manifest(final="INSUFFICIENT_EVIDENCE")
    )
    assert _summarize(evidence)["final_verdict"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"missing_shares": 55}, "DATA_MISSING_SHARES"),
        ({"missing_close": 2}, "DATA_MISSING_CLOSE"),
        ({"formations": 119}, "required formations"),
        ({"blocked": True}, "still blocked"),
    ],
)
def test_verifier_fails_closed_on_coverage_regressions(evidence, kwargs, message):
    _coverage_frame(**kwargs).to_csv(evidence["coverage_audit"], index=False)
    # keep the preflight manifest honest about the rewritten file
    manifest = json.loads(evidence["preflight_manifest"].read_text())
    manifest["outputs"]["coverage_audit.csv"] = _sha(evidence["coverage_audit"])
    _write_json(evidence["preflight_manifest"], manifest)

    with pytest.raises(VerificationError, match=message):
        _summarize(evidence)


def test_verifier_rejects_a_non_ok_preflight(evidence):
    manifest = json.loads(evidence["preflight_manifest"].read_text())
    manifest["status"] = "DATA_BLOCKED"
    _write_json(evidence["preflight_manifest"], manifest)

    with pytest.raises(VerificationError, match="preflight status"):
        _summarize(evidence)


def test_verifier_rejects_a_preflight_output_hash_mismatch(evidence):
    evidence["coverage_audit"].write_text("pit_policy\n", encoding="utf-8")

    with pytest.raises(VerificationError, match="hash mismatch"):
        _summarize(evidence)


def test_verifier_rejects_stale_execution_evidence(evidence):
    receipt = json.loads(evidence["execution_receipt"].read_text())
    stdout_path = Path(receipt["stages"][0]["files"]["stdout"]["path"])
    stdout_path.write_text("rewritten after the run\n", encoding="utf-8")

    with pytest.raises(VerificationError, match="stale evidence"):
        _summarize(evidence)


@pytest.mark.parametrize(
    "manifest, message",
    [
        (_run_manifest(candidates={}), "candidate_statistical_verdicts"),
        (_run_manifest(family=None), "family_statistical_verdict"),
        (_run_manifest(family=""), "family_statistical_verdict"),
        (_run_manifest(invalid=["2023-12-29"]), "still invalid"),
    ],
)
def test_verifier_rejects_an_incomplete_evaluation(evidence, manifest, message):
    _write_json(evidence["run_manifest"], manifest)

    with pytest.raises(VerificationError, match=message):
        _summarize(evidence)


def test_verifier_requires_a_final_verdict_key(evidence):
    manifest = _run_manifest()
    manifest.pop("final_verdict")
    _write_json(evidence["run_manifest"], manifest)

    with pytest.raises(VerificationError, match="final_verdict"):
        _summarize(evidence)


def test_verifier_rejects_a_broken_proposal_chain(evidence):
    _write_json(
        evidence["apply_receipt"],
        {"proposal_manifest_sha256": "0" * 64, "readback_mismatches": 0},
    )

    with pytest.raises(VerificationError, match="not bound"):
        _summarize(evidence)


def test_verifier_rejects_reported_readback_mismatches(evidence):
    payload = json.loads(evidence["canonical_verification"].read_text())
    payload["mismatches"] = 3
    _write_json(evidence["canonical_verification"], payload)

    with pytest.raises(VerificationError, match="mismatches"):
        _summarize(evidence)


def test_verifier_rejects_duplicate_json_keys(evidence):
    evidence["run_manifest"].write_text(
        '{"requested_data_end": "2023-12-31", "requested_data_end": "2024-12-31"}',
        encoding="utf-8",
    )

    with pytest.raises(VerificationError, match="duplicate JSON key"):
        _summarize(evidence)


def test_verifier_rejects_malformed_json(evidence):
    evidence["run_manifest"].write_text("{not json", encoding="utf-8")

    with pytest.raises(VerificationError, match="malformed JSON"):
        _summarize(evidence)


def test_verifier_rejects_a_partial_evidence_tree(evidence):
    evidence["canonical_verification"].unlink()

    with pytest.raises(VerificationError, match="missing evidence file"):
        _summarize(evidence)


def test_verifier_main_writes_a_failure_document_and_exits_nonzero(
    evidence, tmp_path, capsys
):
    evidence["run_manifest"].write_text("{not json", encoding="utf-8")
    output = tmp_path / "final_verification.json"

    code = verify_post_write.main(
        [
            "--execution-receipt", str(evidence["execution_receipt"]),
            "--preflight-manifest", str(evidence["preflight_manifest"]),
            "--coverage-audit", str(evidence["coverage_audit"]),
            "--run-manifest", str(evidence["run_manifest"]),
            "--proposal-manifest", str(evidence["proposal_manifest"]),
            "--apply-receipt", str(evidence["apply_receipt"]),
            "--canonical-verification", str(evidence["canonical_verification"]),
            "--output", str(output),
        ]
    )

    assert code == 1
    assert json.loads(output.read_text())["accepted"] is False


def test_verifier_main_writes_the_summary_atomically(evidence, tmp_path):
    output = tmp_path / "nested" / "final_verification.json"

    code = verify_post_write.main(
        [
            "--execution-receipt", str(evidence["execution_receipt"]),
            "--preflight-manifest", str(evidence["preflight_manifest"]),
            "--coverage-audit", str(evidence["coverage_audit"]),
            "--run-manifest", str(evidence["run_manifest"]),
            "--proposal-manifest", str(evidence["proposal_manifest"]),
            "--apply-receipt", str(evidence["apply_receipt"]),
            "--canonical-verification", str(evidence["canonical_verification"]),
            "--output", str(output),
        ]
    )

    assert code == 0
    assert json.loads(output.read_text())["accepted"] is True
    assert not list(output.parent.glob("*.tmp"))


# -------------------------------------------------------------------- runner


class _FakeCompleted:
    def __init__(self, returncode):
        self.returncode = returncode


def _fake_runner(commands, exit_codes):
    def run(command, stdout=None, stderr=None, cwd=None, check=False):
        commands.append(list(command))
        return _FakeCompleted(exit_codes[len(commands) - 1])

    return run


def test_runner_executes_three_guarded_stages_in_order(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_guarded_b3.subprocess, "run", _fake_runner(commands, [0, 0, 0])
    )

    receipt = run_guarded_b3.run_stages(tmp_path / "run", python="/py")

    assert [stage["name"] for stage in receipt["stages"]] == [
        "preflight",
        "build",
        "eval",
    ]
    assert receipt["complete"] is True
    for command in commands:
        assert command[:6] == [
            "systemd-run",
            "--user",
            "--scope",
            "-p",
            "MemoryMax=8G",
            "/usr/bin/time",
        ]
        assert command[6] == "-v"

    assert commands[0][9:] == [
        "/py",
        "-m",
        "signals.style_basket.b3_build",
        "--stage",
        "preflight",
        "--data-end",
        "2023-12-31",
        "--output-dir",
        str(tmp_path / "run" / "research"),
    ]
    assert commands[1][9:14] == [
        "/py",
        "-m",
        "signals.style_basket.b3_build",
        "--stage",
        "all",
    ]
    assert commands[2][9:14] == [
        "/py",
        "-m",
        "backtest.b3_eval",
        "--data-end",
        "2023-12-31",
    ]


def test_runner_stops_immediately_on_a_failed_preflight(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_guarded_b3.subprocess, "run", _fake_runner(commands, [2, 0, 0])
    )

    receipt = run_guarded_b3.run_stages(tmp_path / "run", python="/py")

    assert len(commands) == 1
    assert receipt["stopped_at"] == "preflight"
    assert receipt["complete"] is False


def test_runner_stops_on_a_failed_build(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_guarded_b3.subprocess, "run", _fake_runner(commands, [0, 1, 0])
    )

    receipt = run_guarded_b3.run_stages(tmp_path / "run", python="/py")

    assert len(commands) == 2
    assert receipt["stopped_at"] == "build"


def test_runner_keeps_an_eval_exit_code_of_two_as_evidence(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_guarded_b3.subprocess, "run", _fake_runner(commands, [0, 0, 2])
    )

    receipt = run_guarded_b3.run_stages(tmp_path / "run", python="/py")

    assert len(commands) == 3
    assert receipt["stages"][2]["exit_code"] == 2
    assert receipt["stopped_at"] is None
    assert receipt["complete"] is True


def test_runner_records_peak_rss_from_the_gnu_time_report(tmp_path):
    time_path = tmp_path / "preflight.time.txt"
    time_path.write_text(
        "\tCommand being timed: \"python\"\n"
        "\tMaximum resident set size (kbytes): 3600512\n",
        encoding="utf-8",
    )

    assert run_guarded_b3.peak_rss_kib(time_path) == 3_600_512
    assert run_guarded_b3.peak_rss_kib(tmp_path / "absent.txt") is None


def test_runner_uses_campaign_scoped_output_directories(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_guarded_b3.subprocess, "run", _fake_runner(commands, [0, 0, 0])
    )

    run_guarded_b3.run_stages(tmp_path / "run", python="/py")

    joined = " ".join(" ".join(command) for command in commands)
    assert str(tmp_path / "run" / "research") in joined
    assert str(tmp_path / "run" / "backtest") in joined
    assert "output/style_basket/b3 " not in joined
