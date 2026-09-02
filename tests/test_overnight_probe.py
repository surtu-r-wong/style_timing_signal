import numpy as np
import pandas as pd
import pytest

from backtest.overnight_probe import (
    INCUMBENT, RENAME, STYLE_NAMES, build_candidates, component_factor, decompose_log_returns, synthetic_price,
)
from signals.citic40d.generate_signal import compute_mean_factor


def _toy(n=400, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    close = pd.DataFrame(np.exp(np.cumsum(rng.normal(0, 0.01, (n, 5)), axis=0)) * 100, index=idx, columns=STYLE_NAMES)
    gap = rng.normal(0, 0.005, (n, 5))
    open_ = close.shift(1).fillna(close.iloc[0]) * np.exp(gap)
    return open_, close


def test_decomposition_sums_to_close_to_close():
    o, c = _toy()
    on, intra = decompose_log_returns(o, c)
    full = np.log(c / c.shift(1)).iloc[1:]
    assert np.allclose((on + intra).iloc[1:], full, atol=1e-12)
    assert (on.iloc[0] == 0).all() and (intra.iloc[0] == 0).all()


def test_synthetic_close_path_reproduces_incumbent_factor():
    """合成价格路径（隔夜+日内之和）走同一因子代码 == 直接用 close 的现任因子。"""
    o, c = _toy()
    on, intra = decompose_log_returns(o, c)
    synth = synthetic_price(on + intra).rename(columns=RENAME)
    a = compute_mean_factor(synth, n=20, z_window=40)
    b = component_factor(o, c, "full", 20, 40)
    common = a.dropna().index.intersection(b.dropna().index)
    assert len(common) > 300
    assert np.allclose(a.reindex(common), b.reindex(common), atol=1e-9)


def test_flat_bar_day_overnight_absorbs_full_return():
    o, c = _toy()
    d = o.index[50]
    o.loc[d] = c.loc[d]  # 平 K 线：open == close
    on, intra = decompose_log_returns(o, c)
    assert (intra.loc[d] == 0).all()
    assert np.allclose(on.loc[d], np.log(c.loc[d] / c.iloc[49]), atol=1e-12)


def test_fused_is_equal_weight_mean_and_candidates_shape():
    o, c = _toy()
    a = component_factor(o, c, "overnight", 20, 40)
    b = component_factor(o, c, "intraday", 20, 40)
    f = component_factor(o, c, "fused", 20, 40)
    idx = a.dropna().index.intersection(b.dropna().index)
    assert np.allclose(f.reindex(idx), (a.reindex(idx) + b.reindex(idx)) / 2, atol=1e-12)
    cands = build_candidates(o, c)
    assert set(cands) == {INCUMBENT, "overnight_lb20_zw40", "overnight_lb20_zw120", "intraday_lb20_zw40",
                          "intraday_lb20_zw120", "fused_lb20_zw40", "fused_lb20_zw120"}
    # 隔夜与日内分量不同 → 因子不应恒等于现任
    assert not np.allclose(a.reindex(idx), cands[INCUMBENT].reindex(idx), atol=1e-6)
