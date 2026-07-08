# 杠杆轴空头信号探针（leverage probe）设计（2026-07-08，brainstorm 定稿）

> 前置：空头换轴数据解锁（`2026-07-08-unlock-short-axis-data-runbook.md`）——
> `stock_selector.edb_daily` 两融 8 条 2010-04 起 3,949 交易日，沪/深恒等式自洽 ~1e-16。
> 背景：财务轴（Phase3 v1）/行业轴（T6）/价格广度轴（Phase4）三轴空头段已全部证伪，
> 空头真解须换轴（design §2 面 3-6）。本轮 = 杠杆面（面 4），换轴第一炮。
> 用户拍板因子族：L1 + L2 + L3'（占流通市值原版因无流通股本数据搁置）。

## 1. 命题

两融杠杆序列能否为空头引擎提供**独立、显著、成本后存活**的信息。
先验（design 面 4）：增速极值=干柴（空-环境）、冰点=底部（多）——即预期 IC 为负；
但不强加先验，双侧检验，方向由数据裁决：若正向主导（杠杆追涨确认），信息归多头
引擎增强，不进空头装配。任一关不过 → 停线归档（负结果第四轴，与前三轴同柜）。

## 2. 因子族（三族逐族裁决）

设 `bal = 融资余额_沪+深`（M0061606+M0061610）、`buy = 融资买入额_沪+深`
（M0061604+M0061609），单位万元；`amt = 全市场成交额`（元，见 §3.2）。

| 族 | 定义 | 语义 | 形态网格 |
|---|---|---|---|
| **L1 余额增速** | `bal.pct_change()` 喂 `series_signal`（lb 日增速→z→tanh） | 杠杆资金流入动能；极值=干柴/冰点 | lb∈{5,10,20,60}×sm∈{0,3}，zw=2lb → 8 形态 |
| **L2 占成交比** | `buy×1e4 / amt` → `level_signal` | 杠杆资金参与度；高占比=散户杠杆主导=脆弱 | lb∈{5,20}×zw∈{60,250} → 4 形态 |
| **L3' 去杠杆天数** | `bal×1e4 / amt.rolling(20).mean()` → `level_signal` | 存量脆弱性：清杠杆需几天成交额消化 | lb∈{5,20}×zw∈{60,250} → 4 形态 |

- L1 是收益型序列 → **零新管线**复用 `rotation_probe.series_signal`
  （nav=cumprod 重构余额路径，rolling lb prod−1 = lb 日增速，与生产管线零漂移）；
- L2/L3' 是水平型序列（信息在水平极值不在变化）→ 新纯函数
  `level_signal(level, lb, zw)` = rolling_mean(lb) → z(zw, min_periods=zw, STD_FLOOR
  守卫同生产) → tanh；水平量 z 窗取 60/250（季度/年度常模），不套 2×lb；
- **融券形态排除**：2024 政策性停融券后余额仅占融资 0.07%，序列 regime 断裂。

## 3. 数据与两个工程要点

### 3.1 PIT：整体 shift(1)

交易所 T 日两融数据 T+1 早间公布；引擎/probe 约定 sig[t] 从 t 收盘起持有 →
所有杠杆因子在交易日网格上 **shift(1)** 后才进 IC/回测（宁多滞后一天不留前视）。

### 3.2 分母单位断层（本轮最大工程坑，已探明）

`stock_daily_price.amount` 多源混单位：老 tushare 段=（千元/手）、新段=（元/股），
**同一 data_source 标签跨期也会翻转、同一天不同股票可混**（2025-06-30 实测
5,379 行千元 + 5 行元）。直接 SUM 错 1000 倍。

- 逐行判别：`amount/(volume×close) ≈ 0.1 →（千元/手）段 ×1000；≈1 → 元`
  （VWAP≈close 容差下阈值 0.3）；volume/close 空或 0 的行剔除（停牌无成交）；
