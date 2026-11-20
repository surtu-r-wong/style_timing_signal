# style_timing_signal：前瞻账户核算软件实施回函

2026-10-15，答复 [实施补充](forward-account-implementation.md)。已完成本地核算软件、参数模板、合成验收与任务入口；**尚未初始化真实账户、部署或启用任何生产任务**。原 [response.md](response.md) 保持原字节，SHA-256 为 `9fb392899aea78064995c4ce66364b4e85273e938b22b11254f32178f2a99e0d`。

## 1. 已完成代码与固定入口

以下代码在开发机 `/home/elfbob/claude-code/style_timing_signal/` 下，链接均相对此目录定位；本轮不重复位置盘点，也未向 Debian 复制代码或状态。

| 交付 | 位置 / 内容 |
|---|---|
| 核算核心 | [operational_returns/account.py](../../../operational_returns/account.py)：原生登记读取、显式参数校验、初始化/基准、增量账本、费用/现金/保证金、幂等、修订与原子发布 |
| CLI | [operational_returns/__main__.py](../../../operational_returns/__main__.py)，`python3 -m operational_returns` |
| 参数与使用说明 | [operational_returns/README.md](../../../operational_returns/README.md)，含现金/期货算式、时间语义、恢复与将来启用顺序 |
| 真实参数待填模板 | [account.template.json](../../../examples/operational_returns/account.template.json)，没有默认资金、资产或成本，未填完整会拒绝初始化 |
| 单日输入待填模板 | [event.template.json](../../../examples/operational_returns/event.template.json)，包括执行/估值价格、输入可用时刻、信号来源、资金流与公司行动声明 |
| 合成输入 | [synthetic.json](../../../examples/operational_returns/synthetic.json)，单独标识、合成时钟，绝非真实账户参数 |
| 合成收益与账本 | [return-feed.synthetic.json](../../../examples/operational_returns/synthetic/return-feed.synthetic.json)、[ledger.synthetic.json](../../../examples/operational_returns/synthetic/ledger.synthetic.json) |
| 验收记录 | [acceptance.json](../../../examples/operational_returns/synthetic/acceptance.json)、[行为测试](../../../tests/test_operational_returns.py) |

原生策略入口直接读取 `SIGNALS`、`PRODUCTION_MAPPING`、`REFERENCE_OUTPUTS`、`POOLS`，不是复制四条线为永久集合。只核算显式配置的账户；研究事项、5/20/0 参照和池名称不自动变成运行策略。采用既有方向映射定义；既有研究 `contract_ledger` 无完整运营现金/利息/持久状态，不把其结果包装为账户收益，而由新账本逐项记账。

同一原生键的不同映射/账户，feed `version` 使用 `策略参数版本/映射/账户ID/初始配置SHA256` 区分。经济参数初始化后不可原地修改；日历可在保留全部旧覆盖及来源的前提下显式追加，单独留痕，不改变经济账户身份。

## 2. 核算支持矩阵

| 范围 | 本版能力 / 限制 |
|---|---|
| 账户 | 单账户单标的/固定合约、单币种 `paper`；多个明确配置账户独立资金账并列导出，不合成池/组合收益 |
| 现金证券 | long-flat / symmetric（仅原生登记允许的映射）；显式允许卖空、卖空款可用、短仓现金担保比例；借款/现金利息/证券借券费计入 |
| 线性期货 | 明确合约、乘数、手数、固定保证金率；平仓结算和每日变动结算；执行与估值时检查保证金，不把名义本金当现金收付 |
| 执行 | 前一会话信号实际观测时间严格早于下一执行时点；每会话按执行前 NAV 和明确杠杆再平衡，向零取整至手数，买加/卖减滑点 |
| 费用 | 双边名义佣金、卖出税、每单位费用；明确年化现金/融资/借券费率，ACT/365 实际秒数；无自动默认零成本 |
| 净收益 | 账户 NAV 分母，无外部申赎支持范围内的 `net_nav_time_weighted`；原生名义杠杆只在目标数量应用一次，实际 gross/NAV 据实导出 |
| 时间与日历 | 显式完整自然日覆盖及休市标记；缺日或覆盖到期拒绝推进。日收益区间首尾衔接，所属日期是估值会话日 |
| 账本与恢复 | 独立本地 SQLite 事务；输入摘要幂等、失败全回滚、重复不收费；最近区间显式修订，旧输入/账及发布版本保留 |
| 尚不支持 | live 成交接入、申赎、多币种、期权、自动换月/到期处理、多个标的同账户、夜盘跨自然日会话、公司行动、T+1 可卖数量限制、最小佣金/分层融资、强平/破产及历史区间连锁修订 |

未支持行为遇到输入明确拒绝，不能把缺口填成 0 或指数代理。现金证券账使用交易日结算模型；需要交易所交收/冻结资金或券商细分利息口径的账户，还需适配器。真实可成交性、到期/停牌/涨跌停、成交价格和结算价的来源也尚未接入；JSON 中 `executable=true` 必须由输入适配器提供证据，模块不会凭价格存在就认定可成交。

## 3. 增量状态、收益与发布

CLI `init` 用真实调用时刻建立现金，无历史初始化参数；随后 `baseline` 只能取初始化后第一个估值时点，初始化到基准的现金利息并入基准 NAV，不发布不完整区间。完成再下一个估值区间后，才出现第一条日收益；`inception_date` 等于这条收益的交易日。若在收盘后初始化，次日收盘只是基准，不能冒领次日完整收益。

