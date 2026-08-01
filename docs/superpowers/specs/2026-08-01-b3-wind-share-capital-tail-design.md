# B3 Wind Historical Share-Capital Tail Design

**Date:** 2026-08-01
**Status:** Interactive design approved; written specification pending user review
**Priority:** Blocker for the frozen-history B3 performance evaluation

## Objective

Resolve the remaining B3 `DATA_MISSING_SHARES` input-contract blocker with
reproducible upstream facts while leaving the preregistered strategy, universe,
formation calendar, PIT policies, exclusion precedence, portfolio construction,
cost model, statistical gates, and fail-closed behavior unchanged.

The finished flow must:

1. obtain direct Wind month-end `total_shares` facts for every currently blocked
   formation coordinate;
2. publish a deterministic, reviewable proposal before any production write;
3. load only guarded, change-compressed facts into the canonical
   `stock_selector.stock_share_capital` relation;
4. prove that `DATA_MISSING_SHARES` and `DATA_MISSING_CLOSE` are both zero on
   the frozen B3 window; and
5. rerun B3 preflight and evaluation to produce a machine-readable statistical
   and final verdict under the original rules.

## Current Evidence and Immutable Anchors

The current B3 share-capital audit is anchored by:

| Artifact | Rows excluding header | SHA-256 |
| --- | ---: | --- |
| `tail.csv` | 57 | `93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223` |
| `shares_tail_impact_by_ticker.csv` | 57 | `ff8a9123504d430225c0ea0618c6a07c06373b07f2826af8bff450d6a55d7a40` |
| `shares_tail_impact_detail.csv` | 5,781 | `a2fd07c9cdf5ba6b1defb89850eaf5bea629c7e76cf372c95931c745eedead13` |
| `impact_audit_manifest.json` | n/a | `777818dd48a0fc4513f74391ce9c57b8bca0f5750173590ba894c93be22b1f74` |

The detail contains `5,781` all-window and `5,445` required-window
`DATA_MISSING_SHARES` coordinates across 56 actual blocker tickers. The 57th
tail ticker, `688347.SH`, has zero affected coordinates because it had not been
listed for 180 days by the final formation date. It is not a write target.

The style repository baseline at design time is
`c1adb29f3ea5c162d1e123071173a0bb71e0bc31`, which contains the completed
continuous-suspension CLOSE evidence chain and exact binding between accepted
evidence and carried close values.

### Approved three-ticker Wind probe

The read-only probe covered three deliberately different failure shapes:

- `000681.SZ`: long-history name just outside the prior par tolerance;
- `603535.SH`: apparent exact-par name whose 30-day calibration still failed;
- `688428.SH`: extreme CSMAR numerator/share-count mismatch.

Wind direct `total_shares` covered every one of the existing 209 blocked
coordinates (`128 + 71 + 10`). Across the monthly probe, its maximum relative
difference from `mkt_cap_ard / unadjusted_close` was approximately
`1.8522e-6`. All three current canonical histories had zero positive share
nodes known by `2023-12-31`.

The daily probe also established the concrete `603535.SH` mechanism:

| Date | Wind `total_shares` |
| --- | ---: |
| 2025-04-01 | 345,211,435 |
| 2025-04-02 | 345,211,435 |
| 2025-04-03 | 510,912,924 |

The 2025-04-03 share jump contaminated the 30-day calibration median. This is
not evidence for widening standard par denominations or tolerance; it is
evidence that direct historical share facts are the correct source for the
residual tail.

## Scope

### In scope

- The 56 tickers and 5,781 formation coordinates in the immutable detail
  anchor.
- Wind WSD fields `total_shares`, `mkt_cap_ard`, and `close` on the frozen
  2013-05 through 2023-12 monthly grid.
- A targeted loader, owned by `stock_selector`, that writes guarded facts to
  the canonical `stock_share_capital` relation.
- Review, backup, rollback, and verification artifacts owned by this
  `style_timing_signal` data-fix campaign.
- Test-schema validation, a separately approved production transaction, and
  guarded B3 preflight/evaluation.

### Out of scope

