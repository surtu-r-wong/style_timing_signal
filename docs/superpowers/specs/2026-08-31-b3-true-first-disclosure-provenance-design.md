# B3 真首披依赖传播设计

日期：2026-08-31。状态：方案 A + A1 已获用户确认，等待书面规格复核。

## 1. 目标与成功条件

在冻结 B3 研究口径不变的前提下，把 `stock_selector.stock_first_disclosure` 的真实首披证据接入 B3 财务事实管线，并将验证状态严格传播到实际参与 `style_score` 的因子依赖。新构建必须满足以下条件：

1. 两个冻结 PIT policy、2014-10 至 2023-12 的所有 model-universe 行均有严格布尔型 `true_first_disclosure_verified`。
2. 只有实际参与该行非空 `style_score` 的全部财务依赖均有真实首披证据时，该值才为 `True`。
3. `tools.audit_b3_disclosure_coverage` 独立审计的覆盖率必须为 100%，才允许继续运行 states、structure 和 eval。
4. 因子值、截面打分、universe、组合、成本、统计阈值和 verdict 规则不得改变。
5. 新输出写入独立 immutable run；2026-08-21 的旧正式归档及其 manifest 不得覆盖。

覆盖率达标只说明数据前置解除，不代表 B3 有效。是否关账仍完全由冻结正式 eval 的原判据决定。

## 2. 冻结边界

本次只改变财务事实的 PIT 日期来源和 provenance 传播。以下内容不在范围内：

- 不修改 `sal_g`、`pro_g`、`ep`、`bp`、`cfp`、`dp` 的计算公式。
- 不修改 `style_scores` 的 5%/95% 缩尾、截面 z-score、缺失因子合成或 `growth - value` 定义。
- 不修改 model/size universe、行业处理、股本、停牌 carry、指数成份、收益、期货 carry 或任何状态变量。
- 不修改 B3 配置、研究窗口、候选集合、显著性门槛、生产准入和关账规则。
- 不修补 `stock_first_disclosure` 上游表，不执行数据库 DDL/DML。
- 金时6号不立项，也不进入本次研究注册表。

实现基线是冻结标签 `archive/b3-wind-share-capital-tail-20260814`（提交 `41ed581`）。该历史工作树只读保留；实施从该标签新建独立工作树和分支，不合并整个长期分叉分支。

## 3. 数据合同

### 3.1 原始财务事实

`_fetch_raw_financial` 继续以 `stock_financial` 为主表和原有 CSMAR/Wind cutoff 取数，但按 `(ts_code, end_date)` 左连接同 schema 下的 `stock_first_disclosure`，新增 `first_disclosure_date` 和 `disclosure_quality`。

左连接不得改变 `stock_financial` 的行数、排序或语义去重结果。`stock_first_disclosure` 缺键、日期为空、`quality != 'ok'`、首披日早于报告期末，均视为无有效真首披证据，不得静默记为 verified。

数据库 evidence 必须把 `<schema>.stock_first_disclosure` 列入 `consumed_sources`，记录实际 join 查询模板哈希、行数及 `end_date` 范围。正式 preflight manifest 仍通过现有 `DatabaseEvidenceRecorder` 和 eval 端 schema 校验，不新增旁路证据格式。

### 3.2 定期报告事实

对 income、balance、cashflow、cashflow_direct 以及 A1 确认的 CSMAR FI_T11 dividend 报告期事实：

- 若 `disclosure_quality == 'ok'`，`first_disclosure_date` 非空且不早于 `end_date`，两个 PIT policy 均使用该真实首披日，`known_date_source = 'stock_first_disclosure'`，原始事实 verified=True。
- 否则回退冻结近似：主 policy 使用 `min(stored_ann_date, legal_deadline)`，lag policy 使用 `legal_deadline + MonthEnd(1)`；`known_date_source` 明确标识各自 fallback，原始事实 verified=False。
- 回退只保持管线可构建，绝不能被计入真首披覆盖。

A1 的原因是 FI_T11 行是按报告期键组织的财务事实，不是分红提案、实施或除权事件；因此它与其他报告事实使用相同的报告真实首披日。将来若使用独立 `stock_dividend_event`，其事件公告日属于另一项设计，不在本次范围。

### 3.3 Wind 段

Wind 段继续使用原 `stored_ann_date` 和 `known_date_source = 'wind_first_disclosure'`，verified=True。若 Wind 日期为空或早于 `end_date`，按现有严格数据合同阻断，不回退 CSMAR 法定日近似。

### 3.4 严格类型与异常

原始、衍生池和月度模型行的 verified 列必须是无缺失的 Python/pandas strict boolean。未知 `data_source`、非法质量值的结构类型、非法日期、semantic duplicate 或依赖传播后出现 null，均抛出 `DataBlocked`；不得把异常转换成 True，也不得用 `bool(np.nan)`。

## 4. 依赖级 provenance

### 4.1 原始事实标识

每条可被因子读取的事实内部携带：

- `true_first_disclosure_verified: bool`
- 仅在未验证时携带紧凑的 `unverified_dependency_keys`，键至少包含 `ts_code/end_date/statement_type/data_source`，用于覆盖不足诊断；已验证事实使用共享空值，不在正式 exposures 中展开。

该诊断元数据不参与任何数值计算、排序或筛选。

### 4.2 TTM

rev、np 和 CSMAR cfo 的 TTM verified 等于该 TTM 数值实际差分所使用原始事实 verified 的逻辑 AND。直接 TTM 的 Wind cfo 只依赖自身。缺失且未进入数值计算的事实不构成依赖；不得按“该股票历史上出现过未验证事实”整体阻断。

