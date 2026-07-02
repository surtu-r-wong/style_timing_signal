# hybrid_20 信号说明

## 结论

`hybrid_20` 是当前更适合使用的 20 日风格择时信号。

它的核心逻辑是：

> 成长/稳健 20 日主信号决定方向；金融/稳定 20 日信号只用于阻止做空，不阻止做多。

换句话说：

- 主信号看多时，直接做多。
- 主信号看空时，只有金融信号没有明确看多，才允许做空。
- 主信号中性时，保持中性。

## 数据来源

相关文件（路径相对仓库根）：

- `data/中信风格合并.csv`：风格指数原始数据，包含稳定、成长、金融、周期、消费。
- `output/hybrid20/growth_stability_signal.csv`：成长/稳健原始信号输出。
- `output/hybrid20/confirmed_signal.csv`：在原始信号基础上加入金融确认后的输出。
- `signals/hybrid20/update_growth_stability.py`：生成成长/稳健原始信号。
- `signals/hybrid20/update_confirmed_signal.py`：生成 `confirmed_20` 和 `hybrid_20`。
- `signals/hybrid20/optimize_signal.py`：五因子阈值优化研究脚本（另需 `data/沪深300.csv`，输出 `output/hybrid20/optimized_signal.csv`）。

`hybrid_20` 位于：

```text
output/hybrid20/confirmed_signal.csv -> hybrid_20
```

## 第一层：成长/稳健主信号 signal_20

主信号来自成长指数相对稳健指数的 20 日强弱。

计算逻辑：

```text
spread_20 = ln(成长(t) / 成长(t-20)) - ln(稳健(t) / 稳健(t-20))
z = (spread_20 - rolling_mean_250) / rolling_std_250
factor_20 = tanh(z)
```

然后用状态机把连续因子 `factor_20` 转成离散信号 `signal_20`。

状态机阈值：

```text
开多: factor_20 > 0.35
平多: factor_20 < 0.10
开空: factor_20 < -0.15
平空: factor_20 > -0.10
```

信号含义：

```text
signal_20 =  1  做多
signal_20 =  0  中性/空仓
signal_20 = -1  做空
```

注意：这是状态机逻辑，有持仓延续，不是每天独立按阈值重新判断。

## 第二层：金融/稳定确认信号 fin_signal_20

金融确认信号来自金融指数相对稳健指数的 20 日强弱。

计算逻辑：

```text
fin_spread_20 = ln(金融(t) / 金融(t-20)) - ln(稳健(t) / 稳健(t-20))
z = (fin_spread_20 - rolling_mean_250) / rolling_std_250
fin_factor_20 = tanh(z)
```

`fin_factor_20` 使用和主信号相同的状态机阈值：

```text
开多: fin_factor_20 > 0.35
平多: fin_factor_20 < 0.10
开空: fin_factor_20 < -0.15
平空: fin_factor_20 > -0.10
```

信号含义：

```text
fin_signal_20 =  1  金融相对稳健偏强
fin_signal_20 =  0  金融确认中性
fin_signal_20 = -1  金融相对稳健偏弱
```

## hybrid_20 的最终规则

`hybrid_20` 以 `signal_20` 为主，只在做空时参考 `fin_signal_20`。

规则如下：

```text
如果 signal_20 = 1:
    hybrid_20 = 1

如果 signal_20 = -1 且 fin_signal_20 != 1:
    hybrid_20 = -1

如果 signal_20 = -1 且 fin_signal_20 = 1:
    hybrid_20 = 0

如果 signal_20 = 0:
    hybrid_20 = 0
```

可以简化理解为：

```text
多头：完全相信成长/稳健主信号。
空头：需要金融/稳定信号不反对。
```

## 和 confirmed_20 的区别

`confirmed_20` 是双向确认。

```text
signal_20 = 1 且 fin_signal_20 = -1 -> confirmed_20 = 0
signal_20 = -1 且 fin_signal_20 = 1 -> confirmed_20 = 0
其他情况保持 signal_20
```

也就是说，`confirmed_20` 会同时过滤多头和空头。

`hybrid_20` 只过滤空头：

```text
signal_20 = 1 -> hybrid_20 = 1
signal_20 = -1 且 fin_signal_20 = 1 -> hybrid_20 = 0
signal_20 = -1 且 fin_signal_20 != 1 -> hybrid_20 = -1
```

因此：

- `confirmed_20` 更保守。
- `hybrid_20` 更尊重成长/稳健主信号。
- 在前面对照中，`hybrid_20` 与 `20260401/backtest_report01.xlsx` 的交易方向基本一致。

## 当前使用建议

如果用于股指期货多空择时：

```text
hybrid_20 =  1 -> 做多
hybrid_20 =  0 -> 空仓/中性
hybrid_20 = -1 -> 做空
```

如果用于只能做多的小市值或权益类资产：

```text
hybrid_20 = 1  -> 持有
hybrid_20 = 0  -> 空仓
hybrid_20 = -1 -> 空仓
```

在只做多口径下，`hybrid_20` 和 `signal_20` 的多头部分一致。

## 运行顺序

更新信号时建议按以下顺序（在仓库根执行）：

```bash
python3 signals/hybrid20/update_growth_stability.py
python3 signals/hybrid20/update_confirmed_signal.py
```

第一步生成：

```text
output/hybrid20/growth_stability_signal.csv
```

第二步生成：

```text
output/hybrid20/confirmed_signal.csv
```

最终使用：

```text
output/hybrid20/confirmed_signal.csv 中的 hybrid_20
```

## 代码位置

核心逻辑在 `signals/hybrid20/update_confirmed_signal.py`：

```python
hybrid = pd.Series(0, index=main_sig.index, dtype=int)
hybrid[main_sig == 1] = 1
hybrid[(main_sig == -1) & (conf_sig != 1)] = -1
out[f"hybrid_{n}"] = hybrid
```

其中 `n = 20` 时，对应输出列就是 `hybrid_20`。
