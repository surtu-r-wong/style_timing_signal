#!/usr/bin/env python3
"""Build a read-only per-ticker audit for B3 SHARES/CLOSE blockers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import NamedTuple

import numpy as np
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
