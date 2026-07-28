import pandas as pd
import pytest

from signals.style_basket.b3_suspension import (
    CANDIDATE_COLUMNS,
    CORE_EVIDENCE_COLUMNS,
    INTERVAL_METHOD,
    build_continuous_suspension_evidence,
    empty_interval_evidence,
    SuspensionEvidenceError,
    build_missing_close_candidates,
)


FORMATION = pd.Timestamp("2021-01-29")


def _frames(*, formations=None, meta=None, closes=None, suspensions=None, carries=None):
    return {
        "formations": formations if formations is not None else pd.DataFrame({"formation_date": [FORMATION]}),
        "stock_meta": pd.DataFrame(
            meta if meta is not None else [{"ts_code": "A.SZ", "list_date": "2020-01-01", "delist_date": None}]
        ),
        "exact_closes": pd.DataFrame(
            closes if closes is not None else [{"ts_code": "A.SZ", "formation_date": FORMATION, "close": None}]
        ),
        "exact_suspensions": pd.DataFrame(
            suspensions if suspensions is not None else [], columns=["ts_code", "formation_date"]
        ),
        "exact_carries": pd.DataFrame(
            carries if carries is not None else [], columns=["ts_code", "formation_date", "close_date", "close"]
        ),
    }


def _build(**kwargs):
    return build_missing_close_candidates(**_frames(**kwargs))


def test_selects_mature_active_sz_ticker_with_missing_exact_close():
    result = _build()
    assert result["ts_code"].tolist() == ["A.SZ"]
    assert result["formation_date"].tolist() == [FORMATION]
    assert result["list_date"].tolist() == [pd.Timestamp("2020-01-01")]
    assert pd.isna(result.loc[0, "delist_date"])


def test_excludes_ticker_listed_less_than_180_calendar_days():
    result = _build(meta=[{"ts_code": "A.SZ", "list_date": "2020-09-01", "delist_date": None}])
    assert result.empty


def test_excludes_usable_exact_carry_with_exact_suspension():
    result = _build(
        suspensions=[{"ts_code": "A.SZ", "formation_date": FORMATION}],
        carries=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": 10.0}],
    )
    assert result.empty


def test_excludes_out_of_scope_delisted_and_unknown_listing_names():
    result = _build(
        meta=[
            {"ts_code": "B.BJ", "list_date": "2020-01-01", "delist_date": None},
            {"ts_code": "H.HK", "list_date": "2020-01-01", "delist_date": None},
            {"ts_code": "D.SZ", "list_date": "2020-01-01", "delist_date": "2021-01-28"},
            {"ts_code": "U.SZ", "list_date": None, "delist_date": None},
        ],
        closes=[
            {"ts_code": code, "formation_date": FORMATION, "close": None}
            for code in ["B.BJ", "H.HK", "D.SZ", "U.SZ"]
        ],
    )
    assert result.empty


