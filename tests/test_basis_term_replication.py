import numpy as np
import pandas as pd

from backtest.basis_term_replication import DIRECTION, one_sided_shift_pvalue, verdict


def test_verdict_rules_literal():
    # PASS：P1 过、P2 同号、未回填（P2 显著性不要求）
    assert verdict(0.01, 0.10, 0.40, False)["PASS"] is True
    # P1 不过 → STOP
    assert verdict(0.06, 0.10, 0.01, False)["PASS"] is False
    # P2 反号 → STOP（即便 P1 过）
    assert verdict(0.01, -0.05, 0.01, False)["PASS"] is False
    # 回填后 P2 须显著：p2 0.40 → STOP；p2 0.02 → PASS
    assert verdict(0.01, 0.10, 0.40, True)["PASS"] is False
    assert verdict(0.01, 0.10, 0.02, True)["PASS"] is True
    # α 边界：p 恰等于 0.05 不过
    assert verdict(0.05, 0.10, None, False)["P1_pass"] is False
    assert DIRECTION == +1


def test_one_sided_p_is_directional():
    """信号 = 未来 40 日收益和（完美正向预测）：方向 +1 的单侧 p 应极小，方向 −1 的单侧 p 应接近 1。"""
    idx = pd.bdate_range("2015-01-01", periods=1200)
    rng = np.random.default_rng(1)
    ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
    sig = ret.rolling(40).sum().shift(-40).fillna(0.0)
    pos = one_sided_shift_pvalue(sig, ret, 40, +1, n_perm=200, seed=0)
    neg = one_sided_shift_pvalue(sig, ret, 40, -1, n_perm=200, seed=0)
    assert pos["ic"] > 0.9 and pos["n_windows"] == 29
    assert pos["p_one_sided"] < 0.02
    assert neg["p_one_sided"] > 0.98
    assert abs(pos["p_two_sided"] - neg["p_two_sided"]) < 1e-12   # 双侧 p 与方向无关
