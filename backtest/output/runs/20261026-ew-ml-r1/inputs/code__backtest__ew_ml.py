"""Small, explicitly time-ordered EW sizing models. No database or production I/O."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

PARAMETERS = {'ridge': (1., 10., 100.), 'tree': (1., 10.)}
OI_COLUMNS = ['oi_g5', 'oi_v5']


def build_features(ew, price, oi):
    """IC price states plus existing style components; OI has an extra session lag."""
    idx = price.index
    if not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError('price calendar must be unique and sorted')
    e = ew.reindex(idx)
    pairs = [f'pair_0{i}_factor_20' for i in range(1, 5)]
    x = e[pairs].copy()
    x['ew'] = e.factor_value
    x['ew_change5'] = e.factor_value.diff(5)
    x['pair_dispersion'] = e[pairs].std(axis=1, ddof=1)
    x['direction'] = np.sign(e.factor_value)
    runs = x.direction.ne(x.direction.shift()).cumsum()
    x['log_age'] = np.log1p(x.groupby(runs).cumcount().add(1).clip(upper=60))
    daily = price.pct_change(fill_method=None)
    x['r5'] = price.pct_change(5, fill_method=None)
    x['r20'] = price.pct_change(20, fill_method=None)
    x['rv20'] = daily.rolling(20, min_periods=20).std(ddof=1)*np.sqrt(245)
    rv60 = daily.rolling(60, min_periods=60).std(ddof=1)*np.sqrt(245)
    x['vol_ratio'] = x.rv20/rv60
    x['drawdown60'] = price/price.rolling(60, min_periods=60).max()-1
    x[OI_COLUMNS] = oi.reindex(idx)[['g5', 'v5']].shift(1).to_numpy()
    return x.replace([np.inf, -np.inf], np.nan)


def make_labels(unit_returns, direction, annual_vol, horizon=5):
    """T+1 close entry: first mark-to-market return T+2, last T+horizon+1."""
    if horizon < 1 or not direction.index.equals(unit_returns.index):
        raise ValueError('invalid horizon or label calendar')
    future = sum(unit_returns.shift(-k) for k in range(2, horizon+2))
    denominator = annual_vol.reindex(unit_returns.index)/np.sqrt(245)*np.sqrt(horizon)
    y = direction*future/denominator.where(denominator > 0)
    end = pd.Series(unit_returns.index, index=unit_returns.index).shift(-(horizon+1))
    return pd.DataFrame({'y': y, 'label_end': end})


def purged_split(label_end, test_start, validation_size=245, min_train=500):
    """Use only matured outcomes; purge again before the inner validation block."""
    eligible = label_end.index[(label_end < pd.Timestamp(test_start)) & label_end.notna()]
    if len(eligible) <= validation_size:
        raise ValueError('insufficient history for validation')
    validation = eligible[-validation_size:]
    train = eligible[label_end.loc[eligible].to_numpy() < validation[0].to_datetime64()]
    if len(train) < min_train:
        raise ValueError('insufficient purged training history')
    return train, validation


def model_pipeline(family, parameter):
    if family == 'ridge':
        return Pipeline([('scale', StandardScaler()),
                         ('model', Ridge(alpha=parameter, solver='svd'))])
    if family == 'tree':
        return Pipeline([('model', HistGradientBoostingRegressor(
            loss='squared_error', max_depth=2, max_leaf_nodes=4,
            learning_rate=.05, max_iter=100, min_samples_leaf=60,
            l2_regularization=parameter, early_stopping=False, random_state=20261026))])
    raise ValueError('unknown model family')


def fit_predict(x, labels, test_dates, family, validation_size=245, min_train=500):
    test_dates = pd.DatetimeIndex(test_dates)
    if not len(test_dates) or not x.index.is_unique or not x.index.is_monotonic_increasing:
        raise ValueError('invalid fit calendar')
    if not test_dates.is_monotonic_increasing or not test_dates.is_unique:
        raise ValueError('invalid test dates')
    if not np.isfinite(x.to_numpy()).all():
        raise ValueError('features must be finite')
    aligned = labels.reindex(x.index)
    end = aligned.label_end.where(np.isfinite(aligned.y))
    train, validation = purged_split(end, test_dates[0], validation_size, min_train)
    tuning = []
    for parameter in PARAMETERS[family]:
        candidate = model_pipeline(family, parameter)
        candidate.fit(x.loc[train], aligned.y.loc[train])
        prediction = candidate.predict(x.loc[validation])
        mse = float(np.mean((prediction-aligned.y.loc[validation].to_numpy())**2))
        tuning.append({'parameter': parameter, 'validation_mse': mse})
    selected = min(tuning, key=lambda row: row['validation_mse'])['parameter']
    full_train = x.index[end < test_dates[0]]
    model = model_pipeline(family, selected)
    model.fit(x.loc[full_train], aligned.y.loc[full_train])
    prediction = pd.Series(model.predict(x.loc[test_dates]), index=test_dates, name='prediction')
    audit = {
        'family': family, 'selected_parameter': selected, 'tuning': tuning,
        'train_start': str(full_train[0].date()), 'train_end': str(full_train[-1].date()),
        'train_label_end': str(end.loc[full_train].max().date()), 'train_n': len(full_train),
        'inner_train_n': len(train), 'inner_train_label_end': str(end.loc[train].max().date()),
        'validation_start': str(validation[0].date()), 'validation_end': str(validation[-1].date()),
        'validation_label_end': str(end.loc[validation].max().date()),
        'test_start': str(test_dates[0].date()), 'test_end': str(test_dates[-1].date()),
        'test_n': len(test_dates), 'train_mean': float(aligned.y.loc[full_train].mean()),
        'features': list(x.columns),
    }
    if family == 'ridge':
        audit['standardized_coefficients'] = dict(zip(x.columns, model.named_steps['model'].coef_.tolist()))
        audit['scaler_mean'] = model.named_steps['scale'].mean_.tolist()
    return prediction, audit, model


def position_scale(prediction):
    if not np.isfinite(prediction).all():
        raise ValueError('cannot trade missing predictions')
    return (.75+.25*np.tanh(prediction)).rolling(5, min_periods=1).mean()


def past_same_side_scale(q, direction):
    """Lagged expanding mean within each side; never fit exposure on future targets."""
    if not q.index.equals(direction.index):
        raise ValueError('unaligned control calendar')
    result = pd.Series(.75, index=q.index)
    for side in (-1, 1):
        samples = q.where(direction.eq(side))
        count = samples.notna().cumsum().shift(1, fill_value=0)
        total = samples.fillna(0).cumsum().shift(1, fill_value=0)
        mean = (total/count.where(count > 0)).fillna(.75)
        result.loc[direction.eq(side)] = mean.loc[direction.eq(side)]
    return result
