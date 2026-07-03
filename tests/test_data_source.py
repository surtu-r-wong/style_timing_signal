import sys
from datetime import date
from pathlib import Path

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
