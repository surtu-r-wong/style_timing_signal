"""显著性检验：同换手随机信号 bootstrap（设计稿 §3.2 Step 1）。

零假设 = "同样的仓位分布与换手，但与收益无预测关系"。用**循环平移**position
生成随机对照：np.roll 保持仓位边际分布与换手（转换次数）几乎不变，只错开与收益的
时间对齐 → 破坏预测力。carry 不随信号平移（它是市场属性，按日期保留）。

p = (#{随机 metric ≥ 实际} + 1) / (n + 1)；须 < 0.05 才算信号显著优于随机。
"""
import numpy as np
import pandas as pd

from backtest.engine import run_strategy
from backtest.metrics import ann_return, calmar, sharpe

_METRICS = {"sharpe": sharpe, "ann_return": ann_return, "calmar": calmar}


def strategy_metric(position, underlying, metric="sharpe", carry=None, cost_bps=3.0) -> float:
    ret = run_strategy(position, underlying, cost_bps=cost_bps, carry=carry)["ret"]
    return _METRICS[metric](ret)


def bootstrap_pvalue(position: pd.Series, underlying: pd.Series, metric="sharpe",
                     n=1000, seed=0, carry=None, cost_bps=3.0) -> float:
    rng = np.random.default_rng(seed)
    actual = strategy_metric(position, underlying, metric, carry, cost_bps)
    vals = position.to_numpy()
    length = len(vals)
    count = 0
    for _ in range(n):
        rolled = pd.Series(np.roll(vals, int(rng.integers(1, length))), index=position.index)
        if strategy_metric(rolled, underlying, metric, carry, cost_bps) >= actual:
            count += 1
    return (count + 1) / (n + 1)
