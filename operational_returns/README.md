# 前瞻纸面账户核算

本模块从真实初始化后的完整估值区间开始记账。运行时仅使用 Python 标准库，支持 Linux（信号观测锁使用 `fcntl`）；不连接数据库服务或行情供应商，不下单、不推送。已有生产 runner 不调用本模块。

## 参数与输入

- [待填账户模板](../examples/operational_returns/account.template.json)：`null` 是待落实项，模板不能初始化。所有字段都必须显式填写，不提供默认资金、资产、杠杆或零费率。
- [待填单日输入模板](../examples/operational_returns/event.template.json)：需经过来源核验的可执行价格、估值/结算价格、可用时刻及已保存的信号观测。
- [合成输入](../examples/operational_returns/synthetic.json)：**仅用于验收**，1000 单位资金、价格、费率和日期均为人为算例，不是生产建议。

账户必填项：

| 配置 | 含义 |
|---|---|
| `account_id` | 独立资金账身份；合成账户必须以 `SYNTHETIC-` 开头 |
| `synthetic` / `mode` | 合成标志 / 当前只支持 `paper`，不支持实盘成交记账 |
| `strategy_id` / `strategy_version` / `mapping` | 原生信号键、已核对的策略参数版本、`symmetric` 或 `longflat`；只允许原生生产/参照/两池中实际登记的映射 |
| `initial_cash` / `currency` | 真实初始化时点的正现金资金与唯一币种；起始无仓位，无外部申赎 |
| `instrument` | 固定单标的/具体合约、币种、乘数、最小交易数量；不接受“主连”自动换月语义 |
| `instrument.kind` / `settlement_rule` | `cash_security` + `cash_trade_date`：按交易日收付现金；`linear_future` + `daily_variation_margin`：平仓盈亏成交时结算，未平仓盈亏估值时逐日结算 |
| `margin_rate` | 现金证券必须 `null`；期货必须显式给 `(0,1]` 固定比例，不代表任何市场当前实际保证金率 |
| `execution` | 只支持 `next_session_fixed_time`、`each_session`；显式名义杠杆、单边滑点 bp、是否允许卖空；证券卖空款可用（`short_proceeds=available`），另设短仓现金担保比例 |
| `fees` | 双边成交名义佣金 bp、卖出税 bp、每单位费用，以及已扣/未计项说明；0 也需明确确认，不能省略 |
| `cash_rules` | 年化现金利率、借款利率、证券借券费率，按 ACT/365 实际秒数计单利；外部资金流只支持 `reject`；期货借券费率必须显式为 0 |
| `calendar` | ID、时区、可追溯来源、覆盖首末日；`days` 必须逐自然日完整列出，休市日显式 `is_open=false`，开市日给执行与估值时间，均带时区 |

现金证券利率是账户现金余额的净有效利率；担保现金不另拆收益子账户。期货利率是已结算现金（包含保证金现金）的净有效利率；不对未结算浮盈计息。无法用这个模型表达的分层计息/保证金规则须新增适配器，不能填一个猜测值。

原生清单通过 AST 读取 `backtest/baseline.py:SIGNALS` 和 `backtest/production.py` 的登记常量，不导入研究模块、不执行策略。不把四条线写死；新增登记只有在明确配置账户后才核算。5/20/0 研究参照及两池名称不会自动生成账户。原生声明将来若改成非字面量，适配器明确失败并要求更新。

每个账户保存完整配置，feed 身份为原生键 + `策略版本/映射/账户ID/初始配置SHA256`。不同账户或映射不会混成同一收益系列；已经初始化的经济参数不可原地更改。`extend-calendar` 仅允许在相同来源/ID/时区下追加完整日历尾部，并保存扩展记录，不改变账户身份。已核算历史日历修正不在本版支持范围。

## 核算与时间约束

1. `init` 使用调用时真实时钟初始化现金，CLI 无历史时间覆盖参数。`baseline` 只能取初始化后第一个估值时点，初始至该时点的现金利息并入基准 NAV，不发布残缺首日收益。
2. 每次 `advance` 只接受基准/上次成功估值后的下一个开市会话。日历缺日、到期、行情缺失或不可执行都报错；不以缺行情推断休市，也不自动延用旧信号。
3. 信号必须属于前一个估值交易日，`available_at` 不早于该日估值、严格早于本次执行。当前信号日不乘同日涨跌。行情可晚到，但计算所用信号必须是执行前实际观测并保存的证据。
4. 执行前 NAV = 现金账及旧持仓按执行参考价估值。目标数量 = 方向 × 向零取整至手数的（执行前 NAV × 名义杠杆 ÷ 参考价 ÷ 乘数）。杠杆只在这里应用一次，每个会话按此规则再平衡；成交价按买加/卖减滑点，按实际数量变化扣佣金、税和每单位费用。
5. 证券现金按实际成交收付，期末 NAV = 现金 + 数量 × 估值价 × 乘数；期货现金按平仓及逐日变动结算，期末 NAV = 已结算现金。利息/融资和证券借券成本按执行前、后两个时间段计入。期货未平仓浮盈不能提前增加可计息现金。
6. `净收益 = 期末NAV / 前次NAV - 1`；无外部申赎，故满足该支持范围内的时间加权净值收益。`unit_nav = NAV / 基准NAV`；`gross_ratio = 绝对名义 / NAV`，期货 `margin_ratio = 保证金 / NAV`，证券该项为 `null`。费用可使实际名义/NAV略偏离目标，导出实际值，不二次缩放。

