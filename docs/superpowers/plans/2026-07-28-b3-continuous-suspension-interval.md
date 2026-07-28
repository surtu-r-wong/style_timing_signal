# B3 Continuous Suspension Interval Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace B3's exact-date-only suspension carry limitation with a
formation-date-causal continuous-suspension evidence path, reducing the anchored
CLOSE blockers from 202/190 to 4/0 without changing signal or performance
assumptions.

**Architecture:** Put candidate construction and interval classification in a
new pure DataFrame module. `b3_build.py` performs bounded, read-only source
queries for candidate tickers, merges exact and interval carries with explicit
precedence, and publishes a hash-bound evidence CSV. The existing impact audit
consumes that exact evidence artifact rather than reimplementing classification.

**Tech Stack:** Python 3.13, pandas, NumPy, PostgreSQL/psycopg2, pytest,
CSV/JSON, SHA-256, systemd-run memory scopes.

**Approved spec:** `docs/superpowers/specs/2026-07-28-b3-continuous-suspension-interval-design.md`

---

## Execution constraints

- At execution time, first use `superpowers:using-git-worktrees` and create an
  isolated worktree on branch `audit/b3-suspension-interval` from `main`.
- Preserve the caller's existing untracked `backtest/output/b3/` and
  `data_fixes/2026-07-24-stock-financial-ann-date/`; never add them.
- Use TDD for every behavior change: red test, observed failure, minimal code,
  observed pass, then commit.
- Database access is read-only. Do not call Wind, write Market Monitor tables,
  run prod, run `--stage all`, or run formal B3 eval.
- The only real-data B3 command in this plan is
  `--stage preflight --data-end 2023-12-31`, under `MemoryMax=8G`.
- The expected preflight process exit code is `2` while required SHARES blockers
  remain. Success for this task means zero required CLOSE blockers, not an
  overall `OK` preflight.

## File map

- Create `signals/style_basket/b3_suspension.py`: candidate construction,
  interval classification, stable schemas, and structural validation.
- Create `tests/test_b3_suspension.py`: pure-function contract tests.
- Modify `signals/style_basket/b3_build.py`: bounded source loading, exact versus
  interval carry integration, coverage rows, evidence publication, manifest
  hashing, and default-source caching.
- Modify `tests/test_b3_exposures.py`: loader, snapshot, coverage, publication,
  and builder parent-manifest tests.
- Modify `backtest/b3_eval.py`: make the evidence CSV a required preflight input
  and reject partial or extra output sets.
- Modify `tests/test_b3_eval.py`: eval-side preflight trust tests.
- Modify
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`: consume
  the published interval evidence, reconstruct post-fix size reasons, and bind
  the evidence hash into the audit manifest.
- Modify `tests/test_b3_impact_audit.py`: post-fix reconciliation, schema,
  duplicate, input-hash, and publication tests.
- Regenerate the four tracked impact CSVs and
  `data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json`.
- Modify `data_fixes/2026-07-25-share-capital-par/README.md`: document the
  post-fix 4/0 CLOSE tail and the exact read-only reproduction command.

### Task 1: Define and test the missing-CLOSE candidate contract

**Files:**

- Create: `signals/style_basket/b3_suspension.py`
- Create: `tests/test_b3_suspension.py`

- [ ] **Step 1: Write failing candidate-selection tests**

Create `tests/test_b3_suspension.py` with imports and a compact fixture proving
the candidate set follows the existing B3 reason precedence:

```python
import numpy as np
import pandas as pd
import pytest

from signals.style_basket.b3_suspension import (
    SuspensionEvidenceError,
    build_missing_close_candidates,
)


def test_candidates_exclude_young_markets_and_usable_exact_carry():
    formation = pd.Timestamp("2021-01-29")
    formations = pd.DataFrame({"formation_date": [formation]})
    meta = pd.DataFrame(
        {
            "ts_code": ["A.SZ", "B.SZ", "C.SZ", "D.BJ", "E.SZ"],
            "list_date": [
                "2010-01-01",
                "2020-12-01",
                "2010-01-01",
                "2010-01-01",
                "2010-01-01",
            ],
            "delist_date": [None, None, None, None, "2020-12-31"],
        }
    )
    exact_closes = pd.DataFrame(
        {
            "ts_code": ["A.SZ", "C.SZ"],
            "formation_date": [formation, formation],
            "close": [np.nan, np.nan],
        }
    )
    exact_suspensions = pd.DataFrame(
        {"ts_code": ["C.SZ"], "formation_date": [formation]}
    )
    exact_carries = pd.DataFrame(
        {
            "ts_code": ["C.SZ"],
            "formation_date": [formation],
            "close_date": ["2021-01-28"],
            "close": [8.5],
        }
    )

    got = build_missing_close_candidates(
        formations=formations,
        stock_meta=meta,
        exact_closes=exact_closes,
        exact_suspensions=exact_suspensions,
        exact_carries=exact_carries,
    )

    assert list(got["ts_code"]) == ["A.SZ"]
    assert got.loc[0, "formation_date"] == formation
    assert got.loc[0, "list_date"] == pd.Timestamp("2010-01-01")
    assert pd.isna(got.loc[0, "delist_date"])


def test_candidate_conflicting_source_key_is_structural_error():
    formation = pd.Timestamp("2021-01-29")
    closes = pd.DataFrame(
        {
            "ts_code": ["A.SZ", "A.SZ"],
            "formation_date": [formation, formation],
            "close": [10.0, 11.0],
        }
    )
    with pytest.raises(SuspensionEvidenceError, match="exact closes"):
        build_missing_close_candidates(
            formations=pd.DataFrame({"formation_date": [formation]}),
            stock_meta=pd.DataFrame(
                {
                    "ts_code": ["A.SZ"],
                    "list_date": ["2010-01-01"],
                    "delist_date": [None],
                }
            ),
            exact_closes=closes,
            exact_suspensions=pd.DataFrame(
                columns=["ts_code", "formation_date"]
            ),
            exact_carries=pd.DataFrame(
                columns=[
                    "ts_code",
                    "formation_date",
                    "close_date",
                    "close",
                ]
            ),
        )
```

- [ ] **Step 2: Run the tests and observe the import failure**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py -q
```

Expected: FAIL during collection because
`signals.style_basket.b3_suspension` does not exist.

- [ ] **Step 3: Implement the schema, structural validator, and candidate builder**

Create `signals/style_basket/b3_suspension.py` with these public names and
contracts:

```python
"""Point-in-time continuous-suspension evidence for B3 formation closes."""

from __future__ import annotations

import numpy as np
import pandas as pd


MIN_LISTED_DAYS = 180
EXCLUDED_MARKET_SUFFIXES = (".BJ", ".HK")
CANDIDATE_COLUMNS = (
    "ts_code",
    "formation_date",
    "list_date",
    "delist_date",
)


class SuspensionEvidenceError(RuntimeError):
    """Raised when source facts cannot produce a unique evidence result."""


def _require_columns(frame, required, label):
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise SuspensionEvidenceError(
            f"{label} missing columns: {missing}"
        )


def _deduplicate_or_raise(frame, keys, label):
    deduplicated = frame.drop_duplicates().copy()
    if deduplicated.duplicated(list(keys), keep=False).any():
        raise SuspensionEvidenceError(
            f"{label} contains conflicting duplicate keys"
        )
    return deduplicated.reset_index(drop=True)


def _dates(frame, columns, label, nullable=()):
    out = frame.copy()
    for column in columns:
        original = out[column]
        parsed = pd.to_datetime(original, errors="coerce")
        invalid = original.notna() & parsed.isna()
        if column not in nullable:
            invalid |= parsed.isna()
        if invalid.any():
            raise SuspensionEvidenceError(
                f"{label}.{column} contains invalid dates"
            )
        out[column] = parsed
    return out


def build_missing_close_candidates(
    *,
    formations,
    stock_meta,
    exact_closes,
    exact_suspensions,
    exact_carries,
):
    _require_columns(
        formations, {"formation_date"}, "formations"
    )
    _require_columns(
        stock_meta,
        {"ts_code", "list_date", "delist_date"},
        "stock metadata",
    )
    _require_columns(
        exact_closes,
        {"ts_code", "formation_date", "close"},
        "exact closes",
    )
    _require_columns(
        exact_suspensions,
        {"ts_code", "formation_date"},
        "exact suspensions",
    )
    _require_columns(
        exact_carries,
        {"ts_code", "formation_date", "close_date", "close"},
        "exact carries",
    )

    formations = _dates(
        formations, ("formation_date",), "formations"
    )
    formations = _deduplicate_or_raise(
        formations, ("formation_date",), "formations"
    )
    meta = _dates(
        stock_meta,
        ("list_date", "delist_date"),
        "stock metadata",
        nullable=("list_date", "delist_date"),
    )
    meta = _deduplicate_or_raise(
        meta, ("ts_code",), "stock metadata"
    )
    closes = _dates(
        exact_closes,
        ("formation_date",),
        "exact closes",
    )
    closes = _deduplicate_or_raise(
        closes,
        ("ts_code", "formation_date"),
        "exact closes",
    )
    closes["close"] = pd.to_numeric(closes["close"], errors="coerce")
    suspensions = _dates(
        exact_suspensions,
        ("formation_date",),
        "exact suspensions",
    )
    suspensions = _deduplicate_or_raise(
        suspensions,
        ("ts_code", "formation_date"),
        "exact suspensions",
    )
    carries = _dates(
        exact_carries,
        ("formation_date", "close_date"),
        "exact carries",
        nullable=("close_date",),
    )
    carries = _deduplicate_or_raise(
        carries,
        ("ts_code", "formation_date"),
        "exact carries",
    )
    carries["close"] = pd.to_numeric(carries["close"], errors="coerce")

    close_map = closes.set_index(
        ["ts_code", "formation_date"]
    )["close"]
    suspended_keys = set(
        map(
            tuple,
            suspensions[["ts_code", "formation_date"]].itertuples(
                index=False, name=None
            ),
        )
    )
    carry_map = carries.set_index(
        ["ts_code", "formation_date"]
    )["close"]

    rows = []
    for formation in sorted(formations["formation_date"].unique()):
        formation = pd.Timestamp(formation)
        active = meta[
            meta["list_date"].notna()
            & ~meta["ts_code"].str.endswith(EXCLUDED_MARKET_SUFFIXES)
            & meta["list_date"].le(formation)
            & (
                meta["delist_date"].isna()
                | meta["delist_date"].ge(formation)
            )
            & (
                meta["list_date"]
                + pd.Timedelta(days=MIN_LISTED_DAYS)
                <= formation
            )
        ]
        for row in active.itertuples(index=False):
            key = (row.ts_code, formation)
            raw_close = close_map.get(key, np.nan)
            exact_usable = (
                key in suspended_keys
                and pd.notna(carry_map.get(key, np.nan))
            )
            if pd.isna(raw_close) and not exact_usable:
                rows.append(
                    {
                        "ts_code": row.ts_code,
                        "formation_date": formation,
                        "list_date": row.list_date,
                        "delist_date": row.delist_date,
                    }
                )
    return (
        pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
        .sort_values(
            ["formation_date", "ts_code"], kind="mergesort"
        )
        .reset_index(drop=True)
    )
```

Also reject blank/non-string ticker keys before suffix checks. Preserve the exact
path's existing `notna` semantics; do not add finite/positive validation to
exact carries in this function.

- [ ] **Step 4: Run candidate tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the candidate contract**

```bash
git add signals/style_basket/b3_suspension.py \
  tests/test_b3_suspension.py
git commit -m "feat(b3): define missing close candidates"
```

### Task 2: Classify continuous suspension intervals without future leakage

**Files:**

- Modify: `signals/style_basket/b3_suspension.py`
- Modify: `tests/test_b3_suspension.py`

- [ ] **Step 1: Add failing happy-path and calendar tests**

Append tests that build an explicit official calendar rather than inferring
weekdays:

