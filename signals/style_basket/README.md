# 自建风格篮子（方向 C · B1）

绕开指数编制方，直接用库内财务数据全市场打分构造成长/价值篮子，产出**自建成长−价值价差**。
B1 目标 = 正确性验证：自建价差应复现 equal_weight 指数对信号（预期 ρ~0.8，见设计稿 §2.4），
过闸后进 B2（行业中性 → 纯风格/行业轮动分解，真正的新信号）。

## 因子口径（设计稿 §2.5 自建 v1 草案，Gen2/Gen3 指数编制为蓝本）

| 因子 | 定义 | 数据源（friendly 字段） |
|---|---|---|
| SalG | 最近 12 季营收 TTM 对时间 OLS 斜率 ÷ \|均值\| | `income.revenue`（YTD→单季→TTM） |
| ProG | 同法，归母净利 TTM | `income.net_profit_parent_ytd` |
| EP | 归母净利 TTM / 总市值 | 同上 + 股本×未复权收盘 |
| BP | 归母权益 / 总市值 | `balance.equity_parent` |
| CF/P | 经营现金流 TTM / 总市值；**金融行业剔除**（银行/非银行金融/综合金融，借 Gen3 规则） | CSMAR `cfo_net`(YTD 链) + Wind `cfo_ttm`(直接) 拼接 |
| DP | 每股税前股利 × 股本 / 总市值 | `dividend.cash_dividend_ps_pre_tax`（CSMAR 全历史 1990 起） |

处理：全市场截面 5%/95% 缩尾 → 截面 Z → /√可用因子数 合成（缺因子不稀释）；
`style_score = growth_score − value_score`。

## PIT 纪律

- ann_date 已在 reader 层 cap 到法定披露上限（`financial_field_map.legal_disclosure_deadline`）；
- **TTM 依赖窗**：TTM(q) 经单季差分依赖 [q−4..q] 各行，可知日 = 窗内 ann 最大值——
  年报晚于次年 Q1 披露的乱序场景不会前视（`pit_ttm_with_known`，有专项测试）；
- 斜率可知日 = 12 窗内各期 TTM 可知日最大值；
- 同季重述行取**先披露**值（当时市场看到的口径）；
- 股本 PIT 用 `stock_share_capital.available_date`。

## 篮子构造

- 评估日 = 每月最后交易日（trading calendar 取自 index_daily 000905.SH）；
- 全市场打分（universe 只做末端过滤器，设计定调）；轻过滤 = 有价、有股本、上市 ≥180 天；
- U0 全市场 / U1 剔前300 / U2 301-1800 / U3 301-3800 / U4 1801-3800（总市值排名带，扫描维度）；
- `style_score` 降序 Top 30% = 成长篮、Bottom 30% = 价值篮，桶内日度等权；
- formation 收盘建仓、次日起计收益、持有至下一 formation（含）；停牌成员当日跳过（ffill≤20 日）。

## 运行

```bash
# 三阶段（pool ~20min / scores ~min / baskets ~min），缓存在 output/style_basket/cache/（gitignored）
python3 -W ignore -m signals.style_basket.build --stage all
# 或分阶段断点：--stage pool | scores | baskets

# B1-T4 验证（vs 指数对 + equal_weight factor_value）
python3 -W ignore -m signals.style_basket.validate
```

产出：`output/style_basket/spread_<U>.csv`（growth_ret / value_ret / spread / 两腿累计净值）
+ `validation_*.csv`。两腿累计净值可直接当作一对"自建指数"喂 equal_weight 信号管线。

## 已知限制（v1 有意取舍）

1. **Wind 段营收 TTM 止 2025Q1**：Wind quarter-end 行 `revenue` 为空（营收 TTM 只在日频
   `gr_ttm` 行），SalG 自 2025Q2 起冻结在最后可算窗口；净利/权益/CFO 不受影响。
   升级路径 = 消费日频 `gr_ttm` 行或回填 Wind 季度营收。
2. **无历史 ST 过滤**：stock_status 历史覆盖不足，v1 不剔 ST（设计 U0 要求剔，留待数据补齐）。
3. 股本覆盖 5,664/6,108，缺股本的票无法排名/算比率 → 自动落出 universe。
4. 市值排名用**总市值**（中证编制用自由流通市值），边界处低流通国企排名偏高（设计稿已知近似）。
5. ΔROE（Gen2 第三成长项）v1 未纳入（B1 验证用双成长因子足够，B2 再议）。
6. DP 取最近披露单行（未 TTM 化），一年多次分红的公司略低估。

## B2 行业中性与分解（2026-07-08）

- `--neutral`：每 CITIC 一级行业内 Top/Bottom pct 选样（`select_baskets_industry_neutral`），
  两腿行业分布恒等 → 行业暴露=0 构造保证；打分复用同一 scores（v1−v2 之差 = 纯行业配置分量）。
  行业 PIT：2021-01 起 67 期月度快照 asof；更早静态近似（每股最早标签外推，设计稿定调）；
  无标签票（623 只，多为 2021 前退市）与小行业（<5 只）并桶。
- 产出 `spread_<U>_neutral.csv` + `decomposition_<U>.csv`（`decompose.py`：方差分解 +
  三分量信号化 20d40z+sm5 + 月频非重叠 IC）。
- **结果（U2）**：混合价差年化波动 13.3% ≈ 纯风格 7.7% + 行业轮动 6.8%（各半，定量印证
  设计稿透视的行业暴露 45-73%）；blend 指数对日收益 R²=0.77 被两分量解释。
  **月频 IC：纯风格 0.179 > 混合 0.126（Spearman，n=149，全 6 组目标一致；分窗
  2014-19/2020-26 排序均成立）**；行业轮动分量月频无独立预测力（0.05）、仅短窗
  d5/d10 ~0.10。Caveat：IC 绝对水平集中于 2020+（前半窗两信号皆≈0，风格动量
  可预测性 regime 依赖）。

## 与既有三线的关系

equal_weight（财务因子轴，指数对）/ hybrid20+citic40d（行业标签轴）→ 本篮子 = 统一语义的
**纯财务因子轴、自主口径**。B1 验证复现（ρ 闸门已过）；B2 分解出纯风格与行业轮动两分量，
纯风格 IC 优于混合体 → 「切主」候选（须先走完整 backtest/ 修秤口径评估）。
