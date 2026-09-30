# 现役信号诊断与事件依赖研究收尾（2026-09-15）

用户指令：“先收尾，后续再研究引入其他维度优化现役信号”。本轮到此结束，不再新增实验；下一轮待用户继续时确定新维度。

## 保留结论

- 现货池slope20多头/空仓、期货池equal_weight多空对称的既有分工及参数，本轮未修改。没有足够证据支持替换现役，也没有证明现役已最优。
- 入场T日涨跌分组、交易胜率基线及equal_weight全部多空明细已保存。逐笔表为无费现货代理；事件归因为日频含3bp成本/carry代理，不能直接拼接比较。
- 事件主口径25个夏普反转规格中，24个放回同多头基准仍落后；没有规格同时实现夏普和累计收益由落后转领先。
- EW20/250/0约81.4%的相对对数收益落后集中于七个10日窗口，其中首日占全部落后的22.7%，后9日占58.6%；不能都解释为“没猜中消息”。
- 同年随机移除七段10日行情，2000次中该规格52.8%夏普反转；次日收盘执行为0/2000。随机日历诊断不是显著性检验。
- EW20/250/0与原四对权重(2/7,1/7,2/7,2/7)只作为敏感性观察项，不升格为替换候选。事件依赖用于诊断和重审优先级，不作为越低越好的单一优化目标。

## 归档入口

| 范围 | 报告与明细 |
|---|---|
| 买入T日分组 | [报告](../../backtest/output/entry_day_direction_20260915/REPORT.md) |
| 做空T日分组 | [报告](../../backtest/output/short_entry_day_direction_20260915/REPORT.md) |
| 现役胜率基线 | [报告](../../backtest/output/incumbent_winrate_baseline_20260915/REPORT.md) |
| equal_weight做多明细 | [报告](../../backtest/output/equal_weight_long_trades_20260915/REPORT.md) · [Excel](../../backtest/output/equal_weight_long_trades_20260915/equal_weight_全部做多明细.xlsx) |
| equal_weight做空明细 | [报告](../../backtest/output/equal_weight_short_trades_20260915/REPORT.md) · [Excel](../../backtest/output/equal_weight_short_trades_20260915/equal_weight_全部做空明细.xlsx) |
| 事件第一轮：覆盖与反转 | [报告](../../backtest/output/runs/20260915-event-dependence-r1/outputs/REPORT.md) · [Excel](../../backtest/output/runs/20260915-event-dependence-r1/outputs/被否决信号_事件依赖审计.xlsx) |
| 事件第二轮：同映射、仓位响应、随机日历 | [报告](../../backtest/output/runs/20260915-event-dependence-r2/outputs/REPORT.md) · [Excel](../../backtest/output/runs/20260915-event-dependence-r2/outputs/事件依赖第二轮_候选复核.xlsx) |

数据截至2026-09-11；各候选原生区间、未平仓交易处理见对应报告。

## 覆盖缺口与解释边界

事件审计使用当时冻结的63项台账，其中38项否决类信号/研究记录；30项有回放或关联证据，部分只覆盖代表或子网格。本次新增登记不回写冻结台账，也不改变原分母。

8项未数值回放：面2横截面、双通道、尾部第五桶、等比五桶、新轴第一批、质量轴第二批、B3连续风格状态、基差期限横截面复制。原因包括持仓缺口、不同执行工具、股票级重建及原研究为结构/IC检验。见[覆盖清单](../../backtest/output/runs/20260915-event-dependence-r1/outputs/registry_coverage.csv)，不能称已排除这些研究的事件影响。

七事件不是全部宏观冲击；收益置零保留原信号路径，不是事件未发生的反事实。2024后历史已复用，不是新的样本外证据。原否决门槛与本次描述性归因分别保留，未重写裁决。

## 验证与工作区

- 两轮run均完成，冻结输入、代码、结果和manifest保留，收尾未修改。
- 第一轮2748组引擎比较、10份历史账本及归因恒等式核对通过，见[记录](../../backtest/output/runs/20260915-event-dependence-r1/outputs/verification.json)。
- 第二轮364条事件分解、2000套随机窗口、18组直接复算及多空腿恒等式核对通过，见[记录](../../backtest/output/runs/20260915-event-dependence-r2/outputs/verification.json)。
- 收尾复用既有验证；新交接页、台账、README索引核对链接及一致性，不重跑模拟或无关测试。
- 本次为本地文件归档，未执行Git提交或推送。未来提交时应一并包含本页链接的研究产物，不把未提交文件误称已进入Git历史。
- 本次未安装后台任务或启动新采集；已有前瞻约定的历史说明见[09-14收尾](2026-09-14-research-closeout.md)，不能据此假定自动运行。

## 下次研究入口

目标是引入其他维度优化现役信号。下次先明确新维度的机制、决策时数据是否可获得，以及比较对象是equal_weight期货池、slope20现货池还是两池增量，再确定固定规格和评价口径。

可复用本轮胜率、逐笔风险、同映射收益和事件依赖诊断作为基线。收益、回撤、执行与样本稳定性共同评价，不只追求胜率或事后事件中性夏普。具体维度和数据源留待下一轮，本次不自动启动。


## 2026-09-16续接

用户已指定equal_weight期货池、多空可独立，研究已继续，见[当前研究入口](2026-09-16-equal-weight-independent-legs.md)。[最新结果](2026-09-16-ew-flow-source-results.md)包含开尾盘空头过滤的来源拆分、其他指数WSS可用性及跨接口单位/范围差异；历史研究仍仅作描述性证据，生产未改。本页原收尾结论与09-15不可变run保持原状态。


2026-09-16续接补记：用户授权三方向推进后，[订单结构、空头进退与仓位稳定性](2026-09-16-ew-flow-structure-results.md)已完成。主来源订单结构未改善；退出机制保留为描述性候选，最好5日置零后同暴露Sharpe优势消失，现役不改。办公室已确认WSS/WSD关键日一致，源端异常机理仍待Wind解释。下一入口仍为[多空独立研究](2026-09-16-equal-weight-independent-legs.md)。


2026-09-16再次续接：[退出机制跨源与贡献审计](2026-09-16-ew-exit-mechanism-results.md)已完成。源分歧明显下降，但对同暴露daily增量不稳；新源只控制退出的改善约94%来自段首额外持仓。上一阶段保留退出机制的结论已收紧为稳定性发现，未确认收益增益，现役不变。
