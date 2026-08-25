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


# ---------------------------------------------------------------- 输入漂移检测（2026-08-25 立）
class FakeParamCursor:
    """记录 (sql, params) 的游标；`execute` 需接受可选参数（`rows_touched_in_window` 用）。"""

    def __init__(self, values):
        self._values = iter(values)
        self.calls = []
        self._value = None

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._value = (next(self._values),)

    def fetchone(self):
        return self._value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeParamConnection:
    def __init__(self, values):
        self.cursor_instance = FakeParamCursor(values)

    def cursor(self):
        return self.cursor_instance


CONTRACT2 = {"index_daily": "trade_date", "stock_financial": "end_date"}


def test_query_table_write_marks_reads_updated_at_not_the_date_column():
    """写入时刻读的是 `updated_at`；cutoffs 读的是数据日期列 —— 两者不能混。"""
    conn = FakeParamConnection(["2026-08-25 11:20:26", "2026-08-21 08:52:53"])
    assert run_manifest.query_table_write_marks(conn, "stock_selector", CONTRACT2) == {
        "index_daily": "2026-08-25 11:20:26",
        "stock_financial": "2026-08-21 08:52:53",
    }
    assert [c[0] for c in conn.cursor_instance.calls] == [
        "SELECT max(updated_at)::text FROM stock_selector.index_daily",
        "SELECT max(updated_at)::text FROM stock_selector.stock_financial",
    ]


def test_rows_touched_in_window_uses_each_table_own_date_column():
    conn = FakeParamConnection([684318, 0])
    got = run_manifest.rows_touched_in_window(
        conn, "stock_selector", CONTRACT2, "2026-08-25 10:19:41", "2026-08-18")
    assert got == {"index_daily": 684318, "stock_financial": 0}
    assert conn.cursor_instance.calls[0] == (
        "SELECT count(*) FROM stock_selector.index_daily "
        "WHERE updated_at > %s AND trade_date <= %s",
        ("2026-08-25 10:19:41", "2026-08-18"),
    )
    assert conn.cursor_instance.calls[1][0].endswith("AND end_date <= %s")


def test_input_drift_report_clean_run_is_registrable():
    """写入时刻一动没动 → 可登记，且**不发**窗口内计数查询（省一趟全表扫）。"""
    before = {"index_daily": "2026-08-20 10:00:00", "stock_financial": "2026-08-21 08:52:53"}
    conn = FakeParamConnection(["2026-08-20 10:00:00", "2026-08-21 08:52:53"])
    rep = run_manifest.input_drift_report(
        conn, "stock_selector", CONTRACT2, before, "2026-08-25 14:30:00", "2026-08-18")
    assert rep["inputs_moved"] is False
    assert rep["registrable_as_first_run"] is True
    assert rep["moved_tables"] == {}
    assert len(conn.cursor_instance.calls) == 2          # 只有 write_marks 那两条


def test_input_drift_report_keeps_registrable_when_writes_are_outside_the_window():
    """日更 timer 只追加当天行（窗口外）→ 标记动了但**不拦登记**。

    这条区分是本机制可用的前提：不加它，每天 18:30 的日更都会把 run 标脏。
    """
    before = {"index_daily": "2026-08-25 10:00:00", "stock_financial": "2026-08-21 08:52:53"}
    conn = FakeParamConnection([
        "2026-08-25 18:30:11", "2026-08-21 08:52:53",   # marks after：index_daily 动了
        0, 0,                                            # 窗口内行数：全 0
    ])
    rep = run_manifest.input_drift_report(
        conn, "stock_selector", CONTRACT2, before, "2026-08-25 10:19:41", "2026-08-18")
    assert rep["inputs_moved"] is True
    assert rep["inputs_moved_in_window"] is False
    assert rep["registrable_as_first_run"] is True
    assert rep["rows_touched_in_window"] == {}
    assert rep["moved_tables"]["index_daily"] == {
        "before": "2026-08-25 10:00:00", "after": "2026-08-25 18:30:11"}


def test_input_drift_report_blocks_registration_on_in_window_rewrite():
    """2026-08-25 实况：另一会话在 run 期间补 stock_indicator 的历史洞（窗口内）→ 拦。"""
    before = {"index_daily": "2026-08-25 11:20:26", "stock_financial": "2026-08-21 08:52:53"}
    conn = FakeParamConnection([
        "2026-08-25 13:09:33", "2026-08-21 08:52:53",   # marks after
        684318, 0,                                       # 窗口内行数
    ])
    rep = run_manifest.input_drift_report(
        conn, "stock_selector", CONTRACT2, before, "2026-08-25 10:19:41", "2026-08-18")
    assert rep["inputs_moved_in_window"] is True
    assert rep["registrable_as_first_run"] is False
    assert rep["rows_touched_in_window"] == {"index_daily": 684318}
