"""Hard-sort and cross-sectional structure evidence for B3."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from signals.style_basket.b3_build import require_parent_manifest
from signals.style_basket.b3_config import load_b3_config
from signals.style_basket.b3_exposures import DataBlocked


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESEARCH_OUTPUT_DIR = ROOT / "output" / "style_basket" / "b3"
DEFAULT_BACKTEST_OUTPUT_DIR = ROOT / "backtest" / "output" / "b3"
SURFACE_COLUMNS = [
    "pit_policy",
    "formation_date",
    "row_type",
    "grid",
    "cell",
    "diagnostic",
    "member_count",
    "industry_distribution",
    "formation_coverage",
    "holding_return",
    "status",
]
COEFFICIENT_COLUMNS = [
    "pit_policy",
    "row_type",
    "formation_date",
    "window",
    "alpha",
    "beta_s",
    "beta_m",
    "beta_h",
    "n",
    "ordinary_t_beta_h",
    "nw_lag3_t_beta_h",
    "affects_verdict",
]
CELLS_2X3 = [
    f"{size}_{style}"
    for size in ("big", "small")
    for style in ("value", "middle", "growth")
]
CELLS_5X5 = [
    f"S{size}_V{style}"
    for size in range(1, 6)
    for style in range(1, 6)
]
WINDOW_SPECS = [
    (
        "2014-2017",
        pd.Timestamp("2014-01-01"),
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
]


def _require_frame(
    frame: pd.DataFrame,
    required: set[str],
    label: str,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataBlocked(f"{label} must be a nonempty DataFrame")
    if frame.columns.has_duplicates:
        raise DataBlocked(f"{label} contains duplicate columns")
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise DataBlocked(
            f"{label} is missing columns: " + ", ".join(missing)
        )
    return frame.copy()


def _strict_dates(values: pd.Series, label: str) -> pd.Series:
    parsed = []
    try:
        for value in values:
            if isinstance(value, (bool, int, float, np.number)):
                raise TypeError(f"invalid numeric date {value!r}")
            date = pd.Timestamp(value)
            if (
                pd.isna(date)
                or date.tz is not None
                or date != date.normalize()
            ):
                raise ValueError(f"invalid date {value!r}")
            parsed.append(date)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} contains invalid dates") from exc
    return pd.Series(
        parsed,
        index=values.index,
        dtype="datetime64[ns]",
        name=values.name,
    )


def _validate_strings(values: pd.Series, label: str) -> None:
    invalid = ~values.map(
        lambda value: (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
        )
    )
    if invalid.any():
        raise DataBlocked(f"{label} must contain canonical nonempty strings")


def _finite_numeric(values: pd.Series, label: str) -> pd.Series:
    if values.map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).any():
        raise DataBlocked(f"{label} cannot contain booleans")
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.isna().any() or not np.isfinite(numeric).all():
        raise DataBlocked(f"{label} must be finite and nonmissing")
    return numeric.astype(float)


def _percentile_position(
    values: pd.Series,
    tickers: pd.Series,
) -> pd.Series:
    order = pd.DataFrame(
        {
            "row_number": np.arange(len(values)),
            "value": values.to_numpy(),
            "ticker": tickers.to_numpy(),
        },
    ).sort_values(
        ["value", "ticker"],
        ascending=[True, True],
        kind="mergesort",
    )
    percentile = np.empty(len(order), dtype=float)
    percentile[order["row_number"].to_numpy(dtype=int)] = (
        np.arange(len(order)) + 0.5
    ) / len(order)
    return pd.Series(percentile, index=values.index, dtype=float)


def assign_hard_sort_cells(month: pd.DataFrame) -> pd.DataFrame:
    """Assign deterministic independent size/style 2x3 and 5x5 cells."""
    required = {"ticker", "m_perp", "s_perp"}
    out = _require_frame(month, required, "hard sort input")
    _validate_strings(out["ticker"], "hard sort ticker")
    if out["ticker"].duplicated().any():
        raise DataBlocked("hard sort input contains duplicate tickers")
    out["m_perp"] = _finite_numeric(
        out["m_perp"],
        "hard sort m_perp",
    )
    out["s_perp"] = _finite_numeric(
        out["s_perp"],
        "hard sort s_perp",
    )
    p_size = _percentile_position(out["m_perp"], out["ticker"])
    p_style = _percentile_position(out["s_perp"], out["ticker"])
    size_2 = np.where(p_size < 0.5, "big", "small")
    style_3 = np.select(
        [p_style < 0.3, p_style >= 0.7],
        ["value", "growth"],
        default="middle",
    )
    out["cell_2x3"] = np.char.add(
        np.char.add(size_2.astype(str), "_"),
        style_3.astype(str),
    )
    size_5 = np.minimum((p_size * 5).astype(int) + 1, 5)
    style_5 = np.minimum((p_style * 5).astype(int) + 1, 5)
    out["cell_5x5"] = (
        "S"
        + size_5.astype(str)
        + "_V"
        + style_5.astype(str)
    )
    return out


def _validated_fama_inputs(
    exposures: pd.DataFrame,
    forward_returns: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    exposure_required = {
        "formation_date",
        "ticker",
        "s_perp",
        "m_perp",
        "h_perp",
    }
    return_required = {
        "formation_date",
        "ticker",
        "forward_return",
    }
    x = _require_frame(
        exposures,
        exposure_required,
        "Fama-MacBeth exposures",
    )
    r = _require_frame(
        forward_returns,
        return_required,
        "Fama-MacBeth forward returns",
    )
    if "universe_role" in x:
        _validate_strings(
            x["universe_role"],
            "Fama-MacBeth universe_role",
        )
        if not x["universe_role"].isin({"model", "size_only"}).all():
            raise DataBlocked(
                "Fama-MacBeth exposures contain unsupported universe_role"
            )
        x = x[x["universe_role"].eq("model")].copy()
        if x.empty:
            raise DataBlocked("Fama-MacBeth model universe is empty")
    x["formation_date"] = _strict_dates(
        x["formation_date"],
        "Fama-MacBeth exposures.formation_date",
    )
    r["formation_date"] = _strict_dates(
        r["formation_date"],
        "Fama-MacBeth forward returns.formation_date",
    )
    _validate_strings(x["ticker"], "Fama-MacBeth exposure ticker")
    _validate_strings(r["ticker"], "Fama-MacBeth return ticker")
    keys = ["formation_date", "ticker"]
    if x.duplicated(keys).any():
        raise DataBlocked("Fama-MacBeth exposures contain duplicate keys")
    if r.duplicated(keys).any():
        raise DataBlocked("Fama-MacBeth returns contain duplicate keys")
    for column in ("s_perp", "m_perp", "h_perp"):
        x[column] = _finite_numeric(
            x[column],
            f"Fama-MacBeth exposures.{column}",
        )
    r["forward_return"] = _finite_numeric(
        r["forward_return"],
        "Fama-MacBeth forward_return",
    )
    if (r["forward_return"] <= -1.0).any():
        raise DataBlocked(
            "Fama-MacBeth forward returns must exceed -100%"
        )
    x_keys = pd.MultiIndex.from_frame(x[keys])
    r_keys = pd.MultiIndex.from_frame(r[keys])
    if set(x_keys) != set(r_keys):
        raise DataBlocked(
            "Fama-MacBeth exposure and return keys do not match"
        )
    return (
        x.sort_values(keys, kind="mergesort").reset_index(drop=True),
        r.sort_values(keys, kind="mergesort").reset_index(drop=True),
    )


def fama_macbeth_coefficients(
    exposures: pd.DataFrame,
    forward_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Fit one OLS cross-section per formation month."""
    clean_exposures, clean_returns = _validated_fama_inputs(
        exposures,
        forward_returns,
    )
    joint = clean_exposures.merge(
        clean_returns,
        on=["formation_date", "ticker"],
        how="inner",
        validate="one_to_one",
    )
    rows = []
    for date, month in joint.groupby("formation_date", sort=True):
        clean = month[
            ["forward_return", "s_perp", "m_perp", "h_perp"]
        ]
        x = np.column_stack(
            [
                np.ones(len(clean)),
                clean[["s_perp", "m_perp", "h_perp"]].to_numpy(dtype=float),
            ]
        )
        if np.linalg.matrix_rank(x) != 4:
            raise RuntimeError(f"cross-sectional OLS rank failure: {date}")
        beta, _, _, _ = np.linalg.lstsq(
            x,
            clean["forward_return"].to_numpy(dtype=float),
            rcond=None,
        )
        rows.append(
            {
                "formation_date": pd.Timestamp(date),
                "alpha": beta[0],
                "beta_s": beta[1],
                "beta_m": beta[2],
                "beta_h": beta[3],
                "n": len(clean),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values("formation_date", kind="mergesort")
        .reset_index(drop=True)
    )


def _t_values(values: pd.Series, label: str) -> np.ndarray:
    if not isinstance(values, pd.Series):
        raise DataBlocked(f"{label} must be a Series")
    if values.map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).any():
        raise DataBlocked(f"{label} cannot contain booleans")
    numeric = pd.to_numeric(values, errors="coerce")
    invalid = values.notna() & numeric.isna()
    if invalid.any():
        raise DataBlocked(f"{label} must be numeric")
    clean = numeric.dropna().to_numpy(dtype=float)
    if not np.isfinite(clean).all():
        raise DataBlocked(f"{label} must be finite when present")
    return clean


