from __future__ import annotations

import importlib.util
import warnings
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "data_fixes"
    / "2026-07-25-share-capital-par"
    / "build_b3_impact_audit.py"
)
SPEC = importlib.util.spec_from_file_location(
    "b3_impact_audit",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def _tail_frame(count: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": [f"{index:06d}.SZ" for index in range(count)],
            "list_date": ["2010-01-01"] * count,
            "csmar_latest_a003101000": [1.0] * count,
            "anchor_2025_shares": [1.0] * count,
            "implied_par": [1.0] * count,
            "note": [""] * count,
        }
    )


def _coverage_frame() -> pd.DataFrame:
    formations = pd.date_range("2013-05-31", periods=128, freq="ME")
    required = [False] * 8 + [True] * 120
    shares = [42] * 8 + [46] * 45 + [45] * 75
    closes = [2] * 4 + [1] * 4 + [2] * 70 + [1] * 50
    rows = []
    for policy in (
        "legal_deadline",
        "legal_deadline_plus_one_month_end",
    ):
        for formation, is_required, share_count, close_count in zip(
            formations,
            required,
            shares,
            closes,
        ):
            for reason, count in (
                ("DATA_MISSING_SHARES", share_count),
                ("DATA_MISSING_CLOSE", close_count),
            ):
                rows.append(
                    {
                        "pit_policy": policy,
                        "formation_date": formation,
                        "required_formation": is_required,
                        "check": "size_exclusion",
                        "side": reason,
                        "eligible_count": count,
                    }
                )
    return pd.DataFrame(rows)


def test_validate_anchors_requires_57_unique_tail_tickers():
    with pytest.raises(AUDIT.AuditContractError, match="57 unique"):
        AUDIT.validate_anchors(_tail_frame(56), _coverage_frame())


def test_validate_anchors_requires_policy_monthly_parity():
    coverage = _coverage_frame()
    mask = (
        coverage["pit_policy"].eq(
            "legal_deadline_plus_one_month_end"
        )
        & coverage["side"].eq("DATA_MISSING_CLOSE")
    )
    coverage.loc[coverage.index[mask][0], "eligible_count"] += 1

    with pytest.raises(AUDIT.AuditContractError, match="PIT policy"):
        AUDIT.validate_anchors(_tail_frame(57), coverage)


def test_validate_anchors_returns_canonical_128_month_grid():
    anchors = AUDIT.validate_anchors(
        _tail_frame(57),
        _coverage_frame(),
    )

    assert len(anchors.formations) == 128
    assert int(anchors.formations["required_formation"].sum()) == 120
    assert anchors.expected_counts["DATA_MISSING_SHARES"].sum() == 5781
    assert anchors.expected_counts["DATA_MISSING_CLOSE"].sum() == 202


def _classification_inputs() -> dict[str, object]:
    formation = pd.Timestamp("2021-03-31")
    return {
        "formations": pd.DataFrame(
            {
                "formation_date": [formation],
                "required_formation": [True],
            }
        ),
        "meta": pd.DataFrame(
            {
                "ticker": [
                    "NEW.SZ",
                    "CLOSE.SZ",
                    "SHARE.SZ",
                    "SUSP.SZ",
                    "NOLIST.SZ",
                ],
                "list_date": [
                    "2021-01-01",
                    "2010-01-01",
                    "2010-01-01",
                    "2010-01-01",
                    None,
                ],
                "delist_date": [None] * 5,
            }
        ),
        "exact_closes": pd.DataFrame(
            {
                "ticker": ["SHARE.SZ", "NOLIST.SZ"],
                "formation_date": [formation, formation],
                "raw_close": [10.0, 8.0],
                "raw_price_row_present": [True, True],
            }
        ),
        "shares": pd.DataFrame(
            {
                "ts_code": ["SUSP.SZ", "NOLIST.SZ"],
                "end_date": ["2020-12-31", "2020-12-31"],
                "known_date": ["2021-01-15", "2021-01-15"],
                "total_shares": [100.0, 80.0],
            }
        ),
        "suspensions": pd.DataFrame(
            {
                "ticker": ["SUSP.SZ"],
                "formation_date": [formation],
            }
        ),
        "carried_closes": pd.DataFrame(
            {
                "ticker": ["SUSP.SZ"],
                "formation_date": [formation],
                "carry_close_date": ["2021-03-20"],
                "carry_close": [9.5],
            }
        ),
        "tail_tickers": ("SHARE.SZ",),
    }


