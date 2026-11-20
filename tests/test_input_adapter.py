"""Synthetic acceptance for read-only operational input adapters."""
import hashlib
import json
from pathlib import Path

import pytest

from operational_returns import input_adapter as adapter
from operational_returns.account import advance
from test_operational_returns import config, event, ready

NOW = "2026-10-13T17:00:00+08:00"


def write(path, obj):
    raw = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "project": "SYNTHETIC", "host": "SYNTHETIC"}


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "clock", lambda: NOW)
    cfg = {"strategy_id": "equal_weight", "mapping": "symmetric", "symbol": "SYNTHETIC",
           "kind": "cash_security", "synthetic": True, "trade_date": "2026-10-13"}
    cfg["signal"] = write(tmp_path/"signal.json", event()["signal"])
    cfg["calendar"] = write(tmp_path/"calendar.json", config()["calendar"])
    cfg["prices"] = write(tmp_path/"prices.csv", b"date,ticker,open,close,fill_time,mark_time,published\n2026-10-13,SYNTHETIC,100,110,2026-10-13T09:30:00+08:00,2026-10-13T15:00:00+08:00,2026-10-13T16:00:00+08:00\n")
    cfg["columns"] = dict(zip("trade_date symbol execution_price valuation_price execution_at valuation_at available_at".split(),
                             "date ticker open close fill_time mark_time published".split()))
    ev = {k: cfg[k] for k in ("strategy_id", "mapping", "symbol", "kind", "synthetic", "trade_date")}
    ev.update(input_sha256={k: cfg[k]["sha256"] for k in ("signal", "calendar", "prices")},
              authority="SYNTHETIC test reviewer", reference="SYNTHETIC evidence", execution_basis="SYNTHETIC open",
              valuation_basis="SYNTHETIC close", calendar_complete=True, session_complete=True,
              execution_verified=True, corporate_actions_complete=True, external_flows_complete=True,
              executable=True, external_flow=0, corporate_actions=[], checked_at="2026-10-13T16:30:00+08:00",
              execution_at="2026-10-13T09:30:00+08:00", valuation_at="2026-10-13T15:00:00+08:00")
    cfg["evidence"] = write(tmp_path/"evidence.json", ev)
    p = tmp_path/"adapter.json"; write(p, cfg)
    return p, cfg, ev


def restamp(inputs, key, obj):
    path, cfg, ev = inputs
    cfg[key] = write(Path(cfg[key]["path"]), obj)
    if key in ev["input_sha256"]:
        ev["input_sha256"][key] = cfg[key]["sha256"]
    cfg["evidence"] = write(Path(cfg["evidence"]["path"]), ev)
    write(path, cfg)


def test_ready_event_consumed_by_existing_account(inputs, tmp_path):
    dest = tmp_path/"input-staging"/"ready"
    report = adapter.prepare(inputs[0], dest)
    assert report["status"] == "ready"
    e = json.loads((dest/"event.json").read_text())
    assert e["available_at"] == NOW
    assert e["signal"]["available_at"] == "2026-10-12T20:31:00+08:00"
    db = ready(tmp_path/"synthetic")
    assert advance(db, "SYNTHETIC-demo", e, now=NOW)["nav"] == pytest.approx(1099)
    assert not list(dest.glob("*.sqlite3"))
    assert len(report["sources"]) == 5
    assert report["account_economics_validated"] is False
    with pytest.raises(adapter.Blocked, match="ALREADY_EXISTS"):
        adapter.prepare(inputs[0], dest)


@pytest.mark.parametrize("field,value", [
    ("calendar_complete", False), ("session_complete", None), ("execution_verified", 1),
    ("executable", False), ("corporate_actions_complete", False), ("external_flows_complete", False),
    ("external_flow", 1), ("corporate_actions", ["split"]), ("authority", ""),
    ("checked_at", "2026-10-14T16:00:00+08:00"),
    ("execution_at", "2026-10-13T10:00:00+08:00"), ("symbol", "OTHER"),
])
def test_external_evidence_never_defaulted(inputs, tmp_path, field, value):
    inputs[2][field] = value
    restamp(inputs, "evidence", inputs[2])
    dest = tmp_path/"input-staging"/"blocked"
    result = adapter.prepare(inputs[0], dest)
    assert result["status"] == "blocked"
    assert not (dest/"event.json").exists()


