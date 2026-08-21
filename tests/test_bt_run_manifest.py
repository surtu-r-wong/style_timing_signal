import hashlib
import json
from pathlib import Path

import pytest

from backtest import run_manifest


def test_create_run_dir_refuses_overwrite(tmp_path):
    run = run_manifest.create_run_dir(tmp_path, "20260821T120000-gate0r-abcdef0")
    assert run == tmp_path / "20260821T120000-gate0r-abcdef0"
    assert run.is_dir()
    assert {path.name for path in run.iterdir()} == {"inputs", "outputs", "logs"}
    assert all((run / name).is_dir() for name in ("inputs", "outputs", "logs"))

    with pytest.raises(FileExistsError):
        run_manifest.create_run_dir(tmp_path, run.name)


@pytest.mark.parametrize("run_id", ["", ".", "../escape", "nested/name"])
def test_create_run_dir_rejects_non_component_ids(tmp_path, run_id):
    root = tmp_path / "runs"
    root.mkdir()

    with pytest.raises(ValueError, match="run_id"):
        run_manifest.create_run_dir(root, run_id)

    assert not list(root.iterdir())
    assert not (tmp_path / "escape").exists()


def test_create_run_dir_rejects_absolute_ids(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "absolute-run"

    with pytest.raises(ValueError, match="run_id"):
        run_manifest.create_run_dir(root, str(outside))

    assert not list(root.iterdir())
    assert not outside.exists()

def test_artifact_record_is_relative_and_hashed(tmp_path):
    path = tmp_path / "outputs" / "value.csv"
    path.parent.mkdir()
    content = b"a,b\n1,2\n"
    path.write_bytes(content)

    assert run_manifest.artifact_record(path, tmp_path) == {
        "path": "outputs/value.csv",
        "size": 8,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def test_write_manifest_is_utf8_sorted_indented_and_atomic(tmp_path, monkeypatch):
    run = run_manifest.create_run_dir(tmp_path, "run-1")
    payload = {"z": "值", "a": {"é": "茶"}}
    replacements = []
    original_replace = run_manifest.os.replace

    def tracking_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(run_manifest.os, "replace", tracking_replace)

    manifest = run_manifest.write_manifest(run, payload)

    expected = '{\n  "a": {\n    "é": "茶"\n  },\n  "z": "值"\n}\n'
    assert manifest == run / "manifest.json"
    assert manifest.read_bytes() == expected.encode("utf-8")
    assert replacements == [
        (run / ".manifest.json.tmp", run / "manifest.json"),
    ]
    assert not (run / ".manifest.json.tmp").exists()


def test_write_manifest_keeps_previous_file_if_replace_fails(tmp_path, monkeypatch):
    run = run_manifest.create_run_dir(tmp_path, "run-atomic")
    manifest = run_manifest.write_manifest(run, {"status": "running"})

    def fail_replace(*_):
        raise OSError("replace failed")

    monkeypatch.setattr(run_manifest.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        run_manifest.write_manifest(run, {"status": "complete"})

    assert json.loads(manifest.read_text(encoding="utf-8")) == {"status": "running"}


def test_git_output_runs_git_in_requested_root(tmp_path, monkeypatch):
    calls = []

    class Completed:
        stdout = "abcdef\n"

    def fake_run(command, *, cwd, check, capture_output, text):
        calls.append((command, cwd, check, capture_output, text))
        return Completed()

    monkeypatch.setattr(run_manifest.subprocess, "run", fake_run)

    assert run_manifest._git_output(tmp_path, "rev-parse", "HEAD") == "abcdef\n"
    assert calls == [
        (["git", "rev-parse", "HEAD"], tmp_path, True, True, True),
    ]


def test_git_state_records_commit_and_dirty(monkeypatch, tmp_path):
    commit = "abcdef0123456789abcdef0123456789abcdef01"
    answers = {
        ("rev-parse", "HEAD"): f"{commit}\n",
        ("status", "--porcelain"): " M backtest/engine.py\n",
    }

    def fake_git_output(root, *args):
        assert root == tmp_path
        return answers[args]

    monkeypatch.setattr(run_manifest, "_git_output", fake_git_output)

    assert run_manifest.git_state(tmp_path) == {"commit": commit, "dirty": True}


class FakeCursor:
    def __init__(self, values):
        self._values = iter(values)
        self.queries = []
        self._value = None

    def execute(self, sql):
        self.queries.append(sql)
        self._value = (next(self._values),)

    def fetchone(self):
        return self._value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, values):
        self.cursor_instance = FakeCursor(values)

    def cursor(self):
        return self.cursor_instance


def test_query_table_cutoffs_uses_explicit_contract():
    connection = FakeConnection(["2026-08-20", "2026-08-19"])
    contract = {
        "index_daily": "trade_date",
        "stock_financial": "end_date",
    }

    assert run_manifest.query_table_cutoffs(
        connection, "stock_selector", contract
    ) == {
        "index_daily": "2026-08-20",
        "stock_financial": "2026-08-19",
    }
    assert connection.cursor_instance.queries == [
        "SELECT max(trade_date)::text FROM stock_selector.index_daily",
        "SELECT max(end_date)::text FROM stock_selector.stock_financial",
    ]


def test_query_table_cutoffs_rejects_missing_cutoff():
    connection = FakeConnection([None])

    with pytest.raises(
        RuntimeError, match="stock_selector.index_daily.trade_date has no cutoff"
    ):
        run_manifest.query_table_cutoffs(
            connection,
            "stock_selector",
            {"index_daily": "trade_date"},
        )
