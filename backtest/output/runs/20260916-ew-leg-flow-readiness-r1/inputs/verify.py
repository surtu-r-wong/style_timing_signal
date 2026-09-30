"""Run from repository root; verify the frozen audit without querying the database."""
import importlib.util
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path.cwd()))
run = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frozen_flow_audit", run / "inputs/code__backtest__flow_field_readiness.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
d = pd.read_csv(run / "inputs/money_flow.csv")
c = pd.read_csv(run / "inputs/calendar.csv")
a, fields, yearly = module.audit(d, c)
assert a == json.loads((run / "outputs/summary.json").read_text())
pd.testing.assert_frame_equal(fields, pd.read_csv(run / "outputs/field_statistics.csv"))
pd.testing.assert_frame_equal(yearly, pd.read_csv(run / "outputs/yearly_field_coverage.csv"))
checks = {"offline_reproduction": True}
mutants = {}
mutants["unique_keys"] = pd.concat([d, d.iloc[[0]]], ignore_index=True)
mutants["calendar_coverage"] = d.drop(index=100)
for check, field, value in [
    ("finite_fields", "open_main_inflow_money", np.nan),
    ("nonnegative_integer_counts", "main_inflow_count", -1),
    ("positive_money_denominator", "main_in_money", -d.loc[0, "main_out_money"]),
    ("net_equals_extra_plus_large", "extra_bill_inflow_money", d.loc[0, "extra_bill_inflow_money"] + 1),
]:
    bad = d.copy(); bad.loc[0, field] = value; mutants[check] = bad
for check, bad in mutants.items():
    result, _, _ = module.audit(bad, c)
    assert not result["checks"][check], check
    assert not result["numerical_checks_pass"], check
    checks["mutation_" + check] = True
raw_root = Path("data_fixes/2026-09-03-index-money-flow/raw")
raw = pd.concat([pd.read_csv(p) for p in sorted(raw_root.glob("*.csv"))], ignore_index=True)
raw["trade_date"] = pd.to_datetime(raw.date).dt.strftime("%Y-%m-%d")
joined = d.merge(raw, on=["index_code", "trade_date"], validate="one_to_one")
assert len(joined) == len(d) == len(raw)
differences = {}
for field in module.FIELDS:
    delta = (joined[field] - joined[field.replace("_", "")]).abs()
    differences[field] = float(delta.max())
    assert delta.le(0.000051).all(), field
checks["raw_snapshot_reconciliation"] = True
receipt = {"checks": checks, "rows_compared": len(d), "raw_max_absolute_differences": differences,
           "source_note": "Outputs reproduced offline after removing an unused import; saved final audit code is the reproducer.",
           "limits": "Does not validate vendor semantics or PIT availability; no returns loaded."}
(run / "outputs/verification.json").write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
