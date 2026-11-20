"""Fixed-model June boundary diagnostic, scoped before results in office request.

Uses original inventory and original return formula; not official replication.
"""
import ast
import argparse
import csv
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
import shutil
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OFFICE = Path('/home/elfbob/claude-code/data_manager')
BASE = OFFICE / 'state/index-replication'
V4 = BASE / 'office_20261026_a_v4'
V7 = BASE / 'office_20261026_a_v7'
OUT = ROOT / 'boundary-diagnostic'
MARKET_ANCHOR = 'db2958920a12707ad3bbf6aa5b10cc0a483e2e257205cc4daccd5310b9952da3'


def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def save(name, records):
    assert records
    with (OUT / name).open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(records[0])); w.writeheader(); w.writerows(records)


def freeze_database(codes):
    sys.path.insert(0, str(OFFICE / 'scripts'))
    from backfill_ledger.query import connect
    queries = {
        'prices.csv': ('''SELECT ts_code,trade_date,close,pre_close,volume,amount,data_source,available_at,updated_at
            FROM stock_selector.stock_daily_price WHERE ts_code=ANY(%s)
            AND trade_date > %s AND trade_date <= %s ORDER BY ts_code,trade_date''',
            (codes, '2025-12-15', '2026-06-23')),
        'status.csv': ('''SELECT ts_code,trade_date,is_suspended,updated_at
            FROM stock_selector.stock_status WHERE ts_code=ANY(%s)
            AND trade_date BETWEEN %s AND %s ORDER BY ts_code,trade_date''',
            (codes, '2026-06-08', '2026-06-23')),
        'suspensions.csv': ('''SELECT ts_code,trade_date,suspend_type,suspend_reason,updated_at
            FROM stock_selector.stock_suspension WHERE ts_code=ANY(%s)
            AND trade_date BETWEEN %s AND %s ORDER BY ts_code,trade_date''',
            (codes, '2026-06-08', '2026-06-23')),
    }
    snapshots = []
    for turn in range(2):
        c = connect(readonly=True)
        c.set_session(readonly=True, isolation_level='REPEATABLE READ')
        check = {}
        with c.cursor() as q:
            q.execute("SET LOCAL statement_timeout='60s'")
            q.execute("SET LOCAL work_mem='16MB'")
            q.execute("SELECT current_setting('transaction_read_only'),now()::text")
            ro, clock = q.fetchone(); assert ro == 'on'
            for name, (sql, params) in queries.items():
                q.execute(sql, params)
                columns = [r.name for r in q.description]
                rows = [dict(zip(columns, r)) for r in q.fetchall()]
                digest = hashlib.sha256(json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()
                check[name] = dict(rows=len(rows), all_fields_digest=digest)
                if turn == 0: save(name, rows)
                else: assert check[name] == snapshots[0]['files'][name], 'Readback drift'
        c.close()
        snapshots.append(dict(readonly=True, captured_at=clock, files=check))
    (OUT / 'database-readback.json').write_text(json.dumps(snapshots, indent=2) + '\n')


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--market-inputs', type=Path, required=True,
                        help='Frozen first-run market CSV directory; no live DB fallback')
    args = parser.parse_args()
    OUT = args.output_dir.resolve()
    OUT.mkdir(exist_ok=False)
    source = V4 / 'sources/style_timing_signal/backtest/pure_style_builder.py'
    names = {'_logistic_prob', 'style_probabilities', '_cap_weights', 'select_legs', '_leg_returns'}
    nodes = [n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    env = dict(pd=pd, np=np, FLOAT_QUANTILES=(.25,.5,.75), WEIGHT_CAP=.15, TOP5_CAP=.60)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), 'exec'), env)
    weights, records, inputs = {}, [], {str(source): sha(source)}
    for mother in ('932000.CSI', '000905.SH'):
        for effective, review in [('2025-12-15','2025-10-31'), ('2026-06-15','2026-04-30')]:
            file = V7 / f'factor-freeze/{effective}-{review}-original_inventory-{mother}-scores.csv'
            inputs[str(file)] = sha(file)
            scores = pd.read_csv(file, index_col='ts_code')
            scores = scores.loc[scores.included_in_original_sample]
            probability = env['style_probabilities'](scores[['value_score','growth_score','circ_mv','industry']])
            growth, value = env['select_legs'](probability, mother == '932000.CSI')
            for side, values in [('growth',growth), ('value',value)]:
                assert len(values) and np.isclose(values.sum(), 1)
                weights[(mother, effective, side)] = values
                records.extend(dict(mother_index=mother, effective_date=effective, review_date=review,
                    side=side, ts_code=code, initial_model_weight=format(v,'.17g')) for code,v in values.items())
    save('fixed-model-weights.csv', records)
    codes = sorted({r['ts_code'] for r in records})
    assert sha(args.market_inputs/'market-input-manifest.json') == MARKET_ANCHOR
    market_manifest = json.loads((args.market_inputs/'market-input-manifest.json').read_text())
    assert set(market_manifest) == {'prices.csv','status.csv','suspensions.csv'}
    for name, item in market_manifest.items():
        source_file = args.market_inputs/name
        assert source_file.stat().st_size == item['bytes'] and sha(source_file) == item['sha256']
        shutil.copyfile(source_file, OUT/name)
    shutil.copyfile(__file__,OUT/'audit_script.py')
    prices = pd.read_csv(OUT / 'prices.csv', parse_dates=['trade_date'])
    valid = prices[prices.close.notna() & (prices.pre_close > 0) & (prices.volume > 0)].copy()
    valid['return'] = valid.close / valid.pre_close - 1
    wide = valid.pivot(index='trade_date', columns='ts_code', values='return').sort_index()
    env['_daily_returns'] = lambda selected, start, end: wide.loc[(wide.index > start) & (wide.index <= end)].reindex(columns=selected).dropna(how='all')
    original = env['_leg_returns']
    begin, cut, prior, end = map(pd.Timestamp, ['2025-12-15','2026-06-15','2026-06-12','2026-06-23'])
    differences, summaries, contributions, imputed = [], [], [], []
    status = {(r.ts_code,r.trade_date):r for r in pd.read_csv(OUT/'status.csv',parse_dates=['trade_date']).itertuples()}
    suspension = {(r.ts_code,r.trade_date) for r in pd.read_csv(OUT/'suspensions.csv',parse_dates=['trade_date']).itertuples()}
    raw = {(r.ts_code, r.trade_date): r for r in prices.itertuples()}

    def independent(values, start, finish, mother, side, arm, segment):
        # Decimal per-security compounding independently verifies numpy drift.
        position = {code: D(format(w, '.17g')) for code,w in values.items()}
        results = {}
        for day in wide.index[(wide.index > start) & (wide.index <= finish)]:
            total = sum(position.values())
            returns, contrib = {}, {}
            for code in position:
                row = raw.get((code, day))
                ok = row is not None and pd.notna(row.close) and row.pre_close > 0 and row.volume > 0
                r = D(str(row.close))/D(str(row.pre_close))-1 if ok else D(0)
                returns[code] = r
                contrib[code] = position[code]/total*r
                if day >= pd.Timestamp('2026-06-08') and not ok:
                    s = status.get((code,day))
                    imputed.append(dict(mother_index=mother, side=side, arm=arm, segment=segment,
                        ts_code=code, trade_date=day.date().isoformat(), starting_weight=str(position[code]/total),
                        price_row_present=row is not None, status_is_suspended=None if s is None else s.is_suspended,
                        suspension_record=(code,day) in suspension, original_return_imputation='0'))
                if day == cut:
                    contributions.append(dict(mother_index=mother, side=side, arm=arm, segment=segment,
                        ts_code=code, starting_weight=str(position[code]/total),
                        original_formula_return=str(r), weighted_contribution=str(contrib[code])))
            results[day] = float(sum(contrib.values()))
            position = {code: position[code]*(1+returns[code]) for code in position}
        return pd.Series(results)

    for mother in ('932000.CSI', '000905.SH'):
        for side in ('growth','value'):
            old, new = weights[(mother,'2025-12-15',side)], weights[(mother,'2026-06-15',side)]
            arms = {}
            for arm, old_end, new_start in [('original',cut,cut), ('boundary_only',prior,prior)]:
                pieces = []
                for segment, w, start, finish in [('old',old,begin,old_end),('new',new,new_start,end)]:
                    result = original(w, start, finish)
                    independent_result = independent(w,start,finish,mother,side,arm,segment)
                    assert result.index.equals(independent_result.index)
                    assert np.allclose(result, independent_result, atol=2e-13, rtol=1e-11)
                    pieces.append(result)
                series = pd.concat(pieces).sort_index()
                assert not series.index.duplicated().any() and end in series.index
                arms[arm] = series
            joined = pd.concat(arms, axis=1)
            assert joined.notna().all().all()
            assert np.array_equal(joined.loc[joined.index < cut,'original'], joined.loc[joined.index < cut,'boundary_only'])
            window = joined.loc[joined.index >= pd.Timestamp('2026-06-08')]
            assert len(window) == 11
            for day,row in window.iterrows():
                differences.append(dict(mother_index=mother, side=side, trade_date=day.date().isoformat(),
                    original_return=row.original, boundary_only_return=row.boundary_only,
                    delta_basis_points=(row.boundary_only-row.original)*10000,
                    original_owner='old' if day <= cut else 'new', corrected_owner='old' if day < cut else 'new'))
            summaries.append(dict(mother_index=mother,side=side,old_model_members=len(old),new_model_members=len(new),
                boundary_day_delta_bp=(window.loc[cut,'boundary_only']-window.loc[cut,'original'])*10000,
                max_later_drift_delta_bp=float(((window.loc[window.index>cut,'boundary_only']-window.loc[window.index>cut,'original'])*10000).abs().max())))
    save('daily-boundary-comparison.csv',differences)
    save('boundary-day-contributions.csv',contributions)
    save('summary.csv',summaries)
    if imputed: save('original-zero-imputation-exposure.csv',imputed)
    for file,digest in inputs.items(): assert sha(Path(file)) == digest
    report=dict(status='PASS', model_boundary_diagnostic_ready=True, official_attribution_ready=False,
        first_run=False, replay_of_frozen_first_run=True, market_manifest_sha256=MARKET_ANCHOR,
        fixed_weight_sha256=sha(OUT/'fixed-model-weights.csv'), source_hashes=inputs,
        rows_compared=len(differences), contribution_rows=len(contributions), imputation_rows=len(imputed),
        non_suspension_imputation_rows=sum(not r['suspension_record'] and r['status_is_suspended'] != True for r in imputed),
        imputation_rows_with_status_false=sum(r['status_is_suspended'] == False for r in imputed),
        boundary_day_imputation_rows=sum(r['trade_date'] == '2026-06-15' for r in imputed),
        independent_decimal_verification=True, prefix_exactly_equal=True, terminal_included=True,
        shared_database_accessed=False, market_snapshot_recorded_readonly=True, paid_calls=0, production_changed=False,
        limitations=['Model baskets and weights are reconstructed, not official weights.',
                    'Original close/pre_close formula and zero-imputation are held fixed; not certified economic returns.',
                    'Current frozen financial revisions and observed member lists are not certified ex-ante.',
                    'No full-window correlation or official Q attribution computed.'], summary=summaries)
    (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    manifest = dict(status='complete', scope='offline_fixed_model_boundary_diagnostic',
        official_attribution_ready=False, input_market_manifest_sha256=MARKET_ANCHOR,
        files=[dict(path=p.name,bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(OUT.iterdir()) if p.is_file()])
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for file in OUT.iterdir(): file.chmod(0o444)
    OUT.chmod(0o555)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
