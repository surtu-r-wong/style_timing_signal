# Share-Capital Two-Stage Par Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the under-recovering 180-day-only par estimator with a monotonic 30-day-primary/180-day-fallback estimator, strengthen the production recovery regression gate, rerun the idempotent backfill, and reverify B3.

**Architecture:** Keep the existing bounded 180-day database read. Inside the pure par estimator, first snap the median implied par from the first 30 calendar days after `CSMAR_END`; only if that fails, fall back to the current full-window median. Extend the recovery verifier so a ticker newly entering the B3 historical gap set fails the run even when it retains a later valued node.

**Tech Stack:** Python 3.13, pandas, NumPy, psycopg2, pytest, PostgreSQL, systemd memory scopes.

**Approved design:** `docs/superpowers/specs/2026-07-27-share-capital-two-stage-par-calibration-design.md`

---

## File Map

- Modify: `/home/elfbob/claude-code/stock_selector/stock_selector/backfill/share_capital.py`
  - Implements the two-stage pure par estimator.
- Modify: `/home/elfbob/claude-code/stock_selector/tests/test_share_capital_backfill.py`
  - Pins primary-window recovery, fallback preservation, and double-failure behavior.
- Modify: `data_fixes/2026-07-25-share-capital-par/verify_par_recovery.py`
  - Adds the new-historical-gap regression set and nonzero exit gate.
- Create: `tests/test_verify_par_recovery.py`
  - Proves `phase_after` fails when a ticker newly enters the historical gap while remaining valued.
- Modify: `data_fixes/2026-07-25-share-capital-par/README.md`
  - Records the two-stage rerun and final measured results.
- Preserve and finally commit:
  - `data_fixes/2026-07-25-share-capital-par/gap_before.csv`
  - `data_fixes/2026-07-25-share-capital-par/valued_tickers_before.csv`
  - `data_fixes/2026-07-25-share-capital-par/gap_after.csv`
  - `data_fixes/2026-07-25-share-capital-par/tail.csv`

### Task 1: Create an isolated stock_selector worktree

**Files:** none.

- [ ] **Step 1: Invoke the required worktree workflow**

Invoke `superpowers:using-git-worktrees`. Create branch
`fix/share-capital-two-stage` from `c31104e` in:

```text
/tmp/stock-selector-par-two-stage
```

- [ ] **Step 2: Verify the source repository and worktree**

Run:

```bash
git -C /home/elfbob/claude-code/stock_selector status --short --branch
git -C /tmp/stock-selector-par-two-stage status --short --branch
git -C /tmp/stock-selector-par-two-stage log -1 --oneline
```

Expected:

```text
original: master at c31104e, with only the known pre-existing untracked files
worktree: clean fix/share-capital-two-stage branch at c31104e
```

- [ ] **Step 3: Make the existing private settings visible without copying credentials**

Run:

```bash
ln -s /home/elfbob/claude-code/stock_selector/config/settings.yaml /tmp/stock-selector-par-two-stage/config/settings.yaml
```

Expected: the worktree's ignored `config/settings.yaml` is a symlink to the existing private configuration.

### Task 2: Implement the two-stage par estimator with TDD

**Files:**
- Modify: `/tmp/stock-selector-par-two-stage/tests/test_share_capital_backfill.py`
- Modify: `/tmp/stock-selector-par-two-stage/stock_selector/backfill/share_capital.py`

- [ ] **Step 1: Invoke the required TDD workflow**

Invoke `superpowers:test-driven-development` before editing either file.

- [ ] **Step 2: Add the primary-window failing test**

Add this helper and test after the existing `_fake_overlap` helper:

