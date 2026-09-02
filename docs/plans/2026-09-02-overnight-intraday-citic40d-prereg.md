# 候选 ⑥ 隔夜 / 日内分解 —— citic40d 对象预登记（2026-09-02，冻结后再跑）

**状态**：冻结规格，尚未运行。议程原登记（`2026-08-12-signal-research-agenda.md` §⑥）的对象是 equal_weight 四对；
本次数据摸底证明 1000/2000 对在 Wind 里**从未有 open**、500 对 2018～2024 断档，equal_weight 对象**数据不可做**。
用户裁「其余按你建议」→ 对象改为 **citic40d**（五个中信风格腿全历史有 open），规格独立重新预登记；原 ⑥ 登记条目改为 data_blocked 说明，不作废其命题。

## 0. 命题（可证伪）

把中信五腿的区间收益拆成**隔夜段**（昨收→今开，承载 T+1 制度效应与隔夜信息冲击）与**日内段**（今开→今收）后，
用隔夜段或两段合成替代全日收益作为 citic40d 五因子的输入，其对交易标的的预测力与 long-flat 收益优于现任（全日收益，lb 20 / zw 40 / sm 0）。
先验不利：收益变换器已五次确认；本命题是变换器族里唯一有**制度性**而非统计性理由的一个，故值得测一次并关帐。

## 1. 数据（已就绪，本记录前一提交）

- `stock_selector.index_daily` 的 CI005917～CI005921.WI open/high/low 已回填（`data_fixes/2026-09-02-index-daily-ohl-backfill/`）。
- 已知缺陷：2010-01～2012-02 全为平 K 线（open=high=low=close，无日内数据）；2012-03 起仅 9 个孤立平 K 线日
  （2013-05-21、2013-08-01/02/13/14、2014-01-16、2014-11-04、2016-03-29、2024-09-13）。
- **处理规则（冻结）**：平 K 线日按原始数据处理——隔夜段吸收全日收益、日内段为 0，不做任何插补或剔除。
  评窗从 2014-01-01 起（2012-03 后的 warm-up 已足），评窗内受影响 4 天，量级不可能改变裁决；结果文档必须报告这一点。

## 2. 分解与因子（冻结）

每条腿 $P$：$r^{on}_t=\ln(O_t/C_{t-1})$，$r^{in}_t=\ln(C_t/O_t)$，恒有 $r^{on}+r^{in}=\ln(C_t/C_{t-1})$。
把各段累成**合成价格** $\tilde P^{on}_t=\exp\sum_{s\le t} r^{on}_s$、$\tilde P^{in}_t$，然后**原样复用** `signals/citic40d/generate_signal.compute_mean_factor`
（spread_N = 两腿 N 日对数收益差 → 滚动 z → tanh → 五因子等权），不改一字。

三个口径：
- `overnight`：五腿全部用 $\tilde P^{on}$；
- `intraday`：五腿全部用 $\tilde P^{in}$；
- `fused`：`(overnight 因子 + intraday 因子) / 2`，固定 50/50（沿用 `fusion_probe.fuse_equal`，不扫权重）。

**前置自检**：用 $C$（收盘）走同一代码得到的「全日」因子，必须与 committed `output/citic40d/citic_style_signal_40d.csv` 的 `factor_20`
在共同日期上 round(4) 后逐位一致（08-26 预筛同款自检），否则 ABORT 不出任何数。

## 3. 参数空间（冻结，缺则不得开工）

3 个口径 × lb ∈ {20} × zw ∈ {40, 120} × sm = 0 → **6 个点**。lb 锁 20（载频结论已定型，07-10 §7.1/7.2），sm 锁 0（citic40d 现役无平滑）。
不扩网格；若日后要扩，按 07-11 勘误 §4 第二支另开独立确认窗。

## 4. 秤（与既有闸门完全同秤）

- 标的收益 `load_underlying_returns("blend")`（500/1000 等权）+ `load_carry("blend")`，成本 3 bps/边，T+1 生效（`engine.run_strategy`）。
- 仓位两口径：对称 `to_position(mode="discrete")` 与生产 long-flat `production_position`。
- 三窗：train 2014-01-01～2020-12-31 / val 2021-01-01～2023-12-31 / holdout 2024-01-01～2026-12-31；**选择只看 train/val**，holdout 只报告。
- 现任 = `citic40d_factor_fn()(lookback=20, z_window=40, smoothing=0)`（与 `backtest/scan.py` 的 SIGNALS 标注一致）。

## 5. 闸门（任一不过 → STOP 归档；全部事前定）

- **⓪ 同秤头对头非偏 rank IC**（`fusion_probe` 机器：k=20 非重叠网格、offset 0、配对 i.i.d. bootstrap n=10,000、seed 0，标的 blend）：
  候选在 train 与 val **双窗 rank IC 均严格高于现任**，且至少一窗配对差的 95% CI 不含 0。
- **① 三窗全正且 worst(train,val) ≥ 现任同窗**（对称口径 Sharpe，含 carry）。
- **② 同秤 long-flat 全口径不劣**：500 / 1000 / blend 三口径 long-flat 的 train、val Sharpe 均 ≥ 现任。
- **③ 明确优于而非平手**：long-flat blend 的 worst(train,val) 提升 ≥ 0.10（`GATE_WORST_TV_LIFT`）；平手 → 不换，记「收益变换器第六次确认」。

6 个点里任一点全过 → 该点进入"生产切换另开 plan"（不在本记录内切换）。6 点全 STOP → ⑥ 关帐，变换器族封轴。
置换 / 多重比较：6 点选优，⓪ 的 p 只作诊断列不作闸门（与 07-10 同）；若日后要把 p 升格为闸门，须先付候选 ⓪（改造置换选优机器）。

## 6. 产出与引用纪律

- 机器：`backtest/overnight_probe.py`（新模块，不碰 `signals/` 生产代码），测试 `tests/test_overnight_probe.py`。
- 输出：`backtest/output/overnight_probe/`（scan、head2head、ic、gates、verdict.json）；结果文档另开。
- 无论结果如何，关的是「citic40d 五腿 × 隔夜/日内/合成 × lb20 × zw{40,120}」这一族；**不得引作**"隔夜效应无价值"或"equal_weight 上已测"（后者数据不可做）。
