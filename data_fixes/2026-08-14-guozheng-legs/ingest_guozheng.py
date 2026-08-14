#!/usr/bin/env python3
"""Probe 7 data prerequisite: ingest Wind-exported index CSV into stock_selector.index_daily.

Source: /home/elfbob/exchange/20260814/国证.csv (UTF-8 BOM, 3 groups side by side,
groups at columns 0/8/16, codes 399370.SZ / 399371.SZ / 932000.CSI).

Subcommands:
  reconcile  parse + sanity + DB pre-insert reconciliation (incl. 932000.CSI overlap
             bit-compare). Read-only.
  insert     re-runs reconcile logic, then inserts only rows missing from DB with
             ON CONFLICT (index_code, trade_date) DO NOTHING, one transaction per code.
             932000.CSI is skipped entirely if any overlap close differs.
  verify     post-insert verification: per-code counts/ranges + missing-day check vs
             000300.SH calendar (from 2014-01-02) and 000852.SH for the 2013 portion
             of 399370/399371.

Run from /home/elfbob/claude-code/style_timing_signal (needs backtest.data).
"""
import csv
import sys
from decimal import Decimal

CSV_PATH = '/home/elfbob/exchange/20260814/国证.csv'
GROUPS = [(0, '399370.SZ'), (8, '399371.SZ'), (16, '932000.CSI')]
DATA_START_ROW = 6  # idx 0-3 metadata, 4 Chinese header, 5 English header


def clean_num(s):
    s = s.strip().replace(',', '')
    if s == '':
        return None
    return Decimal(s)