- Changing `_STANDARD_PARS`, `_PAR_TOLERANCE`, or the existing CSMAR estimator.
- Treating the tail as a B3 universe exemption.
- Adding a B3-only share-capital input path.
- Changing formation dates, minimum listed days, PIT policies, size-exclusion
  precedence, candidates, weights, costs, or statistical/final gates.
- Backfilling tickers outside the 56 audited blockers or dates after the frozen
  `2023-12-31` data end.
- Writing directly to the Pi5 replica; the existing synchronization path must
  propagate primary changes.
- Requiring the final verdict to be `PASS_SHADOW`. The outcome must be computed,
  not selected in advance.

## Considered Approaches

### Adopted: change-compressed canonical Wind observations

Fetch direct monthly facts for every blocker coordinate, normalize share counts
to integral values, and retain only the first target observation plus subsequent
value changes. The sparse nodes live in the existing canonical relation and are
therefore consumed by B3 and all other readers through the established as-of
contract.

This keeps one source of truth, matches the sparse relation's design, and avoids
encoding B3-specific exceptions in strategy code.

### Rejected: one canonical row per blocked formation

Writing all 5,781 observations is easy to audit but stores thousands of
unchanged duplicates in a relation designed for sparse nodes. It has no
coverage advantage over a round-trip-verified compressed series.

### Rejected: frozen B3 supplemental input

A B3-local CSV would avoid a database write but would create a second share
truth that canonical consumers cannot see. The canonical table would remain
known-bad, and B3 would gain a special source path that the objective explicitly
does not require.

## Component Boundaries

### `stock_selector`

The upstream repository owns:

- a targeted CLI script under `scripts/`;
- pure normalization, validation, compression, round-trip, and action-planning
  helpers;
- the guarded transaction that writes `stock_share_capital`; and
- unit and test-schema integration tests.

The targeted loader must not call the generic `write_share_capital` upsert,
because that writer overwrites every natural-key conflict. The new guarded
writer needs narrower semantics defined below.

### `style_timing_signal`

This repository owns:

- the immutable blocker anchors;
- the full Wind observation, proposal, conflict, backup, rollback-key, and
  manifest artifacts;
- the post-write B3 impact audit; and
- the final preflight/evaluation outputs and run record.

B3 implementation code remains unchanged. Its existing database reader must
consume the newly valid canonical rows without an override.

The repositories receive separate commits. Data manifests record both commit
hashes and cross-reference each other by artifact SHA-256.

## Read and Normalize Contract

### Target coordinates

The loader must first validate the four immutable input hashes and reconstruct
the exact sorted target set from `shares_tail_impact_detail.csv`.

Required invariants are:

- exactly 5,781 unique `(ts_code, formation_date)` keys;
- exactly 56 unique tickers;
- exactly 5,445 rows with `required_formation=true`;
- every `reason_code` equals `DATA_MISSING_SHARES`; and
- no target ticker equals the zero-impact `688347.SH`.

Any drift stops the run before a Wind call.

### Wind request

Use the existing authenticated gateway's generic WSD path with:

```text
fields  = total_shares,mkt_cap_ard,close
start   = 2013-05-01
end     = 2023-12-31
options = Period=M;PriceAdj=U;unit=1
codes   = the exact sorted 56-ticker target set
```

Credentials remain in the existing gitignored settings file and must never be
written to an artifact. Record gateway `/health`, `/quota` before/after, request
parameters excluding credentials, and raw response hashes.

### Row normalization

For each returned row:

1. normalize `ts_code` and parse the Wind date as `formation_date`;
2. reject duplicate `(ts_code, formation_date)` keys;
3. coerce the three fields to real numeric values without treating booleans as
   numbers;
4. require direct `total_shares` to be finite, strictly positive, and integral
   to floating-representation tolerance;
5. normalize it to the exact rounded integer share count;
6. require `mkt_cap_ard` and unadjusted `close` to be finite and strictly
   positive for every target key; and
7. calculate `implied_shares = mkt_cap_ard / close` and
   `relative_error = abs(implied_shares - total_shares) / total_shares`.

