import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import (  # noqa: E402
    pick_main_contract, annualized_basis, blend_carry, blend_returns,
    _third_friday, _expiry_from_symbol,
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


def test_blend_carry_fixed_5050_single_leg_halved():
    # IM 上市前只有 IC 的日子：blend carry = IC/2（固定 50/50，缺腿按 0），不是 IC 全额
    idx = pd.to_datetime(["2020-01-02", "2020-01-03", "2023-01-04"])
    ic = pd.Series([0.08, 0.10, 0.04], index=idx)
    im = pd.Series([float("nan"), float("nan"), 0.02], index=idx)
    out = blend_carry(ic, im)
    assert list(out.round(10)) == [0.04, 0.05, 0.03]


def test_blend_carry_index_union():
    # 两腿日期并集都保留，各自缺腿日按半额
    ic = pd.Series([0.08], index=pd.to_datetime(["2020-01-02"]))
    im = pd.Series([0.02], index=pd.to_datetime(["2023-01-04"]))
    out = blend_carry(ic, im)
    assert list(out.index) == list(pd.to_datetime(["2020-01-02", "2023-01-04"]))
    assert list(out.round(10)) == [0.04, 0.01]
