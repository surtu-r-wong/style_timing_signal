# P0 Experiment Evidence Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the pre-fix experiment evidence, rerun Gate 0R and both five-bucket experiments under the repaired D/P factor, and make every authoritative verdict traceable to immutable inputs and manifests.

**Architecture:** Add a small immutable-run manifest layer, make the five existing CLIs accept explicit input/output paths, and orchestrate the P0 rerun into a unique run directory. Preserve the nine loose research CSVs as a legacy snapshot, archive B3 core evidence in Git plus the 121 MiB formal run in a checksummed external tarball, then update verdict documents only from the new machine outputs.

**Tech Stack:** Python 3, pathlib, argparse, subprocess, hashlib, JSON, pandas, pytest, Git, PostgreSQL read-only queries.

---

## File map

- Create `backtest/run_manifest.py`: immutable run IDs, artifact hashes, atomic manifests, read-only database cutoffs.
- Create `backtest/p0_revalidation.py`: fixed command graph, fail-closed execution, verdict validation, old/new comparison.
- Create `tools/verify_b3_formal_archive.py`: verify core files and optional external B3 tarball against the committed inventory.
- Create `tests/test_bt_run_manifest.py`: pure manifest and cutoff tests.
- Create `tests/test_bt_p0_revalidation.py`: CLI path isolation, orchestration order, failure behavior, comparison tests.
- Create `tests/test_b3_formal_archive.py`: inventory verifier tests.
- Modify `backtest/gate0_runner.py`: injectable output directory and argparse CLI.
- Modify `backtest/tail_pair_runner.py`: move import-time execution into `main()` and add output directory.
- Modify `backtest/fifth_bucket_formal.py`: explicit tail input/output directory and complete verdict metadata.
- Modify `backtest/geometric_pairs_runner.py`: explicit output directory.
- Modify `backtest/geometric_5b_formal.py`: explicit geometric input/output directory and complete verdict metadata.
- Modify `.gitignore`: ignore flat `backtest/output/*.log`, while immutable run evidence remains tracked.
- Create `backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/`: nine CSV snapshot, cited R3 log, manifest.
- Create a timestamped directory beneath `backtest/output/runs/`; the orchestrator prints its exact path as `RUN_DIR=`.
- Create `data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/`: core B3 evidence, inventory, restore guide.
- Modify the Gate 0, fifth-bucket, geometric-bucket, data-repair, and backtest index documents after the rerun.

### Task 1: Immutable run manifest primitives

**Files:**
- Create: `backtest/run_manifest.py`
- Create: `tests/test_bt_run_manifest.py`

- [ ] **Step 1: Write failing tests for run creation, hashes, atomic status, and cutoffs**

```python
# tests/test_bt_run_manifest.py
import hashlib
import json
from pathlib import Path

import pytest

from backtest.run_manifest import (
    artifact_record,
    create_run_dir,
    git_state,
    query_table_cutoffs,
    write_manifest,
)


def test_create_run_dir_refuses_overwrite(tmp_path):
    run = create_run_dir(tmp_path, "20260821T120000-gate0r-abcdef0")
    assert run == tmp_path / "20260821T120000-gate0r-abcdef0"
    with pytest.raises(FileExistsError):
        create_run_dir(tmp_path, run.name)


def test_artifact_record_is_relative_and_hashed(tmp_path):
    p = tmp_path / "outputs" / "value.csv"
    p.parent.mkdir()
    p.write_bytes(b"a,b\n1,2\n")
    got = artifact_record(p, tmp_path)
    assert got == {
        "path": "outputs/value.csv",
        "size": 8,
        "sha256": hashlib.sha256(b"a,b\n1,2\n").hexdigest(),
    }


def test_write_manifest_is_machine_readable_and_complete(tmp_path):
    run = create_run_dir(tmp_path, "run-1")
    payload = {"status": "complete", "seed": 0, "command": ["python", "-m", "x"]}
    write_manifest(run, payload)
    assert json.loads((run / "manifest.json").read_text()) == payload
    assert not (run / ".manifest.json.tmp").exists()


def test_write_manifest_keeps_previous_file_if_replace_fails(tmp_path, monkeypatch):
    run = create_run_dir(tmp_path, "run-atomic")
    write_manifest(run, {"status": "running"})
    monkeypatch.setattr("backtest.run_manifest.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        write_manifest(run, {"status": "complete"})
    assert json.loads((run / "manifest.json").read_text()) == {"status": "running"}


def test_git_state_records_commit_and_dirty(monkeypatch):
    answers = iter(["abcdef0123456789\n", " M backtest/engine.py\n"])
    monkeypatch.setattr("backtest.run_manifest._git_output", lambda *_: next(answers))
    assert git_state(Path(".")) == {"commit": "abcdef0123456789", "dirty": True}


class FakeCursor:
    def __init__(self):
        self.value = None

    def execute(self, sql):
        self.value = ("2026-08-20",)

    def fetchone(self):
        return self.value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def cursor(self):
        return FakeCursor()


def test_query_table_cutoffs_uses_explicit_contract():
    got = query_table_cutoffs(FakeConnection(), "stock_selector", {
        "index_daily": "trade_date",
        "stock_financial": "end_date",
    })
    assert got == {"index_daily": "2026-08-20", "stock_financial": "2026-08-20"}
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_run_manifest.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: backtest.run_manifest`.

- [ ] **Step 3: Implement the minimal manifest module**

