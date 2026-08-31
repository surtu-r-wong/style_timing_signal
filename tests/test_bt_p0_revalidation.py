"""Regression coverage for P0 experiment-run isolation."""
from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest import p0_revalidation
from backtest.pit_metadata import current_pit_metadata, sha256_file
from backtest.pure_style_builder import FD_STATEMENTS


ROOT = Path(__file__).resolve().parents[1]
TAIL_RUNNER = ROOT / "backtest" / "tail_pair_runner.py"


def _pair() -> SimpleNamespace:
    index = pd.date_range("2024-01-02", periods=2, freq="B")
    return SimpleNamespace(
        growth=pd.Series([0.01, -0.02], index=index),
        value=pd.Series([0.0, 0.01], index=index),
        n_growth={"2024-01-02": 10},
        n_value={"2024-01-02": 10},
        skipped=[],
    )


def _is_root_path_injection(node: ast.Expr) -> bool:
    return ast.unparse(node.value) == "sys.path.insert(0, str(ROOT))"


def _tail_runner_is_import_safe() -> bool:
    tree = ast.parse(TAIL_RUNNER.read_text(encoding="utf-8"))
    return not any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and not _is_root_path_injection(node) for node in tree.body)


def _verdict_data(window_names: tuple[str, ...], start_name: str) -> type:
    class FakeData:
        def __init__(self, *args, **kwargs):
            self.idx = pd.date_range("2024-01-02", periods=130, freq="B")
            setattr(self, start_name, self.idx[0])
            self.inc_pos = np.zeros(len(self.idx))
            self.cand_pos = np.ones(len(self.idx))

        def metrics(self, _pos):
            return {name: {"sharpe": np.float64(0.2), "ann": np.float64(0.1),
                           "maxdd": np.float64(-0.1), "turnover": np.float64(1.0)}
                    for name in window_names}

        def sharpe_full(self, pos, carry=True):
            return float(np.asarray(pos).mean())

    return FakeData


def test_gate0_dump_writes_json_and_series_only_to_explicit_directory(tmp_path):
    gate0 = importlib.import_module("backtest.gate0_runner")
    mine = pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-02"]))
    official = pd.Series([0.02], index=mine.index)

    gate0.dump("isolated", {"pass": True}, mine, official, outdir=tmp_path)

    assert json.loads((tmp_path / "isolated.json").read_text()) == {"pass": True}
    assert (tmp_path / "isolated_series.csv").is_file()
    assert not (gate0.OUTDIR / "isolated.json").exists()


def test_tail_runner_has_no_top_level_execution_and_exposes_main():
    tree = ast.parse(TAIL_RUNNER.read_text(encoding="utf-8"))

    assert not any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and not _is_root_path_injection(node) for node in tree.body)
    assert any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)


def test_tail_runner_direct_script_help_is_import_safe():
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "backtest/tail_pair_runner.py", "--help"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=10, check=False)

    assert result.returncode == 0, result.stderr
    assert "--output-dir" in result.stdout


def test_tail_runner_main_writes_only_requested_output_directory(monkeypatch, tmp_path):
    if not _tail_runner_is_import_safe():
        pytest.skip("runner is not yet import-safe; AST test above is the red-light assertion")
    runner = importlib.import_module("backtest.tail_pair_runner")
    monkeypatch.setattr(runner, "OUTDIR", tmp_path / "flat-output")
    monkeypatch.setattr(runner, "rebalance_dates", lambda *_: [pd.Timestamp("2024-01-02")])
    monkeypatch.setattr(runner, "build_tail_pair", lambda *_, **__: _pair())

    assert runner.main(["--output-dir", str(tmp_path)]) == 0

    assert (tmp_path / "tail_pair_daily.csv").is_file()
    meta = json.loads((tmp_path / "tail_pair_build.json").read_text())
    assert meta["skipped"] == []
    assert meta["schema_version"] == 2
    assert meta["artifact_type"] == "tail_pair_build"
    assert meta["pit"]["periodic_statement_policy"] == (
        "first_disclosure_else_statutory_deadline"
    )
    assert meta["pit"]["first_disclosure_coverage"] == "partial"
    assert meta["data_artifact"]["path"] == "tail_pair_daily.csv"
    assert meta["data_artifact"]["sha256"] == sha256_file(
        tmp_path / "tail_pair_daily.csv"
    )
    assert not (runner.OUTDIR / "tail_pair_daily.csv").exists()


