# 当前输入适配与来源验收回函

2026-10-15，答复 [current-input-adapter.md](current-input-adapter.md)。已补只读输入适配、无默认值模板、来源/时间映射、合成行为测试和当前结构验收样本。**当前真实输入仍阻塞，没有生成真实可执行 event 或初始化/推进账户。** Debian 的最新完成状态为 OFFICE_LATE；实际账户标的与完整会话、可成交、到期等证据未提供。本次不进行收益研究。

## 接口与验证

| 交付 | 内容 |
|---|---|
| [input_adapter.py](../../../operational_returns/input_adapter.py) | `python3 -m operational_returns.input_adapter`；inspect、capture、catalog、market、prepare 五个显式入口 |
| [INPUT_ADAPTER.md](../../../operational_returns/INPUT_ADAPTER.md) | 参数、字段/来源/时间映射、失败码、外部证明责任、暂存规则及安全复现命令 |
| [适配模板](../../../examples/operational_returns/input-adapter.template.json) / [证据模板](../../../examples/operational_returns/input-evidence.template.json) | 无默认标的/资本/比例；完整性和可成交性字段均留 null，不由适配器填 true |
| [test_input_adapter.py](../../../tests/test_input_adapter.py) | 合成事件衔接已有 advance；锁、迟到/过期信号、缺日/缺价/重复价/列头、来源变更/混批、外部证据不足、到期/换月、只读事务与错误脱敏 |
| [只读远端观察脚本](../../../tools/observe_operational_source.py) | 可经 SSH stdin 在既有生产锁内读取状态/结构/摘要，无远端文件写入或部署 |

账户代码仅提取原有 `validate_calendar` 为共享验证函数，日历规则与 `extend-calendar` 行为保持原样。信号 capture 复用已有生产锁、完成状态判断、真实观测时钟；prepare 输出原七字段 signal 和既有单日 event，无账户经济参数推断、无 ReturnFeed 契约更改。资金和组合权重不是开发接口的前置条件，但唯一标的、原生映射及来源配置缺失会阻塞事件生成。

本轮最终检查：`tests/test_input_adapter.py` + `tests/test_operational_returns.py` **68 passed in 1.39s**。测试包含生成的合成 event 直接进入旧账户核算，NAV=1099；未运行全仓测试。真实 CLI 对未填模板返回码 1，`reason=SYMBOL_REQUIRED`，暂存目录仅有 report.json，见 [模板阻塞样本](input-adapter-acceptance/template-blocked.json)。其测试执行和复现均限制虚拟内存 2 GB。

## 真实来源接入与本次证据

样本是结构/来源验收，全部 `event_ready=false`，不是收益、完整行情样本或真实账户参数。

| 来源 | 本次只读事实 | 可用性判断 |
|---|---|---|
| Debian 生产 | 主机 `debian-server`，`/home/elfbob/jobs/style-signals/current` 解析到 `releases/2d22e57`；2026-10-15 18:43:14 +08:00 取得；状态 `OK / OFFICE_LATE`，作业 10-14 20:30:31 开始、21:30:03 结束；推荐 CSV 最后数据日仍为 10-13 | 不得将作业结束时间或这些旧 CSV 变成 10-14/10-15 的有效信号；未生成信号快照 |
| 开发机现有产物 | `elfbob-ThinkPad-X1-Carbon-Gen-8`，本仓库；10-15 18:41:29 +08:00 取得；状态仍为 09-29 `OFFICE_CHECK_ERROR`，状态登记日期 09-29，推荐 CSV 已到 10-13 | 跨文件日期不一致，不具备生产来源完整性 |
| Market Monitor 既有库 | 使用已有 `signals.common.config.load_db_config` 连接配置；READ ONLY / REPEATABLE READ 元数据查询事务时间 10-15 18:41:29.652960 +08:00，读取完成 18:41:29.803142 | 三张既有表结构存在；本次没有配置真实标的，故未选标的或查询其行情覆盖，不声称价格/状态记录齐全 |

完整字段、路径、摘要和实际取得时刻见 [Debian 样本](input-adapter-acceptance/debian-source.json)、[本机样本](input-adapter-acceptance/local-source.json)、[只读库结构样本](input-adapter-acceptance/database-catalog.json)。数据库样本记录逻辑项目/表与既有连接配置函数；不导出连接环境或地址值，不另声称已核实物理数据库主机。

已连接的读取能力与尚缺部分：