```python
def _two_stage_case(ts_code, implied_par_points):
    balance = _fake_csmar_balance([
        (ts_code, "2025-03-31", "2025-04-30", 1_000_000_000),
    ])
    overlap = _fake_overlap([
        (
            ts_code,
            trade_date,
            (1_000_000_000 / implied_par) * 10.0,
            10.0,
        )
        for trade_date, implied_par in implied_par_points
    ])
    return build_share_capital(balance, overlap)


def test_par_uses_30_day_primary_before_later_share_drift():
    out = _two_stage_case("PRIMARY.SZ", [
        ("2025-04-01", 1.0),
        ("2025-04-15", 1.0),
        ("2025-05-02", 0.8),
        ("2025-06-02", 0.8),
        ("2025-07-02", 0.8),
    ])

    assert out["par_value"].iloc[0] == 1.0
    assert out["quality_flag"].iloc[0] is None
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
cd /tmp/stock-selector-par-two-stage
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest \
  tests/test_share_capital_backfill.py::test_par_uses_30_day_primary_before_later_share_drift -v
```

Expected: FAIL because the current full-window median is `0.8`, so `par_value` is `NaN`.

- [ ] **Step 4: Implement the minimal two-stage estimator**

In `stock_selector/backfill/share_capital.py`, change the imports and inference code to:

```python
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from stock_selector.data.financial_field_map import (
    CSMAR_END,
    legal_disclosure_deadline,
)


_STANDARD_PARS: tuple[float, ...] = (1.0, 0.1)
_PAR_TOLERANCE: float = 0.05
_PRIMARY_PAR_WINDOW_DAYS: int = 30
```

Add the median helper immediately after `_snap_to_standard_par`:

```python
def _snap_median_to_standard_par(implied_pars: list[float]) -> Optional[float]:
    if not implied_pars:
        return None
    return _snap_to_standard_par(float(np.median(implied_pars)))
```

Replace the `implied_pars` accumulation and final return inside
`_infer_par_for_ticker` with:

```python
    implied_pars: list[tuple[date, float]] = []
    for _, ov in ticker_overlap.iterrows():
        close = ov["close"]
        total_mv = ov["total_mv"]
        if close is None or total_mv is None:
            continue
        if not np.isfinite(close) or not np.isfinite(total_mv) or close <= 0:
            continue
        implied_shares = total_mv / close
        if implied_shares <= 0:
            continue
        trade_dt = pd.Timestamp(ov["trade_date"])
        idx = (balance_periods - trade_dt).abs().idxmin()
        a003101000 = balance_sorted.loc[idx, "A003101000"]
        if a003101000 is None or not np.isfinite(a003101000) or a003101000 <= 0:
            continue
        implied_pars.append(
            (
                trade_dt.date(),
                float(a003101000) / float(implied_shares),
            )
        )

    primary_end = CSMAR_END + timedelta(days=_PRIMARY_PAR_WINDOW_DAYS)
    primary = [
        implied
        for trade_date, implied in implied_pars
        if CSMAR_END <= trade_date < primary_end
    ]
    primary_par = _snap_median_to_standard_par(primary)
    if primary_par is not None:
        return primary_par
    return _snap_median_to_standard_par(
        [implied for _, implied in implied_pars]
    )
```

Update `_infer_par_for_ticker`'s docstring to state that it tries the
`[CSMAR_END, CSMAR_END + 30 days)` median first and falls back to the full
supplied overlap median.

- [ ] **Step 5: Run the primary-window test and verify GREEN**

Run the Step-3 command again.

Expected: PASS.

- [ ] **Step 6: Add fallback and double-failure characterization tests**

Add:

```python
def test_par_falls_back_to_180_day_median_when_primary_cannot_snap():
    out = _two_stage_case("FALLBACK.SZ", [
        ("2025-04-01", 1.49),
        ("2025-04-15", 1.49),
        ("2025-05-02", 1.0),
        ("2025-06-02", 1.0),
        ("2025-07-02", 1.0),
    ])

    assert out["par_value"].iloc[0] == 1.0
    assert out["quality_flag"].iloc[0] is None


def test_par_remains_unknown_when_primary_and_fallback_both_fail():
    out = _two_stage_case("UNKNOWN.SZ", [
        ("2025-04-01", 0.8),
        ("2025-04-15", 0.8),
        ("2025-05-02", 0.7),
        ("2025-06-02", 0.7),
        ("2025-07-02", 0.7),
    ])

    assert pd.isna(out["par_value"].iloc[0])
    assert pd.isna(out["total_shares"].iloc[0])
    assert out["quality_flag"].iloc[0] == "par_unknown"
```

