# 外部审查修复记录（2026-07-11）

一份外部代码审查提出 6 条发现（4 P1 + 2 P2）。逐条对代码 + 生产库核实：**六条位置与机制全部属实，无一误报**；但影响面与修法经量化后与审阅建议有实质出入。本文档 = 勘误单一入口，历史文档中与此冲突的数字一律以本文为准。

## 结论一览

| # | 审阅发现 | 核实 | 处置 |
|---|---|---|---|
| 1 | P1 财务 PIT 前视（min 截断） | ✅ 机制属实，但审阅药方不可行（见 §1） | 文档修正（两仓同步），开放项：引真实首披日 |
| 2 | P1 blend carry 单腿放大 | ✅ 且比审阅估计更重（66% 天数受影响） | **代码修复 + 全部 headline 重算**（§2） |
| 3 | P1 holdout 进选择 | ✅ 但生产参数本身不由 holdout 调出 | 选择规则改 train/val，2024-26 降级（§3） |
| 4 | P1 选优后 p 值未校正 | ✅ 但全部受影响决策均为 STOP，反向稳健 | 不改机器，设重开条件（§4） |
| 5 | P2 金融行业标签穿越 | ✅ 且比审阅以为的可修（69 期快照在库） | 开放项，下次动 style basket 时修（§5） |
| 6 | P2 仪表盘新旧文件混显 | ✅ | **已修**：警示行 + 合同守卫测试（§6） |

测试：修复后全套 **209 passed**（新增 5 项）。

---

## §1 财务 PIT（audit 第 1 条）

**核实**：`financial_reader.py` 确实取 `min(库存 ann_date, 法定截止)`，晚披报告会被提前到法定日——机制属实。但查生产库后发现更根本的事实：

- CSMAR 季末行 1,106,803 中 **805,942（73%）ann_date 晚于法定截止**，且聚集在 ~8 个季度批次日（2023-01-28 / 2023-07-29 / … / 2025-07-29，每个 ~5,000+ 行），最长"晚"6,206 天。
- 即 **CSMAR 的 DeclareDate 是数据集导出/更新日，不是首次披露日**。审阅建议的"真实日期为主、法定日只兜底"没有数据可依。
- Wind 段 56,493 行零晚披，干净。

**实际语义**：绝大多数 CSMAR 历史的可知日 ≈ 法定截止日——对按时披露者（绝大多数）保守（滞后），前视只暴露在真正晚披的个股（~1%/年，多 ST/问题股）。

**已做**：`legal_disclosure_deadline` docstring 原文写 "conservative (we never look-ahead)"，方向恰好想反——已改为如实记录上述语义与残留风险；`style_timing_signal` 拷贝与 `stock_selector` 源两侧同步（纯文档零行为）。

**开放项**：如需根治，从 Wind wss 取真实首披日替换晚披尾部。风格篮子/分解探针的结论量级不受此撼动（受影响集合小且多被 U2 过滤）。

## §2 blend carry（audit 第 2 条）——唯一动 headline 的一条

**核实与量化**：`load_carry("blend")` 原用 skip-NaN 均值（"有一个用一个"）。IM 上市（2022-07-22）前 1,770/2,677 天（66%）只有 IC，期间 IC 年化贴水均值 +22.5%——单腿期 blend carry 被按全额而非半仓计。

**修复**：`backtest/data.py` 新增纯函数 `blend_carry`（固定 50/50，缺腿按 0，与引擎对无 carry 日期的处理一致），TDD 两测锚定。

**重算结果**（equal_weight blend · full，同数据 A/B）：

| 口径 | 修正前 | 修正后 | 备注 |
|---|---|---|---|
| **long-flat（部署口径 production_position，阈 0）** | 1.78 / −16.7% | **1.62 / −16.7%** | README 新 headline |
| 对称多空（阈 0） | 1.37 / −30.2% | 1.41 / −29.3% | 空头少付 carry 受益 |
| 双引擎多头腿（θ=0.10，Phase 3 表） | 1.42 / −13.9% | 1.28 / −14.0% | 该口径 Sharpe 排序对 1.42 对称翻转 |
| 空头引擎（θ=0.30 + carry 门控） | −0.42 | 0.01（p=0.13） | 仍无显著独立价值 |
| CITIC 轴 16 组阈值 short_sharpe | [−0.07, +0.04] | **[+0.11, +0.21] 全转正** | "无一盈利"佐证不再成立 |

- 旧 README 表的 1.42 实为 θ=0.10 双引擎多头腿，与部署口径（阈 0）本就不同秤；本次一并改正，README headline 换为部署口径同秤对比。
- **推荐维持 long-flat**：同秤下 Sharpe 1.62 > 1.41、MaxDD −16.7% vs −29.3%、Calmar 1.58 vs 1.22，差距收窄（0.41→0.20）无翻转。
- **诚实降级**："空头段无价值" → "无显著独立价值"（CITIC 阈值全转正、equal_weight 空头段 Sharpe 0.48；但空头引擎 0.01/p=0.13，加空腿 MaxDD 反差 6.5pp、跌月命中 43.5%<50%）。若未来重议对称口径，先做两口径 Sharpe 差的同秤 bootstrap。
- 重跑并已提交的产物：`dual_engine_metrics.csv`、`baseline_metrics.csv`（38/120 行 |ΔSharpe|>0.05：多头段 −0.17~−0.19、空头段 +0.14~+0.21）、`momentum_head2head{,_diag}.csv`、`scan_hybrid20_thresholds.csv`。

