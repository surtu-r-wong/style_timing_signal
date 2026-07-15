"""Causal UU/DD/DIV state features for B3 conditional style legs."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from signals.style_basket.b3_exposures import DataBlocked


def _positive_integer(value, label: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, np.integer),
    ):
        raise DataBlocked(f"{label} must be a positive integer")
    if int(value) <= 0:
        raise DataBlocked(f"{label} must be a positive integer")
    return int(value)


def _positive_finite(value, label: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise DataBlocked(f"{label} must be positive and finite")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} must be positive and finite") from exc
    if not np.isfinite(numeric) or numeric <= 0.0:
        raise DataBlocked(f"{label} must be positive and finite")
    return numeric


def _nonnegative_finite(value, label: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise DataBlocked(f"{label} must be nonnegative and finite")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(
            f"{label} must be nonnegative and finite"
        ) from exc
    if not np.isfinite(numeric) or numeric < 0.0:
        raise DataBlocked(f"{label} must be nonnegative and finite")
    return numeric


def _validate_leg_returns(legs: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(legs, pd.DataFrame) or legs.empty:
        raise DataBlocked("state legs must be a nonempty DataFrame")
    if legs.columns.has_duplicates:
        raise DataBlocked("state legs contain duplicate columns")
    required = ["growth_ret", "value_ret"]
    missing = [column for column in required if column not in legs]
    if missing:
        raise DataBlocked(
            "state legs require growth_ret and value_ret"
        )
    contains_boolean = any(
        legs[column]
        .map(lambda value: isinstance(value, (bool, np.bool_)))
        .any()
        for column in required
    )
    if contains_boolean:
        raise DataBlocked("state leg returns cannot be boolean")
    if not all(is_numeric_dtype(legs[column]) for column in required):
        raise DataBlocked("state leg returns must be numeric")
    values = legs[required].to_numpy(dtype=float, na_value=np.nan)
    if not np.isfinite(values).all():
        raise DataBlocked("state leg returns must be finite and nonmissing")
    if (values <= -1.0).any():
        raise DataBlocked(
            "state leg returns must be greater than -100%"
        )
    return legs.copy()


def decompose_states(
    legs: pd.DataFrame,
    tolerance: float = 1.0e-12,
) -> pd.DataFrame:
    """Split growth-minus-value log returns into exhaustive daily states."""
    identity_tolerance = _nonnegative_finite(
        tolerance,
        "state identity tolerance",
    )
    out = _validate_leg_returns(legs)
    out["g"] = np.log1p(out["growth_ret"])
    out["v"] = np.log1p(out["value_ret"])
    if not np.isfinite(out[["g", "v"]].to_numpy(dtype=float)).all():
        raise DataBlocked("state leg log returns must be finite")

    out["d"] = out["g"] - out["v"]
    uu = out["g"].ge(0.0) & out["v"].ge(0.0)
    dd = out["g"].lt(0.0) & out["v"].lt(0.0)
    out["d_UU"] = out["d"].where(uu, 0.0)
    out["d_DD"] = out["d"].where(dd, 0.0)
    out["d_DIV"] = out["d"] - out["d_UU"] - out["d_DD"]
    out["state"] = np.select(
        [uu, dd],
        ["UU", "DD"],
        default="DIV",
    )
    error = (
        out["d"] - out["d_UU"] - out["d_DD"] - out["d_DIV"]
    ).abs().max()
    if not np.isfinite(error) or error > identity_tolerance:
        raise RuntimeError(
            "state identity error "
            f"{error} exceeds {identity_tolerance}"
        )
    return out


def _causal_transform(
    component: pd.Series,
    raw_window: int,
    z_window: int,
    tanh_scale: float,
    smoothing_window: int,
) -> tuple[pd.Series, pd.Series]:
    raw = component.rolling(
        raw_window,
        min_periods=raw_window,
    ).sum()
    mean = raw.rolling(
        z_window,
        min_periods=z_window,
    ).mean()
    standard_deviation = raw.rolling(
        z_window,
        min_periods=z_window,
    ).std(ddof=1)
    z_score = (raw - mean) / standard_deviation.where(
        standard_deviation >= 1.0e-8
    )
    transformed = np.tanh(z_score / tanh_scale)
    smoothed = transformed.rolling(
        smoothing_window,
        min_periods=smoothing_window,
    ).mean()
    return raw, smoothed


def _validate_feature_index(legs: pd.DataFrame) -> None:
    if not isinstance(legs.index, pd.DatetimeIndex):
        raise DataBlocked("state feature index must be a DatetimeIndex")
    if legs.index.tz is not None:
        raise DataBlocked("state feature dates must be timezone naive")
    if not legs.index.equals(legs.index.normalize()):
        raise DataBlocked("state feature dates must be midnight dates")
    if legs.index.has_duplicates:
        raise DataBlocked("state feature dates must be unique")
    if not legs.index.is_monotonic_increasing:
        raise DataBlocked(
            "state feature dates must be strictly increasing"
        )


def build_state_features(legs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Build the frozen 20/40/tanh/5 features using current and past rows."""
    if not isinstance(cfg, dict):
        raise DataBlocked("state feature config must be a mapping")
    signal_cfg = cfg.get("signal")
    exposure_cfg = cfg.get("exposure")
    if not isinstance(signal_cfg, dict) or not isinstance(
        exposure_cfg,
        dict,
    ):
        raise DataBlocked("state feature config is incomplete")
    raw_window = _positive_integer(
        signal_cfg.get("raw_window"),
        "signal.raw_window",
    )
    z_window = _positive_integer(
        signal_cfg.get("z_window"),
        "signal.z_window",
    )
    smoothing_window = _positive_integer(
        signal_cfg.get("smoothing_window"),
        "signal.smoothing_window",
    )
    tanh_scale = _positive_finite(
        signal_cfg.get("tanh_scale"),
        "signal.tanh_scale",
    )
    tolerance = _nonnegative_finite(
        exposure_cfg.get("identity_tolerance"),
        "exposure.identity_tolerance",
    )
    _validate_feature_index(legs)
    out = decompose_states(legs, tolerance=tolerance)
    mapping = {
        "U": "d_UU",
        "D": "d_DD",
        "X": "d_DIV",
        "T": "d",
    }
    for suffix, source in mapping.items():
        raw, feature = _causal_transform(
            out[source],
            raw_window,
            z_window,
            tanh_scale,
            smoothing_window,
        )
        out[f"raw_{suffix}"] = raw
        out[f"F_{suffix}"] = feature
    return out