```python
# backtest/run_manifest.py
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def create_run_dir(root: Path, run_id: str) -> Path:
    target = Path(root) / run_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "inputs").mkdir()
    (target / "outputs").mkdir()
    (target / "logs").mkdir()
    return target


def artifact_record(path: Path, base: Path) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def write_manifest(run_dir: Path, payload: dict) -> Path:
    target = Path(run_dir) / "manifest.json"
    temporary = Path(run_dir) / ".manifest.json.tmp"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def git_state(root: Path) -> dict[str, object]:
    return {
        "commit": _git_output(root, "rev-parse", "HEAD").strip(),
        "dirty": bool(_git_output(root, "status", "--porcelain").strip()),
    }


def query_table_cutoffs(connection, schema: str, contract: dict[str, str]) -> dict[str, str]:
    out = {}
    with connection.cursor() as cursor:
        for table, column in contract.items():
            cursor.execute(f"SELECT max({column})::text FROM {schema}.{table}")
            value = cursor.fetchone()[0]
            if value is None:
                raise RuntimeError(f"{schema}.{table}.{column} has no cutoff")
            out[table] = value
    return out
```

- [ ] **Step 4: Run focused tests**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_run_manifest.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit manifest primitives**

```bash
git add backtest/run_manifest.py tests/test_bt_run_manifest.py
git commit -m "feat(backtest): add immutable run manifests"
```

### Task 2: Isolate all five experiment CLIs from flat output paths

**Files:**
- Modify: `backtest/gate0_runner.py`
- Modify: `backtest/tail_pair_runner.py`
- Modify: `backtest/fifth_bucket_formal.py`
- Modify: `backtest/geometric_pairs_runner.py`
- Modify: `backtest/geometric_5b_formal.py`
- Create: `tests/test_bt_p0_revalidation.py`

- [ ] **Step 1: Write failing path-isolation tests**

```python
# tests/test_bt_p0_revalidation.py
import ast
import json
from pathlib import Path

import pandas as pd
import pytest

import backtest.gate0_runner as gate0
import backtest.geometric_pairs_runner as geo_runner


def test_gate0_dump_uses_explicit_output_dir(tmp_path):
    gate0.dump("gate0r_result", {"pass": True}, outdir=tmp_path)
    assert json.loads((tmp_path / "gate0r_result.json").read_text())["pass"] is True


class Pair:
    growth = pd.Series([0.01], index=pd.to_datetime(["2026-01-02"]))
    value = pd.Series([0.00], index=pd.to_datetime(["2026-01-02"]))
    n_growth = {"2025-12-15": 1}
    n_value = {"2025-12-15": 1}
    skipped = []


def test_tail_runner_has_no_top_level_execution():
    path = Path("backtest/tail_pair_runner.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [node.lineno for node in tree.body
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
    assert calls == []
    assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "main" for node in tree.body)


def test_geo_main_writes_only_to_explicit_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(geo_runner, "build_geometric_pairs", lambda *_a, **_k: [Pair()] * 5)
    monkeypatch.setattr(geo_runner, "rebalance_dates", lambda *_a: [pd.Timestamp("2025-12-15")])
    assert geo_runner.main(["--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "geo5_pairs_daily.csv").exists()
    assert (tmp_path / "geo5_pairs_build.json").exists()
```

