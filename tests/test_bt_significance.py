import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.significance import bootstrap_pvalue  # noqa: E402


def test_pvalue_in_range_and_reproducible():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(1)
    u = pd.Series(rng.normal(0, 0.01, 300), index=idx)
    pos = pd.Series(rng.choice([-1, 0, 1], 300), index=idx)
    p1 = bootstrap_pvalue(pos, u, metric="sharpe", n=200, seed=0)
    p2 = bootstrap_pvalue(pos, u, metric="sharpe", n=200, seed=0)
    assert 0 <= p1 <= 1
    assert p1 == p2  # deterministic given seed


def test_perfect_foresight_signal_is_significant():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(2)
    u = pd.Series(rng.normal(0.0005, 0.01, 300), index=idx)
    # pos[t] = sign(u[t+1]) → pos_eff[t]=sign(u[t]) → strategy earns |u| daily
    pos = pd.Series(np.sign(u.shift(-1).fillna(0.0)).to_numpy(), index=idx)
    p = bootstrap_pvalue(pos, u, metric="sharpe", n=200, seed=0)
    assert p < 0.05
