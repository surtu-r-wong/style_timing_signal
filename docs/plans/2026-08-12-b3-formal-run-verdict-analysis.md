# B3 正式跑裁决失败分析（数字化复盘，供裁决用）

> **产物来源**：`data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal/`
> （Windows 执行机正式跑，哈希已双审核验，与执行机逐字节一致）。
> **code_commit**：`5e7a9025a97674034584923772b8d3f0479bb32c`。
> **config_hash**：`33e7f69ff47e84e9ac92cabee2aa4f2fd1c19850e3d1a27d42a375a63c6b2c61`。
> **本文档不改变任何裁决**，不写产物、不重跑、不推翻既有结论；唯一目的是把
> "为什么 STOP、离过线多远、修数据能不能翻盘"量化到可裁决的精度。
> 每个数字均标注文件与定位（行号/列名/筛选条件）。闸门阈值出处为设计稿
> `docs/superpowers/plans/2026-07-14-b3-continuous-style-state.md`（下称"spec"）。
> 日历语义出处为 `docs/superpowers/plans/2026-08-11-b3-state-calendar-handoff.md`（下称"交接稿"）。

---

## 0. 一句话结论

**统计层 STOP 由 `stability`（早/晚期斜率一致性）单闸即已过度决定，且这一闸的
失败在多数腿上是"符号反向"级而非"差一点"级——6 条候选腿中 **4 条**（q500、
qblend × 两口径）早/晚期斜率余弦为负（−0.2018 ~ −0.2831），另 2 条 q1000 腿
余弦为正（+0.2077 / +0.0946）但秩相关不足；要过线需要把秩相关从
−0.09~+0.30 抬到 ≥ +0.50。**
与此并列的是经济层的硬事实：**B3 在 2021-2023 确认窗不是"没跑赢"，而是
系统性跑输 equal_weight 基线**（Sharpe 0.809~0.940 vs 基线 1.000931，
闸门还要求 **+0.10**，即需要 +0.16 ~ +0.29 的 Sharpe 位移）。

三个 run blocker 与这个 STOP **在证据上基本独立**：
- `SALG_FRESHNESS` 的实际足迹是**最后一个 formation 上的 1 只票（2 行）**，
  确认窗内受影响的模型行占比 ≤0.13%；
- `TRUE_DISCLOSURE_COVERAGE` 的 0/626,732 是**一条"沾 CSMAR 即判未核验"的
  保守标注规则**（spec:1039-1042），不是 PIT 污染程度的测量值；
- 唯一能把"数据口径"和"闸门失败"放在同一把秤上比的经验尺度，是两个 PIT
  口径之间的位移；在失败最狠的 `stability` 上，这个尺度比"过线所需位移"
  **小一个数量级**（0.010~0.053 vs 0.535~0.588）。

因此：**修 SalG 与回填首披日，能把 `final_verdict` 从 `DATA_BLOCKED` 洗成干净
裁决，但现有证据不支持"洗完之后 STOP 会变成 GO"**。

需如实列全的是：用现有产物**无法排除翻转**的项并不止 q1000 一条腿。完整清单
（逐条对照见 §4.3）为——**q500** 的 `state_coverage`（差 1 个交易日，且在 +1M
口径下**已实际翻转**）与 `m1_increment` IC 腿（缺 0.0095/0.0162 vs 敏感度
0.0062，同阶）；**qblend** 的 `partial_ic` 2022 年符号（缺 0.0992 vs 敏感度
0.102，同阶）；**q1000** 的 `stability` 秩相关（缺 0.197 vs 敏感度 0.186，同阶）
与 `partial_ic` 2021 年符号（跨零 0.0291，敏感度 0.0685 已实际跨零）；
以及组合层的 `sharpe_improvement`（缺 0.161~0.292 vs 敏感度 0.131）与
`post_im_sharpe_difference`（缺 0.185~0.556 vs 敏感度 0.371）。

**但这不改变结论**：真正把四路钉死的是 `stability` 在 **q500 与 qblend** 上的
缺口（+0.535 / +0.565，且需余弦跨零），与已观测口径敏感度相差 **10.05~57.39 倍**；
而 `B3_unified` 只有 qblend 一条腿、`B3_dual_target` 要求 q500/q1000 **双腿全过**
——**即使上述"不能排除"项全部翻正，仍没有任何一条可通过的路径**。

---

## 1. 裁决全景（三层）

出处：`backtest/verdicts.csv`（98 行 = 表头 1 + **数据行 97**）、
`backtest/run_manifest.json`。

| 层 | 主体 | 结果 | 出处 |
|---|---|---|---|
| candidate | 4 路（2 PIT 口径 × 2 候选） | 全部 `STOP` / `STRUCTURE_GATE_FAILED` | verdicts.csv:16-19 |
| family | `B3` | `STOP` / `HEADLINE_CANDIDATES_FAILED` | verdicts.csv:20 |
| run | `ALL` | `DATA_BLOCKED` / `MULTIPLE_RUN_BLOCKERS` | verdicts.csv:38 |

`run_manifest.json` 同步记录 `family_statistical_verdict=STOP`、
`final_verdict=DATA_BLOCKED`、四路 `candidate_statistical_verdicts` 全 `STOP`。

> **口径提示（scope 分布不对称）**：按 `scope` 前缀统计，
> `bootstrap`(2/2)、`boundary`(5/5)、`candidate`(2/2)、`production`(8/8)、
> `structure`(29/29) 在两个 PIT 口径下**完全对称**，唯独 `family` **只有
> `family/legal_deadline` 一行，没有 `+one_month_end` 的对应行**。
> 即 family 级裁决只对主口径发出。解读两口径对照时须注意：
> +1M 口径的"family 结论"并不存在于产物中，只能由其 candidate 行推出
> （两个候选均 STOP）。

三个 run blocker（verdicts.csv:37,39,40）：

| blocker | 详情原文 | 出处 |
|---|---|---|
| `PIT_POLICY_FLIP` | `PIT policy verdict directions disagree` | verdicts.csv:37 |
| `SALG_FRESHNESS` | `SalG valid-through 2020-04-30 is earlier than requested data_end 2023-12-31` | verdicts.csv:39 |
| `TRUE_DISCLOSURE_COVERAGE` | `true disclosure coverage is 0/626732 (0.000000); full explicit coverage required` | verdicts.csv:40 |

**关键语义**：`statistical_verdict` 与 `final_verdict` 是两条独立的账。
spec:45 早已写明这三类 blocker"即使补齐历史 q1000 标定数据也会保持
`final_verdict=DATA_BLOCKED`，而依法计算的近似 PIT `statistical_verdict`
仍然可报"。也就是说，**STOP 是本次跑出来的科学结论，DATA_BLOCKED 是叠在
上面的溯源卫生标记**——后者不削弱前者，前者也不依赖后者被解除。

日历语义（交接稿:27-35「Frozen semantics」节）：结构日历自 2014-10 起、模型日历自 2015-01 起、
hard-sort 证据 `n=110`、确认模型证据 `n=35`；每个 PIT 口径 128 个 formation
月中 **111 个为 required**（coverage_audit.csv 实测，见 §5）。

---

## 2. STOP 的解剖（逐闸门）

### 2.1 bootstrap：**不是测量值，是哨兵值**

