# Equal Weight Raw Signal Design

## Goal

Create a new script that reads `data.csv`, treats every two price columns after the date as one relative-strength pair, and outputs one 20-day raw signal value from equal-weighted pair factors.

## Input

- File: `data.csv`
- First column: date
- Remaining columns: price/index columns
- Pairing rule: adjacent columns form one pair, so columns 2-3 are pair 1, columns 4-5 are pair 2, and so on.

## Calculation

For each pair `(A, B)`:

```text
pair_spread_20 = ln(A / A.shift(20)) - ln(B / B.shift(20))
pair_z_20 = (pair_spread_20 - rolling_mean_250) / rolling_std_250
pair_factor_20 = tanh(pair_z_20)
```

The final raw signal is:

```text
raw_signal_20 = average(pair_01_factor_20, ..., pair_15_factor_20)
```

The average requires all pair factors to be present for a date. Rows with insufficient lookback history are dropped from the saved output.

## Output

- Script: `generate_equal_weight_signal.py`
- CSV: `equal_weight_signal.csv`
- Columns:
  - `date`
  - one factor column per pair, named `pair_01_factor_20`, `pair_02_factor_20`, ...
  - final column `raw_signal_20`

No thresholds or `-1/0/1` state-machine signal are produced.

## Validation

- Dates are parsed and sorted ascending before calculation.
- The number of price columns must be even.
- Price columns are converted to numeric values.
- Non-positive prices naturally produce missing log-return values and are excluded from final saved rows.

## Test Plan

- Unit-test the calculation with synthetic paired price data.
- Verify the final raw signal equals the equal-weight average of per-pair factors.
- Verify the output does not include a discrete threshold signal column.
- Unit-test that an odd number of price columns raises a clear error.
