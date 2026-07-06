"""hybrid20 状态机 4 阈值 walk-forward 扫描（Phase 3 T6，bespoke，非 §3.2 网格）。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_threshold_grid_respects_state_machine_constraints():
    from backtest.scan_thresholds import threshold_grid
    combos = threshold_grid(open_longs=(0.2, 0.4), open_shorts=(-0.2, -0.4), hysteresis=0.1)
    assert len(combos) == 4
    for c in combos:
        assert c["close_long"] < c["open_long"]      # 平多阈 < 开多阈
        assert c["close_short"] > c["open_short"]     # 平空阈 > 开空阈
        assert c["close_long"] >= 0 and c["close_short"] <= 0


def test_scan_thresholds_one_row_per_combo_with_diagnostics():
    from backtest.scan_thresholds import scan_thresholds, threshold_grid
    idx = pd.bdate_range("2015-01-01", periods=800)
    rng = np.random.default_rng(0)
    factor = pd.Series(np.tanh(np.cumsum(rng.normal(0, 0.1, 800)) / 5.0), index=idx)
    und = pd.Series(rng.normal(0.0003, 0.01, 800), index=idx)
    combos = threshold_grid(open_longs=(0.2,), open_shorts=(-0.2, -0.4))
    windows = {"train": ("2015-01-01", "2017-06-30"), "hold": ("2017-07-01", "2019-12-31")}

    df = scan_thresholds(factor, und, None, combos, windows)
    assert len(df) == 2
    assert {"open_long", "open_short", "sharpe_train", "sharpe_hold",
            "short_sharpe", "short_frac"} <= set(df.columns)
    assert df["short_frac"].between(0, 1).all()