`backtest/bootstrap.csv`（4 行数据）四路全同：

```
tail_prob=1.0, holm_adjusted_tail=1.0, ci05/ci50/ci95=空, structure_pass=False, gate_pass=False
```

阈值：`bootstrap.adjusted_tail_max: 0.10`（spec:197），
`gate_pass = passes_tail_gate(holm_adjusted_tail, 0.10)`（spec:3401）。

**必须澄清**：spec:3394 规定"**把结构失败的候选的原始 `tail_prob` 置为 1.0**"，
spec:3504-3506 复述"结构失败者获得原始 tail 1 与空区间"。四行 `structure_pass`
均为 `False`、`ci05/ci50/ci95` 全空 → **本次 bootstrap 根本没有真正执行重抽样**。

> ⚠️ 因此"离通过线多远 = 1.0 − 0.10 = 0.90"这句话**没有统计含义**。
> bootstrap 闸不构成独立证据，它 100% 是 structure 闸的下游转发。
> 任何"B3 的 bootstrap 尾概率极差"的表述都是误读。
> 本次跑**没有产出**关于 B3 收益显著性的重抽样证据。
>
> **防误读补充**：`bootstrap.csv` 中 `draws=5000`、`block_days=20`、
> `seed=20260713` **照常写入**（它们来自 spec:193-196 的配置，是配置回声
> 而非执行证据）。判断"是否真的抽过样"的唯一可靠标志是
> `ci05/ci50/ci95` 三列——本次**全为空**。

### 2.2 structure：候选四闸

阈值出处：spec:3069-3071（gate 语义）、spec:177-179（数值）。

- `m1_increment`：确认窗 M1 的 OOS R² > M0 **且** M1 Spearman IC ≥ M0。
- `partial_ic`：确认窗合并 partial IC > 0 **且** 至少 2 个自然年为正。
- `stability`：`cosine(early_slopes, late_slopes) > 0` **且**
  `score_spearman ≥ 0.50`（`stability_score_spearman_min`）——
  判定式见 spec:2991-2996（四个条件同时成立才 `passed`）。
- `state_coverage`：三个窗口 × 三种状态（UU/DD/DIV）份额均 ≥ `0.10`
  （`state_min_coverage`）——判定式见 spec:3014-3020（逐窗逐状态
  `passed &= share >= minimum`）。

候选口径：`qblend → B3_unified`；`q500` 与 `q1000` 是 `B3_dual_target`
的两条**强制腿**，双腿全过才算候选通过（spec:3060, 3073）。

#### (a) `stability`——**全灭，且多为符号反向**

`backtest/model_comparison.csv`，筛 `gate_name=='stability'`（10 行，全部 `gate_pass=False`）：

| PIT 口径 | 腿 | `cosine_early_late` | `confirmation_score_spearman` | 需 ≥0.50，缺口 | 失败性质 |
|---|---|---|---|---|---|
| legal_deadline | q1000 | **+0.2077** | 0.3028 | −0.197 | 仅秩相关不足 |
| legal_deadline | q500 | **−0.2018** | −0.0345 | −0.535 | **余弦为负 + 秩相关为负** |
| legal_deadline | qblend | **−0.2831** | −0.0652 | −0.565 | **余弦为负 + 秩相关为负** |
| +one_month_end | q1000 | **+0.0946** | 0.1170 | −0.383 | 仅秩相关不足 |
| +one_month_end | q500 | **−0.2604** | −0.0877 | −0.588 | **余弦为负 + 秩相关为负** |
| +one_month_end | qblend | **−0.2607** | −0.0553 | −0.555 | **余弦为负 + 秩相关为负** |

（另有 4 行 `candidate=B3_dual_target/B3_unified` 的聚合行，指标为空、
`gate_pass=False`，是双腿聚合结论。）

**6 条腿中 4 条（q500、qblend × 2 口径）的早/晚期斜率向量余弦为负**——
即模型在发现期（2015-2020）前半段与后半段拟合出的结构方向**相反**。
这不是"差一点"，是**关系反向**。q1000 两口径余弦为正，但秩相关只有
0.30 / 0.12，离 0.50 分别差 0.197 / 0.383。

**旁证（同向）**：`backtest/structure_coefficients.csv` 的 `row_type=summary`
行显示 `beta_h` 在 2018-2020 窗口近乎为零——legal_deadline 下
`beta_h=0.000129`、`nw_lag3_t_beta_h=0.175`；+one_month_end 下
`beta_h=0.000332`、`t=0.449`。公共闸 `beta_h_same_sign` 之所以 PASS
（verdicts.csv:68,97），是因为它只查符号一致性；而中间窗口的系数在统计上
与 0 不可区分。**该 PASS 是弱证据，不应被当作"结构稳定"的支撑。**

#### (b) `state_coverage`——DIV 状态份额是唯一约束

筛 `gate_name=='state_coverage' & affects_verdict==True & q.notna()`。
三个窗口中 UU 份额均在 **0.451~0.553**、DD 份额均在 **0.377~0.432**，
**从未接近 0.10 下限**；**唯一失败源始终是 DIV 份额**。
按 `n × share` 还原为交易日计数：

| 窗口 | 腿 | n | DIV 份额 | DIV 天数 | 需 ≥ | 差几天 | 口径 |
|---|---|---|---|---|---|---|---|
| 2015-2017 | q1000 | 713 | 0.063114 | 45 | 72 | **−27** | 两口径相同 |
| 2018-2020 | q1000 | 730 | 0.082192 | 60 | 73 | **−13** | 两口径相同 |
| 2015-2017 | qblend | 713 | 0.077139 / 0.079944 | 55 / 57 | 72 | **−17 / −15** | LD / +1M |
| 2015-2017 | q500 | 713 | 0.099579 / 0.102384 | **71 / 73** | 72 | **−1 / 0** | LD FAIL / +1M PASS |
| 2018-2020 | q500 | 730 | 0.104110 / 0.105479 | 76 / 77 | 73 | 0 | 两口径 PASS |
| 2021-2023 | 全部 | 727 | 0.136~0.157 | 99~114 | 73 | 0 | 全 PASS |

对应 verdicts.csv:45-46,51-52,61-62（LD）与 :74-75,80-81,90-91（+1M）。

**读法**：q1000 与 qblend 在 2015-2017 的失败是**决定性的**（缺 15~27 个
交易日的 DIV 状态）；q500 在 2015-2017 的 LD 口径失败是**1 个交易日的
刀刃**（71 天 vs 需要 72 天）——这一天就是 §3 的 flip 来源之一。
2021-2023 窗口三态覆盖全部宽松通过，说明这是**样本早期状态定义偏
UU/DD 双边、DIV 稀缺**的问题，不是全期问题。

#### (c) `m1_increment`——只败在 Spearman IC 那一条腿

原始 M0/M1 行（`gate_name` 为空的 36 行，`window=2021-2023`）：