@pytest.mark.parametrize("field,value", [
    ("trade_date", "2026-10-11"), ("available_at", "2026-10-13T09:30:00+08:00"),
    ("available_at", "2026-10-12T14:59:00+08:00"), ("synthetic", False),
    ("source_sha256", ""), ("mapping", "longflat"), ("direction", 2),
])
def test_invalid_signal_blocks(inputs, tmp_path, field, value):
    sig = json.loads(Path(inputs[1]["signal"]["path"]).read_text()); sig[field] = value
    restamp(inputs, "signal", sig)
    report = adapter.prepare(inputs[0], tmp_path/"input-staging"/"blocked")
    assert report["status"] == "blocked"


def test_unpinned_half_update_and_manifest_mismatch(inputs, tmp_path):
    p = Path(inputs[1]["prices"]["path"]); p.write_bytes(p.read_bytes().replace(b",110,", b",111,"))
    assert adapter.prepare(inputs[0], tmp_path/"input-staging"/"hash")["reason"] == "SOURCE_HASH_MISMATCH"
    inputs[1]["prices"] = write(p, p.read_bytes()); write(inputs[0], inputs[1])
    assert adapter.prepare(inputs[0], tmp_path/"input-staging"/"mixed")["reason"] == "EVIDENCE_INPUT_MISMATCH"


def test_mid_read_update_rejected(inputs, tmp_path, monkeypatch):
    original = adapter.Sources.verify
    def change(self):
        p = Path(inputs[1]["prices"]["path"]); p.write_bytes(p.read_bytes()+b"\n")
        original(self)
    monkeypatch.setattr(adapter.Sources, "verify", change)
    dest = tmp_path/"input-staging"/"changed"
    assert adapter.prepare(inputs[0], dest)["reason"] == "SOURCE_CHANGED"
    assert not (dest/"event.json").exists()


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "nan", "wrong_time", "duplicate_header", "missing_calendar_day"])
def test_price_and_calendar_coverage(inputs, tmp_path, mutation):
    if mutation == "missing_calendar_day":
        cal = config()["calendar"]; cal["days"].pop(1); restamp(inputs, "calendar", cal)
    else:
        raw = Path(inputs[1]["prices"]["path"]).read_bytes()
        raw = {"duplicate": raw+raw.splitlines(keepends=True)[1], "missing": raw.splitlines(keepends=True)[0],
               "nan": raw.replace(b",110,", b",nan,"), "wrong_time": raw.replace(b"T09:30", b"T10:30"), "duplicate_header": raw.replace(b"open,close", b"open,open")}[mutation]
        restamp(inputs, "prices", raw)
    assert adapter.prepare(inputs[0], tmp_path/"input-staging"/mutation)["status"] == "blocked"


@pytest.mark.parametrize("expiry,roll,settlement,status", [
    ("2026-10-14T15:00:00+08:00", False, "settlement", "ready"),
    ("2026-10-13T15:00:00+08:00", False, "settlement", "blocked"),
    ("2026-10-12T15:00:00+08:00", False, "settlement", "blocked"),
    (None, False, "settlement", "blocked"),
    ("2026-10-14T15:00:00+08:00", True, "settlement", "blocked"),
    ("2026-10-14T15:00:00+08:00", False, "close", "blocked"),
])
def test_fixed_future_expiry_and_roll(inputs, tmp_path, expiry, roll, settlement, status):
    inputs[1]["kind"] = "linear_future"
    inputs[2].update(kind="linear_future", fixed_contract=True, requires_roll=roll,
                     expires_at=expiry, valuation_basis=settlement)
    restamp(inputs, "evidence", inputs[2])
    assert adapter.prepare(inputs[0], tmp_path/"input-staging"/"future")["status"] == status


def test_template_blocks_without_economic_defaults(tmp_path):
    template = adapter.ROOT/"examples/operational_returns/input-adapter.template.json"
    dest = tmp_path/"input-staging"/"template"
    assert adapter.prepare(template, dest)["status"] == "blocked"
    assert list(dest.iterdir()) == [dest/"report.json"]


def test_staging_guard(inputs, tmp_path):
    with pytest.raises(adapter.Blocked, match="STAGING_PATH_REQUIRED"):
        adapter.prepare(inputs[0], tmp_path/"output")


