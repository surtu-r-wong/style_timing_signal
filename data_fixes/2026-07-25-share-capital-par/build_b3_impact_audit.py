#!/usr/bin/env python3
"""Build a read-only per-ticker audit for B3 SHARES/CLOSE blockers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg2
import yaml


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


MIN_LISTED_DAYS = 180
EXCLUDED_SUFFIXES = (".BJ", ".HK")


def _parse_dates(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        out[column] = pd.to_datetime(out[column], errors="raise")
    return out


def _reject_duplicate_keys(
    frame: pd.DataFrame,
    keys: tuple[str, ...],
    label: str,
) -> None:
    deduplicated = frame.drop_duplicates()
    if deduplicated.duplicated(list(keys), keep=False).any():
        raise AuditContractError(f"{label} has conflicting duplicate keys")


def _share_asof(
    shares: pd.DataFrame,
    formation: pd.Timestamp,
) -> pd.DataFrame:
    known = shares[shares["known_date"].le(formation)]
    if known.empty:
        return known
    return (
        known.sort_values(
            ["ts_code", "end_date"],
            kind="mergesort",
        )
        .groupby("ts_code", as_index=False)
        .tail(1)
    )


def build_impact_details(
    *,
    formations: pd.DataFrame,
    meta: pd.DataFrame,
    exact_closes: pd.DataFrame,
    shares: pd.DataFrame,
    suspensions: pd.DataFrame,
    carried_closes: pd.DataFrame,
    tail_tickers: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Mirror B3 size-reason precedence for every formation coordinate."""
    _require_columns(
        formations,
        {"formation_date", "required_formation"},
        "formations",
    )
    _require_columns(
        meta,
        {"ticker", "list_date", "delist_date"},
        "stock metadata",
    )
    _require_columns(
        exact_closes,
        {
            "ticker",
            "formation_date",
            "raw_close",
            "raw_price_row_present",
        },
        "exact closes",
    )
    _require_columns(
        shares,
        {"ts_code", "end_date", "known_date", "total_shares"},
        "share capital",
    )
    _require_columns(
        suspensions,
        {"ticker", "formation_date"},
        "suspensions",
    )
    _require_columns(
        carried_closes,
        {
            "ticker",
            "formation_date",
            "carry_close_date",
            "carry_close",
        },
        "carried closes",
    )

    formations = _parse_dates(formations, ("formation_date",))
    formations["required_formation"] = _coerce_bool(
        formations["required_formation"],
        "formations required_formation",
    )
    meta = _parse_dates(meta, ("list_date", "delist_date"))
    exact_closes = _parse_dates(exact_closes, ("formation_date",))
    shares = _parse_dates(shares, ("end_date", "known_date"))
    suspensions = _parse_dates(suspensions, ("formation_date",))
    carried_closes = _parse_dates(
        carried_closes,
        ("formation_date", "carry_close_date"),
    )

    for frame, column, label in (
        (meta, "ticker", "stock metadata"),
        (exact_closes, "ticker", "exact closes"),
        (shares, "ts_code", "share capital"),
        (suspensions, "ticker", "suspensions"),
        (carried_closes, "ticker", "carried closes"),
    ):
        values = frame[column]
        if values.isna().any() or values.astype(str).str.strip().eq("").any():
            raise AuditContractError(f"{label} has invalid ticker keys")

    _reject_duplicate_keys(meta, ("ticker",), "stock metadata")
    _reject_duplicate_keys(
        exact_closes,
        ("ticker", "formation_date"),
        "exact closes",
    )
    _reject_duplicate_keys(
        shares,
        ("ts_code", "end_date"),
        "share capital",
    )
    _reject_duplicate_keys(
        suspensions,
        ("ticker", "formation_date"),
        "suspensions",
    )
    _reject_duplicate_keys(
        carried_closes,
        ("ticker", "formation_date"),
        "carried closes",
    )

    exact_closes["raw_close"] = pd.to_numeric(
        exact_closes["raw_close"],
        errors="coerce",
    )
    exact_closes["raw_price_row_present"] = _coerce_bool(
        exact_closes["raw_price_row_present"],
        "exact closes raw_price_row_present",
    )
    shares["total_shares"] = pd.to_numeric(
        shares["total_shares"],
        errors="coerce",
    )
    shares = shares[
        shares["total_shares"].notna()
        & shares["total_shares"].gt(0)
    ].copy()
    carried_closes["carry_close"] = pd.to_numeric(
        carried_closes["carry_close"],
        errors="coerce",
    )

    classified_parts = []
    for formation_row in formations.sort_values(
        "formation_date",
        kind="mergesort",
    ).itertuples(index=False):
        formation = pd.Timestamp(formation_row.formation_date)
        active = meta[
            ~meta["ticker"].str.endswith(EXCLUDED_SUFFIXES)
            & (
                meta["list_date"].isna()
                | meta["list_date"].le(formation)
            )
            & (
                meta["delist_date"].isna()
                | meta["delist_date"].ge(formation)
            )
        ].copy()
        active["formation_date"] = formation
        active["required_formation"] = bool(
            formation_row.required_formation
        )
        active["listed_lt_180"] = (
            active["list_date"].notna()
            & (
                active["list_date"]
                + pd.Timedelta(days=MIN_LISTED_DAYS)
                > formation
            )
        )

        exact = exact_closes[
            exact_closes["formation_date"].eq(formation)
        ][["ticker", "raw_price_row_present", "raw_close"]]
        active = active.merge(exact, on="ticker", how="left")
        active["raw_price_row_present"] = active[
            "raw_price_row_present"
        ].fillna(False)

        suspension_set = set(
            suspensions.loc[
                suspensions["formation_date"].eq(formation),
                "ticker",
            ]
        )
        carry = carried_closes[
            carried_closes["formation_date"].eq(formation)
        ][["ticker", "carry_close_date", "carry_close"]]
        active = active.merge(carry, on="ticker", how="left")
        active["suspension_evidence"] = active["ticker"].isin(
            suspension_set
        )
        active["usable_carry"] = (
            active["suspension_evidence"]
            & active["raw_close"].isna()
            & active["carry_close"].notna()
        )
        active["close"] = active["raw_close"]
        active.loc[active["usable_carry"], "close"] = active.loc[
            active["usable_carry"],
            "carry_close",
        ]
        active["close_source"] = np.where(
            active["raw_close"].notna(),
            "EXACT_FORMATION_CLOSE",
            np.where(
                active["usable_carry"],
                "SUSPENDED_CARRY_FORWARD",
                "",
            ),
        )

        selected = _share_asof(shares, formation).rename(
            columns={
                "end_date": "selected_share_effective_date",
                "known_date": "selected_share_known_date",
                "total_shares": "selected_total_shares",
            }
        )
        active = active.merge(
            selected[
                [
                    "ts_code",
                    "selected_share_effective_date",
                    "selected_share_known_date",
                    "selected_total_shares",
                ]
            ].rename(columns={"ts_code": "ticker"}),
            on="ticker",
            how="left",
        )

        active["size_reason"] = ""
        active.loc[
            active["list_date"].isna(),
            "size_reason",
        ] = "DATA_MISSING_LIST_DATE"
        active.loc[
            active["size_reason"].eq("") & active["listed_lt_180"],
            "size_reason",
        ] = "LISTED_LT_180D"
        active.loc[
            active["size_reason"].eq("") & active["close"].isna(),
            "size_reason",
        ] = "DATA_MISSING_CLOSE"
        active.loc[
            active["size_reason"].eq("")
            & active["selected_total_shares"].isna(),
            "size_reason",
        ] = "DATA_MISSING_SHARES"
        market_value = active["close"] * active["selected_total_shares"]
        invalid_market_value = (
            ~np.isfinite(market_value.to_numpy(dtype=float))
            | market_value.le(0).to_numpy()
        )
        active.loc[
            active["size_reason"].eq("") & invalid_market_value,
            "size_reason",
        ] = "DATA_INVALID_MARKET_VALUE"
        classified_parts.append(active)

    classified = pd.concat(classified_parts, ignore_index=True).rename(
        columns={"ticker": "ts_code"}
    )
    shares_detail = classified[
        classified["size_reason"].eq("DATA_MISSING_SHARES")
    ].copy()
    unexpected = set(shares_detail["ts_code"]) - set(tail_tickers)
    if unexpected:
        raise AuditContractError(
            "DATA_MISSING_SHARES contains tickers outside tail: "
            f"{sorted(unexpected)[:10]}"
        )
    close_detail = classified[
        classified["size_reason"].eq("DATA_MISSING_CLOSE")
    ].copy()
    return (
        shares_detail.reset_index(drop=True),
        close_detail.reset_index(drop=True),
        classified.reset_index(drop=True),
    )


