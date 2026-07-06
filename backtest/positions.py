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
