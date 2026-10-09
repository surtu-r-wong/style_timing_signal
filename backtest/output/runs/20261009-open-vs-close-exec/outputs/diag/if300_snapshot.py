"""Read-only: IF futures + 000300.SH index OHLC for the proxy-bias diagnostic (outside frozen spec)."""
import sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, '.')
from backtest.data import _connect
from signals.common.config import load_db_config
OUT = Path(sys.argv[1])
conn = _connect(load_db_config())
try:
    conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
    with conn.cursor() as q:
        q.execute("SET LOCAL statement_timeout='120s'")
        q.execute("""SELECT trade_date, symbol, open, close, oi FROM public.futures_daily
                     WHERE symbol LIKE 'IF%%' AND trade_date BETWEEN '2015-01-01' AND '2026-10-08' ORDER BY 1, 2""")
        f = pd.DataFrame(q.fetchall(), columns=['date', 'symbol', 'open', 'close', 'oi'])
        q.execute("""SELECT trade_date, open, close FROM stock_selector.index_daily
                     WHERE index_code = '000300.SH' AND trade_date BETWEEN '2015-01-01' AND '2026-10-08' ORDER BY 1""")
        i = pd.DataFrame(q.fetchall(), columns=['date', 'open', 'close'])
    conn.rollback()
finally:
    conn.close()
f.to_csv(OUT/'if_futures.csv', index=False); i.to_csv(OUT/'csi300.csv', index=False)
print(len(f), f.date.min(), f.date.max(), '|', len(i), i.date.min(), i.date.max(), int((pd.to_numeric(i.open) > 0).sum()))
