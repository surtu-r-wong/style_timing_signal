# 部署决策：现役 equal_weight 由 long-flat 切到对称（2026-09-09，用户裁决）

**裁决**：用户 2026-09-09 看过现役空头腿的切面后裁「部署空头」。本文记录依据、改动、风险与回滚。
这是**风险偏好裁决**，不是统计裁决：①a 配对 block bootstrap（`2026-08-12-probe-1a-symmetric-vs-longflat.md`）
两口径 Sharpe 差不显著（1.61 vs 1.41，p 0.87），登记表 `production-equal-weight-long-flat` 明写"采用理由是回撤、换手和风险形态"。
切换不改信号、不改参数，只改下游持仓映射；hybrid20 / citic40d 维持 long-flat（空头腿 Sharpe 0.49 / 0.05，见 `baseline_metrics.csv`）。

## 1. 依据（同秤日频引擎：每日按符号持仓、T+1、3 bps、含期指贴水 carry；评窗 2015-04-16..2026-09-03）

| 腿 | Sharpe | 年化 | 最大回撤 |
|---|---|---|---|
| 空头腿，含贴水 | 0.67 | 13.2% | −25% |
| 空头腿，去贴水 | 0.95 | 19.0% | −23% |
| 多头腿（= long-flat） | 1.54 | 25.8% | −17% |
| **对称** | **1.51** | **39.0%** | **−27%** |

- 空头腿 12 年 11 年为正（仅 2021 −8.5%）；做空日标的年化收益 12 年里 10 年为负；62 个做空段中 65% 标的下跌，中位段长 22 日。
- 崩盘年收益：2015 +28%、2018 +48%、2022 +32%——危机凸性。
- **贴水不可绕开**：按贴水阈值过滤反而变差（阈值 3% 以下 Sharpe 为负）；贴水最深处正是崩盘进行时，空头 alpha 住在高贴水区间里。
- 贴线候选的纯空头腿全部不可用（F1 0.16、T1 −0.01、R4 −0.08、O4 −0.16、O2 −0.76），现役空头腿是仓里唯一可用的空头信号。

## 2. 改动（提交见 git log 本日）

| 位置 | 改动 |
|---|---|
| `backtest/positions.py` | 新增 `symmetric_position`（>0→+1，<0→−1，0→0，阈值死区） |
| `backtest/production.py` | `PRODUCTION_MAPPING = {hybrid20: longflat, citic40d: longflat, equal_weight: symmetric}`；`RECOMMENDED_FILES` 供下游取文件名；equal_weight 的 long-flat 作参照并行产出 |
| `output/recommended/equal_weight_symmetric.csv` | **新现役推荐持仓**（position ∈ {−1, 0, 1}）；`equal_weight_longflat.csv` 继续产出，非现役 |
| `deploy/daily_signals/check_freshness.py` | 硬护栏改盯 symmetric 文件；long-flat 参照降为只报不拦 |
| `dashboard/` | 状态 chip 三态（持多 / 持空 / 空仓），跌色 `figures.DOWN` |
| `tools/export_combined_signals.py` | 从 `RECOMMENDED_FILES` 取文件 |
| 测试 | `test_bt_positions`（对称字面量）、`test_bt_production`（equal_weight 对称 + 参照一致性）、`test_deploy_freshness_guard` 对齐 |

日更链路 `run_daily_signals.sh` 步骤 6 仍是 `python -m backtest.production`，无需改脚本；下次定时器运行即写新文件。

## 3. 风险与承接

- 回撤由 −17% 加深到 −27%，换手翻倍（10.9 → 21.8），空头段支付贴水（2021 起均值约 7%/年）。
- 空头通过 IC/IM 期货承载（blend 50/50），carry 已在引擎内按持仓方向计入；执行价审计（`2026-08-13-exec-price-audit.md`）只审过多头段，空头段执行价未审。
- 2021 年是唯一亏损年（空头腿 −8.5%）；对称在 2021-2023 段反而优于 long-flat，是唯一对称占优窗口，样本短。
- 09-03 起 index_daily 停更（Wind 额度），首个含空头的实际推荐日 = 额度恢复补跑后的第一个 signal<0 日。

## 4. 回滚

把 `backtest/production.py` 的 `PRODUCTION_MAPPING["equal_weight"]` 改回 `"longflat"` 并跑 `python -m backtest.production`；
护栏、仪表盘、导出都从 `RECOMMENDED_FILES` 取名，随之自动回到 long-flat 文件。信号本身自始未动。
