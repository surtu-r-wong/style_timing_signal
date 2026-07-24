from copy import deepcopy
import hashlib
import json

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
    RUN_MANIFEST_FIELDS,
    VERDICT_COLUMNS,
    TRADING_CALENDAR_QUERY_TEMPLATE_HASH,
    TRUE_DISCLOSURE_COVERAGE_BASIS,
    EvaluationFrames,
    RunEvidence,
    PreflightManifestContract,
    StructureProvenanceContract,
    _evaluation_config,
    _validate_bootstrap_output,
    _validate_production_output,
    _validate_yearly_output,
    assemble_verdicts,
    blocked_verdict_rows,
    build_evaluation,
    build_run_manifest,
    git_commit,
    write_run_manifest,
    candidate_statistical_label,
    compute_raw_carry_freshness,
    compute_true_disclosure_coverage,
    family_best_wins,
    final_verdict,
    fit_frozen_m1_scores,
    freshness_blockers,
    hash_files,
    holm_style_adjust,
    materialize_carry,
    paired_moving_block_tail,
    passes_tail_gate,
    salg_valid_through,
    database_source_evidence_blocker,
    verify_preflight_manifest,
    two_leg_candidate_returns,
    verify_structure_provenance,
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
from signals.style_basket.b3_build import _write_stage_manifest
from signals.style_basket.b3_config import config_hash, load_b3_config
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


def test_candidate_labels_are_mutually_exclusive():
    assert candidate_statistical_label(False, False, False) == "STOP"
    assert candidate_statistical_label(True, False, False) == "MEASURE_ONLY"
    assert candidate_statistical_label(True, True, True) == "PASS_SHADOW"
    assert candidate_statistical_label(True, True, False) == "MEASURE_ONLY"


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["PASS_SHADOW", "STOP"], "PASS_SHADOW"),
        (["MEASURE_ONLY", "STOP"], "MEASURE_ONLY"),
        (["STOP", "STOP"], "STOP"),
    ],
)
def test_family_aggregation_is_best_wins(labels, expected):
    assert family_best_wins(labels) == expected


def test_run_blockers_override_only_final_not_statistical():
    assert final_verdict(
        "PASS_SHADOW", data_blocked=True, coverage_blocked=True
    ) == "DATA_BLOCKED"
    assert final_verdict(
        "PASS_SHADOW", data_blocked=False, coverage_blocked=True
    ) == "COVERAGE_BLOCKED"
    assert final_verdict(
        "PASS_SHADOW", data_blocked=False, coverage_blocked=False
    ) == "PASS_SHADOW"


@pytest.mark.parametrize(
    "args",
    [
        (1, False, False),
        (False, "no", False),
        (False, False, None),
    ],
)
def test_candidate_label_rejects_nonboolean_gates(args):
    with pytest.raises(ValueError, match="boolean"):
        candidate_statistical_label(*args)


