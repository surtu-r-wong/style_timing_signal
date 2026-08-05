from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

from signals.style_basket import b3_build as b3_build_module
from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_build import (
    B3Sources,
    EXACT_CARRY_METHOD,
    POLICY_LAG,
    POLICY_MAIN,
    _formation_inputs,
    default_sources,
    _fetch_raw_financial,
    _industry_snapshot,
    _write_stage_manifest,
    apply_pit_policy,
    build_policy_snapshots,
    calibrate_target_coordinates,
    flatten_exposures,
    main,
    require_parent_manifest,
    run_preflight,
    run_exposures_stage,
)
from signals.style_basket.b3_exposures import (
    DEFAULT_DATA_MATERIALITY_THRESHOLD,
    CoverageBlocked,
    DataBlocked,
    ExposureResult,
    NumericalFailure,
    _capped_weights,
    _industry_design,
    _residualize,
    compute_month_exposures,
    resolve_data_materiality_threshold,
)
from signals.style_basket.b3_suspension import (
    CORE_EVIDENCE_COLUMNS,
    INTERVAL_METHOD,
    SUSPENSION_INTERVAL_ARTIFACT_COLUMNS,
    SuspensionEvidenceError,
    build_continuous_suspension_evidence,
    empty_interval_evidence,
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
            "close": np.linspace(10.0, 20.0, n),
            "total_market_value": np.exp(log_mv),
            "industry": industry,
            "style_score": style,
            "close_method": "",
            "close_carried": False,
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


def _snapshot_with_data_holes(count: int) -> pd.DataFrame:
    snapshot = _synthetic_snapshot()
    snapshot["size_eligible"] = True
    snapshot["model_eligible"] = True
    snapshot["size_exclusion_reason"] = ""
    snapshot["model_exclusion_reason"] = ""
    holes = list(range(count))
    snapshot.loc[holes, ["size_eligible", "model_eligible"]] = False
    snapshot.loc[
        holes, ["size_exclusion_reason", "model_exclusion_reason"]
    ] = "DATA_MISSING_CLOSE"
    return snapshot


def test_material_missing_source_field_is_data_blocked_not_coverage_blocked():
    # 20 / 2200 = 0.91%, comfortably over the 0.25% materiality threshold.
    with pytest.raises(DataBlocked, match="DATA_MISSING_CLOSE"):
        compute_month_exposures(_snapshot_with_data_holes(20), load_b3_config())


def test_immaterial_data_hole_is_measured_with_a_recorded_exemption():
    # 4 / 2200 = 0.18%, under the threshold: the month is measured anyway.
    result = compute_month_exposures(
        _snapshot_with_data_holes(4), load_b3_config()
    )

    assert result.exemption is not None
    assert result.exemption["excluded_names"] == 4
    assert result.exemption["measurable_names"] == 2200
    assert result.exemption["reason_codes"] == ["DATA_MISSING_CLOSE"]
    assert result.exemption["share"] == pytest.approx(4 / 2200)
    assert result.exemption["threshold"] == pytest.approx(0.0025)
    assert result.diagnostics["data_exempt_names"] == 4
    assert result.diagnostics["data_exempt_share"] == pytest.approx(4 / 2200)


def test_the_materiality_threshold_boundary_is_inclusive():
    cfg = dict(load_b3_config())
    cfg["data_materiality_threshold"] = 4 / 2200

    assert compute_month_exposures(
        _snapshot_with_data_holes(4), cfg
    ).exemption is not None

    cfg["data_materiality_threshold"] = 4 / 2200 - 1e-9
    with pytest.raises(DataBlocked, match="materiality threshold"):
        compute_month_exposures(_snapshot_with_data_holes(4), cfg)


def test_a_zero_threshold_restores_the_all_or_nothing_gate():
    cfg = dict(load_b3_config())
    cfg["data_materiality_threshold"] = 0.0

    with pytest.raises(DataBlocked, match="DATA_MISSING_CLOSE"):
        compute_month_exposures(_snapshot_with_data_holes(1), cfg)


def test_the_shipped_materiality_threshold_is_a_quarter_percent():
    """A silent widening of this default has to break a test."""

    assert DEFAULT_DATA_MATERIALITY_THRESHOLD == 0.0025
    assert resolve_data_materiality_threshold(load_b3_config()) == 0.0025


@pytest.mark.parametrize("value", ["0.01", True, float("nan"), -0.1, 1.0])
def test_an_invalid_materiality_threshold_fails_closed(value):
    cfg = dict(load_b3_config())
    cfg["data_materiality_threshold"] = value

    with pytest.raises(DataBlocked, match="data_materiality_threshold"):
        resolve_data_materiality_threshold(cfg)


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


def test_csmar_pit_policies_use_legal_deadlines_and_flag_approximation():
    raw = pd.DataFrame(
        {
            "ts_code": ["X", "X"],
            "end_date": ["2020-03-31", "2020-06-30"],
            "stored_ann_date": ["2023-07-29", "2023-07-29"],
            "statement_type": ["income", "income"],
            "data": [{"revenue": 1.0}, {"revenue": 2.0}],
            "data_source": ["csmar", "csmar"],
        }
    )

    main = apply_pit_policy(raw, POLICY_MAIN)
    lag = apply_pit_policy(raw, POLICY_LAG)

    assert list(main["ann_date"]) == [
        pd.Timestamp("2020-04-30"),
        pd.Timestamp("2020-08-31"),
    ]
    assert list(lag["ann_date"]) == [
        pd.Timestamp("2020-05-31"),
        pd.Timestamp("2020-09-30"),
    ]
    assert main["known_date_source"].eq(POLICY_MAIN).all()
    assert lag["known_date_source"].eq(POLICY_LAG).all()
    assert not main["true_first_disclosure_verified"].any()
    assert not lag["true_first_disclosure_verified"].any()


def test_wind_pit_date_is_preserved_and_verified_under_both_policies():
    raw = pd.DataFrame(
        {
            "ts_code": ["X"],
            "end_date": ["2025-06-30"],
            "stored_ann_date": ["2025-08-20"],
            "statement_type": ["income"],
            "data": [{"revenue": 1.0}],
            "data_source": ["wind"],
        }
    )

    for policy in (POLICY_MAIN, POLICY_LAG):
        got = apply_pit_policy(raw, policy)

        assert got.loc[0, "ann_date"] == pd.Timestamp("2025-08-20")
        assert got.loc[0, "known_date_source"] == "wind_first_disclosure"
        assert bool(got.loc[0, "true_first_disclosure_verified"])


def test_industry_snapshot_extends_earliest_label_and_applies_later_update():
    pool = pd.DataFrame(
        {
            "ticker": ["B", "A", "A", "B"],
            "effective_date": [
                "2021-02-01",
                "2021-01-01",
                "2022-01-01",
                "2022-03-01",
            ],
            "industry": ["医药", "电子", "通信", "食品饮料"],
        }
    )

    early = _industry_snapshot(pool, pd.Timestamp("2020-06-30"))
    later = _industry_snapshot(pool, pd.Timestamp("2022-02-28"))

    assert list(early.index) == ["A", "B"]
    assert early.to_dict() == {"A": "电子", "B": "医药"}
    assert list(later.index) == ["A", "B"]
    assert later.to_dict() == {"A": "通信", "B": "医药"}


def test_build_policy_snapshots_assembles_eligibility_and_provenance(
    monkeypatch,
):
    formation = pd.Timestamp("2021-06-30")
    tickers = ["A", "B", "C", "D"]
    raw_facts = pd.DataFrame(
        {
            "ts_code": tickers,
            "end_date": ["2020-12-31"] * 4,
            "stored_ann_date": ["2023-07-29"] * 4,
            "statement_type": ["income"] * 4,
            "data": [{"revenue": float(i)} for i in range(1, 5)],
            "data_source": ["csmar"] * 4,
        }
    )
    closes = pd.DataFrame(
        [[40.0, 20.0, 10.0, 30.0]],
        index=[formation],
        columns=["D", "B", "A", "C"],
    )
    shares_pool = pd.DataFrame(
        {
            "ts_code": ["C", "A", "D", "B"],
            "end_date": ["2020-01-01"] * 4,
            "known_date": ["2020-01-01"] * 4,
            "total_shares": [300.0, 100.0, 400.0, 200.0],
        }
    )
    industry_pool = pd.DataFrame(
        {
            "ticker": ["D", "B", "A"],
            "effective_date": ["2021-01-01"] * 3,
            "industry": ["银行", "医药", "电子"],
        }
    )
    stock_meta = pd.DataFrame(
        {
            "ticker": ["C", "A", "D", "B"],
            "list_date": [
                "2021-03-01",
                "2010-01-01",
                "2012-01-01",
                None,
            ],
            "delist_date": [None, None, None, None],
        }
    )

    def fake_ticker_financial_rows(facts):
        ticker = facts["ts_code"].iloc[0]
        known_date = facts["ann_date"].max()
        common = {
            "ts_code": [ticker, ticker],
            "end_date": [pd.Timestamp("2020-12-31")] * 2,
            "known_date": [known_date] * 2,
        }
        return {
            "ttm": pd.DataFrame(
                {
                    **common,
                    "field": ["np", "cfo"],
                    "ttm": [100.0, 80.0],
                }
            ),
            "slope": pd.DataFrame(
                {
                    **common,
                    "field": ["rev", "np"],
                    "slope": [0.2, 0.1],
                }
            ),
            "event": pd.DataFrame(
                {
                    **common,
                    "field": ["equity", "dps"],
                    "value": [500.0, 0.5],
                }
            ),
        }

    def fake_style_scores(factors):
        assert list(factors.index) == ["A", "D"]
        assert factors.loc["A"].to_dict() == pytest.approx(
            {
                "sal_g": 0.2,
                "pro_g": 0.1,
                "ep": 0.1,
                "bp": 0.5,
                "cfp": 0.08,
                "dp": 0.05,
            }
        )
        assert factors.loc[
            "D", ["sal_g", "pro_g", "ep", "bp", "dp"]
        ].to_dict() == pytest.approx(
            {
                "sal_g": 0.2,
                "pro_g": 0.1,
                "ep": 0.00625,
                "bp": 0.03125,
                "dp": 0.0125,
            }
        )
        assert pd.isna(factors.loc["D", "cfp"])
        out = factors.copy()
        out["style_score"] = [0.75, np.nan]
        return out

    monkeypatch.setattr(
        "signals.style_basket.build.ticker_financial_rows",
        fake_ticker_financial_rows,
    )
    monkeypatch.setattr(
        "signals.style_basket.scoring.style_scores",
        fake_style_scores,
    )

    snapshots = build_policy_snapshots(
        raw_facts,
        [formation],
        closes,
        shares_pool,
        industry_pool,
        stock_meta,
        POLICY_MAIN,
    )

    assert list(snapshots) == [formation]
    got = snapshots[formation]
    assert list(got["ticker"]) == tickers
    assert got["ticker"].is_unique
    assert got["formation_date"].eq(formation).all()
    assert {
        "size_eligible",
        "model_eligible",
        "size_exclusion_reason",
        "model_exclusion_reason",
    }.issubset(got.columns)
    assert got.set_index("ticker")["total_market_value"].to_dict() == {
        "A": 1000.0,
        "B": 4000.0,
        "C": 9000.0,
        "D": 16000.0,
    }
    assert got.set_index("ticker")["industry"].to_dict() == {
        "A": "电子",
        "B": "医药",
        "C": "UNKNOWN",
        "D": "银行",
    }
    assert got.set_index("ticker")["size_eligible"].to_dict() == {
        "A": True,
        "B": False,
        "C": False,
        "D": True,
    }
    assert got.set_index("ticker")["model_eligible"].to_dict() == {
        "A": True,
        "B": False,
        "C": False,
        "D": False,
    }
    assert got.set_index("ticker")["size_exclusion_reason"].to_dict() == {
        "A": "",
        "B": "DATA_MISSING_LIST_DATE",
        "C": "LISTED_LT_180D",
        "D": "",
    }
    assert got.set_index("ticker")["model_exclusion_reason"].to_dict() == {
        "A": "",
        "B": "DATA_MISSING_LIST_DATE",
        "C": "LISTED_LT_180D",
        "D": "MISSING_STYLE_SCORE",
    }
    assert got.set_index("ticker").loc["A", "style_score"] == 0.75
    assert pd.isna(got.set_index("ticker").loc["D", "style_score"])
    assert got.set_index("ticker").loc[
        "A", "salg_source_end_date"
    ] == pd.Timestamp("2020-12-31")
    assert not got["true_first_disclosure_verified"].any()


def _single_pit_fact(**overrides):
    row = {
        "ts_code": "X",
        "end_date": "2020-03-31",
        "stored_ann_date": "2020-04-15",
        "statement_type": "income",
        "data": {"revenue": 1.0},
        "data_source": "csmar",
    }
    row.update(overrides)
    return pd.DataFrame([row])


@pytest.mark.parametrize(
    "missing_column",
    [
        "ts_code",
        "end_date",
        "stored_ann_date",
        "statement_type",
        "data",
        "data_source",
    ],
)
def test_pit_policy_requires_complete_raw_schema(missing_column):
    raw = _single_pit_fact().drop(columns=missing_column)

    with pytest.raises(DataBlocked):
        apply_pit_policy(raw, POLICY_MAIN)


@pytest.mark.parametrize(
    "bad_end_date",
    [
        pytest.param(None, id="missing"),
        pytest.param("not-a-date", id="unparsable"),
    ],
)
def test_pit_policy_rejects_missing_or_invalid_end_date(bad_end_date):
    raw = _single_pit_fact(end_date=bad_end_date)

    with pytest.raises(DataBlocked):
        apply_pit_policy(raw, POLICY_MAIN)


def test_pit_policy_rejects_unparsable_stored_announcement_date():
    raw = _single_pit_fact(stored_ann_date="not-a-date")

    with pytest.raises(DataBlocked):
        apply_pit_policy(raw, POLICY_MAIN)


@pytest.mark.parametrize("policy", [POLICY_MAIN, POLICY_LAG])
def test_wind_requires_a_stored_announcement_date(policy):
    raw = _single_pit_fact(
        end_date="2025-06-30",
        stored_ann_date=None,
        data_source="wind",
    )

    with pytest.raises(DataBlocked):
        apply_pit_policy(raw, policy)


@pytest.mark.parametrize("policy", [POLICY_MAIN, POLICY_LAG])
@pytest.mark.parametrize(
    ("source", "end_date", "stored_ann_date"),
    [
        pytest.param(
            "csmar",
            "2020-03-31",
            "2020-03-30",
            id="csmar",
        ),
        pytest.param(
            "wind",
            "2025-06-30",
            "2025-06-29",
            id="wind",
        ),
    ],
)
def test_pit_policy_rejects_announcement_before_period_end(
    policy,
    source,
    end_date,
    stored_ann_date,
):
    raw = _single_pit_fact(
        end_date=end_date,
        stored_ann_date=stored_ann_date,
        data_source=source,
    )

    with pytest.raises(DataBlocked):
        apply_pit_policy(raw, policy)


def test_csmar_missing_stored_announcement_falls_back_to_legal_deadline():
    got = apply_pit_policy(
        _single_pit_fact(stored_ann_date=None),
        POLICY_MAIN,
    )

    assert got.loc[0, "ann_date"] == pd.Timestamp("2020-04-30")
    assert got.loc[0, "known_date_source"] == POLICY_MAIN
    assert not bool(got.loc[0, "true_first_disclosure_verified"])


def test_pit_policy_rejects_unknown_policy_with_value_error():
    with pytest.raises(ValueError, match="unsupported PIT policy"):
        apply_pit_policy(_single_pit_fact(), "unknown-policy")


def _industry_history():
    return pd.DataFrame(
        {
            "ticker": ["A", "A", "B"],
            "effective_date": [
                "2021-01-01",
                "2022-01-01",
                "2021-02-01",
            ],
            "industry": ["电子", "通信", "医药"],
        }
    )


@pytest.mark.parametrize(
    "bad_ticker",
    [
        pytest.param(None, id="null"),
        pytest.param(7, id="non-string"),
        pytest.param("   ", id="blank"),
    ],
)
def test_industry_snapshot_rejects_invalid_ticker_keys(bad_ticker):
    pool = _industry_history()
    pool["ticker"] = pool["ticker"].astype(object)
    pool.loc[0, "ticker"] = bad_ticker

    with pytest.raises(DataBlocked):
        _industry_snapshot(pool, pd.Timestamp("2022-06-30"))


@pytest.mark.parametrize(
    "bad_effective_date",
    [
        pytest.param(None, id="null"),
        pytest.param("not-a-date", id="unparsable"),
    ],
)
def test_industry_snapshot_rejects_invalid_effective_dates(
    bad_effective_date,
):
    pool = _industry_history()
    pool.loc[0, "effective_date"] = bad_effective_date

    with pytest.raises(DataBlocked):
        _industry_snapshot(pool, pd.Timestamp("2022-06-30"))


def test_industry_snapshot_ignores_exact_duplicate_rows():
    pool = _industry_history()
    pool = pd.concat([pool, pool.iloc[[0]]], ignore_index=True)

    got = _industry_snapshot(pool, pd.Timestamp("2022-06-30"))

    assert got.to_dict() == {"A": "通信", "B": "医药"}


def test_industry_snapshot_blocks_conflicting_same_date_labels():
    pool = pd.DataFrame(
        {
            "ticker": ["A", "A"],
            "effective_date": ["2021-01-01", "2021-01-01"],
            "industry": ["电子", "通信"],
        }
    )

    with pytest.raises(DataBlocked):
        _industry_snapshot(pool, pd.Timestamp("2022-06-30"))


def _minimal_assembly_inputs():
    formation = pd.Timestamp("2021-06-30")
    return {
        "raw_facts": pd.DataFrame(
            {
                "ts_code": ["A", "B"],
                "end_date": ["2020-12-31", "2020-12-31"],
                "stored_ann_date": ["2021-04-30", "2021-04-30"],
                "statement_type": ["income", "income"],
                "data": [{"revenue": 1.0}, {"revenue": 2.0}],
                "data_source": ["csmar", "csmar"],
            }
        ),
        "month_ends": [formation],
        "closes": pd.DataFrame(
            [[10.0, 20.0]],
            index=[formation],
            columns=["A", "B"],
        ),
        "shares_pool": pd.DataFrame(
            {
                "ts_code": ["A", "B"],
                "end_date": ["2020-01-01", "2020-01-01"],
                "known_date": ["2020-01-01", "2020-01-01"],
                "total_shares": [100.0, 200.0],
            }
        ),
        "industry_pool": pd.DataFrame(
            {
                "ticker": ["A", "B"],
                "effective_date": ["2021-01-01", "2021-01-01"],
                "industry": ["电子", "医药"],
            }
        ),
        "stock_meta": pd.DataFrame(
            {
                "ticker": ["A", "B"],
                "list_date": ["2010-01-01", "2011-01-01"],
                "delist_date": [None, None],
            }
        ),
        "policy": POLICY_MAIN,
    }


def _minimal_derived_rows(facts):
    ticker = facts["ts_code"].iloc[0]
    known_date = facts["ann_date"].max()
    common = {
        "ts_code": [ticker, ticker],
        "end_date": [pd.Timestamp("2020-12-31")] * 2,
        "known_date": [known_date] * 2,
    }
    return {
        "ttm": pd.DataFrame(
            {
                **common,
                "field": ["np", "cfo"],
                "ttm": [100.0, 80.0],
            }
        ),
        "slope": pd.DataFrame(
            {
                **common,
                "field": ["rev", "np"],
                "slope": [0.2, 0.1],
            }
        ),
        "event": pd.DataFrame(
            {
                **common,
                "field": ["equity", "dps"],
                "value": [500.0, 0.5],
            }
        ),
    }


def _patch_minimal_assembly_dependencies(
    monkeypatch,
    rows_builder=_minimal_derived_rows,
):
    def fake_style_scores(factors):
        out = factors.copy()
        out["style_score"] = 0.0
        return out

    monkeypatch.setattr(
        "signals.style_basket.build.ticker_financial_rows",
        rows_builder,
    )
    monkeypatch.setattr(
        "signals.style_basket.scoring.style_scores",
        fake_style_scores,
    )


@pytest.mark.parametrize(
    ("location", "bad_key"),
    [
        pytest.param(location, bad_key, id=f"{location}-{case_id}")
        for location in [
            "raw_facts",
            "shares_pool",
            "industry_pool",
            "stock_meta",
            "close_columns",
        ]
        for bad_key, case_id in [
            (None, "null"),
            (7, "mixed-non-string"),
            ("   ", "blank"),
        ]
    ],
)
def test_snapshot_assembly_rejects_invalid_ticker_keys(
    monkeypatch,
    location,
    bad_key,
):
    inputs = _minimal_assembly_inputs()
    if location == "raw_facts":
        inputs["raw_facts"]["ts_code"] = inputs["raw_facts"][
            "ts_code"
        ].astype(object)
        inputs["raw_facts"].loc[1, "ts_code"] = bad_key
    elif location == "shares_pool":
        inputs["shares_pool"]["ts_code"] = inputs["shares_pool"][
            "ts_code"
        ].astype(object)
        inputs["shares_pool"].loc[1, "ts_code"] = bad_key
    elif location == "industry_pool":
        inputs["industry_pool"]["ticker"] = inputs["industry_pool"][
            "ticker"
        ].astype(object)
        inputs["industry_pool"].loc[1, "ticker"] = bad_key
    elif location == "stock_meta":
        inputs["stock_meta"]["ticker"] = inputs["stock_meta"][
            "ticker"
        ].astype(object)
        inputs["stock_meta"].loc[1, "ticker"] = bad_key
    else:
        inputs["closes"].columns = ["A", bad_key]
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


def test_snapshot_assembly_rejects_invalid_derived_ticker_keys(monkeypatch):
    inputs = _minimal_assembly_inputs()

    def invalid_rows(facts):
        rows = _minimal_derived_rows(facts)
        if facts["ts_code"].iloc[0] == "B":
            for pool in rows.values():
                pool["ts_code"] = 7
        return rows

    _patch_minimal_assembly_dependencies(monkeypatch, invalid_rows)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


def test_snapshot_assembly_deduplicates_exact_normalized_metadata(
    monkeypatch,
):
    inputs = _minimal_assembly_inputs()
    duplicate = inputs["stock_meta"].iloc[[0]].copy()
    duplicate["list_date"] = pd.Timestamp("2010-01-01")
    inputs["stock_meta"] = pd.concat(
        [inputs["stock_meta"], duplicate],
        ignore_index=True,
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    got = build_policy_snapshots(**inputs)

    assert list(got[pd.Timestamp("2021-06-30")]["ticker"]) == ["A", "B"]


def test_snapshot_assembly_blocks_conflicting_duplicate_metadata(monkeypatch):
    inputs = _minimal_assembly_inputs()
    conflict = inputs["stock_meta"].iloc[[0]].copy()
    conflict["list_date"] = "2012-01-01"
    inputs["stock_meta"] = pd.concat(
        [inputs["stock_meta"], conflict],
        ignore_index=True,
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize("date_column", ["list_date", "delist_date"])
def test_snapshot_assembly_blocks_invalid_metadata_dates(
    monkeypatch,
    date_column,
):
    inputs = _minimal_assembly_inputs()
    inputs["stock_meta"].loc[0, date_column] = "not-a-date"
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


def test_snapshot_assembly_blocks_duplicate_close_columns(monkeypatch):
    inputs = _minimal_assembly_inputs()
    formation = inputs["month_ends"][0]
    inputs["closes"] = pd.DataFrame(
        [[10.0, 10.0, 20.0]],
        index=[formation],
        columns=["A", "A", "B"],
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


def test_snapshot_assembly_blocks_duplicate_close_dates(monkeypatch):
    inputs = _minimal_assembly_inputs()
    inputs["closes"] = pd.concat(
        [inputs["closes"], inputs["closes"]],
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize(
    "bad_close_date",
    [
        pytest.param(None, id="null"),
        pytest.param("not-a-date", id="unparsable"),
    ],
)
def test_snapshot_assembly_blocks_invalid_close_dates(
    monkeypatch,
    bad_close_date,
):
    inputs = _minimal_assembly_inputs()
    inputs["closes"].index = [bad_close_date]
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


def test_snapshot_assembly_deduplicates_exact_share_rows(monkeypatch):
    inputs = _minimal_assembly_inputs()
    inputs["shares_pool"] = pd.concat(
        [inputs["shares_pool"], inputs["shares_pool"].iloc[[0]]],
        ignore_index=True,
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    got = build_policy_snapshots(**inputs)
    snapshot = got[pd.Timestamp("2021-06-30")].set_index("ticker")

    assert snapshot.loc["A", "total_market_value"] == 1000.0


def test_snapshot_assembly_blocks_conflicting_share_rows(monkeypatch):
    inputs = _minimal_assembly_inputs()
    conflict = inputs["shares_pool"].iloc[[0]].copy()
    conflict["total_shares"] = 999.0
    inputs["shares_pool"] = pd.concat(
        [inputs["shares_pool"], conflict],
        ignore_index=True,
    )
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize("date_column", ["end_date", "known_date"])
@pytest.mark.parametrize(
    "bad_date",
    [
        pytest.param(None, id="null"),
        pytest.param("not-a-date", id="unparsable"),
    ],
)
def test_snapshot_assembly_blocks_invalid_share_dates(
    monkeypatch,
    date_column,
    bad_date,
):
    inputs = _minimal_assembly_inputs()
    inputs["shares_pool"].loc[0, date_column] = bad_date
    _patch_minimal_assembly_dependencies(monkeypatch)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize(
    ("pool_name", "field", "value_column", "conflicting_value"),
    [
        pytest.param("ttm", "np", "ttm", 999.0, id="ttm"),
        pytest.param("slope", "rev", "slope", 0.9, id="slope"),
        pytest.param("event", "equity", "value", 999.0, id="event"),
    ],
)
def test_snapshot_assembly_blocks_conflicting_derived_rows(
    monkeypatch,
    pool_name,
    field,
    value_column,
    conflicting_value,
):
    inputs = _minimal_assembly_inputs()

    def conflicting_rows(facts):
        rows = _minimal_derived_rows(facts)
        if facts["ts_code"].iloc[0] == "A":
            duplicate = rows[pool_name][
                rows[pool_name]["field"].eq(field)
            ].copy()
            duplicate[value_column] = conflicting_value
            rows[pool_name] = pd.concat(
                [rows[pool_name], duplicate],
                ignore_index=True,
            )
        return rows

    _patch_minimal_assembly_dependencies(monkeypatch, conflicting_rows)

    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize(
    "reverse_rows",
    [
        pytest.param(False, id="original-order"),
        pytest.param(True, id="reversed-order"),
    ],
)
def test_pit_policy_blocks_conflicting_raw_payloads_regardless_of_order(
    reverse_rows,
):
    first = _single_pit_fact(
        data={"revenue": 1.0, "net_profit_parent_ytd": 2.0}
    ).iloc[0].to_dict()
    second = {
        **first,
        "data": {"revenue": 9.0, "net_profit_parent_ytd": 2.0},
    }
    rows = [first, second]
    if reverse_rows:
        rows.reverse()

    with pytest.raises(DataBlocked):
        apply_pit_policy(pd.DataFrame(rows), POLICY_MAIN)


def test_pit_policy_deduplicates_semantically_identical_raw_payloads():
    first = _single_pit_fact(
        data={"revenue": 1.0, "net_profit_parent_ytd": 2.0}
    ).iloc[0].to_dict()
    second = {
        **first,
        "data": {"net_profit_parent_ytd": 2.0, "revenue": 1.0},
    }

    got = apply_pit_policy(pd.DataFrame([first, second]), POLICY_MAIN)

    assert len(got) == 1
    assert got.iloc[0]["data"] == {
        "revenue": 1.0,
        "net_profit_parent_ytd": 2.0,
    }


def test_pit_policy_preserves_legal_restatements_with_different_announcements():
    first = _single_pit_fact(
        stored_ann_date="2020-04-15",
        data={"revenue": 1.0},
    ).iloc[0].to_dict()
    second = {
        **first,
        "stored_ann_date": "2020-04-20",
        "data": {"revenue": 2.0},
    }

    got = apply_pit_policy(pd.DataFrame([first, second]), POLICY_MAIN)

    assert len(got) == 2
    assert list(got["ann_date"]) == [
        pd.Timestamp("2020-04-15"),
        pd.Timestamp("2020-04-20"),
    ]


_RAW_FINANCIAL_COLUMNS = [
    "ts_code",
    "end_date",
    "stored_ann_date",
    "statement_type",
    "data",
    "data_source",
]


class _RawFinancialCursor:
    def __init__(self, rows, execute_error=None):
        self._rows = rows
        self._execute_error = execute_error
        self.description = [
            (column, None, None, None, None, None, None)
            for column in _RAW_FINANCIAL_COLUMNS
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params):
        if self._execute_error is not None:
            raise self._execute_error

    def fetchall(self):
        return self._rows


class _RawFinancialConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _raw_db_row(**overrides):
    row = {
        "ts_code": "X",
        "end_date": "2020-03-31",
        "stored_ann_date": "2020-04-15",
        "statement_type": "income",
        "data": {"B001100000": 1.0},
        "data_source": "csmar",
    }
    row.update(overrides)
    return tuple(row[column] for column in _RAW_FINANCIAL_COLUMNS)


def _patch_raw_financial_connection(
    monkeypatch,
    rows,
    *,
    execute_error=None,
):
    cursor = _RawFinancialCursor(rows, execute_error=execute_error)
    connection = _RawFinancialConnection(cursor)
    monkeypatch.setattr(
        "signals.style_basket.b3_build._connect",
        lambda db: connection,
    )
    return connection


@pytest.mark.parametrize(
    ("date_column", "bad_date"),
    [
        pytest.param("end_date", "not-a-date", id="end-date"),
        pytest.param(
            "stored_ann_date",
            "not-a-date",
            id="stored-announcement-date",
        ),
    ],
)
def test_fetch_raw_financial_wraps_bad_database_dates(
    monkeypatch,
    date_column,
    bad_date,
):
    connection = _patch_raw_financial_connection(
        monkeypatch,
        [_raw_db_row(**{date_column: bad_date})],
    )

    with pytest.raises(DataBlocked) as caught:
        _fetch_raw_financial(
            ["X"],
            "2020-01-01",
            "2020-12-31",
            {"schema": "public"},
        )

    assert caught.value.__cause__ is not None
    assert connection.closed is True


@pytest.mark.parametrize(
    "bad_payload",
    [
        pytest.param("not-a-dict", id="non-dict"),
        pytest.param(
            {"B001100000": object()},
            id="not-canonicalizable",
        ),
    ],
)
def test_fetch_raw_financial_wraps_invalid_payloads(
    monkeypatch,
    bad_payload,
):
    connection = _patch_raw_financial_connection(
        monkeypatch,
        [_raw_db_row(data=bad_payload)],
    )

    with pytest.raises(DataBlocked) as caught:
        _fetch_raw_financial(
            ["X"],
            "2020-01-01",
            "2020-12-31",
            {"schema": "public"},
        )

    assert caught.value.__cause__ is not None
    assert connection.closed is True


def test_fetch_raw_financial_preserves_execute_error_and_closes_connection(
    monkeypatch,
):
    class QueryFailure(RuntimeError):
        pass

    error = QueryFailure("database query failed")
    connection = _patch_raw_financial_connection(
        monkeypatch,
        [],
        execute_error=error,
    )

    with pytest.raises(QueryFailure) as caught:
        _fetch_raw_financial(
            ["X"],
            "2020-01-01",
            "2020-12-31",
            {"schema": "public"},
        )

    assert caught.value is error
    assert connection.closed is True


class _BatchAwareRawFinancialCursor:
    """Serves only rows whose ts_code is in the executed ticker chunk."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed_ticker_chunks = []
        self._current = []
        self.description = [
            (column, None, None, None, None, None, None)
            for column in _RAW_FINANCIAL_COLUMNS
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params):
        chunk = list(params[0])
        self.executed_ticker_chunks.append(chunk)
        requested = set(chunk)
        self._current = sorted(
            (row for row in self._rows if row[0] in requested),
            key=lambda row: (row[0], row[3], row[1]),
        )

    def fetchall(self):
        return self._current


def _run_batch_aware_fetch(monkeypatch, rows, tickers, batch_size):
    cursor = _BatchAwareRawFinancialCursor(rows)
    connection = _RawFinancialConnection(cursor)
    monkeypatch.setattr(
        "signals.style_basket.b3_build._connect",
        lambda db: connection,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._RAW_FINANCIAL_TICKER_BATCH",
        batch_size,
        raising=False,
    )
    frame = _fetch_raw_financial(
        tickers,
        "2020-01-01",
        "2020-12-31",
        {"schema": "public"},
    )
    return frame, cursor


def test_fetch_raw_financial_batches_tickers_and_matches_single_query(
    monkeypatch,
):
    rows = []
    for index in range(7):
        ticker = f"T{index:04d}"
        rows.append(
            _raw_db_row(
                ts_code=ticker,
                end_date="2020-06-30",
                stored_ann_date="2020-07-15",
            )
        )
        rows.append(_raw_db_row(ts_code=ticker))
    scrambled = [
        "T0003",
        "T0001",
        "T0003",
        "T0000",
        "T0002",
        "T0006",
        "T0005",
        "T0004",
    ]

    batched, batched_cursor = _run_batch_aware_fetch(
        monkeypatch,
        rows,
        scrambled,
        3,
    )
    single, single_cursor = _run_batch_aware_fetch(
        monkeypatch,
        rows,
        scrambled,
        100,
    )

    assert batched_cursor.executed_ticker_chunks == [
        ["T0000", "T0001", "T0002"],
        ["T0003", "T0004", "T0005"],
        ["T0006"],
    ]
    assert single_cursor.executed_ticker_chunks == [
        ["T0000", "T0001", "T0002", "T0003", "T0004", "T0005", "T0006"],
    ]
    pd.testing.assert_frame_equal(batched, single)
    assert list(single["ts_code"].unique()) == sorted(set(scrambled))


def test_fetch_raw_financial_batched_empty_result_raises_datablocked(
    monkeypatch,
):
    cursor = _BatchAwareRawFinancialCursor([])
    connection = _RawFinancialConnection(cursor)
    monkeypatch.setattr(
        "signals.style_basket.b3_build._connect",
        lambda db: connection,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._RAW_FINANCIAL_TICKER_BATCH",
        2,
        raising=False,
    )

    with pytest.raises(DataBlocked) as caught:
        _fetch_raw_financial(
            ["A", "B", "C"],
            "2020-01-01",
            "2020-12-31",
            {"schema": "public"},
        )

    assert "no financial facts" in str(caught.value)
    assert cursor.executed_ticker_chunks == [["A", "B"], ["C"]]
    assert connection.closed is True


def test_fetch_raw_financial_skips_empty_batches_without_dtype_damage(
    monkeypatch,
):
    rows = [_raw_db_row(ts_code="C")]

    frame, cursor = _run_batch_aware_fetch(
        monkeypatch,
        rows,
        ["A", "B", "C", "D"],
        2,
    )

    assert cursor.executed_ticker_chunks == [["A", "B"], ["C", "D"]]
    assert list(frame["ts_code"]) == ["C"]
    assert pd.api.types.is_datetime64_any_dtype(frame["end_date"])
    assert pd.api.types.is_datetime64_any_dtype(frame["stored_ann_date"])


def _constituents_for_snapshot(snapshot):
    ordered = snapshot.sort_values(
        ["total_market_value", "ticker"],
        ascending=[False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    effective_date = pd.Timestamp("2021-01-29")
    q500 = pd.DataFrame(
        {
            "index_code": "000905.SH",
            "effective_date": effective_date,
            "ticker": ordered.iloc[300:800]["ticker"].to_numpy(),
        }
    )
    q1000 = pd.DataFrame(
        {
            "index_code": "000852.SH",
            "effective_date": effective_date,
            "ticker": ordered.iloc[800:1800]["ticker"].to_numpy(),
        }
    )
    return pd.concat([q500, q1000], ignore_index=True)


def test_target_coordinate_calibration_matches_synthetic_constituents():
    cfg = load_b3_config()
    formation = pd.Timestamp("2021-01-29")
    snapshot = _synthetic_snapshot()
    exposures = {
        formation: compute_month_exposures(snapshot, cfg),
    }

    got = calibrate_target_coordinates(
        exposures,
        _constituents_for_snapshot(snapshot),
    )

    assert got["q500_mean_abs_error"] <= 0.25
    assert got["q1000_mean_abs_error"] <= 0.25
    assert got["q_order_share"] >= 0.90


def test_target_coordinate_calibration_blocks_missing_q1000_constituents():
    cfg = load_b3_config()
    formation = pd.Timestamp("2021-01-29")
    snapshot = _synthetic_snapshot()
    exposures = {
        formation: compute_month_exposures(snapshot, cfg),
    }
    constituents = _constituents_for_snapshot(snapshot)
    constituents = constituents[
        constituents["index_code"].ne("000852.SH")
    ]

    with pytest.raises(DataBlocked, match="000852.SH"):
        calibrate_target_coordinates(exposures, constituents)


def _preflight_sources(
    snapshot,
    constituents,
    *,
    snapshot_error=None,
    interval_evidence=None,
):
    def snapshots(*args, **kwargs):
        if snapshot_error is not None:
            raise snapshot_error
        if isinstance(snapshot, dict):
            return {
                pd.Timestamp(date): frame.copy()
                for date, frame in snapshot.items()
            }
        return {pd.Timestamp("2021-01-29"): snapshot.copy()}

    def constituent_source(*args, **kwargs):
        return constituents.copy()

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "preflight must not access returns or carry inputs"
        )

    return B3Sources(
        snapshots=snapshots,
        constituents=constituent_source,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
        suspension_interval_evidence=interval_evidence,
    )


def _single_month_preflight_config():
    cfg = deepcopy(load_b3_config())
    cfg["windows"]["discovery"] = ["2021-01-01", "2021-01-31"]
    cfg["windows"]["confirmation"] = ["2021-01-01", "2021-01-31"]
    return cfg


def _valid_preflight_interval_evidence():
    return pd.DataFrame(
        [
            {
                "ts_code": "B.SZ",
                "formation_date": "2022-01-28",
                "list_date": "2010-01-01",
                "delist_date": None,
                "suspension_start": None,
                "previous_official_trade_date": None,
                "previous_close_date": "2022-01-27",
                "previous_close": 8.5,
                "suspend_type": "",
                "suspend_reason": "",
                "evidence_method": "",
                "accepted": False,
                "rejection_reason": "NO_EXPLICIT_SUSPENSION_START",
                "next_trade_date": None,
                "next_nonnull_close": None,
                "exact_stock_status_confirmed": pd.NA,
            },
            {
                "ts_code": "A.SZ",
                "formation_date": "2021-01-29",
                "list_date": "2010-01-01",
                "delist_date": None,
                "suspension_start": "2021-01-28",
                "previous_official_trade_date": "2021-01-27",
                "previous_close_date": "2021-01-27",
                "previous_close": 10.0,
                "suspend_type": "今起停牌",
                "suspend_reason": "重大事项",
                "evidence_method": INTERVAL_METHOD,
                "accepted": True,
                "rejection_reason": "",
                "next_trade_date": "2021-02-01",
                "next_nonnull_close": 10.5,
                "exact_stock_status_confirmed": True,
            },
        ],
        columns=CORE_EVIDENCE_COLUMNS,
    )


def _aligned_interval_snapshots(*, accepted_method=INTERVAL_METHOD):
    base = _synthetic_snapshot()
    base.loc[0, "ticker"] = "A.SZ"
    base.loc[1, "ticker"] = "B.SZ"
    first = base.copy()
    first.loc[first["ticker"].eq("A.SZ"), "close_method"] = accepted_method
    first.loc[first["ticker"].eq("A.SZ"), "close_carried"] = True
    second = base.copy()
    second["formation_date"] = pd.Timestamp("2022-01-28")
    return (
        {
            pd.Timestamp("2021-01-29"): first,
            pd.Timestamp("2022-01-28"): second,
        },
        _constituents_for_snapshot(base),
    )


class _UnhashableColumnLabel:
    __hash__ = None

    def __repr__(self):
        return "UnhashableColumnLabel()"


class _ComparisonRaisingColumnLabel:
    def __hash__(self):
        return 314159

    def __lt__(self, other):
        del other
        raise AssertionError("column labels must not be compared")

    def __repr__(self):
        return "ComparisonRaisingColumnLabel()"


class _ReprRaisingColumnLabel:
    def __hash__(self):
        return 271828

    def __repr__(self):
        raise RuntimeError("column label repr failed")


def _with_extra_column_labels(frame, labels):
    extras = np.full((len(frame), len(labels)), "drift", dtype=object)
    result = pd.DataFrame(
        np.column_stack([frame.to_numpy(dtype=object), extras])
    )
    result.columns = [*frame.columns, *labels]
    return result


def test_preflight_is_return_blind_and_writes_ok_artifacts(tmp_path):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
    )

    got = run_preflight(
        cfg,
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "OK"
    assert (tmp_path / "coverage_audit.csv").is_file()
    assert (tmp_path / "manifests" / "preflight.json").is_file()


def test_preflight_publishes_sorted_hash_bound_interval_artifact(tmp_path):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots()
    callback_dates = []
    sources = replace(
        _preflight_sources(snapshots, constituents),
        suspension_interval_evidence=lambda data_end: (
            callback_dates.append(data_end)
            or _valid_preflight_interval_evidence()
        ),
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "OK"
    assert callback_dates == [pd.Timestamp("2023-12-31")]
    path = tmp_path / "suspension_interval_evidence.csv"
    evidence = pd.read_csv(path)
    assert tuple(evidence.columns) == SUSPENSION_INTERVAL_ARTIFACT_COLUMNS
    assert evidence["required_formation"].tolist() == [True, False]
    assert evidence["accepted"].tolist() == [True, False]
    assert evidence["previous_close"].tolist() == [10.0, 8.5]
    assert evidence["next_nonnull_close"].iloc[0] == 10.5
    assert pd.isna(evidence["next_nonnull_close"].iloc[1])
    serialized = pd.read_csv(path, keep_default_na=False, dtype=str)
    expected = pd.DataFrame(
        [
            {
                "ts_code": "A.SZ",
                "formation_date": "2021-01-29",
                "required_formation": "True",
                "list_date": "2010-01-01",
                "delist_date": "",
                "suspension_start": "2021-01-28",
                "previous_official_trade_date": "2021-01-27",
                "previous_close_date": "2021-01-27",
                "previous_close": "10.0",
                "suspend_type": "今起停牌",
                "suspend_reason": "重大事项",
                "evidence_method": INTERVAL_METHOD,
                "accepted": "True",
                "rejection_reason": "",
                "next_trade_date": "2021-02-01",
                "next_nonnull_close": "10.5",
                "exact_stock_status_confirmed": "True",
            },
            {
                "ts_code": "B.SZ",
                "formation_date": "2022-01-28",
                "required_formation": "False",
                "list_date": "2010-01-01",
                "delist_date": "",
                "suspension_start": "",
                "previous_official_trade_date": "",
                "previous_close_date": "2022-01-27",
                "previous_close": "8.5",
                "suspend_type": "",
                "suspend_reason": "",
                "evidence_method": "",
                "accepted": "False",
                "rejection_reason": "NO_EXPLICIT_SUSPENSION_START",
                "next_trade_date": "",
                "next_nonnull_close": "",
                "exact_stock_status_confirmed": "",
            },
        ],
        columns=SUSPENSION_INTERVAL_ARTIFACT_COLUMNS,
        dtype=str,
    )
    pd.testing.assert_frame_equal(serialized, expected)
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(manifest["outputs"]) == {
        "coverage_audit.csv",
        "exposure_diagnostics.csv",
        "suspension_interval_evidence.csv",
    }
    assert manifest["outputs"]["suspension_interval_evidence.csv"] == (
        _file_digest(path)
    )


def test_preflight_without_interval_callback_writes_standard_empty_artifact(
    tmp_path,
):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()

    run_preflight(
        cfg,
        _preflight_sources(snapshot, _constituents_for_snapshot(snapshot)),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    evidence = pd.read_csv(tmp_path / "suspension_interval_evidence.csv")
    assert tuple(evidence.columns) == SUSPENSION_INTERVAL_ARTIFACT_COLUMNS
    assert evidence.empty


def test_preflight_early_source_block_replaces_stale_interval_artifact_without_callback(
    tmp_path,
):
    stale = tmp_path / "suspension_interval_evidence.csv"
    stale.write_text("stale\nold\n", encoding="utf-8")
    calls = []
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        snapshot_error=DataBlocked("DATA_TEST_SNAPSHOT_BLOCK"),
        interval_evidence=lambda data_end: calls.append(data_end),
    )

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    assert calls == []
    evidence = pd.read_csv(stale)
    assert tuple(evidence.columns) == SUSPENSION_INTERVAL_ARTIFACT_COLUMNS
    assert evidence.empty
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["outputs"]["suspension_interval_evidence.csv"] == (
        _file_digest(stale)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda frame: object(), id="not-dataframe"),
        pytest.param(
            lambda frame: frame.drop(columns="previous_close"),
            id="missing-column",
        ),
        pytest.param(
            lambda frame: frame.assign(extra="drift"), id="extra-column"
        ),
        pytest.param(
            lambda frame: frame.rename(
                columns={"previous_close": "previous_close_date"}
            ),
            id="duplicate-column",
        ),
        pytest.param(
            lambda frame: frame.assign(accepted="true"), id="string-bool"
        ),
        pytest.param(
            lambda frame: frame.assign(accepted=1), id="integer-bool"
        ),
        pytest.param(
            lambda frame: frame.assign(previous_close="10.0"),
            id="string-number",
        ),
        pytest.param(
            lambda frame: frame.assign(exact_stock_status_confirmed="yes"),
            id="report-string-bool",
        ),
        pytest.param(
            lambda frame: pd.concat(
                [frame, frame.iloc[[0]]], ignore_index=True
            ),
            id="duplicate-logical-key",
        ),
        pytest.param(
            lambda frame: frame.assign(
                evidence_method="", rejection_reason=""
            ),
            id="accepted-method-mismatch",
        ),
        pytest.param(
            lambda frame: frame.assign(
                evidence_method=INTERVAL_METHOD, rejection_reason=""
            ),
            id="rejected-method-mismatch",
        ),
        pytest.param(
            lambda frame: frame.assign(
                rejection_reason=frame["rejection_reason"].mask(
                    ~frame["accepted"], "NOT_A_CONTRACT_ENUM"
                )
            ),
            id="unknown-rejection-enum",
        ),
        pytest.param(
            lambda frame: frame.assign(
                suspend_type=frame["suspend_type"].mask(
                    frame["accepted"], ""
                )
            ),
            id="accepted-missing-suspend-type",
        ),
        pytest.param(
            lambda frame: frame.assign(
                suspension_start=frame["suspension_start"].mask(
                    frame["accepted"], "2021-02-01"
                )
            ),
            id="start-after-formation",
        ),
        pytest.param(
            lambda frame: frame.assign(
                list_date=frame["list_date"].mask(
                    frame["accepted"], "2021-02-01"
                )
            ),
            id="accepted-outside-listing-interval",
        ),
        pytest.param(
            lambda frame: frame.assign(
                previous_close_date=frame["previous_close_date"].mask(
                    frame["accepted"], "2021-01-26"
                )
            ),
            id="accepted-previous-date-mismatch",
        ),
        pytest.param(
            lambda frame: frame.assign(
                next_trade_date=frame["next_trade_date"].mask(
                    frame["accepted"], "2021-01-29"
                )
            ),
            id="future-report-not-after-formation",
        ),
    ],
)
def test_invalid_interval_callback_contract_becomes_manifested_data_block(
    tmp_path,
    mutate,
):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    raw = mutate(_valid_preflight_interval_evidence())
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=lambda data_end: raw,
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "DATA_BLOCKED"
    evidence = pd.read_csv(tmp_path / "suspension_interval_evidence.csv")
    assert tuple(evidence.columns) == SUSPENSION_INTERVAL_ARTIFACT_COLUMNS
    assert evidence.empty
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    blockers = [
        blocker
        for blocker in manifest["blockers"]
        if blocker["check"] == "suspension_interval_evidence"
    ]
    assert blockers
    assert blockers[0]["status"] == "DATA_BLOCKED"
    assert blockers[0]["reason_code"] == "DATA_CONTRACT"


@pytest.mark.parametrize("column", ["previous_close", "next_nonnull_close"])
def test_huge_interval_numeric_becomes_field_named_manifested_block(
    tmp_path,
    column,
):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    raw = _valid_preflight_interval_evidence()
    raw[column] = raw[column].astype(object)
    raw.loc[raw["accepted"], column] = 10**10000
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=lambda data_end: raw,
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "DATA_BLOCKED"
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    blockers = [
        blocker
        for blocker in manifest["blockers"]
        if blocker["check"] == "suspension_interval_evidence"
    ]
    assert len(blockers) == 1
    assert column in blockers[0]["detail"]


def _mutate_rejected_row(frame, **updates):
    result = frame.copy()
    rejected = ~result["accepted"]
    for column, value in updates.items():
        result.loc[rejected, column] = value
    return result


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                suspension_start="2022-01-28",
                suspend_type="今起停牌",
            ),
            id="no-explicit-start-with-start",
        ),
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                rejection_reason="SUSPENSION_START_PRECEDES_SOURCE_COVERAGE",
                suspension_start="2022-01-28",
                suspend_type="今起停牌",
            ),
            id="coverage-start-with-start",
        ),
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                rejection_reason="START_NOT_OFFICIAL_TRADING_DAY",
            ),
            id="start-required-without-start",
        ),
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                rejection_reason="OUTSIDE_LEGAL_LISTING_INTERVAL",
            ),
            id="outside-reason-inside-listing",
        ),
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                rejection_reason="PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY",
                suspension_start="2022-01-28",
                previous_official_trade_date="2022-01-27",
                suspend_type="今起停牌",
            ),
            id="previous-mismatch-reason-with-equal-dates",
        ),
        pytest.param(
            _mutate_rejected_row(
                _valid_preflight_interval_evidence(),
                rejection_reason="INVALID_PREVIOUS_CLOSE",
                suspension_start="2022-01-28",
                previous_official_trade_date="2022-01-27",
                suspend_type="今起停牌",
            ),
            id="invalid-reason-with-valid-previous-pair",
        ),
    ],
)
def test_self_contradictory_rejection_evidence_is_manifested_block(
    tmp_path,
    raw,
):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=lambda data_end: raw,
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "DATA_BLOCKED"
    assert (
        got.audit["check"].eq("suspension_interval_evidence").sum() == 1
    )


def test_invalid_previous_close_allows_finite_but_stale_previous_pair():
    raw = _mutate_rejected_row(
        _valid_preflight_interval_evidence(),
        rejection_reason="INVALID_PREVIOUS_CLOSE",
        suspension_start="2022-01-28",
        previous_official_trade_date="2022-01-27",
        previous_close_date="2022-01-26",
        suspend_type="今起停牌",
    )

    got = b3_build_module._preflight_interval_evidence(
        raw,
        pd.Timestamp("2021-01-01"),
        pd.Timestamp("2021-01-31"),
    )

    assert len(got) == 2


def test_previous_close_not_prior_roundtrips_without_prior_official():
    formation = pd.Timestamp("2021-01-04")
    evidence = build_continuous_suspension_evidence(
        candidates=pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "formation_date": [formation],
                "list_date": ["2010-01-01"],
                "delist_date": [None],
            }
        ),
        trading_calendar=pd.DataFrame(
            {"calendar_date": [formation], "sfe": [True]}
        ),
        prices=pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": [pd.Timestamp("2021-01-01")],
                "close": [10.0],
            }
        ),
        suspension_events=pd.DataFrame(
            {
                "ts_code": ["A.SZ"],
                "trade_date": [formation],
                "suspend_type": ["今起停牌"],
                "suspend_reason": ["重大事项"],
            }
        ),
        suspension_source_start=pd.Timestamp("2020-01-01"),
    )
    row = evidence.iloc[0]
    assert row["rejection_reason"] == (
        "PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY"
    )
    assert pd.isna(row["previous_official_trade_date"])

    got = b3_build_module._preflight_interval_evidence(
        evidence,
        formation,
        formation,
    )

    assert len(got) == 1


def _alignment_rows(outcome):
    return outcome.audit[
        outcome.audit["check"].eq(
            "suspension_interval_evidence_alignment"
        )
    ]


@pytest.mark.parametrize("mismatch", ["no-carry", "missing-ticker"])
def test_accepted_evidence_requires_snapshot_carry_and_ticker(
    tmp_path,
    mismatch,
):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots()
    first = snapshots[pd.Timestamp("2021-01-29")]
    if mismatch == "no-carry":
        mask = first["ticker"].eq("A.SZ")
        first.loc[mask, "close_method"] = ""
        first.loc[mask, "close_carried"] = False
    else:
        snapshots[pd.Timestamp("2021-01-29")] = first[
            first["ticker"].ne("A.SZ")
        ].copy()

    got = run_preflight(
        cfg,
        _preflight_sources(
            snapshots,
            constituents,
            interval_evidence=lambda data_end: (
                _valid_preflight_interval_evidence()
            ),
        ),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    rows = _alignment_rows(got)
    assert len(rows) == 1
    assert mismatch.replace("-", "_") in rows.iloc[0]["detail"]


def test_empty_artifact_blocks_snapshot_interval_carry(tmp_path):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots()

    got = run_preflight(
        cfg,
        _preflight_sources(
            snapshots,
            constituents,
            interval_evidence=lambda data_end: empty_interval_evidence(),
        ),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    rows = _alignment_rows(got)
    assert len(rows) == 1
    assert "missing_accepted_evidence" in rows.iloc[0]["detail"]


def test_rejected_evidence_cannot_back_snapshot_interval_carry(tmp_path):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots()
    second = snapshots[pd.Timestamp("2022-01-28")]
    mask = second["ticker"].eq("B.SZ")
    second.loc[mask, "close_method"] = INTERVAL_METHOD
    second.loc[mask, "close_carried"] = True

    got = run_preflight(
        cfg,
        _preflight_sources(
            snapshots,
            constituents,
            interval_evidence=lambda data_end: (
                _valid_preflight_interval_evidence()
            ),
        ),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    rows = _alignment_rows(got)
    assert len(rows) == 1
    assert "rejected_interval_carry" in rows.iloc[0]["detail"]


@pytest.mark.parametrize(
    "accepted_method",
    [INTERVAL_METHOD, EXACT_CARRY_METHOD],
)
def test_both_policies_reconcile_interval_evidence_and_exact_shadow(
    tmp_path,
    accepted_method,
):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots(
        accepted_method=accepted_method
    )
    policy_calls = []
    sources = _preflight_sources(
        snapshots,
        constituents,
        interval_evidence=lambda data_end: (
            _valid_preflight_interval_evidence()
        ),
    )
    sources = replace(
        sources,
        snapshots=lambda policy, data_end: (
            policy_calls.append(policy)
            or {
                date: frame.copy()
                for date, frame in snapshots.items()
            }
        ),
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "OK"
    assert policy_calls == list(cfg["pit"]["policies"])
    assert _alignment_rows(got).empty


@pytest.mark.parametrize(
    "accepted_method",
    [INTERVAL_METHOD, EXACT_CARRY_METHOD],
)
def test_evidence_close_mismatch_blocks_interval_and_exact_shadow(
    tmp_path,
    accepted_method,
):
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots(
        accepted_method=accepted_method
    )
    evidence = _valid_preflight_interval_evidence()
    evidence.loc[evidence["accepted"], "previous_close"] = 999.0

    got = run_preflight(
        cfg,
        _preflight_sources(
            snapshots,
            constituents,
            interval_evidence=lambda data_end: evidence,
        ),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    rows = _alignment_rows(got)
    assert len(rows) == 1
    assert "close_mismatch" in rows.iloc[0]["detail"]


@pytest.mark.parametrize(
    "labels,detail",
    [
        pytest.param(
            (1, "z"),
            "extra=['z', 1]",
            id="heterogeneous-hashable-labels",
        ),
        pytest.param(
            (_UnhashableColumnLabel(),),
            "extra=[UnhashableColumnLabel()]",
            id="unhashable-label",
        ),
        pytest.param(
            (_ComparisonRaisingColumnLabel(), "z"),
            "extra=['z', ComparisonRaisingColumnLabel()]",
            id="comparison-raising-label",
        ),
        pytest.param(
            (_ReprRaisingColumnLabel(),),
            "column label repr failed",
            id="repr-raising-label",
        ),
    ],
)
def test_nonstandard_interval_columns_become_manifested_data_block(
    tmp_path,
    labels,
    detail,
):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    raw = _with_extra_column_labels(
        _valid_preflight_interval_evidence(), labels
    )
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=lambda data_end: raw,
    )

    got = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )

    assert got.final_status == "DATA_BLOCKED"
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    blockers = [
        blocker
        for blocker in manifest["blockers"]
        if blocker["check"] == "suspension_interval_evidence"
    ]
    assert len(blockers) == 1
    assert detail in blockers[0]["detail"]


def test_interval_callback_programming_error_is_not_manifested(tmp_path):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()

    def broken_callback(data_end):
        del data_end
        raise RuntimeError("callback programming error")

    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=broken_callback,
    )

    with pytest.raises(RuntimeError, match="callback programming error"):
        run_preflight(
            cfg,
            sources,
            pd.Timestamp("2023-12-31"),
            tmp_path,
        )

    assert not (tmp_path / "manifests" / "preflight.json").exists()


def test_preflight_manifest_is_written_after_all_three_atomic_outputs(
    monkeypatch,
    tmp_path,
):
    events = []
    real_csv = b3_build_module._write_csv_atomic
    real_manifest = b3_build_module._write_stage_manifest

    def tracked_csv(frame, path, **kwargs):
        result = real_csv(frame, path, **kwargs)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        events.append(("csv", Path(path).name, int(len(frame)), digest))
        return result

    def tracked_manifest(*args, **kwargs):
        events.append(("manifest", args[1]))
        return real_manifest(*args, **kwargs)

    monkeypatch.setattr(b3_build_module, "_write_csv_atomic", tracked_csv)
    monkeypatch.setattr(
        b3_build_module, "_write_stage_manifest", tracked_manifest
    )
    cfg = _single_month_preflight_config()
    snapshots, constituents = _aligned_interval_snapshots()

    run_preflight(
        cfg,
        _preflight_sources(
            snapshots,
            constituents,
            interval_evidence=lambda data_end: (
                _valid_preflight_interval_evidence()
            ),
        ),
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    manifest_index = events.index(("manifest", "preflight"))
    written_names = {
        event[1]
        for event in events[:manifest_index]
        if event[0] == "csv"
    }
    assert {
        "coverage_audit.csv",
        "exposure_diagnostics.csv",
        "suspension_interval_evidence.csv",
    }.issubset(written_names)
    evidence_writes = [
        event
        for event in events[:manifest_index]
        if event[0:2] == ("csv", "suspension_interval_evidence.csv")
    ]
    assert [event[2] for event in evidence_writes] == [0, 2]
    assert evidence_writes[0][3] != evidence_writes[1][3]
    published_hash = hashlib.sha256(
        (tmp_path / "suspension_interval_evidence.csv").read_bytes()
    ).hexdigest()
    assert evidence_writes[1][3] == published_hash


def test_preflight_writes_blocked_artifacts_when_snapshots_are_blocked(
    tmp_path,
):
    cfg = load_b3_config()
    snapshot = _synthetic_snapshot()
    output_dir = tmp_path / "blocked"
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        snapshot_error=DataBlocked("DATA_TEST_SNAPSHOT_BLOCK"),
    )

    got = run_preflight(
        cfg,
        sources,
        pd.Timestamp("2023-12-31"),
        output_dir,
    )

    assert got.final_status == "DATA_BLOCKED"
    assert (output_dir / "coverage_audit.csv").is_file()
    assert (output_dir / "manifests" / "preflight.json").is_file()


def test_flatten_exposures_preserves_size_and_model_universes():
    cfg = load_b3_config()
    formation = pd.Timestamp("2021-01-29")
    snapshot = _synthetic_snapshot()
    size_only_ticker = snapshot.iloc[-1]["ticker"]
    snapshot.loc[
        snapshot["ticker"].eq(size_only_ticker),
        "style_score",
    ] = np.nan
    result = compute_month_exposures(snapshot, cfg)
    exposures = {
        POLICY_MAIN: {formation: result},
        POLICY_LAG: {formation: result},
    }

    got = flatten_exposures(exposures)

    for policy in (POLICY_MAIN, POLICY_LAG):
        policy_rows = got[got["pit_policy"].eq(policy)]
        assert len(policy_rows) == result.diagnostics["size_n"]
        assert policy_rows["ticker"].is_unique
        roles = policy_rows.set_index("ticker")["universe_role"]
        assert roles.loc[size_only_ticker] == "size_only"
        assert roles.drop(index=size_only_ticker).eq("model").all()

    required_columns = {
        "s_perp",
        "h_perp",
        "x_qblend",
        "x_q500",
        "x_q1000",
        "w_size_plus",
        "w_size_minus",
    }
    for axis in (
        "style",
        "interaction",
        "qblend",
        "q500",
        "q1000",
    ):
        for side in ("plus", "minus"):
            required_columns.add(f"w_{axis}_{side}")
    assert required_columns.issubset(got.columns)


def test_exposures_stage_writes_artifacts_and_requires_untampered_preflight(
    tmp_path,
):
    cfg = _single_month_preflight_config()
    data_end = pd.Timestamp("2023-12-31")
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
    )
    preflight = run_preflight(cfg, sources, data_end, tmp_path)
    assert preflight.final_status == "OK"

    run_exposures_stage(
        cfg,
        data_end,
        tmp_path,
        preflight,
    )

    assert (tmp_path / "monthly_exposures.csv.gz").is_file()
    assert (tmp_path / "manifests" / "exposures.json").is_file()

    (tmp_path / "coverage_audit.csv").write_text(
        "tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(DataBlocked, match="hash"):
        run_exposures_stage(
            cfg,
            data_end,
            tmp_path,
            preflight,
        )


def test_default_sources_cache_formation_inputs_across_pit_policies(
    monkeypatch,
):
    data_end = pd.Timestamp("2023-12-31")
    db = object()
    sentinel = {
        "facts": object(),
        "month_ends": object(),
        "closes": object(),
        "shares": object(),
        "industry": object(),
        "meta": object(),
        "suspensions": object(),
        "carried_closes": object(),
        "interval_carried_closes": object(),
    }
    formation_calls = []
    build_calls = []

    def fake_formation_inputs(*args, **kwargs):
        formation_calls.append((args, kwargs))
        return sentinel

    def fake_build_policy_snapshots(
        facts,
        month_ends,
        closes,
        shares,
        industry,
        meta,
        policy,
        *,
        suspensions=None,
        carried_closes=None,
        interval_carried_closes=None,
    ):
        build_calls.append(
            {
                "policy": policy,
                "facts": facts,
                "month_ends": month_ends,
                "closes": closes,
                "shares": shares,
                "industry": industry,
                "meta": meta,
                "suspensions": suspensions,
                "carried_closes": carried_closes,
                "interval_carried_closes": interval_carried_closes,
            }
        )
        return {pd.Timestamp("2021-01-29"): pd.DataFrame()}

    monkeypatch.setattr(
        "signals.style_basket.b3_build._formation_inputs",
        fake_formation_inputs,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.build_policy_snapshots",
        fake_build_policy_snapshots,
    )

    sources = default_sources(db)
    sources.snapshots(POLICY_MAIN, data_end)
    sources.snapshots(POLICY_LAG, data_end)

    assert len(formation_calls) == 1
    assert [call["policy"] for call in build_calls] == [
        POLICY_MAIN,
        POLICY_LAG,
    ]
    for key, value in sentinel.items():
        assert build_calls[0][key] is value
        assert build_calls[1][key] is value


def test_default_sources_interval_evidence_cache_miss_is_query_free(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("evidence cache miss must not load formation inputs")

    monkeypatch.setattr(
        "signals.style_basket.b3_build._formation_inputs",
        forbidden,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        forbidden,
    )

    sources = default_sources({"schema": "public"})
    first = sources.suspension_interval_evidence(
        pd.Timestamp("2023-12-31")
    )
    first["probe"] = "mutated"
    second = sources.suspension_interval_evidence(
        pd.Timestamp("2023-12-31")
    )

    assert (
        B3Sources.__dataclass_fields__["suspension_interval_evidence"].default
        is None
    )
    assert tuple(second.columns) == CORE_EVIDENCE_COLUMNS
    assert second.empty
    assert "probe" not in second.columns


def test_default_sources_interval_evidence_callback_returns_cached_copy(
    monkeypatch,
):
    data_end = pd.Timestamp("2023-12-31")
    evidence = pd.DataFrame(
        [{column: pd.NA for column in CORE_EVIDENCE_COLUMNS}],
        columns=CORE_EVIDENCE_COLUMNS,
    )
    evidence.loc[0, "ts_code"] = "A"
    sentinel = {
        "facts": object(),
        "month_ends": object(),
        "closes": object(),
        "shares": object(),
        "industry": object(),
        "meta": object(),
        "suspensions": object(),
        "carried_closes": object(),
        "interval_evidence": evidence,
        "interval_carried_closes": pd.DataFrame(
            columns=["formation_date", "ts_code", "close_date", "close"]
        ),
    }
    calls = []

    def fake_formation_inputs(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(
        "signals.style_basket.b3_build._formation_inputs",
        fake_formation_inputs,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.build_policy_snapshots",
        lambda *args, **kwargs: {},
    )

    sources = default_sources({"schema": "public"})
    sources.snapshots(POLICY_MAIN, data_end)
    first = sources.suspension_interval_evidence(data_end)
    first.loc[0, "ts_code"] = "MUTATED"
    second = sources.suspension_interval_evidence(data_end)

    assert len(calls) == 1
    assert first is not evidence
    pd.testing.assert_frame_equal(second, evidence)


def test_cli_rejects_unfrozen_config_override(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["b3_build.py", "--config", "x"],
    )

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 2


def test_cli_preflight_stage_does_not_run_exposures(
    monkeypatch,
    tmp_path,
):
    calls = {"preflight": 0, "exposures": 0}
    db = object()
    source_sentinel = object()

    class Outcome:
        final_status = "OK"

    def fake_default_sources(got_db):
        assert got_db is db
        return source_sentinel

    def fake_run_preflight(cfg, sources, data_end, output_dir):
        assert sources is source_sentinel
        calls["preflight"] += 1
        return Outcome()

    def forbidden_exposures(*args, **kwargs):
        calls["exposures"] += 1
        raise AssertionError("preflight CLI stage must not run exposures")

    monkeypatch.setattr(
        "signals.style_basket.b3_build.load_db_config",
        lambda: db,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.default_sources",
        fake_default_sources,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_preflight",
        fake_run_preflight,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build.run_exposures_stage",
        forbidden_exposures,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "b3_build.py",
            "--stage",
            "preflight",
            "--data-end",
            "2023-12-31",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert calls == {"preflight": 1, "exposures": 0}


def _required_formation_grid():
    return list(
        pd.period_range(
            "2014-01",
            "2023-12",
            freq="M",
        ).to_timestamp("M")
    )


def _lightweight_exposure_result():
    index = pd.Index(["A", "B"], name="ticker_index")
    size = pd.DataFrame(
        {
            "ticker": ["A", "B"],
            "m_perp": [-1.0, 1.0],
            "w_size_plus": [0.0, 1.0],
            "w_size_minus": [1.0, 0.0],
        },
        index=index,
    )
    model = pd.DataFrame(
        {
            "ticker": ["A", "B"],
            "s_perp": [-1.0, 1.0],
            "h_perp": [-1.0, 1.0],
            "x_qblend": [-1.0, 1.0],
            "x_q500": [-1.0, 1.0],
            "x_q1000": [-1.0, 1.0],
        },
        index=index,
    )
    for axis in (
        "style",
        "interaction",
        "qblend",
        "q500",
        "q1000",
    ):
        model[f"w_{axis}_plus"] = [0.0, 1.0]
        model[f"w_{axis}_minus"] = [1.0, 0.0]
    return ExposureResult(
        size=size,
        model=model,
        q={"q500": -1.0, "q1000": 1.0, "qblend": 0.0},
        diagnostics={
            "size_n": 2,
            "model_n": 2,
            "max_orthogonality_error": 0.0,
        },
    )


def _two_target_constituents():
    return pd.DataFrame(
        {
            "index_code": ["000905.SH", "000852.SH"],
            "effective_date": ["2021-01-01", "2021-01-01"],
            "ticker": ["A", "B"],
        }
    )


def _grid_preflight_sources(policy_snapshots):
    def snapshots(policy, data_end):
        return policy_snapshots[policy]

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not load returns")

    return B3Sources(
        snapshots=snapshots,
        constituents=_two_target_constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )


def _snapshot_map(dates):
    return {
        pd.Timestamp(date): pd.DataFrame(
            {
                "ticker": ["A", "B"],
                "close": [10.0, 20.0],
                "close_method": ["", ""],
                "close_carried": [False, False],
            }
        )
        for date in dates
    }


def _patch_lightweight_exposures(monkeypatch):
    monkeypatch.setattr(
        "signals.style_basket.b3_build.compute_month_exposures",
        lambda snapshot, cfg: _lightweight_exposure_result(),
    )


def test_preflight_blocks_data_end_before_confirmation_end(
    monkeypatch,
    tmp_path,
):
    grid = _required_formation_grid()
    snapshots = _snapshot_map(grid)
    sources = _grid_preflight_sources(
        {
            POLICY_MAIN: snapshots,
            POLICY_LAG: snapshots,
        }
    )
    _patch_lightweight_exposures(monkeypatch)

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-11-30"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"


@pytest.mark.parametrize(
    "missing_position",
    [
        pytest.param(0, id="first-month"),
        pytest.param(60, id="middle-month"),
        pytest.param(-1, id="last-month"),
    ],
)
def test_preflight_blocks_any_missing_required_month(
    monkeypatch,
    tmp_path,
    missing_position,
):
    grid = _required_formation_grid()
    missing_date = grid[missing_position]
    incomplete = _snapshot_map(
        date for date in grid if date != missing_date
    )
    sources = _grid_preflight_sources(
        {
            POLICY_MAIN: incomplete,
            POLICY_LAG: incomplete,
        }
    )
    _patch_lightweight_exposures(monkeypatch)

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"


def test_preflight_blocks_mismatched_required_keys_between_pit_policies(
    monkeypatch,
    tmp_path,
):
    grid = _required_formation_grid()
    main_snapshots = _snapshot_map(grid)
    lag_dates = list(grid)
    midpoint = len(lag_dates) // 2
    lag_dates[midpoint] = lag_dates[midpoint] - pd.Timedelta(days=1)
    lag_snapshots = _snapshot_map(lag_dates)
    sources = _grid_preflight_sources(
        {
            POLICY_MAIN: main_snapshots,
            POLICY_LAG: lag_snapshots,
        }
    )
    _patch_lightweight_exposures(monkeypatch)

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"


def test_blocked_manifest_blockers_use_complete_audit_schema(tmp_path):
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        snapshot_error=DataBlocked("DATA_TEST_SNAPSHOT_BLOCK"),
    )

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    manifest = json.loads(
        (tmp_path / "manifests" / "preflight.json").read_text(
            encoding="utf-8"
        )
    )
    expected_columns = {
        "pit_policy",
        "formation_date",
        "required_formation",
        "affects_final",
        "check",
        "side",
        "eligible_count",
        "max_weight",
        "status",
        "reason_code",
        "detail",
    }
    assert manifest["blockers"]
    assert set(manifest["blockers"][0]) == expected_columns


def test_exclusion_audit_copies_reason_into_reason_code(
    monkeypatch,
    tmp_path,
):
    cfg = _single_month_preflight_config()
    formation = pd.Timestamp("2021-01-29")
    snapshot = pd.DataFrame(
        {
            "ticker": ["A", "B"],
            "close": [10.0, 20.0],
            "style_score": [0.0, 0.0],
            "size_exclusion_reason": ["", "LISTED_LT_180D"],
            "model_exclusion_reason": ["", "MISSING_STYLE_SCORE"],
            "close_method": ["", ""],
            "close_carried": [False, False],
        }
    )
    snapshots = {formation: snapshot}

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must remain return-blind")

    sources = B3Sources(
        snapshots=lambda policy, data_end: snapshots,
        constituents=_two_target_constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )
    _patch_lightweight_exposures(monkeypatch)

    got = run_preflight(
        cfg,
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "OK"
    excluded = got.audit[
        got.audit["side"].isin(
            ["LISTED_LT_180D", "MISSING_STYLE_SCORE"]
        )
    ]
    assert not excluded.empty
    assert excluded["reason_code"].to_list() == excluded["side"].to_list()


def _file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _valid_parent_manifest(tmp_path, cfg):
    coverage = tmp_path / "coverage_audit.csv"
    diagnostics = tmp_path / "exposure_diagnostics.csv"
    evidence = tmp_path / "suspension_interval_evidence.csv"
    coverage.write_text("coverage\n", encoding="utf-8")
    diagnostics.write_text("diagnostics\n", encoding="utf-8")
    evidence.write_text(
        ",".join(SUSPENSION_INTERVAL_ARTIFACT_COLUMNS) + "\n",
        encoding="utf-8",
    )
    return {
        "stage": "preflight",
        "config_hash": config_hash(cfg),
        "data_end": "2023-12-31",
        "status": "OK",
        "blockers": [],
        "outputs": {
            "coverage_audit.csv": _file_digest(coverage),
            "exposure_diagnostics.csv": _file_digest(diagnostics),
            "suspension_interval_evidence.csv": _file_digest(evidence),
        },
    }


def _write_parent_manifest(tmp_path, payload):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "preflight.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_parent_manifest_rejects_non_mapping_root(tmp_path):
    _write_parent_manifest(tmp_path, [])

    with pytest.raises(DataBlocked, match="object"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            load_b3_config(),
            pd.Timestamp("2023-12-31"),
        )


def test_parent_manifest_rejects_stage_mismatch(tmp_path):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    payload["stage"] = "not-preflight"
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="stage"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


def test_parent_manifest_rejects_empty_outputs(tmp_path):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    payload["outputs"] = {}
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="outputs"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


def test_parent_manifest_requires_every_preflight_output(tmp_path):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    payload["outputs"].pop("exposure_diagnostics.csv")
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="output set"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


@pytest.mark.parametrize(
    "bad_hash",
    [
        pytest.param("0" * 63, id="wrong-length"),
        pytest.param("g" * 64, id="non-hex"),
    ],
)
def test_parent_manifest_rejects_invalid_output_hashes(
    tmp_path,
    bad_hash,
):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    payload["outputs"]["coverage_audit.csv"] = bad_hash
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="hash"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        pytest.param("/tmp/coverage_audit.csv", id="absolute"),
        pytest.param("../coverage_audit.csv", id="parent-traversal"),
    ],
)
def test_parent_manifest_rejects_unsafe_output_paths(
    tmp_path,
    unsafe_path,
):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    digest = payload["outputs"].pop("coverage_audit.csv")
    payload["outputs"][unsafe_path] = digest
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="unsafe"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


def test_parent_manifest_rejects_symlink_escape(tmp_path):
    cfg = load_b3_config()
    payload = _valid_parent_manifest(tmp_path, cfg)
    coverage = tmp_path / "coverage_audit.csv"
    coverage.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
    outside.write_text("coverage\n", encoding="utf-8")
    coverage.symlink_to(outside)
    payload["outputs"]["coverage_audit.csv"] = _file_digest(outside)
    _write_parent_manifest(tmp_path, payload)

    with pytest.raises(DataBlocked, match="escape"):
        require_parent_manifest(
            tmp_path,
            "preflight",
            cfg,
            pd.Timestamp("2023-12-31"),
        )


@pytest.mark.parametrize(
    "bad_snapshots",
    [
        pytest.param(
            {"not-a-date": pd.DataFrame()},
            id="invalid-formation-key",
        ),
        pytest.param(
            {pd.Timestamp("2021-01-29"): object()},
            id="non-dataframe-snapshot",
        ),
    ],
)
def test_preflight_turns_invalid_snapshot_contract_into_manifested_block(
    tmp_path,
    bad_snapshots,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not load returns")

    sources = B3Sources(
        snapshots=lambda policy, data_end: bad_snapshots,
        constituents=_two_target_constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    assert (tmp_path / "manifests" / "preflight.json").is_file()


def test_flatten_exposures_is_policy_insertion_order_invariant():
    result = compute_month_exposures(
        _synthetic_snapshot(),
        load_b3_config(),
    )
    first_date = pd.Timestamp("2021-01-29")
    second_date = pd.Timestamp("2021-02-26")
    forward = {
        POLICY_MAIN: {
            first_date: result,
            second_date: result,
        },
        POLICY_LAG: {
            first_date: result,
            second_date: result,
        },
    }
    reversed_order = {
        POLICY_LAG: {
            second_date: result,
            first_date: result,
        },
        POLICY_MAIN: {
            second_date: result,
            first_date: result,
        },
    }

    left = flatten_exposures(forward)
    right = flatten_exposures(reversed_order)

    pd.testing.assert_frame_equal(left, right)
    ordering = list(
        left[["pit_policy", "formation_date", "ticker"]]
        .itertuples(index=False, name=None)
    )
    assert ordering == sorted(ordering)


def test_cli_rejects_invalid_data_end_with_argparse(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["b3_build.py", "--data-end", "not-a-date"],
    )

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 2


def test_preflight_missing_q1000_fails_before_snapshot_loading(tmp_path):
    def snapshots(*args, **kwargs):
        raise AssertionError(
            "known target absence must block before heavy snapshots"
        )

    def constituents():
        return pd.DataFrame(
            {
                "index_code": ["000905.SH"],
                "effective_date": ["2021-01-01"],
                "ticker": ["A"],
            }
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not load returns")

    sources = B3Sources(
        snapshots=snapshots,
        constituents=constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    assert got.audit["detail"].str.contains("000852.SH").any()
    assert (tmp_path / "manifests" / "preflight.json").is_file()


def test_preflight_invalidates_stale_manifest_before_source_failure(
    tmp_path,
):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True)
    stale = manifest_dir / "preflight.json"
    stale.write_text('{"status":"OK"}', encoding="utf-8")

    def constituents():
        return _two_target_constituents()

    def broken_snapshots(*args, **kwargs):
        raise RuntimeError("database transport failed")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not load returns")

    sources = B3Sources(
        snapshots=broken_snapshots,
        constituents=constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )

    with pytest.raises(RuntimeError, match="transport"):
        run_preflight(
            load_b3_config(),
            sources,
            pd.Timestamp("2023-12-31"),
            tmp_path,
        )

    assert not stale.exists()


def test_stage_manifest_writer_rejects_missing_declared_output(tmp_path):
    with pytest.raises(FileNotFoundError):
        _write_stage_manifest(
            tmp_path,
            "preflight",
            load_b3_config(),
            pd.Timestamp("2023-12-31"),
            [tmp_path / "missing.csv"],
            "OK",
            [],
        )
    assert not (tmp_path / "manifests" / "preflight.json").exists()


def _formation_sql_source(overrides):
    def fake_read_sql(db, sql, params=None):
        for marker, frame in overrides:
            if marker in sql:
                result = frame.copy()
                if marker == "trading_calendar" and params:
                    upper_bound = pd.Timestamp(max(params.values()))
                    parsed = pd.to_datetime(
                        result["calendar_date"],
                        errors="coerce",
                        format="mixed",
                    )
                    result = result[
                        parsed.isna() | parsed.le(upper_bound)
                    ]
                if (
                    marker == "stock_daily_price"
                    and params
                    and "dates" in params
                    and "trade_date" in result.columns
                ):
                    requested = set(params["dates"])
                    parsed = pd.to_datetime(
                        result["trade_date"],
                        errors="coerce",
                        format="mixed",
                    )
                    result = result[
                        parsed.isna()
                        | parsed.dt.date.isin(requested)
                    ]
                return result
        raise AssertionError(f"unexpected SQL: {sql}")

    return fake_read_sql


def _authoritative_calendar(end):
    dates = pd.date_range("2013-05-01", end, freq="D")
    return pd.DataFrame(
        {
            "calendar_date": dates,
            "sfe": dates.dayofweek < 5,
        }
    )


def _valid_formation_sql_frames(
    calendar_end="2023-12-31",
    index_end=None,
):
    index_end = calendar_end if index_end is None else index_end
    official = pd.bdate_range("2013-05-01", calendar_end)
    formation_dates = (
        pd.Series(official, index=official)
        .groupby(official.to_period("M"))
        .max()
        .tolist()
    )
    return {
        # 必须先于 "stock_daily_price"：carried-close 的 lateral SQL 同时含
        # 两个标记，按列表顺序首中。
        "JOIN LATERAL": pd.DataFrame(
            columns=["formation_date", "ts_code", "close_date", "close"]
        ),
        "trading_calendar": _authoritative_calendar(calendar_end),
        "index_daily": pd.DataFrame(
            {
                "trade_date": pd.bdate_range(
                    "2013-05-01",
                    index_end,
                )
            }
        ),
        "stock_meta": pd.DataFrame(
            {
                "ticker": ["A"],
                "list_date": ["2010-01-01"],
                "delist_date": [None],
            }
        ),
        "stock_daily_price": pd.DataFrame(
            {
                "ticker": ["A"] * len(formation_dates),
                "trade_date": formation_dates,
                "close": [10.0] * len(formation_dates),
            }
        ),
        "stock_share_capital": pd.DataFrame(
            {
                "ts_code": ["A"],
                "end_date": ["2020-12-31"],
                "known_date": ["2020-12-31"],
                "total_shares": [100.0],
            }
        ),
        "industry_classification": pd.DataFrame(
            {
                "ticker": ["A"],
                "effective_date": ["2020-01-01"],
                "industry": ["电子"],
            }
        ),
        "stock_suspension": pd.DataFrame(columns=["trade_date", "ts_code"]),
    }


def _interval_batch_world(n_tickers=6):
    """Candidates plus the three history tables a batch load reads."""

    formations = [pd.Timestamp("2021-01-29"), pd.Timestamp("2021-02-26")]
    tickers = [f"T{i:02d}.SZ" for i in range(n_tickers)]
    candidates = pd.DataFrame(
        [
            {
                "ts_code": ticker,
                "formation_date": formation,
                "list_date": pd.Timestamp("2015-01-05"),
                "delist_date": pd.NaT,
            }
            for ticker in tickers
            for formation in formations
        ]
    )
    prices = pd.DataFrame(
        [
            {
                "ts_code": ticker,
                "trade_date": day,
                "close": 10.0 + index,
            }
            for index, ticker in enumerate(tickers)
            for day in pd.date_range("2020-12-01", "2021-03-31", freq="B")
        ]
    )
    events = pd.DataFrame(
        [
            {
                "ts_code": ticker,
                "trade_date": pd.Timestamp("2021-01-04"),
                "suspend_type": "今起停牌",
                "suspend_reason": "重大事项",
            }
            for ticker in tickers
        ]
    )
    status = pd.DataFrame(
        [
            {"ts_code": ticker, "trade_date": formation, "is_suspended": True}
            for ticker in tickers
            for formation in formations
        ]
    )
    return candidates, prices, events, status


def _interval_batch_read_sql(prices, events, status, seen_params=None):
    """Honour the ts_code/start/end filters so batching is really exercised."""

    def fake_read_sql(db, sql, params=None):
        if seen_params is not None and params:
            seen_params.append(dict(params))
        if "MIN(trade_date)" in sql:
            return pd.DataFrame({"source_start": [pd.Timestamp("2014-01-02")]})
        table = (
            prices
            if "stock_daily_price" in sql
            else events
            if "stock_suspension" in sql
            else status
        )
        result = table.copy()
        if params and "tickers" in params:
            result = result[result["ts_code"].isin(set(params["tickers"]))]
        if params and "start" in params:
            result = result[
                result["trade_date"] >= pd.Timestamp(params["start"])
            ]
        if params and "end" in params:
            result = result[result["trade_date"] <= pd.Timestamp(params["end"])]
        if params and "dates" in params:
            wanted = {pd.Timestamp(value) for value in params["dates"]}
            result = result[result["trade_date"].isin(wanted)]
        return result.reset_index(drop=True)

    return fake_read_sql


def test_interval_evidence_batching_is_exactly_equivalent(monkeypatch):
    candidates, prices, events, status = _interval_batch_world()
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _interval_batch_read_sql(prices, events, status),
    )
    calendar = _authoritative_calendar(pd.Timestamp("2021-03-31"))

    def build(batch_tickers):
        return b3_build_module.build_interval_evidence_in_batches(
            {"schema": "market"},
            candidates,
            pd.Timestamp("2021-03-31"),
            None,
            calendar,
            batch_tickers=batch_tickers,
        )

    single_shot = build(999)
    fully_batched = build(1)
    paired = build(2)

    assert not single_shot.empty
    pd.testing.assert_frame_equal(single_shot, fully_batched)
    pd.testing.assert_frame_equal(single_shot, paired)


def test_interval_evidence_batching_bounds_each_query(monkeypatch):
    candidates, prices, events, status = _interval_batch_world(n_tickers=6)
    seen: list[dict] = []
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _interval_batch_read_sql(prices, events, status, seen),
    )

    b3_build_module.build_interval_evidence_in_batches(
        {"schema": "market"},
        candidates,
        pd.Timestamp("2021-03-31"),
        None,
        _authoritative_calendar(pd.Timestamp("2021-03-31")),
        batch_tickers=2,
    )

    ticker_batches = [
        params["tickers"] for params in seen if "tickers" in params
    ]
    assert ticker_batches, "no ticker-filtered query was issued"
    assert max(len(batch) for batch in ticker_batches) == 2


def test_interval_history_lower_bound_covers_the_longest_suspension():
    # The longest observed break between consecutive trading days is 2,478
    # days; the bound must sit well before that so it can never decide which
    # price the carry-forward sees.
    assert b3_build_module.INTERVAL_HISTORY_LOOKBACK_YEARS * 365 > 2_478

    start = b3_build_module._interval_history_start(
        [date(2013, 5, 31), date(2020, 6, 30)]
    )
    assert start == date(2003, 5, 31)


def test_interval_history_query_carries_the_lower_bound(monkeypatch):
    candidates, prices, events, status = _interval_batch_world(n_tickers=2)
    seen: list[dict] = []
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _interval_batch_read_sql(prices, events, status, seen),
    )

    b3_build_module._fetch_suspension_interval_history(
        {"schema": "market"},
        candidates,
        pd.Timestamp("2021-03-31"),
        recorder=None,
    )

    bounded = [params for params in seen if "start" in params]
    assert bounded, "history queries must carry a lower bound"
    assert all(params["start"] == date(2011, 1, 29) for params in bounded)


def test_suspension_interval_history_empty_candidates_is_query_free_and_typed(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("empty candidates must not query the database")

    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        forbidden,
    )

    got = b3_build_module._fetch_suspension_interval_history(
        {"schema": "market"},
        pd.DataFrame(columns=["ts_code", "formation_date"]),
        pd.Timestamp("2021-03-31"),
        recorder=None,
    )

    assert tuple(got["prices"].columns) == (
        "ts_code",
        "trade_date",
        "close",
    )
    assert tuple(got["events"].columns) == (
        "ts_code",
        "trade_date",
        "suspend_type",
        "suspend_reason",
    )
    assert tuple(got["status"].columns) == (
        "ts_code",
        "trade_date",
        "is_suspended",
    )
    assert got["prices"].empty
    assert got["events"].empty
    assert got["status"].empty
    assert pd.isna(got["source_start"])


def test_suspension_interval_history_queries_only_candidate_coordinates(
    monkeypatch,
):
    calls = []
    records = []

    class Recorder:
        def record(self, name, sql, frame, date_column=None):
            records.append((name, sql, frame.copy(), date_column))

    def recording_read_sql(db, sql, params=None):
        calls.append((sql, params))
        if "MIN(trade_date) AS source_start" in sql:
            return pd.DataFrame({"source_start": ["2010-01-01"]})
        if "suspend_type" in sql:
            return pd.DataFrame(
                columns=[
                    "ts_code",
                    "trade_date",
                    "suspend_type",
                    "suspend_reason",
                ]
            )
        if "is_suspended" in sql:
            return pd.DataFrame(
                columns=["ts_code", "trade_date", "is_suspended"]
            )
        if "stock_daily_price" in sql:
            return pd.DataFrame(columns=["ts_code", "trade_date", "close"])
        raise AssertionError(f"unexpected interval SQL: {sql}")

    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        recording_read_sql,
    )
    candidates = pd.DataFrame(
        {
            "ts_code": ["B.SZ", "A.SZ", "A.SZ"],
            "formation_date": [
                "2021-02-26",
                "2021-01-29",
                "2021-01-29",
            ],
        }
    )

    got = b3_build_module._fetch_suspension_interval_history(
        {"schema": "market"},
        candidates,
        pd.Timestamp("2021-03-31"),
        Recorder(),
    )

    assert got["source_start"] == pd.Timestamp("2010-01-01")
    assert len(calls) == 4
    price_sql, price_params = calls[0]
    event_sql, event_params = calls[1]
    coverage_sql, coverage_params = calls[2]
    status_sql, status_params = calls[3]
    for sql in (price_sql, event_sql):
        assert "ts_code = ANY(%(tickers)s)" in sql
        assert "trade_date <= %(end)s" in sql
        assert "A.SZ" not in sql and "B.SZ" not in sql
    assert "SELECT ts_code, trade_date, close" in price_sql
    assert "ORDER BY ts_code, trade_date" in price_sql
    assert (
        "SELECT ts_code, trade_date, suspend_type, suspend_reason"
        in event_sql
    )
    assert (
        "ORDER BY ts_code, trade_date, suspend_type, suspend_reason"
        in event_sql
    )
    assert "MIN(trade_date) AS source_start" in coverage_sql
    assert coverage_params is None
    assert "ts_code = ANY(%(tickers)s)" in status_sql
    assert "trade_date = ANY(%(dates)s)" in status_sql
    assert "trade_date <=" not in status_sql
    assert "ORDER BY ts_code, trade_date" in status_sql
    expected_history_params = {
        "tickers": ["A.SZ", "B.SZ"],
        # Ten years before the earliest candidate formation date: far enough
        # back that the bound can never decide which carry-forward price wins.
        "start": pd.Timestamp("2011-01-29").date(),
        "end": pd.Timestamp("2021-03-31").date(),
    }
    assert price_params == expected_history_params
    assert event_params == expected_history_params
    assert status_params == {
        "tickers": ["A.SZ", "B.SZ"],
        "dates": [
            pd.Timestamp("2021-01-29").date(),
            pd.Timestamp("2021-02-26").date(),
        ],
    }
    assert [record[0] for record in records] == [
        "market.stock_daily_price_interval_history",
        "market.stock_suspension_interval_history",
        "market.stock_suspension_source_coverage",
        "market.stock_status_interval_confirmation",
    ]
    assert [record[3] for record in records] == [
        "trade_date",
        "trade_date",
        "source_start",
        "trade_date",
    ]


def test_formation_inputs_builds_interval_evidence_without_future_decision_leakage(
    monkeypatch,
):
    formation = pd.Timestamp("2021-01-29")
    frames = _valid_formation_sql_frames(calendar_end="2021-03-31")
    frames["stock_meta"] = pd.DataFrame(
        {
            "ticker": ["A"],
            "list_date": ["2020-07-30"],
            "delist_date": [formation],
        }
    )
    frames["stock_daily_price"] = pd.DataFrame(
        {"ticker": ["A"], "trade_date": [formation], "close": [None]}
    )
    frames["stock_suspension"] = pd.DataFrame(
        {"trade_date": [formation], "ts_code": ["A"]}
    )
    interval_prices = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "trade_date": ["2021-01-28", "2021-02-01"],
            "close": [9.5, 10.5],
        }
    )
    interval_events = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "trade_date": [formation, "2021-02-01"],
            "suspend_type": ["今起停牌", "复牌"],
            "suspend_reason": ["重大事项", "事项完成"],
        }
    )
    interval_status = pd.DataFrame(
        {
            "ts_code": ["A"],
            "trade_date": [formation],
            "is_suspended": [False],
        }
    )
    ordered = [
        (
            "SELECT MIN(trade_date) AS source_start",
            pd.DataFrame({"source_start": ["2010-01-01"]}),
        ),
        (
            "SELECT ts_code, trade_date, suspend_type, suspend_reason",
            interval_events,
        ),
        (
            "SELECT ts_code, trade_date, is_suspended",
            interval_status,
        ),
        ("SELECT ts_code, trade_date, close", interval_prices),
        *frames.items(),
    ]
    source = _formation_sql_source(ordered)
    calls = []

    def recording_read_sql(db, sql, params=None):
        calls.append((sql, params))
        return source(db, sql, params)

    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        recording_read_sql,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    got = _formation_inputs(
        {"schema": "public"},
        pd.Timestamp("2021-03-31"),
    )

    assert tuple(got["interval_evidence"].columns) == CORE_EVIDENCE_COLUMNS
    row = got["interval_evidence"].iloc[0]
    assert row["accepted"] is True
    assert row["previous_close_date"] == pd.Timestamp("2021-01-28")
    assert row["next_trade_date"] == pd.Timestamp("2021-02-01")
    assert row["next_nonnull_close"] == 10.5
    assert row["exact_stock_status_confirmed"] is False
    assert got["interval_carried_closes"].to_dict("records") == [
        {
            "formation_date": formation,
            "ts_code": "A",
            "close_date": pd.Timestamp("2021-01-28"),
            "close": 9.5,
        }
    ]
    interval_calls = [
        (sql, params)
        for sql, params in calls
        if "interval" not in sql
        and (
            "SELECT ts_code, trade_date, close" in sql
            or "suspend_type" in sql
            or "is_suspended" in sql
        )
    ]
    assert interval_calls
    for sql, params in interval_calls:
        assert params["tickers"] == ["A"]
        if "is_suspended" in sql:
            assert params["dates"] == [formation.date()]
        else:
            assert params["end"] == pd.Timestamp("2021-03-31").date()


def test_formation_inputs_wraps_interval_structure_errors_as_data_blocked(
    monkeypatch,
):
    formation = pd.Timestamp("2021-01-29")
    frames = _valid_formation_sql_frames(calendar_end="2021-03-31")
    frames["stock_meta"] = pd.DataFrame(
        {
            "ticker": ["A"],
            "list_date": ["2020-07-30"],
            "delist_date": [formation],
        }
    )
    frames["stock_daily_price"] = pd.DataFrame(
        {"ticker": ["A"], "trade_date": [formation], "close": [None]}
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_suspension_interval_history",
        lambda *args, **kwargs: {
            "prices": pd.DataFrame(columns=["ts_code", "trade_date"]),
            "events": pd.DataFrame(
                columns=[
                    "ts_code",
                    "trade_date",
                    "suspend_type",
                    "suspend_reason",
                ]
            ),
            "source_start": pd.Timestamp("2010-01-01"),
            "status": pd.DataFrame(
                columns=["ts_code", "trade_date", "is_suspended"]
            ),
        },
    )

    with pytest.raises(
        DataBlocked,
        match="continuous suspension evidence invalid: prices",
    ) as caught:
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2021-03-31"),
        )

    assert isinstance(caught.value.__cause__, SuspensionEvidenceError)


@pytest.mark.parametrize(
    ("data_end", "last_trade", "expected"),
    [
        (
            "2021-03-14",
            "2021-03-12",
            ["2021-01-29", "2021-02-26"],
        ),
        (
            "2021-03-31",
            "2021-03-31",
            ["2021-01-29", "2021-02-26", "2021-03-31"],
        ),
    ],
)
def test_formation_inputs_only_uses_completed_calendar_months(
    monkeypatch,
    data_end,
    last_trade,
    expected,
):
    frames = _valid_formation_sql_frames(
        calendar_end="2021-03-31",
        index_end=last_trade,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    got = _formation_inputs(
        {"schema": "public"},
        pd.Timestamp(data_end),
    )

    assert got["month_ends"][-len(expected) :] == list(
        pd.to_datetime(expected)
    )


def test_formation_inputs_blocks_stale_index_calendar_at_natural_month_end(
    monkeypatch,
):
    frames = _valid_formation_sql_frames(
        calendar_end="2021-03-31",
        index_end="2021-03-14",
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    with pytest.raises(DataBlocked, match="calendar"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2021-03-31"),
        )


def test_formation_inputs_blocks_authoritative_calendar_before_month_end(
    monkeypatch,
):
    frames = _valid_formation_sql_frames(
        calendar_end="2021-03-14",
        index_end="2021-03-12",
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    with pytest.raises(DataBlocked, match="calendar"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2021-03-31"),
        )


@pytest.mark.parametrize(
    "malformation",
    [
        "invalid-date",
        "invalid-sfe",
        "duplicate-date",
        "unsorted",
        "missing-natural-day",
        "month-without-trading-day",
    ],
)
def test_formation_inputs_classifies_malformed_authoritative_calendar(
    monkeypatch,
    malformation,
):
    frames = _valid_formation_sql_frames(
        calendar_end="2021-03-31",
        index_end="2021-03-31",
    )
    calendar = frames["trading_calendar"].copy()
    if malformation == "invalid-date":
        calendar["calendar_date"] = calendar["calendar_date"].astype(object)
        calendar.loc[0, "calendar_date"] = "not-a-date"
    elif malformation == "invalid-sfe":
        calendar["sfe"] = calendar["sfe"].astype(object)
        calendar.loc[0, "sfe"] = "yes"
    elif malformation == "duplicate-date":
        calendar = pd.concat(
            [calendar.iloc[[0]], calendar],
            ignore_index=True,
        )
    elif malformation == "unsorted":
        order = [1, 0, *range(2, len(calendar))]
        calendar = calendar.iloc[order].reset_index(drop=True)
    elif malformation == "missing-natural-day":
        calendar = calendar.drop(index=100).reset_index(drop=True)
    elif malformation == "month-without-trading-day":
        june_2013 = calendar["calendar_date"].dt.to_period("M").eq("2013-06")
        calendar.loc[june_2013, "sfe"] = False
    else:
        raise AssertionError(f"unsupported malformation: {malformation}")
    frames["trading_calendar"] = calendar
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    with pytest.raises(DataBlocked, match="authoritative.*calendar"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2021-03-31"),
        )


@pytest.mark.parametrize("malformation", ["extra-date", "duplicate-date"])
def test_formation_inputs_blocks_noncanonical_index_calendar(
    monkeypatch,
    malformation,
):
    frames = _valid_formation_sql_frames(
        calendar_end="2021-03-31",
        index_end="2021-03-31",
    )
    calendar = frames["index_daily"].copy()
    if malformation == "extra-date":
        calendar = pd.concat(
            [calendar, pd.DataFrame({"trade_date": [pd.Timestamp("2021-01-30")]})],
            ignore_index=True,
        ).sort_values("trade_date", kind="mergesort")
    else:
        calendar = pd.concat(
            [calendar.iloc[[0]], calendar],
            ignore_index=True,
        )
    frames["index_daily"] = calendar.reset_index(drop=True)
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    with pytest.raises(DataBlocked, match="calendar"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2021-03-31"),
        )


def test_formation_inputs_classifies_malformed_calendar_as_data_blocked(
    monkeypatch,
):
    frames = _valid_formation_sql_frames()
    frames["index_daily"]["trade_date"] = frames["index_daily"][
        "trade_date"
    ].astype(object)
    frames["index_daily"].loc[0, "trade_date"] = "not-a-date"
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )

    with pytest.raises(DataBlocked, match="calendar"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2023-12-31"),
        )


def test_formation_inputs_classifies_duplicate_closes_as_data_blocked(
    monkeypatch,
):
    frames = _valid_formation_sql_frames()
    frames["stock_daily_price"] = pd.concat(
        [
            frames["stock_daily_price"],
            frames["stock_daily_price"],
        ],
        ignore_index=True,
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )

    with pytest.raises(DataBlocked, match="close"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2023-12-31"),
        )


def test_formation_inputs_classifies_malformed_share_date_as_data_blocked(
    monkeypatch,
):
    frames = _valid_formation_sql_frames()
    frames["stock_share_capital"].loc[0, "known_date"] = "not-a-date"
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )

    with pytest.raises(DataBlocked, match="share"):
        _formation_inputs(
            {"schema": "public"},
            pd.Timestamp("2023-12-31"),
        )


def test_snapshot_assembly_excludes_bj_and_hk_markets(monkeypatch):
    _patch_minimal_assembly_dependencies(monkeypatch)
    inputs = _minimal_assembly_inputs()
    formation = inputs["month_ends"][0]
    inputs["stock_meta"] = pd.concat(
        [
            inputs["stock_meta"],
            pd.DataFrame(
                {
                    "ticker": ["0700.HK", "830001.BJ"],
                    "list_date": ["2010-01-01", "2010-01-01"],
                    "delist_date": [None, None],
                }
            ),
        ],
        ignore_index=True,
    )
    inputs["closes"]["0700.HK"] = 30.0
    inputs["closes"]["830001.BJ"] = 40.0
    inputs["shares_pool"] = pd.concat(
        [
            inputs["shares_pool"],
            pd.DataFrame(
                {
                    "ts_code": ["0700.HK", "830001.BJ"],
                    "end_date": ["2020-01-01", "2020-01-01"],
                    "known_date": ["2020-01-01", "2020-01-01"],
                    "total_shares": [300.0, 400.0],
                }
            ),
        ],
        ignore_index=True,
    )
    inputs["industry_pool"] = pd.concat(
        [
            inputs["industry_pool"],
            pd.DataFrame(
                {
                    "ticker": ["0700.HK", "830001.BJ"],
                    "effective_date": ["2021-01-01", "2021-01-01"],
                    "industry": ["电子", "电子"],
                }
            ),
        ],
        ignore_index=True,
    )

    snapshots = build_policy_snapshots(**inputs)

    snapshot = snapshots[formation]
    tickers = set(snapshot["ticker"])
    assert "0700.HK" not in tickers
    assert "830001.BJ" not in tickers
    assert {"A", "B"} <= tickers


def test_formation_inputs_excludes_bj_and_hk_markets(monkeypatch):
    frames = _valid_formation_sql_frames()
    frames["stock_meta"] = pd.DataFrame(
        {
            "ticker": ["0700.HK", "830001.BJ", "A"],
            "list_date": ["2010-01-01", "2010-01-01", "2010-01-01"],
            "delist_date": [None, None, None],
        }
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    captured = {}

    def fake_fetch_raw_financial(tickers, start, end, db, recorder=None):
        captured["tickers"] = list(tickers)
        return pd.DataFrame()

    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        fake_fetch_raw_financial,
    )

    got = _formation_inputs(
        {"schema": "public"},
        pd.Timestamp("2021-03-31"),
    )

    assert captured["tickers"] == ["A"]
    assert list(got["meta"]["ticker"]) == ["A"]


def test_snapshot_assembly_carries_forward_suspended_closes(monkeypatch):
    _patch_minimal_assembly_dependencies(monkeypatch)
    inputs = _minimal_assembly_inputs()
    formation = inputs["month_ends"][0]
    inputs["stock_meta"] = pd.concat(
        [
            inputs["stock_meta"],
            pd.DataFrame(
                {
                    "ticker": ["C", "D", "E"],
                    "list_date": ["2010-01-01"] * 3,
                    "delist_date": [None] * 3,
                }
            ),
        ],
        ignore_index=True,
    )
    for name in ("C", "D", "E"):
        inputs["closes"][name] = float("nan")
    inputs["shares_pool"] = pd.concat(
        [
            inputs["shares_pool"],
            pd.DataFrame(
                {
                    "ts_code": ["C", "D", "E"],
                    "end_date": ["2020-01-01"] * 3,
                    "known_date": ["2020-01-01"] * 3,
                    "total_shares": [300.0, 400.0, 500.0],
                }
            ),
        ],
        ignore_index=True,
    )
    inputs["industry_pool"] = pd.concat(
        [
            inputs["industry_pool"],
            pd.DataFrame(
                {
                    "ticker": ["C", "D", "E"],
                    "effective_date": ["2021-01-01"] * 3,
                    "industry": ["电子"] * 3,
                }
            ),
        ],
        ignore_index=True,
    )
    inputs["suspensions"] = pd.DataFrame(
        {
            "trade_date": [formation, formation],
            "ts_code": ["C", "E"],
        }
    )
    inputs["carried_closes"] = pd.DataFrame(
        {
            "formation_date": [formation],
            "ts_code": ["C"],
            "close_date": [formation - pd.Timedelta(days=45)],
            "close": [12.5],
        }
    )

    snapshots = build_policy_snapshots(**inputs)

    snap = snapshots[formation].set_index("ticker")
    # C: suspended with evidence and a carried close → eligible at stale price
    assert snap.loc["C", "size_exclusion_reason"] == ""
    assert bool(snap.loc["C", "close_carried"]) is True
    assert snap.loc["C", "total_market_value"] == pytest.approx(12.5 * 300.0)
    # E: suspension evidence but no carried close → still fail-closed
    assert snap.loc["E", "size_exclusion_reason"] == "DATA_MISSING_CLOSE"
    # D: missing close without evidence → unchanged fail-closed path
    assert snap.loc["D", "size_exclusion_reason"] == "DATA_MISSING_CLOSE"
    assert bool(snap.loc["A", "close_carried"]) is False
    assert snap.loc["C", "close_method"] == "EXACT_SUSPENSION"
    assert snap.loc["A", "close_method"] == ""


def _single_ticker_carry_inputs(monkeypatch):
    _patch_minimal_assembly_dependencies(monkeypatch)
    inputs = _minimal_assembly_inputs()
    formation = inputs["month_ends"][0]
    inputs["closes"].loc[formation, "A"] = np.nan
    return inputs, formation


def _carry_frame(formation, close=9.5):
    return pd.DataFrame(
        {
            "formation_date": [formation],
            "ts_code": ["A"],
            "close_date": [formation - pd.Timedelta(days=1)],
            "close": [close],
        }
    )


def test_snapshot_assembly_applies_interval_only_carry_without_exact_evidence(monkeypatch):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["interval_carried_closes"] = _carry_frame(formation)
    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    assert snap.loc["A", "total_market_value"] == pytest.approx(950.0)
    assert snap.loc["A", "close_method"] == INTERVAL_METHOD
    assert bool(snap.loc["A", "close_carried"]) is True


def test_snapshot_retains_actual_valuation_close_for_original_and_carry(
    monkeypatch,
):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["interval_carried_closes"] = _carry_frame(
        formation,
        close=9.5,
    )

    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")

    assert snap.loc["A", "close"] == 9.5
    assert snap.loc["B", "close"] == 20.0


def test_snapshot_assembly_uses_exact_when_both_carries_have_same_close(monkeypatch):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["suspensions"] = pd.DataFrame({"trade_date": [formation], "ts_code": ["A"]})
    inputs["carried_closes"] = _carry_frame(formation)
    inputs["interval_carried_closes"] = _carry_frame(formation)
    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    assert snap.loc["A", "total_market_value"] == pytest.approx(950.0)
    assert snap.loc["A", "close_method"] == "EXACT_SUSPENSION"
    assert bool(snap.loc["A", "close_carried"]) is True


def test_snapshot_assembly_blocks_conflicting_exact_and_interval_carries(monkeypatch):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["suspensions"] = pd.DataFrame({"trade_date": [formation], "ts_code": ["A"]})
    inputs["carried_closes"] = _carry_frame(formation, close=9.5)
    inputs["interval_carried_closes"] = _carry_frame(formation, close=9.6)
    with pytest.raises(DataBlocked, match="conflicting exact and interval carry closes"):
        build_policy_snapshots(**inputs)


def test_snapshot_blocks_conflicting_carries_without_exact_suspension_evidence(
    monkeypatch,
):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["carried_closes"] = _carry_frame(formation, close=9.5)
    inputs["interval_carried_closes"] = _carry_frame(
        formation,
        close=9.6,
    )

    with pytest.raises(
        DataBlocked,
        match="conflicting exact and interval carry closes",
    ):
        build_policy_snapshots(**inputs)


def test_snapshot_uses_interval_for_equal_exact_carry_without_evidence(
    monkeypatch,
):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["carried_closes"] = _carry_frame(formation)
    inputs["interval_carried_closes"] = _carry_frame(formation)

    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")

    assert snap.loc["A", "total_market_value"] == pytest.approx(950.0)
    assert snap.loc["A", "close_method"] == INTERVAL_METHOD
    assert bool(snap.loc["A", "close_carried"]) is True


def test_snapshot_assembly_does_not_overwrite_original_close(monkeypatch):
    inputs = _minimal_assembly_inputs()
    formation = inputs["month_ends"][0]
    inputs["interval_carried_closes"] = pd.DataFrame(
        {"formation_date": [formation], "ts_code": ["B"], "close_date": [formation - pd.Timedelta(days=1)], "close": [99.0]}
    )
    _patch_minimal_assembly_dependencies(monkeypatch)
    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    assert snap.loc["B", "total_market_value"] == pytest.approx(4000.0)
    assert snap.loc["B", "close_method"] == ""
    assert bool(snap.loc["B", "close_carried"]) is False


@pytest.mark.parametrize("source_name", ["carried_closes", "interval_carried_closes"])
@pytest.mark.parametrize(
    ("case", "bad_value"),
    [
        pytest.param("missing_column", None, id="missing-column"),
        pytest.param("unexpected_column", None, id="unexpected-column"),
        pytest.param("mixed_type_column", None, id="mixed-type-column"),
        pytest.param("duplicate_ticker_column", None, id="duplicate-ticker-column"),
        pytest.param("ticker", " A", id="invalid-ticker"),
        pytest.param("formation_date", "not-a-date", id="invalid-formation"),
        pytest.param("close_date", "not-a-date", id="invalid-close-date"),
        pytest.param("close_date", "formation", id="same-day-close"),
        pytest.param("close_date", "future", id="future-close"),
        pytest.param("close", "9.5", id="string-close"),
        pytest.param("close", True, id="boolean-close"),
        pytest.param("close", np.nan, id="missing-close"),
        pytest.param("close", 0.0, id="zero-close"),
        pytest.param("close", -1.0, id="negative-close"),
        pytest.param("close", np.inf, id="positive-infinite-close"),
        pytest.param("close", -np.inf, id="negative-infinite-close"),
    ],
)
def test_snapshot_assembly_strictly_validates_each_carry_source(monkeypatch, source_name, case, bad_value):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    frame = _carry_frame(formation)
    if case == "missing_column":
        frame = frame.drop(columns="close_date")
    elif case == "unexpected_column":
        frame["unexpected"] = "not allowed"
    elif case == "mixed_type_column":
        frame[1] = "not allowed"
    elif case == "duplicate_ticker_column":
        frame = pd.concat([frame, frame[["ts_code"]]], axis=1)
    elif case == "close_date" and bad_value == "formation":
        frame.loc[0, "close_date"] = formation
    elif case == "close_date" and bad_value == "future":
        frame.loc[0, "close_date"] = formation + pd.Timedelta(days=1)
    else:
        column = "ts_code" if case == "ticker" else case
        frame[column] = frame[column].astype(object)
        frame.loc[0, column] = bad_value
    inputs[source_name] = frame
    if source_name == "carried_closes":
        inputs["suspensions"] = pd.DataFrame({"trade_date": [formation], "ts_code": ["A"]})
    with pytest.raises(DataBlocked):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize(
    ("source_name", "source_label"),
    [
        pytest.param(
            "carried_closes",
            "exact carried closes",
            id="exact",
        ),
        pytest.param(
            "interval_carried_closes",
            "interval carried closes",
            id="interval",
        ),
    ],
)
@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        pytest.param("formation_date", "9999-12-31", id="formation-date"),
        pytest.param("close_date", "1600-01-01", id="close-date"),
    ],
)
def test_snapshot_assembly_wraps_out_of_bounds_carry_dates(
    monkeypatch,
    source_name,
    source_label,
    column,
    bad_value,
):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    frame = _carry_frame(formation)
    frame[column] = pd.Series([bad_value], dtype=object)
    inputs[source_name] = frame

    with pytest.raises(DataBlocked, match=source_label):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize(
    ("source_name", "source_label"),
    [
        pytest.param(
            "carried_closes",
            "exact carried closes",
            id="exact",
        ),
        pytest.param(
            "interval_carried_closes",
            "interval carried closes",
            id="interval",
        ),
    ],
)
def test_snapshot_assembly_wraps_overflowing_carry_close(
    monkeypatch,
    source_name,
    source_label,
):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    frame = _carry_frame(formation)
    frame["close"] = pd.Series([10**10000], dtype=object)
    inputs[source_name] = frame

    with pytest.raises(DataBlocked, match=source_label):
        build_policy_snapshots(**inputs)


@pytest.mark.parametrize("source_name", ["carried_closes", "interval_carried_closes"])
def test_snapshot_assembly_deduplicates_identical_carry_rows(monkeypatch, source_name):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    frame = _carry_frame(formation)
    inputs[source_name] = pd.concat([frame, frame], ignore_index=True)
    if source_name == "carried_closes":
        inputs["suspensions"] = pd.DataFrame({"trade_date": [formation], "ts_code": ["A"]})
    snap = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    expected = "EXACT_SUSPENSION" if source_name == "carried_closes" else INTERVAL_METHOD
    assert snap.loc["A", "close_method"] == expected
    assert bool(snap.loc["A", "close_carried"]) is True


@pytest.mark.parametrize("source_name", ["carried_closes", "interval_carried_closes"])
def test_snapshot_assembly_blocks_conflicting_duplicate_carry_keys(monkeypatch, source_name):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs[source_name] = pd.concat([_carry_frame(formation), _carry_frame(formation, close=9.6)], ignore_index=True)
    if source_name == "carried_closes":
        inputs["suspensions"] = pd.DataFrame({"trade_date": [formation], "ts_code": ["A"]})
    with pytest.raises(DataBlocked, match="conflicting duplicate keys"):
        build_policy_snapshots(**inputs)


def test_snapshot_close_method_is_identical_across_pit_policies(monkeypatch):
    inputs, formation = _single_ticker_carry_inputs(monkeypatch)
    inputs["interval_carried_closes"] = _carry_frame(formation)
    main = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    inputs["policy"] = POLICY_LAG
    lag = build_policy_snapshots(**inputs)[formation].set_index("ticker")
    pd.testing.assert_series_equal(main["close_method"], lag["close_method"])
    pd.testing.assert_series_equal(main["close_carried"], lag["close_carried"])


def test_formation_inputs_loads_suspension_evidence(monkeypatch):
    formation = pd.Timestamp("2021-01-29")
    base_frames = _valid_formation_sql_frames()
    base_frames["stock_suspension"] = pd.DataFrame(
        {
            "trade_date": [formation],
            "ts_code": ["A"],
        }
    )
    ordered = [
        (
            "JOIN LATERAL",
            pd.DataFrame(
                {
                    "formation_date": [formation],
                    "ts_code": ["A"],
                    "close_date": [formation - pd.Timedelta(days=10)],
                    "close": [9.5],
                }
            ),
        ),
        *base_frames.items(),
    ]
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(ordered),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )

    got = _formation_inputs(
        {"schema": "public"},
        pd.Timestamp("2021-03-31"),
    )

    assert list(got["suspensions"]["ts_code"]) == ["A"]
    assert list(got["carried_closes"]["ts_code"]) == ["A"]
    assert got["carried_closes"]["close"].iloc[0] == 9.5


def test_preflight_reports_suspended_carry_forward_distribution(tmp_path):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    snapshot.loc[snapshot.index[:2], "close_method"] = "EXACT_SUSPENSION"
    snapshot.loc[snapshot.index[2:5], "close_method"] = INTERVAL_METHOD
    snapshot["close_carried"] = snapshot["close_method"].isin(
        {"EXACT_SUSPENSION", INTERVAL_METHOD}
    )
    accepted = _valid_preflight_interval_evidence().loc[
        lambda frame: frame["accepted"]
    ]
    evidence = pd.concat(
        [
            accepted.assign(
                ts_code=snapshot.loc[index, "ticker"],
                previous_close=snapshot.loc[index, "close"],
            )
            for index in snapshot.index[2:5]
        ],
        ignore_index=True,
    )
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
        interval_evidence=lambda data_end: evidence,
    )

    got = run_preflight(cfg, sources, pd.Timestamp("2023-12-31"), tmp_path)

    assert got.final_status == "OK"
    audit = pd.read_csv(tmp_path / "coverage_audit.csv")
    rows = audit[audit["check"] == "close_carry_forward"]
    assert set(rows["side"]) == {
        "EXACT_SUSPENSION_CARRY_FORWARD",
        "INTERVAL_SUSPENSION_CARRY_FORWARD",
    }
    assert set(rows["status"]) == {"REPORT_ONLY"}
    assert set(rows["detail"]) == {"suspended names valued at last traded close"}
    counts = rows.groupby(["pit_policy", "side"])["eligible_count"].sum()
    for policy in (POLICY_MAIN, POLICY_LAG):
        assert int(counts.loc[policy, "EXACT_SUSPENSION_CARRY_FORWARD"]) == 2
        assert int(counts.loc[policy, "INTERVAL_SUSPENSION_CARRY_FORWARD"]) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("missing_close", id="missing-close-column"),
        pytest.param("missing_method", id="missing-method-column"),
        pytest.param("missing_bool", id="missing-bool-column"),
        pytest.param("duplicate_ticker", id="duplicate-ticker-column"),
        pytest.param("duplicate_close", id="duplicate-close-column"),
        pytest.param("duplicate_method", id="duplicate-method-column"),
        pytest.param("duplicate_bool", id="duplicate-bool-column"),
        pytest.param("non_numeric_close", id="non-numeric-close"),
        pytest.param("carried_missing_close", id="carried-missing-close"),
        pytest.param("carried_zero_close", id="carried-zero-close"),
        pytest.param("unknown_method", id="unknown-method"),
        pytest.param("method_bool_mismatch", id="method-bool-mismatch"),
        pytest.param("non_boolean", id="non-boolean-carried"),
    ],
)
def test_preflight_blocks_invalid_snapshot_carry_contract(tmp_path, mutation):
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    if mutation.startswith("missing_") and mutation != "carried_missing_close":
        column = {
            "missing_close": "close",
            "missing_method": "close_method",
            "missing_bool": "close_carried",
        }[mutation]
        snapshot = snapshot.drop(columns=column)
    elif mutation.startswith("duplicate_"):
        column = {
            "duplicate_ticker": "ticker",
            "duplicate_close": "close",
            "duplicate_method": "close_method",
            "duplicate_bool": "close_carried",
        }[mutation]
        snapshot = pd.concat(
            [snapshot, snapshot[[column]]],
            axis=1,
        )
    elif mutation == "non_numeric_close":
        snapshot["close"] = snapshot["close"].astype(object)
        snapshot.loc[0, "close"] = "10.0"
    elif mutation in {"carried_missing_close", "carried_zero_close"}:
        snapshot.loc[0, "close_method"] = EXACT_CARRY_METHOD
        snapshot.loc[0, "close_carried"] = True
        snapshot.loc[0, "close"] = (
            np.nan if mutation == "carried_missing_close" else 0.0
        )
    elif mutation == "unknown_method":
        snapshot.loc[0, "close_method"] = "UNKNOWN"
        snapshot.loc[0, "close_carried"] = True
    elif mutation == "method_bool_mismatch":
        snapshot.loc[0, "close_method"] = "EXACT_SUSPENSION"
    else:
        snapshot["close_carried"] = snapshot["close_carried"].astype(object)
        snapshot.loc[0, "close_carried"] = "False"
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(_synthetic_snapshot()),
    )

    got = run_preflight(cfg, sources, pd.Timestamp("2023-12-31"), tmp_path)

    assert got.final_status == "DATA_BLOCKED"
    assert set(got.audit["check"]) == {"snapshot_source"}


def test_preflight_blocks_cross_policy_carry_method_mismatch(tmp_path):
    cfg = _single_month_preflight_config()
    main = _synthetic_snapshot()
    lag = main.copy()
    lag.loc[0, "close_method"] = INTERVAL_METHOD
    lag.loc[0, "close_carried"] = True
    formation = pd.Timestamp("2021-01-29")

    def snapshots(policy, _data_end):
        frame = main if policy == POLICY_MAIN else lag
        return {formation: frame.copy()}

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not access returns or carry inputs")

    sources = B3Sources(
        snapshots=snapshots,
        constituents=lambda: _constituents_for_snapshot(main),
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )

    got = run_preflight(cfg, sources, pd.Timestamp("2023-12-31"), tmp_path)

    assert got.final_status == "DATA_BLOCKED"
    assert "carry" in " ".join(got.audit["detail"].fillna("")).lower()


def test_formation_inputs_records_database_evidence(monkeypatch):
    from backtest.b3_eval import TRADING_CALENDAR_QUERY_TEMPLATE_HASH
    from signals.style_basket.b3_build import DatabaseEvidenceRecorder

    frames = _valid_formation_sql_frames()
    monkeypatch.setattr(
        "signals.style_basket.b3_build._read_sql",
        _formation_sql_source(list(frames.items())),
    )
    monkeypatch.setattr(
        "signals.style_basket.b3_build._fetch_raw_financial",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    recorder = DatabaseEvidenceRecorder()

    _formation_inputs(
        {"schema": "public"},
        pd.Timestamp("2021-03-31"),
        recorder=recorder,
    )

    payload = recorder.payload()
    assert payload is not None
    names = payload["consumed_sources"]
    assert names == sorted(names)
    assert "public.trading_calendar" in names
    assert "public.stock_daily_price" in names
    assert "public.stock_suspension" in names
    calendar = payload["sources"]["public.trading_calendar"]
    assert (
        calendar["query_template_hash"]
        == TRADING_CALENDAR_QUERY_TEMPLATE_HASH
    )
    assert calendar["row_count"] > 0
    assert calendar["min_date"] is not None
    meta_entry = payload["sources"]["public.stock_meta"]
    assert meta_entry["min_date"] is None
    assert meta_entry["max_date"] is None


def test_preflight_manifest_database_evidence_contract_roundtrip(tmp_path):
    import json as json_module
    from dataclasses import replace

    from backtest.b3_eval import (
        TRADING_CALENDAR_QUERY_TEMPLATE,
        database_source_evidence_blocker,
        verify_preflight_manifest,
    )
    from signals.style_basket.b3_build import DatabaseEvidenceRecorder
    from signals.style_basket.b3_config import config_hash

    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    recorder = DatabaseEvidenceRecorder()
    recorder.record(
        "public.trading_calendar",
        TRADING_CALENDAR_QUERY_TEMPLATE,
        pd.DataFrame(
            {
                "calendar_date": [pd.Timestamp("2021-01-29")],
                "sfe": [True],
            }
        ),
        "calendar_date",
    )
    sources = replace(
        _preflight_sources(
            snapshot,
            _constituents_for_snapshot(snapshot),
        ),
        database_evidence=recorder.payload,
    )

    got = run_preflight(
        cfg,
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "OK"
    manifest = json_module.loads(
        (tmp_path / "manifests" / "preflight.json").read_text()
    )
    assert "database_source_evidence" in manifest
    contract = verify_preflight_manifest(tmp_path, config_hash(cfg))
    assert contract.database_source_evidence is not None
    assert database_source_evidence_blocker(contract) is None


def test_preflight_manifest_omits_database_evidence_when_unavailable(
    tmp_path,
):
    assert (
        B3Sources.__dataclass_fields__["database_evidence"].default is None
    )
    cfg = _single_month_preflight_config()
    snapshot = _synthetic_snapshot()
    sources = _preflight_sources(
        snapshot,
        _constituents_for_snapshot(snapshot),
    )

    got = run_preflight(
        cfg,
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "OK"
    import json as json_module

    manifest = json_module.loads(
        (tmp_path / "manifests" / "preflight.json").read_text()
    )
    assert "database_source_evidence" not in manifest


def test_preflight_classifies_invalid_constituent_dates_and_writes_manifest(
    tmp_path,
):
    constituents = _two_target_constituents()
    constituents.loc[0, "effective_date"] = "not-a-date"

    def snapshots(*args, **kwargs):
        raise AssertionError("invalid constituents must fail first")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not load returns")

    sources = B3Sources(
        snapshots=snapshots,
        constituents=lambda: constituents,
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )

    got = run_preflight(
        load_b3_config(),
        sources,
        pd.Timestamp("2023-12-31"),
        tmp_path,
    )

    assert got.final_status == "DATA_BLOCKED"
    assert (tmp_path / "manifests" / "preflight.json").is_file()
