# 探针 ④ 预登记冻结：条件化有效性（信号 × 状态交互）（2026-08-14）

> **首行声明（议程 §3 ④(b) 强制）**：八轴当年测的是各变量的**边际增量**
> （`partial_ic(X | ew)`，全部不显著）；本探针测的是**交互**——`IC(ew | state=s)` 的
> **跨状态差异**。零边际增量与非零交互在数学上不矛盾。**本探针不是重开八轴**，
> 也不是 B3（不用股票级财务、不做市值排序、不碰 `b3_eval`/`b3_structure`、不引用 B3 闸门）。
> 先验已按议程 (b) 下调：B3 碎片 `m1_increment` 闸实际 FAIL（秩序层 M1 四腿劣于 M0），
> 预期得到**弱调节器而非强调节器**。

状态：**冻结**。本文档提交后，状态变量集合 / 切法 / 统计量 / 闸门数值不得增改；
结果只许追加到收官文档。用户于 2026-08-14 拍板解缓（队列纪律：④ 解缓须用户拍板）。

议程出处：`docs/plans/2026-08-12-signal-research-agenda.md` §3 候选④（:279-331）、
§4 C1/C2/C4/C6/C8。技术前提附录（2026-08-14 只读侦察，本次一并提交）：
`docs/plans/2026-08-14-probe-4-tech-annex.md`——数据实测、API 落点、基线出处的证据都在那里，
本文档只记决议。

---

## 1. 命题（可证伪）

现役 equal_weight 信号（生产参数 20/40/5 锁死，`output/equal_weight/equal_weight_signal_20d40z.csv`
列 `factor_value`）的预测力（对 blend 标的前瞻 k=20 收益的 rank IC）在四个可观测市场状态之间
存在系统性差异，且该交互过全部四条闸门。任一闸不过 → **STOP 归档**，
"regime 依赖"直觉正式关帐。

## 2. 数据与口径（全部沿用同秤，零平行实现）

- 样本日历：八风格指数 3,065 天（2014-01-02 ~ 2026-08-11）。
- 窗口：`backtest/baseline.py:30-35 WINDOWS` 唯一权威——train 2014-2020（1,707 天）/
  val 2021-2023（727 天）/ holdout 2024-2026（只报告不进闸，§4 C2）/
  选择窗 = train+val（2014-01-01 ~ 2023-12-31，2,434 天）。
- 同秤机器：`from backtest.fusion_probe import forward_return, nonoverlap_grid, rank_ic,
  spearman_rows, paired_ic_bootstrap`；收益层 `backtest.engine.run_strategy`（cost_bps=3.0,
  carry=blend）+ `backtest.metrics.sharpe` + `backtest.positions` 的 long-flat 生产映射。
  **禁止 import `pair_set_probe`**（配对集合专用）。
- 三个状态缓存**不延展**（缺口 29~30 天全在 holdout 尾巴，闸门零影响）；读取层**必须**剔除
  2026-07-01 垃圾尾行（复用 `dashboard.data.trim_incomplete_tail` 语义，附录 §1.1 补充发现 A），
  产物 metadata 记录各缓存实际末日（预期统一 2026-06-30）。
  **禁止**用 `--rebuild-*` CLI（副作用：覆盖两条已归档轴的产物，附录 §1.4）。
- PG 只在拉价格宽表 / carry 时触达：先探连接、带 connect_timeout/statement_timeout、
  一次性拉下复用（Tailscale 黑洞纪律）。

## 3. 状态变量定义（四个，≤4 上限用满；广度**不入**，留在未测登记）

| # | 状态变量 | 底层序列 | 变换（钉死） |
|---|---|---|---|
| S1 | 已实现波动 | `load_underlying_returns("blend")`（与收益端同源） | 20 日滚动标准差，不年化 |
| S2 | 成交额水位 | `backtest/output/market_turnover.csv` 列 `amt_yuan` | **必须先 250 日滚动分位去趋势**（绝对额直接切桶 ≈ 按时间切桶，会污染闸门 ③；此陷阱明文登记） |
| S3 | 涨停温度 | `backtest/output/thermometer.csv` 列 `lu_ratio` | 250 日滚动分位 |
| S4 | carry 深浅 | `backtest.data.load_carry("blend")` 原值（正=贴水） | 250 日滚动分位；**样本自 2015-04 首个有 carry 日起算**，之前的日子该变量为 NaN——**严禁把无 carry 日并入"低 carry"桶**（会造假状态）。逐变量单独记 n_obs 与首末日 |