```python
from signals.style_basket.b3_suspension import (
    build_continuous_suspension_evidence,
)


def _candidate(formation="2021-09-30"):
    return pd.DataFrame(
        {
            "ts_code": ["A.SZ"],
            "formation_date": [formation],
            "list_date": ["2010-01-01"],
            "delist_date": [None],
        }
    )


def _holiday_calendar():
    return pd.DataFrame(
        {
            "calendar_date": pd.to_datetime(
                [
                    "2021-09-17",
                    "2021-09-18",
                    "2021-09-19",
                    "2021-09-20",
                    "2021-09-21",
                    "2021-09-22",
                    "2021-09-30",
                ]
            ),
            "sfe": [True, False, False, False, False, True, True],
        }
    )


def interval_case(mutation):
    case = {
        "candidates": _candidate(),
        "trading_calendar": _holiday_calendar(),
        "prices": pd.DataFrame(
            {
                "ts_code": ["A.SZ", "A.SZ"],
                "trade_date": ["2021-09-17", "2021-10-08"],
                "close": [10.0, 10.5],
            }
        ),
        "suspension_events": pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": ["2021-09-22"],
                "suspend_type": ["今起停牌"],
                "suspend_reason": ["重大事项"],
            }
        ),
        "suspension_source_start": pd.Timestamp("2013-01-04"),
        "stock_status": pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": ["2021-09-30"],
                "is_suspended": [True],
            }
        ),
    }
    if mutation == "accepted":
        return case
    if mutation == "no_start":
        case["suspension_events"] = case["suspension_events"].iloc[0:0]
    elif mutation == "no_previous_close":
        case["prices"] = case["prices"].iloc[1:].reset_index(drop=True)
    elif mutation == "outside_listing":
        case["candidates"].loc[:, "list_date"] = "2022-01-01"
    elif mutation == "closed_day_start":
        case["suspension_events"].loc[:, "trade_date"] = "2021-09-18"
    elif mutation == "stale_previous_close":
        case["prices"].loc[0, "trade_date"] = "2021-09-16"
    elif mutation == "zero_previous_close":
        case["prices"].loc[0, "close"] = 0.0
    elif mutation == "trade_inside_interval":
        case["prices"] = pd.concat(
            [
                case["prices"],
                pd.DataFrame(
                    {
                        "ts_code": ["A.SZ"],
                        "trade_date": ["2021-09-23"],
                        "close": [10.1],
                    }
                ),
            ],
            ignore_index=True,
        )
    elif mutation == "precoverage_start":
        case = {
            "candidates": _candidate("2012-12-31"),
            "trading_calendar": pd.DataFrame(
                {
                    "calendar_date": pd.to_datetime(
                        ["2012-12-27", "2012-12-28", "2012-12-31"]
                    ),
                    "sfe": [True, True, True],
                }
            ),
            "prices": pd.DataFrame(
                {
                    "ts_code": ["A.SZ"],
                    "trade_date": ["2012-12-27"],
                    "close": [10.0],
                }
            ),
            "suspension_events": pd.DataFrame(
                columns=[
                    "ts_code",
                    "trade_date",
                    "suspend_type",
                    "suspend_reason",
                ]
            ),
            "suspension_source_start": pd.Timestamp("2013-01-04"),
            "stock_status": pd.DataFrame(
                columns=["ts_code", "trade_date", "is_suspended"]
            ),
        }
        case["candidates"].loc[:, "list_date"] = "2000-01-01"
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return case


def test_holiday_gap_accepts_next_official_day_start():
    got = build_continuous_suspension_evidence(
        candidates=_candidate(),
        trading_calendar=_holiday_calendar(),
        prices=pd.DataFrame(
            {
                "ts_code": ["A.SZ", "A.SZ"],
                "trade_date": ["2021-09-17", "2021-10-08"],
                "close": [10.0, 10.5],
            }
        ),
        suspension_events=pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": ["2021-09-22"],
                "suspend_type": ["今起停牌"],
                "suspend_reason": ["重大事项"],
            }
        ),
        suspension_source_start=pd.Timestamp("2013-01-04"),
        stock_status=pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": ["2021-09-30"],
                "is_suspended": [True],
            }
        ),
    )

    row = got.iloc[0]
    assert bool(row["accepted"]) is True
    assert row["evidence_method"] == (
        "CONTINUOUS_SUSPENSION_INTERVAL"
    )
    assert row["previous_close_date"] == pd.Timestamp("2021-09-17")
    assert row["previous_close"] == pytest.approx(10.0)
    assert row["next_trade_date"] == pd.Timestamp("2021-10-08")
    assert bool(row["exact_stock_status_confirmed"]) is True
```

- [ ] **Step 2: Add failing fail-closed, conflict, and causality tests**

Add a parameterized rejection test for all stable codes:

```python
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("no_start", "NO_EXPLICIT_SUSPENSION_START"),
        ("no_previous_close", "INVALID_PREVIOUS_CLOSE"),
        ("outside_listing", "OUTSIDE_LEGAL_LISTING_INTERVAL"),
        ("closed_day_start", "START_NOT_OFFICIAL_TRADING_DAY"),
        (
            "stale_previous_close",
            "PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY",
        ),
        ("zero_previous_close", "INVALID_PREVIOUS_CLOSE"),
        ("trade_inside_interval", "PRICE_OBSERVED_DURING_INTERVAL"),
        (
            "precoverage_start",
            "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE",
        ),
    ],
)
def test_interval_rejections_are_stable(mutation, expected):
    case = interval_case(mutation)
    got = build_continuous_suspension_evidence(**case)
    assert bool(got.loc[0, "accepted"]) is False
    assert got.loc[0, "evidence_method"] == ""
    assert got.loc[0, "rejection_reason"] == expected


@pytest.mark.parametrize("bad_close", [0.0, -1.0, np.nan, np.inf])
def test_previous_close_must_be_finite_and_positive(bad_close):
    case = interval_case("accepted")
    case["prices"].loc[0, "close"] = bad_close
    got = build_continuous_suspension_evidence(**case)
    assert bool(got.loc[0, "accepted"]) is False
    assert got.loc[0, "rejection_reason"] == "INVALID_PREVIOUS_CLOSE"


def test_overlapping_unended_starts_are_structural_error():
    case = interval_case("accepted")
    case["suspension_events"] = pd.concat(
        [
            case["suspension_events"],
            case["suspension_events"].assign(
                trade_date="2021-09-23"
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(SuspensionEvidenceError, match="overlapping"):
        build_continuous_suspension_evidence(**case)


def test_post_formation_facts_cannot_change_decision():
    case = interval_case("accepted")
    first = build_continuous_suspension_evidence(**case)
    case["prices"] = pd.concat(
        [
            case["prices"],
            pd.DataFrame(
                {
                    "ts_code": ["A.SZ"],
                    "trade_date": ["2022-08-22"],
                    "close": [19.0],
                }
            ),
        ],
        ignore_index=True,
    )
    second = build_continuous_suspension_evidence(**case)
    compared = [
        "accepted",
        "rejection_reason",
        "evidence_method",
        "suspension_start",
        "previous_close_date",
        "previous_close",
    ]
    pd.testing.assert_series_equal(
        first.loc[0, compared],
        second.loc[0, compared],
        check_names=False,
    )


def test_stock_status_is_report_only():
    false_case = interval_case("accepted")
    false_case["stock_status"]["is_suspended"] = False
    true_case = interval_case("accepted")
    true_case["stock_status"]["is_suspended"] = True
    rejected_confirmation = build_continuous_suspension_evidence(
        **false_case
    )
    confirmed = build_continuous_suspension_evidence(**true_case)
    assert bool(rejected_confirmation.loc[0, "accepted"]) is True
    assert bool(confirmed.loc[0, "accepted"]) is True
    assert (
        bool(rejected_confirmation.loc[0, "exact_stock_status_confirmed"])
        is False
    )
    assert bool(confirmed.loc[0, "exact_stock_status_confirmed"]) is True
```

