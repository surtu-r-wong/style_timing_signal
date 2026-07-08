"""B2 切主评估：纯风格/自建混合/equal_weight 三方同秤（backtest/pure_style_eval）。"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_eval import align_common_window  # noqa: E402


def test_align_common_window_intersects_indices():
    """不同起点的信号对齐到公共交集窗（同秤前提：同窗对比）。"""
    a = pd.Series(1.0, index=pd.bdate_range("2013-12-01", periods=100))
    b = pd.Series(2.0, index=pd.bdate_range("2014-01-01", periods=100))
    got = align_common_window({"a": a, "b": b})
    assert list(got) == ["a", "b"]
    common = a.index.intersection(b.index)
    assert got["a"].index.equals(common) and got["b"].index.equals(common)
    assert len(common) > 0


def test_align_common_window_single_signal_passthrough():
    a = pd.Series(1.0, index=pd.bdate_range("2020-01-01", periods=10))
    got = align_common_window({"a": a})
    assert got["a"].index.equals(a.index)
