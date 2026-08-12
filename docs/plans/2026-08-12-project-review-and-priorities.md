# style_timing_signal 全量项目盘点与优先级建议（2026-08-12）

> **本文为盘点与建议，优先级最终由用户裁决。** 草稿态交付，未 commit。
> 全程只读：未连数据库、未触执行机、未起停任何进程、未 push。
> 探测证据见 §8（每条附命令与原始输出）。引用外部数字时标注文件与位置。

---

## 0. 一句话结论（结论先行）

1. **项目最初目标已经交付并且没有变**：把三条风格价差信号做成"能日更、能定持仓口径"的东西，
   答案在 2026-07-10 就已定格为 **equal_weight + long-flat（Sharpe 1.62 / MaxDD −16.7%）**。
2. **但这条交付线的"最后一公里"是断的，且断因不是调度器**：上游 PG `index_daily` 的保鲜
   **一直活到 2026-08-11**（stock_selector 的 apscheduler `daily_index` 每周一至五 17:30 执行，
   抓的正是本项目的输入码，末次执行 08-11 17:30）；本机 07-06 起连续运行 36 天、08-11 18:46 关机、
   08-12 08:31 重启，调度器是**昨天关机才死的、今天尚未拉起**。
   三条信号 CSV 停在 **2026-07-08**、推荐持仓停在 **7 月 9 日**的真因是——
   **本仓库从来没有任何自动化**（`crontab` 空、无 user timer），三个脚本一直靠人手跑。
3. **过去一个月的产能约 89%~98% 投给了 B3**（07-12 后 main 90 个 commit 中 75 个是 b3，
   另有未合并分支 41 个；把为 B3 而建的 WSL2 基建计入则接近全部）。
4. **B3 已用四路统计 STOP 收场，且证据显示翻盘所需位移比任何已观测的数据口径效应大一个数量级**
   （10.05~57.39 倍）。因此 B3 三选项里，**A（关轴归档）是与全景一致的默认**，B 是可选的低成本证伪，
   **C 不属本项目预算**（价值主体在 stock_selector 的 PIT 底座）。
5. **关键结构事实**：本项目的三条生产信号**只读 `index_daily`**，不碰 `stock_financial` /
   `stock_share_capital`。也就是说，**过去一个月全部数据质量投入与全部执行机基建，一行都不服务于部署主线**，
   它们的唯一需求方是已 STOP 的研究轴。建议据此把数据质量线整体降级/移交。

---

## 1. 最初目标考证（带出处）

### 1.1 立项时要回答的问题

本仓库 `git init` 于 2026-07-02 的"整理"动作（`6b07639 baseline: 整理前原始状态快照（原目录名 20260325）`），
但研究本身从 2026-03 起。立项时的自我定义有三处原文：

- **项目定义**（`README.md:3`）：
  > 「基于中信风格指数与成长/价值指数配对的 A 股择时信号研究项目。三条活跃信号线 + 历史研究归档。」

- **整理动机与边界**（`docs/plans/2026-07-02-reorganization-design.md:5`）：
  > 「原目录 `20260325/`（已改名 `style_timing_signal/`）经过 3 月—7 月多轮研究，积累了 4 个信号版本家族、
  > 多份重复数据和 ~25 个散落的回测输出，需要整理后继续研究。**本次只做整理，不改变任何信号数值。**」

- **整理后待定的研究方向**（同文件 `:87`，"后续（本次不做）"）：
  > 「研究方向待定（**参数研究 / 三线统一回测对比 / 日常化流程**），整理完成后另起讨论。」

更早的单点设计（`docs/plans/2026-06-18-equal-weight-signal-design.md:3-5`）只有一个工程目标：
> 「Goal: Create a new script that reads `data.csv`, treats every two price columns after the date as one
> relative-strength pair, and outputs one 20-day raw signal value from equal-weighted pair factors.」

**综合还原的立项命题**：*这三条基于指数对价差的风格信号，怎么用来给 A 股宽基择时（多/空/空仓），
以及日常怎么把它跑出来。* 交付物 = ① 可日更的信号 CSV ② 一个有回测证据支撑的推荐持仓口径 ③ 能撑住这两者的评价框架。

### 1.2 后来演化出的主线（同样有出处）

07-03~07-09 的"三方向优化 initiative"把上面三条待定方向一次性做完，并给出定格答案：

- **主信号 = equal_weight**（`docs/plans/2026-07-10-optimization-roadmap-retrospective.md:74-77`）：
  > 「**equal_weight 保持生产主信号**——同秤挑战（自建篮子）、增量挑战（rotation/F1 两个真实信号被其覆盖）、
  > 新轴挑战（八轴偏 IC 全不显著）三重确认。」
- **部署口径 = long-flat**（`README.md:13-21`，07-11 勘误后口径）：
  long-flat 年化 26.4% / **Sharpe 1.62** / **MaxDD −16.7%** / Calmar 1.58；对称多空 1.41 / −29.3%；buy&hold 0.36 / −68.9%。
- **扩展方向的边界已画死**（retrospective `:20-24`）：
  > 「空头五轴 + 多头三轴共**八个观察面全数 STOP**——**库内零成本公开信息面，无一提供独立于 equal_weight
  > 生产信号的增量**。负结果是决策级产出：它把"还有什么值得做"的边界一次性画清了。」

**⚠️ 并且当时已经写明 B3 的归宿**（retrospective `:40-42`，本次盘点的关键出处）：
> 「③自建 B1→B2 完成，**B3（市值×风格双排序）被切主证伪自然终止**——纯风格定位改"风格测量仪"，
> **B3 失去目标非遗漏**」

---

## 2. 优先级总表