TTM 的 `known_date` 仍是现有数值依赖的最晚可知日，verified 传播不得改变数值、报告期网格或 known-date 选择。

### 4.3 成长斜率

`sal_g` 和 `pro_g` 的每个 slope verified 等于该 slope 实际使用的 12 个 TTM 观察 verified 的逻辑 AND。窗口不足时 slope 仍按原逻辑为空；空 slope 不生成模型依赖。

### 4.4 事件型因子输入

equity 和 A1 定义下的 FI_T11 dps 各自只依赖 as-of 选中的那条报告期事实，其 verified 直接继承该事实。股本和市值沿用各自冻结数据合同，不纳入“财务真首披”布尔值。

### 4.5 因子与 style_score 行

形成日对每只 size-eligible 股票进行原有 as-of 选择后，依赖映射为：

- `sal_g` → 选中的 rev slope
- `pro_g` → 选中的 np slope
- `ep` → 选中的 np TTM
- `bp` → 选中的 equity
- `cfp` → 选中的 cfo TTM；金融股按原规则把 cfp 置空后，该项不参与依赖汇总
- `dp` → 选中的 dps；只有因子值非空并实际进入 value composite 时才参与汇总

月度 model 行 verified=True 当且仅当 `style_score` 非空，且所有实际非空、进入 growth/value composite 的因子依赖均 verified=True。无关报表、未被 as-of 选中的旧事实、被置空的金融 cfp、缺失且未参与 composite 的因子及 size-only 行不得制造假阻塞。

正式 coverage 审计只统计 `universe_role=model`；size-only 行仍输出严格布尔值，但不影响 100% gate。

## 5. 组件改动边界

### `signals/style_basket/b3_build.py`

- 扩展 raw schema 校验、SQL join、PIT policy 和 database evidence。
- 在 `build_policy_snapshots` 中保留 derived pool 的 verified/诊断列，并按实际因子可用性汇总模型行。
- 删除现有“只要该 ticker 截至形成日存在任一 CSMAR 历史事实就标 false”的粗粒度逻辑。

### `signals/style_basket/build.py` 与 `signals/common/factors.py`

- 只做 provenance-preserving 的最小扩展，使 TTM 和 slope 返回与现有数值完全同形的 verified 结果。
- 公共 B1 调用在没有 provenance 列时保持原返回 schema 和行为，避免改变现役信号。

如能在 `b3_build.py` 的专用 helper 中完成而不复制数值公式，则优先保持公共模块不变；禁止复制一套可能与原因子漂移的 TTM/斜率算法。

### `backtest/b3_eval.py` 与审计器

- 使用已提交的 2014-10 至 2023-12 coverage 日历合同。
- 不改变 coverage=100% gate、正式统计判据或 verdict 语义。

## 6. 测试策略

所有行为按 red-green-refactor 实施，每个生产改动之前先看到对应测试因缺少该行为而失败。

1. SQL/数据合同：join 不增行；首披表列存在；证据源包含 `stock_first_disclosure`；非法日期和质量不被标 True。
2. PIT policy：有效真首披在两个 policy 下日期相同且 verified；sentinel 分别回退两个冻结近似且 false；A1 dividend 使用报告首披；Wind 行保持原日期与 true。
3. TTM：差分依赖任一 false 则 false，未参与差分的 false 事实无影响；直接 Wind TTM 继承自身。
4. slope：12 个 TTM 任一 false 则 false，窗外 false 观察无影响。
5. factor aggregation：只汇总实际非空因子；金融 cfp 空值不阻断；未选中的历史 sentinel 不阻断；实际选中的 sentinel 必须阻断。
6. artifact/eval：exposures 输出 strict boolean；coverage 审计拒绝 99.999% 和缺月，只接受两个 policy 全月100%；数据库 evidence round-trip 保留新源。
7. 数值不变性：在相同 raw 数值与已知日输入下，六因子、style_score、model membership 和暴露数值与冻结基线一致；唯一允许变化是由真实首披日导致的 as-of 可用时点及 verified 列。

聚焦测试通过后运行冻结 B3 全套测试。长跑全量构建与正式回测按项目规则投 WSL2，遵循 `docs/plans/2026-08-10-wsl2-runbook.md`，不得触碰并存的 Wind、采集或旧 B3 任务。

## 7. 构建、覆盖与正式重跑

1. 在新输出根构建 2014-10 至 2023-12 exposures，不覆盖旧 archive。
2. 用 main 已修复审计器对新 `monthly_exposures.csv.gz` 做独立审计。
3. 若 coverage<100%，停止在 exposures 层，输出精确 `(policy, formation_date, ticker)` 以及其 `unverified_dependency_keys`，只修数据或 provenance，不运行 structure/eval。
4. 若 coverage=100%，冻结输入文件、配置、代码、数据库 evidence 和输出哈希。
5. 原样运行 states → structure → eval。新 verdict 不引用旧 approximate-PIT 结果替代计算。
6. 更新研究注册表：按冻结统计结果决定 B3 是否正式关账；即使结论仍为 STOP，也必须说明真首披前置已解除。若其他冻结前置仍阻断，则保留相应 DATA_BLOCKED，不得把 coverage ready 写成 PASS_SHADOW。

## 8. 验收标准

- 新实现分支基于冻结标签且只包含规格内最小改动。
- 全部新增测试有可复现的 red→green 证据，冻结 B3 测试全绿。
- 新 exposures 的两个 policy、111 个形成月，model 行真首披覆盖均为100%。
- 新 run 的 manifest、配置、输入和输出哈希完整，旧 archive 哈希保持不变。
- 正式 eval 可从 immutable artifacts 独立复核，并按原规则给出最终关账或继续阻断结论。
