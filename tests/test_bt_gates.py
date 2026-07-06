import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_carry_protection_suppresses_short_in_deep_backwardation():
    """深贴水（carry≥θ, 正=贴水）禁空；多头与浅贴水空头不动。"""
    from backtest.gates import carry_protection
    pos = pd.Series([-1, -1, 1, 0, -1])
    carry = pd.Series([0.10, 0.03, 0.10, 0.10, 0.05])
    out = carry_protection(pos, carry, theta=0.06)
    assert list(out) == [0, -1, 1, 0, -1]


def test_carry_protection_missing_carry_keeps_short():
    """缺 carry（NaN / 日期缺）视为无贴水信息 → 不禁空（与引擎 carry 缺=0 一致）。"""
    from backtest.gates import carry_protection
    idx = pd.RangeIndex(3)
    pos = pd.Series([-1, -1, -1], index=idx)
    carry = pd.Series([np.nan, 0.10], index=[0, 1])  # idx 2 缺失
    out = carry_protection(pos, carry, theta=0.06)
    assert list(out) == [-1, 0, -1]


def test_carry_protection_preserves_index():
    from backtest.gates import carry_protection
    idx = pd.bdate_range("2020-01-01", periods=3)
    pos = pd.Series([-1, 1, -1], index=idx)
    carry = pd.Series([0.2, 0.2, 0.0], index=idx)
    out = carry_protection(pos, carry, theta=0.06)
    assert list(out.index) == list(idx)
    assert list(out) == [0, 1, -1]