Every target key must appear once and have `relative_error <= 1e-4`. Wind may
return pre-listing null rows outside the target set; those are retained only in
the raw observation artifact and do not enter the proposal.

Large changes in `total_shares` are reported, not rejected: issuance, buyback,
and split events are legitimate. Cross-field agreement, rather than a
preselected change-size threshold, is the validity gate.

## Sparse Node Construction

Restrict normalized observations to the exact 5,781 target coordinates. Within
each ticker, sort by formation date and retain:

1. the first target observation; and
2. each later observation whose integral `total_shares` differs from the prior
   target observation.

Each proposal node has:

```text
effective_date = formation_date
available_date = formation_date
total_shares   = normalized direct Wind integer
par_value      = NULL
source         = wind_total_shares_month_end
quality_flag   = NULL
```

The dates are observation dates, not claims about the exact corporate-action
effective date. Setting both dates to the month-end observation is conservative
between an intra-month event and the next formation, while exactly matching the
monthly B3 decision coordinate.

Before publication, as-of forward-fill the proposal nodes back onto all 5,781
target keys and require exact equality with every normalized direct Wind value.
This round-trip is the proof that compression did not change the B3 input.

## Existing-Row Conflict Policy

Read all existing canonical rows at proposal natural keys and assign exactly
one action:

1. `INSERT`: no existing row;
2. `UPGRADE_PAR_UNKNOWN`: existing row has `total_shares IS NULL`,
   `par_value IS NULL`, and `quality_flag='par_unknown'`;
3. `KEEP_IDENTICAL`: existing row has a positive integral `total_shares`
   exactly equal to the proposal value; or
4. `BLOCK_CONFLICT`: every other state, including a different positive value,
   a null value with a non-`par_unknown` flag, invalid numeric state, or
   duplicate evidence returned by the read.

Any `BLOCK_CONFLICT` prevents publication of an applicable proposal. A
`KEEP_IDENTICAL` row is not updated, so its original source and timestamp remain
intact.

## Artifact Contract

Create a dedicated campaign directory:

`data_fixes/2026-08-01-b3-wind-share-capital/`

The dry run publishes atomically, with stable sorting and fixed columns:

- `wind_monthly_observations.csv`: all raw-normalized monthly response rows and
  cross-field diagnostics;
- `proposal_nodes.csv`: compressed canonical nodes;
- `proposal_actions.csv`: existing-row state and planned action per node;
- `existing_rows_backup.csv`: the complete pre-write canonical rows at every
  proposal natural key, including `updated_at`;
- `rollback_insert_keys.csv`: keys absent before the transaction;
- `proposal_manifest.json`: request, quota, anchor, commit, row-count, date,
  source-table, and artifact-hash evidence; and
- `README.md`: human-readable execution record and exact rollback semantics.

The manifest is published last. Partial files, a missing declared output, or a
hash mismatch are not an applicable proposal.

No password, bearer token, or complete settings payload may appear in an
artifact.

## Apply Contract

### Test schema

The test-schema path exercises the same guarded SQL with synthetic conflicts.
It must prove all four action classes, exact row counts, and rollback-set
construction. It does not pretend that the test schema contains a complete B3
history.

### Production gate

Production application is a separate explicitly approved operation. Before
starting it, revalidate:

- both repository commits;
- every anchor and proposal artifact hash;
- the proposal manifest's complete-output declaration;
- the current rows at all proposal keys against `existing_rows_backup.csv`;
- zero `BLOCK_CONFLICT` actions; and
- Wind observation and round-trip counts.

### Production transaction

Use one transaction on the Debian primary and acquire a campaign-specific
transaction advisory lock. Re-read and classify all proposal keys inside the
transaction to close the dry-run/apply race.

The transaction may:

- insert only `INSERT` nodes;
- update only the five non-key business columns named total_shares, par_value,
  available_date, source, and quality_flag for rows that still match the exact
  UPGRADE_PAR_UNKNOWN precondition; and
- leave `KEEP_IDENTICAL` rows untouched.

