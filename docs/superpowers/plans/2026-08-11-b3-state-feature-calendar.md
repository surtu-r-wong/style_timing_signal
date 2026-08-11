# B3 State Feature Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make B3 treat complete zero-variance state windows as neutral, keep the structural calendar at 2014-10, freeze the model calendar at 2015-01, and complete the formal states → structure → eval provenance run.

**Architecture:** Add one shared backtest calendar module so structure and eval consume the same frozen dates. States will distinguish incomplete windows from complete zero-variance windows; structure will retain all structural formations for hard-sort auditing while using only model formations for features and fitting; eval will validate the full structural formation proof and then trim all score inputs to the same model calendar.

**Tech Stack:** Python 3, pandas, NumPy, pytest, Git bundle deployment, Windows Git through WSL, SHA-256 manifests.

---

## File map

- Create `backtest/b3_windows.py`: single source of truth for structural and model window dates.
- Modify `signals/style_basket/b3_states.py`: zero-variance z-score semantics only.
- Modify `backtest/b3_structure.py`: structural/model formation split and unified window consumers.
- Modify `backtest/b3_eval.py`: structural proof validation, model-calendar trimming, expected evidence labels, and diagnostics.
- Modify `tests/test_b3_portfolios_states.py`: causal warm-up, zero-variance, transition, and real-gap tests.
- Modify `tests/test_b3_structure.py`: dual-calendar, hard-sort-prefix, model completeness, and window-label tests.
- Modify `tests/test_b3_eval.py`: structural formation start, pre-model trimming, evidence-domain, disclosure-coverage, and yearly-history tests.
- Do not modify `signals/style_basket/b3_config.yaml`; the global config hash must remain unchanged so verified parents can be reused.

### Task 1: Freeze zero-variance state semantics

**Files:**
- Modify: `tests/test_b3_portfolios_states.py`
- Modify: `signals/style_basket/b3_states.py:117-144`

- [ ] **Step 1: Import the causal transform in the state tests**

Update the existing import block to include the unit under test:

```python
from signals.style_basket.b3_states import (
    _causal_transform,
    build_state_features,
    decompose_states,
)
```

- [ ] **Step 2: Write the failing zero-variance and real-gap tests**

Add these tests next to `test_state_transform_uses_full_past_windows_and_never_future_data`:

```python
def test_causal_transform_maps_only_complete_zero_variance_windows_to_zero():
    index = pd.bdate_range("2019-01-01", periods=85)
    component = pd.Series(0.0, index=index)
    component.iloc[80:] = 0.01

    raw, feature = _causal_transform(
        component,
        raw_window=20,
        z_window=40,
        tanh_scale=2.0,
        smoothing_window=5,
    )

    assert raw.iloc[:19].isna().all()
    assert feature.iloc[:62].isna().all()
    assert feature.iloc[62:80].eq(0.0).all()
    assert np.isfinite(feature.iloc[80:]).all()


def test_causal_transform_does_not_turn_a_real_gap_into_zero():
    index = pd.bdate_range("2019-01-01", periods=150)
    component = pd.Series(np.sin(np.arange(150) / 11.0), index=index)
    component.iloc[90] = np.nan

    _, feature = _causal_transform(
        component,
        raw_window=20,
        z_window=40,
        tanh_scale=2.0,
        smoothing_window=5,
    )

    assert feature.iloc[89] == pytest.approx(float(feature.iloc[89]))
    assert feature.iloc[90:149].isna().any()
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
python3 -m pytest \
  tests/test_b3_portfolios_states.py::test_causal_transform_maps_only_complete_zero_variance_windows_to_zero \
  tests/test_b3_portfolios_states.py::test_causal_transform_does_not_turn_a_real_gap_into_zero \
  -q
```

Expected: the first test fails because the current `.where(std >= 1e-8)` leaves complete constant windows as NaN; the real-gap test already remains fail-closed.

- [ ] **Step 4: Implement readiness-aware zero handling**

