"""拷贝校验：确认从 stock_selector 拷来的 financial_field_map 在本项目正确工作
（守设计稿 §4.1 不 import stock_selector；关键用例 port 自源测试）。"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.financial_field_map import (  # noqa: E402
    CSMAR_END, legal_disclosure_deadline, translate_data,
)


def test_csmar_end_cutoff():
    assert CSMAR_END == date(2025, 3, 31)


def test_translate_csmar_income_whitelist():
    """CSMAR 严格白名单：映射键改名，未列键丢弃。"""
    got = translate_data({"B001100000": 100.0, "F999_unknown": 5.0}, "csmar", "income")
    assert got == {"revenue": 100.0}


def test_translate_wind_income_passthrough():
    """Wind：映射键改名，未映射键原样透传。"""
    got = translate_data({"profit_ttm": 50.0, "other_field": 1.0}, "wind", "income")
    assert got == {"net_profit_ttm": 50.0, "other_field": 1.0}


def test_legal_disclosure_deadline_quarters():
    assert legal_disclosure_deadline(date(2020, 3, 31)) == date(2020, 4, 30)   # Q1
    assert legal_disclosure_deadline(date(2020, 6, 30)) == date(2020, 8, 31)   # H1
    assert legal_disclosure_deadline(date(2020, 9, 30)) == date(2020, 10, 31)  # Q3
    assert legal_disclosure_deadline(date(2020, 12, 31)) == date(2021, 4, 30)  # 年报→次年