@pytest.mark.parametrize(
    "statistical,data_blocked,coverage_blocked",
    [
        ("PASS_SHADOW", 1, False),
        ("PASS_SHADOW", False, "no"),
        ("UNKNOWN", False, False),
    ],
)
def test_final_verdict_rejects_invalid_inputs(
    statistical, data_blocked, coverage_blocked
):
    with pytest.raises(ValueError):
        final_verdict(statistical, data_blocked, coverage_blocked)


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
            rows.append(
                row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target,
                    window="2014-2020",
                    model="M1",
                    n=84,
                    partial_ic=partial,
                    is_in_sample=True,
                    affects_verdict=False,
                )
            )
            rows.append(
                row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target,
                    window="2021-2023",
                    model="M0",
                    n=36,
                    affects_verdict=True,
                )
            )
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
            for gate in ("m1_increment", "partial_ic", "stability"):
                rows.append(
                    row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target,
                        window="2021-2023",
                        model="M1",
                        gate_name=gate,
                        gate_pass=True,
                        affects_verdict=True,
                    )
                )
            for window, affects_verdict in (
                ("2014-2017", True),
                ("2018-2020", True),
                ("2021-2023", True),
                ("2014-2020", False),
            ):
                rows.append(
                    row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target,
                        window=window,
                        model="M1",
                        gate_name="state_coverage",
                        gate_pass=True,
                        affects_verdict=affects_verdict,
                    )
                )
            for model in ("M0", "M1"):
                rows.append(
                    row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target,
                        window="2024-2026-report-only",
                        model=model,
                        n=12,
                        partial_ic=-0.99 if model == "M1" else np.nan,
                        affects_verdict=False,
                    )
                )
    rows.append(
        row(
            pit_policy="ALL",
            window="run",
            gate_name="PIT_POLICY_FLIP",
            gate_pass=True,
            affects_verdict=True,
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


def _copy_evaluation_frames(frames):
    return EvaluationFrames(
        production_metrics=frames.production_metrics.copy(deep=True),
        bootstrap=frames.bootstrap.copy(deep=True),
        yearly=frames.yearly.copy(deep=True),
    )


def _force_full_production_pass(frames, policy, candidate):
    changed = _copy_evaluation_frames(frames)
    production = changed.production_metrics
    baseline = (
        production["pit_policy"].eq(policy)
        & production["candidate"].eq("equal_weight")
        & production["component"].eq("blend")
        & production["window"].eq("2021-2023")
        & production["gate_name"].eq("")
    )
    metric = (
        production["pit_policy"].eq(policy)
        & production["candidate"].eq(candidate)
        & production["component"].eq("blend")
        & production["window"].eq("2021-2023")
        & production["gate_name"].eq("")
    )
    aggregate = (
        production["pit_policy"].eq(policy)
        & production["candidate"].eq(candidate)
        & production["component"].eq("aggregate")
        & production["window"].eq("2021-2023")
    )
    sharpe_gate = aggregate & production["gate_name"].eq(
        "sharpe_improvement"
    )
    assert baseline.sum() == metric.sum() == sharpe_gate.sum() == 1
    target_sharpe = float(
        production.loc[baseline, "sharpe"].iloc[0]
    ) + 0.11
    production.loc[metric | aggregate, "sharpe"] = target_sharpe
    production.loc[metric | aggregate, "sharpe_difference"] = 0.11
    production.loc[sharpe_gate, "gate_pass"] = True
    return changed


def _force_structure_failure(comparison, frames, policy, candidate):
    changed_comparison = comparison.copy(deep=True)
    changed_frames = _copy_evaluation_frames(frames)
    structure_gate = (
        changed_comparison["pit_policy"].eq(policy)
        & changed_comparison["candidate"].eq(candidate)
        & changed_comparison["q"].eq("")
        & changed_comparison["gate_name"].eq("stability")
    )
    structure_legs = (
        changed_comparison["pit_policy"].eq(policy)
        & changed_comparison["candidate"].eq(candidate)
        & changed_comparison["q"].ne("")
        & changed_comparison["gate_name"].eq("stability")
    )
    bootstrap_row = (
        changed_frames.bootstrap["pit_policy"].eq(policy)
        & changed_frames.bootstrap["candidate"].eq(candidate)
    )
    assert structure_gate.sum() == bootstrap_row.sum() == 1
    assert structure_legs.any()
    changed_comparison.loc[structure_gate, "gate_pass"] = False
    changed_comparison.loc[structure_legs, "gate_pass"] = False
    changed_frames.bootstrap.loc[
        bootstrap_row,
        ["tail_prob", "holm_adjusted_tail"],
    ] = 1.0
    changed_frames.bootstrap.loc[
        bootstrap_row,
        ["ci05", "ci50", "ci95"],
    ] = np.nan
    changed_frames.bootstrap.loc[
        bootstrap_row,
        ["structure_pass", "gate_pass"],
    ] = False
    return changed_comparison, changed_frames


def _force_dual_boundary_failure(frames, policy):
    changed = _copy_evaluation_frames(frames)
    production = changed.production_metrics
    metric = (
        production["pit_policy"].eq(policy)
        & production["candidate"].eq("B3_dual_target")
        & production["component"].eq("blend")
        & production["window"].eq("post-IM")
        & production["gate_name"].eq("")
    )
    aggregate = (
        production["pit_policy"].eq(policy)
        & production["candidate"].eq("B3_dual_target")
        & production["component"].eq("aggregate")
        & production["window"].eq("post-IM")
    )
    sharpe_gate = aggregate & production["gate_name"].eq(
        "post_im_sharpe_difference"
    )
    assert metric.sum() == sharpe_gate.sum() == 1
    target_sharpe = float(
        production.loc[metric, "baseline_sharpe"].iloc[0]
    ) - 0.01
    production.loc[metric | aggregate, "sharpe"] = target_sharpe
    production.loc[metric | aggregate, "sharpe_difference"] = -0.01
    production.loc[sharpe_gate, "gate_pass"] = False
    return changed


def _force_bootstrap_failure(frames, policy, candidate):
    changed = _copy_evaluation_frames(frames)
    family = changed.bootstrap["pit_policy"].eq(policy)
    candidate_row = family & changed.bootstrap["candidate"].eq(candidate)
    assert candidate_row.sum() == 1
    changed.bootstrap.loc[candidate_row, "tail_prob"] = 1000 / 5001
    adjusted = holm_style_adjust(
        dict(
            zip(
                changed.bootstrap.loc[family, "candidate"],
                changed.bootstrap.loc[family, "tail_prob"],
                strict=True,
            )
        )
    )
    for name, value in adjusted.items():
        row = family & changed.bootstrap["candidate"].eq(name)
        changed.bootstrap.loc[row, "holm_adjusted_tail"] = value
        changed.bootstrap.loc[row, "gate_pass"] = (
            changed.bootstrap.loc[row, "structure_pass"].astype(bool)
            & passes_tail_gate(value, 0.10)
        )
    return changed


def test_verdict_assembler_has_exact_schema_and_policy_candidate_aggregates():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()

    got = assemble_verdicts(
        comparison,
        frames,
        cfg,
    )

    assert list(got.columns) == VERDICT_COLUMNS == [
        "scope",
        "subject",
        "gate",
        "gate_pass",
        "status",
        "reason_code",
        "detail",
        "provisional",
        "affects_statistical",
        "statistical_verdict",
        "final_verdict",
        "shadow_candidate",
        "shadow_start_allowed",
    ]
    summaries = got[
        got["scope"].str.startswith("candidate/")
        & got["gate"].eq("statistical_verdict")
    ]
    assert set(zip(summaries["scope"], summaries["subject"], strict=True)) == {
        (f"candidate/{policy}", candidate)
        for policy in cfg["pit"]["policies"]
        for candidate in cfg["candidates"]
    }
    assert not got.duplicated(["scope", "subject", "gate"]).any()


def test_verdict_assembler_labels_pass_measure_stop_and_family_best_wins():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    headline = "legal_deadline"
    changed_frames = _force_full_production_pass(
        frames,
        headline,
        "B3_unified",
    )
    changed_comparison, changed_frames = _force_structure_failure(
        comparison,
        changed_frames,
        headline,
        "B3_dual_target",
    )

    got = assemble_verdicts(changed_comparison, changed_frames, cfg)

    candidates = got[
        got["gate"].eq("statistical_verdict")
        & got["scope"].str.startswith("candidate/")
    ].set_index(["scope", "subject"])
    assert candidates.loc[
        (f"candidate/{headline}", "B3_unified"),
        "statistical_verdict",
    ] == "PASS_SHADOW"
    assert not bool(
        candidates.loc[
            (f"candidate/{headline}", "B3_unified"),
            "provisional",
        ]
    )
    assert candidates.loc[
        (f"candidate/{headline}", "B3_dual_target"),
        "statistical_verdict",
    ] == "STOP"
    assert candidates.loc[
        (f"candidate/{headline}", "B3_dual_target"),
        "reason_code",
    ] == "STRUCTURE_GATE_FAILED"
    assert bool(
        candidates.loc[
            (f"candidate/{headline}", "B3_dual_target"),
            "provisional",
        ]
    )
    approximate = candidates.loc[
        "candidate/legal_deadline_plus_one_month_end"
    ]
    assert approximate["statistical_verdict"].eq("MEASURE_ONLY").all()
    assert approximate["reason_code"].eq(
        "PRODUCTION_GATE_FAILED"
    ).all()
    assert approximate["provisional"].eq(True).all()

    family = got[
        got["scope"].eq("family/legal_deadline")
        & got["subject"].eq("B3")
        & got["gate"].eq("statistical_verdict")
    ]
    assert len(family) == 1
    assert family["statistical_verdict"].iloc[0] == "PASS_SHADOW"


def test_headline_family_reason_audit_follows_candidate_order_and_clears_on_pass():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    headline = "legal_deadline"

    failed = assemble_verdicts(comparison, frames, cfg)
    failed_family = failed[
        failed["scope"].eq(f"family/{headline}")
        & failed["subject"].eq("B3")
        & failed["gate"].eq("statistical_verdict")
    ].iloc[0]
    assert failed_family["statistical_verdict"] == "MEASURE_ONLY"
    assert failed_family["reason_code"] == "HEADLINE_CANDIDATES_FAILED"
    assert failed_family["detail"] == (
        "B3_unified=MEASURE_ONLY:PRODUCTION_GATE_FAILED,"
        "B3_dual_target=MEASURE_ONLY:PRODUCTION_GATE_FAILED"
    )

    passing_frames = _force_full_production_pass(
        frames,
        headline,
        "B3_unified",
    )
    passing = assemble_verdicts(comparison, passing_frames, cfg)
    passing_family = passing[
        passing["scope"].eq(f"family/{headline}")
        & passing["subject"].eq("B3")
        & passing["gate"].eq("statistical_verdict")
    ].iloc[0]
    assert passing_family["statistical_verdict"] == "PASS_SHADOW"
    assert passing_family["reason_code"] == ""
    assert passing_family["detail"] == ""


def test_dual_pass_requires_all_post_im_boundary_gates():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    policy = "legal_deadline"
    passing = _force_full_production_pass(
        frames,
        policy,
        "B3_dual_target",
    )
    failed = _force_dual_boundary_failure(passing, policy)

    got = assemble_verdicts(comparison, failed, cfg)

    candidate = got[
        got["scope"].eq(f"candidate/{policy}")
        & got["subject"].eq("B3_dual_target")
        & got["gate"].eq("statistical_verdict")
    ].iloc[0]
    assert candidate["statistical_verdict"] == "MEASURE_ONLY"
    assert candidate["reason_code"] == "EXECUTABLE_BOUNDARY_FAILED"
    boundary = got[
        got["scope"].eq(f"boundary/{policy}")
        & got["subject"].eq("B3_dual_target")
        & got["gate"].eq("post_im_sharpe_difference")
    ]
    assert len(boundary) == 1
    assert not bool(boundary["gate_pass"].iloc[0])
    unified = got[
        got["scope"].eq(f"boundary/{policy}")
        & got["subject"].eq("B3_unified")
        & got["gate"].eq("unified_executable_boundary")
    ]
    assert len(unified) == 1
    assert bool(unified["gate_pass"].iloc[0])


def test_bootstrap_gate_is_required_for_pass_shadow():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    policy = "legal_deadline"
    production_pass = _force_full_production_pass(
        frames,
        policy,
        "B3_unified",
    )
    bootstrap_failed = _force_bootstrap_failure(
        production_pass,
        policy,
        "B3_unified",
    )

    got = assemble_verdicts(comparison, bootstrap_failed, cfg)

    candidate = got[
        got["scope"].eq(f"candidate/{policy}")
        & got["subject"].eq("B3_unified")
        & got["gate"].eq("statistical_verdict")
    ].iloc[0]
    assert candidate["statistical_verdict"] == "MEASURE_ONLY"
    assert candidate["reason_code"] == "BOOTSTRAP_GATE_FAILED"
    audit = got[
        got["scope"].eq(f"bootstrap/{policy}")
        & got["subject"].eq("B3_unified")
        & got["gate"].eq("holm_adjusted_tail")
    ]
    assert len(audit) == 1
    assert not bool(audit["gate_pass"].iloc[0])


def test_verdict_assembler_preserves_every_statistical_gate_audit_row():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()

    got = assemble_verdicts(comparison, frames, cfg)

    policies = set(cfg["pit"]["policies"])
    expected_structure = comparison[
        comparison["pit_policy"].isin(policies)
        & comparison["gate_name"].ne("")
        & comparison["affects_verdict"]
    ]
    structure = got[got["scope"].str.startswith("structure/")].copy()
    assert len(structure) == len(expected_structure)
    assert sorted(
        zip(
            structure["scope"].str.removeprefix("structure/"),
            structure["subject"].str.split("|", regex=False).str[0],
            structure["gate"],
            structure["gate_pass"],
            strict=True,
        )
    ) == sorted(
        zip(
            expected_structure["pit_policy"],
            expected_structure["candidate"],
            expected_structure["gate_name"],
            expected_structure["gate_pass"],
            strict=True,
        )
    )

    expected_production = frames.production_metrics[
        frames.production_metrics["component"].eq("aggregate")
        & frames.production_metrics["window"].eq("2021-2023")
        & frames.production_metrics["affects_verdict"]
    ]
    production = got[got["scope"].str.startswith("production/")]
    assert len(production) == len(expected_production)
    assert sorted(
        zip(
            production["scope"].str.removeprefix("production/"),
            production["subject"],
            production["gate"],
            production["gate_pass"],
            strict=True,
        )
    ) == sorted(
        zip(
            expected_production["pit_policy"],
            expected_production["candidate"],
            expected_production["gate_name"],
            expected_production["gate_pass"],
            strict=True,
        )
    )
    assert got["scope"].str.startswith("bootstrap/").sum() == 4
    assert got["scope"].str.startswith("boundary/").sum() == 10
    assert not got.duplicated(["scope", "subject", "gate"]).any()


def test_pit_flip_preserves_statistics_but_blocks_final_and_shadow_start():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    headline = "legal_deadline"
    both_pass = _force_full_production_pass(
        frames,
        headline,
        "B3_unified",
    )
    both_pass = _force_full_production_pass(
        both_pass,
        headline,
        "B3_dual_target",
    )

    unblocked = assemble_verdicts(comparison, both_pass, cfg)
    unblocked_candidates = unblocked[
        unblocked["scope"].eq(f"candidate/{headline}")
        & unblocked["gate"].eq("statistical_verdict")
    ]
    assert len(unblocked_candidates) == 2
    assert unblocked_candidates["statistical_verdict"].eq(
        "PASS_SHADOW"
    ).all()
    assert unblocked_candidates["shadow_candidate"].eq(True).all()
    assert unblocked_candidates["shadow_start_allowed"].eq(True).all()
    unblocked_run = unblocked[
        unblocked["scope"].eq("run")
        & unblocked["subject"].eq("ALL")
        & unblocked["gate"].eq("final_verdict")
    ]
    assert len(unblocked_run) == 1
    assert unblocked_run["final_verdict"].iloc[0] == "PASS_SHADOW"

    flipped = comparison.copy(deep=True)
    pit = flipped["gate_name"].eq("PIT_POLICY_FLIP")
    assert pit.sum() == 1
    flipped.loc[pit, "gate_pass"] = False
    blocked = assemble_verdicts(flipped, both_pass, cfg)

    blocked_candidates = blocked[
        blocked["scope"].eq(f"candidate/{headline}")
        & blocked["gate"].eq("statistical_verdict")
    ]
    assert blocked_candidates["statistical_verdict"].eq(
        "PASS_SHADOW"
    ).all()
    assert blocked_candidates["shadow_candidate"].eq(True).all()
    assert blocked_candidates["shadow_start_allowed"].eq(False).all()
    blocked_run = blocked[
        blocked["scope"].eq("run")
        & blocked["subject"].eq("ALL")
        & blocked["gate"].eq("final_verdict")
    ]
    assert len(blocked_run) == 1
    assert blocked_run["statistical_verdict"].iloc[0] == "PASS_SHADOW"
    assert blocked_run["final_verdict"].iloc[0] == "DATA_BLOCKED"
    pit_audit = blocked[
        blocked["scope"].eq("run")
        & blocked["subject"].eq("ALL")
        & blocked["gate"].eq("PIT_POLICY_FLIP")
    ]
    assert len(pit_audit) == 1
    assert pit_audit["status"].iloc[0] == "DATA_BLOCKED"
    assert pit_audit["reason_code"].iloc[0] == "PIT_POLICY_FLIP"
    assert not bool(pit_audit["affects_statistical"].iloc[0])


def test_extra_run_blockers_are_deterministic_and_data_dominates_coverage():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    coverage = {
        "reason_code": "LEGAL_CROSS_SECTION_INFEASIBLE",
        "status": "COVERAGE_BLOCKED",
        "detail": "one fixed leg has fewer than 100 names",
        "affects_statistical": True,
    }
    data = {
        "reason_code": "TRUE_DISCLOSURE_COVERAGE",
        "status": "DATA_BLOCKED",
        "detail": "true first-disclosure dates are incomplete",
        "affects_statistical": False,
    }

    coverage_only = assemble_verdicts(
        comparison,
        frames,
        cfg,
        run_blockers=[coverage],
    )
    coverage_run = coverage_only[
        coverage_only["scope"].eq("run")
        & coverage_only["subject"].eq("ALL")
        & coverage_only["gate"].eq("final_verdict")
    ].iloc[0]
    assert coverage_run["statistical_verdict"] == "MEASURE_ONLY"
    assert coverage_run["final_verdict"] == "COVERAGE_BLOCKED"

    first = assemble_verdicts(
        comparison,
        frames,
        cfg,
        run_blockers=[coverage, data],
    )
    second = assemble_verdicts(
        comparison,
        frames,
        cfg,
        run_blockers=[data, coverage],
    )
    pd.testing.assert_frame_equal(first, second)
    run = first[
        first["scope"].eq("run")
        & first["subject"].eq("ALL")
        & first["gate"].eq("final_verdict")
    ].iloc[0]
    assert run["statistical_verdict"] == "MEASURE_ONLY"
    assert run["final_verdict"] == "DATA_BLOCKED"
    blocker_rows = first[first["scope"].eq("run/blocker")].set_index(
        "subject"
    )
    assert set(blocker_rows.index) == {
        "LEGAL_CROSS_SECTION_INFEASIBLE",
        "TRUE_DISCLOSURE_COVERAGE",
    }
    for blocker in (coverage, data):
        row = blocker_rows.loc[blocker["reason_code"]]
        assert row["gate"] == "run_blocker"
        assert not bool(row["gate_pass"])
        assert row["status"] == blocker["status"]
        assert row["detail"] == blocker["detail"]
        assert bool(row["affects_statistical"]) == blocker[
            "affects_statistical"
        ]
    assert not first["statistical_verdict"].eq("NOT_EVALUATED").any()


def test_final_run_blocker_reason_audit_includes_pit_and_orders_data_first():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()

    def final_row(verdicts):
        return verdicts[
            verdicts["scope"].eq("run")
            & verdicts["subject"].eq("ALL")
            & verdicts["gate"].eq("final_verdict")
        ].iloc[0]

    unblocked = final_row(assemble_verdicts(comparison, frames, cfg))
    assert unblocked["reason_code"] == ""
    assert unblocked["detail"] == ""

    flipped = comparison.copy(deep=True)
    pit = flipped["gate_name"].eq("PIT_POLICY_FLIP")
    assert pit.sum() == 1
    flipped.loc[pit, "gate_pass"] = False
    pit_only = final_row(assemble_verdicts(flipped, frames, cfg))
    assert pit_only["reason_code"] == "PIT_POLICY_FLIP"
    assert pit_only["detail"] == "PIT_POLICY_FLIP:DATA_BLOCKED"

    coverage = {
        "reason_code": "LEGAL_CROSS_SECTION_INFEASIBLE",
        "status": "COVERAGE_BLOCKED",
        "detail": "one fixed leg has fewer than 100 names",
        "affects_statistical": True,
    }
    coverage_only = final_row(
        assemble_verdicts(
            comparison,
            frames,
            cfg,
            run_blockers=[coverage],
        )
    )
    assert coverage_only["reason_code"] == (
        "LEGAL_CROSS_SECTION_INFEASIBLE"
    )
    assert coverage_only["detail"] == (
        "LEGAL_CROSS_SECTION_INFEASIBLE:COVERAGE_BLOCKED"
    )

    data = {
        "reason_code": "TRUE_DISCLOSURE_COVERAGE",
        "status": "DATA_BLOCKED",
        "detail": "true first-disclosure dates are incomplete",
        "affects_statistical": False,
    }
    multiple = final_row(
        assemble_verdicts(
            flipped,
            frames,
            cfg,
            run_blockers=[coverage, data],
        )
    )
    assert multiple["reason_code"] == "MULTIPLE_RUN_BLOCKERS"
    assert multiple["detail"] == (
        "PIT_POLICY_FLIP:DATA_BLOCKED,"
        "TRUE_DISCLOSURE_COVERAGE:DATA_BLOCKED,"
        "LEGAL_CROSS_SECTION_INFEASIBLE:COVERAGE_BLOCKED"
    )


def test_report_only_and_yearly_mutations_cannot_change_any_verdict():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    baseline = assemble_verdicts(comparison, frames, cfg)
    changed_comparison = comparison.copy(deep=True)
    report_model = (
        changed_comparison["window"].eq("2024-2026-report-only")
        & changed_comparison["model"].eq("M1")
    )
    assert report_model.any()
    changed_comparison.loc[report_model, "partial_ic"] = 0.75

    changed_frames = _copy_evaluation_frames(frames)
    report_production = (
        changed_frames.production_metrics["window"].eq(
            "2024-2026-report-only"
        )
        & changed_frames.production_metrics["candidate"].eq("B3_unified")
        & changed_frames.production_metrics["component"].eq("blend")
        & changed_frames.production_metrics["gate_name"].eq("")
    )
    assert report_production.any()
    changed_frames.production_metrics.loc[
        report_production,
        "ann_return",
    ] += 10.0
    report_year = (
        changed_frames.yearly["window"].eq("2024-2026-report-only")
        & changed_frames.yearly["candidate"].eq("B3_unified")
        & changed_frames.yearly["row_type"].eq("year")
    )
    assert report_year.any()
    changed_frames.yearly.loc[report_year, "ann_return"] += 10.0

    changed = assemble_verdicts(
        changed_comparison,
        changed_frames,
        cfg,
    )

    outcome = (
        baseline["scope"].str.startswith(("candidate/", "family/"))
        | (
            baseline["scope"].eq("run")
            & baseline["gate"].eq("final_verdict")
        )
    )
    changed_outcome = (
        changed["scope"].str.startswith(("candidate/", "family/"))
        | (
            changed["scope"].eq("run")
            & changed["gate"].eq("final_verdict")
        )
    )
    pd.testing.assert_frame_equal(
        baseline.loc[outcome].reset_index(drop=True),
        changed.loc[changed_outcome].reset_index(drop=True),
    )
    assert not changed.loc[
        changed["scope"].str.contains("report", case=False),
        "affects_statistical",
    ].any()


@pytest.mark.parametrize("without_report", ["evaluation", "model"])
def test_verdict_assembler_allows_independent_report_families(
    without_report,
):
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    baseline = assemble_verdicts(comparison, frames, cfg)
    changed_comparison = comparison
    changed_frames = frames
    if without_report == "evaluation":
        changed_frames = EvaluationFrames(
            production_metrics=frames.production_metrics.loc[
                ~frames.production_metrics["window"].eq(
                    "2024-2026-report-only"
                )
            ].reset_index(drop=True),
            bootstrap=frames.bootstrap,
            yearly=frames.yearly.loc[
                ~frames.yearly["window"].eq("2024-2026-report-only")
            ].reset_index(drop=True),
        )
    else:
        changed_comparison = comparison.loc[
            ~comparison["window"].eq("2024-2026-report-only")
        ].reset_index(drop=True)

    got = assemble_verdicts(changed_comparison, changed_frames, cfg)

    pd.testing.assert_frame_equal(got, baseline)


def test_verdict_assembler_rejects_partial_model_report_family():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    report = comparison["window"].eq("2024-2026-report-only")
    assert report.sum() > 1
    partial = comparison.drop(comparison.index[report][0]).reset_index(
        drop=True
    )

    with pytest.raises(DataBlocked, match="frozen domain"):
        assemble_verdicts(partial, frames, cfg)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("pit_policy", "rogue_policy"),
        ("candidate", "rogue_candidate"),
    ],
)
def test_verdict_assembler_rejects_rogue_model_domain_rows(column, value):
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    metric = comparison[
        comparison["gate_name"].eq("")
        & comparison["model"].eq("M1")
    ].iloc[[0]].copy()
    metric.loc[:, "q"] = ""
    metric.loc[:, "target"] = ""
    metric.loc[:, column] = value
    rogue = pd.concat([comparison, metric], ignore_index=True)

    with pytest.raises(DataBlocked, match="domain"):
        assemble_verdicts(rogue, frames, cfg)


