# B3 Tail and CLOSE Impact Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only, hash-bound audit that expands the 57 residual share-capital tickers and 202 missing-close ticker-months into reproducible detail and priority tables.

**Architecture:** A single dated audit script separates pure pandas classification/summarization from a minimal PostgreSQL read adapter. The script derives formation dates and expected counts from the final `coverage_audit.csv`, mirrors B3 size-exclusion precedence, validates every monthly count, and only then publishes four CSVs plus a manifest.

**Tech Stack:** Python 3.13, pandas, psycopg2, pytest, JSON/CSV, SHA-256.

---

## File map

- Create:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`
  — pure audit rules, read-only SQL adapter, reconciliation, atomic CSV/manifest publication, CLI.
- Create:
  `tests/test_b3_impact_audit.py`
  — importlib-loaded unit and CLI tests using synthetic DataFrames and fake database readers.
- Modify:
  `data_fixes/2026-07-25-share-capital-par/README.md`
  — command, input hashes, produced counts, artifact hashes, and interpretation.
- Generate:
  `data_fixes/2026-07-25-share-capital-par/shares_tail_impact_by_ticker.csv`
- Generate:
  `data_fixes/2026-07-25-share-capital-par/shares_tail_impact_detail.csv`
- Generate:
  `data_fixes/2026-07-25-share-capital-par/close_gap_impact_by_ticker.csv`
- Generate:
  `data_fixes/2026-07-25-share-capital-par/close_gap_impact_detail.csv`
- Generate:
  `data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json`

## Task 1: Input anchors and immutable audit contract

**Files:**

- Create: `tests/test_b3_impact_audit.py`
- Create:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`

- [ ] **Step 1: Write the failing import and anchor tests**

Load the dated script exactly as the existing par verifier test does:

```python
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "data_fixes"
    / "2026-07-25-share-capital-par"
    / "build_b3_impact_audit.py"
)
SPEC = importlib.util.spec_from_file_location("b3_impact_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
```

Add helpers that produce 128 ordered formation dates, 120 required dates, both
PIT policies, and `size_exclusion` rows for SHARES/CLOSE. Add these tests:

```python
def test_load_anchors_requires_57_unique_tail_tickers(tmp_path):
    tail = _tail_frame(56)
    coverage = _coverage_frame()

    with pytest.raises(AUDIT.AuditContractError, match="57 unique"):
        AUDIT.validate_anchors(tail, coverage)


def test_load_anchors_requires_policy_monthly_parity():
    tail = _tail_frame(57)
    coverage = _coverage_frame()
    mask = (
        coverage["pit_policy"].eq(AUDIT.POLICY_LAG)
        & coverage["side"].eq("DATA_MISSING_CLOSE")
    )
    coverage.loc[mask.idxmax(), "eligible_count"] += 1

    with pytest.raises(AUDIT.AuditContractError, match="PIT policy"):
        AUDIT.validate_anchors(tail, coverage)


def test_load_anchors_returns_canonical_128_month_grid():
    anchors = AUDIT.validate_anchors(_tail_frame(57), _coverage_frame())

    assert len(anchors.formations) == 128
    assert int(anchors.formations["required_formation"].sum()) == 120
    assert anchors.expected_counts["DATA_MISSING_SHARES"].sum() == 5781
    assert anchors.expected_counts["DATA_MISSING_CLOSE"].sum() == 202
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py -q
```

Expected: collection/import fails because
`build_b3_impact_audit.py` does not exist.

- [ ] **Step 3: Implement the minimal anchor contract**

Create the script with constants, error/dataclass types, file hashing, schema
validation, and `validate_anchors`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
    pass


