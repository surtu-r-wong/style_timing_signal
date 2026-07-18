from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

import backtest.b3_eval as b3_eval_module
from backtest.b3_eval import (
    BOOTSTRAP_COLUMNS,
    BOOTSTRAP_ROW_ID_COLUMNS,
    PRODUCTION_METRICS_COLUMNS,
    PRODUCTION_ROW_ID_COLUMNS,
    YEARLY_COLUMNS,
    YEARLY_ROW_ID_COLUMNS,
    EvaluationFrames,
    _evaluation_config,
    _validate_bootstrap_output,
    _validate_production_output,
    _validate_yearly_output,
    build_evaluation,
    fit_frozen_m1_scores,
    holm_style_adjust,
    materialize_carry,
    paired_moving_block_tail,
    passes_tail_gate,
    two_leg_candidate_returns,
    yearly_contributions,
)
from backtest.b3_structure import (
    MODEL_COMPARISON_COLUMNS,
    apply_model,
    fit_model,
    next_formation_targets,
)
from backtest.engine import run_strategy
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover
from backtest.positions import production_position
from signals.style_basket.b3_config import load_b3_config
from signals.style_basket.b3_exposures import DataBlocked


@pytest.fixture(autouse=True)
def _fast_builder_bootstrap(monkeypatch):
    def fixed_result(candidate, baseline, block_days, draws, seed):
        assert candidate.index.equals(baseline.index)
        assert block_days == 20
        assert draws == 5000
        assert seed == 20260713
        return {
            "tail_prob": 200 / 5001,
            "ci05": 0.01,
            "ci50": 0.02,
            "ci95": 0.03,
        }

    monkeypatch.setattr(
        b3_eval_module,
        "paired_moving_block_tail",
        fixed_result,
    )


def test_eval_public_contract_is_importable():
    assert BOOTSTRAP_COLUMNS
    assert BOOTSTRAP_ROW_ID_COLUMNS
    assert PRODUCTION_METRICS_COLUMNS
    assert PRODUCTION_ROW_ID_COLUMNS
    assert YEARLY_COLUMNS
    assert YEARLY_ROW_ID_COLUMNS
    assert callable(EvaluationFrames)
    assert callable(build_evaluation)
    assert callable(fit_frozen_m1_scores)
    assert callable(holm_style_adjust)
    assert callable(materialize_carry)
    assert callable(paired_moving_block_tail)
    assert callable(passes_tail_gate)
    assert callable(two_leg_candidate_returns)
    assert callable(yearly_contributions)


def _market_inputs(periods=8):
    index = pd.bdate_range("2022-07-20", periods=periods)
    return_500 = pd.Series(
        np.linspace(-0.01, 0.012, periods),
        index=index,
    )
    return_1000 = pd.Series(
        np.linspace(0.009, -0.008, periods),
        index=index,
    )
    carry_500 = pd.Series(0.10, index=index)
    carry_1000 = pd.Series(0.05, index=index)
    return index, return_500, return_1000, carry_500, carry_1000


def test_two_leg_returns_are_exact_half_weight_existing_engine_results():
    index, r500, r1000, c500, c1000 = _market_inputs()
    position = pd.Series([0, 1, 1, 0, 1, 1, 0, 0], index=index)

    got = two_leg_candidate_returns(
        position,
        position,
        r500,
        r1000,
        c500,
        c1000,
        3.0,
    )
    expected = (
        0.5 * run_strategy(position, r500, 3.0, c500)["ret"]
        + 0.5 * run_strategy(position, r1000, 3.0, c1000)["ret"]
    ).rename("ret")

    pd.testing.assert_series_equal(got, expected, check_exact=True)
    assert got.index.equals(index)


def test_two_leg_returns_preserve_tplus1_and_cost_on_effective_change():
    index = pd.bdate_range("2021-01-01", periods=4)
    position = pd.Series([0, 1, 1, 0], index=index)
    r500 = pd.Series([0.0, 0.0, 0.10, 0.0], index=index)
    r1000 = pd.Series([0.0, 0.0, -0.02, 0.0], index=index)
    zero_carry = pd.Series(0.0, index=index)

    got = two_leg_candidate_returns(
        position,
        position,
        r500,
        r1000,
        zero_carry,
        zero_carry,
        3.0,
    )

    assert got.iloc[1] == 0.0
    assert got.iloc[2] == pytest.approx(0.0397)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-cash-day",
        "missing-position-day",
        "duplicate-date",
        "unsorted-date",
        "timezone",
        "non-normalized-date",
        "nonfinite",
        "loss-below-minus-one",
    ],
)
def test_two_leg_returns_reject_invalid_or_nonexact_grids(mutation):
    index, r500, r1000, c500, c1000 = _market_inputs()
    p500 = pd.Series(1, index=index)
    p1000 = pd.Series(1, index=index)
    if mutation == "missing-cash-day":
        r1000 = r1000.drop(index=index[3])
    elif mutation == "missing-position-day":
        p500 = p500.drop(index=index[3])
    elif mutation == "duplicate-date":
        p500.index = pd.DatetimeIndex([*index[:-1], index[-2]])
    elif mutation == "unsorted-date":
        p500 = p500.iloc[[1, 0, *range(2, len(p500))]]
    elif mutation == "timezone":
        p500.index = p500.index.tz_localize("Asia/Shanghai")
    elif mutation == "non-normalized-date":
        p500.index = p500.index + pd.Timedelta(hours=1)
    elif mutation == "nonfinite":
        c500.iloc[2] = np.inf
    elif mutation == "loss-below-minus-one":
        r500.iloc[2] = -1.0
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        two_leg_candidate_returns(
            p500,
            p1000,
            r500,
            r1000,
            c500,
            c1000,
            3.0,
        )


@pytest.mark.parametrize("cost_bps", [-1.0, np.nan, True])
def test_two_leg_returns_reject_invalid_cost(cost_bps):
    index, r500, r1000, c500, c1000 = _market_inputs()
    position = pd.Series(1, index=index)

    with pytest.raises(DataBlocked, match="cost"):
        two_leg_candidate_returns(
            position,
            position,
            r500,
            r1000,
            c500,
            c1000,
            cost_bps,
        )


def test_materialize_carry_zero_fills_prelaunch_and_crops_only_stale_tail():
    calendar = pd.bdate_range("2022-07-20", periods=8)
    raw = pd.Series(0.05, index=calendar[2:6])

    got = materialize_carry(
        raw,
        calendar,
        pd.Timestamp("2022-07-22"),
    )

    assert got.index.equals(calendar[:6])
    assert got.loc[calendar[:2]].eq(0.0).all()
    assert got.loc[calendar[2:6]].eq(0.05).all()


def test_materialize_carry_rejects_internal_postlaunch_gap():
    calendar = pd.bdate_range("2022-07-20", periods=8)
    raw = pd.Series(0.05, index=calendar[2:6]).drop(index=calendar[4])

    with pytest.raises(DataBlocked, match="internal post-launch carry gap"):
        materialize_carry(
            raw,
            calendar,
            pd.Timestamp("2022-07-22"),
        )


@pytest.mark.parametrize(
    "mutation",
    ["outside-calendar", "late-start", "nonfinite", "bad-launch"],
)
def test_materialize_carry_rejects_invalid_contracts(mutation):
    calendar = pd.bdate_range("2022-07-20", periods=8)
    launch = pd.Timestamp("2022-07-22")
    raw = pd.Series(0.05, index=calendar[2:6])
    if mutation == "outside-calendar":
        raw.loc[pd.Timestamp("2022-07-23")] = 0.05
        raw = raw.sort_index()
    elif mutation == "late-start":
        raw = raw.iloc[1:]
    elif mutation == "nonfinite":
        raw.iloc[1] = np.nan
    elif mutation == "bad-launch":
        launch = launch + pd.Timedelta(hours=1)
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        materialize_carry(raw, calendar, launch)


