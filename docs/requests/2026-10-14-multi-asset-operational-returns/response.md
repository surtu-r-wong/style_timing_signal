# style_timing_signal：原生登记、Debian 落点与收益可用性回函

2026-10-15。答复同目录 [原请求](request.md)、[职责补充](coordination-addendum.md) 和 [Debian 落点补充](debian-location-addendum.md)。

**目前可交付原生信号清单、推荐仓位和生产状态，尚无可持续核算的账户单日净收益，也没有合格的 ReturnFeed 文件。** 四条生产线、两池映射和研究产物均不能直接转换成 `net_nav_time_weighted`。本次先登记实际现状，不补零、不生成空壳收益文件，不启动历史回填或研究。

## 1. 主机、稳定路径与原生登记

生产主机为 `elfbob@100.65.111.79`（Debian）。本次只读核对始于 `2026-10-15T13:26:32+08:00`（主机返回时间）：

| 用途 | 主机上的绝对路径 / 当前状态 |
|---|---|
| 稳定运行入口 | `/home/elfbob/jobs/style-signals/current` |
| 本次实际 release | `/home/elfbob/jobs/style-signals/releases/2d22e57` |
| 原生生产信号清单 | `/home/elfbob/jobs/style-signals/current/backtest/baseline.py` 的 `SIGNALS`，四条线的 ID、信号文件与字段正本 |
| 原生映射、参照与两池登记 | `/home/elfbob/jobs/style-signals/current/backtest/production.py` 的 `PRODUCTION_MAPPING`、`RECOMMENDED_FILES`、`REFERENCE_OUTPUTS`、`POOLS`、`POOL_FILES` |
| 完整研究登记 | `/home/elfbob/jobs/style-signals/current/docs/plans/research_registry.yaml`；保留全部原生 `id`、`status`、`production`、`documents` 等字段 |
| 生产产物稳定目录 | `/home/elfbob/jobs/style-signals/state/output/`；`current/output` 已核实软链指向此处 |
| 最近作业状态 | `/home/elfbob/jobs/style-signals/state/logs/daily_signals_status.json`；`current/logs` 已核实软链指向同级 `state/logs` |
| 约定收益入口 | `/home/elfbob/jobs/style-signals/current/output/multi_asset/returns/latest.json`，**尚不存在**；对应持久路径 `/home/elfbob/jobs/style-signals/state/output/multi_asset/returns/latest.json` |

目前没有独立的账户策略注册表。生产信号原生键为 `hybrid20`、`citic40d`、`equal_weight`、`slope20`；研究注册 ID 是研究事项身份，不能把每项都当成运行中的账户策略。接入方可完整读取上述原生入口，无需本项目再维护专用策略名单。

研究表中 `production-equal-weight-symmetric`、`production-slope20-fourth-line` 是相关现役采用记录；`equal-weight-5d20z` 为 `research_only`。其他事项的状态和证据以整份研究表为准，不能凭目录里有文件就启用为收益系列。

本次 release 标识为 `2d22e57`，可用于追溯此次部署；目前没有单独发布的账户收益 `version`，不以 release 名冒充已验收收益版本。下列三个原生登记文件已与开发机同名文件逐一比对 SHA-256，一致：

| 文件 | SHA-256 |
|---|---|
| `backtest/baseline.py` | `e76b8b8ef8ff89cf1705eaa7aefd84f0d6a7ea3e0eaa9d4e395028b8a0bb987d` |
| `backtest/production.py` | `39828284f3fd8bb7b5baa3d023c020f388b2f99d62feccb8befa67e94999cc25` |
| `docs/plans/research_registry.yaml` | `c5959d49c976700bdd350f99f9f796054239a69d3a1314f458b38586b3fcb56e` |

开发机清单正本在 `/home/elfbob/claude-code/style_timing_signal/` 下的同名相对路径。开发机 `output/` 是从 Debian 每小时拉回的镜像；`logs/` 不随之拉回，本地旧日志不能作为生产状态证据。部署布局见 [日更说明](../../../deploy/daily_signals/README.md)。

