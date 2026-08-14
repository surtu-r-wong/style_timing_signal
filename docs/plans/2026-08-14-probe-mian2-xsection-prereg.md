# 探针 面2 预登记冻结：个股截面二阶矩（离散度 / 平均相关性）（2026-08-14）

> **命题定位（② 档案纪律 (b) 的直接兑现）**：Batch 9 的 ② 测的是**四对配对因子之间**的
> 二阶矩（已 STOP）；本探针测的是当年被"离散度≈广度"**假设**跳过、从未实测的
> **个股层面**截面二阶矩——X1 截面收益离散度、X2 个股平均相关性。
> 测完本探针，"库内零成本公开信息面测尽"才第一次真正关帐（正负结果都关）。
> **X1 与 X2 不是两个独立信息面**（Solnik-Roulet 代数：ρ̄ ≈ 1 − mean(截面方差)/平均个股方差，
> X2 是 X1 被个股波动水平归一化的单调反向变换）——预登记明写此点，
> 并强制报诊断 `corr(X1, X2)`（水平量 + 赢家口径信号化后各一版），
> 不许重演"用假设代替实测"。

状态：**冻结**。提交后族/网格/口径/闸门不得增改。用户 2026-08-14 拍板补测（面2 不在
2026-08-12 裁决队列内，本次选择即拍板）。技术前提附录（同日只读侦察，一并提交）：
`docs/plans/2026-08-14-probe-mian2-tech-annex.md`。框架沿用 ②：
`backtest/divergence_probe.py` 的三规矩、评分与闸门机器**原样复用**。

## 1. 数据与宇宙

- 表：`stock_selector.stock_daily_price`（原表，**不用** `_qfq` 视图——它是每查全表 GROUP BY
  的 VIEW，且 close/pre_close 比值对复权缩放不变）。18,329,890 行 / 1990-12-19~2026-08-13。
- **宇宙（D1）**：`(ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ') AND close IS NOT NULL AND
  pre_close > 0 AND volume > 0`；**不含 `.BJ`、不含 `.HK`**，不做市值/流动性/ST/次新筛选。
  这是 thermo 现成行条件的等价修正版（把它本就想排的非 A 股真排干净），零新自由度。
  **数据质量登记（本次侦察新发现）**：该表自 2025-01-02 起混入港股（218,613 行、
  2026-06-30 单日 616 只 `.HK`）；已提交的 `breadth.csv` 自 2025-01 起分母被污染，
  `thermo_probe._ROW_FILTER` 前缀过滤漏放 49 只港股——本探针宇宙已排除；
  上游影响另行登记，不在本探针处置。
  `stock_status` 表 2023 前极度稀疏（2014 仅 244 行），**禁止用于本探针任何过滤**。
- **收益口径（D2）**：`close/pre_close − 1`（`pct_chg` 列近端全 NULL，禁用）。
  **不做任何异常收益剔除/缩尾**；X1 含新股首日尾巴，写进限制段（`|ret|>30%` 年 19~460 行）。
- **序列起点（D5）**：SQL 起点 2012-01-01；`warmup_masked_level_signal` 照 ② 把前
  `zw+lb−2` 行置 NaN。闸门窗 2014-01-02 起满样本，**n_obs = 2,434**。
- **PIT（D8）**：无 `pit_lag`（当日收盘即知，与 `thermo_probe.py:11-12` 逐字同约定）；
  表内 `available_at` 列**明确不使用**（用则与全仓库信号线口径不一致）。

## 2. 两族状态序列定义（D3/D4/D6）

| 族 | 定义（钉死） |
|---|---|
| **X1 截面离散度** | 每日宇宙内个股收益的横截面 `STDDEV_SAMP`（ddof=1，原始有符号收益，不去均值处理）。**明确不采纳**"绝对收益截面均值"（一阶量，命题错配）。SQL 全下推（GROUP BY trade_date），实测 11 s / 3,547 行 |
| **X2 平均相关性** | 方差比隐含平均相关（Solnik & Roulet 2000 / Driessen et al. 2009）：滚动 **60 交易日**窗（`ROWS BETWEEN 59 PRECEDING`，跨停牌原样接受写限制段），**σ_p 用与 σ_i 同一子集**（窗内满 60 天的票，`FILTER (WHERE c60=60)`）的等权日收益——否则 ρ̄ 出界。SQL window 下推，全历史 ≈4~5 分钟 |

- **N 趋势（D6）**：单日截面 2014 年 ~2,301 → 2026 年 ~5,186，不做任何样本量调整；
  登记为限制 + 强制诊断列 `corr(X1_level, N_t)`、`corr(X2_level, N_t)`。
- **重合性（D7）**：保留两族（只留 X1 = 只还半笔债，议程原文点名了平均相关性）；
  预登记明写"两族共享分子、非独立信息面"；多重比较仍按名义 32 点合成单次调用（保守方向）。
- **大内存红线**：禁止 fetchall 个股长表进内存（2012 起 ≈2.8 GB 客户端峰值 = OOM 前科路径）；
  两族全部 SQL 侧聚合，客户端只收 ~3,700 行序列。

## 3. 网格与机器（沿用 ② 逐字）

- 族 2 × `lb∈{5,20} × zw∈{60,250} × k∈{5,10,20,40}` = **32 点合成同一次**
  `selection_permutation_test` 调用；统计量 = 无约束 `|非重叠 rank IC|`（关1）与
  `|偏 IC（控生产 ew）|`（关1b 机器，`abs_partial_batch` + `partial_rank_ic` 原样复用）；
  只认 `p_selected`；`-inf` 只给无法评分行。