def test_geometric_runner_writes_only_requested_output_directory(monkeypatch, tmp_path):
    runner = importlib.import_module("backtest.geometric_pairs_runner")
    explicit_output = tmp_path / "explicit-output"
    monkeypatch.setattr(runner, "OUTDIR", tmp_path / "flat-output")
    monkeypatch.setattr(runner, "rebalance_dates", lambda *_: [pd.Timestamp("2024-01-02")])
    monkeypatch.setattr(runner, "build_geometric_pairs", lambda *_, **__: [_pair()] * 5)

    assert runner.main(["--output-dir", str(explicit_output)]) == 0

    assert (explicit_output / "geo5_pairs_daily.csv").is_file()
    meta = json.loads((explicit_output / "geo5_pairs_build.json").read_text())
    assert meta["skipped"] == []
    assert meta["schema_version"] == 2
    assert meta["artifact_type"] == "geometric_pairs_build"
    assert meta["pit"]["periodic_statement_types"] == sorted(FD_STATEMENTS)
    assert meta["data_artifact"]["path"] == "geo5_pairs_daily.csv"
    assert meta["data_artifact"]["sha256"] == sha256_file(
        explicit_output / "geo5_pairs_daily.csv"
    )
    assert not (runner.OUTDIR / "geo5_pairs_daily.csv").exists()


def test_geometric_smoke_keeps_diagnostic_dates_and_writes_no_artifacts(monkeypatch, tmp_path):
    runner = importlib.import_module("backtest.geometric_pairs_runner")
    calls = []
    monkeypatch.setattr(runner, "build_geometric_pairs",
                        lambda dates, **kwargs: calls.append((dates, kwargs)))

    assert runner.main(["--smoke", "--output-dir", str(tmp_path)]) == 0

    assert [call[0][0] for call in calls] == [pd.Timestamp("2015-06-15"),
                                               pd.Timestamp("2026-06-15")]
    assert all(call[1]["legs_only"] is True for call in calls)
    assert not list(tmp_path.iterdir())


def test_current_pit_metadata_describes_partial_first_disclosure_contract():
    pit = current_pit_metadata(FD_STATEMENTS)
    joined = json.dumps(pit, ensure_ascii=False)
    assert pit["periodic_statement_types"] == sorted(FD_STATEMENTS)
    assert pit["first_disclosure_source"] == (
        "stock_first_disclosure.first_disclosure_date"
    )
    assert pit["fallback_policy"] == "statutory_deadline"
    assert pit["limitations"][0]["code"] == "late_filer_fallback"
    assert "approximate-PIT" not in joined
    assert "2025-03-31" not in joined


@pytest.mark.parametrize(
    ("module_name", "flag", "keyword"),
    [("backtest.fifth_bucket_formal", "--tail-csv", "tail_csv"),
     ("backtest.geometric_5b_formal", "--geo-csv", "geo_csv")],
)
def test_verdict_cli_passes_explicit_input_path_to_data(monkeypatch, tmp_path,
                                                        module_name, flag, keyword):
    module = importlib.import_module(module_name)
    requested = tmp_path / "input.csv"
    seen = {}

    class SentinelData:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("sentinel input path captured")

    monkeypatch.setattr(module, "Data", SentinelData)

    with pytest.raises(RuntimeError, match="sentinel input path captured"):
        module.main([flag, str(requested), "--output-dir", str(tmp_path)])
    assert seen[keyword] == requested


@pytest.mark.parametrize(
    ("module_name", "input_flag", "start_name", "window_names", "output_name"),
    [("backtest.fifth_bucket_formal", "--tail-csv", "tail_start", ("full", "H1", "H2"),
      "fifth_bucket_verdict.json"),
     ("backtest.geometric_5b_formal", "--geo-csv", "geo_start",
      ("full", "2015-2020", "2021-2023", "2024-2026"), "geo5_verdict.json")],
)
def test_verdicts_include_revalidation_evidence(monkeypatch, tmp_path, module_name,
                                                 input_flag, start_name, window_names,
    output_name):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "OUT", tmp_path / "flat-output" / output_name)
    monkeypatch.setattr(module, "Data", _verdict_data(window_names, start_name))
    monkeypatch.setattr(module, "selection_permutation_test",
                        lambda *_args, **_kwargs: SimpleNamespace(
                            observed_best=0.1, p_selected=0.01, p_naive=0.01))

    assert module.main([input_flag, str(tmp_path / "input.csv"), "--output-dir", str(tmp_path),
                        "--n-perm", "1", "--seed", "19"]) == 0

    payload = json.loads((tmp_path / output_name).read_text())
    assert payload["seed"] == 19
    assert payload["position_diff_days"] == 130
    assert payload["position_diff_ratio"] == 1.0
    assert payload["metrics_incumbent"]["full"]["sharpe"] == 0.2
    assert payload["metrics_candidate"]["full"]["sharpe"] == 0.2
    assert not (module.OUT.parent / output_name).exists()