@pytest.mark.parametrize("mutation", ["contradict", "extra"])
def test_verdict_assembler_rejects_invalid_q_leg_gate_set(mutation):
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    leg = (
        comparison["pit_policy"].eq("legal_deadline")
        & comparison["candidate"].eq("B3_unified")
        & comparison["q"].eq("qblend")
        & comparison["target"].eq("blend")
        & comparison["window"].eq("2021-2023")
        & comparison["model"].eq("M1")
        & comparison["gate_name"].eq("m1_increment")
    )
    assert leg.sum() == 1
    broken = comparison.copy(deep=True)
    if mutation == "contradict":
        broken.loc[leg, "gate_pass"] = False
    else:
        extra = broken.loc[leg].copy()
        extra.loc[:, "window"] = "2022"
        extra.loc[:, "gate_pass"] = False
        broken = pd.concat([broken, extra], ignore_index=True)

    with pytest.raises(DataBlocked, match="gate"):
        assemble_verdicts(broken, frames, cfg)


@pytest.mark.parametrize(
    ("source", "column"),
    [
        ("model", "candidate"),
        ("production", "component"),
        ("bootstrap", "candidate"),
        ("yearly", "window"),
    ],
)
def test_verdict_assembler_rejects_unhashable_row_id_values(
    source,
    column,
):
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    broken_comparison = comparison.copy(deep=True)
    broken_frames = _copy_evaluation_frames(frames)
    if source == "model":
        broken_comparison.at[broken_comparison.index[0], column] = [["bad"]]
        value = broken_comparison.at[broken_comparison.index[0], column]
    else:
        frame = getattr(
            broken_frames,
            {
                "production": "production_metrics",
                "bootstrap": "bootstrap",
                "yearly": "yearly",
            }[source],
        )
        frame.at[frame.index[0], column] = [["bad"]]
        value = frame.at[frame.index[0], column]
    with pytest.raises(TypeError):
        hash(value)

    with pytest.raises(DataBlocked):
        assemble_verdicts(
            broken_comparison,
            broken_frames,
            cfg,
        )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("candidates",), ("B3_unified", "B3_dual_target")),
        (
            ("windows", "confirmation"),
            ("2021-01-01", "2023-12-31"),
        ),
        (
            ("windows", "confirmation"),
            [np.str_("2021-01-01"), "2023-12-31"],
        ),
        (
            ("pit", "policies"),
            ("legal_deadline", "legal_deadline_plus_one_month_end"),
        ),
        (("pit", "industry_pit_start"), np.str_("2021-01-01")),
        (("execution", "annualization"), 245.0),
        (("execution", "annualization"), True),
        (("execution", "cost_bps"), 3),
        (("execution", "im_launch_date"), np.str_("2022-07-22")),
        (("production_gates", "sharpe_improvement"), np.float64(0.10)),
        (("production_gates", "post_im_min_days"), 252.0),
        (("bootstrap", "block_days"), np.int64(20)),
        (("bootstrap", "draws"), 5000.0),
        (("bootstrap", "seed"), 20260713.0),
        (("bootstrap", "adjusted_tail_max"), np.float64(0.10)),
    ],
)
def test_verdict_assembler_rejects_equivalent_wrong_config_types(
    path,
    replacement,
):
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    broken = deepcopy(cfg)
    target = broken
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement

    with pytest.raises(DataBlocked, match="type"):
        assemble_verdicts(comparison, frames, broken)


def test_verdict_assembler_invalid_schemas_and_types_fail_closed():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()

    with pytest.raises(DataBlocked, match="schema"):
        assemble_verdicts(
            comparison.assign(extra=1),
            frames,
            cfg,
        )

    missing_pit = comparison[
        ~comparison["gate_name"].eq("PIT_POLICY_FLIP")
    ].copy()
    with pytest.raises(DataBlocked, match="PIT"):
        assemble_verdicts(missing_pit, frames, cfg)

    bad_production = _copy_evaluation_frames(frames)
    production_gate = bad_production.production_metrics["gate_name"].ne("")
    bad_production.production_metrics["gate_pass"] = (
        bad_production.production_metrics["gate_pass"].astype(object)
    )
    bad_production.production_metrics.loc[
        production_gate.idxmax(),
        "gate_pass",
    ] = 1
    with pytest.raises(RuntimeError, match="bool-only"):
        assemble_verdicts(comparison, bad_production, cfg)

    bad_bootstrap = _copy_evaluation_frames(frames)
    bad_bootstrap.bootstrap["gate_pass"] = (
        bad_bootstrap.bootstrap["gate_pass"].astype(object)
    )
    bad_bootstrap.bootstrap.loc[
        bad_bootstrap.bootstrap.index[0],
        "gate_pass",
    ] = 1
    with pytest.raises(RuntimeError, match="boolean"):
        assemble_verdicts(comparison, bad_bootstrap, cfg)

    bad_yearly = _copy_evaluation_frames(frames)
    bad_yearly.yearly.loc[
        bad_yearly.yearly.index[0],
        "affects_verdict",
    ] = True
    with pytest.raises(RuntimeError, match="cannot affect verdict"):
        assemble_verdicts(comparison, bad_yearly, cfg)

    invalid_blockers = [
        [
            {
                "reason_code": "MISSING_DETAIL",
                "status": "DATA_BLOCKED",
                "affects_statistical": False,
            }
        ],
        [
            {
                "reason_code": "BAD_STATUS",
                "status": "STOP",
                "detail": "",
                "affects_statistical": False,
            }
        ],
        [
            {
                "reason_code": "NON_STRING_STATUS",
                "status": [],
                "detail": "",
                "affects_statistical": False,
            }
        ],
        [
            {
                "reason_code": "BAD_BOOL",
                "status": "DATA_BLOCKED",
                "detail": "",
                "affects_statistical": 1,
            }
        ],
    ]
    for blockers in invalid_blockers:
        with pytest.raises(ValueError):
            assemble_verdicts(
                comparison,
                frames,
                cfg,
                run_blockers=blockers,
            )


