"""生产入口的缺腿回归：只替换 PG 读取，保留对齐、计算与 CSV 输出。"""
import runpy
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.momentum_scan import PAIR_NAMES, momentum_pair_factor
from signals.common import data_source
from signals.slope20 import generate_signal as slope


def _prices(columns):
    rng = np.random.default_rng(2)
    idx = pd.bdate_range("2025-01-01", periods=400, name="date")
    return pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, .01, (400, len(columns))), axis=0)),
        index=idx, columns=columns,
    )


def _mock_pg(monkeypatch, prices):
    def load(names, start=None, end=None, trim_ragged_tail=False, trim_ragged_head=False):
        selected = prices.loc[start:end, names]
        rows = [(c, d, v) for c in names for d, v in selected[c].dropna().items()]
        return data_source.rows_to_frame(
            rows, {c: c for c in names}, trim_ragged_tail=trim_ragged_tail,
            trim_ragged_head=trim_ragged_head,
        )
    monkeypatch.setattr(data_source, "load_pg_closes", load)


# ── 头部参差：晚发布的腿不许静默稀释均值（f402b5e 修了尾部，这里补头部的对称面）──


def test_rows_to_frame_trims_ragged_head_only_when_asked(capsys):
    idx = pd.bdate_range("2025-01-01", periods=10, name="date")
    rows = [("A", d, float(i + 1)) for i, d in enumerate(idx)]
    rows += [("B", d, float(i + 1)) for i, d in enumerate(idx[3:])]

    frame = data_source.rows_to_frame(rows, {"A": "甲", "B": "乙"})
    assert frame.index[0] == idx[0]            # 默认（研究口径）：起始缺失放行
    assert frame["乙"].isna().sum() == 3

    trimmed = data_source.rows_to_frame(
        rows, {"A": "甲", "B": "乙"}, trim_ragged_head=True,
    )
    assert trimmed.index[0] == idx[3]          # 生产口径：裁到各列共同首日
    assert not trimmed.isna().any().any()
    err = capsys.readouterr().err
    assert "头部参差" in err and "甲" in err and "乙" in err
    assert idx[3].strftime("%Y-%m-%d") in err  # 警告点名保留起点


def _citic_expected(prices):
    from signals.citic40d.generate_signal import build_output
    cn2en = {"稳定": "stability", "成长": "growth", "金融": "finance",
             "周期": "cycle", "消费": "consumption"}
    return build_output(prices.rename(columns=cn2en).dropna())["factor_20"]


def test_citic_head_ragged_starts_at_common_start(tmp_path, monkeypatch):
    # 成长晚 5 个交易日发布：不许退化成「晚到腿缺位的 3-4 因子均值」，
    # 输出必须等于用对齐后帧完整 5 因子算出的结果。
    prices = _prices(["稳定", "成长", "金融", "周期", "消费"])
    prices.loc[prices.index[:5], "成长"] = np.nan
    _mock_pg(monkeypatch, prices)
    output = tmp_path / "citic.csv"
    monkeypatch.setattr(sys, "argv", ["generate_signal.py", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "signals/citic40d/generate_signal.py"), run_name="__main__")
    assert exc.value.code == 0
    actual = pd.read_csv(output, index_col="date", parse_dates=["date"])["factor_20"]
    pd.testing.assert_series_equal(actual, _citic_expected(prices), check_freq=False)


def test_equal_weight_head_ragged_not_diluted_by_zero(tmp_path, monkeypatch):
    # 一条腿晚 8 个交易日启动：早期不许被硬 0 稀释成四分之一均值。
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal, load_pair_configs,
    )
    prices = _prices(PAIR_NAMES)
    prices.loc[prices.index[:8], "中证2000成长"] = np.nan
    _mock_pg(monkeypatch, prices)
    output = tmp_path / "ew.csv"
    monkeypatch.setattr(sys, "argv", ["generate_signal.py", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "signals/equal_weight/generate_signal.py"), run_name="__main__")
    assert exc.value.code == 0
    actual = pd.read_csv(output, index_col="date", parse_dates=["date"])["factor_value"]
    configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    expected = calculate_contrast_equal_weight_signal(
        prices.dropna(), lookback=20, z_window=40, smoothing_window=5, pair_configs=configs,
    )["factor_value"].round(4)
    pd.testing.assert_series_equal(actual, expected, check_freq=False)