def test_paired_moving_block_tail_is_reproducible_and_detects_dominance():
    index = pd.bdate_range("2021-01-01", periods=240)
    baseline = pd.Series(np.tile([-0.001, 0.001], 120), index=index)
    candidate = baseline + 0.0005

    first = paired_moving_block_tail(
        candidate,
        baseline,
        block_days=20,
        draws=499,
        seed=20260713,
    )
    second = paired_moving_block_tail(
        candidate,
        baseline,
        block_days=20,
        draws=499,
        seed=20260713,
    )

    assert first == second
    assert set(first) == {"tail_prob", "ci05", "ci50", "ci95"}
    assert first["tail_prob"] <= 0.01
    assert first["ci05"] > 0.0


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-date",
        "duplicate-date",
        "unsorted-date",
        "timezone",
        "non-normalized-date",
        "nonfinite",
        "loss-below-minus-one",
    ],
)
def test_paired_moving_block_tail_rejects_nonexact_pair(mutation):
    index = pd.bdate_range("2021-01-01", periods=40)
    baseline = pd.Series(np.tile([-0.001, 0.001], 20), index=index)
    candidate = baseline + 0.0005
    if mutation == "missing-date":
        candidate = candidate.drop(index=index[3])
    elif mutation == "duplicate-date":
        candidate.index = pd.DatetimeIndex([*index[:-1], index[-2]])
    elif mutation == "unsorted-date":
        candidate = candidate.iloc[[1, 0, *range(2, len(candidate))]]
    elif mutation == "timezone":
        candidate.index = candidate.index.tz_localize("UTC")
    elif mutation == "non-normalized-date":
        candidate.index = candidate.index + pd.Timedelta(hours=1)
    elif mutation == "nonfinite":
        candidate.iloc[2] = np.nan
    elif mutation == "loss-below-minus-one":
        baseline.iloc[2] = -1.0
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        paired_moving_block_tail(
            candidate,
            baseline,
            block_days=10,
            draws=9,
            seed=1,
        )


@pytest.mark.parametrize(
    ("block_days", "draws", "seed"),
    [
        (0, 9, 1),
        (41, 9, 1),
        (True, 9, 1),
        (10, 0, 1),
        (10, True, 1),
        (10, 9, -1),
        (10, 9, True),
    ],
)
def test_paired_moving_block_tail_rejects_invalid_parameters(
    block_days,
    draws,
    seed,
):
    index = pd.bdate_range("2021-01-01", periods=40)
    baseline = pd.Series(np.tile([-0.001, 0.001], 20), index=index)
    candidate = baseline + 0.0005

    with pytest.raises(ValueError):
        paired_moving_block_tail(
            candidate,
            baseline,
            block_days=block_days,
            draws=draws,
            seed=seed,
        )


def test_holm_style_uses_two_fixed_candidates_and_strict_boundary():
    adjusted = holm_style_adjust(
        {
            "B3_unified": 0.04,
            "B3_dual_target": 0.20,
        }
    )
    assert adjusted == {
        "B3_unified": pytest.approx(0.08),
        "B3_dual_target": pytest.approx(0.20),
    }
    failed_structure = holm_style_adjust(
        {
            "B3_unified": 0.02,
            "B3_dual_target": 1.0,
        }
    )
    assert failed_structure["B3_unified"] == pytest.approx(0.04)
    assert failed_structure["B3_dual_target"] == pytest.approx(1.0)
    assert passes_tail_gate(0.0999, 0.10)
    assert not passes_tail_gate(0.10, 0.10)


@pytest.mark.parametrize(
    "raw",
    [
        {"B3_unified": 0.04},
        {"B3_unified": 0.04, "B3_dual_target": 0.20, "extra": 0.1},
        {"B3_unified": np.nan, "B3_dual_target": 0.20},
        {"B3_unified": -0.01, "B3_dual_target": 0.20},
        {"B3_unified": 0.04, "B3_dual_target": 1.01},
        {"B3_unified": True, "B3_dual_target": 0.20},
    ],
)
def test_holm_style_rejects_wrong_or_invalid_fixed_family(raw):
    with pytest.raises(ValueError):
        holm_style_adjust(raw)


@pytest.mark.parametrize(
    ("adjusted_tail", "threshold"),
    [
        (np.nan, 0.10),
        (-0.01, 0.10),
        (1.01, 0.10),
        (True, 0.10),
        (0.05, np.nan),
        (0.05, 0.0),
        (0.05, 1.01),
        (0.05, True),
    ],
)
def test_passes_tail_gate_rejects_invalid_inputs(adjusted_tail, threshold):
    with pytest.raises(ValueError):
        passes_tail_gate(adjusted_tail, threshold)


def _evaluation_inputs(end="2024-12-31"):
    cfg = deepcopy(load_b3_config())
    calendar = pd.bdate_range("2014-01-01", end)
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
        },
        index=calendar,
    )
    q_scale = {"qblend": 1.0, "q500": 0.8, "q1000": 1.2}
    q_features = {q: base * scale for q, scale in q_scale.items()}
    labels = np.asarray(["UU", "DD", "DIV"])[np.arange(len(calendar)) % 3]
    rows = []
    for policy in cfg["pit"]["policies"]:
        for q in ("qblend", "q500", "q1000"):
            frame = q_features[q]
            rows.extend(
                {
                    "date": date,
                    "pit_policy": policy,
                    "q": q,
                    "state": str(labels[offset]),
                    "F_U": float(frame.loc[date, "F_U"]),
                    "F_D": float(frame.loc[date, "F_D"]),
                    "F_X": float(frame.loc[date, "F_X"]),
                }
                for offset, date in enumerate(calendar)
            )
    states = pd.DataFrame(rows)

    targets = {}
    for target, q in {
        "blend": "qblend",
        "500": "q500",
        "1000": "q1000",
    }.items():
        daily = pd.Series(0.0, index=calendar, name=target)
        monthly_features = q_features[q].reindex(formations)
        monthly = (
            0.006 * monthly_features["F_U"]
            - 0.004 * monthly_features["F_D"]
            + 0.003 * monthly_features["F_X"]
        )
        for start, finish in zip(formations[:-1], formations[1:]):
            daily.loc[finish] = monthly.loc[start]
        targets[target] = daily
    targets["blend"] = (
        0.5 * targets["500"] + 0.5 * targets["1000"]
    ).rename("blend")
    equal_weight = pd.Series(
        np.sin(number / 29.0) + 0.15 * np.cos(number / 11.0),
        index=calendar,
        name="factor_value",
    )
    carry = {
        "500": pd.Series(
            0.08,
            index=calendar[
                calendar >= pd.Timestamp(cfg["execution"]["ic_launch_date"])
            ],
        ),
        "1000": pd.Series(
            0.05,
            index=calendar[
                calendar >= pd.Timestamp(cfg["execution"]["im_launch_date"])
            ],
        ),
    }
    return cfg, calendar, formations, states, targets, equal_weight, carry


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-window",
        "discovery",
        "confirmation",
        "report",
        "policies",
        "industry-pit-start",
        "candidates",
        "extra-execution",
        "annualization",
        "cost",
        "ic-launch",
        "im-launch",
        "extra-gate",
        "sharpe-gate",
        "maxdd-gate",
        "turnover-gate",
        "post-im-days",
        "extra-bootstrap",
        "block-days",
        "draws",
        "seed",
        "tail",
    ],
)
def test_evaluation_config_is_exactly_frozen(mutation):
    cfg = deepcopy(load_b3_config())
    if mutation == "extra-window":
        cfg["windows"]["extra"] = ["2020-01-01", "2020-12-31"]
    elif mutation == "discovery":
        cfg["windows"]["discovery"][0] = "2014-01-02"
    elif mutation == "confirmation":
        cfg["windows"]["confirmation"][1] = "2023-12-30"
    elif mutation == "report":
        cfg["windows"]["report_only"][0] = "2024-01-02"
    elif mutation == "policies":
        cfg["pit"]["policies"] = list(reversed(cfg["pit"]["policies"]))
    elif mutation == "industry-pit-start":
        cfg["pit"]["industry_pit_start"] = "2021-01-02"
    elif mutation == "candidates":
        cfg["candidates"] = ["B3_unified"]
    elif mutation == "extra-execution":
        cfg["execution"]["extra"] = 1
    elif mutation == "annualization":
        cfg["execution"]["annualization"] = 252
    elif mutation == "cost":
        cfg["execution"]["cost_bps"] = 2.9
    elif mutation == "ic-launch":
        cfg["execution"]["ic_launch_date"] = "2015-04-17"
    elif mutation == "im-launch":
        cfg["execution"]["im_launch_date"] = "2022-07-21"
    elif mutation == "extra-gate":
        cfg["production_gates"]["extra"] = 1
    elif mutation == "sharpe-gate":
        cfg["production_gates"]["sharpe_improvement"] = 0.11
    elif mutation == "maxdd-gate":
        cfg["production_gates"]["maxdd_worsening"] = 0.03
    elif mutation == "turnover-gate":
        cfg["production_gates"]["turnover_multiple"] = 1.51
    elif mutation == "post-im-days":
        cfg["production_gates"]["post_im_min_days"] = 251
    elif mutation == "extra-bootstrap":
        cfg["bootstrap"]["extra"] = 1
    elif mutation == "block-days":
        cfg["bootstrap"]["block_days"] = 21
    elif mutation == "draws":
        cfg["bootstrap"]["draws"] = 4999
    elif mutation == "seed":
        cfg["bootstrap"]["seed"] = 20260714
    elif mutation == "tail":
        cfg["bootstrap"]["adjusted_tail_max"] = 0.11
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        _evaluation_config(cfg)


