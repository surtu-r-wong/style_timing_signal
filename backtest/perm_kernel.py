"""置换检验用的**批量 Sharpe 快核**——`run_strategy` + `metrics.sharpe` 的向量化等价物。

## 为什么要有它

⓪ 机器的 `batch_stat_fn(variant, index_matrix)` 允许一次算完某个变体在**全部 m 次
置换**下的统计量。而 `run_strategy(carry=None)` 展开就是四行数组运算：

    pos_eff = shift(pos, 1)，首位 0
    gross   = pos_eff * und
    trade   = |diff(pos_eff)|，首位 = |pos_eff[0]| = 0
    ret     = gross − cost_bps/1e4 · trade

逐次调用 pandas 版本，1160 变体 × 1000 置换 ≈ 3.5 小时；按 (m, n) 矩阵一次算完，
同样的活是**分钟级**。差别全在 pandas 的逐次调用开销，不在算法。

## 纪律

这是本项目少有的**有意的平行实现**，因此必须被判例钉死：
`tests/test_bt_perm_kernel.py` 对随机输入逐点比对本模块与
`engine.run_strategy` + `metrics.sharpe`，容差 **1e-12**（浮点求和次序不同，
不强求 bitwise）。**任何对 `run_strategy` 的改动都会让那些判例先炸** —— 这正是要的。

carry 一律不支持：跨标的可比口径本来就规定零 carry（见 `underlying_probe` §口径）。
需要 carry 的场景请走 pandas 原路。
"""
from __future__ import annotations

import numpy as np

from backtest.metrics import ANN


def pos_eff_matrix(pos: np.ndarray) -> np.ndarray:
    """(m, n) 或 (n,) 仓位 → T+1 生效仓位（首列置 0），与 `run_strategy` 同义。"""
    pos = np.atleast_2d(np.asarray(pos, dtype=float))
    out = np.empty_like(pos)
    out[:, 0] = 0.0
    out[:, 1:] = pos[:, :-1]
    return out


def strategy_returns(pos: np.ndarray, und: np.ndarray,
                     cost_bps: float = 3.0) -> np.ndarray:
    """(m, n) 策略日收益 —— `run_strategy(pos, und, cost_bps, None)["ret"]` 的批量版。"""
    pe = pos_eff_matrix(pos)
    und = np.asarray(und, dtype=float)
    gross = pe * und                      # (m, n) × (n,) 广播
    trade = np.empty_like(pe)
    trade[:, 0] = 0.0                     # = |pos_eff[:,0]| = 0
    trade[:, 1:] = np.abs(np.diff(pe, axis=1))
    return gross - cost_bps / 1e4 * trade


def sharpe_rows(ret: np.ndarray) -> np.ndarray:
    """(m,) 逐行 Sharpe —— `metrics.sharpe` 的批量版（同为 ddof=1，同为 ×√ANN）。

    `sd` 非有限或为 0 → 0.0（与 `metrics.sharpe` 的短路分支逐字一致）。
    """
    ret = np.atleast_2d(np.asarray(ret, dtype=float))
    sd = ret.std(axis=1, ddof=1)
    mean = ret.mean(axis=1)
    out = np.zeros(ret.shape[0], dtype=float)
    ok = np.isfinite(sd) & (sd != 0)
    out[ok] = mean[ok] / sd[ok] * np.sqrt(ANN)
    return out


def sharpe_batch(pos: np.ndarray, und: np.ndarray,
                 cost_bps: float = 3.0) -> np.ndarray:
    """(m,) 逐行 Sharpe，等价于逐行 `sharpe(run_strategy(pos_i, und, cost_bps)["ret"])`。"""
    return sharpe_rows(strategy_returns(pos, und, cost_bps))


def worst_tv_batch(pos: np.ndarray, und: np.ndarray, masks: list[np.ndarray],
                   cost_bps: float = 3.0) -> np.ndarray:
    """(m,) `worst(窗口们)` 的 Sharpe —— 各窗**先切片再跑引擎**（与逐次口径一致）。

    ⚠️ 切片必须发生在算 `pos_eff` 之前：窗口内第一天的生效仓位是 0（新起一段），
    这与 `threshold_by_underlying_probe` / `param_stability_probe` 的做法逐字相同。
    """
    pos = np.atleast_2d(np.asarray(pos, dtype=float))
    und = np.asarray(und, dtype=float)
    vals = [sharpe_batch(pos[:, m], und[m], cost_bps) for m in masks]
    return np.min(np.vstack(vals), axis=0)