| # | 工作线 / 开放项 | 与最初目标 | 现状（本次实测/文档） | 建议 | 一句话理由 |
|---|---|---|---|---|---|
| 1 | **三线信号保鲜 + 推荐持仓** | **直接=交付物本身** | 停在 2026-07-08，`output/recommended/` 文件时间 7-9；**本仓库零自动化**（crontab 空、无 timer） | **P0** | 项目唯一的活产出，已停 35 天；不恢复则项目实质无产出 |
| 2 | 为三线脚本 + `backtest.production` 建自动化 | 直接（1 的根因） | 从来就没有过；上游 `index_daily` 反而一直保鲜到 08-11 | **P0** | 缺的是本仓库这一段，不是上游；靠人手跑必然再停 |
| 2b | 重启后拉起 stock_selector 调度器 | 直接（上游保鲜） | 08-11 18:46 关机后未再拉起（今日 08:31 重启） | **P0（小事，用户/stock_selector 侧）** | 一条命令的事，但不做上游从今天起也开始断 |
| 3 | 风格仪表盘 :8060 | 间接（展示层） | 未在跑（curl exit=7）；非常驻服务，按需手起 | **P2** | 展示层零新信号；数据源恢复后再起即可 |
| 4 | 研究轴 **B1**（自建篮子复现） | 间接（已完成） | ✅ 闸门过，ρ 0.88-0.91 | **CLOSE** | 目标达成、资产已沉淀（管线+因子层） |
| 5 | 研究轴 **B2**（行业中性分解） | 间接（已完成） | ✅ 分解完成；切主未过（0.98/1.01 < 1.39） | **CLOSE** | 已判"纯风格=测量仪非择时器"，无未决问题 |
| 6 | 研究轴 **B3**（市值×风格连续状态） | 间接（07-13 设计稿重立命题，见 §6.2） | 四路统计 STOP + `DATA_BLOCKED` | **STOP + 写明重开条件（选项 A）**，≠ #20 的"勿重开" | 翻盘位移比口径敏感度大 10~57 倍，闸门拓扑上无通路；但 approximate-PIT 负结果按设计稿预登记为 **provisional**，不得进"勿重开"清单 |
| 7 | B3 分支/产物处置（41 commit 未推未合 + 16 个未跟踪产物目录） | 运维 | 仅存在于本机 | **P1** | 一个月工作无异地备份，须先裁决再动 |
| 8 | 数据质量：share-capital par | 已脱钩（服务 B3） | ✅ 已闭环（−87.4%） | **CLOSE** | 已收官；剩余尾巴见 9/10 |
| 9 | 数据质量：SalG 5 票 | 已脱钩 | 最后 formation 仅 1 只 `000820.SZ` | **P2（挂起）** | 只洗溯源星号，受影响模型行 ≤0.13%，不改结论 |
| 10 | 数据质量：SHARES 57 尾巴 / CLOSE 202 行 | 已脱钩 | 56 只实际影响 / 14 只票 `UNEXPLAINED_EXACT_DATE_GAP` | **P2 → 移交 stock_selector** | 本项目生产线不消费这些表 |
| 11 | 数据质量：**首披日 PIT 底座**（B3 选项 C） | 已脱钩（价值主体在他项目） | 0/626,732 = 保守标注规则；底座 3,721,765 行 | **移交 stock_selector 立项** | 周级投入；为 B3 翻案期望回报极低，为全库 PIT 则另算 |
| 12 | 数据质量：ann_date 修复 | 已脱钩 | ✅ CSMAR 脏行清零（144+43）；Wind 132 行有意不修 | **CLOSE**（留一条口径问题给 stock_selector） | 已完成，无未决动作 |
| 13 | 基建：Windows 128G + WSL2 | 已脱钩（为 B3 而建） | ✅ 已验收，H9 锚生效可无人值守 | **P2 保留不投入** | B3 关轴后唯一需求方消失；仅维护 runbook |
| 14 | 基建：360 内核锁风暴 | 已脱钩 | 挂案待 IT，证据包已备 | **P2（用户自办）** | 只影响宿主 PS 路径，已有变通 |
| 15 | 开放项：**futures_daily 断更 → C1 复检** | 间接（八轴唯一登记复检项） | 两个 backfill 写入方仍在；`collector.py` **非**写入方（见 §5.2）；04-29 停 | **P1（market-monitor 侧）** | roadmap 唯一预先登记的复检闸门，且前提可能已具备 |
| 16 | 开放项：ETF 份额两字段 | 间接 | 待用户加 gateway config（未核） | **P2 挂起** | 面 6 是八轴之外的候补面，价值未验证 |
| 17 | 开放项：Tailscale 黑洞 | 运维 | `debian-server` active 但走 relay "nue"；:8000 → 200 | **P2 观察** | 当前链路可用，根因在 Debian 侧非本项目 |
| 18 | 开放项：07-02~08 缺口 | 运维 | 已拍板不回填 | **CLOSE** | 维持既有决策 |
| 19 | 风格测量仪并入日常输出（B2 岔路 3） | 间接 | `spread_U2*.csv` 停在 2026-07-01 | **P2**（若 1 恢复则顺带） | 轻量；单独立项不值 |
| 20 | rotation 短窗深挖（B2 岔路 2） | 已脱钩 | roadmap 已判 STOP（偏 IC 0.047, p=.60） | **CLOSE 勿重开** | 已在勿重开清单内 |

---

## 3. 分线详述

### 3.1 ① 部署主线（信号生产 / 调度 / 仪表盘）——**P0，断在最后一公里**

只读活性探测（命令与原始输出见 §8）：

| 探测 | 结果 | 判读 |
|---|---|---|
| 三线输出末行日期 | `equal_weight_signal_20d40z.csv` / `confirmed_signal.csv` / `citic_style_signal_40d.csv` **全部 2026-07-08** | 停更 35 天 |
| `output/recommended/` 文件时间 | 三份均 **Jul 9 11:15** | 推荐持仓未再生成 |
| **本仓库自动化** | `crontab -l` → `no crontab for elfbob`；`systemctl --user list-timers` 仅 snap/launchpad 两条无关 timer | **本仓库从来没有任何自动化** |
| **上游保鲜（stock_selector 调度器）** | `logs/scheduler_nohup.log`（mtime **08-11 17:30**）：apscheduler `daily_index`，`cron[mon-fri, 17:30]`，**末次执行 08-11 17:30**、下一次登记 08-12 17:30；日志内可见 `CI005917/18/19` 等本项目输入码与 `100.120.152.1:8080` | **上游一直活到 08-11**，不是长期失效 |
| 本机开关机 | `last -x`：07-06 08:56 起**连续运行 36 天**，**08-11 18:46 关机**，08-12 08:31 重启 | 调度器是**昨天关机才死**，今天尚未拉起 |
| `tmux ls` | 只有 `work`（08-12 08:42 创建） | 与上一行一致（重启后未重挂） |
| `curl :8060` | `000` / exit=7 | 仪表盘未启动（按设计是手动起，非 daemon） |
| `curl 100.65.111.79:8000` | **200** | Debian 侧 market-monitor API 活着 |
| `output/style_basket/spread_U2*.csv` 末行 | 2026-07-01 | 风格测量仪也停更 |
| `pytest --collect-only` | **1053 tests collected** | 测试基座健在（收官期为 190/209） |