| 口径 | 腿 | M0 oos_r2 | M1 oos_r2 | R² 判定 | M0 IC | M1 IC | IC 缺口 | 闸 |
|---|---|---|---|---|---|---|---|---|
| LD | q1000 | 0.052507 | 0.150249 | ✅ M1 胜 | 0.502241 | 0.434734 | **−0.0675** | FAIL |
| LD | q500 | 0.097682 | 0.171713 | ✅ M1 胜 | 0.359104 | 0.349580 | **−0.0095** | FAIL |
| LD | qblend | 0.079093 | 0.195762 | ✅ M1 胜 | 0.480952 | 0.538655 | +0.0577 | PASS |
| +1M | q1000 | 0.047307 | 0.157441 | ✅ M1 胜 | 0.503641 | 0.426331 | **−0.0773** | FAIL |
| +1M | q500 | 0.096244 | 0.170938 | ✅ M1 胜 | 0.359664 | 0.343417 | **−0.0162** | FAIL |
| +1M | qblend | 0.076723 | 0.188229 | ✅ M1 胜 | 0.477311 | 0.490756 | +0.0134 | PASS |

对应 verdicts.csv:47,53,63（LD）与 :76,82,92（+1M）。

**读法**：M1 相对 M0 的 OOS R² 增量在 6 条腿上**一致且幅度大**
（比值 **1.7579~3.3281 倍**，即约 1.76~3.33 倍；绝对增量 +0.0740~+0.1167），
说明状态交互项确实解释了额外方差；但在**秩序层面**（Spearman IC）M1 对
q500/q1000 反而略逊于 M0。q500 的缺口只有 0.0095 / 0.0162——**边缘失败**；
q1000 的 0.0675 / 0.0773 属于中等幅度。qblend 两口径均通过。

#### (d) `partial_ic`（structure 层）

`gate_name=='partial_ic'` 行 + 分年原始行：

| 口径 | 腿 | 合并 partial_ic | 2021 | 2022 | 2023 | 正年数（需 ≥2） | 闸 |
|---|---|---|---|---|---|---|---|
| LD | q1000 | 0.179586 | **+0.0394** | +0.0734 | −0.1117 | 2 | PASS |
| LD | q500 | 0.106807 | +0.1111 | +0.0263 | −0.3606 | 2 | PASS |
| LD | qblend | 0.235340 | +0.7063 | −0.0992 | −0.5059 | **1** | FAIL |
| +1M | q1000 | 0.137850 | **−0.0291** | +0.0589 | −0.1117 | **1** | FAIL |
| +1M | q500 | 0.081774 | +0.0952 | +0.0677 | −0.3657 | 2 | PASS |
| +1M | qblend | 0.132804 | +0.7198 | −0.2016 | −0.6132 | **1** | FAIL |

对应 verdicts.csv:48,54,64 与 :77,83,93。

**读法**：合并 partial IC 六条腿**全为正**（0.082~0.235），失败全部来自
"至少两个自然年为正"这一条。**2023 年在全部 6 条腿上均为负**
（−0.11 ~ −0.61），qblend 的 2022 也为负——即增量信息的秩预测力
在确认窗后段崩塌。q1000 的 2021 值在两口径间跨零（+0.0394 → −0.0291），
是 §3 flip 的第二个来源。

### 2.3 production（2021-2023 聚合）

阈值（spec:186-188, 3424）：`sharpe_difference ≥ +0.10`、
`maxdd_difference ≥ −0.02`、`turnover_ratio ≤ 1.50`、
`partial_ic` 合并为正且至少 2 年为正。
基线 = 由 committed `factor_value` 重算的 equal_weight long-flat
（spec:3408），两口径完全一致：**Sharpe 1.000931 / MaxDD −0.131245 /
年化 0.117226 / turnover 8.425034**（production_metrics.csv，
`candidate=equal_weight, window=2021-2023`）。

| 口径 | 候选 | Sharpe | `sharpe_difference` | 需 ≥+0.10，**缺口** | `maxdd_difference`（需 ≥−0.02） | `turnover_ratio`（需 ≤1.50） |
|---|---|---|---|---|---|---|
| LD | B3_dual_target | 0.808991 | **−0.191940** | **0.291940** | −0.016202（余量 0.0038） | 1.32（余量 0.18） |
| LD | B3_unified | 0.823677 | **−0.177254** | **0.277254** | +0.006028（余量 0.026） | 1.16（余量 0.34） |
| +1M | B3_dual_target | 0.940403 | **−0.060527** | **0.160527** | −0.017904（余量 0.0021） | 1.28（余量 0.22） |
| +1M | B3_unified | 0.842784 | **−0.158147** | **0.258147** | −0.003634（余量 0.016） | 1.08（余量 0.42） |

`sharpe_improvement` 四路全 FAIL（verdicts.csv:23,27,31,35）。
`partial_ic`（production 层）：LD/dual PASS（:22），其余三路 FAIL（:26,30,34）——
FAIL 原因与 §2.2(d) 同源（LD/qblend、+1M/q1000 腿不过）。
`maxdd_worsening` 与 `turnover_multiple` 四路全 PASS（:21,24,25,28,29,32,33,36）。

**读法（本报告最硬的经济事实）**：四路 `sharpe_difference` **全部为负**。
B3 不是"增量不够大"，是**在确认窗系统性地把基线做差了**：年化收益从
基线 11.72% 降到 8.62%~10.67%，同时换手率升到基线的 1.08~1.32 倍。
要过闸需要从 −0.19 走到 +0.10，即 **+0.29 Sharpe 的位移**（最好的
+1M/dual 也需要 +0.16）。

`maxdd_worsening` 虽然 PASS，但 dual_target 两口径的余量只有
**0.0038 / 0.0021**（阈值 −0.02，实测 −0.0162 / −0.0179）——**这是勉强
通过，不是稳健通过**，任何重跑都可能翻面。

**集中度旁证**（`backtest/yearly_contribution.csv`，report-only、不入闸）：
2021 年是四路的 `strongest_year`，绝对 P&L 占比 0.449~0.579。剔除 2021 后
（`row_type=excluding_strongest`）Sharpe 进一步塌到 **0.502（LD/dual）**、
0.669（LD/unified）、0.659（+1M/dual）、0.633（+1M/unified）——
本已跑输基线的表现，还高度集中在单一年份。

### 2.4 boundary（post-IM，仅 dual_target 适用）

`B3_unified` 的可执行边界"恒满足"（verdicts.csv:10,15），故 post-IM 闸只
约束 `B3_dual_target`。窗口 = 2022-07-22 起，`n_obs=352`（≥252，
`post_im_min_days` PASS，verdicts.csv:7,12）。基线 post-IM Sharpe = **0.844690**。

| 口径 | dual Sharpe | `sharpe_difference` | 需 ≥+0.10，**缺口** | `maxdd_difference` | q500 腿 partial IC | q1000 腿 partial IC |
|---|---|---|---|---|---|---|
| LD | 0.388830 | **−0.455860** | **0.555860** | +0.010758 PASS | **−0.093149 FAIL** | +0.276068 PASS |
| +1M | 0.759998 | **−0.084692** | **0.184692** | −0.001860 PASS | **−0.213803 FAIL** | +0.204463 PASS |

`post_im_sharpe_difference` 与 `post_im_partial_ic` 两口径全 FAIL
（verdicts.csv:8-9,13-14）；`post_im_maxdd_difference` 与 `post_im_min_days`
全 PASS（:6-7,11-12）。

