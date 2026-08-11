# B3 Equal-Weight Input Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Let B3 consume the existing multi-column equal-weight production artifact by explicit column name, bind the exact control source in `structure_manifest.json`, and make evaluation fail closed if it would consume a different source.

**Architecture:** `backtest.b3_structure` will materialize a validated control load containing both the truncated `Series` and immutable provenance. File provenance binds the normalized source path, exact file-byte SHA-256, and the selected `date`/`factor_value` columns; injected series use a canonical post-cutoff series hash and are identified as in-memory. `backtest.b3_eval` will strictly validate the new manifest input block and compare it with the control it loads before any evaluation is built.

**Tech Stack:** Python 3, pandas, dataclasses, hashlib, pytest.

---

### Task 1: Freeze the relaxed loader and provenance contract

**Files:**
- Modify: `tests/test_b3_structure.py`
- Modify: `backtest/b3_structure.py`

- [x] **Step 1: Write failing loader tests**

Add tests proving that an eight-column production-shaped CSV is accepted, `factor_value` rather than `factor_value_raw` is selected, missing required columns and duplicate raw headers are rejected, and the returned load binds the source path, raw file SHA-256, and selected column names.

```python
loaded = _load_equal_weight_control(path, pd.Timestamp("2014-02-28"))
assert list(loaded.series) == pytest.approx([0.1, -0.2])
assert loaded.provenance.value_column == "factor_value"
assert loaded.provenance.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
```

- [x] **Step 2: Run the loader tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -k equal_weight_control -q
```

Expected: FAIL because the current loader rejects every extra column and returns a bare `Series` without provenance.

- [x] **Step 3: Implement the minimal loader contract**

Add frozen `EqualWeightControlProvenance` and `EqualWeightControlLoad` dataclasses. Read and hash the same byte buffer, reject duplicate CSV headers, require `date` and `factor_value`, explicitly select those columns, and preserve all existing date/numeric/cutoff validation.

```python
@dataclass(frozen=True)
class EqualWeightControlProvenance:
    source_kind: str
    path: str | None
    sha256: str
    date_column: str = "date"
    value_column: str = "factor_value"


@dataclass(frozen=True)
class EqualWeightControlLoad:
    series: pd.Series
    provenance: EqualWeightControlProvenance
```

- [x] **Step 4: Run the loader tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests PASS.

### Task 2: Bind the control in the structure manifest

**Files:**
- Modify: `tests/test_b3_structure.py`
- Modify: `backtest/b3_structure.py`

- [x] **Step 1: Write failing structure-manifest tests**

Extend the structure-runner test to assert this exact input shape:

```python
assert manifest["inputs"] == {
    "equal_weight_control": {
        "source_kind": "file",
        "path": expected_path,
        "sha256": hashlib.sha256(control_path.read_bytes()).hexdigest(),
        "date_column": "date",
        "value_column": "factor_value",
    }
}
```

Add an in-memory runner assertion proving injected test series are labeled `in_memory` and hashed canonically rather than attributed to an unused file.

- [x] **Step 2: Run the structure manifest tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -k 'manifest or equal_weight_control' -q
```

Expected: FAIL because `_write_structure_manifest` has no `inputs` field.

- [x] **Step 3: Thread provenance through `run_structure`**

Make file and injected-series branches produce `EqualWeightControlLoad`, pass its provenance into `_write_structure_manifest`, and serialize an exact `inputs.equal_weight_control` object. Preserve atomic manifest replacement and stale-output invalidation.

- [x] **Step 4: Run the structure tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: all structure tests PASS.

### Task 3: Verify the same control downstream

**Files:**
- Modify: `tests/test_b3_eval.py`
- Modify: `backtest/b3_eval.py`

- [x] **Step 1: Write failing provenance-verifier tests**

Update structure-manifest fixtures with the frozen input block. Assert `verify_structure_provenance` returns its validated binding and rejects missing/extra input keys, malformed hashes, unsupported source kinds, invalid paths, and changed column names.

- [x] **Step 2: Run verifier tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -k structure_provenance -q
```

Expected: FAIL because the current verifier rejects the new top-level `inputs` key and exposes no input binding.

- [x] **Step 3: Implement strict manifest-input validation**

Add `equal_weight_control` to `StructureProvenanceContract`; require the exact top-level manifest schema and exact nested keys; validate SHA-256, source kind, path semantics, and frozen selected column names before reading structure CSVs.

- [x] **Step 4: Write the failing cross-stage mismatch test**

Create a valid unblocked layout, bind one control in its structure manifest, then evaluate with either a changed file or changed injected series.

```python
with pytest.raises(DataBlocked, match="equal_weight control provenance mismatch"):
    run_evaluation(..., equal_weight_signal=changed_control)
```

- [x] **Step 5: Run the mismatch test and verify RED**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -k equal_weight_control_provenance -q
```

Expected: FAIL because evaluation currently consumes the second control without comparing it to structure provenance.

- [x] **Step 6: Enforce provenance before evaluation**

Load or canonicalize the evaluation control, compare its immutable provenance with `structure.equal_weight_control`, raise `DataBlocked` on any mismatch, and only then call `build_evaluation`. The final run manifest remains transitively bound through `stage_manifest_hashes.structure`.

- [x] **Step 7: Run eval tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -q
```

Expected: all evaluation tests PASS.

### Task 4: Regression verification

**Files:**
- Verify: `backtest/b3_structure.py`
- Verify: `backtest/b3_eval.py`
- Verify: `tests/test_b3_structure.py`
- Verify: `tests/test_b3_eval.py`

- [x] **Step 1: Run focused B3 tests**

```bash
python3 -m pytest tests/test_b3_structure.py tests/test_b3_eval.py -q
```

Expected: PASS with no warnings introduced by this change.

- [x] **Step 2: Run the full suite**

```bash
python3 -m pytest -q
```

Expected: PASS.

- [x] **Step 3: Inspect the final diff**

```bash
git diff --check
git diff -- backtest/b3_structure.py backtest/b3_eval.py tests/test_b3_structure.py tests/test_b3_eval.py docs/superpowers/plans/2026-08-11-b3-equal-weight-input-provenance.md
```

Expected: no whitespace errors; changes stay within the approved provenance scope.