def test_verdict_output_uses_strict_bool_or_null_gate_values():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()

    got = assemble_verdicts(comparison, frames, cfg)

    assert got["gate_pass"].map(
        lambda value: pd.isna(value)
        or isinstance(value, (bool, np.bool_))
    ).all()
    for column in (
        "provisional",
        "affects_statistical",
        "shadow_candidate",
        "shadow_start_allowed",
    ):
        assert got[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all()


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


def test_verdict_assembler_detects_observable_structure_policy_flip():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    changed_comparison, changed_frames = _force_structure_failure(
        comparison,
        frames,
        "legal_deadline_plus_one_month_end",
        "B3_unified",
    )
    source_pit = changed_comparison["gate_name"].eq("PIT_POLICY_FLIP")
    assert source_pit.sum() == 1
    assert bool(changed_comparison.loc[source_pit, "gate_pass"].iloc[0])

    got = assemble_verdicts(changed_comparison, changed_frames, cfg)

    statistical = got[
        got["scope"].eq(
            "candidate/legal_deadline_plus_one_month_end"
        )
        & got["subject"].eq("B3_unified")
        & got["gate"].eq("statistical_verdict")
    ].iloc[0]
    assert statistical["statistical_verdict"] == "STOP"
    run = got[
        got["scope"].eq("run")
        & got["subject"].eq("ALL")
        & got["gate"].eq("final_verdict")
    ].iloc[0]
    assert run["statistical_verdict"] == "MEASURE_ONLY"
    assert run["final_verdict"] == "DATA_BLOCKED"
    assert run["reason_code"] == "PIT_POLICY_FLIP"
    pit_audit = got[
        got["scope"].eq("run")
        & got["subject"].eq("ALL")
        & got["gate"].eq("PIT_POLICY_FLIP")
    ].iloc[0]
    assert not bool(pit_audit["gate_pass"])


def test_verdict_assembler_recomputes_increment_direction_policy_flip():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    baseline = assemble_verdicts(comparison, frames, cfg)
    changed = comparison.copy(deep=True)
    policies = cfg["pit"]["policies"]
    for policy, m1_oos in zip(policies, (0.1, -0.1), strict=True):
        metric = (
            changed["pit_policy"].eq(policy)
            & changed["q"].eq("qblend")
            & changed["window"].eq("2021-2023")
            & changed["gate_name"].eq("")
            & changed["model"].isin(["M0", "M1"])
        )
        assert metric.sum() == 2
        changed.loc[metric & changed["model"].eq("M0"), "oos_r2"] = 0.0
        changed.loc[metric & changed["model"].eq("M1"), "oos_r2"] = m1_oos
    source_pit = changed["gate_name"].eq("PIT_POLICY_FLIP")
    assert source_pit.sum() == 1
    assert bool(changed.loc[source_pit, "gate_pass"].iloc[0])

    got = assemble_verdicts(changed, frames, cfg)

    def candidate_rows(output):
        return output[
            output["gate"].eq("statistical_verdict")
            & output["scope"].str.startswith(("candidate/", "family/"))
        ].reset_index(drop=True)

    pd.testing.assert_frame_equal(
        candidate_rows(got),
        candidate_rows(baseline),
    )
    run = got[
        got["scope"].eq("run")
        & got["subject"].eq("ALL")
        & got["gate"].eq("final_verdict")
    ].iloc[0]
    assert run["final_verdict"] == "DATA_BLOCKED"
    assert run["reason_code"] == "PIT_POLICY_FLIP"


def test_verdict_assembler_never_reverses_source_pit_failure():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    changed = comparison.copy(deep=True)
    source_pit = changed["gate_name"].eq("PIT_POLICY_FLIP")
    assert source_pit.sum() == 1
    changed.loc[source_pit, "gate_pass"] = False

    got = assemble_verdicts(changed, frames, cfg)

    pit_audit = got[
        got["scope"].eq("run")
        & got["subject"].eq("ALL")
        & got["gate"].eq("PIT_POLICY_FLIP")
    ].iloc[0]
    assert not bool(pit_audit["gate_pass"])
    run = got[
        got["scope"].eq("run")
        & got["subject"].eq("ALL")
        & got["gate"].eq("final_verdict")
    ].iloc[0]
    assert run["final_verdict"] == "DATA_BLOCKED"
    assert run["reason_code"] == "PIT_POLICY_FLIP"


def test_verdict_assembler_rejects_reserved_external_pit_blocker():
    cfg, _, _, comparison, _, _, _, frames = _build_fixture()
    changed = comparison.copy(deep=True)
    source_pit = changed["gate_name"].eq("PIT_POLICY_FLIP")
    assert source_pit.sum() == 1
    changed.loc[source_pit, "gate_pass"] = False
    reserved = {
        "reason_code": "PIT_POLICY_FLIP",
        "status": "DATA_BLOCKED",
        "detail": "external duplicate",
        "affects_statistical": False,
    }

    with pytest.raises(ValueError, match="reserved"):
        assemble_verdicts(
            changed,
            frames,
            cfg,
            run_blockers=[reserved],
        )


def test_verdict_helpers_reject_unhashable_labels_as_value_errors():
    with pytest.raises(ValueError, match="candidate labels"):
        family_best_wins([[]])
    with pytest.raises(ValueError, match="statistical verdict"):
        final_verdict([], False, False)


_EXPECTED_CONFIG_HASH = "a" * 64
_NO_DATABASE_EVIDENCE = object()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preflight_blocker(status="DATA_BLOCKED", reason_code="DATA_CONTRACT"):
    return {
        "pit_policy": "all",
        "formation_date": "NaT",
        "required_formation": True,
        "affects_final": True,
        "check": "snapshot_source",
        "side": "",
        "eligible_count": None,
        "max_weight": None,
        "status": status,
        "reason_code": reason_code,
        "detail": "frozen test blocker",
    }


def _database_evidence():
    return {
        "consumed_sources": ["public.trading_calendar"],
        "sources": {
            "public.trading_calendar": {
                "query_template_hash": TRADING_CALENDAR_QUERY_TEMPLATE_HASH,
                "row_count": 10,
                "min_date": "2013-05-01",
                "max_date": "2026-07-31",
            }
        },
    }


def _write_preflight_manifest(
    root,
    *,
    status="OK",
    blockers=None,
    data_end="2026-07-10",
    database_evidence=_NO_DATABASE_EVIDENCE,
    config_hash_value=_EXPECTED_CONFIG_HASH,
):
    root.mkdir(parents=True, exist_ok=True)
    coverage = root / "coverage_audit.csv"
    diagnostics = root / "exposure_diagnostics.csv"
    coverage.write_text("status\nOK\n", encoding="utf-8")
    diagnostics.write_text("scope\nexposure\n", encoding="utf-8")
    payload = {
        "stage": "preflight",
        "config_hash": config_hash_value,
        "data_end": data_end,
        "status": status,
        "blockers": [] if blockers is None else blockers,
        "outputs": {
            "coverage_audit.csv": _sha256(coverage),
            "exposure_diagnostics.csv": _sha256(diagnostics),
        },
    }
    if database_evidence is not _NO_DATABASE_EVIDENCE:
        payload["database_source_evidence"] = database_evidence
    manifest_dir = root / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / "preflight.json"
    manifest_path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path, payload


def _rewrite_json(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_preflight_contract_verifies_outputs_and_inherits_manifest_date(tmp_path):
    manifest_path, payload = _write_preflight_manifest(tmp_path)

    got = verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)

    assert isinstance(got, PreflightManifestContract)
    assert got.status == "OK"
    assert got.data_end == "2026-07-10"
    assert got.blockers == ()
    assert got.database_source_evidence is None
    assert got.manifest_hash == _sha256(manifest_path)
    assert dict(got.output_hashes) == payload["outputs"]
    with pytest.raises(TypeError):
        got.output_hashes["coverage_audit.csv"] = "b" * 64


def test_preflight_contract_rejects_duplicate_json_keys(tmp_path):
    manifest_path, _ = _write_preflight_manifest(tmp_path)
    rendered = manifest_path.read_text(encoding="utf-8")
    rendered = rendered.replace(
        '"stage": "preflight"',
        '"stage": "preflight", "stage": "preflight"',
        1,
    )
    manifest_path.write_text(rendered, encoding="utf-8")

    with pytest.raises(DataBlocked, match="strict JSON"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_missing_preflight_manifest_never_hashes_untrusted_outputs(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "coverage_audit.csv").write_text("untrusted")
    (tmp_path / "exposure_diagnostics.csv").write_text("untrusted")
    hashes = []

    def spy(path):
        hashes.append(path)
        raise AssertionError("unverified output was hashed")

    monkeypatch.setattr(b3_eval_module, "_sha256_file", spy)

    with pytest.raises(DataBlocked, match="manifest.*missing"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)
    assert hashes == []


@pytest.mark.parametrize(
    "status,blockers",
    [
        ("COVERAGE_BLOCKED", [_preflight_blocker("COVERAGE_BLOCKED")]),
        ("DATA_BLOCKED", [_preflight_blocker("DATA_BLOCKED")]),
        (
            "DATA_BLOCKED",
            [
                _preflight_blocker("COVERAGE_BLOCKED", "LEGAL_COVERAGE"),
                _preflight_blocker("DATA_BLOCKED"),
            ],
        ),
    ],
)
def test_preflight_contract_accepts_blocked_status_with_exact_priority(
    tmp_path,
    status,
    blockers,
):
    _write_preflight_manifest(tmp_path, status=status, blockers=blockers)

    got = verify_preflight_manifest(
        tmp_path,
        _EXPECTED_CONFIG_HASH,
        pd.Timestamp("2026-07-10"),
    )

    assert got.status == status
    assert tuple(blocker["status"] for blocker in got.blockers) == tuple(
        blocker["status"] for blocker in blockers
    )
    with pytest.raises(TypeError):
        got.blockers[0]["detail"] = "changed"


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("stage", "exposures", "stage"),
        ("config_hash", "b" * 64, "config hash"),
        ("config_hash", "A" * 64, "config hash"),
        ("data_end", "2026-7-10", "data_end"),
        ("data_end", True, "data_end"),
        ("status", "PASS", "status"),
        ("blockers", {}, "blockers"),
    ],
)
def test_preflight_contract_rejects_manifest_contract_tampering(
    tmp_path,
    field,
    value,
    match,
):
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    payload[field] = value
    _rewrite_json(manifest_path, payload)

    with pytest.raises(DataBlocked, match=match):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


@pytest.mark.parametrize("requested", ["2026-07-09", True, 20260710])
def test_preflight_contract_rejects_nonexact_requested_data_end(
    tmp_path,
    requested,
):
    _write_preflight_manifest(tmp_path)

    with pytest.raises(DataBlocked, match="requested_data_end|data_end mismatch"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, requested)


