"""Offline account acceptance; prices, capital and rates below are SYNTHETIC."""
import copy
import json
from pathlib import Path

import pytest

from operational_returns.account import initialize, baseline, advance, export_feed, account_state


def config(kind="cash_security"):
    return {
        "account_id": "SYNTHETIC-demo", "synthetic": True, "mode": "paper",
        "strategy_id": "equal_weight", "strategy_version": "test-20-40-5",
        "mapping": "symmetric", "currency": "CNY", "initial_cash": 1000,
        "instrument": {"symbol": "SYNTHETIC", "kind": kind, "currency": "CNY",
                       "multiplier": 1, "lot_size": 1, "margin_rate": 0.2 if kind == "linear_future" else None,
                       "settlement_rule": "daily_variation_margin" if kind == "linear_future" else "cash_trade_date"},
        "execution": {"rule": "next_session_fixed_time", "rebalance": "each_session",
                      "gross_leverage": 1, "slippage_bps": 0, "shorting": "allowed",
                      "short_proceeds": "available", "short_collateral_ratio": 1},
        "fees": {"commission_bps": 10, "sell_tax_bps": 0, "per_unit": 0,
                 "description": "SYNTHETIC: 10bp commission, explicit zero tax/per-unit; no other fees"},
        "cash_rules": {"credit_rate": 0, "debit_rate": 0, "short_borrow_rate": 0,
                       "day_count": "ACT/365", "external_flows": "reject"},
        "calendar": {"id": "SYNTHETIC-CN", "timezone": "Asia/Shanghai",
                     "source": "SYNTHETIC complete test calendar",
                     "coverage_start": "2026-10-12", "coverage_end": "2026-10-14",
                     "days": [{"date": f"2026-10-{d}", "is_open": True,
                               "execution_at": f"2026-10-{d}T09:30:00+08:00",
                               "valuation_at": f"2026-10-{d}T15:00:00+08:00"} for d in (12, 13, 14)]}}


def event(day=13, direction=1, execution=100, close=110):
    return {"trade_date": f"2026-10-{day}", "symbol": "SYNTHETIC",
            "execution_price": execution, "valuation_price": close,
            "available_at": f"2026-10-{day}T16:00:00+08:00",
            "source": "SYNTHETIC price fixture", "executable": True,
            "external_flow": 0, "corporate_actions": [],
            "signal": {"strategy_id": "equal_weight", "mapping": "symmetric",
                       "trade_date": f"2026-10-{day-1}", "direction": direction,
                       "available_at": f"2026-10-{day-1}T20:31:00+08:00",
                       "source_sha256": "a" * 64, "synthetic": True}}


def ready(tmp_path, cfg=None):
    db = tmp_path / "accounts.sqlite3"
    cfg = cfg or config()
    initialize(db, cfg, now="2026-10-12T14:59:00+08:00")
    baseline(db, cfg["account_id"], "2026-10-12", now="2026-10-12T16:00:00+08:00")
    return db


def test_two_intervals_cash_nav_cost_and_idempotency(tmp_path):
    db = ready(tmp_path)
    first = advance(db, "SYNTHETIC-demo", event(), now="2026-10-13T16:01:00+08:00")
    # Buy 10 at 100, pay 1 fee, mark 110: cash=-1, assets=1100, NAV=1099.
    assert first["cash"] == pytest.approx(-1)
    assert first["quantity"] == 10
    assert first["nav"] == pytest.approx(1099)
    assert first["daily_return"] == pytest.approx(.099)
    assert first["gross_pnl"] == pytest.approx(100)
    assert first["fees"] == pytest.approx(1)
    second_event = event(14, 0, 120, 125)
    second = advance(db, "SYNTHETIC-demo", second_event, now="2026-10-14T16:01:00+08:00")
    # Sell 10 at 120, pay 1.2; all cash=1197.8; second NAV gain=98.8.
    assert second["cash"] == pytest.approx(1197.8)
    assert second["nav"] == pytest.approx(1197.8)
    assert second["daily_return"] == pytest.approx(1197.8/1099-1)
    assert second["period_start_at"] == first["period_end_at"]
    assert second["gross_ratio"] == 0
    assert second["margin_ratio"] is None
    assert advance(db, "SYNTHETIC-demo", second_event, now="2026-10-14T17:00:00+08:00") == second
    assert account_state(db, "SYNTHETIC-demo")["nav"] == pytest.approx(1197.8)


