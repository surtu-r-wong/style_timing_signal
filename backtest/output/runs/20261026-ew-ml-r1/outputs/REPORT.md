# EW机器学习首轮结果

固定规格，复用历史上的逐年滚动预测。不是未触碰确认样本，不发统计GO。

| 策略 | CAGR | Sharpe | 最大回撤 | 平均目标暴露 | 年换手 |
|---|---:|---:|---:|---:|---:|
| ew | 35.51% | 1.385 | -34.45% | 0.999 | 43.29 |
| fixed75 | 26.28% | 1.385 | -26.87% | 0.750 | 32.40 |
| half_short | 32.52% | 1.600 | -22.89% | 0.760 | 31.88 |
| vol20 | 33.00% | 1.489 | -28.02% | 0.903 | 40.97 |
| ridge_core | 29.59% | 1.430 | -27.15% | 0.813 | 36.33 |
| tree_core | 29.79% | 1.441 | -26.55% | 0.815 | 36.63 |
| ridge_oi | 29.33% | 1.428 | -27.01% | 0.809 | 36.25 |
| tree_oi | 29.57% | 1.436 | -26.56% | 0.812 | 36.55 |

经济筛选：
- ridge_core: 未通过：sharpe_gain_at_least_010, beats_simple_and_past_controls, cagr_loss_at_most_2pp
- tree_core: 未通过：sharpe_gain_at_least_010, beats_simple_and_past_controls, cagr_loss_at_most_2pp
- ridge_oi: 未通过：sharpe_gain_at_least_010, beats_simple_and_past_controls, cagr_loss_at_most_2pp
- tree_oi: 未通过：sharpe_gain_at_least_010, beats_simple_and_past_controls, cagr_loss_at_most_2pp, both_subperiods_positive

完整逐年、分段、成本、延迟、历史仓位对照和最好五日压力分别见metrics/comparisons/stress.csv。
leg_attribution为逐日收益算术归因，多空毛收益减费用相加等于日收益之和，不是可相加的CAGR。
OI固定额外滞后一天；历史发布时间及修订未独立证明。实际合约回测为小数名义仓位，
未建模保证金、整数手、涨跌停成交和市场冲击，不能当实际账户收益。
未修改生产、共享库、自动调度或既有前瞻协议。