## §3 holdout 进选择（audit 第 3 条）

**核实**：`momentum_scan.pick_plateau_representatives` 的 worst_window 确含 holdout；`scan.py` / `scan_thresholds.py` 展示按 holdout 排序。均属实。

**但生产参数不由 holdout 调出**：equal_weight 20d40z+sm5 是 2026-06 的设计默认（commit `a2e19f0` 合并两变体早于扫描器 `3b05c55`），7 月扫描是确认性的。holdout 进选择只偏袒动量挑战者——它们仍全输，"不换现任"反向稳健。

**已做**：
- `pick_plateau_representatives` 选择键改 `min(train, val)`，holdout 仅报告（测试判例锚定：train/val 双高但 holdout 低者必须当选）。
- `scan.py` / `scan_thresholds.py` / `momentum_scan.py main` 展示排序全部改 `worst_tv`；二轮复审补漏 `breadth_dual.py --mode scan`（原按 `dual_sharpe_holdout_24_26` 出 Top 20）同改 `worst_tv`，`momentum_scan` 尾行旧文案 "holdout 1.69" 改为"24-26 列=第二验证窗仅报告"。全仓 grep 确认无残留 holdout 排序键。
- 用新规则 + 修正 carry 重跑 head2head：**六族代表全部换档**（多为 z_window/smoothing），裁决不变——**不换现任**（新代表最强者 slope L20 zw120 sm0：对称 1.428 vs 现任 1.414、long-flat 1.552 vs 1.616，平手；corr 0.818）。**生产第一替补变更登记：slope_L20s0_zw40_sm5 → slope_L20s0_zw120_sm0**。

**声明**：2024-26 窗口已被历次决策消耗，**今后一律称"第二验证窗"，不再称未触碰 OOS**；未触碰 OOS 只剩未来实盘。历史文档中 "holdout 1.69" 等表述按此降级理解。

## §4 选优后置换 p 值（audit 第 4 条）

**核实**：rotation / leverage / thermo / long_axes / erp 五探针确实先网格选优、再只对赢家算置换 p。属实。

**不改机器的理由**：五探针 verdicts 全 STOP（PASS 列全 False）——p 值在系统性**偏乐观**的情况下照样全灭，校正只会更 STOP。该缺陷只在产出 PASS 时咬人，而它一次都没咬到。

**重开条件**：任何探针未来要出 PASS 结论，必须先改造置换过程（每次置换重跑整套选优）或用独立确认窗。生产信号自身的 bootstrap（`significance.py`）为单一预设策略、选择强度低（六月两变体 + long-flat 采纳），不受此条实质影响。

## §5 金融行业标签穿越（audit 第 5 条）

**核实**：`build.py:338` 确以当前快照集合在 2013 起所有月末抹金融股 CF/P。属实。**但审阅低估了可修性**：`industry_classification` 实有 **69 期 CITIC 月度快照（2021-01 ~ 2026-07，360,616 行）**，2021 后可直接 PIT，之前用最早一期兜底。

**处置**：影响小（金融行业成员黏性极高），列开放项——下次重建 style basket 管线时改 `_fetch_financial_set` 为按评分日查快照。

## §6 仪表盘日期守卫（audit 第 6 条）——已修

- `dashboard/app.py status_bar`：`pos_date != date` 时渲染警示行（"⚠ 持仓截至 …，落后信号——重跑 python3 -m backtest.production"），TDD 锚定。
- `tests/test_dashboard_data.py` 新增合同守卫：committed 信号 CSV 与 recommended 持仓 CSV 末日不一致 → 测试套直接挂。

## 未重跑清单（留档为修正前口径，结论方向已论证稳健）

五探针 verdicts / `rotation_probe.csv` / `scan_momentum*.csv` / `scan_equal_weight.csv` / `scan_citic40d.csv` / `scan_breadth.csv` / `pure_style_eval.csv` / `breadth_divergence_metrics.csv`。理由：§4（p 偏乐观仍全 STOP）+ carry 修正对"相对比较/负结论"无方向性影响；重跑只会强化 STOP。

## 历史文档勘误指针

以下**日期存档类**文档引用的 1.42 / 1.39 / −13.9% / holdout-as-OOS 均为修正前口径，以本文 §2/§3 为准，原文不改写：
`2026-07-10-optimization-roadmap-retrospective.md`、`2026-07-10-momentum-transform-scan-design.md`、`2026-07-06-phase3-dual-engine-v1-plan.md`、`2026-07-07-phase4-breadth-divergence-plan.md`、`2026-07-08-b2-industry-neutral-decomposition.md`、`2026-07-09-long-axes-probe-design.md`。

**现状类**文档直接就地更新（非存档）：仓库根 `README.md`（headline 表）、`backtest/README.md`（基线表 + carry 口径 + 顶部勘误链接，二轮复审补）。