@dataclass(frozen=True)
class AuditAnchors:
    formations: pd.DataFrame
    expected_counts: pd.DataFrame
    tail_tickers: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_anchors(tail: pd.DataFrame, coverage: pd.DataFrame) -> AuditAnchors:
    required_tail = {
        "ts_code", "list_date", "csmar_latest_a003101000",
        "anchor_2025_shares", "implied_par", "note",
    }
    if not required_tail.issubset(tail.columns):
        raise AuditContractError("tail columns do not match contract")
    tickers = tail["ts_code"].astype(str)
    if len(tickers) != EXPECTED_TAIL_ROWS or tickers.nunique() != EXPECTED_TAIL_ROWS:
        raise AuditContractError("tail must contain 57 unique tickers")

    rows = coverage[
        coverage["check"].eq("size_exclusion")
        & coverage["side"].isin(REASONS)
        & coverage["pit_policy"].isin((POLICY_MAIN, POLICY_LAG))
    ].copy()
    rows["formation_date"] = pd.to_datetime(rows["formation_date"])
    rows["eligible_count"] = pd.to_numeric(
        rows["eligible_count"], errors="raise"
    ).astype(int)
    parity = rows.pivot_table(
        index=["formation_date", "required_formation", "side"],
        columns="pit_policy",
        values="eligible_count",
        aggfunc="sum",
        fill_value=0,
    )
    parity = parity.reindex(columns=[POLICY_MAIN, POLICY_LAG], fill_value=0)
    if not parity[POLICY_MAIN].equals(parity[POLICY_LAG]):
        raise AuditContractError("PIT policy monthly counts differ")

    all_dates = pd.DatetimeIndex(
        pd.to_datetime(
            coverage.loc[
                coverage["pit_policy"].eq(POLICY_MAIN),
                "formation_date",
            ]
        ).dropna().unique()
    ).sort_values()
    formation_required = (
        coverage[
            coverage["pit_policy"].eq(POLICY_MAIN)
            & coverage["formation_date"].notna()
        ]
        .assign(formation_date=lambda x: pd.to_datetime(x["formation_date"]))
        .groupby("formation_date")["required_formation"]
        .first()
        .reindex(all_dates)
    )
    formations = pd.DataFrame(
        {
            "formation_date": all_dates,
            "required_formation": formation_required.astype(bool).to_numpy(),
        }
    )
    if (
        len(formations) != EXPECTED_FORMATIONS
        or int(formations["required_formation"].sum())
        != EXPECTED_REQUIRED_FORMATIONS
    ):
        raise AuditContractError("formation grid must be 128/120")

    expected = (
        parity[POLICY_MAIN]
        .unstack("side")
        .reindex(
            pd.MultiIndex.from_frame(formations),
            fill_value=0,
        )
        .reindex(columns=REASONS, fill_value=0)
        .astype(int)
    )
    for reason, (all_count, required_count) in EXPECTED_COUNTS.items():
        if int(expected[reason].sum()) != all_count:
            raise AuditContractError(f"{reason} all count mismatch")
        required_mask = expected.index.get_level_values(
            "required_formation"
        ).astype(bool)
        if int(expected.loc[required_mask, reason].sum()) != required_count:
            raise AuditContractError(f"{reason} required count mismatch")

    return AuditAnchors(
        formations=formations,
        expected_counts=expected,
        tail_tickers=tuple(sorted(tickers)),
    )
```

Do not add CLI orchestration in this task; the module only exposes the input
contract used by the tests.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same targeted command. Expected: the three anchor tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  tests/test_b3_impact_audit.py
git commit -m "test: define B3 impact audit anchors"
```

## Task 2: Rebuild B3 size-exclusion detail exactly

**Files:**

- Modify: `tests/test_b3_impact_audit.py`
- Modify:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`

- [ ] **Step 1: Add failing tests for B3 precedence and PIT shares**

Build a three-ticker, two-formation synthetic fixture:

- `NEW.SZ`: listing age below 180 days, also missing close and shares.
- `CLOSE.SZ`: old listing, missing close and shares.
- `SHARE.SZ`: old listing, valid close, no share node known at formation.
- `SUSP.SZ`: old listing, exact close missing, same-date suspension evidence,
  and usable carried close.

Add:

```python
def test_build_details_uses_b3_reason_precedence():
    share_detail, close_detail, _ = AUDIT.build_impact_details(
        **_classification_inputs()
    )

    assert set(close_detail["ts_code"]) == {"CLOSE.SZ"}
    assert set(share_detail["ts_code"]) == {"SHARE.SZ"}
    assert "NEW.SZ" not in set(close_detail["ts_code"])
    assert "NEW.SZ" not in set(share_detail["ts_code"])