def test_short_leverage_once_and_future_settlement(tmp_path):
    cfg = config("linear_future")
    cfg["execution"]["gross_leverage"] = 2
    db = ready(tmp_path, cfg)
    row = advance(db, cfg["account_id"], event(direction=-1, close=90), now="2026-10-13T16:01:00+08:00")
    # Short notional 2000, 20 contracts; 200 gain minus 2 commission.
    assert row["quantity"] == -20
    assert row["cash"] == pytest.approx(1198)
    assert row["nav"] == pytest.approx(1198)
    assert row["daily_return"] == pytest.approx(.198)
    assert row["gross_ratio"] == pytest.approx(1800/1198)
    assert row["margin_ratio"] == pytest.approx(360/1198)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(valuation_price=None),
    lambda e: e["signal"].update(available_at="2026-10-13T09:30:00+08:00"),
    lambda e: e["signal"].update(trade_date="2026-10-11"),
    lambda e: e.update(external_flow=1),
    lambda e: e.update(corporate_actions=["split"]),
    lambda e: e.update(executable=False),
])
def test_invalid_input_keeps_success_state(tmp_path, mutation):
    db = ready(tmp_path)
    before = account_state(db, "SYNTHETIC-demo")
    e = event(); mutation(e)
    with pytest.raises(ValueError):
        advance(db, "SYNTHETIC-demo", e, now="2026-10-13T16:01:00+08:00")
    assert account_state(db, "SYNTHETIC-demo") == before
    assert json.loads(db.with_suffix(".status.json").read_text())["status"] == "failed"
    assert advance(db, "SYNTHETIC-demo", event(), now="2026-10-13T16:02:00+08:00")["nav"] == 1099


def test_skip_date_config_drift_and_revision(tmp_path):
    db = ready(tmp_path)
    with pytest.raises(ValueError, match="next"):
        advance(db, "SYNTHETIC-demo", event(14), now="2026-10-14T16:01:00+08:00")
    cfg = config(); cfg["execution"]["gross_leverage"] = 2
    with pytest.raises(ValueError, match="config"):
        initialize(db, cfg, now="2026-10-13T16:00:00+08:00")
    advance(db, "SYNTHETIC-demo", event(), now="2026-10-13T16:01:00+08:00")
    revised = event(close=111)
    with pytest.raises(ValueError, match="revise"):
        advance(db, "SYNTHETIC-demo", revised, now="2026-10-13T17:00:00+08:00")
    row = advance(db, "SYNTHETIC-demo", revised, now="2026-10-13T17:01:00+08:00", revise=True)
    assert row["revision"] == 2
    assert row["nav"] == pytest.approx(1109)


def test_export_schema_synthetic_guard_and_unchanged_timestamp(tmp_path):
    db = ready(tmp_path)
    out = tmp_path / "synthetic" / "feed.json"
    with pytest.raises(ValueError, match="complete"):
        export_feed(db, out, now="2026-10-12T16:01:00+08:00", synthetic=True)
    advance(db, "SYNTHETIC-demo", event(), now="2026-10-13T16:01:00+08:00")
    advance(db, "SYNTHETIC-demo", event(14,0,120,125), now="2026-10-14T16:01:00+08:00")
    with pytest.raises(ValueError, match="synthetic"):
        export_feed(db, out, now="2026-10-14T16:02:00+08:00")
    export_feed(db, out, now="2026-10-14T16:02:00+08:00", synthetic=True)
    raw = out.read_bytes()
    feed = json.loads(raw)
    assert feed["series"][0]["inception_date"] == "2026-10-13"
    assert feed["series"][0]["rows"][1]["unit_nav"] == pytest.approx(1.1978)
    schema = json.loads((Path(__file__).parents[1]/"docs/requests/2026-10-14-multi-asset-operational-returns/return-feed.schema.json").read_text())
    import jsonschema
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(feed)
    export_feed(db, out, now="2026-10-14T17:00:00+08:00", synthetic=True)
    assert out.read_bytes() == raw
    assert len(list((out.parent/"revisions").glob("*.json"))) == 1


