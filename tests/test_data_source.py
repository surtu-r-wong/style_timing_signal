import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import rows_to_frame  # noqa: E402


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
    assert df.index.is_monotonic_increasing
    assert df.loc["2026-01-05", "稳定"] == 100.0
    assert df["成长"].dtype == float


def test_rows_to_frame_missing_code_raises():
    with pytest.raises(ValueError, match="C9"):
        rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长", "C9": "金融"})
