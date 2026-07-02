# 风格择时信号项目整理设计（2026-07-02）

## 背景

原目录 `20260325/`（已改名 `style_timing_signal/`）经过 3 月—7 月多轮研究，积累了 4 个信号版本家族、多份重复数据和 ~25 个散落的回测输出，需要整理后继续研究。本次**只做整理，不改变任何信号数值**。

## 版本家族与去留（用户已确认）

| 家族 | 原位置 | 去向 |
|---|---|---|
| ① hybrid_20 状态机信号（成长/稳健 20d + 金融确认，250d z） | 根目录 update_*.py | **保留** → `signals/hybrid20/` |
| ② 五因子中信风格 40d z 连续信号 | `citic_style_signal_40d/` | **保留** → `signals/citic40d/` |
| ③ 等权配对信号（配置驱动，两个参数变体） | `latest_equal_weight_signal/`、`latest_equal_weight_signal_5d_20z/` | **保留并合并** → `signals/equal_weight/` |
| ④ 旧版 style_signal 每日系统 | `对比/` | 归档 |

根目录的 `generate_equal_weight_signal.py`（v1）和 `generate_equal_weight_signal_contrast.py`（v2 旧版）是 ③ 的前身 → 归档。

## 目标结构

```
style_timing_signal/
├── README.md                     # 总索引：三条信号线怎么跑、数据流向
├── signals/
│   ├── hybrid20/                 # ①
│   │   ├── update_growth_stability.py   # 中信风格合并.csv → growth_stability_signal.csv
│   │   ├── update_confirmed_signal.py   # + 金融确认 → confirmed_signal.csv (hybrid_20)
│   │   ├── optimize_signal.py           # 五因子阈值优化研究版（读中信风格合并 + 沪深300）
│   │   └── README.md                    # ← hybrid_20_signal说明.md
│   ├── citic40d/                 # ②
│   │   └── generate_signal.py           # ← citic_style_signal_40d/optimize_signal.py 改名
│   └── equal_weight/             # ③ 合并后
│       ├── generate_signal.py           # 参数化：--lookback / --z-window / --smoothing
│       ├── config_6pairs.csv            # 6 组含科创（原 latest 版配置）
│       ├── config_5pairs.csv            # 5 组无科创（原 5d_20z 版配置）
│       └── README.md                    # 两份 README 合并
├── data/                         # 全部输入数据，含 data/README.md 说明来源与更新方式
│   ├── 中信风格合并.csv           # ①② 共用（原根目录与 ② 目录重复，去重留一份）
│   ├── 成长价值指数_2019.csv      # ③ 输入，1566 行，2019-12-31 起（科创有数）
│   ├── 成长价值指数_2014.csv      # ③ 输入，3029 行，2014-01-02 起（科创 2019 前为空）
│   ├── 沪深300.csv               # ① optimize_signal.py 用
│   └── 指数.xlsx                 # 人工源表，无脚本引用，保留备查
├── output/                       # 运行产物，整理后重跑生成
│   ├── hybrid20/                 # growth_stability_signal.csv、confirmed_signal.csv
│   ├── citic40d/                 # citic_style_signal_40d.csv
│   └── equal_weight/             # 两套参数各一份输出
├── archive/
│   ├── 对比/                     # ④ 整目录
│   ├── old_scripts/              # v1、v2 旧脚本 + 旧 15 组指数代码配置 + v1 测试
│   ├── old_data/                 # 指数代码宽表 data.csv、data.csv.0621、equity_index_futures_daily.csv、连续信号数据.xlsx
│   └── research_202606/          # 6 月回测中间输出（four_threshold_*、smooth_vs_raw_*、two_signal_*、PNG 等）
├── docs/plans/                   # 设计文档（本文件 + 2026-06-18 等权信号 v1 设计）
└── tests/                        # contrast 测试改指向 signals/equal_weight/generate_signal.py
```

## ③ 合并方案

以 `latest_equal_weight_signal/` 版为基（功能最全，含 `factor_value_raw` 列），差异参数化：

| 参数 | 变体 A（原 latest） | 变体 B（原 5d_20z） |
|---|---|---|
| `--lookback` 复合收益窗口 | 20（默认） | 5 |
| `--z-window` z-score 窗口 | 40（默认 = lookback×2） | 20 |
| `--smoothing` 平滑窗口 | 5（默认） | 0 = 不平滑 |

输出列名跟随 lookback：`pair_XX_factor_{lookback}`、`raw_signal_{lookback}`、`factor_value_raw`、`factor_value`。
`--smoothing 0` 时 `factor_value = factor_value_raw`（与原 5d_20z 行为一致，输出仍保留两列）。

## 验证方案（整理不得改变数值）

1. 整理前记录基准：①② 今天（7/2）刚跑过、③ 两变体 6/21 输出俱在，记录各输出 md5。
2. 整理后重跑：
   - ①②：只改路径常量，输出与基准**逐字节一致**。
   - ③：合并脚本 + 变体 A 参数 + `成长价值指数_2019.csv` + 6 组配置 ↔ 原 latest 输出；变体 B 参数 + `成长价值指数_2014.csv` + 5 组配置 ↔ 原 5d_20z 输出。**逐数值一致**（列名中窗口后缀不同属预期，见注）。
3. `pytest tests/` 通过。
4. git 分步提交：基线 → 归位 → 代码适配 → 验证，每步可回滚。

注：原 latest 输出列为 `pair_XX_factor_20/raw_signal_20`，原 5d_20z 为 `pair_XX_factor_5/raw_signal_5`，合并脚本按 lookback 自动命名，两侧对照时按列位置+数值比对。

## 安全措施

- 已 `git init` 并提交整理前完整基线（commit `6b07639`）。
- 只移动/归档，不删除任何文件（`__pycache__`、`.pytest_cache` 缓存除外）。
- GitHub 远程：用户自建 `git@github.com:surtu-r-wong/style_timing_signal.git` 后再推送。

## 后续（本次不做）

研究方向待定（参数研究 / 三线统一回测对比 / 日常化流程），整理完成后另起讨论。
