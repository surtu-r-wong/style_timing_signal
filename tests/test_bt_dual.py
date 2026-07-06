import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_synthesize_adds_legs_and_zeros_double_fire():
    """net = long + short；多空同触发 → 0（§1.3 宁可空仓）。"""
    from backtest.dual import synthesize
    long_leg = pd.Series([1, 0, 1, 0])
    short_leg = pd.Series([0, -1, -1, 0])
    assert list(synthesize(long_leg, short_leg)) == [1, -1, 0, 0]


def test_synthesize_clips_to_unit_range():
    from backtest.dual import synthesize
    # 即便传入越界（如比例腿），也 clip 到 {-1..1}
    long_leg = pd.Series([1.5, 0.0])
    short_leg = pd.Series([0.0, -1.5])
    assert list(synthesize(long_leg, short_leg)) == [1.0, -1.0]


def _two_month_returns():
    """1月：标的月跌>5%（每日 −0.005×~22日）；2月：涨（每日 +0.005）。"""
    idx = pd.bdate_range("2020-01-01", "2020-02-28")
    is_jan = idx.month == 1
    underlying = pd.Series(np.where(is_jan, -0.005, 0.005), index=idx)
    long_only = underlying.copy()                       # 多头跟随标的：1月受伤
    dual = pd.Series(np.where(is_jan, 0.005, 0.005), index=idx)  # 空头腿在跌月获利
    return dual, long_only, underlying


def test_hedge_value_down_month_hit_and_maxdd_improve():
    from backtest.dual import hedge_value
    dual, long_only, underlying = _two_month_returns()
    hv = hedge_value(dual, long_only, underlying, down_threshold=-0.05)
    assert hv["n_down_months"] == 1
    assert hv["down_month_hit"] == 1.0            # 跌月里 dual 月收益 > long_only
    assert hv["maxdd_improve"] > 0                # dual 回撤显著小于 long_only


def test_hedge_value_no_down_month_gives_nan_hit():
    from backtest.dual import hedge_value
    idx = pd.bdate_range("2020-01-01", "2020-02-28")
    underlying = pd.Series(0.004, index=idx)      # 全程小涨，无跌月
    hv = hedge_value(underlying, underlying, underlying, down_threshold=-0.05)
    assert hv["n_down_months"] == 0
    assert np.isnan(hv["down_month_hit"])
