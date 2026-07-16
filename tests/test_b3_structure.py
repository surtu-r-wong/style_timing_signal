import json
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

import backtest.b3_structure as b3_structure
from backtest.b3_structure import (
    MODEL_COMPARISON_COLUMNS,
    MODEL_ROW_ID_COLUMNS,
    ModelFit,
    _closed_formation_window,
    _load_equal_weight_control,
    apply_model,
    assign_hard_sort_cells,
    build_hard_sort_surface,
    build_model_comparison,
    build_structure_coefficients,
    fama_macbeth_coefficients,
    fit_model,
    main,
    newey_west_mean_t,
    next_formation_targets,
    oos_r_squared,
    ordinary_mean_t,
    run_structure,
    stability_gate,
    state_coverage_gate,
)
from signals.style_basket.b3_build import _write_stage_manifest
from signals.style_basket.b3_config import load_b3_config
from signals.style_basket.b3_exposures import DataBlocked


def _structure_panel(
    with_interaction: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(19)
    rows = []
    returns = []
    for date in pd.date_range("2014-01-31", periods=36, freq="ME"):
        for number in range(240):
            ticker = f"S{number:03d}"
            s = rng.normal()
            m = rng.normal()
            h = s * m + rng.normal(scale=0.1)
            value = 0.02 * s + 0.01 * m + rng.normal(scale=0.03)
            if with_interaction:
                value += 0.04 * h
            rows.append(
                {
                    "formation_date": date,
                    "ticker": ticker,
                    "s_perp": s,
                    "m_perp": m,
                    "h_perp": h,
                    "industry": "A" if number % 2 else "B",
                }
            )
            returns.append(
                {
                    "formation_date": date,
                    "ticker": ticker,
                    "forward_return": value,
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(returns)


def test_hard_sort_assigns_every_name_to_2x3_and_5x5_cells():
    exposures, _ = _structure_panel(True)
    month = exposures[
        exposures["formation_date"]
        == exposures["formation_date"].min()
    ]

    cells = assign_hard_sort_cells(month)

    assert cells["cell_2x3"].notna().all()
    assert cells["cell_5x5"].notna().all()
    assert cells["cell_2x3"].nunique() == 6
    assert cells["cell_5x5"].nunique() == 25


def test_fama_macbeth_recovers_positive_interaction_and_zero_control():
    positive_x, positive_r = _structure_panel(True)
    null_x, null_r = _structure_panel(False)

    positive = fama_macbeth_coefficients(positive_x, positive_r)
    null = fama_macbeth_coefficients(null_x, null_r)

    assert positive["beta_h"].mean() > 0.03
    assert abs(null["beta_h"].mean()) < 0.01
    assert ordinary_mean_t(positive["beta_h"]) > 2.0
    assert newey_west_mean_t(positive["beta_h"], lag=3) > 2.0


def _surface_inputs(
    dates=None,
    policies=("legal_deadline",),
    names_per_cell=4,
    include_final_return=False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if dates is None:
        dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposure_rows = []
    return_rows = []
    for policy_number, policy in enumerate(policies):
        for date_number, date in enumerate(pd.DatetimeIndex(dates)):
            for size in range(5):
                for style in range(5):
                    for repeat in range(names_per_cell):
                        ticker = (
                            f"P{policy_number}D{date_number}"
                            f"M{size}S{style}R{repeat}"
                        )
                        m = size + repeat / 1_000 + style / 100_000
                        s = style + repeat / 1_000 + size / 100_000
                        h = (m - 2.0) * (s - 2.0) + (
                            (repeat + size + style) % 3
                        ) / 10_000
                        forward = (
                            0.002
                            + 0.01 * s
                            + 0.02 * m
                            + 0.03 * h
                            + date_number / 10_000
                        )
                        exposure_rows.append(
                            {
                                "pit_policy": policy,
                                "formation_date": date,
                                "ticker": ticker,
                                "universe_role": "model",
                                "industry": (
                                    "A" if (size + style + repeat) % 2 else "B"
                                ),
                                "s_perp": s,
                                "m_perp": m,
                                "h_perp": h,
                            }
                        )
                        if (
                            date_number < len(dates) - 1
                            or include_final_return
                        ):
                            return_rows.append(
                                {
                                    "pit_policy": policy,
                                    "formation_date": date,
                                    "ticker": ticker,
                                    "forward_return": forward,
                                }
                            )
    return pd.DataFrame(exposure_rows), pd.DataFrame(return_rows)


def test_hard_sort_ties_are_ticker_deterministic_and_row_order_invariant():
    month = pd.DataFrame(
        {
            "ticker": ["D", "B", "A", "C", "F", "E"],
            "m_perp": [0.0] * 6,
            "s_perp": [1.0] * 6,
        }
    )

    first = assign_hard_sort_cells(month).set_index("ticker")
    second = assign_hard_sort_cells(
        month.sample(frac=1.0, random_state=31)
    ).set_index("ticker")

    pd.testing.assert_frame_equal(
        first[["cell_2x3", "cell_5x5"]].sort_index(),
        second[["cell_2x3", "cell_5x5"]].sort_index(),
    )
    assert first.loc["A", "cell_2x3"].startswith("big_")
    assert first.loc["F", "cell_2x3"].startswith("small_")


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "nan", "boolean", "whitespace"],
)
def test_hard_sort_rejects_illegal_cross_section(mutation):
    month = pd.DataFrame(
        {
            "ticker": ["A", "B", "C", "D"],
            "m_perp": [-1.0, -0.5, 0.5, 1.0],
            "s_perp": [-1.0, 0.5, -0.5, 1.0],
        }
    )
    if mutation == "duplicate":
        month.loc[1, "ticker"] = "A"
    elif mutation == "nan":
        month.loc[1, "m_perp"] = np.nan
    elif mutation == "boolean":
        month["s_perp"] = month["s_perp"].astype(object)
        month.loc[1, "s_perp"] = True
    else:
        month.loc[1, "ticker"] = " B "

    with pytest.raises(DataBlocked):
        assign_hard_sort_cells(month)


def test_hard_sort_surface_rejects_industry_with_edge_whitespace():
    exposures, returns = _surface_inputs()
    exposures.loc[exposures.index[0], "industry"] = " A"

    with pytest.raises(DataBlocked, match="industry"):
        build_hard_sort_surface(exposures, returns)


def test_hard_sort_surface_emits_31_cells_and_14_fixed_diagnostics_per_month():
    exposures, returns = _surface_inputs()

    got = build_hard_sort_surface(exposures, returns)

    assert list(got.columns) == [
        "pit_policy",
        "formation_date",
        "row_type",
        "grid",
        "cell",
        "diagnostic",
        "member_count",
        "industry_distribution",
        "formation_coverage",
        "holding_return",
        "status",
    ]
    for _, month in got.groupby(["pit_policy", "formation_date"]):
        cells = month[month["row_type"].eq("cell")]
        diagnostics = month[month["row_type"].eq("diagnostic")]
        assert len(cells) == 31
        assert len(diagnostics) == 14
        assert (cells["status"] == "OK").all()
        assert cells.loc[cells["grid"].eq("2x3"), "member_count"].sum() == 100
        assert cells.loc[cells["grid"].eq("5x5"), "member_count"].sum() == 100
        assert set(diagnostics["diagnostic"]) == {
            "corner",
            "growth_minus_value",
            "adjacent_row_difference",
            "linear_prediction_residual",
        }
        assert not diagnostics["diagnostic"].str.contains(
            "best|selector", case=False, regex=True
        ).any()
        for row in cells.itertuples():
            industry_count = sum(
                json.loads(row.industry_distribution).values()
            )
            assert industry_count == row.member_count


def test_hard_sort_corner_and_linear_residual_use_frozen_directions():
    dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(dates=dates)
    month = exposures[
        exposures["formation_date"].eq(dates[0])
    ].drop(columns="pit_policy")
    assigned = assign_hard_sort_cells(month)
    corner_returns = {
        "big_value": 0.01,
        "big_middle": 0.00,
        "big_growth": 0.04,
        "small_value": 0.02,
        "small_middle": 0.00,
        "small_growth": 0.08,
    }
    keyed = assigned.set_index("ticker")["cell_2x3"].map(corner_returns)
    returns["forward_return"] = returns["ticker"].map(keyed)

    got = build_hard_sort_surface(exposures, returns)

    corner = got[
        got["diagnostic"].eq("corner")
    ]["holding_return"].iloc[0]
    assert corner == pytest.approx(0.03)

    coefficients = fama_macbeth_coefficients(
        month,
        returns.drop(columns="pit_policy"),
    )
    beta_h = coefficients["beta_h"].iloc[0]
    joined = assigned.merge(
        returns.drop(columns="pit_policy"),
        on=["formation_date", "ticker"],
        validate="one_to_one",
    )
    gmv = {}
    h_spread = {}
    for size in range(1, 6):
        growth = joined[joined["cell_5x5"].eq(f"S{size}_V5")]
        value = joined[joined["cell_5x5"].eq(f"S{size}_V1")]
        gmv[size] = growth["forward_return"].mean() - value["forward_return"].mean()
        h_spread[size] = growth["h_perp"].mean() - value["h_perp"].mean()
    for size in range(2, 6):
        label = f"S{size}-S{size - 1}"
        actual = gmv[size] - gmv[size - 1]
        predicted = beta_h * (h_spread[size] - h_spread[size - 1])
        residual = got[
            got["diagnostic"].eq("linear_prediction_residual")
            & got["cell"].eq(label)
        ]["holding_return"].iloc[0]
        assert residual == pytest.approx(actual - predicted)


def test_hard_sort_surface_reports_empty_legal_cells_without_selecting_around_them():
    exposures, returns = _surface_inputs(
        dates=["2014-01-31", "2014-02-28"]
    )
    exposures = exposures.copy()
    exposures["s_perp"] = exposures["m_perp"] ** 3

    got = build_hard_sort_surface(exposures, returns)

    cells = got[got["row_type"].eq("cell")]
    assert len(cells) == 31
    blocked = cells[cells["status"].eq("COVERAGE_BLOCKED")]
    assert not blocked.empty
    assert blocked["member_count"].eq(0).all()
    assert blocked["holding_return"].isna().all()
    assert len(got[got["row_type"].eq("diagnostic")]) == 14


def test_hard_sort_surface_uses_only_model_universe():
    exposures, returns = _surface_inputs(
        dates=["2014-01-31", "2014-02-28"]
    )
    extra = exposures.iloc[[0]].copy()
    extra["ticker"] = "SIZE_ONLY"
    extra["universe_role"] = "size_only"
    extra[["s_perp", "m_perp", "h_perp"]] = np.nan
    exposures = pd.concat([exposures, extra], ignore_index=True)

    got = build_hard_sort_surface(exposures, returns)

    cells = got[got["row_type"].eq("cell")]
    assert cells.loc[cells["grid"].eq("2x3"), "member_count"].sum() == 100
    assert cells.loc[cells["grid"].eq("5x5"), "member_count"].sum() == 100


@pytest.mark.parametrize("mutation", ["missing_key", "duplicate_key", "middle_gap"])
def test_hard_sort_surface_refuses_silent_inner_join_or_month_selection(mutation):
    dates = pd.to_datetime(["2014-01-31", "2014-02-28", "2014-03-31"])
    exposures, returns = _surface_inputs(dates=dates)
    if mutation == "missing_key":
        returns = returns.drop(index=returns.index[0])
    elif mutation == "duplicate_key":
        returns = pd.concat([returns, returns.iloc[[0]]], ignore_index=True)
    else:
        returns = returns[~returns["formation_date"].eq(dates[1])]

    with pytest.raises(DataBlocked):
        build_hard_sort_surface(exposures, returns)


def test_hard_sort_surface_rejects_common_missing_formation_month():
    dates = pd.to_datetime(["2014-01-31", "2014-02-28", "2014-03-31"])
    exposures, returns = _surface_inputs(dates=dates)
    exposures = exposures[~exposures["formation_date"].eq(dates[1])]
    returns = returns[~returns["formation_date"].eq(dates[1])]

    with pytest.raises(DataBlocked, match="continuous.*calendar month"):
        build_hard_sort_surface(exposures, returns)


def test_hard_sort_surface_requires_full_and_model_formation_grids_to_match():
    dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(dates=dates)
    exposures.loc[
        exposures["formation_date"].eq(dates[1]),
        "universe_role",
    ] = "size_only"
    returns = returns[returns["formation_date"].eq(dates[0])]

    with pytest.raises(DataBlocked, match="full.*model.*formation"):
        build_hard_sort_surface(exposures, returns)


def test_hard_sort_surface_rejects_multiple_formations_in_one_calendar_month():
    dates = pd.to_datetime(["2014-01-30", "2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(dates=dates)

    with pytest.raises(DataBlocked, match="one formation.*calendar month"):
        build_hard_sort_surface(exposures, returns)


def test_hard_sort_surface_allows_only_the_unrealized_final_formation_to_be_absent():
    dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(dates=dates)
    returns = returns[returns["formation_date"].eq(dates[0])]

    got = build_hard_sort_surface(exposures, returns)

    assert set(got["formation_date"]) == {dates[0]}


def test_hard_sort_surface_rejects_return_for_unproven_final_holding_period():
    dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(
        dates=dates,
        include_final_return=True,
    )

    with pytest.raises(DataBlocked, match="exactly one unrealized final"):
        build_hard_sort_surface(exposures, returns)


def test_structure_coefficients_freeze_schema_windows_and_verdict_flags():
    dates = pd.date_range(
        "2014-01-31",
        "2024-02-29",
        freq="ME",
    )
    policies = (
        "legal_deadline",
        "legal_deadline_plus_one_month_end",
    )
    exposures, returns = _surface_inputs(
        dates=dates,
        policies=policies,
        names_per_cell=1,
    )

    got = build_structure_coefficients(
        exposures,
        returns,
        load_b3_config(),
    )

    assert list(got.columns) == [
        "pit_policy",
        "row_type",
        "formation_date",
        "window",
        "alpha",
        "beta_s",
        "beta_m",
        "beta_h",
        "n",
        "ordinary_t_beta_h",
        "nw_lag3_t_beta_h",
        "affects_verdict",
    ]
    for policy, policy_frame in got.groupby("pit_policy"):
        monthly = policy_frame[policy_frame["row_type"].eq("monthly")]
        summary = policy_frame[policy_frame["row_type"].eq("summary")]
        assert len(monthly) == 121
        assert list(summary["window"]) == [
            "2014-2017",
            "2018-2020",
            "2021-2023",
            "2024-2026-report-only",
        ]
        assert list(summary["affects_verdict"]) == [True, True, True, False]
        assert list(summary["n"]) == [48, 36, 36, 1]
        assert np.allclose(summary["beta_h"], 0.03, atol=1.0e-10)
        assert monthly.loc[
            monthly["window"].eq("2024-2026-report-only"),
            "affects_verdict",
        ].eq(False).all()


def _write_structure_parents(tmp_path, cfg, data_end):
    policies = tuple(cfg["pit"]["policies"])
    dates = pd.to_datetime(["2014-01-31", "2014-02-28"])
    exposures, returns = _surface_inputs(dates=dates, policies=policies)

    exposure_path = tmp_path / "monthly_exposures.csv.gz"
    exposures.to_csv(
        exposure_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [exposure_path],
        "OK",
        [],
    )

    axis_path = tmp_path / "axis_returns.csv"
    axis = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2014-02-03"),
                "pit_policy": policy,
                "style": 0.01,
                "size": -0.01,
                "interaction": 0.005,
            }
            for policy in policies
        ]
    )
    axis.to_csv(axis_path, index=False)

    leg_path = tmp_path / "conditional_leg_returns.csv"
    legs = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2014-02-03"),
                "pit_policy": policy,
                "q": q,
                "growth_ret": 0.01,
                "value_ret": 0.005,
            }
            for policy in policies
            for q in ("qblend", "q500", "q1000")
        ]
    )
    legs.to_csv(leg_path, index=False)

    period_path = tmp_path / "stock_period_returns.csv.gz"
    returns.to_csv(
        period_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "portfolios",
        cfg,
        data_end,
        [axis_path, leg_path, period_path],
        "OK",
        [],
    )

    state_path = tmp_path / "state_components.csv"
    state_rows = []
    for policy in policies:
        for q in ("qblend", "q500", "q1000"):
            state_rows.append(
                {
                    "date": pd.Timestamp("2014-02-03"),
                    "pit_policy": policy,
                    "q": q,
                    "growth_ret": 0.01,
                    "value_ret": 0.005,
                    "g": np.log1p(0.01),
                    "v": np.log1p(0.005),
                    "d": np.log1p(0.01) - np.log1p(0.005),
                    "d_UU": np.log1p(0.01) - np.log1p(0.005),
                    "d_DD": 0.0,
                    "d_DIV": 0.0,
                    "state": "UU",
                    "raw_U": np.nan,
                    "F_U": np.nan,
                    "raw_D": np.nan,
                    "F_D": np.nan,
                    "raw_X": np.nan,
                    "F_X": np.nan,
                    "raw_T": np.nan,
                    "F_T": np.nan,
                    "external_market_direction": "up",
                }
            )
    pd.DataFrame(state_rows).to_csv(state_path, index=False)
    _write_stage_manifest(
        tmp_path,
        "states",
        cfg,
        data_end,
        [state_path],
        "OK",
        [],
    )
    return {
        "exposures": exposure_path,
        "axis": axis_path,
        "legs": leg_path,
        "periods": period_path,
        "states": state_path,
    }, exposures, returns


