# style_timing_signal — 风格择时信号研究

基于中信风格指数与成长/价值指数配对的 A 股择时信号研究项目。三条活跃信号线 + 历史研究归档。

## 三条信号线

| 信号线 | 目录 | 一句话逻辑 | 输入 | 输出 |
|---|---|---|---|---|
| ① hybrid20 状态机信号 | `signals/hybrid20/` | 成长/稳健 20d 强弱定方向（250d z + tanh + 状态机），金融/稳定信号只用于阻止做空 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/hybrid20/confirmed_signal.csv` 的 **hybrid_20** 列 |
| ② citic40d 连续信号 | `signals/citic40d/` | 五因子（成长/稳定、周期/消费、金融/稳定、进攻/防御篮子×2）40d z 等权连续值 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/citic40d/citic_style_signal_40d.csv` 的 **factor_20** 列 |
| ③ equal_weight 配对信号 | `signals/equal_weight/` | 配置驱动的成长/价值配对相对强弱，等权平均连续值，参数可调 | **PG `index_daily`**（4对成长价值，默认）· CSV 备份 | `output/equal_weight/equal_weight_signal_{20d40z,5d20z}.csv` 的 **factor_value** 列 |

## 推荐持仓口径（production = long-flat）⭐

**交易这三条信号时，砍掉空头、只做多/空仓（long-flat）。** 部署口径 `production_position`（`signal>0 → +1，否则 0`）与阈值 0 对称多空同秤对比（equal_weight blend · full；**2026-07-11 blend carry 修正后重算**，勘误全记录 → `docs/plans/2026-07-11-external-review-fixes.md`）：

| 持仓口径 | 年化 | Sharpe | MaxDD | Calmar |
|---|---|---|---|---|
| **long-flat（推荐）** | 26.4% | **1.62** | **−16.7%** | **1.58** |
| 对称多空（原口径） | 35.6% | 1.41 | −29.3% | 1.22 |
| buy & hold | 9.2% | 0.36 | −68.9% | 0.13 |

carry 修正（IM 上市前单腿期不再按全额 IC carry 计）让多头少赚 ≈2pp/年、空头少付 ≈3pp/年：同秤下推荐**维持**（Sharpe 差距 0.41→0.20 收窄无翻转；修正前为 1.78 vs 1.37）。两点诚实降级：① "空头无价值"命题弱化为"**无显著独立价值**"——CITIC 轴 16 组阈值 `short_sharpe` 重算后全部转正（`∈[+0.11,+0.21]`，原 [−0.07,+0.04]"无一盈利"不再成立），equal_weight 空头段 Sharpe 0.48，但空头引擎（θ=0.30+carry 门控）单独 Sharpe 仍 0.01 / p=0.13，加空头腿 MaxDD 反差 6.5pp、跌月命中 43.5% < 50%；② Phase 3 双引擎表（θ=0.10 多头腿）修正后 1.28 < 对称 1.42，该评估口径下排序翻转——细节见勘误文档。此后专属信号方向已测尽：**空头五轴 + 多头三轴共八个观察面全 STOP**（p 值偏乐观下仍全灭，校正只会更 STOP）——库内零成本公开信息面无一提供独立于 equal_weight 的增量（复盘 `docs/plans/2026-07-10-optimization-roadmap-retrospective.md`）。

信号 CSV 输出不变（仍是连续因子 / 带空状态机信号）；推荐持仓是**下游口径**，用 `backtest.positions.production_position(factor)`（`signal>0 → +1，否则 0`）得到。一键生成三条线推荐持仓：

```bash
python3 -m backtest.production          # → output/recommended/<signal>_longflat.csv
python3 -m backtest.dual --signal equal_weight   # 复现上表（dual_engine_metrics.csv）
```

## 数据源（2026-07 起：PG 优先）

信号输入的指数收盘价现默认读 **PostgreSQL `stock_selector.index_daily`**（Market Monitor 库），CSV 降级为备份/审计口径（加 `--source csv` 回退）。

- **① hybrid20 / ② citic40d**：默认 `--source pg`（读中信 5 风格 CI005917–21）。已验证 PG 与 CSV 输出**逐字节一致**。
- **③ equal_weight**：默认 `--source pg`。已去掉创业板/科创两对（逻辑性存疑），收敛为 沪深300/中证500/中证1000/中证2000 **四对**（`config_4pairs`，起点 2014-01-02）；csv==pg 输出逐字节一致。旧 `config_5pairs`/`config_6pairs`（含创业板/科创）留档待定稿。
- PG 由 Wind gateway 日更 topup 保鲜（`tools/topup_index_daily.sh`，wsd 额度恢复后接管）；本仓库**只读 PG**，从不直连 gateway。连接配置见 `config/settings.yaml`（gitignored，模板 `config/settings.example.yaml`）。