def test_suspension_carry_eliminates_missing_close():
    _, close_detail, classified = AUDIT.build_impact_details(
        **_classification_inputs()
    )

    row = classified[classified["ts_code"].eq("SUSP.SZ")].iloc[0]
    assert row["close_source"] == "SUSPENDED_CARRY_FORWARD"
    assert row["size_reason"] == ""
    assert "SUSP.SZ" not in set(close_detail["ts_code"])


def test_share_asof_filters_known_date_before_latest_effective_date():
    inputs = _classification_inputs()
    inputs["shares"] = pd.DataFrame(
        {
            "ts_code": ["SHARE.SZ", "SHARE.SZ"],
            "end_date": ["2020-12-31", "2021-12-31"],
            "known_date": ["2021-01-15", "2021-05-01"],
            "total_shares": [100.0, 200.0],
        }
    )

    share_detail, _, classified = AUDIT.build_impact_details(**inputs)

    assert share_detail.empty
    row = classified[classified["ts_code"].eq("SHARE.SZ")].iloc[0]
    assert row["selected_total_shares"] == 100.0
    assert pd.Timestamp(row["selected_share_effective_date"]) == pd.Timestamp(
        "2020-12-31"
    )
```

- [ ] **Step 2: Run targeted tests and verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  -k 'precedence or suspension or share_asof' -q
```

Expected: fail because `build_impact_details` does not exist.

- [ ] **Step 3: Implement the minimal pure classification engine**

Add strict frame validators and these pure helpers:

```python
MIN_LISTED_DAYS = 180
EXCLUDED_SUFFIXES = (".BJ", ".HK")


def _share_asof(shares: pd.DataFrame, formation: pd.Timestamp) -> pd.DataFrame:
    known = shares[shares["known_date"].le(formation)]
    if known.empty:
        return known
    return (
        known.sort_values(["ts_code", "end_date"], kind="mergesort")
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
    # Normalize dates and reject conflicting duplicate keys first.
    # Build one active ticker × formation row at a time to stay memory-bounded.
    classified_parts = []
    for formation_row in formations.itertuples(index=False):
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
                suspensions["formation_date"].eq(formation), "ticker"
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
            & pd.to_numeric(active["carry_close"], errors="coerce").notna()
        )
        active["close"] = pd.to_numeric(
            active["raw_close"], errors="coerce"
        )
        active.loc[active["usable_carry"], "close"] = active.loc[
            active["usable_carry"], "carry_close"
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
            active["list_date"].isna(), "size_reason"
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
        classified_parts.append(active)

    classified = pd.concat(classified_parts, ignore_index=True)
    shares_detail = classified[
        classified["size_reason"].eq("DATA_MISSING_SHARES")
        & classified["ticker"].isin(tail_tickers)
    ].copy()
    close_detail = classified[
        classified["size_reason"].eq("DATA_MISSING_CLOSE")
    ].copy()
    return shares_detail, close_detail, classified
```

At the public return boundary, rename `ticker` to `ts_code` in all three frames
and reindex the two detail frames to the exact column sets specified by the
design. Rows with `DATA_MISSING_LIST_DATE` remain only in `classified`.

- [ ] **Step 4: Run targeted and existing B3 tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py tests/test_b3_exposures.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  tests/test_b3_impact_audit.py
git commit -m "feat: rebuild B3 size gap detail"
```

## Task 3: CLOSE evidence buckets and priority summaries

**Files:**

- Modify: `tests/test_b3_impact_audit.py`
- Modify:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`

- [ ] **Step 1: Add failing evidence and summary tests**

Add one row for each direct evidence bucket and summary tests:

