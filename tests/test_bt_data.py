import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import (  # noqa: E402
    pick_main_contract, annualized_basis, blend_returns, _third_friday, _expiry_from_symbol,
)


def test_pick_main_contract_by_oi():
    df = pd.DataFrame({"symbol": ["IC2606.CFE", "IC2609.CFE"], "oi": [152310, 85759]})
    assert pick_main_contract(df) == "IC2606.CFE"


def test_third_friday_june_2026():
    assert _third_friday(2026, 6) == date(2026, 6, 19)


def test_expiry_from_symbol():
    assert _expiry_from_symbol("IM2607.CFE") == _third_friday(2026, 7)


def test_annualized_basis_discount_positive():
    # 贴水: futures < spot → 正的年化基差率
    b = annualized_basis(futures=8270.0, spot=8400.0, trade_date=date(2026, 4, 29), symbol="IC2606.CFE")
    assert b > 0


def test_annualized_basis_premium_negative():
    b = annualized_basis(futures=8500.0, spot=8400.0, trade_date=date(2026, 4, 29), symbol="IC2606.CFE")
    assert b < 0


def test_blend_equal_weight():
    a = pd.Series([0.01, 0.02])
    c = pd.Series([0.03, 0.00])
    assert list(blend_returns(a, c)) == [0.02, 0.01]
