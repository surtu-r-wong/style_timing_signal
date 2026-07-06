import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.positions import to_position  # noqa: E402


def test_discrete_sign_default():
    s = pd.Series([-0.5, 0.0, 0.3])
    assert list(to_position(s)) == [-1, 0, 1]


def test_discrete_deadband_zeros_inside_band():
    s = pd.Series([-0.5, 0.1, 0.3])
    assert list(to_position(s, threshold=0.2)) == [-1, 0, 1]


def test_proportional_passthrough():
    s = pd.Series([-0.5, 0.0, 0.3])
    assert list(to_position(s, mode="proportional")) == [-0.5, 0.0, 0.3]


def test_already_discrete_untouched():
    s = pd.Series([-1, 0, 1])
    assert list(to_position(s)) == [-1, 0, 1]


def test_preserves_index():
    idx = pd.bdate_range("2020-01-01", periods=3)
    assert list(to_position(pd.Series([-0.5, 0.0, 0.3], index=idx)).index) == list(idx)


def test_unknown_mode_raises():
    import pytest
    with pytest.raises(ValueError, match="mode"):
        to_position(pd.Series([0.1]), mode="bogus")
