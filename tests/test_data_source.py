import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_pg_closes, rows_to_frame  # noqa: E402


def _rows():
    return [
        ("C1", date(2026, 1, 6), 110.0),
        ("C1", date(2026, 1, 5), 100.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 220.0),
    ]


def test_rows_to_frame_shapes_wide_chinese_columns():
    df = rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长"})
    assert list(df.columns) == ["稳定", "成长"]
    assert df.columns.name is None
    assert df.index.is_monotonic_increasing
    assert df.loc["2026-01-05", "稳定"] == 100.0
    assert df["成长"].dtype == float


def test_rows_to_frame_missing_code_raises():
    with pytest.raises(ValueError, match="C9"):
        rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长", "C9": "金融"})


def test_rows_to_frame_interior_gap_raises_with_dates():
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 7), 120.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
    ]
    with pytest.raises(ValueError) as ei:
        rows_to_frame(rows, {"C1": "稳定", "C2": "成长"})
    msg = str(ei.value)
    assert "稳定" in msg
    assert "2026-01-06" in msg


def test_rows_to_frame_ragged_tail_warns_not_raises(capsys):
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 6), 110.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
    ]
    df = rows_to_frame(rows, {"C1": "稳定", "C2": "成长"})
    assert len(df) == 3
    err = capsys.readouterr().err
    assert "稳定" in err
    assert "2026-01-06" in err
    assert "2026-01-07" in err


def test_rows_to_frame_leading_nan_allowed(capsys):
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 6), 110.0),
        ("C1", date(2026, 1, 7), 120.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
    ]
    df = rows_to_frame(rows, {"C1": "稳定", "C2": "成长"})
    assert df["成长"].isna().sum() == 1
    assert df.loc["2026-01-06", "成长"] == 210.0
    assert capsys.readouterr().err == ""


def test_load_pg_closes_unknown_name_raises_without_db():
    with pytest.raises(KeyError, match="不存在的名字"):
        load_pg_closes(["不存在的名字"])


# ── Finding 1: trim_ragged_tail 尾部参差裁剪 ──────────────────────────


def test_rows_to_frame_trim_ragged_tail_truncates(capsys):
    """trim=True + 尾部参差 → 裁到较早的最新有效日，不抛异常，警告落 stderr。"""
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 6), 110.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
    ]
    df = rows_to_frame(rows, {"C1": "稳定", "C2": "成长"}, trim_ragged_tail=True)
    captured = capsys.readouterr()
    assert df.index.max() == pd.Timestamp("2026-01-06")
    assert len(df) == 2
    assert df["成长"].isna().sum() == 0  # 尾部 NaN 已被裁掉
    # 警告在 stderr（不污染 stdout），点名各列最新有效日与裁剪跨度
    assert captured.out == ""
    assert "稳定" in captured.err
    assert "2026-01-06" in captured.err
    assert "2026-01-07" in captured.err


def test_rows_to_frame_trim_aligned_no_warning(capsys):
    """trim=True 但尾部已对齐 → 帧不变，且不发警告。"""
    df = rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长"}, trim_ragged_tail=True)
    captured = capsys.readouterr()
    assert len(df) == 2
    assert df.index.max() == pd.Timestamp("2026-01-06")
    assert captured.err == ""
    assert captured.out == ""


def test_rows_to_frame_trim_interior_gap_still_raises():
    """trim=True 不得掩盖列内部缺口：裁掉尾部参差后内部缺口仍 ValueError。"""
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        # C1 缺 1-6（内部缺口），1-7 有值
        ("C1", date(2026, 1, 7), 120.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
        ("C2", date(2026, 1, 8), 230.0),  # 尾部参差：C2 延到 1-8
    ]
    with pytest.raises(ValueError, match="内部缺口"):
        rows_to_frame(rows, {"C1": "稳定", "C2": "成长"}, trim_ragged_tail=True)


def test_rows_to_frame_trim_leading_nan_allowed(capsys):
    """trim=True + 起始缺失（列晚发布）但尾部对齐 → 放行，不抛异常。"""
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 6), 110.0),
        ("C1", date(2026, 1, 7), 120.0),
        ("C2", date(2026, 1, 6), 210.0),  # C2 晚发布，1-5 为起始 NaN
        ("C2", date(2026, 1, 7), 220.0),
    ]
    df = rows_to_frame(rows, {"C1": "稳定", "C2": "成长"}, trim_ragged_tail=True)
    captured = capsys.readouterr()
    assert df["成长"].isna().sum() == 1  # 起始 NaN 保留
    assert df.index.max() == pd.Timestamp("2026-01-07")
    assert len(df) == 3
    assert captured.err == ""


def test_rows_to_frame_trim_false_default_does_not_truncate(capsys):
    """trim=False（默认）+ 尾部参差 → 沿用旧策略：仅警告，绝不裁剪。"""
    rows = [
        ("C1", date(2026, 1, 5), 100.0),
        ("C1", date(2026, 1, 6), 110.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 210.0),
        ("C2", date(2026, 1, 7), 220.0),
    ]
    df = rows_to_frame(rows, {"C1": "稳定", "C2": "成长"}, trim_ragged_tail=False)
    captured = capsys.readouterr()
    assert len(df) == 3  # 未裁剪
    assert df.index.max() == pd.Timestamp("2026-01-07")
    assert df["稳定"].isna().sum() == 1  # 尾部参差 NaN 保留
    assert "稳定" in captured.err  # 旧策略仍发警告
