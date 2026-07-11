"""仪表盘 app 薄壳 smoke：布局构造 + 状态条装配（不起服务器）。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("dash")

from dashboard.app import build_layout, status_bar  # noqa: E402


def test_build_layout_has_five_zones():
    layout = build_layout()
    ids = []

    def _walk(node):
        if getattr(node, "id", None):
            ids.append(node.id)
        children = getattr(node, "children", None)
        if children is None:
            return
        if not isinstance(children, (list, tuple)):
            children = [children]
        for child in children:
            if hasattr(child, "to_plotly_json"):  # dash 组件才下钻
                _walk(child)

    _walk(layout)
    assert {"range", "status", "g-style", "g-thermo", "g-margin", "g-energy"} <= set(ids)


def _all_text(node, acc=None):
    if acc is None:
        acc = []
    if isinstance(node, str):
        acc.append(node)
        return acc
    children = getattr(node, "children", None)
    if children is None:
        return acc
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        _all_text(child, acc)
    return acc


def test_status_bar_flags_stale_position():
    """持仓文件落后于信号文件（忘跑 backtest.production）→ chip 必须亮警示 + 显示持仓截止日。"""
    signals = [
        {"name": "equal_weight", "factor": -0.22, "date": pd.Timestamp("2026-07-08"),
         "position": 1, "pos_date": pd.Timestamp("2026-06-30"), "series": None},
    ]
    text = " ".join(_all_text(html_root := status_bar(signals, {})[0]))
    assert "2026-06-30" in text          # 旧持仓的真实截止日必须可见
    assert "落后" in text                 # 警示语
    # 日期一致时不出警示
    signals[0]["pos_date"] = signals[0]["date"]
    text_ok = " ".join(_all_text(status_bar(signals, {})[0]))
    assert "落后" not in text_ok


def test_status_bar_renders_chips_and_freshness():
    signals = [
        {"name": "equal_weight", "factor": -0.22, "date": pd.Timestamp("2026-06-18"),
         "position": 1, "pos_date": pd.Timestamp("2026-06-18"), "series": None},
        {"name": "hybrid20", "factor": 0.92, "date": pd.Timestamp("2026-07-02"),
         "position": 0, "pos_date": pd.Timestamp("2026-07-02"), "series": None},
        {"name": "citic40d", "factor": 0.12, "date": pd.Timestamp("2026-07-02"),
         "position": 1, "pos_date": pd.Timestamp("2026-07-02"), "series": None},
    ]
    fresh = {"风格测量仪": pd.Timestamp("2026-07-04")}
    children = status_bar(signals, fresh)
    assert len(children) == 2  # chip 行 + 新鲜度行
    assert len(children[0].children) == 3
