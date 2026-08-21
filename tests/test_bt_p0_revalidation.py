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
    assert json.loads((tmp_path / "tail_pair_build.json").read_text())["skipped"] == []
    assert not (runner.OUTDIR / "tail_pair_daily.csv").exists()


def test_geometric_runner_writes_only_requested_output_directory(monkeypatch, tmp_path):
    runner = importlib.import_module("backtest.geometric_pairs_runner")
    explicit_output = tmp_path / "explicit-output"
    monkeypatch.setattr(runner, "OUTDIR", tmp_path / "flat-output")
    monkeypatch.setattr(runner, "rebalance_dates", lambda *_: [pd.Timestamp("2024-01-02")])
    monkeypatch.setattr(runner, "build_geometric_pairs", lambda *_, **__: [_pair()] * 5)

    assert runner.main(["--output-dir", str(explicit_output)]) == 0

    assert (explicit_output / "geo5_pairs_daily.csv").is_file()
    assert json.loads((explicit_output / "geo5_pairs_build.json").read_text())["skipped"] == []
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