- 单扫 SQL `GROUP BY trade_date` + CASE 归一 → 缓存
  `backtest/output/market_turnover.csv`（与 breadth.csv 同模式，一次构建）；
- **锚点验证内建**（偏差 >15% 拒绝缓存）：2015-06-18 ≈ 1.46 万亿、
  2026-06-30 ≈ 3.25 万亿（均已实测吻合）；另 sanity：融资买入占比历史带 5-15%。

## 4. 三关闸门（rotation probe 模板 + 双侧化）

与 rotation probe 的唯一结构差异：先验方向为负 → 全链路**双侧/同号化**：

- **代表选择**（每族独立，blend 口径）：候选 = 全窗与两半窗 IC **三者同号**的形态×k；
  取 worst-half |IC| 最大者；无同号候选 = 该族直接判负（记录最大 |IC| 行存档）；
- **关1 显著且独立**：置换 p（模板本就双侧）<0.05，且偏 rank IC（控 equal_weight
  生产信号，`partial_ic_with_pvalue`）与主 IC **同号**且 p<0.05——防重复建设
  （rotation 正是死在这一关）；
- **关2 分窗稳健**：2014-19 / 2020-26 同号（选择规则已内建，独立复核）；
- **关3 成本后为正**：`hold_position(sig × sign(ic_full), k)` ±1 全仓 k 日持有，
  3bp 单边 + carry，净 Sharpe > 0；
- 持有网格 k∈{5,10,20,40}（杠杆是环境级慢变量，上限放宽到 40；下限 5 天——
  两融日数据本就带 1 天公布滞后，k=3 意义弱）。

多重比较照模板处置：族内选代表后过闸（selection 温和偏乐观），probe 只是廉价
第一滤网，真正入池裁决在装配阶段的 hedge_value 五判据。

## 5. 实现

`backtest/leverage_probe.py`（与 rotation_probe 并列；loader 走 rotation 同模式
局部函数，不动共享 data.py）。复用 import：`series_signal / nonoverlap_ic /
shift_permutation_pvalue / partial_ic_with_pvalue / hold_position`。

新纯函数（TDD）：
1. `level_signal(level, lb, zw)` — 水平序列信号化（rolling mean→z→tanh）；
2. `unit_scale(amount, volume, close)` — 逐行单位判别（×1000 或 ×1，SQL CASE 镜像）；
3. `pit_lag(sig)` — shift(1) 封装（语义显式化）；
4. `pick_representative(panel)` — 同号约束 + worst-half |IC| 选代表；
5. `build_market_turnover(db, force)` — 单扫聚合 + 锚点断言 + CSV 缓存。

CLI：`python3 -m backtest.leverage_probe [--families L1,L2,L3p] [--n-perm 1000]`
→ IC 面板（16 形态×4k×3 口径）`backtest/output/leverage_probe.csv` +
逐族三关裁决打印。

## 6. 判定后路径

- **STOP（全族负）**：归档「杠杆轴第四轴证伪」——四轴（财务/行业/广度/杠杆）
  同判则「A 股短信号族无空头价值」结论升级为跨情绪面级；转涨停温度计轴（面 3，
  零数据依赖）或收官复盘；
- **PASS（任一族过）**：进装配阶段——`dual_legs_external_short` 现成编排器，
  杠杆信号离散化为空头环境门（percentile>P → −1，hold 持有，carry 门控），
  `hedge_value` 五判据（down-month-hit / maxdd_improve / 独立盈亏 / 三口径 / 分窗）
  才是入池终审；若 IC 为正主导 → 改走多头引擎增强线（另开 brainstorm）。

## 7. 升级路径（本轮不做）

- L3 原版（余额占流通市值）：需交易所口径沪/深**流通市值** EDB 各一条
  （与两融同回填路径，加两条码即可）；全 A 逐股流通股本回填过重，不推荐；
- ETF 申赎轴（面 6）：`unit_fundshare_total/unit_floortrading` 字段已核对，
  待用户加 gateway config 后解锁。
