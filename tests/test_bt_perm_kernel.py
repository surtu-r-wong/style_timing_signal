"""快核与 pandas 原路的等价性判例（这是 `perm_kernel` 存在的唯一许可证）。

`backtest/perm_kernel.py` 是有意的平行实现（为把置换检验从小时级压到分钟级）。
本文件逐点比对它与 `engine.run_strategy` + `metrics.sharpe`，容差 1e-12。
**改动 `run_strategy` 的语义（T+1 生效、首日建仓成本、成本口径）会让这里先炸。**
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.perm_kernel import (  # noqa: E402
    pos_eff_matrix, sharpe_batch, strategy_returns, worst_tv_batch,
)


def _case(n=500, seed=0, longflat=True):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2014-01-02", periods=n)
    sig = rng.normal(size=n)
    pos = (sig > 0).astype(float) if longflat else np.sign(sig)
    und = rng.normal(scale=0.012, size=n)
    return idx, pos, und


def test_pos_eff_matches_engine():
    """T+1 生效仓位（首位 0）—— 与 `run_strategy` 的 `pos_eff` 列逐位相等。"""
    idx, pos, und = _case()
    got = pos_eff_matrix(pos)[0]
    want = run_strategy(pd.Series(pos, index=idx), pd.Series(und, index=idx),
                        3.0, None)["pos_eff"].to_numpy()
    assert np.array_equal(got, want)


@pytest.mark.parametrize("cost_bps", [0.0, 3.0, 8.0])
@pytest.mark.parametrize("longflat", [True, False])
def test_returns_and_sharpe_match_pandas_path(cost_bps, longflat):
    """日收益序列与 Sharpe 都必须与 pandas 原路一致（含对称 ±1 仓位）。"""
    idx, pos, und = _case(seed=3, longflat=longflat)
    ps, us = pd.Series(pos, index=idx), pd.Series(und, index=idx)

    want_ret = run_strategy(ps, us, cost_bps, None)["ret"].to_numpy()
    got_ret = strategy_returns(pos, und, cost_bps)[0]
    assert np.allclose(got_ret, want_ret, atol=1e-15, rtol=0)

    assert sharpe_batch(pos, und, cost_bps)[0] == pytest.approx(
        float(sharpe(run_strategy(ps, us, cost_bps, None)["ret"])), abs=1e-12)


def test_batch_rows_are_independent_and_match_one_by_one():
    """(m, n) 批量的每一行，必须等于把该行单独喂 pandas 原路的结果。"""
    rng = np.random.default_rng(11)
    n, m = 400, 7
    idx = pd.bdate_range("2014-01-02", periods=n)
    und = rng.normal(scale=0.01, size=n)
    posm = (rng.normal(size=(m, n)) > 0).astype(float)

    got = sharpe_batch(posm, und, 3.0)
    for i in range(m):
        want = float(sharpe(run_strategy(pd.Series(posm[i], index=idx),
                                         pd.Series(und, index=idx), 3.0, None)["ret"]))
        assert got[i] == pytest.approx(want, abs=1e-12)


def test_worst_tv_slices_before_running_the_engine():
    """窗口切片必须发生在算 pos_eff 之前（窗内首日生效仓位 = 0）。"""
    rng = np.random.default_rng(5)
    n = 600
    idx = pd.bdate_range("2014-01-02", periods=n)
    und = rng.normal(scale=0.011, size=n)
    pos = (rng.normal(size=n) > 0).astype(float)
    masks = [np.arange(n) < 300, np.arange(n) >= 300]

    got = worst_tv_batch(pos, und, masks, 3.0)[0]
    want = min(
        float(sharpe(run_strategy(pd.Series(pos, index=idx)[m],
                                  pd.Series(und, index=idx)[m], 3.0, None)["ret"]))
        for m in masks)
    assert got == pytest.approx(want, abs=1e-12)


def test_zero_variance_returns_zero_sharpe_like_metrics():
    """恒定收益（sd=0）→ 0.0，与 `metrics.sharpe` 的短路分支一致。"""
    n = 100
    pos = np.zeros(n)                       # 永不持仓 → ret 恒为 0
    und = np.full(n, 0.01)
    assert sharpe_batch(pos, und, 3.0)[0] == 0.0