def close_evidence_bucket(row: pd.Series) -> str:
    """Return a direct-evidence label, never a final disposition."""
    if bool(row["raw_price_row_present"]) and pd.isna(row["raw_close"]):
        return "EXACT_ROW_NULL_CLOSE"
    if bool(row["suspension_evidence"]) and not bool(row["usable_carry"]):
        return "SUSPENSION_WITHOUT_USABLE_CARRY"
    if pd.notna(row["delist_date"]) and pd.isna(
        row["next_nonnull_close"]
    ):
        return "POSSIBLE_DELIST_BOUNDARY"
    return "UNEXPLAINED_EXACT_DATE_GAP"


def enrich_close_evidence(
    close_detail: pd.DataFrame,
    neighbors: pd.DataFrame,
) -> pd.DataFrame:
    required = {
        "ts_code",
        "formation_date",
        "previous_nonnull_close_date",
        "previous_nonnull_close",
        "next_nonnull_close_date",
        "next_nonnull_close",
    }
    _require_columns(neighbors, required, "close neighbors")
    detail = _parse_dates(close_detail, ("formation_date", "delist_date"))
    evidence = _parse_dates(
        neighbors,
        (
            "formation_date",
            "previous_nonnull_close_date",
            "next_nonnull_close_date",
        ),
    )
    _reject_duplicate_keys(
        evidence,
        ("ts_code", "formation_date"),
        "close neighbors",
    )
    out = detail.merge(
        evidence,
        on=["ts_code", "formation_date"],
        how="left",
        validate="1:1",
    )
    out["after_last_observed_close"] = (
        out["previous_nonnull_close_date"].notna()
        & out["previous_nonnull_close_date"].lt(out["formation_date"])
        & out["next_nonnull_close_date"].isna()
    )
    out["no_later_observed_close"] = out[
        "next_nonnull_close_date"
    ].isna()
    out["evidence_bucket"] = out.apply(
        close_evidence_bucket,
        axis=1,
    )
    out["reason_code"] = "DATA_MISSING_CLOSE"
    return out


