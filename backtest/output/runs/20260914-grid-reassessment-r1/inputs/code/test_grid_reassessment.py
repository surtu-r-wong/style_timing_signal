import numpy as np
import pandas as pd

from backtest.grid_reassessment import blend_carry, rank_table
from backtest.momentum_scan import momentum_grid
from backtest.scan import default_grid


def test_carry_missing_leg_preserves_fixed_half_weight():
    legs = pd.DataFrame({'IC': [.12, .12, np.nan], 'IM': [np.nan, .08, np.nan]})
    old, fixed = blend_carry(legs)
    np.testing.assert_allclose(old, [.12, .10])
    np.testing.assert_allclose(fixed, [.06, .10, 0.])


def test_selection_ignores_reused_window_and_ties_keep_original_order():
    table = pd.DataFrame({'grid': ['g'] * 3, 'family': ['s'] * 3,
                          'sharpe_train': [1., 2., 1.], 'sharpe_val': [1., .5, 1.],
                          'sharpe_reused': [-9., 20., 99.]})
    actual = rank_table(table)
    assert actual.rank_family.tolist() == [1, 3, 2]
    assert actual.rank_grid.tolist() == [1, 3, 2]
    assert actual.old_three_window_score.tolist() == [-9., .5, 1.]


def test_original_grid_counts_and_current_points():
    assert len(default_grid()) == 40
    assert len(momentum_grid()) == 174
    assert {'lookback': 20, 'z_window': 40, 'smoothing': 5} in default_grid()
    assert {'family': 'slope', 'length': 20, 'skip': 0, 'z_window': 120, 'smoothing': 0} in momentum_grid()
