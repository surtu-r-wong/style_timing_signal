# 部署决策：slope20 升为第四条生产线（2026-09-09，用户裁决"这个信号正是我想要的，也加到现役里"）

**裁决**：slope20（`slope_L20s0_zw120_sm0`，07-11 登记的生产第一替补）作为**第四条生产线**与 equal_weight / hybrid20 / citic40d 并行上线。
**不融合**（08-12 融合 STOP：corr 0.82 同轴，任何权重的融合都劣于两者中较优者）、**不替换**现役 equal_weight。
映射与现役一致用**对称**（空头腿同形态，见 §2）。

## 1. 定义（与 `backtest.fusion_probe.build_factors` 的 slope20 逐位一致，corr 1.0、max|diff| 5e-5）

每腿 20 日对数价格 OLS 斜率 → 成长减价值（`config_4pairs.csv` 四对）→ 120 日 z（min_periods 120，STD_FLOOR）→ tanh(z/2) → 四对等权 → 不平滑。
生成器 `signals/slope20/generate_signal.py` = `momentum_scan.momentum_factor_fn()(family="slope", length=20, skip=0, z_window=120, smoothing=0)`。
与现役的差别只有两处：斜率代替端点收益（路径加权，抗单日跳空）；120 日 z 窗代替 40 日（回中更慢，换手更低）。

## 2. 依据（同秤日频引擎，2015-04-16..2026-09-03，含贴水，3 bps）

| 腿 / 映射 | Sharpe | 年化 | 回撤 | 换手 |
|---|---|---|---|---|
| slope20 多头腿 | 1.42 | 24.6% | −20% | 10.8 |
| slope20 空头腿，含贴水 | 0.62 | 11.9% | −29% | 10.8 |
| slope20 空头腿，去贴水 | 0.90 | 17.4% | −22% | |
| **slope20 对称（部署）** | **1.41** | **36.5%** | **−32%** | 21.6 |
| 参照：现役 equal_weight 对称 | 1.51 | 39.0% | −27% | 21.8 |

空头腿逐年 12 年 8 正，崩盘年 2015 +59% / 2018 +35% / 2022 +39%，与现役空头腿同形态。
分窗（09-09 重算）：验证窗 2021-23 对称 1.84 vs 现役 1.26，holdout 2024-26 1.08 vs 1.51，全窗平手——它是现役同一根轴的慢档，各赢一段行情。

## 3. 改动

| 位置 | 改动 |
|---|---|
| `signals/slope20/generate_signal.py`（新） | 全量重算覆写 `output/slope20/slope20_signal_L20zw120.csv` |
| `backtest/baseline.py` SIGNALS | 加 `slope20` |
| `backtest/production.py` | `PRODUCTION_MAPPING["slope20"]="symmetric"` → `output/recommended/slope20_symmetric.csv` |
| `deploy/daily_signals/run_daily_signals.sh` | 步骤 `slope20_L20zw120` 加在推荐持仓之前 |
| `deploy/daily_signals/check_freshness.py` | 硬护栏加信号文件与推荐文件 |
| `dashboard/` | 状态条第四个 chip |
| `tools/export_combined_signals.py` | ORDER 加 slope20 |
| 测试 | `test_bt_production` 加 slope20 对称与文件名判例；freshness 测试自动覆盖 |

## 4. 风险与承接

- 两条线会出现**分歧日**（约 20% 的交易日）。上线当日（09-03 数据）即是：equal_weight +0.389 持多，slope20 −0.222 持空。分歧日历史上按 slope20 方向年化 +6%、按 equal_weight −4%，但几乎全由 2015 贡献，其余年份互有胜负——**不要据此在分歧日偏向任何一方**。两条线是并行独立仓位，不是投票。
- slope20 在 2024-26 落后现役（对称 1.08 vs 1.51），上线即处于它的弱势段。
- 空头段执行价未审计（与 equal_weight 同）。
- 回滚：从 `PRODUCTION_MAPPING` / `SIGNALS` / 护栏 / 链路步骤移除 `slope20` 四处即可；信号文件保留。