## 运行（均在仓库根执行）

```bash
# ① hybrid20（两步，顺序执行）
python3 signals/hybrid20/update_growth_stability.py
python3 signals/hybrid20/update_confirmed_signal.py

# ② citic40d
python3 signals/citic40d/generate_signal.py

# ③ equal_weight 变体A（20d 复合收益 + 40d z + 5d 平滑，全部默认值）
python3 signals/equal_weight/generate_signal.py

# ③ equal_weight 变体B（5d + 20d z + 不平滑；同 4 对，仅参数不同）
python3 signals/equal_weight/generate_signal.py \
  --lookback 5 --z-window 20 --smoothing 0 \
  --output output/equal_weight/equal_weight_signal_5d20z.csv

# 测试
python3 -m pytest tests/ -q

# 风格仪表盘（Dash，展示层零新信号；需 pip install dash）
python3 -m dashboard.app        # → http://127.0.0.1:8060
```

## 风格仪表盘（dashboard/）

五轴空头研究收官后的产品化产出（设计 `docs/plans/2026-07-08-style-dashboard-design.md`）：
一屏回答"今天市场在哪"。五区 = ① 三线生产信号状态条（最新因子值 + long-flat
推荐持仓 + 各源截止日）② 风格测量仪（U2 行业中性纯风格价差 + 信号化位置）
③ 涨停温度计（占比/炸板率/溢价 + 250d 分位）④ 杠杆（两融余额/占成交比，读
PG `edb_daily`，不可达自动降级）⑤ 能量+广度（成交额分位 + %>MA + 新高新低差）。
数据来自 committed 研究产物（`output/`、`backtest/output/`），刷新即重读。

尾部数据守卫（自动）：上游只灌了部分股票的日子（如 2026-07-01 仅 11 只）与
qfq 前值复制占位日会被剔除不显示，"数据截至"行按剔除后口径。测量仪价差 CSV
刷新：`python3 -m signals.style_basket.build --stage baskets`（+ `--neutral`）；
温度计/成交额缓存刷新：`python3 -m backtest.thermo_probe --rebuild-thermometer` /
`python3 -m backtest.leverage_probe --rebuild-turnover`。

## 数据流

```
PG stock_selector.index_daily（默认源）
  ├── 中信5风格 CI005917–21 ──→ signals/hybrid20/  ──→ output/hybrid20/
  │                        └──→ signals/citic40d/ ──→ output/citic40d/
  └── 成长价值4对(300/500/1000/2000) ──→ signals/equal_weight/ ──→ output/equal_weight/

data/  (备份/审计口径，--source csv；不再逐日人工维护)
  ├── 中信风格合并.csv ────────────→ ①② 的 --source csv 回退
  ├── 成长价值指数_2014.csv ────────→ ③ 的 --source csv 回退（含旧 5/6pairs 素材）
  └── 沪深300.csv 、 指数.xlsx（研究/备查）
```

日常更新流程：三线输入均读 PG（`tools/topup_index_daily.sh` 保鲜后直接跑命令即可）。CSV 不再需要逐日人工维护，仅作 `--source csv` 备份/审计口径。

## 目录说明

- `signals/` — 三条信号线脚本，各目录有 README 说明计算逻辑
- `data/` — 全部输入数据（`data/README.md` 记录每个文件的来源、格式、更新方式）
- `output/` — 运行产物，脚本自动写入
- `archive/` — 旧版系统（对比/）、被合并的旧脚本、旧数据快照、2026 年 3-6 月回测研究输出（`archive/README.md` 有索引）
- `docs/plans/` — 设计与实施文档（含本次整理的设计与计划）
- `tests/` — pytest 单元测试（含对照独立实现输出的数值回归锚）

## 历史沿革

2026-03 hybrid_20 状态机信号 → 2026-05 对比/style_signal 每日系统 → 2026-06 等权配对信号两参数变体 + citic40d → 2026-07-02 本次整理（版本合并 + 数据输入输出规范化，全程数值零变化，见 `docs/plans/2026-07-02-reorganization-design.md`）→ 2026-07-03~09 三方向优化 initiative（PG 直连 / 修秤+参数重扫 / 双引擎证伪 / long-flat 采纳 / 自建风格篮子四步闭环 / 八轴信号探针全 STOP / 风格仪表盘上线，收官复盘 `docs/plans/2026-07-10-optimization-roadmap-retrospective.md`）。
