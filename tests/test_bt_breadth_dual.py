import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _synthetic():
    idx = pd.bdate_range("2020-01-01", periods=200)
    rng = np.random.default_rng(0)
    und_price = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 200))), index=idx)
    und_ret = und_price.pct_change().fillna(0.0)
    breadth_df = pd.DataFrame({"m1": pd.Series(rng.uniform(0.2, 0.8, 200), index=idx)})
    long_factor = pd.Series(rng.uniform(-0.5, 0.5, 200), index=idx)
    carry = pd.Series(0.0, index=idx)
    return long_factor, breadth_df, und_price, und_ret, carry, idx


def test_scan_breadth_one_row_per_combo_with_hedge_metrics():
    """扫描：每 combo 一行，含各窗 dual_sharpe / maxdd_improve / short_frac（对冲目标，非短腿 Sharpe）。"""
    from backtest.breadth_dual import scan_breadth
    long_factor, breadth_df, und_price, und_ret, carry, idx = _synthetic()
    combos = [{"measure": "m1", "form": "deteriorating", "P": 20, "Q": 20, "threshold": 0.2},
              {"measure": "m1", "form": "nonconfirm", "P": 20, "Q": 20, "threshold": 0.2}]
    windows = {"a": ("2020-01-01", "2020-05-31"), "b": ("2020-06-01", "2020-12-31")}
    df = scan_breadth(long_factor, breadth_df, und_price, und_ret, carry, combos, windows)
    assert len(df) == 2
    assert {"measure", "form", "P", "Q",
            "dual_sharpe_a", "maxdd_improve_a", "short_frac_a",
            "dual_sharpe_b", "maxdd_improve_b", "short_frac_b"} <= set(df.columns)
    assert df["short_frac_a"].between(0, 1).all()
