# Research Registry and B3 Coverage Audit Design

日期：2026-08-31。用户批准采用“结构化台账 + 自动校验/渲染”的混合方案，先完成研究结论治理、陈旧措辞修复与 B3 真首披覆盖审计工具；正式 B3 重跑、跨仓库数据携带、live OOS 和 5d20z 定位留到后续阶段。

## 背景与问题

当前研究证据本身大体可复核，但“实验做完”和“结论被准确登记”之间仍有断层：

1. `docs/plans/README.md` 只登记了部分权威运行与近期研究，未覆盖全部已经裁决的 signal probes。
2. 若干文档把“指定配置在冻结样本内失败”外推成“整个方法族已关账”，或把 long-flat 的风险/换手优势写成统计意义上的全面最优。
3. B3 旧近似 PIT 运行的统计 verdict 是 STOP，但最终 verdict 仍为 `DATA_BLOCKED`；真首披底座虽已大幅补齐，尚未针对实际 B3 模型行完成覆盖审计和正式重跑，因此不能终局关账。
4. 第五桶和等比五桶的未来 verdict 生成器仍硬编码旧 PIT 限制和旧数据截止日，会继续制造陈旧元数据。
5. 历史不可变产物不应被追溯改写；纠错必须通过清晰的 supersede、当前状态和后续运行完成。

## 目标

1. 建立一个机器可读、可校验的研究结论事实源，覆盖已经采用、关闭、暂定、阻塞、研究专用和仍开放的工作。
2. 让 README 的研究状态表由事实源确定性生成，避免双重维护和状态漂移。
3. 精确区分“候选/配置级关账”“方法族降优先级”和“方法族已被充分否定”。
4. 提供只读 B3 真首披覆盖审计工具，直接衡量实际进入 B3 模型计算的记录，而不是引用全库覆盖率。
5. 修复会继续传播的陈旧措辞与硬编码 caveat，同时保持既有不可变 run 的字节内容不变。

## 非目标

- 不在本阶段连接或修改生产数据库。
- 不在本阶段执行正式 B3 build/eval、改变 B3 判据或形成新的正式 verdict。
- 不回写、删除或重命名既有不可变 run 和归档证据。
- 不改动生产信号、仓位、阈值、映射或用户当前生成的输出 CSV。
- 不解决外部 `futures_daily` 数据陈旧，只记录后续交接与阻塞关系。
- 不启动 live OOS，不改变 5d20z 的生产/研究定位。

## 1. 研究台账

新增 `docs/plans/research_registry.yaml`，作为研究状态的唯一事实源。README 仅保留说明文字和由工具维护的受控表格，不再手工维护同一组状态。

每条研究记录至少包含：

- 稳定 ID、标题和研究族；
- 生命周期状态；
- 结论类型和证据等级；
- 被裁决的精确范围；
- 可以声称的结论与禁止外推的结论；
- caveat 和重开条件；
- 设计、执行报告及证据路径；
- supersedes / superseded_by / depends_on 关系；
- 是否影响生产及当前生产角色。

### 1.1 生命周期状态

允许的核心状态为：

- `adopted`：已被生产或当前推荐方案采用；采用理由和统计证据分开记录。
- `closed`：给定候选、参数、样本和判据内已经裁决；必须提供精确 scope。
- `provisional`：当前可以给出行动结论，但存在预先登记的重开条件。
- `research_only`：保留作研究观察，不进入生产。
- `open`：尚待裁决，可以正常推进。
- `blocked`：有明确外部或数据依赖；必须写明 blocker 和解除条件。

`closed` 不等价于“方法族永久无效”。若只测试一个固定 EWMA 分母、一个固定 CUSUM 或一个固定 Hamilton 配置，台账只能关闭这些精确配置，最多将方法族标为低优先级。方法族级关账必须有单独 scope 和足够覆盖该主张的证据。

### 1.2 证据等级

证据等级至少区分：

- 不可变正式 run / archive；
- 已提交的正式 probe 及完整报告；
- 已提交但仅用于筛选的 prescreen；
- 探索性或诊断性结果。

状态与证据等级采用 fail-closed 约束。例如 `adopted` 或 `closed` 不能没有可定位证据；`provisional` 和 `blocked` 不能没有重开/解除条件；证据路径缺失或 supersede 关系成环时校验失败。

### 1.3 已知结论的准确表达

- `equal_weight + long-flat` 登记为 `adopted`。理由是更低回撤、更低换手和更合适的生产风险形态；同时明确 Sharpe 差异没有统计显著性，不能声称全面统计胜出。
- mapping 32 格搜索只关闭冻结网格及既定约束内的候选，不写成全局最优。
- divergence、fusion、pair-set、conditional、crowding、dual-channel、family-unification、tail、geometric、五轴等工作按各自真实 scope 登记，而不是用一句“全层关账”合并。
- B3 登记为 `provisional`：旧近似 PIT 统计 verdict 为 STOP，最终 verdict 为 `DATA_BLOCKED`。只有实际模型行真首披覆盖满足门槛且按冻结设计正式重跑后，才允许转成终局 `closed`。
- 5d20z 保持后续待明确的研究项，本阶段不提升为生产候选。

## 2. 文档清单与 README 渲染

台账同时维护决策型文档清单。配置范围内的每份研究结果文档必须满足其一：

1. 被某条研究记录引用为设计、执行报告或证据；
2. 被标为 superseded，并指向替代材料；
3. 被显式排除，并给出“非决策材料、执行日志或纯设计稿”等原因。

