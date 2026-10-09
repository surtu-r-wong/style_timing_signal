#!/usr/bin/env python3
"""开盘价 vs 收盘价执行检验（2026-10-09 插播）——按冻结规格 ../spec.md 实现。

一条命令重现全部产出（只读 ../inputs/，不连库、不改仓库，产出写在本脚本所在目录）：
    cd /home/elfbob/claude-code/style_timing_signal && python3 <本目录>/run.py

模型（规格 §2，逐日收益、日度再平衡的名义敞口）：
    t 日开盘前持有 p_old = d_{t-2}，收盘后目标 p_new = d_{t-1}；四腿 Δ；
    M_A: p_mid = p_old + Σ_{leg∈A} Δ_leg；r = Σ_g w_g[(1+p_old·gap_g)(1+p_mid·intra_g) − 1] − 成本 − 换月；
    E1 = M_∅，E2 = M_全部；E0 = Σ_g w_g·d_{t-1}·cc_g − 成本 − 换月。
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

REPO = Path('/home/elfbob/claude-code/style_timing_signal')
sys.path.insert(0, str(REPO))
from backtest.data import _expiry_from_symbol  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.exec_price_probe import held_contract_frame  # noqa: E402
from backtest.execution_ledger import contract_ledger, futures_weights  # noqa: E402
from backtest.metrics import ann_return, max_drawdown, sharpe  # noqa: E402
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402

W = Path(__file__).resolve().parent
INP = W.parent / 'inputs'
RUN_0914 = REPO / 'backtest/output/runs/20260914-execution-audit-r2/outputs/execution_panel.csv'

ANN = 245
COST = 3.0 / 1e4
LIMIT_TH = 0.095
BOOT = dict(block=20, n=2000, seed=20261009)
END = pd.Timestamp('2026-10-08')
F_FIRST_EXPECTED = pd.Timestamp('2015-04-17')
IM_FIRST_EXPECTED = pd.Timestamp('2022-07-22')
S_START = pd.Timestamp('2016-01-04')
S_EXACT_START = pd.Timestamp('2026-07-01')
F_WINDOWS = {'full': ('2015-04-17', '2026-10-08'), '2015-2020': ('2015-04-17', '2020-12-31'),
             '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', '2026-10-08'),
             'dual_listed': ('2022-07-25', '2026-10-08'), 'ex2015': ('2016-01-04', '2026-10-08')}
S_WINDOWS = {'full': ('2016-01-04', '2026-10-08'), '2016-2020': ('2016-01-04', '2020-12-31'),
             '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', '2026-10-08')}
LEGS = ('开多', '平多', '开空', '平空')
FLIPS = ('多翻空', '空翻多')
F_POLICIES = {'E1': (), 'E2': LEGS, 'M_开多': ('开多',), 'M_平多': ('平多',), 'M_开空': ('开空',),
              'M_平空': ('平空',), 'M_平多开空': ('平多', '开空'), 'M_平空开多': ('平空', '开多')}
F_ORDER = ['E0', 'E1', 'E2', 'M_开多', 'M_平多', 'M_开空', 'M_平空', 'M_平多开空', 'M_平空开多']
S_POLICIES = {'E1': (), 'E2': ('开多', '平多'), 'M_开多': ('开多',), 'M_平多': ('平多',)}
S_ORDER = ['E0', 'E1', 'E2', 'M_开多', 'M_平多']
F_GROUPS = ('IC', 'IM')
S_GROUPS = ('905', '852')
S_CODE = {'905': '000905.SH', '852': '000852.SH'}


class SpecStop(RuntimeError):
    """规格要求「报错停下」的情形。"""


def require(cond, msg):
    if not bool(cond):
        raise SpecStop(msg)


def T(x):
    return pd.Timestamp(x)


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return None if not math.isfinite(x) else x
    if isinstance(x, pd.Timestamp):
        return str(x.date())
    return x


def dump_json(path, obj):
    path.write_text(json.dumps(jsonable(obj), indent=2, ensure_ascii=False), encoding='utf-8')


# =========================================================================== 输入
def load_inputs():
    snap = json.loads((INP / 'snapshot.json').read_text())
    for name, h in snap['sha256'].items():
        got = hashlib.sha256((INP / name).read_bytes()).hexdigest()
        require(got == h, f'sha256 不符：{name}')
    fut = pd.read_csv(INP / 'futures.csv', parse_dates=['date'])
    spot = pd.read_csv(INP / 'spot.csv', parse_dates=['date'])

    def series(name, col):
        d = pd.read_csv(INP / name, parse_dates=['date'])
        require(d.date.is_unique and d[col].notna().all(), f'{name} 日期重复或有缺值')
        return d.set_index('date')[col].astype(float).sort_index()

    return dict(snap=snap, fut=fut, spot=spot,
                pos_f=series('equal_weight_symmetric.csv', 'position'),
                fac_f=series('equal_weight_signal_20d40z.csv', 'factor_value'),
                pos_s=series('slope20_longflat.csv', 'position'),
                fac_s=series('slope20_signal_L20zw120.csv', 'factor_value'))


# =========================================================================== 持有合约
def main_contract(ff):
    """当日 OI 最大，平局取 symbol 字典序最小（显式排序，不依赖行序）。返回 (main, 平局日数)。"""
    srt = ff.sort_values(['date', 'oi', 'symbol'], ascending=[True, False, True])
    main = srt.groupby('date').symbol.first()
    mx = ff.groupby('date').oi.transform('max')
    ties = int((ff[ff.oi == mx].groupby('date').size() > 1).sum())
    return main, ties


def held_frame(ff, fcal):
    """held(t) = main(t−1)；t 日 gap/intra/cc 一律用同一张合约；held 在 t 日无报价则退化为 main(t)。"""
    main, ties = main_contract(ff)
    P = {c: ff.pivot(index='date', columns='symbol', values=c)
         for c in ('open', 'high', 'low', 'close', 'settle', 'pre_settle')}
    gd = list(main.index)
    rows, fallbacks = [], []
    for i in range(1, len(gd)):
        t, p = gd[i], gd[i - 1]
        k = fcal.get_loc(t)
        require(k > 0 and fcal[k - 1] == p, f'{t.date()}：组内前一报价日 {p.date()} 不是期货日历前一日')
        pick = None
        for j, sym in enumerate((main[p], main[t])):
            pc, op, cl = P['close'].at[p, sym], P['open'].at[t, sym], P['close'].at[t, sym]
            if pd.notna(pc) and pd.notna(op) and pd.notna(cl) and pc > 0 and op > 0 and cl > 0:
                pick = (sym, j == 1, float(pc), float(op), float(cl))
                break
        require(pick is not None, f'{t.date()}：持有合约与当日主力都无完整报价')
        sym, isfb, pc, op, cl = pick
        if isfb:
            fallbacks.append({'date': str(t.date()), 'wanted': main[p], 'used': sym})
        ps, src = P['pre_settle'].at[t, sym], 'data'
        if pd.isna(ps):
            ps, src = P['settle'].at[p, sym], 'prev_settle'
        po = P['open'].at[p, sym]               # 仅供自检 1 复刻 ledger 开盘口径的隔夜敞口漂移
        rows.append(dict(date=t, symbol=sym, fallback=isfb, prev_close=pc, open=op,
                         high=float(P['high'].at[t, sym]), low=float(P['low'].at[t, sym]), close=cl,
                         pre_settle=float(ps), pre_settle_src=src,
                         gap=op / pc - 1.0, intra=cl / op - 1.0, cc=cl / pc - 1.0,
                         prev_intra=(pc / float(po) - 1.0) if pd.notna(po) and po > 0 else np.nan))
    h = pd.DataFrame(rows).set_index('date')
    prev = h.symbol.shift(1)
    h['roll'] = prev.notna() & (h.symbol != prev)
    return h, ties, fallbacks


# =========================================================================== 通用日表与口径
def decision_lags(dec, days):
    """p_old = d_{t−2}，p_new = d_{t−1}：在决策（信号）日历上取前二 / 前一个交易日。"""
    cal = dec.index
    k = cal.get_indexer(days)
    require((k >= 2).all(), '决策序列未覆盖样本日（或缺前两日）')
    p_old = pd.Series(dec.values[k - 2], index=days)
    p_new = pd.Series(dec.values[k - 1], index=days)
    require(p_old.notna().all() and p_new.notna().all(), '决策序列在样本内有缺值')
    return p_old, p_new, cal[k - 1], cal[k - 2]


def leg_deltas(p_old, p_new):
    uo, un = p_old.clip(lower=0), p_new.clip(lower=0)
    so, sn = (-p_old).clip(lower=0), (-p_new).clip(lower=0)
    return {'开多': (un - uo).clip(lower=0) + 0.0,
            '平多': -((uo - un).clip(lower=0)) + 0.0,
            '开空': -((sn - so).clip(lower=0)) + 0.0,
            '平空': (so - sn).clip(lower=0) + 0.0}


def add_legs(D):
    for leg, v in leg_deltas(D.p_old, D.p_new).items():
        D['Δ' + leg] = v
    require(np.allclose(sum(D['Δ' + l] for l in LEGS), D.p_new - D.p_old), '四腿之和 ≠ p_new − p_old')


def add_blends(D, groups):
    for c in ('gap', 'intra', 'cc'):
        D[c + '_b'] = sum(D['w_' + g] * D[f'{c}_{g}'] for g in groups)


def policy_returns(D, groups, A):
    """M_A 净日收益（规格 §2）。"""
    pm = D.p_old + sum((D['Δ' + l] for l in A), 0.0)
    gross = sum(D['w_' + g] * ((1 + D.p_old * D['gap_' + g]) * (1 + pm * D['intra_' + g]) - 1)
                for g in groups)
    tc = COST * (D.p_new - D.p_old).abs()
    rc = sum(D['roll_' + g].astype(float) * 2 * COST * D.p_old.abs() * D['w_' + g] for g in groups)
    return gross - tc - rc


def e0_returns(D, groups):
    """E0：t 日整天持有 d_{t−1}，成本落在持仓变化日（同引擎）；换月按开盘前持仓 |d_{t−1}|。"""
    gross = sum(D['w_' + g] * D.p_new * D['cc_' + g] for g in groups)
    tc = COST * (D.p_new - D.p_old).abs()
    rc = sum(D['roll_' + g].astype(float) * 2 * COST * D.p_new.abs() * D['w_' + g] for g in groups)
    return gross - tc - rc


def all_policies(D, groups, policies, order):
    R = pd.DataFrame(index=D.index)
    R['E0'] = e0_returns(D, groups)
    for name, A in policies.items():
        R[name] = policy_returns(D, groups, A)
    return R[order]


# =========================================================================== 事件与统计
def build_events(D, extra=()):
    ex = D[D.p_new != D.p_old]
    rows = []
    for t, r in ex.iterrows():
        flip = '多翻空' if (r.p_old > 0 and r.p_new < 0) else ('空翻多' if (r.p_old < 0 and r.p_new > 0) else '')
        for leg in LEGS:
            d = float(r['Δ' + leg])
            if d != 0:
                rows.append({'date': t, 'year': t.year, 'leg': leg, 'delta': d, 'p_old': r.p_old,
                             'p_new': r.p_new, 'whole_event': flip, 'intra_b': r.intra_b,
                             'gap_b': r.gap_b, 'cc_b': r.cc_b, 'e': d * r.intra_b, 'o': d * r.gap_b,
                             **{c: r[c] for c in extra}})
    ev = pd.DataFrame(rows)
    ev['e_bp'] = ev.e * 1e4
    ev['o_bp'] = ev.o * 1e4
    return ev


def stat_block(e, o, dd, n_days):
    e, o, dd = (np.asarray(x, float) for x in (e, o, dd))
    n = len(e)
    k, nz = int((e > 0).sum()), int((e != 0).sum())
    sd = float(e.std(ddof=1)) if n > 1 else np.nan
    yrs = n_days / ANN
    return {'n': n,
            'mean_e_bp': float(e.mean() * 1e4) if n else np.nan,
            'median_e_bp': float(np.median(e) * 1e4) if n else np.nan,
            'std_e_bp': sd * 1e4,
            'share_open_better': k / n if n else np.nan,
            'sum_e_pp': float(e.sum() * 100),
            'ann_e_pp': float(e.sum() * 100 / yrs),
            't_p': float(stats.ttest_1samp(e, 0.0).pvalue) if (n > 1 and sd > 0) else np.nan,
            'sign_p': float(stats.binomtest(k, nz, 0.5).pvalue) if nz else np.nan,
            'dedrift_mean_bp': float(dd.mean() * 1e4) if n else np.nan,
            'mean_o_bp': float(o.mean() * 1e4) if n else np.nan,
            'sum_o_pp': float(o.sum() * 100),
            'ann_o_pp': float(o.sum() * 100 / yrs)}


def holm(p):
    p = np.asarray(p, float)
    m = len(p)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(np.argsort(p)):
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return adj


def legs_table(ev, D, windows, legs, flips, variants, holm_windows=()):
    rows = []
    for win, (a, b) in windows.items():
        Dw = D.loc[a:b]
        n_days, mu = len(Dw), float(Dw.intra_b.mean())
        for vname, vmask in variants.items():
            evw = ev[(ev.date >= T(a)) & (ev.date <= T(b)) & vmask(ev)].copy()
            evw['dd'] = np.sign(evw.delta) * (evw.intra_b - mu)
            for leg in legs:
                s = evw[evw.leg == leg]
                rows.append({'window': win, 'variant': vname, 'action': leg, 'kind': 'leg',
                             'n_days_window': n_days, 'mu_intra_window_bp': mu * 1e4,
                             **stat_block(s.e, s.o, s.dd, n_days)})
            for fl in flips:
                s = evw[evw.whole_event == fl].groupby('date')[['e', 'o', 'dd']].sum()
                rows.append({'window': win, 'variant': vname, 'action': fl, 'kind': 'whole_event',
                             'n_days_window': n_days, 'mu_intra_window_bp': mu * 1e4,
                             **stat_block(s.e, s.o, s.dd, n_days)})
    out = pd.DataFrame(rows)
    out['holm_t_p'] = np.nan
    out['holm_sign_p'] = np.nan
    for win in holm_windows:
        for v in variants:
            m = (out.window == win) & (out.variant == v) & (out.kind == 'leg')
            out.loc[m, 'holm_t_p'] = holm(out.loc[m, 't_p'].fillna(1.0))
            out.loc[m, 'holm_sign_p'] = holm(out.loc[m, 'sign_p'].fillna(1.0))
    return out


def by_year(ev, legs, flips):
    rows = []
    for y in sorted(ev.year.unique()):
        ey = ev[ev.year == y]
        for leg in legs:
            s = ey[ey.leg == leg]
            rows.append({'year': int(y), 'action': leg, 'n': len(s), 'sum_e_pp': s.e.sum() * 100,
                         'sum_o_pp': s.o.sum() * 100})
        for fl in flips:
            s = ey[ey.whole_event == fl].groupby('date')[['e', 'o']].sum()
            rows.append({'year': int(y), 'action': fl, 'n': len(s), 'sum_e_pp': s.e.sum() * 100,
                         'sum_o_pp': s.o.sum() * 100})
    return pd.DataFrame(rows)


# =========================================================================== 绩效
def perf(r):
    r = r.astype(float)
    require(r.notna().all(), '日收益有缺值')
    return {'n_days': int(len(r)), 'ann': ann_return(r),
            'cagr': float(np.expm1(np.log1p(r).sum() * ANN / len(r))),
            'sharpe': sharpe(r), 'maxdd': max_drawdown(r), 'vol': float(r.std(ddof=1) * np.sqrt(ANN))}


def policies_table(R, windows):
    rows = []
    for win, (a, b) in windows.items():
        x = R.loc[a:b]
        e1 = perf(x['E1'])
        for name in R.columns:
            row = {'policy': name, 'window': win, 'start': str(x.index[0].date()),
                   'end': str(x.index[-1].date()), **perf(x[name])}
            row['d_ann_vs_E1'] = row['ann'] - e1['ann']
            if name == 'E1':
                row.update(d_sharpe_vs_E1=0.0, ci_lo=np.nan, ci_hi=np.nan, p_value=np.nan,
                           boot_mean=np.nan, boot_sd=np.nan)
            else:
                bt = paired_block_bootstrap_sharpe_diff(x[name], x['E1'], **BOOT)
                row.update(d_sharpe_vs_E1=bt['diff_sharpe'], ci_lo=bt['ci_lo'], ci_hi=bt['ci_hi'],
                           p_value=bt['p_value'], boot_mean=bt['boot_mean'], boot_sd=bt['boot_sd'])
            rows.append(row)
    return pd.DataFrame(rows)


# =========================================================================== 期货池 F
def build_F(inp, dec=None):
    fut, pos_f, fac_f = inp['fut'], inp['pos_f'], inp['fac_f']
    fcal = pd.DatetimeIndex(sorted(fut.date.unique()))
    first_im = fut.loc[fut.symbol.str.startswith('IM'), 'date'].min()
    held, ties, fbs = {}, {}, {}
    for g in F_GROUPS:
        held[g], ties[g], fbs[g] = held_frame(fut[fut.symbol.str.startswith(g)].copy(), fcal)
    first_day = held['IC'].index.min()
    days = fcal[(fcal >= first_day) & (fcal <= END)]
    dec = pos_f if dec is None else dec
    p_old, p_new, d1_dates, d2_dates = decision_lags(dec, days)
    D = pd.DataFrame(index=days)
    D.index.name = 'date'
    D['p_old'], D['p_new'] = p_old, p_new
    D['d_tm1_date'], D['d_tm2_date'] = d1_dates, d2_dates
    add_legs(D)
    wIC = np.where(days > first_im, 0.5, 1.0)
    D['w_IC'], D['w_IM'] = wIC, 1.0 - wIC
    for g in F_GROUPS:
        h = held[g].reindex(days)
        need = D['w_' + g] > 0
        require(h.loc[need, 'gap'].notna().all(), f'{g}：权重>0 的日子缺持有合约收益')
        for c in ('gap', 'intra', 'cc'):
            D[f'{c}_{g}'] = h[c].where(need, 0.0).fillna(0.0)
        D['roll_' + g] = (h['roll'] == True) & need  # noqa: E712 —— reindex 后缺日为 NaN → False
        D['sym_' + g] = h['symbol'].where(need, '').fillna('')
        for c in ('open', 'high', 'low', 'close', 'prev_close', 'pre_settle', 'prev_intra'):
            D[f'{c}_{g}'] = h[c].where(need)
        D['pre_settle_src_' + g] = h['pre_settle_src'].where(need, '').fillna('')
    add_blends(D, F_GROUPS)
    meta = dict(fcal=fcal, first_im=first_im, held=held, ties=ties, fallbacks=fbs, first_day=first_day)
    return D, meta


def limit_table(D):
    ex = D[D.p_new != D.p_old]
    rows = []
    for t, r in ex.iterrows():
        for g in F_GROUPS:
            if r['w_' + g] <= 0:
                continue
            o, h, l, ps = r['open_' + g], r['high_' + g], r['low_' + g], r['pre_settle_' + g]
            ratio = o / ps - 1.0
            f1, f2 = abs(ratio) >= LIMIT_TH, bool(o == h == l)
            rows.append({'date': t, 'group': g, 'symbol': r['sym_' + g], 'w': r['w_' + g],
                         'p_old': r.p_old, 'p_new': r.p_new, 'open': o, 'high': h, 'low': l,
                         'pre_settle': ps, 'pre_settle_src': r['pre_settle_src_' + g],
                         'open_vs_pre_settle': ratio, 'flag_limit_9_5pct': bool(f1), 'flag_O_eq_H_eq_L': f2,
                         'flagged': bool(f1 or f2)})
    return pd.DataFrame(rows)


# =========================================================================== 现货池 S
def build_S(inp, heldF):
    spot, pos_s = inp['spot'], inp['pos_s']
    close = spot.pivot(index='date', columns='symbol', values='close').sort_index()
    opn = spot.pivot(index='date', columns='symbol', values='open').sort_index()
    require(close.notna().all().all(), '现货两腿日历不一致')
    scal = close.index
    days = scal[(scal >= S_START) & (scal <= END)]
    p_old, p_new, d1_dates, d2_dates = decision_lags(pos_s, days)
    cc = close.pct_change()
    ic, im = heldF['IC'], heldF['IM']
    require(days.isin(ic.index).all(), 'S 样本日缺 IC 持有合约 intra（代理不可得）')
    im_ok = days.isin(im.index)
    proxy = {'905': ic.intra.reindex(days),
             '852': pd.Series(np.where(im_ok, im.intra.reindex(days), ic.intra.reindex(days)), index=days)}
    proxy_src = {'905': pd.Series('IC:' + ic.symbol.reindex(days), index=days),
                 '852': pd.Series(np.where(im_ok, 'IM:' + im.symbol.reindex(days).fillna(''),
                                           'IC:' + ic.symbol.reindex(days)), index=days)}
    D = pd.DataFrame(index=days)
    D.index.name = 'date'
    D['p_old'], D['p_new'] = p_old, p_new
    D['d_tm1_date'], D['d_tm2_date'] = d1_dates, d2_dates
    add_legs(D)
    for g in S_GROUPS:
        code = S_CODE[g]
        D['w_' + g] = 0.5
        D['cc_' + g] = cc[code].reindex(days)
        D['intra_' + g] = proxy[g]
        D['gap_' + g] = (1 + D['cc_' + g]) / (1 + D['intra_' + g]) - 1
        D['roll_' + g] = False
        D['proxy_src_' + g] = proxy_src[g]
        D['close_' + g] = close[code].reindex(days)
        D['prevclose_' + g] = close[code].shift(1).reindex(days)
        D['open_real_' + g] = opn[code].reindex(days)
    require(D.filter(regex='^(cc|intra|gap)_').notna().all().all(), 'S 日表有缺值')
    add_blends(D, S_GROUPS)
    return D, dict(scal=scal, close=close, open=opn, cc=cc, im_proxy_first=days[im_ok].min())


def s_exact_frame(D):
    """真实指数开盘价的日子（两腿都有 open>0）：用真实 gap/intra 替换代理。"""
    ok = np.ones(len(D), bool)
    for g in S_GROUPS:
        ok &= (D['open_real_' + g] > 0).fillna(False).to_numpy()
    E = D[ok].copy()
    for g in S_GROUPS:
        E['intra_proxy_' + g] = E['intra_' + g]
        E['gap_proxy_' + g] = E['gap_' + g]
        E['intra_' + g] = E['close_' + g] / E['open_real_' + g] - 1
        E['gap_' + g] = E['open_real_' + g] / E['prevclose_' + g] - 1
    E['intra_proxy_b'] = E.intra_b
    E['gap_proxy_b'] = E.gap_b
    add_blends(E, S_GROUPS)
    return E


def cmp_stats(real, proxy):
    real, proxy = np.asarray(real, float), np.asarray(proxy, float)
    d = real - proxy
    return {'n': len(real), 'corr': float(np.corrcoef(real, proxy)[0, 1]),
            'mean_real_bp': float(real.mean() * 1e4), 'mean_proxy_bp': float(proxy.mean() * 1e4),
            'mean_diff_bp': float(d.mean() * 1e4), 'rmse_bp': float(np.sqrt((d ** 2).mean()) * 1e4),
            'sign_agree': float((np.sign(real) == np.sign(proxy)).mean()),
            'std_real_bp': float(real.std(ddof=1) * 1e4), 'std_proxy_bp': float(proxy.std(ddof=1) * 1e4)}


# =========================================================================== 自检
def check_ledger(inp, metaF, RF, DF):
    """自检 1：execution_ledger.contract_ledger（close/open，3bps）对照本模型 E1/E2。"""
    fut, pos_f = inp['fut'], inp['pos_f']
    fcal, first_im = metaF['fcal'], metaF['first_im']
    spot_cal = inp['spot'].pivot(index='date', columns='symbol', values='close').index
    idx = fcal[fcal <= END]                      # 2015-04-16 起（同 execution_audit START），首日平仓初始化
    sig = pos_f.reindex(idx)
    require(sig.notna().all(), 'ledger 信号缺日')
    w = futures_weights(idx, first_im)
    expiries = {}
    for sym in fut.symbol.unique():              # 照抄 execution_audit.inputs 的到期表逻辑
        expiry = pd.Timestamp(_expiry_from_symbol(sym))
        nxt = spot_cal[spot_cal >= expiry]
        expiries[sym] = nxt[0] if len(nxt) else expiry
    a, b = F_WINDOWS['full']
    out = {'ledger_index_start': str(idx[0].date()), 'compare_window': [a, b]}
    ledgers = {}
    for mine, fill in (('E1', 'close'), ('E2', 'open')):
        L = contract_ledger(fut, sig, w, fill=fill, cost_bps=3., expiries=expiries)
        ledgers[fill] = L
        lr, mr = L.ret.loc[a:b], RF[mine].loc[a:b]
        require(lr.index.equals(mr.index), 'ledger 与本模型日历不一致')
        diff = mr - lr
        # 本模型当日 P&L 合约集合 / 权重 vs ledger（close：前一行 symbols；open：隔夜=前一行、日内=当行）
        my_set = DF.loc[a:b].apply(lambda r: frozenset(s for g, s in (('IC', r.sym_IC), ('IM', r.sym_IM))
                                                        if r['w_' + g] > 0), axis=1)
        lsym = L.symbols.fillna('').apply(lambda s: frozenset(x for x in s.split('|') if x))
        prev_sym = lsym.shift(1).loc[a:b]
        cur_sym = lsym.loc[a:b]
        dec_prev = L.decision_signal.shift(1).loc[a:b]
        wprev = w.shift(1).loc[a:b]
        wcur = w.loc[a:b]
        w_change = (wprev.IC != wcur.IC)
        if fill == 'close':
            # ledger 在 t 日的 P&L 来自前一收盘建立的持仓（合约=前一行 symbols），平仓（决策 0）则空集
            mism = pd.Series([(ps != ms) if dp != 0 else False for ps, ms, dp in
                              zip(prev_sym, my_set, dec_prev)], index=diff.index)
        else:
            dec_cur = L.decision_signal.loc[a:b]
            mism = pd.Series([((ps != ms) and dp != 0) or ((cs != ms) and dc != 0) for ps, cs, ms, dp, dc in
                              zip(prev_sym, cur_sym, my_set, dec_prev, dec_cur)], index=diff.index)
        first = pd.Series(diff.index == diff.index[0], index=diff.index)
        cat_mis = mism | w_change | first
        my_cost = (COST * (DF.p_new - DF.p_old).abs()
                   + sum(DF['roll_' + g].astype(float) * 2 * COST * DF.p_old.abs() * DF['w_' + g]
                         for g in F_GROUPS)).loc[a:b]
        led_cost = L.cost_return.loc[a:b]
        cost_diff = -(my_cost - led_cost)        # 成本差对 (mine − ledger) 的贡献
        oth = ~cat_mis
        resid_other = diff[oth] - cost_diff[oth]
        # 再平衡时点二阶项：本模型逐组乘积式 = 每组敞口在前收与开盘都重置到目标；
        #   close ledger 只在收盘重置 → 当日 = p_old·cc；
        #   open ledger 只在开盘重置 → 隔夜敞口带着前一日日内漂移 (1+intra_{t−1})/(1+Σ p_old·w·intra_{t−1})，
        #   日内 = 组合层面 (1+x)(1+Σ p_new·w·intra) − 1（ledger 同构复刻，忽略费用对敞口的高阶影响）。
        mine_gross = sum(DF['w_' + g] * ((1 + DF.p_old * DF['gap_' + g])
                                         * (1 + (DF.p_old if fill == 'close' else DF.p_new) * DF['intra_' + g]) - 1)
                         for g in F_GROUPS)
        if fill == 'close':
            led_form = sum(DF['w_' + g] * DF.p_old * DF['cc_' + g] for g in F_GROUPS)
        else:
            wp = {g: DF['w_' + g].shift(1) for g in F_GROUPS}
            den = 1 + sum(DF.p_old * wp[g] * DF['prev_intra_' + g].fillna(0.0) for g in F_GROUPS)
            x = sum(DF.p_old * wp[g] * (1 + DF['prev_intra_' + g].fillna(0.0)) * DF['gap_' + g] for g in F_GROUPS) / den
            led_form = (1 + x) * (1 + sum(DF.p_new * DF['w_' + g] * DF['intra_' + g] for g in F_GROUPS)) - 1
        theo = (mine_gross - led_form).loc[a:b]
        im_entry = pd.Series(w_change.to_numpy(), index=diff.index)
        roll_like = cat_mis & ~im_entry & ~first
        pm, pl = perf(mr), perf(lr)
        rec = {'model': mine, 'ledger_fill': fill,
               'model_full': {k: pm[k] for k in ('ann', 'sharpe', 'maxdd', 'n_days')},
               'ledger_full': {k: pl[k] for k in ('ann', 'sharpe', 'maxdd', 'n_days')},
               'd_ann_pp': (pm['ann'] - pl['ann']) * 100, 'd_sharpe': pm['sharpe'] - pl['sharpe'],
               'd_maxdd_pp': (pm['maxdd'] - pl['maxdd']) * 100,
               'daily_corr': float(np.corrcoef(mr, lr)[0, 1]),
               'ledger_rolls': int(L.rolls.loc[a:b].sum()),
               'model_roll_days_IC_plus_IM': int(DF.loc[a:b, ['roll_IC', 'roll_IM']].sum().sum()),
               'ledger_cost_ann_pp': float(led_cost.mean() * ANN * 100),
               'model_cost_ann_pp': float(my_cost.mean() * ANN * 100),
               'decomp_ann_pp': {
                   'total_diff': float(diff.mean() * ANN * 100),
                   'contract_or_weight_mismatch_days(换月时点/IM 入场/首日)': float(diff[cat_mis].sum() / len(diff) * ANN * 100),
                   'of_which_roll_timing_days': float(diff[roll_like].sum() / len(diff) * ANN * 100),
                   'of_which_IM_entry_day_2022-07-25': float(diff[im_entry].sum() / len(diff) * ANN * 100),
                   'of_which_first_day': float(diff[first].sum() / len(diff) * ANN * 100),
                   'cost_diff_on_other_days(再平衡/换月/成交成本口径)': float(cost_diff[oth].sum() / len(diff) * ANN * 100),
                   'residual_on_other_days': float(resid_other.sum() / len(diff) * ANN * 100)},
               'n_mismatch_days': int(cat_mis.sum()), 'n_roll_timing_days': int(roll_like.sum()),
               'max_abs_daily_diff_bp_mismatch_days': float(diff[cat_mis].abs().max() * 1e4),
               'max_abs_daily_diff_bp_other_days': float(diff[oth].abs().max() * 1e4),
               'max_abs_daily_diff_other_days_date': str(diff[oth].abs().idxmax().date()),
               'rebalance_term_bp_on_that_date': float(theo.loc[diff[oth].abs().idxmax()] * 1e4),
               'cost_diff_bp_on_that_date': float(cost_diff.loc[diff[oth].abs().idxmax()] * 1e4),
               'max_abs_residual_bp_other_days': float(resid_other.abs().max() * 1e4)}
        r2 = resid_other - theo[oth]
        rec['decomp_ann_pp']['of_which_rebalance_point_term'] = float(theo[oth].sum() / len(diff) * ANN * 100)
        rec['decomp_ann_pp']['residual_after_rebalance_term'] = float(r2.sum() / len(diff) * ANN * 100)
        rec['max_abs_residual_after_rebalance_term_bp'] = float(r2.abs().max() * 1e4)
        rec['corr_residual_vs_rebalance_term'] = float(np.corrcoef(resid_other, theo[oth])[0, 1])
        rec['rebalance_point_term_definition'] = (
            '本模型逐组乘积式 − close ledger 的 p_old·cc（空头日 = 2·gap·intra）' if fill == 'close' else
            '本模型逐组乘积式 − open ledger 同构式 (1+x)(1+Σp_new·w·intra)−1，x 含前一日日内漂移（空头日 ≈ 2·intra_{t−1}·gap_t）')
        out[f'{mine}_vs_ledger_{fill}'] = rec
    # ledger 口径下的「开盘 − 收盘」与本模型对照（差异几乎全部落在换月时点日）
    lo, lc = ledgers['open'].ret.loc[a:b], ledgers['close'].ret.loc[a:b]
    e1, e2 = out['E1_vs_ledger_close'], out['E2_vs_ledger_open']
    out['open_minus_close'] = {
        'model_E2_minus_E1_ann_pp': (ann_return(RF['E2'].loc[a:b]) - ann_return(RF['E1'].loc[a:b])) * 100,
        'ledger_open_minus_close_ann_pp': (ann_return(lo) - ann_return(lc)) * 100,
        'model_E2_minus_E1_sharpe': sharpe(RF['E2'].loc[a:b]) - sharpe(RF['E1'].loc[a:b]),
        'ledger_open_minus_close_sharpe': sharpe(lo) - sharpe(lc),
        'gap_explained_by_roll_timing_days_pp': e2['decomp_ann_pp']['of_which_roll_timing_days'] - e1['decomp_ann_pp']['of_which_roll_timing_days'],
        'gap_explained_by_IM_entry_and_first_day_pp': (e2['decomp_ann_pp']['of_which_IM_entry_day_2022-07-25'] + e2['decomp_ann_pp']['of_which_first_day']
                                                       - e1['decomp_ann_pp']['of_which_IM_entry_day_2022-07-25'] - e1['decomp_ann_pp']['of_which_first_day']),
        'note': '规格模型里 E1、E2 在同一天持有同一张合约（换月都在前一收盘），E2−E1 只含信号交易的成交时点；'
                'ledger 的换月与信号交易同时成交（开盘口径在开盘换月、收盘口径在收盘换月），其 open−close 还含换月时点差。'}
    ref = pd.read_csv(RUN_0914)
    out['run_20260914_r2_reference(信号文件版本不同，不要求一致)'] = {
        r['name']: {k: r[k] for k in ('start', 'end', 'n', 'ann', 'sharpe', 'maxdd', 'rolls')}
        for _, r in ref[(ref.window == 'full') & ref.name.isin(['futures_close_3bps', 'futures_open_3bps'])].iterrows()}
    return out, ledgers


def check_engine_S(inp, RS):
    """自检 2：S 的 E0 与 engine.run_strategy(position, blend_cc, 3.0) 在窗内逐日一致。"""
    close = inp['spot'].pivot(index='date', columns='symbol', values='close').sort_index()
    blend_cc = close.pct_change().mean(axis=1)
    pos = inp['pos_s'].reindex(close.index)
    full = run_strategy(pos, blend_cc, 3.0)['ret']
    out = {}
    for win, (a, b) in S_WINDOWS.items():
        mine = RS['E0'].loc[a:b]
        d_full = (mine - full.loc[a:b]).abs()
        sliced = run_strategy(pos.loc[a:b], blend_cc.loc[a:b], 3.0)['ret']
        d_sl = (mine - sliced).abs()
        bad = d_sl[d_sl > 1e-12]
        out[win] = {'n_days': int(len(mine)),
                    'max_abs_diff_vs_full_series_run': float(d_full.max()),
                    'n_days_diff_vs_full_series_run(>1e-12)': int((d_full > 1e-12).sum()),
                    'windowed_run_n_days_diff(>1e-12)': int(len(bad)),
                    'windowed_run_diff_days': [str(x.date()) for x in bad.index],
                    'windowed_run_diff_only_first_two_days': bool(set(bad.index) <= set(mine.index[:2]))}
    out['pass'] = all(v['max_abs_diff_vs_full_series_run'] < 1e-12 and v['windowed_run_diff_only_first_two_days']
                      for k, v in out.items() if k != 'pass')
    return out


def check_additivity(D, R, ev, windows, groups, policies):
    """自检 3：Σ(M_A − E1) ≈ Σ_{leg∈A} e；残差 = Σ_t Σ_g w_g·p_old·Δ_A·gap_g·intra_g（二阶项）。"""
    out = {}
    for win, (a, b) in windows.items():
        Dw, Rw = D.loc[a:b], R.loc[a:b]
        evw = ev[(ev.date >= T(a)) & (ev.date <= T(b))]
        rec = {}
        for name, A in policies.items():
            if name == 'E1':
                continue
            dA = sum((Dw['Δ' + l] for l in A), 0.0)
            lhs = float((Rw[name] - Rw['E1']).sum())
            sum_e = float(evw[evw.leg.isin(A)].e.sum())
            analytic = float(sum(Dw['w_' + g] * Dw.p_old * dA * Dw['gap_' + g] * Dw['intra_' + g]
                                 for g in groups).sum())
            rec[name] = {'sum_diff_pp': lhs * 100, 'sum_e_pp': sum_e * 100,
                         'residual_pp': (lhs - sum_e) * 100, 'analytic_second_order_pp': analytic * 100,
                         'residual_minus_analytic': lhs - sum_e - analytic}
        out[win] = rec
    worst = max(abs(r['residual_minus_analytic']) for w_ in out.values() for r in w_.values())
    out['max_abs_residual_minus_analytic'] = worst
    out['pass'] = worst < 1e-12
    return out


def check_timing(inp):
    """自检 4：决策序列前移 / 后移一天重算 F 的 E1/E2 年化。"""
    a, b = F_WINDOWS['full']
    res = {}
    for tag, dec in (('main', inp['pos_f']),
                     ('advance_1d(d_T←d_{T+1})', inp['pos_f'].shift(-1)),
                     ('delay_1d(d_T←d_{T−1})', inp['pos_f'].shift(1))):
        D, _ = build_F(inp, dec=dec)
        R = all_policies(D, F_GROUPS, F_POLICIES, F_ORDER)
        res[tag] = {k: ann_return(R[k].loc[a:b]) for k in ('E0', 'E1', 'E2')}
    m = res['main']
    adv, dly = res['advance_1d(d_T←d_{T+1})'], res['delay_1d(d_T←d_{T−1})']
    diffs = {tag: {k: (v[k] - m[k]) * 100 for k in ('E1', 'E2')} for tag, v in res.items() if tag != 'main'}
    ok = all(abs(x) > 1e-6 for d in diffs.values() for x in d.values())
    return {'ann_full': res, 'diff_vs_main_pp': diffs,
            'consistency(应近似相等，只差成本落日/二阶项)': {
                'advance_E1_minus_main_E0_pp': (adv['E1'] - m['E0']) * 100,
                'delay_E0_minus_main_E1_pp': (dly['E0'] - m['E1']) * 100},
            'pass': ok}


# =========================================================================== 手算例子
def hand_example(inp, D, ev):
    """独立路径：直接读原始行，按最朴素的算术重算一个多翻空日的 e、o、E1、E2。"""
    flips = ev[(ev.whole_event == '多翻空') & (ev.date >= T('2022-07-25'))]
    t = flips.date.min()
    fut = inp['fut']
    fcal = list(pd.DatetimeIndex(sorted(fut.date.unique())))
    tp, tpp = fcal[fcal.index(t) - 1], fcal[fcal.index(t) - 2]
    sig = inp['pos_f']
    scal = list(sig.index)
    p_old, p_new = float(sig.iloc[scal.index(t) - 2]), float(sig.iloc[scal.index(t) - 1])
    lines, gross1, gross2, intra_b, gap_b, roll_cost = [], 0.0, 0.0, 0.0, 0.0, 0.0
    wts = {'IC': 0.5, 'IM': 0.5}

    def raw_main(day, g):
        x = fut[(fut.date == day) & fut.symbol.str.startswith(g)].sort_values('oi', ascending=False)
        return x.symbol.iloc[0]

    for g in F_GROUPS:
        sym = raw_main(tp, g)                     # held(t) = main(t−1)
        prev_held = raw_main(tpp, g)              # held(t−1) = main(t−2)
        rp = fut[(fut.date == tp) & (fut.symbol == sym)].iloc[0]
        rt = fut[(fut.date == t) & (fut.symbol == sym)].iloc[0]
        gap, intra, cc = rt.open / rp.close - 1, rt.close / rt.open - 1, rt.close / rp.close - 1
        gross1 += wts[g] * ((1 + p_old * gap) * (1 + p_old * intra) - 1)
        gross2 += wts[g] * ((1 + p_old * gap) * (1 + p_new * intra) - 1)
        intra_b += wts[g] * intra
        gap_b += wts[g] * gap
        rolled = prev_held != sym
        roll_cost += (2 * COST * abs(p_old) * wts[g]) if rolled else 0.0
        lines.append({'group': g, 'held(t)=main(t−1)': sym, 'oi(t−1)': int(rp.oi), 'C_{t−1}': float(rp.close),
                      'O_t': float(rt.open), 'C_t': float(rt.close), 'gap': gap, 'intra': intra, 'cc': cc,
                      'rolled_today': bool(rolled)})
    tc = COST * abs(p_new - p_old)
    e_leg = -1 * intra_b
    o_leg = -1 * gap_b
    out = {'date': str(t.date()), 'prev_day': str(pd.Timestamp(tp).date()), 'p_old': p_old, 'p_new': p_new,
           'groups': lines, 'intra_blend': intra_b, 'gap_blend': gap_b,
           'e_平多': e_leg, 'e_开空': e_leg, 'e_多翻空': 2 * e_leg, 'o_多翻空': 2 * o_leg,
           'E1_gross': gross1, 'E2_gross': gross2, 'trade_cost': tc, 'roll_cost': roll_cost,
           'E1_net': gross1 - tc - roll_cost, 'E2_net': gross2 - tc - roll_cost}
    pipe_e = ev[(ev.date == t)].e.to_numpy()
    out['pipeline'] = {'e_legs': list(map(float, pipe_e)), 'E1': None, 'E2': None}
    return out


# =========================================================================== 报告
def fp(x, nd=2):
    return '—' if x is None or (isinstance(x, float) and not math.isfinite(x)) else f'{x * 100:.{nd}f}%'


def fn(x, nd=2):
    return '—' if x is None or (isinstance(x, float) and not math.isfinite(x)) else f'{x:.{nd}f}'


def fpv(p):
    if p is None or (isinstance(p, float) and not math.isfinite(p)):
        return '—'
    return f'{p:.3f}' if p >= 0.001 else f'{p:.1e}'


def md(rows, headers):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|']
    out += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
    return '\n'.join(out)


def legs_md(tbl, actions, holm=True):
    rows = []
    for act in actions:
        r = tbl[tbl.action == act].iloc[0]
        rows.append([act, int(r.n), fn(r.mean_e_bp, 1), fn(r.median_e_bp, 1), fn(r.std_e_bp, 1),
                     fp(r.share_open_better, 0) if r.n else '—', fn(r.sum_e_pp, 2), fn(r.ann_e_pp, 3),
                     fpv(r.t_p), fpv(r.sign_p)] + ([fpv(r.holm_t_p), fpv(r.holm_sign_p)] if holm else [])
                    + [fn(r.dedrift_mean_bp, 1), fn(r.mean_o_bp, 1), fn(r.sum_o_pp, 2)])
    head = ['动作', 'n', 'e 均值bp', 'e 中位bp', 'e 标准差bp', '开盘更好', 'e 合计pp', '年化pp/年', 't 检验 p',
            '符号检验 p'] + (['Holm(t)', 'Holm(符号)'] if holm else []) + ['去漂移均值bp', 'o 均值bp', 'o 合计pp']
    return md(rows, head)


def pol_md(P, order, win):
    rows = []
    for name in order:
        r = P[(P.policy == name) & (P.window == win)].iloc[0]
        ci = '—' if name == 'E1' else f'[{fn(r.ci_lo, 3)}, {fn(r.ci_hi, 3)}]'
        rows.append([name, fp(r.ann), fp(r.cagr), fn(r.sharpe, 3), fp(r.maxdd), fp(r.vol), int(r.n_days),
                     fn(r.d_ann_vs_E1 * 100, 2), '—' if name == 'E1' else fn(r.d_sharpe_vs_E1, 3), ci,
                     fpv(r.p_value)])
    return md(rows, ['策略', '算术年化', 'CAGR', 'Sharpe', '最大回撤', '年化波动', '日数', 'Δ年化 vs E1 (pp)',
                     'ΔSharpe vs E1', '95% CI', 'p'])


def write_report(ctx):
    k = ctx
    LF, LS, PF, PS = k['legs_F'], k['legs_S'], k['pol_F'], k['pol_S']
    C = k['checks']
    lf_full = LF[(LF.window == 'full') & (LF.variant == 'all')]
    lf_ex = LF[(LF.window == 'full') & (LF.variant == 'ex_limit')]
    ls_full = LS[(LS.window == 'full') & (LS.variant == 'all')]
    pf = PF[PF.window == 'full'].set_index('policy')
    ps = PS[PS.window == 'full'].set_index('policy')
    e2 = pf.loc['E2']
    s2 = ps.loc['E2']
    pv = k['proxy']
    split = k['split_events']
    L = []
    A = L.append
    A('# 开盘价 vs 收盘价执行检验（2026-10-09 插播）')
    A('')
    A('> 按冻结规格 `../spec.md` 实现；只用 `../inputs/`（sha256 已逐个核对）；不连库、不改仓库。'
      '重现：`cd /home/elfbob/claude-code/style_timing_signal && python3 ' + str(W / 'run.py') + '`。'
      '本检验是执行口径的测量，不设 GO/STOP 闸门。')
    A('')
    A('## 结论速览')
    A('')
    fl_ = {act: lf_full[lf_full.action == act].iloc[0] for act in FLIPS}
    sl_ = {act: ls_full[ls_full.action == act].iloc[0] for act in ('开多', '平多')}
    ci0 = lambda r: r.ci_lo <= 0 <= r.ci_hi
    win_txt = '、'.join(f"{w} {fn(PF[(PF.window == w) & (PF.policy == 'E2')].d_sharpe_vs_E1.iloc[0], 2)}"
                        for w in ('2015-2020', '2021-2023', '2024-2026'))
    A(f"**一句话**：F 改开盘执行 Δ年化 {fn(e2.d_ann_vs_E1*100,2)}pp、ΔSharpe {fn(e2.d_sharpe_vs_E1,3)}"
      f"（95% CI {'含' if ci0(e2) else '不含'} 0），变化主要落在「多翻空」（e 合计 {fn(fl_['多翻空'].sum_e_pp,2)}pp，"
      f"开盘卖出后当日日内平均再涨 {fn(-fl_['多翻空'].mean_e_bp/2,1)}bp/腿），「空翻多」为 {fn(fl_['空翻多'].sum_e_pp,2)}pp；"
      f"分窗 ΔSharpe {win_txt}，方向不稳定。S-proxy 改开盘 Δ年化 {fn(s2.d_ann_vs_E1*100,2)}pp、ΔSharpe {fn(s2.d_sharpe_vs_E1,3)}"
      f"（95% CI {'含' if ci0(s2) else '不含'} 0），开多 / 平多两腿各 {fn(sl_['开多'].sum_e_pp,2)} / {fn(sl_['平多'].sum_e_pp,2)}pp。"
      + ('两池全窗各动作（含整笔事件）的 t 检验与符号检验原始 p 均 > 0.05。'
         if (pd.concat([lf_full, ls_full])[['t_p', 'sign_p']] > 0.05).all().all()
         else '注意：有动作的原始 p ≤ 0.05（见 §3）。'))
    A('')
    A(f"- **F（equal_weight 对称，IC/IM 实际合约，{k['F_first']}~{k['F_last']}，{k['F_n']} 日）**："
      f"E1（T+1 收盘）年化 {fp(pf.loc['E1','ann'])}、Sharpe {fn(pf.loc['E1','sharpe'],3)}、回撤 {fp(pf.loc['E1','maxdd'])}；"
      f"E2（T+1 开盘）年化 {fp(e2.ann)}、Sharpe {fn(e2.sharpe,3)}、回撤 {fp(e2.maxdd)}。"
      f"ΔSharpe(E2−E1) = {fn(e2.d_sharpe_vs_E1,3)}，95% CI [{fn(e2.ci_lo,3)}, {fn(e2.ci_hi,3)}]，p = {fpv(e2.p_value)}；"
      f"Δ年化 = {fn(e2.d_ann_vs_E1*100,2)} pp。E0（T 收盘，不可行参照）年化 {fp(pf.loc['E0','ann'])}、Sharpe {fn(pf.loc['E0','sharpe'],3)}。")
    rows = []
    for act in LEGS + FLIPS:
        r = lf_full[lf_full.action == act].iloc[0]
        rows.append(f"{act} n={int(r.n)}、e 均值 {fn(r.mean_e_bp,1)}bp、合计 {fn(r.sum_e_pp,2)}pp、t-p {fpv(r.t_p)}")
    legs4 = lf_full[lf_full.kind == 'leg']
    sig_txt = ('四腿原始 p 均 > 0.05' if (legs4.t_p > 0.05).all() and (legs4.sign_p > 0.05).all() else '有腿原始 p ≤ 0.05')
    A('- F 逐动作 e（正 = 开盘成交更好）：' + '；'.join(rows) +
      f"。{sig_txt}；Holm 校正后最小 p：t 检验 {fpv(legs4.holm_t_p.min())}、符号检验 {fpv(legs4.holm_sign_p.min())}。")
    oc = C['1_ledger']['open_minus_close']
    A(f"- **F 的换月时点敏感性**：同一快照上用 `contract_ledger`（换月与信号交易同时成交）得到的「开盘 − 收盘」是 "
      f"{fn(oc['ledger_open_minus_close_ann_pp'],2)}pp/年（ΔSharpe {fn(oc['ledger_open_minus_close_sharpe'],3)}），"
      f"规格模型（换月固定在前一收盘）是 {fn(oc['model_E2_minus_E1_ann_pp'],2)}pp/年；差几乎全部落在换月时点日（§5-1）。")
    A(f"- **S-proxy（slope20 long-flat，blend 现货，{k['S_first']}~{k['S_last']}，{k['S_n']} 日）**："
      f"E1 年化 {fp(ps.loc['E1','ann'])}、Sharpe {fn(ps.loc['E1','sharpe'],3)}；E2 年化 {fp(s2.ann)}、Sharpe {fn(s2.sharpe,3)}；"
      f"ΔSharpe = {fn(s2.d_sharpe_vs_E1,3)} [{fn(s2.ci_lo,3)}, {fn(s2.ci_hi,3)}]，p = {fpv(s2.p_value)}。"
      + '；'.join(f"{act} n={int(r.n)}、e 均值 {fn(r.mean_e_bp,1)}bp、合计 {fn(r.sum_e_pp,2)}pp、t-p {fpv(r.t_p)}"
                 for act in ('开多', '平多') for r in [ls_full[ls_full.action == act].iloc[0]]) + '。')
    A(f"- **代理验证**（{pv['n_days']} 个有真实指数开盘价的交易日）：blend intra 真实 vs 代理 相关 {fn(pv['blend_intra']['corr'],3)}、"
      f"均值差 {fn(pv['blend_intra']['mean_diff_bp'],1)}bp、RMSE {fn(pv['blend_intra']['rmse_bp'],1)}bp、符号一致 {fp(pv['blend_intra']['sign_agree'],0)}。")
    A('- **自检**：' + '；'.join(f"{name} {'通过' if ok else '不通过'}" for name, ok in k['check_summary']) + '（详见 §5）。')
    A('')
    A('## 3. 主表：逐动作事件归因')
    A('')
    A('口径：每个执行日 t（d_{t−1} ≠ d_{t−2}）每条非零腿一条事件；e = Δ_leg × Σ_g w_g·intra_g(t)（正 = 开盘成交更好），'
      'o = Δ_leg × Σ_g w_g·gap_g(t)（正 = 隔夜已朝新仓位走、等到开盘就错过了，即 E0 相对 E2 的差）。'
      '年化 = 合计 ÷（窗内交易日 / 245）。t 检验与符号检验均双侧；去漂移均值 = 均值 sign(Δ)×(intra_t − 窗内全部交易日 intra 均值)。')
    A('')
    A(f"**关于「平多与开空同日、平空与开多同日」**：规格称 F 仓位样本内恒为 ±1，实测**不完全成立**——"
      f"{k['zero_note']} 因此除这一次外，平多与开空、平空与开多总是同日发生、单腿效应（e、o）完全相同；"
      f"这一次的 +1→0→−1 拆成两天，各只有一条腿（{split}）。整笔事件「多翻空 / 空翻多」只统计同日翻转（效应 = 两腿之和）。")
    A('')
    A('### 3.1 F 全窗（2015-04-17~2026-10-08），全部事件')
    A('')
    A(legs_md(lf_full, LEGS + FLIPS))
    A('')
    A('Holm 校正只对 F 全窗四腿做（整笔事件不入族）；注意平多≡开空、平空≡开多（除上述一次外），四个检验实际只有两组独立信息。')
    A('')
    A(f"### 3.2 F 全窗，剔除「开盘可能成交不了」事件（共 {k['limit_n_events']} 条腿事件 / {k['limit_n_days']} 个执行日被标记）")
    A('')
    A(legs_md(lf_ex, LEGS + FLIPS))
    A('')
    A('### 3.3 F 分窗（全部事件）')
    A('')
    rows = []
    for win in F_WINDOWS:
        for act in LEGS + FLIPS:
            r = LF[(LF.window == win) & (LF.variant == 'all') & (LF.action == act)].iloc[0]
            rows.append([win, act, int(r.n), fn(r.mean_e_bp, 1), fn(r.sum_e_pp, 2), fn(r.ann_e_pp, 3), fpv(r.t_p),
                         fpv(r.sign_p), fn(r.dedrift_mean_bp, 1), fn(r.sum_o_pp, 2)])
    A(md(rows, ['窗', '动作', 'n', 'e 均值bp', 'e 合计pp', '年化pp/年', 't-p', '符号 p', '去漂移bp', 'o 合计pp']))
    A('')
    A('### 3.4 S-proxy（主读数，近似；intra 用期货持有合约代理，gap 由 cc 反推）')
    A('')
    A(legs_md(ls_full, ('开多', '平多'), holm=False))
    A('')
    rows = []
    for win in S_WINDOWS:
        for act in ('开多', '平多'):
            r = LS[(LS.window == win) & (LS.action == act)].iloc[0]
            rows.append([win, act, int(r.n), fn(r.mean_e_bp, 1), fn(r.sum_e_pp, 2), fn(r.ann_e_pp, 3), fpv(r.t_p),
                         fpv(r.sign_p), fn(r.dedrift_mean_bp, 1), fn(r.sum_o_pp, 2)])
    A(md(rows, ['窗', '动作', 'n', 'e 均值bp', 'e 合计pp', '年化pp/年', 't-p', '符号 p', '去漂移bp', 'o 合计pp']))
    A('')
    A('### 3.5 分年表（e / o 合计 pp，事件数）')
    A('')
    for title, BY, acts in (('F', k['by_year_F'], LEGS + FLIPS), ('S-proxy', k['by_year_S'], ('开多', '平多'))):
        piv_rows = []
        for y in sorted(BY.year.unique()):
            cells = [str(y)]
            for act in acts:
                r = BY[(BY.year == y) & (BY.action == act)].iloc[0]
                cells.append(f"{int(r.n)} / {fn(r.sum_e_pp,2)} / {fn(r.sum_o_pp,2)}")
            piv_rows.append(cells)
        A(f'**{title}**（单元格 = n / e 合计 / o 合计）')
        A('')
        A(md(piv_rows, ['年'] + list(acts)))
        A('')
    A('## 4. 绩效表')
    A('')
    A('算术年化 / Sharpe / 最大回撤用 house `backtest.metrics`；CAGR = expm1(Σlog1p(r)×245/n)；ΔSharpe 为配对 moving-block '
      'bootstrap（`paired_block_bootstrap_sharpe_diff`，block=20、n=2000、seed=20261009），差 = 策略 − E1。')
    A('')
    A('### 4.1 F 全窗')
    A('')
    A(pol_md(PF, F_ORDER, 'full'))
    A('')
    A('### 4.2 F 分窗（E0 / E1 / E2；E2 行的 CI 与 p 为 E2 − E1）')
    A('')
    rows = []
    for win in F_WINDOWS:
        x = PF[PF.window == win].set_index('policy')
        rows.append([win, f"{x.loc['E1','start']}~{x.loc['E1','end']}", int(x.loc['E1', 'n_days']),
                     f"{fp(x.loc['E0','ann'])} / {fn(x.loc['E0','sharpe'],3)}",
                     f"{fp(x.loc['E1','ann'])} / {fn(x.loc['E1','sharpe'],3)} / {fp(x.loc['E1','maxdd'])}",
                     f"{fp(x.loc['E2','ann'])} / {fn(x.loc['E2','sharpe'],3)} / {fp(x.loc['E2','maxdd'])}",
                     fn(x.loc['E2', 'd_sharpe_vs_E1'], 3), f"[{fn(x.loc['E2','ci_lo'],3)}, {fn(x.loc['E2','ci_hi'],3)}]",
                     fpv(x.loc['E2', 'p_value'])])
    A(md(rows, ['窗', '区间', '日数', 'E0 年化/Sharpe', 'E1 年化/Sharpe/回撤', 'E2 年化/Sharpe/回撤', 'ΔSharpe', '95% CI', 'p']))
    A('')
    A('其余部分策略（M_*）的分窗读数见 `policies_F.csv`。')
    A('')
    A('### 4.3 S-proxy 全窗与分窗')
    A('')
    A(pol_md(PS, S_ORDER, 'full'))
    A('')
    rows = []
    for win in S_WINDOWS:
        x = PS[PS.window == win].set_index('policy')
        rows.append([win, f"{x.loc['E1','start']}~{x.loc['E1','end']}", int(x.loc['E1', 'n_days']),
                     f"{fp(x.loc['E0','ann'])} / {fn(x.loc['E0','sharpe'],3)}",
                     f"{fp(x.loc['E1','ann'])} / {fn(x.loc['E1','sharpe'],3)} / {fp(x.loc['E1','maxdd'])}",
                     f"{fp(x.loc['E2','ann'])} / {fn(x.loc['E2','sharpe'],3)} / {fp(x.loc['E2','maxdd'])}",
                     fn(x.loc['E2', 'd_sharpe_vs_E1'], 3), f"[{fn(x.loc['E2','ci_lo'],3)}, {fn(x.loc['E2','ci_hi'],3)}]",
                     fpv(x.loc['E2', 'p_value'])])
    A(md(rows, ['窗', '区间', '日数', 'E0 年化/Sharpe', 'E1 年化/Sharpe/回撤', 'E2 年化/Sharpe/回撤', 'ΔSharpe', '95% CI', 'p']))
    A('')
    A('### 4.4 S-exact（只描述）与代理验证')
    A('')
    rows = []
    for nm in ('905', '852', 'blend'):
        r = pv['legs'][nm] if nm != 'blend' else pv['blend_intra']
        rows.append([{'905': '000905 vs IC', '852': '000852 vs IM', 'blend': 'blend'}[nm], r['n'], fn(r['corr'], 3),
                     fn(r['mean_real_bp'], 1), fn(r['mean_proxy_bp'], 1), fn(r['mean_diff_bp'], 1),
                     fn(r['rmse_bp'], 1), fp(r['sign_agree'], 0)])
    A(md(rows, ['intra（真实 vs 代理）', '日数', '相关', '真实均值bp', '代理均值bp', '均值差bp(真−代)', 'RMSE bp', '符号一致']))
    A('')
    ex = pv['events']
    if ex:
        rows = [[e['date'], e['leg'], fn(e['e_exact_bp'], 1), fn(e['e_proxy_bp'], 1), fn(e['o_exact_bp'], 1),
                 fn(e['o_proxy_bp'], 1)] for e in ex]
        A(f"这些日子里的 S 事件（{len(ex)} 条）：")
        A('')
        A(md(rows, ['日期', '动作', 'e 精确bp', 'e 代理bp', 'o 精确bp', 'o 代理bp']))
        A('')
    se = pv['s_exact_policies']
    A(f"S-exact 窗（{pv['window'][0]}~{pv['window'][1]}，{pv['n_days']} 日）各策略合计收益：" +
      '；'.join(f"{nm} 精确 {fn(se[nm]['exact_sum_pp'],2)}pp / 代理 {fn(se[nm]['proxy_sum_pp'],2)}pp" for nm in S_ORDER) + '。'
      '样本太短，只作描述。')
    A('')
    n_o = int(ls_full[ls_full.action == '开多'].n.iloc[0])
    n_c = int(ls_full[ls_full.action == '平多'].n.iloc[0])
    A(f"读法提示（算术事实，非新口径）：若代理 intra 有一个恒定偏差 b（验证窗 b ≈ {fn(pv['blend_intra']['mean_diff_bp'],1)}bp/日，真 − 代），"
      f"它对开多事件的 e 是 +b、对平多事件是 −b，对 Σe 的净影响 = (n_开多 − n_平多)·b = ({n_o} − {n_c})·b；"
      '所以常数偏差基本不影响 S 的 E2−E1，但会把开多 / 平多两腿各自的读数反向推开 —— 两腿分开看时要打这个折扣。')
    A('')
    A('## 5. 锚点与自检')
    A('')
    c1 = C['1_ledger']
    A('**1. ledger 锚点**（`contract_ledger`，fill=close / open，3bps，`futures_weights`，到期表同 `execution_audit.inputs`；'
      'ledger 自 2015-04-16 起跑、首日平仓初始化，只在 2015-04-17~2026-10-08 比较）：')
    A('')
    rows = []
    for key in ('E1_vs_ledger_close', 'E2_vs_ledger_open'):
        r = c1[key]
        d = r['decomp_ann_pp']
        rows.append([f"{r['model']} vs ledger {r['ledger_fill']}",
                     f"{fp(r['model_full']['ann'])} / {fn(r['model_full']['sharpe'],3)} / {fp(r['model_full']['maxdd'])}",
                     f"{fp(r['ledger_full']['ann'])} / {fn(r['ledger_full']['sharpe'],3)} / {fp(r['ledger_full']['maxdd'])}",
                     fn(r['daily_corr'], 4), fn(d['total_diff'], 3), fn(d['of_which_roll_timing_days'], 3),
                     f"{fn(d['of_which_IM_entry_day_2022-07-25'],3)} / {fn(d['of_which_first_day'],3)}",
                     fn(d['cost_diff_on_other_days(再平衡/换月/成交成本口径)'], 3), fn(d['of_which_rebalance_point_term'], 3),
                     f"{fn(d['residual_after_rebalance_term'],4)}（单日≤{fn(r['max_abs_residual_after_rebalance_term_bp'],2)}bp）"])
    A(md(rows, ['对照', '本模型 年化/Sharpe/回撤', 'ledger 年化/Sharpe/回撤', '日收益相关', 'Δ年化 合计', '换月时点日',
                'IM 入场日 / 首日', '其余日成本口径', '再平衡时点二阶项', '剩余']))
    A('')
    r1, r2 = c1['E1_vs_ledger_close'], c1['E2_vs_ledger_open']
    A(f"（Δ年化各列单位 pp/年，= 本模型 − ledger。换月时点日 {r1['n_roll_timing_days']} 个：本模型 held(t)=main(t−1) 在前一收盘换月，"
      f"close ledger 在 t 收盘、open ledger 在 t 开盘换月，且 ledger 剔除当日到期合约。成本：ledger {fn(r1['ledger_cost_ann_pp'],3)} "
      f"vs 本模型 {fn(r1['model_cost_ann_pp'],3)} pp/年，差在日度再平衡交易与成本基数。再平衡时点二阶项：本模型逐组乘积式相当于每组敞口在前收与开盘都重置，"
      f"close ledger 只在收盘重置（空头日差 2·gap·intra），open ledger 只在开盘重置（空头日差约 2·intra_(t−1)·gap_t）；其余日单日最大差 "
      f"E1 {fn(r1['max_abs_daily_diff_bp_other_days'],1)}bp（{r1['max_abs_daily_diff_other_days_date']}，其中该项 "
      f"{fn(r1['rebalance_term_bp_on_that_date'],1)}bp）、E2 {fn(r2['max_abs_daily_diff_bp_other_days'],1)}bp"
      f"（{r2['max_abs_daily_diff_other_days_date']}，其中该项 {fn(r2['rebalance_term_bp_on_that_date'],1)}bp）。"
      f"ledger 换月 {r1['ledger_rolls']} 次 = 本模型换月日 {r1['model_roll_days_IC_plus_IM']}。）")
    A('')
    A(f"判定：{'通过' if c1['pass'] else '不通过'} —— {c1['verdict']} 判据：{c1['criterion']}。")
    A('')
    oc = c1['open_minus_close']
    A(f"**换月时点敏感性（重要）**：ledger 口径下「开盘 − 收盘」= {fn(oc['ledger_open_minus_close_ann_pp'],2)}pp/年（ΔSharpe "
      f"{fn(oc['ledger_open_minus_close_sharpe'],3)}），规格模型 E2−E1 = {fn(oc['model_E2_minus_E1_ann_pp'],2)}pp/年（ΔSharpe "
      f"{fn(oc['model_E2_minus_E1_sharpe'],3)}）；两者之差 {fn(oc['ledger_open_minus_close_ann_pp']-oc['model_E2_minus_E1_ann_pp'],2)}pp/年中"
      f" {fn(-oc['gap_explained_by_roll_timing_days_pp'],2)} 来自换月时点日、{fn(-oc['gap_explained_by_IM_entry_and_first_day_pp'],2)} 来自 IM 入场日与首日。"
      '规格模型让 E1、E2 同日持有同一张合约（换月都在前一收盘），E2−E1 只含信号交易的成交时点；ledger 的换月与信号交易同时成交'
      '（开盘口径在开盘换月、收盘口径在收盘换月）。所以「改开盘执行」的总差对换月怎么安排很敏感：换月也随之挪到开盘时按 ledger 口径是 '
      f"{fn(oc['ledger_open_minus_close_ann_pp'],2)}pp/年，换月不动时按规格模型是 {fn(oc['model_E2_minus_E1_ann_pp'],2)}pp/年。")
    A('')
    ref = c1['run_20260914_r2_reference(信号文件版本不同，不要求一致)']
    A('09-14 权威 run（`20260914-execution-audit-r2`，信号文件版本不同、截至 2026-09-11，不要求一致）full 窗：' +
      '；'.join(f"{nm} 年化 {fp(v['ann'])} / Sharpe {fn(v['sharpe'],3)} / 回撤 {fp(v['maxdd'])}（{v['start']}~{v['end']}，换月 {int(v['rolls'])}）"
               for nm, v in ref.items()) + '。')
    A('')
    c2 = C['2_engine_S']
    A(f"**2. S 的 E0 vs `engine.run_strategy(position, blend_cc, 3.0)`**：{'通过' if c2['pass'] else '不通过'}。"
      '全序列跑引擎后切窗：各窗逐日最大差 ' + '、'.join(f"{w} {c2[w]['max_abs_diff_vs_full_series_run']:.1e}" for w in S_WINDOWS) +
      '；窗内切片后再跑引擎：差异只出现在窗首两日（首日建仓成本怪癖：窗首 pos_eff 被置 0、建仓成本落第二天）—— ' +
      '、'.join(f"{w} {c2[w]['windowed_run_diff_days']}" for w in S_WINDOWS) + '。')
    A('')
    c3 = C['3_additivity']
    rows = []
    for pool, cc3, wins in (('F', c3['F'], F_WINDOWS), ('S', c3['S'], S_WINDOWS)):
        for win in wins:
            r = cc3[win]['E2']
            rows.append([pool, win, fn(r['sum_diff_pp'], 4), fn(r['sum_e_pp'], 4), fn(r['residual_pp'], 4),
                         f"{r['residual_minus_analytic']:.1e}"])
    A(f"**3. 可加性**（Σ(E2−E1) vs Σe；残差 = 二阶项 Σ w·p_old·Δ·gap·intra）：{'通过' if c3['pass'] else '不通过'}。"
      '各 M_A 同样核对（见 checks.json），残差与解析二阶项之差最大 ' + f"{max(c3['F']['max_abs_residual_minus_analytic'], c3['S']['max_abs_residual_minus_analytic']):.1e}。")
    A('')
    A(md(rows, ['池', '窗', 'Σ(E2−E1) pp', 'Σe pp', '二阶残差 pp', '残差−解析式']))
    A('')
    c4 = C['4_timing']
    A(f"**4. 时点自检**（F 全窗算术年化）：{'通过' if c4['pass'] else '不通过'}。" +
      '；'.join(f"{tag}: E0 {fp(v['E0'])} / E1 {fp(v['E1'])} / E2 {fp(v['E2'])}" for tag, v in c4['ann_full'].items()) +
      '。主口径与前移 / 后移版本都不同（差值见 checks.json）。旁证：前移版 E1 − 主口径 E0 = '
      f"{fn(c4['consistency(应近似相等，只差成本落日/二阶项)']['advance_E1_minus_main_E0_pp'],3)}pp、后移版 E0 − 主口径 E1 = "
      f"{fn(c4['consistency(应近似相等，只差成本落日/二阶项)']['delay_E0_minus_main_E1_pp'],3)}pp —— 主口径 E1 恰好比 E0 晚一天，不多不少。")
    A('')
    c5 = C['5_limit']
    big = c5['F_sample_held_open_ge_9_5pct_all_days(含非执行日，描述)']
    big_txt = '、'.join(f"{x['date']} {x['symbol']} {x['open_vs_pre_settle']*100:+.2f}%" for x in big)
    big_tail = '，都不是执行日。' if not any(x['is_exec_day'] for x in big) else '，其中有执行日（见清单）。'
    A(f"**5. F 涨跌停标记**：执行日持有合约 |O/pre_settle−1| ≥ 9.5% 或 O=H=L 的共 {c5['n_flagged_rows']} 个（执行日×合约），"
      f"涉及 {c5['n_flagged_days']} 个执行日、{c5['n_flagged_events']} 条腿事件；清单见 `limit_F.csv`，剔除版主表见 §3.2"
      f"（{'因无标记，与 §3.1 相同' if c5['n_flagged_events'] == 0 else '已剔除'}）。执行日开盘离前结算最远的是 {c5['max_on_date']}"
      f"（{fn(c5['max_abs_open_vs_pre_settle_on_exec_days']*100,2)}%）；样本内持有合约开盘偏离 ≥9.5% 的全部日子：{big_txt}{big_tail}"
      f"pre_settle 在 2015-04-16~2015-09-18 缺失，用同合约前一日 settle 代替（在两者都有的 {c5['pre_settle_check_rows']} 行上两者完全相等）。")
    if c5['list']:
        A('')
        A(md([[x['date'], x['group'], x['symbol'], fn(x['open_vs_pre_settle'] * 100, 2) + '%', x['flag_O_eq_H_eq_L'],
               f"{int(x['p_old'])}→{int(x['p_new'])}"] for x in c5['list']],
             ['日期', '组', '合约', 'O/pre_settle−1', 'O=H=L', '仓位']))
    A('')
    dc = C['0_data']
    A('**数据与口径核对**：' + '；'.join(f"{kk} = {vv}" for kk, vv in dc['summary'].items()) + '。')
    A('')
    A('## 7. 解读边界')
    A('')
    A('- 开盘集合竞价与收盘的滑点、冲击成本不同，本检验不建模，成本两边同价（3bp/单位 |Δ|，换月 2×3bp）。')
    A(f"- F 的事件约每年十来次（全窗 {k['F_exec_days']} 个执行日 / {k['F_n']/ANN:.1f} 年），单类动作的检验力很低；"
      '「不显著」只说明没看出差别，不说明没有差别。')
    A('- 在样本里挑「哪类动作用开盘、哪类用收盘」是 2^4 种组合里选优，带选择偏差，不能直接当部署依据（M_* 各行只作归因描述）。')
    A('- S 的主读数是代理：intra 用期货持有合约代理（2016 起期货与现货交易时段对齐，但期货含基差变动），gap 由真实 cc 反推；'
      '精确版要真实指数开盘价（Wind 导出），目前只有 2026-07-01 起 66 日。')
    A('- 收益模型是日度再平衡的名义敞口（无整数手、保证金、涨跌停锁死、现金利息），与 ledger 的差异见 §5-1。')
    A('')
    A('## 附录 A：手算核对（一个多翻空日）')
    A('')
    h = k['hand']
    A(f"日期 t = {h['date']}（前一交易日 {h['prev_day']}），p_old = d_(t−2) = {int(h['p_old'])}，p_new = d_(t−1) = {int(h['p_new'])}，"
      'w_IC = w_IM = 0.5。持有合约 = 前一日 OI 最大（直接读原始行排序得到）：')
    A('')
    A(md([[g['group'], g['held(t)=main(t−1)'], g['oi(t−1)'], fn(g['C_{t−1}'], 1), fn(g['O_t'], 1), fn(g['C_t'], 1),
           f"{g['gap']*1e4:.2f}", f"{g['intra']*1e4:.2f}", f"{g['cc']*1e4:.2f}", g['rolled_today']] for g in h['groups']],
         ['组', '持有合约', 'OI(t−1)', 'C(t−1)', 'O(t)', 'C(t)', 'gap bp', 'intra bp', 'cc bp', '当日换月']))
    A('')
    A(f"- Σw·intra = {h['intra_blend']*1e4:.4f}bp，Σw·gap = {h['gap_blend']*1e4:.4f}bp。")
    A(f"- e(平多) = e(开空) = (−1) × Σw·intra = {h['e_平多']*1e4:.4f}bp；多翻空整笔 e = {h['e_多翻空']*1e4:.4f}bp；"
      f"o(多翻空) = (−2) × Σw·gap = {h['o_多翻空']*1e4:.4f}bp。流水线事件表该日两条腿 e = "
      + ', '.join(f'{x*1e4:.4f}' for x in h['pipeline']['e_legs']) + ' bp。')
    A(f"- E1 毛收益 = Σw[(1+p_old·gap)(1+p_old·intra)−1] = {h['E1_gross']*1e4:.4f}bp；"
      f"E2 毛收益 = Σw[(1+p_old·gap)(1+p_new·intra)−1] = {h['E2_gross']*1e4:.4f}bp；"
      f"成交成本 3bp×|p_new−p_old| = {h['trade_cost']*1e4:.1f}bp；换月成本 {h['roll_cost']*1e4:.1f}bp。")
    A(f"- E1 净 = {h['E1_net']*1e4:.4f}bp（流水线 {h['pipeline']['E1']*1e4:.4f}bp），E2 净 = {h['E2_net']*1e4:.4f}bp"
      f"（流水线 {h['pipeline']['E2']*1e4:.4f}bp）；E2−E1 = {(h['E2_net']-h['E1_net'])*1e4:.4f}bp ≈ Σe = {h['e_多翻空']*1e4:.4f}bp"
      f"（差 = 二阶项 Σw·p_old·Δ·gap·intra）。一致性：{'一致' if h['match'] else '不一致'}。")
    A('')
    A(k['hand_manual'])
    A('')
    A('## 附录 B：裁量点（规格有歧义处，按最贴近字面、最保守处理）')
    A('')
    for i, s in enumerate(k['discretion'], 1):
        A(f'{i}. {s}')
    A('')
    A('## 附录 C：规格外诊断（不入主表）')
    A('')
    for s in k['diagnostics']:
        A(f'- {s}')
    A('')
    (W / 'report.md').write_text('\n'.join(L) + '\n', encoding='utf-8')


# =========================================================================== 主流程
def main():
    inp = load_inputs()
    checks = {}

    # ---------------- 数据核对（规格 §1）
    pos_f, fac_f = inp['pos_f'], inp['fac_f']
    require(pos_f.index.equals(fac_f.index), 'F 仓位与因子日历不一致')
    mism = int((pos_f != np.sign(fac_f)).sum())
    require(mism == 0, f'equal_weight_symmetric ≠ sign(factor_value)：{mism} 日不一致（规格：报错停下）')

    DF, mF = build_F(inp)
    fcal = mF['fcal']
    sig_cal = pos_f.index
    in_rng = fcal[(fcal >= fcal[0]) & (fcal <= END)]
    require(in_rng.isin(sig_cal).all(), '决策序列缺期货交易日（规格：缺日报错）')
    require(sig_cal[(sig_cal >= fcal[0]) & (sig_cal <= END)].equals(in_rng), '信号日历在期货区间内多出非期货日')
    require(mF['first_day'] == F_FIRST_EXPECTED, f"F 首个 held 收益日 {mF['first_day'].date()} ≠ 2015-04-17")
    require(DF.index[-1] == END, 'F 样本末日 ≠ 2026-10-08')
    fw = futures_weights(DF.index, mF['first_im'])
    require(np.allclose(fw.IC, DF.w_IC) and np.allclose(fw.IM, DF.w_IM), '权重与 futures_weights 不一致')
    repo_eq = {}
    for g in F_GROUPS:
        ff = inp['fut'][inp['fut'].symbol.str.startswith(g)].rename(columns={'date': 'trade_date'})
        r = held_contract_frame(ff[['trade_date', 'symbol', 'open', 'close', 'oi']])
        h = mF['held'][g]
        repo_eq[g] = bool(r.index.equals(h.index) and (r.symbol_held == h.symbol).all()
                          and np.allclose(r.gap, h.gap, atol=0, rtol=0) and np.allclose(r.ret_oc, h.intra, atol=0, rtol=0)
                          and np.allclose(r.ret_cc, h.cc, atol=0, rtol=0))
    in_sample_zero = [str(d.date()) for d in sig_cal[(sig_cal >= DF.d_tm2_date.iloc[0]) & (sig_cal <= END)]
                      if pos_f.loc[d] == 0]
    fac_at_zero = {d: float(fac_f.loc[d]) for d in in_sample_zero}
    pos_s, fac_s = inp['pos_s'], inp['fac_s']
    s_lf_ok = int((pos_s != (fac_s > 0).astype(float)).sum())

    # ---------------- F：策略、事件、涨跌停
    RF = all_policies(DF, F_GROUPS, F_POLICIES, F_ORDER)
    lim = limit_table(DF)
    flagged_days = set(lim.loc[lim.flagged, 'date']) if len(lim) else set()
    evF = build_events(DF, extra=('w_IC', 'w_IM', 'sym_IC', 'sym_IM', 'intra_IC', 'intra_IM', 'gap_IC', 'gap_IM'))
    evF['limit_flag'] = evF.date.isin(sorted(flagged_days))
    legsF = legs_table(evF, DF, F_WINDOWS, LEGS, FLIPS,
                       {'all': lambda e: pd.Series(True, index=e.index), 'ex_limit': lambda e: ~e.limit_flag},
                       holm_windows=('full',))
    byF = by_year(evF, LEGS, FLIPS)
    polF = policies_table(RF, F_WINDOWS)

    # ---------------- S：代理、策略、事件、精确验证
    DS, mS = build_S(inp, mF['held'])
    RS = all_policies(DS, S_GROUPS, S_POLICIES, S_ORDER)
    evS = build_events(DS, extra=('intra_905', 'intra_852', 'gap_905', 'gap_852', 'proxy_src_905', 'proxy_src_852'))
    legsS = legs_table(evS, DS, S_WINDOWS, ('开多', '平多'), (),
                       {'all': lambda e: pd.Series(True, index=e.index)})
    byS = by_year(evS, ('开多', '平多'), ())
    polS = policies_table(RS, S_WINDOWS)
    require(set(evS.leg) <= {'开多', '平多'}, 'S 出现空头腿')

    EX = s_exact_frame(DS)
    require(len(EX) > 0 and EX.index[0] == S_EXACT_START, 'S-exact 首日不是 2026-07-01')
    pv = {'n_days': int(len(EX)), 'window': [str(EX.index[0].date()), str(EX.index[-1].date())],
          'contiguous_trading_days': bool(EX.index.equals(DS.loc[EX.index[0]:EX.index[-1]].index)),
          'legs': {g: cmp_stats(EX['intra_' + g], EX['intra_proxy_' + g]) for g in S_GROUPS},
          'blend_intra': cmp_stats(EX.intra_b, EX.intra_proxy_b),
          'gap_legs(规格外附带)': {g: cmp_stats(EX['gap_' + g], EX['gap_proxy_' + g]) for g in S_GROUPS},
          'blend_gap(规格外附带)': cmp_stats(EX.gap_b, EX.gap_proxy_b),
          'proxy_source': {g: sorted(set(EX['proxy_src_' + g].str[:2])) for g in S_GROUPS}}
    evts = []
    for t, r in EX[EX.p_new != EX.p_old].iterrows():
        for leg in LEGS:
            d = float(r['Δ' + leg])
            if d != 0:
                evts.append({'date': str(t.date()), 'leg': leg, 'delta': d,
                             'e_exact_bp': d * r.intra_b * 1e4, 'e_proxy_bp': d * r.intra_proxy_b * 1e4,
                             'o_exact_bp': d * r.gap_b * 1e4, 'o_proxy_bp': d * r.gap_proxy_b * 1e4})
    pv['events'] = evts
    REx = all_policies(EX, S_GROUPS, S_POLICIES, S_ORDER)
    RPx = RS.loc[EX.index]
    pv['s_exact_policies'] = {nm: {'exact_sum_pp': float(REx[nm].sum() * 100), 'proxy_sum_pp': float(RPx[nm].sum() * 100),
                                   'exact_ann': ann_return(REx[nm]), 'proxy_ann': ann_return(RPx[nm]),
                                   'exact_sharpe': sharpe(REx[nm]), 'proxy_sharpe': sharpe(RPx[nm])}
                              for nm in S_ORDER}
    pv['note'] = 'S-exact 只描述：E0/E1 只用 cc，精确与代理完全相同；差别只在执行日的 intra/gap 拆分。'

    # ---------------- 自检
    c1, ledgers = check_ledger(inp, mF, RF, DF)
    # 判定：差异全部可归到（换月时点/IM 入场/首日）+（成本口径）+（再平衡时点二阶项），剩余为费用引起的敞口漂移等更高阶项
    r1, r2 = c1['E1_vs_ledger_close'], c1['E2_vs_ledger_open']
    ok1 = all(r['daily_corr'] > 0.99 and abs(r['decomp_ann_pp']['residual_after_rebalance_term']) < 0.05
              and r['max_abs_residual_after_rebalance_term_bp'] < 2.0 for r in (r1, r2))
    c1['pass'] = bool(ok1)
    c1['criterion'] = ('（实现正确性判据，我设定，非规格给定）日收益相关 > 0.99；剔除合约/权重不一致日（换月时点、IM 入场、首日）、'
                       '扣除成本口径差、再扣再平衡时点二阶项之后，剩余 |年化| < 0.05pp 且单日 < 2bp')

    def _v(r):
        d = r['decomp_ann_pp']
        return (f"总差 {d['total_diff']:.3f} = 换月时点 {d['of_which_roll_timing_days']:.3f} + IM 入场日 "
                f"{d['of_which_IM_entry_day_2022-07-25']:.3f} + 首日 {d['of_which_first_day']:.3f} + 成本口径 "
                f"{d['cost_diff_on_other_days(再平衡/换月/成交成本口径)']:.3f} + 再平衡时点二阶项 "
                f"{d['of_which_rebalance_point_term']:.3f} + 剩余 {d['residual_after_rebalance_term']:.4f}")
    c1['verdict'] = f"E1 vs close ledger：{_v(r1)}；E2 vs open ledger：{_v(r2)}（pp/年）。"
    checks['1_ledger'] = c1
    checks['2_engine_S'] = check_engine_S(inp, RS)
    c3F = check_additivity(DF, RF, evF, F_WINDOWS, F_GROUPS, F_POLICIES)
    c3S = check_additivity(DS, RS, evS, S_WINDOWS, S_GROUPS, S_POLICIES)
    checks['3_additivity'] = {'F': c3F, 'S': c3S, 'pass': bool(c3F['pass'] and c3S['pass'])}
    checks['4_timing'] = check_timing(inp)
    a, b = F_WINDOWS['full']
    ev_full = evF[(evF.date >= T(a)) & (evF.date <= T(b))]
    fl = lim[lim.flagged] if len(lim) else lim
    ps_rows = inp['fut'].sort_values(['symbol', 'date']).copy()
    ps_rows['prev_settle'] = ps_rows.groupby('symbol').settle.shift(1)
    both = ps_rows.pre_settle.notna() & ps_rows.prev_settle.notna()
    checks['5_limit'] = {'threshold': LIMIT_TH, 'n_exec_day_contract_rows': int(len(lim)),
                         'n_flagged_rows': int(len(fl)), 'n_flagged_days': int(len(flagged_days)),
                         'n_flagged_events': int(ev_full.limit_flag.sum()),
                         'n_flag_limit_9_5pct': int(lim.flag_limit_9_5pct.sum()) if len(lim) else 0,
                         'n_flag_O_eq_H_eq_L': int(lim.flag_O_eq_H_eq_L.sum()) if len(lim) else 0,
                         'pre_settle_fallback_rows_on_exec_days': int((lim.pre_settle_src == 'prev_settle').sum()) if len(lim) else 0,
                         'pre_settle_check_rows': int(both.sum()),
                         'max_abs_open_vs_pre_settle_on_exec_days': float(lim.open_vs_pre_settle.abs().max()),
                         'max_on_date': str(lim.loc[lim.open_vs_pre_settle.abs().idxmax(), 'date'].date()),
                         'F_sample_held_open_ge_9_5pct_all_days(含非执行日，描述)': [
                             {'date': str(d.date()), 'group': g, 'symbol': DF.at[d, 'sym_' + g],
                              'open_vs_pre_settle': float(DF.at[d, 'open_' + g] / DF.at[d, 'pre_settle_' + g] - 1),
                              'is_exec_day': bool(DF.at[d, 'p_new'] != DF.at[d, 'p_old'])}
                             for g in F_GROUPS
                             for d in DF.index[(DF['open_' + g] / DF['pre_settle_' + g] - 1).abs() >= LIMIT_TH]],
                         'pre_settle_equals_prev_settle_max_abs_diff': float((ps_rows.pre_settle - ps_rows.prev_settle)[both].abs().max()),
                         'list': [{kk: (str(vv.date()) if isinstance(vv, pd.Timestamp) else vv) for kk, vv in r.items()}
                                  for r in fl.to_dict('records')],
                         'pass': True,
                         'note': '标记本身是描述性清单，不设通过门槛；剔除版主表见 legs_F.csv variant=ex_limit'}
    split_events = evF[(evF.whole_event == '') & evF.leg.isin(LEGS)]
    split_txt = '、'.join(f"{str(r.date.date())} {r.leg} e={r.e_bp:.1f}bp" for r in split_events.itertuples())
    raw_fac = pd.read_csv(INP / 'equal_weight_signal_20d40z.csv', dtype=str).set_index('date').factor_value
    zero_note = (f"决策序列在 {', '.join(in_sample_zero)} 为 0（factor_value 在 CSV 中写作 "
                 f"{', '.join(raw_fac.loc[d] for d in in_sample_zero)}，sign 为 0，与仓位文件一致）。")
    checks['0_data'] = {
        'sha256_verified': True,
        'F_position_equals_sign_factor': True, 'F_position_mismatch_days': mism,
        'F_in_sample_zero_position_days': in_sample_zero, 'F_factor_value_on_zero_days': fac_at_zero,
        'F_spec_claim_always_pm1': len(in_sample_zero) == 0,
        'F_split_leg_events(非同日翻转)': [{'date': str(r.date.date()), 'leg': r.leg, 'e_bp': r.e_bp, 'o_bp': r.o_bp}
                                       for r in split_events.itertuples()],
        'S_longflat_equals_factor_gt0_mismatch_days(规格外附带)': s_lf_ok,
        'futures_calendar_equals_signal_calendar_in_range': True,
        'F_first_day': str(DF.index[0].date()), 'F_last_day': str(DF.index[-1].date()),
        'F_first_day_p_old_from(信号日历，早于期货日历首日)': str(DF.d_tm2_date.iloc[0].date()),
        'IM_first_quote_day': str(mF['first_im'].date()), 'IM_first_quote_day_as_expected': mF['first_im'] == IM_FIRST_EXPECTED,
        'oi_tie_days': mF['ties'], 'held_fallback_days': mF['fallbacks'],
        'held_frame_equals_repo_held_contract_frame': repo_eq,
        'weights_equal_futures_weights': True,
        'S_first_day': str(DS.index[0].date()), 'S_last_day': str(DS.index[-1].date()),
        'S_852_proxy_uses_IM_from': str(mS['im_proxy_first'].date()),
    }
    checks['0_data']['summary'] = {
        'sha256': '6 个 CSV 全部一致',
        '仓位=sign(因子)': f'是（0 日不一致）',
        '样本内 0 仓位日': ', '.join(in_sample_zero) or '无',
        'F 样本': f"{DF.index[0].date()}~{DF.index[-1].date()}（{len(DF)} 日）",
        'IM 首个报价日': str(mF['first_im'].date()),
        'OI 平局日': f"IC {mF['ties']['IC']} / IM {mF['ties']['IM']}",
        'held 退化为 main(t) 的日数': f"IC {len(mF['fallbacks']['IC'])} / IM {len(mF['fallbacks']['IM'])}",
        '与 held_contract_frame 逐位一致': f"IC {repo_eq['IC']} / IM {repo_eq['IM']}",
        '换月日': f"IC {int(DF.roll_IC.sum())} / IM {int(DF.roll_IM.sum())}",
        'S-proxy 样本': f"{DS.index[0].date()}~{DS.index[-1].date()}（{len(DS)} 日），000852 代理自 {mS['im_proxy_first'].date()} 起用 IM",
    }
    checks['0_data']['pass'] = bool(all(repo_eq.values()) and mF['first_im'] == IM_FIRST_EXPECTED)

    # E1 口径歧义诊断（规格外）
    e1cc = (sum(DF['w_' + g] * DF.p_old * DF['cc_' + g] for g in F_GROUPS)
            - COST * (DF.p_new - DF.p_old).abs()
            - sum(DF['roll_' + g].astype(float) * 2 * COST * DF.p_old.abs() * DF['w_' + g] for g in F_GROUPS))
    e1cc_full = e1cc.loc[a:b]
    pe1cc = perf(e1cc_full)
    bt_cc = paired_block_bootstrap_sharpe_diff(RF['E2'].loc[a:b], e1cc_full, **BOOT)
    checks['diag_E1_as_p_old_cc(规格外)'] = {
        'note': '规格 §2 写「E1 = M_∅（r_t = p_old·cc）」，但乘积式在 p_old=−1 时 = −gap−intra+gap·intra ≠ −cc（差 2·gap·intra）。'
                '主口径取 E1 = M_∅（乘积式），此处给出 E1 = p_old·cc 的读数作对照。',
        'E1_cc_full': {kk: pe1cc[kk] for kk in ('ann', 'sharpe', 'maxdd', 'cagr', 'vol')},
        'E1_formula_minus_E1_cc_sum_pp': float((RF['E1'] - e1cc).loc[a:b].sum() * 100),
        'E1_formula_minus_E1_cc_ann_pp': float((RF['E1'] - e1cc).loc[a:b].mean() * ANN * 100),
        'E2_minus_E1cc_sharpe_bootstrap': {kk: bt_cc[kk] for kk in ('diff_sharpe', 'ci_lo', 'ci_hi', 'p_value')},
    }

    # ---------------- 手算例子
    hand = hand_example(inp, DF, evF)
    t = T(hand['date'])
    hand['pipeline']['E1'] = float(RF.at[t, 'E1'])
    hand['pipeline']['E2'] = float(RF.at[t, 'E2'])
    hand['match'] = bool(abs(hand['E1_net'] - RF.at[t, 'E1']) < 1e-12 and abs(hand['E2_net'] - RF.at[t, 'E2']) < 1e-12
                         and np.allclose(hand['pipeline']['e_legs'], [hand['e_平多'], hand['e_开空']], rtol=0, atol=1e-15))
    checks['hand_example'] = hand

    # ---------------- 写文件
    def out_csv(df, name, index=False):
        df.to_csv(W / name, index=index, float_format='%.17g')

    evF_out = evF.copy()
    evF_out['date'] = evF_out.date.dt.date
    out_csv(evF_out, 'events_F.csv')
    out_csv(legsF, 'legs_F.csv')
    out_csv(polF, 'policies_F.csv')
    out_csv(byF, 'by_year_F.csv')
    lim_out = fl.copy()
    if len(lim_out):
        lim_out['date'] = lim_out.date.dt.date
    out_csv(lim_out, 'limit_F.csv')
    evS_out = evS.copy()
    evS_out['date'] = evS_out.date.dt.date
    out_csv(evS_out, 'events_S.csv')
    out_csv(legsS, 'legs_S.csv')
    out_csv(polS, 'policies_S.csv')
    out_csv(byS, 'by_year_S.csv')
    dump_json(W / 'proxy_validation_S.json', pv)
    dump_json(W / 'checks.json', checks)
    dF = DF.drop(columns=[c for c in DF.columns if c.startswith(('d_tm',))]).join(RF.add_prefix('ret_'))
    out_csv(dF, 'daily_F.csv', index=True)
    dS = DS.drop(columns=[c for c in DS.columns if c.startswith(('d_tm',))]).join(RS.add_prefix('ret_'))
    out_csv(dS, 'daily_S.csv', index=True)

    # ---------------- key_numbers.json
    def legs_kn(tbl, acts):
        return {act: {'n': int(r.n), 'mean_e_bp': float(r.mean_e_bp), 'sum_e_pp': float(r.sum_e_pp),
                      'mean_o_bp': float(r.mean_o_bp)}
                for act in acts for r in [tbl[tbl.action == act].iloc[0]]}

    lf_full = legsF[(legsF.window == 'full') & (legsF.variant == 'all')]
    ls_full = legsS[(legsS.window == 'full') & (legsS.variant == 'all')]
    pf = polF[polF.window == 'full'].set_index('policy')
    psf = polS[polS.window == 'full'].set_index('policy')
    kn = {'F': {
        'legs_full': legs_kn(lf_full, LEGS),
        'events_full': {fl_: {'n': int(r.n), 'sum_e_pp': float(r.sum_e_pp)}
                        for fl_ in FLIPS for r in [lf_full[lf_full.action == fl_].iloc[0]]},
        'policies_full': {p: {'ann': float(pf.at[p, 'ann']), 'sharpe': float(pf.at[p, 'sharpe']),
                              'maxdd': float(pf.at[p, 'maxdd'])} for p in F_ORDER},
        'sharpe_by_window': {w_: {e: float(polF[(polF.window == w_) & (polF.policy == e)].sharpe.iloc[0])
                                  for e in ('E1', 'E2')}
                             for w_ in ('2015-2020', '2021-2023', '2024-2026', 'dual_listed', 'ex2015')},
        'n_days_full': int(pf.at['E1', 'n_days']), 'first_day': pf.at['E1', 'start'], 'last_day': pf.at['E1', 'end'],
        'roll_days': {g: int(DF.loc[a:b, 'roll_' + g].sum()) for g in F_GROUPS},
        'limit_flag_events': int(ev_full.limit_flag.sum())},
        'S': {
        'legs_full': legs_kn(ls_full, ('开多', '平多')),
        'policies_full': {p: {'ann': float(psf.at[p, 'ann']), 'sharpe': float(psf.at[p, 'sharpe']),
                              'maxdd': float(psf.at[p, 'maxdd'])} for p in S_ORDER},
        'n_days_full': int(psf.at['E1', 'n_days']), 'first_day': psf.at['E1', 'start'], 'last_day': psf.at['E1', 'end'],
        'proxy_validation': {'n_days': pv['n_days'], 'corr_blend_intra': pv['blend_intra']['corr'],
                             'mean_diff_bp': pv['blend_intra']['mean_diff_bp'], 'rmse_bp': pv['blend_intra']['rmse_bp']}}}
    dump_json(W / 'key_numbers.json', kn)

    # ---------------- 独立代码路径复算（crosscheck.py，不复用本文件函数）
    import subprocess
    cx = subprocess.run([sys.executable, str(W / 'crosscheck.py')], capture_output=True, text=True, timeout=600)
    cx_out = (cx.stdout.strip() or cx.stderr.strip()[-500:])
    checks['crosscheck_independent_path(规格外)'] = {'returncode': cx.returncode, 'output': cx_out,
                                                      'pass': cx.returncode == 0 and '超过 1e-9 的项： 无' in cx_out}
    dump_json(W / 'checks.json', checks)

    # ---------------- 报告
    discretion = [
        'E1 的定义：规格同时写了「E1 = M_∅」与括注「r_t = p_old·cc」，两者在 p_old = −1 时相差 2·gap·intra（乘积式相当于空头敞口在开盘重置）。'
        '主口径取 E1 = M_∅（乘积式），使全部 M_A 共用同一再平衡口径、M_A − E1 只在执行日非零，§3 的可加性才成立；'
        f"按括注 p_old·cc 计算的 E1 全窗年化差仅 {checks['diag_E1_as_p_old_cc(规格外)']['E1_formula_minus_E1_cc_ann_pp']:.4f}pp/年，读数见附录 C。S 仓位 ∈{{0,1}}，无此歧义。",
        f"F 首日 2015-04-17 的 p_old = d_(t−2) 取信号日历上的 2015-04-15（期货日历从 2015-04-16 起，没有更早一天）；"
        '该两日决策同为 −1，故首日既无执行也无建仓成本，与按「首日平仓初始化」处理只差一次 3bp 建仓成本（窗外）。',
        '平局规则：snapshot 内 OI 平局日为 0，故仓库 held_contract_frame（idxmax 取行序首个）与「字典序最小」等价；本实现仍显式按 (OI 降序, symbol 升序) 排序，并逐位核对与仓库函数一致。',
        '换月：只在 held_g(t) 与 held_g(t−1) 都有定义且不同的日子计；IC 首日（2015-04-17）与 IM 首个计权日（2022-07-25）的「首次建立该腿」不算换月；'
        'IM 入场当日 IC 由 1 降到 0.5 的再平衡不收费（规格只对 |Δ| 与换月收费）。E0 的「开盘前持仓」取 |d_(t−1)|，E1/E2/M_A 取 |p_old|。',
        '涨跌停：pre_settle 在 2015-04-16~2015-09-18 全部缺失，用同合约前一交易日 settle 代替（两者都有的行上逐位相等）；'
        '一个执行日只要任一权重>0 的持有合约被标记，该日全部腿事件都算被标记；limit_flag_events 按腿事件计数。',
        '整笔事件：「多翻空 / 空翻多」只统计同日 +1→−1 / −1→+1；2017-01-03 仓位为 0 造成的两日拆分翻转只进单腿统计、不进整笔事件。',
        '去漂移均值的整笔事件版本 = 两腿去漂移值之和（与「效应 = 两腿之和」一致）；Holm 只在 F 全窗四腿内做（t 检验与符号检验分别校正），'
        'ex_limit 变体同样在四腿内校正。',
        '年化贡献 = 合计 ÷（窗内交易日数 / 245），与 house ann_return 的年化口径一致（这样各腿年化贡献之和 ≈ E2−E1 的 Δ年化）。',
        'S-proxy：000852 的代理 intra 在 IM 有持有合约收益的日子（2022-07-25 起）用 IM，之前（含 2022-07-22）用 IC；'
        'S-exact 窗 = 两腿都有真实开盘价的 66 日（2026-07-01~2026-10-08）。S 的事件 / 分年表只统计 S-proxy 全窗（2016-01-04 起）。',
        '自检 1 的 ledger 从 2015-04-16 起跑（同 execution_audit 的 START，首日平仓初始化），只在 2015-04-17~2026-10-08 上比较；'
        '自检 1 的「通过」门槛（相关 > 0.99、扣除已识别来源后剩余 |年化| < 0.05pp）是我设定的实现正确性判据，不是规格给的。',
        '自检 4 的「前移」= 用 d_(T+1) 代替 d_T（相当于提前一天、偷看），「后移」= 用 d_(T−1) 代替 d_T；两者都只为证明主口径不差一天。',
    ]
    diag = [
        f"E1 按括注 p_old·cc 计算（规格外对照）：全窗年化 {fp(pe1cc['ann'])}、Sharpe {pe1cc['sharpe']:.3f}、回撤 {fp(pe1cc['maxdd'])}；"
        f"E2 − E1(cc) 的 ΔSharpe = {bt_cc['diff_sharpe']:.3f} [{bt_cc['ci_lo']:.3f}, {bt_cc['ci_hi']:.3f}]，p = {fpv(bt_cc['p_value'])}。"
        f"主口径 E1 与之差 {checks['diag_E1_as_p_old_cc(规格外)']['E1_formula_minus_E1_cc_sum_pp']:.3f}pp（全窗合计）。",
        f"S 仓位文件与 (slope20 factor_value > 0) 逐日一致（不一致 {s_lf_ok} 日）。",
        '数据备注：2022-11-08 IM2211（当日 IM 持有合约）开盘 6045.4 = 前结算 6717.0 × 0.9（跌停价）且 = 当日最低，其余 IM 合约正常开在 6690 附近，'
        '收盘 6707.6 —— 疑似开盘异常成交。按规格照用数据：该日 F、S 都不是执行日（仓位 +1→+1），乘积式在多头日 = cc，E1/E2/事件均不受影响；'
        f"只把 IM（及 S 代理的 000852）当日 intra 抬成 +10.95%，使去漂移用的窗内 intra 均值上移约 0.2bp（F 全窗 μ {DF.intra_b.mean()*1e4:.2f}bp，"
        f"剔除该日 {DF.intra_b.drop(T('2022-11-08')).mean()*1e4:.2f}bp；S 全窗 {DS.intra_b.mean()*1e4:.2f} / {DS.intra_b.drop(T('2022-11-08')).mean()*1e4:.2f}bp）。",
        'S-exact 窗 gap 的真实 vs 代理（代理 gap 由 cc 反推，误差与 intra 误差互补）：blend 相关 '
        f"{pv['blend_gap(规格外附带)']['corr']:.3f}、均值差 {pv['blend_gap(规格外附带)']['mean_diff_bp']:.1f}bp、RMSE {pv['blend_gap(规格外附带)']['rmse_bp']:.1f}bp。",
        '另产出 daily_F.csv / daily_S.csv（逐日输入与各策略净收益），便于逐数复核。',
        '独立代码路径复算（`crosscheck.py`：pivot + 数组位移重算 held 合约、四腿、E0/E1/E2、腿事件与代理验证，不复用 run.py 的函数）：'
        + checks['crosscheck_independent_path(规格外)']['output'].replace('\n', '；')
        + ('（通过）' if checks['crosscheck_independent_path(规格外)']['pass'] else '（不通过）'),
    ]
    hand_manual = ('上面的数由 `hand_example()` 直接读原始行、按最朴素的算术算出（不经过 held 合约表 / 策略函数），再与流水线逐位比对。')
    if hand['date'] == '2022-08-15':
        hand_manual += ('另用计算器把价格当字面量重算（2026-10-09，实现者手算）：IC gap = 6374.0/6386.6 − 1 = −19.7288bp、'
                        'intra = 6398.4/6374.0 − 1 = 38.2805bp；IM gap = 7210.0/7222.4 − 1 = −17.1688bp、intra = 7266.0/7210.0 − 1 = '
                        '77.6699bp；Σw·intra = 57.9752bp → 单腿 e = −57.9752bp、整笔 e = −115.9504bp；o(整笔) = −2×(−18.4488) = 36.8976bp；'
                        'E1 净 = 0.5×18.4762 + 0.5×60.3677 − 6 = 33.4220bp（多头日乘积式 = cc：6398.4/6386.6、7266.0/7222.4）；'
                        'E2 净 = 0.5×[(1−0.00197288)(1−0.00382805)−1] + 0.5×[(1−0.00171688)(1−0.00776699)−1] − 6bp = −82.3196bp。'
                        '与脚本、流水线三方一致。')
    exec_days_full = int((DF.loc[a:b].p_new != DF.loc[a:b].p_old).sum())
    summary_checks = [('1 ledger 锚点', checks['1_ledger']['pass']), ('2 S 引擎一致', checks['2_engine_S']['pass']),
                      ('3 可加性', checks['3_additivity']['pass']), ('4 时点', checks['4_timing']['pass']),
                      ('5 涨跌停清单', checks['5_limit']['pass']), ('数据核对', checks['0_data']['pass']),
                      ('手算例子', hand['match'])]
    ctx = dict(legs_F=legsF, legs_S=legsS, pol_F=polF, pol_S=polS, checks=checks, proxy=pv, hand=hand,
               by_year_F=byF, by_year_S=byS, split_events=split_txt, zero_note=zero_note,
               F_first=str(DF.index[0].date()), F_last=str(DF.index[-1].date()), F_n=len(DF),
               S_first=str(DS.index[0].date()), S_last=str(DS.index[-1].date()), S_n=len(DS),
               limit_n_events=int(ev_full.limit_flag.sum()), limit_n_days=len(flagged_days),
               F_exec_days=exec_days_full, check_summary=summary_checks, discretion=discretion,
               diagnostics=diag, hand_manual=hand_manual)
    write_report(ctx)

    print('F full:', {p: (round(pf.at[p, 'ann'], 4), round(pf.at[p, 'sharpe'], 3)) for p in ('E0', 'E1', 'E2')})
    print('S full:', {p: (round(psf.at[p, 'ann'], 4), round(psf.at[p, 'sharpe'], 3)) for p in ('E0', 'E1', 'E2')})
    print('checks:', summary_checks)
    print('→', W)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
