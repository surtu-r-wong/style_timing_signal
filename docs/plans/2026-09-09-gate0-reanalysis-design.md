# 关 0 批量重分析（2026-09-09，**跑前冻结的处置规则**）

用户 09-09 裁决：关 0 机器有缺陷就修，修完对全部已关帐线一律重跑；"不翻案"不再是原则。
本文在看到任何重跑读数**之前**写下范围、三个版本、翻转的处置。

## 1. 修的是什么（代码事实，与任何候选读数无关）

1. **零分布的选优规则与实际不符**：五条"族探针 + 关 0"线（基差 / 分析师预期 / 资金流 / 创新高 / 期权隐波）
   的零分布按「全窗 |IC| argmax」选，实际代表按 `pick_representative`（三窗同号中最差半窗 |IC| 最大）选。
   机器的 `select_fn` 入口从未被任何探针调用（`grep select_fn backtest/*probe*.py` 零命中）。
2. **口径**：09-03 论证已定 min-P 为默认；本次对所有线一律同时报 max-T 与 min-P。
3. **检验的量**：关 0 只校正了原始 |IC|，"对现役的增量"（偏 IC）从未被校正过。本次加偏 IC 版。

## 2. 范围

| 线 | 网格 | 代表来源 | 原关 0 p |
|---|---|---|---|
| basis_term（基差期限结构） | 3 族 × 4 形态 × 4 k = 48 | `basis_term_probe_verdicts.csv` | 同上 `p_vs_max_null` |
| consensus（分析师预期修正） | 4 × 4 × 4 = 64 | 同名 verdicts | 同上 |
| money_flow（资金流 F1/F2） | 2 × 4 × 4 = 32 | 同名 verdicts | 同上 |
| new_high（创新高参与度） | 2 × 4 × 4 = 32 | 同名 verdicts | 同上 |
| option（隐波五族） | 5 × 4 × 4 = 80 | 同名 verdicts（原关 0 只算了 O2） | `option_axis_selection.json` |
| incumbent_ew（现役，校准用） | 4 × 4 = 16 | 现役点 lb20zw40 / k=20 | `gate0_incumbent_audit.json` 0.2927 |

不在范围：reversal / dual_channel / fifth_bucket / family_unification / threshold 等——它们的选优规则本身就是 argmax（或另有机器），
不存在第 1 条错配；口径与偏 IC 版对它们的适用性另议。

## 3. 三个版本（同一份置换索引矩阵，seed 0，rotation [2k_max, n−2k_max]，B=1000）

| 版本 | 统计量 | 选优 | 报 |
|---|---|---|---|
| V0 复算 | 全窗 \|IC\| | argmax | max-T p（须与原登记 p 一致，作机器复现校验）+ **min-P p** |
| V1 真实规则 | 三窗同号则 min(\|IC_h1\|, \|IC_h2\|)，否则 −inf | argmax（= 实际代表规则） | max-T p / min-P p |
| V3 增量 | 全窗 \|偏 IC（控现役 EW）\| | argmax | max-T p / min-P p |

全部用 `selection_permutation.adjusted_pvalue(res, 代表下标, criterion)`（单步 FWER 校正 p，对任意指定变体有效）。

## 4. 翻转的处置（跑前定死）

- **主校正口径 = V1 min-P**（真实规则 + 已论证口径）。V1 min-P < 0.05 记「关 0 翻转」。
- **翻转 ∧ V3 min-P < 0.05 → 升为「复制候选」**，进横截面复制队列（同今日基差/F1 流程），**不直接改 PASS**。
- 翻转但 V3 ≥ 0.05 → 记「有形态无增量」，不进队列。
- 旧裁决**保留不覆盖**；登记表每条加一行 caveat 写三版读数与机器版本（本提交 hash）。
- 已做过横截面复制的两条（基差 T1、资金流 F1）：无论本次翻不翻，复制结果（都 STOP）优先，因为复制是更强的证据。
- 现役 EW：只作校准，不裁决。

## 5. 产物

`backtest/gate0_reanalysis.py`；`backtest/output/gate0_reanalysis/<line>.json` + 汇总 `gate0_reanalysis_summary.csv`；结果文档 `2026-09-09-gate0-reanalysis-results.md`。
