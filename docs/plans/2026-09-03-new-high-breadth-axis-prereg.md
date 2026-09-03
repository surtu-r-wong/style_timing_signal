# 创新高参与度面·入场券探针预登记（2026-09-03，**用户已冻结，跑前未改**）

## 0. 立项与线索

国信《由创新高个股看市场投资热点》（第 258 期，2026-08-28）按指数报「过去 20 个交易日创 250 日新高的成分股占比」。
仓里广度轴只测过全市场 20/60 日新高减新低（Phase 4 空头背离 STOP、07-09 多头连续 STOP），**250 日窗口与按规模指数拆分的新高参与度从未测过**。
用户 09-03 裁「做」。先验：两次广度 STOP + 同构的规模差（预期修正 R4）形态分裂，预期大概率 STOP，价值在正式关帐。

## 1. 命题（双侧）

创新高参与度（全市场水平；小盘相对大盘的参与度差）是否对 equal_weight 生产信号（long-flat，blend）提供显著、独立、两半窗稳健、成本后存活、且经选优校正仍显著的择时信息。

## 2. 构造（冻结）

- 个股：`stock_daily_price.close_hfq`（后复权，历史值不随未来事件改写）；`NH_t = close_t ≥ max(close_{t−249..t})`（含当日，250 日窗须满）；
  `NH20_t = 过去 20 个交易日内任一日 NH=1`（与研报口径一致）。样本 = 上市满 15 个月（首个 close 日起 ≥ 315 个自然日）。
- **N1 市场参与度** = 全市场 NH20 占比（分母 = 当日满足 250 日窗与上市条件的股票数；分母 < 500 记 NaN）。
- **N2 规模参与度差** = mean(share_500, share_1000) − share_300，其中 share_X = X 指数 PIT 成分内的 NH20 占比（成分 = `index_constituent` 最新 `effective_date ≤ t` 快照）。
- 当日收盘即知，T+1 生效，无滞后。序列缓存 `backtest/output/new_high_breadth_series.csv`（含 share_300/500/1000 供描述）。

## 3. 网格、窗口、秤与闸门（冻结）

- 两族 × `GRID_LEVEL`（lb∈{5,20} × zw∈{60,250}）× k∈{5,10,20,40} = **32 变体**。
- 两半窗默认 2014-2019 / 2020-2026；秤 blend 标的 + carry、3 bps、T+1；置换 1,000。
- 关 1～3 = `run_families_probe` 原样；**关 0** = 32 变体全窗 |IC| argmax 的循环移位空分布，族代表 |IC| ≥ 95% 分位。四关全过才 PASS。

## 4. 功效边界与引用纪律

约 3,000 日，k=20 约 150 窗、k=40 约 75 窗，可检出 |IC| ≳ 0.15。
关的是「250 日新高 20 日参与度（市场级 / 小盘减大盘）× 网格对 equal_weight 的入场券」；不得引作「新高信息对个股或行业无价值」（研报的选股用法未测）。
机器 `backtest/new_high_breadth.py`、`backtest/new_high_axis_probe.py`、`tests/test_new_high_breadth.py`；输出 `new_high_axis_probe{,_verdicts}.csv`、`new_high_axis_selection.json`。