- [ ] **Step 7: Run the complete pure share-capital test module**

Run:

```bash
cd /tmp/stock-selector-par-two-stage
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest \
  tests/test_share_capital_backfill.py -q
```

Expected: all tests pass, including the three new tests and the existing par
`1.0`, par `0.1`, `par_unknown`, and overlap-anchor tests.

- [ ] **Step 8: Commit the stock_selector implementation**

Run:

```bash
git -C /tmp/stock-selector-par-two-stage add \
  stock_selector/backfill/share_capital.py \
  tests/test_share_capital_backfill.py
git -C /tmp/stock-selector-par-two-stage commit \
  -m "fix(share-capital): use tight par window with safe fallback"
```

Expected: one commit containing only the estimator and its tests.

### Task 3: Harden the production recovery regression gate with TDD

**Files:**
- Modify: `data_fixes/2026-07-25-share-capital-par/verify_par_recovery.py`
- Create: `tests/test_verify_par_recovery.py`

- [ ] **Step 1: Add a failing phase-after regression test**

Create `tests/test_verify_par_recovery.py`:

```python
from __future__ import annotations

import csv
import importlib.util
from datetime import date
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "data_fixes"
    / "2026-07-25-share-capital-par"
    / "verify_par_recovery.py"
)
SPEC = importlib.util.spec_from_file_location("verify_par_recovery", MODULE_PATH)
assert SPEC is not None
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERIFY)


def _write_csv(path, header, rows):
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def test_phase_after_fails_for_new_historical_gap_even_if_still_valued(
    tmp_path, monkeypatch, capsys
):
    _write_csv(
        tmp_path / "gap_before.csv",
        ["ts_code", "list_date", "first_eff"],
        [["OLD.SZ", "2010-01-01", "2025-04-01"]],
    )
    _write_csv(
        tmp_path / "valued_tickers_before.csv",
        ["ts_code"],
        [["OLD.SZ"], ["NEW.SZ"]],
    )
    gap_after = [
        ("OLD.SZ", date(2010, 1, 1), date(2025, 4, 1)),
        ("NEW.SZ", date(2011, 1, 1), date(2025, 4, 1)),
    ]
    valued_after = [("OLD.SZ",), ("NEW.SZ",)]
    answers = iter([gap_after, valued_after, []])
    monkeypatch.setattr(VERIFY, "HERE", tmp_path)
    monkeypatch.setattr(VERIFY, "_rows", lambda *args: next(answers))

    result = VERIFY.phase_after(object(), "stock_selector")

    captured = capsys.readouterr()
    assert result == 1
    assert "NEW HISTORICAL GAP REGRESSION: 1" in captured.out
    assert "NEW.SZ" in captured.err
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
cd /home/elfbob/claude-code/style_timing_signal
python -m pytest \
  tests/test_verify_par_recovery.py::test_phase_after_fails_for_new_historical_gap_even_if_still_valued -v
```

Expected: FAIL because the current verifier returns `0` when the valued ticker
set is unchanged.

- [ ] **Step 3: Implement the new-historical-gap gate**

In `phase_after`, immediately after constructing `gap_after` and
`valued_after`, add:

```python
    recovered = gap_before - gap_after
    regressed = valued_before - valued_after
    new_historical_gaps = gap_after - gap_before
```

Replace the summary and return block with:

