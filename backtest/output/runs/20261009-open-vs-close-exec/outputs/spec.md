# 开盘价 vs 收盘价执行检验（2026-10-09 插播）——固定规格

跑前冻结。本检验是**执行口径的测量**，不是信号搜索：不设 GO/STOP 闸门，不改生产，不改仓库。
结果只描述「换开盘价成交，绩效怎么变、变化落在哪类动作上」。

## 0. 问题与时点

生产信号在 T 日收盘后算出（约 20:30 推送），**T+1 日执行**。
- **E1 基准（用户说的「当前用收盘价」）**：T+1 收盘成交。
- **E2 备选**：T+1 开盘成交。
- **E0 参照**：T 收盘成交。这是回测引擎 `run_strategy` 的 `shift(1)` 口径，生产上不可行（信号 20:30 才有），只用来把 E1/E2 和历史回测的标题数字连起来。

## 1. 对象与冻结输入（`inputs/`，sha256 见 `inputs/snapshot.json`）

决策序列 `d_T` = 仓位文件里 T 日那一行（用到 T 收盘为止的数据算出）。

### 期货池 F
- 信号：equal_weight 20d40z，对称映射；`inputs/equal_weight_symmetric.csv`（date, position ∈ {−1,0,1}）。须核对它等于 `sign(factor_value)`（`inputs/equal_weight_signal_20d40z.csv`），不等则报错停下。
- 标的：IC/IM 实际合约，`inputs/futures.csv`。
- 持有合约：`held_g(t) = main_g(t−1)`，main = 当日 OI 最大，平局取 symbol 字典序最小。t 日一律用同一张合约算 gap / intra / cc（同 `backtest/exec_price_probe.held_contract_frame` 的口径）。若 held(t) 在 t 日无报价，退化为 main(t) 并计数报告。
  - `gap_g(t) = O_t / C_{t−1} − 1`
  - `intra_g(t) = C_t / O_t − 1`
  - `cc_g(t) = C_t / C_{t−1} − 1`
- 权重 `w(t)`：IM 首个报价日（应为 2022-07-22）及以前 IC=1；之后的交易日 IC=IM=0.5。整日都用 w(t)。
- 样本：第一个有 held 合约收益的日子（2015-04-17）到 2026-10-08。日历以期货日历为准；决策序列须覆盖每一天，缺日报错。

### 现货池 S
- 信号：slope20 L20zw120 long-flat；`inputs/slope20_longflat.csv`（position ∈ {0,1}）。
- 标的：blend = 000905.SH 与 000852.SH 等权、日度再平衡（同引擎：blend 的 cc = 两腿 cc 的平均）。`inputs/spot.csv`。
- **指数开盘价只在 2026-07-01 以后有（66 日）**，因此：
  - **S-proxy（主读数，近似）**：`intra_idx_g(t) ≈` 对应期货持有合约的 intra（000905↔IC；000852↔IM，IM 可用前用 IC）；`gap_idx_g(t) = (1+cc_idx_g(t)) / (1+intra_proxy_g(t)) − 1`，使 cc 保持精确。样本 2016-01-04 ~ 2026-10-08（2016 起期货交易时段与现货对齐）。
  - **S-exact（只描述）**：2026-07-01 起用真实指数开盘价。
  - **代理验证**：在有真实开盘价的日子比较真实 intra 与代理 intra（逐腿与 blend：相关、均值差、RMSE、符号一致率），另列这些日子里 S 的事件、精确效应与代理效应。

## 2. 执行口径（逐日收益模型，日度再平衡的名义敞口）

t 日开盘前持有 `p_old = d_{t−2}`，收盘后目标 `p_new = d_{t−1}`。
把 `p_old → p_new` 拆成四条腿（Δ 带符号，四腿之和 = p_new − p_old）：
- 开多 `+max(0, max(p_new,0) − max(p_old,0))`
- 平多 `−max(0, max(p_old,0) − max(p_new,0))`
- 开空 `−max(0, max(−p_new,0) − max(−p_old,0))`
- 平空 `+max(0, max(−p_old,0) − max(−p_new,0))`

策略 M_A（A ⊆ 四腿：A 中的腿在 t 日开盘成交，其余在 t 日收盘成交）：
- `p_mid(t) = p_old + Σ_{leg∈A} Δ_leg`
- 毛收益 `r_t = Σ_g w_g(t) · [ (1 + p_old·gap_g)(1 + p_mid·intra_g) − 1 ]`
- **E1 = M_∅**（r_t = p_old·cc），**E2 = M_全部**。
- **E0**：t 日整天持有 `d_{t−1}`，`r_t = Σ_g w_g · d_{t−1} · cc_g`。

