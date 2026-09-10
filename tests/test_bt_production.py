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


def test_recommended_frame_slope20_is_symmetric_and_matches_definition():
    """slope20 2026-09-09 上线为第四条线：对称映射；信号文件须与 momentum_factor_fn 的 slope/20/0/120/0 定义同源（由生成器保证）。"""
    from backtest.production import PRODUCTION_MAPPING, RECOMMENDED_FILES, recommended_position_frame
    assert PRODUCTION_MAPPING["slope20"] == "symmetric"
    assert RECOMMENDED_FILES["slope20"] == "output/recommended/slope20_symmetric.csv"
    df = recommended_position_frame("slope20")
    assert df["position"].isin([-1, 0, 1]).all() and (df["position"] < 0).any() and len(df) > 100


def test_slope20_longflat_reference_output_for_spot_pool():
    """2026-09-10 两池分信号：现货池（只能做多）跟 slope20 long-flat。参照产出必须登记、写盘，且等于对称口径的多头段。"""
    from backtest.production import REFERENCE_OUTPUTS, recommended_position_frame, write_recommended_positions
    assert REFERENCE_OUTPUTS["slope20"] == "longflat"
    sym = recommended_position_frame("slope20")
    lf = recommended_position_frame("slope20", mapping="longflat")
    assert lf["position"].isin([0, 1]).all()
    assert list(lf["position"]) == list((sym["position"] > 0).astype(int).values)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        written = write_recommended_positions(Path(d))
        assert written["slope20[longflat 参照]"].name == "slope20_longflat.csv"
        assert written["slope20[longflat 参照]"].exists()


def test_recommended_frame_other_two_signals_stay_long_flat():
    from backtest.production import PRODUCTION_MAPPING, recommended_position_frame
    for name in ("hybrid20", "citic40d"):
        assert PRODUCTION_MAPPING[name] == "longflat"
        df = recommended_position_frame(name)
        assert df["position"].isin([0, 1]).all(), f"{name} 应为 long-flat {{0,1}}"
        assert len(df) > 100
