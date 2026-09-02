# B3 真首披依赖传播设计（A + A1）

日期：2026-08-31。状态：设计口径已获用户确认；本文为待用户书面复核的权威规格。

## 1. 目标与成功条件

在冻结 B3 研究口径不变的前提下，把 `stock_selector.stock_first_disclosure` 接入财务事实管线，并把真首披验证状态严格传播到实际参与 `style_score` 的因子依赖。

成功条件：

1. 两个冻结 PIT policy、2014-10 至 2023-12 的全部 model-universe 行都含无缺失的严格布尔 `true_first_disclosure_verified`。
2. 只有实际参与该行非空 `style_score` 的全部财务依赖均有真实首披证据时，该值才为 `True`。
3. 独立 coverage 审计达到100%后，才允许运行 states、structure 和 eval。
4. 因子计算公式、截面打分方法、universe、组合、成本、统计阈值及 verdict 规则均不改变；历史数值只能因真实首披日改变了形成日可见事实而变化。
5. 新输出进入独立 immutable run；2026-08-21 旧正式归档及 manifest 不得覆盖。

coverage ready 只解除数据前置，不代表 B3 有效；是否关账仍完全由冻结正式 eval 的原判据决定。

## 2. 冻结边界

本次只改变财务事实的 PIT 日期来源和 provenance 传播，不改变：

- `sal_g`、`pro_g`、`ep`、`bp`、`cfp`、`dp` 的公式；
- 5%/95% 缩尾、截面 z-score、缺失因子合成和 `growth - value`；
- model/size universe、行业、股本、停牌 carry、指数成份、收益、期货 carry 或状态变量；
- 配置、研究窗口、候选集合、显著性门槛、准入和关账规则。

不修补上游首披表，不执行数据库 DDL/DML。金时6号不立项。

实现基线为标签 `archive/b3-wind-share-capital-tail-20260814`（`41ed581`）。历史 B3 工作树只读保留；从标签新建独立实现工作树，不合并整个长期分叉分支。

## 3. 数据合同

### 3.1 原始事实与数据库证据

`_fetch_raw_financial` 保持 `stock_financial` 主表、CSMAR/Wind cutoff、行数、排序及语义去重合同不变，按 `(ts_code, end_date)` 左连接同 schema 的 `stock_first_disclosure`，新增：

- `first_disclosure_date`
- `disclosure_quality`

首披表缺键、日期为空、`quality != 'ok'` 或首披日早于报告期末，均没有有效真首披证据。不得静默记 True。

`DatabaseEvidenceRecorder` 必须把 `<schema>.stock_first_disclosure` 加入 `consumed_sources`，记录实际 join 模板哈希、行数和 `end_date` 范围；preflight manifest 继续使用现有 evidence schema 和 eval 校验，不设旁路格式。

### 3.2 CSMAR 报告期事实（含 A1）

income、balance、cashflow、cashflow_direct 及 CSMAR FI_T11 dividend 都是报告期事实：

- `quality == 'ok'`、首披日非空且 `first_disclosure_date >= end_date`：两个 PIT policy 都使用真实首披日；`known_date_source='stock_first_disclosure'`；原始事实 verified=True。
- 其余情况回退冻结近似：主 policy 使用 `min(stored_ann_date, legal_deadline)`；lag policy 使用 `legal_deadline + MonthEnd(1)`；source 显式标记相应 fallback；verified=False。
- fallback 只让管线可构建，绝不计入真首披覆盖。

A1 明确：FI_T11 的 dps 是按报告期键组织的事实，不是分红提案、实施或除权事件，因此使用报告真实首披日。未来若改用独立 `stock_dividend_event`，需另立设计，本次不包含。

### 3.3 Wind 与异常

Wind 段继续使用 `stored_ann_date`、`known_date_source='wind_first_disclosure'`、verified=True。Wind 日期为空或早于 `end_date` 时按现有严格合同阻断，不回退 CSMAR 近似。

raw、derived pool 和月度行的 verified 均须为 strict boolean。未知 `data_source`、日期不可解析、质量字段结构非法、semantic duplicate 或传播后出现 null，均抛 `DataBlocked`；不得使用 `bool(np.nan)` 或默认 True。

## 4. 实际依赖级 provenance

每条可用于因子的原始事实内部携带：

- `true_first_disclosure_verified: bool`
- 仅在 false 时携带紧凑的 `unverified_dependency_keys`，至少包含 `ts_code/end_date/statement_type/data_source`；已验证事实使用共享空值。该诊断信息不写入正式 exposures，也不参与数值、排序或筛选。

传播规则：