- 房规：`min_shift = 2·max(k) = 80`、`max_shift = n_obs − 80 = 2,354`。
- **不用 `run_families_probe`**（其 `HALVES`=2020-2026 会把 holdout 拉进闸门）；
  复用 `divergence_probe` 的 `variant_grid / warmup_masked_level_signal /
  build_variant_signals / ScoreFrame / build_score_frames / abs_spearman_batch /
  abs_partial_batch / make_batch_scorer / build_ic_panel / run_machines /
  p_at_threshold / net_sharpe_by_window / evaluate_gates`，净新码只有族常量、
  两族序列装载、SQL 缓存构建。
- ② 的"tanh 有界⟹构造上平稳"论证**对面2 不成立**（X1 无上界、X2 的 N 有 ×2.3 趋势），
  限制段必须改写为面2 自己的平稳性说明（z 化后仍须报警惕）；② §9 限制 1
  （k=40 仅 ~60 窗、常在零分布当选抬空分布水位）照抄适用，**禁止事后砍 k=40**。

## 4. 三关闸门（② (f) 原文数值，全过才 GO，任一不过 → STOP 归档）

| 关 | 判据 |
|---|---|
| 关1 | 置换 `p_selected < 0.05` **且** 关1b 偏 IC（控生产 ew）同号 `p_selected < 0.05` |
| 关2 | 两半窗同号，切法 = **train(2014-2020) / val(2021-2023)**（与 ② 自由度 7 同裁定） |
| 关3 | 成本后净 Sharpe > 0（`engine.run_strategy`，cost_bps=3.0 + blend carry） |

样本硬下限沿用 `divergence_probe` 现成：`len(pts)<10` 抛异常 / 诊断 `n_obs<30` 不出行 /
净值窗 `len(s)<60` 跳过。控制变量只控生产 ew（D9；控广度是更强命题留给 GO 分支，不加码）。

## 5. 诊断列（不进闸门）

- `corr(X1, breadth.pct_above_ma20)`：给当年"离散度≈广度"跳过理由第一个数字。
  **限选择窗内计算**（2014-2023 三方日历实测 2,434 天差集为 0；breadth.csv 2025 后含港股
  污染且止于 2026-06-30，选择窗内不受染）。
- `corr(X1, X2)` 两版（§2 D7）；`corr(·, N_t)` 两条（§2 D6）。
- 侦察阶段**刻意未算**上述任何相关数（预登记冻结前算=烧自由度），本文档冻结后才许算。

## 6. 既有怪癖登记（照 ④ 写法，原样保留不顺手修）

`engine.py:24` shift(1)=T 收盘成交（文档债）；`engine.py:27-28` 首日成本落第二天；
blend carry 缺腿按 0、止于 2026-04-29（holdout 段标注，闸门只读 train/val 不受染）。

## 7. 执行与产物（D12/D13）

- 本机跑（算力 ≤② 量级，内存 <100 MB；唯一成本 = 一次性 SQL 缓存 ≈5 分钟）。
- **PG 纪律（硬要求，吸取 ④ 偏离 G）**：`connect_timeout=15` **且显式设
  `statement_timeout=600000`（600 s）**；先探连接；拒连不滚动重试；
  库主机走 `config/settings.yaml`（Debian，本次侦察 18 条查询全通），留 `--db-host` 备用。
- 运行参数：`n_perm=1000`、`seed=0`。
- 代码：`backtest/xsection_probe.py`（新模块；`signals/` 与四台既有机器零改动）
  + `tests/test_bt_xsection_probe.py`（判例覆盖：三规矩、宇宙过滤（含港股排除）、
  X2 分母子集一致（ρ̄∈[−1,1]）、暖机掩码、样本下限、诊断限窗）。
- 产物：`backtest/output/probe_xsection_{panel,permutation,diagnostics,verdict}.csv`
  + `probe_xsection_summary.json` + 两份一次性缓存
  `backtest/output/xs_dispersion.csv`（含 n/xs_sd/xs_mean/xs_med）、
  `backtest/output/avg_correlation.csv`（X2 原料 n_full/sum_sd/sum_var/ew_ret_full）。

## 8. 预算上限

3 个工作日（② 同级探针实跑 1 天量级）。

## 9. 自由度决议表（对附录 §7 的逐项裁决）

| # | 决议 |
|---|---|
| D1 | 宇宙 = SH/SZ 后缀 + thermo 三行条件，无 .BJ/.HK，无任何风格筛选；stock_status 禁用 |
| D2 | close/pre_close−1；不剔异常收益（新股首日尾巴入限制段） |
| D3 | STDDEV_SAMP 有符号收益；不采纳绝对收益均值 |
| D4 | 方差比隐含平均相关，60 日窗，σ_p 同子集（c60=60）；跨停牌原样接受 |
| D5 | SQL 起点 2012-01-01，warmup 掩码，闸门窗满样本 n_obs=2,434 |
| D6 | 不做 N 调整；限制段登记 + corr(·,N_t) 诊断 |
| D7 | 保留两族，明写非独立面，强制 corr(X1,X2) 诊断；名义 32 点不修正 |
| D8 | 无 pit_lag；available_at 明确不用 |
| D9 | 只控生产 ew；广度只诊断不控制 |
| D10 | 沿用 divergence 三条硬下限；k=40 限制照抄，禁事后砍档 |
| D11 | 两半窗 = train/val |
| D12 | n_perm=1000/seed=0/3bp+carry；settings.yaml 主机 + 显式双超时 |
| D13 | 模块 xsection_probe.py，产物 probe_xsection_*（附录建议的 mian2 命名改为英文语义，与兄弟探针一致；中文档案仍称"面2"） |