```python
@pytest.mark.parametrize(
    ("raw_present", "raw_close", "suspended", "carry", "delist", "next_close", "expected"),
    [
        (True, None, False, None, None, None, "EXACT_ROW_NULL_CLOSE"),
        (False, None, True, None, None, None, "SUSPENSION_WITHOUT_USABLE_CARRY"),
        (False, None, False, None, "2021-04-30", None, "POSSIBLE_DELIST_BOUNDARY"),
        (False, None, False, None, None, 10.0, "UNEXPLAINED_EXACT_DATE_GAP"),
    ],
)
def test_close_evidence_bucket_is_observation_only(
    raw_present, raw_close, suspended, carry, delist, next_close, expected
):
    row = pd.Series(
        {
            "raw_price_row_present": raw_present,
            "raw_close": raw_close,
            "suspension_evidence": suspended,
            "carry_close": carry,
            "delist_date": delist,
            "next_nonnull_close": next_close,
        }
    )
    assert AUDIT.close_evidence_bucket(row) == expected


def test_summarize_impacts_prioritizes_active_2023_names():
    detail, pool = _summary_inputs()

    got = AUDIT.summarize_impacts(detail, pool)

    assert list(got["ts_code"]) == ["ACTIVE.SZ", "OLD.SZ"]
    assert got.loc[0, "priority_rank"] == 1
    assert got.loc[0, "in_pool_2023_12"]
    assert got.loc[0, "affected_months_2023"] == 2
    assert got.loc[1, "listing_status_at_data_end"] == "DELISTED"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  -k 'evidence_bucket or summarizes' -q
```

Expected: fail because the two functions do not exist.

- [ ] **Step 3: Implement evidence enrichment and stable summaries**

Add:

```python
def close_evidence_bucket(row: pd.Series) -> str:
    if bool(row["raw_price_row_present"]) and pd.isna(row["raw_close"]):
        return "EXACT_ROW_NULL_CLOSE"
    if bool(row["suspension_evidence"]) and pd.isna(row["carry_close"]):
        return "SUSPENSION_WITHOUT_USABLE_CARRY"
    if pd.notna(row["delist_date"]) and pd.isna(
        row["next_nonnull_close"]
    ):
        return "POSSIBLE_DELIST_BOUNDARY"
    return "UNEXPLAINED_EXACT_DATE_GAP"


def summarize_impacts(
    detail: pd.DataFrame,
    pool_membership: pd.DataFrame,
    *,
    bucket_column: str | None = None,
) -> pd.DataFrame:
    grouped = detail.groupby("ts_code", sort=True)
    out = grouped.agg(
        list_date=("list_date", "first"),
        delist_date=("delist_date", "first"),
        affected_months_all=("formation_date", "size"),
        affected_months_required=("required_formation", "sum"),
        first_affected_formation=("formation_date", "min"),
        last_affected_formation=("formation_date", "max"),
    ).reset_index()
    affected_2023 = (
        detail[pd.to_datetime(detail["formation_date"]).dt.year.eq(2023)]
        .groupby("ts_code")
        .size()
    )
    out["affected_months_2023"] = (
        out["ts_code"].map(affected_2023).fillna(0).astype(int)
    )
    out = out.merge(pool_membership, on="ts_code", how="left")
    out["listing_status_at_data_end"] = np.where(
        out["delist_date"].notna()
        & out["delist_date"].lt(pool_membership.attrs["data_end"]),
        "DELISTED",
        "ACTIVE",
    )
    if bucket_column is not None:
        bucket_counts = pd.crosstab(
            detail["ts_code"], detail[bucket_column]
        )
        out = out.merge(
            bucket_counts,
            left_on="ts_code",
            right_index=True,
            how="left",
        )
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
```

Add `build_pool_membership(classified, formations)` to calculate
`in_pool_2023_any` and `in_pool_2023_12` from listing-age-eligible pool rows,
not from impact rows. Enrich CLOSE details with the previous/next close frame,
direct boolean evidence flags, and `evidence_bucket`. Merge tail diagnostics
onto the 57-row share summary without altering order.