- [ ] **Step 3: Run the new classifier tests and observe missing API failures**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py -q
```

Expected: FAIL because `build_continuous_suspension_evidence` and the evidence
schema are not implemented.

- [ ] **Step 4: Implement the classifier with a formation-date cutoff**

Add these constants and public signature:

```python
CORE_EVIDENCE_COLUMNS = (
    "ts_code",
    "formation_date",
    "list_date",
    "delist_date",
    "suspension_start",
    "previous_official_trade_date",
    "previous_close_date",
    "previous_close",
    "suspend_type",
    "suspend_reason",
    "evidence_method",
    "accepted",
    "rejection_reason",
    "next_trade_date",
    "next_nonnull_close",
    "exact_stock_status_confirmed",
)
INTERVAL_METHOD = "CONTINUOUS_SUSPENSION_INTERVAL"


def empty_interval_evidence():
    return pd.DataFrame(columns=CORE_EVIDENCE_COLUMNS)


def build_continuous_suspension_evidence(
    *,
    candidates,
    trading_calendar,
    prices,
    suspension_events,
    suspension_source_start,
    stock_status=None,
):
    """Return one deterministic evidence row per missing-close candidate."""
```

Normalize and strictly deduplicate:

- candidates on `(ts_code, formation_date)`;
- prices on `(ts_code, trade_date)`;
- relevant `今起停牌` rows on `(ts_code, trade_date)`;
- status on `(ts_code, trade_date)`;
- calendar on `calendar_date`.

Define the official-date helper before the loop:

```python
def next_official_date(official_dates, after):
    later = official_dates[official_dates > pd.Timestamp(after)]
    return pd.NaT if later.empty else pd.Timestamp(later.min())
```

Build `official_dates` only from rows where the validated `sfe` value is true.
For each candidate, use this exact order:

```python
known_events = ticker_events[
    ticker_events["trade_date"].le(formation_date)
].sort_values("trade_date", kind="mergesort")
known_prices = ticker_prices[
    ticker_prices["trade_date"].le(formation_date)
].sort_values("trade_date", kind="mergesort")
future_prices = ticker_prices[
    ticker_prices["trade_date"].gt(formation_date)
].sort_values("trade_date", kind="mergesort")
next_price = future_prices[
    future_prices["close"].notna()
].head(1)

if known_events.empty:
    previous = known_prices[known_prices["close"].notna()].tail(1)
    if previous.empty:
        expected_start = pd.NaT
        rejection = "INVALID_PREVIOUS_CLOSE"
    else:
        expected_start = next_official_date(
            official_dates, previous["trade_date"].iloc[0]
        )
        rejection = (
            "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE"
            if expected_start < suspension_source_start
            else "NO_EXPLICIT_SUSPENSION_START"
        )
else:
    selected_start = known_events.iloc[-1]
    earlier_prices = known_prices[
        known_prices["trade_date"].lt(selected_start["trade_date"])
        & known_prices["close"].notna()
    ]
    previous = earlier_prices.tail(1)
    if previous.empty:
        previous_date = pd.NaT
    else:
        previous_date = pd.Timestamp(
            previous["trade_date"].iloc[0]
        )
    starts_after_previous = known_events[
        known_events["trade_date"].gt(previous_date)
    ]
    if len(starts_after_previous) > 1:
        raise SuspensionEvidenceError(
            "overlapping unended suspension starts"
        )
```

Then apply, in order:

1. `OUTSIDE_LEGAL_LISTING_INTERVAL`;
2. `START_NOT_OFFICIAL_TRADING_DAY`;
3. `INVALID_PREVIOUS_CLOSE`;
4. `PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY`;
5. `PRICE_OBSERVED_DURING_INTERVAL`;
6. accepted with `INTERVAL_METHOD`.

`previous_official_trade_date` is the greatest official date strictly before
the start. Previous CLOSE must be finite and greater than zero. Any non-null
price row from the start through formation produces
`PRICE_OBSERVED_DURING_INTERVAL`.

Populate `next_trade_date`, `next_nonnull_close`, and exact formation-date
status only after the decision has been calculated. Never branch on those
values. Return exactly `CORE_EVIDENCE_COLUMNS`, sorted by
`formation_date, ts_code`.

- [ ] **Step 5: Add row-order and duplicate regression tests**

Add tests proving shuffled inputs produce the same output, identical duplicate
rows are collapsed, and value-conflicting price/event/status duplicates raise
`SuspensionEvidenceError`.

- [ ] **Step 6: Run the complete pure module tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py -q -W error
```

Expected: PASS with no warnings.

- [ ] **Step 7: Commit interval classification**

```bash
git add signals/style_basket/b3_suspension.py \
  tests/test_b3_suspension.py
git commit -m "feat(b3): classify continuous suspension intervals"
```

### Task 3: Load bounded historical evidence and cache it in default sources

**Files:**

- Modify: `signals/style_basket/b3_build.py:998-1327`
- Modify: `signals/style_basket/b3_build.py:1899-1988`
- Modify: `tests/test_b3_exposures.py:2460-3060`

- [ ] **Step 1: Write failing bounded-query tests**

Extend the `_formation_inputs` SQL fixture so one mature active ticker has a
missing formation close and no exact carry. Add assertions that:

- interval price and event queries receive only that candidate ticker;
- every interval query is bounded by `data_end`;
- event rows include `suspend_type` and `suspend_reason`;
- the global suspension source-start query is recorded;
- status reads only candidate ticker/formation coordinates;
- the returned input mapping contains `interval_evidence` and
  `interval_carried_closes`;
- changing a future resume date changes only report columns.

Use a spy with this shape:

```python
calls = []


def recording_read_sql(db, sql, params=None):
    calls.append((sql, params))
    return interval_sql_frame(sql)


monkeypatch.setattr(
    "signals.style_basket.b3_build._read_sql",
    recording_read_sql,
)
```

The accepted carried frame must equal:

```python
assert got["interval_carried_closes"].to_dict("records") == [
    {
        "formation_date": pd.Timestamp("2021-01-29"),
        "ts_code": "A",
        "close_date": pd.Timestamp("2021-01-28"),
        "close": 9.5,
    }
]
```

- [ ] **Step 2: Run loader tests and observe missing-query failures**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  -k "formation_inputs and suspension" -q
```

Expected: FAIL because `_formation_inputs` does not build interval inputs.

- [ ] **Step 3: Add a bounded interval-history loader**

In `b3_build.py`, add
`_fetch_suspension_interval_history(db, candidates, data_end, recorder)`.
For an empty candidate set return fixed empty frames without querying.
Otherwise issue only these read-only query shapes:

```sql
SELECT ts_code, trade_date, close
FROM {schema}.stock_daily_price
WHERE ts_code = ANY(%(tickers)s)
  AND trade_date <= %(end)s
