"""Trading-session alignment, quote gaps and atomic publication of daily references."""

import numpy as np
import pandas as pd
import pytest

from signals import basis_reference as ref


def history(days=335):
    dates = pd.bdate_range(ref.IC_START, periods=days, name="date")
    ew = pd.Series(np.where(np.arange(days) % 3 == 0, -0.2, 0.2), index=dates)
    spot = pd.DataFrame(
        {"trade_date": dates.date, "index_code": "000905.SH", "close": 1000.0}
    )
    quotes = []
    for i, day in enumerate(dates):
        for j, month in enumerate(["1803", "1806", "1809", "1812"]):
            price = 1000 + (j + 1) * (np.sin(i / 6) * 9 + np.cos(i / 40) * 4)
            quotes.append((day.date(), "IC" + month + ".CFE", price))
    return (
        ew,
        dates,
        spot,
        pd.DataFrame(quotes, columns=["trade_date", "symbol", "close"]),
    )


def test_daily_scores_use_native_reference_clock_and_no_later_prices():
    args = history()
    full = ref.basis_history(*args)
    for name, offset in [("T1", 0), ("T2", 0), ("T3", 20)]:
        held = np.zeros(len(full))
        seen = pd.Series(pd.NaT, index=full.index, dtype="datetime64[ns]")
        for j in range(offset, len(full), 40):
            held[j : j + 40] = np.sign(full[name + "_score"].iloc[j])
            seen.iloc[j : j + 40] = full.index[j]
        np.testing.assert_array_equal(full[name + "_direction"], held)
        pd.testing.assert_series_equal(
            full[name + "_sample_date"], seen, check_names=False
        )
    assert full.T1_score.min() < 0 < full.T1_score.max()
    assert full.IM_contracts_used.isna().all()
    pd.testing.assert_frame_equal(ref.basis_history(*history(125)), full.iloc[:125])


def test_partial_leg_calendar_and_missing_spot_are_rejected():
    ew, dates, spot, futures = history(90)
    with pytest.raises(ref.IncompleteData, match="3/4"):
        ref.basis_history(ew, dates, spot, futures.iloc[:-1])
    with pytest.raises(ref.IncompleteData, match="缺少有效现货"):
        ref.basis_history(ew, dates, spot.iloc[:-1], futures)
    with pytest.raises(ref.IncompleteData, match="标准 EW 与交易所"):
        ref.basis_history(ew.drop(dates[-4]), dates, spot, futures)
    with pytest.raises(ref.IncompleteData, match="日期重复"):
        ref.basis_history(ew, dates, spot, pd.concat([futures, futures.tail(1)]))


def test_listed_im_must_supply_its_own_four_contracts():
    ew, dates, spot, futures = history(21)
    listing = pd.Timestamp(ref.IM_START)
    dates = dates.append(pd.DatetimeIndex([listing], name="date"))
    ew.loc[listing] = -0.1
    spot.loc[len(spot)] = [listing.date(), "000905.SH", 1000.0]
    spot.loc[len(spot)] = [listing.date(), "000852.SH", 1500.0]
    ic = pd.DataFrame(
        [
            (listing.date(), "IC" + m + ".CFE", 990.0)
            for m in ("2208", "2209", "2212", "2303")
        ],
        columns=futures.columns,
    )
    futures = pd.concat([futures, ic])
    with pytest.raises(ref.IncompleteData, match="IM 2022-07-22.*0/4"):
        ref.basis_history(ew, dates, spot, futures)
    im = ic.assign(symbol=ic.symbol.str.replace("IC", "IM"), close=1495.0)
    both = ref.basis_history(ew, dates, spot, pd.concat([futures, im]))
    assert both.loc[listing, "IM_contracts_used"] == 4


def test_incomplete_rerun_preserves_last_table_and_success_replaces_it(
    tmp_path, monkeypatch
):
    ew, dates, spot, futures = history(91)
    ew_file, target = tmp_path / "ew.csv", tmp_path / "reference/table.csv"
    ew.rename("factor_value").to_csv(ew_file)
    monkeypatch.setattr(ref, "load_db_config", lambda _: {})
    monkeypatch.setattr(ref, "read_market", lambda *_: (dates, spot, futures))
    ref.generate(ew_file, target, tmp_path)
    expected = target.read_bytes()
    monkeypatch.setattr(ref, "read_market", lambda *_: (dates, spot, futures.iloc[:-1]))
    with pytest.raises(ref.IncompleteData):
        ref.generate(ew_file, target, tmp_path)
    assert target.read_bytes() == expected
    monkeypatch.setattr(ref, "read_market", lambda *_: (dates, spot, futures))
    ref.generate(ew_file, target, tmp_path)
    assert target.read_bytes() == expected
    assert list(target.parent.iterdir()) == [target]