- 所有滚动分位窗长统一 **250 日**（仓库既有唯一实现 `dashboard/data.py:32` 的默认；
  窗口未满前段返回 NaN 即丢弃，产物记录由此损失的天数）。
- **PIT 时点**：状态与信号同时点（T 日收盘），不额外 lag——与 `forward_return`
  "t 处 = t+1..t+k" 自洽。实盘部署时状态可得性依赖当日全量入库，上游未灌全则当日不生成调节
  （登记为部署注意项，不进闸）。
- **明文禁止收益派生的状态变量**（机器 docstring :26-30 的昂贵分支雷区）。

## 4. 网格（冻结，不扩）

**8 个变体 = 4 状态变量 × 2 切法**：
- 切法 A（三分）：滚动分位 p < 1/3 → 低；1/3 ≤ p < 2/3 → 中；p ≥ 2/3 → 高。
- 切法 B（二分）：滚动分位 p < 0.5 → 下；p ≥ 0.5 → 上。

前瞻期固定 **k=20**（同秤，不扫 k）。无其它任何网格维度。

## 5. 条件 IC 口径与交互统计量

- **条件 IC（主口径）**：对窗口 W、变体 v、桶 s，取全部 20 个 offset 的非重叠网格
  （`nonoverlap_grid(index, 20, offset)`，offset=0..19），每个 offset 在 `grid(o) ∩ bucket(s)`
  上算 Spearman rank IC，再按各 cell 样本数 n_{o,s} 加权平均得 `IC_s`。
  理由：沿用单 offset 非重叠会让 val 三分后每桶 ~12 点（⑤b 已被同一问题咬过）；
  推断走置换（rotation 打断配对，自动吸收重叠相关），不依赖独立性假设。
- **每桶最小样本硬下限**：某变体在选择窗内任一桶总天数 < **100**，该变体返回 `-inf`
  （"该行无法评分"——`-inf` 的唯一合法用法，规矩 2）。
- **交互统计量（有符号，钉死）**：三分 = `IC_高 − IC_低`（中桶不参与）；二分 = `IC_上 − IC_下`。
  **双侧检验**：进机器的统计量 = `|有符号值|`；闸门 ① 的同号判定用有符号值**后置独立判**
  （规矩 2：一致性闸绝不编码进统计量/-inf）。符号约定在此冻结，跑完不得改看哪一侧。
- `paired_ic_bootstrap` 在本探针**只作诊断列**（其 i.i.d. 抽样与 20-offset 平均口径不匹配），
  不进闸门。

## 6. ⓪ 机器接入（严格沿用 ② 三规矩）

- **8 变体合成同一次** `selection_permutation_test` 调用（不做 4 次变量级独立调用）；
  选优 = 默认 `argmax_select`（选 `|统计量|` 最大者）；**只有 `p_selected` 进闸门**，
  `p_naive` 只准出现在"选择效应有多大"的证据段。
- 统计量在**选择窗（2014-2023）**上计算；`stat_fn(variant, idx)` 闭包捕获按日历固定的
  状态标签与前瞻收益，**只重排 equal_weight 信号**；变体各自的有效样本以"状态标签非 NaN"
  行内限定（不 fillna）。
- `scheme="rotation"`，`n_perm=1000`，`seed=0`；`min_shift = 2·k = 40`、
  `max_shift = n_obs − 40`（房规 `[2k, n−2k]`）。
- 不动 `selection_permutation.py` 一行。

## 7. 四条闸门（全过才 GO；任一不过 → STOP 归档）

| 闸 | 判据（冻结数值） |
|---|---|
| ① 两窗同号 | 赢家变体的**有符号**交互统计量在 train 与 val 两窗同号（按 §5 口径分别计算） |
| ② 置换显著 | `p_selected < 0.05`（8 点合成单次调用，无需 Bonferroni） |
| ③ 早/晚期一致性（**硬条件，stability 类，失败即刻中止后续闸**） | 选择窗按中位交易日切两半（≈2018 年末，各 ~1,217 天）；赢家变体各桶条件 IC 组成向量（三分 3 维 / 二分 2 维），两半窗向量**去均值后余弦 > 0**（二分时退化为两桶排序一致，接受此退化） |
| ④ 收益层 | 仓位调节版成本后 `worst(train,val)` Sharpe ≥ **1.100931**（= 现役 1.000931 + 0.10；现役出处 `probe_5b_dividend_partner_panel.csv:4` 与 `baseline_metrics.csv:105` 双证，六位小数避免四舍五入争议；§4 C4 同秤纪律：禁止引用 1.42/1.62/1.78/1.81 历史数字） |

