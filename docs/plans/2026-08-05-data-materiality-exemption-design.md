# B3 数据缺口重要性豁免设计

- 日期：2026-08-05
- 状态：用户已确认，design-complete
- 范围：B3 preflight 的 `DATA_*` 月级阻断口径
- 不改：组合构造、成本、PIT 口径、裁决门限、`backtest/production.py`

## 1. 问题

B3 的 `monthly_exposure` 是全有全无的 fail-closed：`b3_exposures.py::_eligibility_masks`
里只要当月任一只票带 `DATA_*` 排除原因就 `raise DataBlocked`，**不看缺口大小**。

这个口径在缺口 20% 时（决策 B 之前，2023-12 单月 680 只）完全正确。但缺口收敛之后
它开始产生失衡：截至本设计，B3 的 120 个 required 月全部阻断，`b3_eval` 从未跑到
统计阶段，而实际缺口是每月 2–5 只票。

**关键观察：这个 raise 不提供数据保护。** 缺数据的票在 raise 之前就已经被打标、
被排除出 `size_eligible`、被 `audit_exclusions` 记进 `coverage_audit.csv`。撤掉 raise
不会让任何一只缺失票进入计算，只会让其余的票能被测量。同一函数已经接受
`LISTED_LT_180D` 这类非数据排除而照常出结果（`b3_exposures.py:230`），可见
"带着排除票测量"本就是被认可的模式，差别只在原因类别。

## 2. 实测尺度

上一轮真实 preflight（`output/style_basket/b3/coverage_audit.csv`，128 个 formation 月）：

| 口径 | 每月中位 | 峰值 |
|---|---|---|
| size-eligible 池 | 3,266 | 4,849（最小 2,211） |
| `DATA_MISSING_SHARES` | 44 | 55（占 1.36%–2.04%） |
| `DATA_MISSING_CLOSE` | 2 | 5（占 0.057%–0.183%） |

阈值扫描（两个 pit_policy 结果一致）：

```
阈值 0.25%  应用 Wind 股本回填后 → 仍阻断 0 个月
阈值 1.00%  不应用 Wind 回填     → 仍阻断 120 个月
```

**股本尾巴在任何合理阈值下都过不去。** 因此事实回填是必需的，闸门只负责吸收
剩余的 2–5 只真洞与退市边界票。二者不是替代关系。

## 3. 判定

在 `_eligibility_masks` 的 raise 之前插入，按 `pit_policy × formation_date` 独立判定：

```
data_excluded = 带 DATA_* 排除原因的票数
measurable    = size_eligible + data_excluded
share         = data_excluded / measurable

share > threshold → 照旧 raise DataBlocked（行为逐字节不变）
share ≤ threshold → 放行，返回豁免记录
```

分母取"可测量宇宙"而非整个快照：快照掺了 `LISTED_LT_180D` 这类正常排除，
用它做分母会人为稀释缺口占比。

`threshold` 默认 `0.0025`，**放在 B3 config 而非代码常量**——`config_hash(cfg)`
已经写进 preflight manifest 与 run_manifest，所以调阈值会改变裁决的配置指纹，
事后可查。阈值设 0 即恢复今天的行为。

## 4. 记账

三层，缺一不可：

1. `ExposureResult.diagnostics` 带该月豁免明细 → 流入 `exposure_diagnostics.csv`；
2. `b3_build` 为豁免月写审计行 `check=monthly_exposure`、
   `status=MEASURE_WITH_EXCLUSION`、`reason_code=DATA_MATERIALITY_EXEMPTION`，
   **但不调 `add_blocker`**。这是解锁机理：`preflight.blockers` 保持空 →
   `b3_eval` 的 `invalid_formation_months` 为空（该字段完全派生自 blockers，
   见 `b3_eval.py:2159`）→ eval 进入统计阶段；
3. preflight stage manifest 增加 `exemptions` 段，`b3_eval` 读取后在 run_manifest
   输出 `materiality_exemptions: {threshold, months, max_share}`。

月份状态是**降级而非抹平**：`MEASURE_WITH_EXCLUSION ≠ OK`，读裁决的人看得见。
`coverage_audit.csv` 继续逐票列出被排除的名字与原因。

连带修改：`data_fixes/2026-08-01-b3-wind-share-capital/verify_post_write.py` 当前
断言 required 月的 `monthly_exposure` 行状态必须为 `OK`，需放行
`MEASURE_WITH_EXCLUSION` 并把豁免月数报进最终判定，否则收口验证自相矛盾。

## 5. 对结论可信度的影响

0.25% 在实际池子里 = 最小池 5 只、最大池 12 只。组合口径为每腿至少 100 只、
单票上限 1%。最不利假设下（豁免票全部本应落进同一腿的极端市值档），该腿仍有
≥100 只，且任何单票原本也拿不到超过 1% 权重，无法改变暴露方向。

残余偏差方向需如实记录：停牌带旧价已吸收 13,475 票·月，剩余 CLOSE 缺口是真洞与
退市边界，**系统性偏向困境小盘**，因此 B3 会轻微低估困境微盘。上界为 0.25% 的
名义票数，实际更小——这些票本就没有市值可参与加权。相对于 B3 要测的跨 120 月
结构效应，0.06% 的定向缺失不在同一数量级。

## 6. 残余风险

1. **余量 1.4 倍**：最差月 0.183% 对阈值 0.25%。缺口若增长会重新阻断。这是
   预期行为，不是缺陷——但这条闸不是一劳永逸。
2. **纪律位置转移**：改完之后挡住 B3 的只剩统计证据本身。防滥用的手段从闸门
   转移到 `config_hash` 指纹与月级降级的可见性上。

## 7. 测试

- 阈值之下、恰好相等、之上三档，`_eligibility_masks` 行为分别为放行/放行/raise；
- 混合多种 `DATA_*` 原因时按合计判定；
- `measurable` 为 0 的退化情形 fail-closed；
- 默认阈值就是 0.0025 的守卫（防止静默放宽）；
- 豁免月产出 exposures 且**不产生 blocker**；超阈值月仍然阻断；
- run_manifest 带 `materiality_exemptions`，`invalid_formation_months` 为空；
- `verify_post_write.py` 接受 `MEASURE_WITH_EXCLUSION`，仍拒绝 `DATA_BLOCKED`。
