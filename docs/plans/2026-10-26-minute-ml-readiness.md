# 分钟结构机器学习：准备完成，等待清洗交付

2026-10-26。按用户要求完成准备阶段；**没有读取真实分钟行情、训练真实模型或运行正式回测**。
当前状态为preparation_only。已知日频首轮结果不作为分钟特征的效果证据。

## 已备妥

- [研究规格](2026-10-26-minute-ml-preparation.md)：历史500/1000成员，各8列分钟结构特征；
  14列日频对照与30列扩展组，沿用Ridge/浅层树和原五日标签/实际合约执行口径。
- [特征与接入模块](../../backtest/minute_ml.py)：48格时间校验、显式历史成员、股票日质量原因、
  覆盖率分母、16列聚合、完整日历接入、两组共同回退及过去仓位参照。
- [交付模板](../../examples/minute_ml/delivery.template.json)：仍为awaiting_cleaning；
  声明检查不会访问数据文件，不把模板或合成结果判成可运行的真实数据。
- [本地需求草稿](../requests/2026-10-26-stock-minute-ml-preparation/request-draft.md)：未送办公室，
  未请求当前清洗扩大范围，未发起共享库写入或收费采集。
- [合成演练入口](../../backtest/prepare_minute_ml.py)：只接受run-id，不提供真实输入路径、
  数据库访问或训练选项。合成分数是人工占位值，不能作为预测力证据。

## 验证证据

20项纯合成行为测试通过。覆盖手算特征、平盘符号、时间标签/时区、成员生效与可知时点、
完整成员数、缺格、迟到、质量屏蔽、OHLC/量额异常、全日零量、零成交沿用价、
95%边界、缺失日不压缩、相同回退及未来分数扰动。

其中手算用例发现log收益累加的极小浮点残差会将平盘误判为上涨，已改用等价区间端点
价格比确定方向；没有根据真实收益调节阈值。未修改上一轮ML模型、训练或执行引擎。

合成run：`20261026-minute-ml-preparation-r1`，5个虚构交易日、4只SYNTH股票。
完整数据日2天，其余3天分别为缺格、迟到、质量屏蔽；都保持原日历并使两组及其仓位参照
回退同一原始方向。输出14/30列输入、16列分钟特征和逐股票原因，未计算收益。

证据：[合成manifest](../../backtest/output/runs/20261026-minute-ml-preparation-r1/manifest.json)、
[演练核验](../../backtest/output/runs/20261026-minute-ml-preparation-r1/outputs/verification.json)、
[逐股票质量原因](../../backtest/output/runs/20261026-minute-ml-preparation-r1/outputs/SYNTHETIC_stock_audit.csv)、
[分池覆盖](../../backtest/output/runs/20261026-minute-ml-preparation-r1/outputs/SYNTHETIC_coverage.csv)。

重演只可用新的run-id，不覆盖本轮证据：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 -m backtest.prepare_minute_ml --run-id NEW_SYNTHETIC_RUN_ID'
```

## 等待项与续接位置

正式研究仍需：明确清洗交付版本和未决问题；确认源分钟到48格的区间/竞价映射；
验收历史成员与可得性依据；实现对应真实导出适配；在候选收益前固定实际共同窗口。
合成测试通过不证明这些实际条件已满足，也不证明真实数据特征分布和模型效果。

收到可消费的清洗交付后，从规格末尾的“清洗完成后的续接顺序”开始。
当前没有安装轮询、调度或自动启动正式研究的任务；不会把“准备完成”解释成“清洗完成”。
工作保留在本地，未自动提交或推送。
