# B3 真首披正式重跑（r4）裁决记录

日期：2026-09-02（Asia/Shanghai）。执行依据：`docs/plans/2026-08-31-b3-true-disclosure-rerun-handoff.md` §6 第 5～8 步。
证据核：`data_fixes/2026-09-02-b3-true-disclosure-formal/`（`inventory.json` + `tools/verify_b3_formal_archive.py` 校验 OK）。
对照基线：2026-08-12 Windows 正式跑（`data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/`，代码 `5e7a902`），其裁决分析见 `docs/plans/2026-08-12-b3-formal-run-verdict-analysis.md`。

## 0. 一句话结论

**真首披覆盖 629,556 / 629,556（100%）的正式重跑，统计 verdict 仍为 STOP（四路全 `STRUCTURE_GATE_FAILED`），最终 verdict 仍为 DATA_BLOCKED，run blocker 由三条减为一条（仅剩 `SALG_FRESHNESS`）。** 08-12 分析的核心预测成立：修数据能洗掉 blocker，洗不掉 STOP。登记表条目 `b3-continuous-style-state` 继续 `provisional / data_blocked`。

## 1. 运行与证据链

| 项 | 值 |
|---|---|
| 代码 | `013f3bc`（main 祖先；含真首披 provenance 线 + 日历拆分 + Jan-01 balance 剔除） |
| config_hash | `33e7f69f…`，与 08-12 相同 |
| 主机 | WSL2 `/home/ghls/b3_runs/20260902_jan01_pit_r4` |
| A 段 `--stage states` | 15:39→16:06，exit 0，峰值 RSS 20.5 GB，四 manifest 零 blocker |
| 曝露一致性 | `monthly_exposures.csv.gz` 等五件与 r3 逐字节同哈希（`0e7fbc4c…`） |
| 独立审计 | main `cda5b2b` 上 `tools.audit_b3_disclosure_coverage`：629,556 / 629,556，`coverage_ready=true` |
| structure | exit 0，38 s |
| eval | exit 2（fail-closed 允许值），10 s；`run_manifest.true_first_disclosure_coverage.ratio = 1.0` |

过程记录：首次 eval 尝试 2 秒退出 `STRUCTURE_PROVENANCE_MISSING`，原因是 runner 脚本漏了 structure 步（交接第 7 条明写 states → structure → eval）；补 `run_jan01_r4_structure_eval.sh` 后完成。首次尝试的回执保留在 run 目录内，未写任何 backtest 产物。

## 2. 裁决全景（与 08-12 逐闸对比）

verdicts 96 行 vs 08-12 的 97 行：少的 1 行是 `run/blocker TRUE_DISCLOSURE_COVERAGE`（已消失）。状态变化的闸门共 6 处，其余 90 处逐行相同：

| scope | subject / gate | 08-12 | r4 |
|---|---|---|---|
| run | `TRUE_DISCLOSURE_COVERAGE` blocker | DATA_BLOCKED（0 / 626,732） | **不存在**（629,556 / 629,556） |
| run | `PIT_POLICY_FLIP` | DATA_BLOCKED | **PASS** |
| run | `final_verdict` | DATA_BLOCKED（`MULTIPLE_RUN_BLOCKERS`） | DATA_BLOCKED（`SALG_FRESHNESS` 单条） |
| structure / legal_deadline | q500 · 2015-2017 · `state_coverage` | FAIL | PASS（DIV 份额 0.0996 → 0.1010） |
| structure / +1M | dual_target 聚合 · 2021-2023 · `partial_ic` | FAIL | PASS |
| structure / +1M | q1000 · 2021-2023 · `partial_ic` | FAIL | PASS（0.1379 → 0.1904） |
| production / +1M | dual_target · 2021-2023 · `partial_ic` | FAIL | PASS |

**没有变化的是决定性闸门**：
- bootstrap 四路 `holm_adjusted_tail = 1.0`、`structure_pass = false`，与 08-12 逐位相同（仍是哨兵值，未真跑）。
- `stability`（2021-2023 早/晚期斜率一致性）四路六腿全 FAIL：q500 余弦 −0.286、qblend −0.265（符号仍反向），q1000 +0.175 但确认秩相关 0.297 < 0.50。真首披把 q500/qblend 的余弦推得更负（08-12 为 −0.202 / −0.283 / −0.260）。
- `sharpe_improvement` 四路全 FAIL：2021-2023 候选 Sharpe 0.838（unified）/ 0.931（dual_target） vs 基线 1.0009，闸门要求 +0.10。dual_target 在 legal_deadline 口径下从 0.809 抬到 0.931，仍差 0.17。
- `B3_unified` 的 `partial_ic`（structure 与 production 两层）仍 FAIL；`post_im_sharpe_difference`、`post_im_partial_ic` 仍 FAIL。

## 3. 两个 PIT 口径的收敛

真首披资产接入后，两口径在多数腿上的数值完全一致（例如 q1000 2021-2023 `partial_ic` 两口径同为 0.1904，`stability` 余弦同为 +0.175），因为法定截止日只在缺首披日时才作回退。08-12 的 `PIT_POLICY_FLIP` 分歧点（q500 2015-2017 `state_coverage`、q1000 2021 `partial_ic` 符号）随之消失。这也说明 08-12 用「两口径位移」作口径敏感度尺度的推理在真首披下不再适用；但决定性缺口（`stability` 需余弦跨零 + 秩相关 ≥ 0.50）比任何已观测位移仍大一个数量级，结论不变。

## 4. 剩余 blocker

`SALG_FRESHNESS`：`salg_valid_through = 2020-04-30 < 2023-12-31`。足迹与 08-12 一致，只有 000820.SZ 一只票——CSMAR 里它 2020-03-31 与 2021-03-31 两条利润表事实是空 JSON，一季度收入缺失使 TTM 断 4 季、12 季斜率窗再断 11 季。08-12 时同类票 5 只，回填后只剩这一只。修法是 Wind 回填这两个季度（用户决定）；修后只需按冻结规格重跑 structure → eval 即可得到干净的最终 verdict。**现有证据不支持洗净后 STOP 会翻 GO**。

## 5. 登记表处理

- `b3-continuous-style-state`：`provisional / data_blocked` 不变；claim 改为真首披下的 STOP + 单 blocker；reopen 条件收窄为「回填 000820.SZ 两季利润表后仅重跑 structure → eval」；证据指向 r4 核。
- 旧 08-12 archive 不改、不删。
