# B3 连续停牌区间 CLOSE 证据设计

**日期：** 2026-07-28
**状态：** 交互设计已确认，书面规格待用户复核
**优先级：** 固定历史窗口 B3 绩效评估阻塞项
**范围：** 扩展 B3 的停牌 CLOSE carry 证据合同；只读数据库，不运行
prod，不改变信号或绩效评价假设

## 背景与问题

当前项目最重要的目标是找到绩效最好的指标。数据新鲜度、最新生产信号和
shadow 运行在指标优选完成前不构成高优先级；眼下需要先解除固定历史窗口的
B3 数据阻塞，才能在相同窗口和相同交易假设下比较指标。

`data_end=2023-12-31` 的 B3 preflight 当前为 `DATA_BLOCKED`。其中 CLOSE
缺口为：

- 202 个全部 formation 票·月；
- 190 个 required formation 票·月；
- 14 只股票；
- 原审计全部归为 `UNEXPLAINED_EXACT_DATE_GAP`。

B3 现有规则只在 formation date 当天存在 `stock_suspension` 行时，才允许使用
最近一个收盘价。当前数据库的 `stock_suspension` 已有 357,936 行，覆盖
2013-01-04 至 2026-07-24，但上述 202 个形成日没有一个精确日期匹配。因此，
继续补充相同来源的历史停牌行也不会自然满足“每个形成日都有一行”的旧合同。

逐票检查表明，12 只 required 缺口股票均存在同一种可观察事实链：

1. 最后一个有效价格日；
2. 紧接着的官方交易日出现 `suspend_type=今起停牌`；
3. 此后直至相关 formation date 没有新的有效价格；
4. 后续才恢复交易。

`stock_status` 在其覆盖期内可独立核实 24 个相关形成日，24 个均为
`is_suspended=true`，但该表从 2020-04-30 才开始覆盖，不能作为完整历史来源。
Wind WSD 当日额度已经耗尽，也不应成为本次可复验构建的运行依赖。

因此问题不是缺少足够的历史停牌起点，而是 B3 把“停牌状态证据”错误地限定为
formation date 当天的事件行。需要在不引入未来信息的前提下，把明确停牌起点
解释为一个持续状态区间。

## 目标

1. 在固定历史窗口内，使用严格的连续停牌区间证据填补合法的 CLOSE 缺口。
2. 保留现有精确日停牌证据，并使其优先于新区间证据。
3. 对每一个候选票·月输出可逐行复验的接受或拒绝依据。
4. 在当前锚定数据库快照下，把 required CLOSE 缺口从 190 降为 0。
5. 让 CLOSE 不再阻塞 B3；随后只需解决 SHARES 阻塞即可进入指标绩效比较。
6. 保持 B1、B2、B3 信号构造、持仓、成本、收益和最终绩效判定规则不变。

## 不在本次范围

- 不处理最新数据、实时性、shadow 或生产信号新鲜度。
- 不运行 prod，也不在本次 CLOSE 实现中启动正式 B3 eval。
- 不写 Market Monitor 数据库，不回填或伪造 `stock_suspension` 行。
- 不调用 Wind 或依赖形成日之后才能获得的数据。
- 不解决 SHARES 尾巴，也不修改面值校准逻辑。
- 不对 `000545.SZ`、`600698.SH` 在源数据覆盖开始前的停牌区间进行推断。
- 不改变 `_PAR_TOLERANCE`、样本池、PIT policy、形成日、最短上市天数或
  `DATA_MISSING_CLOSE` 的原因优先级。

## 方案选择

### 采用：独立纯函数证据模块

新增：

`signals/style_basket/b3_suspension.py`

数据库读取、候选构建和区间判定分离。模块接收规范化的交易日历、元数据、价格、
停牌事件和 formation 候选 DataFrame，输出确定性的证据表。这样可以对时间边界、
重复行、冲突事件和未来数据隔离进行精细测试，也避免继续扩大
`b3_build.py` 内的单体业务逻辑。

### 不采用：在动态 SQL 中直接推导

该方案文件较少，但会把交易日历、上市状态、事件优先级和失败策略隐藏在复杂 SQL
中，难以对每个拒绝分支做纯函数测试，也不利于发布完整证据表。

### 不采用：把推导结果回填成每日停牌行

它对 B3 代码改动最少，但会把“由原始事实推导的持续状态”和“源系统原始事件”
混写进同一数据库表，破坏事实来源边界。本次也没有写库授权，且 Wind WSD 当前
受额度限制。

## 证据合同

### 候选范围

候选 formation 票·月必须同时满足：

1. 属于沪深 A 股样本范围，排除 `.BJ` 和 `.HK`；
2. formation date 当日处于合法上市区间；
3. formation date 时已上市至少 180 天；
4. 原始 formation CLOSE 缺失或非数值；
5. 现有精确日停牌 carry 尚未填补该 CLOSE。

