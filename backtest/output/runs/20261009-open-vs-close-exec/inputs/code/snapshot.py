"""Freeze read-only inputs for the open-vs-close execution check (2026-10-09)."""
import hashlib, json, shutil, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, '.')
from backtest.data import _connect
from signals.common.config import load_db_config

OUT = Path(sys.argv[1]); END = '2026-10-08'
conn = _connect(load_db_config())
try:
    conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
    with conn.cursor() as q:
        q.execute("SET LOCAL statement_timeout='120s'")
        q.execute('SELECT transaction_timestamp()::text'); stamp = q.fetchone()[0]
        q.execute("""SELECT trade_date, symbol, open, high, low, close, settle, pre_settle, pre_close, oi, volume
                     FROM public.futures_daily WHERE (symbol LIKE 'IC%%' OR symbol LIKE 'IM%%') AND trade_date <= %s
                     ORDER BY 1, 2""", (END,))
        fut = pd.DataFrame(q.fetchall(), columns=['date','symbol','open','high','low','close','settle','pre_settle','pre_close','oi','volume'])
        q.execute("""SELECT trade_date, index_code, open, close FROM stock_selector.index_daily
                     WHERE index_code IN ('000905.SH','000852.SH') AND trade_date BETWEEN '2014-01-01' AND %s
                     ORDER BY 1, 2""", (END,))
        spot = pd.DataFrame(q.fetchall(), columns=['date','symbol','open','close'])
    conn.rollback()
finally:
    conn.close()
fut.to_csv(OUT/'futures.csv', index=False); spot.to_csv(OUT/'spot.csv', index=False)
for src in ['output/recommended/equal_weight_symmetric.csv', 'output/recommended/slope20_longflat.csv',
            'output/equal_weight/equal_weight_signal_20d40z.csv', 'output/slope20/slope20_signal_L20zw120.csv']:
    shutil.copyfile(src, OUT/Path(src).name)
meta = {'transaction_timestamp': stamp, 'end': END, 'futures_rows': len(fut), 'spot_rows': len(spot),
        'spot_open_valid': int(pd.to_numeric(spot.open, errors='coerce').gt(0).sum()),
        'note': 'position/signal CSVs copied from working tree (uncommitted daily updates through 2026-10-08)',
        'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.glob('*.csv'))}}
(OUT/'snapshot.json').write_text(json.dumps(meta, indent=2))
print(json.dumps({k: v for k, v in meta.items() if k != 'sha256'}, indent=1))
