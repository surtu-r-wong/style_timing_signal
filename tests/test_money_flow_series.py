import numpy as np
import pandas as pd

from backtest.money_flow_series import families, net_ratio, residualize_pit


def test_net_ratio_is_scale_free_and_guards_zero_gross():
    main_in = pd.Series([60.0, 0.0]); main_out = pd.Series([40.0, 0.0]); net = pd.Series([20.0, 0.0])
    out = net_ratio(net, main_in, main_out)
    assert out.iloc[0] == 0.2
    assert np.isnan(out.iloc[1])
    assert net_ratio(net * 1e4, main_in * 1e4, main_out * 1e4).iloc[0] == 0.2


def test_residualize_removes_known_linear_relation_using_past_only():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2020-01-01", periods=400)
    r = pd.Series(rng.normal(0, 0.01, 400), index=idx, name="r")
    f = (0.03 + 5.0 * r).rename("f")                       # 精确线性，无噪声
    e = residualize_pit(f, r, window=250, min_obs=120)
    assert e.iloc[:120].isna().all()                        # min_obs 之前（含第 120 位，因 shift(1)）不出值
    assert e.iloc[121:].abs().max() < 1e-9                  # 过去 OLS 精确复原 (a,b) → 残差 0


def test_residualize_is_point_in_time():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2020-01-01", periods=300)
    r = pd.Series(rng.normal(0, 0.01, 300), index=idx, name="r")
    f = (0.5 * r + rng.normal(0, 0.005, 300)).rename("f")
    full = residualize_pit(f, r, window=100, min_obs=50)
    trunc = residualize_pit(f.iloc[:200], r.iloc[:200], window=100, min_obs=50)
    pd.testing.assert_series_equal(full.iloc[:200], trunc)  # 追加未来样本不改写历史残差


def test_residual_at_t_excludes_t_itself():
    idx = pd.bdate_range("2021-01-01", periods=60)
    r = pd.Series(np.linspace(-0.01, 0.01, 60), index=idx, name="r")
    f = (2.0 * r).rename("f")
    f2 = f.copy(); f2.iloc[-1] = 99.0                         # 只改最后一日的 f
    e1 = residualize_pit(f, r, window=50, min_obs=10); e2 = residualize_pit(f2, r, window=50, min_obs=10)
    pd.testing.assert_series_equal(e1.iloc[:-1], e2.iloc[:-1])   # 之前各日残差不受影响
    assert abs(e2.iloc[-1] - (99.0 - 2.0 * r.iloc[-1])) < 1e-9


def test_families_literal_and_min_three_rule():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    e = {"000300.SH": pd.Series([0.10, 0.10], index=idx), "000905.SH": pd.Series([0.30, 0.20], index=idx),
         "000852.SH": pd.Series([0.50, 0.40], index=idx), "932000.CSI": pd.Series([np.nan, 0.10], index=idx)}
    F = families(e)
    assert abs(F["F2"].iloc[0] - ((0.30 + 0.50) / 2 - 0.10)) < 1e-12          # 0.30
    assert abs(F["F1"].iloc[0] - (0.10 + 0.30 + 0.50) / 3) < 1e-12            # 三指数可用即记
    assert abs(F["F1"].iloc[1] - (0.10 + 0.20 + 0.40 + 0.10) / 4) < 1e-12
    e["000852.SH"] = pd.Series([np.nan, 0.40], index=idx)
    assert np.isnan(families(e)["F1"].iloc[0])                                 # 仅 2 指数 → NaN