def test_preflight_contract_requires_both_bound_outputs(tmp_path):
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    payload["outputs"].pop("exposure_diagnostics.csv")
    _rewrite_json(manifest_path, payload)

    with pytest.raises(DataBlocked, match="required outputs"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_preflight_contract_rejects_bad_hash_and_file_tamper(tmp_path):
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    payload["outputs"]["coverage_audit.csv"] = "A" * 64
    _rewrite_json(manifest_path, payload)
    with pytest.raises(DataBlocked, match="hash format"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)

    payload["outputs"]["coverage_audit.csv"] = _sha256(
        tmp_path / "coverage_audit.csv"
    )
    _rewrite_json(manifest_path, payload)
    (tmp_path / "coverage_audit.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(DataBlocked, match="hash mismatch"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


@pytest.mark.parametrize("unsafe", ["/tmp/escape.csv", "../escape.csv"])
def test_preflight_contract_rejects_unsafe_output_paths(tmp_path, unsafe):
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    payload["outputs"][unsafe] = "b" * 64
    _rewrite_json(manifest_path, payload)

    with pytest.raises(DataBlocked, match="unsafe"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_preflight_contract_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-preflight.csv"
    outside.write_text("outside", encoding="utf-8")
    manifest_path, payload = _write_preflight_manifest(tmp_path)
    (tmp_path / "escape.csv").symlink_to(outside)
    payload["outputs"]["escape.csv"] = _sha256(outside)
    _rewrite_json(manifest_path, payload)

    with pytest.raises(DataBlocked, match="escapes"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_preflight_contract_rejects_inconsistent_blocker_priority(tmp_path):
    _write_preflight_manifest(
        tmp_path,
        status="DATA_BLOCKED",
        blockers=[_preflight_blocker("COVERAGE_BLOCKED")],
    )

    with pytest.raises(DataBlocked, match="priority"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_missing_database_evidence_is_a_deterministic_run_blocker(tmp_path):
    _write_preflight_manifest(tmp_path)
    contract = verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)

    assert database_source_evidence_blocker(contract) == {
        "reason_code": "DATABASE_SOURCE_EVIDENCE_MISSING",
        "status": "DATA_BLOCKED",
        "detail": "verified preflight manifest lacks database_source_evidence",
        "affects_statistical": False,
    }


def test_database_evidence_is_verified_and_frozen_without_database_access(tmp_path):
    evidence = _database_evidence()
    _write_preflight_manifest(tmp_path, database_evidence=evidence)

    contract = verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)

    assert contract.database_source_evidence == evidence
    assert database_source_evidence_blocker(contract) is None
    with pytest.raises(TypeError):
        contract.database_source_evidence["sources"] = {}
    with pytest.raises(TypeError):
        contract.database_source_evidence["sources"][
            "public.trading_calendar"
        ]["row_count"] = 0


def test_database_evidence_rejects_partial_source_set(tmp_path):
    evidence = _database_evidence()
    evidence["consumed_sources"].append("market.index_daily")
    _write_preflight_manifest(tmp_path, database_evidence=evidence)

    with pytest.raises(DataBlocked, match="consumed_sources"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_database_evidence_rejects_tampered_calendar_query(tmp_path):
    evidence = _database_evidence()
    evidence["sources"]["public.trading_calendar"][
        "query_template_hash"
    ] = "b" * 64
    _write_preflight_manifest(tmp_path, database_evidence=evidence)

    with pytest.raises(DataBlocked, match="trading_calendar.*query"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def test_database_evidence_requires_trading_calendar(tmp_path):
    evidence = _database_evidence()
    evidence["consumed_sources"] = ["market.index_daily"]
    evidence["sources"] = {
        "market.index_daily": evidence["sources"].pop(
            "public.trading_calendar"
        )
    }
    _write_preflight_manifest(tmp_path, database_evidence=evidence)

    with pytest.raises(DataBlocked, match="public.trading_calendar"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("row_count", True),
        ("row_count", -1),
        ("min_date", True),
        ("min_date", "2026-08-01"),
        ("extra", None),
    ],
)
def test_database_evidence_rejects_wrong_source_fields(tmp_path, field, value):
    evidence = _database_evidence()
    evidence["sources"]["public.trading_calendar"][field] = value
    _write_preflight_manifest(tmp_path, database_evidence=evidence)

    with pytest.raises(DataBlocked, match="database source evidence"):
        verify_preflight_manifest(tmp_path, _EXPECTED_CONFIG_HASH, None)


def _write_structure_manifest(root, *, data_end="2026-07-10"):
    root.mkdir(parents=True, exist_ok=True)
    coefficients = root / "structure_coefficients.csv"
    comparison = root / "model_comparison.csv"
    coefficients.write_text("model,coefficient\nM1,0.25\n", encoding="utf-8")
    comparison.write_text("model,oos_r2\nM1,0.10\n", encoding="utf-8")
    payload = {
        "stage": "structure",
        "config_hash": _EXPECTED_CONFIG_HASH,
        "data_end": data_end,
        "status": "OK",
        "outputs": {
            "model_comparison.csv": _sha256(comparison),
            "structure_coefficients.csv": _sha256(coefficients),
        },
    }
    manifest = root / "structure_manifest.json"
    _rewrite_json(manifest, payload)
    return manifest, payload


def test_structure_provenance_verifies_before_returning_frames(tmp_path):
    manifest, payload = _write_structure_manifest(tmp_path)

    got = verify_structure_provenance(
        tmp_path,
        _EXPECTED_CONFIG_HASH,
        "2026-07-10",
    )

    assert isinstance(got, StructureProvenanceContract)
    assert got.data_end == "2026-07-10"
    assert got.manifest_hash == _sha256(manifest)
    assert dict(got.output_hashes) == payload["outputs"]
    assert got.structure_coefficients.to_dict("records") == [
        {"model": "M1", "coefficient": 0.25}
    ]
    assert got.model_comparison.to_dict("records") == [
        {"model": "M1", "oos_r2": 0.10}
    ]


def test_missing_structure_manifest_never_reads_untrusted_csv(tmp_path, monkeypatch):
    (tmp_path / "structure_coefficients.csv").write_text(
        "model,coefficient\nM1,99\n",
        encoding="utf-8",
    )
    (tmp_path / "model_comparison.csv").write_text(
        "model,oos_r2\nM1,99\n",
        encoding="utf-8",
    )
    reads = []

    def spy(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("unverified CSV was read")

    monkeypatch.setattr(b3_eval_module.pd, "read_csv", spy)

    with pytest.raises(DataBlocked, match="STRUCTURE_PROVENANCE_MISSING"):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )
    assert reads == []


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("stage", "preflight", "stage"),
        ("config_hash", "b" * 64, "config hash"),
        ("data_end", "2026-07-09", "data_end"),
        ("data_end", True, "data_end"),
        ("status", "DATA_BLOCKED", "status"),
    ],
)
def test_structure_provenance_rejects_manifest_tampering(
    tmp_path,
    field,
    value,
    match,
):
    manifest, payload = _write_structure_manifest(tmp_path)
    payload[field] = value
    _rewrite_json(manifest, payload)

    with pytest.raises(DataBlocked, match=match):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )


def test_structure_provenance_rejects_partial_and_extra_outputs(tmp_path):
    manifest, payload = _write_structure_manifest(tmp_path)
    payload["outputs"].pop("model_comparison.csv")
    _rewrite_json(manifest, payload)
    with pytest.raises(DataBlocked, match="output set"):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )

    payload["outputs"]["model_comparison.csv"] = _sha256(
        tmp_path / "model_comparison.csv"
    )
    payload["outputs"]["extra.csv"] = "b" * 64
    _rewrite_json(manifest, payload)
    with pytest.raises(DataBlocked, match="output set"):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )


def test_structure_provenance_rejects_hash_or_file_tamper(tmp_path):
    manifest, payload = _write_structure_manifest(tmp_path)
    payload["outputs"]["model_comparison.csv"] = "A" * 64
    _rewrite_json(manifest, payload)
    with pytest.raises(DataBlocked, match="hash format"):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )

    payload["outputs"]["model_comparison.csv"] = _sha256(
        tmp_path / "model_comparison.csv"
    )
    _rewrite_json(manifest, payload)
    (tmp_path / "model_comparison.csv").write_text(
        "model,oos_r2\nM1,0.99\n",
        encoding="utf-8",
    )
    with pytest.raises(DataBlocked, match="hash mismatch"):
        verify_structure_provenance(
            tmp_path,
            _EXPECTED_CONFIG_HASH,
            "2026-07-10",
        )


_PIT_POLICIES = [
    "legal_deadline",
    "legal_deadline_plus_one_month_end",
]


def _required_disclosure_rows(verified=True):
    rows = []
    for policy in _PIT_POLICIES:
        for period in pd.period_range("2014-01", "2023-12", freq="M"):
            for ticker in ("000001.SZ", "600000.SH"):
                rows.append(
                    {
                        "universe_role": "model",
                        "pit_policy": policy,
                        "formation_date": str(period.to_timestamp("M").date()),
                        "ticker": ticker,
                        "true_first_disclosure_verified": verified,
                    }
                )
    return pd.DataFrame(rows)


