# 2026-08-26 完整生产库 P0 / 五轴正式重验执行与接续

状态：**已完成**。本记录供下一次新对话直接接续，不替代 `docs/plans/README.md` 权威索引。

## 1. 正式结果

- P0：`backtest/output/runs/20260826T183948-p0-revalidation-ac11b3c/`。
  manifest `status=complete`，运行代码 `ac11b3c`，seed 0，git `dirty=false`，
  `inputs_moved_in_window=false`，16 个工件 SHA-256 全部复核通过。
- Gate 0R：PASS；三锚为 0.8022 / 0.7966 / 0.9698。
- 第五桶：STOP maintained；Sharpe 差 -0.0079，p=0.5165。
- geometric five-bucket：STOP maintained；Sharpe 差 -0.2793，p=0.9780。
- 五轴批次一：`backtest/output/runs/20260826T222036-axes-ticket-ecf1907/`。
  低波、动量、流动性、股息均维持 FAIL，`flipped_axes=[]`；锚点通过、无输入漂移、
  git `dirty=false`，7 个工件哈希全部通过。
- 五轴批次二：`backtest/output/runs/20260826T223022-axes-ticket-06776ab/`。
  质量轴维持 FAIL，IC=0.1002、p=0.2349，`flipped_axes=[]`；锚点通过、无输入漂移、
  git `dirty=false`，7 个工件哈希全部通过。

结论：P0 权威 PASS、第五桶 STOP、geometric STOP、五轴 ALL_FAIL 均维持；
`docs/plans/README.md` 已指向本次完整生产库正式 run，无需追加用户裁决。

## 2. 本次固化

- `95c4405`：完整 porcelain / tracked-dirty provenance、全链输入漂移、轴基线比较与显式 seed。
- `253ac11`：仅对 PostgreSQL `OperationalError` 做 3 次、间隔 3 秒的有界重连。
- `ecf1907`：登记 clean P0 正式证据并更新 P0 权威索引。
- `06776ab`：登记五轴批次一证据。
- `4d967d1`：登记质量轴证据、五轴执行记录并更新五轴权威索引。
- 全量测试：1762 passed、18 warnings、367.40s；警告为既有 pandas FutureWarning。

## 3. 被正确拒绝的尝试

- `20260826T110842-p0-revalidation-95c4405`：Gate 0R-A 遇到瞬时 PostgreSQL 连接超时。
- `20260826T113305-p0-revalidation-6eb3d51`：Gate 0R-B1 遇到瞬时 PostgreSQL 连接超时。
- `20260826T125412-p0-revalidation-253ac11`：计算完成，但 18:30 日更在运行窗内写入
  `index_daily`，全链漂移检查将 manifest 标为 failed；未登记为证据。

这些目录已精确写入 `.gitignore` 并保留现场，没有删除或伪装成 complete。

## 4. 工作区与下一次对话

- 运行时分支为 `clean-evidence-revalidation`，该分支后来已合并到 `main`；这里保留分支名与
  当时的 `origin/main=91148cf` 仅作为历史 provenance。
- 2026-08-26 日更产生的 8 个 output CSV 已从 stash 恢复，保持为未提交修改；
  它们不属于正式重验提交，也没有污染任何 formal manifest。
- 本任务没有待裁决事项。下一次可先查看本文件、`docs/plans/README.md` 和 `git status`。
- 任务外仍有两项既有 backlog：`futures_daily` 补数需 Windows 端 futures fetcher；
  `run_daily_signals.sh` 的 topup DEGRADED 双登记尚未修复，均不影响本次 P0/五轴结论。
