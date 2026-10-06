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
    def load(names, start=None, end=None, trim_ragged_tail=False):
        selected = prices.loc[start:end, names]
        rows = [(c, d, v) for c in names for d, v in selected[c].dropna().items()]
        return data_source.rows_to_frame(
            rows, {c: c for c in names}, trim_ragged_tail=trim_ragged_tail,
        )
    monkeypatch.setattr(data_source, "load_pg_closes", load)


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
