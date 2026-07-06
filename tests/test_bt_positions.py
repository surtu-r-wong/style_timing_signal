import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.positions import to_position  # noqa: E402


def test_asym_higher_short_bar():
    """空头门槛更高：小正值即做多，中等负值不做空（需更极端）。"""
    from backtest.positions import to_position_asym
    s = pd.Series([0.05, 0.15, 0.35, -0.05, -0.20, -0.35, 0.30, -0.30])
    # long_theta=0.10（严格>）, short_theta=0.30（严格<-0.30）
    assert list(to_position_asym(s, long_theta=0.10, short_theta=0.30)) == \
        [0, 1, 1, 0, 0, -1, 1, 0]


def test_asym_symmetric_reduces_to_sign_thresholds():
    from backtest.positions import to_position_asym
    s = pd.Series([-0.5, 0.1, 0.3, -0.1])
    assert list(to_position_asym(s, long_theta=0.2, short_theta=0.2)) == [-1, 0, 1, 0]


def test_asym_preserves_index():
    from backtest.positions import to_position_asym
    idx = pd.bdate_range("2020-01-01", periods=3)
    out = to_position_asym(pd.Series([0.5, -0.5, 0.0], index=idx), long_theta=0.1, short_theta=0.3)
    assert list(out.index) == list(idx)


def test_asym_negative_theta_raises():
    import pytest
    from backtest.positions import to_position_asym
    with pytest.raises(ValueError, match="theta"):
        to_position_asym(pd.Series([0.1]), long_theta=-0.1, short_theta=0.3)


def test_production_position_long_flat_drops_shorts():
    """推荐 production 口径 = long-flat：signal>0→+1，空头/平仓段一律 0。"""
    from backtest.positions import production_position
    s = pd.Series([-0.5, 0.0, 0.3, -0.1, 0.05])
    assert list(production_position(s)) == [0, 0, 1, 0, 1]


def test_production_position_threshold():
    from backtest.positions import production_position
    s = pd.Series([0.05, 0.2, -0.5])
    assert list(production_position(s, threshold=0.1)) == [0, 1, 0]


def test_production_position_clips_discrete_short_signal():
    """对已离散的带空信号（hybrid_20 ∈ {−1,0,+1}）等价于砍掉 −1。"""
    from backtest.positions import production_position
    s = pd.Series([-1, 0, 1, -1, 1])
    assert list(production_position(s)) == [0, 0, 1, 0, 1]


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