def ordinary_mean_t(values: pd.Series) -> float:
    """Ordinary t-statistic of a time-series mean."""
    clean = _t_values(values, "ordinary t values")
    if len(clean) < 2:
        return float("nan")
    standard_error = clean.std(ddof=1) / np.sqrt(len(clean))
    if standard_error <= 0.0:
        return float("nan")
    return float(clean.mean() / standard_error)


def newey_west_mean_t(values: pd.Series, lag: int = 3) -> float:
    """Bartlett-kernel Newey-West t-statistic of a time-series mean."""
    if isinstance(lag, (bool, np.bool_)) or not isinstance(
        lag,
        (int, np.integer),
    ):
        raise DataBlocked("Newey-West lag must be a nonnegative integer")
    if int(lag) < 0:
        raise DataBlocked("Newey-West lag must be a nonnegative integer")
    lag = int(lag)
    clean = _t_values(values, "Newey-West t values")
    n = len(clean)
    if n <= lag + 1:
        return float("nan")
    demeaned = clean - clean.mean()
    long_run = float(demeaned @ demeaned / n)
    for offset in range(1, lag + 1):
        gamma = float(demeaned[offset:] @ demeaned[:-offset] / n)
        long_run += 2.0 * (1.0 - offset / (lag + 1.0)) * gamma
    variance_of_mean = long_run / n
    if variance_of_mean <= 0.0:
        return float("nan")
    return float(clean.mean() / np.sqrt(variance_of_mean))


