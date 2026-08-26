# Clean Evidence Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改研究口径的前提下，用生产数据库生成一套可审计、输入无漂移的 P0 与五轴正式重验结果，并更新权威索引。

**Architecture:** 复用现有 P0 和 axis runner，只在共享 manifest 层增强 Git provenance，在两个入口各自记录数据库起始写入水位并在整条链结束后复核。五轴显式冻结 `2026-08-24`、现有 seed/permutation 与判断阈值；正式结果携带基线差异，不建立新的通用执行框架。

**Tech Stack:** Python 3、pytest、PostgreSQL、Git、现有 `backtest` runners。

---

## Task 1: Git provenance 与数据库运行窗口

**Files:**
- Modify: `backtest/run_manifest.py`
- Test: `tests/test_bt_run_manifest.py`

- [ ] 先写失败测试：`git_state()` 除 `commit/dirty` 外保存完整 porcelain 条目，并区分 `tracked_dirty`；数据库起始快照包含服务器时间、cutoff 和各表 `updated_at` 水位。
- [ ] 运行 `pytest tests/test_bt_run_manifest.py -q`，确认新增测试先失败。
- [ ] 最小实现上述字段与快照 helper；保持现有 manifest 字段兼容。
- [ ] 重跑该测试文件并通过。

## Task 2: P0 全链输入漂移保护

**Files:**
- Modify: `backtest/p0_revalidation.py`
- Test: `tests/test_bt_p0_revalidation.py`

- [ ] 写失败测试：在创建 run 目录前捕获 Git/数据库状态；tracked worktree 非干净时拒绝正式运行；末端发现终止日前数据在运行窗口内被写入时将 manifest 标为 failed。
- [ ] 运行目标测试确认红灯。
- [ ] 复用 `DEFAULT_INPUT_CONTRACT` 和现有 `input_drift_report()`，为完整 P0 链保存 start/end/drift；不改五个既有命令、终止日和 Gate 0R 锚。
- [ ] 重跑目标测试并通过。

## Task 3: 五轴冻结参数、全链漂移与基线对比

**Files:**
- Modify: `backtest/axis_ticket_runner.py`
- Modify: `backtest/axis_entry_ticket.py`（仅在需要显式传 seed 时）
- Test: `tests/test_bt_axis_rotation.py`

- [ ] 写失败测试：runner 命令显式带 `--end 2026-08-24`；Git/数据库状态在 run 目录创建前捕获；输入漂移使 run failed；提供 baseline 时生成 `comparison.json`。
- [ ] 对比内容仅包含判定所需项：腿文件哈希、partial IC、permutation p、前后半窗与 verdict 的 old/new 值。
- [ ] 最小实现 `--end`、`--baseline-run` 和比较文件；沿用 seed=0、原 permutation 数与阈值，不改变轴构造或统计方法。
- [ ] 重跑目标测试并通过。

## Task 4: 预登记与中断目录处置

**Files:**
- Add: `docs/plans/2026-08-26-axes-pit-indicator-revalidation.md`
- Modify: `.gitignore`

- [ ] 预登记两批轴、冻结截止日、旧基线目录、seed/permutation、通过规则，以及「任一关键 verdict 翻转则停止更新生产结论并请用户裁决」。
- [ ] 对三个已核查的 interrupted run 添加精确 ignore 条目；不删除、不泛化忽略未来失败 run。
- [ ] 用 `git check-ignore -v` 和 `git status --short` 验证：旧目录仍保留，tracked worktree 干净。

## Task 5: 实现验证

- [ ] 运行三个目标测试文件。
- [ ] 运行全量 pytest。
- [ ] 检查 `git diff --check`，提交实现与预登记。

## Task 6: 生产数据库正式重跑

**Files produced:**
- `backtest/output/runs/<new-p0-run>/`
- `backtest/output/runs/<new-axis-batch-1>/`
- `backtest/output/runs/<new-axis-batch-2>/`

- [ ] 在干净提交上、日更完成后的安静窗口启动 P0。runner 必须生成 `status=complete`、`pass=true`、新锚 `0.8022/0.7966/0.9698`、完整 manifest 与 `input_drift.moved_in_window=false`。
- [ ] 正式运行 lowvol/momentum/liquidity/dividend，截止 `2026-08-24`，基线 `20260824T094843-axes-ticket-8d3e099`。
- [ ] 正式运行 quality，截止 `2026-08-24`，基线 `20260824T101433-axes-ticket-8d3e099`。
- [ ] 验证每个 run 的 manifest、artifact hash、数据库只读与 comparison。若关键 verdict 翻转，停止后续生产结论更新并报告。

## Task 7: 固化权威证据

**Files:**
- Modify: `docs/plans/README.md`
- Modify: relevant P0/axis conclusion documents only where run IDs or status must change

- [ ] 在结论维持时，将 P0 和五轴权威索引切到新 complete runs，并删除「锚重登待裁决」过期措辞。
- [ ] 运行 artifact/manifest 校验、相关测试及最终 `git status`/`git diff --check`。
- [ ] 提交正式证据和文档，不改信号逻辑、阈值或生产仓位。