- [ ] **Step 2: Run the focused tests and verify signature/import-time failures**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py -q`

Expected: FAIL because `dump` lacks `outdir`, the tail runner still has top-level execution, and the runners lack `--output-dir`.

- [ ] **Step 3: Refactor Gate 0 output injection without changing defaults**

Apply this API consistently:

```python
def dump(name: str, payload: dict, mine=None, official=None, *, outdir: Path = OUTDIR) -> None:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if mine is not None and official is not None:
        pd.concat([mine.rename("mine"), official.rename("official")], axis=1).to_csv(
            outdir / f"{name}_series.csv"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gate", choices=("0r", "0a", "0b", "0rp"))
    parser.add_argument("--output-dir", type=Path, default=OUTDIR)
    args = parser.parse_args(argv)
    return {"0r": run_0r, "0a": run_0a, "0b": run_0b, "0rp": run_preflight500}[
        args.gate
    ](outdir=args.output_dir)
```

Change `run_0r`, `run_0a`, `run_0b`, and `run_preflight500` to accept `outdir=OUTDIR`, pass it to `dump`, and return `0` after writing.

- [ ] **Step 4: Refactor the two builders into import-safe `main(argv=None)` functions**

Both builders must parse `--output-dir`, create it, and write only beneath it:

```python
def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTDIR)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    return run_full_build(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
```

For `tail_pair_runner.py`, create `run_full_build(output_dir: Path) -> int` by moving current lines 28–43 into it and changing both `OUTDIR /` sinks to `output_dir /`. For `geometric_pairs_runner.py`, pass `args.output_dir` into a corresponding `run_full_build`; keep `--smoke`, and leave its two diagnostic dates and `legs_only=True` behavior unchanged. Neither helper may run at import time.

- [ ] **Step 5: Add explicit formal-input and verdict-output arguments**

For `fifth_bucket_formal.py`:

```python
def load_prices_with_tail(tail_csv: Path = TAIL_CSV):
    from signals.common.data_source import load_pg_closes
    closes = load_pg_closes(PAIR_NAMES)
    tail = pd.read_csv(tail_csv, index_col=0, parse_dates=True).dropna()
    nav = (1.0 + tail).cumprod()
    nav.columns = ["尾部成长", "尾部价值"]
    return pd.concat([closes, nav], axis=1), tail.index.min()


class Data:
    def __init__(self, start_override=None, tail_csv: Path = TAIL_CSV):
        prices, tail_start = load_prices_with_tail(tail_csv)
        inc_f = build_factor(prices, PAIR_NAMES)
        cand_f = build_factor(prices, PAIR_NAMES + ["尾部成长", "尾部价值"])
        # Keep the current index, warm-up, position, underlying, and carry statements
        # byte-for-byte from this point onward.


# Add before parse_args in main
ap.add_argument("--tail-csv", type=Path, default=TAIL_CSV)
ap.add_argument("--output-dir", type=Path, default=OUT.parent)
args.output_dir.mkdir(parents=True, exist_ok=True)
d = Data(start_override=args.start, tail_csv=args.tail_csv)
out_path = args.output_dir / "fifth_bucket_verdict.json"
```

For `geometric_5b_formal.py`, use the same pattern with `--geo-csv`, `load_prices(geo_csv)`, `Data(geo_csv)`, and `args.output_dir / "geo5_verdict.json"`.

Add these fields to both verdict JSONs using the already computed objects:

```python
"seed": args.seed,
"position_diff_days": int((d.inc_pos != d.cand_pos).sum()),
"position_diff_ratio": round(float((d.inc_pos != d.cand_pos).mean()), 6),
"metrics_incumbent": inc_m,
"metrics_candidate": cand_m,
```

- [ ] **Step 6: Extend tests to prove import safety and that formal inputs cannot silently use the flat file**

```python
def test_tail_main_writes_only_to_explicit_output_dir(tmp_path, monkeypatch):
    import backtest.tail_pair_runner as tail_runner

    monkeypatch.setattr(tail_runner, "build_tail_pair", lambda *_a, **_k: Pair())
    monkeypatch.setattr(tail_runner, "rebalance_dates",
                        lambda *_a: [pd.Timestamp("2025-12-15")])
    assert tail_runner.main(["--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "tail_pair_daily.csv").exists()
    assert (tmp_path / "tail_pair_build.json").exists()


def test_fifth_main_passes_explicit_tail_path(tmp_path, monkeypatch):
    import backtest.fifth_bucket_formal as formal
    seen = {}

    class FakeData:
        def __init__(self, start_override=None, tail_csv=None):
            seen["tail_csv"] = tail_csv
            raise RuntimeError("stop after path assertion")

    monkeypatch.setattr(formal, "Data", FakeData)
    chosen = tmp_path / "tail.csv"
    with pytest.raises(RuntimeError, match="path assertion"):
        formal.main(["--tail-csv", str(chosen), "--output-dir", str(tmp_path)])
    assert seen["tail_csv"] == chosen


def test_geo_main_passes_explicit_geo_path(tmp_path, monkeypatch):
    import backtest.geometric_5b_formal as formal
    seen = {}

    class FakeData:
        def __init__(self, geo_csv=None):
            seen["geo_csv"] = geo_csv
            raise RuntimeError("stop after path assertion")

    monkeypatch.setattr(formal, "Data", FakeData)
    chosen = tmp_path / "geo.csv"
    with pytest.raises(RuntimeError, match="path assertion"):
        formal.main(["--geo-csv", str(chosen), "--output-dir", str(tmp_path)])
    assert seen["geo_csv"] == chosen
```

- [ ] **Step 7: Run focused and regression tests**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py tests/test_bt_pure_style_builder.py tests/test_bt_selection_permutation.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit CLI isolation**

```bash
git add backtest/gate0_runner.py backtest/tail_pair_runner.py backtest/fifth_bucket_formal.py backtest/geometric_pairs_runner.py backtest/geometric_5b_formal.py tests/test_bt_p0_revalidation.py
git commit -m "feat(backtest): isolate P0 experiment outputs"
```

### Task 3: P0 orchestrator and comparison report

**Files:**
- Create: `backtest/p0_revalidation.py`
- Modify: `tests/test_bt_p0_revalidation.py`

- [ ] **Step 1: Write failing orchestration tests**

```python
from backtest.p0_revalidation import build_commands, compare_verdicts, execute_steps


def test_build_commands_pins_inputs_outputs_and_seed(tmp_path):
    commands = build_commands(tmp_path, python="PY")
    assert commands[0][:4] == ["PY", "-m", "backtest.gate0_runner", "0r"]
    assert "--tail-csv" in commands[2]
    assert str(tmp_path / "outputs" / "tail_pair_daily.csv") in commands[2]
    assert commands[2][-4:] == ["--n-perm", "1000", "--seed", "0"]
    assert "--geo-csv" in commands[4]


def test_execute_steps_stops_after_first_failure(tmp_path):
    called = []

    def fake_runner(command, log_path):
        called.append(command[-1])
        return 9 if len(called) == 2 else 0

    with pytest.raises(RuntimeError, match="step 2"):
        execute_steps([["x", "one"], ["x", "two"], ["x", "three"]], tmp_path, fake_runner)
    assert called == ["one", "two"]


def test_compare_verdicts_reports_maintained_and_flipped():
    same = compare_verdicts({"OVERALL": "STOP", "sharpe_diff": -0.03},
                            {"OVERALL": "STOP", "sharpe_diff": -0.02})
    assert same["disposition"] == "maintained"
    flipped = compare_verdicts({"OVERALL": "STOP"}, {"OVERALL": "GO"})
    assert flipped["disposition"] == "flipped"
```

- [ ] **Step 2: Run tests and verify the orchestrator is missing**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py -q`

Expected: FAIL with `ModuleNotFoundError: backtest.p0_revalidation`.

- [ ] **Step 3: Implement the fixed command graph and fail-closed runner**

```python
# backtest/p0_revalidation.py
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from backtest.run_manifest import create_run_dir, write_manifest


def build_commands(run_dir: Path, python: str = sys.executable) -> list[list[str]]:
    out = run_dir / "outputs"
    return [
        [python, "-m", "backtest.gate0_runner", "0r", "--output-dir", str(out)],
        [python, "-m", "backtest.tail_pair_runner", "--output-dir", str(out)],
        [python, "-m", "backtest.fifth_bucket_formal", "--tail-csv",
         str(out / "tail_pair_daily.csv"), "--output-dir", str(out),
         "--start", "2022-12-12", "--n-perm", "1000", "--seed", "0"],
        [python, "-m", "backtest.geometric_pairs_runner", "--output-dir", str(out)],
        [python, "-m", "backtest.geometric_5b_formal", "--geo-csv",
         str(out / "geo5_pairs_daily.csv"), "--output-dir", str(out),
         "--n-perm", "1000", "--seed", "0"],
    ]


def subprocess_runner(command: list[str], log_path: Path) -> int:
    with log_path.open("wb") as log:
        return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False).returncode


def execute_steps(commands, log_dir: Path, runner=subprocess_runner) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for number, command in enumerate(commands, start=1):
        code = runner(command, log_dir / f"step-{number}.log")
        if code:
            raise RuntimeError(f"step {number} failed with exit code {code}")


def compare_verdicts(old: dict, new: dict) -> dict:
    old_value = old.get("OVERALL", old.get("pass"))
    new_value = new.get("OVERALL", new.get("pass"))
    return {
        "old": old,
        "new": new,
        "disposition": "maintained" if old_value == new_value else "flipped",
    }
```

Complete the module with the following fail-closed control flow; use `backtest.pure_style_builder._conn` only inside `database_cutoffs()` so importing the module never connects to PostgreSQL:

```python
from datetime import datetime
import math
import shutil

from backtest.run_manifest import (
    artifact_record,
    create_run_dir,
    git_state,
    query_table_cutoffs,
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
CUTOFF_CONTRACT = {
    "index_daily": "trade_date",
    "stock_daily_price": "trade_date",
    "stock_indicator": "trade_date",
    "stock_financial": "end_date",
    "index_constituent": "effective_date",
}
LEGACY_VERDICTS = {
    "gate0r": ROOT / "backtest/output/gate0r_result.json",
    "fifth_bucket": ROOT / "backtest/output/fifth_bucket_verdict.json",
    "geometric_5b": ROOT / "backtest/output/geo5_verdict.json",
}
NEW_VERDICTS = {
    "gate0r": "gate0r_result.json",
    "fifth_bucket": "fifth_bucket_verdict.json",
    "geometric_5b": "geo5_verdict.json",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_gate0(path: Path) -> None:
    if load_json(path).get("pass") is not True:
        raise RuntimeError("Gate 0R did not produce machine pass=true")


def validate_verdict(path: Path) -> None:
    payload = load_json(path)
    if payload.get("OVERALL") not in {"STOP", "GO"}:
        raise RuntimeError(f"{path.name}: invalid OVERALL")
    for key in ("sharpe_diff", "p_selected", "sharpe_incumbent_full",
                "sharpe_candidate_full", "position_diff_ratio"):
        value = payload.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError(f"{path.name}: {key} is not finite")


def database_cutoffs() -> dict[str, str]:
    from backtest.pure_style_builder import _conn
    connection = _conn()
    try:
        return query_table_cutoffs(connection, "stock_selector", CUTOFF_CONTRACT)
    finally:
        connection.close()


def run_revalidation(run_root: Path, run_id: str, metadata: dict,
                     runner=subprocess_runner) -> Path:
    run_dir = create_run_dir(run_root, run_id)
    commands = build_commands(run_dir)
    manifest = {**metadata, "experiment": "p0-revalidation", "seed": 0,
                "status": "running", "commands": commands}
    write_manifest(run_dir, manifest)
    print(f"RUN_DIR={run_dir}", flush=True)
    try:
        for name, source in LEGACY_VERDICTS.items():
            shutil.copy2(source, run_dir / "inputs" / f"{name}.json")
        execute_steps(commands, run_dir / "logs", runner)
        validate_gate0(run_dir / "outputs" / NEW_VERDICTS["gate0r"])
        validate_verdict(run_dir / "outputs" / NEW_VERDICTS["fifth_bucket"])
        validate_verdict(run_dir / "outputs" / NEW_VERDICTS["geometric_5b"])
        comparison = {
            name: compare_verdicts(load_json(run_dir / "inputs" / f"{name}.json"),
                                   load_json(run_dir / "outputs" / filename))
            for name, filename in NEW_VERDICTS.items()
        }
        (run_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest["artifacts"] = [
            artifact_record(path, run_dir)
            for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        ]
        manifest["status"] = "complete"
        write_manifest(run_dir, manifest)
        return run_dir
    except BaseException as exc:
        manifest["status"] = ("interrupted"
                              if isinstance(exc, (KeyboardInterrupt, SystemExit))
                              else "failed")
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["artifacts"] = [
            artifact_record(path, run_dir)
            for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        ]
        write_manifest(run_dir, manifest)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path,
                        default=ROOT / "backtest/output/runs")
    args = parser.parse_args(argv)
    state = git_state(ROOT)
    run_id = (datetime.now().strftime("%Y%m%dT%H%M%S")
              + "-p0-revalidation-" + str(state["commit"])[:7])
    run_revalidation(args.run_root, run_id,
                     {"git": state, "database_cutoffs": database_cutoffs()})
    return 0
```

The formal verdict refactors in Task 2 must emit `position_diff_ratio` so this validator cannot accept an older-schema JSON.

- [ ] **Step 4: Add tests for Gate 0 pass validation and failed manifests**

```python
def test_validate_gate0_requires_machine_pass(tmp_path):
    from backtest.p0_revalidation import validate_gate0
    p = tmp_path / "gate0r_result.json"
    p.write_text('{"pass": false}')
    with pytest.raises(RuntimeError, match="Gate 0R"):
        validate_gate0(p)


def test_validate_gate0_accepts_machine_pass(tmp_path):
    from backtest.p0_revalidation import validate_gate0
    p = tmp_path / "gate0r_result.json"
    p.write_text('{"pass": true}')
    validate_gate0(p)


def test_run_revalidation_records_failed_manifest(tmp_path, monkeypatch):
    import backtest.p0_revalidation as p0

    legacy = {}
    for name in p0.NEW_VERDICTS:
        source = tmp_path / f"{name}-old.json"
        source.write_text('{"OVERALL": "STOP"}')
        legacy[name] = source
    monkeypatch.setattr(p0, "LEGACY_VERDICTS", legacy)
    monkeypatch.setattr(p0, "build_commands", lambda *_a, **_k: [["PY", "fail"]])
    with pytest.raises(RuntimeError, match="step 1"):
        p0.run_revalidation(tmp_path / "runs", "fixed-run", {"git": {"commit": "abc"}},
                            runner=lambda *_: 9)
    manifest = json.loads((tmp_path / "runs/fixed-run/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert "step 1 failed" in manifest["error"]


def test_run_revalidation_records_interruption(tmp_path, monkeypatch):
    import backtest.p0_revalidation as p0

    legacy = {}
    for name in p0.NEW_VERDICTS:
        source = tmp_path / f"{name}-old.json"
        source.write_text('{"OVERALL": "STOP"}')
        legacy[name] = source
    monkeypatch.setattr(p0, "LEGACY_VERDICTS", legacy)
    monkeypatch.setattr(p0, "build_commands", lambda *_a, **_k: [["PY", "stop"]])
    with pytest.raises(KeyboardInterrupt):
        p0.run_revalidation(
            tmp_path / "runs", "interrupted-run", {"git": {"commit": "abc"}},
            runner=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    manifest = json.loads(
        (tmp_path / "runs/interrupted-run/manifest.json").read_text()
    )
    assert manifest["status"] == "interrupted"
```

- [ ] **Step 5: Run focused tests**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_run_manifest.py tests/test_bt_p0_revalidation.py -q`

Expected: all tests pass without database access.

- [ ] **Step 6: Commit the orchestrator**

```bash
git add backtest/p0_revalidation.py tests/test_bt_p0_revalidation.py
git commit -m "feat(backtest): orchestrate P0 evidence revalidation"
```

### Task 4: Preserve loose legacy evidence and establish log policy

**Files:**
- Modify: `.gitignore`
- Create: `backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/manifest.json`
- Create: `backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/outputs/*.csv`
- Create: `backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/logs/r3_rerun.log`
- Modify: `data_fixes/2026-08-20-dp-factor-and-leg-lists/README.md`

- [ ] **Step 1: Record the exact pre-copy inventory**

Run:

```bash
git status --short
sha256sum \
  backtest/output/adaptive_bucket_compare_paired.csv \
  backtest/output/adaptive_bucket_compare_report.csv \
  backtest/output/gate0a_result_series.csv \
  backtest/output/gate0b_result_series.csv \
  backtest/output/geo5_pairs_daily.csv \
  backtest/output/mixed_ensemble_probe_paired.csv \
  backtest/output/mixed_ensemble_probe_report.csv \
  backtest/output/mixed_ensemble_probe_yearly.csv \
  backtest/output/rotation_target_probe.csv \
  backtest/output/r3_rerun.log
```

Expected: ten hashes; the nine CSVs and one cited log still exist and remain unmodified.

- [ ] **Step 2: Create the legacy snapshot without moving originals**

Run this exact repository-local snapshot script:

```bash
/home/elfbob/miniconda3/bin/python - <<'PY'
import shutil
from pathlib import Path

from backtest.run_manifest import artifact_record, create_run_dir, write_manifest

root = Path("backtest/output")
run = create_run_dir(root / "runs",
                     "20260821T000000-legacy-pre-p0-2c17b32")
for name in (
    "adaptive_bucket_compare_paired.csv",
    "adaptive_bucket_compare_report.csv",
    "gate0a_result_series.csv",
    "gate0b_result_series.csv",
    "geo5_pairs_daily.csv",
    "mixed_ensemble_probe_paired.csv",
    "mixed_ensemble_probe_report.csv",
    "mixed_ensemble_probe_yearly.csv",
    "rotation_target_probe.csv",
):
    shutil.copy2(root / name, run / "outputs" / name)
shutil.copy2(root / "r3_rerun.log", run / "logs/r3_rerun.log")
payload = {
    "experiment": "legacy-pre-p0-snapshot",
    "observed_at_commit": "2c17b32c4f1d9f156c8e0d26699bc5ee35fc930c",
    "provenance": "untracked flat files observed during the 2026-08-21 audit; generator commit not asserted",
    "status": "complete",
    "immutable": True,
    "reason": "preserve loose evidence before DP-dependent revalidation",
    "source_paths": [f"backtest/output/{name}" for name in (
        "adaptive_bucket_compare_paired.csv",
        "adaptive_bucket_compare_report.csv",
        "gate0a_result_series.csv",
        "gate0b_result_series.csv",
        "geo5_pairs_daily.csv",
        "mixed_ensemble_probe_paired.csv",
        "mixed_ensemble_probe_report.csv",
        "mixed_ensemble_probe_yearly.csv",
        "rotation_target_probe.csv",
        "r3_rerun.log",
    )],
    "artifacts": [artifact_record(path, run) for path in sorted(run.rglob("*"))
                  if path.is_file()],
}
write_manifest(run, payload)
print(run)
PY
```

Do not delete or rewrite the flat originals in this step.

- [ ] **Step 3: Ignore only flat logs and update the cited log reference**

Add this exact rule:

```gitignore
# Flat experiment logs are ephemeral; cited logs live in immutable run directories.
backtest/output/*.log

# Exact pre-P0 flat research copies are preserved in the immutable legacy snapshot.
backtest/output/adaptive_bucket_compare_paired.csv
backtest/output/adaptive_bucket_compare_report.csv
backtest/output/gate0a_result_series.csv
backtest/output/gate0b_result_series.csv
backtest/output/geo5_pairs_daily.csv
backtest/output/mixed_ensemble_probe_paired.csv
backtest/output/mixed_ensemble_probe_report.csv
backtest/output/mixed_ensemble_probe_yearly.csv
backtest/output/rotation_target_probe.csv
```

Change the R3 README reference from the flat log to the committed legacy run log. The nine exact CSV rules suppress only redundant local flat copies after their hashed snapshot is committed; do not ignore `backtest/output/runs/`.

- [ ] **Step 4: Verify snapshot hashes equal source hashes**

Run:

```bash
for name in adaptive_bucket_compare_paired.csv adaptive_bucket_compare_report.csv \
  gate0a_result_series.csv gate0b_result_series.csv geo5_pairs_daily.csv \
  mixed_ensemble_probe_paired.csv mixed_ensemble_probe_report.csv \
  mixed_ensemble_probe_yearly.csv rotation_target_probe.csv; do
  cmp -s "backtest/output/$name" \
    "backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/outputs/$name" || exit 1
done
cmp -s backtest/output/r3_rerun.log \
  backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/logs/r3_rerun.log
/home/elfbob/miniconda3/bin/python -c 'import json; from pathlib import Path; p=Path("backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/manifest.json"); assert len(json.loads(p.read_text())["artifacts"]) == 10'
```

Expected: every `cmp` exits 0 and the manifest contains ten hashed artifacts.

- [ ] **Step 5: Commit the legacy snapshot**

```bash
git add .gitignore backtest/output/runs data_fixes/2026-08-20-dp-factor-and-leg-lists/README.md
git diff --cached --check
git commit -m "data(backtest): preserve pre-revalidation evidence"
```

### Task 5: Run the repaired P0 experiments

**Files:**
- Create: the exact timestamped directory emitted as `RUN_DIR=` by `backtest.p0_revalidation`
- No production file modifications.

- [ ] **Step 1: Capture production-file hashes before the run**

Run:

```bash
sha256sum output/hybrid20/confirmed_signal.csv \
  output/citic40d/citic_style_signal_40d.csv \
  output/equal_weight/equal_weight_signal_20d40z.csv \
  output/recommended/hybrid20_longflat.csv \
  output/recommended/citic40d_longflat.csv \
  output/recommended/equal_weight_longflat.csv > /tmp/style_timing_p0_production_before.sha256
```

Expected: six hashes written; no files changed.

- [ ] **Step 2: Run the immutable orchestrator**

Run:

```bash
set -o pipefail
/home/elfbob/miniconda3/bin/python -m backtest.p0_revalidation \
  --run-root backtest/output/runs | tee /tmp/style_timing_p0_revalidation.log
P0_RUN_DIR=$(sed -n 's/^RUN_DIR=//p' /tmp/style_timing_p0_revalidation.log | tail -1)
test -n "$P0_RUN_DIR" -a -d "$P0_RUN_DIR"
```

Expected sequence: Gate 0R PASS, tail build complete, fifth-bucket verdict written, geometric build complete, geometric verdict written, manifest status `complete`. This is a long read-only database run; report progress between steps rather than restarting it.

- [ ] **Step 3: Validate the new run independently**

Run:

```bash
P0_RUN_DIR=$(sed -n 's/^RUN_DIR=//p' /tmp/style_timing_p0_revalidation.log | tail -1)
test -n "$P0_RUN_DIR" -a -d "$P0_RUN_DIR"
jq -e '.status == "complete"' "$P0_RUN_DIR/manifest.json"
jq -e '.pass == true' "$P0_RUN_DIR/outputs/gate0r_result.json"
jq -e '.OVERALL == "STOP" or .OVERALL == "GO"' "$P0_RUN_DIR/outputs/fifth_bucket_verdict.json"
jq -e '.OVERALL == "STOP" or .OVERALL == "GO"' "$P0_RUN_DIR/outputs/geo5_verdict.json"
```

Expected: the run-directory assertion and all four `jq` commands exit 0; the validated `P0_RUN_DIR` comes only from this invocation's `RUN_DIR=` line.

- [ ] **Step 4: Compare production hashes**

Run: `sha256sum -c /tmp/style_timing_p0_production_before.sha256`\n\nExpected: all six lines report `OK`.

- [ ] **Step 5: Commit the new immutable run**

```bash
P0_RUN_DIR=$(sed -n 's/^RUN_DIR=//p' /tmp/style_timing_p0_revalidation.log | tail -1)
test -n "$P0_RUN_DIR" -a -d "$P0_RUN_DIR"
git add "$P0_RUN_DIR"
git diff --cached --check
git commit -m "measure(backtest): rerun P0 experiments after DP repair"
```

### Task 6: Recover B3 formal-run evidence

**Files:**
- Create: `data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/README.md`
- Create: `data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json`
- Create: `data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core/**`
- Create: `tools/verify_b3_formal_archive.py`
- Create: `tests/test_b3_formal_archive.py`
- External archive: `/home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz`

- [ ] **Step 1: Write the failing inventory-verifier test**

```python
# tests/test_b3_formal_archive.py
import hashlib
import json

from tools.verify_b3_formal_archive import verify_inventory


def test_verify_inventory_detects_match_and_tamper(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    payload = root / "verdicts.csv"
    payload.write_bytes(b"gate,pass\nstability,false\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    inventory = {"core_files": [{"path": "verdicts.csv", "size": payload.stat().st_size,
                             "sha256": digest}]}
    assert verify_inventory(root, inventory) == []
    payload.write_bytes(b"tampered")
    assert verify_inventory(root, inventory) == ["verdicts.csv: sha256 mismatch"]
```

- [ ] **Step 2: Implement the verifier**

```python
# tools/verify_b3_formal_archive.py
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def verify_inventory(root: Path, inventory: dict) -> list[str]:
    errors = []
    for item in inventory["core_files"]:
        path = root / item["path"]
        if not path.is_file():
            errors.append(f"{item['path']}: missing")
            continue
        if path.stat().st_size != item["size"]:
            errors.append(f"{item['path']}: size mismatch")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            errors.append(f"{item['path']}: sha256 mismatch")
    return errors
```

Complete the same file with this CLI:

```python
def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    errors = verify_inventory(args.root, inventory)
    if errors:
        print("\n".join(errors))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Copy the core evidence into the main repository**

Source root:

```text
/home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/
data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal/
```

Copy these exact files under `core/`, preserving relative paths:

```text
RETRIEVAL.md
b3_execution_receipt.json
backtest/run_manifest.json
backtest/verdicts.csv
backtest/production_metrics.csv
backtest/model_comparison.csv
backtest/bootstrap.csv
backtest/structure_manifest.json
research/manifests/preflight.json
research/coverage_audit.csv
```

Generate `inventory.json` with three explicit sections: `source` records branch `fix/b3-wind-share-capital-tail`, commit `41ed581c649712c90463c587265cd1a47e177c44`, and tag `archive/b3-wind-share-capital-tail-20260814`; `core_files` records the ten copied files' relative paths, sizes, and SHA-256 values; `formal_run_files` records every source formal-run file's relative path, size, and SHA-256. Step 4 adds a separate `external_archive` object with path, size, SHA-256, and member-root `run-windows-formal/`.

- [ ] **Step 4: Create the external complete archive**

After escalation approval, create the exact directory and tarball:

```bash
mkdir -p /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence
tar -C /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital \
  -czf /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz \
  run-windows-formal
sha256sum /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
```

Record the tarball size and hash in `inventory.json` and the restore command in `README.md`.

- [ ] **Step 5: Verify core and external inventories**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_formal_archive.py -q
/home/elfbob/miniconda3/bin/python tools/verify_b3_formal_archive.py \
  --inventory data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json \
  --root data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core
B3_TAR=/home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
B3_TAR_HASH=$(jq -r '.external_archive.sha256' data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json)
echo "$B3_TAR_HASH  $B3_TAR" | sha256sum -c -
tar -tzf "$B3_TAR" >/dev/null
```

Expected: the focused test passes, the core verifier prints `OK`, `sha256sum -c` reports `OK`, and the tar listing exits 0.

- [ ] **Step 6: Commit B3 evidence recovery**

```bash
git add data_fixes/2026-08-01-b3-wind-share-capital-formal-archive tools/verify_b3_formal_archive.py tests/test_b3_formal_archive.py
git diff --cached --check
git commit -m "data(b3): archive formal-run evidence"
```

### Task 7: Update authoritative verdicts and indexes

**Files:**
- Modify: `docs/plans/2026-08-19-gate0-execution-record.md`
- Modify: `docs/plans/2026-08-19-geometric-5buckets-verdict.md`
- Modify: `docs/plans/2026-08-20-data-foundation-repair.md`
- Modify: `backtest/README.md`
- Create: `docs/plans/README.md`

- [ ] **Step 1: Extract machine values from the new run**

Run:

```bash
P0_RUN_DIR=$(sed -n 's/^RUN_DIR=//p' /tmp/style_timing_p0_revalidation.log | tail -1)
test -n "$P0_RUN_DIR" -a -d "$P0_RUN_DIR"
jq '{pass, anchors, thresholds, elapsed_s}' "$P0_RUN_DIR/outputs/gate0r_result.json"
jq '{OVERALL, verdict_case, common_window, sharpe_incumbent_full, sharpe_candidate_full, sharpe_diff, p_naive, p_selected, worst_tv_incumbent, worst_tv_candidate, position_diff_days, position_diff_ratio, metrics_incumbent, metrics_candidate}' "$P0_RUN_DIR/outputs/fifth_bucket_verdict.json"
jq '{OVERALL, verdict_case, common_window, sharpe_incumbent_full, sharpe_candidate_full, sharpe_diff, p_naive, p_selected, worst_tv_incumbent, worst_tv_candidate, position_diff_days, position_diff_ratio, metrics_incumbent, metrics_candidate}' "$P0_RUN_DIR/outputs/geo5_verdict.json"
jq '.' "$P0_RUN_DIR/comparison.json"
```

Expected: all queries exit 0 and print only machine-derived values. Save this console output in the work log; do not transcribe from memory.

- [ ] **Step 2: Append supersession blocks without rewriting history**

Each historical document receives a dated block with this structure and actual machine values substituted:

```markdown
## DP 修复后重验（2026-08-21）

- 权威 run：复制本次 `manifest.json` 中的仓库相对路径。
- 代码 commit：复制 `manifest.git.commit`；seed：`0`；输入/输出哈希见 manifest。
- 旧规格结论与修复后结论：逐字复制 `comparison.json` 对应字段，并按 `disposition` 写明维持或翻转。
- 本节取代旧文档作为当前规格裁决；旧段保留作历史记录。
```

Gate 0 文档必须引用机器 `pass=true` 的新 JSON。第五桶与等比五桶必须逐项列出 Sharpe 差、p、分窗、仓位分歧、MaxDD 和换手。

- [ ] **Step 3: Create the experiment authority index**

`docs/plans/README.md` must contain one row per active/recent formal experiment with columns:

```markdown
| Experiment | Status | Authoritative spec | Authoritative verdict/run | Superseded files | Reopen condition |
```

Create exact rows for Gate 0R, tail fifth-bucket, geometric five-bucket, adaptive-bucket probe, mixed-ensemble probe, rotation-target probe, and B3 formal evaluation. Mark the old Gate 0/fifth-bucket and geometric verdict sections as superseded by the new immutable run; mark `2026-08-18-fifth-bucket-preregistration.md` and `2026-08-18-fifth-bucket-preregistration-r2-DRAFT.md` as superseded by `2026-08-19-fifth-bucket-preregistration-r3.md`. For each probe without a formal verdict, set status `research-only` and point its evidence column at the committed legacy snapshot rather than inventing a decision.

- [ ] **Step 4: Refresh the backtest entry page**

Replace the stale “next step = Phase 2/3” section with:

- current production baseline status;
- immutable-run convention;
- link to `docs/plans/README.md`;
- explicit statement that engine-timing/NaN/carry fixes are outside this P0 batch.

- [ ] **Step 5: Verify every documentation path and run ID**

Run:

```bash
if rg -n 'TBD|TODO' docs/plans backtest/README.md; then exit 1; fi
rg -n 'backtest/output/runs/' docs/plans backtest/README.md
git diff --check
```

Expected: the first command finds no newly introduced markers; the second prints the exact committed run paths cited by the updated documents.

- [ ] **Step 6: Commit documentation closure**

```bash
git add docs/plans/2026-08-19-gate0-execution-record.md \
  docs/plans/2026-08-19-geometric-5buckets-verdict.md \
  docs/plans/2026-08-20-data-foundation-repair.md \
  docs/plans/README.md backtest/README.md
git diff --cached --check
git commit -m "docs(backtest): close P0 evidence revalidation"
```

### Task 8: Final verification and handoff

**Files:**
- No new files unless verification exposes a defect.

- [ ] **Step 1: Run focused tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_bt_run_manifest.py \
  tests/test_bt_p0_revalidation.py \
  tests/test_b3_formal_archive.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the full suite**

Run: `/home/elfbob/miniconda3/bin/python -m pytest -q`

Expected: at least the previous baseline of `1563 passed`, plus the new tests, with zero failures.

- [ ] **Step 3: Run static and repository checks**

```bash
ruff check backtest/run_manifest.py backtest/p0_revalidation.py \
  backtest/gate0_runner.py backtest/tail_pair_runner.py \
  backtest/fifth_bucket_formal.py backtest/geometric_pairs_runner.py \
  backtest/geometric_5b_formal.py tools/verify_b3_formal_archive.py \
  tests/test_bt_run_manifest.py tests/test_bt_p0_revalidation.py tests/test_b3_formal_archive.py
git diff --check
git status --short
```

Expected: new/modified Python files have zero Ruff errors; diff check is clean; only the deliberately ignored flat logs and nine exact legacy CSV copies may remain outside Git.

- [ ] **Step 4: Verify evidence from a clean Git view**

```bash
git ls-files backtest/output/runs data_fixes/2026-08-01-b3-wind-share-capital-formal-archive
git log --oneline 19e98f3..HEAD
```

Expected: both immutable P0 runs and B3 core evidence are tracked; commits are ordered as manifest infrastructure, CLI isolation, orchestrator, legacy snapshot, repaired run, B3 archive, documentation closure.

- [ ] **Step 5: Request code review before declaring completion**

Use `superpowers:requesting-code-review` with base `19e98f3` and current HEAD. Resolve all Critical and Important findings, rerun the affected focused tests, then rerun the full suite.

- [ ] **Step 6: Report results without overclaiming**

Report:

- Gate 0R machine verdict;
- old/new fifth-bucket and geometric-bucket values and whether each conclusion changed;
- immutable run IDs and manifest paths;
- B3 core/external archive hashes;
- full pytest and Ruff results;
- confirmation that six production files retained their pre-run hashes;
- any follow-up intentionally deferred to the separate回测口径 batch.
