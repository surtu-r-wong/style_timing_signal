import json
import sys

import numpy as np
import pandas as pd
import pytest

from signals.style_basket.b3_build import (
    B3Sources,
    PreflightOutcome,
    _fetch_stock_return_status,
    _write_stage_manifest,
    main,
    run_portfolios_stage,
    run_post_preflight_stages,
    run_states_stage,
)
from signals.style_basket.b3_config import load_b3_config
from signals.style_basket.b3_exposures import DataBlocked
from signals.style_basket.b3_portfolios import (
    build_portfolio_panels,
    natural_drift_leg_returns,
    scheduled_portfolio_returns,
    stock_period_returns,
)
from signals.style_basket.b3_states import (
    build_state_features,
    decompose_states,
)


def test_natural_drift_uses_formation_weights_and_does_not_re_equal_weight():
    dates = pd.bdate_range("2021-01-29", periods=3)
    weights = pd.Series({"A": 0.75, "B": 0.25})
    returns = pd.DataFrame(
        {
            "A": [0.99, 0.10, 0.00],
            "B": [0.99, 0.00, 0.20],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    got = natural_drift_leg_returns(
        weights,
        returns,
        suspended,
        dates[0],
        dates[-1],
    )

    assert list(got.index) == list(dates[1:])
    assert got.iloc[0] == pytest.approx(0.075)
    assert got.iloc[1] == pytest.approx(1.125 / 1.075 - 1.0)


def test_exact_suspension_keeps_value_and_unexplained_gap_blocks():
    dates = pd.bdate_range("2021-01-29", periods=2)
    weights = pd.Series({"A": 0.5, "B": 0.5})
    returns = pd.DataFrame(
        {
            "A": [0.0, 0.02],
            "B": [0.0, np.nan],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )
    suspended.loc[dates[1], "B"] = True

    got = natural_drift_leg_returns(
        weights,
        returns,
        suspended,
        dates[0],
        dates[1],
    )

    assert got.iloc[0] == pytest.approx(0.01)
    suspended.loc[dates[1], "B"] = False
    with pytest.raises(DataBlocked, match="unexplained price gap"):
        natural_drift_leg_returns(
            weights,
            returns,
            suspended,
            dates[0],
            dates[1],
        )


def test_next_formation_day_belongs_to_old_portfolio():
    dates = pd.bdate_range("2021-01-29", periods=5)
    returns = pd.DataFrame(
        {
            "A": [0.0, 0.01, 0.01, 0.01, 0.01],
            "B": [0.0, 0.02, 0.02, 0.02, 0.02],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )
    schedule = [
        (dates[0], pd.Series({"A": 1.0})),
        (dates[2], pd.Series({"B": 1.0})),
    ]

    got = scheduled_portfolio_returns(
        schedule,
        returns,
        suspended,
    )

    assert got.loc[dates[2]] == pytest.approx(0.01)
    assert got.loc[dates[3]] == pytest.approx(0.02)


def test_stock_period_returns_compounds_legal_suspension_days():
    dates = pd.bdate_range("2021-01-29", periods=3)
    returns = pd.DataFrame(
        {
            "A": [0.0, 0.10, 0.20],
            "B": [0.0, np.nan, 0.05],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )
    suspended.loc[dates[1], "B"] = True

    got = stock_period_returns(
        pd.Index(["A", "B"]),
        returns,
        suspended,
        dates[0],
        dates[-1],
    )

    assert got.loc["A"] == pytest.approx(1.10 * 1.20 - 1.0)
    assert got.loc["B"] == pytest.approx(0.05)


def _monthly_exposures():
    formation_dates = pd.to_datetime(
        ["2021-01-29", "2021-02-02"]
    )
    rows = []
    for formation, plus in (
        (formation_dates[0], {"A": 0.75, "B": 0.25, "C": 0.0}),
        (formation_dates[1], {"A": 0.0, "B": 1.0, "C": 0.0}),
    ):
        for ticker in ("A", "B", "C"):
            row = {
                "pit_policy": "legal_deadline",
                "formation_date": formation,
                "ticker": ticker,
                "universe_role": "model",
            }
            for axis in (
                "style",
                "size",
                "interaction",
                "qblend",
                "q500",
                "q1000",
            ):
                row[f"w_{axis}_plus"] = plus[ticker]
                row[f"w_{axis}_minus"] = float(ticker == "C")
            rows.append(row)
    return pd.DataFrame(rows)


def _portfolio_daily_inputs():
    dates = pd.bdate_range("2021-01-29", periods=5)
    returns = pd.DataFrame(
        {
            "A": [0.0, 0.01, 0.01, 0.01, 0.01],
            "B": [0.0, 0.02, 0.02, 0.02, 0.02],
            "C": [0.0, 0.00, 0.00, 0.00, 0.00],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )
    return dates, returns, suspended


def test_build_portfolio_panels_freezes_schemas_and_old_portfolio_boundary():
    dates, returns, suspended = _portfolio_daily_inputs()

    axis, legs, periods = build_portfolio_panels(
        _monthly_exposures(),
        returns,
        suspended,
    )

    assert list(axis.columns) == [
        "date",
        "pit_policy",
        "style",
        "size",
        "interaction",
    ]
    assert list(legs.columns) == [
        "date",
        "pit_policy",
        "q",
        "growth_ret",
        "value_ret",
    ]
    assert list(periods.columns) == [
        "pit_policy",
        "formation_date",
        "ticker",
        "forward_return",
    ]
    style = axis.set_index("date")["style"]
    expected_old_plus = (
        0.75 * 1.01 * 0.01
        + 0.25 * 1.02 * 0.02
    ) / (0.75 * 1.01 + 0.25 * 1.02)
    assert style.loc[dates[2]] == pytest.approx(expected_old_plus)
    assert style.loc[dates[3]] == pytest.approx(0.02)
    first_period = periods[
        periods["formation_date"].eq(dates[0])
    ].set_index("ticker")
    assert first_period.loc["A", "forward_return"] == pytest.approx(
        1.01**2 - 1.0
    )
    assert first_period.loc["B", "forward_return"] == pytest.approx(
        1.02**2 - 1.0
    )
    assert set(legs["q"]) == {"qblend", "q500", "q1000"}


def test_build_portfolio_panels_is_exposure_row_order_invariant():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()

    expected = build_portfolio_panels(
        exposures,
        returns,
        suspended,
    )
    shuffled = build_portfolio_panels(
        exposures.sample(frac=1.0, random_state=17),
        returns,
        suspended,
    )

    for left, right in zip(expected, shuffled):
        pd.testing.assert_frame_equal(left, right)


def test_build_portfolio_panels_blocks_duplicate_exposure_keys():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    exposures = pd.concat(
        [exposures, exposures.iloc[[0]]],
        ignore_index=True,
    )

    with pytest.raises(DataBlocked, match="duplicate"):
        build_portfolio_panels(exposures, returns, suspended)


def _stock_loader_frames():
    dates = pd.to_datetime(
        ["2021-01-29", "2021-02-01", "2021-02-02"]
    )
    prices = pd.DataFrame(
        {
            "ticker": ["A", "B", "A", "B"],
            "trade_date": [dates[0], dates[0], dates[1], dates[1]],
            "close": [10.0, 20.0, 11.0, np.nan],
            "pre_close": [10.0, 20.0, 10.0, 20.0],
            "volume": [100.0, 100.0, 100.0, 0.0],
        }
    )
    status = pd.DataFrame(
        {
            "ticker": ["B"],
            "trade_date": [dates[2]],
            "is_suspended": [True],
        }
    )
    calendar = pd.DataFrame({"trade_date": dates})
    return prices, status, calendar


def _patch_stock_loader_sql(monkeypatch, frames):
    prices, status, calendar = frames

    def fake_read_sql(db, sql, params=None):
        if "stock_daily_price_qfq" in sql:
            return prices.copy()
        if "stock_status" in sql:
            return status.copy()
        if "index_daily" in sql:
            return calendar.copy()
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        fake_read_sql,
    )


def test_stock_return_status_loader_uses_only_exact_suspension_dates(
    monkeypatch,
):
    _patch_stock_loader_sql(monkeypatch, _stock_loader_frames())

    returns, suspended = _fetch_stock_return_status(
        {"schema": "public"},
        pd.Timestamp("2021-02-02"),
    )

    assert returns.loc["2021-02-01", "A"] == pytest.approx(0.10)
    assert returns.loc["2021-02-01", "B"] == pytest.approx(0.0)
    assert bool(suspended.loc["2021-02-01", "B"])
    assert pd.isna(returns.loc["2021-02-02", "A"])
    assert not bool(suspended.loc["2021-02-02", "A"])
    assert pd.isna(returns.loc["2021-02-02", "B"])
    assert bool(suspended.loc["2021-02-02", "B"])


def test_stock_return_status_loader_blocks_duplicate_price_keys(
    monkeypatch,
):
    prices, status, calendar = _stock_loader_frames()
    prices = pd.concat([prices, prices.iloc[[0]]], ignore_index=True)
    _patch_stock_loader_sql(
        monkeypatch,
        (prices, status, calendar),
    )

    with pytest.raises(DataBlocked, match="duplicate"):
        _fetch_stock_return_status(
            {"schema": "public"},
            pd.Timestamp("2021-02-02"),
        )


def test_stock_return_status_loader_preserves_sql_failures(monkeypatch):
    error = RuntimeError("database transport failed")

    def broken_read_sql(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        broken_read_sql,
    )

    with pytest.raises(RuntimeError) as caught:
        _fetch_stock_return_status(
            {"schema": "public"},
            pd.Timestamp("2021-02-02"),
        )

    assert caught.value is error


def _write_exposures_parent(tmp_path, cfg, data_end):
    path = tmp_path / "monthly_exposures.csv.gz"
    _monthly_exposures().to_csv(
        path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [path],
        "OK",
        [],
    )
    return path


def _portfolio_sources(returns, suspended):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected source access")

    return B3Sources(
        snapshots=forbidden,
        constituents=forbidden,
        stock_returns=lambda data_end: (returns, suspended),
        target_returns=forbidden,
        carry=forbidden,
    )


def test_portfolios_stage_writes_exact_artifacts_and_manifest(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2021-02-04")
    _write_exposures_parent(tmp_path, cfg, data_end)
    dates, returns, suspended = _portfolio_daily_inputs()

    run_portfolios_stage(
        cfg,
        _portfolio_sources(returns, suspended),
        data_end,
        tmp_path,
    )

    expected = {
        "axis_returns.csv",
        "conditional_leg_returns.csv",
        "stock_period_returns.csv.gz",
    }
    assert all((tmp_path / name).is_file() for name in expected)
    manifest_path = tmp_path / "manifests" / "portfolios.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["stage"] == "portfolios"
    assert manifest["status"] == "OK"
    assert set(manifest["outputs"]) == expected


def test_portfolios_stage_rejects_tampered_exposure_parent(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2021-02-04")
    path = _write_exposures_parent(tmp_path, cfg, data_end)
    path.write_bytes(path.read_bytes() + b"tampered")
    dates, returns, suspended = _portfolio_daily_inputs()

    with pytest.raises(DataBlocked, match="hash"):
        run_portfolios_stage(
            cfg,
            _portfolio_sources(returns, suspended),
            data_end,
            tmp_path,
        )

    assert not (tmp_path / "manifests" / "portfolios.json").exists()


def test_post_preflight_portfolios_dispatches_exposures_then_portfolios(
    monkeypatch,
    tmp_path,
):
    calls = []
    outcome = PreflightOutcome(
        final_status="OK",
        exposures={},
        audit=pd.DataFrame(),
        diagnostics=pd.DataFrame(),
    )
    sources = object()

    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_exposures_stage",
        lambda *args: calls.append("exposures"),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_portfolios_stage",
        lambda *args: calls.append("portfolios"),
    )

    got = run_post_preflight_stages(
        "portfolios",
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
        outcome,
    )

    assert got == 0
    assert calls == ["exposures", "portfolios"]


def test_cli_accepts_portfolios_without_exposing_future_stages(
    monkeypatch,
    tmp_path,
):
    calls = []
    sources = object()
    outcome = PreflightOutcome(
        final_status="OK",
        exposures={},
        audit=pd.DataFrame(),
        diagnostics=pd.DataFrame(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.load_db_config",
        lambda: object(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.default_sources",
        lambda db: sources,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_preflight",
        lambda *args: outcome,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_post_preflight_stages",
        lambda stage, *args: calls.append(stage) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "b3_build.py",
            "--stage",
            "portfolios",
            "--data-end",
            "2023-12-31",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert calls == ["portfolios"]


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("pre_close", 0.0, "pre_close"),
        ("close", np.inf, "close"),
        ("volume", -1.0, "volume"),
    ],
)
def test_stock_loader_blocks_nonmissing_illegal_price_rows(
    monkeypatch,
    column,
    value,
    message,
):
    prices, status, calendar = _stock_loader_frames()
    prices.loc[2, "volume"] = 0.0
    prices.loc[2, column] = value
    _patch_stock_loader_sql(
        monkeypatch,
        (prices, status, calendar),
    )

    with pytest.raises(DataBlocked, match=message):
        _fetch_stock_return_status(
            {"schema": "public"},
            pd.Timestamp("2021-02-02"),
        )


@pytest.mark.parametrize("bad_weight", [-0.25, np.nan])
def test_natural_drift_rejects_illegal_weights(bad_weight):
    dates = pd.bdate_range("2021-01-29", periods=2)
    returns = pd.DataFrame(
        {"A": [0.0, 0.01], "B": [0.0, 0.02]},
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    with pytest.raises(DataBlocked, match="weight"):
        natural_drift_leg_returns(
            pd.Series({"A": 1.0, "B": bad_weight}),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_natural_drift_rejects_boolean_and_loose_sum_weights():
    dates = pd.bdate_range("2021-01-29", periods=2)
    returns = pd.DataFrame(
        {"A": [0.0, 0.01], "B": [0.0, 0.02]},
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    with pytest.raises(DataBlocked, match="weight"):
        natural_drift_leg_returns(
            pd.Series({"A": True, "B": False}),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )
    with pytest.raises(ValueError, match="sum to one"):
        natural_drift_leg_returns(
            pd.Series({"A": 0.5, "B": 0.500001}),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_string_false_cannot_disguise_unexplained_gap_as_suspension():
    dates = pd.bdate_range("2021-01-29", periods=2)
    returns = pd.DataFrame(
        {"A": [0.0, np.nan]},
        index=dates,
    )
    suspended = pd.DataFrame(
        {"A": [False, "False"]},
        index=dates,
    )

    with pytest.raises(DataBlocked, match="boolean"):
        natural_drift_leg_returns(
            pd.Series({"A": 1.0}),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_natural_drift_blocks_stock_return_below_minus_one():
    dates = pd.bdate_range("2021-01-29", periods=2)
    returns = pd.DataFrame(
        {
            "A": [0.0, -2.0],
            "B": [0.0, 0.0],
        },
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    with pytest.raises(DataBlocked, match="greater than -100%"):
        natural_drift_leg_returns(
            pd.Series({"A": 0.1, "B": 0.9}),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_stock_period_returns_blocks_positive_infinity():
    dates = pd.bdate_range("2021-01-29", periods=2)
    returns = pd.DataFrame(
        {"A": [0.0, np.inf]},
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    with pytest.raises(DataBlocked, match="finite"):
        stock_period_returns(
            pd.Index(["A"]),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_portfolio_panels_reject_unknown_universe_role():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    exposures.loc[0, "universe_role"] = "modle"

    with pytest.raises(DataBlocked, match="universe_role"):
        build_portfolio_panels(exposures, returns, suspended)


@pytest.mark.parametrize(
    "mutation",
    [
        "reverse_dates",
        "status_columns",
        "duplicate_dates",
        "duplicate_columns",
    ],
)
def test_portfolio_panels_reject_misaligned_return_panels(mutation):
    dates, returns, suspended = _portfolio_daily_inputs()
    if mutation == "reverse_dates":
        returns = returns.iloc[::-1]
        suspended = suspended.iloc[::-1]
    elif mutation == "status_columns":
        suspended = suspended.drop(columns="C")
    elif mutation == "duplicate_dates":
        returns = pd.concat([returns, returns.iloc[[-1]]])
        suspended = pd.concat([suspended, suspended.iloc[[-1]]])
    else:
        returns.columns = ["A", "B", "B"]
        suspended.columns = ["A", "B", "B"]

    with pytest.raises(DataBlocked, match="return panel"):
        build_portfolio_panels(
            _monthly_exposures(),
            returns,
            suspended,
        )


def test_stock_period_returns_rejects_empty_holding_period():
    dates, returns, suspended = _portfolio_daily_inputs()

    with pytest.raises(DataBlocked, match="no return dates"):
        stock_period_returns(
            pd.Index(["A"]),
            returns,
            suspended,
            dates[-1],
            dates[-1],
        )


def test_portfolio_panels_do_not_publish_all_empty_periods():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    exposures = exposures[
        exposures["formation_date"].eq(
            exposures["formation_date"].max()
        )
    ].copy()
    exposures["formation_date"] = dates[-1]

    with pytest.raises(DataBlocked, match="no portfolio return rows"):
        build_portfolio_panels(exposures, returns, suspended)


def test_final_empty_stock_period_is_omitted_not_encoded_as_zero():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    returns = returns.loc[: dates[2]]
    suspended = suspended.loc[: dates[2]]

    axis, legs, periods = build_portfolio_panels(
        exposures,
        returns,
        suspended,
    )

    assert not axis.empty
    assert not legs.empty
    assert set(periods["formation_date"]) == {dates[0]}
    assert periods["forward_return"].ne(0.0).any()


def test_portfolios_stage_rejects_returns_after_data_end(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2021-02-04")
    _write_exposures_parent(tmp_path, cfg, data_end)
    dates, returns, suspended = _portfolio_daily_inputs()
    future = pd.Timestamp("2021-02-05")
    returns.loc[future] = [0.01, 0.02, 0.00]
    suspended.loc[future] = [False, False, False]

    with pytest.raises(DataBlocked, match="data_end"):
        run_portfolios_stage(
            cfg,
            _portfolio_sources(returns, suspended),
            data_end,
            tmp_path,
        )

    assert not (tmp_path / "manifests" / "portfolios.json").exists()


def test_model_axes_legally_ignore_size_only_nan_weights():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    extra = []
    for formation in sorted(exposures["formation_date"].unique()):
        row = {
            "pit_policy": "legal_deadline",
            "formation_date": formation,
            "ticker": "D",
            "universe_role": "size_only",
        }
        for axis in (
            "style",
            "size",
            "interaction",
            "qblend",
            "q500",
            "q1000",
        ):
            for side in ("plus", "minus"):
                row[f"w_{axis}_{side}"] = (
                    0.0 if axis == "size" else np.nan
                )
        extra.append(row)
    exposures = pd.concat(
        [exposures, pd.DataFrame(extra)],
        ignore_index=True,
    )
    returns["D"] = 0.0
    suspended["D"] = False

    axis, legs, periods = build_portfolio_panels(
        exposures,
        returns,
        suspended,
    )

    assert not axis.empty
    assert not legs.empty
    assert "D" not in set(periods["ticker"])


def test_schedule_requires_each_next_formation_in_return_calendar():
    dates = pd.to_datetime(
        ["2021-01-29", "2021-02-01", "2021-02-03"]
    )
    returns = pd.DataFrame(
        {"A": [0.0, 0.01, 0.02]},
        index=dates,
    )
    suspended = pd.DataFrame(
        False,
        index=dates,
        columns=returns.columns,
    )

    with pytest.raises(DataBlocked, match="formation date"):
        scheduled_portfolio_returns(
            [
                (pd.Timestamp("2021-01-29"), pd.Series({"A": 1.0})),
                (pd.Timestamp("2021-02-02"), pd.Series({"A": 1.0})),
            ],
            returns,
            suspended,
        )


def test_stock_period_members_must_be_unique():
    dates, returns, suspended = _portfolio_daily_inputs()

    with pytest.raises(DataBlocked, match="members.*unique"):
        stock_period_returns(
            pd.Index(["A", "A"]),
            returns,
            suspended,
            dates[0],
            dates[-1],
        )


def test_timezone_aware_exposure_formation_is_data_blocked():
    dates, returns, suspended = _portfolio_daily_inputs()
    exposures = _monthly_exposures()
    exposures["formation_date"] = exposures["formation_date"].dt.tz_localize(
        "Asia/Shanghai"
    )

    with pytest.raises(DataBlocked, match="formation"):
        build_portfolio_panels(
            exposures,
            returns,
            suspended,
            data_end=pd.Timestamp("2021-02-04"),
        )


def test_state_decomposition_covers_uu_dd_div_and_zero_boundary():
    legs = pd.DataFrame(
        {
            "growth_ret": [0.02, -0.01, 0.02, 0.00, -0.01],
            "value_ret": [0.01, -0.03, -0.01, -0.02, 0.00],
        }
    )

    got = decompose_states(legs, tolerance=1.0e-12)

    assert list(got["state"]) == ["UU", "DD", "DIV", "DIV", "DIV"]
    np.testing.assert_allclose(
        got["d"],
        got["d_UU"] + got["d_DD"] + got["d_DIV"],
        atol=1.0e-12,
    )


def test_state_transform_uses_full_past_windows_and_never_future_data():
    index = pd.bdate_range("2019-01-01", periods=120)
    legs = pd.DataFrame(
        {
            "growth_ret": np.sin(np.arange(120) / 8.0) / 100.0,
            "value_ret": np.cos(np.arange(120) / 9.0) / 120.0,
        },
        index=index,
    )
    cfg = load_b3_config()

    original = build_state_features(legs, cfg)
    mutated = legs.copy()
    mutated.loc[index[100] :, "growth_ret"] = 0.20
    changed = build_state_features(mutated, cfg)

    pd.testing.assert_frame_equal(
        original.loc[: index[99]],
        changed.loc[: index[99]],
    )
    features = ["F_U", "F_D", "F_X", "F_T"]
    assert original[features].iloc[:62].isna().all().all()


def _state_leg_rows(cfg):
    dates = pd.bdate_range("2019-01-01", periods=120)
    rows = []
    for policy in cfg["pit"]["policies"]:
        for q_number, q in enumerate(("qblend", "q500", "q1000")):
            for number, date in enumerate(dates):
                rows.append(
                    {
                        "date": date,
                        "pit_policy": policy,
                        "q": q,
                        "growth_ret": (
                            np.sin(number / 8.0 + q_number) / 100.0
                        ),
                        "value_ret": (
                            np.cos(number / 9.0 + q_number) / 120.0
                        ),
                    }
                )
    return dates, pd.DataFrame(rows)


def _write_portfolios_parent(tmp_path, cfg, data_end, legs=None):
    if legs is None:
        _, legs = _state_leg_rows(cfg)
    axis_path = tmp_path / "axis_returns.csv"
    legs_path = tmp_path / "conditional_leg_returns.csv"
    periods_path = tmp_path / "stock_period_returns.csv.gz"
    pd.DataFrame(
        {
            "date": pd.to_datetime(["2019-01-02"]),
            "pit_policy": [cfg["pit"]["policies"][0]],
            "style": [0.0],
            "size": [0.0],
            "interaction": [0.0],
        }
    ).to_csv(axis_path, index=False)
    legs.to_csv(legs_path, index=False)
    pd.DataFrame(
        {
            "pit_policy": [cfg["pit"]["policies"][0]],
            "formation_date": pd.to_datetime(["2019-01-02"]),
            "ticker": ["A"],
            "forward_return": [0.0],
        }
    ).to_csv(
        periods_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "portfolios",
        cfg,
        data_end,
        [axis_path, legs_path, periods_path],
        "OK",
        [],
    )
    return legs_path


def _state_targets(dates):
    return {
        "blend": pd.Series(0.01, index=dates, dtype=float),
        "500": pd.Series(0.0, index=dates, dtype=float),
        "1000": pd.Series(-0.01, index=dates, dtype=float),
    }


def _state_sources(targets):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected source access")

    return B3Sources(
        snapshots=forbidden,
        constituents=forbidden,
        stock_returns=forbidden,
        target_returns=lambda data_end: targets,
        carry=forbidden,
    )


def test_states_stage_writes_exact_artifact_schema_and_manifest(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)

    run_states_stage(
        cfg,
        _state_sources(_state_targets(dates)),
        data_end,
        tmp_path,
    )

    path = tmp_path / "state_components.csv"
    got = pd.read_csv(path, parse_dates=["date"])
    assert list(got.columns) == [
        "date",
        "pit_policy",
        "q",
        "growth_ret",
        "value_ret",
        "g",
        "v",
        "d",
        "d_UU",
        "d_DD",
        "d_DIV",
        "state",
        "raw_U",
        "F_U",
        "raw_D",
        "F_D",
        "raw_X",
        "F_X",
        "raw_T",
        "F_T",
        "external_market_direction",
    ]
    assert len(got) == len(cfg["pit"]["policies"]) * 3 * len(dates)
    directions = got.groupby("q")["external_market_direction"].unique()
    assert list(directions["qblend"]) == ["up"]
    assert list(directions["q500"]) == ["non_positive"]
    assert list(directions["q1000"]) == ["non_positive"]
    manifest = json.loads(
        (tmp_path / "manifests" / "states.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["stage"] == "states"
    assert manifest["status"] == "OK"
    assert set(manifest["outputs"]) == {"state_components.csv"}


def test_states_stage_rejects_tampered_portfolios_parent(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    path = _write_portfolios_parent(tmp_path, cfg, data_end)
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(DataBlocked, match="hash"):
        run_states_stage(
            cfg,
            _state_sources(_state_targets(dates)),
            data_end,
            tmp_path,
        )

    assert not (tmp_path / "manifests" / "states.json").exists()


def test_states_stage_blocks_missing_target_date_instead_of_mislabeling(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)
    targets = _state_targets(dates)
    targets["blend"] = targets["blend"].drop(dates[70])

    with pytest.raises(DataBlocked, match="target.*missing"):
        run_states_stage(
            cfg,
            _state_sources(targets),
            data_end,
            tmp_path,
        )

    assert not (tmp_path / "manifests" / "states.json").exists()


@pytest.mark.parametrize("stage", ["states", "all"])
def test_post_preflight_state_stages_dispatch_full_chain(
    monkeypatch,
    tmp_path,
    stage,
):
    calls = []
    outcome = PreflightOutcome(
        final_status="OK",
        exposures={},
        audit=pd.DataFrame(),
        diagnostics=pd.DataFrame(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_exposures_stage",
        lambda *args: calls.append("exposures"),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_portfolios_stage",
        lambda *args: calls.append("portfolios"),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_states_stage",
        lambda *args: calls.append("states"),
    )

    got = run_post_preflight_stages(
        stage,
        load_b3_config(),
        object(),
        pd.Timestamp("2023-12-31"),
        tmp_path,
        outcome,
    )

    assert got == 0
    assert calls == ["exposures", "portfolios", "states"]


@pytest.mark.parametrize("stage", ["states", "all"])
def test_cli_accepts_state_stages(monkeypatch, tmp_path, stage):
    calls = []
    sources = object()
    outcome = PreflightOutcome(
        final_status="OK",
        exposures={},
        audit=pd.DataFrame(),
        diagnostics=pd.DataFrame(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.load_db_config",
        lambda: object(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.default_sources",
        lambda db: sources,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_preflight",
        lambda *args: outcome,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_post_preflight_stages",
        lambda requested, *args: calls.append(requested) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "b3_build.py",
            "--stage",
            stage,
            "--data-end",
            "2023-12-31",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert calls == [stage]


@pytest.mark.parametrize(
    "bad_value",
    [np.nan, np.inf, -1.0, True],
)
def test_state_decomposition_rejects_illegal_leg_returns(bad_value):
    legs = pd.DataFrame(
        {
            "growth_ret": [bad_value],
            "value_ret": [0.01],
        }
    )

    with pytest.raises(DataBlocked, match="leg return"):
        decompose_states(legs)


def test_state_decomposition_rejects_duplicate_required_columns():
    legs = pd.DataFrame(
        [[0.01, 0.02, 0.00]],
        columns=["growth_ret", "growth_ret", "value_ret"],
    )

    with pytest.raises(DataBlocked, match="duplicate"):
        decompose_states(legs)


@pytest.mark.parametrize(
    "mutation",
    ["non_datetime", "reverse", "duplicate", "timezone"],
)
def test_state_features_require_canonical_causal_date_axis(mutation):
    dates = pd.bdate_range("2019-01-01", periods=70)
    legs = pd.DataFrame(
        {
            "growth_ret": np.sin(np.arange(70) / 8.0) / 100.0,
            "value_ret": np.cos(np.arange(70) / 9.0) / 120.0,
        },
        index=dates,
    )
    if mutation == "non_datetime":
        legs.index = pd.RangeIndex(len(legs))
    elif mutation == "reverse":
        legs = legs.iloc[::-1]
    elif mutation == "duplicate":
        legs = pd.concat([legs, legs.iloc[[-1]]])
    else:
        legs.index = legs.index.tz_localize("Asia/Shanghai")

    with pytest.raises(DataBlocked, match="state feature"):
        build_state_features(legs, load_b3_config())


def test_state_raw_components_remain_additive():
    dates = pd.bdate_range("2019-01-01", periods=120)
    legs = pd.DataFrame(
        {
            "growth_ret": np.sin(np.arange(120) / 8.0) / 100.0,
            "value_ret": np.cos(np.arange(120) / 9.0) / 120.0,
        },
        index=dates,
    )

    got = build_state_features(legs, load_b3_config())

    np.testing.assert_allclose(
        got["raw_T"],
        got["raw_U"] + got["raw_D"] + got["raw_X"],
        atol=1.0e-12,
        equal_nan=True,
    )


def test_states_stage_rejects_policy_q_date_grid_drift(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, legs = _state_leg_rows(cfg)
    drift = legs.drop(
        legs[
            legs["pit_policy"].eq(cfg["pit"]["policies"][0])
            & legs["q"].eq("qblend")
            & legs["date"].eq(dates[50])
        ].index
    )
    _write_portfolios_parent(tmp_path, cfg, data_end, drift)

    with pytest.raises(DataBlocked, match="date grids"):
        run_states_stage(
            cfg,
            _state_sources(_state_targets(dates)),
            data_end,
            tmp_path,
        )


def test_states_stage_rejects_common_leg_gap_against_target_calendar(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, legs = _state_leg_rows(cfg)
    missing_date = dates[50]
    common_gap = legs[~legs["date"].eq(missing_date)].copy()
    _write_portfolios_parent(tmp_path, cfg, data_end, common_gap)

    with pytest.raises(DataBlocked, match="target.*date grid"):
        run_states_stage(
            cfg,
            _state_sources(_state_targets(dates)),
            data_end,
            tmp_path,
        )


def test_portfolios_rerun_invalidates_stale_states_manifest(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2021-02-04")
    state_dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)
    run_states_stage(
        cfg,
        _state_sources(_state_targets(state_dates)),
        data_end,
        tmp_path,
    )
    assert (tmp_path / "manifests" / "states.json").is_file()

    _write_exposures_parent(tmp_path, cfg, data_end)
    dates, returns, suspended = _portfolio_daily_inputs()
    returns = returns.copy()
    returns.loc[dates[1], "A"] = 0.03
    run_portfolios_stage(
        cfg,
        _portfolio_sources(returns, suspended),
        data_end,
        tmp_path,
    )

    assert not (tmp_path / "manifests" / "states.json").exists()


@pytest.mark.parametrize(
    "mutation",
    ["future", "reverse", "duplicate", "nan"],
)
def test_states_stage_rejects_invalid_target_contract(tmp_path, mutation):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)
    targets = _state_targets(dates)
    blend = targets["blend"]
    if mutation == "future":
        blend = pd.concat(
            [blend, pd.Series([0.01], index=[pd.Timestamp("2020-01-02")])]
        )
    elif mutation == "reverse":
        blend = blend.iloc[::-1]
    elif mutation == "duplicate":
        blend = pd.concat([blend, blend.iloc[[0]]])
    else:
        blend = blend.copy()
        blend.iloc[70] = np.nan
    targets["blend"] = blend

    with pytest.raises(DataBlocked, match="state target"):
        run_states_stage(
            cfg,
            _state_sources(targets),
            data_end,
            tmp_path,
        )


def test_states_stage_preserves_target_source_error_and_invalidates_stale(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)
    run_states_stage(
        cfg,
        _state_sources(_state_targets(dates)),
        data_end,
        tmp_path,
    )
    error = RuntimeError("target transport failed")

    def broken(*args, **kwargs):
        raise error

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected source access")

    sources = B3Sources(
        snapshots=forbidden,
        constituents=forbidden,
        stock_returns=forbidden,
        target_returns=broken,
        carry=forbidden,
    )

    with pytest.raises(RuntimeError) as caught:
        run_states_stage(cfg, sources, data_end, tmp_path)

    assert caught.value is error
    assert not (tmp_path / "manifests" / "states.json").exists()


def test_states_stage_is_byte_deterministic(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2019-12-31")
    dates, _ = _state_leg_rows(cfg)
    _write_portfolios_parent(tmp_path, cfg, data_end)
    sources = _state_sources(_state_targets(dates))

    run_states_stage(cfg, sources, data_end, tmp_path)
    first_output = (tmp_path / "state_components.csv").read_bytes()
    first_manifest = (
        tmp_path / "manifests" / "states.json"
    ).read_bytes()
    run_states_stage(cfg, sources, data_end, tmp_path)

    assert (tmp_path / "state_components.csv").read_bytes() == first_output
    assert (
        tmp_path / "manifests" / "states.json"
    ).read_bytes() == first_manifest
