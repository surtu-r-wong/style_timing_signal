import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.metrics import (  # noqa: E402
    ann_return, sharpe, max_drawdown, calmar, turnover, hit_rate,
)


def _noisy(seed):
    rng = np.random.default_rng(seed)
    return pd.Series(0.0005 + rng.normal(0, 0.01, 500),
                     index=pd.bdate_range("2020-01-01", periods=500))


def test_ann_return_full_calendar_mean_times_245():
    r = pd.Series([0.001] * 245, index=pd.bdate_range("2020-01-01", periods=245))
    assert abs(ann_return(r) - 0.001 * 245) < 1e-12


def test_max_drawdown_known_path():
    # cum: 1.1, 0.55, 0.605; peak 1.1 → trough 0.55 → dd = 0.55/1.1 - 1 = -0.5
    r = pd.Series([0.1, -0.5, 0.1])
    assert abs(max_drawdown(r) - (-0.5)) < 1e-9


def test_sharpe_positive_for_positive_drift():
    assert sharpe(_noisy(0)) > 0


def test_sharpe_zero_when_no_variance():
    assert sharpe(pd.Series([0.0, 0.0, 0.0])) == 0.0


def test_calmar_is_ann_over_absmaxdd():
    r = _noisy(1)
    assert abs(calmar(r) - ann_return(r) / abs(max_drawdown(r))) < 1e-12


def test_turnover_annualized_abs_position_change():
    pos = pd.Series([0, 1, 1, -1, 0])  # |Δ| = 1+0+2+1 = 4 over 5 rows
    assert abs(turnover(pos) - 4 / 5 * 245) < 1e-9


def test_hit_rate_positive_fraction_among_nonzero():
    r = pd.Series([0.01, -0.01, 0.0, 0.02])  # nonzero: 3, positive: 2
    assert abs(hit_rate(r) - 2 / 3) < 1e-12
