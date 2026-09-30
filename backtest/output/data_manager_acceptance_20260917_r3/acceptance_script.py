"""Consumer QA only: check office corrections; do not generate market features."""
from pathlib import Path
import hashlib
import json
import shutil
import numpy as np
import pandas as pd

ROOT = Path('/home/elfbob/claude-code/style_timing_signal')
OFFICE = Path('/home/elfbob/claude-code/data_manager/requests')
OUT = ROOT / 'backtest/output/data_manager_acceptance_20260917_r3'
PREV = ROOT / 'backtest/output/data_manager_acceptance_20260917_r2'
OUT.mkdir(exist_ok=False)

def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def dump(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')

# Unchanged prior audit is reusable only after checking its archived evidence.
for item in json.loads((PREV / 'manifest.json').read_text())['artifacts']:
    assert sha(PREV / item['path']) == item['sha256'], item['path']
old_receipts = {x['source']: x for x in json.loads((PREV / 'source_receipts.json').read_text())}
receipts = []

def verify(directory, manifest_name):
    manifest = json.loads((directory / manifest_name).read_text())
    for item in manifest.get('artifacts', manifest.get('files', [])):
        p = directory / item['path']
        assert sha(p) == item['sha256'], str(p)
        if 'bytes' in item:
            assert p.stat().st_size == item['bytes']
        receipts.append({'source': str(p), 'sha256': sha(p), 'bytes': p.stat().st_size})
    return manifest

etf_request = OFFICE / '2026-09-17-style-timing-signal-etf-share-flow'
etf = etf_request / 'delivery/etf-share-flow-v2'
assert verify(etf, 'manifest.json')['contract_version'] == 'etf-share-flow-v2'
shutil.copytree(etf, OUT / 'etf_delivery_v2')
shutil.copyfile(etf_request / 'response-05-office-2026-09-17.md', OUT / 'etf_office_response05.md')
for name in ['etf_share_daily.csv', 'pre_listing_rows.csv', 'share_translation_events.csv']:
    assert sha(etf / name) == sha(PREV / 'etf_delivery' / name), name
assert sha(etf_request / 'delivery/etf-share-flow-v1/net_flow_daily.csv') == sha(PREV / 'etf_delivery/net_flow_daily.csv')
raw = pd.read_csv(etf / 'etf_share_daily.csv', parse_dates=['trade_date'])
flow = pd.read_csv(etf / 'net_flow_daily.csv', parse_dates=['trade_date'])
old = pd.read_csv(PREV / 'etf_delivery/net_flow_daily.csv', parse_dates=['trade_date'])
assert len(raw) == len(flow) == 7013
assert not flow.duplicated(['ts_code', 'trade_date']).any()
pd.testing.assert_frame_equal(flow.loc[~flow.event_adjusted], old.loc[~old.event_adjusted], check_exact=True)
assert int((~flow.event_adjusted).sum()) == 7009
checks = []
for fund, g in raw.groupby('fund_id'):
    g = g.sort_values('trade_date').set_index('trade_date')
    f = flow.loc[flow.ts_code.eq(fund)].set_index('trade_date').sort_index()
    assert g.index.equals(f.index)
    np.testing.assert_allclose(g.total_shares, f.total_shares, rtol=0, atol=0)
    np.testing.assert_allclose(g.unit_nav, f.unit_nav, rtol=0, atol=0)
    assert np.isfinite(f.event_factor).all() and f.event_factor.gt(0).all()
    expected_shares = g.total_shares - g.total_shares.shift(1) * f.event_factor
    expected_amount = expected_shares * (g.unit_nav.shift(1) / f.event_factor)
    detected = f.event_source.eq('detected')
    expected_shares[detected] = np.nan
    expected_amount[detected] = np.nan
    np.testing.assert_allclose(expected_shares, f.net_new_shares, rtol=1e-10, atol=1e-5, equal_nan=True)
    np.testing.assert_allclose(expected_amount, f.est_net_flow_cny, rtol=1e-10, atol=.001, equal_nan=True)
    assert f.net_new_shares.isna().sum() == 1 + int(detected.sum())
    assert f.est_net_flow_cny.isna().sum() == 1 + int(detected.sum())
    assert f.loc[detected, 'flow_note'].str.contains('not independently identifiable').all()
    for date, row in f.loc[f.event_adjusted].iterrows():
        checks.append({'fund': fund, 'date': str(date.date()), 'source': row.event_source,
                       'net_new_shares': None if pd.isna(row.net_new_shares) else float(row.net_new_shares),
                       'est_net_flow_cny': None if pd.isna(row.est_net_flow_cny) else float(row.est_net_flow_cny),
                       'arithmetic_pass': True})
assert sum(x['source'] == 'official' for x in checks) == 3
assert sum(x['source'] == 'detected' for x in checks) == 1
assert next(x for x in checks if x['fund'] == '159845.SZ')['net_new_shares'] > 0
members = []
for phase in ['phase1-IM', 'phase2-IC']:
    request = OFFICE / '2026-09-17-style-timing-signal-futures-member-feasibility'
    directory = request / 'delivery' / phase
    m = verify(directory, 'manifest.corrected.json')
    assert sha(directory / 'manifest.json') == sha(PREV / phase / 'manifest.json')
    for item in m['files']:
        p = directory / item['path']
        assert sha(p) == old_receipts[str(p)]['sha256'], str(p)
    dest = OUT / phase
    dest.mkdir()
    for name in ['manifest.json', 'manifest.corrected.json', 'CORRECTION.md', 'collect.stdout.log', 'collect.stderr.log']:
        shutil.copyfile(directory / name, dest / name)
    members.append({'product': m['product'], 'corrected_manifest_pass': True,
                    'payloads_unchanged_since_prior_acceptance': True,
                    'days': m['days_with_data'], 'rank_rows': m['rank_rows']})
shutil.copyfile(request / 'response-04-office-2026-09-17.md', OUT / 'member_office_response04.md')
shutil.copyfile(__file__, OUT / 'acceptance_script.py')
dump('source_receipts.json', receipts)
report = {'status': 'pass', 'scope': 'ETF v2 correction and member corrected manifests; not signal validation',
          'prior_evidence_hashes_verified': True, 'etf_rows': 7013, 'etf_non_event_rows_identical': 7009,
          'etf_raw_unchanged': True, 'etf_events': checks, 'members': members,
          'prior_structural_checks_reused': str(PREV.relative_to(ROOT) / 'verification.json'),
          'no_market_features_derived_or_repaired': True,
          'limitations': ['historical availability not independently proved',
                         'ETF 2013-2014 calendar not independently verified',
                         'member top20 boards are not full-market positions',
                         'collector code changes reported by office, not independently retested']}
dump('verification.json', report)
dump('manifest.json', {'status': 'complete', 'artifacts': [
    {'path': str(p.relative_to(OUT)), 'sha256': sha(p), 'bytes': p.stat().st_size}
    for p in sorted(OUT.rglob('*')) if p.is_file()]})
print(json.dumps(report, ensure_ascii=False, indent=2))
