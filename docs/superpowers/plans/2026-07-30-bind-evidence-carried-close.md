# Bind Evidence to Carried Close Values Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make preflight prove that every accepted suspension evidence row matches the exact close actually used by each policy snapshot, while preserving the classifier's no-prior-official-day rejection boundary.

**Architecture:** Retain the already-computed winning valuation `close` in each policy snapshot, validate it as part of the frozen carry contract, and compare accepted artifact `previous_close` values with strict equality. Keep the evidence validator self-contained by relaxing only the prior-official presence requirement demonstrated by a real classifier-generated row.

**Tech Stack:** Python, pandas, NumPy, pytest.

---

### Task 1: Bind Snapshot Carry Contracts to Close Values

**Files:**
- Modify: `signals/style_basket/b3_build.py`
- Test: `tests/test_b3_exposures.py`

- [x] **Step 1: Write failing snapshot and preflight tests**

Add assertions that `build_policy_snapshots` exposes the actual original/carried `close`. Add interval and exact-shadow preflight cases where snapshot close `10.0` disagrees with artifact `previous_close=999.0` and must produce the unique `suspension_interval_evidence_alignment` blocker. Keep equal `10.0` cases legal. Add snapshot carry-contract cases for missing, duplicate, nonnumeric, and invalid carried closes.

- [x] **Step 2: Verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py -k 'snapshot_retains_actual_valuation_close or evidence_close_mismatch or invalid_snapshot_carry_contract' -q -W error
```

Expected: failures because snapshots do not expose `close`, reconciliation ignores values, and the carry contract does not require/validate `close`.

- [x] **Step 3: Implement the minimal close contract**

In `build_policy_snapshots`, add:

```python
"close": close.to_numpy(dtype=float),
```

In `_validated_snapshot_carry_contract`, require exactly one `close` column; accept only real numeric values or missing values; normalize to float; require every present close to be finite and positive; require carried rows to have a close; and retain it in the sorted contract.

In `_validate_suspension_interval_evidence_alignment`, retain `(accepted, previous_close)` per artifact key and, for accepted rows with an allowed carried method, require strict `snapshot_close == previous_close`. Report `close_mismatch` through the existing unique alignment blocker.

- [x] **Step 4: Verify GREEN**

Run the Step 2 command and confirm all selected cases pass with no warnings.

### Task 2: Preserve the No-Prior-Official-Day Classifier Boundary

**Files:**
- Modify: `signals/style_basket/b3_build.py`
- Test: `tests/test_b3_exposures.py`

- [x] **Step 1: Write a failing real-classifier roundtrip test**

Call `build_continuous_suspension_evidence` with an official calendar containing the suspension start but no earlier official day, a finite positive prior price, and an explicit `今起停牌` event. Assert it emits `PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY` with null `previous_official_trade_date`, then pass that row through `_preflight_interval_evidence`.

- [x] **Step 2: Verify RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py -k 'previous_close_not_prior_roundtrips_without_prior_official' -q -W error
```

Expected: `_preflight_interval_evidence` raises a row-semantics mismatch because it currently requires the official date.

- [x] **Step 3: Implement the minimal invariant correction**

For `PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY`, continue requiring a complete finite positive previous-close pair. Only reject equality when `previous_official_trade_date` is present:

```python
official_present & previous_close_date.eq(previous_official_trade_date)
```

- [x] **Step 4: Verify GREEN and regressions**

Run the new directed tests, Task 5 alignment/validator filters, Task 4 snapshot-carry filters, and the evaluation preflight-contract filter with `-W error`. Run `git diff --check`; do not run the full suite.

- [x] **Step 5: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py docs/superpowers/plans/2026-07-30-bind-evidence-carried-close.md
git commit -m "fix(b3): bind evidence to carried close values"
```
