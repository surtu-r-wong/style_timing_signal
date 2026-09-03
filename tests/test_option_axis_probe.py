import numpy as np
import pandas as pd

from backtest.leverage_probe import GRID_LEVEL
from backtest.option_axis_probe import FAMILIES_DESC, FAMILIES_MAIN, HALVES_OPTION, build_option_signals


def _frame(n=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"iv30": 0.2 + 0.05 * rng.standard_normal(n).cumsum() / 10,
                         "term": rng.normal(0, 0.01, n), "skew": rng.normal(0.02, 0.01, n),
                         "pcr": 1.0 + rng.normal(0, 0.1, n), "skew_clipped": False}, index=idx)


def test_build_option_signals_families_forms_and_no_lag():
    io, mo = _frame(), _frame(seed=1)
    sigs = build_option_signals(io, mo)
    assert tuple(sigs) == FAMILIES_MAIN + FAMILIES_DESC
    for fam, forms in sigs.items():
        assert set(forms) == {f"{fam}_lb{lb}zw{zw}" for lb, zw in GRID_LEVEL}
        for s in forms.values():
            assert s.index.max() == io.index.max()  # 当日收盘即知，无 pit_lag
            assert s.abs().max() <= 1.0
    # O6 = MO−IO 的 IV30 差，非恒等于 O1
    assert not np.allclose(sigs["O6"]["O6_lb5zw60"].tail(50), sigs["O1"]["O1_lb5zw60"].tail(50))
    assert build_option_signals(io, None).keys() == set(FAMILIES_MAIN)


def test_halves_are_option_specific():
    assert HALVES_OPTION == {"2020-2022": ("2020-01-01", "2022-12-31"), "2023-2026": ("2023-01-01", "2026-12-31")}