def test_fit_frozen_m1_scores_uses_only_discovery_and_no_intercept():
    cfg, calendar, formations, states, targets, equal_weight, _ = (
        _evaluation_inputs()
    )
    got = fit_frozen_m1_scores(
        states,
        targets,
        equal_weight,
        formations,
        cfg,
    )

    assert set(got) == {
        (policy, q)
        for policy in cfg["pit"]["policies"]
        for q in ("qblend", "q500", "q1000")
    }
    q_target = {"qblend": "blend", "q500": "500", "q1000": "1000"}
    for policy in cfg["pit"]["policies"]:
        for q, target in q_target.items():
            features = (
                states[
                    states["pit_policy"].eq(policy) & states["q"].eq(q)
                ]
                .set_index("date")[["F_U", "F_D", "F_X"]]
                .sort_index()
            )
            monthly_target = next_formation_targets(targets[target], formations)
            training = features.reindex(monthly_target.index).copy()
            training["target"] = monthly_target
            period_end = pd.Series(
                formations[1:],
                index=formations[:-1],
            ).reindex(training.index)
            training = training.loc[
                training.index.to_series().ge(pd.Timestamp("2014-01-01"))
                & period_end.le(pd.Timestamp("2020-12-31"))
            ]
            model = fit_model(
                training,
                ["F_U", "F_D", "F_X"],
                "target",
            )
            expected = apply_model(features, model, include_intercept=False)
            pd.testing.assert_series_equal(got[(policy, q)], expected, check_exact=True)
            assert got[(policy, q)].index.equals(calendar)

    future_targets = {name: values.copy() for name, values in targets.items()}
    for values in future_targets.values():
        values.loc["2021-01-01":] += 0.25
    target_mutation = fit_frozen_m1_scores(
        states,
        future_targets,
        equal_weight,
        formations,
        cfg,
    )
    for key in got:
        pd.testing.assert_series_equal(got[key], target_mutation[key], check_exact=True)

    report_states = states.copy()
    report = report_states["date"].ge(pd.Timestamp("2024-01-01"))
    report_states.loc[report, "F_U"] += 1000.0
    feature_mutation = fit_frozen_m1_scores(
        report_states,
        targets,
        equal_weight,
        formations,
        cfg,
    )
    for key in got:
        pd.testing.assert_series_equal(
            got[key].loc[:"2023-12-31"],
            feature_mutation[key].loc[:"2023-12-31"],
            check_exact=True,
        )
        assert not got[key].loc["2024-01-01":].equals(
            feature_mutation[key].loc["2024-01-01":]
        )


def test_fit_frozen_m1_scores_rejects_nonexact_daily_inputs():
    cfg, _, formations, states, targets, equal_weight, _ = _evaluation_inputs()
    broken_targets = {name: values.copy() for name, values in targets.items()}
    broken_targets["1000"] = broken_targets["1000"].drop(
        index=broken_targets["1000"].index[10]
    )

    with pytest.raises(DataBlocked):
        fit_frozen_m1_scores(
            states,
            broken_targets,
            equal_weight,
            formations,
            cfg,
        )


def test_fit_frozen_m1_scores_requires_exact_50_50_blend():
    cfg, _, formations, states, targets, equal_weight, _ = _evaluation_inputs()
    broken_targets = {name: values.copy() for name, values in targets.items()}
    broken_targets["blend"] = broken_targets["500"].copy()
    assert broken_targets["blend"].index.equals(broken_targets["1000"].index)
    expected_blend = 0.5 * broken_targets["500"] + 0.5 * broken_targets["1000"]
    assert not np.allclose(
        broken_targets["blend"].to_numpy(dtype=float),
        expected_blend.to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-15,
    )

    with pytest.raises(DataBlocked, match="50/50"):
        fit_frozen_m1_scores(
            states,
            broken_targets,
            equal_weight,
            formations,
            cfg,
        )


@pytest.mark.parametrize("mutation", ["discovery", "cost", "bootstrap"])
def test_fit_frozen_m1_scores_rejects_evaluation_config_mutations(mutation):
    cfg, _, formations, states, targets, equal_weight, _ = _evaluation_inputs()
    if mutation == "discovery":
        cfg["windows"]["discovery"][0] = "2014-01-02"
    elif mutation == "cost":
        cfg["execution"]["cost_bps"] = 2.9
    elif mutation == "bootstrap":
        cfg["bootstrap"]["draws"] = 4999
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        fit_frozen_m1_scores(
            states,
            targets,
            equal_weight,
            formations,
            cfg,
        )


@pytest.mark.parametrize("entrypoint", ["fit", "build"])
@pytest.mark.parametrize("mutation", ["truncated", "early-month-end"])
def test_eval_entrypoints_require_authoritative_fixed_window_formations(
    entrypoint,
    mutation,
):
    cfg, calendar, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    broken = formations.copy()
    if mutation == "truncated":
        broken = broken[broken <= pd.Timestamp("2023-06-30")]
    elif mutation == "early-month-end":
        period = pd.Period("2022-06", freq="M")
        index = np.flatnonzero(broken.to_period("M") == period)[0]
        month_days = calendar[calendar.to_period("M") == period]
        broken_values = broken.to_numpy(copy=True)
        broken_values[index] = month_days[-2].to_datetime64()
        broken = pd.DatetimeIndex(broken_values)
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked, match="formation"):
        if entrypoint == "fit":
            fit_frozen_m1_scores(
                states,
                targets,
                equal_weight,
                broken,
                cfg,
            )
        elif entrypoint == "build":
            build_evaluation(
                states,
                _model_comparison(cfg),
                targets,
                carry,
                equal_weight,
                broken,
                cfg,
            )
        else:
            raise AssertionError(f"unsupported entrypoint: {entrypoint}")


def test_yearly_contributions_has_exact_schema_and_strongest_exclusion():
    index = pd.bdate_range("2021-01-01", "2023-12-29")
    values = pd.Series(0.0, index=index)
    values.loc[values.index.year == 2021] = 0.0001
    values.loc[values.index.year == 2022] = -0.0003
    values.loc[values.index.year == 2023] = 0.0002

    got = yearly_contributions(
        "legal_deadline",
        "B3_unified",
        values,
        "2021-2023",
        False,
    )

    assert list(got.columns) == YEARLY_COLUMNS
    assert not got.duplicated(YEARLY_ROW_ID_COLUMNS).any()
    assert set(got["row_type"]) == {"year", "excluding_strongest"}
    assert got["affects_verdict"].eq(False).all()
    assert got["is_in_sample"].eq(False).all()
    year_rows = got[got["row_type"].eq("year")].set_index("year")
    assert set(year_rows.index) == {2021, 2022, 2023}
    assert year_rows["strongest_year"].eq(2022).all()
    log_pnl = np.log1p(values).groupby(values.index.year).sum()
    denominator = float(log_pnl.abs().sum())
    for year in (2021, 2022, 2023):
        sample = values.loc[values.index.year == year]
        assert year_rows.loc[year, "n_obs"] == len(sample)
        assert year_rows.loc[year, "signed_log_pnl"] == pytest.approx(
            log_pnl.loc[year]
        )
        assert year_rows.loc[year, "absolute_pnl_share"] == pytest.approx(
            abs(log_pnl.loc[year]) / denominator
        )
        assert year_rows.loc[year, "ann_return"] == pytest.approx(
            ann_return(sample)
        )
        assert year_rows.loc[year, "sharpe"] == pytest.approx(sharpe(sample))
        assert year_rows.loc[year, "maxdd"] == pytest.approx(
            max_drawdown(sample)
        )

    summary = got[got["row_type"].eq("excluding_strongest")].iloc[0]
    remaining = values.loc[values.index.year != 2022]
    assert pd.isna(summary["year"])
    assert summary["excluded_year"] == 2022
    assert summary["n_obs"] == len(remaining)
    assert pd.isna(summary["signed_log_pnl"])
    assert pd.isna(summary["absolute_pnl_share"])
    assert summary["ann_return"] == pytest.approx(ann_return(remaining))
    assert summary["sharpe"] == pytest.approx(sharpe(remaining))
    assert summary["maxdd"] == pytest.approx(max_drawdown(remaining))