Replace the z-score portion of `_causal_transform` with:

```python
    ready = raw.notna() & mean.notna() & standard_deviation.notna()
    variable = ready & standard_deviation.ge(1.0e-8)
    constant = ready & standard_deviation.lt(1.0e-8)
    z_score = pd.Series(np.nan, index=component.index, dtype=float)
    z_score.loc[variable] = (
        (raw.loc[variable] - mean.loc[variable])
        / standard_deviation.loc[variable]
    )
    z_score.loc[constant] = 0.0
    transformed = np.tanh(z_score / tanh_scale)
```

Keep the existing raw, mean, standard deviation, and 5-day smoothing calls unchanged.

- [ ] **Step 5: Run the state feature tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_b3_portfolios_states.py -q
```

Expected: all tests pass; the existing first-62-row warm-up assertion remains true.

- [ ] **Step 6: Commit the zero-variance change**

```bash
git add signals/style_basket/b3_states.py tests/test_b3_portfolios_states.py
git commit -m "fix(b3): keep zero-variance state windows neutral"
```

### Task 2: Create the shared frozen calendar contract

**Files:**
- Create: `backtest/b3_windows.py`
- Modify: `tests/test_b3_structure.py`

- [ ] **Step 1: Write the failing calendar-constant test**

Add imports and this test near the structure window tests:

```python
from backtest.b3_windows import (
    MODEL_DISCOVERY_END,
    MODEL_DISCOVERY_START,
    MODEL_PERIOD_WINDOWS,
    MODEL_STATE_COVERAGE_WINDOWS,
    STRUCTURAL_DISCOVERY_START,
)


def test_structural_and_model_calendars_are_frozen_separately():
    assert STRUCTURAL_DISCOVERY_START == pd.Timestamp("2014-10-01")
    assert MODEL_DISCOVERY_START == pd.Timestamp("2015-01-01")
    assert MODEL_DISCOVERY_END == pd.Timestamp("2020-12-31")
    assert [window[0] for window in MODEL_PERIOD_WINDOWS] == [
        "2015-2017",
        "2018-2020",
        "2021-2023",
        "2024-2026-report-only",
    ]
    assert [window[0] for window in MODEL_STATE_COVERAGE_WINDOWS] == [
        "2015-2017",
        "2018-2020",
        "2021-2023",
        "2015-2020",
    ]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest \
  tests/test_b3_structure.py::test_structural_and_model_calendars_are_frozen_separately \
  -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'backtest.b3_windows'`.

- [ ] **Step 3: Create the shared constants module**

Create `backtest/b3_windows.py` with exactly:

```python
"""Frozen structural and model calendars shared by B3 structure and eval."""

from __future__ import annotations

import pandas as pd


STRUCTURAL_DISCOVERY_START = pd.Timestamp("2014-10-01")
STRUCTURAL_DISCOVERY_END = pd.Timestamp("2020-12-31")
MODEL_DISCOVERY_START = pd.Timestamp("2015-01-01")
MODEL_DISCOVERY_END = pd.Timestamp("2020-12-31")

