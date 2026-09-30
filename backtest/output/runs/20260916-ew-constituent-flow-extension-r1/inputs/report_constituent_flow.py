from pathlib import Path
import json
import numpy as np
import pandas as pd
R=Path('backtest/output/runs/20260916-ew-constituent-flow-extension-r1')
P=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
m=pd.read_csv(R/'outputs/metrics.csv');scores=pd.read_csv(R/'outputs/scores.csv',index_col=0,parse_dates=True)
t=pd.read_csv(R/'outputs/trades.csv');v=json.loads((R/'outputs/research_verification.json').read_text())
flow=pd.read_csv(R/'inputs/flow.csv');old=pd.read_csv(P/'inputs/money_flow.csv')
d=flow.merge(old,on=['index_code','trade_date'],suffixes=('_new','_old'))
newgross=d[['xlarge_buy_money','large_buy_money','xlarge_sell_money','large_sell_money']].sum(axis=1)
oldgross=(d.main_in_money+d.main_out_money)*10000
d['new_over_old_gross']=newgross/oldgross
comparison=[]
for code,g in d.groupby('index_code'):
 row={'index_code':code,'n':len(g),'gross_ratio_min':g.new_over_old_gross.min(),'gross_ratio_median':g.new_over_old_gross.median(),'gross_ratio_max':g.new_over_old_gross.max()}
 for tag,col in [('open','open_main_inflow_money'),('end','end_main_inflow_money')]:
  row[tag+'_matches_scaled_wset']=int(np.isclose(g[col+'_new'],g[col+'_old']*10000,rtol=1e-10,atol=1e-4).sum())
 comparison.append(row)
pd.DataFrame(comparison).to_csv(R/'outputs/wset_vs_constituent_amounts.csv',index=False)
d[['index_code','trade_date','new_over_old_gross']].sort_values('new_over_old_gross').head(15).to_csv(R/'outputs/wset_vs_constituent_lowest_gross_ratios.csv',index=False)
oldscore=pd.read_csv(R/'inputs/wset_continued_scores.csv',index_col=0,parse_dates=True)
newtarget=pd.read_csv(R/'outputs/targets_core_common_close_3bps.csv',index_col=0,parse_dates=True)
gates=[]
for name in ['CSI300','PAIR']:
 idx=newtarget.index
 gates.append({'source':name,'score_correlation':scores.loc[idx,name].corr(oldscore.loc[idx,name]),'target_disagreement_days':int((newtarget['C__'+name]!=newtarget['WSET__'+name]).sum())})
pd.DataFrame(gates).to_csv(R/'outputs/wset_vs_constituent_gates.csv',index=False)
# Closed episodes, with open episodes explicitly excluded from win rate.
win=[]
for (scope,scenario,strategy,side),g in t.groupby(['scope','scenario','strategy','side']):
 closed=g[g.closed]
 win.append({'scope':scope,'scenario':scenario,'strategy':strategy,'side':side,'closed_trades':len(closed),'open_trades':int((~g.closed).sum()),'win_rate':float(closed.net_pnl.gt(0).mean()) if len(closed) else None})
pd.DataFrame(win).to_csv(R/'outputs/trade_summary.csv',index=False)
labels={'C__base':'原组合','C__half':'原多头＋固定半空','C__CSI300':'300','C__CSI500':'500','C__CSI1000':'1000','C__CHINEXT':'创业板综','C__PAIR':'300＋创业板等权','C__POOL':'500＋1000等权','C__CORE':'四源等权','C__STAR':'科创综','C__ALL':'五源等权','WSET__CSI300':'旧WSET 300（单独对照）','WSET__PAIR':'旧WSET两源（单独对照）'}
def table(scope,short=False):
 z=m[(m.scope==scope)&(m.scenario=='close_3bps')&(m.window=='full')&(~m.strategy.str.startswith('S__'))]
 col='cumulative' if short else 'cagr';title='期间累计收益' if short else 'CAGR'
 lines=[f'| 空头过滤来源/参照 | {title} | Sharpe | 最大回撤 |','|---|---:|---:|---:|']
 for row in z.itertuples():lines.append(f'| {labels[row.strategy]} | {getattr(row,col):.2%} | {row.sharpe:.3f} | {row.maxdd:.2%} |')
 return '\n'.join(lines)