def test_true_disclosure_coverage_counts_explicit_mixed_booleans_only():
    frame = _required_disclosure_rows(True)
    frame.loc[0, "true_first_disclosure_verified"] = False
    frame = pd.concat(
        [
            frame,
            pd.DataFrame(
                [
                    {
                        "universe_role": "size_only",
                        "pit_policy": _PIT_POLICIES[0],
                        "formation_date": "2014-01-31",
                        "ticker": "000002.SZ",
                        "true_first_disclosure_verified": False,
                    },
                    {
                        "universe_role": "model",
                        "pit_policy": _PIT_POLICIES[0],
                        "formation_date": "2024-01-31",
                        "ticker": "000002.SZ",
                        "true_first_disclosure_verified": False,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )

    got = compute_true_disclosure_coverage(frame, _PIT_POLICIES)

    assert got == {
        "verified_numerator": 479,
        "required_denominator": 480,
        "ratio": 479 / 480,
        "coverage_basis": TRUE_DISCLOSURE_COVERAGE_BASIS,
    }
    json.dumps(got, sort_keys=True, allow_nan=False)


@pytest.mark.parametrize("verified,ratio", [(False, 0.0), (True, 1.0)])
def test_true_disclosure_coverage_handles_conservative_and_verified_extremes(
    verified,
    ratio,
):
    got = compute_true_disclosure_coverage(
        _required_disclosure_rows(verified),
        _PIT_POLICIES,
    )

    assert got["ratio"] == ratio
    assert got["verified_numerator"] == (480 if verified else 0)
    assert got["required_denominator"] == 480


def test_true_disclosure_coverage_blocks_missing_required_month_or_policy():
    frame = _required_disclosure_rows()
    missing_month = ~(
        frame["pit_policy"].eq(_PIT_POLICIES[1])
        & frame["formation_date"].eq("2014-01-31")
    )
    with pytest.raises(DataBlocked, match="required month"):
        compute_true_disclosure_coverage(frame.loc[missing_month], _PIT_POLICIES)

    with pytest.raises(DataBlocked, match="policy"):
        compute_true_disclosure_coverage(
            frame.loc[frame["pit_policy"].eq(_PIT_POLICIES[0])],
            _PIT_POLICIES,
        )


def test_true_disclosure_coverage_blocks_empty_denominator():
    frame = _required_disclosure_rows().assign(universe_role="size_only")

    with pytest.raises(DataBlocked, match="denominator"):
        compute_true_disclosure_coverage(frame, _PIT_POLICIES)


@pytest.mark.parametrize(
    "column,value,match",
    [
        ("true_first_disclosure_verified", 1, "boolean"),
        ("formation_date", True, "formation_date"),
        ("ticker", "000001", "ticker"),
        ("pit_policy", " legal_deadline", "policy"),
    ],
)
def test_true_disclosure_coverage_rejects_noncanonical_model_keys(
    column,
    value,
    match,
):
    frame = _required_disclosure_rows()
    frame[column] = frame[column].astype(object)
    frame.loc[0, column] = value

    with pytest.raises(DataBlocked, match=match):
        compute_true_disclosure_coverage(frame, _PIT_POLICIES)


def test_true_disclosure_coverage_rejects_duplicate_model_keys():
    frame = _required_disclosure_rows()
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

    with pytest.raises(DataBlocked, match="duplicate"):
        compute_true_disclosure_coverage(frame, _PIT_POLICIES)


def _salg_rows(*source_end_dates, formation_date="2026-06-30"):
    return pd.DataFrame(
        {
            "universe_role": ["model"] * len(source_end_dates),
            "formation_date": [formation_date] * len(source_end_dates),
            "salg_source_end_date": list(source_end_dates),
        }
    )


@pytest.mark.parametrize(
    "source_end,expected",
    [
        ("2025-06-30", "2025-10-31"),
        ("2026-03-31", "2026-08-31"),
        ("2025-12-31", "2026-04-30"),
        ("2025-09-30", "2026-04-30"),
    ],
)
def test_salg_valid_through_uses_frozen_quarter_mapping(source_end, expected):
    assert salg_valid_through(_salg_rows(source_end)) == expected


def test_salg_valid_through_takes_earliest_latest_formation_dependency():
    frame = _salg_rows("2026-03-31", "2025-12-31")
    frame = pd.concat(
        [
            _salg_rows(None, formation_date="2026-05-29"),
            frame,
            pd.DataFrame(
                [
                    {
                        "universe_role": "size_only",
                        "formation_date": "2026-06-30",
                        "salg_source_end_date": None,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    assert salg_valid_through(frame) == "2026-04-30"


@pytest.mark.parametrize("source_end", [None, "2025-06-29", True])
def test_salg_valid_through_rejects_missing_or_nonquarter_latest_dependency(
    source_end,
):
    with pytest.raises(DataBlocked, match="salg_source_end_date|quarter-end"):
        salg_valid_through(_salg_rows(source_end))


def _carry_series(dates, values=None):
    index = pd.DatetimeIndex(dates)
    if values is None:
        values = np.arange(len(index), dtype=float)
    return pd.Series(values, index=index, dtype=float)


def test_raw_carry_freshness_records_both_raw_maxima_and_is_json_safe():
    ic = _carry_series(["2026-07-20", "2026-07-21"])
    im = _carry_series(["2026-07-20", "2026-07-22"])

    got = compute_raw_carry_freshness(ic, im, "2026-07-21")

    assert got == {
        "ic_carry_max_date": "2026-07-21",
        "im_carry_max_date": "2026-07-22",
        "expected_latest_cash_date": "2026-07-21",
        "fresh": True,
    }
    assert json.loads(json.dumps(got, allow_nan=False)) == got


def test_raw_carry_freshness_blocks_stale_raw_even_if_filled_panel_looks_current():
    raw = _carry_series(["2026-07-17", "2026-07-20"])
    market = pd.bdate_range("2026-07-17", "2026-07-22")
    filled = raw.reindex(market).ffill()
    assert filled.last_valid_index() == pd.Timestamp("2026-07-22")

    got = compute_raw_carry_freshness(raw, raw, "2026-07-22")

    assert got["ic_carry_max_date"] == "2026-07-20"
    assert got["im_carry_max_date"] == "2026-07-20"
    assert got["fresh"] is False


def test_raw_carry_freshness_treats_empty_leg_as_missing_not_as_exception():
    ic = _carry_series(["2026-07-22"])
    im = pd.Series(dtype=float, index=pd.DatetimeIndex([]))

    got = compute_raw_carry_freshness(ic, im, "2026-07-22")

    assert got["ic_carry_max_date"] == "2026-07-22"
    assert got["im_carry_max_date"] is None
    assert got["fresh"] is False


def test_raw_carry_freshness_ignores_internal_gaps_when_raw_maxima_are_current():
    ic = _carry_series(["2026-07-17", "2026-07-22"])
    im = _carry_series(["2026-07-16", "2026-07-22"])

    got = compute_raw_carry_freshness(ic, im, pd.Timestamp("2026-07-22"))

    assert got["fresh"] is True


@pytest.mark.parametrize(
    "raw,expected,match",
    [
        ([1.0], "2026-07-22", "Series"),
        (
            _carry_series(["2026-07-22"]).tz_localize("Asia/Shanghai"),
            "2026-07-22",
            "timezone",
        ),
        (
            _carry_series([pd.Timestamp("2026-07-22 01:00:00")]),
            "2026-07-22",
            "normalized",
        ),
        (
            _carry_series(["2026-07-22", "2026-07-22"]),
            "2026-07-22",
            "unique",
        ),
        (
            _carry_series(["2026-07-22", "2026-07-21"]),
            "2026-07-22",
            "increasing",
        ),
        (
            _carry_series(["2026-07-22"], [np.nan]),
            "2026-07-22",
            "finite",
        ),
        (_carry_series(["2026-07-22"]), True, "expected_latest_cash_date"),
    ],
)
def test_raw_carry_freshness_rejects_invalid_raw_contracts(raw, expected, match):
    valid = _carry_series(["2026-07-22"])

    with pytest.raises(DataBlocked, match=match):
        compute_raw_carry_freshness(raw, valid, expected)


def _full_disclosure_coverage():
    return {
        "verified_numerator": 480,
        "required_denominator": 480,
        "ratio": 1.0,
        "coverage_basis": TRUE_DISCLOSURE_COVERAGE_BASIS,
    }


def _fresh_carry_evidence():
    return {
        "ic_carry_max_date": "2026-07-21",
        "im_carry_max_date": "2026-07-21",
        "expected_latest_cash_date": "2026-07-21",
        "fresh": True,
    }


def test_freshness_blockers_are_empty_only_when_all_three_contracts_are_fresh():
    got = freshness_blockers(
        _full_disclosure_coverage(),
        "2026-07-22",
        "2026-07-22",
        _fresh_carry_evidence(),
    )

    assert got == []


def test_freshness_blockers_have_exact_schema_unique_deterministic_order():
    coverage = _full_disclosure_coverage()
    coverage.update(
        {
            "verified_numerator": 479,
            "ratio": 479 / 480,
        }
    )
    carry = _fresh_carry_evidence()
    carry.update(
        {
            "im_carry_max_date": None,
            "fresh": False,
        }
    )

    got = freshness_blockers(
        coverage,
        "2026-07-21",
        "2026-07-22",
        carry,
    )

    assert [row["reason_code"] for row in got] == [
        "CARRY_FRESHNESS",
        "SALG_FRESHNESS",
        "TRUE_DISCLOSURE_COVERAGE",
    ]
    assert all(
        set(row)
        == {"reason_code", "status", "detail", "affects_statistical"}
        for row in got
    )
    assert all(row["status"] == "DATA_BLOCKED" for row in got)
    assert all(row["affects_statistical"] is False for row in got)
    assert len({row["reason_code"] for row in got}) == len(got)
    assert json.loads(json.dumps(got, allow_nan=False)) == got


@pytest.mark.parametrize(
    "mutator,match",
    [
        (
            lambda coverage, carry: coverage.update(
                {"verified_numerator": True}
            ),
            "verified_numerator",
        ),
        (
            lambda coverage, carry: coverage.update({"ratio": np.nan}),
            "ratio",
        ),
        (
            lambda coverage, carry: coverage.update({"unexpected": 1}),
            "coverage schema",
        ),
        (
            lambda coverage, carry: carry.update({"fresh": 1}),
            "fresh",
        ),
        (
            lambda coverage, carry: carry.update(
                {"ic_carry_max_date": "2026-7-21"}
            ),
            "ic_carry_max_date",
        ),
        (
            lambda coverage, carry: carry.update({"unexpected": 1}),
            "carry freshness schema",
        ),
    ],
)
def test_freshness_blockers_reject_tampered_evidence(mutator, match):
    coverage = _full_disclosure_coverage()
    carry = _fresh_carry_evidence()
    mutator(coverage, carry)

    with pytest.raises(DataBlocked, match=match):
        freshness_blockers(
            coverage,
            "2026-07-22",
            "2026-07-22",
            carry,
        )


def test_hash_files_returns_sorted_deterministic_json_safe_hashes(tmp_path):
    root = tmp_path / "run"
    (root / "inputs").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "inputs" / "raw_ic.csv").write_text("ic\n1\n")
    (root / "inputs" / "raw_im.csv").write_text("im\n2\n")
    (root / "outputs" / "candidate.csv").write_text("candidate\n3\n")
    paths = [
        "outputs/candidate.csv",
        "inputs/raw_im.csv",
        "inputs/raw_ic.csv",
    ]

    first = hash_files(root, paths)
    second = hash_files(root, list(reversed(paths)))

    assert first == second
    assert list(first) == sorted(paths)
    assert first["inputs/raw_ic.csv"] == _sha256(
        root / "inputs" / "raw_ic.csv"
    )
    assert all(len(value) == 64 and value == value.lower() for value in first.values())
    assert json.loads(json.dumps(first, allow_nan=False)) == first


@pytest.mark.parametrize(
    "paths,match",
    [
        ([], "non-empty"),
        (["missing.csv"], "missing"),
        (["/absolute.csv"], "unsafe"),
        (["../escape.csv"], "unsafe"),
        (["inputs/../escape.csv"], "unsafe"),
        (["same.csv", "same.csv"], "unique"),
        ([True], "unsafe"),
    ],
)
def test_hash_files_rejects_incomplete_or_unsafe_registrations(
    tmp_path,
    paths,
    match,
):
    root = tmp_path / "run"
    root.mkdir()
    (root / "same.csv").write_text("same")

    with pytest.raises(DataBlocked, match=match):
        hash_files(root, paths)


def test_hash_files_rejects_symlink_escape(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("outside")
    (root / "linked.csv").symlink_to(outside)

    with pytest.raises(DataBlocked, match="escapes"):
        hash_files(root, ["linked.csv"])


def _run_blocker(reason_code, status="DATA_BLOCKED", affects_statistical=False):
    return {
        "reason_code": reason_code,
        "status": status,
        "detail": f"{reason_code} detail",
        "affects_statistical": affects_statistical,
    }


def test_blocked_verdict_rows_report_every_blocker_and_one_final_row():
    rows = blocked_verdict_rows(
        [
            _run_blocker("CARRY_FRESHNESS"),
            _run_blocker("LEGAL_COVERAGE", status="COVERAGE_BLOCKED"),
        ]
    )

    assert list(rows.columns) == VERDICT_COLUMNS
    blockers = rows.loc[rows["scope"].eq("run/blocker")]
    assert sorted(blockers["subject"]) == ["CARRY_FRESHNESS", "LEGAL_COVERAGE"]
    assert not blockers["gate_pass"].any()
    final = rows.loc[rows["gate"].eq("final_verdict")]
    assert len(final) == 1
    assert final["statistical_verdict"].isna().all()
    assert final["final_verdict"].iloc[0] == "DATA_BLOCKED"


def test_blocked_verdict_rows_prefer_data_blocked_over_coverage_blocked():
    coverage_only = blocked_verdict_rows(
        [_run_blocker("LEGAL_COVERAGE", status="COVERAGE_BLOCKED")]
    )
    assert coverage_only.loc[
        coverage_only["gate"].eq("final_verdict"),
        "final_verdict",
    ].iloc[0] == "COVERAGE_BLOCKED"

    mixed = blocked_verdict_rows(
        [
            _run_blocker("LEGAL_COVERAGE", status="COVERAGE_BLOCKED"),
            _run_blocker("CARRY_FRESHNESS"),
        ]
    )
    assert mixed.loc[
        mixed["gate"].eq("final_verdict"),
        "final_verdict",
    ].iloc[0] == "DATA_BLOCKED"


def test_blocked_verdict_rows_never_claim_a_statistical_verdict():
    rows = blocked_verdict_rows([_run_blocker("CARRY_FRESHNESS")])

    assert rows["statistical_verdict"].isna().all()
    assert not rows["gate_pass"].any()
    assert not rows["shadow_candidate"].any()
    assert not rows["shadow_start_allowed"].any()


def test_blocked_verdict_rows_reject_an_unblocked_run():
    with pytest.raises(ValueError, match="blocked run requires"):
        blocked_verdict_rows([])


def _blocked_preflight(root, blockers):
    _write_preflight_manifest(
        root,
        status="DATA_BLOCKED",
        blockers=blockers,
        database_evidence=_database_evidence(),
    )
    return verify_preflight_manifest(root, _EXPECTED_CONFIG_HASH, None)


def _formation_blocker(formation_date, reason_code="DATA_CONTRACT"):
    blocker = _preflight_blocker(reason_code=reason_code)
    blocker["formation_date"] = formation_date
    return blocker


def _full_evidence(**overrides):
    evidence = {
        "stock_price_max_date": "2026-07-10",
        "index_500_max_date": "2026-07-10",
        "index_1000_max_date": "2026-07-09",
        "ic_carry_max_date": "2026-07-08",
        "im_carry_max_date": "2026-07-10",
        "salg_valid_through": "2026-10-31",
        "true_first_disclosure_coverage": {
            "verified_numerator": 0,
            "required_denominator": 240,
            "ratio": 0.0,
            "coverage_basis": TRUE_DISCLOSURE_COVERAGE_BASIS,
        },
        "stage_manifest_hashes": {"preflight": "b" * 64},
        "input_file_hashes": {"state_components.csv": "c" * 64},
    }
    evidence.update(overrides)
    return RunEvidence(**evidence)


def test_run_manifest_holds_exactly_the_frozen_field_set_and_is_json_safe(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])

    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
    )

    assert set(manifest) == set(RUN_MANIFEST_FIELDS)
    assert json.loads(json.dumps(manifest)) == manifest


def test_run_manifest_of_a_blocked_run_claims_no_statistical_verdict(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])

    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
    )

    assert manifest["family_statistical_verdict"] is None
    assert manifest["candidate_statistical_verdicts"] == {}
    assert manifest["final_verdict"] == "DATA_BLOCKED"
    for field_name in (
        "common_historical_end",
        "stock_price_max_date",
        "index_500_max_date",
        "index_1000_max_date",
        "ic_carry_max_date",
        "im_carry_max_date",
        "salg_valid_through",
        "true_first_disclosure_coverage",
    ):
        assert manifest[field_name] is None


def test_common_historical_end_is_the_earliest_source_maximum(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])
    # Evidence coexists with a block only on post-data runs, where the
    # family statistical verdict survives (it is never erased by the block).
    verdicts.loc[
        verdicts["gate"].eq("final_verdict"), "statistical_verdict"
    ] = "STOP"

    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
        evidence=_full_evidence(),
    )

    assert manifest["common_historical_end"] == "2026-07-08"


def test_common_historical_end_is_null_when_any_source_maximum_is_missing(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])
    verdicts.loc[
        verdicts["gate"].eq("final_verdict"), "statistical_verdict"
    ] = "STOP"

    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
        evidence=_full_evidence(im_carry_max_date=None),
    )

    assert manifest["common_historical_end"] is None
    assert manifest["im_carry_max_date"] is None
    assert manifest["ic_carry_max_date"] == "2026-07-08"


def test_run_manifest_records_the_formation_months_that_blocked_the_run(tmp_path):
    preflight = _blocked_preflight(
        tmp_path,
        [
            _formation_blocker("2023-08-31", reason_code="DATA_CONTRACT"),
            _formation_blocker("2023-07-31", reason_code="SNAPSHOT_SOURCE"),
            _formation_blocker("2023-08-31", reason_code="LEG_ELIGIBILITY"),
        ],
    )
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])

    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
    )

    assert manifest["invalid_formation_months"] == ["2023-07", "2023-08"]