候选构建与 PIT policy 无关，因此同一个 formation 票·月只判定一次，再供两种
PIT policy 共用。它不能因股本是否缺失而被提前排除，因为 B3 原因优先级要求
CLOSE 缺失先于 SHARES 缺失。

### 接受连续停牌区间的必要条件

对候选 `(ts_code, formation_date)`，仅当以下条件全部满足时接受：

1. 存在不晚于 formation date 的明确
   `suspend_type=今起停牌` 事件，记为 `suspension_start`；
2. `suspension_start` 是官方交易日；
3. 停牌前最后一个有效价格日等于
   `suspension_start` 前一个官方交易日；
4. 停牌前 CLOSE 是有限、严格大于零的数值；
5. 从 `suspension_start` 到 formation date（含两端）没有任何有效价格；
6. formation date 本身没有有效价格；
7. formation date 位于股票合法上市区间内；
8. 在该候选的可知历史内，不存在使有效停牌起点不唯一的冲突或重叠事件。

接受后使用停牌前最后一个有效 CLOSE，并标记：

`close_carry_method=CONTINUOUS_SUSPENSION_INTERVAL`

该判断只消费 `event_date <= formation_date` 和
`price_date <= formation_date` 的事实。即使读取器为审计目的同时取得了后续交易，
分类函数也必须先按 formation date 截断输入。

### 现有精确日证据

现有规则保留：

- formation date 当天有 `stock_suspension` 证据；
- 能取得不晚于 formation date 的非空 CLOSE；该路径保持当前
  `notna` 合同，非有限或非正数结果仍交给既有
  `DATA_INVALID_MARKET_VALUE` 规则处理；
- 原始 formation CLOSE 缺失。

该路径标记：

`close_carry_method=EXACT_SUSPENSION`

证据优先级固定为：

```text
原始 formation CLOSE
> EXACT_SUSPENSION
> CONTINUOUS_SUSPENSION_INTERVAL
> DATA_MISSING_CLOSE
```

如果精确日证据和区间证据同时存在，必须选择精确日证据；两者若给出不同 carry
CLOSE，则作为结构冲突阻止发布，不能依靠优先级掩盖矛盾。

### 未来信息隔离

以下字段仅供报告和交叉核验，不得影响接受或拒绝：

- `next_trade_date`
- `next_nonnull_close`
- 后续复牌事件
- `exact_stock_status_confirmed`

这意味着：一只股票在 formation date 当时仍处于开放式停牌，也可以依据当时已知的
停牌起点和“截至当日无交易”事实被接受；无需等待未来复牌才能证明。

测试必须证明：任意修改、删除或延后 formation date 之后的价格和状态记录，不能改变
该 formation 的分类和 carry 值。

## 数据读取与处理流程

### 1. 保留现有 formation 输入

`_formation_inputs` 继续以权威交易日历生成月末 formation date，并读取：

- `stock_meta`
- formation date 的 `stock_daily_price`
- 正值 `stock_share_capital`
- 行业历史
- formation date 精确匹配的 `stock_suspension`
- 精确停牌对应的最近 CLOSE
- 财务事实

精确日路径先运行，不改变既有结果。

### 2. 构建最小候选集合

根据 month-end CLOSE 矩阵、上市元数据、180 天规则和精确 carry 结果，在内存中建立
缺失 CLOSE 候选。只有候选中的 ticker 才进入新增历史读取。

当前锚定快照预计为 202 个候选、14 只股票。查询不能硬编码这些 ticker 或计数；
验收数字只作为当前数据快照的回归锚。

### 3. 有界读取候选历史

对候选 ticker 读取不晚于 `data_end` 的：

- `stock_daily_price` 中的交易日期和 CLOSE；
- `stock_suspension` 中的 `trade_date`、`suspend_type`、`suspend_reason`；
- 可选 `stock_status`，仅用于已有覆盖期内的报告级确认。

查询按候选 ticker 和 `data_end` 参数化，排序稳定，并记录到
`DatabaseEvidenceRecorder`。不读取全市场逐日价格，不调用外部 API。

价格和事件可以一次读取到 `data_end`，但纯函数必须针对每个 formation date 使用
`<= formation_date` 的切片。后续价格只允许在证据表中生成报告字段。

`database_source_evidence` 继续记录：

- consumed source 名称；
- SQL query template SHA-256；
- 行数；
- 日期上下界。

它不是数据库内容哈希；完整输出内容由 stage manifest 中的文件 SHA-256 固定。

### 4. 纯函数分类

`b3_suspension.py` 负责：