def test_slope_head_ragged_trims_to_common_start(tmp_path, monkeypatch):
    prices = _prices(PAIR_NAMES)
    prices.loc[prices.index[:4], PAIR_NAMES[6]] = np.nan
    _mock_pg(monkeypatch, prices)
    output = tmp_path / "slope.csv"
    assert slope.main(["--output", str(output)]) == 0
    actual = pd.read_csv(output, index_col="date", parse_dates=["date"])["factor_value"]
    pairs = list(zip(PAIR_NAMES[::2], PAIR_NAMES[1::2]))
    expected = momentum_pair_factor(
        prices.dropna(), pairs, family="slope", length=20, skip=0, z_window=120, smoothing=0,
    ).rename("factor_value").round(4)
    pd.testing.assert_series_equal(actual, expected, check_freq=False)


@pytest.mark.parametrize("missing_leg", [None, *PAIR_NAMES])
def test_slope_output_stops_at_last_complete_day(tmp_path, monkeypatch, missing_leg):
    prices = _prices(PAIR_NAMES)
    pairs = list(zip(PAIR_NAMES[::2], PAIR_NAMES[1::2]))
    expected = momentum_pair_factor(
        prices, pairs, family="slope", length=20, skip=0, z_window=120, smoothing=0,
    ).rename("factor_value").round(4)
    if missing_leg is not None:
        prices.loc[prices.index[-1], missing_leg] = np.nan
        expected = expected.iloc[:-1]
    _mock_pg(monkeypatch, prices)
    output = tmp_path / "slope.csv"
    assert slope.main(["--output", str(output)]) == 0
    actual = pd.read_csv(output, index_col="date", parse_dates=["date"])["factor_value"]
    pd.testing.assert_series_equal(actual, expected, check_freq=False)


def _run_hybrid(tmp_path, monkeypatch, prices, tag):
    _mock_pg(monkeypatch, prices)
    orig, output = tmp_path / f"{tag}-orig.csv", tmp_path / f"{tag}-confirmed.csv"
    for script, args in [
        ("update_growth_stability.py", ["--output", str(orig)]),
        ("update_confirmed_signal.py", ["--orig-signal", str(orig), "--output", str(output)]),
    ]:
        monkeypatch.setattr(sys, "argv", [script, *args])
        runpy.run_path(str(ROOT / "signals/hybrid20" / script), run_name="__main__")
    return pd.read_csv(output, index_col="date", parse_dates=["date"])


@pytest.mark.parametrize("late_leg, days", [("金融", 1), ("金融", 3), ("成长", 1)])
def test_hybrid_late_input_preserves_complete_prefix(tmp_path, monkeypatch, late_leg, days):
    prices = _prices(["稳定", "成长", "金融"])
    complete = _run_hybrid(tmp_path, monkeypatch, prices, "complete")
    prices.loc[prices.index[-days:], late_leg] = np.nan
    actual = _run_hybrid(tmp_path, monkeypatch, prices, "late")
    pd.testing.assert_frame_equal(actual, complete.iloc[:-days])
    assert not actual.isna().any().any()


@pytest.mark.parametrize("history, message", [
    (100, "金融确认信号无有效数据"),
    (400, "没有共同有效区间"),
])
def test_hybrid_without_common_valid_data_keeps_previous_output(
    tmp_path, monkeypatch, history, message,
):
    prices = _prices(["稳定", "成长", "金融"]).iloc[:history]
    _mock_pg(monkeypatch, prices)
    orig, output = tmp_path / "orig.csv", tmp_path / "confirmed.csv"
    pd.DataFrame(
        {"factor_20": [.5], "signal_20": [1], "factor_60": [.5], "signal_60": [1]},
        index=pd.DatetimeIndex(["2027-01-04"], name="date"),
    ).to_csv(orig)
    previous = b"date,hybrid_20\n2026-01-05,1\n"
    output.write_bytes(previous)
    script = ROOT / "signals/hybrid20/update_confirmed_signal.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--orig-signal", str(orig), "--output", str(output)])
    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(script), run_name="__main__")
    assert output.read_bytes() == previous
