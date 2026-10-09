#!/usr/bin/env python3
"""Independent QA re-implementation: open (E2) vs close (E1) execution, 2026-10-09.

Written from spec.md + inputs/*.csv only (no code from openexec/impl/, no
backtest/exec_price_probe.py).  House metrics (ann_return / sharpe /
max_drawdown) are imported for the unified metric definitions; run_strategy and
contract_ledger/futures_weights are imported ONLY for the spec-5.1/5.2 anchors.

Run from the repo root:
    python3 /tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/8ebe6247-4dcd-4083-a029-e12de7a53103/scratchpad/openexec/qa/qa.py

Outputs (all in the qa/ directory):
    key_numbers.json                 primary variant ("spec": E1 = p_old*cc, M_A by the product formula)
    key_numbers_variant_lit.json     product formula for every policy, incl. E1 = M_empty
    key_numbers_variant_ev.json      open-rebalance only on days with an open fill (p_mid != p_old)
    checks_qa.json                   every adversarial check (a)-(f) as numbers
    events_F_qa.csv / events_S_qa.csv / limit_F_qa.csv / daily_F_qa.csv
"""
from __future__ import annotations

import calendar as _cal
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path('/home/elfbob/claude-code/style_timing_signal')
BASE = Path('/tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/'
            '8ebe6247-4dcd-4083-a029-e12de7a53103/scratchpad/openexec')
INP, Q = BASE / 'inputs', BASE / 'qa'
sys.path.insert(0, str(REPO))
from backtest.metrics import ann_return, sharpe, max_drawdown  # noqa: E402

C1 = 3e-4                      # 3bp per unit |delta|
LEGS = ['开多', '平多', '开空', '平空']
END = '2026-10-08'
F_START, S_START = '2015-04-17', '2016-01-04'
F_WIN = {'full': (F_START, END), '2015-2020': (F_START, '2020-12-31'),
         '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', END),
         'dual_listed': ('2022-07-25', END), 'ex2015': ('2016-01-04', END)}
S_WIN = {'full': (S_START, END), '2016-2020': (S_START, '2020-12-31'),
         '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', END)}
F_POL = {'E1': (), 'E2': ('开多', '平多', '开空', '平空'),
         'M_开多': ('开多',), 'M_平多': ('平多',), 'M_开空': ('开空',), 'M_平空': ('平空',),
         'M_平多开空': ('平多', '开空'), 'M_平空开多': ('平空', '开多')}
S_POL = {'E1': (), 'E2': ('开多', '平多'), 'M_开多': ('开多',), 'M_平多': ('平多',)}


