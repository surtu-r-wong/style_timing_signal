# P0 实验证据重验与不可变归档设计

**日期：** 2026-08-21
**状态：** 已获用户批准，待实施计划
**范围：** Gate 0R、尾部第五桶、等比五桶、现有未跟踪研究证据、B3 formal-run 核心证据

## 1. 背景与问题

2026-08-20 的数据底座修复确认：此前所有 Gate 0 构建中的 D/P 因子恒为 0，价值得分实际缺少一腿。Gate 0A、0B、0R 随后按修复后的因子重跑，但 2026-08-19 生成的尾部第五桶与等比五桶输入序列没有重建，两个正式 STOP verdict 因而仍代表旧规格。

与此同时，Gate 0 使用同名 JSON 覆盖了不同 run，形成“JSON 为旧地板下 `pass=false`，文档按新地板演算为 PASS”的冲突；9 个研究 CSV 仍未被 Git 跟踪；B3 formal-run 的核心 verdict、manifest 和摘要仍主要留在独立 worktree。

本批次只恢复实验可信度与证据链，不修改生产信号、回测引擎、统计闸门或部署口径。

## 2. 目标

1. 保存所有现存旧产物，使修复前结果永久可追溯。
2. 使用 DP 修复后的当前代码与数据重新运行 Gate 0R、尾部第五桶和等比五桶正式实验。
3. 所有新 run 写入不可变目录，不覆盖任何旧结果。
4. 为每个 run 固化命令、seed、Git commit、输入哈希、数据截止日和产物哈希。
5. 生成修复前后差异报告，并仅依据新机器产物更新权威结论。
6. 将 B3 formal-run 的核心审计证据收回主仓；大体积中间文件采用带哈希的外部归档策略。

## 3. 非目标

- 不修改 `backtest.engine` 的成交时点、窗口边界、NaN 或 carry 口径。
- 不改变任何既有阈值、seed、样本窗、候选定义或 STOP/GO 规则。
- 不修改三条生产信号及 systemd 日更链。
- 不因新结果自动改变生产配置；任何翻转只进入裁决报告。
- 不建设通用实验平台，只实现本批所需的最小不可变 run 能力。

## 4. 方案选择

采用“不可变 run 归档”方案。直接覆盖现有文件会继续制造版本污染；先建设完整实验平台则超出本批范围。

目录约定：

```text
backtest/output/runs/
  <run-id>/
    manifest.json
    command.log
    inputs/
    outputs/
    verdict.json
    comparison.json
```

`run-id` 使用 `YYYYMMDDTHHMMSS-<experiment>-<short-sha>`。已存在的旧平铺产物不会移动或删除；首次实施时把需保全的旧研究 CSV 复制到一个 `legacy-snapshot` run，并记录原路径与哈希。

## 5. 组件设计

### 5.1 Run manifest

新增一个小型 manifest 模块，职责仅包括：

- 创建唯一 run 目录并拒绝覆盖；
- 计算文件 SHA-256；
- 记录仓库 commit、dirty 状态、完整命令、seed、开始/结束时间；
- 记录输入文件、输出文件、数据库表截止日和运行状态；
- 以原子写入方式生成 `manifest.json`。

manifest 不负责统计计算，也不推断 PASS/STOP。

### 5.2 实验脚本输出隔离

为以下 CLI 增加显式 `--output-dir` 或等价参数，默认值保持现状以保证兼容：

- `backtest.gate0_runner`
- `backtest.tail_pair_runner`
- `backtest.fifth_bucket_formal`
- `backtest.geometric_pairs_runner`
- `backtest.geometric_5b_formal`

正式重验必须传入 run 目录。判定脚本还必须显式接收其输入 CSV 路径，防止误读平铺目录里的旧输入。

### 5.3 P0 重验编排

新增单用途编排入口，按固定顺序运行：

1. Gate 0R；
2. 尾部对重建；
3. 尾部第五桶正式判定；
4. 等比五桶重建；
5. 等比五桶正式判定；
6. 生成旧 run 与新 run 的差异报告。

每一步失败即停止后续步骤，但保留已生成日志、状态和 manifest。编排器不得改数据库；所有数据库连接保持只读。

