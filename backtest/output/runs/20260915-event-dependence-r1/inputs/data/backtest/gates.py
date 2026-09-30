"""环境/保护层门控（双引擎 §1.3 空头引擎的保护层 + 环境层归口）。

v1 只实现 carry 保护层（深贴水禁空）；Phase 4 在此扩两融/题材集中度/微盘拥挤等环境门。
门控只作用于空头腿（position<0），多头腿不动。
"""
import pandas as pd


def carry_protection(position: pd.Series, carry: pd.Series, theta: float) -> pd.Series:
    """深贴水禁空：position<0 且 carry≥theta（正=贴水）的日期 → 0。

    carry 按日期对齐到 position.index；缺失/NaN 视为无贴水信息（不禁空，与引擎 carry 缺=0 一致）。
    """
    aligned = carry.reindex(position.index)
    suppress = (position < 0) & (aligned >= theta)  # NaN 比较为 False → 不禁空
    return position.where(~suppress, 0)
