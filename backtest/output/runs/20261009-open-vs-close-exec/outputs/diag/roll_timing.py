"""Outside-spec: on ledger roll days (main(t-1) != main(t-2)), intraday return of new vs old contract, x position."""
import numpy as np, pandas as pd
from scipy import stats
f = pd.read_csv('inputs/futures.csv', parse_dates=['date']).sort_values(['date', 'symbol'])
pos = pd.read_csv('inputs/equal_weight_symmetric.csv', parse_dates=['date']).set_index('date').position.astype(float)
rows = []
for g in ('IC', 'IM'):
    x = f[f.symbol.str.startswith(g)]
    main = x.loc[x.groupby('date').oi.idxmax()].set_index('date').symbol
    op = x.pivot(index='date', columns='symbol', values='open'); cl = x.pivot(index='date', columns='symbol', values='close')
    d = main.index
    for k in range(2, len(d)):
        t, t1, t2 = d[k], d[k - 1], d[k - 2]
        old, new = main[t2], main[t1]
        if old == new or pd.isna(op.at[t, old]) or pd.isna(op.at[t, new]):
            continue
        w = 1.0 if (g == 'IC' and t <= pd.Timestamp('2022-07-22')) else 0.5
        i_old = cl.at[t, old] / op.at[t, old] - 1; i_new = cl.at[t, new] / op.at[t, new] - 1
        p_hold = pos.iloc[pos.index.get_loc(t) - 2]   # position held into day t under T+1 execution
        rows.append({'date': t, 'g': g, 'w': w, 'p': p_hold, 'spread_intra': i_new - i_old})
r = pd.DataFrame(rows)
r['contrib'] = r.w * r.p * r.spread_intra
yrs = 2788 / 245
print(f'roll days {len(r)} (IC {int((r.g=="IC").sum())}, IM {int((r.g=="IM").sum())}); long {int((r.p>0).sum())} short {int((r.p<0).sum())}')
print(f'mean(new-old intraday) {r.spread_intra.mean()*1e4:+.2f}bp  t={stats.ttest_1samp(r.spread_intra,0).statistic:.2f}')
for s, nm in [(1, 'long'), (-1, 'short')]:
    z = r[r.p == s]
    print(f'  {nm}: n={len(z)} mean spread_intra {z.spread_intra.mean()*1e4:+.2f}bp, sum contrib {z.contrib.sum()*100:+.2f}pp')
print(f'sum w*p*(new-old) {r.contrib.sum()*100:+.2f}pp total = {r.contrib.sum()*100/yrs:+.3f}pp/yr')