def test_build_details_uses_b3_reason_precedence():
    share_detail, close_detail, classified = AUDIT.build_impact_details(
        **_classification_inputs()
    )

    assert set(close_detail["ts_code"]) == {"CLOSE.SZ"}
    assert set(share_detail["ts_code"]) == {"SHARE.SZ"}
    reasons = classified.set_index("ts_code")["size_reason"]
    assert reasons.loc["NEW.SZ"] == "LISTED_LT_180D"
    assert reasons.loc["NOLIST.SZ"] == "DATA_MISSING_LIST_DATE"


def test_suspension_carry_eliminates_missing_close():
    _, close_detail, classified = AUDIT.build_impact_details(
        **_classification_inputs()
    )

    row = classified[classified["ts_code"].eq("SUSP.SZ")].iloc[0]
    assert row["close_source"] == "SUSPENDED_CARRY_FORWARD"
    assert row["size_reason"] == ""
    assert "SUSP.SZ" not in set(close_detail["ts_code"])


def test_share_asof_filters_known_date_before_latest_effective_date():
    inputs = _classification_inputs()
    inputs["shares"] = pd.DataFrame(
        {
            "ts_code": ["SHARE.SZ", "SHARE.SZ", "SUSP.SZ"],
            "end_date": [
                "2020-12-31",
                "2021-12-31",
                "2020-12-31",
            ],
            "known_date": [
                "2021-01-15",
                "2021-05-01",
                "2021-01-15",
            ],
            "total_shares": [100.0, 200.0, 90.0],
        }
    )

    share_detail, _, classified = AUDIT.build_impact_details(**inputs)

    assert share_detail.empty
    row = classified[classified["ts_code"].eq("SHARE.SZ")].iloc[0]
    assert row["selected_total_shares"] == 100.0
    assert pd.Timestamp(row["selected_share_effective_date"]) == pd.Timestamp(
        "2020-12-31"
    )


@pytest.mark.parametrize(
    (
        "raw_present",
        "raw_close",
        "suspended",
        "usable_carry",
        "delist",
        "next_close",
        "expected",
    ),
    [
        (True, None, False, False, None, None, "EXACT_ROW_NULL_CLOSE"),
        (
            False,
            None,
            True,
            False,
            None,
            None,
            "SUSPENSION_WITHOUT_USABLE_CARRY",
        ),
        (
            False,
            None,
            False,
            False,
            "2021-04-30",
            None,
            "POSSIBLE_DELIST_BOUNDARY",
        ),
        (
            False,
            None,
            False,
            False,
            None,
            10.0,
            "UNEXPLAINED_EXACT_DATE_GAP",
        ),
    ],
)
def test_close_evidence_bucket_is_observation_only(
    raw_present,
    raw_close,
    suspended,
    usable_carry,
    delist,
    next_close,
    expected,
):
    row = pd.Series(
        {
            "raw_price_row_present": raw_present,
            "raw_close": raw_close,
            "suspension_evidence": suspended,
            "usable_carry": usable_carry,
            "delist_date": delist,
            "next_nonnull_close": next_close,
        }
    )

    assert AUDIT.close_evidence_bucket(row) == expected


def _summary_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    detail = pd.DataFrame(
        {
            "ts_code": ["ACTIVE.SZ", "ACTIVE.SZ", "OLD.SZ"],
            "formation_date": [
                "2023-01-31",
                "2023-12-29",
                "2020-01-31",
            ],
            "required_formation": [True, True, True],
            "list_date": ["2010-01-01"] * 3,
            "delist_date": [None, None, "2021-01-01"],
            "evidence_bucket": [
                "UNEXPLAINED_EXACT_DATE_GAP",
                "EXACT_ROW_NULL_CLOSE",
                "POSSIBLE_DELIST_BOUNDARY",
            ],
            "raw_price_row_present": [False, True, False],
            "raw_close": [None, None, None],
        }
    )
    pool = pd.DataFrame(
        {
            "ts_code": ["ACTIVE.SZ", "OLD.SZ"],
            "in_pool_2023_any": [True, False],
            "in_pool_2023_12": [True, False],
        }
    )
    pool.attrs["data_end"] = pd.Timestamp("2023-12-29")
    return detail, pool


