"""Timing and event-boundary checks for the isolated exploratory audit."""
import numpy as np
import pandas as pd

from tools.research_equity_bond_crossing import entry_indices, proxy_returns


def test_crossings_exclude_initial_below_state_and_require_reentry():
    ratio = pd.Series([.9, .8, 1.1, 1., .99, 1.2, .98])
    assert entry_indices(ratio, 1., cooldown=1) == [3, 6]
    assert entry_indices(ratio, 1., cooldown=4) == [3]


def test_next_close_execution_cannot_earn_before_that_close():
    position = pd.Series([1., 1., 0., 0., 0.])
    returns = pd.Series([0., .50, .10, .20, .30])
    actual = proxy_returns(position, returns, lag=2, cost_bps=3)
    np.testing.assert_allclose(actual, [0., -.0003, .10, .20-.0003, 0.])


def test_cost_and_pnl_on_short_to_long_flip():
    position = pd.Series([-1., 1., 1., 1.])
    returns = pd.Series([0., .05, .10, .20])
    np.testing.assert_allclose(proxy_returns(position, returns), [0., -.0003, -.10-.0006, .20])
