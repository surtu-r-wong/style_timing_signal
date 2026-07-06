"""信号 → 目标仓位映射。

默认 discrete：|signal| > threshold 取符号得 {−1, 0, +1}（对齐双引擎 §1.3 离散仓位）；
threshold=0 即取符号（恰为 0 → 0）。proportional：原值直通（敏感性对照）。

hybrid20 的 hybrid_20 本就是 {−1,0,+1}，discrete(θ=0) 对它是恒等；citic40d/equal_weight
的连续因子经此离散化。
"""
import numpy as np
import pandas as pd


def to_position(signal: pd.Series, mode: str = "discrete", threshold: float = 0.0) -> pd.Series:
    if mode == "proportional":
        return signal.astype(float)
    if mode == "discrete":
        gated = signal.where(signal.abs() > threshold, 0.0)
        return pd.Series(np.sign(gated), index=signal.index).astype(int)
    raise ValueError(f"unknown mode: {mode!r} (expected 'discrete' or 'proportional')")


def to_position_asym(signal: pd.Series, long_theta: float, short_theta: float) -> pd.Series:
    """非对称离散映射（双引擎 §1.3）：signal>long_theta→+1；signal<−short_theta→−1；否则 0。

    long_theta / short_theta 均为非负绝对阈值。short_theta>long_theta 即"空头门槛更高"。
    """
    if long_theta < 0 or short_theta < 0:
        raise ValueError("long_theta / short_theta must be non-negative")
    pos = pd.Series(0, index=signal.index, dtype=int)
    pos[signal > long_theta] = 1
    pos[signal < -short_theta] = -1
    return pos


def production_position(signal: pd.Series, threshold: float = 0.0) -> pd.Series:
    """推荐 production 持仓口径 = **long-flat**：signal>threshold→+1，否则 0（砍空头）。

    Phase 3 双引擎 v1 实证：复用风格信号的空头段【无独立盈利 + 无避险价值】——
    equal_weight long-flat Sharpe 1.42 vs 对称 1.39、MaxDD −13.9% vs −30.2%；
    CITIC 轴 T6 阈值扫描 short_sharpe≈0（全 16 组 −0.07~+0.04）独立佐证。
    → 交易这些信号时砍掉空头优于对称多空。连续因子与已离散带空信号皆适用。
    """
    return (signal > threshold).astype(int)