**归因（这一条不要搞反）**：`index_daily` 的保鲜链路 **没有** 在 07 月中断——它由 stock_selector 侧的
apscheduler 每个工作日 17:30 跑，抓取作业一路执行到 08-11。**断的是本仓库这一段**：三条信号脚本与
`backtest.production` 的触发方式**只能是人手/交互式**（推断，但依据是硬的：本仓库内不存在任何
能自动写出这些 CSV 的东西——`crontab` 空、无 user timer、无 systemd unit、无自动化脚本），
07-09 之后没人跑，于是 CSV 停在 07-08。
retrospective `:142` 记的"tmux 不抗重启"是对的，但它描述的是 stock_selector 调度器的脆弱性，
**不是三条信号 CSV 停更的原因**——把两者混为一谈会把 P0 修到错的地方。

**建议 P0 动作（低成本、当天可完成）**：
1. **主修：为本仓库建自动化**——把 `tools/topup_index_daily.sh` → 三线重跑 → `python3 -m backtest.production`
   串成一个脚本，并**决定它跑在哪**（本机 systemd user unit + `WantedBy=default.target` 可抗重启；
   或挂进 stock_selector 那台已有的调度器；或明确接受"手动按需"并写进 README）。
2. **附带小事：把 stock_selector 调度器拉起来**——昨天关机后未再启动，不拉起则上游从今天起也开始断。
3. 补跑一次历史，把 07-08 至今的信号与推荐持仓续上。

理由：这是立项交付物本身；其余所有工作线都可以停，唯独这条停了项目就没有产出。
且真因是"零自动化"而非"调度器挂了"，所以只重挂调度器**不解决问题**。

### 3.2 ② 研究轴 B1 / B2 / B3

**B1（`docs/plans/2026-07-08-b1-style-basket-replication.md`）——闸门通过，CLOSE。**
自建成长−价值价差复现指数对信号：信号级 ρ **U0-U3 全部 0.88-0.91**（U2 最高 .906/.915），
> 「① 信号级 U0-U3 全部 0.88-0.91 > 0.8 预期 → **自建管线正确复现指数对信号轴**」（`:55-56`）

沉淀资产：`signals/common/factors.py`（PIT 纪律件 `pit_ttm_with_known` 等，23 测试）+ `signals/style_basket/`。无未决项。

**B2（`docs/plans/2026-07-08-b2-industry-neutral-decomposition.md`）——分解成立、切主未过，CLOSE。**
- 方差分解坐实"混合体"：U2 口径 v1 混合 13.3% ≈ v2 纯风格 7.7% + rotation 6.8%（`:23-24`）；
- IC：纯风格 **0.179** > 混合 0.126 > rotation 0.050（`:32-34`）；
- 但净值层三方同秤：equal_weight **1.39** > self_mixed 1.01 > pure_style 0.98（`:58-62`），结论：
  > 「**IC↑ 而净值↓** …纯风格是更好的"风格测量仪"，但宽基择时恰恰需要行业轮动成分」（`:66-69`）
- 三条岔路的归宿：(1) B3 → 见下；(2) rotation 短窗 → roadmap 已判 STOP，**勿重开**；
  (3) 测量仪并入日常输出 → **未做**，建议随 §3.1 恢复时顺带（表 #19）。

**B3——已实现、已正式跑、已裁决，建议 CLOSE。**
- 立项定位在 07-10 就被判"失去目标非遗漏"（retrospective `:40-42`），07-12 起被重开。
- 实现量（main 口径，`.py` only）：`signals/style_basket/` 6,510 行 + **`backtest/b3_eval.py` 4,892 行
  + `backtest/b3_structure.py` 2,960 行**（= 7,852 行纯 B3 评价/结构实现）≈ **14,362 行实现**；
  测试 `tests/test_b3_*.py` **6 个文件 15,374 行**（worktree 分支上为 16,581 行）；
  main 上 75 个 b3 commit + 分支上 41 个。
- 裁决（`docs/plans/2026-08-12-b3-formal-run-verdict-analysis.md`，双审已过；文件在 b3 worktree）：
  - **统计层四路全 STOP，由 `stability` 单闸过度决定**：6 条候选腿中 4 条早/晚期斜率余弦为**负**
    （−0.2018~−0.2831），过线需把秩相关从 −0.09~+0.30 抬到 ≥ +0.50（`:17-21`）；
  - **经济层独立否决**：2021-2023 确认窗 Sharpe **0.809~0.940 vs 基线 1.000931**，闸门还要 +0.10（`:22-24`）；
  - **翻盘所需位移 vs 已观测口径敏感度差 10.05~57.39 倍**，且 `B3_unified` 只有 qblend 一条腿、
    `B3_dual_target` 要求双腿全过——**"即使上述不能排除项全部翻正，仍没有任何一条可通过的路径"**（`:47-50`）；
  - `final_verdict` = `DATA_BLOCKED`（三 blocker），其中 `SALG_FRESHNESS` 实际足迹 = **1 只票 2 行**、
    `TRUE_DISCLOSURE_COVERAGE` 的 0/626,732 = **保守标注规则不是污染测量**（`:26-33`）。

### 3.3 ③ 数据质量线——**整体建议降级/移交**

先说结构事实（决定了预算归属）：**本项目三条生产信号只读 `index_daily`**（`README.md:32-38`、数据流图 `:82-92`），
`stock_financial` / `stock_share_capital` 只被 B1/B2/B3 的自建篮子消费。B3 关轴后，这条线**在本项目内没有需求方**。

