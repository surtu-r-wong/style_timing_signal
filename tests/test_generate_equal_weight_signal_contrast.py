from pathlib import Path
import importlib.util

import numpy as np
import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_equal_weight_signal_contrast.py"


def _load_module():
    assert MODULE_PATH.exists(), "generate_equal_weight_signal_contrast.py should exist"
    spec = importlib.util.spec_from_file_location("generate_equal_weight_signal_contrast", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prices(values):
    return list(values)


def _expected_pair_signal(left, right, lookback):
    rel_ret = left.pct_change().fillna(0) - right.pct_change().fillna(0)
    cumulative = (1.0 + rel_ret).rolling(lookback, min_periods=1).apply(
        lambda x: x.prod() - 1,
        raw=False,
    )
    rolling_mean = cumulative.rolling(lookback * 2, min_periods=lookback * 2).mean()
    rolling_std = cumulative.rolling(lookback * 2, min_periods=lookback * 2).std()
    rolling_std = rolling_std.where(rolling_std >= 1e-8, 1e-8)
    zscore = ((cumulative - rolling_mean) / rolling_std).fillna(0)
    return np.tanh(zscore / 2.0)


def test_defaults_match_contrast_configuration():
    module = _load_module()

    assert module.LOOKBACK == 20
    assert module.SMOOTHING_WINDOW == 5
    assert module.CONFIG_FILE == "style_factor_groups.csv"
    assert module.z_window_for_lookback(module.LOOKBACK) == 40


def test_calculates_contrast_style_signal_from_config_and_smooths_final_value(tmp_path):
    module = _load_module()
    index = pd.date_range("2024-01-01", periods=8, freq="D")
    prices = pd.DataFrame(
        {
            "A1": _prices([100, 103, 101, 106, 109, 107, 111, 115]),
            "B1": _prices([100, 101, 102, 103, 102, 104, 105, 106]),
            "A2": _prices([80, 82, 81, 83, 85, 84, 86, 89]),
            "B2": _prices([80, 79, 81, 80, 82, 81, 83, 82]),
        },
        index=index,
    )
    config_path = tmp_path / "groups.csv"
    config_path.write_text(
        "\n".join(
            [
                "group,left_column,right_column,direction",
                "1,A1,B1,forward",
                "2,A2,B2,reverse",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    result = module.calculate_contrast_equal_weight_signal(
        prices,
        lookback=2,
        smoothing_window=3,
        pair_configs=pair_configs,
    )

    expected_pair_1 = _expected_pair_signal(prices["A1"], prices["B1"], lookback=2)
    expected_pair_2 = _expected_pair_signal(prices["B2"], prices["A2"], lookback=2)
    expected_raw = pd.concat([expected_pair_1, expected_pair_2], axis=1).mean(axis=1)
    expected_factor = expected_raw.rolling(3, min_periods=1).mean()

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
    pd.testing.assert_series_equal(
        result["factor_value"],
        expected_factor,
        check_names=False,
    )


def test_ignores_unconfigured_price_columns(tmp_path):
    module = _load_module()
    index = pd.date_range("2024-01-01", periods=8, freq="D")
    prices = pd.DataFrame(
        {
            "Unused1": _prices([10, 11, 12, 13, 14, 15, 16, 17]),
            "A1": _prices([100, 103, 101, 106, 109, 107, 111, 115]),
            "Unused2": _prices([20, 21, 22, 23, 24, 25, 26, 27]),
            "B1": _prices([100, 101, 102, 103, 102, 104, 105, 106]),
            "A2": _prices([80, 82, 81, 83, 85, 84, 86, 89]),
            "B2": _prices([80, 79, 81, 80, 82, 81, 83, 82]),
        },
        index=index,
    )
    config_path = tmp_path / "groups.csv"
    config_path.write_text(
        "\n".join(
            [
                "group,left_column,right_column,direction",
                "1,A1,B1,forward",
                "2,A2,B2,forward",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    pair_configs = module.load_pair_configs(config_path, list(prices.columns))
    result = module.calculate_contrast_equal_weight_signal(
        prices,
        lookback=2,
        smoothing_window=3,
        pair_configs=pair_configs,
    )

    assert list(result.columns) == [
        "pair_01_factor_20",
        "pair_02_factor_20",
        "raw_signal_20",
        "factor_value",
    ]


def test_default_group_config_reverses_groups_7_and_8():
    module = _load_module()
    config_path = MODULE_PATH.parent / module.CONFIG_FILE
    prices = module.load_price_data(MODULE_PATH.parent / module.INPUT_FILE)

    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    assert len(pair_configs) == 15
    assert [pair.group for pair in pair_configs] == list(range(1, 16))
    assert [pair.direction for pair in pair_configs if pair.group in {7, 8}] == [
        "reverse",
        "reverse",
    ]
    assert all(pair.direction == "forward" for pair in pair_configs if pair.group not in {7, 8})


def test_generated_factor_value_matches_style_factor_fixture():
    module = _load_module()
    root = MODULE_PATH.parent
    prices = module.load_price_data(root / module.INPUT_FILE)
    expected = pd.read_csv(root / "对比" / "style_factor.csv", parse_dates=["date"])
    max_expected_date = expected["date"].max()
    prices = prices[prices.index <= max_expected_date]
    pair_configs = module.load_pair_configs(root / module.CONFIG_FILE, list(prices.columns))

    result = module.calculate_contrast_equal_weight_signal(prices, pair_configs=pair_configs)
    actual = result[["factor_value"]].round(4).reset_index()
    actual["date"] = pd.to_datetime(actual["date"])
    comparison = actual.merge(expected, on="date", how="inner", suffixes=("_actual", "_expected"))

    assert len(comparison) == len(expected)
    pd.testing.assert_series_equal(
        comparison["factor_value_actual"],
        comparison["factor_value_expected"],
        check_names=False,
    )
