"""Validate data_manager deliveries; does not derive or persist market features."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

PRODUCT_COLUMNS = {'date', 'product', 'total_oi', 'total_volume', 'index_close', 'complete',
                   'expected_contracts', 'observed_contracts'}
FEATURE_COLUMNS = {'date', 'source', 'g5', 'r5', 'rv20', 'v5', 'u5', 'feature_valid',
                   'invalid_reason', 'n_train', 'rank', 'train_start', 'train_end'}
RAW_FEATURES = ['g5', 'r5', 'rv20', 'v5']


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_delivery(products, features, calendar):
    _require(PRODUCT_COLUMNS <= set(products), 'missing product columns')
    _require(FEATURE_COLUMNS <= set(features), 'missing feature columns')
    products, features = products.copy(), features.copy()
    for frame in [products, features]:
        frame['date'] = pd.to_datetime(frame.date)
    for col in ['train_start', 'train_end']:
        features[col] = pd.to_datetime(features[col])
    calendar = pd.DatetimeIndex(calendar)
    _require(len(calendar)>0 and calendar.is_unique and calendar.is_monotonic_increasing, 'invalid calendar')
    _require(set(products['product']) == {'IC', 'IM'}, 'IC and IM required')
    _require(set(features.source) == {'IC', 'POOL'}, 'IC and POOL required')
    _require(not products.duplicated(['product', 'date']).any(), 'duplicate product key')
    _require(not features.duplicated(['source', 'date']).any(), 'duplicate feature key')
    _require(products.complete.map(lambda x: isinstance(x, (bool, np.bool_))).all(), 'completeness must be boolean')
    _require(products.complete.all(), 'incomplete product totals require office resolution')
    starts = {}
    for product, d in products.groupby('product'):
        dates = pd.DatetimeIndex(d.date)
        starts[product] = dates.min()
        _require(dates.equals(calendar[calendar>=dates.min()]), 'product calendar gap or ordering')
        for row in d.itertuples():
            expected, observed = json.loads(row.expected_contracts), json.loads(row.observed_contracts)
            _require(isinstance(expected, list) and isinstance(observed, list), 'invalid contract lists')
            _require(bool(expected) and len(expected)==len(set(expected)) and len(observed)==len(set(observed))
                     and set(expected)==set(observed), 'contract membership mismatch')
            _require(all(isinstance(s, str) and s.startswith(product) for s in expected), 'wrong contract product')
        _require(np.isfinite(d[['total_oi', 'total_volume', 'index_close']].to_numpy(dtype=float)).all(), 'nonfinite product input')
        _require((d.total_oi.ge(0) & d.total_volume.ge(0) & d.index_close.gt(0)).all(), 'invalid product values')
    _require(features.feature_valid.map(lambda x: isinstance(x, (bool, np.bool_))).all(), 'feature_valid must be boolean')
    report = {'product_rows': len(products), 'feature_rows': len(features), 'valid_rows': {},
              'first_valid': {}, 'last_date': str(calendar[-1].date()),
              'scope': 'consumer structural validation; office owns derivation and numerical provenance'}
    for source, d in features.groupby('source'):
        start = starts['IC'] if source=='IC' else max(starts.values())
        dates = pd.DatetimeIndex(d.date)
        _require(dates.equals(calendar[calendar>=start]), 'feature calendar gap, ordering, or prelaunch POOL')
        d = d.reset_index(drop=True)
        ready = d.feature_valid
        values = d.loc[ready, RAW_FEATURES+['u5']].to_numpy(dtype=float)
        _require(np.isfinite(values).all(), 'valid feature has nonfinite value')
        _require(d.loc[~ready, 'u5'].isna().all(), 'invalid feature must have null residual')
        _require(d.loc[~ready, 'invalid_reason'].fillna('').str.len().gt(0).all(), 'invalid feature reason missing')
        _require((d.loc[ready, 'rv20'].ge(0) & d.loc[ready, 'v5'].ge(0)).all(), 'invalid volatility or turnover')
        for i in np.flatnonzero(ready.to_numpy()):
            row = d.iloc[i]
            _require(i>=250 and row.n_train==250 and row['rank']==5, 'invalid training size or rank')
            _require(row.train_start==dates[i-250] and row.train_end==dates[i-1], 'training dates include current day or gap')
            _require(np.isfinite(d.iloc[i-250:i][RAW_FEATURES].to_numpy(dtype=float)).all(), 'incomplete training calendar')
        report['valid_rows'][source] = int(ready.sum())
        report['first_valid'][source] = str(d.loc[ready, 'date'].min().date()) if ready.any() else None
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.delivery/'manifest.json').read_text())
    _require(manifest.get('contract_version')=='ew-oi-v1', 'unsupported delivery contract')
    records = {r['path']: r for r in manifest['artifacts']}
    for filename in ['product_daily.csv', 'features.csv', 'calendar.csv']:
        raw = (args.delivery/filename).read_bytes()
        _require(filename in records and hashlib.sha256(raw).hexdigest()==records[filename]['sha256'], 'delivery hash mismatch')
    products = pd.read_csv(args.delivery/'product_daily.csv')
    features = pd.read_csv(args.delivery/'features.csv')
    calendar = pd.read_csv(args.delivery/'calendar.csv', parse_dates=['date'])
    print(json.dumps(validate_delivery(products, features, calendar.loc[calendar.sfe.eq(1), 'date']),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