ORDER BY ts_code, trade_date
```

```sql
SELECT ts_code, trade_date, suspend_type, suspend_reason
FROM {schema}.stock_suspension
WHERE ts_code = ANY(%(tickers)s)
  AND trade_date <= %(end)s
ORDER BY ts_code, trade_date, suspend_type, suspend_reason
```

```sql
SELECT MIN(trade_date) AS source_start
FROM {schema}.stock_suspension
```

```sql
SELECT ts_code, trade_date, is_suspended
FROM {schema}.stock_status
WHERE ts_code = ANY(%(tickers)s)
  AND trade_date = ANY(%(dates)s)
ORDER BY ts_code, trade_date
```

Use sorted unique ticker/date parameters. Record the queries under distinct
logical recorder names so they do not conflict with existing template hashes:

```python
f"{schema}.stock_daily_price_interval_history"
f"{schema}.stock_suspension_interval_history"
f"{schema}.stock_suspension_source_coverage"
f"{schema}.stock_status_interval_confirmation"
```

- [ ] **Step 4: Build candidates and evidence inside `_formation_inputs`**

After the existing exact suspension/carry reads:

```python
try:
    candidates = build_missing_close_candidates(
        formations=pd.DataFrame({"formation_date": month_ends}),
        stock_meta=meta.rename(columns={"ticker": "ts_code"}),
        exact_closes=closes.rename(
            columns={
                "ticker": "ts_code",
                "trade_date": "formation_date",
            }
        ),
        exact_suspensions=suspensions.rename(
            columns={"trade_date": "formation_date"}
        ),
        exact_carries=carried_closes.rename(
            columns={
                "close_date": "close_date",
                "formation_date": "formation_date",
            }
        ),
    )
    history = _fetch_suspension_interval_history(
        db, candidates, data_end, recorder
    )
    interval_evidence = build_continuous_suspension_evidence(
        candidates=candidates,
        trading_calendar=authoritative,
        prices=history["prices"],
        suspension_events=history["events"],
        suspension_source_start=history["source_start"],
        stock_status=history["status"],
    )
except SuspensionEvidenceError as exc:
    raise DataBlocked(
        f"continuous suspension evidence invalid: {exc}"
    ) from exc

accepted = interval_evidence[
    interval_evidence["accepted"].astype(bool)
]
interval_carried_closes = accepted.rename(
    columns={
        "previous_close_date": "close_date",
        "previous_close": "close",
    }
)[["formation_date", "ts_code", "close_date", "close"]]
```

Return both frames from `_formation_inputs`.

- [ ] **Step 5: Expose cached evidence without triggering new reads**

Add a final optional `B3Sources` field:

```python
suspension_interval_evidence: (
    Callable[[pd.Timestamp], pd.DataFrame] | None
) = None
```

In `default_sources`, pass `interval_carried_closes` to snapshot construction
and add:

```python
def suspension_interval_evidence(data_end):
    key = str(pd.Timestamp(data_end).date())
    source = cached_inputs.get(key)
    if source is None:
        return empty_interval_evidence()
    return source["interval_evidence"].copy()
```

The callback must not call `inputs(data_end)`: an early constituent blocker must
not trigger the expensive formation query merely to write an empty report.

- [ ] **Step 6: Run loader and database-evidence tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  -k "formation_inputs or database_evidence" -q -W error
```

Expected: PASS.

- [ ] **Step 7: Commit bounded loading**

```bash
git add signals/style_basket/b3_build.py \
  tests/test_b3_exposures.py
git commit -m "feat(b3): load suspension interval evidence"
```

### Task 4: Merge exact and interval carries and split coverage reporting

**Files:**

- Modify: `signals/style_basket/b3_build.py:1330-1900`
- Modify: `signals/style_basket/b3_build.py:2080-2130`
- Modify: `tests/test_b3_exposures.py:2880-3040`

- [ ] **Step 1: Write failing snapshot precedence tests**

Extend the existing suspension snapshot test with interval rows for tickers
`D`, `F`, and an overlap on `C`:

```python
inputs["interval_carried_closes"] = pd.DataFrame(
    {
        "formation_date": [formation, formation],
        "ts_code": ["C", "D"],
        "close_date": [
            formation - pd.Timedelta(days=45),
            formation - pd.Timedelta(days=1),
        ],
        "close": [12.5, 7.0],
    }
)
```

Assert:

```python
assert snap.loc["C", "close_carry_method"] == "EXACT_SUSPENSION"
assert snap.loc["D", "close_carry_method"] == (
    "CONTINUOUS_SUSPENSION_INTERVAL"
)
assert bool(snap.loc["C", "close_carried"]) is True
assert bool(snap.loc["D", "close_carried"]) is True
assert snap.loc["D", "total_market_value"] == pytest.approx(7.0 * 400.0)
```

Add a second test changing overlapping `C` interval close to `13.0` and assert
`DataBlocked` with `conflicting exact and interval carry`.

- [ ] **Step 2: Run the focused tests and observe the missing parameter**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  -k "suspension and snapshot" -q
```

Expected: FAIL because `build_policy_snapshots` has no interval-carry input or
method column.

- [ ] **Step 3: Add interval carry normalization and precedence**

Add the keyword argument:

```python
interval_carried_closes: pd.DataFrame | None = None,
```

Normalize it to the same key/value layout as exact carries. Before the monthly
loop, compare overlapping `(formation_date, ts_code)` keys:

```python
overlap = exact_frame.merge(
    interval_frame,
    on=["formation_date", "ts_code"],
    suffixes=("_exact", "_interval"),
)
different = ~np.isclose(
    overlap["close_exact"].to_numpy(dtype=float),
    overlap["close_interval"].to_numpy(dtype=float),
    rtol=0.0,
    atol=0.0,
    equal_nan=True,
)
if different.any():
    raise DataBlocked("conflicting exact and interval carry closes")
interval_frame = interval_frame.merge(
    overlap[["formation_date", "ts_code"]],
    on=["formation_date", "ts_code"],
    how="left",
    indicator=True,
)
interval_frame = interval_frame[
    interval_frame["_merge"].eq("left_only")
].drop(columns="_merge")
```

Inside each formation:

```python
carried_mask = pd.Series(False, index=base)
carry_method = pd.Series("", index=base, dtype=object)

