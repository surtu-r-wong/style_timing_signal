import numpy as np
import pandas as pd
import pytest

from backtest.run_oi_research import build_targets


def example():
    index = pd.bdate_range('2020-01-01', periods=8)
    ew = pd.Series([1., 1., 1., -1., -1., -1., 0., -1.], index=index)
    feature = pd.DataFrame({'r5': [.1, -.1, .1, -.1, .1, -.1, .1, -.1],
                            'g5': [.1, .1, 0., .1, .1, 0., .1, .1],
                            'u5': [-.1, .1, .1, .1, .1, .1, .1, -.1]}, index=index)
    return ew, feature


def test_filters_do_not_flip_direction_or_change_the_other_leg():
    ew, f = example()
    targets, definitions = build_targets(ew, {'IC': f})
    assert len(targets.columns) == 32
    assert len(definitions) == 4
    assert targets['C__IC_L_g5'].tolist() == [1., 0., 0., -1., -1., -1., 0., -1.]
    assert targets['C__IC_S_u5'].tolist() == [1., 1., 1., -1., 0., -1., 0., 0.]
    for row in definitions:
        name = row['candidate']
        actual = targets['LEG__'+name]
        np.testing.assert_allclose(actual.abs().sum(), targets['QLEG__'+name].abs().sum())
        np.testing.assert_allclose(actual.abs().sum(), targets['DLEG__'+name].abs().sum())
        assert 0 <= row['q'] <= 1 and 0 <= row['r'] <= 1


def test_missing_feature_cannot_silently_flatten_positions():
    ew, f = example()
    f.loc[f.index[2], 'u5'] = np.nan
    with pytest.raises(ValueError, match='finite'):
        build_targets(ew, {'IC': f})


def test_feature_index_must_match_calendar():
    ew, f = example()
    with pytest.raises(ValueError, match='calendar'):
        build_targets(ew, {'IC': f.iloc[:-1]})
