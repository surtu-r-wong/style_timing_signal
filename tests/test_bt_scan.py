import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.scan import scan_grid  # noqa: E402


def test_scan_grid_one_row_per_combo_with_window_sharpes():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    und = pd.Series(rng.normal(0.001, 0.01, 300), index=idx)

    def factor_fn(lookback, smoothing):
        f = und.shift(-1).rolling(lookback).mean()  # dummy factor
        return (f * 50).clip(-1, 1).fillna(0.0)

    combos = [{"lookback": 5, "smoothing": 0}, {"lookback": 10, "smoothing": 3}]
    windows = {"a": ("2020-01-01", "2020-06-30"), "b": ("2020-07-01", "2020-12-31")}
    df = scan_grid(factor_fn, combos, und, carry=None, windows=windows)
    assert len(df) == 2
    assert {"lookback", "smoothing", "sharpe_a", "sharpe_b"} <= set(df.columns)
    assert df["sharpe_a"].notna().all()
