import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_code_map  # noqa: E402


def test_code_map_contains_citic_and_gv_pairs():
    m = load_code_map()
    assert m["稳定"] == "CI005921.WI"
    assert m["成长"] == "CI005920.WI"
    assert m["中证500成长"] == "H30351.CSI"
    assert m["中证1000价值"] == "932406.CSI"
    assert m["沪深300"] == "000300.SH"


def test_code_map_rejects_duplicate_names(tmp_path):
    f = tmp_path / "codes.csv"
    f.write_text("name,code\nA,X1\nA,X2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_code_map(f)