| 来源及字段 | 能支持的时点/用途 | 尚缺证据 |
|---|---|---|
| 原生 recommended CSV `date,position` + daily_signals_status + `.daily_signals.lock` | 完整生产成功后、执行前实际观察；只适配显式配置的原生键/允许映射 | 当前生产为 OFFICE_LATE；还没有合格的执行前观测快照 |
| `public.futures_daily`：`symbol,trade_date,open,close,settle,volume,oi` | 明确固定合约的开盘/收盘/每日结算候选 | 未配置合约；指定日非空值覆盖未查；没有时点可成交/涨跌停、完整会话、权威到期/换月证据；close 不自动替代 settle |
| `stock_selector.index_daily`：`index_code,trade_date,open,close` | 指数日线参考时点 | 指数不是已确认的可交易证券，不能自动代替 ETF 或期货；无真实执行价格接入结论 |
| `stock_selector.stock_status`：`ts_code,trade_date,is_suspended,limit_up,limit_down,updated_at` | 同日状态候选；已有表可按明确证券/日读取 | 稀疏或空记录不能推断不停牌；日度标志不能证明某时点双向成交；缺价格、公司行动与流量完整性声明 |
| 日历对象 / 既有 extend-calendar | 完整自然日覆盖、休市与执行/估值会话校验及追加 | 尚无已确认标的对应的权威完整日历；指数出现日期、普通工作日表不代替交易会话 |

表结构中期货 `created_at/updated_at` 为无时区时间，指数/股票状态 `updated_at` 为带时区时间。这些是入库/更新列，不能自动作为行情原始发布或首次实际取得时间；本次仅核对列类型，没有读取对应标的的时间值。数据库事务时间只证明这次查询，不证明数据曾在历史执行前可得。

## 时间和完整性约束

1. 信号 `available_at` 来自实际 capture 调用，不回推为生产 `finished_at`。必须属于前一开市会话且满足“前次估值 ≤ 信号观测 < 本次执行”；旧产物、OFFICE_LATE 与迟到观测不能补成有效前瞻信号。
2. 定价 CSV 显式提供数据日、标的、执行/估值价及各会话时刻、来源可用时刻；后者不早于估值。外部证据另提供 checked_at；event.available_at 为适配实际取得时刻。报告保留各来源实际读取时刻与摘要。
3. 外部责任方提供完整日历/会话、执行可行性、资金流和公司行动完整性证明，并绑定 signal/calendar/prices 的精确文件摘要。证据缺失/false/字段不足即阻塞；hash 绑定检测已稳定但混批的文件，前后重读检测读取中的变化。声明 true 必须有 authority/reference 支撑；本模块检查证据绑定及格式，不替责任方证明市场事实，不提供数字签名认证。
4. 固定期货合约要求明确的 expires_at、fixed_contract=true、requires_roll=false，以及 settlement 定价口径；触及到期或需换月即阻塞，不按持仓量/收益选主力或自动续约。
5. 每次输出独立新暂存目录，失败没有 event；`candidate_only/inspection_only/signal_observed` 即使退出码为 0，也不表示可执行。仅 `ready + event_ready=true` 表示事件已通过输入检查，仍须已有账户的经济配置/日历一致性验证。

## 复现与持续运行限制

在本仓库根目录运行（目录名每次换新）：

```bash
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter inspect --staging /tmp/input-staging/local-check-01'
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter catalog --staging /tmp/input-staging/catalog-check-01'
# 预期返回 1，不生成 event：
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter prepare examples/operational_returns/input-adapter.template.json --staging /tmp/input-staging/template-check-01'
```

远端结构复现、合成测试与五个入口的参数详见 [接口说明](../../../operational_returns/INPUT_ADAPTER.md#安全复现)。运行源文件在开发仓库；未复制到 Debian，远端仅执行 stdin 只读检查。持续运行仍需来源方确认标的、日历、会话定价、证据责任及获取时机，并在执行前持久捕获信号；缺输入就阻塞，无计时器、自动同步或收益完成 SLA。

交 multi_asset_backtest_db 集中落实的账户参数仍为：启用账户/策略版本与映射、唯一证券/固定合约、资金/币种/初始时点、最终组合比例、乘数/手数/杠杆/卖空和保证金规则、费用税滑点及融资/现金/借券利率、执行/结算规则和换月安排。本次没有填默认值，也不把这些经济参数作为接口开发阻塞。

没有共享库写入/DDL、回填、付费取数、生产作业修改、真实账户 advance、正式 latest、timer、下单或企业微信发送；现有生产产物改动保留。原回函保持原字节，验证 SHA-256：

- `response.md`：`9fb392899aea78064995c4ce66364b4e85273e938b22b11254f32178f2a99e0d`
- `implementation-response.md`：`af519f26fc4c154341adaca035663fb80159fc89d05b8e2ffe700afafd891f37`