# Existing exact path runs first.
if usable_exact.any():
    close = close.mask(usable_exact, exact_fill)
    carried_mask = carried_mask | usable_exact
    carry_method = carry_method.mask(
        usable_exact, "EXACT_SUSPENSION"
    )

usable_interval = (
    close.isna()
    & interval_fill.notna()
)
if usable_interval.any():
    close = close.mask(usable_interval, interval_fill)
    carried_mask = carried_mask | usable_interval
    carry_method = carry_method.mask(
        usable_interval,
        "CONTINUOUS_SUSPENSION_INTERVAL",
    )
```

Publish both `close_carried` and `close_carry_method` in every snapshot.

- [ ] **Step 4: Write and implement split coverage tests**

Update synthetic snapshots so any `close_carried=True` row has one of the two
valid methods. Change the existing coverage expectation from the legacy
`SUSPENDED_CARRY_FORWARD` side to:

```python
assert set(rows["side"]) == {
    "EXACT_SUSPENSION_CARRY_FORWARD",
    "INTERVAL_SUSPENSION_CARRY_FORWARD",
}
```

In `audit_exclusions`, require:

```python
valid_methods = {
    "",
    "EXACT_SUSPENSION",
    "CONTINUOUS_SUSPENSION_INTERVAL",
}
if not set(methods).issubset(valid_methods):
    raise DataBlocked("invalid close_carry_method")
if not carried.equals(methods.ne("")):
    raise DataBlocked("close carry flag/method mismatch")
```

Add one report row per non-zero method with the exact side names above. Assert
their sum equals `close_carried.sum()`.

- [ ] **Step 5: Run snapshot and coverage tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  -k "suspension or close_carry_forward" -q -W error
```

Expected: PASS.

- [ ] **Step 6: Commit carry integration**

```bash
git add signals/style_basket/b3_build.py \
  tests/test_b3_exposures.py
git commit -m "feat(b3): apply suspension interval carries"
```

### Task 5: Publish and require the preflight evidence artifact

**Files:**

- Modify: `signals/style_basket/b3_build.py:39-47`
- Modify: `signals/style_basket/b3_build.py:1990-2450`
- Modify: `tests/test_b3_exposures.py:1570-1640`
- Modify: `tests/test_b3_exposures.py:2120-2280`
- Modify: `backtest/b3_eval.py:166-170`
- Modify: `backtest/b3_eval.py:570-615`
- Modify: `tests/test_b3_eval.py:3540-3760`

- [ ] **Step 1: Write failing builder artifact tests**

Use `dataclasses.replace` on `_preflight_sources` to supply a two-row core
evidence frame, one inside and one outside the required window. Assert:

```python
path = tmp_path / "suspension_interval_evidence.csv"
assert path.is_file()
evidence = pd.read_csv(path)
assert list(evidence["required_formation"]) == [True, False]
manifest = json.loads(
    (tmp_path / "manifests/preflight.json").read_text()
)
assert manifest["outputs"]["suspension_interval_evidence.csv"] == (
    _file_digest(path)
)
```

Add tests that an unavailable callback writes an empty fixed-schema file, a
conflicting duplicate candidate makes preflight `DATA_BLOCKED`, and an old
manifest is invalidated before a source failure.

- [ ] **Step 2: Run the artifact tests and observe missing-file failures**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  -k "preflight and (artifact or manifest or interval)" -q
```

Expected: FAIL because the third preflight output is not written or declared.

- [ ] **Step 3: Add the shared artifact schema and preflight writer**

Define this constant in `signals/style_basket/b3_suspension.py`, then import it from both `b3_build.py` and the impact audit:

```python
SUSPENSION_INTERVAL_ARTIFACT_COLUMNS = (
    "ts_code",
    "formation_date",
    "required_formation",
    "list_date",
    "delist_date",
    "suspension_start",
    "previous_official_trade_date",
    "previous_close_date",
    "previous_close",
    "suspend_type",
    "suspend_reason",
    "evidence_method",
    "accepted",
    "rejection_reason",
    "next_trade_date",
    "next_nonnull_close",
    "exact_stock_status_confirmed",
)
```

Add `_preflight_interval_evidence(raw, required_start, required_end)` that:

- validates exactly one row per `(formation_date, ts_code)`;
- validates real boolean `accepted`;
- enforces accepted/method/rejection consistency;
- derives `required_formation` only from the B3 config window;
- sorts stably and returns the exact ordered schema.

Add `suspension_interval_evidence.csv` to `STAGE_OUTPUTS["preflight"]`. In
`run_preflight`, obtain the optional callback result without triggering default
source reads, convert structural errors into a blocker with
`check="suspension_interval_evidence"` and `reason_code="DATA_CONTRACT"`, then
atomically write the fixed-schema CSV before writing the manifest.

- [ ] **Step 4: Write failing eval-side required-output tests**

Update `_write_preflight_manifest` in `tests/test_b3_eval.py` to create and hash
the evidence CSV. Add:

```python
def test_preflight_contract_rejects_partial_and_extra_outputs(tmp_path):
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    payload["outputs"].pop("suspension_interval_evidence.csv")
    _rewrite_json(manifest_path, payload)
    with pytest.raises(DataBlocked, match="output set"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)

    manifest_path, payload = _write_preflight_manifest(tmp_path)
    extra = tmp_path / "extra.csv"
    extra.write_text("x\n", encoding="utf-8")
    payload["outputs"]["extra.csv"] = _sha256(extra)
    _rewrite_json(manifest_path, payload)
    with pytest.raises(DataBlocked, match="output set"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)
```

- [ ] **Step 5: Require the exact output set in eval**

Add the evidence filename to `_PREFLIGHT_OUTPUTS` and replace the subset check:

```python
if set(checked_outputs) != _PREFLIGHT_OUTPUTS:
    raise DataBlocked("preflight output set mismatch")
```

Update every preflight manifest fixture in `tests/test_b3_exposures.py` and
`tests/test_b3_eval.py` to create and hash all three files.

- [ ] **Step 6: Run builder and eval manifest suites**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py tests/test_b3_eval.py \
  -k "preflight or parent_manifest" -q -W error
```

Expected: PASS.

- [ ] **Step 7: Commit the trust-chain change**

```bash
git add signals/style_basket/b3_build.py \
  tests/test_b3_exposures.py \
  backtest/b3_eval.py \
  tests/test_b3_eval.py
git commit -m "feat(b3): bind suspension evidence to preflight"
```

### Task 6: Make the impact audit consume the exact preflight evidence

**Files:**

