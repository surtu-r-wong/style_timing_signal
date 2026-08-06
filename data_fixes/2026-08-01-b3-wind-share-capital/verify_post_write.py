#!/usr/bin/env python3
"""B3 Wind 股本尾巴修复的收口验证：读证据、验哈希、出机器可读判定。

只读现成产物（执行回执 + B3 标准 manifest + 提案链三份），逐条核对验收条件，
任一不过就非零退出。``final_verdict`` 与 ``family_statistical_verdict``
逐字抄自 B3，本脚本不预设也不硬编码任何判定值——包括合法的 ``DATA_BLOCKED``。

注意 ``coverage_audit.csv`` 按 pit_policy × check 双重展开，直接 sum 会翻倍；
本脚本只用它判"是否为 0"，并把口径写进输出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

CAMPAIGN_DIR = Path(__file__).resolve().parent
FINAL_VERIFICATION_NAME = "final_verification.json"
DATA_END = "2023-12-31"
POLICIES = ("legal_deadline", "legal_deadline_plus_one_month_end")
# 111 = 2014-10..2023-12。窗口起点自 2014-01 后移，因为中证1000 于
# 2014-10-17 才发布，在此之前 q1000 无所指。
REQUIRED_FORMATIONS = 111
SHARE_REASON = "DATA_MISSING_SHARES"
CLOSE_REASON = "DATA_MISSING_CLOSE"
EXEMPT_STATUS = "MEASURE_WITH_EXCLUSION"


class VerificationError(RuntimeError):
    """Raised when an acceptance condition fails; the caller exits nonzero."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise VerificationError(f"duplicate JSON key: {key}")
        seen[key] = value
    return seen


def load_json(path: Path) -> dict:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"missing evidence file: {path}") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"malformed JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"evidence file is not an object: {path}")
    return payload


