"""Pure same-scale production evaluation for frozen B3 candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from numbers import Integral
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypedDict, cast

import numpy as np
import pandas as pd

from backtest.engine import run_strategy
from backtest.b3_structure import (
    DEFAULT_BACKTEST_OUTPUT_DIR,
    DEFAULT_EQUAL_WEIGHT_SIGNAL_PATH,
    DEFAULT_RESEARCH_OUTPUT_DIR,
    MODEL_COMPARISON_COLUMNS,
    MODEL_ROW_ID_COLUMNS,
    ROOT,
    _increment_direction,
    _load_equal_weight_control,
    _validated_model_comparison_output,
    _validated_runner_series,
    apply_model,
    fit_model,
    next_formation_targets,
)
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover
from backtest.positions import production_position
from backtest.rotation_probe import partial_rank_ic
from signals.style_basket.b3_build import require_parent_manifest
from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_exposures import DataBlocked


PRODUCTION_METRICS_COLUMNS = [
    "pit_policy",
    "candidate",
    "component",
    "is_candidate",
    "window",
    "executable",
    "n_obs",
    "ann_return",
    "sharpe",
    "maxdd",
    "turnover",
    "baseline_sharpe",
    "sharpe_difference",
    "baseline_maxdd",
    "maxdd_difference",
    "baseline_turnover",
    "turnover_ratio",
    "partial_ic",
    "gate_name",
    "gate_pass",
    "affects_verdict",
]
PRODUCTION_ROW_ID_COLUMNS = [
    "pit_policy",
    "candidate",
    "component",
    "window",
    "gate_name",
]
BOOTSTRAP_COLUMNS = [
    "pit_policy",
    "candidate",
    "draws",
    "block_days",
    "seed",
    "tail_prob",
    "holm_adjusted_tail",
    "ci05",
    "ci50",
    "ci95",
    "structure_pass",
    "gate_pass",
]
BOOTSTRAP_ROW_ID_COLUMNS = ["pit_policy", "candidate"]
YEARLY_COLUMNS = [
    "pit_policy",
    "candidate",
    "window",
    "row_type",
    "year",
    "excluded_year",
    "n_obs",
    "signed_log_pnl",
    "absolute_pnl_share",
    "strongest_year",
    "ann_return",
    "sharpe",
    "maxdd",
    "is_in_sample",
    "affects_verdict",
]
YEARLY_ROW_ID_COLUMNS = [
    "pit_policy",
    "candidate",
    "window",
    "row_type",
    "year",
    "excluded_year",
]
VERDICT_COLUMNS = [
    "scope",
    "subject",
    "gate",
    "gate_pass",
    "status",
    "reason_code",
    "detail",
    "provisional",
    "affects_statistical",
    "statistical_verdict",
    "final_verdict",
    "shadow_candidate",
    "shadow_start_allowed",
]

RUN_MANIFEST_FIELDS = (
    "config_hash",
    "code_commit",
    "requested_data_end",
    "common_historical_end",
    "stock_price_max_date",
    "index_500_max_date",
    "index_1000_max_date",
    "ic_carry_max_date",
    "im_carry_max_date",
    "salg_valid_through",
    "true_first_disclosure_coverage",
    "im_launch_date",
    "invalid_formation_months",
    "stage_manifest_hashes",
    "input_file_hashes",
    "database_source_evidence",
    "candidate_statistical_verdicts",
    "family_statistical_verdict",
    "final_verdict",
)
_SOURCE_MAX_DATE_FIELDS = (
    "stock_price_max_date",
    "index_500_max_date",
    "index_1000_max_date",
    "ic_carry_max_date",
    "im_carry_max_date",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CANONICAL_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATABASE_SOURCE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
)
_CANONICAL_TICKER_RE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
_PIT_POLICY_RE = re.compile(r"[a-z][a-z0-9_]*")
_PREFLIGHT_OUTPUTS = {
    "coverage_audit.csv",
    "exposure_diagnostics.csv",
}
_PREFLIGHT_BLOCKER_KEYS = {
    "pit_policy",
    "formation_date",
    "required_formation",
    "affects_final",
    "check",
    "side",
    "eligible_count",
    "max_weight",
    "status",
    "reason_code",
    "detail",
}
_VERIFIED_PREFLIGHT_TOKEN = object()

TRADING_CALENDAR_QUERY_TEMPLATE = """SELECT calendar_date, sfe
FROM public.trading_calendar
WHERE calendar_date BETWEEN '2013-05-01' AND %(calendar_end)s
  AND deleted_at IS NULL