def build_pool_membership(
    classified: pd.DataFrame,
    formations: pd.DataFrame,
) -> pd.DataFrame:
    frame = _parse_dates(
        classified,
        ("formation_date", "list_date", "delist_date"),
    )
    formation_frame = _parse_dates(formations, ("formation_date",))
    dates_2023 = formation_frame.loc[
        formation_frame["formation_date"].dt.year.eq(2023),
        "formation_date",
    ]
    if dates_2023.empty:
        raise AuditContractError("formation grid has no 2023 dates")
    last_2023 = dates_2023.max()
    eligible = frame[
        frame["list_date"].notna() & ~frame["listed_lt_180"]
    ]
    tickers = pd.DataFrame(
        {"ts_code": sorted(frame["ts_code"].unique())}
    )
    any_2023 = set(
        eligible.loc[
            eligible["formation_date"].dt.year.eq(2023),
            "ts_code",
        ]
    )
    in_last = set(
        eligible.loc[
            eligible["formation_date"].eq(last_2023),
            "ts_code",
        ]
    )
    tickers["in_pool_2023_any"] = tickers["ts_code"].isin(any_2023)
    tickers["in_pool_2023_12"] = tickers["ts_code"].isin(in_last)
    tickers.attrs["data_end"] = formation_frame["formation_date"].max()
    return tickers


