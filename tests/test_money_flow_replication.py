import pandas as pd

from backtest.money_flow_replication import DIRECTION, K, LB, ZW, eval_start, verdict


def test_frozen_parameters_match_original_representative():
    assert (LB, ZW, K, DIRECTION) == (5, 250, 20, +1)


def test_verdict_literal():
    assert verdict(0.049)["PASS"] is True
    assert verdict(0.05)["PASS"] is False
    assert verdict(float("nan"))["PASS"] is False


def test_eval_start_is_first_full_zw_day():
    idx = pd.bdate_range("2020-07-06", periods=300)
    e = pd.Series(0.0, index=idx)
    assert eval_start(e, 250) == idx[249]