def test_single_year_report_has_explicit_empty_exclusion_summary():
    index = pd.bdate_range("2024-01-01", "2024-12-31")
    values = pd.Series(0.0002, index=index)

    got = yearly_contributions(
        "legal_deadline",
        "B3_unified",
        values,
        "2024-2026-report-only",
        False,
    )

    assert list(got.columns) == YEARLY_COLUMNS
    assert len(got) == 2
    year = got[got["row_type"].eq("year")].iloc[0]
    summary = got[got["row_type"].eq("excluding_strongest")].iloc[0]
    assert year["year"] == 2024
    assert year["strongest_year"] == 2024
    assert summary["excluded_year"] == 2024
    assert summary["strongest_year"] == 2024
    assert summary["n_obs"] == 0
    assert pd.isna(summary["signed_log_pnl"])
    assert pd.isna(summary["absolute_pnl_share"])
    assert pd.isna(summary["ann_return"])
    assert pd.isna(summary["sharpe"])
    assert pd.isna(summary["maxdd"])
    assert not bool(summary["is_in_sample"])
    assert not bool(summary["affects_verdict"])


@pytest.mark.parametrize("mutation", ["one-year", "nonfinite", "loss", "timezone"])
def test_yearly_contributions_rejects_invalid_input(mutation):
    index = pd.bdate_range("2021-01-01", "2022-12-30")
    values = pd.Series(0.001, index=index)
    if mutation == "one-year":
        values = values.loc["2021"]
    elif mutation == "nonfinite":
        values.iloc[2] = np.nan
    elif mutation == "loss":
        values.iloc[2] = -1.0
    elif mutation == "timezone":
        values.index = values.index.tz_localize("UTC")
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        yearly_contributions(
            "legal_deadline",
            "B3_unified",
            values,
            "2021-2023",
            False,
        )


def _model_comparison(cfg):
    string_columns = {
        "pit_policy",
        "candidate",
        "q",
        "target",
        "window",
        "model",
        "gate_name",
    }

    def row(**updates):
        values = {
            column: (
                ""
                if column in string_columns
                else False
                if column in {"is_in_sample", "affects_verdict"}
                else np.nan
            )
            for column in MODEL_COMPARISON_COLUMNS
        }
        values.update(updates)
        return values

    rows = []
    public_gates = {
        "beta_h_same_sign": "structure",
        "interaction_axis_corr": "2021-2023",
        "hard_sort_complete": "structure",
    }
    candidate_gates = (
        "m1_increment",
        "partial_ic",
        "stability",
        "state_coverage",
    )
    q_specs = {
        "qblend": ("B3_unified", "blend", 0.21),
        "q500": ("B3_dual_target", "500", 0.17),
        "q1000": ("B3_dual_target", "1000", 0.13),
    }
    for policy in cfg["pit"]["policies"]:
        rows.extend(
            row(
                pit_policy=policy,
                candidate="PUBLIC",
                window=window,
                gate_name=gate,
                gate_pass=True,
                affects_verdict=True,
            )
            for gate, window in public_gates.items()
        )
        for candidate in ("B3_unified", "B3_dual_target"):
            rows.extend(
                row(
                    pit_policy=policy,
                    candidate=candidate,
                    window="2021-2023",
                    gate_name=gate,
                    gate_pass=True,
                    affects_verdict=True,
                )
                for gate in candidate_gates
            )
        for q, (candidate, target, partial) in q_specs.items():
            for window in ("2021-2023", "2021", "2022", "2023"):
                rows.append(
                    row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target,
                        window=window,
                        model="M1",
                        n=36 if window == "2021-2023" else 12,
                        partial_ic=partial,
                        affects_verdict=True,
                    )
                )
            rows.append(
                row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target,
                    window="2024-2026-report-only",
                    model="M1",
                    n=12,
                    partial_ic=-0.99,
                    affects_verdict=False,
                )
            )
    return pd.DataFrame(rows, columns=MODEL_COMPARISON_COLUMNS)


def _build_fixture():
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    comparison = _model_comparison(cfg)
    result = build_evaluation(
        states,
        comparison,
        targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    return (
        cfg,
        formations,
        states,
        comparison,
        targets,
        carry,
        equal_weight,
        result,
    )


def test_build_evaluation_returns_exact_schemas_unique_ids_and_fixed_family():
    cfg, _, _, _, _, _, _, got = _build_fixture()

    assert isinstance(got, EvaluationFrames)
    assert list(got.production_metrics.columns) == PRODUCTION_METRICS_COLUMNS
    assert list(got.bootstrap.columns) == BOOTSTRAP_COLUMNS
    assert list(got.yearly.columns) == YEARLY_COLUMNS
    assert not got.production_metrics.duplicated(
        PRODUCTION_ROW_ID_COLUMNS
    ).any()
    assert not got.bootstrap.duplicated(BOOTSTRAP_ROW_ID_COLUMNS).any()
    assert not got.yearly.duplicated(YEARLY_ROW_ID_COLUMNS).any()
    assert set(got.bootstrap["pit_policy"]) == set(cfg["pit"]["policies"])
    assert set(got.bootstrap["candidate"]) == {
        "B3_unified",
        "B3_dual_target",
    }
    assert len(got.bootstrap) == 2 * len(cfg["pit"]["policies"])
    assert set(got.production_metrics["candidate"]) == {
        "equal_weight",
        "B3_unified",
        "B3_dual_target",
    }
    components = got.production_metrics[
        got.production_metrics["component"].isin({"B3_500", "B3_1000"})
    ]
    assert set(components["component"]) == {"B3_500", "B3_1000"}
    assert components["is_candidate"].eq(False).all()
    assert got.yearly["affects_verdict"].eq(False).all()
    report = got.production_metrics["window"].eq("2024-2026-report-only")
    assert report.any()
    assert got.production_metrics.loc[report, "affects_verdict"].eq(False).all()


def test_production_validator_requires_explicit_report_presence():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    policies = tuple(cfg["pit"]["policies"])

    with_report = _validate_production_output(
        frames.production_metrics,
        policies,
        report_present=True,
    )
    assert with_report["window"].eq("2024-2026-report-only").any()

    without_report = frames.production_metrics[
        ~frames.production_metrics["window"].eq("2024-2026-report-only")
    ].copy()
    validated_without_report = _validate_production_output(
        without_report,
        policies,
        report_present=False,
    )
    assert not validated_without_report["window"].eq(
        "2024-2026-report-only"
    ).any()

    with pytest.raises(RuntimeError):
        _validate_production_output(
            frames.production_metrics,
            policies,
            report_present=False,
        )


def test_production_validator_rejects_exact_row_and_flag_mutations():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    base = frames.production_metrics
    policies = tuple(cfg["pit"]["policies"])
    first_policy = policies[0]
    accepted = []
    for mutation in (
        "missing-row",
        "extra-row",
        "is-candidate",
        "executable",
        "affects-verdict",
    ):
        broken = base.copy()
        if mutation in {"missing-row", "extra-row"}:
            mask = (
                broken["pit_policy"].eq(first_policy)
                & broken["candidate"].eq("B3_dual_target")
                & broken["component"].eq("B3_1000")
                & broken["window"].eq("pre-IM")
                & broken["gate_name"].eq("")
            )
            assert mask.sum() == 1
            if mutation == "missing-row":
                broken = broken.loc[~mask].copy()
            else:
                extra = broken.loc[mask].copy()
                extra["component"] = "unexpected"
                broken = pd.concat([broken, extra], ignore_index=True)
        elif mutation == "is-candidate":
            mask = (
                broken["pit_policy"].eq(first_policy)
                & broken["candidate"].eq("B3_dual_target")
                & broken["component"].eq("B3_500")
                & broken["window"].eq("pre-IM")
                & broken["gate_name"].eq("")
            )
            assert mask.sum() == 1
            broken.loc[mask, "is_candidate"] = True
        elif mutation == "executable":
            mask = (
                broken["pit_policy"].eq(first_policy)
                & broken["candidate"].eq("B3_unified")
                & broken["component"].eq("blend")
                & broken["window"].eq("2021-2023")
                & broken["gate_name"].eq("")
            )
            assert mask.sum() == 1
            broken.loc[mask, "executable"] = True
        elif mutation == "affects-verdict":
            mask = (
                broken["pit_policy"].eq(first_policy)
                & broken["candidate"].eq("B3_dual_target")
                & broken["window"].eq("post-IM")
                & broken["gate_name"].eq("post_im_min_days")
            )
            assert mask.sum() == 1
            broken.loc[mask, "affects_verdict"] = False
        else:
            raise AssertionError(f"unsupported mutation: {mutation}")

        try:
            _validate_production_output(
                broken,
                policies,
                report_present=True,
            )
        except RuntimeError:
            continue
        accepted.append(mutation)

    assert accepted == []


def test_production_validator_recomputes_row_numeric_semantics():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    base = frames.production_metrics
    policies = tuple(cfg["pit"]["policies"])
    first_policy = policies[0]
    accepted = []
    for mutation in (
        "metric-partial-ic",
        "leg-ann-return",
        "gate-partial-ic",
        "gate-copy",
        "metric-difference",
        "metric-n-fraction",
        "leg-n-zero",
        "metric-turnover-negative",
        "baseline-ratio",
        "full-sharpe-gate",
        "full-partial-gate",
        "post-leg-gate",
        "post-min-days-gate",
        "numeric-string",
    ):
        broken = base.copy()
        full_metric = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_unified")
            & broken["component"].eq("blend")
            & broken["window"].eq("2021-2023")
            & broken["gate_name"].eq("")
        )
        full_leg = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_unified")
            & broken["component"].eq("qblend")
            & broken["window"].eq("2021-2023")
            & broken["gate_name"].eq("partial_ic_leg")
        )
        full_sharpe_gate = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_unified")
            & broken["window"].eq("2021-2023")
            & broken["gate_name"].eq("sharpe_improvement")
        )
        full_partial_gate = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_unified")
            & broken["window"].eq("2021-2023")
            & broken["gate_name"].eq("partial_ic")
        )
        post_leg = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_dual_target")
            & broken["component"].eq("q500")
            & broken["window"].eq("post-IM")
            & broken["gate_name"].eq("post_im_partial_ic_leg")
        )
        post_min_gate = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_dual_target")
            & broken["window"].eq("post-IM")
            & broken["gate_name"].eq("post_im_min_days")
        )
        baseline = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("equal_weight")
            & broken["component"].eq("blend")
            & broken["window"].eq("2021-2023")
            & broken["gate_name"].eq("")
        )
        component = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_dual_target")
            & broken["component"].eq("B3_500")
            & broken["window"].eq("pre-IM")
            & broken["gate_name"].eq("")
        )
        for mask in (
            full_metric,
            full_leg,
            full_sharpe_gate,
            full_partial_gate,
            post_leg,
            post_min_gate,
            baseline,
            component,
        ):
            assert mask.sum() == 1

        if mutation == "metric-partial-ic":
            broken.loc[full_metric, "partial_ic"] = 0.0
        elif mutation == "leg-ann-return":
            broken.loc[full_leg, "ann_return"] = 0.0
        elif mutation == "gate-partial-ic":
            broken.loc[full_sharpe_gate, "partial_ic"] = 0.0
        elif mutation == "gate-copy":
            broken.loc[full_sharpe_gate, "ann_return"] += 0.01
        elif mutation == "metric-difference":
            broken.loc[full_metric, "sharpe_difference"] += 0.01
        elif mutation == "metric-n-fraction":
            broken["n_obs"] = broken["n_obs"].astype(float)
            broken.loc[full_metric, "n_obs"] += 0.5
        elif mutation == "leg-n-zero":
            broken.loc[full_leg, "n_obs"] = 0
        elif mutation == "metric-turnover-negative":
            broken.loc[component, "turnover"] = -0.01
        elif mutation == "baseline-ratio":
            broken.loc[baseline, "turnover_ratio"] = 2.0
        elif mutation == "full-sharpe-gate":
            broken.loc[full_sharpe_gate, "gate_pass"] = not bool(
                broken.loc[full_sharpe_gate, "gate_pass"].iloc[0]
            )
        elif mutation == "full-partial-gate":
            broken.loc[full_partial_gate, "gate_pass"] = not bool(
                broken.loc[full_partial_gate, "gate_pass"].iloc[0]
            )
        elif mutation == "post-leg-gate":
            broken.loc[post_leg, "gate_pass"] = not bool(
                broken.loc[post_leg, "gate_pass"].iloc[0]
            )
        elif mutation == "post-min-days-gate":
            broken.loc[post_min_gate, "gate_pass"] = not bool(
                broken.loc[post_min_gate, "gate_pass"].iloc[0]
            )
        elif mutation == "numeric-string":
            broken["n_obs"] = broken["n_obs"].astype(object)
            broken.loc[full_metric, "n_obs"] = str(
                int(base.loc[full_metric, "n_obs"].iloc[0])
            )
        else:
            raise AssertionError(f"unsupported mutation: {mutation}")

        try:
            _validate_production_output(
                broken,
                policies,
                report_present=True,
            )
        except RuntimeError:
            continue
        accepted.append(mutation)

    assert accepted == []


