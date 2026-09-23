# style_timing_signal — 风格择时信号研究

基于中信风格指数与成长/价值指数配对的 A 股择时信号研究项目。四条活跃信号线 + 历史研究归档。

## 四条信号线

| 信号线 | 目录 | 一句话逻辑 | 输入 | 输出 |
|---|---|---|---|---|
| ① hybrid20 状态机信号 | `signals/hybrid20/` | 成长/稳健 20d 强弱定方向（250d z + tanh + 状态机），金融/稳定信号只用于阻止做空 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/hybrid20/confirmed_signal.csv` 的 **hybrid_20** 列 |
| ② citic40d 连续信号 | `signals/citic40d/` | 五因子（成长/稳定、周期/消费、金融/稳定、进攻/防御篮子×2）40d z 等权连续值 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/citic40d/citic_style_signal_40d.csv` 的 **factor_20** 列 |
| ③ equal_weight 配对信号 | `signals/equal_weight/` | 配置驱动的成长/价值配对相对强弱，等权平均连续值，参数可调 | **PG `index_daily`**（4对成长价值，默认）· CSV 备份 | `output/equal_weight/equal_weight_signal_{20d40z,5d20z}.csv` 的 **factor_value** 列 |
| ④ slope20 斜率信号 | `signals/slope20/` | 四对20日对数价格斜率差，120日z，等权、不加平滑 | PG `index_daily` | `output/slope20/slope20_signal_L20zw120.csv` 的 **factor_value** 列 |

## 当前持仓与研究证据

2026-09-10 的两池决策：现货池跟 **slope20 long-flat**，期货池跟 **equal_weight 对称多空**。文件分别是 `output/recommended/slope20_longflat.csv` 与 `output/recommended/equal_weight_symmetric.csv`。四条生产映射和参照文件仍由 `backtest.production` 统一生成。

```bash
python3 -m backtest.production
```

详见 [两池决策](docs/plans/2026-09-10-two-pool-signal-assignment-decision.md) 与 [研究权威索引](docs/plans/README.md)。旧“所有信号只做多、空头无价值”的概括已被取代；各STOP只约束其冻结规格。

[2026-09-14后续研究](docs/plans/2026-09-14-research-followthrough-results.md)补充实际合约执行、独立零假设校准与风险配置。旧指数+carry收益不是实际账户业绩；2024-2026是反复使用的第二验证窗。现货实际持仓尚未提供，当前执行研究使用指数代理。新研究不自动变更生产映射。

**2026-09-14阶段收尾**：暂保留现役，依据是尚无充分替换证据，不能称已证明最优；两池分工优势未确认，统计GO关闭。前瞻记录工具已准备，当前0观察且未安装自动调度。完整状态及后续入口见[收尾交接](docs/plans/2026-09-14-research-closeout.md)。

## 数据源（2026-07 起：PG 优先）

信号输入的指数收盘价现默认读 **PostgreSQL `stock_selector.index_daily`**（Market Monitor 库），CSV 降级为备份/审计口径（加 `--source csv` 回退）。

- **① hybrid20 / ② citic40d**：默认 `--source pg`（读中信 5 风格 CI005917–21）。已验证 PG 与 CSV 输出**逐字节一致**。
- **③ equal_weight**：默认 `--source pg`。已去掉创业板/科创两对（逻辑性存疑），收敛为 沪深300/中证500/中证1000/中证2000 **四对**（`config_4pairs`，起点 2014-01-02）；csv==pg 输出逐字节一致。旧 `config_5pairs`/`config_6pairs`（含创业板/科创）留档待定稿。
- **2026-09-23 起 15 个输入码由 data_manager 办公室的夜间作业写入 `index_daily`**（WSL2 每晚 20:00 起跑、约 20:02 结束，每晚补数前重看前 5 个交易日纠错；请求函与处置见 `data_manager/requests/2026-09-23-style-timing-signal-index-daily-takeover/`），**本链路只读、完全不写库**：日更链路第一步只等当日 15 码到齐（`deploy/daily_signals/wait_for_inputs.py`，最迟等到 21:30），信号脚本与护栏只读 PG，推荐持仓读 committed 信号 CSV。连接配置见 `config/settings.yaml`（gitignored，模板 `config/settings.example.yaml`）。
- 〔回退用，2026-08-12~09-22 的做法〕PG 由日更链路第一步 topup 保鲜：`tools/topup_index_daily.sh` 调 stock_selector 的 backfill CLI，经 Wind gateway 取 15 个输入码、幂等写 `index_daily`（默认回看 14 天）；调用前由前置闸门 `deploy/daily_signals/topup_guard.py` 只读探网关 `/ping`、`/health`、`/quota` 并查库，存疑就不调用（网关地址与 token 在 `config/settings.yaml` 的 `wind_gateway` 段）。删掉标志文件 `deploy/daily_signals/SKIP_TOPUP` 即回到这种模式，见 `deploy/daily_signals/README.md`。

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