def test_capture_reuses_production_checks(tmp_path, monkeypatch):
    import fcntl
    root = tmp_path/"production"
    (root/"backtest").mkdir(parents=True)
    (root/"backtest/baseline.py").write_text("SIGNALS={'equal_weight':('unused','value')}\n")
    (root/"backtest/production.py").write_text("PRODUCTION_MAPPING={'equal_weight':'symmetric'}\nREFERENCE_OUTPUTS={}\nPOOLS={}\n")
    (root/"logs").mkdir(); (root/"logs/.daily_signals.lock").touch()
    (root/"output/recommended").mkdir(parents=True)
    rel = "output/recommended/equal_weight_symmetric.csv"
    (root/rel).write_text("date,position\n2026-10-12,1\n")
    write(root/"logs/daily_signals_status.json", {"result": "OK", "topup": "OFFICE_OK",
          "finished_at": "2026-10-12T20:30:00+08:00", "files": {"item": {"path": rel, "last_date": "2026-10-12"}}})
    monkeypatch.setattr(adapter, "clock", lambda: "2026-10-12T21:00:00+08:00")
    target = tmp_path/"input-staging"/"capture"
    result = adapter.capture(root, "equal_weight", "symmetric", target)
    assert result["status"] == "signal_observed"
    assert json.loads((target/"signal.json").read_text())["available_at"] == "2026-10-12T21:00:00+08:00"
    from tools.observe_operational_source import observe
    observed = observe(root)
    assert observed["event_ready"] is False
    assert observed["files"][0]["data_date"] == "2026-10-12"
    assert len(observed["sources"]) == 2
    status_path = root/"logs/daily_signals_status.json"
    state = json.loads(status_path.read_text()); state["topup"] = "OFFICE_LATE"; write(status_path, state)
    assert adapter.capture(root, "equal_weight", "symmetric", tmp_path/"input-staging"/"late")["status"] == "blocked"
    with (root/"logs/.daily_signals.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert adapter.capture(root, "equal_weight", "symmetric", tmp_path/"input-staging"/"busy")["status"] == "blocked"


def test_database_transactions_are_readonly_and_failures_redacted(monkeypatch):
    import psycopg2
    from signals.common import config as dbconfig
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None): calls.append((sql, params))
        def fetchone(self): return (NOW,)
        def fetchall(self): return [("2026-10-13", "SYNTHETIC", 100, 110)]
    class Connection:
        def set_session(self, **kwargs): calls.append(kwargs)
        def cursor(self): return Cursor()
        def rollback(self): calls.append("rollback")
        def close(self): calls.append("close")
    monkeypatch.setattr(dbconfig, "load_db_config", lambda: dict(host="unused", port=1, name="unused", user="unused", password="SYNTHETIC_SECRET"))
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: Connection())
    adapter.database_read([("SELECT bounded_data WHERE symbol=%s", ("SYNTHETIC",))])
    assert calls[0] == dict(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
    assert calls[-2:] == ["rollback", "close"]
    assert calls[3][1] == ("SYNTHETIC",)
    def fail(**kwargs): raise RuntimeError("SYNTHETIC_SECRET")
    monkeypatch.setattr(psycopg2, "connect", fail)
    with pytest.raises(adapter.Blocked, match="^READ_ONLY_DATABASE_UNAVAILABLE$"):
        adapter.database_read([])


def test_market_exact_symbol_and_date_never_imply_executability(tmp_path, monkeypatch):
    def read(queries):
        sql, params = queries[0]
        assert params == ("IC2610.CFE", "2026-10-13")
        assert "LIMIT 3" in sql and "LIKE" not in sql
        return NOW, [[("2026-10-13", "IC2610.CFE", 100, 110, 109, 5, 10)]]
    monkeypatch.setattr(adapter, "database_read", read)
    target = tmp_path/"input-staging"/"candidate"
    report = adapter.market("futures_daily", "IC2610.CFE", "2026-10-13", target)
    assert report["event_ready"] is False
    assert report["status"] == "candidate_only"
    assert not (target/"event.json").exists()
    assert (target/"market.csv").exists()


def test_database_failure_only_writes_blocked_report(tmp_path, monkeypatch):
    def fail(queries): raise adapter.Blocked("READ_ONLY_DATABASE_UNAVAILABLE")
    monkeypatch.setattr(adapter, "database_read", fail)
    target = tmp_path/"input-staging"/"failure"
    assert adapter.catalog(target)["reason"] == "READ_ONLY_DATABASE_UNAVAILABLE"
    assert list(target.iterdir()) == [target/"report.json"]


def test_cli_has_no_advance_and_reports_failure(inputs, tmp_path, capsys):
    missing = tmp_path/"missing.json"
    assert adapter.main(["prepare", str(missing), "--staging", str(tmp_path/"input-staging"/"cli")]) == 1
    assert '"status": "blocked"' in capsys.readouterr().out
