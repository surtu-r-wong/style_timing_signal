"""Behavioral checks for causal features, executable labels and walk-forward fits."""
import numpy as np
import pandas as pd
import pytest

from backtest.ew_ml import (
    build_features, make_labels, purged_split, fit_predict, model_pipeline,
    position_scale, past_same_side_scale,
)


def synthetic_inputs(n=1100):
    idx = pd.bdate_range('2015-01-01', periods=n, name='date')
    rng = np.random.default_rng(32)
    price = pd.Series(100*np.exp(np.cumsum(rng.normal(0, .01, n))), index=idx)
    ew = pd.DataFrame({f'pair_0{i}_factor_20': np.sin(np.arange(n)/40+i)*.4
                       for i in range(1, 5)}, index=idx)
    ew['factor_value'] = ew.mean(axis=1).rolling(5, min_periods=1).mean()
    oi = pd.DataFrame({'g5': rng.normal(0, .05, n), 'v5': rng.uniform(.2, 1, n)}, index=idx)
    return ew, price, oi


def test_label_starts_after_next_close_and_tail_stays_missing():
    idx = pd.bdate_range('2020-01-01', periods=12)
    r = pd.Series(np.arange(12)/100, index=idx)
    sign = pd.Series(-1., index=idx)
    vol = pd.Series(np.sqrt(245/5), index=idx)  # five-day denominator = 1
    labels = make_labels(r, sign, vol)
    assert labels.y.iloc[0] == pytest.approx(-sum([.02, .03, .04, .05, .06]))
    assert labels.label_end.iloc[0] == idx[6]
    assert labels.y.iloc[-6:].isna().all()
    r.iloc[1] = 99  # execution-day return is not captured
    assert make_labels(r, sign, vol).y.iloc[0] == labels.y.iloc[0]


def test_purge_uses_label_end_not_just_observation_date():
    idx = pd.bdate_range('2020-01-01', periods=40)
    end = pd.Series(idx, index=idx).shift(-6)
    train, validation = purged_split(end, idx[34], validation_size=8, min_train=5)
    assert (end.loc[train] < validation[0]).all()
    assert (end.loc[validation] < idx[34]).all()
    assert not train.intersection(validation).size
    assert idx[28] not in validation  # ends exactly on the test date


def test_features_are_causal_and_oi_has_extra_session_lag():
    ew, price, oi = synthetic_inputs(200)
    before = build_features(ew, price, oi)
    pd.testing.assert_series_equal(before.oi_g5, oi.g5.shift(1), check_names=False)
    ew.iloc[150:] *= 20
    price.iloc[150:] *= 5
    oi.iloc[150:] = 99
    after = build_features(ew, price, oi)
    pd.testing.assert_frame_equal(before.iloc[:150], after.iloc[:150])
    assert len(before.columns) == 16


@pytest.mark.parametrize('family', ['ridge', 'tree'])
def test_future_labels_and_features_cannot_change_past_predictions(family):
    ew, price, oi = synthetic_inputs()
    x = build_features(ew, price, oi).dropna()
    rng = np.random.default_rng(44)
    labels = make_labels(pd.Series(rng.normal(0, .01, len(x)), index=x.index),
                         x.direction, x.rv20)
    dates = x.index[-50:-40]
    p, audit, _ = fit_predict(x, labels, dates, family, validation_size=80, min_train=200)
    changed_x, changed_labels = x.copy(), labels.copy()
    changed_x.loc[x.index > dates[-1]] = 999
    # Alter even labels of earlier rows whose outcome is unavailable at the fit boundary.
    changed_labels.loc[labels.label_end >= dates[0], 'y'] = 999
    changed_labels.loc[changed_labels.index >= dates[0], 'y'] = 999
    p2, audit2, _ = fit_predict(changed_x, changed_labels, dates, family,
                               validation_size=80, min_train=200)
    np.testing.assert_array_equal(p, p2)
    assert audit['selected_parameter'] == audit2['selected_parameter']
    assert pd.Timestamp(audit['train_label_end']) < dates[0]


def test_scaler_and_tree_early_stopping_do_not_see_validation():
    model = model_pipeline('ridge', 10)
    model.fit(pd.DataFrame({'x': [0., 2., 4.]}), [0., 1., 2.])
    assert model.named_steps['scale'].mean_[0] == 2
    model.predict(pd.DataFrame({'x': [999.]}))
    assert model.named_steps['scale'].mean_[0] == 2
    assert model_pipeline('tree', 1).named_steps['model'].early_stopping is False


def test_scale_bounds_and_control_only_uses_past_same_direction():
    idx = pd.bdate_range('2020-01-01', periods=5)
    pred = pd.Series([-100., 100., 0., .2, -.2], index=idx)
    q = position_scale(pred)
    assert q.between(.5, 1).all()
    side = pd.Series([1., -1., 1., 1., -1.], index=idx)
    control = past_same_side_scale(q, side)
    assert control.iloc[0] == control.iloc[1] == .75
    assert control.iloc[2] == q.iloc[0]
    assert control.iloc[3] == pytest.approx((q.iloc[0]+q.iloc[2])/2)
    assert control.iloc[4] == q.iloc[1]