The affected row count must equal `INSERT + UPGRADE_PAR_UNKNOWN`. Any mismatch
rolls back the entire transaction. The existing `updated_at` trigger and sync
worker handle replication; there is no direct Pi5 write.

## Rollback Contract

Rollback is not automatic and requires separate explicit approval. In one
transaction it must:

1. revalidate the applied manifest and current values;
2. delete only keys listed in `rollback_insert_keys.csv` whose current rows
   still exactly match the applied proposal source and values;
3. restore every overwritten row from `existing_rows_backup.csv`; and
4. require exact delete and restore row counts before commit.

If any current row has changed since application, rollback stops rather than
overwriting later work.

## Failure Semantics

The run fails closed before a production write when any of the following occurs:

- Tailscale, gateway health, Wind readiness, quota, or WSD failure;
- input hash, target count, target key, or required-formation drift;
- missing/duplicate Wind keys, invalid values, or cross-field disagreement;
- compression round-trip mismatch;
- existing canonical conflict;
- partial artifact publication or manifest/hash mismatch;
- test-schema regression; or
- dry-run/apply database-state drift.

Retries may repeat idempotent health checks and complete read requests. They may
not retry a partially observed production transaction by assuming it committed;
the next run must re-read canonical state and reclassify actions.

## Testing

### Pure tests in `stock_selector`

Cover:

- anchor validation and exact target reconstruction;
- strict ticker/date/numeric/boolean normalization;
- direct-share integer normalization;
- duplicate, missing, nonpositive, nonfinite, and cross-field-error rejection;
- first-node/change-node compression;
- exact as-of round-trip reconstruction;
- all four existing-row actions;
- stable output ordering and hashes; and
- rollback insert/restore set construction.

### Test-schema integration

Prove that the guarded writer:

- inserts an absent node;
- upgrades only an exact `par_unknown` null row;
- preserves an identical valued row without touching `updated_at`;
- rejects a conflicting valued or malformed null row;
- rolls back all changes on any row-count mismatch; and
- is idempotent on a second application of the same proposal.

### Repository regressions

Run the new upstream tests, the relevant existing share-capital writer/reader
tests, all B3 exposure/preflight/impact/evaluation tests, and both repositories'
full test suites before production application.

## Post-Write Verification and B3 Evaluation

After production commit:

1. read back every proposal natural key and compare it with the applied action;
2. rebuild the share-tail impact audit from current production facts;
3. require `DATA_MISSING_SHARES = 0 all / 0 required`;
4. rebuild B3 preflight using current CLOSE evidence;
5. require `DATA_MISSING_CLOSE = 0 all / 0 required`;
6. require all 120 required formations to survive the size input contract;
7. run B3 preflight/evaluation under
   `systemd-run --user --scope -p MemoryMax=8G`; and
8. publish the complete machine-readable run evidence.

The fixed-history objective is satisfied only when B3 reaches candidate
statistics and produces a nonempty family statistical verdict. The final
verdict may still be `DATA_BLOCKED` if a different preregistered run-level gate
requires it; such a blocker must remain visible and must not erase a legally
computed statistical verdict. No historical result is tuned or coerced into
`PASS_SHADOW`.

The final record includes exact commands, exit codes, peak memory, proposal and
backup counts, both repository commits, configuration hash, database source
evidence, stage-manifest hashes, B3 statistical/final verdicts, and all
remaining run-level blockers.

## Acceptance Criteria

1. The dry-run input is exactly the immutable 56-ticker/5,781-coordinate anchor.
2. Every target coordinate has validated direct Wind share facts and passes the
   `1e-4` cross-field gate.
3. Sparse proposal nodes exactly reconstruct all target monthly values.
4. No valued canonical fact is overwritten or silently disagreed with.
5. Test-schema and repository regression suites pass.
6. A separately approved production transaction applies with exact row counts
   and a complete rollback package.
7. Current production audit proves zero SHARES and zero CLOSE blockers on the
   frozen window.
8. B3 reaches candidate statistics under the 8GB guard and publishes complete,
   hash-bound preflight/evaluation verdict evidence under unchanged rules.
