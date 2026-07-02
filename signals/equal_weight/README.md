# equal_weight — 等权配对择时信号

配置驱动的连续择时信号：从价格 CSV 读取指数收盘价，按配置文件定义的两两配对计算相对强弱，全部配对等权平均输出连续信号。本脚本由原 `latest_equal_weight_signal/`（变体 A）与 `latest_equal_weight_signal_5d_20z/`（变体 B）两个参数变体于 2026-07-02 合并而成，两套参数均已验证与原脚本输出字节级一致。

## 文件

- `generate_signal.py` — 主脚本（参数化：`--lookback` / `--z-window` / `--smoothing`）
- `config_6pairs.csv` — 6 组配置（沪深300/中证500/中证1000/中证2000/创业板/科创 成长vs价值），配 `data/成长价值指数_2019.csv`
- `config_5pairs.csv` — 5 组配置（不含科创），配 `data/成长价值指数_2014.csv`（历史更长）

## 计算逻辑

每组配对：

1. 两列价格的日收益率之差（`direction=reverse` 时交换左右）
2. `lookback` 日滚动复合相对收益
3. `z_window` 日窗口 z-score（窗口满后才产生非零信号；默认 `z_window = lookback × 2`）
4. `tanh(z / 2)` 压缩到约 [-1, 1]
5. 所有组等权平均 → `raw_signal_{lookback}` = `factor_value_raw`
6. `factor_value` = `smoothing` 日滚动平均；`--smoothing 0` 则不平滑（等于 raw）

## 两套预设参数

| | 复合收益窗口 | z 窗口 | 平滑 | 数据 | 配置 |
|---|---|---|---|---|---|
| **变体 A**（默认值） | 20 | 40 | 5 | `成长价值指数_2019.csv` | `config_6pairs.csv` |
| **变体 B** | 5 | 20 | 无 | `成长价值指数_2014.csv` | `config_5pairs.csv` |

## 运行（仓库根执行）

```bash
# 变体 A：全部默认值
python3 signals/equal_weight/generate_signal.py

# 变体 B
python3 signals/equal_weight/generate_signal.py \
  --input data/成长价值指数_2014.csv \
  --config signals/equal_weight/config_5pairs.csv \
  --lookback 5 --z-window 20 --smoothing 0 \
  --output output/equal_weight/equal_weight_signal_5d20z.csv
```

## 输入数据格式

CSV，第一列日期（列名不限），其余为价格列，列名须与配置文件一致。数据可包含未使用的列；日期自动解析升序排序；价格自动转数值。

## 配置文件格式

```csv
group,left_column,right_column,direction
1,沪深300成长,沪深300价值,forward
2,中证500成长,中证500价值,forward
```

- `group`：从 1 开始连续编号；组数不限（4 组、10 组、100 组均可）
- `direction`：`forward` = 左减右；`reverse` = 右减左
- 同一列不能重复出现在多组

## 输出列

- `pair_01_factor_{lookback}`, … — 每组单独信号（列后缀跟随 lookback）
- `raw_signal_{lookback}` — 等权平均原始信号
- `factor_value_raw` — 当天未平滑值（= raw_signal）
- `factor_value` — 最终连续择时信号（平滑后；`--smoothing 0` 时等于 raw）

## 常见错误

- `pair config references unknown columns` — 配置里的列名在数据 CSV 中不存在（检查空格/大小写/后缀）
- `pair config contains duplicated columns` — 同一价格列被用于多组
- `pair config groups must be consecutive starting at 1` — 组号不连续
- `direction must be one of ['forward', 'reverse']` — 方向字段拼写错误

## 换数据步骤

1. 新价格 CSV 放入 `data/`，第一列日期
2. 新建/修改配置 CSV 定义配对
3. 运行时指定 `--input` / `--config` / `--output`（及窗口参数）
4. 检查输出 `factor_value`