**读法**：在**受 post-IM 闸约束的主体（`B3_dual_target`）上**，这是
**Sharpe 维度**缺口最大的单点（LD 缺口 0.555860）。
（不与 `stability` 等其他量纲比较——例如 q500@+1M 的秩相关缺口
0.587672 数值更大，但两者量纲不同、不可比。）
`post_im_partial_ic` 的失败**单一地由
q500 腿造成**（−0.093 / −0.214，需非负），q1000 腿反而是表现最好的组件之一。
组件级看（production_metrics.csv 非闸行）：LD 下 B3_500 post-IM Sharpe
仅 **0.221637**，B3_1000 为 0.437272，都低于基线 0.844690。

> **必须并列记录的反向事实（避免"post-IM 一律最差"的错误印象）**：
> post-IM 窗口内**有跑赢基线的组合**，只是它们不受 post-IM 闸约束。
> 基线 post-IM Sharpe = 0.844690：
>
> | 主体 | 口径 | post-IM Sharpe | vs 基线 |
> |---|---|---|---|
> | `B3_unified`（blend） | LD | **0.906024** | **+0.061334** |
> | `B3_unified`（blend） | +1M | **0.918642** | **+0.073952** |
> | `B3_1000`（组件，`is_candidate=false`） | +1M | **1.003290** | **+0.158601** |
>
> **为什么不改变闸门结论**：(1) `B3_unified` 的可执行边界"恒满足"
> （verdicts.csv:10,15），**post-IM 闸根本不施加于它**，故这两个正差值
> 不是"过闸"证据；(2) 决定四路裁决的是 **2021-2023 聚合窗**，
> 在那里四路 `sharpe_difference` 全为负（§2.3），`B3_unified` 为
> −0.177254 / −0.158147；(3) `B3_1000` 是 `is_candidate=false` 的
> 报告性组件，其单腿表现不构成候选证据，且 `B3_dual_target` 要求
> q500/q1000 双腿，而同口径 B3_500 post-IM 为 0.318834（−0.525855）。
>
> 换言之：**post-IM 的强弱高度取决于看哪个主体**——unified 与 q1000 腿
> 在此窗跑赢，dual blend 与 q500 腿大幅跑输。本报告不主张
> "post-IM 是全局最差窗口"这一绝对表述。

### 2.5 决定性失败 vs 边缘失败

**决定性失败**（差得远，数据口径级扰动不可能弥合）：

| # | 闸 | 主体 | 实测 vs 阈值 | 量级 |
|---|---|---|---|---|
| D1 | `stability` | q500、qblend × 两口径（4 条腿） | 余弦 −0.20~−0.28（需 >0）、秩相关 −0.09~−0.03（需 ≥0.50） | **符号反向 + 缺 0.54~0.59** |
| D2 | `sharpe_improvement` | 四路全部 | −0.061~−0.192（需 ≥+0.10） | **缺 0.16~0.29，且符号为负** |
| D3 | `post_im_sharpe_difference` | dual × 两口径 | −0.456 / −0.085（需 ≥+0.10） | **缺 0.19~0.56** |
| D4 | `state_coverage` 2015-2017 | q1000、qblend | DIV 45/55/57 天（需 72） | **缺 15~27 个交易日** |
| D5 | `state_coverage` 2018-2020 | q1000 | DIV 60 天（需 73） | 缺 13 个交易日 |
| D6 | `partial_ic` 年符号 | qblend × 两口径 | 仅 1 年为正（需 ≥2），2022/2023 双负（−0.10~−0.61） | 需再有一年翻正（最低成本为 2022 的 +0.0992 / +0.2016） |
| D7 | `stability` | q1000 × +1M | 秩相关 0.1170（需 0.50） | 缺 0.383 |

**边缘失败**（差一点，量级与口径扰动同阶）：

| # | 闸 | 主体 | 实测 vs 阈值 | 缺口 |
|---|---|---|---|---|
| M1 | `state_coverage` 2015-2017 | q500 × LD | DIV 71 天（需 72） | **1 个交易日** |
| M2 | `m1_increment` IC 腿 | q500 × LD | 0.349580 vs 0.359104 | **0.0095** |
| M3 | `m1_increment` IC 腿 | q500 × +1M | 0.343417 vs 0.359664 | 0.0162 |
| M4 | `partial_ic` 年符号 | q1000 × +1M | 2021 年 −0.0291（需 >0） | **0.0291（跨零）** |
| M5 | `stability` | q1000 × LD | 秩相关 0.3028（需 0.50） | 0.197 |
| M6 | `m1_increment` IC 腿 | q1000 × 两口径 | 缺 0.0675 / 0.0773 | 中等 |

**"勉强通过"清单（同样是脆弱点，方向相反）**：
`maxdd_worsening` dual 两口径余量仅 0.0038 / 0.0021；
`beta_h_same_sign` 依赖一个 t=0.175 的近零系数（§2.2(a)）。

> **结构性观察**：边缘失败几乎全部集中在 **q500 腿 + q1000 腿的 IC/年符号**，
> 而 `stability` 与 `sharpe_improvement` 两个决定性失败**覆盖了所有 6 条腿 ×
> 两个口径，无一例外**。即使把 M1~M6 全部翻正，D1/D2 仍然独立地把
> 四路候选钉死在 STOP。

---

## 3. `PIT_POLICY_FLIP` 定位

spec:3075 定义：**`beta_h` 符号翻转、M1 增量符号翻转、或候选通过方向翻转**，
三者任一即写入 run 级 `DATA_BLOCKED` 行。产物中也有对应的
`gate_name=PIT_POLICY_FLIP` 行（model_comparison.csv，`pit_policy=ALL`、
`window=run`、`gate_pass=False`、`affects_verdict=True`，其余数值列全空）。

逐条排查三个触发源：

| 触发源 | 是否发生 | 证据 |
|---|---|---|
| `beta_h` 符号翻转 | **否** | structure_coefficients.csv `row_type=summary`：三窗口 × 两口径的 `beta_h` 全为正（+0.001317/+0.000129/+0.001317 与 +0.001372/+0.000332/+0.001415）；`beta_h_same_sign` 两口径均 PASS |
| M1 增量符号翻转 | **否** | 直接比对**增量本身的符号**（非 `gate_pass`）：OOS R² 增量（M1−M0）六条腿两口径**全为正**（+0.0740 ~ +0.1167）；Spearman IC 增量符号逐腿一致——q1000 **−/−**（−0.067507 / −0.077311）、q500 **−/−**（−0.009524 / −0.016246）、qblend **+/+**（+0.057703 / +0.013445）。**无任何一条增量跨零** |
| **候选通过方向翻转** | **是（2 处根因）** | 见下 |

### 3.1 分歧点 1：`state_coverage`，q500 @ 2015-2017

| 口径 | DIV 份额 | DIV 天数（n=713） | 阈值 0.10 → 需 72 天 | 结果 |
|---|---|---|---|---|
| legal_deadline | 0.099579 | **71** | 72 | **FAIL**（verdicts.csv:51） |
| +one_month_end | 0.102384 | **73** | 72 | **PASS**（verdicts.csv:80） |

**分歧大小 = 2 个交易日 / 713（份额差 0.002805）**；判定线正好落在两者之间
（LD 差 1 天）。DD 份额同时从 0.380084 微降到 0.377279，UU 份额不变
（0.520337），即一个月的信息延后把 2 天从 DD 重分类到 DIV。

