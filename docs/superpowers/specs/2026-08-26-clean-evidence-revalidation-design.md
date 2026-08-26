# Clean Evidence Revalidation Design

日期：2026-08-26。用户批准按最小方案执行：修正正式 run 的 provenance 与整链输入漂移检查，随后在冻结窗口内重跑 P0 和五轴。全过程只读生产数据库，不修改外部表。

## 目标

1. 生成“当前代码 + 新锚 + 静默输入”下 `pass=true`、`status=complete` 的 P0 权威 run。
2. 在原窗口、原 seed、原置换次数和原判据下重验低波、动量、流动性、股息、质量五轴。
3. 让正式 run 能区分既有工作区杂项、本次预期输出目录和真正的源码污染，并能拒绝运行期间分析窗内输入被改写的证据。
4. 更新实验权威索引，明确 supersede 污染、移动输入或旧数据底座下的 run。

## 非目标

- 不导出生产数据库的全量原始快照。
- 不改变任何研究阈值、窗口、seed、置换次数、信号或生产仓位。
- 不修 Windows gateway；`futures_daily` 继续作为独立外部阻塞项。
- 不删除 interrupted run。它们迁出活跃正式证据路径或被精确忽略，并保留原 manifest/日志。

## 设计

### 1. Run provenance

正式 runner 在创建新 run 目录前捕获：

- `HEAD` commit；
- 完整 `git status --porcelain` 条目；
- 从状态中分离 tracked 改动与已登记的本地 interrupted-run 路径。

正式运行要求 tracked worktree 干净。manifest 保存原始 porcelain 条目，而不只保存 `dirty: bool`。本次新建 run 目录不反向污染 run-start 状态。

现有三个 interrupted run 保留，但移入明确的本地失败归档策略，使后续正式 run 不再天然 `dirty=true`。

### 2. 整链输入漂移

复用 `backtest.run_manifest` 现有 `updated_at` 写入标记和分析窗行数检查，不另造第二套机制。

- P0：在五步命令开始前拍一次输入标记，在全部步骤与 verdict 校验后检查一次；窗口终点沿现有冻结终点。
- 五轴：在批次构建前拍标记，在判定完成后检查；窗口终点固定为 `2026-08-24`。
- 若运行期间存在分析窗内写入，manifest 标记 `failed`，保留产物但不得进入权威索引。
- 窗口外正常日更不阻断正式 run。

### 3. P0 重验证

使用当前 HEAD、新锚 `0.8022 / 0.7966 / 0.9698`、现有冻结终点、seed 0 和现有五步命令。验收条件：

- Gate 0R `pass=true`；
- 尾部第五桶与等比五桶 verdict 合法；
- manifest `status=complete`；
- run-start tracked worktree 干净；
- 整链 `inputs_moved_in_window=false`；
- manifest 的全部 artifact 哈希可复核。

### 4. 五轴同窗重验证

先登记一页执行预登记，冻结：评估终点 `2026-08-24`、`EVAL_START=2015-08-15`、主目标 blend、k=20、n_perm=2000、seed=0、双侧 p<0.05，Bonferroni 仅作参考不改判。

批次一仍为低波/动量/流动性/股息，批次二仍为质量。新 run 必须输出旧/新逐轴差异，至少包括腿名单/腿收益哈希、partial IC、p 值、半窗和最终 verdict。

若结论维持，更新权威索引并保留旧 run 为历史证据；若翻转，只报告并等待用户裁决，不自动改生产信号。

## 错误处理

- run-id 已存在、tracked worktree 不干净、必要输入缺失、非有限数、分析窗输入漂移或 artifact 校验失败：fail-closed。
- 失败/中断 run 只追加、不覆盖；保留 manifest、日志和已生成产物。
- 数据库连接始终只读，runner 不包含写库命令。

## 验证

- 对 Git 状态分类、创建目录前快照、P0/五轴整链漂移分别写失败优先的单元测试。
- 运行相关 focused tests，再运行全量 `pytest tests/ -q`。
- 正式 run 完成后独立重算 artifact SHA-256，并从 run 目录重新读取 verdict。
- 最后更新 `docs/plans/README.md`，不得继续指向已知污染 run，也不得保留“锚重登待裁决”的过期措辞。
