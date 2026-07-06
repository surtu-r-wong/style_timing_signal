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


def test_citic40d_factor_fn_renames_style_columns_and_passes_params(monkeypatch):
    """citic40d 因子函数：把 PG 中文列名映射到英文并透传 lookback/z_window/smoothing。"""
    import signals.common.data_source as ds
    import signals.citic40d.generate_signal as cs
    from backtest import scan

    idx = pd.bdate_range("2015-01-01", periods=90)
    rng = np.random.default_rng(1)
    cn_cols = ["稳定", "成长", "金融", "周期", "消费"]
    raw = pd.DataFrame(
        {c: 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 90))) for c in cn_cols},
        index=idx,
    )

    def fake_load(names, start=None, end=None, trim_ragged_tail=False):
        assert names == cn_cols
        return raw[names].copy()

    monkeypatch.setattr(ds, "load_pg_closes", fake_load)

    fn = scan.citic40d_factor_fn()
    got = fn(lookback=5, z_window=10, smoothing=3)

    renamed = raw.rename(columns={"稳定": "stability", "成长": "growth", "金融": "finance",
                                  "周期": "cycle", "消费": "consumption"})
    expected = cs.compute_mean_factor(renamed, n=5, z_window=10, smoothing=3)
    pd.testing.assert_series_equal(got, expected, check_names=False)
