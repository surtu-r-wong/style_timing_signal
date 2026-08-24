# 首披日 PIT 升级：腿工厂可知日换真实首披日（设计与重验协议，跑前冻结）

日期：2026-08-24。用户裁决 = 方向 A（首披日集成链）。状态：**§1–3 冻结于
实现与重验运行之前**。

## 0. 依据

- 数据资产 = R5 阶段一/二（`data_fixes/2026-08-21-real-first-disclosure-backfill/`，
  `857a70c`；表 `stock_first_disclosure` 286,784 行，quality ok = 275,258 = 96.0%，
  end_date 2003-03-31 → 2025-03-31，迁移 051 @ stock_selector `a2ff225`）。
- 登记遗留（08-21 阶段二）：「reader 包装器接入 + 5.82% 两源不一致取用规则」。
  本文档裁定本仓消费路径的两源规则（§1.3）；stock_selector reader 侧为 Phase 2（§4）。
- 程序轨道 = `docs/plans/README.md` Gate 0R 行 reopen condition：
  「规格或 DP 数据底座再次变更时，先冻结新锚/阈值并生成新的 complete immutable run」。

## 1. 变更规格（冻结）

**唯一改动点 = `pure_style_builder._fetch_series` 的修正可知日一步。**

1. **新规则**：行的可知日 =
   - `stock_first_disclosure.first_disclosure_date`（join 命中且非空），当
     `statement_type ∈ {income, balance, cashflow_direct, disclosed_indicators,
     profitability}`（定期报告内容，披露粒度 = 整份报告）；
   - 否则回退现行规则 `min(ann_date, 法定披露截止日)`（sentinel 11,526 行、
     2003 前、01-01 伪行、Wind 段 2025Q2+ 自动落入回退——Wind 段 ann_date 本就是
     真实 `stm_issuingdate`，回退即正确）。
2. **dividend 路径豁免**：红利事件行的可知日是**分红公告时点**，不是年报首披日；
   套用报告首披日会把分红可知日错误前移/后移。DP 机器（2026-08-20 冻结）不动。
3. **两源不一致取用规则（裁定 5.82% 开放项，本仓消费路径）**：
   **首披日一律优先**，即便 CSMAR 批次日更早（16,017 对，疑业绩快报/修订日）。
   理由：批次日早于正式报告首披日的行**无法验证**其早期日期对应的数值就是库内
   数值（快报只披露部分科目且可能后修）；PIT 正确性要求宁晚勿早。放弃快报新鲜度
   是有意的保守选择。**不静默取 min**（08-21 台账原话要求）。
4. **范围**：本阶段只改 `pure_style_builder`（腿工厂 + Gate 0 + 尾部/等比桶共用）。
   生产 ew 信号（官方指数对）不经此路径，不受影响。

## 2. 独立于目标统计量的修复证据（修复台账纪律）

1. **定义层**：真实首披日是可知时刻的直接测量；现行规则是其上界代理
   （按时披露者被系统性推迟到截止日，超期披露者构成前视——后者是已登记的
   残余限制）。
2. **测量层**（2026-08-24 实测，n=275,258 配对，全部独立于 ρ/Sharpe）：
   新旧可知日之差 Δ = 首披日 − min(批次日, 截止日)：中位 **−2 天**、p5 −17、
   p95 **+109**；**61.0% 行更早**（新鲜度收益，量级温和）；**8.8% 行更晚**
   （= 旧规则下的真实前视行被修掉，尾部量级达数月）；69.5% 首披早于批次日
   （批次日陷阱再证）。
3. **覆盖层**：96.0% ok；sentinel/出界行显式回退，无静默路径。

## 3. 重验协议（冻结于运行前）

1. **基线刷新**：`backtest/output/` 三个 flat 文件（gate0r_result /
   fifth_bucket_verdict / geo5_verdict）当前是 08-19/20 旧值，先用权威 run
   `20260821T210841-p0-revalidation-3030109/outputs/` 覆盖 → 编排器的
   old-vs-new 比较以当前权威值为基线。
2. **重验 = `backtest.p0_revalidation` 原编排器**（gate0r + tail + fifth +
   geo_pairs + geo_formal，complete immutable run，seed=0，约 8.5h 本机过夜）。
3. **判读预先接受**（DP 修复 §7 同款协议）：
   - 现行锚/地板（repro_hk 0.7951 / sim2000_guarded 地板 0.78 / band500 地板
     0.9598）仅作**参照**；本次改动 61% 行提前、8.8% 行推迟，ρ **允许双向移动**；
     若跌破旧地板，判读措辞 = **「护栏对有意修复的预期反应」**，不回滚修复、
     不调参凑 ρ，交用户重登锚。
   - 尾部/等比两 STOP 预期 maintained（整套方法论替换才动 ±0.05 的先例）；
     若 flipped，同样先报告后裁决。
   - **首跑值永久登记**，不迭代不重跑。
4. **锚重登**：运行收官后各闸新值报用户裁决重登（08-21 判例：裁 A 整体重登）。

## 4. Phase 2（登记，不在本批执行）

stock_selector 侧 reader 包装器接入（frozen legacy roots → 只许包装不许就地改，
Tier 1 双审，不委派 GLM）+ D5 消费方公告 + Pi5 同步提升决策。届时两源规则沿用
§1.3 裁定或按 stock_selector 消费语义另裁。

## 5. 残余限制更新（生效于重验收官后）

「超期披露前视」限定语从**全体 CSMAR 行**收窄为：sentinel 4.0% + 2003 前 +
01-01 伪行的回退路径；96% 覆盖行的前视已被真实首披日消除。provisional 措辞
相应收窄，不删除。
