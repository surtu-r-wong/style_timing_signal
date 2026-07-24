from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_build import (
    B3Sources,
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
    CoverageBlocked,
    DataBlocked,
    ExposureResult,
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


def _preflight_sources(snapshot, constituents, *, snapshot_error=None):
    def snapshots(*args, **kwargs):
        if snapshot_error is not None:
            raise snapshot_error
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
    )


def _single_month_preflight_config():
    cfg = deepcopy(load_b3_config())
    cfg["windows"]["discovery"] = ["2021-01-01", "2021-01-31"]
    cfg["windows"]["confirmation"] = ["2021-01-01", "2021-01-31"]
    return cfg


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
        pd.Timestamp(date): pd.DataFrame({"ticker": ["A", "B"]})
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
            "style_score": [0.0, 0.0],
            "size_exclusion_reason": ["", "LISTED_LT_180D"],
            "model_exclusion_reason": ["", "MISSING_STYLE_SCORE"],
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
    coverage.write_text("coverage\n", encoding="utf-8")
    diagnostics.write_text("diagnostics\n", encoding="utf-8")
    return {
        "stage": "preflight",
        "config_hash": config_hash(cfg),
        "data_end": "2023-12-31",
        "status": "OK",
        "blockers": [],
        "outputs": {
            "coverage_audit.csv": _file_digest(coverage),
            "exposure_diagnostics.csv": _file_digest(diagnostics),
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
    formation = pd.Timestamp("2021-01-29")
    index_end = calendar_end if index_end is None else index_end
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
                "ticker": ["A"],
                "trade_date": [formation],
                "close": [10.0],
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

    def fake_fetch_raw_financial(tickers, start, end, db):
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
    snapshot["close_carried"] = False
    snapshot.loc[snapshot.index[:2], "close_carried"] = True
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
    audit = pd.read_csv(tmp_path / "coverage_audit.csv")
    rows = audit[audit["check"] == "close_carry_forward"]
    assert not rows.empty
    assert set(rows["side"]) == {"SUSPENDED_CARRY_FORWARD"}
    assert set(rows["status"]) == {"REPORT_ONLY"}
    assert rows["eligible_count"].astype(int).tolist() == [2] * len(rows)


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