| 项 | 现状（出处） | 建议 |
|---|---|---|
| share-capital par 两阶段标定 | 已闭环：prod 89,579 行；`DATA_MISSING_SHARES` 46,004→5,781 票·月（−87.4%）（`data_fixes/2026-07-25-share-capital-par/README.md`） | CLOSE |
| SHARES 尾巴 | **两套口径，勿混用**：① 07-28 审计（README `:129-136`）= 57 行汇总 / 实际 56 只 / **5,781 all · 5,445 required**；② **正式批现状**（verdict `§5.1`）= **all 6,656 / required 0 / affects_final 0**，`REPORT_ONLY`，**全部落在 2013-05~2013-12**，完全在结构日历（2014-10 起）之外 | 移交 stock_selector；**以正式批为现状**——与裁决无关 |
| CLOSE 缺口 | 同样两套：① 07-28 审计 = 202 all / 190 required / **14 只票**，全 `UNEXPLAINED_EXACT_DATE_GAP`、同日停牌证据为 0（README `:138-148`）；② **正式批现状**（verdict `§5.2`）= **all 232 / required 64 / affects_final 0**，`REPORT_ONLY`；其 15 个 required 月已走重要性豁免（max_share **0.04375%** ≪ 阈值 0.25%） | 移交（原文明示"不构成自动回填/豁免/剔除结论"）；**以正式批为现状** |
| SalG 5 票 | `000820.SZ`/`300431.SZ`/`600145.SH`/`600421.SH`/`600610.SH`；最后 formation 仅 1 只（verdict `:677`） | P2 挂起（仅当选 B 才做） |
| ann_date | CSMAR 144+43 行已修、**全表 CSMAR 脏行清零**；Wind 132 行查清后**有意不修**（HK 财年网格）（`data_fixes/2026-07-24-stock-financial-ann-date/README.md`） | CLOSE；留一条 HK 口径问题给 stock_selector（该 README `:33` 已留言） |
| 首披日 PIT 底座 | 分母 626,732 模型行，底层 `stock_financial` 本次消费 **3,721,765 行**；周级投入（verdict `:678,747-748`） | **移交 stock_selector 立项裁决**，不占本项目预算 |

### 3.4 ④ 基础设施线（Windows 128G + WSL2）——**已建成，建议保留不投入**

`docs/plans/2026-08-10-wsl2-runbook.md` 是收官交付物（486 行），环境已验收，且 **H9 抗断线锚 08-12 重启后三条验收全过**
（`:331-338`），长跑自此**无需值守**。

维护负担（照单列出，都是"人工不可省"的）：
- **宿主重启需人工登录 Wind**（`:98`）；重启窗口必须用户拍板；
- **H9 锚掉了只能让用户注销重登/重启**，远端补不上（`:345-350`，已实测是死路）；
- **360 内核锁风暴挂案待 IT**（`:356-369`）：PS/.NET 全族 19–70 秒起步、`*-NetFirewall*` 分钟级；
- 共存纪律：禁 `taskkill` 触及 Wind gateway / market monitor / B3 批；重负载前宿主空闲 ≥20 GB（`:466-473`）；
- 本机探 MTU 上限 1280（`:392`）。

**判断**：这套环境是为 B3 长跑而建的。B3 关轴后它没有需求方，但**沉没成本已付、维护成本≈0（不跑就不用管）**，
建议保留 runbook、不再投入建设，等下一个真需要 128G/长跑的任务再启用。

### 3.5 ⑤ repo 外开放项逐条核对

| 开放项（roadmap §6 挂账） | 本次核对结果 | 判定 |
|---|---|---|
| 日更调度器（07-10 tmux 重挂） | **活到 08-11**（`daily_index` 末次执行 08-11 17:30），08-11 18:46 关机后未再拉起；本机今日 08:31 才重启 | 需拉起（P0 #2b）；**但它不是信号停更的原因** |
| futures_daily 断更 → C1 复检 | **写入方指认需更正**：`collector.py` 中 `futures_daily` 出现 **0 次**（那是分钟级采集器）；实际写入方 = `data-collecter/backfill/backfill_optimized.py` 与 `backfill_multiasset.py`（两者均 `import WindPy`）。**新发现见 §5.2** | 待 market-monitor 侧，P1 |
| ETF 份额两字段 gateway config | 未核（属 gateway 配置，本仓库从不直连 gateway，`README.md:38`） | 待用户确认 |
| Tailscale 黑洞根因（Debian 侧） | `tailscale status`：`debian-server` **active 但走 relay "nue"**（非 direct）；`:8000` HTTP 200 | 当前可用，根因未解，P2 观察 |
| 07-02~08 `stock_daily_price` 缺口 | 维持不回填（既有拍板） | CLOSE |
| 仪表盘 :8060 | 未在跑 | 见 #3 |
| push / 分支 | `main` == `origin/main`（0/0，**已同步**）；`fix/b3-wind-share-capital-tail` **本地领先 41 commit、未推送**；worktree 内 **16 个未跟踪产物目录**（15 个 `run-windows-*` + `run/`） | P1，须用户裁决 |

---

## 4. B3 三选项在全景下的重新定位

用户方针是"深挖价值不高的一律降级"。把三选项放回全景后：

| 选项 | 成本 | 能回答什么 | 全景下的定位 | 建议 |
|---|---|---|---|---|
| **A 关轴归档（= STOP + 重开条件，非"勿重开"）** | 零算力，仅文档收尾 | 不再回答新问题；把"B3 在当前库内数据下不提供优于 equal_weight 的增量"沉淀为**provisional 负结果** | 与 07-10 八轴全 STOP 的方向一致；代价是 `final_verdict` 永停 `DATA_BLOCKED`、负结论带"数据未净化"星号——但星号的解释成本已被 verdict 分析 §4 一次性付清 | **默认推荐** |
| **B 修 SalG（5 票）后重跑** | 最低实质投入：5 票、最后 formation 仅 1 只，+ 一次重跑 | 只能拆掉 3 个 blocker 里的 1 个；**`final_verdict` 仍是 `DATA_BLOCKED`**，且几乎不可能改统计结论（受影响行 ≤0.13%） | 是一次"低成本证伪实验"，但**买到的东西是星号的一部分，不是结论** | **P2 挂起**；若用户对"数据脏"心存疑虑、且 stock_selector 侧顺手能补，可作为搭车项 |
| **C 全量首披日回填后重跑** | **周级**（Wind 逐 fact 首披日、入库、manifest、全链重跑、再双审） | 唯一能洗成干净裁决、唯一能在真实 PIT 方向观测敏感度的手段 | **价值主体不在本项目**——verdict `:755-759` 原文：「若这次回填的价值**主要**是为 B3 翻案，§4.3 的 10.05~57.39 倍差距说明**期望回报很低**；若其价值是**为整个 stock_selector 库建立真实 PIT 底座**…**这个权衡属于用户**」 | **移出本项目预算**，转为 stock_selector 的立项议题（见 §5.1 A2） |