### 5.4 旧证据保全

当前 9 个未跟踪研究 CSV 进入 legacy snapshot：

- `adaptive_bucket_compare_{report,paired}.csv`
- `gate0a_result_series.csv`
- `gate0b_result_series.csv`
- `geo5_pairs_daily.csv`
- `mixed_ensemble_probe_{report,paired,yearly}.csv`
- `rotation_target_probe.csv`

普通运行日志默认由 `backtest/output/*.log` 忽略。若日志被正式文档引用，则复制到对应 run 或 `data_fixes` 证据目录并记录哈希；原文件暂不删除。

### 5.5 B3 证据归档

先对独立 B3 worktree 的 formal-run 做只读盘点。主仓必须保存：

- 最终 verdict/receipt；
- run manifest；
- 核心指标与 blocker 摘要；
- 输入与大文件的 SHA-256 清单；
- 来源 branch、commit、worktree 路径和生成命令。

单文件不超过 10 MiB、核心证据总量不超过 50 MiB 时直接纳入主仓。超过阈值的矩阵、压缩 CSV 和 bootstrap 样本写入外部压缩归档，主仓保存相对清单、大小、哈希和恢复说明。不得只保存一个不含未跟踪产物的 Git bundle。

## 6. 数据流与裁决纪律

```text
旧平铺产物 ──哈希/复制──> legacy snapshot
当前代码与只读数据库 ──> 新 run 输入序列 ──> 正式 verdict
legacy verdict + 新 verdict ──> comparison.json/报告 ──> 文档裁决
```

新结果产生前，旧文档不改判定。新结果完成后：

- Gate 0R 的机器 `pass` 必须与当前登记锚、地板一致；
- 第五桶与等比五桶各自保留旧、新 verdict；
- 若结论不变，文档写明“DP 修复后重验，结论维持”并更新数字；
- 若结论翻转，文档标记旧结论被新规格取代，但不自动改生产；
- 比较报告不得只比较 headline，至少包括 Sharpe 差、p 值、分窗、仓位分歧、MaxDD、换手和样本日期。

## 7. 错误处理与安全

- run 目录已存在时立即失败，禁止覆盖。
- 输入文件缺失、哈希变化、数据库截止日无法取得或结果出现 NaN 时 fail-closed。
- 正式判定只接受同一 run 内生成并经 manifest 登记的输入。
- 长跑中断时保留 `status=failed/interrupted`，续跑必须创建新 run-id。
- 不删除现有 19 个未跟踪文件，不重写历史 Git 记录，不修改外部数据库。

## 8. 测试与验证

实施采用 TDD，至少覆盖：

1. run-id 唯一且目录不可覆盖；
2. manifest 的 SHA-256、Git commit、命令、seed 和状态字段完整；
3. 原子写入失败不留下伪完成 manifest；
4. 五个 CLI 的默认路径保持兼容，显式输出目录时无平铺目录写入；
5. 正式判定拒绝读取 run 外输入；
6. 编排步骤顺序、失败即停和 interrupted 状态；
7. comparison 对相同/翻转 verdict 的输出；
8. 目标测试、全部 `tests/`、静态检查；
9. 实跑后核对 manifest 哈希，并从新 run 目录独立重读 verdict。

## 9. 验收标准

- fresh checkout 能从主仓定位每个权威结论对应的 commit、命令、输入、输出和哈希。
- Gate 0R 新 JSON 的机器判定与文档一致。
- DP 修复后的尾部第五桶、等比五桶均有独立新 run 和新裁决。
- 旧 run 未被覆盖，修复前后差异有机器可读报告。
- 9 个研究 CSV 不再处于无归属的未跟踪状态。
- B3 核心 verdict/manifest/摘要在主仓可审计，大文件有可验证恢复路径。
- 生产信号文件与日更部署在本批前后字节不变。

## 10. 实施分批

1. 不可变 run/manifest 基础设施与测试。
2. 五个实验 CLI 的输出隔离与测试。
3. 旧证据 snapshot 与日志政策。
4. Gate 0R、第五桶、等比五桶顺序重验及差异报告。
5. B3 formal-run 证据归档。
6. 权威文档、README 和实验索引收口。
