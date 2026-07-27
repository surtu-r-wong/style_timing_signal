# B3 股本尾巴与 CLOSE 缺口逐票审计设计

**日期：** 2026-07-27
**状态：** 已批准，待实现
**范围：** 只读审计；不执行股本/价格回填，不重跑 B3 preflight，不运行
`verify_par_recovery.py --phase before`

## 目标

把最终 `tail.csv` 中的 57 只股本尾巴，以及最终 B3 preflight 中的
`DATA_MISSING_CLOSE` 202 票·月，展开为可复验的逐票汇总和逐票逐月证据表。
产物必须与最终 `coverage_audit.csv` 的聚合计数完全对账，并优先展示
2023 年仍在样本池、影响月份多的股票，为后续 Wind 事实回填或明示政策处置提供依据。

## 不在本次范围

- 不写 Market Monitor 数据库，也不调用任何 backfill。
- 不修改 `_PAR_TOLERANCE`、标准面值档或 B3 fail-closed 规则。
- 不自动推断或填入股本、收盘价。
- 不把 CLOSE 缺口自动裁决为豁免、剔除或数据修复；只提供可核查证据和初步证据类别。
- 不重跑 preflight/eval；修复后的正式复验仍按既有计划单独执行。

## 推荐方案

新增一个轻量、只读、可测试的审计脚本：

`data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py`

脚本以最终 `tail.csv` 和 `coverage_audit.csv` 为审计锚，从 PostgreSQL 只读取构成
B3 `size_exclusion` 所需的最小数据：

- `stock_meta`
- 月末 formation date 当日的 `stock_daily_price`
- formation date 当日的 `stock_suspension`
- 停牌票 formation date 之前最近一条价格
- 全体沪深样本的正值 `stock_share_capital`（与 B3 原查询一致；输出再限定 tail）
- CLOSE 缺口票前后最近的非空价格证据

核心分类逻辑实现为接收 DataFrame 的纯函数，数据库读取与分类分离，以便用合成数据
覆盖 B3 原因优先级而不依赖真库测试。

不采用以下方案：

1. 修改 preflight 持久化逐票快照并重跑。它会触发不必要的全量财务读取和 8G 运行，
   而本次只需要 size 数据。
2. 用单体 SQL 直接导表。它会复制较多 B3 业务优先级，难以进行细粒度测试，也不便记录
   输入哈希与产物哈希。

## 输入与审计锚

### `tail.csv`

- 必须恰好有 57 个唯一 `ts_code`。
- 必须包含：
  `ts_code,list_date,csmar_latest_a003101000,anchor_2025_shares,implied_par,note`。
- 默认期望 SHA-256：
  `93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223`。

### `coverage_audit.csv`

- formation date 只从最终审计表取得，不自行发明月末日历。
- 以 `pit_policy=legal_deadline`、`check=size_exclusion` 的行作为计数锚。
- 必须验证两种 PIT policy 在 `DATA_MISSING_SHARES` 和
  `DATA_MISSING_CLOSE` 上逐月计数一致。
- 默认期望 SHA-256：
  `13c8af70650a24ba00c1b0890e979c487a0133589e6127967340e622426e9358`。
- 必须包含 128 个 formation 月，其中 120 个 `required_formation=True`。

CLI 允许显式传入其他路径或期望哈希，但不得静默接受哈希不匹配。

## 与 B3 完全一致的分类口径

对每个 formation date：

1. 样本基础池为沪深 A 股；`.BJ`、`.HK` 不进入审计。
2. 股票须满足：
   `list_date <= formation_date <= delist_date`，其中空 `delist_date` 视为仍上市。
3. 原因按 B3 的既有顺序判定：
   - `list_date` 缺失；
   - 上市不足 180 天；
   - formation date 收盘价缺失；
   - PIT 可知股本缺失；
   - 非法市值。
4. formation date 原始 close 缺失时，仅在同日存在 `stock_suspension` 证据且能找到
   formation date 当日或之前的数值 close 时，使用该旧价；否则仍为
   `DATA_MISSING_CLOSE`。
5. 股本只接受 `total_shares > 0` 的节点。`available_date` 为空时按
   `effective_date` 处理；在 `known_date <= formation_date` 的节点中，按
   `effective_date` 取最新一期。
6. 同时缺 close 和 shares 时，按 B3 顺序只计入 `DATA_MISSING_CLOSE`。

## 输出

所有输出先写临时文件，全部对账通过后再原子替换正式文件。

### `shares_tail_impact_by_ticker.csv`

固定 57 行，每票一行。字段包括：
其中 `688347.SH` 截至 data-end 上市不足 180 天，实际 SHARES 影响为 0，但仍保留在 57 行 tail 汇总中。

- `ts_code`
- `list_date`, `delist_date`, `listing_status_at_data_end`
- `in_pool_2023_any`, `in_pool_2023_12`
- `affected_months_all`, `affected_months_required`,
  `affected_months_2023`
- `first_affected_formation`, `last_affected_formation`
- `csmar_latest_a003101000`, `anchor_2025_shares`, `implied_par`, `note`
- `priority_rank`