def test_financing_and_security_borrow_independent_formula(tmp_path):
    cfg = config(); cfg["execution"]["gross_leverage"] = 2
    cfg["cash_rules"]["debit_rate"] = .365
    db = ready(tmp_path/"long", cfg)
    row = advance(db, cfg["account_id"], event(), now="2026-10-13T16:01:00+08:00")
    interest = -1002 * .365 * (5.5/24) / 365
    assert row["quantity"] == 20
    assert row["interest"] == pytest.approx(interest)
    assert row["nav"] == pytest.approx(1200-2+interest)
    cfg = config(); cfg["cash_rules"]["short_borrow_rate"] = .365
    db = ready(tmp_path/"short", cfg)
    row = advance(db, cfg["account_id"], event(direction=-1, close=90), now="2026-10-13T16:01:00+08:00")
    borrow = 1000 * .365 * (5.5/24) / 365
    assert row["cash"] == pytest.approx(1999-borrow)
    assert row["nav"] == pytest.approx(1099-borrow)


def test_futures_multiplier_roll_rejection_and_margin(tmp_path):
    cfg = config("linear_future")
    cfg["instrument"].update(multiplier=5, lot_size=2)
    cfg["execution"]["gross_leverage"] = 2
    db = ready(tmp_path, cfg)
    row = advance(db, cfg["account_id"], event(direction=-1, close=90), now="2026-10-13T16:01:00+08:00")
    assert row["quantity"] == -4
    assert row["nav"] == 1198
    e = event(14,0,80,85)
    e["symbol"] = "OTHER_CONTRACT"
    with pytest.raises(ValueError, match="instrument"):
        advance(db, cfg["account_id"], e, now="2026-10-14T16:01:00+08:00")
    row = advance(db, cfg["account_id"], event(14,0,80,85), now="2026-10-14T16:01:00+08:00")
    assert row["nav"] == pytest.approx(1396.4)  # +200 prior short MTM -1.6 close fee
    assert row["quantity"] == 0
    cfg["execution"]["gross_leverage"] = 10
    other = ready(tmp_path/"margin",cfg)
    with pytest.raises(ValueError, match="margin"):
        advance(other,cfg["account_id"],event(close=100),now="2026-10-13T16:01:00+08:00")


