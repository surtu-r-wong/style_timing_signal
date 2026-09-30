import numpy as np
import pandas as pd
import pytest

from backtest.run_holding_age_audit import age_matched_target


def test_age_reference_matches_exposure_at_each_age_and_resets_between_episodes():
    idx = pd.date_range('2020-01-01', periods=9)
    base = pd.Series([0., -1., -1., -1., 0., -1., -1., 0., -1.], index=idx)
    selected = pd.Series([0., -1., 0., 0., 0., -1., -1., 0., 0.], index=idx)
    target, metadata, profile = age_matched_target(base, selected)
    assert metadata.age.tolist() == [0, 1, 2, 3, 0, 1, 2, 0, 1]
    np.testing.assert_allclose(target, [0., -2/3, -.5, 0., 0., -2/3, -.5, 0., -2/3])
    assert profile.n_days.tolist() == [3, 2, 1]
    assert metadata.iloc[-1].right_censored
    for age in profile.index:
        mask = metadata.age.eq(age)
        assert np.isclose(target[mask].abs().sum(), selected[mask].abs().sum())


def test_unknown_initial_age_is_rejected():
    with pytest.raises(ValueError, match='left'):
        age_matched_target(pd.Series([-1., 0.]), pd.Series([-1., 0.]))


@pytest.mark.parametrize('selected', [[0., np.nan], [-1., -1.], [0., -1.5]])
def test_invalid_candidate_target_is_rejected(selected):
    with pytest.raises(ValueError):
        age_matched_target(pd.Series([0., -1.]), pd.Series(selected))


def test_misaligned_calendar_is_rejected():
    with pytest.raises(ValueError, match='index'):
        age_matched_target(pd.Series([0., -1.]), pd.Series([0., -1.], index=[1, 2]))
