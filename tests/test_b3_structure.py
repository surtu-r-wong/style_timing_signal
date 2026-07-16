import json
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from backtest.b3_structure import (
    assign_hard_sort_cells,
    build_hard_sort_surface,
    build_structure_coefficients,
    fama_macbeth_coefficients,
    main,
    newey_west_mean_t,
    ordinary_mean_t,
    run_structure,
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


def test_structure_runner_hash_checks_parents_and_writes_deterministic_outputs(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"

    result = run_structure(cfg, data_end, tmp_path, compact_dir)

    assert result.status == "OK"
    assert result.surface_path == tmp_path / "hard_sort_surface.csv"
    assert result.coefficients_path == compact_dir / "structure_coefficients.csv"
    surface = pd.read_csv(result.surface_path, parse_dates=["formation_date"])
    coefficients = pd.read_csv(
        result.coefficients_path,
        parse_dates=["formation_date"],
    )
    assert len(surface) == 2 * 1 * 45
    assert len(coefficients) == 2 * (1 + 4)
    first_surface = result.surface_path.read_bytes()
    first_coefficients = result.coefficients_path.read_bytes()

    repeated = run_structure(cfg, data_end, tmp_path, compact_dir)

    assert repeated.status == "OK"
    assert repeated.surface_path.read_bytes() == first_surface
    assert repeated.coefficients_path.read_bytes() == first_coefficients


@pytest.mark.parametrize("artifact", ["exposures", "periods", "states"])
def test_structure_runner_rejects_tampered_parent_and_removes_stale_outputs(
    tmp_path,
    artifact,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    paths, _, _ = _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    run_structure(cfg, data_end, tmp_path, compact_dir)
    paths[artifact].write_bytes(paths[artifact].read_bytes() + b"tampered")

    with pytest.raises(DataBlocked, match="hash"):
        run_structure(cfg, data_end, tmp_path, compact_dir)

    assert not (tmp_path / "hard_sort_surface.csv").exists()
    assert not (compact_dir / "structure_coefficients.csv").exists()


def test_structure_runner_invalidates_outputs_before_config_validation(
    tmp_path,
):
    cfg = load_b3_config()
    data_end = pd.Timestamp("2014-03-31")
    _write_structure_parents(tmp_path, cfg, data_end)
    compact_dir = tmp_path / "compact"
    result = run_structure(cfg, data_end, tmp_path, compact_dir)
    surface_temp = result.surface_path.with_name(
        f".{result.surface_path.name}.tmp"
    )
    coefficients_temp = result.coefficients_path.with_name(
        f".{result.coefficients_path.name}.tmp"
    )
    surface_temp.write_text("stale", encoding="utf-8")
    coefficients_temp.write_text("stale", encoding="utf-8")
    invalid_cfg = deepcopy(cfg)
    invalid_cfg["model"]["newey_west_lag"] = 2

    with pytest.raises(DataBlocked, match="Newey-West"):
        run_structure(invalid_cfg, data_end, tmp_path, compact_dir)

    assert not result.surface_path.exists()
    assert not result.coefficients_path.exists()
    assert not surface_temp.exists()
    assert not coefficients_temp.exists()


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
    surface_temp = surface_path.with_name(f".{surface_path.name}.tmp")
    coefficients_temp = coefficients_path.with_name(
        f".{coefficients_path.name}.tmp"
    )
    for path in (
        surface_path,
        coefficients_path,
        surface_temp,
        coefficients_temp,
    ):
        path.write_text("stale", encoding="utf-8")

    with pytest.raises(DataBlocked, match="incomplete.*formation"):
        run_structure(cfg, data_end, tmp_path, compact_dir)

    assert not surface_path.exists()
    assert not coefficients_path.exists()
    assert not surface_temp.exists()
    assert not coefficients_temp.exists()


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
        run_structure(cfg, data_end, tmp_path, tmp_path / "compact")


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
        run_structure(cfg, data_end, tmp_path, tmp_path / "compact")


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
        run_structure(cfg, data_end, tmp_path, tmp_path / "compact")


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

    result = run_structure(
        cfg,
        data_end,
        tmp_path,
        tmp_path / "compact",
    )

    assert result.status == "COVERAGE_BLOCKED"
    surface = pd.read_csv(result.surface_path)
    assert surface["status"].eq("COVERAGE_BLOCKED").any()
    assert result.coefficients_path.is_file()