def test_transaction_rollback_and_publication_recovery(tmp_path, monkeypatch):
    import operational_returns.account as api
    import sqlite3
    db = ready(tmp_path)
    before = account_state(db,"SYNTHETIC-demo")
    # Abort between inserting a revision and updating account state.
    with sqlite3.connect(db) as con:
        con.execute("CREATE TRIGGER simulated_disk_error BEFORE UPDATE ON accounts BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        advance(db,"SYNTHETIC-demo",event(),now="2026-10-13T16:01:00+08:00")
    assert account_state(db,"SYNTHETIC-demo") == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        con.execute("DROP TRIGGER simulated_disk_error")
    advance(db,"SYNTHETIC-demo",event(),now="2026-10-13T16:02:00+08:00")
    out=tmp_path/"synthetic"/"feed.json"
    export_feed(db,out,now="2026-10-13T16:03:00+08:00",synthetic=True)
    old=out.read_bytes()
    advance(db,"SYNTHETIC-demo",event(14,0,120,125),now="2026-10-14T16:01:00+08:00")
    original=api.atomic_json
    def fail_latest(path,value):
        if Path(path)==out:
            raise OSError("injected replace failure")
        return original(path,value)
    monkeypatch.setattr(api,"atomic_json",fail_latest)
    with pytest.raises(OSError):
        export_feed(db,out,now="2026-10-14T16:02:00+08:00",synthetic=True)
    assert out.read_bytes()==old
    assert json.loads(db.with_suffix(".publish-status.json").read_text())["status"]=="failed"
    monkeypatch.setattr(api,"atomic_json",original)
    feed=export_feed(db,out,now="2026-10-14T16:02:00+08:00",synthetic=True)
    assert len(feed["series"][0]["rows"])==2


def test_calendar_extension_identity_and_no_inferred_holidays(tmp_path):
    from operational_returns.account import extend_calendar, validate_config
    cfg=config()
    broken=copy.deepcopy(cfg); broken["calendar"]["days"].pop(1)
    with pytest.raises(ValueError,match="completely"):
        validate_config(broken)
    db=ready(tmp_path,cfg)
    advance(db,cfg["account_id"],event(),now="2026-10-13T16:01:00+08:00")
    out=tmp_path/"synthetic"/"feed.json"
    first=export_feed(db,out,now="2026-10-13T16:02:00+08:00",synthetic=True)
    calendar=copy.deepcopy(cfg["calendar"])
    calendar["coverage_end"]="2026-10-15"
    calendar["days"].append({"date":"2026-10-15","is_open":False})
    extend_calendar(db,cfg["account_id"],calendar,now="2026-10-13T17:00:00+08:00")
    again=export_feed(db,out,now="2026-10-13T17:01:00+08:00",synthetic=True)
    assert again==first  # Coverage-only extension does not rewrite published identity/time.
    calendar["days"][0]["is_open"]=False
    with pytest.raises(ValueError,match="prefix"):
        extend_calendar(db,cfg["account_id"],calendar,now="2026-10-13T18:00:00+08:00")


def test_native_registry_dynamic_and_capture_never_backdates(tmp_path):
    from operational_returns.account import capture_signal, native_registry
    import fcntl
    root=tmp_path/"root"
    (root/"backtest").mkdir(parents=True)
    (root/"backtest/baseline.py").write_text("SIGNALS={'new_native':('output/new.csv','value')}\n")
    (root/"backtest/production.py").write_text("PRODUCTION_MAPPING={'new_native':'longflat'}\nREFERENCE_OUTPUTS={}\nPOOLS={}\n")
    assert set(native_registry(root))=={"new_native"}
    (root/"logs").mkdir()
    (root/"logs/.daily_signals.lock").touch()
    (root/"output/recommended").mkdir(parents=True)
    rel="output/recommended/new_native_longflat.csv"
    (root/rel).write_text("date,position\n2026-10-13,1\n")
    state={"result":"OK","topup":"OFFICE_LATE","finished_at":"2026-10-13T21:30:00+08:00",
           "files":{"item":{"path":rel,"last_date":"2026-10-13"}}}
    status=root/"logs/daily_signals_status.json"
    status.write_text(json.dumps(state))
    output=tmp_path/"observation.json"
    with pytest.raises(ValueError,match="not complete"):
        capture_signal(root,"new_native","longflat",output,now="2026-10-13T21:31:00+08:00")
    state["topup"]="OFFICE_OK"; status.write_text(json.dumps(state))
    with (root/"logs/.daily_signals.lock").open("rb") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError,match="in progress"):
            capture_signal(root,"new_native","longflat",output,now="2026-10-13T21:31:00+08:00")
    first=capture_signal(root,"new_native","longflat",output,now="2026-10-13T21:31:00+08:00")
    assert first["available_at"]=="2026-10-13T21:31:00+08:00"
    assert capture_signal(root,"new_native","longflat",output,now="2026-10-13T22:00:00+08:00")==first


def test_initialization_no_backfill_and_mapping_identity(tmp_path):
    cfg=config()
    db=tmp_path/"state.sqlite3"
    initialize(db,cfg,now="2026-10-12T16:00:00+08:00")
    with pytest.raises(ValueError,match="first valuation"):
        baseline(db,cfg["account_id"],"2026-10-12",now="2026-10-12T16:01:00+08:00")
    baseline(db,cfg["account_id"],"2026-10-13",now="2026-10-13T16:00:00+08:00")
    advance(db,cfg["account_id"],event(14),now="2026-10-14T16:01:00+08:00")
    other=copy.deepcopy(cfg); other.update(account_id="SYNTHETIC-reference",mapping="longflat")
    initialize(db,other,now="2026-10-12T16:00:00+08:00")
    baseline(db,other["account_id"],"2026-10-13",now="2026-10-13T16:00:00+08:00")
    e=event(14); e["signal"]["mapping"]="longflat"
    advance(db,other["account_id"],e,now="2026-10-14T16:01:00+08:00")
    feed=export_feed(db,tmp_path/"synthetic"/"feed.json",now="2026-10-14T16:02:00+08:00",synthetic=True)
    assert len({s["strategy"]["version"] for s in feed["series"]})==2
    assert all(s["inception_date"]=="2026-10-14" for s in feed["series"])


def test_slippage_sell_tax_and_old_revision_refusal(tmp_path):
    cfg=config()
    cfg["execution"]["slippage_bps"]=100
    cfg["fees"]["sell_tax_bps"]=20
    db=ready(tmp_path,cfg)
    first=advance(db,cfg["account_id"],event(),now="2026-10-13T16:01:00+08:00")
    assert first["nav"]==pytest.approx(1088.99) # 10*110 -10*101 +1000 -1.01
    second=advance(db,cfg["account_id"],event(14,0,120,125),now="2026-10-14T16:01:00+08:00")
    assert second["nav"]==pytest.approx(-11.01+1188-1.188-2.376)
    with pytest.raises(ValueError,match="only latest"):
        advance(db,cfg["account_id"],event(close=111),now="2026-10-14T17:00:00+08:00",revise=True)