def parse_csv():
    with open(CSV_PATH, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    # metadata check: row idx 2 = 证券代码
    for base, code in GROUPS:
        assert rows[2][base] == '证券代码' and rows[2][base + 1] == code, \
            f'metadata code mismatch at col {base}: {rows[2][base:base+2]}'
    out = {}
    for base, code in GROUPS:
        recs = []
        for r in rows[DATA_START_ROW:]:
            date = r[base].strip()
            if not date:
                continue
            vals = [clean_num(r[base + j]) for j in range(1, 7)]
            if all(v is None for v in vals):
                continue  # pre-base-date empty row (932000.CSI early years)
            o, h, l, c, v, a = vals
            recs.append((date, o, h, l, c, v, a))
        out[code] = recs
    return out


def sanity(data):
    ok = True
    for code, recs in data.items():
        dates = [r[0] for r in recs]
        assert dates == sorted(dates) and len(dates) == len(set(dates)), \
            f'{code}: dates not strictly increasing/unique'
        bad_close = [r for r in recs if r[4] is None or r[4] <= 0]
        assert not bad_close, f'{code}: {len(bad_close)} rows with close<=0/NULL'
        # volume must be integral (DB column is bigint)
        frac_vol = [r for r in recs if r[5] is not None and r[5] != r[5].to_integral_value()]
        assert not frac_vol, f'{code}: {len(frac_vol)} rows with fractional volume'
        big_moves = []
        for i in range(1, len(recs)):
            prev, cur = recs[i - 1][4], recs[i][4]
            r_ = abs(cur / prev - 1)
            if r_ >= Decimal('0.25'):
                big_moves.append((recs[i][0], float(r_)))
        if big_moves:
            ok = False
            print(f'  SANITY FAIL {code}: |r|>=25% at {big_moves[:5]}')
        print(f'  {code}: {len(recs)} rows, {recs[0][0]} .. {recs[-1][0]}, sanity '
              + ('OK' if not big_moves else 'FAIL'))
    return ok


def connect():
    sys.path.insert(0, '/home/elfbob/claude-code/style_timing_signal')
    from backtest.data import load_db_config, _connect
    conn = _connect(load_db_config())
    cur = conn.cursor()
    cur.execute("SET statement_timeout='120s'")
    return conn, cur


def reconcile(data, cur):
    """Returns (existing_dates_by_code, csi_close_mismatch_count)."""
    existing = {}
    for code in data:
        cur.execute(
            "SELECT trade_date, open, high, low, close, volume, amount "
            "FROM stock_selector.index_daily WHERE index_code=%s ORDER BY trade_date",
            (code,))
        rows = cur.fetchall()
        existing[code] = {str(r[0]): r[1:] for r in rows}
        rng = f'{rows[0][0]} .. {rows[-1][0]}' if rows else '-'
        print(f'  DB {code}: {len(rows)} rows, {rng}')

    csi = '932000.CSI'
    csv_map = {r[0]: r[1:] for r in data[csi]}
    overlap = sorted(set(csv_map) & set(existing[csi]))
    print(f'  overlap {csi}: {len(overlap)} dates'
          + (f' ({overlap[0]} .. {overlap[-1]})' if overlap else ''))
    cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
    close_mismatch = 0
    for ci, col in enumerate(cols):
        eq = 0
        maxdev = Decimal(0)
        worst = None
        for d in overlap:
            a, b = csv_map[d][ci], existing[csi][d][ci]
            if a is None or b is None:
                if a is None and b is None:
                    eq += 1
                continue
            dev = abs(Decimal(a) - Decimal(b))
            if dev == 0:
                eq += 1
            elif dev > maxdev:
                maxdev, worst = dev, d
        print(f'  overlap col {col:6s}: equal {eq}/{len(overlap)}, max_abs_dev '
              f'{maxdev}' + (f' at {worst}' if worst else ''))
        if col == 'close':
            close_mismatch = len(overlap) - eq
    return existing, close_mismatch


def do_insert(data, conn, cur, existing, csi_blocked):
    for code, recs in data.items():
        if code == '932000.CSI' and csi_blocked:
            print(f'  SKIP {code}: overlap close mismatch, NOT inserting')
            continue
        new = [r for r in recs if r[0] not in existing[code]]
        cur.executemany(
            "INSERT INTO stock_selector.index_daily "
            "(index_code, trade_date, open, high, low, close, volume, amount) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (index_code, trade_date) DO NOTHING",
            [(code, r[0], r[1], r[2], r[3], r[4],
              int(r[5]) if r[5] is not None else None, r[6]) for r in new])
        conn.commit()
        print(f'  INSERT {code}: {len(new)} candidate new rows committed '
              f'(of {len(recs)} parsed)')


def verify(cur):
    for code in ('399370.SZ', '399371.SZ', '932000.CSI'):
        cur.execute(
            "SELECT count(*), min(trade_date), max(trade_date) "
            "FROM stock_selector.index_daily WHERE index_code=%s", (code,))
        print(f'  {code}: {cur.fetchone()}')
    # reference calendars
    def cal(ref, lo, hi):
        cur.execute(
            "SELECT trade_date FROM stock_selector.index_daily "
            "WHERE index_code=%s AND trade_date BETWEEN %s AND %s", (ref, lo, hi))
        return {str(r[0]) for r in cur.fetchall()}

    def have(code, lo, hi):
        cur.execute(
            "SELECT trade_date FROM stock_selector.index_daily "
            "WHERE index_code=%s AND trade_date BETWEEN %s AND %s", (code, lo, hi))
        return {str(r[0]) for r in cur.fetchall()}

    ref300 = cal('000300.SH', '2014-01-02', '2026-08-14')
    ref852 = cal('000852.SH', '2013-01-01', '2013-12-31')
    print(f'  ref 000300.SH 2014-01-02..2026-08-14: {len(ref300)} days; '
          f'ref 000852.SH 2013: {len(ref852)} days')
    for code in ('399370.SZ', '399371.SZ', '932000.CSI'):
        miss = sorted(ref300 - have(code, '2014-01-02', '2026-08-14'))
        print(f'  {code} vs 000300.SH cal (2014-01-02+): missing {len(miss)}'
              + (f', sample {miss[:5]}' + (' ...' if len(miss) > 5 else '') if miss else ''))
    for code in ('399370.SZ', '399371.SZ'):
        miss = sorted(ref852 - have(code, '2013-01-01', '2013-12-31'))
        print(f'  {code} vs 000852.SH cal (2013): missing {len(miss)}'
              + (f', sample {miss[:5]}' + (' ...' if len(miss) > 5 else '') if miss else ''))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'reconcile'
    print(f'== phase: {cmd} ==')
    if cmd == 'verify':
        conn, cur = connect()
        verify(cur)
        conn.close()
        return
    print('-- parse + sanity --')
    data = parse_csv()
    if not sanity(data):
        print('SANITY FAILED, aborting')
        sys.exit(1)
    conn, cur = connect()
    print('-- pre-insert reconciliation --')
    existing, close_mismatch = reconcile(data, cur)
    if cmd == 'insert':
        print('-- insert --')
        do_insert(data, conn, cur, existing, csi_blocked=close_mismatch > 0)
    conn.close()


if __name__ == '__main__':
    main()
