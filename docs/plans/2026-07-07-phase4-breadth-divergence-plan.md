# Phase 4 · 空头引擎专属信号 v1：广度背离（breadth divergence）

- **日期**: 2026-07-07
- **触发**: 用户在"三方向优化"末段岔路选 **B（攻空头）**。Phase 3 双引擎 v1 已证伪"复用风格信号短腿"——`short_engine` Sharpe −0.42（p=0.589，与随机不可分）、避险价值为负（down-month-hit 31.8%、maxdd_improve −6.7%）；T6 阈值扫描独立佐证（CITIC 轴 16 组 short_sharpe∈[−0.07,+0.04] 全≈0）。→ 空头引擎必须喂**专属脆弱性/触发信号**，不能靠共享风格短腿。
- **本轮范围**: 给空头引擎接**第一个**专属信号 = **广度背离**（空-触发/火花），进现成 `backtest/` 双引擎 harness 测**避险价值**。单变量、TDD、复用评价框架。
- **关键前提（2026-07-07 实测）**: Wind 已从 7-06 的 `-103` 会话死恢复（`/quota` 0/500M、`/fetch/price` 出数）；但**广度背离零回填**（数据全在库），不依赖 Wind。

---

## 0. 数据落库规格（P1/P1.5 全菜单 → DB/表）

> 用户要求"列出所有可选数据 + 落哪个库哪张表"。DB 一律 `market_monitor`（Debian `100.65.111.79`）。**派生信号不入库**，与现有三线一致产出 `output/` 因子（信号写库 = design §6 待议）。

### 表1 · 需 Wind 回填 → 落**已有**表
| 数据项 | 信号（引擎） | 来源 | 落库 `schema.table(.列)` | 状态 |
|---|---|---|---|---|
| 000905/000852 `volume`+`amount` | 成交额/换手分位（空-环境） | Wind `/fetch/price` | `stock_selector.index_daily(volume,amount)` | 今日实测可取；close 已在、补两列；PK`(index_code,trade_date)` 幂等 upsert。**本轮暂缓（见 §6）** |
| 932000.CSI 全序列 | 市值轴 | Wind `/fetch/price`（`.CSI` 后缀） | `stock_selector.index_daily`（新码） | 可回填（`.SH` 返 NULL 已验证） |
| 000300.SH 2010–2021 | 大小盘分歧 | Wind | `stock_selector.index_daily` | 补历史段 |

### 表2 · **已在库**直接算 → 信号落 `output/`（不入库）
| 数据项 | 信号（引擎） | 读哪张表 | 状态 |
|---|---|---|---|
| **广度：%>MA、新高−新低、ADL、离散度** | **空-触发（背离）/环境** | `stock_selector.stock_daily_price_qfq`（复权 close，到 06-30） | **就绪·零回填 ← 本轮用** |
| 全市场成交额/换手分位、题材集中度 | 空-环境（干柴） | `stock_daily_price.amount`（到 06-30） | 就绪·零回填 |
| 炸板率/涨停家数（10%/20% 近似） | 空-触发 | `stock_daily_price.pct_chg` 自算 | 就绪（精确版需 Wind 涨跌停标记） |
| 微盘拥挤、市值轴 | 空-环境 | `stock_daily_price`+`stock_indicator.total_mv` | 就绪（**与已证无效风格短腿重叠**） |
| 全A 估值 / ERP 权益腿（1/PE） | 多 | `stock_indicator.pe_ttm` 聚合 | 就绪 |

### 表3 · 需**新表/新列**（DDL）或**新 gateway endpoint**（Windows 侧）
| 数据项 | 信号 | 阻碍 | 落库 |
|---|---|---|---|
| 两融余额/融资买入 | 空-环境（干柴） | 无表 + gateway 无 EDB endpoint | 新表 `stock_selector.margin_daily`（走 SCHEMA_CHANGES.md DDL） |
| 10Y 国债 YTM（ERP 债腿） | 多（ERP） | `public.bond_daily` 空壳 0 行 + 无 EDB endpoint | 填 `bond_daily` 或新表 |
| ETF 份额申赎（日频） | 多（托底） | `fund_nav` 仅月频 `net_asset`（可 derive 份额）；日频份额无列 | 部分 |

### 表4 · 需接**外部管道**
| 数据项 | 信号 | 状态 |
|---|---|---|
| IC/IM 基差 carry / 基差急剧走深 | 空-触发（强） | `public.futures_daily` 止 **2026-04-29**；日更属 market_monitor 侧管道，陈旧 2 月 |

### 表5 · 已在库、未充分利用
`stock_selector.etf_option_daily`（15,332 行，PCR/IV skew，空-环境）· 商品期货日线（跨资产动量，环境）。

---

## 1. 广度背离信号设计