def test_summarize_impacts_prioritizes_active_2023_names():
    detail, pool = _summary_inputs()

    got = AUDIT.summarize_impacts(
        detail,
        pool,
        include_close_buckets=True,
    )

    assert list(got["ts_code"]) == ["ACTIVE.SZ", "OLD.SZ"]
    assert got.loc[0, "priority_rank"] == 1
    assert bool(got.loc[0, "in_pool_2023_12"])
    assert got.loc[0, "affected_months_2023"] == 2
    assert got.loc[1, "listing_status_at_data_end"] == "DELISTED"
    assert got.loc[0, "exact_row_null_close_months"] == 1
    assert got.loc[1, "possible_delist_boundary_months"] == 1


class _FakeConnection:
    def __init__(self):
        self.readonly = None
        self.autocommit = None

    def set_session(self, *, readonly, autocommit):
        self.readonly = readonly
        self.autocommit = autocommit


def _small_anchors() -> object:
    formations = pd.DataFrame(
        {
            "formation_date": pd.to_datetime(["2020-01-31", "2020-02-28"]),
            "required_formation": [False, True],
        }
    )
    expected = pd.DataFrame(
        {
            "DATA_MISSING_SHARES": [1, 0],
            "DATA_MISSING_CLOSE": [0, 1],
            "required_formation": [False, True],
        },
        index=pd.DatetimeIndex(
            ["2020-01-31", "2020-02-28"],
            name="formation_date",
        ),
    )
    return AUDIT.AuditAnchors(formations, expected, ("A.SZ",))