### 3.2 分歧点 2：`partial_ic` 年符号，q1000 @ 2021

| 口径 | 2021 partial IC | 2022 | 2023 | 正年数 | 结果 |
|---|---|---|---|---|---|
| legal_deadline | **+0.039399** | +0.073440 | −0.111664 | 2 | **PASS**（verdicts.csv:48） |
| +one_month_end | **−0.029093** | +0.058879 | −0.111664 | 1 | **FAIL**（verdicts.csv:77） |

**分歧大小 = 0.068492，跨零**；该年 `n=11` 个月度观测。这一处翻转向上传导，
造成另外两处派生分歧：
- structure 层 `B3_dual_target` 聚合 `partial_ic`：LD PASS（:42）→ +1M FAIL（:71）；
- production 层 `B3_dual_target` 聚合 `partial_ic`：LD PASS（:22）→ +1M FAIL（:30）。

### 3.3 分歧的量级与含义

**分歧总清单（gate 级 PASS/FAIL 不一致，共 4 处，2 个根因）**：
`state_coverage q500@2015-2017`、`partial_ic q1000@2021-2023`（structure 腿级）、
`partial_ic dual 聚合`（structure）、`partial_ic dual 聚合`（production）。

**关键定量**：两个根因分歧的绝对量级分别是 **0.0028（状态份额）** 与
**0.0685（年度 partial IC）**。与之对比，§2.5 中的决定性失败缺口是
**0.16~0.59（Sharpe）** 与 **0.54~0.59（stability 秩相关）**——
**相差一个数量级**。

**同时必须承认的方向性事实**：两口径的经济指标位移并不小。
`B3_dual_target` 2021-2023 Sharpe 从 0.808991（LD）走到 0.940403（+1M），
**位移 +0.131**；post-IM 从 0.388830 走到 0.759998，**位移 +0.371**。
也就是说，**一个月的信息时点差异，对组合层 Sharpe 的影响可达 0.13~0.37**。
这个尺度对 §4.3 的推断至关重要——它既是"口径扰动能有多大"的经验刻度，
也是"这条 STOP 有多大程度依赖 PIT 假设"的警示。

---

## 4. 三个 run blocker 与统计 STOP 的独立性（核心交付）

### 4.1 `SALG_FRESHNESS` 的真实足迹：**1 只票**

blocker 文本"SalG valid-through 2020-04-30 is earlier than requested data_end
2023-12-31"读起来像"SalG 这份数据在 2020-04 之后整体断更"。**产物证据表明
不是这样。**

从 `research/monthly_exposures.csv.gz`（804,024 数据行，流式 awk 聚合，
字段 `pit_policy/formation_date/model_eligible/salg_source_end_date`）：

**(a) SalG 源本身是当期的。** 每个 formation 的 `salg_source_end_date` 最大值
随 formation 正常推进（例：2023-12-29 → 2023-09-30；2022-12-30 → 2022-09-30；
2021-06-30 → 2021-03-31），即一个季度的正常滞后。

**(b) 陈旧依赖只存在于极少数个股。** 统计"依赖期末日 < 2020-05-01"的行数，
分母为**该 formation 的 model-eligible 行数**（`model_eligible=True`，
含 `salg_source_end_date` 为空的非依赖行；两口径合计）：

| formation | 陈旧行数 | model-eligible 行数（ME） | 占比 |
|---|---|---|---|
| 2020-09-30 | 10 | 5,818 | 0.1719% |
| 2020-10-30 | 10 | 5,818 | 0.1719% |
| 2020-11-30 | 8 | 6,310 | 0.1268% |
| 2020-12-31 | 8 | 6,308 | 0.1268% |
| 2021-01-29 ~ 2022-03-31（15 个月） | 8 | 6,298 ~ **6,918** | 0.1156% ~ 0.1270% |
| 2022-04-29 | 6 | 6,912 | 0.0868% |
| 2022-05-31 ~ 2023-09-28（17 个月） | 4 | 6,886 ~ 7,116 | 0.0562% ~ 0.0581% |
| 2023-10-31 | 3 | 7,277 | 0.0412% |
| **2023-11-30 / 2023-12-29** | **2** | **7,412** | **0.0270%** |

> 表为 2020-09 及之后的**逐月归并**（区间行内各月 ME 不同，故给出区间；
> 区间内陈旧行数恒定）。2020-09 之前的 formation 未列——彼时"依赖早于
> 2020-05-01"是**正常 PIT 行为**而非陈旧（例：2014-10-31 formation 使用
> 2014-09-30 的依赖），把它计入陈旧会严重高估问题规模。
> 确认窗（2021-2023）内 ME 上界为 **6,918**，陈旧占比上界 **0.1270%**。
> 末次 formation 的 ME=7,412 与交接稿:238-250 的"latest model rows 7,412"
> 一致（该数为两口径合计：7,410 有依赖 + 2 无依赖）。

**(c) 涉及的票只有 5 只**（2020-09 之后全期去重）：
`000820.SZ`（依赖止于 2019-12-31）、`300431.SZ`（2019-09-30）、
`600145.SH` / `600421.SH` / `600610.SH`（均止于 2017-12-31）。
这些是长期停牌/退市整理类标的，其财务披露**本身就真实地停止了**——
这是标的事实，未必是数据缺口。

**(d) 最后一个 formation（2023-12-29）上只剩 2 行异常**：
`000820.SZ`（`salg=2019-12-31`，两口径各 1 行）与 `688266.SH`（字段为空，
两口径各 1 行）。交接稿:238-250 已单独查证 `688266.SH` 的空值表示
"不消费 SalG"而非坏溯源，`9a18090` 因此改为跳过缺失的非依赖行。

**(e) `2020-04-30` 的来源（推断）**：最后一个 formation 上最小的非空依赖
期末日是 `2019-12-31`（即 `000820.SZ` 的 2019 年报），而 2019 年报的
法定披露截止日正是 **2020-04-30**。结合交接稿:252-255（该值由最后一个
formation 的模型行计算），可判定 `salg_valid_through` = 最后 formation 的
最小依赖 → 其法定可得日。

> ⚠️ **不确定**：本条 (e) 的映射关系是从"2019-12-31 年报 → 2020-04-30 法定
> 截止"这一吻合**推断**的，我未阅读 `salg_valid_through` 的实现代码。
> 若实现另有定义，结论 (a)~(d) 的原始计数不受影响。

**独立性结论**：SalG 陈旧在确认窗（2021-2023）内影响的模型行占比
**≤0.13%**，且集中在 5 只长期停牌股。组合层还有 `weight_cap: 0.01` 与
`min_leg_size: 100`（spec:165-166）限制单票影响。
**这一规模无法解释 §2.3 中 0.16~0.29 的 Sharpe 缺口，也无法解释
§2.2(a) 中早/晚期斜率余弦为负。**

### 4.2 `TRUE_DISCLOSURE_COVERAGE` 的 0/626,732：**是标注规则，不是污染测量**

spec:1039-1042 给出该字段的生成逻辑：

```python
has_unverified_csmar_history = bool(
    ((facts["data_source"] == "csmar") & (facts["end_date"] <= date)).any()
)
...
"true_first_disclosure_verified": (not has_unverified_csmar_history)
```