**⚠️ 选项 A 的正确写法（设计稿已预登记，不能省）**：B3 的收尾必须写成 **"STOP + 重开条件"**，
**不是**"勿重开"。出处 = `docs/superpowers/specs/2026-07-13-b3-continuous-style-state-design.md`：
> 「在真实首披日覆盖完成前，可以生成明确标为 approximate-PIT 的 STOP 或 MEASURE_ONLY 研究结果；
> 任何本来会得到 PASS_SHADOW 的候选必须降为 DATA_BLOCKED。**approximate-PIT 的负结果不能写入"勿重开"清单**。」（`:142`）
> 「approximate-PIT 的 STOP/MEASURE_ONLY 必须标为 **provisional**，不进入"勿重开"清单。」（`:513`）

即：本轮 B3 的 STOP 是在近似 PIT 口径下得出的，按自己预先定的规矩只能记为 provisional。
**建议的重开条件（单一条）**：*若 stock_selector 完成真实首披日 PIT 底座（§5.1 A2），
则以单一真实口径重跑一次 B3，验证 §4 预测的"仍 STOP"*——除此之外不重开。
这与 §2 表 #20（rotation，已在 roadmap"勿重开清单"内）是两种不同的关闭强度，不要混用。

**一句话建议**：B3 走 A（STOP + 上述单一重开条件）；把 B 降级为"stock_selector 顺手则搭车"；
把 C 整体转出为 stock_selector 的 PIT 底座立项议题。

---

## 5. 跨线分工清单（可直接转发）

### 5.1 stock_selector 侧

**A1｜SalG 5 票营收 TTM 回填或明示豁免（低优先，可搭车）**
- 请求什么：`000820.SZ`、`300431.SZ`、`600145.SH`、`600421.SH`、`600610.SH` 的营收 TTM 在 B3 消费窗内补齐，
  或给出明示豁免标注；最后一个 formation 上实际只需处理 `000820.SZ` 1 只。
- 为什么：B3 `SALG_FRESHNESS` blocker 的全部足迹；B1 已知限制里"Wind 段营收 TTM 止 2025Q1（SalG 冻结）"的尾巴。
- 不做的后果：B3 的负结论永久带 `DATA_BLOCKED` 星号；**不影响任何统计结论**（受影响模型行 ≤0.13%）。

**A2｜真实首披日 PIT 底座——是否立项（本清单里最重要的一条）**
- 请求什么：由 stock_selector 决定是否立项"Wind 逐 fact 首披日回填"，替换 CSMAR 的
  `ann_date`=数据集批次日口径。量级参照：B3 本次消费的财务事实底座 **3,721,765 行**，对应 626,732 模型行。
- 为什么：CSMAR `ann_date` 是批次日不是首披日，这是**全库级 PIT 缺陷**，所有依赖 `stock_financial`
  的下游研究共享同一个星号；B3 只是第一个撞上它的消费方，不是唯一理由。
- 不做的后果：任何 PIT 敏感研究（因子回测、选股回溯）都无法给出"真实可得信息"口径的结论，
  只能继续用"法定披露上限"这类保守近似；B3 的 `DATA_BLOCKED` 也无法解除。
- **注意**：不要把这件事当成"为 B3 翻案"来立项——那样期望回报很低（差 10~57 倍）。

**A3｜SHARES 57 尾巴 / CLOSE 202 行 → 收编为 stock_selector 数据质量待办**
- 请求什么：把 `data_fixes/2026-07-25-share-capital-par/tail.csv`（57 行，实际 56 只）与
  CLOSE 202 行 / 14 只票（`000670.SZ` 28、`000155.SZ` 19、`000995.SZ` 19…）纳入其数据质量队列，
  按活跃度优先（2023-12 仍在池且全 128 月受影响者优先）。
- 为什么：本项目生产线不消费这两张表，但 stock_selector 的选股管线消费；且 CLOSE 202 行全是
  `UNEXPLAINED_EXACT_DATE_GAP`（同日停牌证据为 0、后续均有非空 close），属价格源缺口性质。
- 不做的后果：任何财务/股本派生研究的 coverage 闸门继续报 `DATA_MISSING_*`，逐次都要重新解释。

**A4｜Wind 132 行 HK 财年网格口径定义（已留言，仅需确认）**
- 请求什么：明确这些槽位行的**数值**属于哪个财报期（财年 H1 还是日历半年）。
- 为什么：`data_fixes/2026-07-24-stock-financial-ann-date/README.md:33` 已留言；这 132 行是 Wind 真值，
  本项目已决定不改写。
- 不做的后果：若 stock_selector 管线未来加 `ann_date ≥ end_date` 约束会直接撞墙。

### 5.2 market-monitor 侧

**B1｜futures_daily 日更复活（P1，且成本可能远低于原估）**
- **先更正写入方指认**（本项目侧原记录有误，照原样转发会让人去复活错的脚本）：
  retrospective `:142` 写的是「写入方已定位=market-monitor `data-collecter/collector.py`」，
  但实测 **`collector.py` 里 `futures_daily` 出现 0 次**（那是分钟级采集器）。
  实际引用 `futures_daily` 且 `import WindPy` 的是
  **`data-collecter/backfill/backfill_optimized.py`** 与 **`data-collecter/backfill/backfill_multiasset.py`**
  （另有 `backfill/check_missing_futures_daily.sql` 可用于核对缺口）。
