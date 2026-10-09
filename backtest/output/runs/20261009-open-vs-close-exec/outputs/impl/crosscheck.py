#!/usr/bin/env python3
"""规格外：独立代码路径复算 key_numbers 的主要数字（不 import run.py，不用 held_frame / policy 函数）。

python3 crosscheck.py  → 打印与 key_numbers.json 的最大绝对差。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/elfbob/claude-code/style_timing_signal')
from backtest.metrics import ann_return, max_drawdown, sharpe  # noqa: E402

W = Path(__file__).resolve().parent
I = W.parent / 'inputs'
fut = pd.read_csv(I / 'futures.csv', parse_dates=['date'])
spot = pd.read_csv(I / 'spot.csv', parse_dates=['date'])
dec_f = pd.read_csv(I / 'equal_weight_symmetric.csv', parse_dates=['date']).set_index('date').position.astype(float)
dec_s = pd.read_csv(I / 'slope20_longflat.csv', parse_dates=['date']).set_index('date').position.astype(float)
C = 3e-4


def group_frame(g):
    ff = fut[fut.symbol.str.startswith(g)]
    O = ff.pivot(index='date', columns='symbol', values='open')
    Cl = ff.pivot(index='date', columns='symbol', values='close')
    OI = ff.pivot(index='date', columns='symbol', values='oi')
    main = OI.fillna(-1).idxmax(axis=1)            # 列按 symbol 升序 → 平局取字典序最小
    held = main.shift(1).dropna()
    d = held.index
    cols = Cl.columns.get_indexer(held.values)
    ri = Cl.index.get_indexer(d)
    o, c, pc = O.values[ri, cols], Cl.values[ri, cols], Cl.values[ri - 1, cols]
    out = pd.DataFrame({'sym': held.values, 'gap': o / pc - 1, 'intra': c / o - 1, 'cc': c / pc - 1}, index=d)
    out['roll'] = out.sym.ne(out.sym.shift(1)) & out.sym.shift(1).notna()
    return out


ic, im = group_frame('IC'), group_frame('IM')
days = ic.index[ic.index <= '2026-10-08']
first_im = fut[fut.symbol.str.startswith('IM')].date.min()
wim = pd.Series(np.where(days > first_im, .5, 0.), index=days)
wic = 1 - wim
imr = im.reindex(days)
for c in ('gap', 'intra', 'cc'):
    imr[c] = imr[c].fillna(0.)
imr['roll'] = imr['roll'].eq(True)
pos = dec_f.index.get_indexer(days)
p_old = pd.Series(dec_f.values[pos - 2], index=days)
p_new = pd.Series(dec_f.values[pos - 1], index=days)
dlt = p_new - p_old
up = lambda x: x.clip(lower=0)
legs = {'开多': (up(p_new) - up(p_old)).clip(lower=0), '平多': -(up(p_old) - up(p_new)).clip(lower=0),
        '开空': -(up(-p_new) - up(-p_old)).clip(lower=0), '平空': (up(-p_old) - up(-p_new)).clip(lower=0)}
tc = C * dlt.abs()
roll = lambda p: 2 * C * p.abs() * (wic * ic.roll.reindex(days).astype(float) + wim * imr.roll.astype(float))


def M(pm):
    g = wic * ((1 + p_old * ic.gap.reindex(days)) * (1 + pm * ic.intra.reindex(days)) - 1) \
        + wim * ((1 + p_old * imr.gap) * (1 + pm * imr.intra) - 1)
    return g - tc - roll(p_old)


R = {'E0': wic * p_new * ic.cc.reindex(days) + wim * p_new * imr.cc - tc - roll(p_new),
     'E1': M(p_old), 'E2': M(p_new)}
ib = wic * ic.intra.reindex(days) + wim * imr.intra
gb = wic * ic.gap.reindex(days) + wim * imr.gap
kn = json.loads((W / 'key_numbers.json').read_text())
diffs = []
for p in ('E0', 'E1', 'E2'):
    for k, f in (('ann', ann_return), ('sharpe', sharpe), ('maxdd', max_drawdown)):
        diffs.append((f'F {p} {k}', f(R[p]) - kn['F']['policies_full'][p][k]))
for leg, d in legs.items():
    m = d != 0
    e, o = d[m] * ib[m], d[m] * gb[m]
    ref = kn['F']['legs_full'][leg]
    diffs += [(f'F {leg} n', int(m.sum()) - ref['n']), (f'F {leg} sum_e_pp', e.sum() * 100 - ref['sum_e_pp']),
              (f'F {leg} mean_e_bp', e.mean() * 1e4 - ref['mean_e_bp']), (f'F {leg} mean_o_bp', o.mean() * 1e4 - ref['mean_o_bp'])]

# ---------------- S-proxy
cl = spot.pivot(index='date', columns='symbol', values='close')
op = spot.pivot(index='date', columns='symbol', values='open')
sd = cl.index[(cl.index >= '2016-01-04') & (cl.index <= '2026-10-08')]
cc = cl.pct_change().reindex(sd)
px905 = ic.intra.reindex(sd)
px852 = im.intra.reindex(sd).where(sd.isin(im.index), ic.intra.reindex(sd))
pos = dec_s.index.get_indexer(sd)
po = pd.Series(dec_s.values[pos - 2], index=sd)
pn = pd.Series(dec_s.values[pos - 1], index=sd)
g905 = (1 + cc['000905.SH']) / (1 + px905) - 1
g852 = (1 + cc['000852.SH']) / (1 + px852) - 1


def MS(pm):
    return 0.5 * ((1 + po * g905) * (1 + pm * px905) - 1) + 0.5 * ((1 + po * g852) * (1 + pm * px852) - 1) - C * (pn - po).abs()


RS = {'E0': pn * cc.mean(axis=1) - C * (pn - po).abs(), 'E1': MS(po), 'E2': MS(pn)}
for p in ('E0', 'E1', 'E2'):
    for k, f in (('ann', ann_return), ('sharpe', sharpe), ('maxdd', max_drawdown)):
        diffs.append((f'S {p} {k}', f(RS[p]) - kn['S']['policies_full'][p][k]))
ibs = 0.5 * (px905 + px852)
for leg, sgn in (('开多', 1), ('平多', -1)):
    m = (pn - po) == sgn
    e = sgn * ibs[m]
    diffs += [(f'S {leg} n', int(m.sum()) - kn['S']['legs_full'][leg]['n']),
              (f'S {leg} sum_e_pp', e.sum() * 100 - kn['S']['legs_full'][leg]['sum_e_pp'])]
ok = (op > 0).all(axis=1)
okd = ok[ok].index
real = (cl.loc[okd] / op.loc[okd] - 1).mean(axis=1)
prox = 0.5 * (ic.intra.reindex(okd) + im.intra.reindex(okd))
diffs += [('S proxy corr', np.corrcoef(real, prox)[0, 1] - kn['S']['proxy_validation']['corr_blend_intra']),
          ('S proxy mean_diff_bp', (real - prox).mean() * 1e4 - kn['S']['proxy_validation']['mean_diff_bp']),
          ('S proxy rmse_bp', np.sqrt(((real - prox) ** 2).mean()) * 1e4 - kn['S']['proxy_validation']['rmse_bp'])]
worst = max(diffs, key=lambda x: abs(x[1]))
print(f'{len(diffs)} 项比对；最大绝对差 {worst[0]} = {worst[1]:.3e}')
bad = [d for d in diffs if abs(d[1]) > 1e-9]
print('超过 1e-9 的项：', bad if bad else '无')