### 1.1 数据源（锁定）
- **广度分母**: `stock_daily_price_qfq.close`（复权，避拆分/分红跳变），每日约 5,200 只（含退市票 → survivorship 天然可控）。
- **标的价**: `index_daily`（000905.SH/000852.SH，`data.load_spot_close`，已在库到 07-03）。
- **PIT**: 广度按 `trade_date=T` 计算，harness 于 **T+1 收盘**成交 → T 日 EOD 数据当晚可得，PIT 安全（`available_at` 滞后仅威胁 T+1 开盘成交口径，本项目不用）。
- **最新完整日 = 2026-06-30**（07-01 仅 11 只，未灌全，裁掉）。

### 1.2 广度度量族（参数，不硬定 → 交给扫描）
1. `pct_above_ma_L` = 全市场 close_qfq > 自身 L 日均线的占比，L ∈ {20, 60}
2. `hi_lo_diff` = (N 日新高家数 − N 日新低家数) / 活跃家数，N ∈ {20, 60}
3. `adl_slope` = 腾落线（涨家数−跌家数累积）的 M 日斜率

### 1.3 背离规则（空-触发）
标的**创新高**但广度**不确认** → 空头触发（火花）：
- `underlying` 处于 **P 日滚动新高**（P ∈ {20, 60}）；**且**
- `breadth` 低于其 **Q 日前**水平（顶背离），或 breadth 处于滚动分位 < 下阈 —— Q ∈ {10, 20, 40}
- 触发 → `short_leg_bd = −1`，否则 0。（可选平滑/持有期，进扫描）

### 1.4 参数扫描维度（§3.2 方法论，选高原不选尖峰）
`breadth_measure × L/N × P(新高窗) × Q(背离回看) × 分位阈`，train/val/holdout 三窗，复用 `backtest/scan.py` 骨架。**不硬编码阈值**（延续 lb=20 甜点跨线交叉验证的做法）。

### 1.5 universe
v1 = **全市场**广度（最简、市场级情绪读数）；v2 备选 = 标的成分内广度（`index_constituent` 月度快照，更贴标的但需处理成分漂移）——留后续。

---

## 2. 引擎装配（关键：专属短信号，非门控）

v1 教训：门控一个**无价值**的短腿只能把它推到中性（T6 已证阈值永不转正）。所以广度背离是**独立空头信号发生器**，不是 `gates.py` 上的一道门。

```
long_leg  = 已证多头引擎（equal_weight 基因子多头段，long_theta=0.10，clip≥0）  ← 复用 v1 winner（Sharpe 1.42）
short_bd  = carry_protection(广度背离短触发, carry, carry_theta)              ← 新，专属空头信号
net       = synthesize(long_leg, short_bd)                                    ← 复用 dual.synthesize（多空同触发→0）
```

新增 `backtest/dual.py::dual_legs_external_short(long_factor, short_signal, carry, long_theta, carry_theta)`：多头来自基因子、空头来自外部信号（carry 门控），合成。评价复用 `hedge_value` / `bootstrap_pvalue` / KOU_JING × WINDOWS。

## 3. 评价与成功判据

对照组：`long_engine`（v1 winner，long-flat）vs `dual_bd`（long + 广度短）vs `short_bd_engine`（广度短独立）vs `buy_hold`。

**广度短算"有避险价值"当且仅当**（对齐 §1.3）：
1. `down_month_hit` > 50%（标的跌月里 dual 跑赢 long-only）；**且**
2. `maxdd_improve` > 0（加空头腿后回撤改善，非恶化）；**且**
3. `short_bd_engine` 或 dual 的避险贡献 bootstrap 显著（p<0.05）；**且**
4. `dual` 全窗 Sharpe 不过度拖累 long_engine（对冲让渡少量 Sharpe 换回撤可接受）。
5. **分窗**（2014-20/21-23/24-26）看 regime 依赖——v1 短腿是强 regime 依赖，广度是否更稳是关键诊断。

**若判据不过** → 记录为第二个"专属短信号也无避险价值"数据点（与 v1 共享短腿并列），再评估下一个候选（成交额分位/基差走深）。演进式：先证/证伪，不预设成功。

## 4. TDD 任务分解
- **T1** `backtest/breadth.py::build_breadth(db, start)` → 从 PG 算广度度量族、缓存 `output/breadth.csv`（一日一行，~3000 行）；纯 SQL 聚合一次性、下游复用。测试：小样本构造 → %>MA/新高新低数值正确 + PIT（不含未来）。
- **T2** `breadth_divergence(underlying, breadth, P, Q, ...)` → 背离短触发 {−1,0}。测试：构造"价新高+广度走弱"必触发、"价广度同步"不触发。
- **T3** `dual_legs_external_short` + report 编排。测试：多头段 == v1 long_engine（复用护栏）；短腿来自外部信号；synthesize 多空同触发→0。
- **T4** `scan_breadth`（§1.4 网格 × 三窗）→ `output/scan_breadth.csv`。
- **T5** 报告 `output/breadth_divergence_metrics.csv` + 结论（§3 五判据、分窗、显著性）。

