"""阈值 θ 选优校正检验的口径判例（纯离线，不连库）。

钉死三件事：
1. **冻结常量**——`min_shift=60`、闸门窗不含 2024-2026、`cost_bps=3.0`、θ 网格形状；
2. **配对性**——同一置换下 `stat_fn((u, 0.0), idx)` 必须**恒等于 0**
   （统计量是"相对 θ=0 的增益"，基线自己减自己）；
3. **阈值与置换可交换**——模块 docstring 声称"先阈值后旋转 = 先旋转后阈值"，
   这是"置换作用在仓位序列上"这种写法的合法性前提，必须有判例守。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.positions import production_position  # noqa: E402
from backtest.threshold_selection_formal import (  # noqa: E402
    COST_BPS, MIN_SHIFT, STAT_WINDOWS, make_stat_fn,
)
from backtest.threshold_by_underlying_probe import THETAS  # noqa: E402


# ================================================================ 1. 冻结常量
def test_frozen_gate_constants():
    """与 ⑧ 正式跑同口径；闸门窗只读 train/val，2024-26 一律不入。"""
    assert MIN_SHIFT == 60
    assert STAT_WINDOWS == ("2014-2020", "2021-2023")
    assert "2024-2026" not in STAT_WINDOWS
    assert COST_BPS == 3.0


def test_theta_grid_shape_and_buyhold_reference():
    """θ 网格 = [−0.7, +0.7] 步长 0.05 共 29 点，另有 −1.0 一行作买入持有参照。"""
    grid = [t for t in THETAS if t > -1.0]
    assert len(grid) == 29
    assert min(grid) == pytest.approx(-0.70) and max(grid) == pytest.approx(0.70)
    assert -1.0 in THETAS                      # 参照行在，但不进选优
    assert 0.0 in grid                         # 现役点必须在网格里


# ================================================================ 2. 配对性
class _StubData:
    """`make_stat_fn` 只用到 `pos` 与 `worst_tv`，其余不必造。"""

    def __init__(self, n=200, seed=0):
        rng = np.random.default_rng(seed)
        self.n = n
        self.pos = {t: (rng.normal(size=n) > t).astype(float) for t in (-0.2, 0.0, 0.35)}
        self._w = rng.normal(size=n)

    def worst_tv(self, pos_arr, uname):
        # 任意确定性泛函即可——判例只查"配对相减"的结构，不查数值本身
        return float(np.dot(pos_arr, self._w) / self.n + len(uname) * 0.01)


def test_statistic_is_paired_and_zero_at_the_incumbent_theta():
    """θ=0 的统计量在**任何**置换下都必须恰好是 0（基线自己减自己）。"""
    d = _StubData()
    fn = make_stat_fn(d)
    rng = np.random.default_rng(1)
    for _ in range(5):
        idx = rng.permutation(d.n)
        assert fn(("X", 0.0), idx) == 0.0
    assert fn(("X", 0.0), np.arange(d.n)) == 0.0


def test_statistic_cancels_the_underlying_specific_offset():
    """配对形式必须把"标的自身"的贡献消掉：同一 θ、同一 idx，换标的名不改变统计量
    中来自基线的那一部分（stub 里标的贡献是一个常数偏移，配对后应消失）。"""
    d = _StubData()
    fn = make_stat_fn(d)
    idx = np.arange(d.n)
    a = fn(("AAAA", 0.35), idx)
    b = fn(("BB", 0.35), idx)          # 名字长度不同 → 基线偏移不同
    assert a == pytest.approx(b, abs=1e-12)


# ================================================================ 3. 可交换性
def test_thresholding_commutes_with_permutation():
    """先阈值后旋转 == 先旋转后阈值 —— "置换作用在仓位序列上"的合法性前提。"""
    rng = np.random.default_rng(7)
    n = 300
    idx = pd.bdate_range("2014-01-02", periods=n)
    sig = pd.Series(rng.normal(size=n), index=idx)
    perm = np.roll(np.arange(n), 61)

    for th in (-0.2, 0.0, 0.35):
        先阈值后旋转 = production_position(sig, threshold=th).to_numpy()[perm]
        后者信号 = pd.Series(sig.to_numpy()[perm], index=idx)
        先旋转后阈值 = production_position(后者信号, threshold=th).to_numpy()
        assert np.array_equal(先阈值后旋转, 先旋转后阈值)


# ================================================================ 4. 跨文档可比性
def test_longflat_full_segment_equals_long_segment():
    """对 0/1 的 long-flat 仓位，`run_strategy(...)['ret']` 与
    `evaluate(...)['long']` 必须逐位相等。

    探针 `threshold_by_underlying_probe` 报的是多头段，正式检验
    `threshold_selection_formal` 用的是整段 —— 两份文档要能互相引用，
    这条等式就必须成立。若 `segment_returns` 的成本归属被改动，本判例先炸。
    """
    from backtest.baseline import evaluate
    from backtest.engine import run_strategy
    from backtest.metrics import sharpe

    rng = np.random.default_rng(3)
    n = 400
    idx = pd.bdate_range("2014-01-02", periods=n)
    sig = pd.Series(rng.normal(size=n), index=idx)
    und = pd.Series(rng.normal(scale=0.012, size=n), index=idx)
    for th in (-0.2, 0.0, 0.35):
        pos = production_position(sig, threshold=th).astype(float)
        assert set(pos.unique()) <= {0.0, 1.0}
        a = float(sharpe(run_strategy(pos, und, 3.0, None)["ret"]))
        b = float(evaluate(pos, und, None, 3.0, 0)["long"]["sharpe"])
        assert a == pytest.approx(b, abs=1e-12)