## 2. 全部生产线与收益缺口

下表文件均位于 Debian 的 **`/home/elfbob/jobs/style-signals/state/output/`** 下，目录前缀加表内相对路径即为绝对路径。四条线均已进入例行信号日更；收益可用性全部为“未具备”，不是收益为零。

| 原生 ID | 参数 / 生产映射 | 信号文件与字段 | 推荐仓位文件 |
|---|---|---|---|
| `hybrid20` | 回看 20、z 窗口 250、状态机；金融/稳定确认；long-flat | `hybrid20/confirmed_signal.csv`：`hybrid_20` | `recommended/hybrid20_longflat.csv` |
| `citic40d` | 回看 20、z 窗口 40、五因子等权、无平滑；long-flat | `citic40d/citic_style_signal_40d.csv`：`factor_20` | `recommended/citic40d_longflat.csv` |
| `equal_weight` | 四对、20/40/5；symmetric；期货池采用 | `equal_weight/equal_weight_signal_20d40z.csv`：`factor_value` | `recommended/equal_weight_symmetric.csv` |
| `slope20` | 四对、对数价格斜率 L20、z 窗口 120、无平滑；symmetric | `slope20/slope20_signal_L20zw120.csv`：`factor_value` | `recommended/slope20_symmetric.csv` |

另外持续产出：

- 现货池采用 `recommended/slope20_longflat.csv`；期货池与现货池是既有信号映射，尚非两套完整账户账本。
- `recommended/equal_weight_longflat.csv` 是参照口径。
- `equal_weight/equal_weight_signal_5d20z.csv` 是 5/20/0 的研究参照信号，不在 `SIGNALS` 的四条生产键中。
- `hybrid20/growth_stability_signal.csv` 是上游中间信号；confirmed 文件中的 60d 列不是额外登记的账户策略。
- `recommended/combined_signals.csv` 是四条线信号与推荐仓位的合并快照，**不是组合收益账本**；其中 `slope20_position` 来自 symmetric 文件，不能误当现货池 long-flat 仓位。

本次读取的上述日更 CSV 末日均为 **2026-10-13**。推荐仓位表头只有 `date,position`，信号与合并表也没有 NAV、账户现金流、费用或净收益列。`position` 的 −1/0/+1 表示方向，不是总名义/NAV 或保证金/NAV。

四条线及两池共同缺少可对账的实际/纸面账户期初 NAV、资金流调整、成交与持仓估值、费用及杠杆台账；真实现货标的、实际两池权重、券商成交与保证金约束尚未接入。因此既不能提供 live 净收益，也不能把自动生成推荐仓位称为已完成 paper 净值日更。

已有研究证据可按原生研究表的 `documents.evidence` 找到（开发机根目录为 `/home/elfbob/claude-code/style_timing_signal/`）；历史指数代理、carry、模型执行与回测 PnL 均维持研究身份，不转成此次运营收益。相关边界见 [阶段收尾](../../plans/2026-09-14-research-closeout.md)。Debian 的两个前瞻目录 `current/backtest/output/forward/20260914-incumbents-v1/` 与 `current/backtest/output/forward/20260914-incumbents-market-v1/` 有 campaign/market 元数据，但本次检查均无 `records` 目录，不能视为已积累真实日收益。`output/hybrid20/optimized_signal.csv` 仍停在 2026-03-25，也不属于现行日更收益。

## 3. 更新方式、日期与日历

现有系统级 `style-signals-daily.timer` 工作日 20:30（Asia/Shanghai）触发，输入等候最多到 21:30；输入到齐后计算信号、推荐仓位、合并快照、检查新鲜度并按现行方式推送。生成器全量重算覆盖 CSV；这些信号文件没有 ReturnFeed 所要求的收益发布/修订窗口保留机制。**目前不能承诺账户收益完成时刻，20:30、20:45、21:30 均不是净值交付 SLA。**