## 5. 风险
- 广度全市场聚合 18M 行 → **必须缓存**（T1 一次性），扫描复用缓存序列，勿每组重算。
- qfq 前视重述（分红日历史值被追改）→ 对 %>MA 影响可忽略（跨 5000 股洗掉）；如敏感改 `close×adj_factor`（hfq，同到 06-30）。
- `stock_daily_price` 日更滞后：最新完整日 06-30，回测窗到此即可（信号窗本就到 2026-06）。

## 6. 数据决策记录（本轮）
- **000905/852 volume/amount 回填暂缓**：广度背离不需要；全市场成交额（`stock_daily_price.amount`）免费且更全 → 待真要"指数口径成交额分位"时再回填（Wind 恢复即可取，数据不会丢）。修正上一轮"趁窗口无悔回填"的口径：无悔性弱于全市场免费替代 + YAGNI。
- **两融 / ERP 债腿 / ETF 份额**：需新表/新 endpoint（表3）→ 排在广度背离之后，需时走 DDL + Windows 侧 Wind 字段配合。
- **基差走深**：等 market_monitor 期货日更接续（表4）。

---

## 8. 结果与结论（2026-07-07，本 session 落地）

**TDD 落地**：`backtest/breadth.py`（compute_breadth/build_breadth/breadth_divergence，含 hold 持有期）+ `backtest/breadth_dual.py`（dual_legs_external_short 编排 + scan_breadth + build_breadth_report + CLId）+ `backtest/dual.py::dual_legs_external_short`。新增 12 测试，全套 **93 过**。真实广度序列缓存 `output/breadth.csv`（3,419 日 2012-06→2026-06，4 度量）。

**关键中途发现（scan 暴露）**：广度背离作 **1 日触发**时 short_frac 仅 0.01–2.4%、maxdd_improve≈0 —— 事件型对冲信号**必须持有穿越下跌**。据此 TDD 加 `hold` 持有期维度（§1.3 pre-registered），重扫 168 组（度量×形式×P×Q×hold）。

**★ v1 核心结论——广度背离短腿无避险价值，且有害（对 v1/T6 的第三条独立佐证）**：
- **扫描（168 组，blend，train/val/holdout）**：**0 组** maxdd_improve 在三窗全为正；训练窗（含 2015 股灾/2018 熊）最优 maxdd_improve=0.000、最差 **−0.28**（加空头腿使回撤恶化 28%）。
- **定参报告（pct_above_ma20/deteriorating/P20/Q20/hold10，blend·full）**：long_engine Sharpe **1.42**/MaxDD −13.9% vs dual_bd **1.16**/−18.0%；**down_month_hit 仅 9.1%**（跌月里加空头腿 91% 时候更糟）、**maxdd_improve −4.1%**；short_bd_engine 独立 Sharpe −0.71、**p=0.95**（与随机不可分）。dual 的 p=0.023 是**多头引擎**的显著性穿透，非空头贡献。
- **五判据全灭**（§3）：down_month_hit>50% ✗、maxdd_improve>0 ✗、短腿显著 ✗、不拖累多头 ✗（1.42→1.16）、regime 稳定——稳定地**负**（三窗皆 dual≤long，2014-2020 最伤）。
- **机制**：A 股牛段常"价新高+广度收窄"（龙头抱团），信号做空进行中的上涨 → 逆 carry 贴水而亏；carry 门控（剔深贴水那批最差的空）只能减轻不能翻正。

**∴ 三轴同判**：财务轴（v1 共享短腿）、行业轴（T6 CITIC 阈值扫描）、**价格/广度轴（本轮广度背离，专属信号+持有期+168 组扫描）** 均证 A 股这些信号族的**空头段无避险价值**。空头引擎的真正解不在 price/breadth/style 派生信号，须换轴 = 杠杆/情绪/衍生品面（两融增速/涨停温度计/ETF 申赎/基差走深，design §2 面3-6）——**多数需 Wind P1.5 数据**（两融无表、bond_daily 空壳、futures 陈旧，见 §0 表3/4）。

**下一步（待用户）**：(a) 换轴攻空头需先补 Wind 数据（两融 EDB endpoint + margin_daily 表 / futures 日更）；(b) 转 doc 2.4/2.5 自建风格篮子（数据不卡、roadmap 另一主线）；(c) 把广度当**多头确认**信号（面2"扩张启动"）而非空头——广度短既证无用，其长向或有价值（新问题）。

## 7. 落地后回填 MEMORY
本 plan 收口后更新 `optimization-roadmap-2026-07.md`：Wind 7-07 恢复、B 启动、广度背离 v1 结论、数据菜单落库规格。✅ 已回填。