ORDER BY calendar_date"""
TRADING_CALENDAR_QUERY_TEMPLATE_HASH = hashlib.sha256(
    TRADING_CALENDAR_QUERY_TEMPLATE.encode("utf-8")
).hexdigest()
TRUE_DISCLOSURE_COVERAGE_BASIS = (
    "explicit true_first_disclosure_verified on model rows for every frozen "
    "PIT policy and formation month from 2014-01 through 2023-12"
)


class _FrozenDict(dict):
    """A JSON-serializable mapping that cannot be mutated after construction."""

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("frozen mapping cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class _FrozenList(list):
    """A JSON-serializable sequence that cannot be mutated."""

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("frozen sequence cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return _FrozenDict(
            {
                key: _freeze_json(mapping[key])
                for key in sorted(mapping, key=str)
            }
        )
    if type(value) is list:
        return _FrozenList(
            _freeze_json(item) for item in cast(list[object], value)
        )
    return value


@dataclass(frozen=True)
class PreflightManifestContract:
    status: str
    data_end: str
    blockers: tuple[_FrozenDict, ...]
    manifest_hash: str
    output_hashes: _FrozenDict
    database_source_evidence: _FrozenDict | None
    _verification_token: object = field(
        default=None,
        repr=False,
        compare=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_strict_json(raw: bytes, label: str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise DataBlocked(f"{label} is not strict JSON") from exc
    if type(payload) is not dict:
        raise DataBlocked(f"{label} must be a JSON object")
    return cast(dict[str, object], payload)


def _canonical_relative_path(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise DataBlocked(f"{label} path is unsafe: {value!r}")
    relative = cast(str, value)
    pure = PurePosixPath(relative)
    if (
        "\\" in relative
        or pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise DataBlocked(f"{label} path is unsafe: {relative!r}")
    return relative


def _verified_relative_file(root: Path, relative: object, label: str) -> Path:
    canonical = _canonical_relative_path(relative, label)
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (root / canonical).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DataBlocked(f"{label} file is missing: {canonical}") from exc
    if not resolved_root.is_dir():
        raise DataBlocked(f"{label} root is not a directory")
    if not candidate.is_relative_to(resolved_root):
        raise DataBlocked(f"{label} path escapes its root: {canonical}")
    if not candidate.is_file():
        raise DataBlocked(f"{label} is not a file: {canonical}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DataBlocked(f"unable to hash input file: {path.name}") from exc
    return digest.hexdigest()


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(cast(str, value)) is None:
        raise DataBlocked(f"{label} hash format is invalid")
    return cast(str, value)


def _canonical_date_string(value: object, label: str) -> str:
    if (
        type(value) is not str
        or _CANONICAL_DATE_RE.fullmatch(cast(str, value)) is None
    ):
        raise DataBlocked(f"{label} must be a canonical date")
    rendered = cast(str, value)
    try:
        timestamp = pd.Timestamp(rendered)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} must be a canonical date") from exc
    if pd.isna(timestamp) or str(timestamp.date()) != rendered:
        raise DataBlocked(f"{label} must be a canonical date")
    return rendered


def _requested_date_string(value: object, label: str) -> str:
    if type(value) is str:
        return _canonical_date_string(value, label)
    timestamp = _strict_timestamp(value, label)
    return str(timestamp.date())


def _validate_preflight_blockers(
    raw: object,
    manifest_status: str,
) -> tuple[_FrozenDict, ...]:
    if type(raw) is not list:
        raise DataBlocked("preflight blockers must be a list")
    blockers = cast(list[object], raw)
    checked: list[_FrozenDict] = []
    statuses: set[str] = set()
    for blocker in blockers:
        if type(blocker) is not dict or set(cast(dict, blocker)) != _PREFLIGHT_BLOCKER_KEYS:
            raise DataBlocked("preflight blockers schema mismatch")
        row = cast(dict[str, object], blocker)
        for key in ("pit_policy", "check", "reason_code"):
            value = row[key]
            if type(value) is not str or not value or value != value.strip():
                raise DataBlocked(f"preflight blockers {key} is invalid")
        for key in ("side", "detail"):
            if type(row[key]) is not str:
                raise DataBlocked(f"preflight blockers {key} is invalid")
        formation_date = row["formation_date"]
        if type(formation_date) is not str:
            raise DataBlocked("preflight blockers formation_date is invalid")
        if formation_date != "NaT":
            try:
                parsed = pd.Timestamp(formation_date)
            except (TypeError, ValueError, OverflowError) as exc:
                raise DataBlocked(
                    "preflight blockers formation_date is invalid"
                ) from exc
            if pd.isna(parsed) or parsed.tz is not None or parsed != parsed.normalize():
                raise DataBlocked("preflight blockers formation_date is invalid")
        if type(row["required_formation"]) is not bool or not row[
            "required_formation"
        ]:
            raise DataBlocked("preflight blockers required_formation is invalid")
        if type(row["affects_final"]) is not bool or not row["affects_final"]:
            raise DataBlocked("preflight blockers affects_final is invalid")
        count = row["eligible_count"]
        if count is not None and (type(count) is not int or count < 0):
            raise DataBlocked("preflight blockers eligible_count is invalid")
        weight = row["max_weight"]
        if weight is not None and (
            type(weight) not in {int, float} or not np.isfinite(weight)
        ):
            raise DataBlocked("preflight blockers max_weight is invalid")
        status = row["status"]
        if type(status) is not str or status not in {
            "DATA_BLOCKED",
            "COVERAGE_BLOCKED",
        }:
            raise DataBlocked("preflight blockers status is invalid")
        statuses.add(cast(str, status))
        checked.append(cast(_FrozenDict, _freeze_json(row)))

    if not statuses:
        expected_status = "OK"
    elif "DATA_BLOCKED" in statuses:
        expected_status = "DATA_BLOCKED"
    else:
        expected_status = "COVERAGE_BLOCKED"
    if manifest_status != expected_status:
        raise DataBlocked("preflight blocker priority is inconsistent with status")
    return tuple(checked)


def _validate_database_source_evidence(raw: object) -> _FrozenDict:
    if type(raw) is not dict or set(cast(dict, raw)) != {
        "consumed_sources",
        "sources",
    }:
        raise DataBlocked("database source evidence schema mismatch")
    evidence = cast(dict[str, object], raw)
    consumed = evidence["consumed_sources"]
    sources = evidence["sources"]
    if type(consumed) is not list or not consumed or type(sources) is not dict:
        raise DataBlocked("database source evidence consumed_sources is invalid")
    names = cast(list[object], consumed)
    if any(
        type(name) is not str
        or _DATABASE_SOURCE_RE.fullmatch(cast(str, name)) is None
        for name in names
    ):
        raise DataBlocked("database source evidence consumed_sources is invalid")
    string_names = cast(list[str], names)
    if len(set(string_names)) != len(string_names) or string_names != sorted(
        string_names
    ):
        raise DataBlocked("database source evidence consumed_sources is invalid")
    source_rows = cast(dict[object, object], sources)
    if set(source_rows) != set(string_names):
        raise DataBlocked(
            "database source evidence consumed_sources must exactly match sources"
        )
    if "public.trading_calendar" not in source_rows:
        raise DataBlocked(
            "database source evidence requires public.trading_calendar"
        )

    expected_fields = {
        "query_template_hash",
        "row_count",
        "min_date",
        "max_date",
    }
    checked_sources: dict[str, object] = {}
    for name in string_names:
        raw_row = source_rows[name]
        if type(raw_row) is not dict or set(cast(dict, raw_row)) != expected_fields:
            raise DataBlocked(f"database source evidence fields are invalid: {name}")
        row = cast(dict[str, object], raw_row)
        query_hash = _require_sha256(
            row["query_template_hash"],
            f"database source evidence {name} query_template",
        )
        row_count = row["row_count"]
        if type(row_count) is not int or row_count < 0:
            raise DataBlocked(
                f"database source evidence row_count is invalid: {name}"
            )
        min_date = row["min_date"]
        max_date = row["max_date"]
        if (min_date is None) != (max_date is None):
            raise DataBlocked(f"database source evidence dates are invalid: {name}")
        if min_date is not None:
            minimum = _canonical_date_string(
                min_date,
                f"database source evidence {name} min_date",
            )
            maximum = _canonical_date_string(
                max_date,
                f"database source evidence {name} max_date",
            )
            if minimum > maximum:
                raise DataBlocked(
                    f"database source evidence date range is invalid: {name}"
                )
        checked_sources[name] = {
            "query_template_hash": query_hash,
            "row_count": row_count,
            "min_date": min_date,
            "max_date": max_date,
        }

    calendar = cast(dict[str, object], checked_sources["public.trading_calendar"])
    if calendar["query_template_hash"] != TRADING_CALENDAR_QUERY_TEMPLATE_HASH:
        raise DataBlocked(
            "public.trading_calendar query_template_hash does not bind the "
            "calendar_date/sfe/deleted_at template"
        )
    return cast(
        _FrozenDict,
        _freeze_json(
            {
                "consumed_sources": string_names,
                "sources": checked_sources,
            }
        ),
    )


def verify_preflight_manifest(
    research_root: str | Path,
    expected_config_hash: str,
    requested_data_end: object | None = None,
) -> PreflightManifestContract:
    """Verify the preflight manifest and every declared output before use."""

    try:
        root = Path(research_root)
    except TypeError as exc:
        raise DataBlocked("preflight research root is invalid") from exc
    manifest_path = _verified_relative_file(
        root,
        "manifests/preflight.json",
        "preflight manifest",
    )
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise DataBlocked("preflight manifest cannot be read") from exc
    manifest = _load_strict_json(manifest_bytes, "preflight manifest")

    base_keys = {
        "stage",
        "config_hash",
        "data_end",
        "status",
        "blockers",
        "outputs",
    }
    if set(manifest) not in (
        base_keys,
        base_keys | {"database_source_evidence"},
    ):
        raise DataBlocked("preflight manifest schema mismatch")
    if manifest["stage"] != "preflight":
        raise DataBlocked("preflight manifest stage mismatch")
    expected_hash = _require_sha256(expected_config_hash, "expected config")
    actual_hash = _require_sha256(manifest["config_hash"], "preflight config")
    if actual_hash != expected_hash:
        raise DataBlocked("preflight config hash mismatch")
    data_end = _canonical_date_string(manifest["data_end"], "preflight data_end")
    if requested_data_end is not None:
        requested = _requested_date_string(
            requested_data_end,
            "requested_data_end",
        )
        if requested != data_end:
            raise DataBlocked("preflight data_end mismatch")
    status = manifest["status"]
    if type(status) is not str or status not in {
        "OK",
        "DATA_BLOCKED",
        "COVERAGE_BLOCKED",
    }:
        raise DataBlocked("preflight status is invalid")
    blockers = _validate_preflight_blockers(
        manifest["blockers"],
        cast(str, status),
    )

    raw_outputs = manifest["outputs"]
    if type(raw_outputs) is not dict or not raw_outputs:
        raise DataBlocked("preflight outputs are invalid")
    output_mapping = cast(dict[object, object], raw_outputs)
    checked_outputs: dict[str, str] = {}
    for raw_relative in sorted(output_mapping, key=str):
        relative = _canonical_relative_path(raw_relative, "preflight output")
        expected_output_hash = _require_sha256(
            output_mapping[raw_relative],
            f"preflight output {relative}",
        )
        output_path = _verified_relative_file(
            root,
            relative,
            "preflight output",
        )
        if _sha256_file(output_path) != expected_output_hash:
            raise DataBlocked(f"preflight output hash mismatch: {relative}")
        checked_outputs[relative] = expected_output_hash
    if not _PREFLIGHT_OUTPUTS.issubset(checked_outputs):
        raise DataBlocked("preflight required outputs are missing")

    database_evidence = None
    if "database_source_evidence" in manifest:
        database_evidence = _validate_database_source_evidence(
            manifest["database_source_evidence"]
        )
    return PreflightManifestContract(
        status=cast(str, status),
        data_end=data_end,
        blockers=blockers,
        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
        output_hashes=_FrozenDict(checked_outputs),
        database_source_evidence=database_evidence,
        _verification_token=_VERIFIED_PREFLIGHT_TOKEN,
    )


def database_source_evidence_blocker(
    contract: PreflightManifestContract,
) -> dict[str, object] | None:
    """Return the run blocker for an evidence-free verified preflight."""

    if (
        type(contract) is not PreflightManifestContract
        or contract._verification_token is not _VERIFIED_PREFLIGHT_TOKEN
    ):
        raise TypeError("contract must come from verify_preflight_manifest")
    if contract.database_source_evidence is not None:
        return None
    return {
        "reason_code": "DATABASE_SOURCE_EVIDENCE_MISSING",
        "status": "DATA_BLOCKED",
        "detail": "verified preflight manifest lacks database_source_evidence",
        "affects_statistical": False,
    }

@dataclass(frozen=True)
class StructureProvenanceContract:
    data_end: str
    structure_coefficients: pd.DataFrame
    model_comparison: pd.DataFrame
    manifest_hash: str
    output_hashes: _FrozenDict


def verify_structure_provenance(
    compact_root: str | Path,
    expected_config_hash: str,
    requested_data_end: object,
) -> StructureProvenanceContract:
    """Verify structure provenance before reading either compact CSV."""

    try:
        root = Path(compact_root)
    except TypeError as exc:
        raise DataBlocked("structure compact root is invalid") from exc
    manifest_candidate = root / "structure_manifest.json"
    if not manifest_candidate.is_file():
        raise DataBlocked(
            "STRUCTURE_PROVENANCE_MISSING: structure_manifest.json is missing"
        )
    manifest_path = _verified_relative_file(
        root,
        "structure_manifest.json",
        "structure manifest",
    )
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise DataBlocked("structure manifest cannot be read") from exc
    manifest = _load_strict_json(manifest_bytes, "structure manifest")
    if set(manifest) != {
        "stage",
        "config_hash",
        "data_end",
        "status",
        "outputs",
    }:
        raise DataBlocked("structure manifest schema mismatch")
    if manifest["stage"] != "structure":
        raise DataBlocked("structure manifest stage mismatch")
    expected_hash = _require_sha256(expected_config_hash, "expected config")
    actual_hash = _require_sha256(manifest["config_hash"], "structure config")
    if actual_hash != expected_hash:
        raise DataBlocked("structure config hash mismatch")
    data_end = _canonical_date_string(manifest["data_end"], "structure data_end")
    requested = _requested_date_string(requested_data_end, "requested_data_end")
    if requested != data_end:
        raise DataBlocked("structure data_end mismatch")
    if manifest["status"] != "OK":
        raise DataBlocked("structure status must be OK")

    raw_outputs = manifest["outputs"]
    expected_outputs = {
        "structure_coefficients.csv",
        "model_comparison.csv",
    }
    if (
        type(raw_outputs) is not dict
        or set(cast(dict, raw_outputs)) != expected_outputs
    ):
        raise DataBlocked("structure output set mismatch")
    outputs = cast(dict[str, object], raw_outputs)
    checked_hashes: dict[str, str] = {}
    paths: dict[str, Path] = {}
    for relative in sorted(expected_outputs):
        expected_output_hash = _require_sha256(
            outputs[relative],
            f"structure output {relative}",
        )
        path = _verified_relative_file(root, relative, "structure output")
        if _sha256_file(path) != expected_output_hash:
            raise DataBlocked(f"structure output hash mismatch: {relative}")
        checked_hashes[relative] = expected_output_hash
        paths[relative] = path

    try:
        coefficients = pd.read_csv(paths["structure_coefficients.csv"])
        comparison = pd.read_csv(paths["model_comparison.csv"])
    except (OSError, TypeError, ValueError) as exc:
        raise DataBlocked("verified structure CSV cannot be read") from exc
    return StructureProvenanceContract(
        data_end=data_end,
        structure_coefficients=coefficients,
        model_comparison=comparison,
        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
        output_hashes=_FrozenDict(checked_hashes),
    )


def _strict_frame_date(value: object, label: str) -> pd.Timestamp:
    if type(value) is str:
        canonical = _canonical_date_string(value, label)
        return pd.Timestamp(canonical)
    return _strict_timestamp(value, label)


def _validated_pit_policies(pit_policies: object) -> list[str]:
    if type(pit_policies) not in {list, tuple} or not pit_policies:
        raise DataBlocked("frozen PIT policy list is invalid")
    policies = list(cast(list[str] | tuple[str, ...], pit_policies))
    if (
        len(set(policies)) != len(policies)
        or any(
            type(policy) is not str
            or _PIT_POLICY_RE.fullmatch(policy) is None
            for policy in policies
        )
    ):
        raise DataBlocked("frozen PIT policy list is invalid")
    return policies


def compute_true_disclosure_coverage(
    monthly_exposures: pd.DataFrame,
    pit_policies: object,
) -> dict[str, object]:
    """Count explicit true-disclosure provenance on the frozen model grid."""

    if not isinstance(monthly_exposures, pd.DataFrame):
        raise DataBlocked("monthly exposures must be a DataFrame")
    required_columns = {
        "universe_role",
        "pit_policy",
        "formation_date",
        "ticker",
        "true_first_disclosure_verified",
    }
    if not required_columns.issubset(monthly_exposures.columns):
        raise DataBlocked("true disclosure coverage schema is incomplete")
    policies = _validated_pit_policies(pit_policies)
    roles = monthly_exposures["universe_role"]
    if any(
        type(role) is not str or role not in {"model", "size_only"}
        for role in roles
    ):
        raise DataBlocked("monthly exposures universe_role is invalid")
    model = monthly_exposures.loc[roles.eq("model")].copy()
    if model.empty:
        raise DataBlocked("true disclosure coverage denominator is empty")
    model_dates = [
        _strict_frame_date(value, "formation_date")
        for value in model["formation_date"]
    ]
    model["_formation_date"] = model_dates
    periods = pd.PeriodIndex(model["_formation_date"], freq="M")
    required_periods = pd.period_range("2014-01", "2023-12", freq="M")
    required_mask = periods.isin(required_periods)
    required = model.loc[required_mask].copy()
    if required.empty:
        raise DataBlocked("true disclosure coverage denominator is empty")

    observed_policies = required["pit_policy"]
    if any(
        type(policy) is not str
        or _PIT_POLICY_RE.fullmatch(policy) is None
        for policy in observed_policies
    ):
        raise DataBlocked("true disclosure coverage policy is invalid")
    if set(observed_policies) != set(policies):
        raise DataBlocked("true disclosure coverage policy set mismatch")
    tickers = required["ticker"]
    if any(
        type(ticker) is not str
        or _CANONICAL_TICKER_RE.fullmatch(ticker) is None
        for ticker in tickers
    ):
        raise DataBlocked("true disclosure coverage ticker is invalid")
    verified = required["true_first_disclosure_verified"]
    if any(not isinstance(value, (bool, np.bool_)) for value in verified):
        raise DataBlocked(
            "true_first_disclosure_verified must contain strict boolean values"
        )
    if required.duplicated(
        ["pit_policy", "_formation_date", "ticker"]
    ).any():
        raise DataBlocked("true disclosure coverage contains duplicate model keys")
    required_row_periods = pd.PeriodIndex(required["_formation_date"], freq="M")
    for policy in policies:
        policy_periods = set(
            required_row_periods[required["pit_policy"].eq(policy)]
        )
        missing = set(required_periods).difference(policy_periods)
        if missing:
            raise DataBlocked(
                "true disclosure coverage is missing a required month for "
                f"policy {policy}"
            )

    denominator = int(len(required))
    numerator = int(sum(bool(value) for value in verified))
    return {
        "verified_numerator": numerator,
        "required_denominator": denominator,
        "ratio": float(numerator / denominator),
        "coverage_basis": TRUE_DISCLOSURE_COVERAGE_BASIS,
    }


def salg_valid_through(monthly_exposures: pd.DataFrame) -> str:
    """Return the earliest SalG valid-through date on the latest model formation."""

    if not isinstance(monthly_exposures, pd.DataFrame):
        raise DataBlocked("monthly exposures must be a DataFrame")
    required_columns = {
        "universe_role",
        "formation_date",
        "salg_source_end_date",
    }
    if not required_columns.issubset(monthly_exposures.columns):
        raise DataBlocked("SalG freshness schema is incomplete")
    roles = monthly_exposures["universe_role"]
    if any(
        type(role) is not str or role not in {"model", "size_only"}
        for role in roles
    ):
        raise DataBlocked("monthly exposures universe_role is invalid")
    model = monthly_exposures.loc[roles.eq("model")].copy()
    if model.empty:
        raise DataBlocked("SalG freshness has no model rows")
    model["_formation_date"] = [
        _strict_frame_date(value, "formation_date")
        for value in model["formation_date"]
    ]
    latest = model["_formation_date"].max()
    latest_rows = model.loc[model["_formation_date"].eq(latest)]
    valid_through: list[pd.Timestamp] = []
    mapping = {
        (3, 31): (0, 8, 31),
        (6, 30): (0, 10, 31),
        (9, 30): (1, 4, 30),
        (12, 31): (1, 4, 30),
    }
    for value in latest_rows["salg_source_end_date"]:
        source_end = _strict_frame_date(value, "salg_source_end_date")
        rule = mapping.get((source_end.month, source_end.day))
        if rule is None:
            raise DataBlocked("salg_source_end_date must be a canonical quarter-end")
        year_offset, month, day = rule
        valid_through.append(
            pd.Timestamp(
                year=source_end.year + year_offset,
                month=month,
                day=day,
            )
        )
    return str(min(valid_through).date())


def _raw_carry_max_date(raw: object, label: str) -> str | None:
    if not isinstance(raw, pd.Series):
        raise DataBlocked(f"{label} raw carry must be a Series")
    if not isinstance(raw.index, pd.DatetimeIndex):
        raise DataBlocked(f"{label} raw carry index must be a DatetimeIndex")
    index = raw.index
    if index.tz is not None:
        raise DataBlocked(f"{label} raw carry index must be timezone-naive")
    if index.hasnans:
        raise DataBlocked(f"{label} raw carry index cannot contain NaT")
    if not index.equals(index.normalize()):
        raise DataBlocked(f"{label} raw carry index must be normalized")
    if not index.is_unique:
        raise DataBlocked(f"{label} raw carry index must be unique")
    if not index.is_monotonic_increasing:
        raise DataBlocked(f"{label} raw carry index must be increasing")
    if pd.api.types.is_bool_dtype(raw.dtype) or not pd.api.types.is_numeric_dtype(
        raw.dtype
    ):
        raise DataBlocked(f"{label} raw carry values must be numeric")
    try:
        values = raw.to_numpy(dtype=float, copy=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} raw carry values must be numeric") from exc
    if not np.isfinite(values).all():
        raise DataBlocked(f"{label} raw carry values must be finite")
    if raw.empty:
        return None
    return str(index[-1].date())


def compute_raw_carry_freshness(
    raw_ic_carry: object,
    raw_im_carry: object,
    expected_latest_cash_date: object,
) -> dict[str, object]:
    """Bind carry freshness to raw source maxima, never a filled panel."""

    expected = _requested_date_string(
        expected_latest_cash_date,
        "expected_latest_cash_date",
    )
    ic_max = _raw_carry_max_date(raw_ic_carry, "IC")
    im_max = _raw_carry_max_date(raw_im_carry, "IM")
    fresh = (
        ic_max is not None
        and im_max is not None
        and ic_max >= expected
        and im_max >= expected
    )
    return {
        "ic_carry_max_date": ic_max,
        "im_carry_max_date": im_max,
        "expected_latest_cash_date": expected,
        "fresh": fresh,
    }


def _validated_disclosure_coverage(raw: object) -> dict[str, object]:
    expected_keys = {
        "verified_numerator",
        "required_denominator",
        "ratio",
        "coverage_basis",
    }
    if type(raw) is not dict or set(cast(dict, raw)) != expected_keys:
        raise DataBlocked("true disclosure coverage schema mismatch")
    coverage = cast(dict[str, object], raw)
    numerator = coverage["verified_numerator"]
    denominator = coverage["required_denominator"]
    ratio = coverage["ratio"]
    if type(numerator) is not int or numerator < 0:
        raise DataBlocked("true disclosure verified_numerator is invalid")
    if type(denominator) is not int or denominator <= 0:
        raise DataBlocked("true disclosure required_denominator is invalid")
    if numerator > denominator:
        raise DataBlocked("true disclosure numerator exceeds denominator")
    if type(ratio) is not float or not np.isfinite(ratio):
        raise DataBlocked("true disclosure ratio is invalid")
    expected_ratio = numerator / denominator
    if ratio != expected_ratio:
        raise DataBlocked("true disclosure ratio is inconsistent")
    if coverage["coverage_basis"] != TRUE_DISCLOSURE_COVERAGE_BASIS:
        raise DataBlocked("true disclosure coverage_basis is invalid")
    return coverage


def _validated_carry_freshness(raw: object) -> dict[str, object]:
    expected_keys = {
        "ic_carry_max_date",
        "im_carry_max_date",
        "expected_latest_cash_date",
        "fresh",
    }
    if type(raw) is not dict or set(cast(dict, raw)) != expected_keys:
        raise DataBlocked("carry freshness schema mismatch")
    carry = cast(dict[str, object], raw)
    expected = _canonical_date_string(
        carry["expected_latest_cash_date"],
        "expected_latest_cash_date",
    )
    maxima: dict[str, str | None] = {}
    for key in ("ic_carry_max_date", "im_carry_max_date"):
        value = carry[key]
        maxima[key] = (
            None if value is None else _canonical_date_string(value, key)
        )
    fresh = carry["fresh"]
    if type(fresh) is not bool:
        raise DataBlocked("carry freshness fresh must be boolean")
    expected_fresh = (
        maxima["ic_carry_max_date"] is not None
        and maxima["im_carry_max_date"] is not None
        and cast(str, maxima["ic_carry_max_date"]) >= expected
        and cast(str, maxima["im_carry_max_date"]) >= expected
    )
    if fresh is not expected_fresh:
        raise DataBlocked("carry freshness fresh is inconsistent with raw maxima")
    return carry


def freshness_blockers(
    true_disclosure_coverage: object,
    salg_valid_through_date: object,
    requested_data_end: object,
    raw_carry_freshness: object,
) -> list[dict[str, object]]:
    """Translate validated freshness evidence into run-level blockers."""

    coverage = _validated_disclosure_coverage(true_disclosure_coverage)
    salg_through = _canonical_date_string(
        salg_valid_through_date,
        "salg_valid_through",
    )
    requested = _requested_date_string(requested_data_end, "requested_data_end")
    carry = _validated_carry_freshness(raw_carry_freshness)
    expected_cash = cast(str, carry["expected_latest_cash_date"])
    if expected_cash > requested:
        raise DataBlocked(
            "expected_latest_cash_date cannot be later than requested_data_end"
        )

    blockers: list[dict[str, object]] = []
    if not cast(bool, carry["fresh"]):
        ic_max = carry["ic_carry_max_date"] or "MISSING"
        im_max = carry["im_carry_max_date"] or "MISSING"
        blockers.append(
            {
                "reason_code": "CARRY_FRESHNESS",
                "status": "DATA_BLOCKED",
                "detail": (
                    "raw IC/IM carry does not reach expected latest cash date "
                    f"{expected_cash}; maxima: IC={ic_max}, IM={im_max}"
                ),
                "affects_statistical": False,
            }
        )
    if salg_through < requested:
        blockers.append(
            {
                "reason_code": "SALG_FRESHNESS",
                "status": "DATA_BLOCKED",
                "detail": (
                    f"SalG valid-through {salg_through} is earlier than "
                    f"requested data_end {requested}"
                ),
                "affects_statistical": False,
            }
        )
    numerator = cast(int, coverage["verified_numerator"])
    denominator = cast(int, coverage["required_denominator"])
    ratio = cast(float, coverage["ratio"])
    if ratio < 1.0:
        blockers.append(
            {
                "reason_code": "TRUE_DISCLOSURE_COVERAGE",
                "status": "DATA_BLOCKED",
                "detail": (
                    "true disclosure coverage is "
                    f"{numerator}/{denominator} ({ratio:.6f}); "
                    "full explicit coverage required"
                ),
                "affects_statistical": False,
            }
        )
    return sorted(blockers, key=lambda row: cast(str, row["reason_code"]))


def hash_files(
    root: str | Path,
    relative_paths: object,
) -> dict[str, str]:
    """Hash an explicit non-empty registration of files beneath one root."""

    try:
        resolved_root = Path(root)
    except TypeError as exc:
        raise DataBlocked("file hash root is invalid") from exc
    if type(relative_paths) not in {list, tuple} or not relative_paths:
        raise DataBlocked("file hash registrations must be a non-empty list or tuple")
    paths = cast(list[object] | tuple[object, ...], relative_paths)
    canonical = [
        _canonical_relative_path(path, "file hash registration") for path in paths
    ]
    if len(canonical) != len(set(canonical)):
        raise DataBlocked("file hash registrations must be unique")
    return {
        relative: _sha256_file(
            _verified_relative_file(
                resolved_root,
                relative,
                "file hash registration",
            )
        )
        for relative in sorted(canonical)
    }


_FAMILY_RANK = {"STOP": 0, "MEASURE_ONLY": 1, "PASS_SHADOW": 2}


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be boolean")
    return bool(value)


def candidate_statistical_label(
    structure_pass: bool,
    production_pass: bool,
    executable_boundary_pass: bool,
) -> str:
    structure = _require_bool(structure_pass, "structure_pass")
    production = _require_bool(production_pass, "production_pass")
    boundary = _require_bool(
        executable_boundary_pass,
        "executable_boundary_pass",
    )
    if not structure:
        return "STOP"
    if production and boundary:
        return "PASS_SHADOW"
    return "MEASURE_ONLY"


def family_best_wins(labels: list[str]) -> str:
    if (
        type(labels) is not list
        or not labels
        or any(
            type(label) is not str or label not in _FAMILY_RANK
            for label in labels
        )
    ):
        raise ValueError("family requires evaluated candidate labels")
    return max(labels, key=_FAMILY_RANK.__getitem__)


def final_verdict(
    statistical_verdict: str | None,
    data_blocked: bool,
    coverage_blocked: bool,
) -> str:
    data = _require_bool(data_blocked, "data_blocked")
    coverage = _require_bool(coverage_blocked, "coverage_blocked")
    if statistical_verdict is not None and (
        type(statistical_verdict) is not str
        or statistical_verdict not in _FAMILY_RANK
    ):
        raise ValueError("statistical verdict is invalid")
    if data:
        return "DATA_BLOCKED"
    if coverage:
        return "COVERAGE_BLOCKED"
    if statistical_verdict is None:
        raise ValueError("unblocked run requires statistical verdict")
    return statistical_verdict


@dataclass(frozen=True)
class RunEvidence:
    """Data-boundary evidence a run may claim only for the stages it reached.

    Every field stays ``None`` on a blocked run: the run legally never read the
    series or caches the value would have to come from.
    """

    stock_price_max_date: str | None = None
    index_500_max_date: str | None = None
    index_1000_max_date: str | None = None
    ic_carry_max_date: str | None = None
    im_carry_max_date: str | None = None
    salg_valid_through: str | None = None
    true_first_disclosure_coverage: dict[str, object] | None = None
    stage_manifest_hashes: dict[str, str] = field(default_factory=dict)
    input_file_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationFrames:
    production_metrics: pd.DataFrame
    bootstrap: pd.DataFrame
    yearly: pd.DataFrame


def _verdict_gate_audit_row(
    scope: str,
    subject: str,
    gate: str,
    gate_pass: bool,
    reason_code: str,
    detail: str,
) -> dict[str, object]:
    passed = _require_bool(gate_pass, "verdict audit gate_pass")
    return {
        "scope": scope,
        "subject": subject,
        "gate": gate,
        "gate_pass": passed,
        "status": "PASS" if passed else "FAIL",
        "reason_code": "" if passed else reason_code,
        "detail": detail,
        "provisional": False,
        "affects_statistical": True,
        "statistical_verdict": None,
        "final_verdict": None,
        "shadow_candidate": False,
        "shadow_start_allowed": False,
    }


def _validated_run_blockers(
    run_blockers: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    if run_blockers is None:
        return []
    if type(run_blockers) is not list:
        raise ValueError("run_blockers must be a list")
    expected_keys = {
        "reason_code",
        "status",
        "detail",
        "affects_statistical",
    }
    validated: list[dict[str, object]] = []
    for blocker in run_blockers:
        if type(blocker) is not dict or set(blocker) != expected_keys:
            raise ValueError("run blocker schema mismatch")
        reason = blocker["reason_code"]
        status = blocker["status"]
        detail = blocker["detail"]
        affects = blocker["affects_statistical"]
        if (
            type(reason) is not str
            or not reason
            or reason != reason.strip()
        ):
            raise ValueError("run blocker reason_code is invalid")
        if reason == "PIT_POLICY_FLIP":
            raise ValueError("run blocker reason_code is reserved")
        if (
            type(status) is not str
            or status not in {"DATA_BLOCKED", "COVERAGE_BLOCKED"}
        ):
            raise ValueError("run blocker status is invalid")
        if type(detail) is not str or detail != detail.strip():
            raise ValueError("run blocker detail is invalid")
        if not isinstance(affects, (bool, np.bool_)):
            raise ValueError("run blocker affects_statistical must be boolean")
        validated.append(
            {
                "reason_code": reason,
                "status": status,
                "detail": detail,
                "affects_statistical": bool(affects),
            }
        )
    reasons = [str(blocker["reason_code"]) for blocker in validated]
    if len(reasons) != len(set(reasons)):
        raise ValueError("run blocker reason_code must be unique")
    return sorted(
        validated,
        key=lambda blocker: (
            str(blocker["reason_code"]),
            str(blocker["status"]),
            str(blocker["detail"]),
        ),
    )


def _validate_model_domain(
    comparison: pd.DataFrame,
    policies: tuple[str, ...],
    candidates: tuple[str, ...],
    report_present: bool,
) -> None:
    report = _require_bool(report_present, "model report presence")
    q_specs = {
        "qblend": ("B3_unified", "blend"),
        "q500": ("B3_dual_target", "500"),
        "q1000": ("B3_dual_target", "1000"),
    }
    gate_names = (
        "m1_increment",
        "partial_ic",
        "stability",
        "state_coverage",
    )
    expected: dict[tuple[str, ...], tuple[bool, bool]] = {}

    def add(
        identity: tuple[str, ...],
        *,
        is_in_sample: bool = False,
        affects_verdict: bool = False,
    ) -> None:
        expected[identity] = (is_in_sample, affects_verdict)

    for policy in policies:
        for gate_name, window in {
            "beta_h_same_sign": "structure",
            "interaction_axis_corr": "2021-2023",
            "hard_sort_complete": "structure",
        }.items():
            add(
                (
                    policy,
                    "PUBLIC",
                    "",
                    "",
                    window,
                    "",
                    gate_name,
                ),
                affects_verdict=True,
            )
        for q, (candidate, target) in q_specs.items():
            add(
                (
                    policy,
                    candidate,
                    q,
                    target,
                    "2014-2020",
                    "M1",
                    "",
                ),
                is_in_sample=True,
            )
            for model in ("M0", "M1"):
                add(
                    (
                        policy,
                        candidate,
                        q,
                        target,
                        "2021-2023",
                        model,
                        "",
                    ),
                    affects_verdict=True,
                )
            for year in ("2021", "2022", "2023"):
                add(
                    (
                        policy,
                        candidate,
                        q,
                        target,
                        year,
                        "M1",
                        "",
                    ),
                    affects_verdict=True,
                )
            if report:
                for model in ("M0", "M1"):
                    add(
                        (
                            policy,
                            candidate,
                            q,
                            target,
                            "2024-2026-report-only",
                            model,
                            "",
                        )
                    )
            for gate_name in gate_names[:3]:
                add(
                    (
                        policy,
                        candidate,
                        q,
                        target,
                        "2021-2023",
                        "M1",
                        gate_name,
                    ),
                    affects_verdict=True,
                )
            for window, affects_verdict in (
                ("2014-2017", True),
                ("2018-2020", True),
                ("2021-2023", True),
                ("2014-2020", False),
            ):
                add(
                    (
                        policy,
                        candidate,
                        q,
                        target,
                        window,
                        "M1",
                        "state_coverage",
                    ),
                    affects_verdict=affects_verdict,
                )
        for candidate in candidates:
            for gate_name in gate_names:
                add(
                    (
                        policy,
                        candidate,
                        "",
                        "",
                        "2021-2023",
                        "",
                        gate_name,
                    ),
                    affects_verdict=True,
                )
    add(
        ("ALL", "", "", "", "run", "", "PIT_POLICY_FLIP"),
        affects_verdict=True,
    )

    actual = set(
        comparison[MODEL_ROW_ID_COLUMNS].itertuples(
            index=False,
            name=None,
        )
    )
    if actual != set(expected):
        raise DataBlocked(
            "model comparison row or gate identity lies outside frozen domain"
        )
    for row in comparison.itertuples(index=False):
        identity = tuple(
            str(getattr(row, column))
            for column in MODEL_ROW_ID_COLUMNS
        )
        expected_in_sample, expected_affects = expected[identity]
        if (
            bool(row.is_in_sample) != expected_in_sample
            or bool(row.affects_verdict) != expected_affects
        ):
            raise DataBlocked(
                "model comparison row flags differ from frozen domain"
            )

    candidate_legs = {
        "B3_unified": ("qblend",),
        "B3_dual_target": ("q500", "q1000"),
    }
    for policy in policies:
        for candidate, legs in candidate_legs.items():
            for gate_name in gate_names:
                leg = comparison[
                    comparison["pit_policy"].eq(policy)
                    & comparison["candidate"].eq(candidate)
                    & comparison["q"].isin(legs)
                    & comparison["gate_name"].eq(gate_name)
                    & comparison["affects_verdict"]
                ]
                aggregate = comparison[
                    comparison["pit_policy"].eq(policy)
                    & comparison["candidate"].eq(candidate)
                    & comparison["q"].eq("")
                    & comparison["gate_name"].eq(gate_name)
                    & comparison["affects_verdict"]
                ]
                if (
                    len(aggregate) != 1
                    or bool(aggregate["gate_pass"].iloc[0])
                    != bool(leg["gate_pass"].astype(bool).all())
                ):
                    raise DataBlocked(
                        "model comparison q-leg gate disagrees with aggregate gate"
                    )


def _validate_hashable_row_ids(
    frame: pd.DataFrame,
    columns: list[str],
    row_id_columns: list[str],
    label: str,
) -> None:
    if not isinstance(frame, pd.DataFrame) or list(frame.columns) != columns:
        raise DataBlocked(f"{label} schema mismatch")
    for column in row_id_columns:
        for value in frame[column]:
            try:
                hash(value)
            except TypeError as exc:
                raise DataBlocked(
                    f"{label}.{column} row identity must be hashable"
                ) from exc


def _observable_pit_policy_flip(
    comparison: pd.DataFrame,
    policies: tuple[str, ...],
    candidates: tuple[str, ...],
) -> bool:
    first_policy, second_policy = policies
    aggregate_gate_names = (
        "m1_increment",
        "partial_ic",
        "stability",
        "state_coverage",
    )
    for candidate in candidates:
        vectors: list[tuple[bool, ...]] = []
        for policy in (first_policy, second_policy):
            aggregate = comparison[
                comparison["pit_policy"].eq(policy)
                & comparison["candidate"].eq(candidate)
                & comparison["q"].eq("")
                & comparison["window"].eq("2021-2023")
                & comparison["gate_name"].isin(aggregate_gate_names)
            ].set_index("gate_name")
            vectors.append(
                tuple(
                    bool(aggregate.at[gate_name, "gate_pass"])
                    for gate_name in aggregate_gate_names
                )
            )
        if vectors[0] != vectors[1]:
            return True

    # Confirmation M0/M1 metric rows serialize every input required by the
    # Task9 increment-direction rule. Beta coefficient signs do not survive
    # into this table, so the producer PIT row remains authoritative for a
    # beta-only flip while these two observable conditions can only tighten it.
    for q in ("qblend", "q500", "q1000"):
        directions: list[int | str] = []
        for policy in (first_policy, second_policy):
            metric = comparison[
                comparison["pit_policy"].eq(policy)
                & comparison["q"].eq(q)
                & comparison["window"].eq("2021-2023")
                & comparison["gate_name"].eq("")
                & comparison["model"].isin(("M0", "M1"))
            ].set_index("model")
            try:
                increment = float(metric.at["M1", "oos_r2"]) - float(
                    metric.at["M0", "oos_r2"]
                )
            except (TypeError, ValueError) as exc:
                raise DataBlocked(
                    "model comparison increment metrics are invalid"
                ) from exc
            directions.append(_increment_direction(increment))
        if directions[0] != directions[1]:
            return True
    return False


def assemble_verdicts(
    model_comparison: pd.DataFrame,
    frames: EvaluationFrames,
    cfg: dict,
    run_blockers: list[dict[str, object]] | None = None,
) -> pd.DataFrame:
    _evaluation_config(cfg)
    policies = tuple(cfg["pit"]["policies"])
    candidates = tuple(cfg["candidates"])
    if not isinstance(frames, EvaluationFrames):
        raise ValueError("frames must be EvaluationFrames")
    if not all(
        isinstance(frame, pd.DataFrame)
        for frame in (
            frames.production_metrics,
            frames.bootstrap,
            frames.yearly,
        )
    ):
        raise ValueError("evaluation outputs must be DataFrames")
    for frame, columns, row_ids, label in (
        (
            model_comparison,
            MODEL_COMPARISON_COLUMNS,
            MODEL_ROW_ID_COLUMNS,
            "model comparison",
        ),
        (
            frames.production_metrics,
            PRODUCTION_METRICS_COLUMNS,
            PRODUCTION_ROW_ID_COLUMNS,
            "production metrics",
        ),
        (
            frames.bootstrap,
            BOOTSTRAP_COLUMNS,
            BOOTSTRAP_ROW_ID_COLUMNS,
            "bootstrap",
        ),
        (
            frames.yearly,
            YEARLY_COLUMNS,
            YEARLY_ROW_ID_COLUMNS,
            "yearly",
        ),
    ):
        _validate_hashable_row_ids(
            frame,
            columns,
            row_ids,
            label,
        )
    blockers = _validated_run_blockers(run_blockers)

    report_present = (
        "window" in frames.production_metrics
        and frames.production_metrics["window"].eq(
            "2024-2026-report-only"
        ).any()
    )
    comparison = _validated_model_comparison_output(model_comparison)
    model_report_present = comparison["window"].eq(
        "2024-2026-report-only"
    ).any()
    _validate_model_domain(
        comparison,
        policies,
        candidates,
        bool(model_report_present),
    )
    model_structure, _ = _validated_model_evidence(comparison, policies)
    production = _validate_production_output(
        frames.production_metrics,
        policies,
        report_present=bool(report_present),
    )
    bootstrap = _validate_bootstrap_output(frames.bootstrap, policies)
    _validate_yearly_output(
        frames.yearly,
        policies,
        report_present=bool(report_present),
    )

    full_gate_names = {
        "sharpe_improvement",
        "maxdd_worsening",
        "turnover_multiple",
        "partial_ic",
    }
    boundary_gate_names = {
        "post_im_min_days",
        "post_im_sharpe_difference",
        "post_im_maxdd_difference",
        "post_im_partial_ic",
    }
    rows: list[dict[str, object]] = []
    structure_rows = comparison[
        comparison["pit_policy"].isin(policies)
        & comparison["gate_name"].ne("")
        & comparison["affects_verdict"]
    ]
    for _, gate_row in structure_rows.iterrows():
        subject = "|".join(
            [
                str(gate_row["candidate"]),
                f"q={gate_row['q'] or '-'}",
                f"target={gate_row['target'] or '-'}",
                f"window={gate_row['window'] or '-'}",
                f"model={gate_row['model'] or '-'}",
            ]
        )
        rows.append(
            _verdict_gate_audit_row(
                f"structure/{gate_row['pit_policy']}",
                subject,
                str(gate_row["gate_name"]),
                bool(gate_row["gate_pass"]),
                "STRUCTURE_GATE_FAILED",
                "model comparison structure gate",
            )
        )
    production_gate_rows = production[
        production["component"].eq("aggregate")
        & production["window"].eq("2021-2023")
        & production["gate_name"].isin(full_gate_names)
        & production["affects_verdict"]
    ]
    for _, gate_row in production_gate_rows.iterrows():
        rows.append(
            _verdict_gate_audit_row(
                f"production/{gate_row['pit_policy']}",
                str(gate_row["candidate"]),
                str(gate_row["gate_name"]),
                bool(gate_row["gate_pass"]),
                "PRODUCTION_GATE_FAILED",
                "2021-2023 aggregate production gate",
            )
        )
    candidate_labels: dict[tuple[str, str], str] = {}
    candidate_reasons: dict[tuple[str, str], str] = {}
    for policy in policies:
        for candidate in candidates:
            bootstrap_row = bootstrap[
                bootstrap["pit_policy"].eq(policy)
                & bootstrap["candidate"].eq(candidate)
            ].iloc[0]
            structure_pass = bool(bootstrap_row["structure_pass"])
            if structure_pass != model_structure[(policy, candidate)]:
                raise RuntimeError(
                    "bootstrap structure result disagrees with model evidence"
                )
            rows.append(
                _verdict_gate_audit_row(
                    f"bootstrap/{policy}",
                    candidate,
                    "holm_adjusted_tail",
                    bool(bootstrap_row["gate_pass"]),
                    "BOOTSTRAP_GATE_FAILED",
                    (
                        "holm_adjusted_tail="
                        f"{float(bootstrap_row['holm_adjusted_tail']):.12g}"
                    ),
                )
            )
            full = production[
                production["pit_policy"].eq(policy)
                & production["candidate"].eq(candidate)
                & production["component"].eq("aggregate")
                & production["window"].eq("2021-2023")
                & production["gate_name"].isin(full_gate_names)
                & production["affects_verdict"]
            ]
            full_pass = bool(full["gate_pass"].astype(bool).all())
            bootstrap_pass = bool(bootstrap_row["gate_pass"])
            production_pass = full_pass and bootstrap_pass
            if candidate == "B3_unified":
                boundary_pass = True
                rows.append(
                    _verdict_gate_audit_row(
                        f"boundary/{policy}",
                        candidate,
                        "unified_executable_boundary",
                        True,
                        "EXECUTABLE_BOUNDARY_FAILED",
                        "unified executable boundary is always satisfied",
                    )
                )
            else:
                boundary = production[
                    production["pit_policy"].eq(policy)
                    & production["candidate"].eq(candidate)
                    & production["component"].eq("aggregate")
                    & production["window"].eq("post-IM")
                    & production["gate_name"].isin(boundary_gate_names)
                    & production["affects_verdict"]
                ]
                boundary_pass = bool(
                    boundary["gate_pass"].astype(bool).all()
                )
                for _, gate_row in boundary.iterrows():
                    rows.append(
                        _verdict_gate_audit_row(
                            f"boundary/{policy}",
                            candidate,
                            str(gate_row["gate_name"]),
                            bool(gate_row["gate_pass"]),
                            "EXECUTABLE_BOUNDARY_FAILED",
                            "post-IM aggregate production gate",
                        )
                    )
            label = candidate_statistical_label(
                structure_pass,
                production_pass,
                boundary_pass,
            )
            if not structure_pass:
                reason_code = "STRUCTURE_GATE_FAILED"
                detail = "bootstrap.structure_pass=false"
            elif not full_pass:
                reason_code = "PRODUCTION_GATE_FAILED"
                failed = sorted(
                    full.loc[
                        ~full["gate_pass"].astype(bool),
                        "gate_name",
                    ].astype(str)
                )
                detail = "failed gates: " + ",".join(failed)
            elif not bootstrap_pass:
                reason_code = "BOOTSTRAP_GATE_FAILED"
                detail = (
                    "holm_adjusted_tail="
                    f"{float(bootstrap_row['holm_adjusted_tail']):.12g}"
                )
            elif not boundary_pass:
                reason_code = "EXECUTABLE_BOUNDARY_FAILED"
                failed = sorted(
                    boundary.loc[
                        ~boundary["gate_pass"].astype(bool),
                        "gate_name",
                    ].astype(str)
                )
                detail = "failed gates: " + ",".join(failed)
            else:
                reason_code = ""
                detail = ""
            candidate_labels[(policy, candidate)] = label
            candidate_reasons[(policy, candidate)] = reason_code
            rows.append(
                {
                    "scope": f"candidate/{policy}",
                    "subject": candidate,
                    "gate": "statistical_verdict",
                    "gate_pass": label == "PASS_SHADOW",
                    "status": label,
                    "reason_code": reason_code,
                    "detail": detail,
                    "provisional": label in {"STOP", "MEASURE_ONLY"},
                    "affects_statistical": True,
                    "statistical_verdict": label,
                    "final_verdict": None,
                    "shadow_candidate": (
                        policy == "legal_deadline"
                        and label == "PASS_SHADOW"
                    ),
                    "shadow_start_allowed": False,
                }
            )
    headline_policy = "legal_deadline"
    headline_label = family_best_wins(
        [
            candidate_labels[(headline_policy, candidate)]
            for candidate in candidates
        ]
    )
    headline_failed = headline_label != "PASS_SHADOW"
    rows.append(
        {
            "scope": f"family/{headline_policy}",
            "subject": "B3",
            "gate": "statistical_verdict",
            "gate_pass": headline_label == "PASS_SHADOW",
            "status": headline_label,
            "reason_code": (
                "HEADLINE_CANDIDATES_FAILED" if headline_failed else ""
            ),
            "detail": (
                ",".join(
                    (
                        f"{candidate}="
                        f"{candidate_labels[(headline_policy, candidate)]}:"
                        f"{candidate_reasons[(headline_policy, candidate)]}"
                    )
                    for candidate in candidates
                )
                if headline_failed
                else ""
            ),
            "provisional": False,
            "affects_statistical": True,
            "statistical_verdict": headline_label,
            "final_verdict": None,
            "shadow_candidate": False,
            "shadow_start_allowed": False,
        }
    )
    pit_row = comparison[
        comparison["pit_policy"].eq("ALL")
        & comparison["window"].eq("run")
        & comparison["gate_name"].eq("PIT_POLICY_FLIP")
    ].iloc[0]
    pit_pass = bool(pit_row["gate_pass"]) and not (
        _observable_pit_policy_flip(comparison, policies, candidates)
    )
    active_run_blockers = [
        (str(blocker["reason_code"]), str(blocker["status"]))
        for blocker in blockers
    ]
    if not pit_pass:
        active_run_blockers.append(("PIT_POLICY_FLIP", "DATA_BLOCKED"))
    active_run_blockers.sort(
        key=lambda blocker: (
            0 if blocker[1] == "DATA_BLOCKED" else 1,
            blocker[0],
        )
    )
    data_blocked = (not pit_pass) or any(
        blocker["status"] == "DATA_BLOCKED" for blocker in blockers
    )
    coverage_blocked = any(
        blocker["status"] == "COVERAGE_BLOCKED" for blocker in blockers
    )
    run_final = final_verdict(
        headline_label,
        data_blocked=data_blocked,
        coverage_blocked=coverage_blocked,
    )
    if not active_run_blockers:
        final_reason_code = ""
    elif len(active_run_blockers) == 1:
        final_reason_code = active_run_blockers[0][0]
    else:
        final_reason_code = "MULTIPLE_RUN_BLOCKERS"
    final_detail = ",".join(
        f"{reason_code}:{status}"
        for reason_code, status in active_run_blockers
    )
    rows.append(
        {
            "scope": "run",
            "subject": "ALL",
            "gate": "PIT_POLICY_FLIP",
            "gate_pass": pit_pass,
            "status": "PASS" if pit_pass else "DATA_BLOCKED",
            "reason_code": "" if pit_pass else "PIT_POLICY_FLIP",
            "detail": (
                "PIT policy verdict directions agree"
                if pit_pass
                else "PIT policy verdict directions disagree"
            ),
            "provisional": False,
            "affects_statistical": False,
            "statistical_verdict": None,
            "final_verdict": None,
            "shadow_candidate": False,
            "shadow_start_allowed": False,
        }
    )
    for blocker in blockers:
        rows.append(
            {
                "scope": "run/blocker",
                "subject": blocker["reason_code"],
                "gate": "run_blocker",
                "gate_pass": False,
                "status": blocker["status"],
                "reason_code": blocker["reason_code"],
                "detail": blocker["detail"],
                "provisional": False,
                "affects_statistical": blocker["affects_statistical"],
                "statistical_verdict": None,
                "final_verdict": None,
                "shadow_candidate": False,
                "shadow_start_allowed": False,
            }
        )
    for row in rows:
        if (
            row["scope"] == f"candidate/{headline_policy}"
            and bool(row["shadow_candidate"])
        ):
            row["shadow_start_allowed"] = run_final == "PASS_SHADOW"
    rows.append(
        {
            "scope": "run",
            "subject": "ALL",
            "gate": "final_verdict",
            "gate_pass": run_final == "PASS_SHADOW",
            "status": run_final,
            "reason_code": final_reason_code,
            "detail": final_detail,
            "provisional": False,
            "affects_statistical": False,
            "statistical_verdict": headline_label,
            "final_verdict": run_final,
            "shadow_candidate": False,
            "shadow_start_allowed": False,
        }
    )
    output = pd.DataFrame(rows, columns=VERDICT_COLUMNS)
    if output.duplicated(["scope", "subject", "gate"]).any():
        raise RuntimeError("verdict row identity is not unique")
    return output.sort_values(
        ["scope", "subject", "gate"],
        kind="mergesort",
    ).reset_index(drop=True)


def blocked_verdict_rows(
    run_blockers: list[dict[str, object]],
) -> pd.DataFrame:
    """Emit the only verdict rows a blocked run may legally claim."""

    blockers = _validated_run_blockers(run_blockers)
    if not blockers:
        raise ValueError("blocked run requires at least one blocker")
    statuses = {str(blocker["status"]) for blocker in blockers}
    run_final = final_verdict(
        None,
        "DATA_BLOCKED" in statuses,
        "COVERAGE_BLOCKED" in statuses,
    )
    rows = [
        {
            "scope": "run/blocker",
            "subject": blocker["reason_code"],
            "gate": "run_blocker",
            "gate_pass": False,
            "status": blocker["status"],
            "reason_code": blocker["reason_code"],
            "detail": blocker["detail"],
            "provisional": False,
            "affects_statistical": blocker["affects_statistical"],
            "statistical_verdict": None,
            "final_verdict": None,
            "shadow_candidate": False,
            "shadow_start_allowed": False,
        }
        for blocker in blockers
    ]
    rows.append(
        {
            "scope": "run",
            "subject": "ALL",
            "gate": "final_verdict",
            "gate_pass": False,
            "status": run_final,
            "reason_code": run_final,
            "detail": "blocked run never reached candidate statistics",
            "provisional": False,
            "affects_statistical": False,
            "statistical_verdict": None,
            "final_verdict": run_final,
            "shadow_candidate": False,
            "shadow_start_allowed": False,
        }
    )
    output = pd.DataFrame(rows, columns=VERDICT_COLUMNS)
    if output.duplicated(["scope", "subject", "gate"]).any():
        raise RuntimeError("verdict row identity is not unique")
    return output.sort_values(
        ["scope", "subject", "gate"],
        kind="mergesort",
    ).reset_index(drop=True)


def git_commit() -> str:
    """Return the exact commit the run executed from."""

    try:
        rendered = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DataBlocked("code commit cannot be resolved") from exc
    commit = rendered.strip()
    if _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise DataBlocked("code commit is not a full git commit hash")
    return commit


def _optional_date(value: object, label: str) -> str | None:
    return None if value is None else _canonical_date_string(value, label)


def _validated_hash_map(raw: object, label: str) -> dict[str, str]:
    if type(raw) is not dict:
        raise DataBlocked(f"{label} must be a mapping")
    entries = cast(dict[object, object], raw)
    validated: dict[str, str] = {}
    for key, value in entries.items():
        if type(key) is not str or not key:
            raise DataBlocked(f"{label} keys must be non-empty strings")
        validated[key] = _require_sha256(value, f"{label} {key}")
    return dict(sorted(validated.items()))


def _verdict_cell(row: pd.Series, column: str) -> str | None:
    value = row[column]
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if type(value) is not str:
        raise DataBlocked(f"verdict {column} must be a string or null")
    return value


def build_run_manifest(
    cfg: dict,
    preflight: PreflightManifestContract,
    verdicts: pd.DataFrame,
    requested_data_end: object,
    *,
    evidence: RunEvidence | None = None,
) -> dict[str, object]:
    """Seal what the run consumed, what it refused and what it concluded."""

    if (
        type(preflight) is not PreflightManifestContract
        or preflight._verification_token is not _VERIFIED_PREFLIGHT_TOKEN
    ):
        raise TypeError("preflight must come from verify_preflight_manifest")
    if evidence is None:
        evidence = RunEvidence()
    if type(evidence) is not RunEvidence:
        raise TypeError("evidence must be RunEvidence")
    if (
        not isinstance(verdicts, pd.DataFrame)
        or list(verdicts.columns) != VERDICT_COLUMNS
    ):
        raise DataBlocked("verdict rows schema mismatch")
    final_rows = verdicts.loc[verdicts["gate"].eq("final_verdict")]
    if len(final_rows) != 1:
        raise DataBlocked("run manifest requires exactly one final verdict row")
    final_row = final_rows.iloc[0]
    family_statistical_verdict = _verdict_cell(final_row, "statistical_verdict")
    if family_statistical_verdict is None and evidence != RunEvidence():
        raise RuntimeError(
            "a run without a family statistical verdict cannot carry evidence"
        )

    settings = _evaluation_config(cfg)
    maxima = {
        name: _optional_date(getattr(evidence, name), name)
        for name in _SOURCE_MAX_DATE_FIELDS
    }
    observed = [value for value in maxima.values() if value is not None]
    common_historical_end = (
        min(observed) if len(observed) == len(_SOURCE_MAX_DATE_FIELDS) else None
    )
    coverage = evidence.true_first_disclosure_coverage
    candidates = {
        f"{str(row['scope']).removeprefix('candidate/')}/{row['subject']}": (
            _verdict_cell(row, "statistical_verdict")
        )
        for _, row in verdicts.iterrows()
        if str(row["scope"]).startswith("candidate/")
        and row["gate"] == "statistical_verdict"
    }
    manifest: dict[str, object] = {
        "config_hash": config_hash(cfg),
        "code_commit": git_commit(),
        "requested_data_end": _requested_date_string(
            requested_data_end,
            "requested_data_end",
        ),
        "common_historical_end": common_historical_end,
        **maxima,
        "salg_valid_through": _optional_date(
            evidence.salg_valid_through,
            "salg_valid_through",
        ),
        "true_first_disclosure_coverage": (
            None if coverage is None else _validated_disclosure_coverage(coverage)
        ),
        "im_launch_date": str(settings["im_launch"].date()),
        "invalid_formation_months": sorted(
            {
                str(blocker["formation_date"])[:7]
                for blocker in preflight.blockers
                if str(blocker["formation_date"]) != "NaT"
            }
        ),
        "stage_manifest_hashes": _validated_hash_map(
            evidence.stage_manifest_hashes,
            "stage_manifest_hashes",
        ),
        "input_file_hashes": _validated_hash_map(
            evidence.input_file_hashes,
            "input_file_hashes",
        ),
        "database_source_evidence": preflight.database_source_evidence,
        "candidate_statistical_verdicts": candidates,
        "family_statistical_verdict": family_statistical_verdict,
        "final_verdict": _verdict_cell(final_row, "final_verdict"),
    }
    if set(manifest) != set(RUN_MANIFEST_FIELDS):
        raise RuntimeError("run manifest field set is not frozen")
    return manifest


def write_run_manifest(path: str | Path, manifest: dict[str, object]) -> Path:
    """Write the sealed manifest atomically once every product is closed."""

    if type(manifest) is not dict or set(manifest) != set(RUN_MANIFEST_FIELDS):
        raise DataBlocked("run manifest schema mismatch")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.tmp")
    rendered = json.dumps(
        manifest,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )
    try:
        temp_path.write_text(rendered, encoding="utf-8")
        temp_path.replace(target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target


class EvaluationSettings(TypedDict):
    confirmation: tuple[pd.Timestamp, pd.Timestamp]
    report: tuple[pd.Timestamp, pd.Timestamp]
    ic_launch: pd.Timestamp
    im_launch: pd.Timestamp
    cost_bps: float
    sharpe_improvement: float
    maxdd_worsening: float
    turnover_multiple: float
    post_im_min_days: int
    block_days: int
    draws: int
    seed: int
    tail_threshold: float


def _validate_datetime_index(index: pd.Index, label: str) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise DataBlocked(f"{label} index must be a DatetimeIndex")
    if index.empty:
        raise DataBlocked(f"{label} index must not be empty")
    if index.tz is not None:
        raise DataBlocked(f"{label} index must be timezone-naive")
    if not index.equals(index.normalize()):
        raise DataBlocked(f"{label} index must contain normalized dates")
    if not index.is_unique:
        raise DataBlocked(f"{label} index must be unique")
    if not index.is_monotonic_increasing:
        raise DataBlocked(f"{label} index must be strictly increasing")
    return index


def _validate_finite_series(
    values: pd.Series,
    label: str,
    *,
    returns: bool = False,
    positions: bool = False,
) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise DataBlocked(f"{label} must be a Series")
    _validate_datetime_index(values.index, label)
    try:
        array = values.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise DataBlocked(f"{label} must be numeric") from exc
    if not np.isfinite(array).all():
        raise DataBlocked(f"{label} must contain only finite values")
    if returns and np.any(array <= -1.0):
        raise DataBlocked(f"{label} returns must be greater than -100%")
    if positions and not np.isin(array, [0.0, 1.0]).all():
        raise DataBlocked(f"{label} must contain only long-flat positions")
    return pd.Series(array, index=values.index, name=values.name, dtype=float)


def _require_same_grid(reference: pd.DatetimeIndex, values: pd.Series, label: str) -> None:
    if not values.index.equals(reference):
        raise DataBlocked(f"{label} must exactly match the benchmark calendar")


def materialize_carry(
    raw: pd.Series,
    calendar: pd.DatetimeIndex,
    launch_date: pd.Timestamp,
) -> pd.Series:
    calendar = _validate_datetime_index(calendar, "cash calendar")
    raw = _validate_finite_series(raw, "raw carry")
    if not isinstance(launch_date, pd.Timestamp):
        raise DataBlocked("carry launch date must be a Timestamp")
    if launch_date.tz is not None or launch_date != launch_date.normalize():
        raise DataBlocked("carry launch date must be timezone-naive and normalized")
    if not raw.index.isin(calendar).all():
        raise DataBlocked("raw carry contains dates outside the cash calendar")

    post_launch_calendar = calendar[calendar >= launch_date]
    observed_post_launch = raw.index[raw.index >= launch_date]
    if len(post_launch_calendar) and not len(observed_post_launch):
        raise DataBlocked("raw carry has no post-launch observations")

    common_end = raw.index.max()
    usable_calendar = calendar[calendar <= common_end]
    result = raw.reindex(usable_calendar)
    pre_launch = usable_calendar < launch_date
    result.loc[pre_launch] = 0.0
    if result.loc[~pre_launch].isna().any():
        raise DataBlocked("internal post-launch carry gap")
    return result.astype(float).rename(raw.name)


def two_leg_candidate_returns(
    position_500: pd.Series,
    position_1000: pd.Series,
    return_500: pd.Series,
    return_1000: pd.Series,
    carry_500: pd.Series,
    carry_1000: pd.Series,
    cost_bps: float,
) -> pd.Series:
    if isinstance(cost_bps, (bool, np.bool_)):
        raise DataBlocked("cost_bps must be a finite nonnegative number")
    try:
        cost = float(cost_bps)
    except (TypeError, ValueError) as exc:
        raise DataBlocked("cost_bps must be a finite nonnegative number") from exc
    if not np.isfinite(cost) or cost < 0.0:
        raise DataBlocked("cost_bps must be a finite nonnegative number")

    return_500 = _validate_finite_series(return_500, "500 cash returns", returns=True)
    return_1000 = _validate_finite_series(
        return_1000, "1000 cash returns", returns=True
    )
    position_500 = _validate_finite_series(
        position_500, "500 positions", positions=True
    )
    position_1000 = _validate_finite_series(
        position_1000, "1000 positions", positions=True
    )
    carry_500 = _validate_finite_series(carry_500, "500 carry")
    carry_1000 = _validate_finite_series(carry_1000, "1000 carry")

    calendar = return_500.index
    for values, label in [
        (return_1000, "1000 cash returns"),
        (position_500, "500 positions"),
        (position_1000, "1000 positions"),
        (carry_500, "500 carry"),
        (carry_1000, "1000 carry"),
    ]:
        _require_same_grid(calendar, values, label)

    leg_500 = run_strategy(position_500, return_500, cost, carry_500)["ret"]
    leg_1000 = run_strategy(position_1000, return_1000, cost, carry_1000)["ret"]
    combined = (0.5 * leg_500 + 0.5 * leg_1000).rename("ret")
    if not np.isfinite(combined.to_numpy(dtype=float)).all():
        raise DataBlocked("candidate returns are non-finite")
    return combined


def paired_moving_block_tail(
    candidate: pd.Series,
    baseline: pd.Series,
    block_days: int,
    draws: int,
    seed: int,
) -> dict[str, float]:
    candidate = _validate_finite_series(
        candidate, "candidate bootstrap returns", returns=True
    )
    baseline = _validate_finite_series(
        baseline, "baseline bootstrap returns", returns=True
    )
    _require_same_grid(candidate.index, baseline, "baseline bootstrap returns")

    for value, label, allow_zero in [
        (block_days, "block_days", False),
        (draws, "draws", False),
        (seed, "seed", True),
    ]:
        if (
            isinstance(value, (bool, np.bool_))
            or not isinstance(value, Integral)
            or (value < 0 if allow_zero else value <= 0)
        ):
            qualifier = "nonnegative" if allow_zero else "positive"
            raise ValueError(f"{label} must be a {qualifier} integer")
    if block_days > len(candidate):
        raise ValueError("paired bootstrap sample shorter than block")

    values = np.column_stack(
        [
            candidate.to_numpy(dtype=float),
            baseline.to_numpy(dtype=float),
        ]
    )
    n_obs = len(values)
    starts = np.arange(n_obs - block_days + 1)
    blocks_per_draw = int(np.ceil(n_obs / block_days))
    rng = np.random.default_rng(seed)
    differences: np.ndarray = np.empty(draws, dtype=float)
    for draw in range(draws):
        selected = rng.choice(starts, size=blocks_per_draw, replace=True)
        sample = np.concatenate(
            [values[start : start + block_days] for start in selected],
            axis=0,
        )[:n_obs]
        differences[draw] = sharpe(pd.Series(sample[:, 0])) - sharpe(
            pd.Series(sample[:, 1])
        )
    if not np.isfinite(differences).all():
        raise DataBlocked("paired bootstrap produced non-finite Sharpe differences")
    return {
        "tail_prob": float(
            (1 + np.count_nonzero(differences <= 0.0)) / (draws + 1)
        ),
        "ci05": float(np.quantile(differences, 0.05)),
        "ci50": float(np.quantile(differences, 0.50)),
        "ci95": float(np.quantile(differences, 0.95)),
    }


def holm_style_adjust(raw: dict[str, float]) -> dict[str, float]:
    family = {"B3_unified", "B3_dual_target"}
    if not isinstance(raw, dict) or set(raw) != family:
        raise ValueError("Holm-style family must contain exactly two candidates")
    checked: dict[str, float] = {}
    for candidate, value in raw.items():
        if isinstance(value, (bool, np.bool_)):
            raise ValueError("Holm-style tail probabilities must be finite in [0, 1]")
        try:
            probability = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Holm-style tail probabilities must be finite in [0, 1]"
            ) from exc
        if not np.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(
                "Holm-style tail probabilities must be finite in [0, 1]"
            )
        checked[candidate] = probability

    first, second = sorted(
        checked,
        key=lambda candidate: (checked[candidate], candidate),
    )
    first_adjusted = min(1.0, 2.0 * checked[first])
    second_adjusted = min(1.0, max(first_adjusted, checked[second]))
    return {first: first_adjusted, second: second_adjusted}


def passes_tail_gate(adjusted_tail: float, threshold: float) -> bool:
    checked: list[float] = []
    for value, label in [
        (adjusted_tail, "adjusted tail probability"),
        (threshold, "tail threshold"),
    ]:
        if isinstance(value, (bool, np.bool_)):
            raise ValueError(f"{label} must be finite")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be finite") from exc
        if not np.isfinite(numeric):
            raise ValueError(f"{label} must be finite")
        checked.append(numeric)
    probability, cutoff = checked
    if not 0.0 <= probability <= 1.0:
        raise ValueError("adjusted tail probability must be in [0, 1]")
    if not 0.0 < cutoff <= 1.0:
        raise ValueError("tail threshold must be in (0, 1]")
    return probability < cutoff


def _strict_timestamp(value: object, label: str) -> pd.Timestamp:
    if isinstance(value, (bool, int, float, np.number)):
        raise DataBlocked(f"{label} must be a normalized date")
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} must be a normalized date") from exc
    if pd.isna(timestamp) or timestamp.tz is not None or timestamp != timestamp.normalize():
        raise DataBlocked(f"{label} must be a normalized date")
    return timestamp


def _validate_formations(
    formation_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
) -> pd.DatetimeIndex:
    formations = _validate_datetime_index(formation_dates, "formation dates")
    if len(formations) < 2:
        raise DataBlocked("formation dates must contain at least two dates")
    periods = formations.to_period("M")
    if periods.has_duplicates or not periods.equals(
        pd.period_range(periods[0], periods[-1], freq="M")
    ):
        raise DataBlocked("formation dates must be monthly continuous")
    if not formations.isin(calendar).all():
        raise DataBlocked("formation dates must lie on the daily calendar")
    required_periods = pd.period_range("2014-01", "2023-12", freq="M")
    required_formations = formations[periods.isin(required_periods)]
    if not required_formations.to_period("M").equals(required_periods):
        raise DataBlocked(
            "formation dates must cover every fixed-window month"
        )
    calendar_periods = calendar.to_period("M")
    expected_month_ends = pd.DatetimeIndex(
        [
            calendar[calendar_periods == period][-1]
            for period in required_periods
            if (calendar_periods == period).any()
        ]
    )
    if (
        len(expected_month_ends) != len(required_periods)
        or not required_formations.equals(expected_month_ends)
    ):
        raise DataBlocked(
            "formation dates must equal fixed-window monthly last trading days"
        )
    return formations


def _validate_score_inputs(
    state_components: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> tuple[
    pd.DataFrame,
    dict[str, pd.Series],
    pd.Series,
    pd.DatetimeIndex,
    tuple[str, ...],
    tuple[pd.Timestamp, pd.Timestamp],
]:
    if not isinstance(cfg, dict):
        raise DataBlocked("B3 configuration must be a mapping")
    _evaluation_config(cfg)
    try:
        policies = tuple(cfg["pit"]["policies"])
        discovery_values = cfg["windows"]["discovery"]
    except (KeyError, TypeError) as exc:
        raise DataBlocked("B3 configuration lacks PIT or discovery settings") from exc
    if (
        not policies
        or len(set(policies)) != len(policies)
        or any(
            not isinstance(policy, str)
            or not policy
            or policy != policy.strip()
            for policy in policies
        )
    ):
        raise DataBlocked("B3 PIT policies must be unique canonical strings")
    if not isinstance(discovery_values, list) or len(discovery_values) != 2:
        raise DataBlocked("B3 discovery window must contain two dates")
    discovery = (
        _strict_timestamp(discovery_values[0], "discovery start"),
        _strict_timestamp(discovery_values[1], "discovery end"),
    )
    if discovery[0] > discovery[1]:
        raise DataBlocked("B3 discovery window is reversed")

    if not isinstance(target_returns, dict) or set(target_returns) != {
        "blend",
        "500",
        "1000",
    }:
        raise DataBlocked("target returns must contain blend, 500 and 1000")
    targets = {
        name: _validate_finite_series(values, f"{name} target returns", returns=True)
        for name, values in target_returns.items()
    }
    calendar = targets["500"].index
    for name, values in targets.items():
        _require_same_grid(calendar, values, f"{name} target returns")
    expected_blend = 0.5 * targets["500"] + 0.5 * targets["1000"]
    if not np.allclose(
        targets["blend"].to_numpy(dtype=float),
        expected_blend.to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-15,
    ):
        raise DataBlocked("blend target returns must equal the 50/50 cash blend")
    control = _validate_finite_series(equal_weight_signal, "equal_weight signal")
    _require_same_grid(calendar, control, "equal_weight signal")
    formations = _validate_formations(formation_dates, calendar)

    required = {"date", "pit_policy", "q", "state", "F_U", "F_D", "F_X"}
    if (
        not isinstance(state_components, pd.DataFrame)
        or state_components.empty
        or state_components.columns.has_duplicates
        or not required.issubset(state_components.columns)
    ):
        raise DataBlocked("state components schema is incomplete")
    states = state_components.copy()
    states["date"] = [
        _strict_timestamp(value, "state component date")
        for value in states["date"]
    ]
    for column in ("pit_policy", "q", "state"):
        if not states[column].map(
            lambda value: isinstance(value, str)
            and bool(value)
            and value == value.strip()
        ).all():
            raise DataBlocked(f"state components {column} is invalid")
    if set(states["pit_policy"]) != set(policies):
        raise DataBlocked("state component policy set mismatch")
    if set(states["q"]) != {"qblend", "q500", "q1000"}:
        raise DataBlocked("state component q set mismatch")
    if not states["state"].isin({"UU", "DD", "DIV"}).all():
        raise DataBlocked("state component state label is invalid")
    keys = ["date", "pit_policy", "q"]
    if states.duplicated(keys).any():
        raise DataBlocked("state components contain duplicate keys")
    for column in ("F_U", "F_D", "F_X"):
        if states[column].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise DataBlocked(f"state components {column} cannot be boolean")
        numeric = pd.to_numeric(states[column], errors="coerce")
        if numeric.isna().any() or not np.isfinite(numeric.to_numpy()).all():
            raise DataBlocked(f"state components {column} must be finite")
        states[column] = numeric.astype(float)
    for policy in policies:
        for q in ("qblend", "q500", "q1000"):
            dates = pd.DatetimeIndex(
                states.loc[
                    states["pit_policy"].eq(policy) & states["q"].eq(q),
                    "date",
                ].sort_values(kind="mergesort")
            )
            if not dates.equals(calendar):
                raise DataBlocked("state component daily grids must match cash returns")
    return states, targets, control, formations, policies, discovery


def fit_frozen_m1_scores(
    state_components: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> dict[tuple[str, str], pd.Series]:
    states, targets, _, formations, policies, discovery = _validate_score_inputs(
        state_components,
        target_returns,
        equal_weight_signal,
        formation_dates,
        cfg,
    )
    target_for_q = {"qblend": "blend", "q500": "500", "q1000": "1000"}
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    )
    output: dict[tuple[str, str], pd.Series] = {}
    for policy in policies:
        for q, target_name in target_for_q.items():
            features = (
                states[
                    states["pit_policy"].eq(policy) & states["q"].eq(q)
                ]
                .sort_values("date", kind="mergesort")
                .set_index("date")[["F_U", "F_D", "F_X"]]
            )
            monthly_target = next_formation_targets(targets[target_name], formations)
            monthly = features.reindex(monthly_target.index).copy()
            if monthly.isna().any().any():
                raise DataBlocked("formation-date M1 features are incomplete")
            monthly["target"] = monthly_target
            target_end = period_end.reindex(monthly.index)
            discovery_mask = (
                monthly.index.to_series().ge(discovery[0])
                & target_end.le(discovery[1])
            )
            training = monthly.loc[discovery_mask.to_numpy()].copy()
            if training.empty:
                raise DataBlocked("M1 discovery training window is empty")
            model = fit_model(
                training,
                ["F_U", "F_D", "F_X"],
                "target",
            )
            output[(policy, q)] = apply_model(
                features,
                model,
                include_intercept=False,
            )
    return output


def yearly_contributions(
    pit_policy: str,
    candidate: str,
    returns: pd.Series,
    window: str,
    is_in_sample: bool,
) -> pd.DataFrame:
    for value, label in [
        (pit_policy, "yearly PIT policy"),
        (candidate, "yearly candidate"),
        (window, "yearly window"),
    ]:
        if not isinstance(value, str) or not value or value != value.strip():
            raise DataBlocked(f"{label} must be a canonical nonempty string")
    if not isinstance(is_in_sample, (bool, np.bool_)):
        raise DataBlocked("yearly is_in_sample must be boolean")
    clean = _validate_finite_series(returns, "yearly returns", returns=True)
    years = clean.index.year
    unique_years = np.unique(years)
    single_year_report = (
        len(unique_years) == 1
        and window == "2024-2026-report-only"
        and not bool(is_in_sample)
    )
    if len(unique_years) < 2 and not single_year_report:
        raise DataBlocked("yearly contributions require at least two years")

    log_returns = pd.Series(
        np.log1p(clean.to_numpy(dtype=float)),
        index=clean.index,
    )
    log_pnl = log_returns.groupby(years).sum()
    if not np.isfinite(log_pnl.to_numpy(dtype=float)).all():
        raise DataBlocked("yearly log P&L is non-finite")
    denominator = float(log_pnl.abs().sum())
    strongest_year = int(log_pnl.abs().idxmax())
    rows: list[dict[str, object]] = []
    for year, signed_value in log_pnl.items():
        year = int(year)
        sample = clean.loc[years == year]
        rows.append(
            {
                "pit_policy": pit_policy,
                "candidate": candidate,
                "window": window,
                "row_type": "year",
                "year": year,
                "excluded_year": pd.NA,
                "n_obs": int(len(sample)),
                "signed_log_pnl": float(signed_value),
                "absolute_pnl_share": (
                    float(abs(signed_value) / denominator)
                    if denominator > 0.0
                    else float("nan")
                ),
                "strongest_year": strongest_year,
                "ann_return": ann_return(sample),
                "sharpe": sharpe(sample),
                "maxdd": max_drawdown(sample),
                "is_in_sample": bool(is_in_sample),
                "affects_verdict": False,
            }
        )
    remaining = clean.loc[years != strongest_year]
    if remaining.empty and not single_year_report:
        raise DataBlocked("strongest-year exclusion leaves no observations")
    rows.append(
        {
            "pit_policy": pit_policy,
            "candidate": candidate,
            "window": window,
            "row_type": "excluding_strongest",
            "year": pd.NA,
            "excluded_year": strongest_year,
            "n_obs": int(len(remaining)),
            "signed_log_pnl": float("nan"),
            "absolute_pnl_share": float("nan"),
            "strongest_year": strongest_year,
            "ann_return": ann_return(remaining) if not remaining.empty else float("nan"),
            "sharpe": sharpe(remaining) if not remaining.empty else float("nan"),
            "maxdd": max_drawdown(remaining) if not remaining.empty else float("nan"),
            "is_in_sample": bool(is_in_sample),
            "affects_verdict": False,
        }
    )
    output = pd.DataFrame(rows, columns=YEARLY_COLUMNS)
    if output.duplicated(YEARLY_ROW_ID_COLUMNS).any():
        raise RuntimeError("yearly contribution row identity is not unique")
    return output.reset_index(drop=True)


def _frozen_config_types_match(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        actual_dict = cast(dict[object, object], actual)
        expected_dict = cast(dict[object, object], expected)
        return all(
            key not in actual_dict
            or _frozen_config_types_match(actual_dict[key], value)
            for key, value in expected_dict.items()
        )
    if type(expected) is list:
        actual_list = cast(list[object], actual)
        expected_list = cast(list[object], expected)
        return all(
            _frozen_config_types_match(actual_value, expected_value)
            for actual_value, expected_value in zip(
                actual_list,
                expected_list,
                strict=False,
            )
        )
    return True


def _evaluation_config(cfg: dict) -> EvaluationSettings:
    expected_windows = {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    expected_execution = {
        "annualization": 245,
        "cost_bps": 3.0,
        "im_launch_date": "2022-07-22",
        "ic_launch_date": "2015-04-16",
    }
    expected_gates = {
        "sharpe_improvement": 0.10,
        "maxdd_worsening": 0.02,
        "turnover_multiple": 1.50,
        "post_im_min_days": 252,
    }
    expected_bootstrap = {
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "adjusted_tail_max": 0.10,
    }
    try:
        pit = cfg["pit"]
        frozen_sections = {
            "candidates": cfg["candidates"],
            "windows": cfg["windows"],
            "pit": pit,
            "execution": cfg["execution"],
            "production_gates": cfg["production_gates"],
            "bootstrap": cfg["bootstrap"],
        }
        expected_sections = {
            "candidates": ["B3_unified", "B3_dual_target"],
            "windows": expected_windows,
            "pit": {
                "policies": [
                    "legal_deadline",
                    "legal_deadline_plus_one_month_end",
                ],
                "industry_pit_start": "2021-01-01",
            },
            "execution": expected_execution,
            "production_gates": expected_gates,
            "bootstrap": expected_bootstrap,
        }
        if not _frozen_config_types_match(
            frozen_sections,
            expected_sections,
        ):
            raise DataBlocked(
                "B3 evaluation configuration type differs from preregistration"
            )
        exact = (
            cfg["candidates"] == ["B3_unified", "B3_dual_target"]
            and cfg["windows"] == expected_windows
            and isinstance(pit, dict)
            and set(pit) == {"policies", "industry_pit_start"}
            and pit["policies"]
            == ["legal_deadline", "legal_deadline_plus_one_month_end"]
            and pit["industry_pit_start"] == "2021-01-01"
            and cfg["execution"] == expected_execution
            and cfg["production_gates"] == expected_gates
            and cfg["bootstrap"] == expected_bootstrap
        )
    except (KeyError, TypeError) as exc:
        raise DataBlocked("B3 evaluation configuration is incomplete") from exc
    if not exact:
        raise DataBlocked("B3 evaluation configuration differs from preregistration")
    return {
        "confirmation": (
            pd.Timestamp("2021-01-01"),
            pd.Timestamp("2023-12-31"),
        ),
        "report": (pd.Timestamp("2024-01-01"), pd.Timestamp("2026-12-31")),
        "ic_launch": pd.Timestamp("2015-04-16"),
        "im_launch": pd.Timestamp("2022-07-22"),
        "cost_bps": 3.0,
        "sharpe_improvement": 0.10,
        "maxdd_worsening": 0.02,
        "turnover_multiple": 1.50,
        "post_im_min_days": 252,
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "tail_threshold": 0.10,
    }


def _validated_model_evidence(
    model_comparison: pd.DataFrame,
    policies: tuple[str, ...],
) -> tuple[
    dict[tuple[str, str], bool],
    dict[tuple[str, str], tuple[float, dict[int, float], bool, int]],
]:
    if (
        not isinstance(model_comparison, pd.DataFrame)
        or model_comparison.empty
        or list(model_comparison.columns) != MODEL_COMPARISON_COLUMNS
    ):
        raise DataBlocked("model comparison schema mismatch")
    evidence = model_comparison.copy()
    if evidence.duplicated(MODEL_ROW_ID_COLUMNS).any():
        raise DataBlocked("model comparison row identity is duplicated")
    for column in (
        "pit_policy",
        "candidate",
        "q",
        "target",
        "window",
        "model",
        "gate_name",
    ):
        if not evidence[column].map(
            lambda value: isinstance(value, str) and value == value.strip()
        ).all():
            raise DataBlocked(f"model comparison {column} is invalid")
    for column in ("is_in_sample", "affects_verdict"):
        if not evidence[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise DataBlocked(f"model comparison {column} must be boolean")
        evidence[column] = evidence[column].astype(bool)
    gate_mask = evidence["gate_name"].ne("")
    if not evidence.loc[gate_mask, "gate_pass"].map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).all():
        raise DataBlocked("model comparison gate values must be boolean")
    if evidence.loc[gate_mask, "is_in_sample"].any():
        raise DataBlocked("model comparison gates cannot be in-sample")
    if evidence.loc[~gate_mask, "gate_pass"].notna().any():
        raise DataBlocked("model comparison metric rows cannot carry gate values")

    public_windows = {
        "beta_h_same_sign": "structure",
        "interaction_axis_corr": "2021-2023",
        "hard_sort_complete": "structure",
    }
    candidate_names = {
        "m1_increment",
        "partial_ic",
        "stability",
        "state_coverage",
    }
    structure: dict[tuple[str, str], bool] = {}
    for policy in policies:
        public = evidence[
            evidence["pit_policy"].eq(policy)
            & evidence["candidate"].eq("PUBLIC")
            & evidence["q"].eq("")
            & evidence["target"].eq("")
            & evidence["model"].eq("")
            & evidence["affects_verdict"]
        ]
        if (
            len(public) != 3
            or set(
                public[["gate_name", "window"]].itertuples(
                    index=False,
                    name=None,
                )
            )
            != set(public_windows.items())
            or public["gate_name"].duplicated().any()
        ):
            raise DataBlocked(
                f"{policy} must contain exactly three PUBLIC structure gates"
            )
        public_pass = bool(public["gate_pass"].map(bool).all())
        for candidate in ("B3_unified", "B3_dual_target"):
            aggregate = evidence[
                evidence["pit_policy"].eq(policy)
                & evidence["candidate"].eq(candidate)
                & evidence["q"].eq("")
                & evidence["target"].eq("")
                & evidence["model"].eq("")
                & evidence["affects_verdict"]
            ]
            if (
                len(aggregate) != 4
                or set(aggregate["gate_name"]) != candidate_names
                or aggregate["gate_name"].duplicated().any()
                or not aggregate["window"].eq("2021-2023").all()
            ):
                raise DataBlocked(
                    f"{policy}/{candidate} must contain exactly four "
                    "aggregate candidate structure gates"
                )
            structure[(policy, candidate)] = bool(
                public_pass and aggregate["gate_pass"].map(bool).all()
            )

    q_specs = {
        "qblend": ("B3_unified", "blend"),
        "q500": ("B3_dual_target", "500"),
        "q1000": ("B3_dual_target", "1000"),
    }
    partials: dict[
        tuple[str, str], tuple[float, dict[int, float], bool, int]
    ] = {}
    for policy in policies:
        for q, (candidate, target) in q_specs.items():
            values: dict[str, float] = {}
            sample_sizes: dict[str, int] = {}
            for window in ("2021-2023", "2021", "2022", "2023"):
                row = evidence[
                    evidence["pit_policy"].eq(policy)
                    & evidence["candidate"].eq(candidate)
                    & evidence["q"].eq(q)
                    & evidence["target"].eq(target)
                    & evidence["window"].eq(window)
                    & evidence["model"].eq("M1")
                    & evidence["gate_name"].eq("")
                ]
                if len(row) != 1:
                    raise DataBlocked(
                        f"model comparison lacks one concrete {policy}/{q}/{window} "
                        "M1 partial-IC metric row"
                    )
                if (
                    not bool(row["affects_verdict"].iloc[0])
                    or bool(row["is_in_sample"].iloc[0])
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} M1 partial-IC "
                        "flags changed"
                    )
                value = row["partial_ic"].iloc[0]
                if (
                    isinstance(value, (bool, np.bool_))
                    or pd.isna(value)
                    or not np.isfinite(float(value))
                    or not -1.0 <= float(value) <= 1.0
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} partial IC "
                        "must be finite"
                    )
                values[window] = float(value)
                raw_n = row["n"].iloc[0]
                try:
                    numeric_n = float(cast(Any, raw_n))
                except (TypeError, ValueError) as exc:
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} n "
                        "must be a positive integer"
                    ) from exc
                if (
                    isinstance(raw_n, (bool, np.bool_))
                    or isinstance(raw_n, (str, bytes))
                    or not np.isfinite(numeric_n)
                    or numeric_n <= 0
                    or numeric_n != np.floor(numeric_n)
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} n "
                        "must be a positive integer"
                    )
                sample_sizes[window] = int(numeric_n)
            yearly = {int(year): values[year] for year in ("2021", "2022", "2023")}
            passed = bool(
                values["2021-2023"] > 0.0
                and sum(value > 0.0 for value in yearly.values()) >= 2
            )
            partials[(policy, q)] = (
                values["2021-2023"],
                yearly,
                passed,
                sample_sizes["2021-2023"],
            )
    return structure, partials


def _monthly_partial_for_window(
    score: pd.Series,
    daily_target: pd.Series,
    control: pd.Series,
    formations: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[float, int]:
    _require_same_grid(daily_target.index, score, "post-IM score")
    _require_same_grid(daily_target.index, control, "post-IM equal_weight control")
    target = next_formation_targets(daily_target, formations)
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    ).reindex(target.index)
    mask = target.index.to_series().ge(start) & period_end.le(end)
    dates = target.index[mask.to_numpy()]
    if len(dates) < 10:
        return float("nan"), int(len(dates))
    score_monthly = score.reindex(dates)
    control_monthly = control.reindex(dates)
    target_monthly = target.reindex(dates)
    if (
        score_monthly.isna().any()
        or control_monthly.isna().any()
        or target_monthly.isna().any()
    ):
        raise DataBlocked("post-IM monthly partial-IC inputs are incomplete")
    return (
        float(partial_rank_ic(score_monthly, target_monthly, control_monthly)),
        int(len(dates)),
    )


def _window_metrics(
    returns: pd.Series,
    positions: tuple[pd.Series, ...],
) -> dict[str, float | int]:
    if returns.empty:
        raise DataBlocked("evaluation window has no common trading dates")
    if any(not position.index.equals(returns.index) for position in positions):
        raise DataBlocked("metric position and return grids differ")
    position_turnover = float(
        sum(turnover(position) for position in positions) / len(positions)
    )
    return {
        "n_obs": int(len(returns)),
        "ann_return": ann_return(returns),
        "sharpe": sharpe(returns),
        "maxdd": max_drawdown(returns),
        "turnover": position_turnover,
    }


def _production_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        column: (
            ""
            if column in {
                "pit_policy",
                "candidate",
                "component",
                "window",
                "gate_name",
            }
            else False
            if column in {"is_candidate", "executable", "affects_verdict"}
            else np.nan
        )
        for column in PRODUCTION_METRICS_COLUMNS
    }
    row.update(updates)
    return row


def _metric_row(
    policy: str,
    candidate: str,
    component: str,
    is_candidate: bool,
    window: str,
    executable: bool,
    metrics: dict[str, float | int],
    baseline: dict[str, float | int],
) -> dict[str, object]:
    baseline_turnover = float(baseline["turnover"])
    candidate_turnover = float(metrics["turnover"])
    turnover_ratio = (
        candidate_turnover / baseline_turnover
        if baseline_turnover > 0.0
        else 1.0
        if candidate_turnover == 0.0
        else float("nan")
    )
    return _production_row(
        pit_policy=policy,
        candidate=candidate,
        component=component,
        is_candidate=is_candidate,
        window=window,
        executable=executable,
        n_obs=int(metrics["n_obs"]),
        ann_return=float(metrics["ann_return"]),
        sharpe=float(metrics["sharpe"]),
        maxdd=float(metrics["maxdd"]),
        turnover=candidate_turnover,
        baseline_sharpe=float(baseline["sharpe"]),
        sharpe_difference=float(metrics["sharpe"]) - float(baseline["sharpe"]),
        baseline_maxdd=float(baseline["maxdd"]),
        maxdd_difference=float(metrics["maxdd"]) - float(baseline["maxdd"]),
        baseline_turnover=baseline_turnover,
        turnover_ratio=turnover_ratio,
    )


def _gate_row(
    metric_row: dict[str, object],
    gate_name: str,
    gate_pass: bool,
) -> dict[str, object]:
    row = dict(metric_row)
    row.update(
        component="aggregate",
        is_candidate=True,
        gate_name=gate_name,
        gate_pass=bool(gate_pass),
        affects_verdict=True,
    )
    return row


def _row_float(row: dict[str, object], key: str) -> float:
    value = row[key]
    if isinstance(value, (bool, np.bool_)):
        raise RuntimeError(f"production row {key} is not numeric")
    try:
        numeric = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"production row {key} is not numeric") from exc
    if not np.isfinite(numeric):
        raise RuntimeError(f"production row {key} is non-finite")
    return numeric


def _row_int(row: dict[str, object], key: str) -> int:
    numeric = _row_float(row, key)
    if numeric < 0.0 or numeric != np.floor(numeric):
        raise RuntimeError(f"production row {key} is not a nonnegative integer")
    return int(numeric)


def _validate_production_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
    *,
    report_present: bool,
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != PRODUCTION_METRICS_COLUMNS:
        raise RuntimeError("production metrics output schema mismatch")
    output = frame.copy()
    if not isinstance(report_present, (bool, np.bool_)):
        raise RuntimeError("production report presence must be boolean")
    actual_report_present = output["window"].eq(
        "2024-2026-report-only"
    ).any()
    if bool(actual_report_present) != bool(report_present):
        raise RuntimeError("production report presence changed")
    if output.duplicated(PRODUCTION_ROW_ID_COLUMNS).any():
        raise RuntimeError("production metrics row identity is not unique")
    for column in ("pit_policy", "candidate", "component", "window", "gate_name"):
        if not output[column].map(
            lambda value: isinstance(value, str) and value == value.strip()
        ).all():
            raise RuntimeError(f"production metrics {column} is invalid")
    for column in ("is_candidate", "executable", "affects_verdict"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"production metrics {column} must be boolean")
        output[column] = output[column].astype(bool)
    gate = output["gate_name"].ne("")
    if not output.loc[gate, "gate_pass"].map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).all() or output.loc[~gate, "gate_pass"].notna().any():
        raise RuntimeError("production gate rows must have bool-only gate values")
    if output.loc[output["affects_verdict"], "gate_name"].eq("").any():
        raise RuntimeError("verdict-affecting production rows must be gates")
    if output.loc[
        output["window"].isin({"pre-IM", "2024-2026-report-only"}),
        "affects_verdict",
    ].any():
        raise RuntimeError("pre-IM and report-only rows cannot affect verdict")
    numeric_columns = [
        "n_obs",
        "ann_return",
        "sharpe",
        "maxdd",
        "turnover",
        "baseline_sharpe",
        "sharpe_difference",
        "baseline_maxdd",
        "maxdd_difference",
        "baseline_turnover",
        "turnover_ratio",
        "partial_ic",
    ]
    for column in numeric_columns:
        invalid_type = output[column].map(
            lambda value: (
                not pd.isna(value)
                and isinstance(value, (bool, np.bool_, str, bytes))
            )
        )
        if invalid_type.any():
            raise RuntimeError(f"production metrics {column} is not numeric")
        numeric = pd.to_numeric(output[column], errors="coerce")
        invalid = output[column].notna() & numeric.isna()
        if invalid.any() or not np.isfinite(numeric.dropna().to_numpy()).all():
            raise RuntimeError(f"production metrics {column} is non-finite")
        output[column] = numeric

    metric_specs = (
        ("equal_weight", "blend"),
        ("B3_unified", "blend"),
        ("B3_dual_target", "blend"),
        ("B3_dual_target", "B3_500"),
        ("B3_dual_target", "B3_1000"),
    )
    windows = ["2021-2023", "pre-IM", "post-IM"]
    if report_present:
        windows.append("2024-2026-report-only")
    full_gate_names = (
        "sharpe_improvement",
        "maxdd_worsening",
        "turnover_multiple",
        "partial_ic",
    )
    post_gate_names = (
        "post_im_min_days",
        "post_im_sharpe_difference",
        "post_im_maxdd_difference",
        "post_im_partial_ic",
    )
    expected_ids: set[tuple[str, str, str, str, str]] = set()
    for policy in policies:
        for window in windows:
            expected_ids.update(
                (policy, candidate, component, window, "")
                for candidate, component in metric_specs
            )
        for candidate in ("B3_unified", "B3_dual_target"):
            expected_ids.update(
                (
                    policy,
                    candidate,
                    "aggregate",
                    "2021-2023",
                    gate_name,
                )
                for gate_name in full_gate_names
            )
        expected_ids.update(
            {
                (
                    policy,
                    "B3_unified",
                    "qblend",
                    "2021-2023",
                    "partial_ic_leg",
                ),
                (
                    policy,
                    "B3_dual_target",
                    "q500",
                    "2021-2023",
                    "partial_ic_leg",
                ),
                (
                    policy,
                    "B3_dual_target",
                    "q1000",
                    "2021-2023",
                    "partial_ic_leg",
                ),
            }
        )
        expected_ids.update(
            (
                policy,
                "B3_dual_target",
                "aggregate",
                "post-IM",
                gate_name,
            )
            for gate_name in post_gate_names
        )
        expected_ids.update(
            (
                policy,
                "B3_dual_target",
                component,
                "post-IM",
                "post_im_partial_ic_leg",
            )
            for component in ("q500", "q1000")
        )
    actual_ids = set(
        output[PRODUCTION_ROW_ID_COLUMNS].itertuples(index=False, name=None)
    )
    if actual_ids != expected_ids:
        raise RuntimeError("production metrics exact row identities changed")

    leg_gate_names = {"partial_ic_leg", "post_im_partial_ic_leg"}
    aggregate_gate_names = set(full_gate_names) | set(post_gate_names)
    for _, row in output.iterrows():
        gate_name = str(row["gate_name"])
        expected_affects = gate_name in aggregate_gate_names
        expected_executable = (
            gate_name == "" and str(row["component"]) == "B3_500"
        ) or str(row["window"]) in {
            "post-IM",
            "2024-2026-report-only",
        }
        expected_is_candidate = expected_affects or (
            gate_name == ""
            and str(row["component"]) == "blend"
            and str(row["candidate"]) in {"B3_unified", "B3_dual_target"}
        )
        if (
            bool(row["affects_verdict"]) != expected_affects
            or bool(row["executable"]) != expected_executable
            or bool(row["is_candidate"]) != expected_is_candidate
        ):
            raise RuntimeError("production row flags changed")

    performance_columns = [
        "ann_return",
        "sharpe",
        "maxdd",
        "turnover",
        "baseline_sharpe",
        "sharpe_difference",
        "baseline_maxdd",
        "maxdd_difference",
        "baseline_turnover",
        "turnover_ratio",
    ]
    leg_rows = output["gate_name"].isin(leg_gate_names)
    if (
        output.loc[leg_rows, performance_columns].notna().any(axis=None)
        or output.loc[~leg_rows, performance_columns].isna().any(axis=None)
        or output.loc[leg_rows, "partial_ic"].isna().any()
        or output.loc[~leg_rows, "partial_ic"].notna().any()
    ):
        raise RuntimeError("production row numeric nullability changed")
    if (
        output["n_obs"].isna().any()
        or (output["n_obs"] <= 0).any()
        or not np.equal(output["n_obs"], np.floor(output["n_obs"])).all()
    ):
        raise RuntimeError("production metrics n_obs must be a positive integer")
    if not output.loc[leg_rows, "partial_ic"].between(-1.0, 1.0).all():
        raise RuntimeError("production partial IC must be in [-1, 1]")
    full_leg_rows = output["gate_name"].eq("partial_ic_leg")
    passing_full_leg = output.loc[full_leg_rows, "gate_pass"].astype(bool)
    if (
        passing_full_leg
        & output.loc[full_leg_rows, "partial_ic"].le(0.0)
    ).any():
        raise RuntimeError(
            "production full-confirmation leg gate semantics changed"
        )
    performance_rows = output.loc[~leg_rows]
    if (
        (performance_rows[["turnover", "baseline_turnover", "turnover_ratio"]] < 0.0)
        .any(axis=None)
        or (performance_rows[["maxdd", "baseline_maxdd"]] > 0.0).any(axis=None)
    ):
        raise RuntimeError("production performance metric domain changed")

    row_lookup: dict[tuple[str, str, str, str, str], pd.Series] = {}
    for _, row in output.iterrows():
        key = tuple(str(row[column]) for column in PRODUCTION_ROW_ID_COLUMNS)
        row_lookup[cast(tuple[str, str, str, str, str], key)] = row

    def same_number(left: object, right: object) -> bool:
        return bool(
            np.isclose(
                float(cast(Any, left)),
                float(cast(Any, right)),
                rtol=0.0,
                atol=1e-12,
            )
        )

    def require_gate(row: pd.Series, expected: bool) -> None:
        if bool(row["gate_pass"]) != bool(expected):
            raise RuntimeError("production aggregate gate semantics changed")

    for policy in policies:
        for window in windows:
            baseline = row_lookup[
                (policy, "equal_weight", "blend", window, "")
            ]
            baseline_n = int(baseline["n_obs"])
            baseline_sharpe = float(baseline["sharpe"])
            baseline_maxdd = float(baseline["maxdd"])
            baseline_turnover = float(baseline["turnover"])
            for candidate, component in metric_specs:
                metric = row_lookup[
                    (policy, candidate, component, window, "")
                ]
                turnover_value = float(metric["turnover"])
                if int(metric["n_obs"]) != baseline_n:
                    raise RuntimeError("production window observation counts changed")
                if not (
                    same_number(metric["baseline_sharpe"], baseline_sharpe)
                    and same_number(metric["baseline_maxdd"], baseline_maxdd)
                    and same_number(
                        metric["baseline_turnover"],
                        baseline_turnover,
                    )
                    and same_number(
                        metric["sharpe_difference"],
                        float(metric["sharpe"]) - baseline_sharpe,
                    )
                    and same_number(
                        metric["maxdd_difference"],
                        float(metric["maxdd"]) - baseline_maxdd,
                    )
                ):
                    raise RuntimeError("production baseline differences changed")
                if baseline_turnover > 0.0:
                    expected_ratio = turnover_value / baseline_turnover
                elif turnover_value == 0.0:
                    expected_ratio = 1.0
                else:
                    raise RuntimeError("production turnover ratio is undefined")
                if not same_number(metric["turnover_ratio"], expected_ratio):
                    raise RuntimeError("production turnover ratio changed")

        for candidate in ("B3_unified", "B3_dual_target"):
            metric = row_lookup[
                (policy, candidate, "blend", "2021-2023", "")
            ]
            for gate_name in full_gate_names:
                gate_row = row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        gate_name,
                    )
                ]
                if int(gate_row["n_obs"]) != int(metric["n_obs"]) or any(
                    not same_number(gate_row[column], metric[column])
                    for column in performance_columns
                ):
                    raise RuntimeError("production gate metric copy changed")
            leg_components = (
                ("qblend",)
                if candidate == "B3_unified"
                else ("q500", "q1000")
            )
            leg_pass = all(
                bool(
                    row_lookup[
                        (
                            policy,
                            candidate,
                            component,
                            "2021-2023",
                            "partial_ic_leg",
                        )
                    ]["gate_pass"]
                )
                for component in leg_components
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "sharpe_improvement",
                    )
                ],
                float(metric["sharpe_difference"]) >= 0.10,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "maxdd_worsening",
                    )
                ],
                float(metric["maxdd_difference"]) >= -0.02,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "turnover_multiple",
                    )
                ],
                float(metric["turnover_ratio"]) <= 1.50,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "partial_ic",
                    )
                ],
                leg_pass,
            )

        post_metric = row_lookup[
            (policy, "B3_dual_target", "blend", "post-IM", "")
        ]
        for gate_name in post_gate_names:
            gate_row = row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    gate_name,
                )
            ]
            if int(gate_row["n_obs"]) != int(post_metric["n_obs"]) or any(
                not same_number(gate_row[column], post_metric[column])
                for column in performance_columns
            ):
                raise RuntimeError("production post-IM gate metric copy changed")
        post_leg_passes = []
        for component in ("q500", "q1000"):
            leg = row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    component,
                    "post-IM",
                    "post_im_partial_ic_leg",
                )
            ]
            expected_leg_pass = float(leg["partial_ic"]) >= 0.0
            if bool(leg["gate_pass"]) != expected_leg_pass:
                raise RuntimeError("production post-IM leg gate semantics changed")
            post_leg_passes.append(expected_leg_pass)
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_min_days",
                )
            ],
            int(post_metric["n_obs"]) >= 252,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_sharpe_difference",
                )
            ],
            float(post_metric["sharpe_difference"]) > 0.0,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_maxdd_difference",
                )
            ],
            float(post_metric["maxdd_difference"]) >= -0.02,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_partial_ic",
                )
            ],
            all(post_leg_passes),
        )
    return output.sort_values(
        PRODUCTION_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validate_bootstrap_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != BOOTSTRAP_COLUMNS:
        raise RuntimeError("bootstrap output schema mismatch")
    output = frame.copy()
    if output.duplicated(BOOTSTRAP_ROW_ID_COLUMNS).any():
        raise RuntimeError("bootstrap row identity is not unique")
    expected = {
        (policy, candidate)
        for policy in policies
        for candidate in ("B3_unified", "B3_dual_target")
    }
    if set(
        output[BOOTSTRAP_ROW_ID_COLUMNS].itertuples(index=False, name=None)
    ) != expected:
        raise RuntimeError("bootstrap fixed candidate family changed")
    for column in ("structure_pass", "gate_pass"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"bootstrap {column} must be boolean")
        output[column] = output[column].astype(bool)
    for column in ("draws", "block_days", "seed"):
        numeric = pd.to_numeric(output[column], errors="coerce")
        if numeric.isna().any() or not np.equal(numeric, np.floor(numeric)).all():
            raise RuntimeError(f"bootstrap {column} must be integral")
        output[column] = numeric.astype(int)
    frozen_sampling = {
        "draws": 5000,
        "block_days": 20,
        "seed": 20260713,
    }
    for column, expected_value in frozen_sampling.items():
        if not output[column].eq(expected_value).all():
            raise RuntimeError(f"bootstrap {column} changed from frozen contract")
    for column in ("tail_prob", "holm_adjusted_tail"):
        numeric = pd.to_numeric(output[column], errors="coerce")
        if (
            numeric.isna().any()
            or not np.isfinite(numeric.to_numpy()).all()
            or not numeric.between(0.0, 1.0).all()
        ):
            raise RuntimeError(f"bootstrap {column} is invalid")
        output[column] = numeric.astype(float)
    empirical_rank = output["tail_prob"] * (output["draws"] + 1)
    nearest_rank = np.rint(empirical_rank)
    on_empirical_grid = np.isclose(
        empirical_rank,
        nearest_rank,
        rtol=0.0,
        atol=1e-10,
    )
    rank_in_range = (nearest_rank >= 1) & (
        nearest_rank <= output["draws"] + 1
    )
    if not (on_empirical_grid & rank_in_range).all():
        raise RuntimeError(
            "bootstrap tail_prob must lie on empirical grid"
        )
    ci_columns = ["ci05", "ci50", "ci95"]
    for index, row in output.iterrows():
        ci = pd.to_numeric(row[ci_columns], errors="coerce")
        if bool(row["structure_pass"]):
            if ci.isna().any() or not np.isfinite(ci.to_numpy()).all():
                raise RuntimeError("structure-passing bootstrap CI is invalid")
            if not float(ci["ci05"]) <= float(ci["ci50"]) <= float(ci["ci95"]):
                raise RuntimeError("structure-passing bootstrap CI is unordered")
        elif ci.notna().any() or float(row["tail_prob"]) != 1.0:
            raise RuntimeError("structure-failed bootstrap must have tail 1 and blank CI")
    for policy in policies:
        family = output.loc[output["pit_policy"].eq(policy)]
        adjusted = holm_style_adjust(
            dict(
                zip(
                    family["candidate"],
                    family["tail_prob"].astype(float),
                    strict=True,
                )
            )
        )
        for _, row in family.iterrows():
            expected_adjusted = adjusted[str(row["candidate"])]
            if float(row["holm_adjusted_tail"]) != expected_adjusted:
                raise RuntimeError("bootstrap Holm adjustment changed")
            expected_gate = bool(row["structure_pass"]) and passes_tail_gate(
                expected_adjusted,
                0.10,
            )
            if bool(row["gate_pass"]) != expected_gate:
                raise RuntimeError("bootstrap gate semantics changed")
    return output.sort_values(
        BOOTSTRAP_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validate_yearly_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
    *,
    report_present: bool,
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != YEARLY_COLUMNS:
        raise RuntimeError("yearly output schema mismatch")
    output = frame.copy()
    if not isinstance(report_present, (bool, np.bool_)):
        raise RuntimeError("yearly report presence must be boolean")
    actual_report_present = output["window"].eq(
        "2024-2026-report-only"
    ).any()
    if bool(actual_report_present) != bool(report_present):
        raise RuntimeError("yearly report presence changed")
    if output.duplicated(YEARLY_ROW_ID_COLUMNS).any():
        raise RuntimeError("yearly row identity is not unique")
    for column in ("is_in_sample", "affects_verdict"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"yearly {column} must be strictly boolean")
        output[column] = output[column].astype(bool)
    if output["affects_verdict"].any():
        raise RuntimeError("yearly output cannot affect verdict")
    for column in ("pit_policy", "candidate", "window", "row_type"):
        if not output[column].map(
            lambda value: (
                isinstance(value, str)
                and bool(value)
                and value == value.strip()
            )
        ).all():
            raise RuntimeError(f"yearly {column} is invalid")
    if set(output["pit_policy"]) != set(policies):
        raise RuntimeError("yearly PIT policy set changed")
    if set(output["candidate"]) != {"B3_unified", "B3_dual_target"}:
        raise RuntimeError("yearly candidate family changed")
    mandatory_windows = {"2021-2023", "2014-2023"}
    allowed_windows = mandatory_windows | {"2024-2026-report-only"}
    actual_windows = set(output["window"])
    if not mandatory_windows.issubset(actual_windows) or not actual_windows.issubset(
        allowed_windows
    ):
        raise RuntimeError("yearly diagnostic windows changed")
    if set(output["row_type"]) != {"year", "excluding_strongest"}:
        raise RuntimeError("yearly row type is invalid")
    for column in (
        "n_obs",
        "year",
        "excluded_year",
        "signed_log_pnl",
        "absolute_pnl_share",
        "strongest_year",
        "ann_return",
        "sharpe",
        "maxdd",
    ):
        numeric = pd.to_numeric(output[column], errors="coerce")
        invalid = output[column].notna() & numeric.isna()
        if invalid.any() or not np.isfinite(numeric.dropna().to_numpy()).all():
            raise RuntimeError(f"yearly {column} row semantics are invalid")
        output[column] = numeric

    expected_years = {
        "2021-2023": {2021, 2022, 2023},
        "2014-2023": set(range(2014, 2024)),
    }
    if "2024-2026-report-only" in actual_windows:
        report_years = set(
            output.loc[
                output["window"].eq("2024-2026-report-only")
                & output["row_type"].eq("year"),
                "year",
            ].astype(int)
        )
        ordered_report_years = sorted(report_years)
        if (
            len(report_years) < 1
            or not report_years.issubset({2024, 2025, 2026})
            or ordered_report_years[0] != 2024
            or ordered_report_years
            != list(
                range(
                    ordered_report_years[0],
                    ordered_report_years[-1] + 1,
                )
            )
        ):
            raise RuntimeError(
                "yearly row semantics have invalid report year coverage"
            )
        expected_years["2024-2026-report-only"] = report_years
    for policy in policies:
        for candidate in ("B3_unified", "B3_dual_target"):
            for window in actual_windows:
                group = output[
                    output["pit_policy"].eq(policy)
                    & output["candidate"].eq(candidate)
                    & output["window"].eq(window)
                ]
                if group.empty:
                    raise RuntimeError("yearly row semantics require every group")
                year_rows = group[group["row_type"].eq("year")]
                summary = group[group["row_type"].eq("excluding_strongest")]
                actual_years = set(year_rows["year"].astype(int))
                required_years = expected_years[window]
                if (
                    actual_years != required_years
                    or len(summary) != 1
                    or len(group) != len(required_years) + 1
                ):
                    raise RuntimeError("yearly row semantics have wrong year coverage")
                expected_in_sample = window == "2014-2023"
                if not group["is_in_sample"].eq(expected_in_sample).all():
                    raise RuntimeError("yearly row semantics have wrong sample label")
                if (
                    year_rows["excluded_year"].notna().any()
                    or summary["year"].notna().any()
                    or year_rows["signed_log_pnl"].isna().any()
                    or summary["signed_log_pnl"].notna().any()
                    or summary["absolute_pnl_share"].notna().any()
                ):
                    raise RuntimeError("yearly row semantics are inconsistent")
                integral_columns = ["year", "strongest_year", "n_obs"]
                for column in integral_columns:
                    values = group[column].dropna()
                    if (
                        (values < 0).any()
                        or not np.equal(values, np.floor(values)).all()
                    ):
                        raise RuntimeError("yearly row semantics require integers")
                single_year_report = (
                    window == "2024-2026-report-only"
                    and len(required_years) == 1
                )
                summary_n = int(summary["n_obs"].iloc[0])
                if (year_rows["n_obs"] <= 0).any() or (
                    not single_year_report and summary_n <= 0
                ) or (single_year_report and summary_n != 0):
                    raise RuntimeError("yearly row semantics require observations")
                strongest_values = set(group["strongest_year"].astype(int))
                if len(strongest_values) != 1:
                    raise RuntimeError("yearly row semantics disagree on strongest year")
                strongest_year = strongest_values.pop()
                if strongest_year not in required_years:
                    raise RuntimeError("yearly row semantics have invalid strongest year")
                signed_by_year = (
                    year_rows.set_index("year")["signed_log_pnl"].sort_index()
                )
                expected_strongest = int(signed_by_year.abs().idxmax())
                if strongest_year != expected_strongest:
                    raise RuntimeError(
                        "yearly row semantics have wrong strongest year"
                    )
                excluded = summary["excluded_year"].iloc[0]
                if (
                    pd.isna(excluded)
                    or float(excluded) != strongest_year
                    or float(excluded) != np.floor(float(excluded))
                ):
                    raise RuntimeError("yearly row semantics have wrong excluded year")
                strongest_n = int(
                    year_rows.loc[
                        year_rows["year"].eq(strongest_year), "n_obs"
                    ].iloc[0]
                )
                if int(summary["n_obs"].iloc[0]) != int(
                    year_rows["n_obs"].sum()
                ) - strongest_n:
                    raise RuntimeError("yearly row semantics have wrong summary n")
                year_metrics = year_rows[["ann_return", "sharpe", "maxdd"]]
                if year_metrics.isna().any(axis=None):
                    raise RuntimeError(
                        "yearly row semantics require year metrics"
                    )
                if (group["maxdd"].dropna() > 0.0).any():
                    raise RuntimeError(
                        "yearly row semantics require nonpositive maxdd"
                    )
                summary_metrics = summary[["ann_return", "sharpe", "maxdd"]]
                if single_year_report:
                    if summary_metrics.notna().any(axis=None):
                        raise RuntimeError(
                            "yearly row semantics require an empty report summary"
                        )
                elif summary_metrics.isna().any(axis=None):
                    raise RuntimeError("yearly row semantics require summary metrics")
                shares_by_year = (
                    year_rows.set_index("year")["absolute_pnl_share"].sort_index()
                )
                denominator = float(signed_by_year.abs().sum())
                if denominator > 0.0:
                    expected_shares = signed_by_year.abs() / denominator
                    if shares_by_year.isna().any() or not np.allclose(
                        shares_by_year.to_numpy(dtype=float),
                        expected_shares.to_numpy(dtype=float),
                        rtol=0.0,
                        atol=1e-12,
                    ):
                        raise RuntimeError("yearly row semantics have invalid shares")
                elif shares_by_year.notna().any():
                    raise RuntimeError("yearly row semantics have invalid zero shares")
    return output.sort_values(
        YEARLY_ROW_ID_COLUMNS,
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)


def build_evaluation(
    state_components: pd.DataFrame,
    model_comparison: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    carry_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> EvaluationFrames:
    settings = _evaluation_config(cfg)
    (
        states,
        targets,
        control,
        formations,
        policies,
        _,
    ) = _validate_score_inputs(
        state_components,
        target_returns,
        equal_weight_signal,
        formation_dates,
        cfg,
    )
    structure, full_partials = _validated_model_evidence(
        model_comparison,
        policies,
    )
    if not isinstance(carry_returns, dict) or set(carry_returns) != {"500", "1000"}:
        raise DataBlocked("carry returns must contain exactly 500 and 1000")
    cash_calendar = targets["500"].index
    carry_500_full = materialize_carry(
        carry_returns["500"],
        cash_calendar,
        settings["ic_launch"],
    )
    carry_1000_full = materialize_carry(
        carry_returns["1000"],
        cash_calendar,
        settings["im_launch"],
    )
    common_end = min(carry_500_full.index.max(), carry_1000_full.index.max())
    calendar = cash_calendar[cash_calendar <= common_end]
    confirmation_start, confirmation_end = settings["confirmation"]
    expected_confirmation = cash_calendar[
        (cash_calendar >= confirmation_start)
        & (cash_calendar <= confirmation_end)
    ]
    actual_confirmation = calendar[
        (calendar >= confirmation_start) & (calendar <= confirmation_end)
    ]
    if (
        not len(expected_confirmation)
        or not actual_confirmation.equals(expected_confirmation)
    ):
        raise DataBlocked(
            "common carry tail does not cover the full cash confirmation calendar"
        )
    carry_500 = carry_500_full.reindex(calendar)
    carry_1000 = carry_1000_full.reindex(calendar)
    if carry_500.isna().any() or carry_1000.isna().any():
        raise DataBlocked("common carry calendar contains an internal gap")
    cash_500 = targets["500"].loc[calendar]
    cash_1000 = targets["1000"].loc[calendar]
    control = control.loc[calendar]
    full_scores = fit_frozen_m1_scores(
        states,
        targets,
        equal_weight_signal,
        formations,
        cfg,
    )
    scores = {key: value.loc[calendar] for key, value in full_scores.items()}
    baseline_position = production_position(control).astype(int)
    cost_bps = settings["cost_bps"]

    im_launch = settings["im_launch"]
    window_specs: list[tuple[str, pd.DatetimeIndex, bool]] = []
    confirmation_index = actual_confirmation
    pre_im_index = confirmation_index[confirmation_index < im_launch]
    post_im_index = confirmation_index[confirmation_index >= im_launch]
    if not len(pre_im_index) or not len(post_im_index):
        raise DataBlocked("confirmation window must straddle the IM launch boundary")
    window_specs.extend(
        [
            ("2021-2023", confirmation_index, False),
            ("pre-IM", pre_im_index, False),
            ("post-IM", post_im_index, True),
        ]
    )
    report_start, report_end = settings["report"]
    report_index = calendar[(calendar >= report_start) & (calendar <= report_end)]
    if len(report_index):
        window_specs.append(("2024-2026-report-only", report_index, True))

    production_rows: list[dict[str, object]] = []
    bootstrap_rows: list[dict[str, object]] = []
    yearly_frames: list[pd.DataFrame] = []
    for policy in policies:
        unified_position = production_position(scores[(policy, "qblend")]).astype(int)
        position_500 = production_position(scores[(policy, "q500")]).astype(int)
        position_1000 = production_position(scores[(policy, "q1000")]).astype(int)
        candidate_positions = {
            "B3_unified": (unified_position, unified_position),
            "B3_dual_target": (position_500, position_1000),
        }
        window_candidate_returns: dict[tuple[str, str], pd.Series] = {}
        window_baseline_returns: dict[str, pd.Series] = {}
        metric_rows: dict[tuple[str, str], dict[str, object]] = {}
        for window, index, executable in window_specs:
            baseline_window_position = baseline_position.loc[index]
            baseline_return = two_leg_candidate_returns(
                baseline_window_position,
                baseline_window_position,
                cash_500.loc[index],
                cash_1000.loc[index],
                carry_500.loc[index],
                carry_1000.loc[index],
                cost_bps,
            )
            window_baseline_returns[window] = baseline_return
            baseline_metrics = _window_metrics(
                baseline_return,
                (baseline_window_position, baseline_window_position),
            )
            baseline_row = _metric_row(
                policy,
                "equal_weight",
                "blend",
                False,
                window,
                executable,
                baseline_metrics,
                baseline_metrics,
            )
            production_rows.append(baseline_row)
            for candidate, positions in candidate_positions.items():
                window_positions = tuple(
                    position.loc[index] for position in positions
                )
                returns = two_leg_candidate_returns(
                    window_positions[0],
                    window_positions[1],
                    cash_500.loc[index],
                    cash_1000.loc[index],
                    carry_500.loc[index],
                    carry_1000.loc[index],
                    cost_bps,
                )
                window_candidate_returns[(candidate, window)] = returns
                metrics = _window_metrics(
                    returns,
                    window_positions,
                )
                row = _metric_row(
                    policy,
                    candidate,
                    "blend",
                    True,
                    window,
                    executable,
                    metrics,
                    baseline_metrics,
                )
                production_rows.append(row)
                metric_rows[(candidate, window)] = row
            for component, leg_position, leg_cash, leg_carry, component_executable in [
                (
                    "B3_500",
                    position_500,
                    cash_500,
                    carry_500,
                    bool(index.min() >= settings["ic_launch"]),
                ),
                (
                    "B3_1000",
                    position_1000,
                    cash_1000,
                    carry_1000,
                    bool(index.min() >= im_launch),
                ),
            ]:
                component_position = leg_position.loc[index]
                leg_return = run_strategy(
                    component_position,
                    leg_cash.loc[index],
                    cost_bps,
                    leg_carry.loc[index],
                )["ret"]
                component_metrics = _window_metrics(
                    leg_return,
                    (component_position,),
                )
                production_rows.append(
                    _metric_row(
                        policy,
                        "B3_dual_target",
                        component,
                        False,
                        window,
                        component_executable,
                        component_metrics,
                        baseline_metrics,
                    )
                )

        for candidate in ("B3_unified", "B3_dual_target"):
            metric = metric_rows[(candidate, "2021-2023")]
            production_rows.extend(
                [
                    _gate_row(
                        metric,
                        "sharpe_improvement",
                        _row_float(metric, "sharpe_difference")
                        >= settings["sharpe_improvement"],
                    ),
                    _gate_row(
                        metric,
                        "maxdd_worsening",
                        _row_float(metric, "maxdd_difference")
                        >= -settings["maxdd_worsening"],
                    ),
                    _gate_row(
                        metric,
                        "turnover_multiple",
                        _row_float(metric, "turnover_ratio")
                        <= settings["turnover_multiple"],
                    ),
                ]
            )
            q_legs = (
                ("qblend",)
                if candidate == "B3_unified"
                else ("q500", "q1000")
            )
            leg_passes = []
            for q in q_legs:
                full_partial, _, leg_pass, full_n = full_partials[(policy, q)]
                leg_passes.append(leg_pass)
                production_rows.append(
                    _production_row(
                        pit_policy=policy,
                        candidate=candidate,
                        component=q,
                        is_candidate=False,
                        window="2021-2023",
                        executable=False,
                        n_obs=full_n,
                        partial_ic=full_partial,
                        gate_name="partial_ic_leg",
                        gate_pass=bool(leg_pass),
                        affects_verdict=False,
                    )
                )
            partial_gate = _gate_row(
                metric,
                "partial_ic",
                all(leg_passes),
            )
            partial_gate["partial_ic"] = np.nan
            production_rows.append(partial_gate)

        post_metric = metric_rows[("B3_dual_target", "post-IM")]
        post_partial: dict[str, float] = {}
        for q, target_name in (("q500", "500"), ("q1000", "1000")):
            value, partial_n = _monthly_partial_for_window(
                full_scores[(policy, q)],
                targets[target_name],
                equal_weight_signal,
                formations,
                im_launch,
                confirmation_end,
            )
            post_partial[q] = value
            leg_pass = bool(np.isfinite(value) and value >= 0.0)
            production_rows.append(
                _production_row(
                    pit_policy=policy,
                    candidate="B3_dual_target",
                    component=q,
                    is_candidate=False,
                    window="post-IM",
                    executable=True,
                    n_obs=partial_n,
                    partial_ic=value,
                    gate_name="post_im_partial_ic_leg",
                    gate_pass=leg_pass,
                    affects_verdict=False,
                )
            )
        production_rows.extend(
            [
                _gate_row(
                    post_metric,
                    "post_im_min_days",
                    _row_int(post_metric, "n_obs")
                    >= settings["post_im_min_days"],
                ),
                _gate_row(
                    post_metric,
                    "post_im_sharpe_difference",
                    _row_float(post_metric, "sharpe_difference") > 0.0,
                ),
                _gate_row(
                    post_metric,
                    "post_im_maxdd_difference",
                    _row_float(post_metric, "maxdd_difference")
                    >= -settings["maxdd_worsening"],
                ),
            ]
        )
        post_partial_gate = _gate_row(
            post_metric,
            "post_im_partial_ic",
            all(np.isfinite(value) and value >= 0.0 for value in post_partial.values()),
        )
        post_partial_gate["partial_ic"] = np.nan
        production_rows.append(post_partial_gate)

        raw_evidence: dict[str, dict[str, float]] = {}
        raw_tails: dict[str, float] = {}
        for candidate in ("B3_unified", "B3_dual_target"):
            if structure[(policy, candidate)]:
                result = paired_moving_block_tail(
                    window_candidate_returns[(candidate, "2021-2023")],
                    window_baseline_returns["2021-2023"],
                    settings["block_days"],
                    settings["draws"],
                    settings["seed"],
                )
                raw_evidence[candidate] = result
                raw_tails[candidate] = result["tail_prob"]
            else:
                raw_evidence[candidate] = {
                    "tail_prob": 1.0,
                    "ci05": float("nan"),
                    "ci50": float("nan"),
                    "ci95": float("nan"),
                }
                raw_tails[candidate] = 1.0
        adjusted = holm_style_adjust(raw_tails)
        for candidate in ("B3_unified", "B3_dual_target"):
            evidence = raw_evidence[candidate]
            bootstrap_rows.append(
                {
                    "pit_policy": policy,
                    "candidate": candidate,
                    "draws": settings["draws"],
                    "block_days": settings["block_days"],
                    "seed": settings["seed"],
                    "tail_prob": float(evidence["tail_prob"]),
                    "holm_adjusted_tail": float(adjusted[candidate]),
                    "ci05": float(evidence["ci05"]),
                    "ci50": float(evidence["ci50"]),
                    "ci95": float(evidence["ci95"]),
                    "structure_pass": bool(structure[(policy, candidate)]),
                    "gate_pass": bool(
                        structure[(policy, candidate)]
                        and passes_tail_gate(
                            adjusted[candidate],
                            settings["tail_threshold"],
                        )
                    ),
                }
            )

        history_end = min(pd.Timestamp("2023-12-31"), calendar.max())
        full_history = calendar[
            (calendar >= pd.Timestamp("2014-01-01"))
            & (calendar <= history_end)
        ]
        if (
            not len(full_history)
            or full_history.min().to_period("M") != pd.Period("2014-01", freq="M")
            or full_history.max().to_period("M") != pd.Period("2023-12", freq="M")
        ):
            raise DataBlocked("2014-2023 yearly diagnostic window is incomplete")
        for candidate, positions in candidate_positions.items():
            history_positions = tuple(
                position.loc[full_history] for position in positions
            )
            history_returns = two_leg_candidate_returns(
                history_positions[0],
                history_positions[1],
                cash_500.loc[full_history],
                cash_1000.loc[full_history],
                carry_500.loc[full_history],
                carry_1000.loc[full_history],
                cost_bps,
            )
            yearly_frames.append(
                yearly_contributions(
                    policy,
                    candidate,
                    window_candidate_returns[(candidate, "2021-2023")],
                    "2021-2023",
                    False,
                )
            )
            yearly_frames.append(
                yearly_contributions(
                    policy,
                    candidate,
                    history_returns,
                    "2014-2023",
                    True,
                )
            )
            if len(np.unique(report_index.year)) >= 1:
                yearly_frames.append(
                    yearly_contributions(
                        policy,
                        candidate,
                        window_candidate_returns[
                            (candidate, "2024-2026-report-only")
                        ],
                        "2024-2026-report-only",
                        False,
                    )
                )

    production = _validate_production_output(
        pd.DataFrame(production_rows, columns=PRODUCTION_METRICS_COLUMNS),
        policies,
        report_present=bool(len(report_index)),
    )
    bootstrap = _validate_bootstrap_output(
        pd.DataFrame(bootstrap_rows, columns=BOOTSTRAP_COLUMNS),
        policies,
    )
    yearly = _validate_yearly_output(
        pd.concat(yearly_frames, ignore_index=True)[YEARLY_COLUMNS],
        policies,
        report_present=bool(len(report_index)),
    )
    return EvaluationFrames(
        production_metrics=production,
        bootstrap=bootstrap,
        yearly=yearly,
    )


@dataclass(frozen=True)
class EvaluationRunResult:
    """What an evaluation run concluded and where it sealed the evidence."""

    final_verdict: str
    blocked: bool
    verdicts_path: Path
    manifest_path: Path


# Products the eval stage owns. structure_coefficients.csv and
# model_comparison.csv belong to the structure stage and are never rewritten or
# invalidated here.
_EVAL_BLOCKED_PRODUCTS = ("verdicts.csv", "run_manifest.json")
_EVAL_FULL_ONLY_PRODUCTS = (
    "production_metrics.csv",
    "yearly_contribution.csv",
    "bootstrap.csv",
)


def _invalidate_evaluation_outputs(backtest_root: Path) -> None:
    """Drop eval-owned products so a rerun never leaves a stale claim behind."""

    for name in _EVAL_BLOCKED_PRODUCTS + _EVAL_FULL_ONLY_PRODUCTS:
        (backtest_root / name).unlink(missing_ok=True)
        (backtest_root / f".{name}.tmp").unlink(missing_ok=True)


def _write_evaluation_csv(path: Path, frame: pd.DataFrame) -> Path:
    """Write a compact product atomically, leaving no temporary behind."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        frame.to_csv(temp_path, index=False)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _preflight_run_blockers(
    preflight: PreflightManifestContract,
) -> list[dict[str, object]]:
    """Collect the run-level blockers derivable before any data is loaded.

    Preflight blockers use a per-formation schema and repeat one reason code
    across every affected month, so they are de-duplicated by reason code into
    the run-blocker schema. A same-reason ``DATA_BLOCKED`` sibling dominates a
    ``COVERAGE_BLOCKED`` one. The evidence-free preflight blocker is appended
    last because it is derived from the verified contract, not the blocker list.
    """

    translated: dict[str, dict[str, object]] = {}
    for blocker in preflight.blockers:
        reason = str(blocker["reason_code"])
        status = str(blocker["status"])
        existing = translated.get(reason)
        if existing is None:
            translated[reason] = {
                "reason_code": reason,
                "status": status,
                "detail": str(blocker["detail"]).strip() or reason,
                "affects_statistical": False,
            }
        elif status == "DATA_BLOCKED":
            existing["status"] = status
    run_blockers = list(translated.values())
    evidence_blocker = database_source_evidence_blocker(preflight)
    if evidence_blocker is not None:
        run_blockers.append(evidence_blocker)
    return run_blockers