def _monthly_formation_grid(
    values: pd.Series,
    label: str,
) -> tuple[pd.Timestamp, ...]:
    dates = pd.DatetimeIndex(sorted(values.unique()))
    if dates.empty:
        raise DataBlocked(f"{label} contains no formation dates")
    periods = dates.to_period("M")
    if periods.has_duplicates:
        raise DataBlocked(
            f"{label} must contain exactly one formation per calendar month"
        )
    expected = pd.period_range(periods[0], periods[-1], freq="M")
    if not periods.equals(expected):
        raise DataBlocked(
            f"{label} formation grid must be continuous by calendar month"
        )
    return tuple(dates)


def _validated_surface_inputs(
    exposures: pd.DataFrame,
    stock_period_returns: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    exposure_required = {
        "pit_policy",
        "formation_date",
        "ticker",
        "universe_role",
        "industry",
        "s_perp",
        "m_perp",
        "h_perp",
    }
    return_required = {
        "pit_policy",
        "formation_date",
        "ticker",
        "forward_return",
    }
    x = _require_frame(
        exposures,
        exposure_required,
        "hard-sort exposures",
    )
    r = _require_frame(
        stock_period_returns,
        return_required,
        "stock period returns",
    )
    x["formation_date"] = _strict_dates(
        x["formation_date"],
        "hard-sort exposures.formation_date",
    )
    r["formation_date"] = _strict_dates(
        r["formation_date"],
        "stock period returns.formation_date",
    )
    for frame, label in (
        (x, "hard-sort exposures"),
        (r, "stock period returns"),
    ):
        _validate_strings(frame["pit_policy"], f"{label}.pit_policy")
        _validate_strings(frame["ticker"], f"{label}.ticker")
    _validate_strings(x["universe_role"], "hard-sort universe_role")
    if not x["universe_role"].isin({"model", "size_only"}).all():
        raise DataBlocked(
            "hard-sort exposures contain unsupported universe_role"
        )
    keys = ["pit_policy", "formation_date", "ticker"]
    if x.duplicated(keys).any():
        raise DataBlocked("hard-sort exposures contain duplicate keys")
    if r.duplicated(keys).any():
        raise DataBlocked("stock period returns contain duplicate keys")

    model = x[x["universe_role"].eq("model")].copy()
    if model.empty:
        raise DataBlocked("hard-sort model universe is empty")
    _validate_strings(model["industry"], "hard-sort model industry")
    for column in ("s_perp", "m_perp", "h_perp"):
        model[column] = _finite_numeric(
            model[column],
            f"hard-sort exposures.{column}",
        )
    r["forward_return"] = _finite_numeric(
        r["forward_return"],
        "stock period returns.forward_return",
    )
    if (r["forward_return"] <= -1.0).any():
        raise DataBlocked("stock period returns must exceed -100%")

    exposure_policies = sorted(x["pit_policy"].unique())
    model_policies = sorted(model["pit_policy"].unique())
    return_policies = sorted(r["pit_policy"].unique())
    if not (
        exposure_policies == model_policies == return_policies
    ):
        raise DataBlocked(
            "hard-sort exposure and return policy sets do not match"
        )
    reference_exposure_dates: tuple[pd.Timestamp, ...] | None = None
    reference_return_dates: tuple[pd.Timestamp, ...] | None = None
    for policy in exposure_policies:
        policy_full = x[x["pit_policy"].eq(policy)]
        policy_model = model[model["pit_policy"].eq(policy)]
        policy_returns = r[r["pit_policy"].eq(policy)]
        full_exposure_dates = _monthly_formation_grid(
            policy_full["formation_date"],
            f"hard-sort full exposures for {policy}",
        )
        exposure_dates = _monthly_formation_grid(
            policy_model["formation_date"],
            f"hard-sort model exposures for {policy}",
        )
        if full_exposure_dates != exposure_dates:
            raise DataBlocked(
                "hard-sort full and model formation grids do not match "
                f"for {policy}"
            )
        return_dates = _monthly_formation_grid(
            policy_returns["formation_date"],
            f"hard-sort returns for {policy}",
        )
        if return_dates != exposure_dates[:-1]:
            raise DataBlocked(
                "stock period return months must be the complete exposure "
                "prefix with exactly one unrealized final formation"
            )
        if reference_exposure_dates is None:
            reference_exposure_dates = exposure_dates
            reference_return_dates = return_dates
        elif (
            exposure_dates != reference_exposure_dates
            or return_dates != reference_return_dates
        ):
            raise DataBlocked(
                "hard-sort formation grids differ across PIT policies"
            )
        for date in return_dates:
            exposure_tickers = set(
                policy_model.loc[
                    policy_model["formation_date"].eq(date),
                    "ticker",
                ]
            )
            return_tickers = set(
                policy_returns.loc[
                    policy_returns["formation_date"].eq(date),
                    "ticker",
                ]
            )
            if exposure_tickers != return_tickers:
                raise DataBlocked(
                    "hard-sort model exposure and return ticker keys "
                    f"do not match for {policy} on {date.date()}"
                )
    return (
        model.sort_values(keys, kind="mergesort").reset_index(drop=True),
        r.sort_values(keys, kind="mergesort").reset_index(drop=True),
    )


def _industry_distribution(members: pd.DataFrame) -> str:
    counts = members["industry"].value_counts(sort=False)
    payload = {
        str(industry): int(count)
        for industry, count in sorted(
            counts.items(),
            key=lambda item: str(item[0]),
        )
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _surface_row(
    policy: str,
    date: pd.Timestamp,
    row_type: str,
    grid: str,
    cell: str,
    diagnostic: str,
    member_count: int,
    industry_distribution: str,
    holding_return: float,
    status: str,
) -> dict[str, object]:
    return {
        "pit_policy": policy,
        "formation_date": pd.Timestamp(date),
        "row_type": row_type,
        "grid": grid,
        "cell": cell,
        "diagnostic": diagnostic,
        "member_count": int(member_count),
        "industry_distribution": industry_distribution,
        "formation_coverage": 1.0,
        "holding_return": holding_return,
        "status": status,
    }


def build_hard_sort_surface(
    exposures: pd.DataFrame,
    stock_period_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Build every fixed hard-sort cell and derived shape diagnostic."""
    model, returns = _validated_surface_inputs(
        exposures,
        stock_period_returns,
    )
    rows: list[dict[str, object]] = []
    for (policy, date), month_returns in returns.groupby(
        ["pit_policy", "formation_date"],
        sort=True,
    ):
        month = model[
            model["pit_policy"].eq(policy)
            & model["formation_date"].eq(date)
        ].copy()
        assigned = assign_hard_sort_cells(month)
        joined = assigned.merge(
            month_returns[
                ["formation_date", "ticker", "forward_return"]
            ],
            on=["formation_date", "ticker"],
            how="inner",
            validate="one_to_one",
        )
        coefficients = fama_macbeth_coefficients(
            month[
                [
                    "formation_date",
                    "ticker",
                    "s_perp",
                    "m_perp",
                    "h_perp",
                ]
            ],
            month_returns[
                ["formation_date", "ticker", "forward_return"]
            ],
        )
        beta_h = float(coefficients["beta_h"].iloc[0])
        cell_returns: dict[tuple[str, str], float] = {}
        cell_counts: dict[tuple[str, str], int] = {}
        cell_h_means: dict[tuple[str, str], float] = {}
        for grid, column, cells in (
            ("2x3", "cell_2x3", CELLS_2X3),
            ("5x5", "cell_5x5", CELLS_5X5),
        ):
            for cell in cells:
                members = joined[joined[column].eq(cell)]
                count = len(members)
                status = "OK" if count else "COVERAGE_BLOCKED"
                holding_return = (
                    float(members["forward_return"].mean())
                    if count
                    else float("nan")
                )
                h_mean = (
                    float(members["h_perp"].mean())
                    if count
                    else float("nan")
                )
                cell_returns[(grid, cell)] = holding_return
                cell_counts[(grid, cell)] = count
                cell_h_means[(grid, cell)] = h_mean
                rows.append(
                    _surface_row(
                        policy,
                        pd.Timestamp(date),
                        "cell",
                        grid,
                        cell,
                        "",
                        count,
                        _industry_distribution(members),
                        holding_return,
                        status,
                    )
                )

        corner_cells = [
            "small_growth",
            "small_value",
            "big_growth",
            "big_value",
        ]
        corner_values = [
            cell_returns[("2x3", cell)] for cell in corner_cells
        ]
        corner_ok = bool(np.isfinite(corner_values).all())
        corner = (
            corner_values[0]
            - corner_values[1]
            - corner_values[2]
            + corner_values[3]
            if corner_ok
            else float("nan")
        )
        rows.append(
            _surface_row(
                policy,
                pd.Timestamp(date),
                "diagnostic",
                "2x3",
                "all",
                "corner",
                sum(cell_counts[("2x3", cell)] for cell in corner_cells),
                "{}",
                float(corner),
                "OK" if corner_ok else "COVERAGE_BLOCKED",
            )
        )

        growth_minus_value: dict[int, float] = {}
        h_spread: dict[int, float] = {}
        for size in range(1, 6):
            growth_cell = f"S{size}_V5"
            value_cell = f"S{size}_V1"
            growth = cell_returns[("5x5", growth_cell)]
            value = cell_returns[("5x5", value_cell)]
            growth_h = cell_h_means[("5x5", growth_cell)]
            value_h = cell_h_means[("5x5", value_cell)]
            ok = bool(
                np.isfinite([growth, value, growth_h, value_h]).all()
            )
            growth_minus_value[size] = (
                growth - value if ok else float("nan")
            )
            h_spread[size] = (
                growth_h - value_h if ok else float("nan")
            )
            rows.append(
                _surface_row(
                    policy,
                    pd.Timestamp(date),
                    "diagnostic",
                    "5x5",
                    f"S{size}",
                    "growth_minus_value",
                    cell_counts[("5x5", growth_cell)]
                    + cell_counts[("5x5", value_cell)],
                    "{}",
                    float(growth_minus_value[size]),
                    "OK" if ok else "COVERAGE_BLOCKED",
                )
            )

        for size in range(2, 6):
            label = f"S{size}-S{size - 1}"
            source_cells = [
                f"S{size}_V5",
                f"S{size}_V1",
                f"S{size - 1}_V5",
                f"S{size - 1}_V1",
            ]
            actual = growth_minus_value[size] - growth_minus_value[size - 1]
            ok = bool(np.isfinite(actual))
            rows.append(
                _surface_row(
                    policy,
                    pd.Timestamp(date),
                    "diagnostic",
                    "5x5",
                    label,
                    "adjacent_row_difference",
                    sum(
                        cell_counts[("5x5", cell)]
                        for cell in source_cells
                    ),
                    "{}",
                    float(actual) if ok else float("nan"),
                    "OK" if ok else "COVERAGE_BLOCKED",
                )
            )
        for size in range(2, 6):
            label = f"S{size}-S{size - 1}"
            source_cells = [
                f"S{size}_V5",
                f"S{size}_V1",
                f"S{size - 1}_V5",
                f"S{size - 1}_V1",
            ]
            actual = growth_minus_value[size] - growth_minus_value[size - 1]
            predicted = beta_h * (h_spread[size] - h_spread[size - 1])
            residual = actual - predicted
            ok = bool(np.isfinite([actual, predicted, residual]).all())
            rows.append(
                _surface_row(
                    policy,
                    pd.Timestamp(date),
                    "diagnostic",
                    "5x5",
                    label,
                    "linear_prediction_residual",
                    sum(
                        cell_counts[("5x5", cell)]
                        for cell in source_cells
                    ),
                    "{}",
                    float(residual) if ok else float("nan"),
                    "OK" if ok else "COVERAGE_BLOCKED",
                )
            )
    output = pd.DataFrame(rows, columns=SURFACE_COLUMNS)
    if output.empty:
        raise DataBlocked("hard-sort surface contains no valid months")
    return output.reset_index(drop=True)


def _window_for_date(
    date: pd.Timestamp,
) -> tuple[str, bool] | None:
    for name, start, end, affects_verdict in WINDOW_SPECS:
        if start <= pd.Timestamp(date) <= end:
            return name, affects_verdict
    return None


def _validate_structure_config(cfg: dict) -> None:
    expected_windows = {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    if not isinstance(cfg, dict) or cfg.get("windows") != expected_windows:
        raise DataBlocked("B3 structure windows do not match the frozen spec")
    pit = cfg.get("pit")
    expected_policies = [
        "legal_deadline",
        "legal_deadline_plus_one_month_end",
    ]
    if not isinstance(pit, dict) or pit.get("policies") != expected_policies:
        raise DataBlocked("B3 structure policy set does not match the frozen spec")
    model = cfg.get("model")
    if not isinstance(model, dict) or model.get("newey_west_lag") != 3:
        raise DataBlocked("B3 structure Newey-West lag must remain 3")


def build_structure_coefficients(
    exposures: pd.DataFrame,
    stock_period_returns: pd.DataFrame,
    cfg: dict,
) -> pd.DataFrame:
    """Build monthly cross-sectional coefficients and fixed-window means."""
    _validate_structure_config(cfg)
    model, returns = _validated_surface_inputs(
        exposures,
        stock_period_returns,
    )
    if sorted(model["pit_policy"].unique()) != sorted(
        cfg["pit"]["policies"]
    ):
        raise DataBlocked(
            "structure coefficient policy set is incomplete"
        )
    rows: list[dict[str, object]] = []
    for policy in sorted(model["pit_policy"].unique()):
        policy_returns = returns[returns["pit_policy"].eq(policy)]
        valid_dates = set(policy_returns["formation_date"])
        policy_model = model[
            model["pit_policy"].eq(policy)
            & model["formation_date"].isin(valid_dates)
        ]
        monthly = fama_macbeth_coefficients(
            policy_model[
                [
                    "formation_date",
                    "ticker",
                    "s_perp",
                    "m_perp",
                    "h_perp",
                ]
            ],
            policy_returns[
                ["formation_date", "ticker", "forward_return"]
            ],
        )
        monthly = monthly[
            monthly["formation_date"].map(_window_for_date).notna()
        ].copy()
        for row in monthly.itertuples(index=False):
            window_info = _window_for_date(row.formation_date)
            if window_info is None:
                continue
            window, affects_verdict = window_info
            rows.append(
                {
                    "pit_policy": policy,
                    "row_type": "monthly",
                    "formation_date": row.formation_date,
                    "window": window,
                    "alpha": float(row.alpha),
                    "beta_s": float(row.beta_s),
                    "beta_m": float(row.beta_m),
                    "beta_h": float(row.beta_h),
                    "n": int(row.n),
                    "ordinary_t_beta_h": float("nan"),
                    "nw_lag3_t_beta_h": float("nan"),
                    "affects_verdict": bool(affects_verdict),
                }
            )
        for window, start, end, affects_verdict in WINDOW_SPECS:
            subset = monthly[
                monthly["formation_date"].between(start, end)
            ]
            beta_h = subset["beta_h"]
            rows.append(
                {
                    "pit_policy": policy,
                    "row_type": "summary",
                    "formation_date": pd.NaT,
                    "window": window,
                    "alpha": (
                        float(subset["alpha"].mean())
                        if len(subset)
                        else float("nan")
                    ),
                    "beta_s": (
                        float(subset["beta_s"].mean())
                        if len(subset)
                        else float("nan")
                    ),
                    "beta_m": (
                        float(subset["beta_m"].mean())
                        if len(subset)
                        else float("nan")
                    ),
                    "beta_h": (
                        float(beta_h.mean())
                        if len(subset)
                        else float("nan")
                    ),
                    "n": int(len(subset)),
                    "ordinary_t_beta_h": ordinary_mean_t(beta_h),
                    "nw_lag3_t_beta_h": newey_west_mean_t(
                        beta_h,
                        lag=3,
                    ),
                    "affects_verdict": bool(affects_verdict),
                }
            )
    output = pd.DataFrame(rows, columns=COEFFICIENT_COLUMNS)
    if output.empty:
        raise DataBlocked("structure coefficients contain no policies")
    return output.reset_index(drop=True)


@dataclass(frozen=True)
class StructureRunResult:
    surface_path: Path
    coefficients_path: Path
    status: str


def _read_cache(path: Path, label: str) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (EOFError, OSError, ValueError) as exc:
        raise DataBlocked(f"{label} cache cannot be read") from exc


def _optional_finite_numeric(
    values: pd.Series,
    label: str,
) -> pd.Series:
    if values.map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).any():
        raise DataBlocked(f"{label} cannot contain booleans")
    numeric = pd.to_numeric(values, errors="coerce")
    invalid = values.notna() & numeric.isna()
    nonfinite = numeric.notna() & ~np.isfinite(numeric)
    if invalid.any() or nonfinite.any():
        raise DataBlocked(f"{label} must be numeric and finite when present")
    return numeric.astype(float)


def _validate_auxiliary_caches(
    axis: pd.DataFrame,
    states: pd.DataFrame,
    cfg: dict,
    data_end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    axis_columns = {
        "date",
        "pit_policy",
        "style",
        "size",
        "interaction",
    }
    state_columns = {
        "date",
        "pit_policy",
        "q",
        "growth_ret",
        "value_ret",
        "g",
        "v",
        "d",
        "d_UU",
        "d_DD",
        "d_DIV",
        "state",
        "raw_U",
        "F_U",
        "raw_D",
        "F_D",
        "raw_X",
        "F_X",
        "raw_T",
        "F_T",
        "external_market_direction",
    }
    axis_frame = _require_frame(axis, axis_columns, "axis cache")
    state_frame = _require_frame(states, state_columns, "states cache")
    if set(axis_frame.columns) != axis_columns:
        raise DataBlocked("axis cache schema mismatch")
    if set(state_frame.columns) != state_columns:
        raise DataBlocked("states cache schema mismatch")
    axis_frame["date"] = _strict_dates(
        axis_frame["date"],
        "axis cache.date",
    )
    state_frame["date"] = _strict_dates(
        state_frame["date"],
        "states cache.date",
    )
    if axis_frame["date"].max() > data_end:
        raise DataBlocked("axis cache contains dates after data_end")
    if state_frame["date"].max() > data_end:
        raise DataBlocked("states cache contains dates after data_end")
    _validate_strings(axis_frame["pit_policy"], "axis cache.pit_policy")
    _validate_strings(
        state_frame["pit_policy"],
        "states cache.pit_policy",
    )
    _validate_strings(state_frame["q"], "states cache.q")
    policies = sorted(cfg["pit"]["policies"])
    if sorted(axis_frame["pit_policy"].unique()) != policies:
        raise DataBlocked("axis cache policy set mismatch")
    if sorted(state_frame["pit_policy"].unique()) != policies:
        raise DataBlocked("states cache policy set mismatch")
    if set(state_frame["q"]) != {"qblend", "q500", "q1000"}:
        raise DataBlocked("states cache q set mismatch")
    if axis_frame.duplicated(["date", "pit_policy"]).any():
        raise DataBlocked("axis cache contains duplicate keys")
    if state_frame.duplicated(["date", "pit_policy", "q"]).any():
        raise DataBlocked("states cache contains duplicate keys")
    for column in ("style", "size", "interaction"):
        axis_frame[column] = _finite_numeric(
            axis_frame[column],
            f"axis cache.{column}",
        )
    for column in (
        "growth_ret",
        "value_ret",
        "g",
        "v",
        "d",
        "d_UU",
        "d_DD",
        "d_DIV",
    ):
        state_frame[column] = _finite_numeric(
            state_frame[column],
            f"states cache.{column}",
        )
    for column in (
        "raw_U",
        "F_U",
        "raw_D",
        "F_D",
        "raw_X",
        "F_X",
        "raw_T",
        "F_T",
    ):
        state_frame[column] = _optional_finite_numeric(
            state_frame[column],
            f"states cache.{column}",
        )
    if not state_frame["state"].isin({"UU", "DD", "DIV"}).all():
        raise DataBlocked("states cache contains unsupported state labels")
    if not state_frame["external_market_direction"].isin(
        {"up", "non_positive"}
    ).all():
        raise DataBlocked(
            "states cache contains unsupported external directions"
        )
    tolerance = float(cfg["exposure"]["identity_tolerance"])
    identity_error = (
        state_frame["d"]
        - state_frame["d_UU"]
        - state_frame["d_DD"]
        - state_frame["d_DIV"]
    ).abs().max()
    if not np.isfinite(identity_error) or identity_error > tolerance:
        raise DataBlocked("states cache additive identity mismatch")

    reference_axis_dates: pd.DatetimeIndex | None = None
    for policy in policies:
        dates = pd.DatetimeIndex(
            axis_frame.loc[
                axis_frame["pit_policy"].eq(policy),
                "date",
            ].sort_values(kind="mergesort")
        )
        if reference_axis_dates is None:
            reference_axis_dates = dates
        elif not dates.equals(reference_axis_dates):
            raise DataBlocked("axis cache date grids differ across policies")
    reference_state_dates: pd.DatetimeIndex | None = None
    for policy in policies:
        for q in ("qblend", "q500", "q1000"):
            dates = pd.DatetimeIndex(
                state_frame.loc[
                    state_frame["pit_policy"].eq(policy)
                    & state_frame["q"].eq(q),
                    "date",
                ].sort_values(kind="mergesort")
            )
            if len(dates) == 0:
                raise DataBlocked("states cache policy/q groups are incomplete")
            if reference_state_dates is None:
                reference_state_dates = dates
            elif not dates.equals(reference_state_dates):
                raise DataBlocked(
                    "states cache date grids differ across policy/q groups"
                )
    if (
        reference_axis_dates is None
        or reference_state_dates is None
        or not reference_axis_dates.equals(reference_state_dates)
    ):
        raise DataBlocked("axis and states cache date grids do not match")
    return axis_frame, state_frame


def _atomic_write_outputs(
    surface: pd.DataFrame,
    coefficients: pd.DataFrame,
    surface_path: Path,
    coefficients_path: Path,
) -> None:
    surface_path.parent.mkdir(parents=True, exist_ok=True)
    coefficients_path.parent.mkdir(parents=True, exist_ok=True)
    surface_temp = surface_path.with_name(f".{surface_path.name}.tmp")
    coefficients_temp = coefficients_path.with_name(
        f".{coefficients_path.name}.tmp"
    )
    try:
        surface.to_csv(
            surface_temp,
            index=False,
            date_format="%Y-%m-%d",
            lineterminator="\n",
        )
        coefficients.to_csv(
            coefficients_temp,
            index=False,
            date_format="%Y-%m-%d",
            lineterminator="\n",
        )
        surface_temp.replace(surface_path)
        coefficients_temp.replace(coefficients_path)
    except Exception:
        surface_path.unlink(missing_ok=True)
        coefficients_path.unlink(missing_ok=True)
        raise
    finally:
        surface_temp.unlink(missing_ok=True)
        coefficients_temp.unlink(missing_ok=True)


def _invalidate_structure_outputs(
    surface_path: Path,
    coefficients_path: Path,
) -> None:
    for path in (surface_path, coefficients_path):
        path.unlink(missing_ok=True)
        path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)


def run_structure(
    cfg: dict,
    data_end: pd.Timestamp,
    research_output_dir: str | Path = DEFAULT_RESEARCH_OUTPUT_DIR,
    backtest_output_dir: str | Path = DEFAULT_BACKTEST_OUTPUT_DIR,
) -> StructureRunResult:
    """Verify staged caches and materialize the frozen structure audit."""
    research_root = Path(research_output_dir)
    backtest_root = Path(backtest_output_dir)
    surface_path = research_root / "hard_sort_surface.csv"
    coefficients_path = backtest_root / "structure_coefficients.csv"
    _invalidate_structure_outputs(surface_path, coefficients_path)
    _validate_structure_config(cfg)
    cutoff = _strict_dates(
        pd.Series([data_end]),
        "structure data_end",
    ).iloc[0]

    for parent in ("exposures", "portfolios", "states"):
        require_parent_manifest(
            research_root,
            parent,
            cfg,
            cutoff,
        )
    exposures = _read_cache(
        research_root / "monthly_exposures.csv.gz",
        "monthly exposures",
    )
    stock_returns = _read_cache(
        research_root / "stock_period_returns.csv.gz",
        "stock period returns",
    )
    axis = _read_cache(
        research_root / "axis_returns.csv",
        "axis",
    )
    states = _read_cache(
        research_root / "state_components.csv",
        "states",
    )
    _validate_auxiliary_caches(axis, states, cfg, cutoff)

    exposures = _require_frame(
        exposures,
        {"formation_date"},
        "monthly exposures",
    )
    stock_returns = _require_frame(
        stock_returns,
        {"formation_date"},
        "stock period returns",
    )

    exposure_dates = _strict_dates(
        exposures["formation_date"],
        "monthly exposures.formation_date",
    )
    return_dates = _strict_dates(
        stock_returns["formation_date"],
        "stock period returns.formation_date",
    )
    if exposure_dates.max() > cutoff:
        raise DataBlocked(
            "monthly exposures contain formation dates after data_end"
        )
    if (
        not cutoff.is_month_end
        and exposure_dates.max().to_period("M")
        == cutoff.to_period("M")
    ):
        raise DataBlocked(
            "monthly exposures contain an incomplete cutoff-month formation"
        )
    if return_dates.max() > cutoff:
        raise DataBlocked(
            "stock period returns contain dates after data_end"
        )
    start = WINDOW_SPECS[0][1]
    end = min(cutoff, WINDOW_SPECS[-1][2])
    exposures = exposures.loc[
        exposure_dates.between(start, end)
    ].copy()
    stock_returns = stock_returns.loc[
        return_dates.between(start, end)
    ].copy()
    if exposures.empty or stock_returns.empty:
        raise DataBlocked(
            "structure caches contain no rows inside frozen windows"
        )

    surface = build_hard_sort_surface(exposures, stock_returns)
    coefficients = build_structure_coefficients(
        exposures,
        stock_returns,
        cfg,
    )
    _atomic_write_outputs(
        surface,
        coefficients,
        surface_path,
        coefficients_path,
    )
    blocked = surface.loc[
        surface["row_type"].eq("cell"),
        "status",
    ].eq("COVERAGE_BLOCKED").any()
    return StructureRunResult(
        surface_path=surface_path,
        coefficients_path=coefficients_path,
        status="COVERAGE_BLOCKED" if blocked else "OK",
    )


def _parse_cli_date(value: str) -> pd.Timestamp:
    try:
        return _strict_dates(
            pd.Series([value]),
            "structure data_end",
        ).iloc[0]
    except DataBlocked as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="B3 hard-sort and cross-sectional structure audit"
    )
    parser.add_argument(
        "--data-end",
        type=_parse_cli_date,
        default="2026-12-31",
    )
    parser.add_argument(
        "--research-output-dir",
        type=Path,
        default=DEFAULT_RESEARCH_OUTPUT_DIR,
    )
    parser.add_argument(
        "--backtest-output-dir",
        type=Path,
        default=DEFAULT_BACKTEST_OUTPUT_DIR,
    )
    args = parser.parse_args(argv)
    cfg = load_b3_config()
    try:
        result = run_structure(
            cfg,
            args.data_end,
            args.research_output_dir,
            args.backtest_output_dir,
        )
    except DataBlocked as exc:
        print(f"DATA_BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 3 if result.status == "COVERAGE_BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
