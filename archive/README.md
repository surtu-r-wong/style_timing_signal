# archive/ — 历史归档索引

2026-07-02 整理时归档，只读参考。整理前完整原貌见 git 基线 commit `6b07639`。

## 对比/

2026-05 的旧版每日风格信号系统（被 ③ equal_weight 取代）：`style_signal.py` + `run_daily.py` + `generate_style_factor.py`，含其输入 `data.csv`（指数代码宽表）、`连续信号数据.xlsx` 与输出 `style_factor.csv`。
**注意**：`style_factor.csv` 仍被 `tests/` 用作数值回归锚（对照独立实现的输出），勿删。

## old_scripts/

被合并/取代的旧脚本：

- `root/` — 原根目录的 ③ 前身：`generate_equal_weight_signal.py`（v1，相邻列自动配对）、`generate_equal_weight_signal_contrast.py`（v2，无 factor_value_raw 列）、`style_factor_groups.csv`（旧 15 组指数代码配置，测试数值锚仍引用）、v1 的测试文件
- `latest_equal_weight_signal/` — 变体 A 原 README（脚本本体已成为 `signals/equal_weight/generate_signal.py` 的基底）
- `latest_equal_weight_signal_5d_20z/` — 变体 B 原脚本 + README（已并入合并脚本，等价参数 `--lookback 5 --z-window 20 --smoothing 0`）

## old_data/

- `data.csv.0621` — 指数代码宽表 6/21 快照（对比/data.csv 的后续版本）
- `equity_index_futures_daily.csv` — 股指期货日线（2026-03，无脚本引用）

## old_outputs/

2026 年 3-6 月的回测/研究中间输出，按产生来源分目录：

- `root/` — 根目录散落的：four_threshold_*、two_signal_*、optimized_*、threshold_*、citic_vs_config_*、smallcap_*、multi_sleeve_timing_signal、旧等权信号输出、两张 PNG 净值对比图
- `citic40d/` — ② 目录里的 four_threshold 优化结果
- `equal_weight_20d40z/` — 变体 A 的 smooth_vs_raw 系列研究（6/21-6/22）+ 旧输出快照
- `equal_weight_5d20z/` — 变体 B 的 four_threshold 优化结果

## md5 去重记录（整理时删除的完全重复文件）

| 已删除 | 保留的相同文件 | md5 前 8 位 |
|---|---|---|
| `citic_style_signal_40d/中信风格合并.csv` | `data/中信风格合并.csv` | 66558c81 |
| 根目录 `data.csv` | `archive/对比/data.csv` | 26009330 |
| `latest_equal_weight_signal_5d_20z/data.csv` | `data/成长价值指数_2014.csv` | 13dbe3ba |
| `latest_equal_weight_signal_5d_20z/data0621.csv` | `archive/old_data/data.csv.0621` | 961cb39b |
