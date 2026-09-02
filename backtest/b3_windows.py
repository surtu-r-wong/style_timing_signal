"""Frozen structural and model calendars shared by B3 structure and eval."""

from __future__ import annotations

import pandas as pd


STRUCTURAL_DISCOVERY_START = pd.Timestamp("2014-10-01")
STRUCTURAL_DISCOVERY_END = pd.Timestamp("2020-12-31")

MODEL_DISCOVERY_START = pd.Timestamp("2015-01-01")
MODEL_DISCOVERY_END = pd.Timestamp("2020-12-31")

MODEL_PERIOD_WINDOWS = (
    (
        "2015-2017",
        pd.Timestamp("2015-01-01"),
        pd.Timestamp("2017-12-31"),
        True,
    ),
    (
        "2018-2020",
        pd.Timestamp("2018-01-01"),
        pd.Timestamp("2020-12-31"),
        True,
    ),
    (
        "2021-2023",
        pd.Timestamp("2021-01-01"),
        pd.Timestamp("2023-12-31"),
        True,
    ),
    (
        "2024-2026-report-only",
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2026-12-31"),
        False,
    ),
)

MODEL_STATE_COVERAGE_WINDOWS = (
    *MODEL_PERIOD_WINDOWS[:3],
    ("2015-2020", MODEL_DISCOVERY_START, MODEL_DISCOVERY_END, False),
)