# ----------------------------------------------------------------------------- inputs
def load():
    meta = json.loads((INP / 'snapshot.json').read_text())
    for name, digest in meta['sha256'].items():
        if hashlib.sha256((INP / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f'sha256 mismatch: {name}')
    fut = pd.read_csv(INP / 'futures.csv', parse_dates=['date'])
    spot = pd.read_csv(INP / 'spot.csv', parse_dates=['date'])
    rd = lambda n, c: pd.read_csv(INP / n, parse_dates=['date'], index_col='date')[c]  # noqa: E731
    pos_f = rd('equal_weight_symmetric.csv', 'position').astype(float)
    fv_f = rd('equal_weight_signal_20d40z.csv', 'factor_value').astype(float)
    pos_s = rd('slope20_longflat.csv', 'position').astype(float)
    fv_s = rd('slope20_signal_L20zw120.csv', 'factor_value').astype(float)
    if not pos_f.index.equals(fv_f.index) or not np.array_equal(pos_f.values, np.sign(fv_f.values)):
        raise SystemExit('STOP: equal_weight_symmetric != sign(factor_value)')
    for s in (pos_f, pos_s):
        if not s.index.is_unique or not s.index.is_monotonic_increasing or s.isna().any():
            raise SystemExit('bad decision series')
    return meta, fut, spot, pos_f, fv_f, pos_s, fv_s


# ----------------------------------------------------------------------------- legs
def legs(p_old, p_new):
    po, pn = np.asarray(p_old, float), np.asarray(p_new, float)
    L = {'开多': np.maximum(0., np.maximum(pn, 0.) - np.maximum(po, 0.)),
         '平多': -np.maximum(0., np.maximum(po, 0.) - np.maximum(pn, 0.)),
         '开空': -np.maximum(0., np.maximum(-pn, 0.) - np.maximum(-po, 0.)),
         '平空': np.maximum(0., np.maximum(-po, 0.) - np.maximum(-pn, 0.))}
    if not np.allclose(sum(L.values()), pn - po, atol=0, rtol=0):
        raise AssertionError('legs do not add up')
    return L


def gross_formula(p_old, p_mid, gap, intra):
    """Spec section 2, per group: (1 + p_old*gap)(1 + p_mid*intra) - 1."""
    return (1. + p_old * gap) * (1. + p_mid * intra) - 1.


# ----------------------------------------------------------------------------- futures held contracts
def main_table(fut):
    f = fut.assign(g=fut.symbol.str[:2])
    srt = f.sort_values(['date', 'g', 'oi', 'symbol'], ascending=[True, True, False, True])
    top = srt.groupby(['date', 'g'], sort=False).head(1)
    return {(r.date, r.g): r.symbol for r in top.itertuples()}


def held_frames(fut, cal):
    """held_g(t) = main_g(t-1) (prev futures day); if not quoted on t -> main_g(t) (counted)."""
    main = main_table(fut)
    q = {(r.date, r.symbol): r for r in fut.itertuples()}
    out = {}
    for g in ('IC', 'IM'):
        rows, prev_sym = [], None
        for i in range(1, len(cal)):
            t, tp = cal[i], cal[i - 1]
            h = main.get((tp, g))
            if h is None:                      # group not yet listed on t-1 -> no held contract
                rows.append({'date': t, 'sym': None, 'main_t': main.get((t, g)), 'main_tm1': None})
                prev_sym = None
                continue
            degraded = (t, h) not in q
            if degraded:
                h = main[(t, g)]
            r, rp = q[(t, h)], q.get((tp, h))
            if rp is None:
                raise SystemExit(f'held {h} has no t-1 close on {t.date()}')
            rows.append({'date': t, 'sym': h, 'main_t': main.get((t, g)), 'main_tm1': main.get((tp, g)),
                         'degraded': degraded, 'roll': prev_sym is not None and h != prev_sym,
                         'O': r.open, 'H': r.high, 'L': r.low, 'C': r.close, 'Cp': rp.close,
                         'pre_settle': r.pre_settle,
                         'gap': r.open / rp.close - 1., 'intra': r.close / r.open - 1.,
                         'cc': r.close / rp.close - 1.})
            prev_sym = h
        out[g] = pd.DataFrame(rows).set_index('date')
    return out


def third_friday(sym):
    yymm = sym.split('.')[0][2:]
    y, m = 2000 + int(yymm[:2]), int(yymm[2:])
    fr = [d for d in _cal.Calendar().itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fr[2]


# ----------------------------------------------------------------------------- metrics
def stats(r):
    r = pd.Series(r, dtype=float)
    if r.isna().any():
        raise SystemExit('NaN daily return')
    return {'ann': ann_return(r), 'sharpe': sharpe(r), 'maxdd': max_drawdown(r)}


def win(s, a, b):
    return s.loc[a:b]


# ----------------------------------------------------------------------------- F
def build_F(fut, pos_f, cal_f, H, d_override=None):
    d = pos_f if d_override is None else d_override
    days = cal_f[(cal_f >= F_START) & (cal_f <= END)]
    if len(days.difference(d.index)):
        raise SystemExit('decision series misses futures days')
    p_new = d.shift(1).reindex(days)           # d_{t-1} on the decision calendar
    p_old = d.shift(2).reindex(days)           # d_{t-2}
    if p_new.isna().any() or p_old.isna().any():
        raise SystemExit('decision series does not cover t-1/t-2')
    first_im = fut.loc[fut.symbol.str.startswith('IM'), 'date'].min()
    w = pd.DataFrame({'IC': np.where(days > first_im, .5, 1.), 'IM': np.where(days > first_im, .5, 0.)},
                     index=days)
    G = {}
    for g in ('IC', 'IM'):
        h = H[g].reindex(days)
        live = w[g] > 0
        need = h.loc[live, ['gap', 'intra', 'cc']]
        if need.isna().any().any():
            raise SystemExit(f'{g} missing held data on a weighted day')
        G[g] = pd.DataFrame({k: h[k].where(live, 0.).astype(float) for k in ('gap', 'intra', 'cc')})
        G[g]['roll'] = h['roll'].eq(True) & live
        G[g]['sym'] = h['sym'].where(live)
    return days, p_old, p_new, w, G, first_im


def f_returns(days, p_old, p_new, w, G, variant='spec'):
    po, pn = p_old.values, p_new.values
    L = legs(po, pn)
    trade_cost = C1 * np.abs(pn - po)
    roll_old = sum(G[g]['roll'].values * w[g].values for g in G) * 2 * C1 * np.abs(po)
    cc_old = sum(w[g].values * po * G[g]['cc'].values for g in G)
    out = {}
    for name, A in F_POL.items():
        pm = po + sum((L[a] for a in A), np.zeros_like(po))
        lit = sum(w[g].values * gross_formula(po, pm, G[g]['gap'].values, G[g]['intra'].values) for g in G)
        if variant == 'lit':
            gross = lit
        elif variant == 'spec':
            gross = cc_old if len(A) == 0 else lit
        elif variant == 'ev':
            gross = np.where(pm == po, cc_old, lit)
        else:
            raise ValueError(variant)
        out[name] = pd.Series(gross - trade_cost - roll_old, index=days)
    # E0: hold d_{t-1} all day t, cost on the change day (engine), roll on the pre-open position d_{t-1}
    roll_e0 = sum(G[g]['roll'].values * w[g].values for g in G) * 2 * C1 * np.abs(pn)
    e0_gross = sum(w[g].values * pn * G[g]['cc'].values for g in G)
    out['E0'] = pd.Series(e0_gross - trade_cost - roll_e0, index=days)
    return out, L


def f_events(days, p_old, p_new, w, G, L):
    intra = sum(w[g].values * G[g]['intra'].values for g in G)
    gap = sum(w[g].values * G[g]['gap'].values for g in G)
    rows = []
    for i, t in enumerate(days):
        if p_new.iloc[i] == p_old.iloc[i]:
            continue
        for leg in LEGS:
            dl = L[leg][i]
            if dl != 0:
                rows.append({'date': t, 'leg': leg, 'p_old': p_old.iloc[i], 'p_new': p_new.iloc[i], 'delta': dl,
                             'intra_w': intra[i], 'gap_w': gap[i], 'e': dl * intra[i], 'o': dl * gap[i]})
    return pd.DataFrame(rows)


def leg_summary(ev, legs_list):
    out = {}
    for leg in legs_list:
        x = ev[ev.leg == leg]
        out[leg] = {'n': int(len(x)), 'mean_e_bp': float(x.e.mean() * 1e4),
                    'sum_e_pp': float(x.e.sum() * 100), 'mean_o_bp': float(x.o.mean() * 1e4)}
    return out


# ----------------------------------------------------------------------------- S
def build_S(spot, pos_s, H, cal_f):
    px = spot.pivot(index='date', columns='symbol', values='close').sort_index()
    if px.isna().any().any():
        raise SystemExit('spot legs calendar mismatch')
    cc_idx = px.pct_change()
    blend_cc = cc_idx.mean(axis=1)
    cal_s = px.index
    days = cal_s[(cal_s >= S_START) & (cal_s <= END)]
    if len(days.difference(pos_s.index)) or not set(days) <= set(cal_f):
        raise SystemExit('S calendar coverage')
    p_new = pos_s.shift(1).reindex(days)
    p_old = pos_s.shift(2).reindex(days)
    ic = H['IC'].reindex(days)
    im = H['IM'].reindex(days)
    im_ok = im['intra'].notna()
    prox = pd.DataFrame({'000905.SH': ic['intra'],
                         '000852.SH': im['intra'].where(im_ok, ic['intra'])})
    if prox.isna().any().any():
        raise SystemExit('proxy intra missing')
    gap_idx = (1. + cc_idx.reindex(days)) / (1. + prox) - 1.
    return dict(days=days, p_old=p_old, p_new=p_new, cc_idx=cc_idx.reindex(days), blend_cc=blend_cc,
                intra=prox, gap=gap_idx, im_first_proxy=im_ok.idxmax(), px=px, cc_full=cc_idx)


def s_returns(S, intra=None, gap=None):
    days, po, pn = S['days'], S['p_old'].values, S['p_new'].values
    intra = S['intra'] if intra is None else intra
    gap = S['gap'] if gap is None else gap
    L = legs(po, pn)
    cost = C1 * np.abs(pn - po)
    out = {}
    for name, A in S_POL.items():
        pm = po + sum((L[a] for a in A), np.zeros_like(po))
        gross = sum(.5 * gross_formula(po, pm, gap[c].values, intra[c].values) for c in intra.columns)
        out[name] = pd.Series(gross - cost, index=days)
    out['E0'] = pd.Series(pn * S['blend_cc'].reindex(days).values - cost, index=days)
    out['E1_cc'] = pd.Series(po * S['blend_cc'].reindex(days).values - cost, index=days)
    return out, L


def s_events(S, L, intra=None, gap=None):
    intra = S['intra'] if intra is None else intra
    gap = S['gap'] if gap is None else gap
    bi, bg = intra.mean(axis=1).values, gap.mean(axis=1).values
    rows = []
    for i, t in enumerate(S['days']):
        for leg in ('开多', '平多', '开空', '平空'):
            dl = L[leg][i]
            if dl != 0:
                rows.append({'date': t, 'leg': leg, 'delta': dl, 'intra_w': bi[i], 'gap_w': bg[i],
                             'e': dl * bi[i], 'o': dl * bg[i]})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- key numbers
def key_numbers(Fd, Sd):
    days, p_old, p_new, w, G = Fd['days'], Fd['p_old'], Fd['p_new'], Fd['w'], Fd['G']
    rets, ev = Fd['rets'], Fd['ev']
    ev_full = ev[(ev.date >= F_START) & (ev.date <= END)]
    flip = {}
    for name, (a, b) in {'多翻空': (1., -1.), '空翻多': (-1., 1.)}.items():
        x = ev_full[(ev_full.p_old == a) & (ev_full.p_new == b)]
        flip[name] = {'n': int(x.date.nunique()), 'sum_e_pp': float(x.e.sum() * 100)}
    F = {'legs_full': leg_summary(ev_full, LEGS),
         'events_full': flip,
         'policies_full': {k: stats(win(rets[k], *F_WIN['full']))
                           for k in ['E0', 'E1', 'E2', 'M_开多', 'M_平多', 'M_开空', 'M_平空',
                                     'M_平多开空', 'M_平空开多']},
         'sharpe_by_window': {wn: {'E1': sharpe(win(rets['E1'], a, b)), 'E2': sharpe(win(rets['E2'], a, b))}
                              for wn, (a, b) in F_WIN.items() if wn != 'full'},
         'n_days_full': int(len(win(rets['E1'], *F_WIN['full']))),
         'first_day': str(days[0].date()), 'last_day': str(days[-1].date()),
         'roll_days': {g: int(G[g]['roll'].loc[F_START:END].sum()) for g in ('IC', 'IM')},
         'limit_flag_events': int(Fd['limit_events'])}
    sr, sev = Sd['rets'], Sd['ev']
    S = {'legs_full': leg_summary(sev, ['开多', '平多']),
         'policies_full': {k: stats(win(sr[k], *S_WIN['full'])) for k in ['E0', 'E1', 'E2', 'M_开多', 'M_平多']},
         'n_days_full': int(len(win(sr['E1'], *S_WIN['full']))),
         'first_day': str(Sd['S']['days'][0].date()), 'last_day': str(Sd['S']['days'][-1].date()),
         'proxy_validation': Sd['pv_key']}
    return {'F': F, 'S': S}


# ----------------------------------------------------------------------------- main
def main():
    meta, fut, spot, pos_f, fv_f, pos_s, fv_s = load()
    cal_f = pd.DatetimeIndex(sorted(fut.date.unique()))
    H = held_frames(fut, cal_f)
    checks = {'inputs_sha256_ok': True, 'F_position_equals_sign_factor': True}

    # ---------------- F
    days, p_old, p_new, w, G, first_im = build_F(fut, pos_f, cal_f, H)
    variants = {}
    for v in ('spec', 'lit', 'ev'):
        variants[v] = f_returns(days, p_old, p_new, w, G, v)
    rets, L = variants['spec']
    ev = f_events(days, p_old, p_new, w, G, L)

    # limit flags on execution days (held contract with weight > 0)
    lim_rows = []
    exec_days = set(ev.date)
    for g in ('IC', 'IM'):
        h = H[g]
        for t in sorted(exec_days):
            if w.at[t, g] <= 0:
                continue
            r = h.loc[t]
            dev = r.O / r.pre_settle - 1. if pd.notna(r.pre_settle) else np.nan
            one_price = (r.O == r.H) and (r.O == r.L)
            loose = (r.O == r.H) or (r.O == r.L)
            lim_rows.append({'date': t, 'group': g, 'sym': r.sym, 'O': r.O, 'H': r.H, 'L': r.L, 'C': r.C,
                             'pre_settle': r.pre_settle, 'open_vs_presettle': dev,
                             'flag_9p5': bool(pd.notna(dev) and abs(dev) >= .095), 'flag_OHL': bool(one_price),
                             'loose_O_eq_H_or_L': bool(loose)})
    lim = pd.DataFrame(lim_rows)
    lim['flag'] = lim.flag_9p5 | lim.flag_OHL
    flagged_days = set(lim.loc[lim.flag, 'date'])
    ev['limit_flag'] = ev.date.isin(flagged_days)
    limit_events = int(ev.limit_flag.sum())

    Fd = dict(days=days, p_old=p_old, p_new=p_new, w=w, G=G, rets=rets, ev=ev, limit_events=limit_events)

    # ---------------- S
    S = build_S(spot, pos_s, H, cal_f)
    srets, SL = s_returns(S)
    sev = s_events(S, SL)
    # proxy validation on days with real index opens
    so = spot.pivot(index='date', columns='symbol', values='open')
    sc = spot.pivot(index='date', columns='symbol', values='close')
    have = so.notna().all(axis=1) & (so > 0).all(axis=1)
    vd = so.index[have & (so.index >= S_START) & (so.index <= END)]
    real_intra = (sc.loc[vd] / so.loc[vd] - 1.)
    prox_intra = S['intra'].loc[vd]
    rb, pb = real_intra.mean(axis=1), prox_intra.mean(axis=1)
    diff = rb - pb
    pv_key = {'n_days': int(len(vd)), 'corr_blend_intra': float(np.corrcoef(rb, pb)[0, 1]),
              'mean_diff_bp': float(diff.mean() * 1e4), 'rmse_bp': float(np.sqrt((diff ** 2).mean()) * 1e4)}
    Sd = dict(S=S, rets=srets, ev=sev, pv_key=pv_key)

    kn = key_numbers(Fd, Sd)
    (Q / 'key_numbers.json').write_text(json.dumps(kn, ensure_ascii=False, indent=1))
    for v in ('lit', 'ev'):
        Fv = dict(Fd, rets=variants[v][0])
        (Q / f'key_numbers_variant_{v}.json').write_text(json.dumps(key_numbers(Fv, Sd), ensure_ascii=False, indent=1))

    # ------------------------------------------------------------------ checks
    # (a1) toy example with two reversals, hand values
    toy_po = np.array([1., -1., -1., 1.])
    toy_pn = np.array([-1., -1., 1., 1.])
    toy_gap = np.array([.01, -.005, .02, -.01])
    toy_intra = np.array([-.02, .01, .01, .005])
    TL = legs(toy_po, toy_pn)
    toy = {}
    for name, A in F_POL.items():
        pm = toy_po + sum((TL[a] for a in A), np.zeros(4))
        toy[name] = gross_formula(toy_po, pm, toy_gap, toy_intra)
    toy_cc = (1 + toy_gap) * (1 + toy_intra) - 1
    hand = {  # computed by hand, see qa_report.md section (a)
        'legs': {'开多': [0, 0, 1, 0], '平多': [-1, 0, 0, 0], '开空': [-1, 0, 0, 0], '平空': [0, 0, 1, 0]},
        'E1_pold_cc': [-0.0102, -0.00495, -0.0302, -0.00505],
        'E1': [-0.0102, -0.00505, -0.0298, -0.00505],          # product formula with p_mid = p_old
        'E2': [0.0302, -0.00505, -0.0102, -0.00505],
        'M_平多': [0.01, -0.00505, -0.0298, -0.00505],
        'M_开空': [0.01, -0.00505, -0.0298, -0.00505],
        'M_平空': [-0.0102, -0.00505, -0.02, -0.00505],
        'M_开多': [-0.0102, -0.00505, -0.02, -0.00505],
        'M_平多开空': [0.0302, -0.00505, -0.0298, -0.00505],
        'M_平空开多': [-0.0102, -0.00505, -0.0102, -0.00505],
        'e_by_leg': {'开多': [0, 0, 0.01, 0], '平多': [0.02, 0, 0, 0], '开空': [0.02, 0, 0, 0], '平空': [0, 0, 0.01, 0]},
        'o_by_leg': {'开多': [0, 0, 0.02, 0], '平多': [-0.01, 0, 0, 0], '开空': [-0.01, 0, 0, 0], '平空': [0, 0, 0.02, 0]},
    }
    toy_out = {k: [float(x) for x in v] for k, v in toy.items()}
    toy_out['legs'] = {k: [float(x) for x in v] for k, v in TL.items()}
    toy_out['E1_pold_cc'] = [float(x) for x in toy_po * toy_cc]
    toy_out['e_by_leg'] = {k: [float(x) for x in v * toy_intra] for k, v in TL.items()}
    toy_out['o_by_leg'] = {k: [float(x) for x in v * toy_gap] for k, v in TL.items()}
    toy_out['inputs'] = {'p_old': toy_po.tolist(), 'p_new': toy_pn.tolist(), 'gap': toy_gap.tolist(),
                         'intra': toy_intra.tolist()}
    for k, v in hand.items():
        if k in ('legs', 'e_by_leg', 'o_by_leg'):
            for leg, vals in v.items():
                assert np.allclose(toy_out[k][leg], vals, atol=1e-12, rtol=0), (k, leg)
        else:
            assert np.allclose(toy_out[k], v, atol=1e-12, rtol=0), (k, toy_out[k], v)
    toy_out['hand_values_match'] = True
    checks['toy'] = toy_out

    # (a2) E1 == p_old * sum_w cc, computed by an independent path from raw closes
    q = fut.set_index(['date', 'symbol']).close
    ind = []
    for i, t in enumerate(days):
        tot = 0.
        tp = cal_f[cal_f.get_loc(t) - 1]
        for g in ('IC', 'IM'):
            if w.at[t, g] > 0:
                s = H[g].at[t, 'sym']
                tot += w.at[t, g] * (q[(t, s)] / q[(tp, s)] - 1.)
        ind.append(p_old.iloc[i] * tot)
    ind = pd.Series(ind, index=days)
    roll_old = sum(G[g]['roll'] * w[g] for g in G) * 2 * C1 * p_old.abs()
    e1_gross = rets['E1'] + C1 * (p_new - p_old).abs() + roll_old
    lit_e1_gross = variants['lit'][0]['E1'] + C1 * (p_new - p_old).abs() + roll_old
    short_days = p_old < 0
    gi = sum(w[g] * 2 * G[g]['gap'] * G[g]['intra'] for g in G)
    checks['E1_equals_pold_cc'] = {
        'max_abs_diff_spec_vs_independent': float((e1_gross - ind).abs().max()),
        'literal_formula_E1_minus_pold_cc_max_abs': float((lit_e1_gross - ind).abs().max()),
        'literal_formula_E1_minus_pold_cc_sum_pp': float((lit_e1_gross - ind).sum() * 100),
        'literal_minus_pold_cc_equals_2_gap_intra_on_short_days': float(
            (lit_e1_gross - ind - gi.where(short_days, 0.)).abs().max()),
        'short_days': int(short_days.sum()),
        'sum_2w_gap_intra_on_short_days_pp': float(gi[short_days].sum() * 100),
        'ann_pp_of_that_term': float(gi.where(short_days, 0.).mean() * 245 * 100)}

    # (a3) shift the decision series by one day
    shift = {}
    for tag, dd in {'main': pos_f, 'lead_1d(前移)': pos_f.shift(-1), 'lag_1d(后移)': pos_f.shift(1)}.items():
        dys, po2, pn2, w2, G2, _ = build_F(fut, pos_f, cal_f, H, d_override=dd)
        r2, _ = f_returns(dys, po2, pn2, w2, G2, 'spec')
        shift[tag] = {'E1_ann': ann_return(r2['E1']), 'E2_ann': ann_return(r2['E2']), 'E0_ann': ann_return(r2['E0']),
                      'E1_sharpe': sharpe(r2['E1']), 'E2_sharpe': sharpe(r2['E2'])}
    checks['timing_shift'] = shift

    # (b) no look-ahead: held(t) == main(t-1) always (except degradations), and != main(t) on some days
    hb = {}
    for g in ('IC', 'IM'):
        h = H[g].dropna(subset=['sym'])
        nd = h[~h.degraded.eq(True)]
        hb[g] = {'n_days': int(len(h)), 'held_eq_main_tm1': int((nd.sym == nd.main_tm1).sum()),
                 'held_ne_main_t': int((h.sym != h.main_t).sum()), 'degraded': int(h.degraded.eq(True).sum())}
    # perturbation: scramble OI on every day t and recompute held(t) using only days <= t-1 is identical
    fut_scr = fut.copy()
    rng = np.random.default_rng(1)
    last_day = cal_f[-1]
    m = fut_scr.date == last_day
    fut_scr.loc[m, 'oi'] = rng.permutation(fut_scr.loc[m, 'oi'].values)
    H_scr = held_frames(fut_scr, cal_f)
    hb['scramble_last_day_oi_changes_held_last_day'] = {
        g: bool(H_scr[g].at[last_day, 'sym'] != H[g].at[last_day, 'sym']) for g in ('IC', 'IM')}
    hb['scramble_last_day_main_t_changed'] = {
        g: bool(H_scr[g].at[last_day, 'main_t'] != H[g].at[last_day, 'main_t']) for g in ('IC', 'IM')}
    # three roll-day examples with full OI tables
    examples = []
    ic_rolls = H['IC'][H['IC'].roll.eq(True)].index
    im_rolls = H['IM'][H['IM'].roll.eq(True)].index
    for g, t in [('IC', ic_rolls[0]), ('IC', ic_rolls[len(ic_rolls) // 2]), ('IM', im_rolls[-1])]:
        tp = cal_f[cal_f.get_loc(t) - 1]
        tab = fut[(fut.date.isin([tp, t])) & fut.symbol.str.startswith(g)].pivot(index='symbol', columns='date',
                                                                                     values='oi')
        tab.columns = [str(c.date()) for c in tab.columns]
        examples.append({'group': g, 't-1': str(tp.date()), 't': str(t.date()),
                         'oi': {s: {k: (None if pd.isna(v) else int(v)) for k, v in r.items()} for s, r in tab.iterrows()},
                         'main(t-1)': H[g].at[t, 'main_tm1'], 'held(t)': H[g].at[t, 'sym'],
                         'main(t)': H[g].at[t, 'main_t'], 'held(t-1)': H[g].at[tp, 'sym'] if tp in H[g].index else None})
    hb['examples'] = examples
    checks['no_lookahead'] = hb

    # (c) weight switch
    from backtest.execution_ledger import contract_ledger, futures_weights
    fw = futures_weights(days, first_im)
    i_sw = days.get_loc(pd.Timestamp('2022-07-25'))
    checks['weights'] = {'first_im_quote': str(first_im.date()),
                         'first_5050_day': str(days[(w.IM > 0).values][0].date()),
                         'w_prev_day': {'date': str(days[i_sw - 1].date()), **w.iloc[i_sw - 1].to_dict()},
                         'w_switch_day': {'date': str(days[i_sw].date()), **w.iloc[i_sw].to_dict()},
                         'equals_futures_weights': bool(np.allclose(fw.values, w.values)),
                         'held_IM_on_2022-07-25': H['IM'].at[pd.Timestamp('2022-07-25'), 'sym'],
                         'held_IM_on_2022-07-22': H['IM'].at[pd.Timestamp('2022-07-22'), 'sym']}

    # (d) anchors
    spot_cal = S['px'].index
    expiries = {}
    for sym in fut.symbol.unique():
        ex = pd.Timestamp(third_friday(sym))
        nxt = spot_cal[spot_cal >= ex]
        expiries[sym] = nxt[0] if len(nxt) else ex
    lidx = cal_f[(cal_f >= '2015-04-16') & (cal_f <= END)]
    sig = pos_f.reindex(lidx)
    lw = futures_weights(lidx, first_im)
    anchors = {}
    led = {}
    for fill in ('close', 'open'):
        led[fill] = contract_ledger(fut[['date', 'symbol', 'open', 'close', 'oi']], sig, lw, fill=fill,
                                    cost_bps=3., expiries=expiries)
    for fill, pol in (('close', 'E1'), ('open', 'E2')):
        lr = led[fill].ret.loc[F_START:END]
        for v in ('spec', 'lit', 'ev'):
            mr = variants[v][0][pol].loc[F_START:END]
            anchors[f'{pol}_vs_ledger_{fill}_{v}'] = {
                'model': stats(mr), 'ledger': stats(lr), 'corr': float(np.corrcoef(mr, lr)[0, 1]),
                'ann_diff_pp': float((ann_return(mr) - ann_return(lr)) * 100),
                'max_abs_daily_diff': float((mr - lr).abs().max()),
                'ledger_cost_ann_pp': float(led[fill].cost_return.loc[F_START:END].mean() * 245 * 100),
                'ledger_rolls': int(led[fill].rolls.loc[F_START:END].sum())}
    # decompose model - ledger daily differences: first day / roll-or-contract-mismatch days / others
    roll_any = (G['IC']['roll'] | G['IM']['roll']).loc[F_START:END]
    mine_syms = pd.Series(['|'.join(sorted(G[g]['sym'].at[t] for g in ('IC', 'IM') if w.at[t, g] > 0))
                           for t in days], index=days).loc[F_START:END]
    for fill, pol in (('close', 'E1'), ('open', 'E2')):
        lr = led[fill].ret.loc[F_START:END]
        mr = variants['spec'][0][pol].loc[F_START:END]
        dd = mr - lr
        lsym = led[fill].symbols.loc[F_START:END]
        if fill == 'open':
            mism = (lsym != mine_syms) & (lsym != '')
        else:   # close fill: ledger's day-t selection is held over (t, t+1] -> compare with model held(t+1)
            mism = (lsym != mine_syms.shift(-1)) & (lsym != '') & mine_syms.shift(-1).notna()
            mism = mism | mism.shift(1, fill_value=False)
        first = pd.Series(False, index=dd.index)
        first.iloc[0] = True
        special = roll_any | mism | (led[fill].rolls.loc[F_START:END] > 0)
        other = ~(special | first)
        anchors[f'{pol}_vs_ledger_{fill}_decomp_ann_pp'] = {
            'first_day': float(dd[first].sum() * 245 / len(dd) * 100),
            'roll_or_contract_mismatch_days': float(dd[special & ~first].sum() * 245 / len(dd) * 100),
            'n_special_days': int((special & ~first).sum()),
            'other_days': float(dd[other].sum() * 245 / len(dd) * 100),
            'other_days_max_abs': float(dd[other].abs().max()),
            'contract_mismatch_days': int(mism.sum())}
    held_on_expiry = 0
    for g in ('IC', 'IM'):
        for t, s in H[g]['sym'].dropna().items():
            if w.at[t, g] > 0 and expiries[s] == t:
                held_on_expiry += 1
    anchors['model_days_holding_contract_on_its_expiry_day'] = held_on_expiry
    # historical reference (09-14 run)
    try:
        hp = pd.read_csv(REPO / 'backtest/output/runs/20260914-execution-audit-r2/outputs/execution_panel.csv')
        hp = hp[(hp.window == 'full') & hp.name.isin(['futures_close_3bps', 'futures_open_3bps'])]
        anchors['run_20260914_full'] = hp[['name', 'start', 'end', 'ann', 'sharpe', 'maxdd']].to_dict('records')
    except FileNotFoundError:
        anchors['run_20260914_full'] = 'missing'
    # S E0 vs run_strategy
    from backtest.engine import run_strategy
    rs = run_strategy(pos_s, S['blend_cc'], 3.0)['ret']
    d_e0 = (srets['E0'] - rs.reindex(S['days'])).loc[S_START:END]
    rs_win = run_strategy(pos_s.loc[S_START:END], S['blend_cc'].loc[S_START:END], 3.0)['ret']
    d_e0w = (srets['E0'] - rs_win.reindex(S['days'])).loc[S_START:END]
    anchors['S_E0_vs_run_strategy_fullseries'] = {'max_abs_daily_diff': float(d_e0.abs().max()),
                                                  'n': int(len(d_e0))}
    anchors['S_E0_vs_run_strategy_windowonly'] = {
        'max_abs_daily_diff': float(d_e0w.abs().max()),
        'days_with_diff': [str(t.date()) for t in d_e0w.index[d_e0w.abs() > 1e-12]][:5]}
    anchors['S_E1_formula_vs_pold_cc_max_abs'] = float((srets['E1'] - srets['E1_cc']).abs().max())
    checks['anchors'] = anchors

    # additivity (spec 5.3) per window, all variants
    add = {}
    for v in ('spec', 'lit', 'ev'):
        rr = variants[v][0]
        for wn, (a, b) in F_WIN.items():
            de = (rr['E2'] - rr['E1']).loc[a:b].sum()
            se = ev[(ev.date >= a) & (ev.date <= b)].e.sum()
            add[f'{v}:{wn}'] = {'sum_E2_minus_E1_pp': float(de * 100), 'sum_e_pp': float(se * 100),
                                'residual_pp': float((de - se) * 100)}
    sd = (srets['E2'] - srets['E1']).sum(); se = sev.e.sum()
    add['S:full'] = {'sum_E2_minus_E1_pp': float(sd * 100), 'sum_e_pp': float(se * 100),
                     'residual_pp': float((sd - se) * 100)}
    checks['additivity'] = add

    # (e) data anomalies
    checks['data'] = {
        'futures_duplicate_date_symbol': int(fut.duplicated(['date', 'symbol']).sum()),
        'futures_nonpositive_ohlc': int((fut[['open', 'high', 'low', 'close']] <= 0).sum().sum()),
        'futures_nan_ohlc': int(fut[['open', 'high', 'low', 'close']].isna().sum().sum()),
        'futures_oi_zero_rows': int((fut.oi <= 0).sum()),
        'futures_oi_ties_at_max': int(sum(1 for _, x in fut.assign(g=fut.symbol.str[:2]).groupby(['date', 'g'])
                                          if (x.oi == x.oi.max()).sum() > 1)),
        'held_degraded_days': {g: int(H[g].degraded.eq(True).sum()) for g in ('IC', 'IM')},
        'spot_duplicate_date_symbol': int(spot.duplicated(['date', 'symbol']).sum()),
        'spot_open_days': int(len(vd)),
        'F_position_zero_days_in_sample': [str(t.date()) for t in pos_f.loc['2015-04-15':END].index[
            pos_f.loc['2015-04-15':END] == 0]],
        'limit_rows_checked': int(len(lim)), 'limit_flag_rows': int(lim.flag.sum()),
        'limit_flag_exec_days': int(len(flagged_days)), 'limit_flag_leg_events': limit_events,
        'limit_flag_by_rule': {'9.5%': int(lim.flag_9p5.sum()), 'O=H=L': int(lim.flag_OHL.sum()),
                               'loose O=H or O=L (info only)': int(lim.loose_O_eq_H_or_L.sum())},
        'exec_days_that_are_roll_days': int(sum(1 for t in exec_days if any(
            bool(G[g]['roll'].at[t]) for g in ('IC', 'IM'))))}
    lim.to_csv(Q / 'limit_F_qa.csv', index=False)

    # extra: legs excluding flagged events, flip events, by-year sums
    exf = ev[~ev.limit_flag]
    checks['legs_full_ex_limit'] = leg_summary(exf, LEGS)
    checks['legs_full_e_stats'] = {
        leg: {'median_bp': float(ev[ev.leg == leg].e.median() * 1e4), 'std_bp': float(ev[ev.leg == leg].e.std() * 1e4),
              'share_open_better': float((ev[ev.leg == leg].e > 0).mean()), 'sum_o_pp': float(ev[ev.leg == leg].o.sum() * 100)}
        for leg in LEGS}
    checks['S_legs_e_stats'] = {
        leg: {'median_bp': float(sev[sev.leg == leg].e.median() * 1e4), 'sum_o_pp': float(sev[sev.leg == leg].o.sum() * 100),
              'share_open_better': float((sev[sev.leg == leg].e > 0).mean())} for leg in ('开多', '平多')}
    # full policy tables for windows (spec variant)
    pol_tab = {}
    for wn, (a, b) in F_WIN.items():
        for k, r in rets.items():
            x = r.loc[a:b]
            pol_tab[f'F|{wn}|{k}'] = {**stats(x), 'n': int(len(x))}
    for wn, (a, b) in S_WIN.items():
        for k in ['E0', 'E1', 'E2', 'M_开多', 'M_平多']:
            x = srets[k].loc[a:b]
            pol_tab[f'S|{wn}|{k}'] = {**stats(x), 'n': int(len(x))}
    checks['policy_table_spec_variant'] = pol_tab
    # flat-start alternative for F (p_old = 0 on 2015-04-17, ledger-like)
    pos_flat = pos_f.copy()
    pos_flat.loc[:'2015-04-15'] = 0.
    dys, po2, pn2, w2, G2, _ = build_F(fut, pos_f, cal_f, H, d_override=pos_flat)
    r2, L2 = f_returns(dys, po2, pn2, w2, G2, 'spec')
    checks['flat_start_alternative_F'] = {
        'p_old_first_day': float(po2.iloc[0]), 'p_new_first_day': float(pn2.iloc[0]),
        'E1': stats(r2['E1']), 'E2': stats(r2['E2']), 'E0': stats(r2['E0'])}
    # S proxy validation, per leg and sign agreement
    pvf = {}
    for c in real_intra.columns:
        dd = real_intra[c] - prox_intra[c]
        pvf[c] = {'corr': float(np.corrcoef(real_intra[c], prox_intra[c])[0, 1]), 'mean_diff_bp': float(dd.mean() * 1e4),
                  'rmse_bp': float(np.sqrt((dd ** 2).mean()) * 1e4),
                  'sign_agree': float((np.sign(real_intra[c]) == np.sign(prox_intra[c])).mean()),
                  'real_mean_bp': float(real_intra[c].mean() * 1e4), 'proxy_mean_bp': float(prox_intra[c].mean() * 1e4)}
    pvf['blend'] = {**pv_key, 'sign_agree': float((np.sign(rb) == np.sign(pb)).mean()),
                    'real_mean_bp': float(rb.mean() * 1e4), 'proxy_mean_bp': float(pb.mean() * 1e4),
                    't_mean_diff': float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))))}
    # exact vs proxy effects of S events in the open-price window
    real_gap = (1. + S['cc_idx'].loc[vd]) / (1. + real_intra) - 1.
    sev_v = sev[sev.date.isin(vd)]
    pvf['S_events_in_open_window'] = [
        {'date': str(r.date.date()), 'leg': r.leg, 'e_proxy_bp': float(r.e * 1e4),
         'e_exact_bp': float(r.delta * real_intra.loc[r.date].mean() * 1e4)} for r in sev_v.itertuples()]
    checks['proxy_validation_detail'] = pvf
    checks['S_im_first_proxy_day'] = str(S['im_first_proxy'].date())
    # extra evidence used in qa_report.md
    extra = {}
    # spec limit rule applied to every held-contract day (not only execution days)
    allflag = []
    for g in ('IC', 'IM'):
        h = H[g].dropna(subset=['sym'])
        dev = (h.O / h.pre_settle - 1.).abs()
        f = (dev >= .095) | ((h.O == h.H) & (h.O == h.L))
        allflag += [(str(t.date()), g, h.at[t, 'sym'], float(h.at[t, 'O']), float(h.at[t, 'pre_settle']))
                    for t in h.index[f]]
    extra['limit_rule_all_held_days'] = allflag
    # pre_settle gaps: fall back to the same contract's previous-day settle
    fs = fut.sort_values(['symbol', 'date']).copy()
    fs['prev_settle'] = fs.groupby('symbol').settle.shift(1)
    fs['traded_before'] = fs.groupby('symbol').date.shift(1).notna()
    extra['pre_settle_nan_rows_contract_traded_before'] = int((fs.pre_settle.isna() & fs.traded_before).sum())
    extra['pre_settle_ne_prev_settle_rows'] = int((fs.pre_settle.notna() & fs.prev_settle.notna()
                                                   & ((fs.pre_settle - fs.prev_settle).abs() > 1e-6)).sum())
    extra['exec_rows_nan_pre_settle'] = int(lim.pre_settle.isna().sum())
    ps_map = fs.set_index(['date', 'symbol']).prev_settle
    fb_all, fb_exec = [], 0
    for g in ('IC', 'IM'):
        h = H[g].dropna(subset=['sym'])
        for t, r in h.iterrows():
            ps = r.pre_settle if pd.notna(r.pre_settle) else ps_map.get((t, r.sym), np.nan)
            if (pd.notna(ps) and abs(r.O / ps - 1.) >= .095) or (r.O == r.H == r.L):
                fb_all.append((str(t.date()), r.sym, round(float(r.O / ps - 1.), 4)))
                fb_exec += int(t in exec_days and w.at[t, g] > 0)
    extra['limit_rule_settle_fallback_all_held_days'] = fb_all
    extra['limit_rule_settle_fallback_exec_rows'] = fb_exec
    lim_fb = lim.merge(fs[['date', 'symbol', 'prev_settle']], left_on=['date', 'sym'], right_on=['date', 'symbol'])
    extra['exec_rows_max_abs_open_vs_settle_fallback'] = float(
        (lim_fb.O / lim_fb.pre_settle.fillna(lim_fb.prev_settle) - 1.).abs().max())
    # S start-of-window convention: shift on the decision calendar (main) vs window-first shift + fillna(0)
    S2 = dict(S)
    dw = pos_s.loc[S_START:]
    S2['p_new'] = dw.shift(1).reindex(S['days']).fillna(0.)
    S2['p_old'] = dw.shift(2).reindex(S['days']).fillna(0.)
    r2s, _ = s_returns(S2)
    extra['S_window_first_convention'] = {
        'blend_cc_2016-01-04': float(S['blend_cc'].loc['2016-01-04']),
        **{k: {'main': stats(srets[k]), 'window_first': stats(r2s[k])} for k in ('E0', 'E1', 'E2')}}
    # o identity: sum(E0 - E2) vs sum o
    extra['sum_E0_minus_E2_pp'] = float((rets['E0'] - rets['E2']).sum() * 100)
    extra['sum_o_pp'] = float(ev.o.sum() * 100)
    # drift and de-drifted leg means
    wi = sum(w[g] * G[g]['intra'] for g in G)
    mu = float(wi.mean())
    extra['F_mean_weighted_intra_bp'] = mu * 1e4
    extra['F_dedrift_mean_bp'] = {leg: float((np.sign(ev[ev.leg == leg].delta) * (ev[ev.leg == leg].intra_w - mu)).mean() * 1e4)
                                  for leg in LEGS}
    # model vs open-fill ledger: largest non-roll-day gaps
    dd = (rets['E2'] - led['open'].ret.reindex(days))
    extra['E2_vs_ledger_open_top_nonroll'] = {str(t.date()): float(v) for t, v in
                                              dd[~(G['IC']['roll'] | G['IM']['roll'])].abs().nlargest(3).items()}
    # OI tables t-2, t-1, t for three roll days
    ex3 = []
    for g, t in [('IC', '2015-05-14'), ('IC', '2020-01-14'), ('IM', '2026-09-15')]:
        t = pd.Timestamp(t)
        i = cal_f.get_loc(t)
        ds = cal_f[i - 2:i + 1]
        tab = fut[fut.date.isin(ds) & fut.symbol.str.startswith(g)].pivot(index='symbol', columns='date', values='oi')
        ex3.append({'group': g, 'dates': [str(x.date()) for x in ds],
                    'oi': {s: [int(v) for v in r.values] for s, r in tab.iterrows()},
                    'held': [H[g].at[x, 'sym'] for x in ds], 'main': [H[g].at[x, 'main_t'] for x in ds]})
    extra['roll_examples'] = ex3
    extra['flip_counts_by_year'] = ev.assign(y=ev.date.dt.year).groupby(['y', 'leg']).e.agg(['count', 'sum']).reset_index(
    ).assign(sum=lambda x: x['sum'] * 100).to_dict('records')
    checks['extra'] = extra
    # misc: F zero-position episode
    zero_ev = ev[(ev.date >= '2017-01-01') & (ev.date <= '2017-01-10')]
    checks['F_zero_episode_events'] = zero_ev[['date', 'leg', 'p_old', 'p_new', 'delta', 'e', 'o']].assign(
        date=lambda x: x.date.astype(str)).to_dict('records')
    (Q / 'checks_qa.json').write_text(json.dumps(checks, ensure_ascii=False, indent=1, default=str))

    ev.to_csv(Q / 'events_F_qa.csv', index=False)
    sev.to_csv(Q / 'events_S_qa.csv', index=False)
    daily = pd.DataFrame({'p_old': p_old, 'p_new': p_new, 'w_IC': w.IC, 'w_IM': w.IM,
                          **{f'{g}_{k}': G[g][k] for g in G for k in ('sym', 'gap', 'intra', 'cc', 'roll')},
                          **{f'ret_{k}': v for k, v in rets.items()}})
    daily.to_csv(Q / 'daily_F_qa.csv')
    print(json.dumps(kn, ensure_ascii=False)[:3000])


if __name__ == '__main__':
    main()
