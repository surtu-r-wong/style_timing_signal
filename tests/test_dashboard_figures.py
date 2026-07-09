"""仪表盘图形层（dashboard/figures）：trace 结构 + chrome 轻断言。

图形正确性靠启动验收；此处锁 trace 数 / 2px 线宽 / 面色 / 无双轴。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("plotly")

from dashboard import figures as F  # noqa: E402


def _idx(n=300):
    return pd.bdate_range("2024-01-01", periods=n)


def test_fig_style_meter_traces_and_chrome():
    idx = _idx()
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "growth_index": (1 + rng.normal(0, 0.01, 300)).cumprod(),
        "value_index": (1 + rng.normal(0, 0.01, 300)).cumprod(),
        "spread": rng.normal(0, 0.005, 300),
        "signal": np.tanh(rng.normal(0, 1, 300)),
    }, index=idx)
    fig = F.fig_style_meter(df)
    assert len(fig.data) == 3
    assert all(tr.line.width == 2 for tr in fig.data)
    assert fig.layout.paper_bgcolor == F.SURFACE
    # 单系列子图（信号）不进图例——子图标题即身份
    assert fig.data[2].showlegend is False


def test_fig_thermometer_three_small_multiples_no_legend():
    idx = _idx()
    rng = np.random.default_rng(1)
    df = pd.DataFrame({
        "lu_ratio": rng.uniform(0, 0.05, 300), "lu_ratio_pct": rng.uniform(0, 1, 300),
        "burst_rate": rng.uniform(0, 1, 300), "burst_rate_pct": rng.uniform(0, 1, 300),
        "lu_premium": rng.normal(0, 0.02, 300), "lu_premium_pct": rng.uniform(0, 1, 300),
    }, index=idx)
    fig = F.fig_thermometer(df)
    assert len(fig.data) == 3
    assert fig.layout.showlegend is False
    # 小倍数 = 三个独立 y 轴（无双轴）
    assert {tr.yaxis for tr in fig.data} == {"y", "y2", "y3"}


def test_fig_margin_and_placeholder():
    idx = _idx()
    rng = np.random.default_rng(2)
    df = pd.DataFrame({
        "balance": rng.uniform(1e8, 2e8, 300), "buy": rng.uniform(1e6, 2e6, 300),
        "buy_ratio": rng.uniform(0.05, 0.12, 300), "bal_growth20": rng.normal(0, 0.03, 300),
        "buy_ratio_pct": rng.uniform(0, 1, 300),
    }, index=idx)
    assert len(F.fig_margin(df).data) == 3
    ph = F.fig_placeholder("PG 不可达")
    assert len(ph.data) == 0 and len(ph.layout.annotations) >= 1


def test_fig_energy_breadth_four_traces():
    idx = _idx()
    rng = np.random.default_rng(3)
    amt = pd.Series(rng.uniform(1e12, 3e12, 300), index=idx)
    br = pd.DataFrame({
        "pct_above_ma20": rng.uniform(0, 1, 300),
        "pct_above_ma60": rng.uniform(0, 1, 300),
        "hi_lo_diff20": rng.normal(0, 0.05, 300),
    }, index=idx)
    fig = F.fig_energy_breadth(amt, pd.Series(rng.uniform(0, 1, 300), index=idx), br)
    assert len(fig.data) == 4
    # 图例只留双系列子图的 MA20/60；单系列子图（成交额/新高新低）标题即身份
    assert [tr.showlegend for tr in fig.data] == [False, True, True, False]