即：**只要一只票在该 formation 之前的财务事实历史中出现过任何一条
CSMAR 来源的记录，整条观测就被标为"未核验"。** spec:3672 明确说明这是
"第一轮保守规则"，"在当前 2014-2023 数据上覆盖率因此是 0，而不是编造一个
部分估计"，并要求后续用 fact 级真实首披日回填后才能变成 1。

产物侧核对（monthly_exposures.csv.gz 流式统计）：

| 口径 | model-eligible 行数 | `true_first_disclosure_verified=True` 行数 |
|---|---|---|
| legal_deadline | 314,237 | **0** |
| legal_deadline_plus_one_month_end | 312,495 | **0** |
| **合计** | **626,732** | **0** |

合计 626,732 与 `run_manifest.json` 的 `required_denominator: 626732`
**逐位吻合**，确认分母口径 = 两个 PIT 口径下的全部模型行。

**独立性结论**：0/626,732 **不度量 PIT 误差的大小**，它度量的是
"有多少观测的依赖链里出现过 CSMAR"——在以 CSMAR 为历史底座的库里，
这个数字必然是 0。**因此不能从"覆盖率 0"推出"当前裁决被 PIT 污染
到不可信"，也不能推出"回填后裁决会变"。** 它只说明：两个近似口径
都是近似，真实口径未被观测。

### 4.3 "修好数据后 STOP 被翻转"需要什么条件成立

**翻转的逻辑链**（自下而上，缺一不可）：

```
statistical_verdict=GO
  ← bootstrap.gate_pass=True（需 holm_adjusted_tail < 0.10，本次未真正计算）
  ← bootstrap.structure_pass=True
  ← 某个候选的全部候选闸 PASS
      · B3_unified：qblend 需同时过 m1_increment✅ + partial_ic❌ + stability❌ + state_coverage❌
      · B3_dual_target：q500 与 q1000 **双腿** 各自四闸全过
```

**逐条评估"数据修复能否达成"**：

| 需翻转的闸 | 需要的位移 | 已观测的口径敏感度（两 PIT 口径间同一统计量的位移） | 判断 |
|---|---|---|---|
| `stability` q500 秩相关 | −0.034490 → ≥+0.50，**+0.534490** | 0.053182（−0.034490→−0.087672） | **相差 10.05 倍，证据不支持翻转** |
| `stability` qblend 秩相关 | −0.065171 → ≥+0.50，**+0.565171** | 0.009848（−0.065171→−0.055323） | **相差 57.39 倍，证据不支持翻转** |
| `stability` q500/qblend 余弦符号 | 负 → 正（需跨零 0.2018~0.2831） | 余弦位移 **0.022392（qblend）~ 0.058651（q500）**，**从未跨零** | **证据不支持翻转** |
| `stability` q1000 秩相关 | +0.3028 → ≥+0.50，**+0.197** | **0.186**（0.3028→0.1170） | **同阶，不能排除** |
| `state_coverage` q500@2015-2017 | +1 个交易日 | 2 个交易日 | **可翻转（已在 +1M 口径下实际翻转）** |
| `state_coverage` q1000/qblend@2015-2017 | +15~27 个交易日 | 2 个交易日 | **相差 7~13 倍，证据不支持** |
| `m1_increment` q500 IC | +0.0095 / +0.0162 | 0.0062（0.349580→0.343417） | **同阶，不能排除** |
| `m1_increment` q1000 IC | +0.0675 / +0.0773 | 0.0084 | **相差 8 倍，证据不支持** |
| `partial_ic` q1000@2021 符号 | 跨零 0.0291 | **0.0685（已实际跨零）** | **可翻转** |
| `partial_ic` qblend 需再有一年翻正 | 最低成本 = 2022 的 **+0.0992**（LD） | **0.102**（−0.0992→−0.2016） | **同阶，不能排除** |
| `sharpe_improvement` | +0.161 ~ +0.292 | **0.131**（0.808991→0.940403） | **同阶偏大，不能排除但需超出已观测幅度** |
| `post_im_sharpe_difference` | +0.185 ~ +0.556 | **0.371**（0.388830→0.759998） | **同阶，不能排除（LD 侧仍差 1.5 倍）** |

**综合判断**：

1. **`stability` 是压倒性的否决闸。** 要让任一候选通过，其对应腿的
   `stability` 必须过。按 **q500 / qblend** 同一腿序对照：
   需要的秩相关位移为 **+0.5345 / +0.5652**，**并且**余弦须跨零；
   而已观测的秩相关口径敏感度只有 **0.053182 / 0.009848**、
   余弦位移只有 **0.058651 / 0.022392** 且从未跨零——秩相关维度
   **相差 10.05 倍（q500）与 57.39 倍（qblend）**。q1000 单腿的 0.197 缺口虽与
   敏感度 0.186 同阶不能排除，但
   **q1000 属于 `B3_dual_target`，该候选要求 q500 与 q1000 双腿全过**
   （spec:3073）——q500 腿的 stability 缺口是 0.535，因此**即使 q1000 全部
   翻正，`B3_dual_target` 仍然 STOP**。`B3_unified` 只有 qblend 一条腿，
   缺口 0.565，**同样 STOP**。
   → **结论：在现有证据下，没有任何一条可通过的路径。**

2. **经济层是第二道独立否决。** 即使结构闸全部翻正，
   `sharpe_improvement` 仍需从负值走到 +0.10。四路全负这一点与 PIT 口径
   无关（基线在两口径下完全相同：1.000931）。

3. **必须诚实标注的反向证据**：口径敏感度在**组合层**并不小
   （Sharpe 位移 0.131 / post-IM 0.371）。若真实首披日回填带来的扰动
   显著大于"legal_deadline → 晚一个月"这一对比，则 `sharpe_improvement`
   与 `post_im_sharpe_difference` 的缺口**在量级上不能被排除**。
   但这不改变第 1 条：**`stability` 的缺口比任何已观测的口径效应大一个
   数量级，且需要余弦符号反转。**

> ⚠️ **不确定（方向性外推）**：现有两个口径只探测了"信息更晚可得"这一个方向
> （legal_deadline → 再晚一个月）。真实首披日回填是**相反方向**的扰动
> （信息通常比法定截止日**更早**可得），且是**逐 fact 异质**的，不是全局
> 平移。产物中**没有**任何"更早方向"的观测。因此上表的"口径敏感度"
> 只能作为**同量级参照**，**不是严格上界**。这是本报告最大的不确定性来源，
> 结论 1 的强度依赖于"10.05~57.39 倍的差距足以吸收方向外推的误差"这一判断，
> 而不是依赖一个可证明的界。

> ⚠️ **不确定**：`stability` 的 early/late 切分点未在本次分析中从产物直接
> 核实（推测为发现窗 2015-2020 的前后半段）。切分点若不同，余弦与秩相关
> 的解释不变（仍是"发现期内部结构不一致"），但"哪一段驱动了反号"无法从
> 现有产物定位。

> ⚠️ **不确定**：本次 bootstrap 未真正执行（§2.1），因此
> **完全没有**关于"B3 相对基线的收益差是否显著"的重抽样证据。
> 若未来结构闸被翻正，bootstrap 闸是一个**尚未被测试过的**新关卡，
> 其通过与否无法从本次产物预判。

---

## 5. 数据尾巴现状（coverage_audit 与两个"闸"的口径差）

