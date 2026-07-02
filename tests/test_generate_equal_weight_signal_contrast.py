from pathlib import Path
import importlib.util

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "signals" / "equal_weight" / "generate_signal.py"


def _load_module():
    assert MODULE_PATH.exists(), "signals/equal_weight/generate_signal.py should exist"
    spec = importlib.util.spec_from_file_location("generate_signal", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prices(values):
    return list(values)


def _sample_prices():
    index = pd.date_range("2024-01-01", periods=8, freq="D")
    return pd.DataFrame(
        {
            "A1": _prices([100, 103, 101, 106, 109, 107, 111, 115]),
            "B1": _prices([100, 101, 102, 103, 102, 104, 105, 106]),
            "A2": _prices([80, 82, 81, 83, 85, 84, 86, 89]),
            "B2": _prices([80, 79, 81, 80, 82, 81, 83, 82]),
        },
        index=index,
    )


def _write_two_group_config(tmp_path, second_direction="reverse"):
    config_path = tmp_path / "groups.csv"
    config_path.write_text(
        "\n".join(
            [
                "group,left_column,right_column,direction",
                "1,A1,B1,forward",
                f"2,A2,B2,{second_direction}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _expected_pair_signal(left, right, lookback, z_window=None):
    if z_window is None:
        z_window = lookback * 2
    rel_ret = left.pct_change().fillna(0) - right.pct_change().fillna(0)
    cumulative = (1.0 + rel_ret).rolling(lookback, min_periods=1).apply(
        lambda x: x.prod() - 1,
        raw=False,
    )
    rolling_mean = cumulative.rolling(z_window, min_periods=z_window).mean()
    rolling_std = cumulative.rolling(z_window, min_periods=z_window).std()
    rolling_std = rolling_std.where(rolling_std >= 1e-8, 1e-8)
    zscore = ((cumulative - rolling_mean) / rolling_std).fillna(0)
    return np.tanh(zscore / 2.0)


def test_defaults_match_variant_a_configuration():
    module = _load_module()

    assert module.LOOKBACK == 20
    assert module.SMOOTHING_WINDOW == 5
    assert Path(module.CONFIG_FILE).name == "config_6pairs.csv"
    assert Path(module.INPUT_FILE).name == "成长价值指数_2019.csv"
    assert module.z_window_for_lookback(module.LOOKBACK) == 40


def test_calculates_contrast_style_signal_from_config_and_smooths_final_value(tmp_path):
    module = _load_module()
    prices = _sample_prices()
    config_path = _write_two_group_config(tmp_path)
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
        result["pair_01_factor_2"],
        expected_pair_1,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        result["pair_02_factor_2"],
        expected_pair_2,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        result["raw_signal_2"],
        expected_raw,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        result["factor_value"],
        expected_factor,
        check_names=False,
    )


def test_column_suffix_follows_lookback_and_keeps_raw_column(tmp_path):
    module = _load_module()
    prices = _sample_prices()
    config_path = _write_two_group_config(tmp_path, second_direction="forward")
    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    result = module.calculate_contrast_equal_weight_signal(
        prices,
        lookback=3,
        smoothing_window=2,
        pair_configs=pair_configs,
    )

    assert list(result.columns) == [
        "pair_01_factor_3",
        "pair_02_factor_3",
        "raw_signal_3",
        "factor_value_raw",
        "factor_value",
    ]


def test_no_smoothing_keeps_raw_value(tmp_path):
    module = _load_module()
    prices = _sample_prices()
    config_path = _write_two_group_config(tmp_path)
    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    result = module.calculate_contrast_equal_weight_signal(
        prices,
        lookback=2,
        smoothing_window=0,
        pair_configs=pair_configs,
    )

    pd.testing.assert_series_equal(
        result["factor_value"],
        result["factor_value_raw"],
        check_names=False,
    )


def test_explicit_z_window_overrides_lookback_default(tmp_path):
    module = _load_module()
    prices = _sample_prices()
    config_path = _write_two_group_config(tmp_path)
    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    result = module.calculate_contrast_equal_weight_signal(
        prices,
        lookback=2,
        z_window=3,
        smoothing_window=0,
        pair_configs=pair_configs,
    )

    expected_pair_1 = _expected_pair_signal(prices["A1"], prices["B1"], lookback=2, z_window=3)
    pd.testing.assert_series_equal(
        result["pair_01_factor_2"],
        expected_pair_1,
        check_names=False,
    )


def test_negative_smoothing_window_rejected(tmp_path):
    module = _load_module()
    prices = _sample_prices()
    config_path = _write_two_group_config(tmp_path)
    pair_configs = module.load_pair_configs(config_path, list(prices.columns))

    try:
        module.calculate_contrast_equal_weight_signal(
            prices,
            lookback=2,
            smoothing_window=-1,
            pair_configs=pair_configs,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("negative smoothing_window should raise ValueError")


def test_bundled_preset_configs_are_valid():
    module = _load_module()
    config_dir = MODULE_PATH.parent

    six = module.load_pair_configs(config_dir / "config_6pairs.csv")
    assert len(six) == 6
    assert all(pair.direction == "forward" for pair in six)

    five = module.load_pair_configs(config_dir / "config_5pairs.csv")
    assert len(five) == 5
    assert all(pair.direction == "forward" for pair in five)


def test_generated_factor_value_matches_style_factor_fixture():
    """数值回归锚：旧 15 组指数代码配置 + 归档数据，对照独立实现(对比/style_signal.py)的输出。"""
    module = _load_module()
    prices = module.load_price_data(REPO_ROOT / "archive" / "对比" / "data.csv")
    expected = pd.read_csv(
        REPO_ROOT / "archive" / "对比" / "style_factor.csv", parse_dates=["date"]
    )
    max_expected_date = expected["date"].max()
    prices = prices[prices.index <= max_expected_date]
    pair_configs = module.load_pair_configs(
        REPO_ROOT / "archive" / "old_scripts" / "root" / "style_factor_groups.csv",
        list(prices.columns),
    )

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