- Modify:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`
- Modify: `tests/test_b3_impact_audit.py`

- [ ] **Step 1: Write failing post-fix classification tests**

Add a fixture containing two old CLOSE gaps: one accepted interval and one
pre-coverage rejection. Pass the evidence into `build_impact_details` and
assert:

```python
assert set(classified["close_source"]) == {
    "CONTINUOUS_SUSPENSION_INTERVAL",
    "",
}
assert close_detail[["ts_code", "interval_rejection_reason"]].to_dict(
    "records"
) == [
    {
        "ts_code": "OLD.SZ",
        "interval_rejection_reason": (
            "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE"
        ),
    }
]
```

Also add:

- accepted evidence with a missing share becomes
  `DATA_MISSING_SHARES`, proving reason precedence is recomputed;
- exact carry still wins over interval evidence;
- duplicate/conflicting interval keys raise `AuditContractError`;
- rejected evidence never fills a close.

- [ ] **Step 2: Run focused audit tests and observe signature failures**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py \
  -k "interval or impact_details" -q
```

Expected: FAIL because the audit does not accept interval evidence.

- [ ] **Step 3: Validate and merge the preflight evidence artifact**

Add an `interval_evidence` keyword to `build_impact_details`. Import `SUSPENSION_INTERVAL_ARTIFACT_COLUMNS` from `b3_suspension`,
validate that exact artifact schema, coerce `accepted` with `_coerce_bool`, reject conflicting keys, and rename before merging all
evidence rows by `(ts_code, formation_date)`:

```python
interval = interval_evidence.rename(
    columns={
        "accepted": "interval_accepted",
        "previous_close": "interval_previous_close",
        "evidence_method": "interval_evidence_method",
        "rejection_reason": "interval_rejection_reason",
    }
)
```

Use:

```python
active["usable_interval_carry"] = (
    active["raw_close"].isna()
    & ~active["usable_carry"]
    & active["interval_accepted"].fillna(False)
    & active["interval_previous_close"].notna()
)
active.loc[active["usable_interval_carry"], "close"] = active.loc[
    active["usable_interval_carry"],
    "interval_previous_close",
]
active["close_source"] = np.select(
    [
        active["raw_close"].notna(),
        active["usable_carry"],
        active["usable_interval_carry"],
    ],
    [
        "EXACT_FORMATION_CLOSE",
        "EXACT_SUSPENSION",
        "CONTINUOUS_SUSPENSION_INTERVAL",
    ],
    default="",
)
```

Do not call the classifier again: the hash-bound preflight artifact is the
single classification result used by both B3 and this audit.

- [ ] **Step 4: Extend residual CLOSE schemas and summaries**

Add these residual detail fields:

```python
"suspension_start",
"interval_evidence_method",
"interval_accepted",
"interval_rejection_reason",
```

Add `SUSPENSION_START_PRECEDES_SOURCE_COVERAGE` to allowed evidence buckets and
make `close_evidence_bucket` return it before legacy observational buckets.
Add
`suspension_start_precedes_source_coverage_months` to
`CLOSE_SUMMARY_COLUMNS`.

Set the post-fix anchored CLOSE expectation:

```python
EXPECTED_COUNTS["DATA_MISSING_CLOSE"] = (4, 0)
```

Keep SHARES at `(5781, 5445)` unless the real preflight in Task 7 disproves it;
if it differs, stop and diagnose rather than changing the constant to force a
pass.

- [ ] **Step 5: Bind the evidence input hash**

Add CLI arguments:

```python
parser.add_argument(
    "--suspension-interval-evidence",
    type=Path,
    default=(
        ROOT
        / "output/style_basket/b3/suspension_interval_evidence.csv"
    ),
)
parser.add_argument(
    "--expected-suspension-evidence-sha256",
    required=True,
)
```

Verify it with `_verify_hash`, read it before connecting, pass it to
`build_impact_details`, and record this input path and hash in
`impact_audit_manifest.json`. Keep the database connection read-only and the
existing atomic publication/rollback mechanism unchanged.

- [ ] **Step 6: Update audit publication tests**

Update artifact fixtures to the new ordered schemas and post-fix counts. Add a
CLI/main test with a temporary evidence file and its exact digest. Assert
manifest input hashes include all three immutable inputs:

```python
assert set(manifest["inputs"]) == {
    "tail.csv",
    "coverage_audit.csv",
    "suspension_interval_evidence.csv",
}
```

- [ ] **Step 7: Run the full audit test module**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py -q -W error
```

Expected: PASS.

- [ ] **Step 8: Commit audit code before real-data publication**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  tests/test_b3_impact_audit.py
git commit -m "feat(b3): audit post-interval close residuals"
```

### Task 7: Verify with real data and republish the read-only impact audit

**Files:**

- Modify: `data_fixes/2026-07-25-share-capital-par/README.md`
- Regenerate:
  `data_fixes/2026-07-25-share-capital-par/shares_tail_impact_by_ticker.csv`
- Regenerate:
  `data_fixes/2026-07-25-share-capital-par/shares_tail_impact_detail.csv`
- Regenerate:
  `data_fixes/2026-07-25-share-capital-par/close_gap_impact_by_ticker.csv`
- Regenerate:
  `data_fixes/2026-07-25-share-capital-par/close_gap_impact_detail.csv`
- Regenerate:
  `data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json`
- Modify:
  `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`
  only to replace the anchored coverage hash after the verified run.

- [ ] **Step 1: Run all code tests before accessing real data**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py \
  tests/test_b3_exposures.py \
  tests/test_b3_eval.py \
  tests/test_b3_impact_audit.py -q -W error
```

Expected: PASS.

- [ ] **Step 2: Run only fixed-window preflight under 8G**

From the isolated worktree:

```bash
systemd-run --user --scope -p MemoryMax=8G \
  /home/elfbob/miniconda3/bin/python \
  -m signals.style_basket.b3_build \
  --stage preflight \
  --data-end 2023-12-31 \
  --output-dir output/style_basket/b3