def summarize_impacts(
    detail: pd.DataFrame,
    pool_membership: pd.DataFrame,
    *,
    include_close_buckets: bool = False,
) -> pd.DataFrame:
    """Build stable per-ticker impact counts and priority ordering."""
    frame = _parse_dates(
        detail,
        ("formation_date", "list_date", "delist_date"),
    )
    frame["required_formation"] = _coerce_bool(
        frame["required_formation"],
        "detail required_formation",
    )
    grouped = frame.groupby("ts_code", sort=True)
    out = grouped.agg(
        list_date=("list_date", "first"),
        delist_date=("delist_date", "first"),
        affected_months_all=("formation_date", "size"),
        affected_months_required=("required_formation", "sum"),
        first_affected_formation=("formation_date", "min"),
        last_affected_formation=("formation_date", "max"),
    ).reset_index()
    affected_2023 = (
        frame[frame["formation_date"].dt.year.eq(2023)]
        .groupby("ts_code")
        .size()
    )
    out["affected_months_2023"] = (
        out["ts_code"].map(affected_2023).fillna(0).astype(int)
    )
    data_end = pool_membership.attrs.get("data_end")
    if data_end is None:
        raise AuditContractError("pool membership lacks data_end")
    out = out.merge(
        pool_membership,
        on="ts_code",
        how="left",
        validate="1:1",
    )
    out[["in_pool_2023_any", "in_pool_2023_12"]] = out[
        ["in_pool_2023_any", "in_pool_2023_12"]
    ].fillna(False).astype(bool)
    out["listing_status_at_data_end"] = np.where(
        out["list_date"].isna(),
        "LIST_DATE_MISSING",
        np.where(
            out["delist_date"].notna()
            & out["delist_date"].lt(pd.Timestamp(data_end)),
            "DELISTED",
            "ACTIVE",
        ),
    )

    if include_close_buckets:
        _require_columns(
            frame,
            {
                "evidence_bucket",
                "raw_price_row_present",
                "raw_close",
            },
            "close detail",
        )
        raw_present = _coerce_bool(
            frame["raw_price_row_present"],
            "close detail raw_price_row_present",
        )
        count_frame = pd.DataFrame(
            {
                "ts_code": frame["ts_code"],
                "exact_row_missing_months": (~raw_present).astype(int),
                "exact_row_null_close_months": (
                    raw_present & frame["raw_close"].isna()
                ).astype(int),
                "suspension_without_usable_carry_months": frame[
                    "evidence_bucket"
                ].eq("SUSPENSION_WITHOUT_USABLE_CARRY").astype(int),
                "possible_delist_boundary_months": frame[
                    "evidence_bucket"
                ].eq("POSSIBLE_DELIST_BOUNDARY").astype(int),
                "unexplained_exact_date_gap_months": frame[
                    "evidence_bucket"
                ].eq("UNEXPLAINED_EXACT_DATE_GAP").astype(int),
            }
        )
        counts = count_frame.groupby("ts_code", as_index=False).sum()
        out = out.merge(counts, on="ts_code", how="left", validate="1:1")

    out = out.sort_values(
        [
            "in_pool_2023_12",
            "affected_months_2023",
            "affected_months_required",
            "affected_months_all",
            "ts_code",
        ],
        ascending=[False, False, False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    out["priority_rank"] = np.arange(1, len(out) + 1)
    return out


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARTIFACT_NAMES = (
    "shares_tail_impact_by_ticker.csv",
    "shares_tail_impact_detail.csv",
    "close_gap_impact_by_ticker.csv",
    "close_gap_impact_detail.csv",
)


def connect_read_only(db: dict):
    conn = psycopg2.connect(
        host=db["host"],
        port=db["port"],
        dbname=db["name"],
        user=db["user"],
        password=db["password"],
        connect_timeout=8,
        options="-c statement_timeout=180000",
    )
    conn.set_session(readonly=True, autocommit=False)
    return conn


def reconcile_details(
    shares_detail: pd.DataFrame,
    close_detail: pd.DataFrame,
    anchors: AuditAnchors,
) -> None:
    expected = anchors.expected_counts.copy()
    expected.index = pd.DatetimeIndex(expected.index)
    for reason, detail in (
        ("DATA_MISSING_SHARES", shares_detail),
        ("DATA_MISSING_CLOSE", close_detail),
    ):
        _require_columns(
            detail,
            {"ts_code", "formation_date", "required_formation"},
            f"{reason} detail",
        )
        frame = _parse_dates(detail, ("formation_date",))
        frame["required_formation"] = _coerce_bool(
            frame["required_formation"],
            f"{reason} required_formation",
        )
        unknown_dates = set(frame["formation_date"]) - set(expected.index)
        if unknown_dates:
            raise AuditContractError(
                f"{reason} contains unknown formation dates"
            )
        actual = (
            frame.groupby("formation_date")
            .size()
            .reindex(expected.index, fill_value=0)
            .astype(int)
        )
        wanted = expected[reason].astype(int)
        if not actual.equals(wanted):
            mismatch = actual.ne(wanted)
            first = actual.index[mismatch][0]
            raise AuditContractError(
                f"{reason} monthly count mismatch at {first.date()}: "
                f"got {actual.loc[first]}, expected {wanted.loc[first]}"
            )
        wanted_required = int(
            expected.loc[expected["required_formation"], reason].sum()
        )
        if int(frame["required_formation"].sum()) != wanted_required:
            raise AuditContractError(
                f"{reason} required count mismatch"
            )
    if set(shares_detail["ts_code"]) != set(anchors.tail_tickers):
        raise AuditContractError(
            "DATA_MISSING_SHARES ticker set differs from tail"
        )


def _validate_artifacts(artifacts: dict[str, pd.DataFrame]) -> None:
    if set(artifacts) != set(ARTIFACT_NAMES):
        raise AuditContractError("artifact file set differs from contract")
    requirements = {
        "shares_tail_impact_by_ticker.csv": {
            "ts_code",
            "priority_rank",
        },
        "shares_tail_impact_detail.csv": {
            "ts_code",
            "reason_code",
        },
        "close_gap_impact_by_ticker.csv": {
            "ts_code",
            "priority_rank",
        },
        "close_gap_impact_detail.csv": {
            "ts_code",
            "reason_code",
            "evidence_bucket",
        },
    }
    for name, required in requirements.items():
        _require_columns(artifacts[name], required, name)
    for name in (
        "shares_tail_impact_detail.csv",
        "close_gap_impact_detail.csv",
    ):
        reason = artifacts[name]["reason_code"]
        if reason.isna().any() or reason.astype(str).str.strip().eq("").any():
            raise AuditContractError(f"{name} reason_code is blank")
    bucket = artifacts["close_gap_impact_detail.csv"][
        "evidence_bucket"
    ]
    if bucket.isna().any() or bucket.astype(str).str.strip().eq("").any():
        raise AuditContractError("close detail evidence_bucket is blank")


def publish_outputs(
    output_dir: str | Path,
    artifacts: dict[str, pd.DataFrame],
    manifest_base: dict,
) -> dict:
    _validate_artifacts(artifacts)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".impact-audit-", dir=root)
    )
    try:
        outputs = {}
        for name in ARTIFACT_NAMES:
            path = staging / name
            artifacts[name].to_csv(
                path,
                index=False,
                date_format="%Y-%m-%d",
                lineterminator="\n",
            )
            outputs[name] = {
                "row_count": int(len(artifacts[name])),
                "sha256": sha256_file(path),
            }
        manifest = {
            **manifest_base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "outputs": outputs,
        }
        manifest_path = staging / "impact_audit_manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest,
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        for name in (*ARTIFACT_NAMES, "impact_audit_manifest.json"):
            os.replace(staging / name, root / name)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validated_schema(schema: str) -> str:
    if not _SCHEMA_RE.fullmatch(str(schema)):
        raise AuditContractError(f"invalid database schema: {schema!r}")
    return str(schema)


