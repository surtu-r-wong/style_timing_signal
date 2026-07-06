"""绩效指标（全日历口径 —— 传入的日收益序列已含空仓日的 0，不剔零）。

对应设计稿 §3.1 问题①（年化被夸大：剔 signal==0 后仍 ×245）的修复：这里所有
年化都以完整日历序列的 mean/std 为基础。
"""
import numpy as np
import pandas as pd

ANN = 245  # 交易日/年


def ann_return(r: pd.Series) -> float:
    return float(r.mean() * ANN)


def sharpe(r: pd.Series) -> float:
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return 0.0
    return float(r.mean() / sd * np.sqrt(ANN))


def max_drawdown(r: pd.Series) -> float:
    """最大回撤（负数）。基于 (1+r) 累乘净值的峰谷。"""
    cum = (1.0 + r).cumprod()
    dd = cum / cum.cummax() - 1.0
    return float(dd.min()) if len(dd) else 0.0


def calmar(r: pd.Series) -> float:
    mdd = abs(max_drawdown(r))
    if mdd == 0:
        return float("nan")
    return ann_return(r) / mdd


def turnover(position: pd.Series) -> float:
    """年化换手：Σ|Δposition| / 天数 × 245。position 为逐日目标仓位。"""
    if len(position) == 0:
        return 0.0
    return float(position.diff().abs().sum() / len(position) * ANN)


def hit_rate(r: pd.Series) -> float:
    """非零收益日里为正的占比（全零 → nan）。"""
    nz = r[r != 0]
    if len(nz) == 0:
        return float("nan")
    return float((nz > 0).mean())
