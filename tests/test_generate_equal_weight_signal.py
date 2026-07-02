import numpy as np
import pandas as pd
import pytest

from generate_equal_weight_signal import calculate_equal_weight_signal


def _prices(values):
    return pd.Series(values, dtype=float)


def test_calculates_raw_signal_as_equal_weight_average_of_pair_factors():
    index = pd.date_range("2024-01-01", periods=12, freq="D")
    prices = pd.DataFrame(
        {
            "A1": _prices([100, 103, 101, 106, 109, 107, 111, 115, 112, 118, 121, 119]),
            "B1": _prices([100, 101, 102, 103, 102, 104, 105, 106, 105, 107, 108, 109]),
            "A2": _prices([80, 82, 81, 83, 85, 84, 86, 89, 88, 90, 92, 91]),
            "B2": _prices([80, 79, 81, 80, 82, 81, 83, 82, 84, 83, 85, 84]),
        },
        index=index,
    )

    result = calculate_equal_weight_signal(prices, lookback=2, z_window=3)

    pair_1_spread = np.log(prices["A1"] / prices["A1"].shift(2)) - np.log(
        prices["B1"] / prices["B1"].shift(2)
    )
    pair_1_z = (pair_1_spread - pair_1_spread.rolling(3, min_periods=3).mean()) / (
        pair_1_spread.rolling(3, min_periods=3).std()
    )
    expected_pair_1 = np.tanh(pair_1_z)

    pair_2_spread = np.log(prices["A2"] / prices["A2"].shift(2)) - np.log(
        prices["B2"] / prices["B2"].shift(2)
    )
    pair_2_z = (pair_2_spread - pair_2_spread.rolling(3, min_periods=3).mean()) / (
        pair_2_spread.rolling(3, min_periods=3).std()
    )
    expected_pair_2 = np.tanh(pair_2_z)

    expected_raw = pd.concat([expected_pair_1, expected_pair_2], axis=1).mean(
        axis=1, skipna=False
    )

    pd.testing.assert_series_equal(
        result["pair_01_factor_20"],
        expected_pair_1,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        result["pair_02_factor_20"],
        expected_pair_2,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        result["raw_signal_20"],
        expected_raw,
        check_names=False,
    )
    assert "signal_20" not in result.columns


def test_rejects_odd_number_of_price_columns():
    prices = pd.DataFrame(
        {
            "A1": [100, 101, 102],
            "B1": [100, 99, 101],
            "A2": [80, 81, 82],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )

    with pytest.raises(ValueError, match="even number of price columns"):
        calculate_equal_weight_signal(prices, lookback=1, z_window=2)
