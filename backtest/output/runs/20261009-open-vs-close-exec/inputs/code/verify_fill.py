"""Our own acceptance of the office open fill: compare against our 08:41 pre-write snapshot; freeze a v2 spot snapshot."""
import hashlib, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from backtest.data import _connect
from signals.common.config import load_db_config
S = Path(sys.argv[1])
conn = _connect(load_db_config())
try:
    conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
    with conn.cursor() as q:
        q.execute("SET LOCAL statement_timeout='60s'")
        q.execute('SELECT transaction_timestamp()::text'); stamp = q.fetchone()[0]
        q.execute("""SELECT trade_date, index_code, open, high, low, close, volume, amount, updated_at
                     FROM stock_selector.index_daily WHERE index_code IN ('000905.SH','000852.SH')
                     AND trade_date BETWEEN '2013-01-01' AND '2026-10-08' ORDER BY 1, 2""")
        now = pd.DataFrame(q.fetchall(), columns=['date','symbol','open','high','low','close','volume','amount','updated_at'])
    conn.rollback()
finally:
    conn.close()
now['date'] = pd.to_datetime(now['date'])
for c in ('open','high','low','close','volume','amount'):
    now[c] = pd.to_numeric(now[c], errors='coerce')
old = pd.read_csv(S/'inputs/spot.csv', parse_dates=['date'])
m = old.merge(now, on=['date','symbol'], how='outer', suffixes=('_old',''), indicator=True)
print('snapshot stamp', stamp, '| rows now', len(now), '| join', m['_merge'].value_counts().to_dict())
both = m[m['_merge'] == 'both']
print('close bitwise-equal rows:', int((both.close_old == both.close).sum()), '/', len(both),
      '| max abs diff', float((both.close_old - both.close).abs().max()))
had = both.open_old.notna()
print('pre-existing opens (07-01+) unchanged:', int((both.loc[had, 'open_old'] == both.loc[had, 'open']).sum()), '/', int(had.sum()))
fill = now[now.date <= '2026-06-30']
for sym, g in fill.groupby('symbol'):
    ok = g.open.notna() & (g.open > 0) & (g.low <= g.open) & (g.open <= g.high) & (g.low <= g.close) & (g.close <= g.high)
    print(sym, 'fill rows', len(g), '| open notna', int(g.open.notna().sum()), '| OHLC consistent', int(ok.sum()),
          '| vol>0', int((g.volume > 0).sum()), '| updated_at distinct', g.updated_at.nunique(), str(g.updated_at.iloc[0]))
w = now.pivot(index='date', columns='symbol', values='close')
o = now.pivot(index='date', columns='symbol', values='open')
gap = (o / w.shift(1) - 1).loc['2013-03-07':]
print('|gap| median bp: fill', (gap.loc[:'2026-06-30'].abs().median() * 1e4).round(1).to_dict(),
      '| 07-01+', (gap.loc['2026-07-01':].abs().median() * 1e4).round(1).to_dict())
print('largest |gap| days:', (gap.abs().max(axis=1).sort_values(ascending=False).head(4) * 1e4).round(0).to_dict())
now[['date','symbol','open','close']].to_csv(S/'inputs_v2/spot.csv', index=False)
(S/'inputs_v2/snapshot.json').write_text(json.dumps({'transaction_timestamp': stamp, 'rows': len(now),
    'note': 'post office fill_nulls 2026-10-09 10:37; close verified bitwise vs inputs/spot.csv',
    'sha256': hashlib.sha256((S/'inputs_v2/spot.csv').read_bytes()).hexdigest()}, indent=2))
