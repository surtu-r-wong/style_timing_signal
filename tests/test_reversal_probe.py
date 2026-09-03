import numpy as np
import pandas as pd

from backtest.reversal_probe import (
    GRID_A, GRID_B1, GRID_B2, b1_position, b2_position, reversal_factor, short_only_metrics, short_trigger_hold, window_sharpes,
)


def test_grids_match_preregistration():
    assert len(GRID_A) == 60 and len(GRID_B1) == 18 and len(GRID_B2) == 9
    assert all(g["skip"] == 0 for g in GRID_A)


def test_reversal_factor_is_negated_momentum():
    idx = pd.bdate_range("2020-01-01", periods=10)
    fn = lambda **kw: pd.Series(np.arange(10, dtype=float), index=idx)
    assert (reversal_factor(fn, family="classic") == -np.arange(10)).all()


def test_short_trigger_hold_holds_k_days_and_extends():
    idx = pd.bdate_range("2020-01-01", periods=12)
    trig = pd.Series([False] * 12, index=idx); trig.iloc[2] = True; trig.iloc[4] = True
    pos = short_trigger_hold(trig, 3)
    assert pos.tolist() == [0, 0, -1, -1, -1, -1, -1, 0, 0, 0, 0, 0]   # 第 2 日触发持 3 日，第 4 日再触发顺延到第 6 日


def test_b1_and_b2_positions_are_short_only_and_sensible():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2014-01-01", periods=600)
    ret = pd.Series(rng.normal(0.0003, 0.012, 600), index=idx)
    p1 = b1_position(ret, 10, 60, 5); p2 = b2_position(ret, 120, 10)
    assert set(p1.unique()) <= {0, -1} and set(p2.unique()) <= {0, -1}
    assert 0 < (p1 < 0).mean() < 0.6
    # B2：价格持续下跌的序列应当大部分时间在空头
    down = pd.Series(-0.002, index=idx)
    assert (b2_position(down, 60, 5) < 0).iloc[100:].mean() > 0.95


def test_short_only_metrics_pays_carry():
    idx = pd.bdate_range("2014-01-01", periods=500)
    und = pd.Series(0.0, index=idx); carry = pd.Series(0.10, index=idx)     # 标的不动、年化贴水 10%
    pos = pd.Series(-1, index=idx)
    m = short_only_metrics(pos, und, carry)
    assert m["ann_full"] < 0 and m["short_share"] == 1.0                   # 纯贴水成本 → 空头亏
    ws = window_sharpes(pos, und, carry, {"w": ("2014-01-01", "2015-12-31")})
    assert ws["w"] < 0
