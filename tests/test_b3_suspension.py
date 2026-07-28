import pandas as pd
import pytest

from signals.style_basket.b3_suspension import (
    CANDIDATE_COLUMNS,
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
