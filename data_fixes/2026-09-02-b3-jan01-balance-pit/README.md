# B3: exclude CSMAR Jan-01 balance events (PIT correction)

Date: 2026-09-02 (Asia/Shanghai). Branch `fix/b3-jan1-pit-correction`.
Handoff source: `HANDOFF_JAN01_PIT_FIX.md` (repo root, commit `c3fafa3`).

## Problem

The WSL r2 exposure build (`/home/ghls/b3_runs/20260901_true_disclosure_r2`,
HEAD `3dbec32`) covered `629,466 / 629,556` model rows in the independent
true-disclosure coverage audit. Every one of the 90 uncovered rows had a single
unverifiable dependency of the form `ticker|YYYY-01-01|balance|csmar`.

CSMAR stores a `YYYY-01-01` balance-sheet row for some tickers. It is not an
independent disclosure event (no standalone balance sheet exists for that
date). Because `legal_disclosure_deadline` falls back to `end_date + 120 days`
for non-quarter-end dates, such a row became "known" around May 1 and moved
book equity forward before the genuine annual report was public. The audit
classified 70 of the 90 rows as genuine look-ahead. The user chose
correctness over preserving historical BP/style-score output.

## Fix (`013f3bc`)

`signals/style_basket/b3_build.py`: new
`_exclude_invalid_csmar_jan_01_balance_events`, applied in
`build_policy_snapshots` after `apply_pit_policy` and before
`ticker_financial_rows`. It drops facts only when all of the following hold:
`data_source == "csmar"`, `statement_type == "balance"`, end date is
January 1, and the date is not a quarter end. CSMAR income and
direct-cashflow facts on January 1 are untouched (their YTD/TTM chain is a
separate concern). No provenance is rewritten.

## Tests (`tests/test_b3_exposures.py`)

- `test_snapshot_bp_excludes_csmar_jan_01_balance_event` — both PIT policies
  × Jan-01 equity 500.0/900.0; asserts BP stays 0.5 from the verified annual
  row, `true_first_disclosure_verified` stays true, no dependency keys.
- `test_snapshot_jan_01_filter_is_balance_event_scoped` — Jan-01 income and
  cashflow_direct facts still reach derived-row generation; only `balance`
  is removed.

RED before the fix (same-equity failed on the verified flag, different-equity
failed with BP 0.9 ≠ 0.5); GREEN after. Mutation checks: dropping the
`statement_type` condition turns the scope test red; removing the call turns
all five red.

Focused suite: 423 passed (was 418). Full suite on this branch: 1242 passed.

## r3 rebuild and verification

Run: `/home/ghls/b3_runs/20260902_jan01_pit_r3` on WSL (worktree
`/home/ghls/style_timing_signal-jan01-pit`, HEAD `013f3bc`), same runner
shape as r2 (stages limited to preflight + exposures; states/portfolios/eval
forbidden). preflight 17:56 wall / 19.5 GB peak RSS, exposures 18:38 wall /
19.4 GB peak RSS, both exit 0. Six artifacts copied to
`/tmp/b3-20260902-jan01-pit-r3` with SHA256 matching on both ends
(`evidence/r3_artifacts_sha256.txt`).

Independent audit, same clean detached worktree as r2
(`.worktrees/b3-coverage-audit-clean`, HEAD `8899775`, config identical):

| | r2 (`3dbec32`) | r3 (`013f3bc`) |
|---|---|---|
| verified / required | 629,466 / 629,556 | **629,556 / 629,556** |
| `legal_deadline` | 314,728 / 314,778 | 314,778 / 314,778 |
| `legal_deadline_plus_one_month_end` | 314,738 / 314,778 | 314,778 / 314,778 |
| `coverage_ready` | false (exit 1) | **true (exit 0)** |

Gate 4 (exact 100% coverage) is met. states / portfolios / evaluation have
not been run.

r2 vs r3 exposure diff (`evidence/compare_r2_r3.txt`):

- Row set identical (804,024 rows per build); `model_eligible` and
  `size_eligible` unchanged on every row.
- `true_first_disclosure_verified` flipped false→true on exactly the 90
  previously uncovered rows (50 main policy, 40 lag policy); unverified model
  rows 90 → 0.
- 21 tickers on 13 formation months moved their style score by more than
  0.05 (max 1.86): these are the rows whose BP input actually changed. The
  other ~152k changed rows moved by < 0.01 because the style score is
  standardized per formation month, so a single BP change re-centres the
  cross-section. Twelve further months (2017-06..2018-04, 2022-04) show only
  spill-over of at most 0.005 with no eligibility or verification change.

Historical BP/style-score output therefore changes on those months, as
accepted in the handoff.