```python
    print(f"AFTER: gap={len(gap_after)} tickers (was {len(gap_before)})")
    print(f"  recovered (dropped out of gap): {len(recovered)}  (expected ~639)")
    print(f"  residual tail still gapped:     {len(gap_after)}  (expected ~57)")
    print(f"  REGRESSION (valued->unvalued):  {len(regressed)}  (MUST be 0)")
    print(
        "  NEW HISTORICAL GAP REGRESSION: "
        f"{len(new_historical_gaps)}  (MUST be 0)"
    )
    if regressed:
        print("  !! regressed tickers:", sorted(regressed)[:20], file=sys.stderr)
        print(
            "  !! forward-window skew (reviewer M1) may have flipped these; "
            "consider a centered window before accepting the rerun.",
            file=sys.stderr,
        )
    if new_historical_gaps:
        print(
            "  !! new historical gap tickers:",
            sorted(new_historical_gaps)[:20],
            file=sys.stderr,
        )
    if regressed or new_historical_gaps:
        return 1
    return 0
```

Keep the existing explanatory forward-window warning only for `regressed`;
do not print it for a new historical gap unless `regressed` is also nonempty.

- [ ] **Step 4: Run the verifier test and verify GREEN**

Run the Step-2 command again.

Expected: PASS.

- [ ] **Step 5: Run the complete verifier test module**

Run:

```bash
python -m pytest tests/test_verify_par_recovery.py -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit the verifier, its test, and the preloaded runbook**

Run:

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/README.md \
  data_fixes/2026-07-25-share-capital-par/verify_par_recovery.py \
  tests/test_verify_par_recovery.py
git commit -m "fix: detect new historical gaps in par recovery"
```

Expected: the CSV snapshots remain untracked until final production
verification; no B3 output is included.

### Task 4: Verify and review both code changes

**Files:** all files changed in Tasks 2 and 3.

- [ ] **Step 1: Run the targeted stock_selector suites**

Run:

```bash
cd /tmp/stock-selector-par-two-stage
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest \
  tests/test_share_capital_backfill.py \
  tests/test_writers_share_capital.py \
  tests/test_share_capital_indicator.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete stock_selector suite**

Run:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -q
```

Expected: `1819 passed` (the prior `1816` plus three tests), with no failures.

- [ ] **Step 3: Run the complete style_timing_signal suite**

Run:

```bash
cd /home/elfbob/claude-code/style_timing_signal
python -m pytest -q
```

Expected: all existing tests plus `test_verify_par_recovery.py` pass.

- [ ] **Step 4: Check patch hygiene**

Run:

```bash
git -C /tmp/stock-selector-par-two-stage diff c31104e --check
git -C /home/elfbob/claude-code/style_timing_signal diff HEAD~1 --check
```

Expected: no whitespace errors.

- [ ] **Step 5: Invoke code review**

Invoke `superpowers:requesting-code-review`. The review must check:

1. primary-window boundary semantics are exactly
   `[CSMAR_END, CSMAR_END + 30 days)`;
2. fallback is behaviorally identical to the previous 180-day median;
3. no standard denomination or tolerance changed;
4. `indicator_implied` is untouched;
5. new historical gaps cause a nonzero verifier exit.

Resolve any Important findings with the same TDD cycle and rerun the affected
suite. Do not make unrelated cleanup changes.

- [ ] **Step 6: Fast-forward the verified stock_selector commit onto local master**

Run:

```bash
git -C /home/elfbob/claude-code/stock_selector merge \
  --ff-only fix/share-capital-two-stage
git -C /home/elfbob/claude-code/stock_selector log -2 --oneline
```

Expected: local `master` advances from `c31104e` to the reviewed two-stage
commit. Do not push origin.

### Task 5: Sandbox, production rerun, and final recovery gate

**Files:**
- Preserve: `gap_before.csv`, `valued_tickers_before.csv`
- Rewrite: `gap_after.csv`, `tail.csv`

- [ ] **Step 1: Confirm the original baseline has not been overwritten**

Run:

```bash
cd /home/elfbob/claude-code/style_timing_signal/data_fixes/2026-07-25-share-capital-par
wc -l gap_before.csv valued_tickers_before.csv
python -c "import csv; print(len(list(csv.reader(open('gap_before.csv'))))-1)"
```

Expected:

```text
gap_before.csv contains 696 data rows
valued_tickers_before.csv contains 5200 data rows
```

Do **not** run `verify_par_recovery.py --phase before` again.

- [ ] **Step 2: Run the test-schema backfill**

Run:

```bash
cd /home/elfbob/claude-code/stock_selector
env PGCONNECT_TIMEOUT=15 PGTCPUSERTIMEOUT=30000 \
  .venv/bin/python -m stock_selector.backfill.cli share-capital --use-test
```

Expected: exit `0`, a coverage CSV, and no exception. The test schema is sparse,
so its historical B3 gap count is not an acceptance metric.

- [ ] **Step 3: Run the production backfill exactly once**

Run:

```bash
env PGCONNECT_TIMEOUT=15 PGTCPUSERTIMEOUT=30000 \
  .venv/bin/python -m stock_selector.backfill.cli share-capital
```

Expected: exit `0`; an idempotent UPSERT of the full share-capital table.
Do not start a second process while this one is running and do not auto-retry a
failure.

- [ ] **Step 4: Run the final after gate**

Run:

```bash
cd /home/elfbob/claude-code/style_timing_signal/data_fixes/2026-07-25-share-capital-par
env PGCONNECT_TIMEOUT=10 PGTCPUSERTIMEOUT=30000 \
  python verify_par_recovery.py --phase after
```

Expected:

```text
recovered approximately 639-640
residual tail approximately 56-57
REGRESSION (valued->unvalued): 0
NEW HISTORICAL GAP REGRESSION: 0
```

If either regression count is nonzero or recovery is materially below `639`,
stop before B3 and diagnose. Do not tune tolerance or add par denominations.

### Task 6: Rebuild B3 preflight and rerun evaluation under the memory guard

**Files:**
- Regenerate: `output/style_basket/b3/coverage_audit.csv`
- Regenerate: `output/style_basket/b3/manifests/preflight.json`
- Regenerate: `backtest/output/b3/verdicts.csv`
- Regenerate: `backtest/output/b3/run_manifest.json`

- [ ] **Step 1: Rebuild the standalone preflight**

Run:

```bash
cd /home/elfbob/claude-code/style_timing_signal
systemd-run --user --scope -p MemoryMax=8G \
  python -m signals.style_basket.b3_build \
  --stage preflight --data-end 2023-12-31
```

Expected: exit `2` is acceptable if the residual share/close tail still causes
`DATA_BLOCKED`; the command must regenerate the preflight manifest and coverage
audit. Exit `3`, OOM termination, missing audit outputs, or an unhandled
exception is a stop condition.

- [ ] **Step 2: Run B3 evaluation against the new verified preflight**

Run:

```bash
systemd-run --user --scope -p MemoryMax=8G \
  python -m backtest.b3_eval --data-end 2023-12-31
```

Expected: exit `2` remains acceptable while any fail-closed data tail exists.
The run manifest must bind the newly generated preflight manifest.

- [ ] **Step 3: Recompute the exact three-part audit**

Run:

```bash
python -c '
import json
import pandas as pd

d = pd.read_csv("output/style_basket/b3/coverage_audit.csv")
size = d[
    (d["pit_policy"] == "legal_deadline")
    & (d["check"] == "size_exclusion")
]
reasons = size.groupby("side")["eligible_count"].sum()
months = size[
    size["side"].isin(["DATA_MISSING_CLOSE", "DATA_MISSING_SHARES"])
].groupby("side")["formation_date"].nunique()
carry = d[
    (d["pit_policy"] == "legal_deadline")
    & (d["check"] == "close_carry_forward")
].groupby("side")["eligible_count"].sum()
required = d[
    (d["pit_policy"] == "legal_deadline")
    & (d["check"] == "monthly_exposure")
    & (d["required_formation"] == True)
]
manifest = json.load(open("backtest/output/b3/run_manifest.json"))

print("reason_ticket_months", {
    key: int(reasons.get(key, 0))
    for key in ("DATA_MISSING_CLOSE", "DATA_MISSING_SHARES")
})
print("reason_months", {key: int(value) for key, value in months.items()})
print("suspended_carry", int(carry.get("SUSPENDED_CARRY_FORWARD", 0)))
print("required_month_status", required["status"].value_counts().to_dict())
print("invalid_formation_months", len(manifest.get("invalid_formation_months", [])))
'
```