def test_production_validator_rejects_nonpositive_passing_full_partial_leg():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.production_metrics.copy()
    policy = cfg["pit"]["policies"][0]
    leg = (
        broken["pit_policy"].eq(policy)
        & broken["candidate"].eq("B3_unified")
        & broken["component"].eq("qblend")
        & broken["window"].eq("2021-2023")
        & broken["gate_name"].eq("partial_ic_leg")
    )
    aggregate = (
        broken["pit_policy"].eq(policy)
        & broken["candidate"].eq("B3_unified")
        & broken["component"].eq("aggregate")
        & broken["window"].eq("2021-2023")
        & broken["gate_name"].eq("partial_ic")
    )
    assert leg.sum() == aggregate.sum() == 1
    assert bool(broken.loc[leg, "gate_pass"].iloc[0])
    assert bool(broken.loc[aggregate, "gate_pass"].iloc[0])
    broken.loc[leg, "partial_ic"] = -0.01

    with pytest.raises(RuntimeError, match="leg gate semantics"):
        _validate_production_output(
            broken,
            tuple(cfg["pit"]["policies"]),
            report_present=True,
        )


def test_structure_pass_is_exact_public_and_candidate_and_failed_tail_is_one():
    cfg, formations, states, comparison, targets, carry, equal_weight, _ = (
        _build_fixture()
    )
    first_policy = cfg["pit"]["policies"][0]
    failed = comparison.copy()
    aggregate = (
        failed["pit_policy"].eq(first_policy)
        & failed["candidate"].eq("B3_dual_target")
        & failed["q"].eq("")
        & failed["gate_name"].eq("stability")
    )
    assert aggregate.sum() == 1
    failed.loc[aggregate, "gate_pass"] = False

    got = build_evaluation(
        states,
        failed,
        targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    policy_rows = got.bootstrap[got.bootstrap["pit_policy"].eq(first_policy)]
    dual = policy_rows[
        policy_rows["candidate"].eq("B3_dual_target")
    ].iloc[0]
    unified = policy_rows[
        policy_rows["candidate"].eq("B3_unified")
    ].iloc[0]
    assert not bool(dual["structure_pass"])
    assert dual["tail_prob"] == 1.0
    assert pd.isna(dual["ci05"])
    assert bool(unified["structure_pass"])
    assert set(policy_rows["candidate"]) == {
        "B3_unified",
        "B3_dual_target",
    }

    missing_public = comparison.drop(
        index=comparison[
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("PUBLIC")
            & comparison["gate_name"].eq("hard_sort_complete")
        ].index
    )
    with pytest.raises(DataBlocked, match="exactly three PUBLIC"):
        build_evaluation(
            states,
            missing_public,
            targets,
            carry,
            equal_weight,
            formations,
            cfg,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "public-beta-window",
        "public-interaction-window",
        "public-hard-sort-window",
        "aggregate-window",
        "concrete-affects-verdict",
        "concrete-is-in-sample",
        "concrete-n",
    ],
)
def test_model_evidence_validator_freezes_required_row_semantics(mutation):
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    comparison = _model_comparison(cfg)
    first_policy = cfg["pit"]["policies"][0]
    if mutation.startswith("public-"):
        gate = {
            "public-beta-window": "beta_h_same_sign",
            "public-interaction-window": "interaction_axis_corr",
            "public-hard-sort-window": "hard_sort_complete",
        }[mutation]
        index = comparison[
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("PUBLIC")
            & comparison["gate_name"].eq(gate)
        ].index[0]
        comparison.loc[index, "window"] = (
            "2021-2023" if gate != "interaction_axis_corr" else "structure"
        )
    elif mutation == "aggregate-window":
        index = comparison[
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("B3_unified")
            & comparison["gate_name"].eq("m1_increment")
        ].index[0]
        comparison.loc[index, "window"] = "structure"
    else:
        index = comparison[
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("B3_unified")
            & comparison["q"].eq("qblend")
            & comparison["window"].eq("2021-2023")
            & comparison["model"].eq("M1")
            & comparison["gate_name"].eq("")
        ].index[0]
        if mutation == "concrete-affects-verdict":
            comparison.loc[index, "affects_verdict"] = False
        elif mutation == "concrete-is-in-sample":
            comparison.loc[index, "is_in_sample"] = True
        elif mutation == "concrete-n":
            comparison["n"] = comparison["n"].astype(object)
            comparison.loc[index, "n"] = "36"
        else:
            raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(DataBlocked):
        build_evaluation(
            states,
            comparison,
            targets,
            carry,
            equal_weight,
            formations,
            cfg,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "public-is-in-sample",
        "aggregate-is-in-sample",
        "full-partial-above-one",
        "year-partial-below-minus-one",
    ],
)
def test_model_evidence_rejects_in_sample_gates_and_invalid_partial_bounds(
    mutation,
):
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    comparison = _model_comparison(cfg)
    first_policy = cfg["pit"]["policies"][0]
    if mutation == "public-is-in-sample":
        mask = (
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("PUBLIC")
            & comparison["gate_name"].eq("interaction_axis_corr")
        )
        comparison.loc[mask, "is_in_sample"] = True
    elif mutation == "aggregate-is-in-sample":
        mask = (
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("B3_unified")
            & comparison["q"].eq("")
            & comparison["gate_name"].eq("partial_ic")
        )
        comparison.loc[mask, "is_in_sample"] = True
    else:
        window = (
            "2021-2023"
            if mutation == "full-partial-above-one"
            else "2022"
        )
        mask = (
            comparison["pit_policy"].eq(first_policy)
            & comparison["candidate"].eq("B3_unified")
            & comparison["q"].eq("qblend")
            & comparison["window"].eq(window)
            & comparison["model"].eq("M1")
            & comparison["gate_name"].eq("")
        )
        comparison.loc[mask, "partial_ic"] = (
            1.01
            if mutation == "full-partial-above-one"
            else -1.01
        )
    assert mask.sum() == 1

    with pytest.raises(DataBlocked):
        build_evaluation(
            states,
            comparison,
            targets,
            carry,
            equal_weight,
            formations,
            cfg,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "adjusted-tail",
        "gate-pass",
        "ci-order",
        "draws",
        "block-days",
        "seed",
    ],
)
def test_bootstrap_output_validator_recomputes_frozen_contract(mutation):
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.bootstrap.copy()
    passing = broken["structure_pass"].astype(bool)
    index = broken[passing].index[0]
    if mutation == "adjusted-tail":
        broken.loc[index, "holm_adjusted_tail"] = 0.77
    elif mutation == "gate-pass":
        broken.loc[index, "gate_pass"] = not bool(
            broken.loc[index, "gate_pass"]
        )
    elif mutation == "ci-order":
        broken.loc[index, "ci05"] = float(broken.loc[index, "ci95"]) + 0.01
    elif mutation == "draws":
        broken.loc[index, "draws"] = 4999
    elif mutation == "block-days":
        broken.loc[index, "block_days"] = 21
    elif mutation == "seed":
        broken.loc[index, "seed"] = 20260714
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(RuntimeError):
        _validate_bootstrap_output(
            broken,
            tuple(cfg["pit"]["policies"]),
        )


