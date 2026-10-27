"""Offline consumer audit of the three frozen A deliveries; no DB or API access.

This verifies delivery integrity and recomputes coverage, not official date/weight
semantics or investment returns. Run under the workspace's 2 GB memory limit.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
ANCHORS = {
    "office_20261023_a_v1": "e82a8ecb727b537b7390e88e3a7ddcc1b6f4277b323d6d69f2f6d01ab68ec412",
    "office_20261023_a_v2": "ec6a4f261a383ff5a74b986bc826149e8849b11290f4c680bb6ae6bbb140488a",
    "office_20261024_a_v3": "2a502793d00eaa1b814398c8b9f54052bebda784d3b7105934b5dfccba19f6ee",
}
LEGS = ("932409.CSI", "932408.CSI", "932403.CSI", "932402.CSI")
MOTHERS = ("932000.CSI", "000905.SH")
KEY = ("ts_code", "trade_date")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rows(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def keyed(records, fields):
    result = {tuple(row[k] for k in fields): row for row in records}
    require(len(result) == len(records), f"Duplicate key: {fields}")
    return result


def flag(value):
    require(value in ("True", "False"), f"Unknown boolean: {value!r}")
    return value == "True"


def write_csv(path, records):
    require(bool(records), f"No rows to write: {path.name}")
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def audit_package(pack, anchor):
    require(sha(pack / "manifest.json") == anchor, f"Manifest mismatch: {pack.name}")
    manifest = read_json(pack / "manifest.json")
    names = [item["path"] for item in manifest["files"]]
    require(len(names) == len(set(names)) == manifest["file_count"], "Manifest file count")
    actual = {str(p.relative_to(pack)) for p in pack.rglob("*") if p.is_file()}
    require(actual == set(names) | {"manifest.json"}, f"Unlisted/missing files: {pack.name}")
    for item in manifest["files"]:
        p = pack / item["path"]
        require(p.resolve().is_relative_to(pack.resolve()) and not p.is_symlink(), "Unsafe input path")
        require(p.stat().st_size == item["bytes"] and sha(p) == item["sha256"], f"Changed: {p}")
    require(sum(x["bytes"] for x in manifest["files"]) == manifest["total_bytes"], "Byte total")
    for p in (pack, *pack.rglob("*")):
        require(p.stat().st_mode & 0o222 == 0, f"Delivery is writable: {p}")
    metadata = read_json(pack / "csv-metadata.json")
    checked_keys = 0
    for name, expected in metadata.items():
        with (pack / name).open(encoding=expected.get("encoding", "utf-8"), newline="") as f:
            reader = csv.DictReader(f)
            require(reader.fieldnames == expected["columns"], f"Columns: {name}")
            empty, seen, count, duplicates = Counter(), set(), 0, 0
            for row in reader:
                require(None not in row, f"Extra CSV cells: {name}")
                count += 1
                empty.update(k for k, v in row.items() if v in (None, ""))
                if expected["unique_key"]:
                    key = tuple(row[k] for k in expected["unique_key"])
                    duplicates += key in seen
                    seen.add(key)
        require(count == expected["rows"], f"Rows: {name}")
        nulls = expected.get("nulls", expected.get("empty_cells", expected.get("null_cells")))
        if nulls is not None:  # Column dictionaries declare rows/keys but no NULL statistics.
            require(dict(empty) == {k: v for k, v in nulls.items() if v}, f"NULL counts: {name}")
        if expected["unique_key"]:
            require(duplicates == expected["duplicate_keys"], f"Duplicates: {name}")
            checked_keys += 1
    return {"version": pack.name, "manifest_sha256": anchor, "files": len(names),
            "bytes": manifest["total_bytes"], "csv_tables": len(metadata),
            "csv_declared_keys_checked": checked_keys, "readonly": True}


def audit_wset(v3):
    observed = rows(v3 / "constituents-observed.csv")
    keyed(observed, ("index_code", "query_date", "ts_code"))
    groups = defaultdict(dict)
    for row in observed:
        groups[(row["index_code"], row["query_date"])][row["ts_code"]] = row
        require(all(row[k] == "" for k in ("official_effective_date", "official_announcement_date",
                                             "weight_reference_date")), "Unknown date filled")
        require(row["source_date_semantics"] == "UNVERIFIED", "Source date certified unexpectedly")
    summaries = rows(v3 / "snapshot-summary.csv")
    calls = keyed(read_json(v3 / "source-calls.json"), ("task_id",))
    require(len({x["call_id"] for x in calls.values()}) == len(calls) == 15, "Source call count")
    require(set(groups) == {(s["index_code"], s["query_date"]) for s in summaries}, "Snapshot scope")
    cells = 0
    for summary in summaries:
        path = v3 / "evidence/raw" / summary["task_id"] / "response.json"
        raw = json.loads(path.read_text(), parse_float=Decimal)
        require(raw["status"] == "ok", "Source status")
        require(raw["columns"] == ["date", "wind_code", "sec_name", "i_weight", "industry"], "SDK columns")
        records = keyed([dict(zip(raw["columns"], r, strict=True)) for r in raw["rows"]], ("wind_code",))
        group = groups[(summary["index_code"], summary["query_date"])]
        require({k[0] for k in records} == set(group), "Raw/normalized membership")
        call = calls[(summary["task_id"],)]
        response_sha = sha(path)
        require(response_sha == summary["response_sha256"] == call["response_sha256"], "Response hash")
        require(len(records) == int(summary["rows"]), "Snapshot row count")
        count = len(records) * len(raw["columns"])
        require(count == int(summary["returned_cells"]) == call["returned_units"]["amount"], "SDK cells")
        require(call["returned_units"]["rows"] == len(records)
                and call["returned_units"]["columns"] == 5, "SDK shape")
        require(call["call_id"] == summary["source_call_id"], "Source UUID")
        cells += count
        for key, original in records.items():
            row = group[key[0]]
            for dest, source in (("source_date_raw", "date"), ("sec_name_original", "sec_name"),
                                 ("industry_original", "industry")):
                require(row[dest] == str(original[source]), f"Raw field: {dest}")
            value = original["i_weight"]
            require((row["i_weight_original"] == "") if value is None else
                    Decimal(row["i_weight_original"]) == Decimal(str(value)), "Raw weight changed")
            require(row["source_call_id"] == call["call_id"] and row["response_sha256"] == response_sha,
                    "Row source binding")
        require(sum(r["i_weight_original"] == "" for r in group.values()) == int(summary["null_weights"]),
                "Snapshot NULL weights")
        weight_sum = sum((Decimal(r["i_weight_original"]) for r in group.values() if r["i_weight_original"]), Decimal(0))
        require(weight_sum == Decimal(summary["nonnull_weight_sum"]), "Weight sum")
        dates = Counter(r["source_date_raw"][:10] for r in group.values())
        require(dates == json.loads(summary["source_date_counts"]), "Mixed source dates")
    require(len(observed) == 5326 and cells == 26630, "WSET totals")
    require(sum(r["i_weight_original"] == "" for r in observed) == 501, "WSET NULL total")
    return groups, {"rows_compared_to_raw": len(observed), "null_weights": 501,
                    "archived_calls": len(calls), "archived_sdk_cells": cells}


def audit_snapshots(v1, calendar):
    result = []
    for row in rows(v1 / "coverage/original-indicator-snapshot.csv"):
        mode = "all" if flag(row["explicit_codes"]) else "shsz"
        asof = row["asof"]
        counts = rows(v1 / f"coverage/indicator-counts-{asof}-{mode}.csv")
        lower = (date.fromisoformat(asof) - timedelta(days=400)).isoformat()
        counts = [r for r in counts if lower < r["trade_date"] <= asof]
        peak = max(int(r["n"]) for r in counts)
        selected = max(r["trade_date"] for r in counts if int(r["n"]) >= peak / 2)
        raw = keyed(rows(v1 / f"raw/indicator-{asof}-{mode}.csv"), ("ts_code",))
        targets = rows(v1 / f"coverage/indicator-target-{row['effective_date']}-{row['index_code']}-{asof}-{mode}.csv")
        require(selected == row["snapshot_date"] and peak == int(row["peak_rows"]), "Snapshot selection")
        require(len(raw) == int(row["selected_rows"]), "Snapshot global rows")
        require(all(r["trade_date"] == selected and r["total_mv"] for r in raw.values()), "Snapshot values")
        for target in targets:
            source = raw.get((target["ts_code"],))
            require(flag(target["matched"]) == (source is not None), "Target match")
            if source:
                require(all(target[k] == source[k] for k in ("total_mv", "circ_mv")), "Target values")
        matched = sum(flag(t["matched"]) for t in targets)
        require((len(targets), matched, len(targets) - matched) ==
                tuple(int(row[k]) for k in ("list_rows", "matched", "missing")), "Target coverage")
        require(int(row["age_calendar_to_asof"]) == (date.fromisoformat(asof) - date.fromisoformat(selected)).days,
                "Snapshot calendar age")
        require(int(row["age_trading_to_asof"]) == sum(selected < day <= asof for day in calendar), "Trading age")
        result.append(row)
    return result


def audit_windows(v1, v2, v3, groups, out):
    calendar = sorted(r["calendar_date"] for r in rows(v1 / "raw/calendar.csv") if flag(r["is_business_day"]))
    snapshots = audit_snapshots(v1, calendar)
    write_csv(out / "indicator-snapshot-check.csv", snapshots)
    legacy = defaultdict(dict)
    for r in rows(v1 / "raw/index-constituents.csv"):
        legacy[(r["index_code"], r["effective_date"])][r["ts_code"]] = r
    def basket(code, day):
        return groups.get((code, day), legacy.get((code, day), {}))
    differences = keyed(rows(v3 / "basket-difference-summary.csv"), ("effective_window", "index_code"))
    changes = keyed(rows(v3 / "basket-changes.csv"), ("effective_window", "index_code", "ts_code"))
    union_rows = rows(v3 / "window-universe-comparison.csv")
    conflicts = rows(v2 / "status-conflicts.csv")
    summary, anomalies, whole_legs, weights, mother_diffs = [], [], [], [], []
    expected_changes, expected_conflicts = set(), set()
    added_500_nulls = 0
    for window in read_json(v1 / "coverage/windows.json"):
        effective, days = window["effective_date"], window["dates"]
        i = calendar.index(effective)
        require(days == calendar[i - 5:i + 6] and len(days) == 11, "Trading window")
        old_day = calendar[i - 1]
        union, leg_sets = set(), {}
        for code in MOTHERS:
            current = set(basket(code, effective))
            original = {r["ts_code"] for r in rows(v1 / "coverage/mother-security-periods.csv")
                        if r["effective_date"] == effective and r["index_code"] == code}
            require(len(current) == (2000 if code == "932000.CSI" else 500), "Mother size")
            union |= current
            mother_diffs.append({"effective_window": effective, "index_code": code,
                                 "original_candidates": len(original), "observed_members": len(current),
                                 "added": len(current - original), "removed": len(original - current)})
        for code in LEGS:
            old, new = set(basket(code, old_day)), set(basket(code, effective))
            require(bool(old) and bool(new), "Missing leg membership")
            d = differences[(effective, code)]
            require(d["old_query_date"] == old_day and d["new_query_date"] == effective, "Basket query date")
            for key, value in {"old_count": len(old), "new_count": len(new), "added": len(new - old),
                               "removed": len(old - new), "retained": len(new & old)}.items():
                require(int(d[key]) == value, f"Basket difference: {key}")
            for role, day, codes in (("old", old_day, old), ("new", effective, new)):
                members = basket(code, day)
                field = "i_weight_original" if (code, day) in groups else "weight"
                nulls = sum(members[c][field] == "" for c in codes)
                weights.append({"effective_window": effective, "index_code": code, "role": role,
                                "query_date": day, "source": "v3_raw_wset" if field == "i_weight_original" else "v1_stored",
                                "members": len(codes), "null_weights": nulls,
                                "nonnull_weight_sum_unscaled": str(sum((Decimal(members[c][field]) for c in codes
                                                                         if members[c][field]), Decimal(0))),
                                "official_weight_dates_verified": False})
                leg_sets[(code, role)] = codes
            for kind, codes in (("ADDED", new - old), ("REMOVED", old - new)):
                for security in codes:
                    key = (effective, code, security)
                    expected_changes.add(key)
                    require(changes[key]["change"] == kind and changes[key]["official_effective_date"] == "", "Change row")
                    if kind == "ADDED" and code in ("932403.CSI", "932402.CSI"):
                        require(basket(code, effective)[security]["i_weight_original"] == "", "500 added weight")
                        require(flag(changes[key]["new_source_weight_is_null"]), "500 missing-weight flag")
                        added_500_nulls += 1
            union |= old | new
        original_union = {r["ts_code"] for r in rows(v1 / f"coverage/known-union-{effective}.csv")}
        comparison = {r["ts_code"]: r for r in union_rows if r["effective_window"] == effective}
        require(set(comparison) == original_union | union, "Union comparison keys")
        for code, row in comparison.items():
            require((flag(row["in_v1"]), flag(row["in_updated_known_union"]), flag(row["additional_to_v1"])) ==
                    (code in original_union, code in union, code in union - original_union), "Union flags")
        base_prices = keyed(rows(v1 / f"raw/prices-{effective}.csv"), KEY)
        base_status = keyed(rows(v1 / f"raw/status-{effective}.csv"), KEY)
        extra_prices = keyed(rows(v3 / f"extra-prices-{effective}.csv"), KEY)
        extra_status = keyed(rows(v3 / f"extra-status-{effective}.csv"), KEY)
        expected_extra = {(code, day) for code in union - original_union for day in days}
        require(set(extra_prices) == set(extra_status) == expected_extra, "Extra price/status grid")
        prices = keyed(list(base_prices.values()) + list(extra_prices.values()), KEY)
        statuses = keyed(list(base_status.values()) + list(extra_status.values()), KEY)
        suspensions = keyed(rows(v2 / f"suspensions-{effective}.csv") + rows(v3 / f"extra-suspensions-{effective}.csv"), KEY)
        v1_grid = keyed(rows(v1 / f"coverage/price-status-{effective}.csv"), KEY)
        v2_grid = keyed(rows(v2 / f"price-state-evidence-{effective}.csv"), KEY)
        require(set(v1_grid) == set(v2_grid) == {(c, d) for c in original_union for d in days}, "Base grid")
        for key, row in v2_grid.items():
            require(all(row[k] == v for k, v in v1_grid[key].items()), "v2 changed base evidence")
            require(flag(row["price_row_missing"]) == (key not in base_prices), "v2 price gap")
            require(flag(row["suspension_record_present"]) == (key in suspensions), "v2 suspension evidence")
            conflict = key in suspensions and not flag(base_status[key]["is_suspended"])
            require(flag(row["suspension_status_conflict"]) == conflict, "v2 status conflict")
            if conflict:
                expected_conflicts.add((effective, *key))
        stats = Counter()
        for code in sorted(union):
            for day in days:
                key = (code, day)
                p, s = prices.get(key), statuses.get(key)
                missing = p is None
                invalid = p is not None and any(not p[k] or not Decimal(p[k]).is_finite() or Decimal(p[k]) <= 0
                                                for k in ("close", "pre_close"))
                zero = p is not None and p["volume"] != "" and Decimal(p["volume"]) == 0
                suspended = key in suspensions
                conflict = suspended and s is not None and not flag(s["is_suspended"])
                values = {"price_missing": missing, "invalid_price": invalid, "zero_volume": zero,
                          "status_missing": s is None, "suspension_record_present": suspended,
                          "status_conflict": conflict, "missing_with_suspension": missing and suspended,
                          "missing_without_suspension": missing and not suspended}
                stats.update(k for k, v in values.items() if v)
                if missing or invalid or zero or suspended or conflict or s is None:
                    anomalies.append({"effective_window": effective, "ts_code": code, "trade_date": day, **values,
                                      "corporate_action_validation": "pending", "return_imputed": False})
        for (code, role), members in leg_sets.items():
            for day in days:
                present = sum((c, day) in prices for c in members)
                whole_legs.append({"effective_window": effective, "index_code": code, "role": role,
                                   "trade_date": day, "members": len(members), "price_rows": present,
                                   "missing_prices": len(members) - present, "whole_leg_no_price": present == 0})
        index_rows = keyed(rows(v1 / f"raw/index-prices-{effective}.csv"), ("index_code", "trade_date"))
        require(set(index_rows) == {(c, d) for c in MOTHERS + LEGS for d in days}, "Index price grid")
        require(all(Decimal(r["close"]) > 0 for r in index_rows.values()), "Index invalid close")
        summary.append({"effective_window": effective, "dates": days, "v1_union": len(original_union),
                        "updated_union": len(union), "added_to_v1": len(union - original_union),
                        "removed_from_v1": len(original_union - union), "expected_security_days": len(union) * len(days),
                        "price_rows": len(union) * len(days) - stats["price_missing"],
                        "extra_price_rows": len(extra_prices), "index_price_rows": len(index_rows), **dict(stats)})
    require(set(changes) == expected_changes and added_500_nulls == 218, "Changes/500 added NULL total")
    require({(r["effective_date"], r["ts_code"], r["trade_date"]) for r in conflicts} == expected_conflicts, "Conflict keys")
    write_csv(out / "price-state-anomalies.csv", anomalies)
    write_csv(out / "whole-leg-days.csv", whole_legs)
    write_csv(out / "weight-completeness.csv", weights)
    write_csv(out / "mother-observation-differences.csv", mother_diffs)
    return {"windows": summary, "old_v2_status_conflicts": len(expected_conflicts),
            "new_500_added_null_weights": added_500_nulls,
            "whole_leg_days_checked": len(whole_legs),
            "whole_leg_days_without_prices": sum(r["whole_leg_no_price"] for r in whole_legs),
            "snapshot_rows_checked": len(snapshots)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--office-root", type=Path, default=Path("/home/elfbob/claude-code/data_manager/state/index-replication"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out.exists() or not out.is_relative_to((ROOT / "backtest/output/runs").resolve()):
        parser.error("Use a new directory inside backtest/output/runs.")
    packs = [args.office_root / name for name in ANCHORS]
    package_checks = [audit_package(p, ANCHORS[p.name]) for p in packs]
    v1, v2, v3 = packs
    require(read_json(v2 / "manifest.json")["base_manifest_sha256"] == ANCHORS[v1.name], "Base version link")
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "manifest.json", {"status": "running", "scope": "offline_office_delivery_consumer_audit"})
    shutil.copyfile(__file__, out / "audit_script.py")
    try:
        groups, wset = audit_wset(v3)
        coverage = audit_windows(v1, v2, v3, groups, out)
        mapping = rows(v2 / "index-return-series-map.csv")
        require({r["database_price_code"] for r in mapping} == set(MOTHERS + LEGS), "Return-series mapping")
        for row in mapping:
            require(sha(v2 / row["source_pdf"]) == row["source_sha256"], "Factsheet hash")
            require(row["total_return_vendor_suffix"] == row["net_return_vendor_suffix"] == "", "Unknown suffix filled")
        # The archived vendor label is evidence, not certification of official Q.
        require("自由流通市值\nw.wss(\"000001.SZ\", \"mkt_freeshares\"" in
                (v2 / "vendor-field-codegen.txt").read_text(), "Vendor free-float label")
        result = {"status": "PASS", "scope": "integrity_and_recomputed_coverage_only", "packages": package_checks,
                  "wset": wset, **coverage, "official_attribution_ready": False,
                  "blocked_by": ["Official Q/L/A and historical vendor definitions unresolved",
                                 "501 v3 NULL weights plus incomplete v1 new 2000 weights",
                                 "Source dates, effective dates and weight reference prices unresolved",
                                 "Pure-style seven-factor panel and added-security inputs not frozen",
                                 "Company actions, pre_close semantics and return lineage unverified"],
                  "shared_database_accessed": False, "paid_calls": 0, "production_changed": False}
        write_json(out / "verification.json", result)
        # A second hash-only pass detects mutation during the audit; no DB re-query.
        for pack in packs:
            require(sha(pack / "manifest.json") == ANCHORS[pack.name], "Manifest changed during audit")
            for item in read_json(pack / "manifest.json")["files"]:
                require(sha(pack / item["path"]) == item["sha256"], "Input changed during audit")
        require(sha(Path(__file__)) == sha(out / "audit_script.py"), "Audit script changed during run")
        write_json(out / "manifest.json", {"status": "complete", "scope": "offline_office_delivery_consumer_audit",
                   "overall_request_status": "OPEN", "official_attribution_ready": False,
                   "created_at": datetime.now(timezone.utc).isoformat(), "input_manifests": ANCHORS,
                   "office_root": str(args.office_root.resolve()), "inputs_unchanged": True,
                   "shared_database_accessed": False, "paid_calls": 0, "production_changed": False,
                   "outputs_sha256": {p.name: sha(p) for p in sorted(out.iterdir()) if p.name != "manifest.json"}})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        write_json(out / "manifest.json", {"status": "failed", "error": str(exc), "official_attribution_ready": False})
        raise


if __name__ == "__main__":
    main()
