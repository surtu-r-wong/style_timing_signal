from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


PIT_SCHEMA_VERSION = 1
BUILD_SCHEMA_VERSION = 2


def current_pit_metadata(statement_types) -> dict:
    return {
        "schema_version": PIT_SCHEMA_VERSION,
        "periodic_statement_policy": "first_disclosure_else_statutory_deadline",
        "periodic_statement_types": sorted(statement_types),
        "first_disclosure_source": "stock_first_disclosure.first_disclosure_date",
        "first_disclosure_coverage": "partial",
        "fallback_policy": "statutory_deadline",
        "dividend_policy": "event_ann_date_capped_by_statutory_deadline",
        "limitations": [{
            "code": "late_filer_fallback",
            "text": (
                "缺失或无效首披日的定期报告回退法定截止日；超期披露者仍可能被过早"
                "视为可知，因此结论保持 provisional。"
            ),
        }],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def data_artifact(path: Path, base: Path) -> dict:
    path = Path(path)
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def load_build_pit_metadata(
    build_path: Path,
    data_path: Path,
    expected_artifact_type: str,
) -> dict:
    """Validate a build sidecar against its CSV and return its PIT contract."""
    build_path, data_path = Path(build_path), Path(data_path)
    try:
        payload = json.loads(build_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"build metadata is missing or invalid: {build_path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("build metadata must be a JSON object")
    if payload.get("schema_version") != BUILD_SCHEMA_VERSION:
        raise RuntimeError(
            f"build metadata schema_version must be {BUILD_SCHEMA_VERSION}"
        )
    if payload.get("artifact_type") != expected_artifact_type:
        raise RuntimeError(
            "build metadata artifact_type mismatch: "
            f"expected {expected_artifact_type!r}, got {payload.get('artifact_type')!r}"
        )

    artifact = payload.get("data_artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("build metadata data_artifact must be an object")
    if artifact.get("path") != data_path.name:
        raise RuntimeError(
            "build metadata data_artifact.path mismatch: "
            f"expected {data_path.name!r}, got {artifact.get('path')!r}"
        )
    try:
        actual_size = data_path.stat().st_size
        actual_sha256 = sha256_file(data_path)
    except OSError as exc:
        raise RuntimeError(f"build metadata data artifact is unreadable: {data_path}") from exc
    if artifact.get("size") != actual_size:
        raise RuntimeError(
            "build metadata data_artifact.size mismatch: "
            f"expected {actual_size}, got {artifact.get('size')!r}"
        )
    if artifact.get("sha256") != actual_sha256:
        raise RuntimeError(
            "build metadata data_artifact.sha256 mismatch: "
            f"expected {actual_sha256}, got {artifact.get('sha256')!r}"
        )

    pit = payload.get("pit")
    if not isinstance(pit, dict):
        raise RuntimeError("build metadata pit_metadata must be an object")
    expected_scalars = {
        "schema_version": PIT_SCHEMA_VERSION,
        "periodic_statement_policy": "first_disclosure_else_statutory_deadline",
        "first_disclosure_source": "stock_first_disclosure.first_disclosure_date",
        "first_disclosure_coverage": "partial",
        "fallback_policy": "statutory_deadline",
        "dividend_policy": "event_ann_date_capped_by_statutory_deadline",
    }
    for field, expected in expected_scalars.items():
        if pit.get(field) != expected:
            raise RuntimeError(
                f"build metadata pit_metadata.{field} must be {expected!r}"
            )

    statement_types = pit.get("periodic_statement_types")
    if (
        not isinstance(statement_types, list)
        or not statement_types
        or any(not isinstance(item, str) or not item for item in statement_types)
        or len(statement_types) != len(set(statement_types))
        or statement_types != sorted(statement_types)
    ):
        raise RuntimeError(
            "build metadata pit_metadata.periodic_statement_types must be a "
            "non-empty sorted unique string list"
        )

    limitations = pit.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise RuntimeError(
            "build metadata pit_metadata.limitations must be a non-empty list"
        )
    limitation_codes = []
    for item in limitations:
        if not isinstance(item, dict) or set(item) != {"code", "text"}:
            raise RuntimeError(
                "build metadata pit_metadata.limitations entries require code and text"
            )
        if any(
            not isinstance(item[field], str) or not item[field]
            for field in ("code", "text")
        ):
            raise RuntimeError(
                "build metadata pit_metadata.limitations code/text must be non-empty strings"
            )
        limitation_codes.append(item["code"])
    if len(limitation_codes) != len(set(limitation_codes)):
        raise RuntimeError(
            "build metadata pit_metadata.limitations codes must be unique"
        )

    return copy.deepcopy(pit)