1. rev、np、CSMAR cfo 的 TTM verified，是该 TTM 数值实际差分依赖事实的逻辑 AND；Wind 直接 TTM cfo 只继承自身。未参与差分的事实不构成依赖。
2. `sal_g`、`pro_g` slope verified，是实际12个 TTM 窗口的逻辑 AND；窗外事实及未形成 slope 的空窗口不构成依赖。
3. equity 与 A1 下的 FI_T11 dps 只继承形成日 as-of 选中事实的 verified。
4. TTM 和 slope 的数值网格、`known_date` 及 as-of 选择算法保持不变。

形成日因子依赖映射：

- `sal_g` → rev slope
- `pro_g` → np slope
- `ep` → np TTM
- `bp` → equity
- `cfp` → cfo TTM；金融股按冻结规则置空后不参与依赖汇总
- `dp` → dps；只有非空且实际进入 value composite 时参与汇总

model 行 verified=True 当且仅当 `style_score` 非空，且所有实际非空、进入 growth/value composite 的因子依赖均为 True。未被 as-of 选中的旧事实、无关字段、金融股已置空的 cfp、缺失且未参与 composite 的因子及 size-only 行不得造成假阻塞。

正式 coverage 只统计 `universe_role=model`。size-only 行仍必须输出 strict boolean，但不影响100% gate。

## 5. 组件边界

`signals/style_basket/b3_build.py`：扩展 raw schema、SQL join、PIT policy、database evidence、derived pool provenance 以及月度实际因子依赖汇总；删除现有“ticker 截至形成日存在任一 CSMAR 历史事实即 false”的粗粒度逻辑。

`signals/style_basket/build.py` 与 `signals/common/factors.py`：只做 provenance-preserving 的最小扩展。公共 B1 在没有 provenance 列时必须保持原 schema 和行为。如能在 B3 专用 helper 中完成且不复制数值公式，则优先不改公共模块；禁止复制可能漂移的 TTM/斜率算法。

`backtest/b3_eval.py` 与 coverage 审计器：沿用已修复的 2014-10 至 2023-12 日历合同，不改变100% gate、统计判据或 verdict。

## 6. 测试合同

全部实现按 red→green→refactor：生产改动前先看到对应测试因缺少行为而失败。

1. SQL/contract：join 不增行；首披列存在；evidence 含 `stock_first_disclosure`；非法日期或质量不记 True。
2. PIT：有效真首披在两 policy 下日期相同且 True；sentinel 分别回退并 false；A1 dividend 使用报告首披；Wind 保持日期和 True。
3. TTM：实际差分依赖任一 false 则 false，未参与差分的 false 无影响；Wind 直接 TTM 继承自身。
4. slope：12个 TTM 任一 false 则 false，窗外 false 无影响。
5. factor aggregation：只汇总实际非空因子；金融 cfp 空值和未选中历史 sentinel 不阻断；实际选中的 sentinel 必须阻断。
6. artifact/eval：exposures strict boolean；审计拒绝99.999%和缺月，只接受两 policy 全月100%；database evidence round-trip 保留新源。
7. 数值不变性：当 as-of 选中事实相同时，六因子、style_score、model membership 和暴露必须与冻结基线一致；允许的数值变化仅来自真实首披日改变了形成日可见事实，另有 verified 按新合同变化。

聚焦测试后运行冻结 B3 全套测试。全量构建和正式重跑属于重量级任务，按 `docs/plans/2026-08-10-wsl2-runbook.md` 投 WSL2，不触碰并存 Wind、采集和旧 B3 任务。

## 7. 构建与正式裁决

1. 在新输出根构建 2014-10 至 2023-12 exposures。
2. 用 main 的已修复审计器独立审计新 `monthly_exposures.csv.gz`。
3. coverage<100%：停在 exposures 层，输出精确 `(policy, formation_date, ticker)` 及 `unverified_dependency_keys`，只修数据或 provenance，不运行 structure/eval。
4. coverage=100%：冻结输入、配置、代码、database evidence 和输出哈希；原样运行 states → structure → eval。
5. 按冻结 eval 更新研究注册表。coverage ready 不等于 PASS_SHADOW；其他冻结前置仍失败时保留相应 DATA_BLOCKED。

## 8. 验收标准

- 实现分支基于冻结标签且只含规格内最小改动。
- 每项新增行为都有可复现的 red→green 证据，冻结 B3 测试全绿。
- 两个 policy、111个形成月的 model 行真首披覆盖均为100%。
- 新 run 的输入、配置、代码、database evidence、输出哈希完整，旧 archive 哈希不变。
- 正式 eval 可从 immutable artifacts 独立复核，并按原规则给出关账或继续阻断结论。
