"""Explicit paper accounts with transactional accounting and immutable revisions.

All rates, prices and capital are supplied by the caller; nothing is inferred from
research returns. SQLite is the commit authority; publication is a retryable view.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError
        return dt
    except (ValueError, TypeError):
        raise ValueError("timestamp must be ISO-8601 with timezone") from None


def number(value, name, *, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if (minimum is not None and value < minimum) or (positive and value <= 0):
        raise ValueError(f"invalid {name}")
    return float(value)


def keys(obj, expected, name):
    if not isinstance(obj, dict) or set(obj) != set(expected.split()):
        raise ValueError(f"{name}: missing or unknown fields; required: {expected}")


def text_field(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {name}")


def native_registry(root=ROOT):
    """Read native declarations without importing pandas or executing strategy code."""
    root = Path(root)
    def literal(path, name):
        for node in ast.parse((root/path).read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    raise ValueError(f"native {name} is no longer literal; update registry adapter") from None
        raise ValueError(f"native declaration missing: {name}")
    signals = literal("backtest/baseline.py", "SIGNALS")
    mappings = literal("backtest/production.py", "PRODUCTION_MAPPING")
    references = literal("backtest/production.py", "REFERENCE_OUTPUTS")
    pools = literal("backtest/production.py", "POOLS")
    result = {}
    for name, (path, column) in signals.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name not in mappings:
            raise ValueError("invalid native signal registration")
        allowed = {mappings[name]}
        if name in references:
            allowed.add(references[name])
        allowed.update(mapping for signal, mapping in pools.values() if signal == name)
        if not allowed <= {"longflat", "symmetric"}:
            raise ValueError("unsupported native mapping")
        result[name] = {"signal_path": path, "column": column, "production_mapping": mappings[name],
                        "mappings": sorted(allowed),
                        "pools": {pool: mapping for pool, (signal, mapping) in pools.items() if signal == name}}
    return result


def validate_config(cfg):
    keys(cfg, "account_id synthetic mode strategy_id strategy_version mapping currency initial_cash instrument execution fees cash_rules calendar", "config")
    for field in ("account_id", "strategy_id", "strategy_version", "currency"):
        if not isinstance(cfg[field], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", cfg[field]):
            raise ValueError(f"invalid {field}")
    if type(cfg["synthetic"]) is not bool or cfg["mode"] != "paper":
        raise ValueError("only explicit synthetic flag and paper mode supported; live adapter missing")
    if cfg["synthetic"] and not cfg["account_id"].startswith("SYNTHETIC-"):
        raise ValueError("synthetic account_id must start SYNTHETIC-")
    native = native_registry()
    if cfg["strategy_id"] not in native or cfg["mapping"] not in native[cfg["strategy_id"]]["mappings"]:
        raise ValueError("strategy/mapping not registered natively")
    number(cfg["initial_cash"], "initial_cash", positive=True)
    ins = cfg["instrument"]
    keys(ins, "symbol kind currency multiplier lot_size margin_rate settlement_rule", "instrument")
    text_field(ins["symbol"], "symbol")
    if ins["currency"] != cfg["currency"]:
        raise ValueError("multi-currency unsupported")
    if ins["kind"] not in ("cash_security", "linear_future"):
        raise ValueError("only cash_security and linear_future supported")
    expected_settlement = "cash_trade_date" if ins["kind"] == "cash_security" else "daily_variation_margin"
    if ins["settlement_rule"] != expected_settlement:
        raise ValueError("unsupported settlement_rule for instrument kind")
    number(ins["multiplier"], "multiplier", positive=True)
    number(ins["lot_size"], "lot_size", positive=True)
    if ins["kind"] == "cash_security":
        if ins["margin_rate"] is not None:
            raise ValueError("cash security margin_rate must be null; short collateral is explicit separately")
    elif not 0 < number(ins["margin_rate"], "margin_rate") <= 1:
        raise ValueError("future margin_rate must be in (0,1]")
    ex = cfg["execution"]
    keys(ex, "rule rebalance gross_leverage slippage_bps shorting short_proceeds short_collateral_ratio", "execution")
    if ex["rule"] != "next_session_fixed_time" or ex["rebalance"] != "each_session":
        raise ValueError("unsupported execution/rebalance rule")
    number(ex["gross_leverage"], "gross_leverage", positive=True)
    if not 0 <= number(ex["slippage_bps"], "slippage_bps") < 10000:
        raise ValueError("invalid slippage_bps")
    if ex["shorting"] not in ("allowed", "forbidden") or ex["short_proceeds"] != "available":
        raise ValueError("shorting must be explicit; only available short proceeds supported")
    number(ex["short_collateral_ratio"], "short_collateral_ratio", minimum=0)
    fees = cfg["fees"]
    keys(fees, "commission_bps sell_tax_bps per_unit description", "fees")
    for name in ("commission_bps", "sell_tax_bps", "per_unit"):
        number(fees[name], name, minimum=0)
    text_field(fees["description"], "fees.description")
    cash = cfg["cash_rules"]
    keys(cash, "credit_rate debit_rate short_borrow_rate day_count external_flows", "cash_rules")
    for name in ("credit_rate", "debit_rate", "short_borrow_rate"):
        number(cash[name], name, minimum=0)
    if cash["day_count"] != "ACT/365" or cash["external_flows"] != "reject":
        raise ValueError("only ACT/365 and no external flows supported")
    if ins["kind"] == "linear_future" and cash["short_borrow_rate"] != 0:
        raise ValueError("linear_future has no securities borrow; short_borrow_rate must be explicit zero")
    validate_calendar(cfg["calendar"])
    return cfg


def validate_calendar(cal):
    """Validate complete sessions independently of account economic parameters."""
    keys(cal, "id timezone source coverage_start coverage_end days", "calendar")
    text_field(cal["id"], "calendar.id"); text_field(cal["source"], "calendar.source")
    zone = ZoneInfo(cal["timezone"])
    start, end = date.fromisoformat(cal["coverage_start"]), date.fromisoformat(cal["coverage_end"])
    if not start <= end or (end-start).days > 36600:
        raise ValueError("invalid calendar coverage")
    if not isinstance(cal["days"], list):
        raise ValueError("calendar days must be a list")
    expected = [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    if [day.get("date") for day in cal["days"]] != expected:
        raise ValueError("calendar must completely cover every date, including closed days")
    previous = None
    for day in cal["days"]:
        if type(day.get("is_open")) is not bool:
            raise ValueError("calendar is_open must be boolean")
        keys(day, "date is_open execution_at valuation_at" if day["is_open"] else "date is_open", "calendar day")
        if day["is_open"]:
            execution, valuation = timestamp(day["execution_at"]), timestamp(day["valuation_at"])
            for t in (execution, valuation):
                if t.astimezone(zone).date().isoformat() != day["date"] or t.utcoffset() != t.astimezone(zone).utcoffset():
                    raise ValueError("session date/timezone mismatch; overnight sessions unsupported")
            if not execution < valuation or (previous and not previous < execution):
                raise ValueError("session timestamps must increase")
            previous = valuation
    if previous is None:
        raise ValueError("calendar has no open sessions")
    return cal


def atomic_json(path, value, *, replace=True):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(encoded(value)+"\n"); f.flush(); os.fsync(f.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)  # Atomic create-if-absent for immutable observations.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def connect(db):
    db = Path(db); db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=FULL")
    con.executescript("""
      CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY, config TEXT NOT NULL, config_hash TEXT NOT NULL,
        initialized_at TEXT NOT NULL, state TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, trade_date TEXT NOT NULL,
        revision INTEGER NOT NULL, event_hash TEXT NOT NULL, event TEXT NOT NULL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL, row_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL, UNIQUE(account_id, trade_date, revision));
      CREATE TABLE IF NOT EXISTS calendar_extensions (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, calendar TEXT NOT NULL,
        recorded_at TEXT NOT NULL);
    """)
    try:
        with con:
            yield con
    finally:
        con.close()


def get_account(con, account_id):
    a = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if a is None:
        raise ValueError("account not initialized")
    return a, json.loads(a["config"]), json.loads(a["state"])


def status(db, account_id, now, outcome, error=None):
    atomic_json(Path(db).with_suffix(".status.json"), {"account_id": account_id, "attempted_at": now,
                "status": outcome, "error": error})


def account_state(db, account_id):
    with connect(db) as con:
        return get_account(con, account_id)[2]


def initialize(db, cfg, *, now):
    """Initialize cash at actual invocation time; no historical rows are created."""
    validate_config(cfg); t = timestamp(now)
    cal = cfg["calendar"]
    local_date = t.astimezone(ZoneInfo(cal["timezone"])).date().isoformat()
    if not cal["coverage_start"] <= local_date <= cal["coverage_end"]:
        raise ValueError("initialization outside calendar coverage")
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        prior = con.execute("SELECT * FROM accounts WHERE id=?", (cfg["account_id"],)).fetchone()
        if prior:
            if digest(json.loads(prior["config"])) != digest(cfg):
                raise ValueError("config drift; use a new account identity")
            return json.loads(prior["state"])
        # A shared ledger may never silently mix synthetic and genuine accounts.
        for existing in con.execute("SELECT config FROM accounts"):
            if json.loads(existing[0])["synthetic"] != cfg["synthetic"]:
                raise ValueError("synthetic and genuine accounts require separate databases")
        state = {"cash": cfg["initial_cash"], "nav": cfg["initial_cash"], "quantity": 0,
                 "mark": None, "as_of": None, "trade_date": None, "baseline_nav": None}
        con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)", (cfg["account_id"], encoded(cfg), digest(cfg), now, encoded(state)))
    return state


def extend_calendar(db, account_id, calendar, *, now):
    """Append verified calendar coverage without changing economic account identity."""
    timestamp(now)
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        _, cfg, _ = get_account(con, account_id)
        old = cfg["calendar"]
        if calendar == old:
            return old
        if (not isinstance(calendar, dict) or
            any(calendar.get(k) != old[k] for k in ("id", "timezone", "source", "coverage_start")) or
            calendar.get("days", [])[:len(old["days"])] != old["days"] or
            calendar.get("coverage_end", "") <= old["coverage_end"]):
            raise ValueError("calendar extension must preserve entire existing prefix and provenance")
        cfg["calendar"] = calendar
        validate_config(cfg)
        con.execute("INSERT INTO calendar_extensions(account_id,calendar,recorded_at) VALUES (?,?,?)",
                    (account_id, encoded(calendar), now))
        con.execute("UPDATE accounts SET config=? WHERE id=?", (encoded(cfg), account_id))
    return calendar


def baseline(db, account_id, trade_date, *, now):
    """First valuation after initialization establishes unit NAV; it is not a return."""
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        a, cfg, state = get_account(con, account_id)
        sessions = [d for d in cfg["calendar"]["days"] if d["is_open"] and timestamp(d["valuation_at"]) >= timestamp(a["initialized_at"])]
        if not sessions or trade_date != sessions[0]["date"]:
            raise ValueError("baseline must be first valuation after actual initialization")
        at = sessions[0]["valuation_at"]
        if timestamp(at) > timestamp(now):
            raise ValueError("baseline valuation is not yet available")
        if state["as_of"]:
            if state["trade_date"] == trade_date and state["quantity"] == 0:
                return state
            raise ValueError("baseline already established")
        years = (timestamp(at)-timestamp(a["initialized_at"])).total_seconds()/(365*86400)
        cash = state["cash"] * (1 + cfg["cash_rules"]["credit_rate"] * years)
        state.update(cash=cash, nav=cash, baseline_nav=cash, as_of=at, trade_date=trade_date)
        con.execute("UPDATE accounts SET state=? WHERE id=?", (encoded(state), account_id))
    return state


def _calculate(cfg, state, event, now):
    validate_config(cfg)
    keys(event, "trade_date symbol execution_price valuation_price available_at source executable external_flow corporate_actions signal", "event")
    if state["as_of"] is None:
        raise ValueError("baseline required before first complete interval")
    sessions = [d for d in cfg["calendar"]["days"] if d["is_open"] and d["date"] > state["trade_date"]]
    if not sessions or event["trade_date"] != sessions[0]["date"]:
        raise ValueError("event must be the next open valuation session; no missing days or coverage inference")
    session = sessions[0]
    start, fill_at, end = map(timestamp, (state["as_of"], session["execution_at"], session["valuation_at"]))
    if not start < fill_at < end <= timestamp(event["available_at"]) <= timestamp(now):
        raise ValueError("invalid valuation/availability interval")
    text_field(event["source"], "market source")
    ins, ex, fees, rules = (cfg[k] for k in ("instrument", "execution", "fees", "cash_rules"))
    if event["symbol"] != ins["symbol"] or event["executable"] is not True:
        raise ValueError("wrong instrument or unavailable execution")
    if number(event["external_flow"], "external_flow") != 0 or event["corporate_actions"] != []:
        raise ValueError("external flows and corporate actions unsupported")
    price = number(event["execution_price"], "execution_price", positive=True)
    mark = number(event["valuation_price"], "valuation_price", positive=True)
    sig = event["signal"]
    keys(sig, "strategy_id mapping trade_date direction available_at source_sha256 synthetic", "signal")
    if type(sig["synthetic"]) is not bool or (sig["strategy_id"], sig["mapping"], sig["synthetic"]) != (cfg["strategy_id"], cfg["mapping"], cfg["synthetic"]):
        raise ValueError("signal identity/synthetic mismatch")
    if sig["trade_date"] != state["trade_date"] or not start <= timestamp(sig["available_at"]) < fill_at:
        raise ValueError("signal must be observed after prior valuation and strictly before execution")
    if not isinstance(sig["source_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", sig["source_sha256"]):
        raise ValueError("missing signal source SHA-256")
    direction = number(sig["direction"], "direction")
    if direction not in (-1, 0, 1) or (cfg["mapping"] == "longflat" and direction < 0):
        raise ValueError("direction inconsistent with mapping")
    if direction < 0 and ex["shorting"] != "allowed":
        raise ValueError("shorting forbidden")
    multiplier, old_q = ins["multiplier"], state["quantity"]
    cash, old_mark = state["cash"], state["mark"]
    def carry(balance, q, value, seconds):
        years = seconds/(365*86400)
        interest = balance * rules["credit_rate" if balance >= 0 else "debit_rate"] * years
        borrow = max(-q, 0)*value*multiplier*rules["short_borrow_rate"]*years if ins["kind"] == "cash_security" else 0
        return interest, borrow
    interest1, borrow1 = carry(cash, old_q, old_mark or price, (fill_at-start).total_seconds())
    cash += interest1-borrow1
    pre_pnl = old_q*(price-old_mark)*multiplier if old_q else 0
    if ins["kind"] == "linear_future":
        cash += pre_pnl
        fill_nav = cash
    else:
        fill_nav = cash+old_q*price*multiplier
    if fill_nav <= 0:
        raise ValueError("account insolvent before execution")
    # Target at the supplied reference price, rounded towards zero to explicit lots.
    # Leverage enters only here. Fees/slippage can change realized gross/NAV.
    target = direction*math.floor(fill_nav*ex["gross_leverage"]/(price*multiplier*ins["lot_size"]))*ins["lot_size"]
    delta = target-old_q
    fill = price*(1 + (1 if delta > 0 else -1 if delta < 0 else 0)*ex["slippage_bps"]/10000)
    commission = abs(delta)*fill*multiplier*fees["commission_bps"]/10000 + abs(delta)*fees["per_unit"]
    tax = max(-delta, 0)*fill*multiplier*fees["sell_tax_bps"]/10000
    cost = commission+tax
    slippage = delta*(fill-price)*multiplier
    if ins["kind"] == "cash_security":
        cash -= delta*fill*multiplier+cost
        fill_equity = cash+target*price*multiplier
        if target < 0 and cash < abs(target)*price*multiplier*ex["short_collateral_ratio"]:
            raise ValueError("insufficient short collateral at execution")
    else:
        cash -= slippage+cost
        fill_equity = cash
        if abs(target)*price*multiplier*ins["margin_rate"] > fill_equity:
            raise ValueError("insufficient futures margin at execution")
    if fill_equity <= 0:
        raise ValueError("account insolvent after fees")
    interest_balance = cash
    if ins["kind"] == "linear_future":
        # PnL on closed contracts settles at execution; remaining MTM settles
        # only at valuation. Do not pay cash interest on unrealized futures PnL.
        closed = min(abs(old_q), abs(delta)) if old_q*delta < 0 else 0
        realized = (1 if old_q > 0 else -1)*closed*(fill-old_mark)*multiplier if closed else 0
        interest_balance = cash-pre_pnl+slippage+realized
    interest2, borrow2 = carry(interest_balance, target, price, (end-fill_at).total_seconds())
    cash += interest2-borrow2
    post_pnl = target*(mark-price)*multiplier
    if ins["kind"] == "linear_future":
        cash += post_pnl
        nav = cash
        margin = abs(target)*mark*multiplier*ins["margin_rate"]
        if margin > nav:
            raise ValueError("insufficient futures margin at valuation")
    else:
        nav = cash+target*mark*multiplier
        margin = None
        if target < 0 and cash < abs(target)*mark*multiplier*ex["short_collateral_ratio"]:
            raise ValueError("insufficient short collateral at valuation")
    if not math.isfinite(nav) or nav <= 0:
        raise ValueError("nonpositive NAV requires explicit insolvency handling")
    row = {"trade_date": event["trade_date"], "period_start_at": state["as_of"],
           "period_end_at": session["valuation_at"], "available_at": now,
           "daily_return": nav/state["nav"]-1, "unit_nav": nav/state["baseline_nav"],
           "gross_ratio": abs(target)*mark*multiplier/nav, "margin_ratio": margin/nav if margin is not None else None,
           "cash": cash, "quantity": target, "nav": nav, "gross_pnl": pre_pnl+post_pnl,
           "fees": cost, "slippage": slippage, "interest": interest1+interest2,
           "borrow_cost": borrow1+borrow2, "execution_price": fill,
           "trade_quantity": delta, "prior_nav": state["nav"],
           "settled_cash_at_execution": interest_balance}
    if not math.isclose(nav-state["nav"], row["gross_pnl"]-cost-slippage+row["interest"]-row["borrow_cost"], abs_tol=1e-8, rel_tol=1e-9):
        raise ValueError("ledger reconciliation failed")
    after = dict(state, cash=cash, nav=nav, quantity=target, mark=mark,
                 as_of=session["valuation_at"], trade_date=event["trade_date"])
    return row, after


def advance(db, account_id, event, *, now, revise=False):
    """Commit one whole session, or explicitly revise only the latest session."""
    try:
        timestamp(now)
        if not isinstance(event, dict):
            raise ValueError("event must be a JSON object")
        event_hash = digest(event)
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            _, cfg, state = get_account(con, account_id)
            old = con.execute("SELECT * FROM records WHERE account_id=? AND trade_date=? ORDER BY revision DESC LIMIT 1",
                              (account_id, event.get("trade_date"))).fetchone()
            if old and old["event_hash"] == event_hash:
                row = json.loads(old["row_json"])
            else:
                revision = 1
                if old:
                    if not revise:
                        raise ValueError("different committed input: explicit revise required")
                    if state["trade_date"] != event["trade_date"]:
                        raise ValueError("only latest interval can be revised; historical replay unsupported")
                    if timestamp(now) <= timestamp(old["recorded_at"]):
                        raise ValueError("revision timestamp must advance")
                    state = json.loads(old["before_state"])
                    revision = old["revision"]+1
                elif revise:
                    raise ValueError("cannot revise a nonexistent interval")
                row, after = _calculate(cfg, state, event, now)
                row["revision"] = revision
                con.execute("INSERT INTO records(account_id,trade_date,revision,event_hash,event,before_state,after_state,row_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (account_id, event["trade_date"], revision, event_hash, encoded(event), encoded(state), encoded(after), encoded(row), now))
                con.execute("UPDATE accounts SET state=? WHERE id=?", (encoded(after), account_id))
        status(db, account_id, now, "ok")
        return row
    except Exception as exc:
        status(db, account_id, now, "failed", str(exc))
        raise


RETURN_FIELDS = "trade_date period_start_at period_end_at available_at daily_return unit_nav gross_ratio margin_ratio".split()


def _export_feed(db, output, *, now, synthetic=False):
    """Publish committed series only. Archive first, then atomically replace latest.

    Holding the database write lock also serializes concurrent exporters. Retrying
    after archive/replace failure is safe; prior archive files are never removed.
    """
    output = Path(output)
    timestamp(now)
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        accounts = con.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        if not accounts:
            raise ValueError("no complete accounts")
        series = []
        for account in accounts:
            cfg = json.loads(account["config"])
            if cfg["synthetic"] != synthetic:
                raise ValueError("synthetic export flag must match every account")
            if synthetic and (output.name == "latest.json" or "synthetic" not in str(output).lower()):
                raise ValueError("synthetic output requires a synthetic directory and cannot be latest.json")
            rows = con.execute("SELECT r.row_json FROM records r WHERE account_id=? AND revision=(SELECT MAX(revision) FROM records WHERE account_id=r.account_id AND trade_date=r.trade_date) ORDER BY trade_date", (account["id"],)).fetchall()
            if not rows:
                raise ValueError("every configured account needs a complete interval before publication")
            daily = [{k: json.loads(r[0])[k] for k in RETURN_FIELDS} for r in rows]
            if any(timestamp(row["available_at"]) > timestamp(now) for row in daily):
                raise ValueError("export precedes committed availability")
            series.append({"strategy": {"source_project": "style_timing_signal", "strategy_id": cfg["strategy_id"],
                                        "version": f'{cfg["strategy_version"]}/{cfg["mapping"]}/{cfg["account_id"]}/{account["config_hash"]}'},
                           "kind": "strategy", "mode": cfg["mode"],
                           "label": ("SYNTHETIC / " if synthetic else "")+cfg["account_id"],
                           "currency": cfg["currency"], "return_basis": "net_nav_time_weighted",
                           "fee_model": encoded({"fees": cfg["fees"], "cash_rules": cfg["cash_rules"],
                                                 "slippage_bps": cfg["execution"]["slippage_bps"]}),
                           "calendar_id": cfg["calendar"]["id"], "inception_date": daily[0]["trade_date"], "rows": daily})
        feed = {"schema_version": 1, "source_project": "style_timing_signal", "generated_at": now, "series": series}
        if output.exists():
            prior = json.loads(output.read_text())
            if prior.get("source_project") != feed["source_project"]:
                raise ValueError("refuse to overwrite another source feed")
            if prior.get("series") == series:
                return prior
            old_ids = {encoded(s["strategy"]) for s in prior["series"]}
            if not old_ids <= {encoded(s["strategy"]) for s in series}:
                raise ValueError("refuse to drop previously published account identities")
            if timestamp(prior["generated_at"]) >= timestamp(now):
                raise ValueError("publication timestamp must advance")
        archive = output.parent/"revisions"/(digest(feed)+".json")
        if archive.exists():
            if json.loads(archive.read_text()) != feed:
                raise ValueError("immutable publication conflict")
        else:
            atomic_json(archive, feed)
        atomic_json(output, feed)
        return feed


def export_feed(db, output, *, now, synthetic=False):
    status_path = Path(db).with_suffix(".publish-status.json")
    try:
        result = _export_feed(db, output, now=now, synthetic=synthetic)
    except Exception as exc:
        atomic_json(status_path, {"attempted_at": now, "status": "failed", "error": str(exc)})
        raise
    atomic_json(status_path, {"attempted_at": now, "status": "ok", "generated_at": result["generated_at"]})
    return result


def _capture_signal(root, strategy_id, mapping, output, *, now):
    """Observe a genuine current CSV now; never infer historical available_at.

    The caller must run this before the next execution. It is an opt-in sidecar,
    not a modification to the production signal runner.
    """
    root = Path(root)
    native = native_registry(root)
    if strategy_id not in native or mapping not in native[strategy_id]["mappings"]:
        raise ValueError("unknown native signal/mapping")
    state_path = root/"logs/daily_signals_status.json"
    before = state_path.read_bytes()
    state = json.loads(before)
    if state.get("result") != "OK" or state.get("topup") not in ("OFFICE_OK", "OFFICE_OK_WARN", "OFFICE_ACCEPTED"):
        raise ValueError("production input status is not complete; no signal snapshot")
    if timestamp(state["finished_at"]) > timestamp(now):
        raise ValueError("production completion is in the future")
    rel = f"output/recommended/{strategy_id}_{mapping}.csv"
    path = root/rel
    raw = path.read_bytes()
    rows = list(csv.DictReader(raw.decode().splitlines()))
    if not rows:
        raise ValueError("empty recommended position file")
    last = rows[-1]
    if last["date"] != timestamp(state["finished_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ValueError("production date does not match completed session")
    evidence = [v for v in state.get("files", {}).values() if v.get("path") == rel and v.get("last_date") == last["date"]]
    if not evidence or before != state_path.read_bytes() or raw != path.read_bytes():
        raise ValueError("missing status evidence or changing production snapshot; retry")
    direction = number(float(last["position"]), "position")
    if direction not in (-1, 0, 1) or (mapping == "longflat" and direction < 0):
        raise ValueError("invalid native direction")
    snapshot = {"strategy_id": strategy_id, "mapping": mapping, "trade_date": last["date"],
                "direction": direction, "available_at": now, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "synthetic": False}
    output = Path(output)
    if output.exists():
        prior = json.loads(output.read_text())
        if {k:v for k,v in prior.items() if k != "available_at"} == {k:v for k,v in snapshot.items() if k != "available_at"}:
            return prior
        raise ValueError("signal observation is immutable; choose a new revision path")
    try:
        atomic_json(output, snapshot, replace=False)
    except FileExistsError:
        # Another shared-lock observer may have won the create race.
        prior = json.loads(output.read_text())
        if {k:v for k,v in prior.items() if k != "available_at"} != {k:v for k,v in snapshot.items() if k != "available_at"}:
            raise ValueError("signal observation conflict; choose a new revision path") from None
        return prior
    return snapshot


def capture_signal(root, strategy_id, mapping, output, *, now):
    # The existing runner uses this exclusive flock while rewriting its CSVs.
    # Read-only shared locking prevents observing a half-completed production run.
    lock_path = Path(root)/"logs/.daily_signals.lock"
    with lock_path.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("production run in progress; retry signal observation later") from None
        return _capture_signal(root, strategy_id, mapping, output, now=now)
