from __future__ import annotations

import importlib.util
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
