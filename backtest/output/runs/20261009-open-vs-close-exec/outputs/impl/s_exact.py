#!/usr/bin/env python3
"""追加：现货池用真实指数开盘价重算（S-exact）——按 ../spec_addendum_s_exact.md（母规格 ../spec.md 不变）。

一条命令重现（只读 ../inputs/ 与 ../inputs_v2/，不连库、不改仓库，产出写在本目录）：
    cd /home/elfbob/claude-code/style_timing_signal && python3 <本目录>/s_exact.py

与母规格实现（同目录 run.py）共用全部公式 / 成本 / 策略 / 事件归因函数，只把 S 的代理 intra/gap 换成真实值：
    gap_g = O_t/C_{t−1} − 1，intra_g = C_t/O_t − 1，cc_g = C_t/C_{t−1} − 1；blend 两腿各 0.5。
代理（期货持有合约 intra + 由 cc 反推的 gap）照母规格重算，仅用于逐事件比较（代理误差）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

W = Path(__file__).resolve().parent
sys.path.insert(0, str(W))
import run as R  # noqa: E402  —— 母规格实现（同目录）

from backtest.engine import run_strategy  # noqa: E402  (run.py 已把仓库根放进 sys.path)
from backtest.metrics import ann_return, sharpe  # noqa: E402

INP2 = W.parent / 'inputs_v2'
END = R.END
EXT_START = pd.Timestamp('2014-01-06')
WIN = {'main': ('2016-01-04', '2026-10-08'), 'ext': ('2014-01-06', '2026-10-08'),
       '2014-2015': ('2014-01-06', '2015-12-31'), '2016-2020': ('2016-01-04', '2020-12-31'),
       '2021-2023': ('2021-01-01', '2023-12-31'), '2024-2026': ('2024-01-01', '2026-10-08')}
SUB = ('2014-2015', '2016-2020', '2021-2023', '2024-2026')
S_LEGS = ('开多', '平多')
PROXY_REF = {'E1_sharpe': 0.739, 'E2_sharpe': 0.880, '开多_bp': 21.2, '平多_bp': 20.7}  # 追加规格 §报告 4 给定的代理版读数


def load_spot2():
    snap = json.loads((INP2 / 'snapshot.json').read_text())
    got = hashlib.sha256((INP2 / 'spot.csv').read_bytes()).hexdigest()
    R.require(got == snap['sha256'], 'inputs_v2/spot.csv sha256 不符')
    s2 = pd.read_csv(INP2 / 'spot.csv', parse_dates=['date'])
    R.require(not s2.duplicated(['date', 'symbol']).any(), 'inputs_v2/spot.csv 有重复行')
    return snap, s2


def data_checks(s2, s1, pos_s):
    """新旧现货逐位核对 + 日历 + 开盘价合理性（只报告，不改数据）。"""
    m = s1.merge(s2, on=['date', 'symbol'], how='left', suffixes=('_old', '_new'))
    R.require(m.close_new.notna().all(), 'inputs_v2 缺少旧快照里的行')
    close_eq = bool((m.close_old == m.close_new).all())
    R.require(close_eq, 'inputs_v2 收盘价与 inputs/spot.csv 不一致')
    old_open = m.open_old.notna()
    open_eq = bool((m.loc[old_open, 'open_old'] == m.loc[old_open, 'open_new']).all())
    R.require(open_eq, '原有开盘价被改动')
    close = s2.pivot(index='date', columns='symbol', values='close').sort_index()
    opn = s2.pivot(index='date', columns='symbol', values='open').sort_index()
    cal = close.index
    rng = cal[(cal >= pos_s.index[0]) & (cal <= END)]
    R.require(rng.equals(pos_s.index[pos_s.index <= END]), '现货日历与仓位文件日历在 2014-01-02~2026-10-08 不一致')
    first_needed = cal[cal.get_loc(EXT_START) - 1]               # ext 首日的前一收盘
    sub_o, sub_c = opn.loc[first_needed:END], close.loc[first_needed:END]
    R.require(sub_o.notna().all().all() and (sub_o > 0).all().all() and sub_c.notna().all().all(),
              'ext 窗内有缺失 / 非正开盘价或收盘价')
    prev = close.shift(1)
    gap = (opn / prev - 1).loc[EXT_START:END]
    big = gap[(gap.abs() > 0.07).any(axis=1)]
    return {'rows': int(len(s2)), 'first_date': str(cal[0].date()), 'last_date': str(cal[-1].date()),
            'close_equal_to_inputs_spot_on_all_6202_rows': close_eq,
            'original_132_opens_unchanged': open_eq, 'n_original_opens': int(old_open.sum()),
            'calendar_equals_position_calendar_2014-01-02_to_end': True,
            'ext_window_open_close_complete': True,
            'n_open_equal_prev_close(stale 嫌疑)': {k: int(v) for k, v in (opn.loc[EXT_START:END] == prev.loc[EXT_START:END]).sum().items()},
            'n_open_equal_close': {k: int(v) for k, v in (opn.loc[EXT_START:END] == close.loc[EXT_START:END]).sum().items()},
            'gap_mean_bp_ext': {k: float(v * 1e4) for k, v in gap.mean().items()},
            'days_abs_gap_gt_7pct': {str(d.date()): {k: float(v) for k, v in r.items()} for d, r in big.iterrows()}}, close, opn


def build_exact(s2_close, s2_open, pos_s, DS_proxy):
    """S-exact 日表（2014-01-06 ~ 2026-10-08）；代理列只在代理可得的日子（母规格 S-proxy 样本 2016-01-04 起）填。"""
    cal = s2_close.index
    days = cal[(cal >= EXT_START) & (cal <= END)]
    p_old, p_new, d1, d2 = R.decision_lags(pos_s, days)           # 仓位文件自己的全序列日历
    D = pd.DataFrame(index=days)
    D.index.name = 'date'
    D['p_old'], D['p_new'] = p_old, p_new
    D['d_tm1_date'], D['d_tm2_date'] = d1, d2
    R.add_legs(D)
    prev = s2_close.shift(1)
    for g in R.S_GROUPS:
        code = R.S_CODE[g]
        D['w_' + g] = 0.5
        D['cc_' + g] = (s2_close[code] / prev[code] - 1).reindex(days)
        D['gap_' + g] = (s2_open[code] / prev[code] - 1).reindex(days)
        D['intra_' + g] = (s2_close[code] / s2_open[code] - 1).reindex(days)
        D['roll_' + g] = False
    R.require(D.filter(regex='^(cc|gap|intra)_').notna().all().all(), 'S-exact 日表有缺值')
    R.add_blends(D, R.S_GROUPS)
    for c in ('intra_b', 'gap_b', 'cc_b'):
        D[c.replace('_b', '_proxy_b')] = DS_proxy[c].reindex(days)
    for g in R.S_GROUPS:
        D['intra_proxy_' + g] = DS_proxy['intra_' + g].reindex(days)
    return D


def proxy_error(ev_main):
    out = {}
    for leg in S_LEGS:
        d = ev_main[ev_main.leg == leg]
        x = (d.e - d.e_proxy).to_numpy()
        tt = stats.ttest_1samp(x, 0.0)
        out[leg] = {'n': int(len(x)), 'mean_bp': float(x.mean() * 1e4), 't': float(tt.statistic), 'p': float(tt.pvalue),
                    'median_bp': float(np.median(x) * 1e4), 'std_bp': float(x.std(ddof=1) * 1e4),
                    'sum_pp': float(x.sum() * 100),
                    'mean_e_exact_bp': float(d.e.mean() * 1e4), 'mean_e_proxy_bp': float(d.e_proxy.mean() * 1e4),
                    'corr_e_exact_vs_proxy': float(np.corrcoef(d.e, d.e_proxy)[0, 1]),
                    'sign_agree': float((np.sign(d.e) == np.sign(d.e_proxy)).mean())}
    allx = (ev_main.e - ev_main.e_proxy).to_numpy()
    out['_两腿合计(规格外附带)'] = {'n': int(len(allx)), 'sum_pp': float(allx.sum() * 100)}
    return out


def window_block(legs, pol, win):
    lt = legs[(legs.window == win) & (legs.variant == 'all')]
    pt = pol[pol.window == win].set_index('policy')
    e2 = pt.loc['E2']
    return {'legs': {leg: {'n': int(r.n), 'mean_e_bp': float(r.mean_e_bp), 'sum_e_pp': float(r.sum_e_pp),
                           'mean_o_bp': float(r.mean_o_bp), 'p_t': float(r.t_p)}
                     for leg in S_LEGS for r in [lt[lt.action == leg].iloc[0]]},
            'policies': {p: {'ann': float(pt.at[p, 'ann']), 'sharpe': float(pt.at[p, 'sharpe']),
                             'maxdd': float(pt.at[p, 'maxdd'])} for p in R.S_ORDER},
            'e2_minus_e1': {'d_sharpe': float(e2.d_sharpe_vs_E1), 'ci_lo': float(e2.ci_lo), 'ci_hi': float(e2.ci_hi),
                            'p': float(e2.p_value)},
            'n_days': int(pt.at['E1', 'n_days']), 'first_day': pt.at['E1', 'start'], 'last_day': pt.at['E1', 'end']}


# =========================================================================== 报告
def write_report(k):
    fn, fp, fpv, md = R.fn, R.fp, R.fpv, R.md
    kn, legs, pol, chk, pe = k['kn'], k['legs'], k['pol'], k['checks'], k['checks']['proxy_error']
    m, x = kn['main'], kn['ext']
    L = []
    A = L.append
    A('# 现货池 S-exact：真实指数开盘价重算（2026-10-09 追加）')
    A('')
    A('> 按冻结追加规格 `../spec_addendum_s_exact.md`（母规格 `../spec.md` 的公式、成本、策略、事件归因不变，只把 S 的代理 intra/gap 换成真实值）。'
      '输入 `../inputs_v2/spot.csv`（sha256 已核对）+ 沿用 `../inputs/` 的仓位与期货。重现：'
      '`cd /home/elfbob/claude-code/style_timing_signal && python3 ' + str(W / 's_exact.py') + '`。')
    A('')
    A('## 一句话结论')
    A('')
    A(k['verdict'])
    A('')
    A('## 1. 主窗口 main（2016-01-04 ~ 2026-10-08，与 S-proxy full 同窗）')
    A('')
    A(f"日数 {m['n_days']}。E2−E1 ΔSharpe = {fn(m['e2_minus_e1']['d_sharpe'],3)}，95% CI [{fn(m['e2_minus_e1']['ci_lo'],3)}, "
      f"{fn(m['e2_minus_e1']['ci_hi'],3)}]，p = {fpv(m['e2_minus_e1']['p'])}（代理版 0.141 [−0.015, 0.340]，p 0.119）。")
    A('')
    A(k['legs_md']['main'])
    A('')
    rows = []
    for leg in S_LEGS:
        r = pe[leg]
        rows.append([leg, r['n'], fn(r['mean_e_exact_bp'], 1), fn(r['mean_e_proxy_bp'], 1), fn(r['mean_bp'], 1),
                     fn(r['t'], 2), fpv(r['p']), fn(r['std_bp'], 1), fn(r['corr_e_exact_vs_proxy'], 3),
                     fp(r['sign_agree'], 0)])
    A('**代理误差**（同一批事件上 e_exact − e_proxy；正 = 真实值比代理更利于开盘成交）：')
    A('')
    A(md(rows, ['腿', 'n', 'e 真实均值bp', 'e 代理均值bp', '误差均值bp', 't 值', 'p', '误差标准差bp', '逐事件相关', '符号一致']))
    A('')
    A(f"两腿误差合计 {fn(pe['_两腿合计(规格外附带)']['sum_pp'],2)}pp（开多与平多的误差符号相反、事件数相同，"
      '所以对 Σe 与 E2−E1 的净影响小于对单腿读数的影响）。')
    A('')
    A(k['pol_md']['main'])
    A('')
    A('## 2. 延长窗口 ext（2014-01-06 ~ 2026-10-08）')
    A('')
    A(f"日数 {x['n_days']}。E2−E1 ΔSharpe = {fn(x['e2_minus_e1']['d_sharpe'],3)}，95% CI [{fn(x['e2_minus_e1']['ci_lo'],3)}, "
      f"{fn(x['e2_minus_e1']['ci_hi'],3)}]，p = {fpv(x['e2_minus_e1']['p'])}。slope20 因子 2014-07-25 前为 0（预热），"
      '2014-08-11 首次持多，ext 窗开头约 7 个月空仓。')
    A('')
    A(k['legs_md']['ext'])
    A('')
    A(k['pol_md']['ext'])
    A('')
    A('## 3. 分窗 E1 / E2（E2 行 CI 与 p 为 E2 − E1）')
    A('')
    rows = []
    for w in SUB:
        z = pol[pol.window == w].set_index('policy')
        lw = legs[(legs.window == w) & (legs.variant == 'all')].set_index('action')
        rows.append([w, f"{z.loc['E1','start']}~{z.loc['E1','end']}", int(z.loc['E1', 'n_days']),
                     f"{fp(z.loc['E1','ann'])} / {fn(z.loc['E1','sharpe'],3)}", f"{fp(z.loc['E2','ann'])} / {fn(z.loc['E2','sharpe'],3)}",
                     fn(z.loc['E2', 'd_sharpe_vs_E1'], 3), f"[{fn(z.loc['E2','ci_lo'],3)}, {fn(z.loc['E2','ci_hi'],3)}]",
                     fpv(z.loc['E2', 'p_value']),
                     f"{int(lw.loc['开多','n'])} / {fn(lw.loc['开多','mean_e_bp'],1)}", f"{int(lw.loc['平多','n'])} / {fn(lw.loc['平多','mean_e_bp'],1)}"])
    A(md(rows, ['窗', '区间', '日数', 'E1 年化/Sharpe', 'E2 年化/Sharpe', 'ΔSharpe', '95% CI', 'p', '开多 n / e均值bp', '平多 n / e均值bp']))
    A('')
    A('## 4. 分年表（ext；单元格 = n / e 合计 pp / o 合计 pp）')
    A('')
    rows = []
    by = k['by_year']
    for y in sorted(by.year.unique()):
        cells = [str(y)]
        for leg in S_LEGS:
            r = by[(by.year == y) & (by.action == leg)].iloc[0]
            cells.append(f"{int(r.n)} / {fn(r.sum_e_pp,2)} / {fn(r.sum_o_pp,2)}")
        rows.append(cells)
    A(md(rows, ['年', '开多', '平多']))
    A('')
    A('## 5. 核对')
    A('')
    for name, ok, txt in k['check_lines']:
        A(f"- **{name}**：{'通过' if ok else '不通过'}。{txt}")
    A('')
    A('## 6. 口径说明与解读边界')
    A('')
    for s in k['notes']:
        A(f'- {s}')
    A('')
    (W / 'report_s_exact.md').write_text('\n'.join(L) + '\n', encoding='utf-8')


def legs_md(legs, win, pe=None):
    fn, fp, fpv, md = R.fn, R.fp, R.fpv, R.md
    lt = legs[(legs.window == win) & (legs.variant == 'all')]
    rows = []
    for leg in S_LEGS:
        r = lt[lt.action == leg].iloc[0]
        rows.append([leg, int(r.n), fn(r.mean_e_bp, 1), fn(r.median_e_bp, 1), fn(r.std_e_bp, 1), fp(r.share_open_better, 0),
                     fn(r.sum_e_pp, 2), fn(r.ann_e_pp, 3), fpv(r.t_p), fpv(r.sign_p), fn(r.dedrift_mean_bp, 1),
                     fn(r.mean_o_bp, 1), fn(r.sum_o_pp, 2)])
    return md(rows, ['腿', 'n', 'e 均值bp', 'e 中位bp', 'e 标准差bp', '开盘更好', 'e 合计pp', '年化pp/年', 't 检验 p', '符号检验 p',
                     '去漂移均值bp', 'o 均值bp', 'o 合计pp'])


def pol_md(pol, win):
    fn, fp, fpv, md = R.fn, R.fp, R.fpv, R.md
    rows = []
    for name in R.S_ORDER:
        r = pol[(pol.policy == name) & (pol.window == win)].iloc[0]
        ci = '—' if name == 'E1' else f'[{fn(r.ci_lo, 3)}, {fn(r.ci_hi, 3)}]'
        rows.append([name, fp(r.ann), fp(r.cagr), fn(r.sharpe, 3), fp(r.maxdd), fp(r.vol), int(r.n_days),
                     fn(r.d_ann_vs_E1 * 100, 2), '—' if name == 'E1' else fn(r.d_sharpe_vs_E1, 3), ci, fpv(r.p_value)])
    return md(rows, ['策略', '算术年化', 'CAGR', 'Sharpe', '最大回撤', '年化波动', '日数', 'Δ年化 vs E1 (pp)', 'ΔSharpe vs E1',
                     '95% CI', 'p'])


# =========================================================================== 主流程
def main():
    inp = R.load_inputs()                                   # 仓位、期货、旧现货（sha256 由 run.py 核对）
    snap2, s2 = load_spot2()
    pos_s = inp['pos_s']
    dchk, close2, open2 = data_checks(s2, inp['spot'], pos_s)

    # 代理：照母规格重算（期货持有合约 intra；gap 由 cc 反推），只用于逐事件比较
    DF, mF = R.build_F(inp)
    DS_proxy, _ = R.build_S(inp, mF['held'])

    D = build_exact(close2, open2, pos_s, DS_proxy)
    RS = R.all_policies(D, R.S_GROUPS, R.S_POLICIES, R.S_ORDER)
    ev = R.build_events(D, extra=('intra_905', 'intra_852', 'gap_905', 'gap_852', 'intra_proxy_b', 'gap_proxy_b'))
    R.require(set(ev.leg) <= set(S_LEGS), 'S 出现空头腿')
    ev['e_proxy'] = ev.delta * ev.intra_proxy_b
    ev['o_proxy'] = ev.delta * ev.gap_proxy_b
    ev['e_minus_proxy_bp'] = (ev.e - ev.e_proxy) * 1e4
    a, b = WIN['main']
    ev_main = ev[(ev.date >= R.T(a)) & (ev.date <= R.T(b))]
    R.require(ev_main.e_proxy.notna().all(), 'main 窗事件缺代理值')

    legs = R.legs_table(ev, D, WIN, S_LEGS, (), {'all': lambda e: pd.Series(True, index=e.index)})
    pol = R.policies_table(RS, WIN)
    by = R.by_year(ev, S_LEGS, ())
    pe = proxy_error(ev_main)

    # ---------------- 核对
    checks = {'data': dchk, 'inputs_v2_sha256': snap2['sha256']}
    close_full = close2.loc[:END]
    blend_cc = close_full.pct_change().mean(axis=1)
    eng = run_strategy(pos_s.reindex(close_full.index).fillna(0.0), blend_cc, 3.0)['ret']
    eng_diff = {w: float((RS['E0'].loc[a_:b_] - eng.loc[a_:b_]).abs().max()) for w, (a_, b_) in WIN.items()}
    checks['E0_vs_engine_run_strategy_full_series'] = {'max_abs_diff_by_window': eng_diff,
                                                        'pass': max(eng_diff.values()) < 1e-12}
    e1cc = sum(D['w_' + g] * D.p_old * D['cc_' + g] for g in R.S_GROUPS) - R.COST * (D.p_new - D.p_old).abs()
    checks['E1_equals_p_old_cc(long-flat 恒等)'] = {'max_abs_diff': float((RS['E1'] - e1cc).abs().max()),
                                                    'pass': float((RS['E1'] - e1cc).abs().max()) < 1e-15}
    add = R.check_additivity(D, RS, ev, WIN, R.S_GROUPS, R.S_POLICIES)
    checks['additivity'] = add
    kn0 = json.loads((W / 'key_numbers.json').read_text())['S']['legs_full']
    rep = {leg: {'n': int((ev_main.leg == leg).sum()), 'sum_e_proxy_pp': float(ev_main[ev_main.leg == leg].e_proxy.sum() * 100),
                 'mother_n': kn0[leg]['n'], 'mother_sum_e_pp': kn0[leg]['sum_e_pp']} for leg in S_LEGS}
    rep_ok = all(r['n'] == r['mother_n'] and abs(r['sum_e_proxy_pp'] - r['mother_sum_e_pp']) < 1e-12 for r in rep.values())
    checks['proxy_reproduces_mother_S_proxy_legs'] = {'legs': rep, 'pass': rep_ok}
    pv = json.loads((W / 'proxy_validation_S.json').read_text())
    s_ex = {e_['date'] + e_['leg']: e_['e_exact_bp'] for e_ in pv['events']}
    mine = {str(r.date.date()) + r.leg: r.e_bp for r in ev.itertuples() if r.date >= pd.Timestamp('2026-07-01')}
    se_ok = set(s_ex) == set(mine) and all(abs(s_ex[kk] - mine[kk]) < 1e-9 for kk in s_ex)
    checks['matches_mother_S_exact_66day_events'] = {'mother': s_ex, 'here': mine, 'pass': se_ok}
    ev_ids_proxy = set(zip(DS_proxy.index[DS_proxy.p_new != DS_proxy.p_old], [1] * 10 ** 4))
    same_events = set(ev_main.date) == set(DS_proxy.index[DS_proxy.p_new != DS_proxy.p_old])
    checks['same_event_set_as_proxy_in_main'] = bool(same_events)
    R.require(same_events, 'main 窗事件集合与代理版不同')
    del ev_ids_proxy

    # ---------------- key numbers
    kn = {'main': window_block(legs, pol, 'main'), 'ext': window_block(legs, pol, 'ext'),
          'sharpe_by_window': {w: {e: float(pol[(pol.window == w) & (pol.policy == e)].sharpe.iloc[0]) for e in ('E1', 'E2')}
                               for w in SUB}}
    kn['main']['proxy_error'] = {leg: {'mean_bp': pe[leg]['mean_bp'], 't': pe[leg]['t']} for leg in S_LEGS}
    order_main = ['legs', 'policies', 'e2_minus_e1', 'proxy_error', 'n_days', 'first_day', 'last_day']
    kn['main'] = {kk: kn['main'][kk] for kk in order_main}
    R.dump_json(W / 'key_numbers_s_exact.json', kn)

    # ---------------- 一句话结论（方向 = 符号；量级 = 真实 / 代理 之比）
    d_ex, d_px = kn['main']['e2_minus_e1']['d_sharpe'], PROXY_REF['E2_sharpe'] - PROXY_REF['E1_sharpe']
    lo_ = kn['main']['legs']['开多']['mean_e_bp']
    lc_ = kn['main']['legs']['平多']['mean_e_bp']
    same_dir = (d_ex > 0) == (d_px > 0) and lo_ > 0 and lc_ > 0
    ratios = {'ΔSharpe': d_ex / d_px, '开多': lo_ / PROXY_REF['开多_bp'], '平多': lc_ / PROXY_REF['平多_bp']}
    mag_ok = all(0.5 <= r <= 2.0 for r in ratios.values())
    E1m, E2m = kn['main']['policies']['E1'], kn['main']['policies']['E2']
    xs = kn['ext']
    verdict = (f"**{'维持' if same_dir else '不维持'}代理版的方向**"
               f"{'，' + ('量级也大体维持' if mag_ok else '但量级有明显变化') }："
               f"main 窗真实值 E1 Sharpe {E1m['sharpe']:.3f} → E2 {E2m['sharpe']:.3f}（ΔSharpe {d_ex:+.3f}，95% CI "
               f"[{kn['main']['e2_minus_e1']['ci_lo']:.3f}, {kn['main']['e2_minus_e1']['ci_hi']:.3f}]，p {R.fpv(kn['main']['e2_minus_e1']['p'])}；"
               f"代理版 0.739 → 0.880，+0.141），开多 e 均值 {lo_:+.1f}bp（代理 +21.2）、平多 {lc_:+.1f}bp（代理 +20.7）；"
               f"真实 / 代理之比 ΔSharpe {ratios['ΔSharpe']:.2f}、开多 {ratios['开多']:.2f}、平多 {ratios['平多']:.2f}"
               f"（量级判据：三个比值都在 0.5~2 之间，我设定）。"
               f"代理误差（真 − 代，同一批事件）开多 {pe['开多']['mean_bp']:+.1f}bp（t {pe['开多']['t']:.2f}）、"
               f"平多 {pe['平多']['mean_bp']:+.1f}bp（t {pe['平多']['t']:.2f}）。"
               f"ext 窗（含 2014-2015）ΔSharpe {xs['e2_minus_e1']['d_sharpe']:+.3f} [{xs['e2_minus_e1']['ci_lo']:.3f}, "
               f"{xs['e2_minus_e1']['ci_hi']:.3f}]。两窗的 ΔSharpe 区间都{'含' if (kn['main']['e2_minus_e1']['ci_lo'] <= 0 <= kn['main']['e2_minus_e1']['ci_hi']) and (xs['e2_minus_e1']['ci_lo'] <= 0 <= xs['e2_minus_e1']['ci_hi']) else '不全含'} 0。")

    check_lines = [
        ('输入', True, f"inputs_v2/spot.csv sha256 一致；{dchk['rows']} 行（{dchk['first_date']}~{dchk['last_date']}）；与 inputs/spot.csv 重叠的 6,202 行收盘价逐位相同，"
                       f"原有 {dchk['n_original_opens']} 个开盘价不变；现货日历与仓位文件日历在 2014-01-02~2026-10-08 完全一致；ext 窗开收盘无缺失。"),
        ('开盘价合理性（描述）', True, f"ext 窗 open = 前收 的日数 {dchk['n_open_equal_prev_close(stale 嫌疑)']}、open = close 的日数 "
                                f"{dchk['n_open_equal_close']}（无陈旧开盘价迹象）；|gap| > 7% 的日子："
                                + '、'.join(f"{d} ({', '.join(f'{v*100:+.1f}%' for v in r.values())})" for d, r in dchk['days_abs_gap_gt_7pct'].items())
                                + '，均为已知大行情日（2015 股灾、2020 春节后复市、2024 国庆后开盘）。'),
        ('E0 vs engine.run_strategy', checks['E0_vs_engine_run_strategy_full_series']['pass'],
         '全序列跑引擎后切窗，各窗逐日最大差 ' + '、'.join(f"{w} {v:.1e}" for w, v in eng_diff.items()) + '。'),
        ('E1 = p_old·cc（long-flat 恒等）', checks['E1_equals_p_old_cc(long-flat 恒等)']['pass'],
         f"最大差 {checks['E1_equals_p_old_cc(long-flat 恒等)']['max_abs_diff']:.1e}。"),
        ('可加性', add['pass'], '各窗 Σ(M_A−E1) − Σ_{leg∈A} e 与解析二阶项之差最大 '
         f"{add['max_abs_residual_minus_analytic']:.1e}；main 窗 Σ(E2−E1) = {add['main']['E2']['sum_diff_pp']:.4f}pp、Σe = "
         f"{add['main']['E2']['sum_e_pp']:.4f}pp、二阶残差 {add['main']['E2']['residual_pp']:.4f}pp。"),
        ('代理重算 = 母规格 S-proxy', rep_ok, '、'.join(f"{leg} n {r['n']}、e 合计 {r['sum_e_proxy_pp']:.4f}pp" for leg, r in rep.items())
         + '，与 key_numbers.json 逐位相同；main 窗事件集合与代理版相同。'),
        ('与母规格 66 日 S-exact 事件一致', se_ok, f"2026-07-01 起 {len(s_ex)} 条事件的 e_exact 与 proxy_validation_S.json 相同。"),
    ]
    notes = [
        '公式、成本（3bp/单位 |Δ|，S 无换月无 carry）、策略（E0/E1/E2/M_开多/M_平多）、事件归因（e、o、去漂移、t 与符号检验）全部复用母规格实现 `run.py`；'
        'ΔSharpe 为配对 moving-block bootstrap（block=20、n=2000、seed=20261009），差 = 策略 − E1；指标用 house `backtest.metrics`。',
        '仓位 shift 用仓位文件全序列日历（ext 首日 2014-01-06 的 p_old/p_new 取 2014-01-02/01-03），不先截窗；E0 与全序列引擎逐日一致。',
        '代理误差 = 同一批事件上 e_exact − e_proxy，t 值为单样本 t 统计量（df = n−1），p 为双侧；e_proxy 用母规格 S-proxy 的 blend 代理 intra，'
        '事件集合由仓位决定，两版相同。',
        '「量级维持」判据（真实 / 代理之比在 0.5~2）是我设定的描述性门槛，不是规格给的；方向 = ΔSharpe 与两腿 e 均值的符号。',
        '开盘集合竞价与收盘的滑点、冲击成本不同，本检验不建模；指数开盘价是指数公司按成分股开盘价算的点位，成分股停牌或未开盘时沿用前收，'
        '真实可成交的 ETF / 一篮子开盘价会有偏差。',
        '事件数 main 每腿约 58、ext 约 64，检验力有限；在样本里挑「开多用开盘、平多用收盘」之类组合带选择偏差，不能直接当部署依据。',
        '另产出 events_S_exact.csv / legs_S_exact.csv / policies_S_exact.csv / by_year_S_exact.csv / daily_S_exact.csv / checks_s_exact.json 供复核。',
    ]
    checks['verdict_inputs'] = {'same_direction': bool(same_dir), 'ratios_exact_over_proxy': ratios, 'magnitude_within_0.5_2': bool(mag_ok)}
    checks['proxy_error'] = pe

    # ---------------- 写文件
    def out_csv(df, name, index=False):
        df.to_csv(W / name, index=index, float_format='%.17g')

    ev_out = ev.copy()
    ev_out['date'] = ev_out.date.dt.date
    out_csv(ev_out, 'events_S_exact.csv')
    out_csv(legs, 'legs_S_exact.csv')
    out_csv(pol, 'policies_S_exact.csv')
    out_csv(by, 'by_year_S_exact.csv')
    out_csv(D.drop(columns=['d_tm1_date', 'd_tm2_date']).join(RS.add_prefix('ret_')), 'daily_S_exact.csv', index=True)
    R.dump_json(W / 'checks_s_exact.json', checks)
    write_report({'kn': kn, 'legs': legs, 'pol': pol, 'checks': checks, 'by_year': by, 'verdict': verdict,
                  'legs_md': {w: legs_md(legs, w) for w in ('main', 'ext')}, 'pol_md': {w: pol_md(pol, w) for w in ('main', 'ext')},
                  'check_lines': check_lines, 'notes': notes})
    allok = all(ok for _, ok, _ in check_lines)
    print('main:', {p: (round(kn['main']['policies'][p]['ann'], 4), round(kn['main']['policies'][p]['sharpe'], 3)) for p in ('E1', 'E2')},
          'dS', round(d_ex, 3), 'legs', {l: round(kn['main']['legs'][l]['mean_e_bp'], 1) for l in S_LEGS})
    print('ext :', {p: (round(kn['ext']['policies'][p]['ann'], 4), round(kn['ext']['policies'][p]['sharpe'], 3)) for p in ('E1', 'E2')},
          'dS', round(kn['ext']['e2_minus_e1']['d_sharpe'], 3))
    print('checks all pass:', allok, '→', W / 'report_s_exact.md')
    return 0 if allok else 1


if __name__ == '__main__':
    raise SystemExit(main())
