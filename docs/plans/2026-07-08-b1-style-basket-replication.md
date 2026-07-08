# 方向 C · B1：自建风格篮子复现验证（2026-07-08 落地）

> 设计依据：`2026-07-03-optimization-roadmap-design.md` §2.4/2.5（自建方案 + 因子草案 + universe 定调）。
> 前置：B1-T1 财务数据层（commit 91922c3，CSMAR+Wind 拼接 reader）。
> 定位：**正确性闸门**——自建成长−价值价差须复现 equal_weight 指数对信号（预期 ρ~0.8），
> 过闸后 B2（行业中性 → 纯风格/行业轮动分解）才有意义。

## 1. 交付

### 因子纯函数层 `signals/common/factors.py`（TDD，23 测试）

- `filter_quarter_ends / quarterize_ytd / ttm_from_quarterly`：YTD→单季→TTM，缺季窗口 NaN；
- **`pit_ttm_with_known`**：TTM 依赖窗 PIT——known(q) = 窗内 [q−4..q] 各行 ann 最大值。
  堵住一个真实前视：TTM(Q1) = Q1ytd + 年报 − 上年Q1ytd，**年报晚于 Q1 披露时旧实现会提前
  用未公开年报**（有专项泄漏测试）。同季重述行取先披露值；
- `rolling_growth_slope`：批量滚动 OLS 斜率（sliding_window_view 向量化，与 `growth_slope`
  连续窗口逐值恒等），窗内缺季 NaN、known = 窗内 TTM 可知日最大值；
- `asof_latest`：pooled 长表按 as_of 取每股已知最新一期（不假设 known/end 单调）；
- `winsorize / cross_section_zscore / composite_score(/√可用因子数) / latest_pit / extract_statement_field`。

### 篮子模块 `signals/style_basket/`（TDD，7 测试）

- `scoring.py`：`style_scores`（缩尾→截面z→成长/价值合成→style_score=G−V）、
  `select_baskets`（Top/Bottom 30%）、`universe_mask`（U0-U4 排名带）、
  `basket_spread_returns`（formation 收盘建仓/次日计收益/持至下一 formation、停牌跳过）；
- `build.py`：三阶段管线 pool→scores→baskets（缓存 `output/style_basket/cache/`，gitignored）
  + `ticker_financial_rows`（CSMAR cfo YTD 链 + Wind cfo_ttm 直接行拼接，有测试）；
- `validate.py`：T4 双层验证 CLI；`README.md`：口径 + 6 项已知限制。

### 真库构建（2026-07-08）

- pool：6,820 票 → TTM 755k / slope 384k / event 430k 行（12 分钟）；
- scores：158 月末（2013-05..2026-07）× 全市场 = 502k 行（1 分钟）；覆盖率 67%(2013)→89%(2026)，
  2014-06 已有 1,434 只可打分（每桶 ~430 只 = 设计稿 ~450 量级）；
- baskets：U0-U4 五套日度价差各 3,178 天 → `output/style_basket/spread_<U>.csv`。

**真库 sanity（2023-06-30 截面抽查）**：茅台 mv_rank=1/EP 3.1%/PB 10；宁德 sal_g=0.20 全场
最高成长；三只金融 cfp=NaN（剔除生效）；长电 sal_g≈0/股息 3.9%；style 两极 = 宁德(+3.3) vs
平安(−2.9)。逐年价差方向全符 A 股风格史（2014 价值年 −17.5% / 2015 创业板牛 +14.7% /
2022-24 红利三年连负 / 2025-26 转正）。

## 2. ★ T4 结果——闸门通过，超预期

| universe | vs 300对 | vs 500对 | vs 1000对 | vs 2000对 | **信号级 vs equal_weight factor_value** |
|---|---|---|---|---|---|
| U0 全市场 | .662/.710 | .732/.808 | .820/.877 | .733/.748 | **.897/.904** |
| U1 剔前300 | .605/.665 | .729/.793 | .826/.873 | .763/.771 | .878/.886 |
| **U2 301-1800** | .665/.727 | .790/.852 | **.873/.912** | .809/.829 | **.906/.915** |
| U3 301-3800 | .616/.678 | .741/.806 | .838/.889 | .786/.798 | .881/.888 |
| U4 1801-3800 | .372/.483 | .408/.557 | .452/.587 | .537/.672 | .634/.619 |

（每格 = 日频 ρ / 月频 ρ；价差收益层 n=3,027 天；信号级=自建两腿净值走 equal_weight
生产管线 20d40z+sm5 后与 committed factor_value 相关）

**判读**：① 信号级 U0-U3 全部 0.88-0.91 > 0.8 预期 → **自建管线正确复现指数对信号轴**；
② 相关性排序与结构直觉完全一致——U2（标的段）最高、与 1000 对最贴（等权篮子的有效市值
段）、300 对最远（大盘 vs 等权）、U4 小微盘诊断段显著脱钩（0.63）→ 不是巧合拟合而是
语义正确；③ 自建价差与指数对**不是同一序列**（0.87 ≠ 1.0）——差异 = universe/加权/
编制怪癖，正是 B2 要分解的对象。

## 3. 已知限制（v1 有意取舍，详见模块 README）

Wind 段营收 TTM 止 2025Q1（SalG 冻结）；无历史 ST 过滤；股本覆盖 5,664/6,108；总市值排名
（非自由流通）；ΔROE 未纳入；DP 取最近披露单行。

## 4. 下一步（B2 起点）

1. **B2 行业中性**：行业内排序重建篮子 → 指数对信号分解为「纯风格 α + 行业轮动 β」，
   两分量各自测 IC——分解本身产生两个新信号（设计稿 §2.4 v2）；
2. universe 选择建议：U2 作对照分解主口径（与指数对语义最贴）、U4 留诊断；
3. 可选加固：Wind 季度营收回填解冻 SalG；软分桶（Gen3 sigmoid）对照变体进扫描。