执行时和估值时均检查期货保证金、证券空头担保及正 NAV；不足时拒绝提交，不模拟自动追保、强平或破产。这类失败必须处理参数/适配器缺口，不能用旧 NAV 充作当天收益。

`event.execution_price` 是配置执行会话的可成交参考价；`valuation_price` 对现金证券为估值价，对期货为结算价。`source` 和 `available_at` 必须由行情适配器据实提供。当前交付的是 JSON 接口，**尚未接入真实价格、可成交性、到期/停牌/涨跌停及结算来源**；`executable=true` 是经上游验证的事实要求，不是模块替上游完成验证。

## 本地持久化和恢复

SQLite 文件由 `--db` 显式指定，应放独立持久目录。数据库中的 `accounts` 保存配置与当前状态，`records` 保存每次完整输入、输入摘要、前后账户状态、分项账、真实核算时刻和修订号，`calendar_extensions` 保存日历追加。事务使用 `BEGIN IMMEDIATE` 与 FULL 同步；一次区间要么全部提交，要么全部回滚。这里只操作项目自己的 SQLite，绝不创建共享 PostgreSQL 表。

- 完全相同的已提交输入重跑返回原记录，保留原可用时刻，不再收费或入账。
- 同日不同输入必须显式 `--revise`，仅允许修订最近完整区间；从其原期初状态重算，原修订保留。更早区间需要连锁重算，本版明确拒绝。
- 核算失败保留上次成功账和时间，另写同名 `*.status.json`；发布状态另写 `*.publish-status.json`。失败重试无需删除状态或重新初始化。
- `export` 导出每个账户全部已积累前瞻收益；未满一个完整区间的已配置账户会阻止发布，不生成空壳 feed。所有账户须同为合成或同为真实纸面账户。
- 先保存 `revisions/<内容摘要>.json`，再同目录临时文件 + fsync + 原子替换输出。无变化时连 `generated_at` 都不改；发布失败可从已提交账本重试。旧版本不删除；不同来源或账户身份被移除时拒绝覆盖。
- 合成导出必须 `--synthetic`，路径需含 `synthetic` 且文件名不能为 `latest.json`。所有合成系列标签也有 `SYNTHETIC`。

信号快照是旁路只读观察：`capture-signal` 在生产 `.daily_signals.lock` 上取共享非阻塞锁，拒绝在生产写入中读取；核对原生映射、成功状态、输入完整状态及日期后保存 CSV SHA256 和**本次观测时刻**。`OFFICE_LATE` 不产生快照。已有相同快照保留最初时刻，不回推成生产完成时刻；内容改变必须用新的观测文件路径。输入端必须保留这些快照，不能以后从全量重写的 CSV 重建历史可用性。

## 离线复现

在仓根执行，以下只写 `/tmp` 合成目录：

```bash
bash -c 'ulimit -v 2000000; python3 -m operational_returns registry'
bash -c 'ulimit -v 2000000; python3 -m operational_returns demo --output-dir /tmp/style-returns-acceptance/synthetic'
# 同命令重跑，重复事件不收费，feed generated_at 不变。
bash -c 'ulimit -v 2000000; python3 -m operational_returns demo --output-dir /tmp/style-returns-acceptance/synthetic'
```

算例两期：首期买入 10 单位，执行价 100、估值价 110、佣金 1，现金 −1、NAV 1099、收益 0.099；第二期在 120 卖出，佣金 1.2，现金/NAV 1197.8，收益 `1197.8/1099-1`。第二期末现金不再因同一输入重跑而变化。收益区间首尾均为相邻估值日 15:00 +08:00，第一条日收益日期为 2026-10-13；这些都是合成时钟，不是真实初始化证据。

测试所需 `pytest` 沿用开发依赖；Schema 验收另需 [requirements-returns-validation.txt](../requirements-returns-validation.txt)。通常运行：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 -m pytest tests/test_operational_returns.py -q'
```

本机 Conda Python 未安装 jsonschema，但系统 Python 已有 4.10.3。本次验证将系统包路径追加在现有依赖之后（不修改安装、不覆盖现有包）：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 -' <<'PY'
import sys
sys.path.append('/usr/lib/python3/dist-packages')
import pytest
raise SystemExit(pytest.main(['tests/test_operational_returns.py', '-q']))
PY
```

## 尚未执行的真实启用顺序

统一验收参数和真实输入适配器后才执行：`validate-config` → `init` → 首个估值时点 `baseline` → 每晚在生产完成后、下一执行会话前 `capture-signal` → 取得次一会话执行/结算价格后 `advance` → `export`。命令详见 `python3 -m operational_returns --help` 及各子命令 `--help`。

建议未来账本 `/home/elfbob/jobs/style-signals/state/operational_returns/accounts.sqlite3`，观测/行情输入放同级独立目录，真实输出仍为 `/home/elfbob/jobs/style-signals/state/output/multi_asset/returns/latest.json`。使用相同 SQLite 的核算和发布串行化；发布目标必须由单一账本实例负责，不允许多个数据库争写同一 latest。

触发方案是独立旁路任务，既有 20:30 作业不改。信号观察需在其完成后、配置执行时点之前完成；收益核算只能在相应估值/结算价格及可成交性证据到齐后执行。失败记录状态并等待有效输入，不补零。不设目前无法承诺的 20:45 或 T+1 固定收益 SLA。本次不提供已启用 timer，不初始化真实账户。
