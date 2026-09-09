import numpy as np

from backtest.gate0_reanalysis import consistent_worst_half, flip_status


def test_consistent_worst_half_literal():
    assert consistent_worst_half(0.30, 0.25, 0.10) == 0.10          # 同号 → 最差半窗
    assert consistent_worst_half(-0.30, -0.25, -0.40) == 0.25       # 负向同号
    assert consistent_worst_half(0.30, -0.25, 0.10) == -np.inf      # 半窗反号 → 不可选
    assert consistent_worst_half(0.30, 0.25, float("nan")) == -np.inf
    assert consistent_worst_half(0.0, 0.1, 0.1) == -np.inf          # 全窗为 0 无方向


def test_flip_status_rules():
    assert flip_status(0.01, 0.01) == "replication_candidate"
    assert flip_status(0.01, 0.20) == "pattern_no_increment"
    assert flip_status(0.20, 0.01) == "no_flip"
    assert flip_status(0.05, 0.01) == "no_flip"                      # 边界不过
    assert flip_status(float("nan"), 0.01) == "no_flip"
