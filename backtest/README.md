# backtest/ — Phase 2「修秤」评价框架

正确口径的回测/评价模块：把信号线当作对**中证500/1000/50-50**的市场择时信号，跑真实基线。修掉 `signals/hybrid20/optimize_signal.py` 的四个评价问题（设计稿 §3.1）。**只读 PG + `output/`，不碰信号生成。**

## 跑

```bash
python3 -m backtest.baseline --source pg --bootstrap 500
# → backtest/output/baseline_metrics.csv + console 表
# --mode proportional 用比例仓位对照；--cost-bps 改成本
```

## 口径（对应设计稿 §0 / §3.2）

- **全日历收益**：空仓日收益=0 计入分母（不再"剔 signal==0 再 ×245"）
- **成交**：T 收盘出信号 → T+1 收盘生效（`pos_eff = position.shift(1)`）
- **成本**：换手 3bp/边 + 空头段贴水 carry 成本（多头段 carry 收益）
- **carry**：`public.futures_daily` 主力合约（oi 最大）年化基差率 = (spot−futures)/spot×365/到期天数；实测 **IC≈+8.8% / IM≈+12.6%** 年化贴水
- **仓位**：离散 `{−1,0,+1}`（hybrid20 用自带三态；citic40d/equal_weight 连续因子取符号）
- **显著性**：同换手随机 bootstrap（循环平移 position 的零假设），p<0.05 才算显著

## 数据 & carry 窗口约束

- 标的：`stock_selector.index_daily` 的 `000905`(500)/`000852`(1000)，2013-03-06→2026-07-03；blend=两者日收益等权
- carry：IC(500) 2015-04+、IM(1000) 2022-07+（期货上市日锁死；spot-only 可到 2013）；`futures_daily` 止于 2026-04-29，之后 carry=0

## 三条线真实基线（2026-07-06，full window · 整段）

| 信号 | 口径 blend | 年化 | Sharpe | MaxDD | p |
|---|---|---|---|---|---|
| **equal_weight** | | 34.8% | **1.39** | −30% | 0.007 |
| **hybrid20** | | 27.3% | **1.32** | −22% | 0.010 |
| citic40d | | 19.2% | 0.76 | −43% | 0.017 |
| buy_hold（对照） | | 9.6% | 0.38 | −69% | — |

**结论：**
1. **三条线均统计显著**（p≤0.023）且大幅跑赢满仓（Sharpe ~1.3 vs 0.38、MaxDD 从 −69% 腰斩到 −22~30%）。修正口径（全日历+成本+carry）后信号**依然成立**——这是后续一切新信号的对照组。
2. **equal_weight / hybrid20 最强**（Sharpe 1.3–1.5）；**citic40d 最弱**（Sharpe 0.71–0.83、MaxDD −42%、换手 44 为两倍），是 step-2 参数重扫的重点。
3. **多头段 ≫ 空头段**（full window Sharpe）——**实证验证设计稿 §0 双引擎不对称**：

   | | equal_weight | hybrid20 | citic40d |
   |---|---|---|---|
   | 多头段(500) | 1.77 | 1.35 | 1.28 |
   | 空头段(500) | 0.20 | 0.34 | **−0.18** |

   carry 逆风 + 政策底截断使空头端弱得多、价值几乎全在多头端 → 支持"多空分离两套系统、空头门槛更高"。
4. 1000 口径普遍略优于 500（贴水更深、波动更大、可择时空间更多；equal_weight 1000 Sharpe 1.52 全场最高）。

## 模块

`metrics.py` 指标（全日历）· `positions.py` 信号→仓位 · `engine.py` 全日历引擎（T+1/成本/carry/多空分段）· `data.py` 标的收益+主力基差carry · `significance.py` bootstrap · `baseline.py` 编排+CLI。测试 `tests/test_bt_*.py`。

## 已知 & 后续

- **citic40d `build_basket`（generate_signal.py:56）用 `iloc[0]` 归一**：非理想 PIT（换起始日会改历史值），但除的是**最早日、无前视**，基线有效；z-score 对常数缩放近似不变、影响小 → PIT 清理并入 step-2 再评估（设计稿 §3.2 Step 4）。
- **下一步 = 方向二 step-2**：walk-forward 参数重扫（lookback×z_window×平滑，选 Sharpe 高原出热图），citic40d 重点；之后 Phase 3 双引擎 v1 组装。