成本：每单位 |Δ| 收 3bp（开盘或收盘成交同价），落在 t 日；E0 的成本落在其持仓变化日（同引擎）。
F 的换月：`held_g(t) ≠ held_g(t−1)` 的日子另收 `2 × 3bp × |该日开盘前持仓| × w_g(t)`，各口径同规则。
S 无换月、无 carry。F 用真实期货价格，**不加 carry**（基差已在价格里）。

要报告的策略：
- F：E0、E1、E2、M_{开多}、M_{平多}、M_{开空}、M_{平空}、M_{平多,开空}（多翻空整笔开盘）、M_{平空,开多}（空翻多整笔开盘）。
- S：E0、E1、E2(=M_{开多,平多})、M_{开多}、M_{平多}。

## 3. 主表：逐动作事件归因（回答「四类动作分开看」）

每个执行日 t（`d_{t−1} ≠ d_{t−2}`）的每条非零腿记一条事件：
- **开盘 vs 收盘效应** `e = Δ_leg × Σ_g w_g intra_g(t)`（正 = 开盘成交更好）。
- **隔夜效应** `o = Δ_leg × Σ_g w_g gap_g(t)`（正 = 隔夜已朝新仓位方向走，等到开盘就错过了；即 E0 相对 E2 的差）。

按动作类型报告：事件数、e 的均值/中位数/标准差（bp）、开盘更好的占比、合计（pp）、年化贡献（合计 ÷ 样本年数，pp/年）、t 检验 p、符号检验（二项）p；F 全窗四腿的 Holm 校正 p；**去漂移均值** = 均值 of `sign(Δ) × (intra_t − 该窗全部交易日 intra 均值)`；o 的均值与合计。
F 的仓位在样本内恒为 ±1，因此平多与开空、平空与开多总是同日发生、单腿效应相同。表里要明说这一点，并另列两种整笔事件「多翻空」「空翻多」（效应 = 两腿之和）。
分年表：年份 × 动作，e 与 o 的合计（pp）和事件数。

## 4. 绩效表

每个策略、每个窗：算术年化（house `backtest.metrics.ann_return`）、CAGR、Sharpe（house `sharpe`）、最大回撤（house `max_drawdown`）、年化波动、日数。
相对 E1 的 Sharpe 差：配对 moving-block bootstrap，复用 `backtest.paired_bootstrap.paired_block_bootstrap_sharpe_diff`，block=20、n=2000、seed=20261009。

窗：
- F：full（2015-04-17~2026-10-08）、2015-2020、2021-2023、2024-2026、dual_listed（2022-07-25 起）、ex2015（2016-01-04 起）。
- S-proxy：full（2016-01-04~2026-10-08）、2016-2020、2021-2023、2024-2026。

## 5. 锚点与自检（任一不过须报告，不得静默放行）

1. F：用 `backtest.execution_ledger.contract_ledger`（fill='close' / 'open'，3bps，权重 `futures_weights`，到期表同 `backtest/execution_audit.inputs`）在同一快照上跑 E1/E2，与本模型比较 full 窗年化、Sharpe、最大回撤和日收益相关。差异应只来自换月时点和日度再平衡成本；给出量级。另列 09-14 权威 run `backtest/output/runs/20260914-execution-audit-r2/outputs/execution_panel.csv` 里 futures_close_3bps / futures_open_3bps 的 full 窗数字作历史对照（信号文件版本不同，不要求一致）。
2. S：E0 必须与 `backtest.engine.run_strategy(position, blend_cc, 3.0)` 的 `ret` 在窗内逐日一致（首日建仓成本的已知怪癖除外）。
3. 可加性：每个窗里 E2 − E1 的逐日差之和 ≈ 全部腿的 e 之和，报告二阶项残差。
4. 时点自检：把决策序列人为前移、后移一天各重算一次 F 的 E1/E2 年化，证明主口径与它们不同（防止差一天）。
5. F 涨跌停：执行日持有合约若 `|O/pre_settle − 1| ≥ 9.5%` 或 `O = H = L`，标为「开盘可能成交不了」，列清单；主表另给剔除这些事件的版本。

## 6. 产出（写在自己的目录里，不改仓库）

events_F.csv、legs_F.csv、policies_F.csv、by_year_F.csv、limit_F.csv、
events_S.csv、legs_S.csv、policies_S.csv、by_year_S.csv、proxy_validation_S.json、
checks.json（第 5 节全部自检结果）、report.md（中文，简明表格）。

## 7. 解读边界（写进 report.md）

- 开盘集合竞价与收盘的滑点、冲击成本不同，本检验不建模，成本两边同价。
- F 的事件约每年十来次，单类动作的检验力很低；「不显著」只说明没看出差别，不说明没有差别。
- 在样本里挑「哪类动作用开盘、哪类用收盘」是 2^4 种组合里选优，带选择偏差，不能直接当部署依据。
- S 的主读数是代理，精确版要真实指数开盘价（Wind 导出）。
