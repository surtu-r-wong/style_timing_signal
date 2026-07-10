# 三方向优化 initiative 复盘收官（2026-07-03 → 2026-07-09）

> 本文是 2026-07 三方向优化 initiative 的收官单一入口：对照设计稿逐项关帐、
> 八轴裁决全景、资产与教训沉淀、重开条件。细节链接到各 plan 文档，
> 数字均取自 committed 产出（`backtest/output/*.csv`）。
> 设计稿：`2026-07-03-optimization-roadmap-design.md`（v4 终版）。

---

## 0. 一句话结论

**六个工作日、53 个 commit，三方向全闭环**：生产信号从对称三线升级为
**equal_weight long-flat（Sharpe 1.42 / MaxDD −13.9%）**，评价体系换上
真实口径的"秤"，而信号扩展方向——空头五轴 + 多头三轴共**八个观察面
全数 STOP**——**库内零成本公开信息面，无一提供独立于 equal_weight
生产信号的增量**。负结果是决策级产出：它把"还有什么值得做"的边界
一次性画清了。

## 1. 承诺 vs 交付（设计稿 §5 路线图逐项对账）

| 设计稿承诺 | 交付 | 关键 commit / 文档 |
|---|---|---|
| Phase 1 方向③ 存量入库+读库改造 | ✅ 三线默认 `--source pg`，CSV 降备份；13 码 44,244 行 seed + 字节回归全绿 | 1371a2e；`2026-07-03-phase1-db-integration-plan.md` |
| Phase 2 方向② 修评价口径 | ✅ `backtest/` 六模块：全日历/3bp+carry/三口径/bootstrap；三线真实 OOS 基线 = 后续一切对照组 | 2799fa3；`2026-07-06-phase2-eval-framework-plan.md` |
| Phase 2 参数重扫 | ✅ equal_weight 与 citic40d 各 40 组 walk-forward；hybrid20 因子层证明 ≡ citic40d 免扫。三线一致：**lb=20 甜点、现参数近最优、弱环是结构性非参数性** | cfd9d70 / cc08e1d |
| Phase 3 双引擎 v1 | ✅ 组装完成且**证伪核心假设**：共享信号短腿无独立盈利、无避险价值（short Sharpe −0.42, p=.589；dual 1.14 < long 1.42） | ad75847 / 224c27d；`2026-07-06-phase3-dual-engine-v1-plan.md` |
| （计划外）方向 A：long-flat 采纳 | ✅ `production_position` + `output/recommended/`，Sharpe 1.42 > 对称 1.39、MaxDD −13.9% vs −30.2% | 213f807 |
| Phase 4 新信号入池（P1/P1.5） | ✅ 八轴逐个过三关闸门，**全 STOP**（§2） | 9e356fd → 609d2df |
| （计划外）方向 C：自建风格篮子 | ✅ 四步闭环：B1 复现（ρ 0.88-0.91）→ B2 分解（纯风格 IC 0.179 > 混合 0.126）→ 切主证伪（净值 1.01 < 1.39）→ rotation 证伪 | 04a0016 / ba5c27c；`2026-07-08-b1/b2-*.md` |
| （计划外）风格仪表盘产品化 | ✅ Dash 五区一屏 :8060，三重尾部守卫 | 192ad7e；`2026-07-08-style-dashboard-design.md` |

**设计稿 §6 开放问题六条的归宿**：①配对精简 → 以"去创业板/科创、留
300/500/1000/2000 四对"落地（dcb291f），300 对未单独转分歧变量；②universe
→ U0-U4 五套并行落地于 B1；③自建 B1→B2 完成，**B3（市值×风格双排序）被切主
证伪自然终止**——纯风格定位改"风格测量仪"，B3 失去目标非遗漏；④P1/P1.5
清单：基差率/广度/成交额分位/两融/涨停/ERP 全测，**市值轴与离散度未单独立
探针**（前者≈已证伪的风格短腿、后者与广度同面，重叠度判定跳过），ETF 份额
卡 gateway config；⑤成本假设 3bp+carry 落地于引擎全程沿用（carry 实测
IC≈+8.8%/IM≈+12.6% 年化贴水，合 5-10% 先验）；⑥信号写库+定时跑+通知
未列入本期，保鲜靠 topup+重跑。

## 2. ★ 八轴裁决全景（initiative 的核心产出）

空头五轴以回测段/扫描格式裁决，多头三轴 + 后期空头轴以三关闸门探针裁决。
**三关 = 关1 置换显著性 + 关1b 偏 IC 独立增量（控 equal_weight）+
关2 跨窗稳定 + 关3 净 Sharpe**。

