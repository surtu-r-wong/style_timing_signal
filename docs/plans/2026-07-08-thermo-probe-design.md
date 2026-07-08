# 涨停温度计轴探针（thermo probe）设计（2026-07-08，杠杆轴 STOP 后用户选定）

> 前置：杠杆轴探针（`2026-07-08-leverage-probe-design.md`）STOP 归档——四轴
> （财务/行业/广度/杠杆）同判空头无价值。本轮 = 面 3 温度轴（A 股特色短线情绪
> 直接读数），空头换轴第二炮，**零数据等待**：涨停判定用 `stock_daily_price`
> **未复权**价规则重构（限价规则作用于原始价），成交额分母复用
> `market_turnover.csv`。探针机器全部复用 `leverage_probe`（level_signal /
> pick_representative / validate_anchors + rotation 五纯函数）。

## 1. 命题

涨停温度计族（涨停占比/炸板率/涨停溢价/成交额水位）能否为空头引擎提供
**独立、显著、成本后存活**的信息。design 面 3 先验：炸板率飙升+涨停溢价转负
=可靠火花（空-触发）、冰点反转辅助多——即快频、双向语义；双侧检验，方向由
数据裁决。任一关不过 → 停线归档（第五轴）。

## 2. 涨停判定规则重构（侦察已验证）

`limit = ROUND(pre_close × (1+pct), 2)`（交易所四舍五入到分）：

- **pct**：创业板（`30*`）2020-08-24 起 20%、之前 10%；科创（`68*`）20%；
  主板（`60/00`）10%；**北交所剔除**（30% 限幅、微盘、不在沪深情绪主战场）；
- **封板** `|close − limit| < 0.005`；**炸板** `|high − limit| < 0.005 且
  close < limit − 0.005`；行过滤 `volume>0 AND pre_close>0`；
- **ST 股（5% 限幅）无法历史识别 → 自然排除**：其 high 到不了 10% 限价，
  零假阳性、涨停家数轻微低估（可接受，非 ST 情绪本就是主目标）；
- **上市首日等无涨跌幅日自然排除**：high 可越过理论限价 → 等值判定不命中；
- 涨跌停制度 1996-12-16 起；探针窗口 2013+（与宽基收益交集）天然规避早期。

**锚点验证（2026-07-08 实测，写死进缓存构建断言）**：2024-01-08 全市场
封板 33/炸板 16（主板 31/15 = runbook 前期独立侦察逐数复现）；2024-09-30
封板 825（史诗暴涨日）；2015-05-28 炸板 214≫封板 95（大跌日形态）。

## 3. 因子族（面 3 一次扫齐；连板高度/题材集中度 P2 缓做）

| 族 | 定义 | 语义 |
|---|---|---|
| **F1 涨停占比** | `n_sealed / n_active` | 打板赚钱效应广度 |
| **F2 炸板率** | `n_burst / (n_sealed + n_burst)` | 追高资金被套=火花先验 |
| **F3 涨停溢价** | 昨日封板股**今日**平均收益（日期戳=今日，PIT 干净） | 打板隔日兑现度；转负=火花先验 |
| **F4 成交额水位** | `log(market_turnover)` | 情绪能量水位（P1 成交额分位，分母现成） |

全部水平型 → `level_signal`，网格 lb∈{5,20}×zw∈{60,250}（各 4 形态）。
**无 pit_lag**：涨停/成交额当日收盘即知（与全仓库信号线同"T 收盘执行"约定；
两融因 T+1 公布才需滞后——本质区别记录在案）。持有 k∈{3,5,10,20}
（温度=快频触发面，用 rotation 短网格，非杠杆的 {5,10,20,40}）。

## 4. 数据管线

单扫 SQL（分类逐行 CASE + `LAG(sealed) OVER (PARTITION BY ts_code)` 取昨日
封板标记）→ 日度聚合 `backtest/output/thermometer.csv`
（n_sealed/n_burst/n_active/lu_ratio/burst_rate/lu_premium）+ 锚点断言。
已知噪声（记录不修）：停牌股 LAG 跨停牌段（封板股次日停牌罕见）；
lu_premium 在零封板次日为 NaN（dropna 后信号化）。

## 5. 三关闸门

与杠杆探针完全同构（import 复用，双侧化）：族内同号代表（`pick_representative`）
→ 关1 置换 p<0.05 + 偏 IC（控 equal_weight）同号显著 → 关2 两半窗同号 →
关3 `hold_position(sig×direction, k)` 3bp+carry 净 Sharpe>0。
blend 口径裁决，500/1000 面板留档。

## 6. 实现

`backtest/thermo_probe.py`。新纯函数（TDD）：
1. `board_limit_pct(ts_code, trade_date)` — 板别限幅（含创业板 2020-08-24 切换）；
2. `classify_limit(pre_close, high, close, pct)` — 封板/炸板逐行判定
   （rounding + 容差 + 无限幅日自然排除，SQL CASE 的镜像语义）；
3. `thermometer_measures(rows)` — 小样本日度聚合（含 lu_premium 日期戳
   =实现日的 PIT 关键测试）；
4. `build_thermo_signals(thermo, amt)` — 四族×网格装配（组合恒等测试）。

CLI：`python3 -m backtest.thermo_probe [--families F1,F2,F3,F4] [--n-perm 1000]`
→ `backtest/output/thermo_probe{,_verdicts}.csv` + 逐族裁决。

## 7. 判定后路径

- **STOP（全族负）**：第五轴归档——温度面（快频触发的原型面）亦无独立空头
  信息，则空头引擎命题在现有八观察面框架内基本走完（剩衍生品 PCR/期指持仓
  =P2 重数据），转仪表盘产品化/复盘收官；
- **PASS**：进 `dual_legs_external_short` 装配 + `hedge_value` 五判据终审；
  若正向主导 → 多头引擎增强线。
