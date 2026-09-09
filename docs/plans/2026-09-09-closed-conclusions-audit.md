# 关帐结论全面复审（2026-09-09）—— 51 条登记逐条核对

缘起：当日查出两类错误后用户要求「回顾所有关账的研究结论，看看有没有错误结论」。
四个只读子代理按统一检查清单（测试对象≠结论对象 / 证据早于修复 / 偏好写成支配 / 措辞超范围 / 引擎口径 / 低功效写成无信息 / 叙事当证据）逐条审，
我复核被标记条目并执行处置。任务书与四份报告见会话记录；处置全部已落登记表（`research_registry.yaml` 各条 caveats/claim）与原文档更正块。

## 1. 判定分布（51 条）

| 判定 | 数 | 条目 |
|---|---|---|
| 成立 | 27 | 略（登记表未动或只加注） |
| 措辞过宽 | 9 | dual-engine-v1（源头）、index-short-trigger「第六次确认」、staged-entry「机制被证伪」、overnight「变换器族封轴」、mapping-grid-32「现役保持不变」、money-flow 复制「镜像/反向」、analyst-revision「形态矛盾非功效」、signal-generator-smoothing / cusum-hamilton（预筛写成 stop） |
| 证据过期 | 9 | long-axes（C1/C2 信号=修复前 carry）、breadth-divergence（空头触发线，07-11「重跑只会强化 STOP」方向说反）、basis-c1-retest、thermo/leverage 关 3 数字、momentum §7 扫描、rotation-short-window、b2、tail-fifth-bucket（09-02 伪行修复后未重跑）、carry-mode / yearly-concentration（现役切对称后未刷新） |
| 疑似错误 | 4 | gate0-criterion claim「同一条信号 0.293/0.013」（两个变体缝在一起）、gateway-provenance-gap（09-04 已回收，登记未更新）、equal-weight-5d20z / b1 / b2 evidence 指错文件 |
| 无法核实 | 2 | production-equal-weight-symmetric 依据数字无提交脚本（**已补** `backtest/short_leg_audit.py`）、现网网关状态 |

**没有一条 GO/STOP 裁决被推翻。** 错的是四类东西：源头叙事（空头无价值）、标签计数（第 N 次确认）、过读（镜像）、证据溯源（指错文件/未重跑）。

## 2. 修完重跑的三条（用户规则：机器/数据修了就一律重跑）

| 线 | 旧读数（07-08/09，carry bug 下） | 修复后重跑（09-09） | 结论 |
|---|---|---|---|
| long-axes **C1** 主力合约基差水平 | lb20zw60/k10，IC −0.107，p **0.090**，净 Sharpe 0.51（全批最贴线） | 代表换为 lb5zw60/k40，IC −0.068，p **0.605**，偏 IC p 0.670，净 Sharpe 0.27 | **贴线是 bug 伪影**：修复前 IM 上市处的人为断点给 z 化序列造了结构。STOP 加强；`basis-c1-retest` 由 blocked 改 closed/stop |
| long-axes C2 | IC −0.143，p 0.223 | IC −0.131，p 0.383 | STOP 不变 |
| long-axes B1/B2/B3/E1/E2 | 关 1 与 carry 无关 | IC/p 逐位不变，仅净 Sharpe 微动 | 不变 |
| breadth-divergence 冻结候选（report 模式） | short_bd_engine 500/full −0.99，blend 各窗全负 | −1.03；blend 2014-20 / 21-23 / 24-26 短引擎 −0.14~−0.53 全负 | STOP 不变；07-11「重跑只会强化 STOP」的方向论证是错的，但结果恰好未翻 |
| breadth 168 组扫描 | 最优 dual 1.54 / 0.76 / 1.75 | 1.44 / 0.65 / 1.61（short_frac ≤0.2%，门控空头几乎不触发） | 不变 |

补跑的证据脚本：`backtest/short_leg_audit.py` → `short_leg_audit.json`（现役空头腿切面、逐年、贴水门控、10 种映射、5 条贴线候选空头腿、对称仓位承载模式）。
对称仓位下三种承载的年化 carry 净贡献均为负（A 自然 −1.4%、B 全 IM −2.8%、C 全 IC −2.7%，A 含 IM 上市前半仓伪影），模式选择是二阶问题。

## 3. 系统性教训（写进检查模板）

1. **「第 N 次确认」是最危险的句式**：六次「空头无价值」的对象没有一次是现役空头腿；十次「覆盖度确认」把低功效未检出累加成了定论。登记表已逐条改为「本规格未检出」。
2. **预筛不是 STOP**：两条 signal-generator 线只做了分歧天数秤就登记 closed/stop，与 08-20 §7.8「预筛不得作为不跑的理由」相抵；outcome 改 descriptive。
3. **修复后必须重跑而不是推理方向**：07-11 对广度线的「重跑只会强化 STOP」推理方向反了；这次恰好没翻，下次未必。
4. **偏好项不进否决**：mapping-grid-32 的②③、staged-entry 的回撤/换手、dual-engine 的 maxdd_improve 都把风险偏好当成了支配关系。
5. **证据要能自证**：五份 07-08/09 的 verdicts 从未在 carry 修复后重跑；三条登记 evidence 指错文件。

## 4. 留给用户的两项裁决

- **半仓空头 (0,0,−0.5)**：08-13 冻结的 32 格赢家，对对称参照五窗 Sharpe 全高（full 1.636 vs 1.412）、回撤浅 10pp、换手低 25%，只输年化（31.0% vs 35.8%）。是当前对称部署的中间档，改一处映射即可。
- **两条 signal-generator 预筛线**是否补跑收益层配对检验（每变体约 1 分钟），否则维持 descriptive。
