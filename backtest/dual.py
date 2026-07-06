"""双引擎 v1 装配 + 分开评价 + CLI（Phase 3，见 2026-07-06-phase3-dual-engine-v1-plan.md）。

多头引擎 = 基信号多头段（门槛低）；空头引擎 = 基信号空头段（门槛高）经 carry 保护层门控；
执行层合成 net = long + short（多空同触发→0）。评价分开：多头 vs 满仓、空头避险价值。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.metrics import max_drawdown  # noqa: E402


def synthesize(long_leg: pd.Series, short_leg: pd.Series) -> pd.Series:
    """合成净仓位：long+short，多空同触发(long>0 & short<0)→0，clip 到 [−1,1]。"""
    both = (long_leg > 0) & (short_leg < 0)
    net = (long_leg.astype(float) + short_leg.astype(float)).clip(-1.0, 1.0)
    return net.where(~both, 0.0)


def _monthly_compound(r: pd.Series) -> pd.Series:
    """日收益 → 月复利收益（按自然月分组，版本无关）。"""
    return (1.0 + r).groupby(r.index.to_period("M")).prod() - 1.0


def hedge_value(dual_ret: pd.Series, long_only_ret: pd.Series,
                underlying: pd.Series, down_threshold: float = -0.05) -> dict:
    """空头引擎避险价值（§1.3）。

    - down_month_hit：标的月收益 < down_threshold 的月份里，dual 月收益 > long_only 月收益的占比
      （无跌月 → nan）
    - maxdd_improve：|maxdd(long_only)| − |maxdd(dual)|（正=加空头腿后回撤改善）
    """
    u_m = _monthly_compound(underlying)
    d_m = _monthly_compound(dual_ret)
    l_m = _monthly_compound(long_only_ret)

    down = u_m[u_m < down_threshold]
    if len(down) == 0:
        hit = float("nan")
    else:
        beat = d_m.reindex(down.index) > l_m.reindex(down.index)
        hit = float(beat.mean())

    maxdd_improve = abs(max_drawdown(long_only_ret)) - abs(max_drawdown(dual_ret))
    return {"down_month_hit": hit, "n_down_months": int(len(down)),
            "maxdd_improve": float(maxdd_improve)}
