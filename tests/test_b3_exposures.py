from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_exposures import (
    CoverageBlocked,
    DataBlocked,
    NumericalFailure,
    _capped_weights,
    _industry_design,
    _residualize,
    compute_month_exposures,
)


def test_b3_config_freezes_candidates_windows_and_execution():
    cfg = load_b3_config()

    assert cfg["candidates"] == ["B3_unified", "B3_dual_target"]
    assert cfg["windows"] == {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    assert cfg["execution"]["cost_bps"] == 3.0
    assert cfg["execution"]["annualization"] == 245
    assert cfg["portfolio"]["weight_cap"] == 0.01
    assert cfg["portfolio"]["min_leg_size"] == 100
    assert cfg["bootstrap"] == {
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "adjusted_tail_max": 0.10,
    }


def test_b3_config_hash_is_order_independent_and_value_sensitive():
    cfg = load_b3_config()
    reordered = dict(reversed(list(cfg.items())))
    changed = deepcopy(cfg)
    changed["signal"]["z_window"] = 41

    assert config_hash(reordered) == config_hash(cfg)
    assert config_hash(changed) != config_hash(cfg)


def test_b3_config_rejects_candidate_expansion(tmp_path):
    cfg = load_b3_config()
    cfg["candidates"].append("B3_after_the_fact")
    path = Path(tmp_path) / "b3_config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly"):
        load_b3_config(path)


def _synthetic_snapshot(n=2200):
    rng = np.random.default_rng(20260713)
    ticker = [f"S{i:04d}" for i in range(n)]
    log_mv = np.linspace(16.0, 8.0, n) + rng.normal(0.0, 0.03, n)
    industry = np.where(np.arange(n) % 2 == 0, "电子", "医药")
    style = (
        0.4 * (industry == "电子")
        + 0.25 * (log_mv - log_mv.mean())
        + rng.normal(0.0, 1.0, n)
    )
    return pd.DataFrame(
        {
            "ticker": ticker,
            "formation_date": pd.Timestamp("2021-01-29"),
            "total_market_value": np.exp(log_mv),
            "industry": industry,
            "style_score": style,
        }
    )


def test_month_exposures_are_orthogonal_and_row_order_invariant():
    snapshot = _synthetic_snapshot()
    cfg = load_b3_config()

    got = compute_month_exposures(snapshot, cfg)
    shuffled = compute_month_exposures(
        snapshot.sample(frac=1.0, random_state=7), cfg
    )

    assert got.diagnostics["max_orthogonality_error"] <= 1e-8
    assert abs(got.model["s_perp"].mean()) <= 1e-12
    assert got.model["s_perp"].std() == pytest.approx(1.0, abs=1e-12)
    assert got.model["h_perp"].std() == pytest.approx(1.0, abs=1e-12)
    pd.testing.assert_frame_equal(
        got.model.sort_index(), shuffled.model.sort_index(), check_like=True
    )


def test_target_coordinates_use_rank_bands_but_weights_use_full_model_universe():
    got = compute_month_exposures(_synthetic_snapshot(), load_b3_config())

    assert got.q["q1000"] > got.q["q500"]
    tail = got.model.iloc[1900:]
    assert (
        (tail["w_q1000_plus"] > 0.0) | (tail["w_q1000_minus"] > 0.0)
    ).any()


def test_every_leg_is_normalized_capped_and_has_at_least_100_names():
    got = compute_month_exposures(_synthetic_snapshot(), load_b3_config())

    for axis in ["style", "size", "interaction", "qblend", "q500", "q1000"]:
        frame = got.size if axis == "size" else got.model
        for side in ["plus", "minus"]:
            weights = frame[f"w_{axis}_{side}"]
            assert weights.sum() == pytest.approx(1.0, abs=1e-10)
            assert weights.max() <= 0.01 + 1e-12
            assert (weights > 0.0).sum() >= 100


def test_thin_legal_cross_section_raises_coverage_blocked():
    with pytest.raises(CoverageBlocked, match="100"):
        compute_month_exposures(_synthetic_snapshot(n=180), load_b3_config())


def test_missing_source_field_is_data_blocked_not_coverage_blocked():
    snapshot = _synthetic_snapshot()
    snapshot["size_eligible"] = True
    snapshot["model_eligible"] = True
    snapshot["size_exclusion_reason"] = ""
    snapshot["model_exclusion_reason"] = ""
    snapshot.loc[0, ["size_eligible", "model_eligible"]] = False
    snapshot.loc[
        0, ["size_exclusion_reason", "model_exclusion_reason"]
    ] = "DATA_MISSING_CLOSE"

    with pytest.raises(DataBlocked, match="DATA_MISSING_CLOSE"):
        compute_month_exposures(snapshot, load_b3_config())


def test_explained_legal_exclusions_can_end_as_coverage_blocked():
    snapshot = _synthetic_snapshot()
    snapshot["size_eligible"] = False
    snapshot["model_eligible"] = False
    snapshot["size_exclusion_reason"] = "LISTED_LT_180D"
    snapshot["model_exclusion_reason"] = "LISTED_LT_180D"
    snapshot.loc[:179, ["size_eligible", "model_eligible"]] = True
    snapshot.loc[
        :179, ["size_exclusion_reason", "model_exclusion_reason"]
    ] = ""

    with pytest.raises(CoverageBlocked, match="100"):
        compute_month_exposures(snapshot, load_b3_config())


def _explicit_snapshot():
    snapshot = _synthetic_snapshot()
    snapshot["size_eligible"] = True
    snapshot["model_eligible"] = True
    snapshot["size_exclusion_reason"] = ""
    snapshot["model_exclusion_reason"] = ""
    return snapshot


