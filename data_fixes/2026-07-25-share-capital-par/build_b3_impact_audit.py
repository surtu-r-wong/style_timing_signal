#!/usr/bin/env python3
"""Build a read-only per-ticker audit for B3 SHARES/CLOSE blockers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import NamedTuple

import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
POLICY_MAIN = "legal_deadline"
POLICY_LAG = "legal_deadline_plus_one_month_end"
REASONS = ("DATA_MISSING_SHARES", "DATA_MISSING_CLOSE")
EXPECTED_TAIL_ROWS = 57
EXPECTED_FORMATIONS = 128
EXPECTED_REQUIRED_FORMATIONS = 120
EXPECTED_COUNTS = {
    "DATA_MISSING_SHARES": (5781, 5445),
    "DATA_MISSING_CLOSE": (202, 190),
}
EXPECTED_TAIL_SHA256 = (
    "93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223"
)
EXPECTED_COVERAGE_SHA256 = (
    "13c8af70650a24ba00c1b0890e979c487a0133589e6127967340e622426e9358"
)


class AuditContractError(RuntimeError):
    """Raised when immutable inputs or reconstructed audit counts diverge."""


class AuditAnchors(NamedTuple):
    formations: pd.DataFrame
    expected_counts: pd.DataFrame
    tail_tickers: tuple[str, ...]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_bool(values: pd.Series, label: str) -> pd.Series:
    if pd.api.types.is_bool_dtype(values):
        return values.astype(bool)
    normalized = values.astype(str).str.strip().str.lower()
    invalid = ~normalized.isin({"true", "false"})
    if invalid.any():
        raise AuditContractError(f"{label} contains invalid booleans")
    return normalized.eq("true")


def _require_columns(
    frame: pd.DataFrame,
    required: set[str],
    label: str,
) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise AuditContractError(f"{label} missing columns: {missing}")


def validate_anchors(
    tail: pd.DataFrame,
    coverage: pd.DataFrame,
) -> AuditAnchors:
    """Validate the immutable 57/128 audit inputs and canonical counts."""
    _require_columns(
        tail,
        {
            "ts_code",
            "list_date",
            "csmar_latest_a003101000",
            "anchor_2025_shares",
            "implied_par",
            "note",
        },
        "tail",
    )
    tickers = tail["ts_code"].astype(str).str.strip()
    if (
        len(tickers) != EXPECTED_TAIL_ROWS
        or tickers.nunique() != EXPECTED_TAIL_ROWS
        or tickers.eq("").any()
    ):
        raise AuditContractError("tail must contain 57 unique tickers")

    _require_columns(
        coverage,
        {
            "pit_policy",
            "formation_date",
            "required_formation",
            "check",
            "side",
            "eligible_count",
        },
        "coverage audit",
    )
    dated = coverage[coverage["formation_date"].notna()].copy()
    dated["formation_date"] = pd.to_datetime(
        dated["formation_date"],
        errors="raise",
    )
    dated["required_formation"] = _coerce_bool(
        dated["required_formation"],
        "coverage required_formation",
    )

    flag_counts = dated.groupby("formation_date")[
        "required_formation"
    ].nunique()
    if flag_counts.ne(1).any():
        raise AuditContractError(
            "coverage has conflicting required_formation flags"
        )
    flags = (
        dated.groupby("formation_date", sort=True)["required_formation"]
        .first()
        .rename("required_formation")
    )
    formations = flags.reset_index()
    if (
        len(formations) != EXPECTED_FORMATIONS
        or int(formations["required_formation"].sum())
        != EXPECTED_REQUIRED_FORMATIONS
    ):
        raise AuditContractError("formation grid must be 128/120")

    size_rows = dated[
        dated["check"].eq("size_exclusion")
        & dated["side"].isin(REASONS)
        & dated["pit_policy"].isin((POLICY_MAIN, POLICY_LAG))
    ].copy()
    size_rows["eligible_count"] = pd.to_numeric(
        size_rows["eligible_count"],
        errors="raise",
    ).astype(int)
    if size_rows["eligible_count"].lt(0).any():
        raise AuditContractError("coverage counts must be non-negative")

    full_index = pd.MultiIndex.from_product(
        [
            formations["formation_date"],
            REASONS,
        ],
        names=["formation_date", "side"],
    )
    policy_counts: dict[str, pd.Series] = {}
    for policy in (POLICY_MAIN, POLICY_LAG):
        policy_counts[policy] = (
            size_rows[size_rows["pit_policy"].eq(policy)]
            .groupby(["formation_date", "side"])["eligible_count"]
            .sum()
            .reindex(full_index, fill_value=0)
            .astype(int)
        )
    if not policy_counts[POLICY_MAIN].equals(policy_counts[POLICY_LAG]):
        raise AuditContractError("PIT policy monthly counts differ")

    expected = (
        policy_counts[POLICY_MAIN]
        .unstack("side")
        .reindex(columns=REASONS, fill_value=0)
        .reset_index()
        .merge(formations, on="formation_date", how="left", validate="1:1")
        .set_index("formation_date")
    )
    for reason, (all_count, required_count) in EXPECTED_COUNTS.items():
        if int(expected[reason].sum()) != all_count:
            raise AuditContractError(f"{reason} all count mismatch")
        if (
            int(
                expected.loc[
                    expected["required_formation"],
                    reason,
                ].sum()
            )
            != required_count
        ):
            raise AuditContractError(f"{reason} required count mismatch")

    return AuditAnchors(
        formations=formations,
        expected_counts=expected,
        tail_tickers=tuple(sorted(tickers)),
    )