def _valid_verdict(overall="STOP"):
    return {
        "OVERALL": overall,
        "sharpe_diff": 0.1,
        "p_selected": 0.02,
        "sharpe_incumbent_full": 0.3,
        "sharpe_candidate_full": 0.4,
        "position_diff_ratio": 0.25,
    }


def _legacy_verdict(overall):
    payload = _valid_verdict(overall)
    payload.pop("position_diff_ratio")
    return payload


def test_p0_revalidation_reports_parseable_run_dir(tmp_path, capsys):
    _write_legacy_outputs(tmp_path)

    run_dir = p0_revalidation.run_revalidation(
        tmp_path / "runs", "run-dir", root=tmp_path, metadata={}, runner=_complete_runner
    )

    assert capsys.readouterr().out == f"RUN_DIR={run_dir}\n"
def _write_legacy_outputs(root):
    legacy = root / "backtest" / "output"
    legacy.mkdir(parents=True)
    payloads = {
        "gate0r_result.json": {"pass": False},
        "fifth_bucket_verdict.json": _legacy_verdict("STOP"),
        "geo5_verdict.json": _legacy_verdict("GO"),
    }
    for name, payload in payloads.items():
        (legacy / name).write_text(json.dumps(payload), encoding="utf-8")
    return legacy


def test_p0_revalidation_import_is_db_safe():
    assert p0_revalidation.ROOT == ROOT
    assert callable(p0_revalidation.database_cutoffs)


def test_p0_revalidation_builds_the_five_frozen_commands(tmp_path):
    run_dir = tmp_path / "run"

    assert p0_revalidation.build_commands(run_dir, python="python-bin") == [
        ("gate0r", ["python-bin", "-m", "backtest.gate0_runner", "0r",
                    "--output-dir", str(run_dir / "outputs")]),
        ("tail", ["python-bin", "-m", "backtest.tail_pair_runner",
                  "--output-dir", str(run_dir / "outputs")]),
        ("fifth", ["python-bin", "-m", "backtest.fifth_bucket_formal",
                   "--tail-csv", str(run_dir / "outputs" / "tail_pair_daily.csv"),
                   "--output-dir", str(run_dir / "outputs"), "--start", "2022-12-12",
                   "--n-perm", "1000", "--seed", "0"]),
        ("geo_pairs", ["python-bin", "-m", "backtest.geometric_pairs_runner",
                       "--output-dir", str(run_dir / "outputs")]),
        ("geo_formal", ["python-bin", "-m", "backtest.geometric_5b_formal",
                        "--geo-csv", str(run_dir / "outputs" / "geo5_pairs_daily.csv"),
                        "--output-dir", str(run_dir / "outputs"), "--n-perm", "1000",
                        "--seed", "0"]),
    ]


def test_p0_revalidation_stops_after_second_step_failure(tmp_path):
    commands = [("one", ["one"]), ("two", ["two"]), ("three", ["three"])]
    calls = []

    def runner(command, log_path):
        calls.append(command)
        log_path.write_bytes(b"runner output")
        return 7 if command == ["two"] else 0

    with pytest.raises(RuntimeError, match="two.*7"):
        p0_revalidation.execute_steps(tmp_path, commands, runner=runner)

    assert calls == [["one"], ["two"]]
    assert (tmp_path / "logs" / "step-1-one.log").read_bytes() == b"runner output"
    assert (tmp_path / "logs" / "step-2-two.log").is_file()
    assert not (tmp_path / "logs" / "step-3-three.log").exists()