def test_single_industry_snapshot_has_standardized_exposures_and_valid_legs():
    snapshot = _synthetic_snapshot()
    snapshot["industry"] = "电子"

    got = compute_month_exposures(snapshot, load_b3_config())

    for exposure in [
        got.size["m_perp"],
        got.model["s_perp"],
        got.model["h_perp"],
    ]:
        assert np.isfinite(exposure).all()
        assert exposure.std() == pytest.approx(1.0, abs=1e-12)
    for axis in ["style", "size", "interaction", "qblend", "q500", "q1000"]:
        frame = got.size if axis == "size" else got.model
        for side in ["plus", "minus"]:
            weights = frame[f"w_{axis}_{side}"]
            assert np.isfinite(weights).all()
            assert weights.sum() == pytest.approx(1.0, abs=1e-10)


def test_industry_design_namespaces_labels_and_drops_sorted_reference():
    industry = pd.Series(
        ["m_perp", "intercept", "m", "s_perp", None],
        index=["A", "B", "C", "D", "E"],
    )

    design = _industry_design(industry)

    assert list(design.columns) == [
        "intercept",
        "industry=intercept",
        "industry=m",
        "industry=m_perp",
        "industry=s_perp",
    ]


@pytest.mark.parametrize(
    "bad_style",
    [
        pytest.param(None, id="none"),
        pytest.param("not-a-number", id="text"),
        pytest.param(np.inf, id="positive-infinity"),
        pytest.param(-np.inf, id="negative-infinity"),
    ],
)
def test_explicit_model_eligible_invalid_style_is_data_blocked(bad_style):
    snapshot = _explicit_snapshot()
    snapshot["style_score"] = snapshot["style_score"].astype(object)
    snapshot.loc[0, "style_score"] = bad_style

    with pytest.raises(DataBlocked, match=r"style_score.*S0000"):
        compute_month_exposures(snapshot, load_b3_config())


def test_legacy_nonnumeric_style_is_data_blocked():
    snapshot = _synthetic_snapshot()
    snapshot["style_score"] = snapshot["style_score"].astype(object)
    snapshot.loc[0, "style_score"] = "not-a-number"

    with pytest.raises(DataBlocked, match=r"style_score.*S0000"):
        compute_month_exposures(snapshot, load_b3_config())


def test_residualize_rejects_rows_with_missing_inputs():
    index = pd.Index(["A", "B", "C"])
    y = pd.Series([1.0, np.nan, 3.0], index=index)
    controls = pd.DataFrame({"intercept": 1.0}, index=index)

    with pytest.raises(NumericalFailure, match="missing"):
        _residualize(y, controls, "test_perp")


def test_capped_weights_rejects_nonfinite_exposure_before_sign_filtering():
    exposure = pd.Series([1.0, 2.0, np.nan], index=["A", "B", "C"])

    with pytest.raises(NumericalFailure, match="non-finite"):
        _capped_weights(exposure, positive=True, cap=1.0, min_members=1)


@pytest.mark.parametrize(
    ("flag_column", "bad_value", "reason_column", "reason"),
    [
        pytest.param(
            "size_eligible",
            "False",
            "size_exclusion_reason",
            "LISTED_LT_180D",
            id="string-size-flag",
        ),
        pytest.param(
            "size_eligible",
            None,
            "size_exclusion_reason",
            "LISTED_LT_180D",
            id="null-size-flag",
        ),
        pytest.param(
            "model_eligible",
            "False",
            "model_exclusion_reason",
            "MISSING_STYLE_SCORE",
            id="string-model-flag",
        ),
        pytest.param(
            "model_eligible",
            None,
            "model_exclusion_reason",
            "MISSING_STYLE_SCORE",
            id="null-model-flag",
        ),
    ],
)
def test_explicit_eligibility_flags_require_actual_nonnull_booleans(
    flag_column, bad_value, reason_column, reason
):
    snapshot = _explicit_snapshot()
    snapshot[flag_column] = snapshot[flag_column].astype(object)
    snapshot.loc[0, flag_column] = bad_value
    snapshot.loc[0, reason_column] = reason
    if flag_column == "size_eligible":
        snapshot.loc[0, "model_eligible"] = False
        snapshot.loc[0, "model_exclusion_reason"] = "LISTED_LT_180D"

    with pytest.raises(DataBlocked, match="bool"):
        compute_month_exposures(snapshot, load_b3_config())


@pytest.mark.parametrize(
    ("reason_column", "reason"),
    [
        pytest.param(
            "size_exclusion_reason",
            "LISTED_LT_180D",
            id="size-reason-on-eligible-row",
        ),
        pytest.param(
            "model_exclusion_reason",
            "MISSING_STYLE_SCORE",
            id="model-reason-on-eligible-row",
        ),
    ],
)
def test_explicit_eligible_rows_require_blank_reasons(reason_column, reason):
    snapshot = _explicit_snapshot()
    snapshot.loc[0, reason_column] = reason

    with pytest.raises(DataBlocked, match=r"eligible.*blank"):
        compute_month_exposures(snapshot, load_b3_config())


def test_numpy_boolean_eligibility_flags_are_accepted():
    snapshot = _explicit_snapshot()
    snapshot["size_eligible"] = snapshot["size_eligible"].map(np.bool_)
    snapshot["model_eligible"] = snapshot["model_eligible"].map(np.bool_)

    got = compute_month_exposures(snapshot, load_b3_config())

    assert got.diagnostics["size_n"] == len(snapshot)
    assert got.diagnostics["model_n"] == len(snapshot)
