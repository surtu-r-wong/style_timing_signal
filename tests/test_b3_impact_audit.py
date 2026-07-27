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