def sha256_file(path: Path) -> str:
    path = Path(path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise VerificationError(f"missing evidence file: {path}") from exc


def _require(condition: object, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def verify_execution_receipt(receipt: dict) -> dict:
    _require(
        receipt.get("data_end") == DATA_END,
        f"execution receipt data_end must be {DATA_END}",
    )
    _require(receipt.get("complete") is True, "B3 execution did not complete")
    stages = receipt.get("stages")
    _require(isinstance(stages, list) and len(stages) == 3, "expected three stages")
    _require(
        [stage.get("name") for stage in stages] == ["preflight", "build", "eval"],
        "stages did not run in the frozen order",
    )
    for stage in stages:
        allowed = stage.get("allowed_exit_codes") or [0]
        _require(
            stage.get("exit_code") in allowed,
            f"stage {stage.get('name')} exited {stage.get('exit_code')}",
        )
        for label, entry in (stage.get("files") or {}).items():
            declared = entry.get("sha256")
            _require(
                declared is not None,
                f"stage {stage.get('name')} {label} has no declared hash",
            )
            _require(
                sha256_file(entry["path"]) == declared,
                f"stage {stage.get('name')} {label} hash mismatch (stale evidence)",
            )
    return {
        "stages": [
            {
                "name": stage["name"],
                "exit_code": stage["exit_code"],
                "wall_seconds": stage.get("wall_seconds"),
                "peak_rss_kib": stage.get("peak_rss_kib"),
            }
            for stage in stages
        ]
    }


def verify_preflight_manifest(manifest_path: Path) -> dict:
    manifest = load_json(manifest_path)
    _require(
        manifest.get("stage") == "preflight", "preflight manifest stage mismatch"
    )
    _require(
        manifest.get("data_end") == DATA_END,
        f"preflight manifest data_end must be {DATA_END}",
    )
    _require(
        manifest.get("status") == "OK",
        f"preflight status is {manifest.get('status')!r}, not OK",
    )
    outputs = manifest.get("outputs")
    _require(isinstance(outputs, dict) and outputs, "preflight declares no outputs")
    root = Path(manifest_path).parent.parent
    for relative, declared in outputs.items():
        _require(
            sha256_file(root / relative) == declared,
            f"preflight output hash mismatch: {relative}",
        )
    return {
        "status": manifest["status"],
        "declared_outputs": sorted(outputs),
        "blockers": list(manifest.get("blockers") or []),
    }


def _coverage_counts(audit: pd.DataFrame, reason: str) -> dict[str, int]:
    rows = audit.loc[audit["reason_code"].astype(str) == reason]
    required = rows.loc[rows["required_formation"].astype(str).isin({"True", "true"})]
    return {
        "all": int(pd.to_numeric(rows["eligible_count"], errors="coerce").fillna(0).sum()),
        "required": int(
            pd.to_numeric(required["eligible_count"], errors="coerce").fillna(0).sum()
        ),
    }


def verify_coverage_audit(audit_path: Path) -> dict:
    audit_path = Path(audit_path)
    try:
        audit = pd.read_csv(audit_path)
    except (OSError, pd.errors.ParserError) as exc:
        raise VerificationError(f"unreadable coverage audit: {audit_path}") from exc
    expected = {
        "pit_policy",
        "formation_date",
        "required_formation",
        "check",
        "status",
        "reason_code",
        "eligible_count",
    }
    missing = expected - set(audit.columns)
    _require(not missing, f"coverage audit missing columns: {sorted(missing)}")

    shares = _coverage_counts(audit, SHARE_REASON)
    close = _coverage_counts(audit, CLOSE_REASON)
    # SHARES is repaired with facts, so it must be gone outright.  CLOSE is a
    # genuine hole in a handful of names per month; it may survive only inside
    # a month that preflight downgraded under the materiality threshold, which
    # the monthly_exposure status check below enforces.
    _require(
        shares["all"] == 0 and shares["required"] == 0,
        f"{SHARE_REASON} is not cleared: {shares}",
    )

    required_rows = audit.loc[
        audit["required_formation"].astype(str).isin({"True", "true"})
    ]
    by_policy: dict[str, int] = {}
    for policy in POLICIES:
        policy_rows = required_rows.loc[
            required_rows["pit_policy"].astype(str) == policy
        ]
        distinct = int(policy_rows["formation_date"].dropna().nunique())
        by_policy[policy] = distinct
        _require(
            distinct == REQUIRED_FORMATIONS,
            f"{policy} has {distinct} required formations, expected "
            f"{REQUIRED_FORMATIONS}",
        )

    exposure_rows = required_rows.loc[
        required_rows["check"].astype(str) == "monthly_exposure"
    ]
    statuses = exposure_rows["status"].astype(str)
    exempted = exposure_rows.loc[statuses == EXEMPT_STATUS]
    blocked = exposure_rows.loc[~statuses.isin({"OK", EXEMPT_STATUS})]
    _require(
        blocked.empty,
        f"{len(blocked)} required monthly_exposure formations are still blocked",
    )
    return {
        "data_missing_shares": shares,
        "data_missing_close": close,
        "required_formations_by_policy": by_policy,
        "blocked_monthly_exposures": int(len(blocked)),
        "months_measured_with_exclusion": int(len(exempted)),
        "note": (
            "coverage_audit.csv expands by pit_policy x check; the counts above "
            "are sums of eligible_count and are only used as zero/non-zero gates"
        ),
    }


def verify_run_manifest(run_manifest_path: Path) -> dict:
    manifest = load_json(run_manifest_path)
    _require(
        str(manifest.get("requested_data_end", ""))[:10] == DATA_END,
        f"run manifest requested_data_end must be {DATA_END}",
    )
    candidates = manifest.get("candidate_statistical_verdicts")
    _require(
        isinstance(candidates, dict) and len(candidates) > 0,
        "candidate_statistical_verdicts is empty; statistics never ran",
    )
    family = manifest.get("family_statistical_verdict")
    _require(
        family is not None and str(family).strip() != "",
        "family_statistical_verdict is missing",
    )
    invalid = manifest.get("invalid_formation_months")
    _require(
        isinstance(invalid, list) and not invalid,
        f"{len(invalid or [])} formation months are still invalid",
    )
    if "final_verdict" not in manifest:
        raise VerificationError("run manifest has no final_verdict")
    return {
        "candidate_statistical_verdicts": candidates,
        "family_statistical_verdict": family,
        # Copied verbatim: a legal DATA_BLOCKED result is preserved, not rewritten.
        "final_verdict": manifest["final_verdict"],
        "invalid_formation_months": list(invalid),
        "materiality_exemptions": manifest.get("materiality_exemptions"),
    }


def verify_proposal_chain(
    proposal_manifest: Path,
    apply_receipt: Path,
    canonical_verification: Path,
) -> dict:
    proposal_hash = sha256_file(proposal_manifest)
    applied = load_json(apply_receipt)
    canonical = load_json(canonical_verification)
    _require(
        applied.get("proposal_manifest_sha256") == proposal_hash,
        "apply receipt is not bound to this proposal manifest",
    )
    _require(
        canonical.get("proposal_manifest_sha256") == proposal_hash,
        "canonical verification is not bound to this proposal manifest",
    )
    _require(
        int(applied.get("readback_mismatches", -1)) == 0,
        "apply receipt reports readback mismatches",
    )
    _require(
        int(canonical.get("mismatches", -1)) == 0,
        "canonical verification reports mismatches",
    )
    return {
        "proposal_manifest_sha256": proposal_hash,
        "apply_receipt_sha256": sha256_file(apply_receipt),
        "canonical_verification_sha256": sha256_file(canonical_verification),
        "coordinates_verified": canonical.get("coordinates"),
        "wind_sourced_coordinates": canonical.get("wind_sourced_coordinates"),
    }


def build_summary(
    *,
    execution_receipt: Path,
    preflight_manifest: Path,
    coverage_audit: Path,
    run_manifest: Path,
    proposal_manifest: Path,
    apply_receipt: Path,
    canonical_verification: Path,
) -> dict:
    receipt = load_json(execution_receipt)
    execution = verify_execution_receipt(receipt)
    preflight = verify_preflight_manifest(preflight_manifest)
    coverage = verify_coverage_audit(coverage_audit)
    evaluation = verify_run_manifest(run_manifest)
    chain = verify_proposal_chain(
        proposal_manifest, apply_receipt, canonical_verification
    )
    return {
        "schema": "b3-wind-share-capital-final-verification",
        "version": 1,
        "data_end": DATA_END,
        "data_missing_shares": coverage["data_missing_shares"],
        "data_missing_close": coverage["data_missing_close"],
        "required_formations_by_policy": coverage["required_formations_by_policy"],
        "months_measured_with_exclusion": coverage["months_measured_with_exclusion"],
        "materiality_exemptions": evaluation["materiality_exemptions"],
        "family_statistical_verdict": evaluation["family_statistical_verdict"],
        "final_verdict": evaluation["final_verdict"],
        "candidate_statistical_verdicts": evaluation[
            "candidate_statistical_verdicts"
        ],
        "invalid_formation_months": evaluation["invalid_formation_months"],
        "preflight": preflight,
        "execution": execution,
        "proposal_chain": chain,
        "coverage_note": coverage["note"],
        "accepted": True,
    }


def _write_json_atomic(path: Path, payload: dict) -> str:
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-receipt", required=True)
    parser.add_argument("--preflight-manifest", required=True)
    parser.add_argument("--coverage-audit", required=True)
    parser.add_argument("--run-manifest", required=True)
    parser.add_argument("--proposal-manifest", required=True)
    parser.add_argument("--apply-receipt", required=True)
    parser.add_argument("--canonical-verification", required=True)
    parser.add_argument(
        "--output", default=str(CAMPAIGN_DIR / FINAL_VERIFICATION_NAME)
    )
    args = parser.parse_args(argv)

    try:
        summary = build_summary(
            execution_receipt=Path(args.execution_receipt),
            preflight_manifest=Path(args.preflight_manifest),
            coverage_audit=Path(args.coverage_audit),
            run_manifest=Path(args.run_manifest),
            proposal_manifest=Path(args.proposal_manifest),
            apply_receipt=Path(args.apply_receipt),
            canonical_verification=Path(args.canonical_verification),
        )
    except VerificationError as exc:
        failure = {
            "schema": "b3-wind-share-capital-final-verification",
            "version": 1,
            "accepted": False,
            "failure": str(exc),
        }
        _write_json_atomic(Path(args.output), failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1

    _write_json_atomic(Path(args.output), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