**仓位调节器（闸 ④ 被测对象，单点无自由参数）**：
`pos_adj(t) = pos_longflat(t) × w(s_t)`，其中 `w(低效力桶)=0.5`、`w(其余)=1.0`；
"低效力桶" = 赢家变体有符号统计量指示的 IC 较低一侧极端桶（三分的中桶恒为 1.0），
由 IC 层同一次选优结果指定，**不扫 w、不另开网格**。先 long-flat 映射再乘 `w`；
换手变化只报告不设闸（议程 (f) 无换手闸，不自行加码）。

## 8. 既有怪癖登记（原样保留，不"顺手修"）

- `engine.py:24` shift(1) = 经济上 T 收盘成交（Batch 12 定性为文档债）；
  `engine.py:27-28` 首日成本落第二天。两者对现役与调节版是**同引擎同日期的非差分偏差**，
  对差分闸门影响在小数第三位以下（`exec_price_probe.py:133` 同款纪律）。
- carry：blend 缺腿按 0（2015-04~2022-07 为 IC 单腿 ÷2）；序列止于 2026-04-29 →
  holdout 段最后一年无 carry 输入（对多头略偏悲观，§4 C6 勘误口径）。闸门只读 train/val，
  不受染；holdout 报告段必须带此标注。

## 9. 禁区清单

不碰 `b3_eval`/`b3_structure`（早/晚期余弦**自写**，B3 那份在禁区内）；不扩网格；不延展缓存；
禁 `--rebuild-*` CLI；禁收益派生状态变量；一致性闸不得编码进 `-inf`；不改
`selection_permutation.py`/`fusion_probe.py`/`paired_bootstrap.py`；不触碰冻结 legacy 根
（`signals/ selection/ research/ factors/ stock_selector/data/`——新代码全在 `backtest/` 探针层）。

## 10. 执行与产物

- **本机跑，不上 WSL2**（≤8 变体 × 1,000 置换 × 小规模 Spearman，输入 <50 MB，附录 §5 判断）。
- 代码：`backtest/conditional_probe.py`（新增探针模块）+ `tests/test_bt_conditional_probe.py`
  （判例须覆盖：三规矩、垃圾尾剔除、carry NaN 不入桶、每桶最小样本 `-inf`、余弦定义、
  调节器单点映射）。
- 产物：`backtest/output/probe4_conditional_panel.csv`（变体 × 窗口 × 桶的条件 IC 与 n）、
  `backtest/output/probe4_conditional_verdict.csv`（四闸判定 + p_selected/p_naive + metadata）。

## 11. 预算上限

4 个工作日；闸 ③（stability 类）不过即刻中止，不跑闸 ④。

## 12. 自由度决议表（对附录 §7 的逐项裁决）

| # | 自由度 | 决议 |
|---|---|---|
| D1 | 波动口径 | blend 标的收益 20 日滚动 std，不年化（单口径，不得双报） |
| D2 | 分位窗长 | 250 日；未满前段 NaN 丢弃并记损失天数 |
| D3 | 缓存列选 | `lu_ratio` / `amt_yuan`；广度不入（守 ≤4 上限） |
| D4 | carry 起点 | 自 2015-04 首个有 carry 日起算，NaN 不入桶，逐变量记 n_obs |
| D5 | 成交额去趋势 | 强制 250 日滚动分位，绝对额陷阱明文登记 |
| D6 | 条件 IC 样本 | 20-offset 加权平均主口径；每桶 <100 天 → `-inf` |
| D7 | 统计量符号 | 有符号差 + 机器取 `|·|` 双侧；同号闸后置独立判 |
| D8 | 早/晚期切点与余弦 | 选择窗中位交易日对切；桶 IC 向量去均值余弦 > 0，硬闸 |
| D9 | 调节器映射 | 单点 `w(低效力)=0.5` 其余 1.0，不扫 w；换手只报告 |
| D10 | 尾部与延展 | 不延展；强制剔 2026-07-01 垃圾尾 |
| D11 | PIT 时点 | 状态与信号同时点（T 收盘），不额外 lag；部署可得性登记 |