def test_connect_marks_transaction_read_only(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(
        AUDIT.psycopg2,
        "connect",
        lambda **kwargs: fake,
    )

    got = AUDIT.connect_read_only(
        {
            "host": "db",
            "port": 5432,
            "name": "market_monitor",
            "user": "reader",
            "password": "secret",
            "schema": "stock_selector",
        }
    )

    assert got is fake
    assert fake.readonly is True
    assert fake.autocommit is False


def test_reconcile_rejects_one_month_mismatch():
    anchors = _small_anchors()
    shares = pd.DataFrame(
        {
            "ts_code": ["A.SZ"],
            "formation_date": ["2020-01-31"],
            "required_formation": [False],
        }
    )
    closes = pd.DataFrame(
        {
            "ts_code": ["B.SZ"],
            "formation_date": ["2020-02-28"],
            "required_formation": [True],
        }
    )
    AUDIT.reconcile_details(shares, closes, anchors)

    with pytest.raises(AUDIT.AuditContractError, match="monthly"):
        AUDIT.reconcile_details(shares.iloc[0:0], closes, anchors)


def test_publish_outputs_leaves_no_formal_files_on_invalid_artifact(tmp_path):
    artifacts = {
        "shares_tail_impact_by_ticker.csv": pd.DataFrame(
            {"ts_code": ["A.SZ"], "priority_rank": [1]}
        ),
        "shares_tail_impact_detail.csv": pd.DataFrame(
            {"ts_code": ["A.SZ"]}
        ),
        "close_gap_impact_by_ticker.csv": pd.DataFrame(
            {"ts_code": ["B.SZ"], "priority_rank": [1]}
        ),
        "close_gap_impact_detail.csv": pd.DataFrame(
            {
                "ts_code": ["B.SZ"],
                "reason_code": ["DATA_MISSING_CLOSE"],
                "evidence_bucket": ["UNEXPLAINED_EXACT_DATE_GAP"],
            }
        ),
    }

    with pytest.raises(AUDIT.AuditContractError, match="reason_code"):
        AUDIT.publish_outputs(
            tmp_path,
            artifacts,
            {"schema": "stock_selector"},
        )

    assert not list(tmp_path.glob("*impact*.csv"))
    assert not (tmp_path / "impact_audit_manifest.json").exists()


def test_fetch_sources_rejects_invalid_schema_before_sql():
    with pytest.raises(AUDIT.AuditContractError, match="schema"):
        AUDIT.fetch_audit_sources(
            object(),
            "stock_selector;DROP",
            pd.DataFrame(
                {
                    "formation_date": [pd.Timestamp("2023-12-29")],
                    "required_formation": [True],
                }
            ),
            ("A.SZ",),
        )


def test_main_rejects_hash_mismatch_before_database_connect(
    tmp_path,
    monkeypatch,
):
    tail_path = tmp_path / "tail.csv"
    coverage_path = tmp_path / "coverage.csv"
    _tail_frame(57).to_csv(tail_path, index=False)
    _coverage_frame().to_csv(coverage_path, index=False)
    monkeypatch.setattr(
        AUDIT,
        "connect_read_only",
        lambda *_: pytest.fail("database connection must not be attempted"),
    )

    with pytest.raises(AUDIT.AuditContractError, match="tail SHA-256"):
        AUDIT.main(
            [
                "--tail",
                str(tail_path),
                "--coverage-audit",
                str(coverage_path),
                "--expected-tail-sha256",
                "0" * 64,
            ]
        )


def test_fetch_sources_loads_full_positive_share_history(monkeypatch):
    calls = []

    def fake_read_sql(conn, sql, params=None):
        calls.append((sql, params))
        return pd.DataFrame()

    monkeypatch.setattr(AUDIT, "_read_sql", fake_read_sql)
    AUDIT.fetch_audit_sources(
        object(),
        "stock_selector",
        pd.DataFrame(
            {
                "formation_date": [pd.Timestamp("2023-12-29")],
                "required_formation": [True],
            }
        ),
        ("TAIL.SZ",),
    )

    share_sql, share_params = next(
        (sql, params)
        for sql, params in calls
        if "stock_share_capital" in sql
    )
    assert "ts_code = ANY" not in share_sql
    assert share_params is None


def test_manifest_helpers_render_schema_and_counts():
    assert AUDIT.source_table_names("stock_selector") == [
        "stock_selector.stock_meta",
        "stock_selector.stock_daily_price",
        "stock_selector.stock_share_capital",
        "stock_selector.stock_suspension",
    ]
    assert AUDIT.format_count_line(
        "DATA_MISSING_CLOSE",
        {"all": 202, "required": 190, "tickers": 12},
    ) == "DATA_MISSING_CLOSE: 202 all / 190 required / 12 tickers"


def test_reconcile_allows_tail_ticker_with_zero_size_impact():
    anchors = _small_anchors()._replace(
        tail_tickers=("A.SZ", "ZERO.SZ")
    )
    shares = pd.DataFrame(
        {
            "ts_code": ["A.SZ"],
            "formation_date": ["2020-01-31"],
            "required_formation": [False],
        }
    )
    closes = pd.DataFrame(
        {
            "ts_code": ["B.SZ"],
            "formation_date": ["2020-02-28"],
            "required_formation": [True],
        }
    )

    AUDIT.reconcile_details(shares, closes, anchors)


def test_complete_share_summary_keeps_zero_impact_tail_ticker():
    tail = _tail_frame(2)
    impacted = tail.loc[0, "ts_code"]
    zero = tail.loc[1, "ts_code"]
    impact_summary = pd.DataFrame(
        {
            "ts_code": [impacted],
            "affected_months_all": [10],
            "affected_months_required": [8],
            "affected_months_2023": [2],
            "first_affected_formation": [pd.Timestamp("2020-01-31")],
            "last_affected_formation": [pd.Timestamp("2023-12-29")],
        }
    )
    classified = pd.DataFrame(
        {
            "ts_code": [impacted, zero],
            "list_date": ["2010-01-01", "2023-08-07"],
            "delist_date": [None, None],
        }
    )
    pool = pd.DataFrame(
        {
            "ts_code": [impacted, zero],
            "in_pool_2023_any": [True, False],
            "in_pool_2023_12": [True, False],
        }
    )
    pool.attrs["data_end"] = pd.Timestamp("2023-12-29")

    got = AUDIT.complete_share_summary(
        impact_summary,
        tail,
        classified,
        pool,
    )

    assert len(got) == 2
    zero_row = got.set_index("ts_code").loc[zero]
    assert zero_row["affected_months_all"] == 0
    assert not bool(zero_row["in_pool_2023_12"])
    assert zero_row["priority_rank"] == 2


class _CursorRows:
    description = [("ticker",), ("value",)]

    def __init__(self):
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchall(self):
        return [("A.SZ", 1.0)]


class _ConnectionRows:
    def __init__(self):
        self.cursor_object = _CursorRows()

    def cursor(self):
        return self.cursor_object


def test_read_sql_uses_dbapi_cursor_without_pandas_warning():
    conn = _ConnectionRows()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        got = AUDIT._read_sql(conn, "SELECT ticker, value", {"x": 1})

    assert got.to_dict("records") == [{"ticker": "A.SZ", "value": 1.0}]
    assert conn.cursor_object.executed == (
        "SELECT ticker, value",
        {"x": 1},
    )
