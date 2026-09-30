import json

import numpy as np
import pandas as pd
import pytest

from backtest.oi_delivery_contract import validate_delivery


def bundle():
    calendar = pd.bdate_range('2020-01-01', periods=280)
    products, features = [], []
    for product in ['IC', 'IM']:
        for date in calendar:
            contracts = [f'{product}{i}' for i in range(4)]
            products.append(dict(date=date, product=product, total_oi=100., total_volume=20.,
                                 index_close=1000., complete=True,
                                 expected_contracts=json.dumps(contracts), observed_contracts=json.dumps(contracts)))
    for source in ['IC', 'POOL']:
        for i, date in enumerate(calendar):
            ready = i >= 270
            features.append(dict(date=date, source=source, g5=.01 if i>=20 else np.nan,
                                 r5=.02 if i>=20 else np.nan, rv20=.2 if i>=20 else np.nan,
                                 v5=.1 if i>=20 else np.nan, u5=.005 if ready else np.nan,
                                 feature_valid=ready, invalid_reason='' if ready else 'warmup',
                                 n_train=250 if ready else 0, rank=5 if ready else 0,
                                 train_start=calendar[i-250] if ready else pd.NaT,
                                 train_end=calendar[i-1] if ready else pd.NaT))
    return pd.DataFrame(products), pd.DataFrame(features), calendar


def test_complete_delivery_preserves_warmup_and_reports_two_sources():
    report = validate_delivery(*bundle())
    assert report['valid_rows'] == {'IC': 10, 'POOL': 10}
    assert report['product_rows'] == 560


def test_missing_whole_day_is_not_silently_compressed():
    products, features, calendar = bundle()
    features = features.drop(features.index[50])
    with pytest.raises(ValueError, match='calendar'):
        validate_delivery(products, features, calendar)


def test_equal_counts_do_not_hide_wrong_contract_membership():
    products, features, calendar = bundle()
    products.loc[3, 'observed_contracts'] = json.dumps(['IC0', 'IC1', 'IC2', 'IC9'])
    with pytest.raises(ValueError, match='contract'):
        validate_delivery(products, features, calendar)


def test_training_cannot_consume_current_day():
    products, features, calendar = bundle()
    features.loc[279, 'train_end'] = calendar[-1]
    with pytest.raises(ValueError, match='training'):
        validate_delivery(products, features, calendar)


def test_valid_score_requires_contiguous_complete_training_data():
    products, features, calendar = bundle()
    features.loc[100, 'g5'] = np.nan
    with pytest.raises(ValueError, match='training'):
        validate_delivery(products, features, calendar)


def test_false_completeness_cannot_be_consumed_as_good_total():
    products, features, calendar = bundle()
    products.loc[3, 'complete'] = False
    with pytest.raises(ValueError, match='incomplete'):
        validate_delivery(products, features, calendar)


def test_early_pool_cannot_be_filled_with_ic_only():
    products, features, calendar = bundle()
    products = products.loc[~(products['product'].eq('IM') & products.date.lt(calendar[10]))]
    with pytest.raises(ValueError, match='calendar'):
        validate_delivery(products, features, calendar)
