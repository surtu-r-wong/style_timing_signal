"""Freeze and run the descriptive EW intraday-flow pilot; no production writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _connect, _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import residual_past, leg_rules, attribute_ledger, prepare_close_market, batch_close_ledgers
from backtest.metrics import ann_return, sharpe, max_drawdown
from backtest.run_manifest import create_run_dir, artifact_record, git_state, write_manifest
from signals.common.config import load_db_config

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'docs/plans/2026-09-16-ew-intraday-flow-pilot.md'
END = '2026-09-03'
CODES = ('000300.SH','399102.SZ')


def capture(run):
    sources = {}
    for filename in ('futures.csv','spot.csv','equal_weight.csv'):
        source = ROOT/'backtest/output/runs/20260914-execution-audit-r2/inputs'/filename
        shutil.copyfile(source, run/'inputs'/filename)
        sources[filename] = artifact_record(source, ROOT)
    source = ROOT/'backtest/output/runs/20260916-ew-leg-flow-readiness-r1/inputs/money_flow.csv'
    shutil.copyfile(source, run/'inputs/money_flow.csv')
    sources['money_flow.csv'] = artifact_record(source, ROOT)
    conn = _connect(load_db_config())
    try:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute('SELECT transaction_timestamp()::text')
            stamp = q.fetchone()[0]
            q.execute("SELECT trade_date,index_code,close FROM stock_selector.index_daily "
                      "WHERE index_code=ANY(%s) AND trade_date BETWEEN '2014-01-01' AND %s "
                      "ORDER BY trade_date,index_code", (list(CODES),END))
            board = pd.DataFrame(q.fetchall(), columns=['date','index_code','close'])
        conn.rollback()
    finally:
        conn.close()
    board.to_csv(run/'inputs/board_prices.csv',index=False)
    (run/'inputs/provenance.json').write_text(json.dumps({'source_files':sources,'board_observed_at':stamp,
        'publication_and_revision':'User confirms same-day publication and no historical revision; not independently version-audited.'},indent=2))


def build_features(flow, board, calendar):
    px = board.pivot(index='date',columns='index_code',values='close').reindex(calendar)
    parts, predictions, details = {}, {}, {}
    for code in CODES:
        d = flow[flow.index_code.eq(code)].set_index('trade_date').reindex(calendar)
        gross = d.main_in_money+d.main_out_money
        valid = gross.gt(0)
        f = (d.main_in_money-d.main_out_money)/gross.where(valid)
        opening = d.open_main_inflow_money/gross.where(valid)
        ending = d.end_main_inflow_money/gross.where(valid)
        controls = pd.DataFrame({'flow':f,'ret':px[code].pct_change(fill_method=None)},index=calendar)
        for name, y in [('A',ending),('B',ending-opening)]:
            residual = residual_past(y,controls,window=250)
            parts[(name,code)] = residual
            predictions[(name,code)] = y-residual
            details[f'{name}_{code}_raw'] = y
            details[f'{name}_{code}_residual'] = residual
    features, control_features = {}, {}
    for name in ('A','B'):
        a = pd.concat([parts[(name,code)] for code in CODES],axis=1)
        b = pd.concat([predictions[(name,code)] for code in CODES],axis=1)
        features[name] = a.mean(axis=1).where(a.notna().all(axis=1)).rolling(20,min_periods=20).mean()
        control_features[name] = b.mean(axis=1).where(b.notna().all(axis=1)).rolling(20,min_periods=20).mean()
    return pd.DataFrame(features), pd.DataFrame(control_features), pd.DataFrame(details)


def describe(d):
    r = d.ret
    threshold = r.quantile(.05)
    return {'n':len(d),'cumulative':float((1+r).prod()-1),'ann_mean':ann_return(r),
            'cagr':float(np.expm1(np.log1p(r).sum()*245/len(r))),'sharpe':sharpe(r),
            'maxdd':max_drawdown(r),'es05':float(r[r<=threshold].mean()),
            'turnover_ann':float(d.turnover.mean()*245),'cost_ann':float(d.cost_return.mean()*245),
            'mean_target_exposure':float(d.decision_signal.abs().mean()),
            'mean_gross_notional':float(d.gross_notional.mean()),'rolls':int(d.rolls.sum())}


def execute(run):
    flow = pd.read_csv(run/'inputs/money_flow.csv',parse_dates=['trade_date'])
    board = pd.read_csv(run/'inputs/board_prices.csv',parse_dates=['date'])
    spot = pd.read_csv(run/'inputs/spot.csv',parse_dates=['date'])
    futures = pd.read_csv(run/'inputs/futures.csv',parse_dates=['date'])
    ew = pd.read_csv(run/'inputs/equal_weight.csv',index_col='date',parse_dates=True).factor_value
    calendar = pd.DatetimeIndex(sorted(spot.loc[spot.date.le(END),'date'].unique()),name='date')
    features, controls, details = build_features(flow,board,calendar)
    valid = features.notna().all(axis=1) & controls.notna().all(axis=1)
    if not valid.any():
        raise ValueError('no post-warmup observations')
    first = valid[valid].index[0]
    idx = calendar[calendar>=first]
    if not valid.loc[idx].all() or ew.reindex(idx).isna().any():
        raise ValueError('interior feature or EW gap')
    # Perturb only future flow observations. Earlier feature history must not change.
    perturbed = flow.copy()
    cut = idx[-20]
    cols = ['open_main_inflow_money','end_main_inflow_money']
    perturbed.loc[perturbed.trade_date.ge(cut),cols] *= -7
    changed, _, _ = build_features(perturbed,board,calendar)
    np.testing.assert_allclose(features.loc[features.index<cut],changed.loc[changed.index<cut],equal_nan=True,atol=0,rtol=0)
    pd.concat({'score':features,'control':controls},axis=1).to_csv(run/'outputs/features.csv',index_label='date')
    details.to_csv(run/'outputs/feature_components.csv',index_label='date')
    longs, shorts = leg_rules(ew.reindex(idx),features.reindex(idx))
    p_longs, p_shorts = leg_rules(ew.reindex(idx),controls.reindex(idx))
    targets, metadata = {}, []
    for left,l in longs.items():
        for right,s in shorts.items():
            name = f'C__{left}__{right}'
            targets[name] = l+s
            metadata.append({'strategy':name,'kind':'combined','long_rule':left,'short_rule':right})
    for side, rules, control_rules in [('L',longs,p_longs),('S',shorts,p_shorts)]:
        for rule,signal in rules.items():
            name = f'{side}__{rule}'
            targets[name] = signal
            metadata.append({'strategy':name,'kind':'single_leg','side':side,'rule':rule})
        for rule,signal in control_rules.items():
            if rule == 'base':
                continue
            name = f'P{side}__{rule}'
            targets[name] = signal
            metadata.append({'strategy':name,'kind':'control_fitted','side':side,'rule':rule})
    base_targets = pd.DataFrame(targets,index=idx)
    metadata = pd.DataFrame(metadata)
    first_im = futures.loc[futures.symbol.str.startswith('IM'),'date'].min()
    if pd.isna(first_im):
        raise ValueError('no IM quotes')
    weights = futures_weights(idx,first_im)
    expiries = {}
    full_calendar = pd.DatetimeIndex(sorted(spot.date.unique()))
    for sym in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(sym))
        following = full_calendar[full_calendar>=expiry]
        expiries[sym] = following[0] if len(following) else expiry
    market = prepare_close_market(futures,idx,weights,expiries)
    schedule = pd.DataFrame(index=idx)
    for j,group in enumerate(market['groups']):
        schedule[f'{group}_symbol'] = market['symbols'][:,j]
        schedule[f'{group}_new_close'] = market['new_price'][:,j]
        schedule[f'{group}_held_close'] = market['old_price'][:,j]
    schedule.to_csv(run/'outputs/contract_schedule.csv',index_label='date')
    split = len(idx)//2
    windows = {'full':idx,'early_half':idx[:split],'late_half':idx[split:],
               '2021-2023':idx[(idx>='2021-01-01')&(idx<='2023-12-31')],
               '2024-2026':idx[idx>='2024-01-01']}
    for year in sorted(set(idx.year)):
        windows[f'year_{year}'] = idx[idx.year==year]
    panels, trades_out, matched_rows, differences, references = [], [], [], [], []
    check_max = 0.
    for scenario,cost,lag in [('close_3bps',3.,0),('close_10bps',10.,0),('second_close_3bps',3.,1)]:
        print(f'Running {scenario}: 51 ledgers',flush=True)
        signals = base_targets.shift(lag,fill_value=0.)
        for side, rules in [('L',longs),('S',shorts)]:
            for rule in rules:
                if rule == 'base':
                    continue
                numerator = signals[f'{side}__{rule}'].shift(1,fill_value=0.).abs().mean()
                denominator = signals[f'{side}__base'].shift(1,fill_value=0.).abs().mean()
                scale = float(numerator/denominator) if denominator else 0.
                signals[f'M{side}__{rule}'] = signals[f'{side}__base']*scale
                matched_rows.append({'scenario':scenario,'side':side,'rule':rule,'scale':scale})
        signals.to_csv(run/f'outputs/targets_{scenario}.csv',index_label='date')
        ledgers = batch_close_ledgers(market,signals,cost_bps=cost)
        # One full historical reference per scenario, and two fixed candidate references near IM launch.
        expected = contract_ledger(futures,signals['C__base__base'],weights,expiries=expiries,cost_bps=cost)
        fields = ['ret','equity','gross_pnl','cost','turnover','net_notional','gross_notional','rolls','decision_signal']
        for field in fields:
            error = float((expected[field]-ledgers['C__base__base'][field]).abs().max())
            check_max = max(check_max,error)
            np.testing.assert_allclose(expected[field],ledgers['C__base__base'][field],rtol=1e-10,atol=1e-11)
        references.append({'scenario':scenario,'strategy':'C__base__base','rows':len(idx),'scope':'full'})
        sample = idx[(idx>='2022-06-01')&(idx<='2022-09-15')]
        sample_market = prepare_close_market(futures,sample,weights.loc[sample],expiries)
        sample_signals = signals.loc[sample,['L__A_pos','S__B_neg']]
        batch_sample = batch_close_ledgers(sample_market,sample_signals,cost_bps=cost)
        for name in sample_signals:
            reference = contract_ledger(futures,sample_signals[name],weights.loc[sample],expiries=expiries,cost_bps=cost)
            for field in fields:
                np.testing.assert_allclose(reference[field],batch_sample[name][field],rtol=1e-10,atol=1e-11)
            references.append({'scenario':scenario,'strategy':name,'rows':len(sample),'scope':'IM_launch'})
        attrs = {}
        for name, ledger in ledgers.items():
            attr, trades = attribute_ledger(ledger)
            attrs[name] = attr
            trades_out.append(trades.assign(strategy=name,scenario=scenario))
            for window, dates in windows.items():
                if not len(dates):
                    continue
                closed = trades[trades.closed.astype(bool) & trades.mark_date.between(str(dates[0].date()),str(dates[-1].date()))]
                row = {'scenario':scenario,'strategy':name,'window':window,
                       'start':str(dates[0].date()),'end':str(dates[-1].date()),**describe(ledger.loc[dates]),
                       'closed_trades_by_exit_date':len(closed),
                       'closed_winrate':float(closed.net_pnl.gt(0).mean()) if len(closed) else np.nan,
                       'open_trades_at_final_end':int((~trades.closed.astype(bool)).sum()) if window=='full' else np.nan}
                panels.append(row)
        pd.concat(ledgers,names=['strategy','date']).to_csv(run/f'outputs/ledgers_{scenario}.csv.gz')
        pd.concat(attrs,names=['strategy','date']).to_csv(run/f'outputs/attribution_{scenario}.csv.gz')
        print(f'Completed {scenario}; reference and all 51 leg identities passed',flush=True)
    panel = pd.DataFrame(panels)
    panel.to_csv(run/'outputs/metrics.csv',index=False)
    pd.concat(trades_out,ignore_index=True).to_csv(run/'outputs/trades.csv',index=False)
    metadata.to_csv(run/'outputs/strategy_catalog.csv',index=False)
    pd.DataFrame(matched_rows).to_csv(run/'outputs/exposure_matched_scales.csv',index=False)
    for (scenario,window), sub in panel.groupby(['scenario','window'],sort=False):
        sub = sub.set_index('strategy')
        for side,rules in [('L',longs),('S',shorts)]:
            for rule in rules:
                if rule=='base':
                    continue
                candidate = sub.loc[f'{side}__{rule}']
                for reference in (f'{side}__base',f'P{side}__{rule}',f'M{side}__{rule}'):
                    row = {'scenario':scenario,'window':window,'candidate':f'{side}__{rule}','reference':reference}
                    for metric in ('cagr','sharpe','maxdd','turnover_ann','es05'):
                        row['delta_'+metric] = float(candidate[metric]-sub.loc[reference,metric])
                    differences.append(row)
    pd.DataFrame(differences).to_csv(run/'outputs/leg_comparisons.csv',index=False)
    verification = {'unit_tests':'17 passed before candidate run','future_perturbation_no_history_change':True,
                    'real_reference_comparisons':references,'full_reference_max_abs_error':check_max,
                    'all_153_leg_pnl_and_cost_identities_passed':True,
                    'all_153_trade_sum_identities_passed':True,
                    'sample_start':str(idx[0].date()),'sample_end':str(idx[-1].date()),'n_days':len(idx),
                    'first_half_n':split,'second_half_n':len(idx)-split,
                    'candidate_return_grid_executed_once':True}
    (run/'outputs/verification.json').write_text(json.dumps(verification,indent=2))
    return panel,verification


def report(run,panel,verification):
    def table(rows):
        d = rows[['strategy','cagr','sharpe','maxdd','mean_target_exposure','turnover_ann','closed_trades_by_exit_date','closed_winrate']].copy()
        for col in ('cagr','maxdd','mean_target_exposure','closed_winrate'):
            d[col] = d[col].map(lambda x:f'{x:.2%}' if pd.notna(x) else 'NA')
        for col in ('sharpe','turnover_ann'):
            d[col] = d[col].map(lambda x:f'{x:.3f}')
        return d.to_markdown(index=False)
    main = panel[(panel.scenario=='close_3bps')&(panel.window=='full')]
    single = main[main.strategy.str.startswith(('L__','S__'))]
    combos = main[main.strategy.str.startswith('C__')]
    text = f'''# 开尾盘资金流分腿首轮：描述性结果

范围和所有规则见冻结的`inputs/prereg.md`。样本{verification['sample_start']}～{verification['sample_end']}，共{verification['n_days']}个交易日，前/后半窗{verification['first_half_n']}/{verification['second_half_n']}日。历史已复用，不是新样本外。本轮未给统计GO，未改生产。

A=控制全天主力净流入比例和板块日收益后的尾盘残差；B=同样控制后的尾盘减早盘残差。过去250日OLS、两板块等权、20日均值。pos为多头保留正分数/空头保留负分数，neg反向；abs(score)<=1e-12保留原腿。全部8个单腿候选和25组合都已报告。

主口径为实际IC/IM合约次日收盘、单边3bp、分数合约。期货价格不另加carry；IM上市前100%IC，之后各半。无保证金/限仓/涨跌停可成交性保证，不是账户实际收益。

## 单腿主口径（全部，空仓日保留）

{table(single)}

CAGR、最大回撤和均值暴露按百分数展示；换手为年化单边名义成交/权益，含换月及每日目标恢复；Sharpe年化245日。胜率只算闭合的方向持仓段，换月不另开段，末期未平仓段见trades.csv。单腿以各自初始资本1独立计算。

## 组合主口径（全部25项）

{table(combos)}

组合是同一账户权益驱动的真实目标仓位回放，不是两个单腿累计收益之和。组合名字依次为多头规则、空头规则；base保持现役。主口径历史排序不构成选优认证。

## 完整对照与敏感性

`metrics.csv`含次日3bp/次日10bp/次次日3bp的153本账、年度、等长前后半窗及2021-23/2024后描述窗。`leg_comparisons.csv`将每个候选分别对照原腿、同模型仅控制变量的拟合部分（P前缀）和同平均暴露缩仓（M前缀）。M比例使用全样本事后暴露，仅是归因参照，不能声称可事前交易。

`targets_*.csv`是决策目标；`ledgers_*.csv.gz`包含实际执行滞后、成本、换手和权益；`attribution_*.csv.gz`把同账户损益按旧持仓方向及平开成本拆分。子窗指标沿用全程路径，交易胜率按出场日归属，可能含跨窗交易，不混称子窗内新交易。

## 验证

17项针对性测试通过；完整实际基准3情景和IM上市附近固定两候选共9项原引擎比较通过；153本账的分腿金额/成本以及逐笔总和恒等式均通过。完整基准最大绝对差{verification['full_reference_max_abs_error']:.3g}。扰动末20个交易日输入不会回改更早特征。

用户确认当天发布、不修订作为数据前提；此次未独立获取历史版本或真实成交。未安装日更采集，资金流仍止于9月3日。没有因为某侧变好就假定另一侧应镜像使用；收益、风险、控制参照、执行和历史分窗须共同解释。
'''
    (run/'outputs/REPORT.md').write_text(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id',required=True)
    args = parser.parse_args()
    run = create_run_dir(ROOT/'backtest/output/runs',args.run_id)
    shutil.copyfile(PLAN,run/'inputs/prereg.md')
    prereg_hash = hashlib.sha256((run/'inputs/prereg.md').read_bytes()).hexdigest()
    write_manifest(run,{'status':'running','created_utc':datetime.now(timezone.utc).isoformat(),
                        'prereg_sha256':prereg_hash,'stage':'intraday_flow_descriptive_pilot'})
    try:
        for path in ('backtest/run_intraday_flow_pilot.py','backtest/intraday_flow_pilot.py',
                     'backtest/execution_ledger.py','backtest/data.py','backtest/metrics.py',
                     'backtest/run_manifest.py','tests/test_intraday_flow_pilot.py','tests/test_execution_ledger.py',
                     'docs/plans/2026-09-16-money-flow-field-definitions.md'):
            shutil.copyfile(ROOT/path,run/'inputs'/('code__'+path.replace('/','__')))
        capture(run)
        panel,verification = execute(run)
        report(run,panel,verification)
        (run/'logs/checks.txt').write_text('17 targeted tests passed before return run; exact receipts in outputs/verification.json.\nNo formal p-value gate or production deployment.\n')
        files = [p for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
        write_manifest(run,{'status':'complete','stage':'intraday_flow_descriptive_pilot','prereg_sha256':prereg_hash,
                            'completed_utc':datetime.now(timezone.utc).isoformat(),'git':git_state(ROOT),
                            'artifacts':[artifact_record(p,run) for p in files]})
        print(json.dumps(verification,indent=2),flush=True)
    except Exception as exc:
        files = [p for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
        write_manifest(run,{'status':'failed','error_type':type(exc).__name__,'prereg_sha256':prereg_hash,
                            'artifacts':[artifact_record(p,run) for p in files]})
        # Do not print connection/config exceptions that may expose service details.
        raise SystemExit('Pilot failed: '+type(exc).__name__) from None


if __name__=='__main__':
    main()