# ④ slope20
python3 signals/slope20/generate_signal.py

# 测试
python3 -m pytest tests/ -q

# 风格仪表盘（Dash，展示层零新信号；需 pip install dash）
python3 -m dashboard.app        # → http://127.0.0.1:8060
```

### 日更自动化（2026-08-12 起）⭐

上面这些命令**不再需要人手跑**：`deploy/daily_signals/` 把输入 → 各信号线 → 推荐持仓 → 护栏 → 企业微信推送
串成一条链，由 systemd user timer 在**工作日 20:30**（Asia/Shanghai；2026-09-23 前是 18:30）自动触发，
`Persistent=true` 会补跑关机错过的触发。输入指数 2026-09-23 起由 data_manager 办公室的夜间作业（20:00 起跑、
约 20:02 结束）写 `index_daily`，链路第一步只读等当日 15 码到齐（最迟等到 21:30，没齐就用库内已有数据照算、
推送里标明）；此前由链路第一步 topup 自己经 Wind 网关取（stock_selector 17:30 的 daily_index 自 2026-08-14 起
改手动），topup 留作回退。
链路末尾有**新鲜度护栏**：各生产信号与推荐持仓（含现货池文件）落后 `index_daily` 最新交易日
超过 1 个交易日即失败退出并打 `STALE`
（本仓库此前零自动化、停更 35 天无人发现，见 `docs/plans/2026-08-12-project-review-and-priorities.md`）。

```bash
systemctl --user list-timers style-signals-daily.timer   # 下次触发时间
systemctl --user start style-signals-daily.service       # 立即跑一次
cat logs/daily_signals_status.json                       # 最近一次运行的结果与各产出末日
```

安装步骤、环境变量与护栏演示见 `deploy/daily_signals/README.md`。

## 风格仪表盘（dashboard/）

五轴空头研究收官后的产品化产出（设计 `docs/plans/2026-07-08-style-dashboard-design.md`）：
一屏回答"今天市场在哪"。五区 = ① 各生产线信号状态条（最新因子值 + 生产口径
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
                                    └──→ signals/slope20/      ──→ output/slope20/

data/  (备份/审计口径，--source csv；不再逐日人工维护)
  ├── 中信风格合并.csv ────────────→ ①② 的 --source csv 回退
  ├── 成长价值指数_2014.csv ────────→ ③ 的 --source csv 回退（含旧 5/6pairs 素材）
  └── 沪深300.csv 、 指数.xlsx（研究/备查）
```

日常更新流程：各生产线输入均读 PG（2026-09-23 起由 data_manager 办公室的夜间作业写入，此前由 `tools/topup_index_daily.sh` 保鲜）；这一串由 `deploy/daily_signals/` 的 systemd timer 每工作日 20:30 自动执行（2026-08-12 起自动化，2026-09-23 前是 18:30），无需人工介入。CSV 不再需要逐日人工维护，仅作 `--source csv` 备份/审计口径。

## 目录说明

- `signals/` — 生产信号线脚本（每线一个子目录，见上「四条信号线」表）+ `common/` 共用件 + `style_basket/` 研究篮子；计算逻辑见各目录 README 或脚本头注释
- `data/` — 全部输入数据（`data/README.md` 记录每个文件的来源、格式、更新方式）
- `output/` — 运行产物，脚本自动写入
- `archive/` — 旧版系统（对比/）、被合并的旧脚本、旧数据快照、2026 年 3-6 月回测研究输出（`archive/README.md` 有索引）
- `docs/plans/` — 设计与实施文档（含本次整理的设计与计划）
- `tests/` — pytest 单元测试（含对照独立实现输出的数值回归锚）

## 历史沿革

2026-03 hybrid_20 状态机信号 → 2026-05 对比/style_signal 每日系统 → 2026-06 等权配对信号两参数变体 + citic40d → 2026-07-02 本次整理（版本合并 + 数据输入输出规范化，全程数值零变化，见 `docs/plans/2026-07-02-reorganization-design.md`）→ 2026-07-03~09 三方向优化 initiative（PG 直连 / 修秤+参数重扫 / 双引擎证伪 / long-flat 采纳 / 自建风格篮子四步闭环 / 八轴信号探针全 STOP / 风格仪表盘上线，收官复盘 `docs/plans/2026-07-10-optimization-roadmap-retrospective.md`）。