本次只读快照：

| 字段 | 值 |
|---|---|
| timer | `ActiveState=active` |
| 最近触发 / 作业开始 | `2026-10-14T20:30:31+08:00` |
| 作业结束 | `2026-10-14T21:30:03+08:00` |
| service | `Result=success`，`ExecMainStatus=0` |
| 状态 JSON | `result=OK`，`topup=OFFICE_LATE` |
| 产物末日 | `2026-10-13` |
| 相对库内观测日历的落后 / 缺口 | `max_lag_trading_days=0`，`output_gap_total=0` |

这说明最近一次链路成功结束，但等候截止仍未取得当日全部输入；`OK` 和相对库内日历的零落后不证明当日信号或净值已齐。此次只登记现状，不变更排期或补跑。

日期口径如下：

- 信号和推荐仓位的 `date` 是输入行情对应的信号日，映射代码保留原日期；它不是已实现收益的 `trade_date`，也不是成交日承诺。不能将最新仓位直接乘当天价格涨跌来补收益。
- 运行时区为 `Asia/Shanghai`（UTC+08:00）。当前未发布任何收益行，所以收益所属交易日、`period_start_at`、`period_end_at`、`available_at` 尚无可交付证据，不能从文件 mtime 或作业完成时刻补造历史值。
- 等数程序原生日历为 `data_manager.business_calendar`，`calendar_id='CN'`；日历缺项时会按工作日推断并标明。这是现有输入调度口径，**本次未核验其完整覆盖范围，不能直接认证为 ReturnFeed 收益日历**。
- 新鲜度护栏使用 `stock_selector.index_daily` 的观测交易日，不是完整交易日历。不得据此把缺数据日当休市。原函提出的 `public.trading_calendar` 市场字段尚未完成与收益账户会话的映射与覆盖核验；收益 feed 的 `calendar_id` 待具备收益条件时确定。

费用与杠杆口径同样未具备：没有可声明已扣费用/未计项的账户 `fee_model`，没有可靠 `unit_nav`、`gross_ratio`、`margin_ratio`。研究中的费用假设不替代账户费用；将来发布时缺失的敞口比例按契约写 `null`，不写 0，不再去杠杆或归一化。

## 4. 可读取内容与后续接入边界

当前可直接在 Debian 读取第 1 节原生登记、状态 JSON 和第 2 节 CSV；已有 SSH 访问即可，例如只读查看：

```bash
ssh -o BatchMode=yes elfbob@100.65.111.79 \
  'cat /home/elfbob/jobs/style-signals/current/backtest/production.py'
ssh -o BatchMode=yes elfbob@100.65.111.79 \
  'head -n 3 /home/elfbob/jobs/style-signals/state/output/recommended/combined_signals.csv'
```

本次核验采用只读 SSH、指定 systemd 状态字段、CSV 流式读取表头和首末日、登记文件 SHA-256 比对；未运行策略、连接数据库或触发通知。此时**没有现成合格收益文件可交验**，故未制作真实收益样本，也未将 [合成示例](return-feed.example.json) 当作输入；没有实际 ReturnFeed 可做 [Schema](return-feed.schema.json) 验收。

约定固定收益入口继续保留，尚未部署导出器或持续同步。接入方自行维护 `/home/elfbob/jobs/multi-asset/state/inputs/style_timing_signal/` 的副本与来源校验；本项目无需向该目录写入。具备账户收益条件后，再按原契约发布完整窗口、真实生成/可用时刻、原子替换和失败保留状态，先交文件验收再安排生产启用。

本次仅补此回函；未新增共享表或 DDL、未回填历史、未申请付费补数、未修改生产推送或调度。原持仓/合约/杠杆契约继续有效；分类、组合配置、周/月复利汇总及新增收益通知仍由 multi_asset_backtest_db 负责。