def _stub_model_comparison(*args, **kwargs):
    del args, kwargs
    rows = []
    for gate_name, gate_pass in (("", np.nan), ("PIT_POLICY_FLIP", True)):
        row = {
            column: np.nan for column in MODEL_COMPARISON_COLUMNS
        }
        row.update(
            {
                "pit_policy": "ALL" if gate_name else "legal_deadline",
                "candidate": "" if gate_name else "B3_unified",
                "q": "" if gate_name else "qblend",
                "target": "" if gate_name else "blend",
                "window": "run" if gate_name else "2021-2023",
                "model": "" if gate_name else "M1",
                "gate_name": gate_name,
                "gate_pass": gate_pass,
                "is_in_sample": False,
                "affects_verdict": True,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=MODEL_COMPARISON_COLUMNS)


def _run_short_structure(cfg, data_end, research_dir, backtest_dir):
    index = pd.DatetimeIndex([pd.Timestamp(data_end)])
    targets = {
        target: pd.Series(0.0, index=index)
        for target in ("blend", "500", "1000")
    }
    control = pd.Series(0.0, index=index)
    return run_structure(
        cfg,
        data_end,
        research_dir,
        backtest_dir,
        target_returns=targets,
        equal_weight_signal=control,
        model_comparison_builder=_stub_model_comparison,
    )


def test_structure_runner_hash_checks_parents_and_writes_deterministic_outputs(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"

    result = _run_short_structure(cfg, data_end, tmp_path, compact_dir)

    assert result.status == "OK"
    assert result.surface_path == tmp_path / "hard_sort_surface.csv"
    assert result.coefficients_path == compact_dir / "structure_coefficients.csv"
    assert result.model_path == compact_dir / "model_comparison.csv"
    surface = pd.read_csv(result.surface_path, parse_dates=["formation_date"])
    coefficients = pd.read_csv(
        result.coefficients_path,
        parse_dates=["formation_date"],
    )
    model = pd.read_csv(result.model_path)
    assert len(surface) == 2 * 1 * 45
    assert len(coefficients) == 2 * (1 + 4)
    assert list(model.columns) == MODEL_COMPARISON_COLUMNS
    first_surface = result.surface_path.read_bytes()
    first_coefficients = result.coefficients_path.read_bytes()
    first_model = result.model_path.read_bytes()

    repeated = _run_short_structure(cfg, data_end, tmp_path, compact_dir)

    assert repeated.status == "OK"
    assert repeated.surface_path.read_bytes() == first_surface
    assert repeated.coefficients_path.read_bytes() == first_coefficients
    assert repeated.model_path.read_bytes() == first_model


@pytest.mark.parametrize("artifact", ["exposures", "periods", "states"])
def test_structure_runner_rejects_tampered_parent_and_removes_stale_outputs(
    tmp_path,
    artifact,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    _run_short_structure(cfg, data_end, tmp_path, compact_dir)
    paths[artifact].write_bytes(paths[artifact].read_bytes() + b"tampered")

    with pytest.raises(DataBlocked, match="hash"):
        _run_short_structure(cfg, data_end, tmp_path, compact_dir)

    assert not (tmp_path / "hard_sort_surface.csv").exists()
    assert not (compact_dir / "structure_coefficients.csv").exists()
    assert not (compact_dir / "model_comparison.csv").exists()


def test_structure_runner_invalidates_outputs_before_config_validation(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    result = _run_short_structure(cfg, data_end, tmp_path, compact_dir)
    surface_temp = result.surface_path.with_name(
        f".{result.surface_path.name}.tmp"
    )
    coefficients_temp = result.coefficients_path.with_name(
        f".{result.coefficients_path.name}.tmp"
    )
    model_temp = result.model_path.with_name(
        f".{result.model_path.name}.tmp"
    )
    surface_temp.write_text("stale", encoding="utf-8")
    coefficients_temp.write_text("stale", encoding="utf-8")
    model_temp.write_text("stale", encoding="utf-8")
    invalid_cfg = deepcopy(cfg)
    invalid_cfg["model"]["newey_west_lag"] = 2

    with pytest.raises(DataBlocked, match="Newey-West"):
        _run_short_structure(invalid_cfg, data_end, tmp_path, compact_dir)

    assert not result.surface_path.exists()
    assert not result.coefficients_path.exists()
    assert not result.model_path.exists()
    assert not surface_temp.exists()
    assert not coefficients_temp.exists()
    assert not model_temp.exists()


def test_structure_runner_validates_injected_model_output_contract(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    index = pd.DatetimeIndex([data_end])
    targets = {
        target: pd.Series(0.0, index=index)
        for target in ("blend", "500", "1000")
    }

    def invalid_builder(*args, **kwargs):
        del args, kwargs
        return _stub_model_comparison().drop(columns="gate_pass")

    with pytest.raises(DataBlocked, match="model comparison.*schema"):
        run_structure(
            cfg,
            data_end,
            tmp_path,
            compact_dir,
            target_returns=targets,
            equal_weight_signal=pd.Series(0.0, index=index),
            model_comparison_builder=invalid_builder,
        )

    assert not (tmp_path / "hard_sort_surface.csv").exists()
    assert not (compact_dir / "structure_coefficients.csv").exists()
    assert not (compact_dir / "model_comparison.csv").exists()


def test_structure_runner_third_write_failure_removes_all_outputs(
    tmp_path,
    monkeypatch,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    original = pd.DataFrame.to_csv

    def failing_to_csv(frame, path, *args, **kwargs):
        if str(path).endswith(".model_comparison.csv.tmp"):
            raise OSError("model write failed")
        return original(frame, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_csv", failing_to_csv)

    with pytest.raises(OSError, match="model write failed"):
        _run_short_structure(cfg, data_end, tmp_path, compact_dir)

    for path in (
        tmp_path / "hard_sort_surface.csv",
        compact_dir / "structure_coefficients.csv",
        compact_dir / "model_comparison.csv",
        tmp_path / ".hard_sort_surface.csv.tmp",
        compact_dir / ".structure_coefficients.csv.tmp",
        compact_dir / ".model_comparison.csv.tmp",
    ):
        assert not path.exists()


def test_structure_runner_rejects_incomplete_cutoff_month_formation(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-14")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    policies = tuple(cfg["pit"]["policies"])
    exposures, returns = _surface_inputs(
        dates=pd.to_datetime(
            ["2014-01-31", "2014-02-28", "2014-03-14"]
        ),
        policies=policies,
    )
    exposures.to_csv(
        paths["exposures"],
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    returns.to_csv(
        paths["periods"],
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [paths["exposures"]],
        "OK",
        [],
    )
    _write_stage_manifest(
        tmp_path,
        "portfolios",
        cfg,
        data_end,
        [paths["axis"], paths["legs"], paths["periods"]],
        "OK",
        [],
    )
    compact_dir = tmp_path / "compact"
    compact_dir.mkdir()
    surface_path = tmp_path / "hard_sort_surface.csv"
    coefficients_path = compact_dir / "structure_coefficients.csv"
    model_path = compact_dir / "model_comparison.csv"
    surface_temp = surface_path.with_name(f".{surface_path.name}.tmp")
    coefficients_temp = coefficients_path.with_name(
        f".{coefficients_path.name}.tmp"
    )
    model_temp = model_path.with_name(f".{model_path.name}.tmp")
    for path in (
        surface_path,
        coefficients_path,
        model_path,
        surface_temp,
        coefficients_temp,
        model_temp,
    ):
        path.write_text("stale", encoding="utf-8")

    with pytest.raises(DataBlocked, match="incomplete.*formation"):
        _run_short_structure(cfg, data_end, tmp_path, compact_dir)

    assert not surface_path.exists()
    assert not coefficients_path.exists()
    assert not model_path.exists()
    assert not surface_temp.exists()
    assert not coefficients_temp.exists()
    assert not model_temp.exists()


@pytest.mark.parametrize("artifact", ["axis", "states"])
def test_structure_runner_reads_and_validates_auxiliary_caches(
    tmp_path,
    artifact,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    if artifact == "axis":
        frame = pd.read_csv(paths["axis"]).drop(columns="interaction")
        frame.to_csv(paths["axis"], index=False)
        _write_stage_manifest(
            tmp_path,
            "portfolios",
            cfg,
            data_end,
            [paths["axis"], paths["legs"], paths["periods"]],
            "OK",
            [],
        )
    else:
        frame = pd.read_csv(paths["states"]).drop(
            columns="external_market_direction"
        )
        frame.to_csv(paths["states"], index=False)
        _write_stage_manifest(
            tmp_path,
            "states",
            cfg,
            data_end,
            [paths["states"]],
            "OK",
            [],
        )

    with pytest.raises(DataBlocked, match=artifact):
        _run_short_structure(cfg, data_end, tmp_path, tmp_path / "compact")


def test_structure_runner_blocks_rehashed_primary_cache_with_missing_schema(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    malformed = pd.read_csv(paths["exposures"]).drop(
        columns="formation_date"
    )
    malformed.to_csv(
        paths["exposures"],
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [paths["exposures"]],
        "OK",
        [],
    )

    with pytest.raises(DataBlocked, match="monthly exposures"):
        _run_short_structure(cfg, data_end, tmp_path, tmp_path / "compact")


def test_structure_runner_requires_axis_and_state_daily_grids_to_match(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    states = pd.read_csv(paths["states"])
    extra = states.copy()
    extra["date"] = "2014-02-04"
    pd.concat([states, extra], ignore_index=True).to_csv(
        paths["states"],
        index=False,
    )
    _write_stage_manifest(
        tmp_path,
        "states",
        cfg,
        data_end,
        [paths["states"]],
        "OK",
        [],
    )

    with pytest.raises(DataBlocked, match="axis.*states|states.*axis"):
        _run_short_structure(cfg, data_end, tmp_path, tmp_path / "compact")


def test_structure_coefficients_require_both_frozen_pit_policies():
    cfg = load_b3_config()
    exposures, returns = _surface_inputs(
        dates=["2014-01-31", "2014-02-28"]
    )

    with pytest.raises(DataBlocked, match="policy"):
        build_structure_coefficients(exposures, returns, cfg)


def test_structure_cli_returns_data_blocked_when_states_parent_is_absent(
    tmp_path,
):
    compact_dir = tmp_path / "compact"

    exit_code = main(
        [
            "--data-end",
            "2014-03-31",
            "--research-output-dir",
            str(tmp_path),
            "--backtest-output-dir",
            str(compact_dir),
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "hard_sort_surface.csv").exists()
    assert not (compact_dir / "structure_coefficients.csv").exists()
    assert not (compact_dir / "model_comparison.csv").exists()


def test_equal_weight_control_file_freezes_schema_keys_and_cutoff(tmp_path):
    path = tmp_path / "equal_weight_signal_20d40z.csv"
    pd.DataFrame(
        {
            "date": ["2014-01-31", "2014-02-28", "2014-03-31"],
            "factor_value": [0.1, -0.2, 0.3],
        }
    ).to_csv(path, index=False)

    got = _load_equal_weight_control(path, pd.Timestamp("2014-02-28"))

    assert list(got.index) == list(
        pd.to_datetime(["2014-01-31", "2014-02-28"])
    )
    assert list(got) == pytest.approx([0.1, -0.2])
    duplicated = pd.read_csv(path)
    duplicated = pd.concat([duplicated, duplicated.iloc[[0]]])
    duplicated.to_csv(path, index=False)
    with pytest.raises(DataBlocked, match="duplicate"):
        _load_equal_weight_control(path, pd.Timestamp("2014-03-31"))


def test_structure_cli_uses_default_cash_loader_and_equal_weight_path(
    tmp_path,
    monkeypatch,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    index = pd.DatetimeIndex([data_end])
    calls = []

    def loader(target, start=None, db=None):
        del start, db
        calls.append(target)
        return pd.Series(0.0, index=index)

    control_paths = []

    def load_control(path, cutoff):
        control_paths.append((path, cutoff))
        return pd.Series(0.0, index=index)

    monkeypatch.setattr("backtest.data.load_underlying_returns", loader)
    monkeypatch.setattr(
        "backtest.b3_structure._load_equal_weight_control",
        load_control,
    )
    monkeypatch.setattr(
        "backtest.b3_structure.build_model_comparison",
        _stub_model_comparison,
    )

    exit_code = main(
        [
            "--data-end",
            str(data_end.date()),
            "--research-output-dir",
            str(tmp_path),
            "--backtest-output-dir",
            str(tmp_path / "compact"),
        ]
    )

    assert exit_code == 0
    assert calls == ["blend", "500", "1000"]
    assert len(control_paths) == 1
    assert str(control_paths[0][0]).endswith(
        "output/equal_weight/equal_weight_signal_20d40z.csv"
    )
    assert control_paths[0][1] == data_end
    assert (tmp_path / "compact" / "model_comparison.csv").is_file()


def test_structure_runner_reports_coverage_blocked_but_writes_full_audit(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, exposures, _ = _write_structure_parents(tmp_path, cfg, data_end)
    exposures["s_perp"] = exposures["m_perp"] ** 3
    exposures.to_csv(
        paths["exposures"],
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [paths["exposures"]],
        "OK",
        [],
    )

    result = _run_short_structure(
        cfg,
        data_end,
        tmp_path,
        tmp_path / "compact",
    )

    assert result.status == "COVERAGE_BLOCKED"
    surface = pd.read_csv(result.surface_path)
    assert surface["status"].eq("COVERAGE_BLOCKED").any()
    assert result.coefficients_path.is_file()
    assert result.model_path.is_file()


def _monthly_model_frame() -> pd.DataFrame:
    dates = pd.date_range("2014-01-31", "2026-12-31", freq="ME")
    rng = np.random.default_rng(23)
    frame = pd.DataFrame(index=dates)
    frame["F_U"] = rng.normal(size=len(frame))
    frame["F_D"] = rng.normal(size=len(frame))
    frame["F_X"] = rng.normal(size=len(frame))
    frame["F_T"] = rng.normal(size=len(frame))
    frame["target"] = (
        0.35 * frame["F_U"]
        - 0.20 * frame["F_D"]
        + 0.15 * frame["F_X"]
        + rng.normal(scale=0.05, size=len(frame))
    )
    return frame


def test_discovery_coefficients_are_frozen_and_m1_beats_m0_oos():
    frame = _monthly_model_frame()
    train = frame.loc[:"2020-12-31"]
    confirm = frame.loc["2021-01-01":"2023-12-31"]

    m0 = fit_model(train, ["F_T"], "target")
    m1 = fit_model(train, ["F_U", "F_D", "F_X"], "target")
    pred0 = apply_model(confirm, m0)
    pred1 = apply_model(confirm, m1)

    assert isinstance(m1, ModelFit)
    assert m1.features == ("F_U", "F_D", "F_X")
    assert oos_r_squared(
        confirm["target"], pred1, m1.train_target_mean
    ) > oos_r_squared(confirm["target"], pred0, m0.train_target_mean)
    changed = confirm.copy()
    changed["target"] *= -100.0
    assert fit_model(train, ["F_U", "F_D", "F_X"], "target") == m1


def test_model_fit_requires_full_rank_and_scores_without_intercept():
    frame = pd.DataFrame(
        {
            "x": [-2.0, -1.0, 1.0, 2.0],
            "target": [-3.0, -1.0, 3.0, 5.0],
        }
    )
    model = fit_model(frame, ["x"], "target")

    assert model.intercept == pytest.approx(1.0)
    assert model.slopes == pytest.approx((2.0,))
    assert list(apply_model(frame, model)) == pytest.approx(
        [-4.0, -2.0, 2.0, 4.0]
    )
    assert list(
        apply_model(frame, model, include_intercept=True)
    ) == pytest.approx(frame["target"])
    with pytest.raises(RuntimeError, match="insufficient"):
        fit_model(frame.iloc[:2], ["x"], "target")
    rank_deficient = frame.assign(copy=frame["x"])
    with pytest.raises(RuntimeError, match="rank"):
        fit_model(rank_deficient, ["x", "copy"], "target")


def test_stability_and_three_window_state_coverage_are_hard_gates():
    dates = pd.bdate_range("2021-01-01", periods=300)
    features = pd.DataFrame(
        {
            "F_U": np.linspace(-1.0, 1.0, 300),
            "F_D": np.sin(np.arange(300) / 20.0),
            "F_X": np.cos(np.arange(300) / 25.0),
        },
        index=dates,
    )
    stable = stability_gate(
        np.array([1.0, 0.5, -0.2]),
        np.array([0.9, 0.4, -0.1]),
        features,
        min_score_spearman=0.50,
    )
    reversed_fit = stability_gate(
        np.array([1.0, 0.5, -0.2]),
        np.array([-1.0, -0.5, 0.2]),
        features,
        min_score_spearman=0.50,
    )
    assert stable["pass"]
    assert stable["cosine"] > 0.0
    assert stable["score_spearman"] >= 0.50
    assert not reversed_fit["pass"]

    coverage_dates = []
    coverage_states = []
    for year in (2014, 2018, 2021):
        for number, state in enumerate(("UU", "DD", "DIV") * 4):
            coverage_dates.append(
                pd.Timestamp(year=year, month=1, day=number + 1)
            )
            coverage_states.append(state)
    coverage = pd.Series(
        coverage_states,
        index=pd.DatetimeIndex(coverage_dates),
        name="state",
    )
    passed = state_coverage_gate(coverage, 0.10)
    failed = state_coverage_gate(
        coverage[coverage.ne("DIV")],
        0.10,
    )

    assert passed["pass"]
    assert passed["2014-2017_DIV"] == pytest.approx(1.0 / 3.0)
    assert not failed["pass"]


def test_state_coverage_rejects_unordered_dates():
    state = pd.Series(
        ["UU", "DD", "DIV"],
        index=pd.DatetimeIndex(
            ["2014-01-02", "2014-01-01", "2014-01-03"]
        ),
    )

    with pytest.raises(DataBlocked, match="monotonic"):
        state_coverage_gate(state, 0.10)


def test_next_formation_targets_are_nonoverlapping_and_omit_final():
    dates = pd.bdate_range("2021-01-29", "2021-02-05")
    daily = pd.Series(
        [0.0, 0.01, 0.02, 0.03, 0.04, 0.05],
        index=dates,
    )
    formations = pd.DatetimeIndex([dates[0], dates[2], dates[5]])

    got = next_formation_targets(daily, formations)

    assert list(got.index) == list(formations[:-1])
    assert got.iloc[0] == pytest.approx(1.01 * 1.02 - 1.0)
    assert got.iloc[1] == pytest.approx(1.03 * 1.04 * 1.05 - 1.0)


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "nan"])
def test_next_formation_targets_fail_closed_on_invalid_inputs(mutation):
    dates = pd.bdate_range("2021-01-29", periods=5)
    daily = pd.Series(0.01, index=dates)
    formations = pd.DatetimeIndex([dates[0], dates[2], dates[4]])
    if mutation == "duplicate":
        formations = pd.DatetimeIndex([dates[0], dates[2], dates[2]])
    elif mutation == "missing":
        formations = pd.DatetimeIndex(
            [dates[0], pd.Timestamp("2021-01-30"), dates[4]]
        )
    else:
        daily.loc[dates[1]] = np.nan

    with pytest.raises((DataBlocked, RuntimeError)):
        next_formation_targets(daily, formations)


def test_report_only_mutation_cannot_change_frozen_model():
    frame = _monthly_model_frame()
    train = frame.loc["2014-01-01":"2020-12-31"]
    baseline = fit_model(train, ["F_U", "F_D", "F_X"], "target")
    mutated = frame.copy()
    report_dates = mutated.index >= pd.Timestamp("2024-01-01")
    mutated.loc[
        report_dates,
        ["F_U", "F_D", "F_X", "F_T", "target"],
    ] = 1.0e9

    changed = fit_model(
        mutated.loc["2014-01-01":"2020-12-31"],
        ["F_U", "F_D", "F_X"],
        "target",
    )

    assert baseline == changed


def _model_comparison_inputs():
    cfg = load_b3_config()
    policies = tuple(cfg["pit"]["policies"])
    calendar = pd.bdate_range("2014-01-01", "2026-12-31")
    formations = pd.DatetimeIndex(
        pd.Series(calendar, index=calendar)
        .groupby(calendar.to_period("M"))
        .max()
    )
    number = np.arange(len(calendar), dtype=float)
    base = pd.DataFrame(
        {
            "F_U": np.sin(number / 17.0) + 0.2 * np.cos(number / 53.0),
            "F_D": np.cos(number / 23.0) - 0.1 * np.sin(number / 41.0),
            "F_X": np.sin(number / 31.0) + np.cos(number / 47.0),
            "F_T": np.cos(number / 13.0) + 0.1 * np.sin(number / 7.0),
        },
        index=calendar,
    )
    q_scale = {"qblend": 1.0, "q500": 0.8, "q1000": 1.2}
    features = {
        q: base.mul(scale).assign(
            F_T=base["F_T"] * (2.0 - scale / 2.0)
        )
        for q, scale in q_scale.items()
    }
    labels = np.asarray(["UU", "DD", "DIV"])[
        np.arange(len(calendar)) % 3
    ]
    state_rows = []
    for policy in policies:
        for q in ("qblend", "q500", "q1000"):
            q_features = features[q]
            for offset, date in enumerate(calendar):
                label = str(labels[offset])
                d_uu = 0.01 if label == "UU" else 0.0
                d_dd = -0.01 if label == "DD" else 0.0
                d_div = 0.005 if label == "DIV" else 0.0
                state_rows.append(
                    {
                        "date": date,
                        "pit_policy": policy,
                        "q": q,
                        "growth_ret": 0.001,
                        "value_ret": 0.0,
                        "g": d_uu + d_dd + d_div,
                        "v": 0.0,
                        "d": d_uu + d_dd + d_div,
                        "d_UU": d_uu,
                        "d_DD": d_dd,
                        "d_DIV": d_div,
                        "state": label,
                        "raw_U": float(q_features.loc[date, "F_U"]),
                        "F_U": float(q_features.loc[date, "F_U"]),
                        "raw_D": float(q_features.loc[date, "F_D"]),
                        "F_D": float(q_features.loc[date, "F_D"]),
                        "raw_X": float(q_features.loc[date, "F_X"]),
                        "F_X": float(q_features.loc[date, "F_X"]),
                        "raw_T": float(q_features.loc[date, "F_T"]),
                        "F_T": float(q_features.loc[date, "F_T"]),
                        "external_market_direction": "up",
                    }
                )
    states = pd.DataFrame(state_rows)

    target_returns = {}
    target_q = {"blend": "qblend", "500": "q500", "1000": "q1000"}
    for target, q in target_q.items():
        daily = pd.Series(0.0, index=calendar, name=target)
        formation_features = features[q].reindex(formations)
        monthly = (
            0.012 * formation_features["F_U"]
            - 0.008 * formation_features["F_D"]
            + 0.006 * formation_features["F_X"]
        )
        for start, end in zip(formations[:-1], formations[1:]):
            daily.loc[end] = monthly.loc[start]
        target_returns[target] = daily

    style = pd.Series(0.0, index=calendar)
    interaction = pd.Series(0.0, index=calendar)
    for offset, (_, end) in enumerate(
        zip(formations[:-1], formations[1:])
    ):
        style.loc[end] = 0.004 * np.sin(offset / 2.0)
        interaction.loc[end] = 0.003 * np.cos(offset / 3.0)
    axis = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": calendar,
                    "pit_policy": policy,
                    "style": style.to_numpy(),
                    "size": 0.0,
                    "interaction": interaction.to_numpy(),
                }
            )
            for policy in policies
        ],
        ignore_index=True,
    )

    coefficient_rows = []
    for policy in policies:
        for window in ("2014-2017", "2018-2020", "2021-2023"):
            coefficient_rows.append(
                {
                    "pit_policy": policy,
                    "row_type": "summary",
                    "formation_date": pd.NaT,
                    "window": window,
                    "alpha": 0.0,
                    "beta_s": 0.01,
                    "beta_m": 0.02,
                    "beta_h": 0.03,
                    "n": 36,
                    "ordinary_t_beta_h": 2.0,
                    "nw_lag3_t_beta_h": 2.0,
                    "affects_verdict": True,
                }
            )
        for formation in formations[:-1]:
            if formation <= pd.Timestamp("2017-12-31"):
                window = "2014-2017"
                affects_verdict = True
            elif formation <= pd.Timestamp("2020-12-31"):
                window = "2018-2020"
                affects_verdict = True
            elif formation <= pd.Timestamp("2023-12-31"):
                window = "2021-2023"
                affects_verdict = True
            else:
                window = "2024-2026-report-only"
                affects_verdict = False
            coefficient_rows.append(
                {
                    "pit_policy": policy,
                    "row_type": "monthly",
                    "formation_date": formation,
                    "window": window,
                    "alpha": 0.0,
                    "beta_s": 0.01,
                    "beta_m": 0.02,
                    "beta_h": 0.03,
                    "n": 100,
                    "ordinary_t_beta_h": np.nan,
                    "nw_lag3_t_beta_h": np.nan,
                    "affects_verdict": affects_verdict,
                }
            )
    coefficients = pd.DataFrame(coefficient_rows)

    surface_rows = []
    required_formations = formations[
        (formations >= pd.Timestamp("2014-01-01"))
        & (formations <= pd.Timestamp("2023-12-31"))
    ]
    cells = [("2x3", cell) for cell in (
        "big_value",
        "big_middle",
        "big_growth",
        "small_value",
        "small_middle",
        "small_growth",
    )] + [
        ("5x5", f"S{size}_V{style_number}")
        for size in range(1, 6)
        for style_number in range(1, 6)
    ]
    for policy in policies:
        for formation in required_formations:
            for grid, cell in cells:
                surface_rows.append(
                    {
                        "pit_policy": policy,
                        "formation_date": formation,
                        "row_type": "cell",
                        "grid": grid,
                        "cell": cell,
                        "diagnostic": "",
                        "member_count": 4,
                        "industry_distribution": "{}",
                        "formation_coverage": 1.0,
                        "holding_return": 0.0,
                        "status": "OK",
                    }
                )
    surface = pd.DataFrame(surface_rows)
    control = pd.Series(
        np.sin(number / 11.0),
        index=calendar,
        name="factor_value",
    )
    return (
        states,
        axis,
        coefficients,
        surface,
        target_returns,
        control,
        formations,
        cfg,
    )


def _build_synthetic_model_comparison(**overrides):
    inputs = list(_model_comparison_inputs())
    names = {
        "state_components": 0,
        "axis_returns": 1,
        "structure_coefficients": 2,
        "hard_sort_surface": 3,
        "target_returns": 4,
        "equal_weight_signal": 5,
        "formation_dates": 6,
        "cfg": 7,
    }
    for name, value in overrides.items():
        inputs[names[name]] = value
    return build_model_comparison(*inputs)


def test_model_comparison_freezes_exact_schema_mapping_and_unique_rows():
    got = _build_synthetic_model_comparison()

    assert list(got.columns) == MODEL_COMPARISON_COLUMNS
    assert not got.duplicated(MODEL_ROW_ID_COLUMNS).any()
    diagnostic = got[got["q"].ne("")]
    assert set(
        diagnostic[["candidate", "q", "target"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    ) == {
        ("B3_unified", "qblend", "blend"),
        ("B3_dual_target", "q500", "500"),
        ("B3_dual_target", "q1000", "1000"),
    }
    metric = got[got["gate_name"].eq("")]
    assert set(metric["model"]) == {"M0", "M1"}
    assert metric.loc[metric["model"].eq("M0"), "partial_ic"].isna().all()
    assert not got.loc[
        got["gate_name"].ne(""), "gate_pass"
    ].isna().any()
    assert got.loc[got["gate_name"].eq(""), "gate_pass"].isna().all()


def test_model_comparison_emits_public_leg_and_dual_and_gates():
    got = _build_synthetic_model_comparison()
    policies = load_b3_config()["pit"]["policies"]

    public = got[got["candidate"].eq("PUBLIC")]
    assert set(public["gate_name"]) == {
        "beta_h_same_sign",
        "interaction_axis_corr",
        "hard_sort_complete",
    }
    assert len(public) == len(policies) * 3
    assert public["gate_pass"].eq(True).all()

    candidate_gates = {"m1_increment", "partial_ic", "stability", "state_coverage"}
    aggregate = got[
        got["candidate"].isin({"B3_unified", "B3_dual_target"})
        & got["q"].eq("")
        & got["gate_name"].isin(candidate_gates)
    ]
    assert len(aggregate) == len(policies) * 2 * 4
    assert aggregate["gate_pass"].eq(True).all()

    states, axis, coefficients, surface, targets, control, formations, cfg = (
        _model_comparison_inputs()
    )
    mask = (
        states["q"].eq("q1000")
        & states["date"].between("2021-01-01", "2023-12-31")
    )
    states.loc[mask, "state"] = "UU"
    failed = build_model_comparison(
        states,
        axis,
        coefficients,
        surface,
        targets,
        control,
        formations,
        cfg,
    )
    q500 = failed[
        failed["q"].eq("q500")
        & failed["gate_name"].eq("state_coverage")
        & failed["window"].eq("2021-2023")
    ]
    q1000 = failed[
        failed["q"].eq("q1000")
        & failed["gate_name"].eq("state_coverage")
        & failed["window"].eq("2021-2023")
    ]
    dual = failed[
        failed["candidate"].eq("B3_dual_target")
        & failed["q"].eq("")
        & failed["gate_name"].eq("state_coverage")
    ]
    assert q500["gate_pass"].eq(True).all()
    assert q1000["gate_pass"].eq(False).all()
    assert dual["gate_pass"].eq(False).all()


def test_model_comparison_adds_one_run_level_pit_policy_flip_row():
    baseline = _build_synthetic_model_comparison()
    pit = baseline[baseline["gate_name"].eq("PIT_POLICY_FLIP")]
    assert len(pit) == 1
    assert pit.iloc[0]["pit_policy"] == "ALL"
    assert bool(pit.iloc[0]["gate_pass"])

    inputs = list(_model_comparison_inputs())
    coefficients = inputs[2].copy()
    policy = load_b3_config()["pit"]["policies"][1]
    coefficients.loc[
        coefficients["pit_policy"].eq(policy)
        & coefficients["row_type"].eq("monthly")
        & coefficients["formation_date"].between(
            "2021-01-01", "2023-12-31"
        ),
        "beta_h",
    ] = -0.03
    inputs[2] = coefficients

    changed = build_model_comparison(*inputs)
    pit = changed[changed["gate_name"].eq("PIT_POLICY_FLIP")]

    assert len(pit) == 1
    assert not bool(pit.iloc[0]["gate_pass"])
    assert bool(pit.iloc[0]["affects_verdict"])


@pytest.mark.filterwarnings("ignore:An input array is constant")
def test_model_comparison_handles_undefined_confirmation_oos():
    inputs = list(_model_comparison_inputs())
    inputs[4] = {
        name: pd.Series(0.0, index=series.index, name=series.name)
        for name, series in inputs[4].items()
    }

    got = build_model_comparison(*inputs)

    confirmation_metrics = got[
        got["window"].eq("2021-2023")
        & got["gate_name"].eq("")
        & got["model"].isin({"M0", "M1"})
    ]
    increments = got[got["gate_name"].eq("m1_increment")]
    pit = got[got["gate_name"].eq("PIT_POLICY_FLIP")]
    assert confirmation_metrics["oos_r2"].isna().all()
    assert increments["gate_pass"].eq(False).all()
    assert len(pit) == 1
    assert bool(pit.iloc[0]["gate_pass"])


def test_increment_direction_distinguishes_finite_from_undefined():
    undefined = b3_structure._increment_direction(float("nan"))

    assert undefined == "NONFINITE"
    assert undefined == b3_structure._increment_direction(float("inf"))
    assert undefined != b3_structure._increment_direction(0.25)
    assert b3_structure._increment_direction(-0.25) == -1


def test_model_comparison_report_mutation_cannot_change_verdict_rows():
    inputs = list(_model_comparison_inputs())
    baseline = build_model_comparison(*inputs)
    states = inputs[0].copy()
    report_state = states["date"].ge("2024-01-01")
    state_sequence = np.arange(int(report_state.sum()), dtype=float)
    states.loc[report_state, "F_U"] = 1.0e8 + state_sequence
    states.loc[report_state, "F_D"] = -2.0e8 + state_sequence**2
    states.loc[report_state, "F_X"] = 3.0e8 - state_sequence
    states.loc[report_state, "F_T"] = -4.0e8 - state_sequence**2
    targets = {name: series.copy() for name, series in inputs[4].items()}
    for series in targets.values():
        report_target = series.index >= pd.Timestamp("2024-01-01")
        series.loc[report_target] = np.linspace(
            0.40,
            0.60,
            int(report_target.sum()),
        )
    control = inputs[5].copy()
    report_control = control.index >= pd.Timestamp("2024-01-01")
    control.loc[report_control] = np.linspace(
        -1.0e9,
        1.0e9,
        int(report_control.sum()),
    )
    axis = inputs[1].copy()
    report_axis = axis["date"].ge("2024-01-01")
    axis_sequence = np.arange(int(report_axis.sum()), dtype=float)
    axis.loc[report_axis, "style"] = 1.0e8 + axis_sequence**2
    axis.loc[report_axis, "interaction"] = 1.0e8 - axis_sequence
    inputs[0] = states
    inputs[1] = axis
    inputs[4] = targets
    inputs[5] = control

    changed = build_model_comparison(*inputs)
    baseline_verdict = baseline[baseline["affects_verdict"]].sort_values(
        MODEL_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)
    changed_verdict = changed[changed["affects_verdict"]].sort_values(
        MODEL_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)

    pd.testing.assert_frame_equal(baseline_verdict, changed_verdict)
    assert not baseline.loc[
        baseline["window"].eq("2024-2026-report-only"),
        "affects_verdict",
    ].any()


def test_closed_formation_window_excludes_cross_boundary_periods():
    formations = pd.date_range("2020-10-31", "2021-02-28", freq="ME")
    frame = pd.DataFrame(
        {"value": np.arange(len(formations) - 1)},
        index=formations[:-1],
    )

    got = _closed_formation_window(
        frame,
        formations,
        "2020-01-01",
        "2020-12-31",
    )

    assert list(got.index) == list(formations[:2])


def test_post_2020_target_mutation_cannot_change_discovery_or_stability():
    inputs = list(_model_comparison_inputs())
    baseline = build_model_comparison(*inputs)
    targets = {name: series.copy() for name, series in inputs[4].items()}
    for series in targets.values():
        series.loc[series.index >= pd.Timestamp("2021-01-01")] = 0.50
    inputs[4] = targets

    changed = build_model_comparison(*inputs)
    protected = baseline["window"].isin(
        {"2014-2017", "2018-2020", "2014-2020"}
    ) | baseline["gate_name"].eq("stability")
    baseline_protected = baseline[protected].reset_index(drop=True)
    protected_keys = set(
        baseline_protected[MODEL_ROW_ID_COLUMNS].itertuples(
            index=False,
            name=None,
        )
    )
    changed_protected = changed[
        changed[MODEL_ROW_ID_COLUMNS].apply(tuple, axis=1).isin(
            protected_keys
        )
    ].reset_index(drop=True)

    pd.testing.assert_frame_equal(baseline_protected, changed_protected)


def test_beta_h_gate_recomputes_complete_monthly_windows_not_summary_rows():
    inputs = list(_model_comparison_inputs())
    coefficients = inputs[2].copy()
    policy = load_b3_config()["pit"]["policies"][1]
    coefficients.loc[
        coefficients["pit_policy"].eq(policy)
        & coefficients["row_type"].eq("summary"),
        "beta_h",
    ] = -0.03
    inputs[2] = coefficients

    got = build_model_comparison(*inputs)
    beta_gate = got[got["gate_name"].eq("beta_h_same_sign")]
    pit_gate = got[got["gate_name"].eq("PIT_POLICY_FLIP")]

    assert beta_gate["gate_pass"].eq(True).all()
    assert pit_gate["gate_pass"].eq(True).all()


@pytest.mark.parametrize(
    ("source", "mutation"),
    [
        ("target", "missing"),
        ("target", "duplicate"),
        ("control", "missing"),
        ("control", "duplicate"),
    ],
)
def test_model_comparison_rejects_incomplete_target_or_control_keys(
    source,
    mutation,
):
    inputs = list(_model_comparison_inputs())
    formation = inputs[6][10]
    if source == "target":
        targets = {name: series.copy() for name, series in inputs[4].items()}
        series = targets["blend"]
        if mutation == "missing":
            targets["blend"] = series.drop(index=formation)
        else:
            targets["blend"] = pd.concat([series, series.loc[[formation]]])
        inputs[4] = targets
    else:
        control = inputs[5]
        if mutation == "missing":
            control = control.drop(index=formation)
        else:
            control = pd.concat([control, control.loc[[formation]]])
        inputs[5] = control

    with pytest.raises(DataBlocked):
        build_model_comparison(*inputs)


def test_model_comparison_rejects_missing_nonformation_target_day():
    inputs = list(_model_comparison_inputs())
    targets = {name: series.copy() for name, series in inputs[4].items()}
    formations = inputs[6]
    between = targets["blend"].index[
        (targets["blend"].index > formations[10])
        & (targets["blend"].index < formations[11])
    ]
    targets["blend"] = targets["blend"].drop(index=between[0])
    inputs[4] = targets

    with pytest.raises(DataBlocked, match="daily grid"):
        build_model_comparison(*inputs)


def _write_full_model_parents(tmp_path):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2024-01-31")
    (
        states,
        axis,
        _,
        _,
        targets,
        control,
        formations,
        _,
    ) = _model_comparison_inputs()
    formations = formations[formations <= data_end]
    policies = tuple(cfg["pit"]["policies"])
    exposures, periods = _surface_inputs(
        dates=formations,
        policies=policies,
        names_per_cell=1,
    )
    exposure_path = tmp_path / "monthly_exposures.csv.gz"
    exposures.to_csv(
        exposure_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "exposures",
        cfg,
        data_end,
        [exposure_path],
        "OK",
        [],
    )

    axis = axis[axis["date"].le(data_end)].copy()
    axis_path = tmp_path / "axis_returns.csv"
    axis.to_csv(axis_path, index=False)
    states = states[states["date"].le(data_end)].copy()
    leg_path = tmp_path / "conditional_leg_returns.csv"
    states[
        ["date", "pit_policy", "q", "growth_ret", "value_ret"]
    ].to_csv(leg_path, index=False)
    period_path = tmp_path / "stock_period_returns.csv.gz"
    periods.to_csv(
        period_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        tmp_path,
        "portfolios",
        cfg,
        data_end,
        [axis_path, leg_path, period_path],
        "OK",
        [],
    )

    state_path = tmp_path / "state_components.csv"
    states.to_csv(state_path, index=False)
    _write_stage_manifest(
        tmp_path,
        "states",
        cfg,
        data_end,
        [state_path],
        "OK",
        [],
    )
    prepared_targets = {
        target: series.loc[:data_end].copy()
        for target, series in targets.items()
    }
    return (
        cfg,
        data_end,
        prepared_targets,
        control.loc[:data_end].copy(),
        {
            "exposures": exposure_path,
            "axis": axis_path,
            "legs": leg_path,
            "periods": period_path,
            "states": state_path,
        },
    )


def test_structure_runner_real_builder_uses_loader_and_is_deterministic(
    tmp_path,
):
    cfg, data_end, targets, control, _ = _write_full_model_parents(tmp_path)
    compact_dir = tmp_path / "compact"
    calls = []

    def loader(target):
        calls.append(target)
        return targets[target]

    result = run_structure(
        cfg,
        data_end,
        tmp_path,
        compact_dir,
        underlying_return_loader=loader,
        equal_weight_signal=control,
    )

    assert calls == ["blend", "500", "1000"]
    model = pd.read_csv(result.model_path)
    assert list(model.columns) == MODEL_COMPARISON_COLUMNS
    assert model["gate_name"].eq("PIT_POLICY_FLIP").sum() == 1
    first = tuple(
        path.read_bytes()
        for path in (
            result.surface_path,
            result.coefficients_path,
            result.model_path,
        )
    )
    calls.clear()

    repeated = run_structure(
        cfg,
        data_end,
        tmp_path,
        compact_dir,
        underlying_return_loader=loader,
        equal_weight_signal=control,
    )

    assert calls == ["blend", "500", "1000"]
    assert tuple(
        path.read_bytes()
        for path in (
            repeated.surface_path,
            repeated.coefficients_path,
            repeated.model_path,
        )
    ) == first


def test_structure_runner_real_builder_failure_removes_all_outputs(tmp_path):
    cfg, data_end, targets, control, paths = _write_full_model_parents(tmp_path)
    states = pd.read_csv(paths["states"])
    states["F_X"] = states["F_U"]
    states.to_csv(paths["states"], index=False)
    _write_stage_manifest(
        tmp_path,
        "states",
        cfg,
        data_end,
        [paths["states"]],
        "OK",
        [],
    )
    compact_dir = tmp_path / "compact"
    compact_dir.mkdir()
    stale_paths = (
        tmp_path / "hard_sort_surface.csv",
        compact_dir / "structure_coefficients.csv",
        compact_dir / "model_comparison.csv",
        tmp_path / ".hard_sort_surface.csv.tmp",
        compact_dir / ".structure_coefficients.csv.tmp",
        compact_dir / ".model_comparison.csv.tmp",
    )
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")

    with pytest.raises(RuntimeError, match="rank"):
        run_structure(
            cfg,
            data_end,
            tmp_path,
            compact_dir,
            target_returns=targets,
            equal_weight_signal=control,
        )

    assert not any(path.exists() for path in stale_paths)
