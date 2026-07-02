"""
Generate an equal-weight style signal using configurable index-pair directions.

The first CSV column is the date. Pair definitions live in style_factor_groups.csv.
For each configured pair, this script follows the contrast implementation: daily
relative return, 20-day rolling compound return, 40-day rolling z-score after the
40-day window is available, tanh(z / 2), equal-weight average, then 5-day smoothing.
The output keeps both the same-day unsmoothed value and the 5-day smoothed value.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_FILE = "data.csv"
OUTPUT_FILE = "equal_weight_signal_contrast.csv"
CONFIG_FILE = "style_factor_groups.csv"
LOOKBACK = 20
SMOOTHING_WINDOW = 5
OUTPUT_WINDOW_LABEL = 20
STD_FLOOR = 1e-8
VALID_DIRECTIONS = {"forward", "reverse"}


@dataclass(frozen=True)
class PairConfig:
    group: int
    left_column: str
    right_column: str
    direction: str = "forward"

    def effective_columns(self) -> tuple[str, str]:
        if self.direction == "reverse":
            return self.right_column, self.left_column
        return self.left_column, self.right_column


def z_window_for_lookback(lookback: int) -> int:
    return lookback * 2


def _validate_price_columns(columns: list[str], require_even: bool = True) -> None:
    if not columns:
        raise ValueError("data must contain at least one price column after the date column")
    if require_even and len(columns) % 2 != 0:
        raise ValueError("data must contain an even number of price columns after the date column")


def _default_pair_configs(price_columns: list[str]) -> list[PairConfig]:
    _validate_price_columns(price_columns, require_even=True)
    return [
        PairConfig(
            group=pair_idx // 2 + 1,
            left_column=price_columns[pair_idx],
            right_column=price_columns[pair_idx + 1],
            direction="forward",
        )
        for pair_idx in range(0, len(price_columns), 2)
    ]


def _validate_pair_configs(
    pair_configs: list[PairConfig],
    price_columns: list[str] | None = None,
) -> list[PairConfig]:
    if not pair_configs:
        raise ValueError("pair config must contain at least one group")

    normalized = sorted(pair_configs, key=lambda pair: pair.group)
    groups = [pair.group for pair in normalized]
    expected_groups = list(range(1, len(normalized) + 1))
    if groups != expected_groups:
        raise ValueError(f"pair config groups must be consecutive starting at 1: {expected_groups}")

    for pair in normalized:
        if pair.direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"group {pair.group} direction must be one of {sorted(VALID_DIRECTIONS)}"
            )
        if pair.left_column == pair.right_column:
            raise ValueError(f"group {pair.group} must use two different columns")

    configured_columns = [col for pair in normalized for col in (pair.left_column, pair.right_column)]
    duplicated = sorted({col for col in configured_columns if configured_columns.count(col) > 1})
    if duplicated:
        raise ValueError(f"pair config contains duplicated columns: {duplicated}")

    if price_columns is not None:
        unknown = sorted(set(configured_columns) - set(price_columns))
        if unknown:
            raise ValueError(f"pair config references unknown columns: {unknown}")

    return normalized


def load_pair_configs(
    config_file: str | Path = CONFIG_FILE,
    price_columns: list[str] | None = None,
) -> list[PairConfig]:
    """Load pair groups and directions from CSV."""
    df = pd.read_csv(config_file, encoding="utf-8-sig")
    required_columns = {"group", "left_column", "right_column", "direction"}
    missing_columns = sorted(required_columns - set(df.columns))
    if missing_columns:
        raise ValueError(f"pair config missing required columns: {missing_columns}")

    pair_configs: list[PairConfig] = []
    for row in df.itertuples(index=False):
        group = int(getattr(row, "group"))
        left_column = str(getattr(row, "left_column")).strip()
        right_column = str(getattr(row, "right_column")).strip()
        direction = str(getattr(row, "direction")).strip().lower()
        pair_configs.append(
            PairConfig(
                group=group,
                left_column=left_column,
                right_column=right_column,
                direction=direction,
            )
        )

    return _validate_pair_configs(pair_configs, price_columns)


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
    _validate_price_columns(price_columns, require_even=False)

    for col in price_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def _compute_pair_signal(
    left_price: pd.Series,
    right_price: pd.Series,
    lookback: int,
) -> pd.Series:
    aligned = pd.DataFrame({"left": left_price, "right": right_price}).dropna()
    if aligned.empty:
        return pd.Series(dtype=float)

    left_ret = aligned["left"].pct_change().fillna(0)
    right_ret = aligned["right"].pct_change().fillna(0)
    relative_return = left_ret - right_ret

    cumulative = (1.0 + relative_return).rolling(lookback, min_periods=1).apply(
        lambda x: x.prod() - 1,
        raw=False,
    )
    z_window = z_window_for_lookback(lookback)
    rolling_mean = cumulative.rolling(z_window, min_periods=z_window).mean()
    rolling_std = cumulative.rolling(z_window, min_periods=z_window).std()
    rolling_std = pd.Series(
        np.where(rolling_std < STD_FLOOR, STD_FLOOR, rolling_std),
        index=rolling_std.index,
    )

    zscore = (cumulative - rolling_mean) / rolling_std
    zscore = zscore.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return pd.Series(np.tanh(zscore / 2.0), index=aligned.index)


def calculate_contrast_equal_weight_signal(
    prices: pd.DataFrame,
    lookback: int = LOOKBACK,
    smoothing_window: int = SMOOTHING_WINDOW,
    pair_configs: list[PairConfig] | None = None,
) -> pd.DataFrame:
    """Calculate contrast-style pair signals, raw value, and smoothed factor value."""
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if smoothing_window <= 0:
        raise ValueError("smoothing_window must be positive")

    price_columns = list(prices.columns)
    if pair_configs is None:
        pair_configs = _default_pair_configs(price_columns)
    else:
        _validate_price_columns(price_columns, require_even=False)
        pair_configs = _validate_pair_configs(pair_configs, price_columns)

    output = pd.DataFrame(index=prices.index)
    pair_signals: list[pd.Series] = []

    for pair in pair_configs:
        left_col, right_col = pair.effective_columns()
        signal = _compute_pair_signal(prices[left_col], prices[right_col], lookback)
        pair_signals.append(signal)
        output[f"pair_{pair.group:02d}_factor_{OUTPUT_WINDOW_LABEL}"] = signal

    raw_signal = pd.Series(0.0, index=output.index)
    for signal in pair_signals:
        raw_signal += signal.reindex(output.index).fillna(0.0)
    raw_signal /= len(pair_signals)

    raw_signal_col = f"raw_signal_{OUTPUT_WINDOW_LABEL}"
    output[raw_signal_col] = raw_signal
    output["factor_value_raw"] = raw_signal
    output["factor_value"] = output["factor_value_raw"].rolling(
        smoothing_window,
        min_periods=1,
    ).mean()

    return output


def generate_contrast_equal_weight_signal(
    input_file: str | Path = INPUT_FILE,
    output_file: str | Path = OUTPUT_FILE,
    config_file: str | Path = CONFIG_FILE,
) -> pd.DataFrame:
    prices = load_price_data(input_file)
    pair_configs = load_pair_configs(config_file, list(prices.columns))
    result = calculate_contrast_equal_weight_signal(prices, pair_configs=pair_configs)
    result.round(4).to_csv(output_file, index_label="date")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate contrast-style equal-weight signal")
    parser.add_argument("--input", default=INPUT_FILE, help="input CSV path, default: data.csv")
    parser.add_argument(
        "--output",
        default=OUTPUT_FILE,
        help="output CSV path, default: equal_weight_signal_contrast.csv",
    )
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        help="pair config CSV path, default: style_factor_groups.csv",
    )
    args = parser.parse_args()

    prices = load_price_data(args.input)
    pair_configs = load_pair_configs(args.config, list(prices.columns))
    output = calculate_contrast_equal_weight_signal(prices, pair_configs=pair_configs)
    output.round(4).to_csv(args.output, index_label="date")

    print(f"Input: {args.input}")
    print(f"Output: {args.output}")
    print(f"Config: {args.config}")
    print(f"Pairs: {len(pair_configs)}")
    print(f"Lookback: {LOOKBACK}")
    print(f"Z-window: {z_window_for_lookback(LOOKBACK)}")
    print(f"Smoothing window: {SMOOTHING_WINDOW}")

    if output.empty:
        print("No valid rows after signal calculation")
        return 0

    latest = output.iloc[-1]
    print(f"Range: {output.index.min().date()} ~ {output.index.max().date()}, {len(output)} rows")
    print(
        f"Latest ({output.index[-1].date()}): "
        f"factor_value_raw={latest['factor_value_raw']:.4f}, "
        f"factor_value={latest['factor_value']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
