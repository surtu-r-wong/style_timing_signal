# 实验权威索引

本页只回答“当前应读哪份规格、哪份 verdict/run”。历史文档继续保留，但被本表标为
superseded 的段落不得再作为当前裁决。P0 与此后新建的正式机器证据采用不可变 run：目录
只追加不覆盖，`manifest.json` 必须为 `status=complete`，输入、输出和日志由 manifest 的
SHA-256 清单追溯。

B3 formal evaluation 是 legacy archive exception：其 `core/backtest/run_manifest.json` 没有
`status` 字段；仓库内证据由冻结的 `inventory.json` 和
`tools/verify_b3_formal_archive.py` 对 10-file core 的校验建立，不套用 P0 manifest schema。

| Experiment | Status | Authoritative spec | Authoritative verdict/run | Superseded files | Reopen condition |
|---|---|---|---|---|---|
| Gate 0R | **PASS** (pass=true) | [当前数值锚/地板：data-foundation repair §7](2026-08-20-data-foundation-repair.md#7-锚重登记录2026-08-21用户裁决-a整体重登)；[r3 §4/§4.2b](2026-08-19-fifth-bucket-preregistration-r3.md#4-gate-0冻结) 仅保留未变的过程语义 | [2026-08-26 完整生产库重验 complete manifest](../../backtest/output/runs/20260826T183948-p0-revalidation-ac11b3c/manifest.json) | r3 §4.2b 的旧数值锚/地板及 execution record 旧 §2 Gate 0R 数值段由 immutable run 取代；r3 的过程语义与 Gate 0A/0B 均未被取代 | 规格或 DP 数据底座再次变更时，先冻结新锚/阈值并生成新的 complete immutable run |
| tail fifth-bucket | **STOP**（当前规格下增量不可辨认，不等于尾部无信息） | [fifth-bucket preregistration r3](2026-08-19-fifth-bucket-preregistration-r3.md) | [2026-08-26 完整生产库重验（STOP maintained）](../../backtest/output/runs/20260826T183948-p0-revalidation-ac11b3c/outputs/fifth_bucket_verdict.json) | [r1](2026-08-18-fifth-bucket-preregistration.md)、[r2 draft](2026-08-18-fifth-bucket-preregistration-r2-DRAFT.md) 均由 r3 取代；execution record 旧 §5.1“⓪ 机器判定结果”由 immutable run 取代 | 获得更长且更新鲜的 PIT 财务窗，或改变会压缩尾部影响的架构时，须先新预登记 |
| geometric five-bucket | **STOP**（当前规格下不可辨认，不等于重划无价值） | [geometric five-bucket preregistration](2026-08-19-geometric-5buckets-preregistration.md) | [2026-08-26 完整生产库重验（STOP maintained）](../../backtest/output/runs/20260826T183948-p0-revalidation-ac11b3c/outputs/geo5_verdict.json) | geometric verdict 旧“判定：情形② STOP”“读法与机制”“沉淀与方向状态”三节及文末旧“有效窗截至 2025-03-31”限定由 immutable run 取代 | 新候选须先说明如何逃出多重平均的钝化，并另立预登记 |
| axes entry ticket batch 1（低波/动量/流动性/股息） | **ALL_FAIL**（当前功效下不可辨认，不等于无增量信息） | [entry-ticket 设计冻结 §0–3](2026-08-24-new-rotation-axes-entry-ticket.md) | [执行记录 §4](2026-08-24-new-rotation-axes-entry-ticket.md#batch1-execution)；[complete manifest](../../backtest/output/runs/20260824T094843-axes-ticket-8d3e099/manifest.json)；[machine verdict](../../backtest/output/runs/20260824T094843-axes-ticket-8d3e099/outputs/axis_ticket_verdict.json) | — | 新证据或更长评窗（受 stock_indicator 2015 起点约束）；股息轴贴线读数（偏 IC 0.160/p 0.063、与现役近正交）只作排队参考，重开须新预登记 |
| axes entry ticket batch 2（质量） | **ALL_FAIL**（当前功效下不可辨认，不等于无增量信息） | [批次二增量设计 §1–3](2026-08-24-quality-axis-entry-ticket-b2.md)（母设计同左行） | [执行记录 §4](2026-08-24-quality-axis-entry-ticket-b2.md#b2-execution)；[complete manifest](../../backtest/output/runs/20260824T102455-axes-ticket-e2df789/manifest.json)；[machine verdict](../../backtest/output/runs/20260824T102455-axes-ticket-e2df789/outputs/axis_ticket_verdict.json) | — | 同批次一行；方向一 5 轴全族 0 过闸，重开任一轴须新证据/更长评窗+新预登记 |
| adaptive-bucket probe | research-only（无正式 verdict） | [classifier-swap argument §7.8](2026-08-20-classifier-swap-argument.md) | [committed legacy snapshot manifest](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/manifest.json)；[report](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/outputs/adaptive_bucket_compare_report.csv) | — | 先冻结正式规格、闸门和判定措辞，再生成 complete immutable run |
| mixed-ensemble probe | research-only（无正式 verdict） | [classifier-swap argument §7.9](2026-08-20-classifier-swap-argument.md) | [committed legacy snapshot manifest](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/manifest.json)；[report](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/outputs/mixed_ensemble_probe_report.csv) | — | 先冻结正式规格、闸门和判定措辞，再生成 complete immutable run |
| rotation-target probe | research-only（无正式 verdict） | [classifier-swap argument §7.5](2026-08-20-classifier-swap-argument.md) | [committed legacy snapshot manifest](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/manifest.json)；[output](../../backtest/output/runs/20260821T000000-legacy-pre-p0-2c17b32/outputs/rotation_target_probe.csv) | — | 先冻结正式规格、闸门和判定措辞，再生成 complete immutable run |
| B3 formal evaluation | archived formal evidence | [archive provenance and source tag](../../data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/README.md) | [run manifest](../../data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core/backtest/run_manifest.json)；[verdicts](../../data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core/backtest/verdicts.csv)；[retrieval boundaries](../../data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core/RETRIEVAL.md)；完整 32 文件仅在 host-local /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz，SHA-256 a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843 | — | 需审计完整树或重跑时，先按 archive README 校验 host-local tar；Git checkout 只含 10 文件 review core |

## 解释边界

- P0 权威 run 已由 2026-08-26 完整生产库重验取代：代码 commit `ac11b3c`、seed 0、run
  `20260826T183948-p0-revalidation-ac11b3c`；三锚 0.8022/0.7966/0.9698、pass=true，
  第五桶与 geometric 均 STOP maintained，整链输入无漂移且 git dirty=false。08-24 的 da4db0e run 作为前一版权威证据保留。
- （历史）DP 修复版 P0 权威 run 的代码 commit 为 3030109458a1499ca7601515625fd1ac6dafb025，seed 为 0；
  manifest 的跑后 provenance 补记不改变这两个运行参数。
- research-only 三行只指向已提交的 legacy snapshot；该 snapshot 记录
  observed_at_commit=2c17b32c4f1d9f156c8e0d26699bc5ee35fc930c，且明确不声称 generator commit，
  所以本索引不为它们补造正式 verdict。
- B3 仓库 archive 是可审阅的 10 文件 core，不是完整 formal-run；完整 tar 是 host-local、
  在 Git 之外，单独 checkout 无法恢复。