| # | 轴（观察面） | 引擎向 | 载体 | 最强证据 | 死因 | 文档/commit |
|---|---|---|---|---|---|---|
| 1 | 财务轴 | 空头 | equal_weight 短腿 | short Sharpe −0.42, p=.589；加腿 maxdd_improve −6.7% | 无独立盈利、无避险价值 | Phase3 v1, ad75847 |
| 2 | 行业轴 | 空头 | CITIC 复合因子×16 组阈值 | short_sharpe ∈ [−0.07,+0.04] 全≈0 | 无任一阈值让空头段转正 | T6, 224c27d |
| 3 | 价格/广度轴 | 空头 | 广度背离 168 组（度量×形式×P×Q×hold） | 0 组 maxdd_improve 三窗全正；定参 down_month_hit 9.1% | 牛段"价新高+广度收窄"做空逆 carry | Phase4, 9e356fd |
| 4 | 杠杆轴 | 空头 | 两融三族（L1/L2/L3'） | best IC 0.145, p=.134, 偏p=.365；三族同号 **+1** | "干柴"方向不存在——两融是趋势跟随变量 | 7a758c7 |
| 5 | 温度轴 | 空头 | 涨停温度计四族 | F1 涨停占比 p=**.038** 独立显著，但偏 p=.269 | 信息真实但被 equal_weight 全覆盖 | 7d01a18 |
| 6 | 基差率轴 | 多头 | C1 水平/C2 变化 | C1 p=.090、净 Sharpe 0.51、方向 **−1**（与先验反） | 不过闸 + 不独立；贴水深=风险预警非修复信号 | 36e0254 |
| 7 | 广度多头向 | 多头 | B1/B2/B3 三族 | 全窗 IC≈0（p .73-.94） | 广度对宽基择时**双向皆无信息**，面2 关帐 | 36e0254 |
| 8 | ERP 轴 | 多头 | E1 水平/E2 变化（1/PE−10Y） | E1 方向合先验但 IC 0.05, p=.358 | 年度级锚语义在日频持有网格读不出（口径限制） | 609d2df |

旁支：**rotation（行业轮动分量，方向 C 拆出）** 同判 STOP——IC 0.193
置换 p=.036 真实，偏 IC 0.047 p=.60 被覆盖（ba5c27c）。它与 F1 是两例
"信息真实但同源"，**没有关1b 就会误报两条新信号**——独立增量检验是
本 initiative 最重要的方法论纪律。

## 3. 不变结论（勿重开清单）

1. **A股短信号族无空头价值**——财务/行业/广度/杠杆/温度五轴同判，
   跨情绪面级收官。空头引擎若要重开，只能在 P2 重数据轴（期权 PCR/
   期指会员持仓）或事件型专属信号里找，不在库内零成本面。
2. **equal_weight 保持生产主信号**——同秤挑战（自建篮子）、增量挑战
   （rotation/F1 两个真实信号被其覆盖）、新轴挑战（八轴偏 IC 全不显著）
   三重确认。其对公开信息面的覆盖度是本轮最被低估的发现。
3. **production 口径 = long-flat**（Sharpe 1.42 / MaxDD −13.9% / 换手
   10.8）；加空头腿在任何已测形态下都是拖累。
4. **lb=20 甜点**跨 equal_weight/citic40d 两条独立线交叉验证；现有参数
   近最优，**增益是结构性不是参数性**——继续调参无产出。
5. **指数对价差 = 纯风格 + 行业轮动约各半**（vol 7.7%+6.8%≈13.3%）；
   纯风格是更好的"风格测量仪"但更差的宽基择时信号（IC↑净值↓=目标错位）。
6. **分窗好看 ≠ 真信号**——跨 regime 秩抵消（E2 分窗 −0.21/−0.39 全窗
   归零、F2 早 regime 独有），非重叠 IC + 循环移位置换 + 分窗核对缺一不可。
7. **描述力 ≠ 预测力**——温度计对 2024-10 情绪顶的刻画是教科书级
   （10-08 炸板率 68.7% → 10-09 溢价 −4.7%），对 k 日前瞻仍零增量。
   好仪表盘不必是好信号，两种价值分开计。

## 4. 资产清单（后续工作的起点）

**方法论**（全部有测试钉死，190 项全绿）：
- 三关闸门 + `partial_rank_ic`（关1b）——一切新信号入池的评估模板；
- `run_families_probe` 通用探针编排器（`backtest/leverage_probe.py`）——
  任意新轴装配 `sigs_all` 即插即用，杠杆/温度/多头/ERP 四轮复用验证；
- 双侧化探针（`pick_representative` 同号选代表）+ 非重叠 IC + 循环移位
  置换 + 同换手 bootstrap；
- "先修秤再称重"：`backtest/` 评价框架（全日历/成本/三口径/分段/显著性）
  是后续一切研究的对照组基座；
- PIT 纪律件：`pit_ttm_with_known`（堵年报晚披露前视）/`pit_lag`（T+1
  公布滞后）/涨停判定 half-up 整数分位算法（Python round 银行家舍入陷阱）。

**数据**（PG + committed 缓存）：
- `stock_selector.edb_daily`：两融 8 条（2010-04 起）+ 10Y 双源 + 万得
  全A 静/滚 PE 15,720 行（2005 起）；
- `backtest/output/market_turnover.csv` 全市场成交额干净分母（8,672 日，
  混单位逐行归一，2024-10-08 盲验偏差 2%）；
- `backtest/output/thermometer.csv` 涨停温度计（8,672 日，锚点+SQL↔pandas
  双验证）——仪表盘直接消费；