def _read_sql(conn, sql: str, params=None) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def fetch_audit_sources(
    conn,
    schema: str,
    formations: pd.DataFrame,
    tail_tickers: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    schema = _validated_schema(schema)
    dates = [
        value.date()
        for value in pd.to_datetime(formations["formation_date"])
    ]
    meta = _read_sql(
        conn,
        f"""
        SELECT ts_code AS ticker, list_date, delist_date
        FROM {schema}.stock_meta
        ORDER BY ts_code
        """,
    )
    exact_closes = _read_sql(
        conn,
        f"""
        SELECT ts_code AS ticker, trade_date AS formation_date,
               close AS raw_close, TRUE AS raw_price_row_present
        FROM {schema}.stock_daily_price
        WHERE trade_date = ANY(%(dates)s)
        ORDER BY trade_date, ts_code
        """,
        {"dates": dates},
    )
    shares = _read_sql(
        conn,
        f"""
        SELECT ts_code, effective_date AS end_date,
               COALESCE(available_date, effective_date) AS known_date,
               total_shares
        FROM {schema}.stock_share_capital
        WHERE ts_code = ANY(%(tickers)s)
          AND total_shares IS NOT NULL
          AND total_shares > 0
        ORDER BY ts_code, effective_date
        """,
        {"tickers": list(tail_tickers)},
    )
    suspensions = _read_sql(
        conn,
        f"""
        SELECT ts_code AS ticker, trade_date AS formation_date
        FROM {schema}.stock_suspension
        WHERE trade_date = ANY(%(dates)s)
        ORDER BY trade_date, ts_code
        """,
        {"dates": dates},
    )
    carried_closes = _read_sql(
        conn,
        f"""
        SELECT s.ts_code AS ticker, s.trade_date AS formation_date,
               p.trade_date AS carry_close_date, p.close AS carry_close
        FROM {schema}.stock_suspension s
        JOIN LATERAL (
            SELECT q.trade_date, q.close
            FROM {schema}.stock_daily_price q
            WHERE q.ts_code = s.ts_code
              AND q.trade_date <= s.trade_date
            ORDER BY q.trade_date DESC
            LIMIT 1
        ) p ON TRUE
        WHERE s.trade_date = ANY(%(dates)s)
        ORDER BY s.trade_date, s.ts_code
        """,
        {"dates": dates},
    )
    return {
        "meta": meta,
        "exact_closes": exact_closes,
        "shares": shares,
        "suspensions": suspensions,
        "carried_closes": carried_closes,
    }


def fetch_close_neighbors(
    conn,
    schema: str,
    close_detail: pd.DataFrame,
) -> pd.DataFrame:
    schema = _validated_schema(schema)
    keys = close_detail[["ts_code", "formation_date"]].drop_duplicates()
    columns = [
        "ts_code",
        "formation_date",
        "previous_nonnull_close_date",
        "previous_nonnull_close",
        "next_nonnull_close_date",
        "next_nonnull_close",
    ]
    if keys.empty:
        return pd.DataFrame(columns=columns)
    keys["formation_date"] = pd.to_datetime(keys["formation_date"])
    sql = f"""
        WITH holes AS (
            SELECT *
            FROM unnest(
                %(tickers)s::text[],
                %(dates)s::date[]
            ) AS h(ts_code, formation_date)
        )
        SELECT h.ts_code, h.formation_date,
               prev.trade_date AS previous_nonnull_close_date,
               prev.close AS previous_nonnull_close,
               nxt.trade_date AS next_nonnull_close_date,
               nxt.close AS next_nonnull_close
        FROM holes h
        LEFT JOIN LATERAL (
            SELECT p.trade_date, p.close
            FROM {schema}.stock_daily_price p
            WHERE p.ts_code = h.ts_code
              AND p.trade_date < h.formation_date
              AND p.close IS NOT NULL
            ORDER BY p.trade_date DESC
            LIMIT 1
        ) prev ON TRUE
        LEFT JOIN LATERAL (
            SELECT p.trade_date, p.close
            FROM {schema}.stock_daily_price p
            WHERE p.ts_code = h.ts_code
              AND p.trade_date > h.formation_date
              AND p.close IS NOT NULL
            ORDER BY p.trade_date
            LIMIT 1
        ) nxt ON TRUE
        ORDER BY h.formation_date, h.ts_code
    """
    return _read_sql(
        conn,
        sql,
        {
            "tickers": keys["ts_code"].tolist(),
            "dates": [value.date() for value in keys["formation_date"]],
        },
    )


def load_db_config(path: str | Path) -> dict:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    db = payload.get("database") or {}
    required = {"host", "port", "name", "user", "password", "schema"}
    missing = sorted(
        key for key in required if db.get(key) in (None, "")
    )
    if missing:
        raise AuditContractError(f"database config missing: {missing}")
    _validated_schema(db["schema"])
    return db


SHARE_DETAIL_COLUMNS = [
    "ts_code",
    "formation_date",
    "required_formation",
    "list_date",
    "delist_date",
    "raw_close",
    "close_source",
    "selected_share_effective_date",
    "selected_share_known_date",
    "selected_total_shares",
    "reason_code",
]
CLOSE_DETAIL_COLUMNS = [
    "ts_code",
    "formation_date",
    "required_formation",
    "list_date",
    "delist_date",
    "raw_price_row_present",
    "raw_close",
    "suspension_evidence",
    "carry_close_date",
    "carry_close",
    "usable_carry",
    "previous_nonnull_close_date",
    "previous_nonnull_close",
    "next_nonnull_close_date",
    "next_nonnull_close",
    "after_last_observed_close",
    "no_later_observed_close",
    "evidence_bucket",
    "reason_code",
]
SHARE_SUMMARY_COLUMNS = [
    "ts_code",
    "list_date",
    "delist_date",
    "listing_status_at_data_end",
    "in_pool_2023_any",
    "in_pool_2023_12",
    "affected_months_all",
    "affected_months_required",
    "affected_months_2023",
    "first_affected_formation",
    "last_affected_formation",
    "csmar_latest_a003101000",
    "anchor_2025_shares",
    "implied_par",
    "note",
    "priority_rank",
]
CLOSE_SUMMARY_COLUMNS = [
    "ts_code",
    "list_date",
    "delist_date",
    "listing_status_at_data_end",
    "in_pool_2023_any",
    "in_pool_2023_12",
    "affected_months_all",
    "affected_months_required",
    "affected_months_2023",
    "first_affected_formation",
    "last_affected_formation",
    "exact_row_missing_months",
    "exact_row_null_close_months",
    "suspension_without_usable_carry_months",
    "possible_delist_boundary_months",
    "unexplained_exact_date_gap_months",
    "priority_rank",
]


def prepare_artifacts(
    tail: pd.DataFrame,
    shares_detail: pd.DataFrame,
    close_detail: pd.DataFrame,
    classified: pd.DataFrame,
    formations: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    pool = build_pool_membership(classified, formations)
    share_summary = summarize_impacts(shares_detail, pool)
    diagnostics = tail[
        [
            "ts_code",
            "csmar_latest_a003101000",
            "anchor_2025_shares",
            "implied_par",
            "note",
        ]
    ]
    share_summary = share_summary.merge(
        diagnostics,
        on="ts_code",
        how="left",
        validate="1:1",
    ).reindex(columns=SHARE_SUMMARY_COLUMNS)
    close_summary = summarize_impacts(
        close_detail,
        pool,
        include_close_buckets=True,
    ).reindex(columns=CLOSE_SUMMARY_COLUMNS)
    share_rows = shares_detail.copy()
    share_rows["reason_code"] = "DATA_MISSING_SHARES"
    share_rows = share_rows.reindex(columns=SHARE_DETAIL_COLUMNS)
    close_rows = close_detail.reindex(columns=CLOSE_DETAIL_COLUMNS)
    return {
        "shares_tail_impact_by_ticker.csv": share_summary,
        "shares_tail_impact_detail.csv": share_rows,
        "close_gap_impact_by_ticker.csv": close_summary,
        "close_gap_impact_detail.csv": close_rows,
    }


def _verify_hash(path: Path, expected: str, label: str) -> str:
    actual = sha256_file(path)
    if actual != expected:
        raise AuditContractError(
            f"{label} SHA-256 mismatch: got {actual}, expected {expected}"
        )
    return actual


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", type=Path, default=HERE / "tail.csv")
    parser.add_argument(
        "--coverage-audit",
        type=Path,
        default=ROOT / "output/style_basket/b3/coverage_audit.csv",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=ROOT / "config/settings.yaml",
    )
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument(
        "--expected-tail-sha256",
        default=EXPECTED_TAIL_SHA256,
    )
    parser.add_argument(
        "--expected-coverage-sha256",
        default=EXPECTED_COVERAGE_SHA256,
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    tail_hash = _verify_hash(
        args.tail,
        args.expected_tail_sha256,
        "tail",
    )
    coverage_hash = _verify_hash(
        args.coverage_audit,
        args.expected_coverage_sha256,
        "coverage_audit",
    )
    tail = pd.read_csv(args.tail)
    coverage = pd.read_csv(args.coverage_audit)
    anchors = validate_anchors(tail, coverage)
    db = load_db_config(args.settings)
    conn = connect_read_only(db)
    try:
        sources = fetch_audit_sources(
            conn,
            db["schema"],
            anchors.formations,
            anchors.tail_tickers,
        )
        shares_detail, close_detail, classified = build_impact_details(
            formations=anchors.formations,
            tail_tickers=anchors.tail_tickers,
            **sources,
        )
        neighbors = fetch_close_neighbors(
            conn,
            db["schema"],
            close_detail,
        )
        close_detail = enrich_close_evidence(close_detail, neighbors)
        reconcile_details(shares_detail, close_detail, anchors)
        artifacts = prepare_artifacts(
            tail,
            shares_detail,
            close_detail,
            classified,
            anchors.formations,
        )
        manifest = publish_outputs(
            args.output_dir,
            artifacts,
            {
                "data_end": str(
                    anchors.formations["formation_date"].max().date()
                ),
                "schema": db["schema"],
                "inputs": {
                    "tail.csv": {
                        "path": str(args.tail.resolve()),
                        "sha256": tail_hash,
                    },
                    "coverage_audit.csv": {
                        "path": str(args.coverage_audit.resolve()),
                        "sha256": coverage_hash,
                    },
                },
                "counts": {
                    "DATA_MISSING_SHARES": {
                        "all": int(len(shares_detail)),
                        "required": int(
                            shares_detail["required_formation"].sum()
                        ),
                        "tickers": int(shares_detail["ts_code"].nunique()),
                    },
                    "DATA_MISSING_CLOSE": {
                        "all": int(len(close_detail)),
                        "required": int(
                            close_detail["required_formation"].sum()
                        ),
                        "tickers": int(close_detail["ts_code"].nunique()),
                    },
                },
                "source_tables": [
                    f"{db[schema]}.stock_meta",
                    f"{db[schema]}.stock_daily_price",
                    f"{db[schema]}.stock_share_capital",
                    f"{db[schema]}.stock_suspension",
                ],
            },
        )
    finally:
        conn.rollback()
        conn.close()

    share_counts = manifest["counts"]["DATA_MISSING_SHARES"]
    close_counts = manifest["counts"]["DATA_MISSING_CLOSE"]
    print(
        "DATA_MISSING_SHARES: "
        f"{share_counts[all]} all / "
        f"{share_counts[required]} required / "
        f"{share_counts[tickers]} tickers"
    )
    print(
        "DATA_MISSING_CLOSE: "
        f"{close_counts[all]} all / "
        f"{close_counts[required]} required / "
        f"{close_counts[tickers]} tickers"
    )
    print("monthly reconciliation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
