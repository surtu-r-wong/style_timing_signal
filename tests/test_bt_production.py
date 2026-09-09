"""推荐 production 持仓生成器测试（hybrid20/citic40d long-flat；equal_weight 对称，2026-09-09 裁决）。"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_recommended_frame_equal_weight_is_symmetric_on_committed_signal():
    from backtest.baseline import SIGNALS
    from backtest.positions import symmetric_position
    from backtest.production import PRODUCTION_MAPPING, RECOMMENDED_FILES, recommended_position_frame

    assert PRODUCTION_MAPPING["equal_weight"] == "symmetric"
    assert RECOMMENDED_FILES["equal_weight"] == "output/recommended/equal_weight_symmetric.csv"
    df = recommended_position_frame("equal_weight")
    assert list(df.columns) == ["date", "position"]
    assert df["position"].isin([-1, 0, 1]).all() and (df["position"] < 0).any()

    path, col = SIGNALS["equal_weight"]
    raw = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    assert list(df["position"]) == list(symmetric_position(raw[col]).astype(int).values)
    # 参照口径仍可显式取到 long-flat，且与对称的多头段逐日一致
    lf = recommended_position_frame("equal_weight", mapping="longflat")
    assert list(lf["position"]) == list((df["position"] > 0).astype(int).values)


def test_recommended_frame_other_two_signals_stay_long_flat():
    from backtest.production import PRODUCTION_MAPPING, recommended_position_frame
    for name in ("hybrid20", "citic40d"):
        assert PRODUCTION_MAPPING[name] == "longflat"
        df = recommended_position_frame(name)
        assert df["position"].isin([0, 1]).all(), f"{name} 应为 long-flat {{0,1}}"
        assert len(df) > 100
