# 动量变换器扫描:成长/价值配对的判强尺子替换评估(2026-07-10)

> 用户开题:分组不动(仍是成长/价值四对),把配对判强弱的度量从"区间相对
> 收益"换成"相对动量"。brainstorm 三段(家族/机器裁决/交付)已逐段确认。
> 这**不是第九根信息轴**——同一信息源换变换器,裁决框架用 walk-forward
> 扫描 + 同秤头对头(step-2/切主评估同款),不用八轴三关闸门(同源变体
> 几乎必死在关 1b 偏 IC,那是未测先判,不适合替换命题)。

## 1. 命题

现任:`_compute_pair_signal` = 日收益差 → lookback 滚动复利(20d)→
z(40) → 四对等权 → 平滑(5) = 生产 equal_weight(long-flat Sharpe 1.42)。
替换候选:每腿算动量、配对取差,z→等权→平滑下游完全一致。问:**动量
变换器能否在同秤下打赢现任收益变换器?**

## 2. 因子家族(三族 14 形态)

| 族 | 定义(每腿) | 网格 |
|---|---|---|
| M1 经典跳月 | `P(t−skip)/P(t−L−skip) − 1` | L∈{60,120,250} × skip∈{0,20} |
| M2 趋势斜率 | log 价格对时间滚动 OLS 斜率 | L∈{20,60,120,250} |
| M3 风险调整 | 区间收益 / 同窗日波动 | L∈{20,60,120,250} |

下游:z_window∈{40,120,250} × smoothing∈{0,5} → **84 组合**,scan_grid
三窗(train 14-20 / val 21-23 / holdout 24-26,blend 口径 + carry,
mode=discrete,3bp)。

**先验登记(诚实口径)**:step-2 已扫过收益差 L∈{40,60} 且全退化 →
"纯拉长形成期(skip=0)"大概率已死;本轮真正的活假设是 **skip 跳月**
(剔近月反转污染)与**非端点度量**(斜率路径加权 / 波动归一)。
锚点:M1(L=20, skip=0) ≈ 现任,作恒等性 sanity 不进裁决。

## 3. 机器

新模块 `backtest/momentum_scan.py`,**不碰 `signals/` 生产代码**(零字节
回归风险),四层:

1. 动量纯函数:`momentum_classic(price, L, skip)` /
   `momentum_slope(log_price, L)`(向量化滚动 OLS,拒绝逐窗 apply)/
   `momentum_voladj(price, L)`;
2. 配对装配 `momentum_pair_factor(prices, ...)`:每腿动量 → 配对差 →
   z(z_window) → 四对等权 → 平滑(下游与生产逐步对齐,用生产函数对拍);
3. `momentum_factor_fn(family)` 接入现成 `scan_grid`(PG 8 列一次加载,
   equal_weight_factor_fn 同款);
4. 同秤头对头:每族高原代表上 baseline 全套口径(blend·full+分段、
   long-flat+对称、bootstrap 500)与现任 20d40z+sm5 并排。

产出:`backtest/output/scan_momentum.csv` + `momentum_head2head.csv`
(胜负皆入库,证伪也留证据)。

## 4. 裁决标准(预登记,防事后挑数)

**换尺子门槛,三条全中才谈替换**:

1. 三窗全正且是高原(worst-window ≥ 现任同窗);
2. holdout Sharpe 明确高于现任;
3. 全窗 long-flat Sharpe ≥ 1.42 且 MaxDD 不劣。

**平局/局部略优 → 不换**:同源变体预期相关 0.8+,无全面优势时替换只有
回归成本没有收益,结论记"收益变换器第三次确认"。corr(变体,现任)与
置换 p 只作诊断列不作闸门。反过拟合纪律沿用 step-2:拒绝 train 负、
holdout 高的尖峰;高原比较看三窗最差值。

## 5. 测试锚(TDD 先行)

1. 恒等锚:M1(L=20,skip=0) 因子与现任 factor_value 相关 >0.99;
2. slope 向量化 ≡ naive 逐窗 OLS(数值恒等);
3. voladj 小样本手算锚;
4. 装配层下游与生产 `calculate_contrast_equal_weight_signal` 同输入对拍;
5. skip 对齐语义钉死(`P(t−skip)` off-by-one);
6. 短历史/NaN 守卫(min_periods 语义)。

## 6. 判定后路径

- **三条全中** → 生产切换另开 plan(改 equal_weight 默认破字节回归:
  单独 commit + 重生成两输出 + 改测试断言,dcb291f 先例);
- **不过** → 本文档追加 ★ 结果段,复盘文档 §6 追一行,STOP 归档;
- 工程量:纯函数+测试半天内,扫描分钟级,当天出裁决。