出处：`research/coverage_audit.csv`（4,600 行）。
每个 PIT 口径 **128 个 formation 月**，其中 **111 个 `required_formation=True`**
（与交接稿一致）；被排除的 17 个月是 2013-05-31~2014-09-30
（`status=DATA_BLOCKED`，34 行 = 17 月 × 2 口径）。

### 5.1 `DATA_MISSING_SHARES`：all=6,656 / required=0

| 项 | 值 |
|---|---|
| 行数 | 32（`check` = `size_exclusion` 16 + `model_exclusion` 16） |
| `eligible_count` 合计 | **6,656** |
| `required_formation=True` 部分 | **0** |
| `affects_final=True` 部分 | **0** |
| `status` | 全部 `REPORT_ONLY` |
| formation 范围 | **2013-05-31 ~ 2013-12-31** |

**含义**：6,656 个票·月**全部落在 2013 年**，即**完全在结构日历（2014-10 起）
之外**，也完全在 111 个 required 月之外。这 8 个月（2013-05-31~2013-12-31）
本身已因 `DATA_MISSING_CLOSE + DATA_MISSING_SHARES` 合计 211~213/2,465 =
**8.56%~8.64% 超过 0.25% 重要性阈值**而被整月标记
`DATA_BLOCKED / DATA_CONTRACT`（coverage_audit.csv `check=monthly_exposure`），
但因 `required_formation=False`，不进入任何裁决。

（被排除的另外 9 个月 2014-01-30~2014-09-30 与股本缺失无关，
其 detail 为 `q500 index members are missing`——`index_constituent`
的 `min_date` 是 2014-10-31，见 `run_manifest.json`。这正是
结构日历自 2014-10 起的原因。）

### 5.2 `DATA_MISSING_CLOSE`：all=232 / required=64

| 项 | 值 |
|---|---|
| 行数 | 120（`size_exclusion` 60 + `model_exclusion` 60） |
| `eligible_count` 合计 | **232** |
| `required_formation=True` 部分 | **64** |
| `affects_final=True` 部分 | **0** |
| `status` | 全部 `REPORT_ONLY` |
| formation 范围 | 2013-05-31 ~ 2023-07-31 |

`required` 那 64 的精确构成：**15 个 required formation 月 × 2 个 check ×
2 个 PIT 口径 = 60 行**，`eligible_count` 合计 64（每口径每 check 16 个
票·月，即 15 个月中有一个月缺 2 只）。这 15 个月是
`2018-12-28, 2019-11-29, 2020-05-29, 2020-06-30, 2020-07-31, 2020-08-31,
2020-09-30, 2020-10-30, 2020-11-30, 2020-12-31, 2021-01-29, 2021-02-26,
2021-05-31, 2022-06-30, 2023-07-31`。

它们与 `status=MEASURE_WITH_EXCLUSION / DATA_MATERIALITY_EXEMPTION`
的 30 行（15 月 × 2 口径）**是同一批月份**，逐月 detail 形如
`1/3527 names (0.0284%) excluded within the 0.2500% materiality threshold: DATA_MISSING_CLOSE`。
`run_manifest.json` 汇总：`materiality_exemptions = {months: 15,
max_share: 0.0004375410194705754（**0.04375%**）, threshold: 0.0025（0.25%）}`。

> 口径提示：逐月 detail 把该值按四位小数百分比呈现为 **0.0438%**
> （四舍五入），本文正文取截断写法 0.0437%——**两者是同一个数**
> `0.0004375410194705754` 的不同呈现，非数据不一致。下文统一以 0.04375% 为准。

### 5.3 为什么 `b3_eval` 不阻断，而 `verify_post_write` 闸 3 判不过

**这是两把不同的尺，不是矛盾。**

| | `b3_eval` 的数据契约 | `verify_post_write.py` 闸 3 |
|---|---|---|
| 判据 | 逐 formation 月：缺失名单占比是否超过 `data_materiality_threshold=0.0025`；超阈 → 整月 `DATA_BLOCKED`；未超阈 → `MEASURE_WITH_EXCLUSION` 并继续 | `shares["all"] == 0 and shares["required"] == 0`（RETRIEVAL.md:254-256） |
| 作用域 | 只看 **required formation 月**是否可测 | **全历史**，含 2013 年非 required 月 |
| 本次结果 | 15 个 required 月各缺 1~2 只（≤0.04375% ≪ 0.25%）→ 全部豁免继续；`affects_final=False` | `all=6656 ≠ 0` → **失败** |

`verify_post_write.py` 闸 3 的失败文本为
`DATA_MISSING_SHARES is not cleared: {'all': 6656, 'required': 0}`
（RETRIEVAL.md:251），RETRIEVAL.md:254 明确判定
**"这是真实且特定于闸 3 的判定，不是路径伪影"**，并记录闸 1/2 通过、
闸 4/5 因闸 3 中止未执行。闸门表逐行对应为
**RETRIEVAL.md:243=闸 1（`verify_execution_receipt`，✅）、
244=闸 2（`verify_preflight_manifest`，✅）、245=闸 3（`verify_coverage_audit`，❌）、
246=闸 4/5（run_manifest、提案链，未达）**。

**结论**：闸 3 用的是"股本缺失必须全历史归零"的**验收级严格口径**，
而 B3 裁决用的是"required 月 + 重要性阈值"的**可测性口径**。
6,656 全部位于 2013 年、`required=0`、`affects_final=0`，
**对本次统计裁决零影响**；它只是让部署验收脚本无法给出 `accepted:true`。

---

## 6. 裁决底线（供用户决策，不代作决定）

### 6.1 把 `DATA_BLOCKED` 变成干净裁决需要哪些上游工作

| # | 工作 | 量级（基于产物实测） | 解除的 blocker |
|---|---|---|---|
| U1 | 补齐/明示豁免 5 只票的 SalG 依赖 | **5 只票**（`000820.SZ`、`300431.SZ`、`600145.SH`、`600421.SH`、`600610.SH`）；最后 formation 上只需处理 **`000820.SZ` 1 只** | `SALG_FRESHNESS` |
| U2 | 回填 fact 级真实首披日，替换保守标注 | 分母 **626,732 模型行**；但真正要回填的是其底层财务事实的首披日（`stock_financial` 本次消费 3,721,765 行），量级由 Wind 首披日可得性决定 | `TRUE_DISCLOSURE_COVERAGE` |
| U3 | U2 完成后重跑，观察两口径分歧是否消失 | 一次全链路重跑（Windows 128G 执行机） | `PIT_POLICY_FLIP`（不保证消失） |
| U4 | （仅为部署验收）清理 2013 年 `DATA_MISSING_SHARES` 6,656 票·月 | 全部在 2013-05~2013-12，非 required | `verify_post_write` 闸 3（**与裁决无关**） |

> 注：U3 不保证解除。`PIT_POLICY_FLIP` 比较的是**两个近似口径**；
> 若 U2 完成后改为单一真实口径，该比较本身可能不再适用（需确认代码语义），
> 但若仍保留两个近似口径对照，分歧是否消失取决于 §3.1/§3.2 那
> **2 个交易日**与 **0.0685 的年度 IC** 是否被真实日期抹平——现有产物
> **无法预判**。

### 6.2 "数据修复后 B3 仍 STOP"的可能性评估