信号快照通过 `capture-signal` 旁路只读取得，保留本次实际观测时刻和源 CSV 摘要，不能拿生产完成时间倒签可用性。它与既有生产锁配合，拒绝运行中的半成品，以及 `OFFICE_LATE` 等不完整输入。没有执行前有效观测时，当期核算报错，不采用旧输入；不影响原有信号作业和通知。

`advance` 将现金、持仓、NAV、费用、滑点、融资、借券、盈亏及完整输入在一个事务中提交；失败保留上一成功状态，另写 `.status.json`。同输入重跑保留原记录及可用时刻。仅最近完整区间允许 `--revise`，从原期初状态重算并递增修订号，不覆盖旧账。

导出沿用本目录 [ReturnFeed Schema](return-feed.schema.json)。每个系列包含从真实起算以来全部已积累区间，不为补足月/周窗口倒填。证券 `margin_ratio=null`，期货据保证金/NAV填写；gross/NAV 不再去杠杆。生成器先归档版本再原子替换目标，无变化保留原 `generated_at`；发布失败另写 `.publish-status.json`，可从已成功账本恢复。

建议启用后账本放 `/home/elfbob/jobs/style-signals/state/operational_returns/accounts.sqlite3`，快照/行情输入在同级持久目录；真实导出目标仍为 `/home/elfbob/jobs/style-signals/state/output/multi_asset/returns/latest.json`（即原 `current/output/...` 入口）。这是部署方案，**当前未创建这些真实账户或正式收益文件**。合成输出必须使用独立 synthetic 路径，程序禁止合成导出文件名为 `latest.json`。

## 4. 验收结果与复现命令

本轮合成与相关既有回归：**123 passed in 6.61s**，范围为 `test_operational_returns.py`、`test_execution_ledger.py`、`test_bt_production.py`、`test_bt_positions.py`、`test_deploy_daily_wiring.py`；未声称跑过全仓测试。

覆盖了两个连续估值区间、买入/卖出费用、重复运行、2 倍原生杠杆、期货乘数/保证金/平仓、证券融资/借券、期货未结算浮盈不计现金利息、滑点/卖出税、迟到信号/缺行情/缺日、事务中途失败与恢复、原子发布失败重试、最近区间修订留痕、映射身份、原生登记增删适配、生产锁及实际观测时刻。实盘/期权/多币种/申赎和缺失参数均有拒绝测试。

合成现金账独立算式：

- 起始基准 1000，首期买入 10 × 100，佣金 1，按 110 估值：现金 −1，NAV = −1 + 1100 = **1099**，净收益 **0.099**。
- 第二期卖出 10 × 120，佣金 1.2：现金/NAV = −1 + 1200 − 1.2 = **1197.8**，净收益 = `1197.8 / 1099 - 1`。重复运行不再收费。
- 期货另测 2 倍杠杆：初始 1000、空头名义 2000，100→90 的收益为 200，费用 2，NAV **1198**；不是再乘一次 2。乘数和手数另有核对。

CLI 可复现（只写 `/tmp`）：

```bash
bash -c 'ulimit -v 2000000; python3 -m operational_returns registry'
bash -c 'ulimit -v 2000000; python3 -m operational_returns demo --output-dir /tmp/style-returns-acceptance/synthetic'
```

第二条已连续执行两次，账本一致、重复运行不收费、生成时间不变；保存的合成 feed 通过 `Draft202012Validator` + `FormatChecker` 对原 Schema 的验证。运行时仅标准库；验收库见 [requirements-returns-validation.txt](../../../requirements-returns-validation.txt)，本机复用系统已安装 jsonschema 的具体命令见 [README](../../../operational_returns/README.md#离线复现)，未安装或修改生产依赖。

## 5. 待落实参数

以下交 multi_asset_backtest_db 集中落实，本项目不另向用户猜配或逐项追问：

1. 启用哪些原生信号/映射、对应账户 ID 与确认后的参数版本；哪些是独立账户，是否需要本版尚不支持的多标的账户。
2. 每个账户资金、币种、允许初始化的真实时点；两池的实际资金与权重尚未给定，不预填 100 万或固定比例。
3. 可交易证券/具体期货合约、乘数、手数、名义杠杆、卖空权限及担保、保证金规则；换月/交收/T+1 限制等是否需要新增适配器。
4. 费用、税、滑点、融资利率、现金利率、借券费，以及未计项目；本版模型不覆盖的费用/计息结构不能用零值替代。
5. 权威日历来源、完整覆盖、执行/估值/结算会话和定价规则；原输入调度的 CN 工作日表或指数观测日期不能直接冒充账户交易日历。
6. 实际执行参考价、估值/结算价、可成交性和可用时刻的可核验来源；当前交付 JSON 适配接口，未接真实供应商或付费数据。

## 6. 触发方案与未部署边界

统一验收及参数落实后：`validate-config` → 显式 `init` → 首个估值 `baseline`；独立旁路在原生信号作业成功完成后、下一执行会话前 `capture-signal`；会话执行/结算输入完整后逐账户 `advance`，再 `export`。状态/发布失败按本地状态文件检查并重试，不重启原生产链来伪造完成。

依赖包括已确认账户配置、完整日历、执行前真实信号快照、对应执行/结算行情及来源证据。预计完成条件是相应估值/结算输入到齐并成功核算，尚不能承诺固定钟点；既有 20:30 触发、21:30 等数截止或 T+1 目标都不是收益 SLA。本轮交付命令和调度方案，未安装/启用 timer、未修改既有生产作业、未实际推送或下单。

没有共享库新表/DDL，没有历史回填或研究重算，没有付费补数，没有正式空壳 feed。分类、比例、周/月汇总及新增收益通知仍由 multi_asset_backtest_db 负责；原持仓/合约/杠杆复杂契约继续有效。
