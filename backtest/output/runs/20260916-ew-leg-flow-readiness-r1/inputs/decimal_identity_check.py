import csv
from decimal import Decimal
import json
from pathlib import Path
run = Path(__file__).resolve().parents[1]
with (run / "inputs/money_flow.csv").open() as f:
    rows = list(csv.DictReader(f))
out = {}
for name in ("vendor_main_equals_net", "four_buckets_zero_sum"):
    errors = []
    for row in rows:
        d = lambda key: Decimal(row[key])
        if name == "vendor_main_equals_net":
            error = d("main_inflow_money") - d("main_in_money") + d("main_out_money")
        else:
            error = sum(d(k + "_bill_inflow_money") for k in ("extra", "large", "middle", "small"))
        errors.append(abs(error))
    out[name] = {"decimal_gt_0_001": sum(e > Decimal("0.001") for e in errors),
                 "exactly_0_001": sum(e == Decimal("0.001") for e in errors)}
assert out == json.loads((run / "outputs/decimal_identity_check.json").read_text())
print("Decimal identity counts reproduced")