- `output/style_basket/` U0-U4×2 套自建风格价差（风格测量仪）；
- `index_daily` 13 信号码 2010 起 + ICIM 现货 2013 起。

**工程**：三线 `--source pg`（PG 唯一真源）、`backtest/` 十余模块、
`dashboard/` 五区（:8060）、`tools/`（diff/topup/EDB 回填链路）。

**运维坑图**（详见各 runbook）：360 拦截幻象（502≠gateway 崩溃；可靠路径
= ssh→本机 curl→scp→backfill）、WindPy 桌面会话隔离、gateway `wind_ready`
一次性 flag 不可信、Tailscale 假直连黑洞（复发性，根因 Debian 侧待查）、
`stock_daily_price.amount` 混单位（同日可混千元/元）、pip 绕代理直连清华。

## 5. 生产状态定格（2026-07-10）

- **主信号**：equal_weight（四对 20d40z+sm5，blend 口径），下游持仓口径
  long-flat（`backtest/production.py` → `output/recommended/`）；
- **信号保鲜至 2026-07-08**（3eeef48）：7 月首周风格转价值——ew −0.25
  空仓 / citic −0.36 空仓 / hybrid +0.58 持多；
- **仪表盘**：`python3 -m dashboard.app` → :8060，五区一屏 + 三重尾部守卫；
- **测试基线**：本项目 190 过（收官日实测），wind_gateway 148 过；
- **保鲜链路**：`tools/topup_index_daily.sh` → 三线重跑 → production.py。
  日更调度未列本期（见 §6）。

## 6. 边界外与重开条件

按"什么变化触发重开"组织，而非待办：

| 项 | 现状 | 重开条件 |
|---|---|---|
| **基差率 C1**（八轴中唯一接近过闸者） | p=.090/净 Sharpe 0.51，样本被 IM 上市日+futures_daily 断更锁短（k=40 仅 79 窗） | `futures_daily` 日更接通、样本拉长后**复检**——这是唯一预先登记的复检项；语义按"风险预警"用，不是修复做多 |
| ERP 年度口径 | 日频持有网格测不到年度级锚语义（诚实口径限制） | 若做**年度再平衡**战略配置口径，可另立探针 |
| ETF 申赎（面6） | fund_nav_daily 只有净值列；份额两字段（unit_fundshare_total/unit_floortrading）已核对待用户加 gateway config | config 加字段 + 回填后，走 run_families_probe 即插即测 |
| P2 重数据轴 | 期权 PCR/期指会员持仓未入库 | 本 initiative 不追；若空头命题重开，这是仅存方向 |
| 日更调度器 | ✅ **已于 07-10 上午本机 tmux 重启**（session `stock_scheduler`，三任务注册；全链探针 PG/gateway/Wind 会话全绿）；07-09 缺日已按 runbook 补齐（price/indicator/status 各 6,107 行=全 universe，旧宿主只写 5,200）；07-02~08 缺口维持不回填 | 注意 tmux 不抗重启，机器重启后需重挂；17:00 首跑后用 runbook SQL 验收。**C1 复检的真正闸门是 futures_daily**（写入方已定位=market-monitor `data-collecter/collector.py`，WindPy 直连、须 Windows 桌面会话启动，04-29 停）——复活待用户：桌面起 collector，或另议 Linux 侧走 gateway 的 IC/IM 轻量 topup |
| stock_daily_price 07-02~08 缺口 | **已拍板不回填**（只碰仪表盘尾端，守卫自动剔除） | 如补：backfill CLI 幂等续跑 + `--rebuild-thermometer/--rebuild-turnover` |
| 创业板/科创 4 条 Wind 码 | 用户判定两对逻辑不通、不追 | 仅当想复现 equal_weight 旧 6 对变体 A 全列时才需要 |

## 7. 过程教训（工作方式层）

1. **两次"卡数据"误判被实查翻案**——"涨停温度计卡 Wind"（实际
   stock_daily_price 规则重构零等待）、"广度要跨库回填"（实际同库直算）。
   教训：**说"被数据卡住"之前，先实查库**；本 initiative 两个最高产的
   探针轴都来自翻案。
2. **证伪要花和证实同等的工程**——八轴 STOP 每一个都有 TDD 探针、置换
   检验、双重验证锚点撑着；正因为如此，"勿重开清单"才敢写"不变结论"。
3. **闸门纪律防了两次误报**（rotation、F1 均过关1 却死在关1b）；反过来，
   温度轴 F2 分窗漂亮但全窗归零，是置换+非重叠拦住的。**没有这两道闸，
   本轮会"发现"三四条假新信号**。
4. **字节回归让重构敢下手**——三线源切换、参数暴露、四对 reshape 全程
   `cmp` 逐字节护栏，生产输出零意外漂移。
5. 演进式推进的正确姿势：Phase 3 v1 的价值恰恰是**先立框架、快速证伪
   共享短腿**，让后面五个空头轴有了明确的证伪对照和复用机器。

---

**initiative 关闭。** 后续新工作（若有）从 §6 重开条件表进入；新信号
一律走 `run_families_probe` + 三关闸门，对照组 = long-flat 基线
Sharpe 1.42。
