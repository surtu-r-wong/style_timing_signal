# 等权风格择时信号生成说明

本目录包含一个可配置的连续择时信号生成脚本。脚本从价格 CSV 中读取指数或资产价格，按配置文件定义的两两组合计算相对强弱信号，再对所有组做等权平均并输出连续信号。

## 文件

- `generate_equal_weight_signal_contrast.py`: 主脚本。
- `style_factor_groups.csv`: 组配置文件，定义每组使用哪两列以及计算方向。

## 计算逻辑

每组信号按以下步骤计算：

1. 计算两列价格的日收益率。
2. 计算相对收益：左列日收益率减右列日收益率。
3. 计算 5 日滚动复合相对收益。
4. 用 20 日窗口做 z-score 标准化，20 日窗口满后才开始产生非零信号。
5. 用 `tanh(z / 2)` 压缩到约 `[-1, 1]`。
6. 所有组等权平均，得到 `raw_signal_5`，同时输出为 `factor_value_raw`。
7. `factor_value` 直接等于当天未平滑值，不再做 5 日滚动平均。

## 输入数据格式

输入数据必须是 CSV。第一列必须是日期，列名不限；后面是价格列，列名需要和配置文件一致。

示例：

```csv
date,A1,B1,A2,B2,Unused
2024-01-01,100,98,200,210,999
2024-01-02,101,97,198,211,999
2024-01-03,103,99,201,209,999
```

说明：

- 日期列会自动解析并按升序排序。
- 价格列会自动转成数值。
- 数据里可以有未使用的列，脚本会忽略配置文件中没有出现的列。
- 配置文件中出现的列必须在数据 CSV 中存在。
- 同一列不能在配置文件中重复使用。

## 配置文件格式

配置文件是 CSV，必须包含四列：

```csv
group,left_column,right_column,direction
1,A1,B1,forward
2,A2,B2,reverse
```

字段含义：

- `group`: 组编号，必须从 1 开始连续编号，例如 `1,2,3,4`。
- `left_column`: 左侧价格列名。
- `right_column`: 右侧价格列名。
- `direction`: 计算方向，只能是 `forward` 或 `reverse`。

方向含义：

- `forward`: 按 `left_column - right_column` 计算相对收益。
- `reverse`: 按 `right_column - left_column` 计算相对收益。

例如：

```csv
group,left_column,right_column,direction
1,成长指数,价值指数,forward
2,成长指数2,价值指数2,reverse
```

第 1 组表示“成长相对价值”，第 2 组表示“价值指数2相对成长指数2”。

## 组数和列顺序

组数不固定，4 组、10 组、100 组都可以。脚本会按配置文件中的组数自动计算，并对所有组等权平均。

数据 CSV 的列顺序不重要。脚本按配置文件中的列名取数，不按 CSV 中的列位置配组。

如果数据 CSV 有 100 列，但只想用其中 20 列组成 10 组，只需要在配置文件中写这 10 组即可。其余 80 列会被忽略。

示例：

```csv
group,left_column,right_column,direction
1,A1,B1,forward
2,A2,B2,forward
3,A3,B3,reverse
4,A4,B4,forward
5,A5,B5,forward
6,A6,B6,forward
7,A7,B7,reverse
8,A8,B8,reverse
9,A9,B9,forward
10,A10,B10,forward
```

## 运行命令

在项目根目录运行：

```bash
python latest_equal_weight_signal_5d_20z/generate_equal_weight_signal_contrast.py \
  --input latest_equal_weight_signal_5d_20z/data.csv \
  --config latest_equal_weight_signal_5d_20z/style_factor_groups.csv \
  --output latest_equal_weight_signal_5d_20z/equal_weight_signal_contrast.csv
```

如果脚本、配置文件和数据文件都在同一个目录，也可以进入该目录后运行：

```bash
python generate_equal_weight_signal_contrast.py \
  --input data.csv \
  --config style_factor_groups.csv \
  --output equal_weight_signal_contrast.csv
```

## 输出文件

输出 CSV 包含：

- `date`: 日期。
- `pair_01_factor_5`, `pair_02_factor_5`, ...: 每组单独的信号。
- `raw_signal_5`: 所有组的等权平均原始信号。
- `factor_value_raw`: 当天未平滑的等权信号，等于 `raw_signal_5`。
- `factor_value`: 最终连续择时信号，本版本不做平滑，等于 `factor_value_raw`。

示例：

```csv
date,pair_01_factor_5,pair_02_factor_5,raw_signal_5,factor_value_raw,factor_value
2024-01-01,0.0000,0.0000,0.0000,0.0000,0.0000
2024-01-02,0.0000,0.0000,0.0000,0.0000,0.0000
```

## 换数据步骤

1. 准备新的价格 CSV，第一列为日期。
2. 确认要使用哪些价格列组成配对。
3. 修改或新建配置 CSV，写入 `group,left_column,right_column,direction`。
4. 运行脚本并指定 `--input`、`--config`、`--output`。
5. 检查输出中的 `factor_value`。

## 常见错误

`pair config references unknown columns`

配置文件里有列名在数据 CSV 中不存在。检查列名是否完全一致，包括空格、大小写和后缀。

`pair config contains duplicated columns`

同一个价格列在配置文件中被使用了多次。当前脚本要求每列最多只能属于一组。

`pair config groups must be consecutive starting at 1`

组编号不连续。比如有 `1,2,4`，缺少 `3`。

`direction must be one of ['forward', 'reverse']`

方向字段只能写 `forward` 或 `reverse`。

## 当前默认配置

当前 `style_factor_groups.csv` 使用 15 组。第 7、8 组方向为 `reverse`，其余组为 `forward`。
