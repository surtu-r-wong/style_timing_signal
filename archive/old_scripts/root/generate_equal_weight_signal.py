"""
Generate a 20-day equal-weight raw signal from adjacent column pairs in data.csv.

The first CSV column is the date. Every two remaining columns form one
relative-strength pair: column 1 versus column 2, column 3 versus column 4,
and so on. Each pair is standardized before all pair factors are equally
weighted into raw_signal_20.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_FILE = "data.csv"
OUTPUT_FILE = "equal_weight_signal.csv"
LOOKBACK = 20
Z_WINDOW = 250
OUTPUT_WINDOW_LABEL = 20


def _validate_price_columns(columns: list[str]) -> None:
    if not columns:
        raise ValueError("data must contain at least two price columns after the date column")
    if len(columns) % 2 != 0:
        raise ValueError("data must contain an even number of price columns after the date column")


def load_price_data(input_file: str | Path = INPUT_FILE) -> pd.DataFrame:
    """Load price data from CSV, parse the first column as date, and sort ascending."""
    df = pd.read_csv(input_file, encoding="utf-8-sig")
    if df.empty:
        raise ValueError(f"{input_file} is empty")

    date_col = df.columns[0]
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()

    price_columns = list(df.columns)
    _validate_price_columns(price_columns)

    for col in price_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def calculate_equal_weight_signal(
    prices: pd.DataFrame,
    lookback: int = LOOKBACK,
    z_window: int = Z_WINDOW,
) -> pd.DataFrame:
    """Calculate per-pair standardized factors and their equal-weight raw signal."""
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if z_window <= 1:
        raise ValueError("z_window must be greater than 1")

    price_columns = list(prices.columns)
    _validate_price_columns(price_columns)

    output = pd.DataFrame(index=prices.index)

    for pair_idx in range(0, len(price_columns), 2):
        left_col = price_columns[pair_idx]
        right_col = price_columns[pair_idx + 1]

        left = prices[left_col].where(prices[left_col] > 0)
        right = prices[right_col].where(prices[right_col] > 0)

        spread = np.log(left / left.shift(lookback)) - np.log(right / right.shift(lookback))
        rolling_mean = spread.rolling(z_window, min_periods=z_window).mean()
        rolling_std = spread.rolling(z_window, min_periods=z_window).std().replace(0, np.nan)
        factor = np.tanh((spread - rolling_mean) / rolling_std)

        output[f"pair_{pair_idx // 2 + 1:02d}_factor_{OUTPUT_WINDOW_LABEL}"] = factor

    pair_factor_columns = list(output.columns)
    output[f"raw_signal_{OUTPUT_WINDOW_LABEL}"] = output[pair_factor_columns].mean(
        axis=1,
        skipna=False,
    )

    return output


def generate_equal_weight_signal(
    input_file: str | Path = INPUT_FILE,
    output_file: str | Path = OUTPUT_FILE,
) -> pd.DataFrame:
    prices = load_price_data(input_file)
    result = calculate_equal_weight_signal(prices)
    result = result.dropna(subset=[f"raw_signal_{OUTPUT_WINDOW_LABEL}"])
    result.round(4).to_csv(output_file, index_label="date")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate equal-weight 20-day raw signal")
    parser.add_argument("--input", default=INPUT_FILE, help="input CSV path, default: data.csv")
    parser.add_argument(
        "--output",
        default=OUTPUT_FILE,
        help="output CSV path, default: equal_weight_signal.csv",
    )
    args = parser.parse_args()

    prices = load_price_data(args.input)
    output = calculate_equal_weight_signal(prices)
    output = output.dropna(subset=[f"raw_signal_{OUTPUT_WINDOW_LABEL}"])
    output.round(4).to_csv(args.output, index_label="date")

    pair_count = len(prices.columns) // 2
    print(f"Input: {args.input}")
    print(f"Output: {args.output}")
    print(f"Pairs: {pair_count}")

    if output.empty:
        print("No valid rows after lookback and rolling-window calculation")
        return 0

    print(f"Range: {output.index.min().date()} ~ {output.index.max().date()}, {len(output)} rows")
    latest = output.iloc[-1]
    print(
        f"Latest ({output.index[-1].date()}): "
        f"raw_signal_{OUTPUT_WINDOW_LABEL}={latest[f'raw_signal_{OUTPUT_WINDOW_LABEL}']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