def test_run_manifest_carries_the_verified_code_commit_and_config_hash(tmp_path):
    cfg = load_b3_config()
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])

    manifest = build_run_manifest(cfg, preflight, verdicts, "2026-07-10")

    assert manifest["config_hash"] == config_hash(cfg)
    assert manifest["code_commit"] == git_commit()
    assert manifest["requested_data_end"] == "2026-07-10"
    assert manifest["im_launch_date"] == "2022-07-22"
    assert manifest["database_source_evidence"] == _database_evidence()


def test_run_manifest_rejects_verdicts_without_a_single_final_row(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])
    without_final = verdicts.loc[verdicts["gate"].ne("final_verdict")]

    with pytest.raises(DataBlocked, match="exactly one final verdict row"):
        build_run_manifest(
            load_b3_config(),
            preflight,
            without_final,
            "2026-07-10",
        )


def test_write_run_manifest_is_atomic_and_leaves_no_temporary(tmp_path):
    preflight = _blocked_preflight(tmp_path / "research", [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])
    manifest = build_run_manifest(
        load_b3_config(),
        preflight,
        verdicts,
        "2026-07-10",
    )
    target = tmp_path / "compact" / "run_manifest.json"

    written = write_run_manifest(target, manifest)

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == manifest
    assert not list(target.parent.glob(".*.tmp"))


def test_write_run_manifest_refuses_a_payload_that_is_not_the_frozen_schema(tmp_path):
    target = tmp_path / "run_manifest.json"

    with pytest.raises(DataBlocked, match="run manifest schema mismatch"):
        write_run_manifest(target, {"final_verdict": "STOP"})

    assert not target.exists()


# ---------------------------------------------------------------------------
# Task 10 Step 6: run_evaluation orchestration and the eval CLI
# ---------------------------------------------------------------------------


def _forbidden_market_loader(*args, **kwargs):
    raise AssertionError("a blocked run must never load market data")


def _blocked_run_layout(
    tmp_path,
    blockers,
    *,
    status="DATA_BLOCKED",
    database_evidence=_NO_DATABASE_EVIDENCE,
    data_end="2026-07-10",
):
    research = tmp_path / "research"
    compact = tmp_path / "compact"
    _write_preflight_manifest(
        research,
        status=status,
        blockers=blockers,
        data_end=data_end,
        database_evidence=database_evidence,
        config_hash_value=config_hash(load_b3_config()),
    )
    return research, compact


