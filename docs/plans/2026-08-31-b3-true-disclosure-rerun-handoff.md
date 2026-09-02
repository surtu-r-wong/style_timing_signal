# B3 真首披重跑续接记录

日期：2026-08-31。状态：日历审计根因已修复并提交；冻结 B3 builder 的真首披接入设计待用户确认 A 后实施。

## 1. 用户边界

- 只推进 B3；金时6号不立项。
- 不修改 B3 因子、参数、窗口、成本、统计门槛或裁决规则。
- 覆盖不足时只修数据/来源传播；只有 coverage ready=100% 才允许正式重跑。
- 用户明日新对话续接；builder 行为修改前需确认推荐方案 A。

## 2. 工作区与提交

- 主工作区：`/home/elfbob/claude-code/style_timing_signal`，`main` 在本轮开始为 `8cf22ec`；有8个用户信号 CSV 未提交修改，禁止覆盖或混入 B3 提交。
- 日历修复工作树：`/home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-calendar`
- 分支：`fix/b3-true-disclosure-calendar`
- 稳定提交：`12689cc fix(b3): align disclosure audit with frozen calendar`
- 冻结 B3 历史工作树（只读调查，未修改）：`/home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital`
- 冻结分支/tag：`fix/b3-wind-share-capital-tail` / `archive/b3-wind-share-capital-tail-20260814`，HEAD `41ed581`。该分支与 main 长期分叉，不得整分支合并；应从冻结 tag/HEAD 新建专用实现分支，再择取最小提交/证据。

## 3. 旧正式证据恢复与审计

- 完整 archive：`/home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz`
- 已复核大小：117,548,606 bytes。
- 已复核 SHA256：`a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843`。
- 本轮临时恢复：`/tmp/b3-formal-run-restore.D9cJNf/run-windows-formal/`。
- 旧 `monthly_exposures.csv.gz` SHA256：`8c3365508abef5cff12230da12ce4b8e1377f4df06c9a18f77b3af76d0c928db`，与 archive manifest 一致。

首次用 main 审计器运行时错误返回“legal_deadline 缺少必需月份”。根因不是 artifact 缺月，而是 main 的 coverage 函数硬编码 2014-01；冻结 B3 最终结构日历实际从 2014-10 开始。

`12689cc` 将 coverage 起止月集中为 `2014-10` 至 `2023-12`，由 `backtest.b3_eval.compute_true_disclosure_coverage` 与 `tools.audit_b3_disclosure_coverage` 共用。TDD 证据：修复前7项按预期失败；修复后聚焦24项通过，完整 B3 eval+audit `328 passed in 405.97s`。

修复后真实旧 artifact 审计输出：`/tmp/b3-coverage-audit-legacy-fixed-20260831/`：

- coverage basis：两个冻结 PIT policy、2014-10 至 2023-12；
- denominator：626,732；
- verified：0；
- ratio：0；
- coverage ready：false；
- 首月2014-10、末月2023-12均存在；返回码1（partial），不再是返回码2（invalid）。

## 4. 当前 PostgreSQL 真首披底座（全为只读查询）

表：`stock_selector.stock_first_disclosure`，键为 `(ts_code, end_date)`。

2003-01-01 至 2023-12-31：

- 总季度键259,323；未关联键0；
- `quality=ok` 且有有效首披日249,229（96.11%）；
- `quality=sentinel` 10,094（3.89%）；
- 时间范围2003-03-31至2023-12-31；5,824只股票。

旧 B3 artifact 的实际 model ticker 共3,855只。其中只有9只、16个季度键是 sentinel：

- 002206.SZ：2006Q3；首次模型月2014-10；
- 002817.SZ：2015Q3；首次模型月2020-11；
- 300573.SZ：2015Q3；首次模型月2020-11；
- 300817.SZ：2018Q3；首次模型月2023-10；
- 600018.SH：2003Q1/Q2/Q3、2004Q1/Q2/Q3、2005Q1、2006Q2；首次模型月2014-10；
- 600348.SH：2003Q2；首次模型月2014-10；
- 600449.SH：2003Q2；首次模型月2014-10；
- 603200.SH：2016Q1；首次模型月2021-04；
- 603980.SH：2016Q1；首次模型月2021-04。

这些 sentinel 均早于首次模型月超过12季成长依赖窗。因此按“实际进入 style_score 的事实依赖”传播 verified，新构建很可能达到100%；不能以此预测替代正式 build+audit。

## 5. 待确认设计（推荐 A）

### A：实际因子依赖级 provenance（推荐；与冻结 spec 一致）

1. `_fetch_raw_financial` 左连接 `stock_first_disclosure`，读取 `first_disclosure_date`、`quality` 并将该表加入 database evidence。
2. 对定期报告事实：有效 `quality=ok` 且首披日不早于期末时，两种 PIT policy 都使用真实首披日；sentinel 才分别回退原 `legal_deadline` / `legal_deadline_plus_one_month_end`。dividend 仍使用事件公告日，不套报告首披日。
3. 原始事实携带 strict boolean verified；TTM 的 verified 为该 TTM 实际差分依赖事实的 all；growth slope 为12个 TTM 窗的 all；equity/DP 等事件值使用自身依赖。
4. 每个模型行只汇总实际非空、参与 growth/value composite 的因子依赖。任一实际依赖未 verified 则该模型行 false；无关报表/字段/sentinel 不得制造假阻塞。
5. 不改变任何因子值公式、截面打分、篮子、执行或裁决参数。

### B：ticker/月全事实保守标记（不推荐）

只要已知事实中存在任一 sentinel 就将该 ticker/月标 false。实现较小，但会让未参与 style_score 的无关报表或字段制造假阻塞，不符合“实际进入 style_score 的每条事实”冻结定义。

### C：回退也记 verified（禁止）

会虚构100%真首披覆盖，违反数据合同。

用户明日只需回复“确认A”。在确认前不得修改冻结 B3 builder 生产代码。

## 6. 确认 A 后的执行顺序

1. 从冻结 B3 tag/HEAD 新建独立工作树/分支，不修改历史 B3 工作树。
2. 写失败测试：join/schema/日期守卫、两 policy 同真首披、sentinel 分别回退、TTM/斜率/事件 verified 传播、仅实际非空因子汇总、database evidence 包含首披表。
3. 最小实现并跑冻结 B3 全套测试。
4. 在新输出目录构建 2014-10至2023-12 exposures；不得覆盖旧 archive。
5. 用 main 的 `tools.audit_b3_disclosure_coverage` 审计新 `monthly_exposures.csv.gz`。
6. 若小于100%，只输出精确 `(policy, formation_date, ticker)` 缺口和依赖事实，不启动 structure/eval；若100%，冻结输入/配置/代码哈希。
7. 原样运行 states → structure → eval，按旧门槛出正式 verdict；保存新 immutable run，旧 archive 不改。
8. 更新研究注册表：coverage ready不等于因子有效；仅以正式 eval 原判据决定 `closed` 或继续 `provisional/data_blocked`。

## 7. 明日首条检查命令

```bash
git -C /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-calendar status -sb
git -C /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-calendar log -1 --oneline
git -C /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital status -sb
```

预期日历分支干净并位于 `12689cc`；冻结 B3 历史工作树保持未修改。