def test_excludes_raw_non_null_exact_close():
    assert _build(closes=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close": "10"}]).empty


def test_carry_without_exact_suspension_does_not_exclude_candidate():
    result = _build(carries=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": 10.0}])
    assert result["ts_code"].tolist() == ["A.SZ"]


def test_null_carry_with_exact_suspension_does_not_exclude_candidate():
    result = _build(
        suspensions=[{"ts_code": "A.SZ", "formation_date": FORMATION}],
        carries=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": None}],
    )
    assert result["ts_code"].tolist() == ["A.SZ"]


def test_identical_rows_do_not_amplify_candidates():
    result = _build(
        meta=[{"ts_code": "A.SZ", "list_date": "2020-01-01", "delist_date": None}] * 2,
        closes=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close": None}] * 2,
    )
    assert len(result) == 1


def test_conflicting_exact_close_keys_raise_source_named_error():
    with pytest.raises(SuspensionEvidenceError, match="exact closes"):
        _build(closes=[
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close": 10},
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close": 11},
        ])


@pytest.mark.parametrize("frames", [
    {"formations": pd.DataFrame({"formation_date": ["invalid"]})},
    {"stock_meta": pd.DataFrame({"ts_code": [" "], "list_date": ["2020-01-01"], "delist_date": [None]})},
])
def test_invalid_required_dates_and_blank_ticker_keys_raise_stable_structural_errors(frames):
    supplied = _frames()
    supplied.update(frames)
    with pytest.raises(SuspensionEvidenceError):
        build_missing_close_candidates(**supplied)


def test_row_order_does_not_change_output():
    meta = [
        {"ts_code": "B.SZ", "list_date": "2020-01-01", "delist_date": None},
        {"ts_code": "A.SZ", "list_date": "2020-01-01", "delist_date": None},
    ]
    closes = [{"ts_code": row["ts_code"], "formation_date": FORMATION, "close": None} for row in meta]
    forward = _build(meta=meta, closes=closes)
    reverse = _build(meta=list(reversed(meta)), closes=list(reversed(closes)))
    pd.testing.assert_frame_equal(forward, reverse)
    assert forward["ts_code"].tolist() == ["A.SZ", "B.SZ"]


def test_empty_inputs_return_exact_ordered_schema():
    result = build_missing_close_candidates(
        formations=pd.DataFrame(columns=["formation_date"]),
        stock_meta=pd.DataFrame(columns=["ts_code", "list_date", "delist_date"]),
        exact_closes=pd.DataFrame(columns=["ts_code", "formation_date", "close"]),
        exact_suspensions=pd.DataFrame(columns=["ts_code", "formation_date"]),
        exact_carries=pd.DataFrame(columns=["ts_code", "formation_date", "close_date", "close"]),
    )
    assert tuple(result.columns) == CANDIDATE_COLUMNS
    assert result.empty


def test_conflicting_formation_keys_raise_source_named_error():
    formations = pd.DataFrame([
        {"formation_date": FORMATION, "source": "first"},
        {"formation_date": FORMATION, "source": "second"},
    ])
    with pytest.raises(SuspensionEvidenceError, match="formations"):
        _build(formations=formations)


def test_distinct_non_numeric_exact_close_values_are_conflicting_evidence():
    with pytest.raises(SuspensionEvidenceError, match="exact closes"):
        _build(closes=[
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close": "junk"},
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close": "garbage"},
        ])


def test_distinct_non_numeric_exact_carry_values_are_conflicting_evidence():
    with pytest.raises(SuspensionEvidenceError, match="exact carries"):
        _build(carries=[
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": "junk"},
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": "garbage"},
        ])


def test_null_carry_close_date_with_nonusable_close_remains_candidate():
    result = _build(
        suspensions=[{"ts_code": "A.SZ", "formation_date": FORMATION}],
        carries=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": None, "close": None}],
    )
    assert result["ts_code"].tolist() == ["A.SZ"]


def test_non_bj_hk_suffix_is_not_silently_excluded():
    result = _build(
        meta=[{"ts_code": "OTHER.X", "list_date": "2020-01-01", "delist_date": None}],
        closes=[{"ts_code": "OTHER.X", "formation_date": FORMATION, "close": None}],
    )
    assert result["ts_code"].tolist() == ["OTHER.X"]


@pytest.mark.parametrize(
    ("meta", "carries", "label"),
    [
        (
            [{"ts_code": "A.SZ", "list_date": "", "delist_date": None}],
            None,
            "stock meta",
        ),
        (
            None,
            [{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "", "close": None}],
            "exact carries",
        ),
    ],
)
def test_invalid_non_null_nullable_date_tokens_raise_source_named_errors(meta, carries, label):
    with pytest.raises(SuspensionEvidenceError, match=label):
        _build(meta=meta, carries=carries)


def test_raw_distinct_date_equivalent_formation_rows_are_conflicting_evidence():
    formations = pd.DataFrame({"formation_date": ["2021-01-29", FORMATION]})
    with pytest.raises(SuspensionEvidenceError, match="formations"):
        _build(formations=formations)


def test_raw_distinct_date_equivalent_exact_close_rows_are_conflicting_evidence():
    with pytest.raises(SuspensionEvidenceError, match="exact closes"):
        _build(closes=[
            {"ts_code": "A.SZ", "formation_date": "2021-01-29", "close": None},
            {"ts_code": "A.SZ", "formation_date": FORMATION, "close": None},
        ])


@pytest.mark.parametrize("carry_close", [0.0, -1.0, float("inf")])
def test_non_null_exact_carry_values_exclude_candidate(carry_close):
    result = _build(
        suspensions=[{"ts_code": "A.SZ", "formation_date": FORMATION}],
        carries=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close_date": "2021-01-28", "close": carry_close}],
    )
    assert result.empty


def test_singleton_non_numeric_exact_close_is_coerced_to_missing():
    result = _build(closes=[{"ts_code": "A.SZ", "formation_date": FORMATION, "close": "not-a-number"}])
    assert result["ts_code"].tolist() == ["A.SZ"]


@pytest.mark.parametrize(
    ("formations", "label"),
    [
        (pd.DataFrame({"formation_date": [20210129]}), "formations"),
        (pd.DataFrame({"formation_date": [pd.Timestamp("2021-01-29", tz="UTC")]}), "formations"),
        (pd.DataFrame({"formation_date": [FORMATION, pd.Timestamp("2021-01-29", tz="UTC")]}), "formations"),
        (pd.DataFrame({"formation_date": [pd.Timestamp("2021-01-29 09:30")] }), "formations"),
    ],
)
def test_noncanonical_required_dates_raise_source_named_errors(formations, label):
    with pytest.raises(SuspensionEvidenceError, match=label):
        _build(formations=formations)


@pytest.mark.parametrize("ts_code", [" A.SZ", "A.SZ ", "A.HK ", "A.BJ "])
def test_ticker_whitespace_is_a_structural_error(ts_code):
    with pytest.raises(SuspensionEvidenceError, match="stock meta"):
        _build(meta=[{"ts_code": ts_code, "list_date": "2020-01-01", "delist_date": None}])


def test_absent_exact_close_row_is_a_candidate():
    frames = _frames()
    frames["exact_closes"] = pd.DataFrame(columns=["ts_code", "formation_date", "close"])
    result = build_missing_close_candidates(**frames)
    assert result["ts_code"].tolist() == ["A.SZ"]


def test_exact_180_day_listing_age_is_mature():
    result = _build(meta=[{
        "ts_code": "A.SZ", "list_date": FORMATION - pd.Timedelta(days=180), "delist_date": None,
    }])
    assert result["ts_code"].tolist() == ["A.SZ"]


def test_delist_date_equal_to_formation_is_active():
    result = _build(meta=[{
        "ts_code": "A.SZ", "list_date": "2020-01-01", "delist_date": FORMATION,
    }])
    assert result["ts_code"].tolist() == ["A.SZ"]


@pytest.mark.parametrize(
    ("formations", "meta", "label"),
    [
        (pd.DataFrame({"formation_date": ["9999-12-31"]}), None, "formations"),
        (None, [{"ts_code": "A.SZ", "list_date": "2020-01-01", "delist_date": "9999-12-31"}], "stock meta"),
    ],
)
def test_out_of_range_dates_raise_source_named_errors(formations, meta, label):
    with pytest.raises(SuspensionEvidenceError, match=label):
        _build(formations=formations, meta=meta)


INTERVAL_FORMATION = pd.Timestamp("2021-09-30")


def _interval_frames(*, candidates=None, calendar=None, prices=None, events=None, status=None):
    return {
        "candidates": pd.DataFrame(candidates if candidates is not None else [{"ts_code": "A.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2020-01-01", "delist_date": None}]),
        "trading_calendar": pd.DataFrame(calendar if calendar is not None else [
            {"calendar_date": "2021-09-17", "sfe": True}, {"calendar_date": "2021-09-18", "sfe": False},
            {"calendar_date": "2021-09-19", "sfe": False}, {"calendar_date": "2021-09-20", "sfe": False},
            {"calendar_date": "2021-09-21", "sfe": False}, {"calendar_date": "2021-09-22", "sfe": True},
            {"calendar_date": "2021-09-30", "sfe": True}, {"calendar_date": "2021-10-08", "sfe": True},
        ]),
        "prices": pd.DataFrame(prices if prices is not None else [{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}, {"ts_code": "A.SZ", "trade_date": "2021-10-08", "close": 12.0}]),
        "suspension_events": pd.DataFrame(events if events is not None else [{"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "重大事项"}], columns=["ts_code", "trade_date", "suspend_type", "suspend_reason"]),
        "stock_status": None if status is None else pd.DataFrame(status),
    }


def _interval_build(*, source_start="2021-01-01", **kwargs):
    return build_continuous_suspension_evidence(**_interval_frames(**kwargs), suspension_source_start=source_start)


def test_empty_interval_evidence_has_exact_schema():
    result = empty_interval_evidence()
    assert tuple(result.columns) == CORE_EVIDENCE_COLUMNS
    assert result.empty


def test_classifies_explicit_interval_over_holiday_and_reports_future_resume():
    row = _interval_build().iloc[0]
    assert row["accepted"] is True
    assert row["evidence_method"] == INTERVAL_METHOD
    assert row["rejection_reason"] == ""
    assert row["suspension_start"] == pd.Timestamp("2021-09-22")
    assert row["previous_official_trade_date"] == pd.Timestamp("2021-09-17")
    assert row["previous_close_date"] == pd.Timestamp("2021-09-17")
    assert row["previous_close"] == 10.0
    assert row["next_trade_date"] == pd.Timestamp("2021-10-08")
    assert row["next_nonnull_close"] == 12.0
    assert pd.isna(row["exact_stock_status_confirmed"])


@pytest.mark.parametrize(("events", "source_start", "reason"), [
    ([], "2021-01-01", "NO_EXPLICIT_SUSPENSION_START"),
    ([], "2021-09-23", "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE"),
    ([{"ts_code": "A.SZ", "trade_date": "2021-09-21", "suspend_type": "今起停牌", "suspend_reason": "x"}], "2021-01-01", "START_NOT_OFFICIAL_TRADING_DAY"),
])
def test_start_absence_coverage_and_closed_day_rejections(events, source_start, reason):
    assert _interval_build(events=events, source_start=source_start).iloc[0]["rejection_reason"] == reason


def test_rejects_stale_prior_close_and_price_inside_interval():
    stale = _interval_build(prices=[{"ts_code": "A.SZ", "trade_date": "2021-09-16", "close": 10.0}]).iloc[0]
    inside = _interval_build(prices=[{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}, {"ts_code": "A.SZ", "trade_date": "2021-09-30", "close": 11.0}]).iloc[0]
    assert stale["rejection_reason"] == "PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY"
    assert inside["rejection_reason"] == "PRICE_OBSERVED_DURING_INTERVAL"


@pytest.mark.parametrize("close", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_rejects_missing_nonpositive_or_nonfinite_previous_close(close):
    row = _interval_build(prices=[{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": close}]).iloc[0]
    assert row["rejection_reason"] == "INVALID_PREVIOUS_CLOSE"


def test_rechecks_legal_listing_interval_and_detects_overlapping_starts():
    outside = _interval_build(candidates=[{"ts_code": "A.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2021-10-01", "delist_date": None}]).iloc[0]
    assert outside["rejection_reason"] == "OUTSIDE_LEGAL_LISTING_INTERVAL"
    with pytest.raises(SuspensionEvidenceError, match="overlapping"):
        _interval_build(events=[
            {"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "first"},
            {"ts_code": "A.SZ", "trade_date": "2021-09-24", "suspend_type": "今起停牌", "suspend_reason": "second"},
        ])


def test_prior_completed_cycle_is_allowed():
    calendar = _interval_frames()["trading_calendar"].to_dict("records") + [{"calendar_date": "2021-09-15", "sfe": True}]
    row = _interval_build(calendar=calendar, events=[
        {"ts_code": "A.SZ", "trade_date": "2021-09-15", "suspend_type": "今起停牌", "suspend_reason": "old"},
        {"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "new"},
    ]).iloc[0]
    assert row["accepted"] is True
    assert row["suspension_start"] == pd.Timestamp("2021-09-22")


def test_future_facts_cannot_change_decision_fields():
    baseline = _interval_build(prices=[{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}])
    changed = _interval_build(prices=[{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}, {"ts_code": "A.SZ", "trade_date": "2021-10-08", "close": 99.0}], events=[
        {"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "重大事项"},
        {"ts_code": "A.SZ", "trade_date": "2021-10-08", "suspend_type": "复牌", "suspend_reason": "future"},
    ])
    fields = ["accepted", "rejection_reason", "evidence_method", "suspension_start", "previous_close_date", "previous_close"]
    pd.testing.assert_frame_equal(baseline[fields], changed[fields])


@pytest.mark.parametrize(("status", "expected"), [
    ([{"ts_code": "A.SZ", "trade_date": "2021-09-30", "is_suspended": True}], True),
    ([{"ts_code": "A.SZ", "trade_date": "2021-09-30", "is_suspended": False}], False), (None, pd.NA),
])
def test_exact_stock_status_is_report_only(status, expected):
    row = _interval_build(status=status).iloc[0]
    assert row["accepted"] is True
    assert pd.isna(row["exact_stock_status_confirmed"]) if expected is pd.NA else row["exact_stock_status_confirmed"] is expected


def test_order_and_identical_duplicates_do_not_change_output():
    frames = _interval_frames(candidates=[
        {"ts_code": "B.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2020-01-01", "delist_date": None},
        {"ts_code": "A.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2020-01-01", "delist_date": None},
    ], prices=[{"ts_code": "B.SZ", "trade_date": "2021-09-17", "close": 20.0}, {"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}], events=[
        {"ts_code": "B.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "b"},
        {"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "a"},
    ])
    forward = build_continuous_suspension_evidence(**frames, suspension_source_start="2021-01-01")
    duplicate = build_continuous_suspension_evidence(
        candidates=pd.concat([frames["candidates"].iloc[::-1], frames["candidates"].iloc[::-1]]),
        trading_calendar=pd.concat([frames["trading_calendar"].iloc[::-1]] * 2), prices=pd.concat([frames["prices"].iloc[::-1]] * 2),
        suspension_events=pd.concat([frames["suspension_events"].iloc[::-1]] * 2), suspension_source_start="2021-01-01",
    )
    pd.testing.assert_frame_equal(forward, duplicate)
    assert forward["ts_code"].tolist() == ["A.SZ", "B.SZ"]


@pytest.mark.parametrize(("frame_name", "replacement", "label"), [
    ("candidates", pd.DataFrame([{"ts_code": "A.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2020-01-01", "delist_date": None}, {"ts_code": "A.SZ", "formation_date": INTERVAL_FORMATION, "list_date": "2020-02-01", "delist_date": None}]), "candidates"),
    ("prices", pd.DataFrame([{"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 10.0}, {"ts_code": "A.SZ", "trade_date": "2021-09-17", "close": 11.0}]), "prices"),
    ("suspension_events", pd.DataFrame([{"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "one"}, {"ts_code": "A.SZ", "trade_date": "2021-09-22", "suspend_type": "今起停牌", "suspend_reason": "two"}]), "suspension events"),
    ("stock_status", pd.DataFrame([{"ts_code": "A.SZ", "trade_date": "2021-09-30", "is_suspended": True}, {"ts_code": "A.SZ", "trade_date": "2021-09-30", "is_suspended": False}]), "stock status"),
    ("trading_calendar", pd.DataFrame([{"calendar_date": "2021-09-22", "sfe": True}, {"calendar_date": "2021-09-22", "sfe": False}]), "trading calendar"),
])
def test_conflicting_interval_logical_keys_raise_source_named_errors(frame_name, replacement, label):
    frames = _interval_frames()
    frames[frame_name] = replacement
    with pytest.raises(SuspensionEvidenceError, match=label):
        build_continuous_suspension_evidence(**frames, suspension_source_start="2021-01-01")


def test_empty_candidates_return_schema_without_nonempty_history():
    result = build_continuous_suspension_evidence(
        candidates=pd.DataFrame(columns=CANDIDATE_COLUMNS), trading_calendar=pd.DataFrame(columns=["calendar_date", "sfe"]),
        prices=pd.DataFrame(columns=["ts_code", "trade_date", "close"]), suspension_events=pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type", "suspend_reason"]),
        stock_status=pd.DataFrame(columns=["ts_code", "trade_date", "is_suspended"]), suspension_source_start="2021-01-01",
    )
    assert tuple(result.columns) == CORE_EVIDENCE_COLUMNS
    assert result.empty


@pytest.mark.parametrize(("frame_name", "replacement", "source_start", "label"), [
    ("trading_calendar", pd.DataFrame([{"calendar_date": "2021-09-17", "sfe": 1}]), "2021-01-01", "trading calendar"),
    ("stock_status", pd.DataFrame([{"ts_code": "A.SZ", "trade_date": "2021-09-30", "is_suspended": "yes"}]), "2021-01-01", "stock status"),
    (None, None, 20210101, "suspension source start"), (None, None, pd.Timestamp("2021-01-01", tz="UTC"), "suspension source start"),
    (None, None, pd.Timestamp("2021-01-01 09:30"), "suspension source start"),
    ("prices", pd.DataFrame([{"ts_code": " A.SZ", "trade_date": "2021-09-17", "close": 10.0}]), "2021-01-01", "prices"),
    ("suspension_events", pd.DataFrame([{"ts_code": "A.SZ", "trade_date": 20210922, "suspend_type": "今起停牌", "suspend_reason": "x"}]), "2021-01-01", "suspension events"),
])
def test_malformed_interval_inputs_raise_structural_errors(frame_name, replacement, source_start, label):
    frames = _interval_frames()
    if frame_name is not None:
        frames[frame_name] = replacement
    with pytest.raises(SuspensionEvidenceError, match=label):
        build_continuous_suspension_evidence(**frames, suspension_source_start=source_start)