Expected:

- `DATA_MISSING_SHARES` falls sharply from `46,004`;
- `DATA_MISSING_CLOSE` remains approximately `202`;
- `SUSPENDED_CARRY_FORWARD` remains approximately `13,475`;
- any remaining blocked months are explained by the explicit residual tail.

- [ ] **Step 4: Inspect the final blocker set with bounded output**

Run:

```bash
sed -n '1,20p' backtest/output/b3/verdicts.csv
python -c "import json; m=json.load(open('backtest/output/b3/run_manifest.json')); print({'final_verdict':m.get('final_verdict'),'invalid_formation_months':len(m.get('invalid_formation_months',[])),'stage_manifest_hashes':m.get('stage_manifest_hashes',{})})"
```

Expected: the report reflects the new preflight hash and contains no database
source-evidence regression.

### Task 7: Finalize audit artifacts and handoff

**Files:**
- Modify: `data_fixes/2026-07-25-share-capital-par/README.md`
- Add: the four recovery CSV snapshots
- Update external memories:
  - `/home/elfbob/.claude/projects/-home-elfbob-claude-code-style-timing-signal/memory/project-b3-task10-state.md`
  - `/home/elfbob/.claude/projects/-home-elfbob-claude-code-style-timing-signal/memory/MEMORY.md`

- [ ] **Step 1: Update the runbook with measured final results**

Record:

- the new stock_selector commit hash;
- final recovered, residual-tail, valued regression, and new-gap regression counts;
- final B3 `DATA_MISSING_SHARES` ticket-months and invalid-month count;
- that the tail remains a user decision and was not auto-resolved.

- [ ] **Step 2: Commit the reproducible audit artifacts**

Run:

```bash
git add \
  data_fixes/2026-07-25-share-capital-par/README.md \
  data_fixes/2026-07-25-share-capital-par/gap_before.csv \
  data_fixes/2026-07-25-share-capital-par/valued_tickers_before.csv \
  data_fixes/2026-07-25-share-capital-par/gap_after.csv \
  data_fixes/2026-07-25-share-capital-par/tail.csv
git commit -m "chore: record share-capital par recovery audit"
```

Expected: only the runbook and four CSV audit files are added; generated B3
output remains untracked.

- [ ] **Step 3: Update the two project memories**

Use `apply_patch` to replace the 07-25 resume state with the exact final counts,
commit hashes, B3 outcome, and unresolved tail decision. Do not alter unrelated
memory entries.

- [ ] **Step 4: Invoke verification-before-completion**

Invoke `superpowers:verification-before-completion`. Recheck:

```bash
git -C /home/elfbob/claude-code/stock_selector status --short --branch
git -C /home/elfbob/claude-code/stock_selector log -2 --oneline
git -C /home/elfbob/claude-code/style_timing_signal status --short --branch
git -C /home/elfbob/claude-code/style_timing_signal log -4 --oneline
```

Report the exact test outputs, recovery counts, B3 counts, commits, untracked
pre-existing files, and that neither repository was pushed.

- [ ] **Step 5: Invoke finishing-a-development-branch**

Invoke `superpowers:finishing-a-development-branch` to clean up the isolated
worktree/branch after the stock_selector commit has been fast-forwarded onto
local master. Do not push either repository without a separate user decision.