@pytest.mark.parametrize("tail_prob", [0.0, 0.04])
def test_bootstrap_output_validator_requires_empirical_tail_grid(tail_prob):
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.bootstrap.copy()
    policy = cfg["pit"]["policies"][0]
    family_mask = broken["pit_policy"].eq(policy)
    index = broken[
        family_mask & broken["structure_pass"].astype(bool)
    ].index[0]
    broken.loc[index, "tail_prob"] = tail_prob
    family = broken.loc[family_mask]
    adjusted = holm_style_adjust(
        dict(
            zip(
                family["candidate"],
                family["tail_prob"].astype(float),
                strict=True,
            )
        )
    )
    for candidate, adjusted_tail in adjusted.items():
        row_mask = family_mask & broken["candidate"].eq(candidate)
        broken.loc[row_mask, "holm_adjusted_tail"] = adjusted_tail
        broken.loc[row_mask, "gate_pass"] = bool(
            broken.loc[row_mask, "structure_pass"].iloc[0]
        ) and passes_tail_gate(adjusted_tail, 0.10)

    with pytest.raises(RuntimeError, match="empirical grid"):
        _validate_bootstrap_output(
            broken,
            tuple(cfg["pit"]["policies"]),
        )


def test_dual_im_boundary_and_partial_rows_preserve_both_legs():
    cfg, _, _, _, _, _, _, got = _build_fixture()
    first_policy = cfg["pit"]["policies"][0]
    production = got.production_metrics[
        got.production_metrics["pit_policy"].eq(first_policy)
        & got.production_metrics["candidate"].eq("B3_dual_target")
    ]
    pre = production[
        production["window"].eq("pre-IM")
        & production["component"].eq("blend")
        & production["gate_name"].eq("")
    ]
    post = production[
        production["window"].eq("post-IM")
        & production["component"].eq("blend")
        & production["gate_name"].eq("")
    ]
    assert len(pre) == len(post) == 1
    assert not bool(pre["executable"].iloc[0])
    assert bool(post["executable"].iloc[0])
    assert int(post["n_obs"].iloc[0]) >= cfg["production_gates"][
        "post_im_min_days"
    ]
    post_gates = production[
        production["window"].eq("post-IM")
        & production["affects_verdict"]
    ]
    assert set(post_gates["gate_name"]) == {
        "post_im_min_days",
        "post_im_sharpe_difference",
        "post_im_maxdd_difference",
        "post_im_partial_ic",
    }
    legs = production[
        production["window"].eq("post-IM")
        & production["gate_name"].eq("post_im_partial_ic_leg")
    ]
    assert set(legs["component"]) == {"q500", "q1000"}
    assert legs["partial_ic"].notna().all()
    aggregate = production[
        production["window"].eq("post-IM")
        & production["gate_name"].eq("post_im_partial_ic")
    ]
    assert len(aggregate) == 1
    assert pd.isna(aggregate["partial_ic"].iloc[0])
    assert bool(aggregate["gate_pass"].iloc[0]) == bool(
        legs["gate_pass"].all()
    )


def test_partial_ic_leg_n_obs_uses_monthly_evidence_frequency():
    cfg, formations, _, comparison, targets, _, _, got = _build_fixture()
    full_legs = got.production_metrics[
        got.production_metrics["window"].eq("2021-2023")
        & got.production_metrics["gate_name"].eq("partial_ic_leg")
    ]
    assert len(full_legs) == 3 * len(cfg["pit"]["policies"])
    assert full_legs["n_obs"].eq(36).all()

    monthly_target = next_formation_targets(targets["500"], formations)
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    ).reindex(monthly_target.index)
    expected_post_n = int(
        (
            monthly_target.index.to_series().ge(
                pd.Timestamp(cfg["execution"]["im_launch_date"])
            )
            & period_end.le(pd.Timestamp(cfg["windows"]["confirmation"][1]))
        ).sum()
    )
    post_legs = got.production_metrics[
        got.production_metrics["window"].eq("post-IM")
        & got.production_metrics["gate_name"].eq("post_im_partial_ic_leg")
    ]
    assert expected_post_n >= 10
    assert len(post_legs) == 2 * len(cfg["pit"]["policies"])
    assert post_legs["n_obs"].eq(expected_post_n).all()

    concrete = comparison[
        comparison["window"].eq("2021-2023")
        & comparison["model"].eq("M1")
        & comparison["gate_name"].eq("")
    ]
    assert concrete["n"].eq(36).all()


def test_report_and_yearly_mutations_cannot_change_verdict_inputs():
    (
        cfg,
        formations,
        states,
        comparison,
        targets,
        carry,
        equal_weight,
        baseline,
    ) = _build_fixture()
    changed_states = states.copy()
    report_state = changed_states["date"].ge(pd.Timestamp("2024-01-01"))
    changed_states.loc[report_state, ["F_U", "F_D", "F_X"]] += 500.0
    changed_targets = {name: values.copy() for name, values in targets.items()}
    for values in changed_targets.values():
        values.loc["2024-01-01":] += 0.001
    changed = build_evaluation(
        changed_states,
        comparison,
        changed_targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )

    baseline_verdict = baseline.production_metrics[
        baseline.production_metrics["affects_verdict"]
    ].reset_index(drop=True)
    changed_verdict = changed.production_metrics[
        changed.production_metrics["affects_verdict"]
    ].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        baseline_verdict,
        changed_verdict,
        check_exact=True,
    )
    pd.testing.assert_frame_equal(
        baseline.bootstrap,
        changed.bootstrap,
        check_exact=True,
    )
    baseline_historical = ~baseline.yearly["window"].eq(
        "2024-2026-report-only"
    )
    changed_historical = ~changed.yearly["window"].eq(
        "2024-2026-report-only"
    )
    pd.testing.assert_frame_equal(
        baseline.yearly.loc[baseline_historical].reset_index(drop=True),
        changed.yearly.loc[changed_historical].reset_index(drop=True),
        check_exact=True,
    )
    assert not baseline.yearly.loc[~baseline_historical].reset_index(
        drop=True
    ).equals(
        changed.yearly.loc[~changed_historical].reset_index(drop=True)
    )
    baseline_report = baseline.production_metrics[
        baseline.production_metrics["window"].eq("2024-2026-report-only")
    ]
    changed_report = changed.production_metrics[
        changed.production_metrics["window"].eq("2024-2026-report-only")
    ]
    assert not baseline_report.equals(changed_report)

    production_before = baseline.production_metrics.copy(deep=True)
    bootstrap_before = baseline.bootstrap.copy(deep=True)
    baseline.yearly.loc[:, "absolute_pnl_share"] = 999.0
    pd.testing.assert_frame_equal(
        baseline.production_metrics,
        production_before,
        check_exact=True,
    )
    pd.testing.assert_frame_equal(
        baseline.bootstrap,
        bootstrap_before,
        check_exact=True,
    )


