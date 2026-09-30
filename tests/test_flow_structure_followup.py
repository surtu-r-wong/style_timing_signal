import numpy as np
import pandas as pd
import pytest

from backtest.flow_structure_followup import short_modes, stable_short, daily_phase


def test_entry_exit_rules_reset_and_do_not_reenter():
    base = pd.Series([-1., -1., -1., -1., 0., -1., -1., -1.])
    score = pd.Series([-1., 1., -1., 1., 1., 0., -1., 1.])
    actual = short_modes(base, score)
    assert actual.daily.tolist() == [0, -1, 0, -1, 0, -1, 0, -1]
    assert actual.entry_hold.tolist() == [0, 0, 0, 0, 0, -1, -1, -1]
    assert actual.exit_only.tolist() == [-1, -1, 0, 0, 0, -1, 0, 0]
    assert actual.entry_exit.tolist() == [0, 0, 0, 0, 0, -1, 0, 0]


def test_hysteresis_boundaries_memory_and_reset():
    base = pd.Series([-1., -1., -1., -1., -1., 0., -1., -1.])
    score = pd.Series([0., 2., 1., -1., -2., 2., 0., 2.])
    actual = stable_short(base, score, pd.Series(1., index=base.index))
    assert actual.tier.tolist() == [-.5, -1, -.5, -.5, 0, 0, -.5, -1]
    assert actual.hysteresis.tolist() == [0, -1, -1, -1, 0, 0, 0, -1]


def test_state_rules_are_prefix_invariant():
    base = pd.Series([-1., -1., 0., -1., -1., -1., -1.])
    score = pd.Series([0., 2., -1., 2., -2., 0., 1.])
    threshold = pd.Series(1., index=base.index)
    pd.testing.assert_frame_equal(short_modes(base, score).iloc[:5], short_modes(base.iloc[:5], score.iloc[:5]))
    pd.testing.assert_frame_equal(stable_short(base, score, threshold).iloc[:5], stable_short(base.iloc[:5], score.iloc[:5], threshold.iloc[:5]))
    bad = score.copy(); bad.iloc[2] = np.nan
    with pytest.raises(ValueError, match='nonfinite'):
        short_modes(base, bad)


def test_phase_labels_include_rejections_and_interior_gap():
    base = pd.Series([-1.] * 6 + [0., -1.])
    target = pd.Series([0., -1., 0., -1., 0., 0., 0., 0.])
    assert daily_phase(base, target).tolist() == ['before_first_entry', 'kept', 'between_entries',
        'kept', 'after_last_exit', 'after_last_exit', 'outside_short', 'whole_episode_rejected']