**基于 §4.3 的界**：

- **高置信度（现有证据强烈支持）**：`B3_unified` 与 `B3_dual_target`
  在数据修复后**仍将 STOP**。理由：唯一路径必须让 qblend（unified）或
  q500+q1000 双腿（dual）的 `stability` 通过，而 q500/qblend 需要
  **+0.54~0.57 的秩相关位移并伴随余弦符号反转**，已观测的口径敏感度
  只有 **0.009848~0.053182（秩相关）/ 0.022392~0.058651（余弦）且从未跨零**，
  相差 **10.05~57.39 倍**。
- **中置信度**：即使结构闸全过，`sharpe_improvement` 仍需 +0.16~0.29 的
  位移；已观测口径敏感度为 0.131（同阶偏大），**不能仅凭此排除**，
  但四路 Sharpe 差全为负是与 PIT 无关的事实（基线两口径完全相同）。
- **明确不能排除的局部翻转**：`state_coverage q500@2015-2017`（差 1 天）、
  `m1_increment q500 IC`（差 0.0095）、`partial_ic q1000@2021`（跨零 0.029）、
  `stability q1000`（差 0.197）、**`partial_ic qblend@2022`（差 0.0992，
  敏感度 0.102 同阶）**。**但它们都不足以解锁任一候选**：dual_target 需
  q500/q1000 双腿全过，而 **q500 的 stability 缺口 0.5345 仍然否决**；
  unified 只有 qblend 一条腿，即使其 `partial_ic` 年符号翻正，
  **qblend 侧仍由自身 stability 缺口 0.5652（且余弦需跨零）独立否决**。
- **完全未知**：bootstrap 闸从未真正执行；若结构闸奇迹般全过，
  该闸是一个未测试的新关卡。

**一句话**：现有产物**不能证明** B3 修完数据后必然 STOP（那需要反事实
重跑），但**能证明**"翻盘所需的位移比已观测到的任何数据口径效应大一个
数量级"，且这一判断的主要不确定性是**方向性外推**（见 §4.3 末的 ⚠️）。

### 6.3 这是用户的决策点——三个可选项

**本报告不作选择。** 三个选项的成本与"能回答什么问题"如下：

#### 选项 A：关轴归档

- **成本**：仅文档化收尾（把本报告与交接稿固化为 B3 单一入口，
  在 roadmap 上标 STOP + 重开条件）。**零计算成本**。
- **能回答**：不再回答新问题；把"B3 连续风格状态择时在当前库内数据下
  不提供优于 equal_weight 的增量"作为已交付的负结果沉淀。
- **代价/风险**：`final_verdict` 永久停在 `DATA_BLOCKED`，
  该负结论在溯源上带"数据未净化"的星号；若日后有人质疑
  "是不是数据脏导致的"，需要重新翻本报告的 §4 才能回答。
- **与既有决策的一致性**：与 2026-07 roadmap"八轴全 STOP、
  A 股短信号族无独立价值"的结论方向一致。

#### 选项 B：先修 SalG（U1），观察 flip 是否消失

- **成本**：**最低的实质性投入**——只需处理 5 只票（最后 formation 上
  仅 1 只 `000820.SZ`），随后一次重跑。
- **能回答**：(1) `SALG_FRESHNESS` 是否确为孤立尾巴（本报告预测：是）；
  (2) 三个 blocker 去掉一个后，`final_verdict` 是否只剩两个 blocker。
- **不能回答**：**不能**解除 `PIT_POLICY_FLIP` 或
  `TRUE_DISCLOSURE_COVERAGE`，因此 `final_verdict` **仍是
  `DATA_BLOCKED`**；也几乎不可能改变统计 STOP（受影响行 ≤0.13%）。
- **判断要点**：这是一次**低成本的证伪实验**——若修完 SalG 后统计层
  数字几乎不动，就为 §4.1 的独立性结论提供了直接的经验确认。

#### 选项 C：全量首披日回填（U2）后重跑

- **成本**：**最高**。需要 Wind 侧逐 fact 首披日覆盖（分母侧
  626,732 模型行对应的财务事实底座本次消费 3,721,765 行），
  外加 gateway 取数、入库、manifest 刷新、全链路重跑与再次双审。
  参照既往同类工程（share-capital par 标定），这是**周级**投入。
- **能回答**：(1) 唯一能把 `final_verdict` 洗成干净裁决的路径；
  (2) 唯一能在**真实 PIT 方向**（信息更早可得）上观测敏感度的手段——
  这正是 §4.3 标注的最大不确定性所在；(3) 顺带解除
  `PIT_POLICY_FLIP`（若改为单一真实口径）。
- **不能回答**：不改变 `stability` 需要跨越的量级本身；
  本报告预测重跑后仍 STOP，但**该预测只有靠这一选项才能被真正检验**。
- **判断要点**：若这次回填的价值**主要**是为 B3 翻案，
  §4.3 的 10.05~57.39 倍差距说明期望回报很低；
  若其价值是**为整个 stock_selector 库建立真实 PIT 底座**
  （惠及所有依赖 `stock_financial` 的下游研究），
  那么 B3 只是它的第一个受益者而非唯一理由——**这个权衡属于用户**。

---

## 附录：数字出处索引

| 章节 | 主要文件（相对 `run-windows-formal/`） | 定位方式 |
|---|---|---|
| §1 | `backtest/verdicts.csv`、`backtest/run_manifest.json` | 行号已逐条标注 |
| §2.1 | `backtest/bootstrap.csv` | 全表 4 行；阈值 spec:197,3401；哨兵语义 spec:3394,3504-3506 |
| §2.2 | `backtest/model_comparison.csv`、`backtest/structure_coefficients.csv` | 按 `gate_name` 筛选；阈值 spec:177-179,3069-3071；判定式 spec:2991-2996（stability）、spec:3014-3020（state_coverage） |
| §2.3 | `backtest/production_metrics.csv`、`backtest/yearly_contribution.csv` | 按 `gate_name`/`window`/`row_type` 筛选；阈值 spec:186-188,3424 |
| §2.4 | `backtest/production_metrics.csv`（`window=post-IM`） | 同上；`im_launch_date` spec:183 |
| §3 | `backtest/verdicts.csv`、`model_comparison.csv`、`structure_coefficients.csv` | flip 定义 spec:3075 |
| §4.1 | `research/monthly_exposures.csv.gz` | 流式 awk 按 `formation_date`/`model_eligible`/`salg_source_end_date` 聚合（未整表载入） |
| §4.2 | `research/monthly_exposures.csv.gz`、`backtest/run_manifest.json` | 同上 + `true_first_disclosure_coverage`；规则 spec:1039-1042,3672 |
| §5 | `research/coverage_audit.csv`、`RETRIEVAL.md` | 按 `side`/`required_formation`/`status` 分组；闸门表 RETRIEVAL.md:243-246、闸 3 失败原文 :251、判定说明 :254-256 |

**方法学备注**：`monthly_exposures.csv.gz`（107MB）与
`stock_period_returns.csv.gz` 未整表载入 pandas；§4 的全部统计由
`zcat | awk` 单遍流式聚合得出（常数内存），仅对小 CSV 使用 pandas。
本分析为纯只读，未修改任何产物，未连接数据库，未接触执行机。