def test_common_carry_tail_must_cover_every_original_confirmation_date():
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    comparison = _model_comparison(cfg)
    stale_end = pd.Timestamp("2023-12-01")
    stale_carry = {
        name: values.loc[:stale_end].copy()
        for name, values in carry.items()
    }

    with pytest.raises(DataBlocked, match="full cash confirmation calendar"):
        build_evaluation(
            states,
            comparison,
            targets,
            stale_carry,
            equal_weight,
            formations,
            cfg,
        )


def test_common_carry_tail_after_confirmation_only_crops_report_window():
    cfg, calendar, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs("2026-12-31")
    )
    common_end = pd.Timestamp("2024-06-28")
    cropped_carry = {
        name: values.loc[:common_end].copy()
        for name, values in carry.items()
    }

    got = build_evaluation(
        states,
        _model_comparison(cfg),
        targets,
        cropped_carry,
        equal_weight,
        formations,
        cfg,
    )

    expected_report_days = int(
        (
            (calendar >= pd.Timestamp("2024-01-01"))
            & (calendar <= common_end)
        ).sum()
    )
    report_metrics = got.production_metrics[
        got.production_metrics["window"].eq("2024-2026-report-only")
        & got.production_metrics["gate_name"].eq("")
    ]
    assert report_metrics["n_obs"].eq(expected_report_days).all()
    report_yearly = got.yearly[
        got.yearly["window"].eq("2024-2026-report-only")
    ]
    assert set(
        report_yearly.loc[report_yearly["row_type"].eq("year"), "year"]
    ) == {2024}
    assert report_yearly.loc[
        report_yearly["row_type"].eq("excluding_strongest"), "n_obs"
    ].eq(0).all()


@pytest.mark.parametrize(
    ("window", "start", "end"),
    [
        ("2021-2023", "2021-01-01", "2023-12-31"),
        ("post-IM", "2022-07-22", "2023-12-31"),
    ],
)
def test_fixed_window_metrics_reset_engine_before_confirmation_and_post_im(
    window,
    start,
    end,
):
    cfg, calendar, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs()
    )
    got = build_evaluation(
        states,
        _model_comparison(cfg),
        targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    index = calendar[
        (calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))
    ]
    c500 = materialize_carry(
        carry["500"],
        calendar,
        pd.Timestamp(cfg["execution"]["ic_launch_date"]),
    ).loc[index]
    c1000 = materialize_carry(
        carry["1000"],
        calendar,
        pd.Timestamp(cfg["execution"]["im_launch_date"]),
    ).loc[index]
    scores = fit_frozen_m1_scores(
        states,
        targets,
        equal_weight,
        formations,
        cfg,
    )
    baseline_position = production_position(equal_weight.loc[index])
    p500 = production_position(
        scores[(cfg["pit"]["policies"][0], "q500")].loc[index]
    )
    p1000 = production_position(
        scores[(cfg["pit"]["policies"][0], "q1000")].loc[index]
    )
    cost = float(cfg["execution"]["cost_bps"])
    direct_baseline = two_leg_candidate_returns(
        baseline_position,
        baseline_position,
        targets["500"].loc[index],
        targets["1000"].loc[index],
        c500,
        c1000,
        cost,
    )
    direct_dual = two_leg_candidate_returns(
        p500,
        p1000,
        targets["500"].loc[index],
        targets["1000"].loc[index],
        c500,
        c1000,
        cost,
    )
    assert direct_baseline.iloc[0] == 0.0
    assert direct_dual.iloc[0] == 0.0

    policy = cfg["pit"]["policies"][0]
    baseline_row = got.production_metrics[
        got.production_metrics["pit_policy"].eq(policy)
        & got.production_metrics["candidate"].eq("equal_weight")
        & got.production_metrics["window"].eq(window)
        & got.production_metrics["component"].eq("blend")
        & got.production_metrics["gate_name"].eq("")
    ].iloc[0]
    dual_row = got.production_metrics[
        got.production_metrics["pit_policy"].eq(policy)
        & got.production_metrics["candidate"].eq("B3_dual_target")
        & got.production_metrics["window"].eq(window)
        & got.production_metrics["component"].eq("blend")
        & got.production_metrics["gate_name"].eq("")
    ].iloc[0]
    for row, returns, positions in [
        (
            baseline_row,
            direct_baseline,
            (baseline_position, baseline_position),
        ),
        (dual_row, direct_dual, (p500, p1000)),
    ]:
        assert row["n_obs"] == len(index)
        assert row["ann_return"] == ann_return(returns)
        assert row["sharpe"] == sharpe(returns)
        assert row["maxdd"] == max_drawdown(returns)
        assert row["turnover"] == sum(
            turnover(position) for position in positions
        ) / len(positions)


