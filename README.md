# style_timing_signal — 风格择时信号研究

基于中信风格指数与成长/价值指数配对的 A 股择时信号研究项目。三条活跃信号线 + 历史研究归档。

## 三条信号线

| 信号线 | 目录 | 一句话逻辑 | 输入 | 输出 |
|---|---|---|---|---|
| ① hybrid20 状态机信号 | `signals/hybrid20/` | 成长/稳健 20d 强弱定方向（250d z + tanh + 状态机），金融/稳定信号只用于阻止做空 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/hybrid20/confirmed_signal.csv` 的 **hybrid_20** 列 |
| ② citic40d 连续信号 | `signals/citic40d/` | 五因子（成长/稳定、周期/消费、金融/稳定、进攻/防御篮子×2）40d z 等权连续值 | **PG `index_daily`**（中信5风格，默认）· CSV 备份 | `output/citic40d/citic_style_signal_40d.csv` 的 **factor_20** 列 |
| ③ equal_weight 配对信号 | `signals/equal_weight/` | 配置驱动的成长/价值配对相对强弱，等权平均连续值，参数可调 | CSV `成长价值指数_2019/_2014`（PG 待切） | `output/equal_weight/equal_weight_signal_{20d40z,5d20z}.csv` 的 **factor_value** 列 |

## 数据源（2026-07 起：PG 优先）

信号输入的指数收盘价现默认读 **PostgreSQL `stock_selector.index_daily`**（Market Monitor 库），CSV 降级为备份/审计口径（加 `--source csv` 回退）。

- **① hybrid20 / ② citic40d**：默认 `--source pg`（读中信 5 风格 CI005917–21）。已验证 PG 与 CSV 输出**逐字节一致**。
- **③ equal_weight**：暂仍默认 `--source csv`。其配置含创业板/科创两对（逻辑性存疑、暂缓），定稿后再切 PG。
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

# ③ equal_weight 变体B（5d + 20d z + 不平滑，更长历史数据）
python3 signals/equal_weight/generate_signal.py \
  --input data/成长价值指数_2014.csv \
  --config signals/equal_weight/config_5pairs.csv \
  --lookback 5 --z-window 20 --smoothing 0 \
  --output output/equal_weight/equal_weight_signal_5d20z.csv

# 测试
python3 -m pytest tests/ -q
```

## 数据流

```
PG stock_selector.index_daily（默认源）
  ├── 中信5风格 CI005917–21 ──→ signals/hybrid20/  ──→ output/hybrid20/
  │                        └──→ signals/citic40d/ ──→ output/citic40d/
  └──（成长价值指数：待 equal_weight 切 PG 后启用）

data/  (备份/审计口径，--source csv；不再逐日人工维护)
  ├── 中信风格合并.csv ──────────→ ①② 的 --source csv 回退
  ├── 成长价值指数_2019/_2014.csv ──→ signals/equal_weight/ ──→ output/equal_weight/（③ 暂默认）
  └── 沪深300.csv 、 指数.xlsx（研究/备查）
```

日常更新流程：①② 输入由 PG 日更（`tools/topup_index_daily.sh`，wsd 额度恢复后接管），直接跑命令即可；③ 待切 PG。CSV 不再需要逐日人工维护，仅作 `--source csv` 备份/审计口径。

## 目录说明

- `signals/` — 三条信号线脚本，各目录有 README 说明计算逻辑
- `data/` — 全部输入数据（`data/README.md` 记录每个文件的来源、格式、更新方式）
- `output/` — 运行产物，脚本自动写入
- `archive/` — 旧版系统（对比/）、被合并的旧脚本、旧数据快照、2026 年 3-6 月回测研究输出（`archive/README.md` 有索引）
- `docs/plans/` — 设计与实施文档（含本次整理的设计与计划）
- `tests/` — pytest 单元测试（含对照独立实现输出的数值回归锚）

## 历史沿革

2026-03 hybrid_20 状态机信号 → 2026-05 对比/style_signal 每日系统 → 2026-06 等权配对信号两参数变体 + citic40d → 2026-07-02 本次整理（版本合并 + 数据输入输出规范化，全程数值零变化，见 `docs/plans/2026-07-02-reorganization-design.md`）。