MODEL_PERIOD_WINDOWS = (
    ("2015-2017", pd.Timestamp("2015-01-01"), pd.Timestamp("2017-12-31"), True),
    ("2018-2020", pd.Timestamp("2018-01-01"), pd.Timestamp("2020-12-31"), True),
    ("2021-2023", pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31"), True),
    (
        "2024-2026-report-only",
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2026-12-31"),
        False,
    ),
)

MODEL_STATE_COVERAGE_WINDOWS = (
    *MODEL_PERIOD_WINDOWS[:3],
    ("2015-2020", MODEL_DISCOVERY_START, MODEL_DISCOVERY_END, False),
)
```

- [ ] **Step 4: Run the constant test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit the shared contract**

```bash
git add backtest/b3_windows.py tests/test_b3_structure.py
git commit -m "refactor(b3): centralize frozen model windows"
```

### Task 3: Split structural and model calendars in structure

**Files:**
- Modify: `backtest/b3_structure.py:89-123,382-430,1329-1538,1658-2140`
- Modify: `tests/test_b3_structure.py`

- [ ] **Step 1: Extend the synthetic fixture and write the failing structural-prefix test**

In `_model_comparison_inputs`, start the synthetic daily calendar at
`2014-10-01`, keep target-return inputs finite, set state features before
`2015-01-01` to NaN, and do not create coefficient rows for the structural-only
prefix:

```python
    calendar = pd.bdate_range("2014-10-01", "2026-12-31")
    # After state_rows has been converted to the states DataFrame:
    pre_model = states["date"].lt("2015-01-01")
    states.loc[pre_model, ["F_U", "F_D", "F_X", "F_T"]] = np.nan
    # At the start of the coefficient formation loop:
    if formation < pd.Timestamp("2015-01-01"):
        continue
```

Then add this test:

```python
def test_model_comparison_excludes_structural_prefix_from_model_calendar():
    inputs = list(_model_comparison_inputs())
    baseline = build_model_comparison(*inputs)

    discovery = baseline[
        baseline["window"].eq("2015-2020")
        & baseline["model"].eq("M1")
        & baseline["gate_name"].eq("")
    ]
    hard_sort = baseline[baseline["gate_name"].eq("hard_sort_complete")]
    assert len(discovery) == 6
    assert hard_sort["gate_pass"].eq(True).all()

    surface = inputs[3]
    inputs[3] = surface[
        surface["formation_date"].ge("2015-01-01")
    ].copy()
    missing_prefix = build_model_comparison(*inputs)
    hard_sort = missing_prefix[
        missing_prefix["gate_name"].eq("hard_sort_complete")
    ]
    assert hard_sort["gate_pass"].eq(False).all()
```

The two hard-sort assertions prove that the complete structural prefix remains
audited even though its NaN state features are outside the model calendar.

- [ ] **Step 2: Write the failing post-2015 completeness test**

```python
def test_model_comparison_still_blocks_missing_model_formation_feature():
    inputs = list(_model_comparison_inputs())
    states = inputs[0].copy()
    first_model_formation = inputs[6][0]
    mask = states["date"].eq(first_model_formation)
    states.loc[mask, "F_X"] = np.nan
    inputs[0] = states

    with pytest.raises(DataBlocked, match="F_X|formation features"):
        build_model_comparison(*inputs)
```

- [ ] **Step 3: Run the new tests and verify RED**

```bash
python3 -m pytest \
  tests/test_b3_structure.py::test_model_comparison_excludes_structural_prefix_from_model_calendar \
  tests/test_b3_structure.py::test_model_comparison_still_blocks_missing_model_formation_feature \
  -q
```

Expected: the prefix test fails because the current validator requires the
2014-10--12 state features to be finite and emits `2014-2020`, while the
model-formation test remains fail-closed.

- [ ] **Step 4: Replace local window literals with the shared contract**

Import:

```python
from backtest.b3_windows import (
    MODEL_DISCOVERY_END,
    MODEL_DISCOVERY_START,
    MODEL_PERIOD_WINDOWS,
    MODEL_STATE_COVERAGE_WINDOWS,
    STRUCTURAL_DISCOVERY_START,
)
```

Set the compatibility alias used by existing coefficient code:

```python
WINDOW_SPECS = list(MODEL_PERIOD_WINDOWS)
```

Make `state_coverage_gate` iterate the shared tuple:

```python
    for window, start, end, affects_gate in MODEL_STATE_COVERAGE_WINDOWS:
        sample = state.loc[start:end].dropna()
        for label in ("UU", "DD", "DIV"):
            share = float(sample.eq(label).mean()) if len(sample) else 0.0
            result[f"{window}_{label}"] = share
            if affects_gate:
                passed &= share >= float(minimum)
```

- [ ] **Step 5: Return both validated calendars**

In `_validated_model_comparison_inputs`, validate `structural_formations`, then derive:

```python
    structural_formations = formation_dates.copy()
    structural_periods = structural_formations.to_period("M")
    if (
        len(structural_formations) < 2
        or structural_formations.tz is not None
        or not structural_formations.equals(structural_formations.normalize())
        or structural_formations.has_duplicates
        or not structural_formations.is_monotonic_increasing
        or structural_periods[0] != STRUCTURAL_DISCOVERY_START.to_period("M")
        or structural_periods.has_duplicates
        or not structural_periods.equals(
            pd.period_range(structural_periods[0], structural_periods[-1], freq="M")
        )
    ):
        raise DataBlocked("model structural formation dates are invalid")
    model_formations = structural_formations[
        structural_formations >= MODEL_DISCOVERY_START
    ]
    if (
        len(model_formations) < 2
        or model_formations[0].to_period("M")
        != MODEL_DISCOVERY_START.to_period("M")
    ):
        raise DataBlocked("model formation dates must start in 2015-01")
```

Use `model_formations` for state/axis filtering, formation-feature presence, target returns, and equal_weight validation. Return both indexes:

Update the function's return annotation from seven tuple members to eight, with
the final two members both declared as `pd.DatetimeIndex`.

```python
    return (
        states,
        axis,
        coefficients,
        surface,
        targets,
        control,
        structural_formations,
        model_formations,
    )
```

- [ ] **Step 6: Route each consumer to the correct calendar**

At the start of `build_model_comparison`, unpack both calendars and define:

```python
    structural_realized_formations = structural_formations[:-1]
    realized_formations = model_formations[:-1]
```

Use `structural_realized_formations` only for the `hard_sort_complete` row and its `n`. Use `model_formations` everywhere that builds next-formation targets, monthly state features, M0/M1 samples, stability, and control samples.

Build beta-sign windows from the first three shared model periods:

```python
        for _, start, end, affects_verdict in MODEL_PERIOD_WINDOWS[:3]:
            assert affects_verdict
            expected = _closed_formation_window(
                pd.DataFrame(index=realized_formations),
                model_formations,
                str(start.date()),
                str(end.date()),
            ).index
```

Build discovery and early samples with the shared dates:

```python
            discovery = _closed_formation_window(
                monthly,
                model_formations,
                str(MODEL_DISCOVERY_START.date()),
                str(MODEL_DISCOVERY_END.date()),
            )
            early = _closed_formation_window(
                monthly,
                model_formations,
                "2015-01-01",
                "2017-12-31",
            )
```

Emit the in-sample row as `window="2015-2020"`, and iterate `MODEL_STATE_COVERAGE_WINDOWS` for coverage rows.

- [ ] **Step 7: Update stale structure tests and comments**

Replace expectations that the three 2014 months enter M0/M1 or `2014-2020` coverage. Keep coefficient summary expectations at `2015-2017`, and update the protected-window set to:

```python
{"2015-2017", "2018-2020", "2015-2020"}
```

- [ ] **Step 8: Run all structure tests and verify GREEN**

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: all structure tests pass, including both new dual-calendar tests.

- [ ] **Step 9: Commit the structure split**

```bash
git add backtest/b3_structure.py tests/test_b3_structure.py
git commit -m "fix(b3): split structural and model calendars"
```

### Task 4: Apply the same model calendar in eval

**Files:**
- Modify: `backtest/b3_eval.py:197-200,880-930,1380-1530,2575-2726,2897-2930,4425-4515`
- Modify: `tests/test_b3_eval.py`

- [ ] **Step 1: Move eval fixtures to the structural start and write the failing trim test**

In `_evaluation_inputs`, start the synthetic calendar at `2014-10-01`. Set `F_U`, `F_D`, and `F_X` to NaN before `2015-01-01`, then add:

```python
def test_eval_accepts_structural_warmup_but_trims_scores_to_model_calendar():
    cfg, _, formations, states, targets, equal_weight, _ = _evaluation_inputs()

    validated = _validate_score_inputs(
        states, targets, equal_weight, formations, cfg
    )
    validated_states, validated_targets, _, model_formations, _, discovery = validated

    assert model_formations[0].to_period("M") == pd.Period("2015-01")
    assert validated_states["date"].min() == model_formations[0]
    assert validated_targets["500"].index.min() == model_formations[0]
    assert discovery == (
        pd.Timestamp("2015-01-01"),
        pd.Timestamp("2020-12-31"),
    )
```

Import `_validate_score_inputs` and the shared window constants in the test file.

- [ ] **Step 2: Write the failing eval model-gap test**

```python
def test_eval_still_blocks_nonfinite_feature_on_model_calendar():
    cfg, _, formations, states, targets, equal_weight, _ = _evaluation_inputs()
    first_model_date = formations[
        formations >= pd.Timestamp("2015-01-01")
    ][0]
    mask = states["date"].eq(first_model_date)
    states.loc[mask, "F_X"] = np.nan

    with pytest.raises(DataBlocked, match="F_X.*finite"):
        _validate_score_inputs(
            states, targets, equal_weight, formations, cfg
        )
```

- [ ] **Step 3: Run the new eval tests and verify RED**

```bash
python3 -m pytest \
  tests/test_b3_eval.py::test_eval_accepts_structural_warmup_but_trims_scores_to_model_calendar \
  tests/test_b3_eval.py::test_eval_still_blocks_nonfinite_feature_on_model_calendar \
  -q
```

Expected: the warm-up test fails because eval currently requires every pre-2015 feature to be finite and treats config's 2014-10 structural start as the model discovery start.

- [ ] **Step 4: Validate the structural proof and return model formations**

Import the shared constants. In `_validate_formations`, require the structural period range from `2014-10` through `2023-12`, verify exact last trading days, then return the frozen model subset:

```python
    required_periods = pd.period_range("2014-10", "2023-12", freq="M")
    if periods[0] != STRUCTURAL_DISCOVERY_START.to_period("M"):
        raise DataBlocked("formation dates must start in 2014-10")
    required_formations = formations[periods.isin(required_periods)]
    if not required_formations.to_period("M").equals(required_periods):
        raise DataBlocked("formation dates must cover every structural month")
    # Keep the existing exact-last-trading-day check here.
    model_formations = formations[formations >= MODEL_DISCOVERY_START]
    if (
        len(model_formations) < 2
        or model_formations[0].to_period("M")
        != MODEL_DISCOVERY_START.to_period("M")
    ):
        raise DataBlocked("model formation dates must start in 2015-01")
    return model_formations
```

- [ ] **Step 5: Trim score inputs only after validating the full sources**

In `_validate_score_inputs`, keep full-series finite/grid/blend checks first. After `_validate_formations`, define the exact model daily calendar:

```python
    formations = _validate_formations(formation_dates, calendar)
    model_calendar = calendar[
        (calendar >= formations.min()) & (calendar <= formations.max())
    ]
    targets = {name: series.loc[model_calendar] for name, series in targets.items()}
    control = control.loc[model_calendar]
    discovery = (MODEL_DISCOVERY_START, MODEL_DISCOVERY_END)
```

Filter state rows to `model_calendar.min()` through `model_calendar.max()` before numeric finiteness checks, then require every policy/q date grid to equal `model_calendar`. Return the trimmed targets, control, states, and model formations.

- [ ] **Step 6: Update every eval model-window consumer**

Apply these exact semantic changes:

- expected model evidence in-sample row: `2015-2020`;
- non-gate state coverage row: `2015-2020`;
- full yearly model diagnostic: `2015-2023`, starting at `MODEL_DISCOVERY_START`;
- yearly output label: `2015-2023`;
- `TRUE_DISCLOSURE_COVERAGE_BASIS` and its required structural period range: `2014-10` through `2023-12`.

Do not change confirmation, report-only, execution, bootstrap, or provenance thresholds.

- [ ] **Step 7: Update exact-domain fixtures and expected rows**

In `_valid_model_comparison`, emit `2015-2020` for the discovery and non-gate coverage rows. Update structural disclosure fixtures to start at `2014-10`, and update yearly assertions from `2014-2023` to `2015-2023`.

- [ ] **Step 8: Run all eval tests and verify GREEN**

```bash
python3 -m pytest tests/test_b3_eval.py -q
```

Expected: all eval tests pass, including strict equal_weight provenance tests.

- [ ] **Step 9: Commit the eval split**

```bash
git add backtest/b3_eval.py tests/test_b3_eval.py
git commit -m "fix(b3): score eval on the frozen model calendar"
```

### Task 5: Local regression and review gate

**Files:**
- Verify only; no intended edits.

- [ ] **Step 1: Run the three directly affected test modules**

```bash
python3 -m pytest tests/test_b3_portfolios_states.py tests/test_b3_structure.py tests/test_b3_eval.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full repository suite**

```bash
python3 -m pytest -q
```

Expected: all tests pass; only already-known pandas FutureWarnings may remain.

- [ ] **Step 3: Check exact diff scope and whitespace**

```bash
git status --short
git diff --check 1426a073b84fdac0201009466d4540757d37048b..HEAD
git diff --stat 1426a073b84fdac0201009466d4540757d37048b..HEAD
```

Expected source/test scope:

```text
backtest/b3_windows.py
backtest/b3_structure.py
backtest/b3_eval.py
signals/style_basket/b3_states.py
tests/test_b3_portfolios_states.py
tests/test_b3_structure.py
tests/test_b3_eval.py
docs/superpowers/specs/2026-08-11-b3-state-feature-calendar-design.md
docs/superpowers/plans/2026-08-11-b3-state-feature-calendar.md
```

- [ ] **Step 4: Invoke verification-before-completion and requesting-code-review**

Review the complete diff against the approved spec. Do not deploy if tests, window labels, parent reuse, or failure atomicity differ from the spec.

### Task 6: Deploy and complete the formal campaign

**Files:**
- Remote code: `D:/style_timing_signal`
- Formal research outputs: `D:/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research`
- Formal backtest outputs: `D:/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/backtest`
- Recoverable backup: `D:/deploy_stage/wsl2/b3-state-before-model-calendar-20260811`

- [ ] **Step 1: Create and verify a tested deployment bundle**

```bash
git bundle create /tmp/b3-state-calendar.bundle merge-equal-weight
git bundle verify /tmp/b3-state-calendar.bundle
sha256sum /tmp/b3-state-calendar.bundle
```

Expected: bundle verification succeeds and records the tested branch HEAD.

- [ ] **Step 2: Transfer the bundle and recheck the remote gate**

```bash
scp -P 2222 /tmp/b3-state-calendar.bundle ghls@100.120.152.1:D:/deploy_stage/wsl2/b3-state-calendar.bundle
```

Through `ssh -p 2222 ... wsl -e`, verify:

```text
Windows Git HEAD is still the expected deployed ancestor.
Windows Git status contains only the existing untracked campaign run directory.
No b3_structure, b3_eval, or guarded runner process is active.
Remote bundle SHA-256 equals the local SHA-256.
```

- [ ] **Step 3: Fast-forward with Windows Git through WSL**

Use `wsl -e "/mnt/c/Program Files/Git/cmd/git.exe"` to:

```text
bundle verify
fetch refs/heads/merge-equal-weight into FETCH_HEAD
verify FETCH_HEAD's ancestor equals the current remote HEAD
git diff --check HEAD..FETCH_HEAD
git merge --ff-only FETCH_HEAD
```

Do not use Linux Git for status or merge on the NTFS checkout because its CRLF configuration reports the whole worktree as modified.

- [ ] **Step 4: Run remote affected tests**

From `/mnt/d/style_timing_signal` with `/home/ghls/style_timing_signal/.venv/bin/python`:

```bash
python -m pytest tests/test_b3_portfolios_states.py tests/test_b3_structure.py tests/test_b3_eval.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 5: Snapshot the old states artifacts before mutation**

Create the explicit backup directory and copy, without deleting originals:

```bash
mkdir -p /mnt/d/deploy_stage/wsl2/b3-state-before-model-calendar-20260811
cp -p /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research/state_components.csv /mnt/d/deploy_stage/wsl2/b3-state-before-model-calendar-20260811/state_components.csv
cp -p /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research/manifests/states.json /mnt/d/deploy_stage/wsl2/b3-state-before-model-calendar-20260811/states.json
sha256sum /mnt/d/deploy_stage/wsl2/b3-state-before-model-calendar-20260811/*
```

- [ ] **Step 6: Rebuild only states from the verified portfolios parent**

Run this Python program from the remote repository; do not use `b3_build --stage states`, because that CLI cumulatively reruns exposures and portfolios:

```python
from pathlib import Path

import pandas as pd

from signals.common.config import load_db_config
from signals.style_basket.b3_build import default_sources, run_states_stage
from signals.style_basket.b3_config import load_b3_config

research = Path(
    "/mnt/d/style_timing_signal/"
    "data_fixes/2026-08-01-b3-wind-share-capital/run/research"
)
manifest = run_states_stage(
    load_b3_config(),
    default_sources(load_db_config()),
    pd.Timestamp("2023-12-31"),
    research,
)
print(manifest)
```

Expected: `manifests/states.json` is written with status `OK`. If this program fails, stop; the old CSV remains recoverable and the missing/invalid manifest prevents downstream trust.

- [ ] **Step 7: Verify the rebuilt states artifact before structure**

Check all of the following with a read-only Python probe:

```text
states.json has the exact expected schema and data_end 2023-12-31.
Its state_components.csv SHA-256 equals the file bytes.
All six policy/q groups have identical daily grids.
The first date on which F_U/F_D/F_X/F_T are all finite is 2015-01-30.
Every model feature from 2015-01-30 through 2023-12-29 is finite.
2014 warm-up rows remain NaN.
```

- [ ] **Step 8: Run formal structure**

```bash
python -m backtest.b3_structure --data-end 2023-12-31 --research-output-dir /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research --backtest-output-dir /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/backtest
```

Allowed exit codes: `0` or `3`. Both require a valid `structure_manifest.json`; exit `1` or a missing manifest is a stop condition.

- [ ] **Step 9: Verify structure output and equal_weight input provenance**

Read `structure_manifest.json` and assert:

```python
assert manifest["stage"] == "structure"
assert manifest["data_end"] == "2023-12-31"
assert set(manifest["outputs"]) == {
    "structure_coefficients.csv",
    "model_comparison.csv",
}
binding = manifest["inputs"]["equal_weight_control"]
assert binding["source_kind"] == "file"
assert binding["date_column"] == "date"
assert binding["value_column"] == "factor_value"
assert len(binding["sha256"]) == 64
```

Resolve the recorded path from the repository root and independently recompute its SHA-256; it must equal `binding["sha256"]`. Independently recompute both structure output hashes as well.

- [ ] **Step 10: Run formal eval without refreshing the control file**

```bash
python -m backtest.b3_eval --data-end 2023-12-31 --research-output-dir /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research --backtest-output-dir /mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/backtest
```

Allowed exit codes: `0` or `2`. Inspect `verdicts.csv` and `run_manifest.json`. Any remaining blocker must be an already registered research/data gate; neither `equal_weight control provenance mismatch` nor state-feature/model-calendar incompleteness is acceptable.

- [ ] **Step 11: Record final evidence**

Report:

```text
deployed Git commit
local affected and full-suite test counts
remote affected test count
old and new states hashes
structure output hashes
equal_weight path, date column, value column, and SHA-256
eval exit code and final verdict/blocker codes
backup directory and recoverability
```