- 请求什么：确认上述两个 backfill 脚本能否恢复为日更（或另议 Linux 侧走 gateway 的 IC/IM 轻量 topup）。
- **一个能降低成本的新线索**：B3 执行机 `DESKTOP-P7MGEIR` 上**本来就在跑 Wind 终端 + Wind 网关
  + market monitor 采集**（`docs/plans/2026-08-10-wsl2-runbook.md:15`、`:462-464`：「采集进程为 Console 会话下的
  多个 python.exe」）。**更硬的旁证**：stock_selector 的调度器直到 **08-11** 都在从该机
  `100.120.152.1:8080` 取数，且 `tailscale status` 显示该机 `active; direct`——
  即"Windows 桌面会话 + WindPy 可用"是**当前正在发生的事实**，不是推测。
  原记录里的阻塞条件（retrospective `:142` 原文：「复活待用户：**桌面起 collector**，
  或另议 Linux 侧走 gateway 的 IC/IM 轻量 topup」）在这台机器上疑似已经不成立。
- **一处需要 market-monitor 侧解释的张力**：该机的 market monitor 采集在跑、Wind 会话可用，
  `futures_daily` 却自 04-29 断更——这本身就佐证了"写入方另有其人"（即上面两个 backfill 脚本
  未被纳入日更），而不是"没有 Wind 环境"。
- 为什么：C1 基差率是 2026-07 八轴收官表里**唯一预先登记的复检项**（p=.090 / 净 Sharpe 0.51，
  样本被 IM 上市日 + futures_daily 断更锁短，k=40 仅 79 窗）。
- 不做的后果：C1 永远无法复检，八轴收官表留一个技术性未决；本项目侧无替代数据源
  （本仓库从不直连 gateway）。
- **纪律提醒**：该机器上禁止对 Wind gateway / market monitor / B3 批做任何 `taskkill`；
  重启宿主会中断 Wind 与采集且 **Wind 需人工登录**才恢复。

### 5.3 用户自办项

**C1｜本仓库信号自动化拍板 + 拉起调度器（P0，最紧急的一条）**
- 请求什么：**两件事，别只做第二件**。
  ① **主修**：决定三条信号脚本 + `backtest.production` 的自动化形态与落点——
  (a) 本机 systemd user unit（抗重启）/ (b) 挂进 stock_selector 那台已有的 apscheduler /
  (c) 明确接受"手动按需"并写进 README。**本仓库目前 `crontab` 为空、无任何 user timer，一直是人手跑。**
  ② **附带**：把 stock_selector 的调度器重新拉起来（08-11 18:46 关机后未启动）。
- 为什么：三条信号停在 2026-07-08、推荐持仓停在 7-9，而上游 `index_daily` **一直保鲜到 08-11**——
  说明缺的是本仓库这最后一段，不是上游。只做 ② 不能解决信号停更。
- 不做的后果：只做 ②，上游继续有数但信号 CSV 仍不动；两件都不做，则从今天起上游也一起断，
  long-flat 推荐持仓彻底失去时效，仪表盘即使起来也只显示一个月前的市场。

**C2｜B3 分支与产物处置（P1）**
- 请求什么：裁决 `fix/b3-wind-share-capital-tail`（**41 commit 未推送、未合并**）是合并进 main、
  单独推分支归档、还是就地封存；以及 worktree 内 **16 个未跟踪的产物目录**
  （15 个 `run-windows-*` + `run/`；其中 `run-windows-formal/` 是 verdict 分析的全部数字出处）
  留哪几个、留在哪里。
- 为什么：这一个月的工作目前**只存在于本机单盘**，无异地备份；而 verdict 分析引用的产物哈希需要可追溯。
- 不做的后果：磁盘故障或误清理即全部丢失，负结论将失去证据支撑（星号变成无法回答的质疑）。

**C3｜B3 三选项裁决（A/B/C）**
- 请求什么：在 §4 的 A / B / C 中选一个；若选 A，同时确认收尾写法为 **"STOP + provisional + 单一重开条件"**
  （而非"勿重开"），重开条件 = 真实首披日 PIT 底座建成后重跑一次。
- 为什么：三个 blocker 已定性、统计四路已 STOP，后续每一分投入的归属都取决于这一裁决；
  且设计稿 `:142/:513` 预登记了 approximate-PIT 负结果**必须**标 provisional，收尾写法不是自由选择。
- 不做的后果：B3 停在"跑完但没结论"的状态——代码与产物继续占着心智与磁盘，
  下一个接手者仍要重读 780 行 verdict 才能判断能不能碰它；§5.1 的 A1/A2 也无法定优先级。

**C4｜gateway config 加 ETF 份额两字段（低优先）**
- 请求什么：给 gateway config 加 `unit_fundshare_total` / `unit_floortrading` 两字段。
- 为什么：面 6（ETF 申赎）是八轴之外唯一"已核对完、只差配置"的候补面，回填后走 `run_families_probe` 即插即测。
- 不做的后果：该面无法测——考虑到同族八轴全 STOP，**建议维持挂起，不必特意去做**。

**C5｜360 内核锁风暴的 IT 升级（低优先）**
- 请求什么：把 `deploy_backups/2026-08-10-wsl2/evidence/diagnostics/it-escalation-package.md` 提给 IT。
- 为什么：宿主所有 PS/.NET 调用被单一内核串行点拖到 19–70 秒（`*-NetFirewall*` 分钟级）。
- 不做的后果：宿主侧运维永远要走"写 .ps1 → scp → 轮询结果文件"的变通路径（runbook 已固化，可长期忍受）。

---

## 6. 偏离检查：当前投入与最初目标的偏离度

### 6.1 投入 flow 的事实（可复核）

| 度量 | 数字 | 命令 |
|---|---|---|
| 全仓提交 | 162（2026-07: 153 / 2026-08: 9） | `git log --pretty='%ad' --date=format:'%Y-%m' \| sort \| uniq -c` |
| 07-12 之后 main 提交 | **90**，其中标题含 `b3` 的 **75（83%）** | `git log --oneline --since=2026-07-12 \| grep -ci b3` |
| B3 分支未合并提交 | **41** | `git rev-list --left-right --count main...fix/...` |
| **07-12→08-12 服务 B3 的提交占比** | **≥ 116 / 131 ≈ 89%（按标题含 b3 计的保守下界）**；把 15 条非 b3 标题提交中的 **13 条 B3 支撑提交**（6 WSL2 + 4 share-capital par + 3 B3 计划/基建）计入则 **129/131 ≈ 98%**（余下 2 条为 07-12 的年度集中度分解） | 上两行 + `git log --since=2026-07-12 \| grep -vi b3` |
| B3 实现体量 | `signals/style_basket/` 6,510 + `backtest/b3_eval.py` 4,892 + `backtest/b3_structure.py` 2,960 ≈ **14,362 行** | `wc -l` |
| B3 测试体量 | `tests/test_b3_*.py` **6 文件 15,374 行**（worktree 16,581） | `wc -l tests/*b3*` |
| 全仓测试数 | **1,053**（07-10 收官期为 190） | `pytest --collect-only` |
| 同期部署主线产出 | 信号末日 **2026-07-08**，推荐持仓文件时间 **7-9** | `tail -1` / `ls -la` |