def _coerce_model_comparison(frame: pd.DataFrame) -> pd.DataFrame:
    """Restore the dtypes a CSV round-trip loses so structure output re-validates.

    ``verify_structure_provenance`` reads ``model_comparison.csv`` with a plain
    ``read_csv``: blank canonical strings arrive as NaN and booleans as text.
    The frozen validators are strict, so the eval stage rebuilds the exact
    in-memory contract before handing the frame to ``build_evaluation``.
    """

    if (
        not isinstance(frame, pd.DataFrame)
        or list(frame.columns) != MODEL_COMPARISON_COLUMNS
    ):
        raise DataBlocked("structure model comparison schema mismatch")
    output = frame.copy()
    for column in (
        "pit_policy",
        "candidate",
        "q",
        "target",
        "window",
        "model",
        "gate_name",
    ):
        output[column] = [
            ""
            if value is None
            or (not isinstance(value, str) and pd.isna(value))
            else str(value)
            for value in output[column]
        ]

    def _restore_bool(value: object) -> bool:
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if value == "True":
            return True
        if value == "False":
            return False
        raise DataBlocked("structure model comparison boolean is invalid")

    for column in ("is_in_sample", "affects_verdict"):
        output[column] = [_restore_bool(value) for value in output[column]]
    gate_rows = output["gate_name"].ne("")
    output["gate_pass"] = [
        _restore_bool(value) if is_gate else np.nan
        for is_gate, value in zip(gate_rows, output["gate_pass"])
    ]
    return output


