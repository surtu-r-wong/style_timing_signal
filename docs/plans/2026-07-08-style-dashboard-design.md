# 风格仪表盘（style dashboard）设计（2026-07-08，五轴收官后用户选定产品化）

> 前置：五轴空头证伪收官（杠杆 `2026-07-08-leverage-probe-design.md` + 温度
> `2026-07-08-thermo-probe-design.md`）。定位来自切主证伪的结论：纯风格信号
> =**风格测量仪**（研究/监控，非生产信号）；温度计的**描述力**（2024-10 教科书
> 序列）正是仪表盘素材。用户拍板：Dash 交互服务、五区一屏（核心三件套+杠杆
> +能量/广度），carry 面板不上（futures_daily 止 2026-04-29 陈旧）。

## 1. 定位

回答一个问题：**"今天市场在哪"**——生产信号什么状态、风格钟摆什么位置、
情绪什么温度、杠杆什么水位。展示层产品，**不产生任何新信号**；所有序列来自
已闭环研究的 committed 产物 + PG 两融。

## 2. 架构

```
dashboard/
├── __init__.py
├── data.py      # 装载层（纯函数，TDD）：CSV 读取 + PG 两融 + 滚动分位 + 新鲜度
├── figures.py   # 图形层（纯函数）：DataFrame → plotly Figure
└── app.py       # Dash 薄壳：布局 + 回调；python3 -m dashboard.app → :8060
```

- 端口 8060（避开 spread_analyzer 的 8050）；依赖 `pip install dash`（带 plotly）；
- **刷新即重读**：页面刷新触发回调重新装载（CSV 便宜、两融一条 PG 查询，
  复用 `leverage_probe._load_margin`）；无后台轮询——上游数据本就是批式更新；
- **新鲜度明示**：数据源截止日不齐（equal_weight 止 6-18 / hybrid20/citic40d
  止 7-02 / 温度计·成交额止 7-01 / 两融止 7-07）→ 每面板右上角标截止日；
- 全局时间范围选择器：1y / 3y / 5y / 全部。

## 3. 五区一屏

| 区 | 内容 | 数据源 |
|---|---|---|
| ① 状态条 | 三线信号最新值方向色块 + long-flat 推荐持仓（多/平）+ 各源截止日 | `output/{equal_weight,hybrid20,citic40d}` + `output/recommended/*_longflat.csv` |
| ② 风格测量仪 | U2 中性纯风格累计价差（growth/value 双线）+ 信号化位置副图 | `output/style_basket/spread_U2_neutral.csv` + `signal_pure_style_U2.csv` |
| ③ 涨停温度计 | 涨停占比 / 炸板率 / 溢价，250d 滚动分位着色 | `backtest/output/thermometer.csv` |
| ④ 杠杆 | 两融余额（沪+深）+ 融资买入占成交比 + 20d 增速 | PG `edb_daily` + `market_turnover.csv` |
| ⑤ 能量+广度 | 成交额 250d 分位带；pct>MA20/60 + 新高新低差 | `market_turnover.csv` + `breadth.csv` |

统一口径：**250d 滚动分位**（`rolling_percentile`）作为所有"当前在历史什么
位置"的读数；不重复信号化（信号化位置用 committed 信号 CSV 直读）。

## 4. 测试

- `data.py` 纯函数 TDD：rolling_percentile 语义 / 新鲜度提取 / 状态条装配
  schema / 各 loader 对 committed CSV 的列契约；PG 两融 loader 不进单测
  （集成路径，复用已测的 `_load_margin`）；
- `figures.py` 轻断言：trace 数 / 轴标题（图形正确性靠人工验收）；
- `app.py` smoke：布局构造不炸；启动验证走人工/curl。

## 5. 边界（v1 不做）

- 不做日更调度（上游断点在运维侧，等 futures_daily/wsd 日更接通后另议）；
- 不做信号历史回放/参数调节（是仪表盘不是研究台）；
- carry 面板等数据保鲜后加（占位注释）。

## 6. ★ 结果（2026-07-09 落地）——上线 localhost:8060，视觉验收两轮通过

TDD 21 新测试（data 13 / figures 4 / app 2 + 守卫 2，全套 **182 过**）；
headless Chrome 截图两轮视觉验收（dataviz 第 7 步）。色板 = dataviz 参考
色板，`validate_palette.js` PASS（aqua 对比度 WARN 以图例+悬浮值缓解）。

**性能**：PG 两融查询 ~7s 是页面瓶颈 → 进程内 TTL 缓存（300s），首载 ~2s、
range 切换 **0.26s**。

**第一轮截图抓出 3 个真问题（全部 TDD 修复）**：

1. **尾部不完整日**：`stock_daily_price` 2026-07-01 只灌了 **11 只股票**
   （正常 ~5,175）→ 成交额 130 亿垃圾值、占成交比飙 3000%、涨停占比归零。
   修 = `trim_incomplete_tail`（尾部回走剔除 <0.3×近20日中位的日子，
   中段真实低活跃日不受影响）；
2. **测量仪切窗未归一**：两腿显示全历史累计（5.0 vs 1.5）近窗不可读。
   修 = `rebase_indices`（切窗后归一窗口起点=1）；
3. **图例混排**：单系列子图 trace 混入全局图例。修 = 子图标题即身份原则，
   单系列 `showlegend=False`。

**追加数据发现（运维级）**：`stock_daily_price_qfq` 的 2026-07-01 在
style basket build 时点（07-08）是**前值复制占位行**（两腿收益精确 0），
之后管线覆盖为真值（+0.94%/σ3.3%）——committed spread CSV 尾部即占位快照。
修 = `trim_zero_return_tail`（尾部全腿 |ret|<1e-4 判占位剔除）。
**测量仪 CSV 日常刷新 SOP**：`python3 -m signals.style_basket.build
--stage baskets`（+ `--neutral`）——注意 qfq 全表重算会使全历史价差微 diff，
刷新 commit 与研究 commit 分开。

**留跑验收**：`python3 -m dashboard.app` → http://127.0.0.1:8060。