report='''# P1指数成分资金流：交付验收与固定来源比较结果

2026-09-16。[固定规格](2026-09-16-ew-constituent-flow-extension.md)下的研究已完成，run=`20260916-ew-constituent-flow-extension-r1`。500/1000数据已可用，当前没有字段、治理或价格补数阻塞。

**结论：固定B_neg空头过滤直接换成500/1000，没有带来原组合的收益或Sharpe提升；加入更多来源也没有改善。** 新表300单独输入改善Sharpe和回撤，但主窗CAGR略低于原组合，较固定半空的Sharpe增量也很小。以上只针对本公式/方向，不判定500/1000资金流的全部研究价值；生产不变。

## 交付验收

办公室最新回复06已取代原“待09-21/待字段”状态：用户协调恢复Wind额度，实际以WSD完成取数。只读消费`stock_selector.index_constituent_money_flow`，src=`wind:wsd:mfd`，单位元；不是旧WSET全板表，也不是本轮由WSS采集。I=超大单买入+大单买入，U=超大单卖出+大单卖出；原七个金额字段保留。

- 10,806行，75,642个金额值与办公室原快照逐项Decimal精确一致；唯一键、非空有限、买卖非负、I+U>0、source_unit/src均通过。
- 300/500/1000/创业板综各2,601行，2016-01-04至2026-09-15，内部无缺交易日；2015年源端无数据，不补0、不拼旧WSET。
- 科创综402行，2025-01-20至2026-09-15，内部无缺日。旧全板口径的2019年起点不能挪用。
- 五指数配套close已至9月15日，资金流有效窗口内无价格缺口。ON_DEMAND表没有持续日更义务，不将本次回填完成当自动日更已经接通。
- 源净额与I−U的差除以I+U，>1e-5仍为23行/12日，最大2.8075%；与办公室异常键逐个一致。全部原值保留，异常成簇并不能证明具体市场或供应商机制。

## 比较口径

沿原B=(E−O)/(I+U)，前250完整日OLS控制截距、(I−U)/(I+U)和各自指数日收益，再取20日均值。EW指空且分数为正时保留空头，否则空仓；abs<=1e-12保留原腿，多头不变。未重搜方向、窗口、阈值、权重。

为保持执行输入一致，期货实际合约、现役EW及其日历复用已冻结资料，收益终点为**2026-09-11**；资金流分数另算到09-15。不是价格补数仍受阻，也不是声称收益已更新至09-15。IC/IM依既有上市日、前日OI和到期换月规则；次日收盘、单边3bp为主，高成本10bp及多延迟一日作敏感性。每个共同窗从空仓初始化，不能拿不同起点的表直接排名。

## 主窗：2017-02-14至2026-09-11，2,330交易日

除最后两行标注旧WSET对照外，全部来自新指数成分表。表中是同账户组合收益，非单独空头收益。

'''+table('core_common')+'''

500/1000虽与IC/IM标的对应，来源贴近并不保证这套过滤规则有效。主口径500+1000等权CAGR31.52%、Sharpe1.402，均低于原组合36.12%/1.423；相对固定半空30.99%/1.575，收益只略高而风险调整表现与回撤更差。四源等权CAGR30.97%、Sharpe1.379，继续稀释结果。

对应空头单腿：原空头CAGR8.84%、Sharpe0.572；300为8.29%/0.700；500为4.67%/0.402；1000为5.05%/0.419；500+1000为5.16%/0.432。组合收益不是两腿独立复利相加。

新300组合CAGR35.43%，略低于原组合36.12%；Sharpe1.600、高于1.423，回撤−16.33%、浅于−34.45%。但固定半空Sharpe已有1.575，新300只多约0.025；不能把全部风险改善解释为新择时信息。不同来源保留空头日也不同：原1,131个指空日，300保留488、500保留515、1000保留596、500+1000保留532。完整暴露、换手、交易胜率及未平仓笔数均另存。

## 稳定性

- 主窗前后半各1,165日：500+1000的Sharpe从1.603降至1.245，后半CAGR29.63%，低于原组合37.16%和固定半空30.84%。1000单独的后半更弱，CAGR28.63%、Sharpe1.205。
- 10bp：500+1000的CAGR28.09%、Sharpe1.277；原组合31.98%/1.291、固定半空28.06%/1.450。新300为31.60%/1.456，相对半空的Sharpe优势仅约0.007。
- 多延迟一日：500+1000为28.58%/1.296，低于原组合CAGR30.97%，也低于固定半空Sharpe1.417。新300为32.85%/1.510；新两源为30.28%/1.387，Sharpe低于固定半空。
- 所有年度、前后半和2024后结果保留在metrics.csv；没有把较差窗口删去或再调参数。

## 科创共同短窗：2026-03-06至2026-09-11，131交易日

科创仅402个原始交易日，250+20预热后只能得到很短的共同收益窗。此处用期间累计收益，避免把半年年化表现当长期证据；完整年化指标仍保留在CSV。

'''+table('star_common',True)+'''

科创单独及五源等权未带来优势，但131日不能作为长期有效/无效裁决。此窗不是新的样本外确认。

## 净额差异与换源诊断

预设诊断把全天控制替换为供应商源净额/(I+U)，不改变主公式分母，不修改原值。同七来源分数相关均>0.99999；原EW指空日的决策分歧依次为300:0、500:1、1000:3、创业板:0、两源:1、500+1000:0、四源:1。**500+1000决策完全不变**，因此不能将它的主窗较弱表现归因于这组净额定义差异。该诊断包含全部大小差异，不只是23个阈值以上点；没有据此另选收益赢家。

旧WSET与新成分来源也不能当同一序列。相同2017-02-14起点和执行输入下，旧WSET 300为38.40%/1.688，新300为35.43%/1.600；旧两源37.46%/1.629，新两源34.55%/1.535。旧序列只是独立对照，没有混入新源信号。

逐金额核查补充：300共2,593个重叠日中，开盘值在万元→元换算后并非全历史都一致；此前6日小样一致不能外推到全历史。新旧总额分母之比中位数1，但最小约0.133；创业板、科创也有范围/数值差异。完整数值对账与持仓分歧见附件，不仅凭“同为300”或一个固定倍率推断相同口径，具体差异原因未在本轮确认。

## 验证与交付

共122本账：主窗18、短窗22，乘3情景，再加主口径2本独立WSET对照。全部账户损益、分腿成本和逐笔累计损益恒等式通过；9份代表账本与原逐合约引擎逐日收益完全一致。未来输入扰动不改变过去分数。新增runner通过ruff F/E9，未改核心引擎，复用既有17项测试证据。

[数据验收](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/data_verification.json) · [完整指标](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/metrics.csv) · [回测核验](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/research_verification.json) · [交易汇总](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/trade_summary.csv) · [净额诊断](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/vendor_net_sensitivity.csv) · [新旧金额对账](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/wset_vs_constituent_amounts.csv) · [分数](../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/scores.csv)。

数据请求已完成消费验收，本轮固定来源扩展也完成。没有足够证据支持将500/1000过滤或更多来源组合替换现役；也没有证明旧WSET赢家可部署。后续若探索不同机制，应另固定构造并计入既有选择历史，不能继续试方向后把赢家称独立确认。未改数据库、网关、生产信号，未调用Wind，未提交或推送。
'''
Path('docs/plans/2026-09-16-ew-constituent-flow-results.md').write_text(report)
(R/'outputs/REPORT.md').write_text(report.replace('(2026-09-16-ew-constituent-flow-extension.md)','(../../../../../docs/plans/2026-09-16-ew-constituent-flow-extension.md)').replace('../../backtest/output/runs/20260916-ew-constituent-flow-extension-r1/outputs/',''))
print('Source comparisons:',comparison)
print('Gate comparisons:',gates)
print('Latest scores:',scores.tail(1).to_string())
print('Short-window report uses cumulative returns, n=131.')
