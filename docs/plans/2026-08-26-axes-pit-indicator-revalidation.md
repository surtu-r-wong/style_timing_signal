# 五轴 PIT / indicator 回填后正式重验（跑前预登记）

日期：2026-08-26。状态：**冻结于运行前**。本次只重验既有五轴入场券，
不新增因子、不改构建口径、不改阈值。

## 1. 重验原因

- 两批原正式 run 均早于首披日 PIT 升级及 `stock_indicator` 历史回填。
- PIT A/B 已显示等比腿影响很小，但 indicator 回填可能改变样本空间与股息轴；
  股息旧值 p=0.063 贴近 0.05，故旧结论降为待重验。
- 本次读取生产数据库，所有 SQL 路径保持只读。

## 2. 冻结参数

- 数据截止：`2026-08-24`，两批构建命令均显式传 `--end 2026-08-24`。
- 评窗、腿构造、canonical 方向、目标与控制变量：逐字继承 2026-08-24 两份预登记。
- 主判读：blend / k=20 / 控现役 ew / 双侧置换 p<0.05。
- `n_perm=2000`，`seed=0`；Bonferroni 仅作参考，不改逐轴判定。
- 未过闸措辞仍为「当前功效下不可辨认」，不得改写为「无增量信息」。

## 3. 两批与基线

1. 批次一：`lowvol,momentum,liquidity,dividend`。旧基线：
   `backtest/output/runs/20260824T094843-axes-ticket-8d3e099/`。
2. 批次二：`quality`。旧基线：
   `backtest/output/runs/20260824T102455-axes-ticket-e2df789/`。

每个新 run 必须生成 `comparison.json`，至少保存 old/new：
腿文件 SHA-256、partial IC、置换 p、h1/h2 点估计与逐轴 pass。

## 4. 可登记条件

- Git tracked worktree 在 run 目录创建前为 clean；manifest 保存完整 porcelain。
- manifest `status=complete`，两步日志和四份输出齐全且哈希可复核。
- 整条链记录数据库起始服务器时间与 `updated_at` 水位；截至 2026-08-24 的
  输入在运行窗口内无写入，`inputs_moved_in_window=false`。
- 两锚均通过。任一条件不满足，该 run 只能记 failed/interrupted，不构成证据。

## 5. 跑后裁决规则

- 五轴逐轴沿用原 pass 规则，禁止根据新读数改阈值或补做选择性检验。
- 若五轴 pass 均与旧基线一致，则更新权威索引并登记数值变化。
- 若任一关键 pass 翻转、锚失败或出现窗口内输入漂移，停止更新生产结论，
  保留完整 run 并请求用户裁决。
