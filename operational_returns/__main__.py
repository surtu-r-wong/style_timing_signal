"""Opt-in local CLI. No scheduled task, provider, order or notification integration."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .account import (ROOT, account_state, advance, baseline, capture_signal,
                      export_feed, extend_calendar, initialize, native_registry,
                      validate_config)


def read(path):
    return json.loads(Path(path).read_text())


def demo(fixture, output_dir):
    """Historical clock injection is confined to explicitly synthetic fixtures."""
    fixture = read(fixture)
    cfg = fixture["config"]
    if cfg.get("synthetic") is not True:
        raise ValueError("demo requires synthetic config")
    root = Path(output_dir).resolve()
    if root.name != "synthetic":
        raise ValueError("demo output directory must be named synthetic")
    db = root/"accounts.sqlite3"
    initialize(db, cfg, now=fixture["initialized_at"])
    # Repeat demo on an already completed state without trying to reset its baseline.
    if account_state(db, cfg["account_id"])["as_of"] is None:
        baseline(db, cfg["account_id"], fixture["baseline_date"], now=fixture["baseline_available_at"])
    ledger = []
    for item in fixture["events"]:
        ledger.append(advance(db, cfg["account_id"], item["input"], now=item["observed_at"]))
    duplicate = advance(db, cfg["account_id"], fixture["events"][-1]["input"], now=fixture["published_at"])
    if duplicate != ledger[-1]:
        raise ValueError("idempotency check failed")
    feed = export_feed(db, root/"return-feed.synthetic.json", now=fixture["published_at"], synthetic=True)
    return {"synthetic": True, "database": str(db), "ledger": ledger,
            "duplicate_unchanged": True, "feed": str(root/"return-feed.synthetic.json"),
            "generated_at": feed["generated_at"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("registry", help="read all native signal and mapping declarations")
    p = sub.add_parser("validate-config"); p.add_argument("config")
    p = sub.add_parser("init"); p.add_argument("config"); p.add_argument("--db", required=True)
    p = sub.add_parser("baseline"); p.add_argument("account_id"); p.add_argument("trade_date"); p.add_argument("--db", required=True)
    p = sub.add_parser("advance"); p.add_argument("account_id"); p.add_argument("event"); p.add_argument("--db", required=True); p.add_argument("--revise", action="store_true")
    p = sub.add_parser("extend-calendar"); p.add_argument("account_id"); p.add_argument("calendar"); p.add_argument("--db", required=True)
    p = sub.add_parser("state"); p.add_argument("account_id"); p.add_argument("--db", required=True)
    p = sub.add_parser("export"); p.add_argument("--db", required=True); p.add_argument("--output", required=True); p.add_argument("--synthetic", action="store_true")
    p = sub.add_parser("capture-signal"); p.add_argument("strategy_id"); p.add_argument("mapping"); p.add_argument("--root", default=str(ROOT)); p.add_argument("--output", required=True)
    p = sub.add_parser("demo"); p.add_argument("--fixture", default=str(ROOT/"examples/operational_returns/synthetic.json")); p.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    try:
        if args.command == "registry":
            result = native_registry()
        elif args.command == "validate-config":
            validate_config(read(args.config)); result = {"valid": True}
        elif args.command == "init":
            result = initialize(args.db, read(args.config), now=now)
        elif args.command == "baseline":
            result = baseline(args.db, args.account_id, args.trade_date, now=now)
        elif args.command == "advance":
            result = advance(args.db, args.account_id, read(args.event), now=now, revise=args.revise)
        elif args.command == "extend-calendar":
            result = extend_calendar(args.db, args.account_id, read(args.calendar), now=now)
        elif args.command == "state":
            result = account_state(args.db, args.account_id)
        elif args.command == "export":
            result = export_feed(args.db, args.output, now=now, synthetic=args.synthetic)
        elif args.command == "capture-signal":
            result = capture_signal(args.root, args.strategy_id, args.mapping, args.output, now=now)
        else:
            result = demo(args.fixture, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"operational_returns: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