def test_future_interest_excludes_unsettled_pnl(tmp_path):
    cfg=config("linear_future")
    cfg["cash_rules"]["credit_rate"]=.365
    cfg["fees"]["commission_bps"]=0
    db=ready(tmp_path,cfg)
    first=advance(db,cfg["account_id"],event(close=100),now="2026-10-13T16:01:00+08:00")
    second=advance(db,cfg["account_id"],event(14,1,110,110),now="2026-10-14T16:01:00+08:00")
    # At 110, 10 old contracts remain 10 (lot rounding); overnight +100 is
    # still unrealized at execution and must not accrue cash interest.
    assert first["quantity"]==second["quantity"]==10
    overnight_interest=first["cash"]*.365*(18.5/24)/365
    settled=first["cash"]+overnight_interest
    assert second["settled_cash_at_execution"]==pytest.approx(settled)
    assert second["interest"]==pytest.approx(overnight_interest+settled*.365*(5.5/24)/365)
    assert second["nav"]==pytest.approx(first["nav"]+100+second["interest"])


@pytest.mark.parametrize("mutation", [
    lambda c:c.update(initial_cash=None),
    lambda c:c.update(mode="live"),
    lambda c:c["instrument"].update(kind="option"),
    lambda c:c["instrument"].update(currency="USD"),
    lambda c:c["fees"].update(commission_bps=None),
    lambda c:c["cash_rules"].update(external_flows="allow"),
])
def test_missing_parameters_and_unsupported_modes_rejected(tmp_path,mutation):
    cfg=config();mutation(cfg)
    with pytest.raises(ValueError):
        initialize(tmp_path/"unused.sqlite3",cfg,now="2026-10-12T14:59:00+08:00")
    assert not (tmp_path/"unused.sqlite3").exists()


def test_explicit_closed_session_and_full_elapsed_interval(tmp_path):
    cfg=config();cfg["calendar"]["days"][1]={"date":"2026-10-13","is_open":False}
    db=ready(tmp_path,cfg)
    e=event(14);e["signal"].update(trade_date="2026-10-12",available_at="2026-10-12T20:31:00+08:00")
    row=advance(db,cfg["account_id"],e,now="2026-10-14T16:01:00+08:00")
    assert row["period_start_at"]=="2026-10-12T15:00:00+08:00"
    assert row["period_end_at"]=="2026-10-14T15:00:00+08:00"


def test_revision_preserves_prior_input_and_publication(tmp_path):
    import sqlite3
    db=ready(tmp_path)
    advance(db,"SYNTHETIC-demo",event(),now="2026-10-13T16:01:00+08:00")
    out=tmp_path/"synthetic"/"feed.json"
    first=export_feed(db,out,now="2026-10-13T16:02:00+08:00",synthetic=True)
    advance(db,"SYNTHETIC-demo",event(close=111),now="2026-10-13T17:00:00+08:00",revise=True)
    revised=export_feed(db,out,now="2026-10-13T17:01:00+08:00",synthetic=True)
    assert first["series"][0]["rows"][0]["unit_nav"]==1.099
    assert revised["series"][0]["rows"][0]["unit_nav"]==1.109
    assert len(list((out.parent/"revisions").glob('*.json')))==2
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT count(*) FROM records').fetchone()[0]==2


def test_demo_cli_and_template_rejection(tmp_path):
    import subprocess,sys
    root=Path(__file__).parents[1]
    cmd=[sys.executable,'-m','operational_returns','demo','--output-dir',str(tmp_path/'synthetic')]
    a=subprocess.run(cmd,cwd=root,capture_output=True,text=True,check=True)
    b=subprocess.run(cmd,cwd=root,capture_output=True,text=True,check=True)
    assert json.loads(a.stdout)==json.loads(b.stdout)
    p=subprocess.run([sys.executable,'-m','operational_returns','validate-config',
                     'examples/operational_returns/account.template.json'],cwd=root,capture_output=True,text=True)
    assert p.returncode==1