def test_report_yearly_is_emitted_for_two_plus_years_and_is_firewalled():
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs("2026-12-31")
    )
    comparison = _model_comparison(cfg)
    baseline = build_evaluation(
        states,
        comparison,
        targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    report = baseline.yearly[
        baseline.yearly["window"].eq("2024-2026-report-only")
    ]
    assert not report.empty
    assert set(report["pit_policy"]) == set(cfg["pit"]["policies"])
    assert set(report["candidate"]) == {"B3_unified", "B3_dual_target"}
    assert report["is_in_sample"].eq(False).all()
    assert report["affects_verdict"].eq(False).all()
    assert set(report.loc[report["row_type"].eq("year"), "year"]) == {
        2024,
        2025,
        2026,
    }

    changed_states = states.copy()
    report_state = changed_states["date"].ge(pd.Timestamp("2024-01-01"))
    changed_states.loc[report_state, ["F_U", "F_D", "F_X"]] += 700.0
    changed_targets = {name: values.copy() for name, values in targets.items()}
    for values in changed_targets.values():
        values.loc["2024-01-01":] += 0.001
    changed = build_evaluation(
        changed_states,
        comparison,
        changed_targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    pd.testing.assert_frame_equal(
        baseline.production_metrics[
            baseline.production_metrics["affects_verdict"]
        ].reset_index(drop=True),
        changed.production_metrics[
            changed.production_metrics["affects_verdict"]
        ].reset_index(drop=True),
        check_exact=True,
    )
    pd.testing.assert_frame_equal(
        baseline.bootstrap,
        changed.bootstrap,
        check_exact=True,
    )
    historical = ~baseline.yearly["window"].eq("2024-2026-report-only")
    changed_historical = ~changed.yearly["window"].eq(
        "2024-2026-report-only"
    )
    pd.testing.assert_frame_equal(
        baseline.yearly.loc[historical].reset_index(drop=True),
        changed.yearly.loc[changed_historical].reset_index(drop=True),
        check_exact=True,
    )
    assert not report.reset_index(drop=True).equals(
        changed.yearly[
            changed.yearly["window"].eq("2024-2026-report-only")
        ].reset_index(drop=True)
    )


def test_report_yearly_accepts_exactly_two_available_natural_years():
    cfg, _, formations, states, targets, equal_weight, carry = (
        _evaluation_inputs("2025-12-31")
    )
    got = build_evaluation(
        states,
        _model_comparison(cfg),
        targets,
        carry,
        equal_weight,
        formations,
        cfg,
    )
    report_years = got.yearly[
        got.yearly["window"].eq("2024-2026-report-only")
        & got.yearly["row_type"].eq("year")
    ]["year"]

    assert set(report_years) == {2024, 2025}


def test_builder_emits_single_available_report_year():
    cfg, _, _, _, _, _, _, got = _build_fixture()
    report = got.yearly[
        got.yearly["window"].eq("2024-2026-report-only")
    ]

    assert set(report.loc[report["row_type"].eq("year"), "year"]) == {2024}
    summary = report[report["row_type"].eq("excluding_strongest")]
    assert len(summary) == 2 * len(cfg["pit"]["policies"])
    assert summary["n_obs"].eq(0).all()
    assert summary[["ann_return", "sharpe", "maxdd"]].isna().all().all()


def test_yearly_validator_requires_explicit_report_presence():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    policies = tuple(cfg["pit"]["policies"])

    with_report = _validate_yearly_output(
        frames.yearly,
        policies,
        report_present=True,
    )
    assert with_report["window"].eq("2024-2026-report-only").any()

    without_report = frames.yearly[
        ~frames.yearly["window"].eq("2024-2026-report-only")
    ].copy()
    validated_without_report = _validate_yearly_output(
        without_report,
        policies,
        report_present=False,
    )
    assert not validated_without_report["window"].eq(
        "2024-2026-report-only"
    ).any()

    with pytest.raises(RuntimeError):
        _validate_yearly_output(
            frames.yearly,
            policies,
            report_present=False,
        )
    with pytest.raises(RuntimeError):
        _validate_yearly_output(
            without_report,
            policies,
            report_present=True,
        )


def test_yearly_validator_requires_report_years_to_start_in_2024():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.yearly.copy()
    report = broken["window"].eq("2024-2026-report-only")
    year_rows = report & broken["row_type"].eq("year")
    summary = report & broken["row_type"].eq("excluding_strongest")
    assert set(broken.loc[year_rows, "year"]) == {2024}
    broken.loc[year_rows, "year"] = 2025
    broken.loc[report, "strongest_year"] = 2025
    broken.loc[summary, "excluded_year"] = 2025

    with pytest.raises(RuntimeError, match="report year coverage"):
        _validate_yearly_output(
            broken,
            tuple(cfg["pit"]["policies"]),
            report_present=True,
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("affects_verdict", 0),
        ("is_in_sample", "False"),
    ],
)
def test_yearly_output_validator_requires_strict_booleans(column, value):
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.yearly.copy()
    broken[column] = broken[column].astype(object)
    broken.loc[broken.index[0], column] = value

    with pytest.raises(RuntimeError, match="boolean"):
        _validate_yearly_output(
            broken,
            tuple(cfg["pit"]["policies"]),
            report_present=True,
        )


@pytest.mark.parametrize("mutation", ["year-exclusion", "summary-contribution"])
def test_yearly_output_validator_enforces_row_semantics(mutation):
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.yearly.copy()
    if mutation == "year-exclusion":
        index = broken[broken["row_type"].eq("year")].index[0]
        broken.loc[index, "excluded_year"] = 2022
    elif mutation == "summary-contribution":
        index = broken[
            broken["row_type"].eq("excluding_strongest")
        ].index[0]
        broken.loc[index, "signed_log_pnl"] = 0.0
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(RuntimeError, match="row semantics"):
        _validate_yearly_output(
            broken,
            tuple(cfg["pit"]["policies"]),
            report_present=True,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "year-ann-return-null",
        "year-sharpe-null",
        "year-maxdd-null",
        "year-maxdd-positive",
        "summary-maxdd-positive",
    ],
)
def test_yearly_output_validator_requires_metrics_and_nonpositive_maxdd(
    mutation,
):
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    broken = frames.yearly.copy()
    group = (
        broken["pit_policy"].eq(cfg["pit"]["policies"][0])
        & broken["candidate"].eq("B3_unified")
        & broken["window"].eq("2021-2023")
    )
    year_index = broken[
        group & broken["row_type"].eq("year")
    ].index[0]
    summary_index = broken[
        group & broken["row_type"].eq("excluding_strongest")
    ].index[0]
    if mutation == "year-ann-return-null":
        broken.loc[year_index, "ann_return"] = np.nan
    elif mutation == "year-sharpe-null":
        broken.loc[year_index, "sharpe"] = np.nan
    elif mutation == "year-maxdd-null":
        broken.loc[year_index, "maxdd"] = np.nan
    elif mutation == "year-maxdd-positive":
        broken.loc[year_index, "maxdd"] = 0.01
    elif mutation == "summary-maxdd-positive":
        broken.loc[summary_index, "maxdd"] = 0.01
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(RuntimeError, match="row semantics"):
        _validate_yearly_output(
            broken,
            tuple(cfg["pit"]["policies"]),
            report_present=True,
        )


def test_yearly_output_validator_recomputes_shares_and_strongest():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    base = frames.yearly
    policies = tuple(cfg["pit"]["policies"])
    first_policy = policies[0]
    accepted = []
    for mutation in ("share", "strongest", "tie-strongest"):
        broken = base.copy()
        group = (
            broken["pit_policy"].eq(first_policy)
            & broken["candidate"].eq("B3_unified")
            & broken["window"].eq("2021-2023")
        )
        year_rows = broken.loc[
            group & broken["row_type"].eq("year")
        ].sort_values("year", kind="mergesort")
        summary = group & broken["row_type"].eq("excluding_strongest")
        assert len(year_rows) == 3
        assert summary.sum() == 1
        if mutation == "share":
            shares = year_rows["absolute_pnl_share"].to_numpy(dtype=float)
            pair = next(
                (
                    (left, right)
                    for left in range(len(shares))
                    for right in range(left + 1, len(shares))
                    if not np.isclose(shares[left], shares[right])
                ),
                None,
            )
            assert pair is not None
            left, right = pair
            broken.loc[
                year_rows.index[left],
                "absolute_pnl_share",
            ] = shares[right]
            broken.loc[
                year_rows.index[right],
                "absolute_pnl_share",
            ] = shares[left]
        else:
            if mutation == "tie-strongest":
                broken.loc[
                    year_rows.index,
                    "signed_log_pnl",
                ] = [1.0, -1.0, 0.5]
                broken.loc[
                    year_rows.index,
                    "absolute_pnl_share",
                ] = [0.4, 0.4, 0.2]
                wrong_strongest = int(year_rows["year"].iloc[1])
            else:
                signed = year_rows["signed_log_pnl"].abs()
                expected_strongest = int(
                    year_rows.loc[signed.idxmax(), "year"]
                )
                wrong_strongest = next(
                    int(year)
                    for year in year_rows["year"]
                    if int(year) != expected_strongest
                )
            broken.loc[group, "strongest_year"] = wrong_strongest
            broken.loc[summary, "excluded_year"] = wrong_strongest
            excluded_n = int(
                year_rows.loc[
                    year_rows["year"].eq(wrong_strongest),
                    "n_obs",
                ].iloc[0]
            )
            broken.loc[summary, "n_obs"] = (
                int(year_rows["n_obs"].sum()) - excluded_n
            )

        try:
            _validate_yearly_output(
                broken,
                policies,
                report_present=True,
            )
        except RuntimeError:
            continue
        accepted.append(mutation)

    assert accepted == []


def test_yearly_output_validator_allows_all_na_shares_for_zero_denominator():
    cfg, _, _, _, _, _, _, frames = _build_fixture()
    zero = frames.yearly.copy()
    policies = tuple(cfg["pit"]["policies"])
    first_policy = policies[0]
    group = (
        zero["pit_policy"].eq(first_policy)
        & zero["candidate"].eq("B3_unified")
        & zero["window"].eq("2021-2023")
    )
    year_rows = zero.loc[
        group & zero["row_type"].eq("year")
    ].sort_values("year", kind="mergesort")
    summary = group & zero["row_type"].eq("excluding_strongest")
    earliest = int(year_rows["year"].iloc[0])
    zero.loc[year_rows.index, "signed_log_pnl"] = 0.0
    zero.loc[year_rows.index, "absolute_pnl_share"] = np.nan
    zero.loc[group, "strongest_year"] = earliest
    zero.loc[summary, "excluded_year"] = earliest
    earliest_n = int(
        year_rows.loc[year_rows["year"].eq(earliest), "n_obs"].iloc[0]
    )
    zero.loc[summary, "n_obs"] = int(year_rows["n_obs"].sum()) - earliest_n

    validated = _validate_yearly_output(
        zero,
        policies,
        report_present=True,
    )

    checked = validated[
        validated["pit_policy"].eq(first_policy)
        & validated["candidate"].eq("B3_unified")
        & validated["window"].eq("2021-2023")
        & validated["row_type"].eq("year")
    ]
    assert checked["absolute_pnl_share"].isna().all()
    assert checked["strongest_year"].eq(earliest).all()