def _evaluation_formation_dates(exposures: pd.DataFrame) -> pd.DatetimeIndex:
    """Derive the monthly formation calendar the eval stage scores against."""

    if (
        not isinstance(exposures, pd.DataFrame)
        or "formation_date" not in exposures.columns
    ):
        raise DataBlocked("monthly exposures lack a formation_date column")
    return pd.DatetimeIndex(
        sorted(pd.to_datetime(exposures["formation_date"]).unique())
    )


def run_evaluation(
    cfg: dict,
    requested_data_end: object,
    research_output_dir: str | Path = DEFAULT_RESEARCH_OUTPUT_DIR,
    backtest_output_dir: str | Path = DEFAULT_BACKTEST_OUTPUT_DIR,
    *,
    underlying_return_loader: Callable[[str], pd.Series] | None = None,
    carry_loader: Callable[[str], pd.Series] | None = None,
    equal_weight_signal: pd.Series | None = None,
    equal_weight_path: str | Path | None = None,
) -> EvaluationRunResult:
    """Verify staged provenance and emit fail-closed B3 verdicts.

    Preflight is verified first. If any run-level blocker is derivable before
    reading data, only the two blocked products are written and the run never
    requests states, structure files or returns.

    A ``requested_data_end`` of ``None`` (bare CLI runs) adopts the verified
    preflight ``data_end`` as the run boundary; an explicit value must match
    the preflight boundary exactly.
    """

    research_root = Path(research_output_dir)
    backtest_root = Path(backtest_output_dir)
    _evaluation_config(cfg)
    _invalidate_evaluation_outputs(backtest_root)
    preflight = verify_preflight_manifest(
        research_root,
        config_hash(cfg),
        requested_data_end,
    )
    resolved_data_end = (
        preflight.data_end
        if requested_data_end is None
        else _requested_date_string(requested_data_end, "requested_data_end")
    )
    run_blockers = _preflight_run_blockers(preflight)
    if run_blockers:
        verdicts = blocked_verdict_rows(run_blockers)
        manifest = build_run_manifest(
            cfg,
            preflight,
            verdicts,
            resolved_data_end,
        )
        verdicts_path = _write_evaluation_csv(
            backtest_root / "verdicts.csv",
            verdicts,
        )
        manifest_path = write_run_manifest(
            backtest_root / "run_manifest.json",
            manifest,
        )
        return EvaluationRunResult(
            final_verdict=str(manifest["final_verdict"]),
            blocked=True,
            verdicts_path=verdicts_path,
            manifest_path=manifest_path,
        )
    # Unblocked before loading data: verify staged provenance, then evaluate.
    cutoff = pd.Timestamp(preflight.data_end)
    require_parent_manifest(research_root, "exposures", cfg, cutoff)
    require_parent_manifest(research_root, "states", cfg, cutoff)
    structure = verify_structure_provenance(
        backtest_root,
        config_hash(cfg),
        resolved_data_end,
    )
    model_comparison = _coerce_model_comparison(structure.model_comparison)

    exposures = pd.read_csv(research_root / "monthly_exposures.csv.gz")
    state_components = pd.read_csv(research_root / "state_components.csv")
    formation_dates = _evaluation_formation_dates(exposures)

    if underlying_return_loader is None or carry_loader is None:
        from backtest.data import load_carry, load_underlying_returns

        underlying_return_loader = underlying_return_loader or load_underlying_returns
        carry_loader = carry_loader or load_carry
    target_returns = {
        target: _validated_runner_series(
            underlying_return_loader(target),
            f"evaluation target returns.{target}",
            cutoff,
        )
        for target in ("blend", "500", "1000")
    }
    # Carry freshness must be computed from the raw untruncated series
    # (plan Task 10 Step 5); the evaluation itself consumes carry on the
    # truncated cash calendar, which materialize_carry enforces strictly.
    raw_carry = {leg: carry_loader(leg) for leg in ("500", "1000")}
    evaluation_carry = {
        leg: (
            series.loc[series.index <= cutoff]
            if isinstance(series, pd.Series)
            and isinstance(series.index, pd.DatetimeIndex)
            else series
        )
        for leg, series in raw_carry.items()
    }
    if equal_weight_signal is None:
        equal_weight_signal = _load_equal_weight_control(
            equal_weight_path
            if equal_weight_path is not None
            else DEFAULT_EQUAL_WEIGHT_SIGNAL_PATH,
            cutoff,
        )
    else:
        equal_weight_signal = _validated_runner_series(
            equal_weight_signal,
            "equal_weight control",
            cutoff,
        )

    frames = build_evaluation(
        state_components,
        model_comparison,
        target_returns,
        evaluation_carry,
        equal_weight_signal,
        formation_dates,
        cfg,
    )

    # Data-dependent run blockers become derivable only after the series load;
    # they override the final verdict without erasing the statistical evidence.
    expected_latest_cash = str(target_returns["500"].index.max().date())
    carry_freshness = compute_raw_carry_freshness(
        raw_carry["500"],
        raw_carry["1000"],
        expected_latest_cash,
    )
    coverage = compute_true_disclosure_coverage(exposures, cfg["pit"]["policies"])
    salg_through = salg_valid_through(exposures)
    data_blockers = freshness_blockers(
        coverage,
        salg_through,
        resolved_data_end,
        carry_freshness,
    )
    verdicts = assemble_verdicts(
        model_comparison,
        frames,
        cfg,
        run_blockers=data_blockers,
    )

    evidence = RunEvidence(
        stock_price_max_date=None,
        index_500_max_date=str(target_returns["500"].index.max().date()),
        index_1000_max_date=str(target_returns["1000"].index.max().date()),
        ic_carry_max_date=cast("str | None", carry_freshness["ic_carry_max_date"]),
        im_carry_max_date=cast("str | None", carry_freshness["im_carry_max_date"]),
        salg_valid_through=salg_through,
        true_first_disclosure_coverage=coverage,
        stage_manifest_hashes={
            "preflight": preflight.manifest_hash,
            "exposures": _sha256_file(
                research_root / "manifests" / "exposures.json"
            ),
            "states": _sha256_file(research_root / "manifests" / "states.json"),
            "structure": structure.manifest_hash,
        },
        input_file_hashes={
            "monthly_exposures.csv.gz": _sha256_file(
                research_root / "monthly_exposures.csv.gz"
            ),
            "state_components.csv": _sha256_file(
                research_root / "state_components.csv"
            ),
            **dict(structure.output_hashes),
        },
    )
    manifest = build_run_manifest(
        cfg,
        preflight,
        verdicts,
        resolved_data_end,
        evidence=evidence,
    )

    verdicts_path = _write_evaluation_csv(backtest_root / "verdicts.csv", verdicts)
    _write_evaluation_csv(
        backtest_root / "production_metrics.csv",
        frames.production_metrics,
    )
    _write_evaluation_csv(
        backtest_root / "yearly_contribution.csv",
        frames.yearly,
    )
    _write_evaluation_csv(backtest_root / "bootstrap.csv", frames.bootstrap)
    manifest_path = write_run_manifest(
        backtest_root / "run_manifest.json",
        manifest,
    )
    final = str(manifest["final_verdict"])
    return EvaluationRunResult(
        final_verdict=final,
        blocked=final in {"DATA_BLOCKED", "COVERAGE_BLOCKED"},
        verdicts_path=verdicts_path,
        manifest_path=manifest_path,
    )


def _cli_data_end(value: str) -> str:
    try:
        return _requested_date_string(value, "requested_data_end")
    except DataBlocked as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="B3 fail-closed same-scale evaluation and verdicts"
    )
    parser.add_argument(
        "--data-end",
        type=_cli_data_end,
        default=None,
    )
    parser.add_argument(
        "--research-output-dir",
        type=Path,
        default=DEFAULT_RESEARCH_OUTPUT_DIR,
    )
    parser.add_argument(
        "--backtest-output-dir",
        type=Path,
        default=DEFAULT_BACKTEST_OUTPUT_DIR,
    )
    args = parser.parse_args(argv)
    cfg = load_b3_config()
    try:
        result = run_evaluation(
            cfg,
            args.data_end,
            args.research_output_dir,
            args.backtest_output_dir,
        )
    except DataBlocked as exc:
        # Pre-audit rejection: unlike an audited block, nothing was written.
        print(
            f"DATA_BLOCKED (pre-audit rejection, no audit evidence written): {exc}",
            file=sys.stderr,
        )
        return 2
    if result.final_verdict == "DATA_BLOCKED":
        return 2
    if result.final_verdict == "COVERAGE_BLOCKED":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
