# Phase 3 双引擎 v1 组装 — 实施计划（2026-07-06）

架构层设计见 `2026-07-03-optimization-roadmap-design.md` §1（多空分离双引擎，架构 (c) 演进式）。
本文只定 **v1 具体落地范围 + 我在用户"继续"授权下自主拍的决策**（可回测、可 revert）。

## 0. v1 目标（一句话）

把"现有信号的多头段 = 多头引擎、现有信号的空头段经**环境门槛**过滤 = 空头引擎、执行层合成"
的框架立起来，用**当前已有数据**跑出分开评价，验证核心假设：

> **刻意抬高空头门槛 + 深贴水禁空，是否让双引擎优于原始对称信号**（基线实测三线空头段弱/负）。

## 1. 自主决策（用户 60s×2 无应答 + "继续"，按推荐执行，flagged 可推翻）

| 决策 | 选择 | 理由 |
|---|---|---|
| v1 基信号 | **equal_weight blend** | 基线最强 Sharpe 1.39；连续 tanh 因子→干净离散化；框架 demo 最清爽 |
| 空头环境门槛（v1） | **仅 carry 保护层 + 非对称阈值** | 这两个用现有数据（futures_daily carry + 信号自身）零成本可建 |
| 富环境层（两融/题材/微盘/成交额分位） | **推迟 Phase 4** | 需 Wind P1.5 数据，未入库；框架先行不阻塞 |
| 合成规则 | net = long + gated_short，both-fire→0 | §1.3；单基信号下多空互斥，both→0 在 v1 恒真、为 Phase 4 多信号预留 |
| hybrid20 阈值扫描 | **并行子交付** | 用户 (b) 明确"顺带扫"；informs 最终空头阈值该不该"门槛更高" |

## 2. 关键语义澄清（写死进设计）

- **信号 = 对指数（IC/IM）择时**，不是交易风格价差本身。offensive/defensive 风格强弱是"市场 risk-on/off"的择时代理：因子高→risk-on→做多指数；因子极低→risk-off→做空指数。
- **hybrid20 现阈值与设计意图相反**：open_long 0.35 > |open_short| 0.15 → 现状是"更易做空"，属 Sharpe 优化产物，非"空头门槛更高"的刻意设计。v1 把"空头门槛更高"做成**显式假设**（short_theta > long_theta）并回测裁决；hybrid20 阈值扫描顺带查其 walk-forward 最优阈值到底偏哪边。
- **carry 正=贴水**（data.py 口径）。深贴水（carry ≥ θ_carry）→ 做空付高额 carry 逆风 → 禁空（gate 为 0）。

## 3. 模块设计（全部加进 backtest/，加法、不动现有）

- `positions.py` 增 `to_position_asym(signal, long_theta, short_theta)`：
  `factor > long_theta → +1`；`factor < −short_theta → −1`；否则 0。（short_theta、long_theta 均 ≥ 0，取绝对阈值）
- `gates.py`（新，环境/保护层归口，Phase 4 扩）：`carry_protection(position, carry, theta)`：
  `position < 0 且 carry ≥ theta` 的日期 → 0（深贴水禁空）；其余不动。
- `dual.py`（新，双引擎装配 + 评价 + CLI）：
  - `synthesize(long_leg, short_leg)`：`net = long_leg + short_leg`，多空同触发(long>0 & short<0)→0，clip 到 {−1,0,+1}
  - `assemble_dual(factor, carry, long_theta, short_theta, carry_theta)`：非对称映射→carry 门控空头→合成 net
  - `hedge_value(dual_ret, long_only_ret, underlying)`：①down-month hit：underlying 月收益 < −5% 的月里，dual 月收益 > long_only 月收益的占比；②maxdd_improve：|maxdd(long_only)| − |maxdd(dual)|（正=回撤改善）
  - 编排：口径×窗口，对比 **buy_hold / raw_symmetric / long_only / dual**，复用 baseline.evaluate 的 full/long/short 分段 + significance.bootstrap；产出 `backtest/output/dual_engine_metrics.csv`

## 4. 参数默认（均可扫，先给逻辑先验）

- `long_theta = 0.10`（门槛低、信趋势）、`short_theta = 0.30`（门槛高、显式假设）、`carry_theta = 0.06`（≈IC 贴水中枢 8.8% 的保守档，深贴水禁空）
- 均暴露为参数；v1 用默认跑通 + 出一版对 short_theta/carry_theta 的粗敏感性，正式高原扫描并入 scan 框架（后续）。

## 5. Task 分解（TDD，逐 task commit）

- **T1** `to_position_asym`（positions.py）— 非对称离散映射
- **T2** `carry_protection`（gates.py）— 深贴水禁空
- **T3** `synthesize`（dual.py）— 合成 + both-fire→0
- **T4** `hedge_value`（dual.py）— down-month hit + maxdd 改善
- **T5** `assemble_dual` + 编排 + CLI + 出 `dual_engine_metrics.csv`（真实 PG 跑）
- **T6** hybrid20 4 阈值 walk-forward 扫描（并行；bespoke，非 §3.2 网格）

## 6. 评价与成功判据

v1 成功 = 框架跑通且产出可解释对比表。**信号结论**看：
- 多头引擎 vs buy_hold：超额 + 回撤改善（预期成立，基线 long 段已强）
- 双引擎 net vs raw_symmetric：碳门槛+高空头阈值是否**至少不劣、且回撤更好**
- 空头引擎避险价值：down-month hit + maxdd_improve 是否显著（即使独立盈亏平平，避险价值高即保留）

不追求 v1 就跑赢——框架 + 干净的可回测裁决就是交付。