**结论**：过去一个月，项目产能几乎全部（89% 保守下界，计入基建约 98%）流向 B3，部署主线**零维护**。
8 月的 9 个提交拆开看是 **3 个 B3（08-01/08-02 计划稿 + 08-11 `fix(b3)`）+ 6 个 WSL2 基建**——
**而 WSL2 基建本身是为 B3 长跑而建的支撑投入**（runbook `:15` 明写并存负载含"`D:\style_timing_signal` 上的 B3 原生批"），
所以"8 月 = 100% 服务 B3"。

### 6.2 沉没方向的定性

- **B3 的重开在程序上是合规的，别把它当成"偷跑"**。07-10 收官文档确实写过
  「**B3（市值×风格双排序）被切主证伪自然终止**…**B3 失去目标非遗漏**」（retrospective `:40-42`），
  但 **07-13 的设计稿正面推翻了这条终止理由**，且处于"用户已确认、design-complete"状态：
  > 「这个终止理由不成立。B2 检验的是"行业中性的纯风格读数能否直接替换生产信号"，
  > B3 检验的是独立命题：1. 风格效应是否随市值连续变化；2. size × style 交互是否包含现有指数对
  > 没有完整表达的信息；3. …是否具有不同的宽基择时含义。」
  > 「B1 已经提供了支持继续检验而不是终止 B3 的证据：U2 与 equal_weight 的信号相关约 0.91，
  > 而 U4 约 0.63，说明市值异质性不可忽略。」（`docs/superpowers/specs/2026-07-13-b3-continuous-style-state-design.md` §1）

  并且 retrospective §6 的"重开条件"表里**本来就没有 B3 这一行**，所以"未记录命中重开条件"
  这个指控不成立。**偏离的证据只有一条：提交统计**——一个月里 89%~98% 的产能进了一个研究轴，
  而这个轴的三条命题最终全部落空。这是**资源配置**问题，不是**流程合规**问题。
- **一个月后的结果与 07-08 B2 的判断方向完全一致**：自建路线的两步梯级（自建 vs 指数对、行业中性）
  在净值层都输给 equal_weight；B3 只是把同一结论在市值×风格双排序形态上又验证了一遍，
  且这次连结构闸都没过（stability 符号反向级失败）。
- **数据质量线与执行机基建是 B3 的下游沉没**：两者都不服务于只读 `index_daily` 的生产信号。
  它们的独立价值（PIT 底座、128G 长跑环境）确实存在，但**受益方分别是 stock_selector 和"未来某个长跑任务"**，
  不是本项目。

### 6.3 防再偏的三条建议（供用户采纳与否）

1. **重开一个已判终止的轴时，除了写清"为什么终止理由不成立"（B3 设计稿 §1 做到了），
   还要写"预算上限是多少、什么时候认输"**。本次缺的是后半句，不是前半句。
2. **给研究轴预设算力/时间上限与中止判据**（例如"两周内若结构闸仍不过则 STOP"）。
   B3 从 07-12 到 08-12 连续投入一个月，中途没有预设的中止点——而 `stability` 闸的
   符号反向级失败其实在早期结构阶段就可观测。
3. **部署主线设为不可挪用的 P0 预算**：任何研究轴开工前，先确认信号保鲜链路是活的。
   本次停更 35 天没有被任何人发现，是因为没有人在看它。

---

## 7. 我标注"待用户确认"的清单

| # | 待确认项 | 为什么我核不了 |
|---|---|---|
| 1 | `futures_daily` 现在是否仍断更 | 纪律要求不连数据库；文件层面两个 backfill 写入方与 `check_missing_futures_daily.sql` 都在 |
| 2 | 两个 backfill 写入方能否复用 `DESKTOP-P7MGEIR` 现有的 Wind 桌面会话（§5.2 新线索） | 需 market-monitor 侧确认 Console 会话与计划任务 Services 会话的共存可行性；且不得触碰该机进程 |
| 3 | ETF 份额两字段是否已加进 gateway config | 本仓库从不直连 gateway |
| 4 | Tailscale 黑洞根因是否仍未解 | 当前 `debian-server` 走 relay 可用、:8000 返回 200，看不出黑洞；根因在 Debian 侧 |
| 5 | B3 分支与 16 个未跟踪产物目录的处置 | 属用户决定（合并/归档/丢弃 + 留存策略） |
| 6 | 是否接受"数据质量线不属本项目预算"的划分 | 这是预算归属判断，需用户拍板 |
| 7 | stock_selector 是否愿意接 A1/A2/A3/A4 | 该项目当前有自己的活跃工作线（影子日、D-W1、M3） |

> 已从本清单**删除**的两项（QA 加测后已有确凿答案，无需用户确认）：
> ① `index_daily` 保鲜度——**调度器抓取作业执行至 08-11 17:30**（`scheduler_nohup.log`）；
>   PG 实际落库结果本轮**未核**（纪律不连库），但抓取侧一直在跑这一点已确凿；
> ② 调度器是否迁走——**没迁**，08-11 18:46 关机才停，今日重启后未拉起。

---

## 8. 本次探测证据（只读，可复核）