1. 输入列、日期、ticker 和数值规范化；
2. 重复与冲突检测；
3. 对每个候选按 formation date 截断可知事实；
4. 验证官方交易日前后关系；
5. 找到唯一有效的停牌起点和前一有效 CLOSE；
6. 验证区间内无有效价格；
7. 生成一行接受或拒绝结果；
8. 使用稳定排序输出。

纯函数不连接数据库、不读环境变量、不写文件。

### 5. 合并 carry

`build_policy_snapshots` 接收精确 carry 和已接受的区间 carry：

- 原始 CLOSE 非空时绝不覆盖；
- 精确 carry 优先；
- 区间 carry 只填仍缺失的 CLOSE；
- 输出原有 `close_carried: bool`；
- 新增 `close_carry_method: str`，未 carry 时为空字符串。

市场价值、size exclusion 和其后的因子计算继续使用合并后的同一 CLOSE 列，不改变
其他计算路径。

### 6. Preflight 发布

`STAGE_OUTPUTS["preflight"]` 增加：

`suspension_interval_evidence.csv`

`B3Sources` 增加一个可选的证据读取接口。默认数据库 sources 返回本次缓存的证据表；
合成测试 sources 或在形成输入前已经阻塞的运行，仍发布具有固定列的空证据表。

写入顺序为：

1. 启动时失效当前 preflight manifest；
2. 原子写 coverage、diagnostics 和 interval evidence；
3. 验证声明输出集合；
4. 最后原子发布 preflight manifest。

因此任何部分写入、陈旧文件或哈希不匹配都不能被下游阶段当作有效 preflight。

## `suspension_interval_evidence.csv` 合同

文件对每一个连续区间候选保留一行，包括接受和拒绝结果。固定字段至少包括：

- `ts_code`
- `formation_date`
- `required_formation`
- `list_date`
- `delist_date`
- `suspension_start`
- `previous_official_trade_date`
- `previous_close_date`
- `previous_close`
- `suspend_type`
- `suspend_reason`
- `evidence_method`
- `accepted`
- `rejection_reason`
- `next_trade_date`
- `next_nonnull_close`
- `exact_stock_status_confirmed`

规则：

- `evidence_method` 对接受行固定为
  `CONTINUOUS_SUSPENSION_INTERVAL`，拒绝行为空；
- `accepted` 必须是真正的布尔值；
- `rejection_reason` 对接受行为空，对拒绝行使用稳定枚举；
- 日期统一为无时区 `YYYY-MM-DD`；
- 按 `formation_date, ts_code` 稳定排序；
- `(formation_date, ts_code)` 必须唯一；
- 报告字段缺失不得转化为拒绝理由；
- 文件 SHA-256 写入 preflight manifest。

稳定拒绝枚举至少包括：

- `NO_EXPLICIT_SUSPENSION_START`
- `START_NOT_OFFICIAL_TRADING_DAY`
- `PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY`
- `INVALID_PREVIOUS_CLOSE`
- `PRICE_OBSERVED_DURING_INTERVAL`
- `OUTSIDE_LEGAL_LISTING_INTERVAL`
- `SUSPENSION_START_PRECEDES_SOURCE_COVERAGE`

重复键、冲突起点或互相矛盾的来源事实不是普通拒绝枚举，而是结构错误。

## Coverage 审计合同

原 `close_carry_forward` 报告行拆分或增加 `side`：

- `EXACT_SUSPENSION_CARRY_FORWARD`
- `INTERVAL_SUSPENSION_CARRY_FORWARD`

`close_carried` 总数必须等于两类之和。两种 PIT policy 对同一个 formation date 的
CLOSE carry 方法和数量必须一致。

当前锚定快照的预期对账为：

| 项目 | 修改前 | 修改后 |
| --- | ---: | ---: |
| CLOSE 候选 all | 202 | 202 |
| 区间证据接受 all | 0 | 198 |
| CLOSE 残留 all | 202 | 4 |
| CLOSE 候选 required | 190 | 190 |
| 区间证据接受 required | 0 | 190 |
| CLOSE 残留 required | 190 | 0 |

198 个接受项由 190 个 required 项和 `000751.SZ` 的 8 个非 required 项组成。
4 个拒绝项来自 `000545.SZ`、`600698.SH` 在停牌来源覆盖开始前的历史，不得通过
宽松规则消除。

这些数字必须通过证据表、snapshot exclusion 和 `coverage_audit.csv` 三方逐月对账。
如果数据库快照发生变化，脚本必须报告实际差异，而不能为了满足硬编码数字改写事实；
固定锚数据测试仍负责捕获非预期漂移。

## 失败策略

### 保守拒绝并保留 `DATA_MISSING_CLOSE`

以下情况只拒绝该候选，不自动推断：