```

Expected: process exit `2`, because required SHARES remains blocked. It must not
run exposures, portfolios, states, eval, or prod.

- [ ] **Step 3: Reconcile evidence, coverage, blockers, and manifest**

Run:

```bash
/home/elfbob/miniconda3/bin/python -c '
import hashlib, json
from pathlib import Path
import pandas as pd
root = Path("output/style_basket/b3")
evidence_path = root / "suspension_interval_evidence.csv"
coverage_path = root / "coverage_audit.csv"
evidence = pd.read_csv(evidence_path)
coverage = pd.read_csv(coverage_path)
required = evidence["required_formation"].astype(bool)
accepted = evidence["accepted"].astype(bool)
assert len(evidence) == 202
assert int(accepted.sum()) == 198
assert int((accepted & required).sum()) == 190
assert int((~accepted).sum()) == 4
assert int((~accepted & required).sum()) == 0
assert set(evidence.loc[~accepted, "ts_code"]) == {
    "000545.SZ", "600698.SH"
}
main = coverage[
    coverage["pit_policy"].eq("legal_deadline")
    & coverage["check"].eq("size_exclusion")
]
close = main[main["side"].eq("DATA_MISSING_CLOSE")]
assert int(close["eligible_count"].sum()) == 4
assert int(
    close.loc[close["required_formation"].astype(bool), "eligible_count"].sum()
) == 0
carry = coverage[coverage["check"].eq("close_carry_forward")]
carry_counts = carry.groupby(["pit_policy", "side"])[
    "eligible_count"
].sum()
for policy in (
    "legal_deadline",
    "legal_deadline_plus_one_month_end",
):
    assert int(carry_counts.get(
        (policy, "INTERVAL_SUSPENSION_CARRY_FORWARD"), 0
    )) == 198
    assert int(carry_counts.get(
        (policy, "EXACT_SUSPENSION_CARRY_FORWARD"), 0
    )) == 0
manifest_path = root / "manifests/preflight.json"
manifest = json.loads(manifest_path.read_text())
assert manifest["status"] == "DATA_BLOCKED"
assert not any(
    row["side"] == "DATA_MISSING_CLOSE"
    for row in manifest["blockers"]
)
assert any(
    row["side"] == "DATA_MISSING_SHARES"
    for row in manifest["blockers"]
)
for name, expected in manifest["outputs"].items():
    actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
    assert actual == expected
print("interval=202/198/4; required=190/190/0; manifest=OK")
'
```

Expected:

```text
interval=202/198/4; required=190/190/0; manifest=OK
```

If any anchor differs, stop. Do not weaken classification or change expected
counts to fit the output.

- [ ] **Step 4: Capture exact new input hashes**

Run:

```bash
sha256sum \
  output/style_basket/b3/coverage_audit.csv \
  output/style_basket/b3/suspension_interval_evidence.csv
```

Expected: two lowercase 64-character SHA-256 values. Copy the coverage digest
into `EXPECTED_COVERAGE_SHA256` using `apply_patch`. Keep the interval digest as
the explicit required CLI argument and record it verbatim in the README.

- [ ] **Step 5: Republish the read-only impact audit**

Run the audit with the two exact digests emitted in Step 4:

```bash
/home/elfbob/miniconda3/bin/python \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  --coverage-audit output/style_basket/b3/coverage_audit.csv \
  --suspension-interval-evidence \
    output/style_basket/b3/suspension_interval_evidence.csv \
  --expected-suspension-evidence-sha256 \
    "$(sha256sum output/style_basket/b3/suspension_interval_evidence.csv | awk '{print $1}')"
```

Expected:

```text
DATA_MISSING_SHARES: 5781 all / 5445 required / 56 tickers
DATA_MISSING_CLOSE: 4 all / 0 required / 2 tickers
monthly reconciliation: OK
```

The command uses a read-only transaction and atomically replaces only the five
tracked audit outputs in its own directory.

- [ ] **Step 6: Update README with final facts and reproduction command**

Document:

- exact and interval carry definitions;
- `202/190 -> 4/0`;
- the two residual tickers and
  `SUSPENSION_START_PRECEDES_SOURCE_COVERAGE`;
- unchanged SHARES counts if verified;
- exact coverage/evidence SHA-256 values;
- the command from Step 5;
- explicit statements: no Wind, no DB writes, no prod, no formal eval.

- [ ] **Step 7: Verify regenerated artifacts and audit tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_impact_audit.py -q -W error
git diff --check
git status --short
```

Expected: tests PASS; diff check is silent; status contains only intended code,
README, four audit CSVs, manifest, and no user-owned untracked paths.

- [ ] **Step 8: Commit the anchored real-data audit**

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/README.md \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  data_fixes/2026-07-25-share-capital-par/shares_tail_impact_by_ticker.csv \
  data_fixes/2026-07-25-share-capital-par/shares_tail_impact_detail.csv \
  data_fixes/2026-07-25-share-capital-par/close_gap_impact_by_ticker.csv \
  data_fixes/2026-07-25-share-capital-par/close_gap_impact_detail.csv \
  data_fixes/2026-07-25-share-capital-par/impact_audit_manifest.json
git commit -m "data: reconcile B3 suspension interval impact"
```

### Task 8: Final verification and review

**Files:**

- Verify only; modify code only if a failing test or review finding requires a
  separately tested fix.

- [ ] **Step 1: Run every B3 test with warnings as errors**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_suspension.py \
  tests/test_b3_exposures.py \
  tests/test_b3_portfolios_states.py \
  tests/test_b3_structure.py \
  tests/test_b3_eval.py \
  tests/test_b3_impact_audit.py \
  -q -W error
```

Expected: PASS.

- [ ] **Step 2: Run the full repository suite**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected: PASS, with a total greater than the pre-change 842 tests because the
new suspension module tests are included.

- [ ] **Step 3: Verify Git scope and commit hygiene**

Run:

```bash
git diff --check
git status --short --branch
git log --oneline main..HEAD
git diff --name-only main...HEAD -- output backtest/output
```

Expected:

- no whitespace errors;
- only the intended branch commits;
- no staged or modified files;
- user-owned untracked paths are still untouched;
- `git diff --name-only main...HEAD -- output backtest/output` is empty, so
  committed B1, B2, equal-weight, and other research outputs are unchanged.

- [ ] **Step 4: Review against the approved spec**

Check every completion criterion:

- decisions use only facts dated on or before formation;
- future resume/status fields are report-only;
- exact carry wins and conflicting values block;
- original CLOSE is never overwritten;
- evidence contains 202 unique candidate keys, 198 accepted, 4 rejected;
- required interval candidates are 190/190 accepted;
- required `DATA_MISSING_CLOSE` is zero;
- evidence file is mandatory and hash-bound on both builder and eval sides;
- impact audit consumes the evidence file rather than reclassifying;
- no DB writes, Wind calls, prod, or formal eval occurred.

Use `superpowers:requesting-code-review` if the selected execution mode permits
subagents; otherwise perform the same Critical/Important review inline. Resolve
findings with `superpowers:receiving-code-review`, TDD, and separate fix commits.
Acceptance requires Critical 0 and Important 0.

- [ ] **Step 5: Apply verification-before-completion**

Use `superpowers:verification-before-completion`. Cite the fresh commands and
outputs from Steps 1–3 and the anchored preflight reconciliation from Task 7.
Do not claim that B3 evaluation is unblocked overall: SHARES is intentionally
still the remaining required blocker.