def test_run_evaluation_blocked_preflight_writes_only_blocked_products(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [
            _formation_blocker(
                "2023-08-31", reason_code="TARGET_COORDINATE_CALIBRATION"
            )
        ],
        database_evidence=_database_evidence(),
    )

    result = b3_eval_module.run_evaluation(
        load_b3_config(),
        "2026-07-10",
        research,
        compact,
        underlying_return_loader=_forbidden_market_loader,
        carry_loader=_forbidden_market_loader,
    )

    assert result.blocked is True
    assert result.final_verdict == "DATA_BLOCKED"
    assert {p.name for p in compact.iterdir()} == {
        "verdicts.csv",
        "run_manifest.json",
    }
    manifest = json.loads(
        (compact / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["final_verdict"] == "DATA_BLOCKED"
    assert manifest["family_statistical_verdict"] is None
    assert manifest["candidate_statistical_verdicts"] == {}
    verdicts = pd.read_csv(compact / "verdicts.csv")
    final_rows = verdicts.loc[verdicts["gate"] == "final_verdict"]
    assert len(final_rows) == 1
    assert final_rows["final_verdict"].iloc[0] == "DATA_BLOCKED"
    assert (verdicts["reason_code"] == "TARGET_COORDINATE_CALIBRATION").any()


def test_run_evaluation_dedupes_repeated_preflight_reason_codes(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [
            _formation_blocker(
                "2023-07-31", reason_code="TARGET_COORDINATE_CALIBRATION"
            ),
            _formation_blocker(
                "2023-08-31", reason_code="TARGET_COORDINATE_CALIBRATION"
            ),
        ],
        database_evidence=_database_evidence(),
    )

    result = b3_eval_module.run_evaluation(
        load_b3_config(),
        "2026-07-10",
        research,
        compact,
        underlying_return_loader=_forbidden_market_loader,
        carry_loader=_forbidden_market_loader,
    )

    assert result.final_verdict == "DATA_BLOCKED"
    verdicts = pd.read_csv(compact / "verdicts.csv")
    blocker_rows = verdicts.loc[
        verdicts["reason_code"] == "TARGET_COORDINATE_CALIBRATION"
    ]
    assert len(blocker_rows) == 1


def test_run_evaluation_blocks_a_preflight_without_database_evidence(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        None,
        status="OK",
    )

    result = b3_eval_module.run_evaluation(
        load_b3_config(),
        "2026-07-10",
        research,
        compact,
        underlying_return_loader=_forbidden_market_loader,
        carry_loader=_forbidden_market_loader,
    )

    assert result.blocked is True
    assert result.final_verdict == "DATA_BLOCKED"
    verdicts = pd.read_csv(compact / "verdicts.csv")
    assert (
        verdicts["reason_code"] == "DATABASE_SOURCE_EVIDENCE_MISSING"
    ).any()


def test_run_evaluation_invalidates_stale_full_run_products(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [_preflight_blocker(status="DATA_BLOCKED")],
        database_evidence=_database_evidence(),
    )
    compact.mkdir(parents=True, exist_ok=True)
    stale = compact / "production_metrics.csv"
    stale.write_text("stale\n", encoding="utf-8")

    b3_eval_module.run_evaluation(
        load_b3_config(),
        "2026-07-10",
        research,
        compact,
        underlying_return_loader=_forbidden_market_loader,
        carry_loader=_forbidden_market_loader,
    )

    assert not stale.exists()


def test_eval_cli_exits_2_on_data_blocked_preflight(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [_preflight_blocker(status="DATA_BLOCKED")],
        database_evidence=_database_evidence(),
    )

    code = b3_eval_module.main(
        [
            "--data-end",
            "2026-07-10",
            "--research-output-dir",
            str(research),
            "--backtest-output-dir",
            str(compact),
        ]
    )

    assert code == 2


def test_eval_cli_exits_3_on_coverage_blocked_preflight(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [_preflight_blocker(status="COVERAGE_BLOCKED")],
        status="COVERAGE_BLOCKED",
        database_evidence=_database_evidence(),
    )

    code = b3_eval_module.main(
        [
            "--data-end",
            "2026-07-10",
            "--research-output-dir",
            str(research),
            "--backtest-output-dir",
            str(compact),
        ]
    )

    assert code == 3


def _write_eval_exposures(path, formations, policies):
    rows = []
    for policy in policies:
        for offset, formation in enumerate(formations):
            rows.append(
                {
                    "universe_role": "model",
                    "pit_policy": policy,
                    "formation_date": pd.Timestamp(formation),
                    "ticker": f"{600000 + offset:06d}.SH",
                    "true_first_disclosure_verified": True,
                    "salg_source_end_date": pd.Timestamp("2024-12-31"),
                }
            )
    frame = pd.DataFrame(rows)
    frame.to_csv(
        path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    return frame


def _write_eval_structure(compact, cfg, comparison, data_end):
    compact.mkdir(parents=True, exist_ok=True)
    coefficients = compact / "structure_coefficients.csv"
    model = compact / "model_comparison.csv"
    pd.DataFrame({"coefficient": [0.1]}).to_csv(coefficients, index=False)
    comparison.to_csv(model, index=False)
    payload = {
        "stage": "structure",
        "config_hash": config_hash(cfg),
        "data_end": data_end,
        "status": "OK",
        "outputs": {
            "structure_coefficients.csv": _sha256(coefficients),
            "model_comparison.csv": _sha256(model),
        },
    }
    (compact / "structure_manifest.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def _unblocked_run_layout(tmp_path, *, data_end="2024-12-31"):
    cfg, _, formations, states, targets, equal_weight, carry = _evaluation_inputs()
    comparison = _model_comparison(cfg)
    research = tmp_path / "research"
    compact = tmp_path / "compact"
    research.mkdir(parents=True, exist_ok=True)
    cutoff = pd.Timestamp(data_end)

    exposure_path = research / "monthly_exposures.csv.gz"
    _write_eval_exposures(exposure_path, formations, tuple(cfg["pit"]["policies"]))
    _write_stage_manifest(
        research, "exposures", cfg, cutoff, [exposure_path], "OK", []
    )

    state_path = research / "state_components.csv"
    states.to_csv(state_path, index=False)
    _write_stage_manifest(research, "states", cfg, cutoff, [state_path], "OK", [])

    _write_eval_structure(compact, cfg, comparison, data_end)

    _write_preflight_manifest(
        research,
        status="OK",
        data_end=data_end,
        database_evidence=_database_evidence(),
        config_hash_value=config_hash(cfg),
    )
    return cfg, research, compact, targets, carry, equal_weight, data_end


def test_run_evaluation_unblocked_writes_seven_products_and_concludes(tmp_path):
    cfg, research, compact, targets, carry, equal_weight, data_end = (
        _unblocked_run_layout(tmp_path)
    )

    result = b3_eval_module.run_evaluation(
        cfg,
        data_end,
        research,
        compact,
        underlying_return_loader=lambda leg: targets[leg],
        carry_loader=lambda leg: carry[leg],
        equal_weight_signal=equal_weight,
    )

    assert result.blocked is False
    assert result.final_verdict in {"STOP", "MEASURE_ONLY", "PASS_SHADOW"}
    present = {p.name for p in compact.iterdir()}
    assert {
        "verdicts.csv",
        "run_manifest.json",
        "production_metrics.csv",
        "yearly_contribution.csv",
        "bootstrap.csv",
        "structure_coefficients.csv",
        "model_comparison.csv",
    } <= present
    manifest = json.loads(
        (compact / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["final_verdict"] == result.final_verdict
    assert manifest["family_statistical_verdict"] is not None
    assert manifest["true_first_disclosure_coverage"]["ratio"] == 1.0
    bootstrap = pd.read_csv(compact / "bootstrap.csv")
    assert not bootstrap.empty


def test_run_evaluation_post_data_coverage_gap_blocks_but_keeps_statistics(tmp_path):
    cfg, research, compact, targets, carry, equal_weight, data_end = (
        _unblocked_run_layout(tmp_path)
    )
    # Break true-disclosure coverage after the fact: one model row unverified.
    exposure_path = research / "monthly_exposures.csv.gz"
    exposures = pd.read_csv(exposure_path)
    exposures.loc[0, "true_first_disclosure_verified"] = False
    exposures.to_csv(
        exposure_path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        research, "exposures", cfg, pd.Timestamp(data_end), [exposure_path], "OK", []
    )

    result = b3_eval_module.run_evaluation(
        cfg,
        data_end,
        research,
        compact,
        underlying_return_loader=lambda leg: targets[leg],
        carry_loader=lambda leg: carry[leg],
        equal_weight_signal=equal_weight,
    )

    assert result.blocked is True
    assert result.final_verdict == "DATA_BLOCKED"
    manifest = json.loads(
        (compact / "run_manifest.json").read_text(encoding="utf-8")
    )
    # The run-level block never erases the legal statistical evidence.
    assert manifest["family_statistical_verdict"] is not None
    verdicts = pd.read_csv(compact / "verdicts.csv")
    assert (verdicts["reason_code"] == "TRUE_DISCLOSURE_COVERAGE").any()


# ---------------------------------------------------------------------------
# Review fixes: bare runs adopt the verified preflight data_end (C1)


def test_eval_cli_bare_run_writes_blocked_evidence_without_data_end(tmp_path):
    research, compact = _blocked_run_layout(
        tmp_path,
        [
            _formation_blocker(
                "2023-08-31", reason_code="TARGET_COORDINATE_CALIBRATION"
            )
        ],
        database_evidence=_database_evidence(),
    )

    code = b3_eval_module.main(
        [
            "--research-output-dir",
            str(research),
            "--backtest-output-dir",
            str(compact),
        ]
    )

    assert code == 2
    assert (compact / "verdicts.csv").is_file()
    manifest_path = compact / "run_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["requested_data_end"] == "2026-07-10"
    assert manifest["final_verdict"] == "DATA_BLOCKED"
    assert manifest["family_statistical_verdict"] is None


def test_run_evaluation_none_data_end_adopts_preflight_boundary(tmp_path):
    cfg, research, compact, targets, carry, equal_weight, data_end = (
        _unblocked_run_layout(tmp_path)
    )

    result = b3_eval_module.run_evaluation(
        cfg,
        None,
        research,
        compact,
        underlying_return_loader=lambda leg: targets[leg],
        carry_loader=lambda leg: carry[leg],
        equal_weight_signal=equal_weight,
    )

    assert result.blocked is False
    manifest = json.loads(
        (compact / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["requested_data_end"] == data_end


# ---------------------------------------------------------------------------
# Review fixes: eval validators must speak the producer vocabulary (C2)


def test_true_disclosure_coverage_accepts_producer_size_only_rows():
    frame = _required_disclosure_rows()
    size_only = pd.DataFrame(
        [
            {
                "universe_role": "size_only",
                "pit_policy": _PIT_POLICIES[0],
                "formation_date": "2014-01-31",
                "ticker": "000002.SZ",
                "true_first_disclosure_verified": False,
            }
        ]
    )
    mixed = pd.concat([frame, size_only], ignore_index=True)

    got = compute_true_disclosure_coverage(mixed, _PIT_POLICIES)

    assert got["ratio"] == 1.0


def test_salg_valid_through_accepts_producer_size_only_rows():
    base = _salg_rows("2026-03-31", "2025-12-31")
    size_only = pd.DataFrame(
        [
            {
                "universe_role": "size_only",
                "formation_date": "2026-06-30",
                "salg_source_end_date": None,
            }
        ]
    )
    mixed = pd.concat([base, size_only], ignore_index=True)

    assert salg_valid_through(mixed) == salg_valid_through(base)


def test_flatten_exposures_output_passes_eval_disclosure_validators(tmp_path):
    from test_b3_exposures import _synthetic_snapshot

    from signals.style_basket.b3_build import flatten_exposures
    from signals.style_basket.b3_exposures import (
        ExposureResult,
        compute_month_exposures,
    )

    cfg = load_b3_config()
    # The production snapshot (b3_build monthly_style_snapshots) carries the
    # Task 3 provenance columns; the exposures stage passes them through.
    snapshot = _synthetic_snapshot().assign(
        salg_source_end_date=pd.Timestamp("2020-12-31"),
        true_first_disclosure_verified=True,
    )
    snapshot["ticker"] = [
        f"{600000 + offset:06d}.SH" for offset in range(len(snapshot))
    ]
    size_only_ticker = snapshot.iloc[-1]["ticker"]
    snapshot.loc[snapshot["ticker"].eq(size_only_ticker), "style_score"] = np.nan
    result = compute_month_exposures(snapshot, cfg)
    policies = tuple(cfg["pit"]["policies"])
    # The frozen config needs the full 2,200-name snapshot to compute, but
    # the schema contract survives row selection; a small slice keeps the
    # 120-month grid below the CSV round-trip pain threshold.
    kept = result.size.index[:8].union(pd.Index([size_only_ticker]))
    result = ExposureResult(
        size=result.size.loc[result.size.index.isin(kept)],
        model=result.model.loc[result.model.index.isin(kept)],
        q=result.q,
        diagnostics=result.diagnostics,
    )
    # Coverage demands the complete 2014-2023 formation grid; stamping the
    # same exposure result on every month keeps the producer path authentic
    # while staying cheap (flatten assigns formation_date per key).
    grid_calendar = pd.bdate_range("2014-01-01", "2023-12-31")
    formations = pd.DatetimeIndex(
        pd.Series(grid_calendar, index=grid_calendar)
        .groupby(grid_calendar.to_period("M"))
        .max()
    )
    flattened = flatten_exposures(
        {
            policy: {formation: result for formation in formations}
            for policy in policies
        }
    )

    path = tmp_path / "monthly_exposures.csv.gz"
    flattened.to_csv(
        path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    round_tripped = pd.read_csv(path)

    assert set(round_tripped["universe_role"]) == {"model", "size_only"}
    coverage = compute_true_disclosure_coverage(round_tripped, policies)
    assert 0.0 <= coverage["ratio"] <= 1.0
    salg_valid_through(round_tripped)


# ---------------------------------------------------------------------------
# Review fixes: loader series are truncated at the preflight boundary (C3)


def test_run_evaluation_truncates_loader_series_at_the_preflight_boundary(tmp_path):
    cfg, research, compact, targets, carry, equal_weight, data_end = (
        _unblocked_run_layout(tmp_path)
    )

    def beyond(series):
        extra_index = pd.bdate_range(
            series.index.max() + pd.Timedelta(days=1), periods=5
        )
        return pd.concat(
            [series, pd.Series(0.001, index=extra_index, name=series.name)]
        )

    result = b3_eval_module.run_evaluation(
        cfg,
        data_end,
        research,
        compact,
        underlying_return_loader=lambda leg: beyond(targets[leg]),
        carry_loader=lambda leg: beyond(carry[leg]),
        equal_weight_signal=beyond(equal_weight),
    )

    assert result.blocked is False
    manifest = json.loads(
        (compact / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["index_500_max_date"] <= data_end
    assert manifest["index_1000_max_date"] <= data_end
    # Raw carry freshness evidence must keep the untruncated series boundary.
    assert manifest["ic_carry_max_date"] > data_end
    assert manifest["im_carry_max_date"] > data_end


# ---------------------------------------------------------------------------
# Review fixes: the blocked-run evidence invariant is validator-enforced (I2)


def test_build_run_manifest_rejects_evidence_when_family_verdict_is_null(tmp_path):
    preflight = _blocked_preflight(tmp_path, [_preflight_blocker()])
    verdicts = blocked_verdict_rows([_run_blocker("DATA_CONTRACT")])

    with pytest.raises(RuntimeError, match="evidence"):
        build_run_manifest(
            load_b3_config(),
            preflight,
            verdicts,
            "2026-07-10",
            evidence=_full_evidence(),
        )


# ---------------------------------------------------------------------------
# Review fixes: CLI hardening (I3 + minors)


def test_eval_cli_explicit_mismatch_reports_pre_audit_rejection(tmp_path, capsys):
    research, compact = _blocked_run_layout(
        tmp_path,
        [
            _formation_blocker(
                "2023-08-31", reason_code="TARGET_COORDINATE_CALIBRATION"
            )
        ],
        database_evidence=_database_evidence(),
    )

    code = b3_eval_module.main(
        [
            "--data-end",
            "2026-12-31",
            "--research-output-dir",
            str(research),
            "--backtest-output-dir",
            str(compact),
        ]
    )

    assert code == 2
    assert not (compact / "run_manifest.json").exists()
    assert "no audit evidence" in capsys.readouterr().err


def test_invalidate_evaluation_outputs_clears_stale_temporaries(tmp_path):
    (tmp_path / "verdicts.csv").write_text("stale", encoding="utf-8")
    (tmp_path / ".run_manifest.json.tmp").write_text("orphan", encoding="utf-8")

    b3_eval_module._invalidate_evaluation_outputs(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_eval_cli_unblocked_run_exits_zero_with_default_loaders(
    tmp_path, monkeypatch
):
    cfg, research, compact, targets, carry, equal_weight, data_end = (
        _unblocked_run_layout(tmp_path)
    )
    control_path = tmp_path / "equal_weight_control.csv"
    pd.DataFrame(
        {
            "date": equal_weight.index.strftime("%Y-%m-%d"),
            "factor_value": equal_weight.to_numpy(),
        }
    ).to_csv(control_path, index=False)
    import backtest.data as data_module

    monkeypatch.setattr(
        data_module, "load_underlying_returns", lambda leg: targets[leg]
    )
    monkeypatch.setattr(data_module, "load_carry", lambda leg: carry[leg])
    monkeypatch.setattr(
        b3_eval_module, "DEFAULT_EQUAL_WEIGHT_SIGNAL_PATH", control_path
    )

    code = b3_eval_module.main(
        [
            "--research-output-dir",
            str(research),
            "--backtest-output-dir",
            str(compact),
        ]
    )

    assert code == 0
    assert (compact / "run_manifest.json").is_file()
