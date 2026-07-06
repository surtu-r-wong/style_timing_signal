import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import evaluate  # noqa: E402

_KEYS = {"ann", "sharpe", "maxdd", "calmar", "turnover", "hit", "n_obs"}


def test_evaluate_returns_full_long_short_with_metric_keys():
    idx = pd.bdate_range("2020-01-01", periods=60)
    rng = np.random.default_rng(0)
    und = pd.Series(rng.normal(0.001, 0.01, 60), index=idx)
    pos = pd.Series(rng.choice([-1, 0, 1], 60), index=idx)
    out = evaluate(pos, und, carry=None, cost_bps=3, bootstrap_n=0)
    assert set(out) == {"full", "long", "short"}
    for seg in out.values():
        assert _KEYS <= set(seg)


def test_evaluate_aligns_mismatched_indices():
    idx1 = pd.bdate_range("2020-01-01", periods=50)
    idx2 = pd.bdate_range("2020-01-15", periods=50)  # partial overlap
    out = evaluate(pd.Series(1, index=idx1), pd.Series(0.01, index=idx2))
    assert out["full"]["n_obs"] == len(idx1.intersection(idx2))


def test_evaluate_bootstrap_adds_pvalue():
    idx = pd.bdate_range("2020-01-01", periods=120)
    rng = np.random.default_rng(3)
    und = pd.Series(rng.normal(0.001, 0.01, 120), index=idx)
    pos = pd.Series(rng.choice([-1, 0, 1], 120), index=idx)
    out = evaluate(pos, und, bootstrap_n=50, seed=0)
    assert 0 <= out["full"]["pvalue"] <= 1
