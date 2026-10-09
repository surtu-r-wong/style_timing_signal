#!/usr/bin/env python3
"""Independent QA for the S-exact addendum (spot pool with real index opens), 2026-10-09.

Reuses only my own qa.py (held contracts / proxy / legs / product formula) and the house
metrics + paired bootstrap + run_strategy.  Never reads openexec/impl/.

Run from the repo root:
    python3 /tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/8ebe6247-4dcd-4083-a029-e12de7a53103/scratchpad/openexec/qa/qa_s_exact.py
Writes: key_numbers_s_exact.json, checks_s_exact.json, events_S_exact_qa.csv, daily_S_exact_qa.csv
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sst

QDIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('qa_base', QDIR / 'qa.py')
qa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qa)                      # also puts the repo on sys.path

from backtest.metrics import ann_return, sharpe, max_drawdown  # noqa: E402
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402

V2 = qa.BASE / 'inputs_v2'
END = '2026-10-08'
MAIN = ('2016-01-04', END)
EXT = ('2014-01-06', END)
SUBW = {'2014-2015': ('2014-01-06', '2015-12-31'), '2016-2020': ('2016-01-04', '2020-12-31'),
        '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', END)}
POLS = {'E1': (), 'E2': ('开多', '平多'), 'M_开多': ('开多',), 'M_平多': ('平多',)}
IDX = ('000905.SH', '000852.SH')
C1 = 3e-4


def load_v2():
    meta = json.loads((V2 / 'snapshot.json').read_text())
    digest = hashlib.sha256((V2 / 'spot.csv').read_bytes()).hexdigest()
    if digest != meta['sha256']:
        raise SystemExit('inputs_v2/spot.csv sha256 mismatch')
    raw2 = pd.read_csv(V2 / 'spot.csv', dtype=str)
    raw1 = pd.read_csv(qa.INP / 'spot.csv', dtype=str)
    m = raw1.merge(raw2, on=['date', 'symbol'], how='left', suffixes=('_1', '_2'))
    chk = {'sha256_ok': True, 'rows': int(len(raw2)), 'v1_rows_missing_in_v2': int(m.close_2.isna().sum()),
           'close_numeric_equal_v1': bool((m.close_1.map(Decimal) == m.close_2.map(Decimal)).all()),
           'close_string_equal_v1_rows': int((m.close_1 == m.close_2).sum())}
    o = m[m.open_1.notna()]
    chk['orig_opens'] = int(len(o))
    chk['orig_opens_numeric_equal'] = bool((o.open_1.map(Decimal) == o.open_2.map(Decimal)).all())
    if not (chk['close_numeric_equal_v1'] and chk['orig_opens_numeric_equal'] and chk['v1_rows_missing_in_v2'] == 0):
        raise SystemExit('v2 does not reproduce v1 closes/opens')
    sp = pd.read_csv(V2 / 'spot.csv', parse_dates=['date'])
    if sp.duplicated(['date', 'symbol']).any() or sp[['open', 'close']].isna().any().any():
        raise SystemExit('v2 duplicates / NaN')
    if ((sp.open <= 0) | (sp.close <= 0)).any():
        raise SystemExit('v2 non-positive price')
    O = sp.pivot(index='date', columns='symbol', values='open').sort_index()[list(IDX)]
    C = sp.pivot(index='date', columns='symbol', values='close').sort_index()[list(IDX)]
    if O.isna().any().any() or C.isna().any().any():
        raise SystemExit('legs not on a common calendar')
    return O, C, chk


def leg_stats(ev):
    out = {}
    for leg in ('开多', '平多'):
        x = ev[ev.leg == leg].e
        out[leg] = {'n': int(len(x)), 'mean_e_bp': float(x.mean() * 1e4), 'sum_e_pp': float(x.sum() * 100),
                    'mean_o_bp': float(ev[ev.leg == leg].o.mean() * 1e4),
                    'p_t': float(sst.ttest_1samp(x, 0.).pvalue)}
    return out


def pol_stats(r):
    r = pd.Series(r, dtype=float)
    if r.isna().any():
        raise SystemExit('NaN return')
    return {'ann': ann_return(r), 'sharpe': sharpe(r), 'maxdd': max_drawdown(r)}


def main():
    O, C, data_chk = load_v2()
    cal = C.index
    gap = O / C.shift(1) - 1.
    intra = C / O - 1.
    cc = C.pct_change()
    blend_cc = cc.mean(axis=1)

    _, fut, spot1, pos_f, _, pos_s, _ = qa.load()
    # decision calendar must coincide with the v2 calendar wherever positions exist
    in_rng = cal[(cal >= pos_s.index[0]) & (cal <= pos_s.index[-1])]
    if not in_rng.equals(pos_s.index):
        raise SystemExit('position calendar != v2 spot calendar')
    days = cal[(cal >= EXT[0]) & (cal <= EXT[1])]
    p_new = pos_s.shift(1).reindex(days)            # shift on the full decision calendar
    p_old = pos_s.shift(2).reindex(days)
    if p_new.isna().any() or p_old.isna().any():
        raise SystemExit('decision series does not cover t-1/t-2')
    po, pn = p_old.values, p_new.values
    L = qa.legs(po, pn)
    cost = C1 * np.abs(pn - po)
    G, I_, CC = gap.reindex(days), intra.reindex(days), cc.reindex(days)
    rets = {}
    for name, A in POLS.items():
        pm = po + sum((L[a] for a in A), np.zeros_like(po))
        gross = sum(.5 * qa.gross_formula(po, pm, G[c].values, I_[c].values) for c in IDX)
        rets[name] = pd.Series(gross - cost, index=days)
    rets['E0'] = pd.Series(pn * blend_cc.reindex(days).values - cost, index=days)
    e1_cc = pd.Series(po * blend_cc.reindex(days).values - cost, index=days)

    bi = I_.mean(axis=1).values
    bg = G.mean(axis=1).values
    rows = []
    for i, t in enumerate(days):
        for leg in ('开多', '平多', '开空', '平空'):
            if L[leg][i] != 0:
                rows.append({'date': t, 'leg': leg, 'delta': L[leg][i], 'intra_w': bi[i], 'gap_w': bg[i],
                             'e': L[leg][i] * bi[i], 'o': L[leg][i] * bg[i]})
    ev = pd.DataFrame(rows)
    if set(ev.leg) - {'开多', '平多'}:
        raise SystemExit('S has short legs?')

    # proxy events (my qa.py S-proxy) for the proxy-error block
    cal_f = pd.DatetimeIndex(sorted(fut.date.unique()))
    H = qa.held_frames(fut, cal_f)
    Sp = qa.build_S(spot1, pos_s, H, cal_f)
    srets_p, SL = qa.s_returns(Sp)
    ev_p = qa.s_events(Sp, SL)

    def block(a, b, with_proxy):
        r = {k: v.loc[a:b] for k, v in rets.items()}
        e = ev[(ev.date >= a) & (ev.date <= b)]
        bs = paired_block_bootstrap_sharpe_diff(r['E2'], r['E1'], block=20, n=2000, seed=20261009)
        out = {'legs': leg_stats(e),
               'policies': {k: pol_stats(r[k]) for k in ('E0', 'E1', 'E2', 'M_开多', 'M_平多')},
               'e2_minus_e1': {'d_sharpe': bs['diff_sharpe'], 'ci_lo': bs['ci_lo'], 'ci_hi': bs['ci_hi'],
                               'p': bs['p_value']}}
        if with_proxy:
            j = e.merge(ev_p[['date', 'leg', 'e']], on=['date', 'leg'], how='outer', suffixes=('_x', '_p'),
                        indicator=True)
            if (j._merge != 'both').any():
                raise SystemExit('exact and proxy event sets differ')
            pe = {}
            for leg in ('开多', '平多'):
                d = (j[j.leg == leg].e_x - j[j.leg == leg].e_p)
                pe[leg] = {'mean_bp': float(d.mean() * 1e4), 't': float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))))}
            out['proxy_error'] = pe
        idx = r['E1'].index
        out.update({'n_days': int(len(idx)), 'first_day': str(idx[0].date()), 'last_day': str(idx[-1].date())})
        return out

    kn = {'main': block(*MAIN, True), 'ext': block(*EXT, False),
          'sharpe_by_window': {w: {'E1': sharpe(rets['E1'].loc[a:b]), 'E2': sharpe(rets['E2'].loc[a:b])}
                               for w, (a, b) in SUBW.items()}}
    (QDIR / 'key_numbers_s_exact.json').write_text(json.dumps(kn, ensure_ascii=False, indent=1))

    # ------------------------------------------------------------------ checks for the report
    ck = {'data': data_chk}
    # (a) identity (1+cc) = (1+gap)(1+intra), per leg
    ident = {}
    for c in IDX:
        dev_all = ((1 + cc[c]) - (1 + gap[c]) * (1 + intra[c])).abs()
        ident[c] = {'max_abs_all_rows': float(dev_all.max()), 'max_abs_ext': float(dev_all.loc[EXT[0]:EXT[1]].max()),
                    'n_rows_checked': int(dev_all.notna().sum())}
    blend_dev = ((1 + blend_cc) - (1 + gap.mean(axis=1) + intra.mean(axis=1) + (gap * intra).mean(axis=1))).abs()
    ident['blend_note'] = ('blend: 1+mean(cc) vs mean((1+gap)(1+intra)) max abs '
                           f'{float(blend_dev.loc[EXT[0]:EXT[1]].max()):.3e}')
    ck['identity'] = ident
    # extra sanity of the filled opens
    san = {}
    for c in IDX:
        g = gap[c].loc[EXT[0]:EXT[1]]
        san[c] = {'O_eq_prev_close': int((O[c] == C[c].shift(1)).sum()), 'O_eq_close': int((O[c] == C[c]).sum()),
                  'top_abs_gap': {str(t.date()): round(float(v), 4) for t, v in g.abs().nlargest(4).items()},
                  'mean_gap_bp_by_period': {k: float(gap[c].loc[a:b].mean() * 1e4) for k, (a, b) in
                                            {**SUBW, 'pre_launch_2013-03-06~2014-10-16': ('2013-03-06', '2014-10-16')}.items()},
                  'mean_intra_bp_by_period': {k: float(intra[c].loc[a:b].mean() * 1e4) for k, (a, b) in SUBW.items()}}
    ck['open_sanity'] = san
    # real vs proxy intra over the whole main window (validation now possible beyond 66 days)
    pv = {}
    for k, (a, b) in {'main': MAIN, '2016-2020': SUBW['2016-2020'], '2021-2023': SUBW['2021-2023'],
                      '2024-2026': SUBW['2024-2026'], 'IM_era 2022-07-25~': ('2022-07-25', END),
                      'old_66d 2026-07-01~': ('2026-07-01', END)}.items():
        ri = intra.loc[a:b].mean(axis=1)
        pi = Sp['intra'].loc[a:b].mean(axis=1)
        d = (ri - pi).dropna()
        pv[k] = {'n': int(len(d)), 'corr': float(np.corrcoef(ri.loc[d.index], pi.loc[d.index])[0, 1]),
                 'mean_diff_bp': float(d.mean() * 1e4), 't': float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))),
                 'rmse_bp': float(np.sqrt((d ** 2).mean()) * 1e4)}
    for c in IDX:
        d = (intra[c] - Sp['intra'][c]).loc[MAIN[0]:MAIN[1]].dropna()
        pv[f'leg {c} main'] = {'mean_diff_bp': float(d.mean() * 1e4), 'corr': float(np.corrcoef(
            intra[c].loc[d.index], Sp['intra'][c].loc[d.index])[0, 1])}
    ck['proxy_intra_validation'] = pv
    # proxy vs exact policies in the main window
    ck['main_proxy_vs_exact'] = {k: {'proxy': pol_stats(srets_p[k].loc[MAIN[0]:MAIN[1]]),
                                     'exact': pol_stats(rets[k].loc[MAIN[0]:MAIN[1]])}
                                 for k in ('E0', 'E1', 'E2', 'M_开多', 'M_平多')}
    ck['E0_E1_identical_to_proxy_run'] = {
        k: float((rets[k].loc[MAIN[0]:MAIN[1]] - srets_p[k].loc[MAIN[0]:MAIN[1]]).abs().max()) for k in ('E0', 'E1')}
    ck['E1_formula_vs_pold_cc_max_abs'] = float((rets['E1'] - e1_cc).abs().max())
    # additivity
    for w, (a, b) in {'main': MAIN, 'ext': EXT}.items():
        de = (rets['E2'] - rets['E1']).loc[a:b].sum()
        se = ev[(ev.date >= a) & (ev.date <= b)].e.sum()
        ck[f'additivity_{w}'] = {'sum_E2_minus_E1_pp': float(de * 100), 'sum_e_pp': float(se * 100),
                                 'residual_pp': float((de - se) * 100)}
    # (b) 2014-2015 segment
    p14 = pos_s.loc[:'2015-12-31']
    nz = p14[p14 != 0]
    e14 = ev[(ev.date >= SUBW['2014-2015'][0]) & (ev.date <= SUBW['2014-2015'][1])]
    ck['seg_2014_2015'] = {
        'first_nonzero_position_day': str(nz.index[0].date()) if len(nz) else None,
        'position_days_long': int((pos_s.loc['2014-01-02':'2015-12-31'] == 1).sum()),
        'position_days_total': int(len(pos_s.loc['2014-01-02':'2015-12-31'])),
        'events_by_leg': e14.leg.value_counts().to_dict(),
        'first_event': str(e14.date.min().date()) if len(e14) else None,
        'events': [{'date': str(r.date.date()), 'leg': r.leg, 'e_bp': round(r.e * 1e4, 1), 'o_bp': round(r.o * 1e4, 1),
                    'gap_w_bp': round(r.gap_w * 1e4, 1)} for r in e14.itertuples()],
        'legs': leg_stats(e14) if len(e14) else None,
        'E2_minus_E1_sum_pp': float((rets['E2'] - rets['E1']).loc[SUBW['2014-2015'][0]:SUBW['2014-2015'][1]].sum() * 100),
        'ann_E1_E2': {k: ann_return(rets[k].loc[SUBW['2014-2015'][0]:SUBW['2014-2015'][1]]) for k in ('E1', 'E2')}}
    # (c) E0 vs run_strategy on the full position series with v2 closes
    rs = run_strategy(pos_s, blend_cc, 3.0)['ret']
    ck['E0_vs_run_strategy'] = {w: {'max_abs_daily_diff': float((rets['E0'].loc[a:b] - rs.loc[a:b]).abs().max()),
                                    'n': int(len(rets['E0'].loc[a:b]))} for w, (a, b) in {'main': MAIN, 'ext': EXT}.items()}
    rs_first = run_strategy(pos_s, blend_cc, 3.0)
    ck['run_strategy_first_row'] = {'date': str(rs_first.index[0].date()), 'gross': float(rs_first.gross.iloc[0]),
                                    'note': 'v2 has 2013 closes, so blend_cc on 2014-01-02 is defined'}
    # concentration of E2-E1
    d21 = (rets['E2'] - rets['E1'])
    for w, (a, b) in {'main': MAIN, 'ext': EXT}.items():
        x = d21.loc[a:b]
        top = x.abs().nlargest(5)
        ck[f'E2_minus_E1_top5_{w}'] = {str(t.date()): float(x.loc[t] * 100) for t in top.index}
        ck[f'E2_minus_E1_total_{w}_pp'] = float(x.sum() * 100)
    # per-year e sums (exact) by leg
    ck['by_year_e_pp'] = ev.assign(y=ev.date.dt.year).groupby(['y', 'leg']).e.agg(['count', 'sum']).assign(
        sum=lambda x: x['sum'] * 100).reset_index().to_dict('records')
    (QDIR / 'checks_s_exact.json').write_text(json.dumps(ck, ensure_ascii=False, indent=1, default=str))
    ev.to_csv(QDIR / 'events_S_exact_qa.csv', index=False)
    pd.DataFrame({'p_old': p_old, 'p_new': p_new, **{f'gap_{c}': G[c] for c in IDX}, **{f'intra_{c}': I_[c] for c in IDX},
                  **{f'ret_{k}': v for k, v in rets.items()}}).to_csv(QDIR / 'daily_S_exact_qa.csv')
    print(json.dumps(kn, ensure_ascii=False)[:2500])


if __name__ == '__main__':
    main()