- 缺少明确 `今起停牌` 起点；
- 起点不是官方交易日；
- 前一有效 CLOSE 不在紧邻的前一个官方交易日；
- 前值为零、负数、NaN 或无穷值；
- 起点到 formation date 之间存在有效价格；
- formation date 不在合法上市区间；
- 必须依赖 formation date 之后的信息才能成立；
- 事件或价格历史早于可验证的源覆盖范围。

### 阻止发布

以下情况抛出 `DataBlocked`，生成带 blocker 的 preflight manifest，且不得发布为
`OK`：

- 候选、价格或事件存在值冲突的重复键；
- 同一候选存在多个无法唯一裁决的停牌起点；
- 精确证据与区间证据给出不同 carry CLOSE；
- 一个候选生成多行结果；
- 接受行缺少必要证据字段；
- 区间证据、snapshot carry 和 coverage 汇总无法逐行对账；
- 两种 PIT policy 的 CLOSE carry 结果不一致；
- 新增证据文件缺失、列合同错误、路径不安全或 manifest 哈希不匹配。

空的或早期阻塞的证据文件不表示成功；最终状态仍由 blocker 决定。

## 文件影响

预计新增：

- `signals/style_basket/b3_suspension.py`
- `tests/test_b3_suspension.py`

预计修改：

- `signals/style_basket/b3_build.py`
- `tests/test_b3_exposures.py`
- `data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`
- `tests/test_b3_impact_audit.py`
- `data_fixes/2026-07-25-share-capital-par/README.md`

只读 impact audit 将使用同一纯函数或同一稳定证据枚举重算 CLOSE 影响，禁止复制另一套
稍有差异的区间判定逻辑。

## 测试策略

实现按 TDD 推进。

### 纯函数单元测试

1. `今起停牌` 紧接最后交易日且区间无价格时接受。
2. 周末和法定节假日通过官方交易日历正确跳过。
3. 区间中任何一个有效价格都会拒绝。
4. 缺少明确起点时拒绝。
5. 前一 CLOSE 不在紧邻官方交易日时拒绝。
6. 零、负数、NaN 和无穷 CLOSE 均拒绝。
7. 上市或退市边界冲突时拒绝。
8. 重叠起点、冲突重复行和不唯一 carry 值抛出 `DataBlocked`。
9. 精确日证据优先，且与区间值冲突时阻止发布。
10. 修改 formation date 之后的复牌或价格记录不改变分类。
11. `stock_status` 确认字段只影响报告字段，不影响 `accepted`。
12. 输入行顺序不影响结果。
13. 完全相同的重复源行按现有去重合同处理，值冲突重复行不得放大结果。

### 集成与 manifest 测试

1. 新查询只读取候选 ticker，且结束日期不超过 `data_end`。
2. `DatabaseEvidenceRecorder` 记录新增价格、停牌和可选状态查询。
3. 成功和阻塞 preflight 都写固定 schema 的证据文件。
4. manifest 必须声明证据文件并验证 SHA-256。
5. 父阶段验证拒绝缺失、被篡改、路径越界或额外的 preflight 输出。
6. coverage 分开报告 exact 和 interval carry，且总数一致。
7. 两种 PIT policy 的 carry 明细一致。
8. 当前锚定数据对账为 202 个候选、198 个接受、4 个拒绝；
   required 为 190 个候选、190 个接受、0 个拒绝。
9. required blocker 不再包含 `DATA_MISSING_CLOSE`，但在 SHARES 尚未解决时
   preflight 仍应保持 `DATA_BLOCKED`。
10. B1、B2 和既有 equal-weight 已提交产物不发生变化。

### 验证顺序

1. 新增纯函数测试；
2. B3 exposures/preflight/manifest 相关测试；
3. B3 impact audit 测试；
4. B3 全部相关测试；
5. 全量测试；
6. 在 `MemoryMax=8G` 限制下运行固定
   `data_end=2023-12-31` preflight；
7. 核对 evidence、coverage 和 manifest；
8. 不运行 prod，不运行正式 eval。

## 完成标准

- 连续停牌证据仅依据 formation date 当时可知事实。
- 精确日证据保留且优先，原始 CLOSE 从不被覆盖。
- `suspension_interval_evidence.csv` 对全部候选逐行解释并由 manifest 固定哈希。
- 当前锚定快照下，CLOSE 从 202/190 降为 4/0，剩余 4 个均为非 required。
- required blocker 中不再存在 `DATA_MISSING_CLOSE`；SHARES 是当前剩余的数据阻塞。
- 不写数据库、不调用 Wind、不运行 prod 或正式 eval。
- B3 相关测试与全量测试通过。
- 只有在 SHARES 也解除且固定窗口 preflight 为 `OK` 后，才进入同窗、同成本、
  同 carry 假设下的指标绩效比较。
