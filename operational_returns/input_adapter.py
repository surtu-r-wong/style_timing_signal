"""Read-only input observation and fail-closed event assembly; staging only."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
from zoneinfo import ZoneInfo

from .account import (ROOT, capture_signal, encoded, keys, native_registry, number,
                      timestamp, validate_calendar)

LIMIT = 32 * 1024 * 1024


def clock():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class Blocked(ValueError):
    """Only static, credential-free codes are exposed by this CLI."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path):
    with Path(path).open("rb") as f:
        raw = f.read(LIMIT + 1)
    require(len(raw) <= LIMIT, "SOURCE_TOO_LARGE")
    return raw


class Sources:
    def __init__(self):
        self.raw = {}
        self.provenance = []

    def read(self, path, *, expected=None, project="style_timing_signal", host=None):
        path = Path(path).resolve()
        raw = read_bytes(path)
        require(expected is None or sha(raw) == expected, "SOURCE_HASH_MISMATCH")
        require(path not in self.raw or self.raw[path] == raw, "SOURCE_CHANGED")
        self.raw[path] = raw
        self.provenance.append({"project": project, "host": host or socket.gethostname(),
                                "path": str(path), "sha256": sha(raw), "bytes": len(raw),
                                "retrieved_at": clock()})
        return raw

    def descriptor(self, spec, base):
        keys(spec, "path sha256 project host", "source")
        for k in ("path", "project", "host"):
            require(isinstance(spec[k], str) and bool(spec[k].strip()), "SOURCE_IDENTITY_REQUIRED")
        require(isinstance(spec["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", spec["sha256"]),
                "SOURCE_HASH_REQUIRED")
        return self.read(base/spec["path"], expected=spec["sha256"],
                         project=spec["project"], host=spec["host"])

    def verify(self):
        for path, raw in self.raw.items():
            require(read_bytes(path) == raw, "SOURCE_CHANGED")


def stage(destination, producer):
    """Publish a new immutable directory; blocked bundles never contain event.json."""
    destination = Path(destination).absolute()
    require("input-staging" in destination.resolve().parts, "STAGING_PATH_REQUIRED")
    for forbidden in (ROOT/"output", ROOT/"logs", ROOT/"deploy"):
        require(not destination.resolve().is_relative_to(forbidden.resolve()), "STAGING_PATH_REQUIRED")
    require(not destination.exists(), "STAGING_ALREADY_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
    sources = Sources()
    report = {"observed_at": clock(), "observer_host": socket.gethostname(), "status": "blocked"}
    try:
        try:
            payload = producer(sources, tmp)
            sources.verify()
            report.update(payload)
        except Exception:
            # Never include raw exception text (paths, provider credentials, input values).
            import sys
            exc = sys.exc_info()[1]
            report["reason"] = str(exc) if isinstance(exc, Blocked) else "INVALID_OR_MISSING_INPUT"
            for p in tmp.iterdir():
                p.unlink()
        report["sources"] = sources.provenance
        (tmp/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        # Destination is new for every attempt, so a failed attempt cannot leave a stale event.
        os.rename(tmp, destination)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return report


def inspect(root, destination):
    root = Path(root)
    def produce(sources, tmp):
        try:
            lock = (root/"logs/.daily_signals.lock").open("rb")
        except OSError:
            raise Blocked("PRODUCTION_LOCK_MISSING") from None
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked("PRODUCTION_BUSY") from None
            state = json.loads(sources.read(root/"logs/daily_signals_status.json"))
            files = []
            for name, item in native_registry(root).items():
                for mapping in item["mappings"]:
                    rel = f"output/recommended/{name}_{mapping}.csv"
                    raw = sources.read(root/rel)
                    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                    files.append({"path": rel, "columns": list(rows[0]) if rows else [],
                                  "data_date": rows[-1].get("date") if rows else None,
                                  "status_dates": [v.get("last_date") for v in state.get("files", {}).values()
                                                   if v.get("path") == rel]})
            sources.verify()
        return {"status": "inspection_only", "event_ready": False,
                "production": {k: state.get(k) for k in ("result", "topup", "started_at", "finished_at")},
                "files": files, "reason": "EXPLICIT_CONFIGURATION_AND_COMPLETE_EVIDENCE_REQUIRED"}
    return stage(destination, produce)


def capture(root, strategy_id, mapping, destination):
    def produce(sources, tmp):
        root_path = Path(root)
        with (root_path/"logs/.daily_signals.lock").open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked("PRODUCTION_BUSY") from None
            sources.read(root_path/"logs/daily_signals_status.json")
            for rel in ("backtest/baseline.py", "backtest/production.py"):
                sources.read(root_path/rel)
            native = native_registry(root_path)
            require(strategy_id in native and mapping in native[strategy_id]["mappings"], "NATIVE_MAPPING_REQUIRED")
            sources.read(root_path/f"output/recommended/{strategy_id}_{mapping}.csv")
            # Reuse the existing shared lock/status checks and actual observation clock.
            snapshot = capture_signal(root_path, strategy_id, mapping, tmp/"signal.json", now=clock())
            sources.verify()
        return {"status": "signal_observed", "signal": snapshot, "event_ready": False,
                "production_root": str(root_path.resolve())}

    return stage(destination, produce)


def assemble(config_path, sources):
    config_path = Path(config_path).resolve()
    cfg = json.loads(sources.read(config_path))
    keys(cfg, "strategy_id mapping symbol kind synthetic trade_date signal calendar prices evidence columns", "adapter config")
    require(type(cfg["synthetic"]) is bool, "SYNTHETIC_FLAG_REQUIRED")
    require(isinstance(cfg["symbol"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+", cfg["symbol"]), "SYMBOL_REQUIRED")
    require(cfg["kind"] in ("cash_security", "linear_future"), "UNSUPPORTED_INSTRUMENT")
    native = native_registry()
    require(cfg["strategy_id"] in native and cfg["mapping"] in native[cfg["strategy_id"]]["mappings"], "NATIVE_MAPPING_REQUIRED")
    base = config_path.parent
    signal = json.loads(sources.descriptor(cfg["signal"], base))
    calendar = json.loads(sources.descriptor(cfg["calendar"], base))
    prices_raw = sources.descriptor(cfg["prices"], base)
    evidence = json.loads(sources.descriptor(cfg["evidence"], base))
    validate_calendar(calendar)
    sessions = [d for d in calendar["days"] if d["is_open"]]
    index = next(i for i, d in enumerate(sessions) if d["date"] == cfg["trade_date"])
    require(index > 0, "PREVIOUS_SESSION_REQUIRED")
    session, previous = sessions[index], sessions[index-1]
    execution_at, valuation_at = map(timestamp, (session["execution_at"], session["valuation_at"]))
    keys(signal, "strategy_id mapping trade_date direction available_at source_sha256 synthetic", "signal")
    require(type(signal["synthetic"]) is bool and all(signal[k] == cfg[k] for k in ("strategy_id", "mapping", "synthetic")), "SIGNAL_IDENTITY_MISMATCH")
    require(signal["trade_date"] == previous["date"], "STALE_SIGNAL")
    require(timestamp(previous["valuation_at"]) <= timestamp(signal["available_at"]) < execution_at,
            "SIGNAL_NOT_OBSERVED_BEFORE_EXECUTION")
    require(isinstance(signal["source_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", signal["source_sha256"]), "SIGNAL_DIGEST_REQUIRED")
    direction = number(signal["direction"], "direction")
    require(direction in (-1, 0, 1) and (cfg["mapping"] != "longflat" or direction >= 0), "INVALID_DIRECTION")
    # An externally supplied, pinned attestation binds ALL inputs to a single reviewed set.
    require(evidence.get("input_sha256") == {k: cfg[k]["sha256"] for k in ("signal", "calendar", "prices")}, "EVIDENCE_INPUT_MISMATCH")
    require(all(evidence.get(k) == cfg[k] for k in ("strategy_id", "mapping", "symbol", "kind", "trade_date", "synthetic")), "EVIDENCE_IDENTITY_MISMATCH")
    require(type(evidence.get("synthetic")) is bool, "EVIDENCE_IDENTITY_MISMATCH")
    for field in ("authority", "reference", "execution_basis", "valuation_basis"):
        require(isinstance(evidence.get(field), str) and bool(evidence[field].strip()), "EVIDENCE_REFERENCE_REQUIRED")
    for field in ("calendar_complete", "session_complete", "execution_verified", "corporate_actions_complete", "external_flows_complete"):
        require(evidence.get(field) is True, "INCOMPLETE_EXTERNAL_EVIDENCE")
    require(evidence.get("executable") is True, "EXECUTION_BLOCKED")
    require(number(evidence.get("external_flow"), "external_flow") == 0 and evidence.get("corporate_actions") == [], "UNSUPPORTED_FLOW_OR_ACTION")
    require(evidence.get("execution_at") == session["execution_at"] and evidence.get("valuation_at") == session["valuation_at"], "EVIDENCE_SESSION_MISMATCH")
    if cfg["kind"] == "linear_future":
        require(evidence.get("fixed_contract") is True and evidence.get("requires_roll") is False, "CONTRACT_ROLL_BLOCKED")
        require(valuation_at < timestamp(evidence.get("expires_at")), "CONTRACT_EXPIRED_OR_EXPIRING")
        require(evidence["valuation_basis"] == "settlement", "SETTLEMENT_REQUIRED")
    columns = cfg["columns"]
    keys(columns, "trade_date symbol execution_price valuation_price execution_at valuation_at available_at", "columns")
    require(all(isinstance(v, str) and v for v in columns.values()) and len(set(columns.values())) == len(columns), "INVALID_COLUMN_MAPPING")
    reader = csv.DictReader(io.StringIO(prices_raw.decode("utf-8-sig")))
    headers = reader.fieldnames or []
    require(len(set(headers)) == len(headers) and set(columns.values()) <= set(headers), "INVALID_PRICE_HEADER")
    rows = list(reader)
    selected = [r for r in rows if r[columns["trade_date"]] == cfg["trade_date"] and r[columns["symbol"]] == cfg["symbol"]]
    require(len(selected) == 1, "MISSING_OR_DUPLICATE_PRICE")
    row = {k: selected[0][v] for k, v in columns.items()}
    require(timestamp(row["execution_at"]) == execution_at and timestamp(row["valuation_at"]) == valuation_at, "PRICE_SESSION_MISMATCH")
    price = number(float(row["execution_price"]), "execution_price", positive=True)
    mark = number(float(row["valuation_price"]), "valuation_price", positive=True)
    now = clock()
    require(valuation_at <= timestamp(row["available_at"]) <= timestamp(evidence["checked_at"]) <= timestamp(now), "INVALID_MARKET_AVAILABILITY")
    # Source publication, observer acquisition and valuation time are distinct.
    event = {"trade_date": cfg["trade_date"], "symbol": cfg["symbol"],
             "execution_price": price, "valuation_price": mark, "available_at": now,
             "source": "input-adapter:evidence-sha256:" + cfg["evidence"]["sha256"],
             "executable": evidence["executable"], "external_flow": evidence["external_flow"],
             "corporate_actions": evidence["corporate_actions"], "signal": signal}
    sources.verify()
    return event, {"status": "ready", "event_ready": True, "synthetic": cfg["synthetic"],
                   "data_date": cfg["trade_date"], "session": session, "previous_session": previous,
                   "market_available_at": row["available_at"], "evidence_checked_at": evidence["checked_at"],
                   "signal_observed_at": signal["available_at"], "event_sha256": sha(encoded(event).encode()),
                   "account_economics_validated": False}


def prepare(config_path, destination):
    def produce(sources, tmp):
        event, report = assemble(config_path, sources)
        (tmp/"event.json").write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n")
        return report
    return stage(destination, produce)


# Deliberately bounded existing sources, no arbitrary SQL, no main-contract selection.
MARKETS = {
    "futures_daily": ("public", "futures_daily", "symbol", "trade_date,symbol,open,close,settle,volume,oi"),
    "index_daily": ("stock_selector", "index_daily", "index_code", "trade_date,index_code,open,close"),
    "stock_status": ("stock_selector", "stock_status", "ts_code", "trade_date,ts_code,is_suspended,limit_up,limit_down,updated_at"),
}


def database_read(queries):
    """Existing connection profile, one bounded read-only transaction, no raw errors."""
    try:
        from signals.common.config import load_db_config
        import psycopg2
        db = load_db_config()
        con = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                               user=db["user"], password=db["password"], connect_timeout=8)
        try:
            con.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
            with con.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '8s'")
                cur.execute("SELECT transaction_timestamp()::text")
                transaction_at = cur.fetchone()[0]
                results = []
                for sql, params in queries:
                    cur.execute(sql, params)
                    results.append(cur.fetchall())
            con.rollback()
        finally:
            con.close()
    except Exception:
        raise Blocked("READ_ONLY_DATABASE_UNAVAILABLE") from None
    return transaction_at, results


def catalog(destination):
    def produce(sources, tmp):
        queries = [("SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
                   for schema, table, _, _ in MARKETS.values()]
        stamp, results = database_read(queries)
        tables = [{"table": f"{spec[0]}.{spec[1]}", "columns": rows}
                  for spec, rows in zip(MARKETS.values(), results)]
        return {"status": "inspection_only", "event_ready": False,
                "source_project": "market-monitor", "connection_profile": "signals.common.config.load_db_config",
                "transaction_at": stamp, "retrieved_at": clock(), "tables": tables,
                "sha256": sha(encoded(tables).encode()), "data_date": None,
                "reason": "SCHEMA_ONLY_NO_INSTRUMENT_SELECTED"}
    return stage(destination, produce)


def market(source, symbol, trade_date, destination):
    def produce(sources, tmp):
        from datetime import date
        date.fromisoformat(trade_date)
        require(isinstance(symbol, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", symbol), "SYMBOL_REQUIRED")
        schema, table, key, columns = MARKETS[source]
        transaction_at, results = database_read([
            (f"SELECT {columns} FROM {schema}.{table} WHERE {key}=%s AND trade_date=%s LIMIT 3", (symbol, trade_date))])
        rows = results[0]
        text = io.StringIO(); writer = csv.writer(text); writer.writerow(columns.split(",")); writer.writerows(rows)
        raw = text.getvalue().encode(); (tmp/"market.csv").write_bytes(raw)
        return {"status": "candidate_only", "event_ready": False, "source": f"{schema}.{table}",
                "source_project": "market-monitor", "connection_profile": "signals.common.config.load_db_config",
                "data_date": trade_date, "symbol": symbol, "rows": len(rows), "fields": columns.split(","),
                "transaction_at": transaction_at, "retrieved_at": clock(), "sha256": sha(raw),
                "missing": ["source_publication_time", "execution_time_evidence", "complete_sessions",
                            "executable_evidence", "fixed_contract_expiry_if_future"],
                "reason": "DAILY_ROW_IS_NOT_EXECUTABILITY_OR_CALENDAR_PROOF"}
    return stage(destination, produce)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect"); p.add_argument("--root", default=str(ROOT)); p.add_argument("--staging", required=True)
    p = sub.add_parser("capture"); p.add_argument("strategy_id"); p.add_argument("mapping"); p.add_argument("--root", default=str(ROOT)); p.add_argument("--staging", required=True)
    p = sub.add_parser("prepare"); p.add_argument("config"); p.add_argument("--staging", required=True)
    p = sub.add_parser("catalog"); p.add_argument("--staging", required=True)
    p = sub.add_parser("market"); p.add_argument("source", choices=MARKETS); p.add_argument("symbol"); p.add_argument("trade_date"); p.add_argument("--staging", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect(args.root, args.staging)
        elif args.command == "capture":
            report = capture(args.root, args.strategy_id, args.mapping, args.staging)
        elif args.command == "catalog":
            report = catalog(args.staging)
        elif args.command == "prepare":
            report = prepare(args.config, args.staging)
        else:
            report = market(args.source, args.symbol, args.trade_date, args.staging)
    except Exception:
        print('{"status":"blocked","reason":"INVALID_STAGING_OR_INPUT"}')
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