def test_p0_revalidation_compares_overall_and_pass_fallbacks():
    comparison = p0_revalidation.compare_verdicts(
        {"gate": {"pass": True}, "fifth": _valid_verdict("STOP")},
        {"gate": {"OVERALL": "GO"}, "fifth": _valid_verdict("GO")},
    )

    assert comparison["gate"]["old"] == {"pass": True}
    assert comparison["gate"]["new"] == {"OVERALL": "GO"}
    assert comparison["gate"]["maintained"] is True
    assert comparison["gate"]["flipped"] is False
    assert comparison["fifth"]["maintained"] is False
    assert comparison["fifth"]["flipped"] is True


@pytest.mark.parametrize("rendered", [
    '{"nested": [NaN]}',
    '{"nested": {"value": Infinity}}',
    '{"nested": {"value": -Infinity}}',
])
def test_p0_revalidation_rejects_nonfinite_json_at_any_depth(tmp_path, rendered):
    path = tmp_path / "bad.json"
    path.write_text(rendered, encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite"):
        p0_revalidation.load_json(path)


def test_p0_revalidation_validates_gate0_true_only():
    assert p0_revalidation.validate_gate0({"pass": True}) == {"pass": True}

    with pytest.raises(ValueError, match="pass"):
        p0_revalidation.validate_gate0({"pass": False})


def test_p0_revalidation_validates_complete_finite_verdicts():
    assert p0_revalidation.validate_verdict(_valid_verdict()) == _valid_verdict()

    invalid = _valid_verdict()
    invalid.pop("position_diff_ratio")
    with pytest.raises(ValueError, match="position_diff_ratio"):
        p0_revalidation.validate_verdict(invalid)

    invalid = _valid_verdict()
    invalid["position_diff_ratio"] = True
    with pytest.raises(ValueError, match="position_diff_ratio"):
        p0_revalidation.validate_verdict(invalid)

    invalid = _valid_verdict()
    invalid["sharpe_diff"] = float("nan")
    with pytest.raises(ValueError, match="sharpe_diff"):
        p0_revalidation.validate_verdict(invalid)

    invalid = _valid_verdict()
    invalid["p_selected"] = float("inf")
    with pytest.raises(ValueError, match="p_selected"):
        p0_revalidation.validate_verdict(invalid)

    invalid = _valid_verdict("MAYBE")
    with pytest.raises(ValueError, match="OVERALL"):
        p0_revalidation.validate_verdict(invalid)


@pytest.mark.parametrize(
    ("raised", "expected_status"),
    [(RuntimeError("runner failed"), "failed"), (KeyboardInterrupt(), "interrupted")],
)
def test_p0_revalidation_records_failed_and_interrupted_manifests(
    tmp_path, raised, expected_status
):
    _write_legacy_outputs(tmp_path)

    def runner(_command, log_path):
        log_path.write_bytes(b"failed")
        raise raised

    with pytest.raises(type(raised)):
        p0_revalidation.run_revalidation(
            tmp_path / "runs", "failure", root=tmp_path, metadata={"test": True}, runner=runner
        )

    manifest = json.loads((tmp_path / "runs" / "failure" / "manifest.json").read_text())
    assert manifest["status"] == expected_status
    assert manifest["error"]["type"] == type(raised).__name__
    assert manifest["artifacts"]


def test_p0_revalidation_writes_complete_evidence_run(tmp_path):
    _write_legacy_outputs(tmp_path)
    calls = []

    def runner(command, log_path):
        calls.append(command)
        return _complete_runner(command, log_path)

    metadata = {"git": {"commit": "abcdef0", "dirty": False},
                "database_cutoffs": {"index_daily": "2026-08-20"}}
    run_dir = p0_revalidation.run_revalidation(
        tmp_path / "runs", "success", root=tmp_path, metadata=metadata, runner=runner
    )

    manifest = json.loads((run_dir / "manifest.json").read_text())
    comparison = json.loads((run_dir / "comparison.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["metadata"] == metadata
    assert manifest["experiment"] == "p0-revalidation"
    assert manifest["seed"] == 0
    assert len(manifest["commands"]) == 5
    assert set(manifest["inputs"]) == {"gate0r", "fifth", "geo"}
    assert comparison["gate0r"]["flipped"] is True
    assert comparison["fifth"]["maintained"] is True
    assert comparison["geo"]["flipped"] is True
    assert (run_dir / "outputs" / "fifth_bucket_verdict.json").is_file()
    assert (run_dir / "comparison.json").read_bytes().endswith(b"\n")
    assert all("manifest" not in item["path"] and ".tmp" not in item["path"]
               for item in manifest["artifacts"])
    assert len(calls) == 5
    assert {record["path"] for record in manifest["artifacts"]} >= {
        "inputs/gate0r_result.json", "outputs/geo5_verdict.json", "comparison.json"}
    assert all(len(record["sha256"]) == 64 for record in manifest["artifacts"])

    expected_paths = {
        "comparison.json",
        "inputs/gate0r_result.json",
        "inputs/fifth_bucket_verdict.json",
        "inputs/geo5_verdict.json",
        "outputs/gate0r_result.json",
        "outputs/tail_pair_daily.csv",
        "outputs/tail_pair_build.json",
        "outputs/fifth_bucket_verdict.json",
        "outputs/geo5_pairs_daily.csv",
        "outputs/geo5_pairs_build.json",
        "outputs/geo5_verdict.json",
        "logs/step-1-gate0r.log",
        "logs/step-2-tail.log",
        "logs/step-3-fifth.log",
        "logs/step-4-geo_pairs.log",
        "logs/step-5-geo_formal.log",
    }
    assert expected_paths <= {record["path"] for record in manifest["artifacts"]}
def test_p0_revalidation_import_does_not_call_poisoned_connection(monkeypatch):
    builder = importlib.import_module("backtest.pure_style_builder")
    calls = []

    def forbidden_connection():
        calls.append(True)
        raise AssertionError("import must not connect to the database")

    monkeypatch.setattr(builder, "_conn", forbidden_connection)
    monkeypatch.delitem(sys.modules, "backtest.p0_revalidation", raising=False)

    reimported = importlib.import_module("backtest.p0_revalidation")

    assert reimported.ROOT == ROOT
    assert calls == []


_VERDICT_NUMERIC_FIELDS = (
    "sharpe_diff",
    "p_selected",
    "sharpe_incumbent_full",
    "sharpe_candidate_full",
    "position_diff_ratio",
)


@pytest.mark.parametrize(
    ("field", "mode"),
    [(field, mode) for field in _VERDICT_NUMERIC_FIELDS
     for mode in ("missing", "bool", "string", "nan", "infinity")],
)
def test_p0_revalidation_rejects_every_invalid_required_numeric_field(field, mode):
    payload = _valid_verdict()
    if mode == "missing":
        payload.pop(field)
    elif mode == "bool":
        payload[field] = True
    elif mode == "string":
        payload[field] = "not-a-number"
    elif mode == "nan":
        payload[field] = float("nan")
    else:
        payload[field] = float("inf")

    with pytest.raises(ValueError, match=field):
        p0_revalidation.validate_verdict(payload)

def test_p0_main_refuses_tracked_changes_before_database_or_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        p0_revalidation,
        "git_state",
        lambda _root: {
            "commit": "abcdef0",
            "dirty": True,
            "tracked_dirty": True,
            "porcelain": [" M backtest/p0_revalidation.py"],
        },
    )
    monkeypatch.setattr(
        p0_revalidation, "database_input_state",
        lambda: calls.append("database")
    )
    monkeypatch.setattr(
        p0_revalidation, "run_revalidation",
        lambda *_args, **_kwargs: calls.append("run")
    )

    with pytest.raises(RuntimeError, match="tracked changes"):
        p0_revalidation.main(["--run-root", str(tmp_path / "runs")])

    assert calls == []


def test_p0_revalidation_fails_when_whole_chain_inputs_move_in_window(tmp_path):
    _write_legacy_outputs(tmp_path)
    drift = {
        "inputs_moved_in_window": True,
        "rows_touched_in_window": {"stock_indicator": 3},
        "registrable_as_first_run": False,
    }

    with pytest.raises(RuntimeError, match="input drift"):
        p0_revalidation.run_revalidation(
            tmp_path / "runs", "drifted", root=tmp_path, metadata={},
            runner=_complete_runner, drift_check=lambda: drift,
        )

    manifest = json.loads(
        (tmp_path / "runs" / "drifted" / "manifest.json").read_text()
    )
    assert manifest["status"] == "failed"
    assert manifest["metadata"]["input_drift"] == drift


def _complete_runner(command, log_path, *, include_build=True):
    log_path.write_bytes(b"step log")
    output = Path(command[command.index("--output-dir") + 1])
    if "backtest.gate0_runner" in command:
        artifacts = [("gate0r_result.json", {"pass": True})]
    elif "backtest.tail_pair_runner" in command:
        artifacts = [("tail_pair_daily.csv", "growth,value\n0.0,0.0\n")]
        if include_build:
            artifacts.append(("tail_pair_build.json", {}))
    elif "backtest.fifth_bucket_formal" in command:
        artifacts = [("fifth_bucket_verdict.json", _valid_verdict("STOP"))]
    elif "backtest.geometric_pairs_runner" in command:
        artifacts = [("geo5_pairs_daily.csv", "g1_growth,g1_value\n0.0,0.0\n")]
        if include_build:
            artifacts.append(("geo5_pairs_build.json", {}))
    else:
        artifacts = [("geo5_verdict.json", _valid_verdict("STOP"))]
    for name, payload in artifacts:
        target = output / name
        if isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
        else:
            target.write_text(json.dumps(payload), encoding="utf-8")
    return 0


def test_p0_revalidation_records_started_and_finished_timestamps(monkeypatch, tmp_path):
    _write_legacy_outputs(tmp_path)
    times = iter([
        "2026-08-21T09:00:00+08:00",
        "2026-08-21T09:10:00+08:00",
    ])
    monkeypatch.setattr(p0_revalidation, "_now_iso", lambda: next(times))

    run_dir = p0_revalidation.run_revalidation(
        tmp_path / "runs", "timestamps", root=tmp_path, metadata={},
        runner=_complete_runner,
    )

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["started_at"] == "2026-08-21T09:00:00+08:00"
    assert manifest["finished_at"] == "2026-08-21T09:10:00+08:00"


@pytest.mark.parametrize(
    ("raised", "status"),
    [(RuntimeError("boom"), "failed"), (KeyboardInterrupt(), "interrupted")],
)
def test_p0_revalidation_records_finished_time_for_terminal_failures(
    monkeypatch, tmp_path, raised, status
):
    _write_legacy_outputs(tmp_path)
    times = iter([
        "2026-08-21T09:00:00+08:00",
        "2026-08-21T09:10:00+08:00",
    ])
    monkeypatch.setattr(p0_revalidation, "_now_iso", lambda: next(times))

    def runner(_command, log_path):
        log_path.write_bytes(b"failed")
        raise raised

    with pytest.raises(type(raised)):
        p0_revalidation.run_revalidation(
            tmp_path / "runs", status, root=tmp_path, metadata={}, runner=runner
        )

    manifest = json.loads((tmp_path / "runs" / status / "manifest.json").read_text())
    assert manifest["status"] == status
    assert manifest["started_at"] == "2026-08-21T09:00:00+08:00"
    assert manifest["finished_at"] == "2026-08-21T09:10:00+08:00"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"OVERALL": "MAYBE"},
        {"pass": 1},
        {"pass": "true"},
        {"OVERALL": "GO", "pass": False},
    ],
)
def test_p0_revalidation_compare_rejects_invalid_or_conflicting_decisions(payload):
    with pytest.raises(RuntimeError, match="decision"):
        p0_revalidation.compare_verdicts({"verdict": payload}, {"verdict": {"OVERALL": "STOP"}})


def test_p0_revalidation_compare_accepts_consistent_decisions():
    comparison = p0_revalidation.compare_verdicts(
        {"gate": {"OVERALL": "STOP", "pass": False}},
        {"gate": {"pass": True}},
    )

    assert comparison["gate"]["flipped"] is True
    assert comparison["gate"]["maintained"] is False


def test_p0_revalidation_missing_build_evidence_fails_run(tmp_path):
    _write_legacy_outputs(tmp_path)

    def incomplete_runner(command, log_path):
        return _complete_runner(command, log_path, include_build=False)

    with pytest.raises(RuntimeError, match="required evidence"):
        p0_revalidation.run_revalidation(
            tmp_path / "runs", "incomplete", root=tmp_path, metadata={},
            runner=incomplete_runner,
        )

    manifest = json.loads((tmp_path / "runs" / "incomplete" / "manifest.json").read_text())
    assert manifest["status"] == "failed"
