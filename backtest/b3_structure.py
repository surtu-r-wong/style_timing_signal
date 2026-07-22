"""Hard-sort and cross-sectional structure evidence for B3."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.rotation_probe import partial_rank_ic
from signals.style_basket.b3_build import require_parent_manifest
from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_exposures import DataBlocked


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESEARCH_OUTPUT_DIR = ROOT / "output" / "style_basket" / "b3"
DEFAULT_BACKTEST_OUTPUT_DIR = ROOT / "backtest" / "output" / "b3"
DEFAULT_EQUAL_WEIGHT_SIGNAL_PATH = (
    ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"
)
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
MODEL_COMPARISON_COLUMNS = [
    "pit_policy",
    "candidate",
    "q",
    "target",
    "window",
    "model",
    "n",
    "oos_r2",
    "spearman_ic",
    "partial_ic",
    "cosine_early_late",
    "confirmation_score_spearman",
    "state_UU_share",
    "state_DD_share",
    "state_DIV_share",
    "gate_name",
    "gate_pass",
    "is_in_sample",
    "affects_verdict",
]
MODEL_ROW_ID_COLUMNS = [
    "pit_policy",
    "candidate",
    "q",
    "target",
    "window",
    "model",
    "gate_name",
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


@dataclass(frozen=True)
class ModelFit:
    features: tuple[str, ...]
    intercept: float
    slopes: tuple[float, ...]
    train_target_mean: float
    n: int


def fit_model(
    frame: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
) -> ModelFit:
    """Fit one frozen OLS model using only the supplied training slice."""
    if (
        not isinstance(feature_columns, list)
        or not feature_columns
        or len(set(feature_columns)) != len(feature_columns)
    ):
        raise DataBlocked("model features must be a unique nonempty list")
    if not isinstance(target_column, str) or not target_column.strip():
        raise DataBlocked("model target must be a nonempty string")
    if target_column in feature_columns:
        raise DataBlocked("model target cannot also be a feature")
    required = {target_column, *feature_columns}
    source = _require_frame(frame, required, "model training frame")
    clean = source[[target_column, *feature_columns]].dropna()
    for column in (target_column, *feature_columns):
        clean[column] = _finite_numeric(
            clean[column],
            f"model training frame.{column}",
        )
    parameter_count = len(feature_columns) + 1
    if len(clean) <= parameter_count:
        raise RuntimeError("model has insufficient observations")
    design = np.column_stack(
        [
            np.ones(len(clean), dtype=float),
            clean[feature_columns].to_numpy(dtype=float),
        ]
    )
    if np.linalg.matrix_rank(design) != design.shape[1]:
        raise RuntimeError("model design is rank deficient")
    beta, _, _, _ = np.linalg.lstsq(
        design,
        clean[target_column].to_numpy(dtype=float),
        rcond=None,
    )
    return ModelFit(
        features=tuple(feature_columns),
        intercept=float(beta[0]),
        slopes=tuple(float(value) for value in beta[1:]),
        train_target_mean=float(clean[target_column].mean()),
        n=int(len(clean)),
    )


def apply_model(
    frame: pd.DataFrame,
    model: ModelFit,
    include_intercept: bool = False,
) -> pd.Series:
    """Apply frozen slopes; the fitted intercept is diagnostic-only by default."""
    if not isinstance(model, ModelFit):
        raise DataBlocked("model must be a ModelFit")
    if not isinstance(include_intercept, (bool, np.bool_)):
        raise DataBlocked("include_intercept must be boolean")
    source = _require_frame(
        frame,
        set(model.features),
        "model scoring frame",
    )
    features = source[list(model.features)].copy()
    for column in model.features:
        features[column] = _finite_numeric(
            features[column],
            f"model scoring frame.{column}",
        )
    values = features.to_numpy(dtype=float) @ np.asarray(
        model.slopes,
        dtype=float,
    )
    if include_intercept:
        values = values + model.intercept
    return pd.Series(values, index=source.index, name="score", dtype=float)


def oos_r_squared(
    target: pd.Series,
    prediction: pd.Series,
    train_target_mean: float,
) -> float:
    """Return 1-SSE/SST where SST uses the frozen training target mean."""
    if not isinstance(target, pd.Series) or not isinstance(
        prediction, pd.Series
    ):
        raise DataBlocked("OOS target and prediction must be Series")
    if target.index.has_duplicates or prediction.index.has_duplicates:
        raise DataBlocked("OOS target and prediction keys must be unique")
    if isinstance(train_target_mean, (bool, np.bool_)) or not np.isfinite(
        train_target_mean
    ):
        raise DataBlocked("training target mean must be finite")
    joint = pd.concat(
        [target.rename("target"), prediction.rename("prediction")],
        axis=1,
        join="inner",
    ).dropna()
    if joint.empty:
        raise DataBlocked("OOS target and prediction do not overlap")
    joint["target"] = _finite_numeric(joint["target"], "OOS target")
    joint["prediction"] = _finite_numeric(
        joint["prediction"],
        "OOS prediction",
    )
    sse = float(
        ((joint["target"] - joint["prediction"]) ** 2).sum()
    )
    denominator = float(
        ((joint["target"] - float(train_target_mean)) ** 2).sum()
    )
    if denominator == 0.0:
        return float("nan")
    return 1.0 - sse / denominator


def stability_gate(
    early_slopes: np.ndarray,
    late_slopes: np.ndarray,
    confirmation_daily_features: pd.DataFrame,
    min_score_spearman: float,
) -> dict[str, float | bool]:
    """Evaluate the frozen subwindow slope and confirmation-score gate."""
    early = np.asarray(early_slopes)
    late = np.asarray(late_slopes)
    if (
        early.ndim != 1
        or late.ndim != 1
        or early.shape != late.shape
        or len(early) == 0
        or not np.issubdtype(early.dtype, np.number)
        or not np.issubdtype(late.dtype, np.number)
        or not np.isfinite(early).all()
        or not np.isfinite(late).all()
    ):
        raise DataBlocked("stability slopes must be matching finite vectors")
    if (
        isinstance(min_score_spearman, (bool, np.bool_))
        or not np.isfinite(min_score_spearman)
        or not -1.0 <= float(min_score_spearman) <= 1.0
    ):
        raise DataBlocked("stability Spearman minimum is invalid")
    features = _require_frame(
        confirmation_daily_features,
        set(confirmation_daily_features.columns),
        "stability confirmation features",
    )
    if features.shape[1] != len(early):
        raise DataBlocked("stability feature and slope dimensions differ")
    for column in features.columns:
        features[column] = _finite_numeric(
            features[column],
            f"stability confirmation features.{column}",
        )
    denominator = float(np.linalg.norm(early) * np.linalg.norm(late))
    cosine = (
        float(early @ late / denominator)
        if denominator > 0.0
        else float("nan")
    )
    values = features.to_numpy(dtype=float)
    early_score = pd.Series(values @ early, index=features.index)
    late_score = pd.Series(values @ late, index=features.index)
    score_spearman = early_score.corr(late_score, method="spearman")
    passed = bool(
        np.isfinite(cosine)
        and cosine > 0.0
        and np.isfinite(score_spearman)
        and score_spearman >= float(min_score_spearman)
    )
    return {
        "cosine": cosine,
        "score_spearman": float(score_spearman),
        "pass": passed,
    }


def state_coverage_gate(
    state: pd.Series,
    minimum: float,
) -> dict[str, float | bool]:
    """Check all three states in each frozen gate subwindow."""
    if not isinstance(state, pd.Series) or state.empty:
        raise DataBlocked("state coverage input must be a nonempty Series")
    if not isinstance(state.index, pd.DatetimeIndex):
        raise DataBlocked("state coverage index must be a DatetimeIndex")
    if (
        state.index.tz is not None
        or not state.index.equals(state.index.normalize())
        or state.index.has_duplicates
        or not state.index.is_monotonic_increasing
    ):
        raise DataBlocked(
            "state coverage dates must be unique monotonic naive dates"
        )
    if not state.dropna().isin({"UU", "DD", "DIV"}).all():
        raise DataBlocked("state coverage contains unsupported labels")
    if (
        isinstance(minimum, (bool, np.bool_))
        or not np.isfinite(minimum)
        or not 0.0 <= float(minimum) <= 1.0
    ):
        raise DataBlocked("state coverage minimum is invalid")
    windows = {
        "2014-2017": ("2014-01-01", "2017-12-31", True),
        "2018-2020": ("2018-01-01", "2020-12-31", True),
        "2021-2023": ("2021-01-01", "2023-12-31", True),
        "2014-2020": ("2014-01-01", "2020-12-31", False),
    }
    result: dict[str, float | bool] = {}
    passed = True
    for window, (start, end, affects_gate) in windows.items():
        sample = state.loc[start:end].dropna()
        for label in ("UU", "DD", "DIV"):
            share = float(sample.eq(label).mean()) if len(sample) else 0.0
            result[f"{window}_{label}"] = share
            if affects_gate:
                passed &= share >= float(minimum)
    result["pass"] = bool(passed)
    return result


def next_formation_targets(
    daily_return: pd.Series,
    formations: pd.DatetimeIndex,
) -> pd.Series:
    """Compound exact non-overlapping (formation, next formation] targets."""
    if not isinstance(daily_return, pd.Series) or daily_return.empty:
        raise DataBlocked("daily target return must be a nonempty Series")
    if not isinstance(daily_return.index, pd.DatetimeIndex):
        raise DataBlocked("daily target return index must be a DatetimeIndex")
    if (
        daily_return.index.tz is not None
        or not daily_return.index.equals(daily_return.index.normalize())
        or daily_return.index.has_duplicates
        or not daily_return.index.is_monotonic_increasing
    ):
        raise DataBlocked(
            "daily target return dates must be unique increasing naive dates"
        )
    returns = _finite_numeric(daily_return, "daily target return")
    if returns.le(-1.0).any():
        raise DataBlocked("daily target returns must exceed -100%")
    if not isinstance(formations, pd.DatetimeIndex) or len(formations) < 2:
        raise DataBlocked("formation dates must contain at least two dates")
    if (
        formations.tz is not None
        or not formations.equals(formations.normalize())
        or formations.has_duplicates
        or not formations.is_monotonic_increasing
    ):
        raise DataBlocked(
            "formation dates must be unique increasing naive dates"
        )
    if not formations.isin(returns.index).all():
        raise DataBlocked("formation dates are missing from target returns")
    output: dict[pd.Timestamp, float] = {}
    for start, end in zip(formations[:-1], formations[1:]):
        sample = returns.loc[
            returns.index.to_series().between(start, end, inclusive="right")
        ]
        if sample.empty:
            raise RuntimeError(
                f"invalid target holding period: {start.date()}"
            )
        output[pd.Timestamp(start)] = float(
            (1.0 + sample).prod() - 1.0
        )
    return pd.Series(output, name="target", dtype=float)


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
    percentile: np.ndarray = np.empty(len(order), dtype=float)
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
    frozen_thresholds = {
        "state_min_coverage": 0.10,
        "stability_score_spearman_min": 0.50,
        "interaction_axis_corr_max": 0.80,
    }
    for name, expected in frozen_thresholds.items():
        value = model.get(name)
        if (
            isinstance(value, (bool, np.bool_))
            or not isinstance(value, (int, float, np.number))
            or not np.isfinite(value)
            or float(value) != expected
        ):
            raise DataBlocked(
                f"B3 structure {name} must remain {expected:.2f}"
            )


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


Q_MODEL_SPECS = {
    "qblend": ("B3_unified", "blend"),
    "q500": ("B3_dual_target", "500"),
    "q1000": ("B3_dual_target", "1000"),
}
MODEL_FEATURES = ["F_U", "F_D", "F_X"]


def _validated_daily_model_series(
    values: pd.Series,
    label: str,
    formations: pd.DatetimeIndex,
    *,
    is_return: bool,
) -> pd.Series:
    if not isinstance(values, pd.Series) or values.empty:
        raise DataBlocked(f"{label} must be a nonempty Series")
    if not isinstance(values.index, pd.DatetimeIndex):
        raise DataBlocked(f"{label} index must be a DatetimeIndex")
    if (
        values.index.tz is not None
        or not values.index.equals(values.index.normalize())
        or values.index.has_duplicates
        or not values.index.is_monotonic_increasing
    ):
        raise DataBlocked(
            f"{label} dates must be unique increasing naive dates"
        )
    sample = values.loc[formations.min() : formations.max()].copy()
    sample = _finite_numeric(sample, label)
    if not formations.isin(sample.index).all():
        raise DataBlocked(f"{label} is missing required formation dates")
    if is_return and sample.le(-1.0).any():
        raise DataBlocked(f"{label} must exceed -100%")
    return sample


def _validated_model_comparison_inputs(
    state_components: pd.DataFrame,
    axis_returns: pd.DataFrame,
    structure_coefficients: pd.DataFrame,
    hard_sort_surface: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, pd.Series],
    pd.Series,
    pd.DatetimeIndex,
]:
    _validate_structure_config(cfg)
    if not isinstance(formation_dates, pd.DatetimeIndex):
        raise DataBlocked("model formation dates must be a DatetimeIndex")
    formations = formation_dates.copy()
    if (
        len(formations) < 2
        or formations.tz is not None
        or not formations.equals(formations.normalize())
        or formations.has_duplicates
        or not formations.is_monotonic_increasing
    ):
        raise DataBlocked(
            "model formation dates must be unique increasing naive dates"
        )
    periods = formations.to_period("M")
    if periods.has_duplicates or not periods.equals(
        pd.period_range(periods[0], periods[-1], freq="M")
    ):
        raise DataBlocked("model formation dates must be monthly continuous")

    state_required = {
        "date",
        "pit_policy",
        "q",
        "state",
        "F_U",
        "F_D",
        "F_X",
        "F_T",
    }
    states = _require_frame(
        state_components,
        state_required,
        "model state components",
    )
    states["date"] = _strict_dates(
        states["date"],
        "model state components.date",
    )
    states = states[
        states["date"].between(formations.min(), formations.max())
    ].copy()
    if states.empty:
        raise DataBlocked("model state components miss the formation window")
    _validate_strings(
        states["pit_policy"],
        "model state components.pit_policy",
    )
    _validate_strings(states["q"], "model state components.q")
    if sorted(states["pit_policy"].unique()) != sorted(
        cfg["pit"]["policies"]
    ):
        raise DataBlocked("model state policy set mismatch")
    if set(states["q"]) != set(Q_MODEL_SPECS):
        raise DataBlocked("model state q set mismatch")
    if states.duplicated(["date", "pit_policy", "q"]).any():
        raise DataBlocked("model state components contain duplicate keys")
    if not states["state"].isin({"UU", "DD", "DIV"}).all():
        raise DataBlocked("model state components contain invalid states")
    for column in (*MODEL_FEATURES, "F_T"):
        states[column] = _finite_numeric(
            states[column],
            f"model state components.{column}",
        )

    reference_dates: pd.DatetimeIndex | None = None
    for policy in cfg["pit"]["policies"]:
        for q in Q_MODEL_SPECS:
            group_dates = pd.DatetimeIndex(
                states.loc[
                    states["pit_policy"].eq(policy)
                    & states["q"].eq(q),
                    "date",
                ].sort_values(kind="mergesort")
            )
            if not formations.isin(group_dates).all():
                raise DataBlocked(
                    "model state components miss formation-date features"
                )
            if reference_dates is None:
                reference_dates = group_dates
            elif not group_dates.equals(reference_dates):
                raise DataBlocked("model state daily grids differ")

    axis = _require_frame(
        axis_returns,
        {"date", "pit_policy", "style", "interaction"},
        "model axis returns",
    )
    axis["date"] = _strict_dates(axis["date"], "model axis returns.date")
    axis = axis[
        axis["date"].between(formations.min(), formations.max())
    ].copy()
    _validate_strings(axis["pit_policy"], "model axis returns.pit_policy")
    if sorted(axis["pit_policy"].unique()) != sorted(
        cfg["pit"]["policies"]
    ):
        raise DataBlocked("model axis policy set mismatch")
    if axis.duplicated(["date", "pit_policy"]).any():
        raise DataBlocked("model axis returns contain duplicate keys")
    for column in ("style", "interaction"):
        axis[column] = _finite_numeric(
            axis[column],
            f"model axis returns.{column}",
        )
    if reference_dates is None:
        raise DataBlocked("model state daily grid is empty")
    for policy in cfg["pit"]["policies"]:
        policy_dates = pd.DatetimeIndex(
            axis.loc[
                axis["pit_policy"].eq(policy), "date"
            ].sort_values(kind="mergesort")
        )
        if not policy_dates.equals(reference_dates):
            raise DataBlocked("model state and axis daily grids differ")

    coefficients = _require_frame(
        structure_coefficients,
        {
            "pit_policy",
            "row_type",
            "formation_date",
            "window",
            "beta_h",
        },
        "model structure coefficients",
    )
    _validate_strings(
        coefficients["pit_policy"],
        "model structure coefficients.pit_policy",
    )
    _validate_strings(
        coefficients["row_type"],
        "model structure coefficients.row_type",
    )
    _validate_strings(
        coefficients["window"],
        "model structure coefficients.window",
    )
    coefficients["beta_h"] = _optional_finite_numeric(
        coefficients["beta_h"],
        "model structure coefficients.beta_h",
    )
    monthly_coefficients = coefficients["row_type"].eq("monthly")
    if not monthly_coefficients.any():
        raise DataBlocked("model structure coefficients lack monthly rows")
    coefficients.loc[
        monthly_coefficients,
        "formation_date",
    ] = _strict_dates(
        coefficients.loc[monthly_coefficients, "formation_date"],
        "model structure coefficients.formation_date",
    )

    surface = _require_frame(
        hard_sort_surface,
        {"pit_policy", "formation_date", "row_type", "grid", "cell", "status"},
        "model hard-sort surface",
    )
    surface["formation_date"] = _strict_dates(
        surface["formation_date"],
        "model hard-sort surface.formation_date",
    )
    for column in ("pit_policy", "row_type", "grid", "cell", "status"):
        _validate_strings(
            surface[column],
            f"model hard-sort surface.{column}",
        )

    if not isinstance(target_returns, dict) or set(target_returns) != {
        "blend",
        "500",
        "1000",
    }:
        raise DataBlocked("model target return mapping must be blend/500/1000")
    targets = {
        target: _validated_daily_model_series(
            target_returns[target],
            f"model target returns.{target}",
            formations,
            is_return=True,
        )
        for target in ("blend", "500", "1000")
    }
    for target, series in targets.items():
        if not series.index.equals(reference_dates):
            raise DataBlocked(
                f"model target returns.{target} daily grid mismatch"
            )
    control = _validated_daily_model_series(
        equal_weight_signal,
        "equal_weight control",
        formations,
        is_return=False,
    )
    return (
        states,
        axis,
        coefficients,
        surface,
        targets,
        control,
        formations,
    )


def _model_comparison_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "pit_policy": "",
        "candidate": "",
        "q": "",
        "target": "",
        "window": "",
        "model": "",
        "n": float("nan"),
        "oos_r2": float("nan"),
        "spearman_ic": float("nan"),
        "partial_ic": float("nan"),
        "cosine_early_late": float("nan"),
        "confirmation_score_spearman": float("nan"),
        "state_UU_share": float("nan"),
        "state_DD_share": float("nan"),
        "state_DIV_share": float("nan"),
        "gate_name": "",
        "gate_pass": float("nan"),
        "is_in_sample": False,
        "affects_verdict": False,
    }
    row.update(overrides)
    return row


def _score_metrics(
    sample: pd.DataFrame,
    model: ModelFit,
) -> tuple[pd.Series, float, float]:
    if sample.empty:
        raise DataBlocked("frozen model metric window is empty")
    score = apply_model(sample, model, include_intercept=False)
    oos = oos_r_squared(
        sample["target"],
        score,
        model.train_target_mean,
    )
    spearman = score.corr(sample["target"], method="spearman")
    return score, float(oos), float(spearman)


def _increment_direction(value: float) -> int | str:
    """Map a model increment to its frozen PIT-comparison direction."""
    if not np.isfinite(value):
        return "NONFINITE"
    return int(np.sign(value))


def _closed_formation_window(
    frame: pd.DataFrame,
    formations: pd.DatetimeIndex,
    start: str,
    end: str,
) -> pd.DataFrame:
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    ).reindex(frame.index)
    if period_end.isna().any():
        raise DataBlocked("model metric rows lack next-formation boundaries")
    mask = (
        frame.index.to_series().ge(pd.Timestamp(start))
        & period_end.le(pd.Timestamp(end))
    )
    return frame.loc[mask.to_numpy()].copy()


def _hard_sort_gate_passes(
    surface: pd.DataFrame,
    policy: str,
    formations: pd.DatetimeIndex,
) -> bool:
    required_dates = formations[
        (formations >= pd.Timestamp("2014-01-01"))
        & (formations <= pd.Timestamp("2023-12-31"))
    ]
    cells = surface[
        surface["pit_policy"].eq(policy)
        & surface["row_type"].eq("cell")
        & surface["formation_date"].isin(required_dates)
    ]
    if len(required_dates) == 0:
        return False
    if cells.duplicated(["formation_date", "grid", "cell"]).any():
        return False
    expected = {
        (date, "2x3", cell)
        for date in required_dates
        for cell in CELLS_2X3
    } | {
        (date, "5x5", cell)
        for date in required_dates
        for cell in CELLS_5X5
    }
    actual = set(
        cells[["formation_date", "grid", "cell"]].itertuples(
            index=False,
            name=None,
        )
    )
    return actual == expected and cells["status"].eq("OK").all()


def build_model_comparison(
    state_components: pd.DataFrame,
    axis_returns: pd.DataFrame,
    structure_coefficients: pd.DataFrame,
    hard_sort_surface: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> pd.DataFrame:
    """Build frozen M0/M1 evidence and fixed structure-gate rows."""
    (
        states,
        axis,
        coefficients,
        surface,
        targets,
        control,
        formations,
    ) = _validated_model_comparison_inputs(
        state_components,
        axis_returns,
        structure_coefficients,
        hard_sort_surface,
        target_returns,
        equal_weight_signal,
        formation_dates,
        cfg,
    )
    rows: list[dict[str, object]] = []
    leg_gate_pass: dict[tuple[str, str, str], bool] = {}
    aggregate_gate_pass: dict[tuple[str, str, str], bool] = {}
    increment_signs: dict[tuple[str, str], int | str] = {}
    beta_signs: dict[str, tuple[int, ...]] = {}
    minimum_coverage = float(cfg["model"]["state_min_coverage"])
    minimum_stability = float(
        cfg["model"]["stability_score_spearman_min"]
    )
    maximum_axis_corr = float(
        cfg["model"]["interaction_axis_corr_max"]
    )
    realized_formations = formations[:-1]

    for policy in cfg["pit"]["policies"]:
        policy_coefficients = coefficients[
            coefficients["pit_policy"].eq(policy)
            & coefficients["row_type"].eq("monthly")
        ].copy()
        if policy_coefficients.duplicated("formation_date").any():
            raise DataBlocked("monthly beta_h rows contain duplicate dates")
        policy_coefficients = policy_coefficients.set_index(
            "formation_date"
        ).sort_index()
        beta_values_list: list[float] = []
        for start, end in (
            ("2014-01-01", "2017-12-31"),
            ("2018-01-01", "2020-12-31"),
            ("2021-01-01", "2023-12-31"),
        ):
            expected = _closed_formation_window(
                pd.DataFrame(index=realized_formations),
                formations,
                start,
                end,
            ).index
            sample = policy_coefficients.reindex(expected)
            if (
                len(sample) != len(expected)
                or sample["beta_h"].isna().any()
            ):
                beta_values_list.append(float("nan"))
            else:
                beta_values_list.append(float(sample["beta_h"].mean()))
        beta_values = np.asarray(beta_values_list, dtype=float)
        beta_direction = tuple(
            int(np.sign(value)) if np.isfinite(value) else 0
            for value in beta_values
        )
        beta_signs[policy] = beta_direction
        beta_pass = bool(
            np.isfinite(beta_values).all()
            and np.all(beta_values != 0.0)
            and len(set(beta_direction)) == 1
        )
        rows.append(
            _model_comparison_row(
                pit_policy=policy,
                candidate="PUBLIC",
                window="structure",
                n=3,
                gate_name="beta_h_same_sign",
                gate_pass=beta_pass,
                affects_verdict=True,
            )
        )

        policy_axis = (
            axis[axis["pit_policy"].eq(policy)]
            .sort_values("date", kind="mergesort")
            .set_index("date")
        )
        style_monthly = next_formation_targets(
            policy_axis["style"], formations
        )
        interaction_monthly = next_formation_targets(
            policy_axis["interaction"], formations
        )
        axis_joint = pd.concat(
            [
                style_monthly.rename("style"),
                interaction_monthly.rename("interaction"),
            ],
            axis=1,
            join="inner",
        )
        axis_joint = _closed_formation_window(
            axis_joint,
            formations,
            "2021-01-01",
            "2023-12-31",
        )
        axis_corr = axis_joint["style"].corr(
            axis_joint["interaction"],
            method="pearson",
        )
        axis_pass = bool(
            np.isfinite(axis_corr) and abs(axis_corr) < maximum_axis_corr
        )
        rows.append(
            _model_comparison_row(
                pit_policy=policy,
                candidate="PUBLIC",
                window="2021-2023",
                n=int(len(axis_joint)),
                gate_name="interaction_axis_corr",
                gate_pass=axis_pass,
                affects_verdict=True,
            )
        )
        rows.append(
            _model_comparison_row(
                pit_policy=policy,
                candidate="PUBLIC",
                window="structure",
                n=int(
                    len(
                        realized_formations[
                            (realized_formations >= pd.Timestamp("2014-01-01"))
                            & (
                                realized_formations
                                <= pd.Timestamp("2023-12-31")
                            )
                        ]
                    )
                ),
                gate_name="hard_sort_complete",
                gate_pass=_hard_sort_gate_passes(
                    surface,
                    policy,
                    realized_formations,
                ),
                affects_verdict=True,
            )
        )

        for q, (candidate, target_name) in Q_MODEL_SPECS.items():
            q_state = (
                states[
                    states["pit_policy"].eq(policy)
                    & states["q"].eq(q)
                ]
                .sort_values("date", kind="mergesort")
                .set_index("date")
            )
            target = next_formation_targets(
                targets[target_name],
                formations,
            )
            monthly = q_state.reindex(target.index)[
                [*MODEL_FEATURES, "F_T"]
            ].copy()
            if monthly.isna().any().any():
                raise DataBlocked(
                    f"model formation features are incomplete for {policy}/{q}"
                )
            monthly["target"] = target
            monthly_control = control.reindex(monthly.index)
            if monthly_control.isna().any():
                raise DataBlocked(
                    f"equal_weight control is incomplete for {policy}/{q}"
                )
            discovery = _closed_formation_window(
                monthly,
                formations,
                "2014-01-01",
                "2020-12-31",
            )
            early = _closed_formation_window(
                monthly,
                formations,
                "2014-01-01",
                "2017-12-31",
            )
            late = _closed_formation_window(
                monthly,
                formations,
                "2018-01-01",
                "2020-12-31",
            )
            confirmation = _closed_formation_window(
                monthly,
                formations,
                "2021-01-01",
                "2023-12-31",
            )
            report = _closed_formation_window(
                monthly,
                formations,
                "2024-01-01",
                "2026-12-31",
            )
            m0 = fit_model(discovery, ["F_T"], "target")
            m1 = fit_model(discovery, MODEL_FEATURES, "target")
            early_m1 = fit_model(early, MODEL_FEATURES, "target")
            late_m1 = fit_model(late, MODEL_FEATURES, "target")

            discovery_score = apply_model(
                discovery,
                m1,
                include_intercept=False,
            )
            discovery_partial = partial_rank_ic(
                discovery_score,
                discovery["target"],
                monthly_control.reindex(discovery.index),
            )
            rows.append(
                _model_comparison_row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target_name,
                    window="2014-2020",
                    model="M1",
                    n=int(len(discovery)),
                    spearman_ic=float(
                        discovery_score.corr(
                            discovery["target"],
                            method="spearman",
                        )
                    ),
                    partial_ic=float(discovery_partial),
                    is_in_sample=True,
                    affects_verdict=False,
                )
            )

            m0_confirmation_score, m0_oos, m0_ic = _score_metrics(
                confirmation,
                m0,
            )
            m1_confirmation_score, m1_oos, m1_ic = _score_metrics(
                confirmation,
                m1,
            )
            del m0_confirmation_score
            confirmation_partial = partial_rank_ic(
                m1_confirmation_score,
                confirmation["target"],
                monthly_control.reindex(confirmation.index),
            )
            rows.extend(
                [
                    _model_comparison_row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target_name,
                        window="2021-2023",
                        model="M0",
                        n=int(len(confirmation)),
                        oos_r2=m0_oos,
                        spearman_ic=m0_ic,
                        affects_verdict=True,
                    ),
                    _model_comparison_row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target_name,
                        window="2021-2023",
                        model="M1",
                        n=int(len(confirmation)),
                        oos_r2=m1_oos,
                        spearman_ic=m1_ic,
                        partial_ic=float(confirmation_partial),
                        affects_verdict=True,
                    ),
                ]
            )

            yearly_partial: dict[str, float] = {}
            for year in ("2021", "2022", "2023"):
                year_sample = _closed_formation_window(
                    monthly,
                    formations,
                    f"{year}-01-01",
                    f"{year}-12-31",
                )
                year_score, year_oos, year_ic = _score_metrics(
                    year_sample,
                    m1,
                )
                year_partial = partial_rank_ic(
                    year_score,
                    year_sample["target"],
                    monthly_control.reindex(year_sample.index),
                )
                yearly_partial[year] = float(year_partial)
                rows.append(
                    _model_comparison_row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target_name,
                        window=year,
                        model="M1",
                        n=int(len(year_sample)),
                        oos_r2=year_oos,
                        spearman_ic=year_ic,
                        partial_ic=float(year_partial),
                        affects_verdict=True,
                    )
                )

            if len(report):
                _, m0_report_oos, m0_report_ic = _score_metrics(report, m0)
                _, m1_report_oos, m1_report_ic = _score_metrics(report, m1)
                rows.extend(
                    [
                        _model_comparison_row(
                            pit_policy=policy,
                            candidate=candidate,
                            q=q,
                            target=target_name,
                            window="2024-2026-report-only",
                            model="M0",
                            n=int(len(report)),
                            oos_r2=m0_report_oos,
                            spearman_ic=m0_report_ic,
                        ),
                        _model_comparison_row(
                            pit_policy=policy,
                            candidate=candidate,
                            q=q,
                            target=target_name,
                            window="2024-2026-report-only",
                            model="M1",
                            n=int(len(report)),
                            oos_r2=m1_report_oos,
                            spearman_ic=m1_report_ic,
                        ),
                    ]
                )

            increment_delta = m1_oos - m0_oos
            increment_pass = bool(
                np.isfinite([increment_delta, m1_ic, m0_ic]).all()
                and increment_delta > 0.0
                and m1_ic >= m0_ic
            )
            increment_signs[(policy, q)] = _increment_direction(
                increment_delta
            )
            leg_gate_pass[(policy, q, "m1_increment")] = increment_pass
            rows.append(
                _model_comparison_row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target_name,
                    window="2021-2023",
                    model="M1",
                    gate_name="m1_increment",
                    gate_pass=increment_pass,
                    affects_verdict=True,
                )
            )

            partial_pass = bool(
                np.isfinite(confirmation_partial)
                and confirmation_partial > 0.0
                and sum(
                    np.isfinite(value) and value > 0.0
                    for value in yearly_partial.values()
                )
                >= 2
            )
            leg_gate_pass[(policy, q, "partial_ic")] = partial_pass
            rows.append(
                _model_comparison_row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target_name,
                    window="2021-2023",
                    model="M1",
                    partial_ic=float(confirmation_partial),
                    gate_name="partial_ic",
                    gate_pass=partial_pass,
                    affects_verdict=True,
                )
            )

            confirmation_daily = q_state.loc[
                "2021-01-01":"2023-12-31", MODEL_FEATURES
            ]
            stability = stability_gate(
                np.asarray(early_m1.slopes),
                np.asarray(late_m1.slopes),
                confirmation_daily,
                min_score_spearman=minimum_stability,
            )
            stability_pass = bool(stability["pass"])
            leg_gate_pass[(policy, q, "stability")] = stability_pass
            rows.append(
                _model_comparison_row(
                    pit_policy=policy,
                    candidate=candidate,
                    q=q,
                    target=target_name,
                    window="2021-2023",
                    model="M1",
                    cosine_early_late=float(stability["cosine"]),
                    confirmation_score_spearman=float(
                        stability["score_spearman"]
                    ),
                    gate_name="stability",
                    gate_pass=stability_pass,
                    affects_verdict=True,
                )
            )

            coverage = state_coverage_gate(
                q_state["state"],
                minimum_coverage,
            )
            coverage_windows = {
                "2014-2017": ("2014-01-01", "2017-12-31", True),
                "2018-2020": ("2018-01-01", "2020-12-31", True),
                "2021-2023": ("2021-01-01", "2023-12-31", True),
                "2014-2020": ("2014-01-01", "2020-12-31", False),
            }
            for window, (start, end, affects_verdict) in (
                coverage_windows.items()
            ):
                shares = {
                    label: float(coverage[f"{window}_{label}"])
                    for label in ("UU", "DD", "DIV")
                }
                window_pass = all(
                    share >= minimum_coverage for share in shares.values()
                )
                rows.append(
                    _model_comparison_row(
                        pit_policy=policy,
                        candidate=candidate,
                        q=q,
                        target=target_name,
                        window=window,
                        model="M1",
                        n=int(len(q_state.loc[start:end])),
                        state_UU_share=shares["UU"],
                        state_DD_share=shares["DD"],
                        state_DIV_share=shares["DIV"],
                        gate_name="state_coverage",
                        gate_pass=bool(window_pass),
                        affects_verdict=affects_verdict,
                    )
                )
            leg_gate_pass[(policy, q, "state_coverage")] = bool(
                coverage["pass"]
            )

        candidate_legs = {
            "B3_unified": ("qblend",),
            "B3_dual_target": ("q500", "q1000"),
        }
        for candidate, legs in candidate_legs.items():
            for gate_name in (
                "m1_increment",
                "partial_ic",
                "stability",
                "state_coverage",
            ):
                passed = all(
                    leg_gate_pass[(policy, q, gate_name)] for q in legs
                )
                aggregate_gate_pass[(policy, candidate, gate_name)] = passed
                rows.append(
                    _model_comparison_row(
                        pit_policy=policy,
                        candidate=candidate,
                        window="2021-2023",
                        gate_name=gate_name,
                        gate_pass=bool(passed),
                        affects_verdict=True,
                    )
                )

    first_policy, second_policy = cfg["pit"]["policies"]
    pit_flip = beta_signs[first_policy] != beta_signs[second_policy]
    for q in Q_MODEL_SPECS:
        pit_flip |= (
            increment_signs[(first_policy, q)]
            != increment_signs[(second_policy, q)]
        )
    for candidate in ("B3_unified", "B3_dual_target"):
        for gate_name in (
            "m1_increment",
            "partial_ic",
            "stability",
            "state_coverage",
        ):
            pit_flip |= (
                aggregate_gate_pass[(first_policy, candidate, gate_name)]
                != aggregate_gate_pass[(second_policy, candidate, gate_name)]
            )
    rows.append(
        _model_comparison_row(
            pit_policy="ALL",
            window="run",
            gate_name="PIT_POLICY_FLIP",
            gate_pass=not pit_flip,
            affects_verdict=True,
        )
    )

    output = pd.DataFrame(rows, columns=MODEL_COMPARISON_COLUMNS)
    if output.empty:
        raise RuntimeError("model comparison unexpectedly produced no rows")
    if output.duplicated(MODEL_ROW_ID_COLUMNS).any():
        raise RuntimeError("model comparison row identity is not unique")
    output["is_in_sample"] = output["is_in_sample"].astype(bool)
    output["affects_verdict"] = output["affects_verdict"].astype(bool)
    gate_mask = output["gate_name"].ne("")
    gate_values = pd.Series(
        np.nan,
        index=output.index,
        dtype=object,
    )
    gate_values.loc[gate_mask] = [
        bool(value) for value in output.loc[gate_mask, "gate_pass"]
    ]
    output["gate_pass"] = gate_values
    return output.sort_values(
        MODEL_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validated_model_comparison_output(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataBlocked("model comparison output must be a nonempty frame")
    if list(frame.columns) != MODEL_COMPARISON_COLUMNS:
        raise DataBlocked("model comparison output schema mismatch")
    output = frame.copy()
    string_columns = [
        "pit_policy",
        "candidate",
        "q",
        "target",
        "window",
        "model",
        "gate_name",
    ]
    for column in string_columns:
        invalid = ~output[column].map(
            lambda value: (
                isinstance(value, str) and value == value.strip()
            )
        )
        if invalid.any():
            raise DataBlocked(
                f"model comparison output.{column} must be canonical strings"
            )
    if output.duplicated(MODEL_ROW_ID_COLUMNS).any():
        raise DataBlocked("model comparison output row identity is duplicated")
    for column in ("is_in_sample", "affects_verdict"):
        invalid = ~output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        )
        if invalid.any():
            raise DataBlocked(
                f"model comparison output.{column} must be boolean"
            )
        output[column] = output[column].astype(bool)
    numeric_columns = [
        "n",
        "oos_r2",
        "spearman_ic",
        "partial_ic",
        "cosine_early_late",
        "confirmation_score_spearman",
        "state_UU_share",
        "state_DD_share",
        "state_DIV_share",
    ]
    for column in numeric_columns:
        output[column] = _optional_finite_numeric(
            output[column],
            f"model comparison output.{column}",
        )
    gate = output["gate_name"].ne("")
    invalid_gate = ~output.loc[gate, "gate_pass"].map(
        lambda value: isinstance(value, (bool, np.bool_))
    )
    if invalid_gate.any() or output.loc[~gate, "gate_pass"].notna().any():
        raise DataBlocked(
            "model comparison gate rows require bool and metric rows require blank"
        )
    if output.loc[gate, "is_in_sample"].any():
        raise DataBlocked("model comparison gate rows cannot be in-sample")
    q_rows = output["q"].ne("")
    expected_mapping = {
        q: (candidate, target)
        for q, (candidate, target) in Q_MODEL_SPECS.items()
    }
    for row in output.loc[q_rows].itertuples(index=False):
        if row.q not in expected_mapping or (
            row.candidate,
            row.target,
        ) != expected_mapping[row.q]:
            raise DataBlocked("model comparison candidate/q/target mismatch")
    metric = ~gate
    if not output.loc[metric, "model"].isin({"M0", "M1"}).all():
        raise DataBlocked("model comparison metrics must be M0/M1 rows")
    if output.loc[
        metric & output["model"].eq("M0"), "partial_ic"
    ].notna().any():
        raise DataBlocked("M0 cannot carry partial IC")
    report = output["window"].eq("2024-2026-report-only")
    if output.loc[report, "affects_verdict"].any():
        raise DataBlocked("report-only model rows cannot affect verdict")
    pit = output["gate_name"].eq("PIT_POLICY_FLIP")
    if (
        int(pit.sum()) != 1
        or output.loc[pit, "pit_policy"].iloc[0] != "ALL"
        or output.loc[pit, "window"].iloc[0] != "run"
    ):
        raise DataBlocked("model comparison requires one ALL/run PIT row")
    gate_values = pd.Series(np.nan, index=output.index, dtype=object)
    gate_values.loc[gate] = [
        bool(value) for value in output.loc[gate, "gate_pass"]
    ]
    output["gate_pass"] = gate_values
    return output.sort_values(
        MODEL_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validated_runner_series(
    values: pd.Series,
    label: str,
    cutoff: pd.Timestamp,
) -> pd.Series:
    if not isinstance(values, pd.Series) or values.empty:
        raise DataBlocked(f"{label} must be a nonempty Series")
    if not isinstance(values.index, pd.DatetimeIndex):
        raise DataBlocked(f"{label} index must be a DatetimeIndex")
    if (
        values.index.tz is not None
        or not values.index.equals(values.index.normalize())
        or values.index.has_duplicates
        or not values.index.is_monotonic_increasing
    ):
        raise DataBlocked(
            f"{label} dates must be unique increasing naive dates"
        )
    numeric = _finite_numeric(values, label)
    return numeric.loc[:cutoff].copy()


def _load_equal_weight_control(
    path: str | Path,
    cutoff: pd.Timestamp,
) -> pd.Series:
    frame = _read_cache(Path(path), "equal_weight control")
    if list(frame.columns) != ["date", "factor_value"]:
        raise DataBlocked("equal_weight control schema mismatch")
    dates = _strict_dates(frame["date"], "equal_weight control.date")
    if dates.duplicated().any():
        raise DataBlocked("equal_weight control contains duplicate dates")
    values = pd.Series(
        frame["factor_value"].to_numpy(),
        index=pd.DatetimeIndex(dates),
        name="factor_value",
    ).sort_index()
    return _validated_runner_series(values, "equal_weight control", cutoff)


@dataclass(frozen=True)
class StructureRunResult:
    surface_path: Path
    coefficients_path: Path
    model_path: Path
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
    model_comparison: pd.DataFrame,
    surface_path: Path,
    coefficients_path: Path,
    model_path: Path,
) -> None:
    surface_path.parent.mkdir(parents=True, exist_ok=True)
    coefficients_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    surface_temp = surface_path.with_name(f".{surface_path.name}.tmp")
    coefficients_temp = coefficients_path.with_name(
        f".{coefficients_path.name}.tmp"
    )
    model_temp = model_path.with_name(f".{model_path.name}.tmp")
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
        model_comparison.to_csv(
            model_temp,
            index=False,
            lineterminator="\n",
        )
        surface_temp.replace(surface_path)
        coefficients_temp.replace(coefficients_path)
        model_temp.replace(model_path)
    except Exception:
        surface_path.unlink(missing_ok=True)
        coefficients_path.unlink(missing_ok=True)
        model_path.unlink(missing_ok=True)
        raise
    finally:
        surface_temp.unlink(missing_ok=True)
        coefficients_temp.unlink(missing_ok=True)
        model_temp.unlink(missing_ok=True)


def _invalidate_structure_outputs(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_structure_manifest(
    manifest_path: Path,
    cfg: dict,
    data_end: pd.Timestamp,
    coefficients_path: Path,
    model_path: Path,
    status: str,
) -> Path:
    """Seal the structure stage so the eval stage can verify before reading."""

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": "structure",
        "config_hash": config_hash(cfg),
        "data_end": str(pd.Timestamp(data_end).date()),
        "status": status,
        "outputs": {
            "structure_coefficients.csv": _sha256_file(coefficients_path),
            "model_comparison.csv": _sha256_file(model_path),
        },
    }
    temp_path = manifest_path.with_name(f".{manifest_path.name}.tmp")
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )
    try:
        temp_path.write_text(rendered, encoding="utf-8")
        temp_path.replace(manifest_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return manifest_path


def run_structure(
    cfg: dict,
    data_end: pd.Timestamp,
    research_output_dir: str | Path = DEFAULT_RESEARCH_OUTPUT_DIR,
    backtest_output_dir: str | Path = DEFAULT_BACKTEST_OUTPUT_DIR,
    *,
    target_returns: dict[str, pd.Series] | None = None,
    equal_weight_signal: pd.Series | None = None,
    underlying_return_loader: Callable[[str], pd.Series] | None = None,
    equal_weight_path: str | Path = DEFAULT_EQUAL_WEIGHT_SIGNAL_PATH,
    model_comparison_builder: Callable[..., pd.DataFrame] | None = None,
) -> StructureRunResult:
    """Verify staged caches and materialize the frozen structure audit."""
    research_root = Path(research_output_dir)
    backtest_root = Path(backtest_output_dir)
    surface_path = research_root / "hard_sort_surface.csv"
    coefficients_path = backtest_root / "structure_coefficients.csv"
    model_path = backtest_root / "model_comparison.csv"
    manifest_path = backtest_root / "structure_manifest.json"
    _invalidate_structure_outputs(
        surface_path,
        coefficients_path,
        model_path,
        manifest_path,
    )
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
    axis, states = _validate_auxiliary_caches(axis, states, cfg, cutoff)

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
    formation_dates = pd.DatetimeIndex(
        sorted(exposures["formation_date"].unique())
    )
    if target_returns is None:
        if underlying_return_loader is None:
            from backtest.data import load_underlying_returns

            underlying_return_loader = load_underlying_returns
        target_returns = {
            target: underlying_return_loader(target)
            for target in ("blend", "500", "1000")
        }
    if not isinstance(target_returns, dict) or set(target_returns) != {
        "blend",
        "500",
        "1000",
    }:
        raise DataBlocked(
            "structure target returns must map blend/500/1000"
        )
    prepared_targets = {
        target: _validated_runner_series(
            target_returns[target],
            f"structure target returns.{target}",
            cutoff,
        )
        for target in ("blend", "500", "1000")
    }
    if equal_weight_signal is None:
        control = _load_equal_weight_control(equal_weight_path, cutoff)
    else:
        control = _validated_runner_series(
            equal_weight_signal,
            "equal_weight control",
            cutoff,
        )
    builder = (
        build_model_comparison
        if model_comparison_builder is None
        else model_comparison_builder
    )
    model_comparison = builder(
        states,
        axis,
        coefficients,
        surface,
        prepared_targets,
        control,
        formation_dates,
        cfg,
    )
    model_comparison = _validated_model_comparison_output(
        model_comparison
    )
    _atomic_write_outputs(
        surface,
        coefficients,
        model_comparison,
        surface_path,
        coefficients_path,
        model_path,
    )
    blocked = surface.loc[
        surface["row_type"].eq("cell"),
        "status",
    ].eq("COVERAGE_BLOCKED").any()
    status = "COVERAGE_BLOCKED" if blocked else "OK"
    _write_structure_manifest(
        manifest_path,
        cfg,
        cutoff,
        coefficients_path,
        model_path,
        status,
    )
    return StructureRunResult(
        surface_path=surface_path,
        coefficients_path=coefficients_path,
        model_path=model_path,
        status=status,
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