```
# —— 停摆归因（本轮补测，推翻了初稿的"调度器已死"叙述）——
$ last -x reboot | head -2   → Aug 12 08:31 still running / Jul 6 08:56 - 18:46 (36+09:50)
$ last -x shutdown | head -1 → shutdown Tue Aug 11 18:46 - 08:31 (13:44)
$ crontab -l                 → no crontab for elfbob          # 本仓库零自动化
$ systemctl --user list-timers → 仅 snap.firmware-updater / launchpadlib-cache-clean 两条无关
$ ls -la stock_selector/logs/scheduler_nohup.log → mtime Aug 11 17:30
$ grep 'daily_index' …scheduler_nohup.log | tail → cron[mon-fri,17:30]，末次执行 08-11 17:30，
                                                    下次登记 08-12 17:30（关机中断）
$ grep -o 'CI00591[0-9]' …scheduler_nohup.log | sort -u → CI005917 / CI005918 / CI005919（本项目输入码）
$ grep -o '100\.120\.152\.1:8080' …scheduler_nohup.log  → 命中（走 Windows 机 gateway）

# —— 其余 ——
$ tmux ls                        → work: 3 windows (created Wed Aug 12 08:42:35 2026)
$ curl -m5 -o/dev/null -w '%{http_code}' http://127.0.0.1:8060/   → 000 (exit 7, 连接被拒)
$ curl -m5 -o/dev/null -w '%{http_code}' http://100.65.111.79:8000/ → 200
$ tail -1 output/equal_weight/equal_weight_signal_20d40z.csv       → 2026-07-08,...
$ tail -1 output/hybrid20/confirmed_signal.csv                     → 2026-07-08,...
$ tail -1 output/citic40d/citic_style_signal_40d.csv               → 2026-07-08,-0.3618
$ tail -1 output/style_basket/spread_U2.csv                        → 2026-07-01,...
$ ls -la output/recommended/                                       → 三份均 Jul 9 11:15
$ tailscale status | head -6     → debian-server active; relay "nue"（非 direct）；
                                    desktop-p7mgeir(100.120.152.1) active; **direct**
$ grep -c futures_daily market-monitor/data-collecter/collector.py → 0   # 写入方不是它
$ grep -rl futures_daily …/data-collecter --include='*.py'         → backfill/backfill_optimized.py,
                                                                      backfill/backfill_multiasset.py（均含 WindPy）
$ git rev-list --left-right --count origin/main...main             → 0  0（main 已同步）
$ git rev-list --left-right --count main...fix/b3-wind-share-capital-tail → 7  41
$ git status --porcelain（b3 worktree）| grep -c '^??' → 16（15 个 run-windows-* + run/）
$ wc -l tests/*b3*        → 6 文件 15,374 行（初稿的 24,610 误含 __pycache__ 的 .pyc）
$ wc -l backtest/b3_eval.py backtest/b3_structure.py → 4,892 + 2,960 = 7,852
$ pytest tests/ -q --collect-only | tail -1                        → 1053 tests collected
$ git log --pretty='%ad' --date=format:'%Y-%m' | sort | uniq -c    → 2026-07: 153 / 2026-08: 9
$ git log --oneline --since=2026-07-12 | wc -l                     → 90（其中 grep -ci b3 = 75）
```

未执行：任何数据库连接、任何执行机操作、任何进程启停、任何 commit/push。

---

## 9. 用户裁决（2026-08-12）

本节由用户在读完 §0–§8 后拍板，覆盖本文档中一切"建议"字样。执行以本节为准。

### 9.1 优先级总表：**接受**

§2 的 20 行优先级表整体**接受**，不做行级改动。其中 P0 两行（#1 三线信号保鲜 + 推荐持仓、
#2 为三线脚本与 `backtest.production` 建自动化）合并为一个交付项，立即执行。

### 9.2 P0 落点：**本机 systemd user timer**

§3.1 建议 1 给了三个候选（本机 systemd user unit / 挂进 stock_selector 调度器 / 明示手动）。
裁决取**第一个**：

- 在本机（Ubuntu 开发机）建 **systemd user service + timer**，不挂 stock_selector 的 apscheduler
  ——理由是本仓库的信号链路不应依赖另一个项目的进程生命周期，且 systemd 抗重启（`Persistent=true`
  + `enable-linger`），正是 §6.3 建议 3 里"停 35 天没人发现"的直接对策。
- 触发时点 **工作日 18:30 Asia/Shanghai**，晚于上游 `daily_index` 的 17:30 抓取一小时。
- 链路顺序 = `tools/topup_index_daily.sh`（可优雅降级）→ 三条信号脚本 → `python3 -m backtest.production`。
- 必须带**新鲜度护栏**：跑完校验三条 CSV 末行日期是否追平 `index_daily` 最新交易日，超限即以非零码退出
  并在日志与状态文件里显式标记——不能再出现"链路静默停摆无人知"。
- 单元文件在仓库内留副本 + 安装说明，安装态放 `~/.config/systemd/user/`。
- 同批补跑 2026-07-09 以来的历史，把三条信号与 `output/recommended/` 续上（补跑前先备份并做零篡改比对）。

§3.1 建议 2（拉起 stock_selector 调度器）**归用户自办**，不属本批交付范围。

### 9.3 B3：**STOP + 写明重开条件**（§4 选项 A）

- 采纳**选项 A**：关轴归档，不再投入算力。
- 但按设计稿预登记，approximate-PIT 下的负结果是 **provisional**，因此 B3 **不进"勿重开"清单**
  （与 §2 第 20 行 rotation 短窗的 `CLOSE 勿重开` 性质不同，不要混同）。
- **重开条件（唯一）**：待 stock_selector 侧的**首披日 PIT 底座**建成后，用真 PIT 数据把 B3 的
  四路统计**重跑一次验证**。届时若闸门仍不过，则升级为终态 STOP；在此之前不得以其他理由重开。
- §6.3 建议 1/2 一并接受：任何重开必须同时写明预算上限与中止判据。
- B3 分支与未跟踪产物的处置（§2 第 7 行 P1）另行处理，本批不动。

### 9.4 跨线分工（§5）：由用户转发

§5.1（stock_selector 侧）、§5.2（market-monitor 侧）、§5.3（用户自办项）三份清单**照原样由用户
转发**给对应项目/自办，本项目不代为发起、不代为跟踪。§7 的 7 项"待用户确认"同此处理。

### 9.5 本节落地记录

P0 的实现与补跑在后续提交 `feat(deploy): automate daily signal chain with systemd timer` 中交付，
runner、单元文件与安装说明位于 `deploy/daily_signals/`。