- [ ] **Step 4: Run all audit tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  tests/test_b3_impact_audit.py
git commit -m "feat: summarize B3 tail impact evidence"
```

## Task 4: Read-only PostgreSQL adapter, reconciliation, and atomic publication

**Files:**

- Modify: `tests/test_b3_impact_audit.py`
- Modify:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`

- [ ] **Step 1: Add failing safety, reconciliation, and publication tests**

Use fake connection/cursor objects and `tmp_path`:

```python
def test_connect_marks_transaction_read_only(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(AUDIT.psycopg2, "connect", lambda **kwargs: fake)

    conn = AUDIT.connect_read_only(_db_config())

    assert conn is fake
    assert fake.readonly is True


def test_reconcile_rejects_one_month_mismatch():
    anchors = AUDIT.validate_anchors(_tail_frame(57), _coverage_frame())
    shares, closes = _matching_details(anchors)
    shares = shares.iloc[1:].copy()

    with pytest.raises(AUDIT.AuditContractError, match="monthly"):
        AUDIT.reconcile_details(shares, closes, anchors)


def test_publish_outputs_leaves_no_formal_files_on_validation_failure(tmp_path):
    artifacts = _artifact_frames()
    artifacts["shares_tail_impact_detail"] = artifacts[
        "shares_tail_impact_detail"
    ].iloc[1:]

    with pytest.raises(AUDIT.AuditContractError):
        AUDIT.publish_outputs(tmp_path, artifacts, _manifest_inputs())

    assert not list(tmp_path.glob("*impact*.csv"))
    assert not (tmp_path / "impact_audit_manifest.json").exists()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  -k 'read_only or reconcile or publish' -q
```

Expected: fail because adapter/reconciliation/publication functions do not
exist.

- [ ] **Step 3: Implement bounded SELECT queries**

Import `psycopg2` and `yaml` lazily or at module scope, validate schema with
`^[A-Za-z_][A-Za-z0-9_]*$`, and use one transaction:

```python
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
```

Implement separate fetchers with parameterized dates/tickers:

```sql
SELECT ts_code AS ticker, list_date, delist_date
FROM {schema}.stock_meta
ORDER BY ts_code
```

```sql
SELECT ts_code AS ticker, trade_date AS formation_date, close AS raw_close,
       TRUE AS raw_price_row_present
FROM {schema}.stock_daily_price
WHERE trade_date = ANY(%s)
ORDER BY trade_date, ts_code
```

```sql
SELECT ts_code, effective_date AS end_date,
       COALESCE(available_date, effective_date) AS known_date, total_shares
FROM {schema}.stock_share_capital
WHERE total_shares IS NOT NULL AND total_shares > 0
ORDER BY ts_code, effective_date
```

Use the exact B3 same-date suspension and lateral carried-close SQL. After
identifying the 202 holes, query previous/next non-null close with a parameterized
`VALUES` relation; do not interpolate ticker/date values into SQL.

- [ ] **Step 4: Implement exact reconciliation and staged publication**

`reconcile_details` must group each detail frame by
`formation_date,required_formation`, reindex to the full formation grid with
zero, and compare every row to `anchors.expected_counts`. It must also enforce
SHARES detail tickers as a subset of the 57-tail anchor, a fixed 57-row summary, and the exact all/required totals.

`publish_outputs` must:

1. validate all artifact columns and counts;
2. write four CSVs plus manifest into a unique staging directory under the
   output directory;
3. compute SHA-256 and row counts from staged files;
4. write the manifest without secrets;
5. replace formal files only after every staged artifact is complete;
6. remove the staging directory in `finally`.

Use `Path.replace`/`os.replace`, not shell moves.

- [ ] **Step 5: Add and test CLI orchestration**

Implement:

```text
--tail
--coverage-audit
--settings
--output-dir
--expected-tail-sha256
--expected-coverage-sha256
```

Defaults point to the repository paths and immutable final hashes. `main()`:

1. checks both hashes before connecting;
2. loads/validates anchors;
3. opens one read-only transaction;
4. fetches minimal source frames;
5. builds detail, then CLOSE neighbor evidence and summaries;
6. reconciles;
7. publishes;
8. rolls back/closes in `finally`;
9. prints bounded row-count/hash summaries only.

Add a CLI test that monkeypatches the read adapter and proves no mutating SQL is
issued.

- [ ] **Step 6: Run targeted and B3 tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  tests/test_b3_exposures.py \
  tests/test_verify_par_recovery.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  tests/test_b3_impact_audit.py
git commit -m "feat: publish read-only B3 impact audit"
```

## Task 5: Run against final evidence and record artifacts

**Files:**

- Generate the four CSVs and manifest listed in the file map.
- Modify:
  `data_fixes/2026-07-25-share-capital-par/README.md`

- [ ] **Step 1: Probe database connectivity once**

Use the configured connection with an 8-second timeout and execute only
`SELECT 1`. If it fails, stop and report the environmental blocker; do not
retry in a loop.

- [ ] **Step 2: Run the audit with explicit final input paths**

The linked worktree does not contain gitignored runtime/config files, so pass
the canonical checkout paths explicitly:

```bash
/home/elfbob/miniconda3/bin/python \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  --tail data_fixes/2026-07-25-share-capital-par/tail.csv \
  --coverage-audit \
    /home/elfbob/claude-code/style_timing_signal/output/style_basket/b3/coverage_audit.csv \
  --settings \
    /home/elfbob/claude-code/style_timing_signal/config/settings.yaml \
  --output-dir data_fixes/2026-07-25-share-capital-par
```

Expected bounded summary:

```text
DATA_MISSING_SHARES: 5781 all / 5445 required / 56 tickers
DATA_MISSING_CLOSE: 202 all / 190 required / <derived> tickers
monthly reconciliation: OK
```

- [ ] **Step 3: Independently validate artifacts**

Run a separate read-only pandas command that checks:

- four CSV row counts;
- 57 share summary rows;
- 5,781/5,445 share detail counts;
- 202/190 close detail counts;
- unique priority ranks;
- no blank `reason_code` or `evidence_bucket`;
- manifest hashes match files;
- top priority rows are sorted by the documented key.

Also run:

```bash
sha256sum \
  data_fixes/2026-07-25-share-capital-par/*impact*.csv \
  data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json
```

- [ ] **Step 4: Update README with facts, not policy decisions**

Append a “2026-07-27 逐票影响审计” section recording:

- exact command and input hashes;
- row counts and unique ticker counts;
- artifact hashes;
- top active/high-impact share tail names;
- CLOSE evidence-bucket distribution;
- explicit statement that evidence buckets are not final exemptions/fixes;
- next action: Wind fact retrieval for active share names and row-by-row CLOSE
  adjudication.

- [ ] **Step 5: Run audit tests after documentation/artifact generation**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  tests/test_b3_exposures.py \
  tests/test_verify_par_recovery.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/README.md \
  data_fixes/2026-07-25-share-capital-par/*impact*.csv \
  data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json
git commit -m "data: record B3 tail impact audit"
```

## Task 6: Final verification and handoff

**Files:**

- No expected modifications.

- [ ] **Step 1: Use verification-before-completion**

Read and follow
`/home/elfbob/.codex/skills/verification-before-completion/SKILL.md`.

- [ ] **Step 2: Run formatting/diff checks**

```bash
git diff main...HEAD --check
git status --short --branch
```

Expected: no whitespace errors; only intended branch commits/files.

- [ ] **Step 3: Run the complete test suite**

```bash
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected baseline-equivalent or better result: at least the existing 813 tests
plus the new audit tests, all passing.

- [ ] **Step 4: Inspect bounded final diff and commit list**

```bash
git log --oneline main..HEAD
git diff --stat main...HEAD
git status --short --branch
```

- [ ] **Step 5: Use finishing-a-development-branch**

Read and follow
`/home/elfbob/.codex/skills/finishing-a-development-branch/SKILL.md`, then
present integration choices without pushing or merging unless the user
explicitly selects one.
