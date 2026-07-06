"""推荐 production 持仓（long-flat）生成器测试。"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_recommended_frame_matches_production_position_on_committed_signal():
    from backtest.baseline import SIGNALS
    from backtest.positions import production_position
    from backtest.production import recommended_position_frame

    df = recommended_position_frame("equal_weight")
    assert list(df.columns) == ["date", "position"]
    assert df["position"].isin([0, 1]).all()

    path, col = SIGNALS["equal_weight"]
    raw = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    expected = production_position(raw[col])
    assert list(df["position"]) == list(expected.astype(int).values)


def test_recommended_frame_all_three_signals_long_flat():
    from backtest.production import recommended_position_frame
    for name in ("hybrid20", "citic40d", "equal_weight"):
        df = recommended_position_frame(name)
        assert df["position"].isin([0, 1]).all(), f"{name} 应为 long-flat {{0,1}}"
        assert len(df) > 100