校验器对配置的研究结果目录和命名规则执行清单比对。基线建立后，新出现但未登记/排除的决策文档会使校验失败，避免再次静默遗漏。

`tools/research_registry.py` 提供至少两个只读/文档生成动作：

- `validate`：校验 schema、枚举、状态约束、路径、文档清单和依赖图；
- `render`：确定性生成 README 中有明确起止标记的研究状态表。

渲染结果包含状态、精确 scope、当前结论、关键 caveat、重开条件和权威证据链接。工具不得重排或覆盖 README 受控区块之外的人工说明。测试通过“重新渲染无 diff”阻止 YAML 与 README 漂移。

## 3. B3 真首披覆盖审计

新增只读 CLI，输入冻结 B3 builder 产出的 `monthly_exposures` 文件。它复用 `backtest.b3_eval.compute_true_disclosure_coverage` 的既有判定语义，不建立第二套覆盖口径。

### 3.1 审计总体口径

- 分母是实际参与 B3 模型计算、满足现有 B3 coverage 函数口径的模型记录，而不是 `stock_first_disclosure` 全表行数或全库财务事实行数。
- 分子是这些模型记录中 `true_first_disclosure_verified` 为真的记录。
- 与现有函数一致，只纳入 `universe_role=model`、形成月份为 2014-01 至 2023-12 的冻结发现/确认网格；`size_only` 行和窗口外行不进入分母。
- 覆盖主键为 PIT policy × formation month × ticker。B3 候选在后续评估层共享同一套 exposures，因此不得虚构 candidate 维度或重复计算分母。
- 总体 ratio、numerator 和 denominator 必须与既有函数逐项一致。
- 只有 denominator 大于零且 numerator 等于 denominator，才可输出 `coverage_ready=true`。
- 工具只证明覆盖准备度，不证明 B3 因子有效，也不生成或提升正式研究 verdict。

### 3.2 分解与产物

除总体结果外，报告按真实覆盖主键分解：

- PIT policy；
- formation month；
- PIT policy × formation month。

输出包括：

- 一个 schema 稳定、字段顺序固定的 JSON 摘要；
- 一个未覆盖记录明细文件；
- 输入文件 SHA-256、B3 配置 SHA-256、Git HEAD 和 tracked-worktree 状态；
- schema/version、生成时间、总体与分组统计、失败原因。

输出目录必须由用户显式指定；若目标已存在则拒绝覆盖。该产物属于 coverage audit，不自动进入 B3 正式 archive。

### 3.3 错误处理

以下情况 fail-closed，并以非零退出码结束：

- 输入不存在、不可读或格式不受支持；
- 必要字段缺失、布尔值不是现有函数接受的严格布尔类型或 denominator 为零；
- 模型主键重复或分组统计不能回加到总体；
- 既有 coverage 函数与审计重算结果不一致；
- 配置文件缺失或无法计算 provenance；
- 输出目录已存在，或写完后的 artifact 哈希无法复核。

失败可以保留明确标记的诊断信息，但不得留下貌似成功的 `coverage_ready=true` 摘要。

## 4. 陈旧措辞与未来 verdict

本阶段修正当前说明材料中的已知漂移：

- 已合并的 clean evidence revalidation 不再写成“分支尚未合并”；
- dual-channel 不再写成“待裁”；
- “现役稳健最优”限定在冻结网格、风险与换手约束内；
- 单一 EWMA/CUSUM/Hamilton 配置的失败不再写成整个生成器方法层已被证明无效；
- B3 索引明确同时展示统计 STOP、最终 `DATA_BLOCKED` 和终局关账条件。

未来第五桶和等比五桶 verdict 的 limitations/caveats 从相邻 build metadata 读取并结构化写出，不再硬编码“approximate-PIT”或固定的 2025-03-31 截止日。若必要 metadata 缺失或互相矛盾，runner fail-closed，而不是猜测当前覆盖状态。

已有不可变 verdict 保持原字节和哈希。台账通过 supersede、历史 caveat 和当前状态解释旧文字，不对历史证据进行就地修补。

## 5. 测试策略

实现采用失败测试优先：

1. 台账 schema、状态不变量、依赖图、路径及清单遗漏检测；
2. README 受控区块的确定性渲染、区块外内容保护和 no-diff 检查；
3. B3 覆盖审计的 100%、部分覆盖、零分母、缺列、重复主键、非法布尔值和分组回加失败；
4. 审计总体结果与现有 `compute_true_disclosure_coverage` 一致；
5. verdict metadata 缺失/冲突时失败，以及未来产物不再生成已知陈旧短语；
6. 既有不可变证据哈希不变，当前生产输出 CSV 不被工具触碰。

先运行 focused tests，再运行全量测试。任何全量失败都必须区分本次回归与既有环境问题，不以“多数通过”替代验收。

## 6. 完成标准

本阶段只有同时满足以下条件才算完成：

1. 所有已识别的决策型信号研究都已登记，或有可审计的排除理由；
2. 台账验证通过，README 可由台账确定性重建且无 diff；
3. long-flat、mapping、方法生成器和 B3 的措辞符合各自证据强度；
4. B3 审计工具通过合成与契约测试，并能读取合法的 B3 monthly exposures；
5. 未来第五桶/等比五桶 verdict 不再硬编码旧 PIT caveat；
6. focused tests 与全量测试通过；
7. 既有不可变运行产物及用户当前输出 CSV 均未改变。

完成本阶段仍不意味着 B3 终局关账。下一阶段的独立决策门为：在当前真首披底座上生成 B3 模型记录，先通过本设计的 coverage audit，再按冻结设计执行正式 B3 重跑并依据原判据裁决。
