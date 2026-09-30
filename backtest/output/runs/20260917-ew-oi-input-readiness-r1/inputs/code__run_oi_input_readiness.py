"""Read-only raw-input inventory; no market aggregates or signal features produced."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect
from backtest.run_manifest import artifact_record, create_run_dir, git_state, write_manifest
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = '20260917-ew-oi-input-readiness-r1'
START, END = '2015-04-16', '2026-09-16'


def capture(run):
    # Never print configuration or driver errors: they can contain connection details.
    conn = _connect(load_db_config())
    try:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute('SELECT transaction_timestamp()::text')
            transaction = q.fetchone()[0]
            q.execute('SELECT trade_date,symbol,open,close,oi,settle,volume FROM public.futures_daily '
                      'WHERE (symbol LIKE %s OR symbol LIKE %s) AND trade_date BETWEEN %s AND %s '
                      'ORDER BY trade_date,symbol', ('IC%', 'IM%', START, END))
            fut = pd.DataFrame(q.fetchall(), columns=['date', 'symbol', 'open', 'close', 'oi', 'settle', 'volume'])
            q.execute('SELECT trade_date,index_code,close FROM stock_selector.index_daily '
                      'WHERE index_code IN (%s,%s) AND trade_date BETWEEN %s AND %s ORDER BY trade_date,index_code',
                      ('000905.SH', '000852.SH', '2015-01-01', END))
            spot = pd.DataFrame(q.fetchall(), columns=['date', 'index_code', 'close'])
            q.execute('SELECT calendar_date,sfe FROM public.trading_calendar '
                      'WHERE calendar_date BETWEEN %s AND %s AND deleted_at IS NULL ORDER BY calendar_date',
                      ('2015-01-01', END))
            calendar = pd.DataFrame(q.fetchall(), columns=['date', 'sfe'])
            q.execute("SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns "
                      "WHERE table_schema IN ('public','stock_selector') AND "
                      "(table_name IN ('futures_daily','index_daily','trading_calendar','etf_constituent','fund_nav',"
                      "'fund_holder_structure') OR table_name ~ '(etf.*share|futures.*member|futures.*rank)') "
                      'ORDER BY table_schema,table_name,ordinal_position')
            columns = pd.DataFrame(q.fetchall(), columns=['schema', 'table', 'column', 'type'])
        conn.rollback()
    finally:
        conn.close()
    for name, df in [('futures', fut), ('spot', spot), ('calendar', calendar), ('columns', columns)]:
        df.to_csv(run/f'inputs/{name}.csv', index=False)
    provenance = {'transaction_timestamp': transaction, 'readonly': True, 'isolation': 'REPEATABLE READ',
                  'statement_timeout_seconds': 30, 'window_end': END,
                  'supplier_calls': 0, 'database_writes': 0, 'derived_market_datasets_created': 0}
    (run/'inputs/provenance.json').write_text(json.dumps(provenance, indent=2))


def audit(run):
    fut = pd.read_csv(run/'inputs/futures.csv', parse_dates=['date'])
    spot = pd.read_csv(run/'inputs/spot.csv', parse_dates=['date'])
    calendar = pd.read_csv(run/'inputs/calendar.csv', parse_dates=['date'])
    sessions = pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1), 'date'])
    if not len(sessions) or not sessions.is_unique:
        raise ValueError('invalid calendar')
    assert not fut.duplicated(['date', 'symbol']).any()
    assert not spot.duplicated(['date', 'index_code']).any()
    coverage, gaps, bad_values, shapes = [], [], [], []
    for product in ['IC', 'IM']:
        d = fut.loc[fut.symbol.str.fullmatch(product+r'\d{4}\.CFE')].copy()
        if d.empty:
            raise ValueError('no exact-symbol futures rows')
        dates = pd.DatetimeIndex(sorted(d.date.unique()))
        expected = sessions[(sessions>=dates.min()) & (sessions<=END)]
        counts = d.groupby('date').symbol.nunique()
        shapes.append(counts.rename('observed_contract_count').reset_index().assign(product=product))
        for date in expected.difference(dates):
            gaps.append({'series': product, 'date': str(date.date()), 'kind': 'missing_entire_product'})
        for date in dates.difference(sessions):
            gaps.append({'series': product, 'date': str(date.date()), 'kind': 'outside_calendar'})
        for col in ['oi', 'volume', 'open', 'close', 'settle']:
            invalid = ~np.isfinite(d[col]) | (d[col].lt(0) if col in ['oi', 'volume'] else d[col].le(0))
            bad_values += [{'series': product, 'date': str(row.date.date()), 'symbol': row.symbol,
                            'field': col, 'value': None if pd.isna(getattr(row, col)) else float(getattr(row, col))}
                           for row in d.loc[invalid].itertuples()]
        coverage.append({'series': product, 'rows': len(d), 'days': len(dates), 'first': str(dates.min().date()),
                         'last': str(dates.max().date()), 'missing_days': len(expected.difference(dates)),
                         'min_contracts': int(counts.min()), 'max_contracts': int(counts.max()),
                         'days_count_not_four': int(counts.ne(4).sum())})
    for code, d in spot.groupby('index_code'):
        dates = pd.DatetimeIndex(d.date)
        expected = sessions[(sessions>=dates.min()) & (sessions<=END)]
        coverage.append({'series': code, 'rows': len(d), 'days': len(dates), 'first': str(dates.min().date()),
                         'last': str(dates.max().date()), 'missing_days': len(expected.difference(dates))})
        for date in expected.difference(dates):
            gaps.append({'series': code, 'date': str(date.date()), 'kind': 'missing_index_close'})
        for row in d.loc[~np.isfinite(d.close) | d.close.le(0)].itertuples():
            bad_values.append({'series': code, 'date': str(row.date.date()), 'field': 'close',
                               'value': None if pd.isna(row.close) else float(row.close)})
    pd.DataFrame(coverage).to_csv(run/'outputs/coverage.csv', index=False)
    pd.concat(shapes, ignore_index=True).to_csv(run/'outputs/contract_counts.csv', index=False)
    pd.DataFrame(gaps, columns=['series', 'date', 'kind']).to_csv(run/'outputs/gaps.csv', index=False)
    pd.DataFrame(bad_values, columns=['series', 'date', 'symbol', 'field', 'value']).to_csv(run/'outputs/invalid_values.csv', index=False)
    result = {'read_only_snapshot_complete': True, 'duplicate_keys': 0, 'invalid_value_cells': len(bad_values),
              'coverage_gaps': len(gaps), 'rows': len(fut),
              'observed_count_is_not_proof_of_listed_contract_completeness': True,
              'office_delivery_required_before_feature_research': True,
              'finished_utc': datetime.now(timezone.utc).isoformat()}
    (run/'outputs/verification.json').write_text(json.dumps(result, indent=2))
    print(pd.DataFrame(coverage).to_string(index=False), flush=True)
    print(json.dumps(result), flush=True)
    return result


def main():
    run = create_run_dir(ROOT/'backtest/output/runs', RUN_ID)
    for rel in ['backtest/run_oi_input_readiness.py', 'backtest/data.py', 'backtest/run_manifest.py']:
        shutil.copyfile(ROOT/rel, run/'inputs'/('code__'+Path(rel).name))
    manifest = {'run_id': RUN_ID, 'status': 'running', 'git': git_state(ROOT)}
    write_manifest(run, manifest)
    try:
        capture(run)
        result = audit(run)
    except Exception as exc:
        manifest.update(status='failed', error_type=type(exc).__name__)
        write_manifest(run, manifest)
        print('Readiness failed; sanitized exception type:', type(exc).__name__)
        raise SystemExit(1) from None
    manifest.update(status='computed_pending_report', verification=result,
                    artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*')) if p.is_file() and p.name!='manifest.json'])
    write_manifest(run, manifest)


if __name__ == '__main__':
    main()
