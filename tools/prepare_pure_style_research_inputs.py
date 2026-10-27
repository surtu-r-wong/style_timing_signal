"""Build a local, read-only SQLite research input from frozen office packages.

No shared DB, API, project config, strategy execution or fee-bearing calls.
All numbers are stored as source text; missing cells become SQL NULL, never zero.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

ANCHORS = {
    'office_20261023_a_v1': 'e82a8ecb727b537b7390e88e3a7ddcc1b6f4277b323d6d69f2f6d01ab68ec412',
    'office_20261023_a_v2': 'ec6a4f261a383ff5a74b986bc826149e8849b11290f4c680bb6ae6bbb140488a',
    'office_20261024_a_v3': '2a502793d00eaa1b814398c8b9f54052bebda784d3b7105934b5dfccba19f6ee',
    'office_20261026_a_v4': '0d1fd655669213e793118bea3ccb2beeec6ddd35462aef16bf824328bb9fe1a4',
    'office_20261026_a_v5': 'c5ba0f79d342b5c9faa817e3f8508de90b69957767bc80382812734bd853fd8a',
    'office_20261026_a_v6': '993d03e96ced49ef6ce1444a1dec8dd113c3ade2ee364c8a169d688d7de6b431',
}


def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def js(path): return json.loads(path.read_text())


def read(path):
    with path.open(newline='') as f: return list(csv.DictReader(f))


def keyed(rows, cols=('ts_code', 'trade_date')):
    result = {tuple(r[c] for c in cols): r for r in rows}
    assert len(result) == len(rows), ('Duplicate keys', cols)
    return result


def verify_package(path, anchor):
    assert sha(path / 'manifest.json') == anchor, ('Manifest changed', path.name)
    m = js(path / 'manifest.json')
    names = {f['path'] for f in m['files']}
    assert len(names) == len(m['files']) == m['file_count']
    actual = {str(p.relative_to(path)) for p in path.rglob('*') if p.is_file()}
    assert actual == names | {'manifest.json'}
    for f in m['files']:
        p = path / f['path']
        assert not p.is_symlink() and p.resolve().is_relative_to(path.resolve())
        assert p.stat().st_mode & 0o222 == 0
        assert p.stat().st_size == f['bytes'] and sha(p) == f['sha256']
    size = sum(f['bytes'] for f in m['files'])
    assert size == m.get('total_bytes', m.get('bytes'))
    return dict(version=path.name, files=len(names), bytes=size, manifest_sha256=anchor)


def table(db, name, records, primary=()):
    assert records, name
    fields = list(records[0])
    assert all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', c) for c in fields + [name])
    sql = ','.join('"' + c + '" TEXT' for c in fields)
    if primary: sql += ',PRIMARY KEY (' + ','.join('"' + c + '"' for c in primary) + ')'
    db.execute(f'CREATE TABLE "{name}" ({sql})')
    db.executemany(f'INSERT INTO "{name}" VALUES (' + ','.join('?' for _ in fields) + ')',
        [[None if r.get(c) in (None, '') else str(r[c]) for c in fields] for r in records])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--office-root', type=Path, default=Path('/home/elfbob/claude-code/data_manager/state/index-replication'))
    p.add_argument('--v7-manifest-sha256', required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    args = p.parse_args()
    out = args.output_dir.resolve()
    assert not out.exists(), 'Use a new run directory'
    anchors = dict(ANCHORS, office_20261026_a_v7=args.v7_manifest_sha256)
    packs = [args.office_root / name for name in anchors]
    checks = [verify_package(pack, anchors[pack.name]) for pack in packs]
    v1, v2, v3, v4, v5, v6, v7 = packs
    out.mkdir(parents=True)
    shutil.copyfile(__file__, out / 'prepare_script.py')
    target = out / 'research-inputs.sqlite'
    db = sqlite3.connect(target)
    db.execute('PRAGMA cache_size=-16000')
    db.execute('PRAGMA journal_mode=DELETE')
    repaired = keyed(read(v6 / 'status-repaired-rows.csv'))
    actions = keyed(read(v4 / 'coverage/price-reference-resolved.csv'), ('effective_window', 'ts_code', 'trade_date'))
    union = read(v3 / 'window-universe-comparison.csv')
    stock = []
    applied, conflicts = set(), 0
    for window in js(v1 / 'coverage/windows.json'):
        effective = window['effective_date']
        codes = sorted(r['ts_code'] for r in union if r['effective_window'] == effective and r['in_updated_known_union'] == 'True')
        prices = keyed(read(v1 / f'raw/prices-{effective}.csv') + read(v3 / f'extra-prices-{effective}.csv'))
        status = keyed(read(v1 / f'raw/status-{effective}.csv') + read(v3 / f'extra-status-{effective}.csv'))
        suspended = keyed(read(v2 / f'suspensions-{effective}.csv') + read(v3 / f'extra-suspensions-{effective}.csv'))
        for code in codes:
            for day in window['dates']:
                key = code, day
                raw, s = prices.get(key, {}), status.get(key, {})
                layer = 'v1_v3'
                if key in repaired:
                    replacement = repaired[key]
                    assert s['is_suspended'] == 'False' and replacement['is_suspended'] == 'True'
                    assert all(s[k] == replacement[k] for k in ('is_st', 'limit_up', 'limit_down', 'is_new'))
                    s, layer = replacement, 'v6'
                    applied.add(key)
                conflict = key in suspended and s.get('is_suspended') != 'True'
                conflicts += int(conflict)
                action = actions.get((effective, code, day), {})
                stock.append(dict(effective_window=effective, ts_code=code, trade_date=day,
                    close=raw.get('close'), pre_close=raw.get('pre_close'), volume=raw.get('volume'), amount=raw.get('amount'),
                    price_missing=not bool(raw), is_suspended=s.get('is_suspended'), status_source_layer=layer,
                    suspension_evidence=key in suspended, unresolved_status_conflict=conflict,
                    observed_reference_change_resolution=action.get('resolution'),
                    action_evidence=action.get('evidence'), return_imputed=False))
    assert applied == set(repaired) and len(applied) == 62 and conflicts == 0
    assert len(stock) == 57123
    assert sum(r['price_missing'] for r in stock) == 42
    assert sum(bool(r['observed_reference_change_resolution']) for r in stock) == 508
    table(db, 'stock_day', stock, ('effective_window', 'ts_code', 'trade_date'))
    table(db, 'official_index', read(v7 / 'official-returns/official-index-levels.csv'),
          ('effective_window', 'series_code', 'trade_date'))
    table(db, 'stored_index_comparison', read(v7 / 'official-returns/daily-return-comparison.csv'),
          ('effective_window', 'index_code', 'trade_date'))
    table(db, 'industry_supplement', read(v6 / 'industry-historical-supplement.csv'), ('ts_code', 'asof'))
    table(db, 'financial_alias_facts', read(v5 / 'derived/financial-alias-bridge.csv'),
          ('canonical_ts_code', 'ts_code', 'end_date', 'statement_type', 'field'))
    table(db, 'observed_constituents_v3', read(v3 / 'constituents-observed.csv'), ('index_code', 'query_date', 'ts_code'))
    table(db, 'stored_constituents_v1', read(v1 / 'raw/index-constituents.csv'), ('index_code', 'effective_date', 'ts_code'))
    table(db, 'missing_weight_keys', read(v4 / 'coverage/weight-gap-keys.csv'))
    panels, scores, samples = [], [], []
    for file in sorted((v7 / 'factor-freeze').glob('*.csv')):
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})-(original_inventory|office_layers)-(.*)\.csv', file.name)
        assert match, file.name
        effective, asof, variant, kind = match.groups()
        for row in read(file):
            record = dict(effective_window=effective, asof=asof, variant=variant, **row)
            if kind == 'factors': panels.append(record)
            elif kind == 'sample-inputs': samples.append(record)
            else:
                assert kind.endswith('-scores')
                scores.append(dict(mother_index=kind.removesuffix('-scores'), **record))
    table(db, 'factor_panel', panels, ('effective_window', 'asof', 'variant', 'ts_code'))
    table(db, 'sample_inputs', samples, ('effective_window', 'asof', 'variant', 'ts_code'))
    table(db, 'mother_scores', scores, ('effective_window', 'asof', 'variant', 'mother_index', 'ts_code'))
    assert len(panels) == len(samples) == 20772 and len(scores) == 20000
    readiness = dict(input_preparation_ready=True, official_attribution_ready=False,
        ready_tasks=['Read and audit frozen original / supplemented factor panels and mother scores',
                     'Compare official price / total-return / net-return benchmarks in the two fixed windows',
                     'Inspect observed company-action evidence and repaired suspension state'],
        blocked_tasks={'official_free_float_attribution': 'Official unbanded Q, separate S/L/A and price/date definitions unavailable',
                       'official_weight_boundary_experiment': '740 new-leg / 283 mother weights and reference/effective-date contract unresolved',
                       'full_replication_acceptance': 'Official industry / financial vintage and complete corporate-action / divisor treatment unverified'},
        stock_day_rows=len(stock), status_repair_rows_applied=len(applied), remaining_status_conflicts=conflicts,
        observed_reference_changes_explained=508, missing_prices_preserved=42,
        factor_rows=len(panels), mother_score_rows=len(scores), official_index_rows=432,
        database_accessed=False, network_accessed=False, paid_calls=0, production_changed=False)
    db.commit()
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    db.close()
    # A fresh read-only consumer proves that this artifact, not just its manifest,
    # is usable. Missing values and input variants must survive materialization.
    with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as check:
        assert check.execute("SELECT count(*) FROM stock_day WHERE close IS NULL").fetchone()[0] == 42
        assert check.execute("SELECT count(*) FROM stock_day WHERE status_source_layer='v6' AND is_suspended='True'").fetchone()[0] == 62
        assert check.execute('SELECT count(*) FROM mother_scores').fetchone()[0] == 20000
        assert check.execute("SELECT count(*) FROM official_index WHERE in_requested_window='True'").fetchone()[0] == 396
    (out / 'readiness.json').write_text(json.dumps(readiness, ensure_ascii=False, indent=2) + '\n')
    (out / 'manifest.json').write_text(json.dumps(dict(status='complete', scope='offline_research_input_preparation',
        input_packages=checks, official_attribution_ready=False,
        outputs={p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in out.iterdir()}), indent=2) + '\n')
    for file in out.iterdir(): file.chmod(0o444)
    out.chmod(0o555)
    print(json.dumps(readiness, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