排序键为：
`in_pool_2023_12` 降序、`affected_months_2023` 降序、
`affected_months_required` 降序、`affected_months_all` 降序、`ts_code` 升序。
`priority_rank` 是该稳定排序后的 1 起始序号，不代表自动处置结论。

### `shares_tail_impact_detail.csv`

每个实际 `DATA_MISSING_SHARES` 票·月一行，包含：

- `ts_code`, `formation_date`, `required_formation`
- `list_date`, `delist_date`
- `raw_close`, `close_source`
- `selected_share_effective_date`, `selected_share_known_date`,
  `selected_total_shares`
- `reason_code`

最终应为 5,781 行，其中 required 5,445 行。
真库结果涉及 56 只股票；第 57 只 `688347.SH` 因上市不足 180 天未进入 size 缺股本原因。

### `close_gap_impact_by_ticker.csv`

每个出现过 `DATA_MISSING_CLOSE` 的 ticker 一行，字段包括：

- 与股本汇总相同的上市状态、2023 在池、影响月份和首末日期字段
- `exact_row_missing_months`
- `exact_row_null_close_months`
- `suspension_without_usable_carry_months`
- `possible_delist_boundary_months`
- `unexplained_exact_date_gap_months`
- `priority_rank`

排序规则与股本汇总一致。

### `close_gap_impact_detail.csv`

最终应为 202 行，其中 required 190 行。每行至少包含：

- `ts_code`, `formation_date`, `required_formation`
- `list_date`, `delist_date`
- `raw_price_row_present`, `raw_close`
- `suspension_evidence`
- `carry_close_date`, `carry_close`, `usable_carry`
- `previous_nonnull_close_date`, `previous_nonnull_close`
- `next_nonnull_close_date`, `next_nonnull_close`
- `after_last_observed_close`, `no_later_observed_close`
- `evidence_bucket`, `reason_code`

`evidence_bucket` 只表达可直接观察的证据：

1. `EXACT_ROW_NULL_CLOSE`
2. `SUSPENSION_WITHOUT_USABLE_CARRY`
3. `POSSIBLE_DELIST_BOUNDARY`：无后续非空 close 且数据库存在退市日期
4. `UNEXPLAINED_EXACT_DATE_GAP`

它不是最终修复或豁免结论。交易日历错位、遗漏停牌证据等判断留给后续逐行审查。

### `impact_audit_manifest.json`

记录：

- 生成时间、数据截止日与数据库 schema
- 两个输入文件的路径和 SHA-256
- 四个 CSV 的 SHA-256、行数
- 全量与 required 对账计数
- 只读 SQL 涉及的表名

manifest 不记录数据库密码、token 或完整连接串。

## 对账与失败策略

以下任一条件不满足时退出非零，且不得留下部分正式输出：

- tail 不是 57 个唯一 ticker；
- coverage 两个 PIT policy 的逐月缺口计数不同；
- formation 月不是 128/120；
- 股本明细不是 5,781/5,445；
- CLOSE 明细不是 202/190；
- 逐月重建计数与 coverage 审计任一月份不一致；
- 股本缺口 ticker 集不是 tail ticker 集的子集，或 57 行 tail 汇总不完整；
- 数据库返回冲突重复键、非法日期或 ticker；
- 输入哈希不符。

## 数据库安全

- 连接设置 `connect_timeout` 与 `statement_timeout`。
- 打开事务后执行 `SET TRANSACTION READ ONLY`。
- SQL 只允许 `SELECT`，并将 schema 作为受校验标识符插入。
- 不在日志或 manifest 输出凭据。
- 网络连接失败时立即停止并解释，不滚动重试。

## 测试策略

新增 `tests/test_b3_impact_audit.py`，严格按 TDD 推进：

1. 输入锚验证：哈希、57 ticker、128/120 formation 月、PIT policy 一致性。
2. B3 原因优先级：上市不足 180 天优先于 close/shares，close 缺失优先于 shares。
3. 停牌旧价：只有同日停牌证据加可用旧价才能消除 CLOSE 缺口。
4. 股本 as-of：按 known date 过滤，再按 effective date 取最新。
5. 逐票汇总：2023 状态、首末影响月、稳定优先级排序。
6. CLOSE 证据桶：精确行空值、无可用 carry、可能退市边界、未解释缺口。
7. 对账失败不得发布正式输出；成功输出包含四份 CSV 与 manifest。
8. 真库只读运行后，再用最终 coverage 审计逐月交叉验证，并运行相关测试与完整测试。

## 完成标准

- 四份 CSV 和 manifest 在
  `data_fixes/2026-07-25-share-capital-par/` 下生成。
- 股本与 CLOSE 总数、required 数和逐月计数全部与最终 coverage 审计一致。
- 57 股本尾巴逐票排序可直接支持 Wind 回填优先级讨论。
- CLOSE 202 每行具备足够证据进入下一阶段人工/规则处置，且没有自动伪造事实。
- 所有新增测试、B3 相关测试和完整测试通过。
