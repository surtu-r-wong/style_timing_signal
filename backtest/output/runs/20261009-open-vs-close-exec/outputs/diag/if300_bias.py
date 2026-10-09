"""Outside-spec diagnostic: is a futures-intraday proxy biased on slope20 event days? Use IF vs real CSI300 opens."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
from scipy import stats
D = Path(sys.argv[1])
f = pd.read_csv(D/'diag/if_futures.csv', parse_dates=['date']).sort_values(['date', 'symbol'])
i = pd.read_csv(D/'diag/csi300.csv', parse_dates=['date']).set_index('date')
pos = pd.read_csv(D/'inputs/slope20_longflat.csv', parse_dates=['date']).set_index('date').position.astype(float)

main = f.loc[f.groupby('date').oi.idxmax()].set_index('date').symbol  # idxmax keeps first = smallest symbol on ties
op = f.pivot(index='date', columns='symbol', values='open'); cl = f.pivot(index='date', columns='symbol', values='close')
days = [d for d in pos.index if d >= pd.Timestamp('2016-01-04') and d in op.index and d in i.index]
rows = []
for t in days:
    prev = op.index[op.index.get_loc(t) - 1]
    sym = main.loc[prev]
    o, c = op.at[t, sym], cl.at[t, sym]
    rows.append({'date': t, 'intra_if': c / o - 1, 'intra_300': i.at[t, 'close'] / i.at[t, 'open'] - 1})
x = pd.DataFrame(rows).set_index('date')
k = pos.index.get_indexer(x.index)
x['p_old'] = pos.iloc[k - 2].values; x['p_new'] = pos.iloc[k - 1].values
x['delta'] = x.p_new - x.p_old
x['diff'] = x.intra_if - x.intra_300
print(f'days {len(x)} {x.index[0].date()}~{x.index[-1].date()}; corr(intra_if,intra_300)={x.intra_if.corr(x.intra_300):.4f}')
print(f'unconditional mean diff (IF-300 intra) {x["diff"].mean()*1e4:+.2f}bp  t={stats.ttest_1samp(x["diff"],0).statistic:.2f}')
v = x.loc['2026-07-01':]
print(f'2026-07-01+ ({len(v)}d): mean diff {v["diff"].mean()*1e4:+.2f}bp; first33 {v["diff"].iloc[:33].mean()*1e4:+.2f}bp; last33 {v["diff"].iloc[33:].mean()*1e4:+.2f}bp')
for name, sign in [('开多', 1), ('平多', -1)]:
    ev = x[x.delta == sign]
    e_real, e_if = sign * ev.intra_300, sign * ev.intra_if
    d = e_if - e_real
    print(f'{name}: n={len(ev)} e_real300 mean {e_real.mean()*1e4:+.2f}bp (t={stats.ttest_1samp(e_real,0).statistic:.2f}) | '
          f'e_IF mean {e_if.mean()*1e4:+.2f}bp | proxy-real {d.mean()*1e4:+.2f}bp (t={stats.ttest_1samp(d,0).statistic:.2f})')
ev = x[x.delta != 0]
print(f'both legs: sum e_real300 {(ev.delta*ev.intra_300).sum()*100:+.2f}pp, sum e_IF {(ev.delta*ev.intra_if).sum()*100:+.2f}pp')
