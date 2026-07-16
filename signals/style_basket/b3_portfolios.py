"""Natural-drift holding-period returns for B3 research portfolios."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from signals.style_basket.b3_exposures import DataBlocked


def _validated_date(value, label: str) -> pd.Timestamp:
    if isinstance(value, (bool, int, float, np.number)):
        raise DataBlocked(f"{label} is not a valid date")
    try:
        date = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} is not a valid date") from exc
    if pd.isna(date) or date.tz is not None or date != date.normalize():
        raise DataBlocked(f"{label} is not a naive midnight date")
    return date


def validate_return_panels(
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    *,
    data_end: pd.Timestamp | None = None,
) -> None:
    """Validate the shared daily axes and fail closed on observed bad data."""
    if not isinstance(returns, pd.DataFrame) or returns.empty:
        raise DataBlocked("return panel must be a nonempty DataFrame")
    if not isinstance(suspended, pd.DataFrame) or suspended.empty:
        raise DataBlocked(
            "return panel suspension flags must be a nonempty DataFrame"
        )
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise DataBlocked("return panel index must be a DatetimeIndex")
    if not isinstance(suspended.index, pd.DatetimeIndex):
        raise DataBlocked(
            "return panel suspension index must be a DatetimeIndex"
        )
    if returns.index.tz is not None or suspended.index.tz is not None:
        raise DataBlocked("return panel dates must be timezone naive")
    if (
        not returns.index.equals(returns.index.normalize())
        or not suspended.index.equals(suspended.index.normalize())
    ):
        raise DataBlocked("return panel dates must be midnight dates")
    if (
        returns.index.has_duplicates
        or suspended.index.has_duplicates
        or returns.columns.has_duplicates
        or suspended.columns.has_duplicates
    ):
        raise DataBlocked("return panel axes contain duplicate keys")
    if (
        not returns.index.is_monotonic_increasing
        or not suspended.index.is_monotonic_increasing
    ):
        raise DataBlocked("return panel dates must be strictly increasing")
    if (
        not returns.index.equals(suspended.index)
        or not returns.columns.equals(suspended.columns)
    ):
        raise DataBlocked(
            "return panel and suspension panel axes do not match"
        )
    invalid_tickers = [
        ticker
        for ticker in returns.columns
        if not isinstance(ticker, str) or not ticker.strip()
    ]
    if invalid_tickers:
        raise DataBlocked(
            "return panel columns must be nonempty string tickers"
        )
    if not all(is_numeric_dtype(dtype) for dtype in returns.dtypes):
        raise DataBlocked("return panel values must be numeric")
    if not all(is_bool_dtype(dtype) for dtype in suspended.dtypes):
        raise DataBlocked(
            "return panel suspension values must be boolean"
        )
    if suspended.isna().any().any():
        raise DataBlocked(
            "return panel suspension values must be nonmissing booleans"
        )

    values = returns.to_numpy(dtype=float, na_value=np.nan)
    observed = ~np.isnan(values)
    if not np.isfinite(values[observed]).all():
        raise DataBlocked("observed return panel values must be finite")
    if (values[observed] <= -1.0).any():
        raise DataBlocked(
            "observed return panel values must be greater than -100%"
        )
    if data_end is not None:
        cutoff = _validated_date(data_end, "data_end")
        if returns.index.max() > cutoff:
            raise DataBlocked("return panel contains dates after data_end")


def _validated_weights(initial_weights: pd.Series) -> pd.Series:
    if not isinstance(initial_weights, pd.Series):
        raise DataBlocked("initial leg weights must be a Series")
    if initial_weights.index.has_duplicates:
        raise DataBlocked("initial leg weight tickers must be unique")
    invalid_tickers = [
        ticker
        for ticker in initial_weights.index
        if not isinstance(ticker, str) or not ticker.strip()
    ]
    if invalid_tickers:
        raise DataBlocked(
            "initial leg weight tickers must be nonempty strings"
        )
    if initial_weights.map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).any():
        raise DataBlocked("initial leg weights cannot be booleans")
    numeric = pd.to_numeric(initial_weights, errors="coerce")
    if numeric.isna().any() or not np.isfinite(numeric).all():
        raise DataBlocked("initial leg weights must be finite numbers")
    if (numeric < 0.0).any():
        raise DataBlocked("initial leg weights must be nonnegative")
    weights = numeric[numeric > 0.0].astype(float).sort_index()
    if not np.isclose(
        weights.sum(),
        1.0,
        rtol=0.0,
        atol=1.0e-10,
    ):
        raise ValueError("initial leg weights must sum to one")
    return weights


def _validated_members(members: pd.Index) -> pd.Index:
    if not isinstance(members, pd.Index):
        raise DataBlocked("stock period members must be an Index")
    if len(members) == 0:
        raise DataBlocked("stock period members cannot be empty")
    if members.has_duplicates:
        raise DataBlocked("stock period members must be unique")
    invalid = [
        member
        for member in members
        if not isinstance(member, str) or not member.strip()
    ]
    if invalid:
        raise DataBlocked(
            "stock period members must be nonempty string tickers"
        )
    return members


def _legal_returns(
    members: pd.Index,
    days: pd.DatetimeIndex,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> pd.DataFrame:
    missing_columns = members.difference(returns.columns)
    if len(missing_columns):
        raise DataBlocked(
            "held tickers absent from return panel: "
            f"{list(missing_columns[:5])}"
        )
    panel = returns.reindex(
        index=days,
        columns=members,
    ).astype(float)
    flags = suspended.reindex(
        index=days,
        columns=members,
    )
    if flags.isna().any().any() or not all(
        is_bool_dtype(dtype) for dtype in flags.dtypes
    ):
        raise DataBlocked("suspension flags must be nonmissing booleans")
    unexplained = panel.isna() & ~flags
    if unexplained.any().any():
        day, ticker = (
            unexplained.stack()
            .loc[lambda values: values]
            .index[0]
        )
        raise DataBlocked(
            f"unexplained price gap for {ticker} on {day.date()}"
        )
    legal = panel.mask(flags & panel.isna(), 0.0)
    values = legal.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise DataBlocked("observed stock returns must be finite")
    if (values <= -1.0).any():
        raise DataBlocked(
            "observed stock returns must be greater than -100%"
        )
    return legal


def _natural_drift_leg_returns_validated(
    initial_weights: pd.Series,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    weights = _validated_weights(initial_weights)
    formation = _validated_date(formation_date, "formation_date")
    end = _validated_date(end_date, "end_date")
    if end < formation:
        raise DataBlocked("end_date cannot precede formation_date")
    if formation not in returns.index:
        raise DataBlocked("portfolio formation date is absent from return panel")
    days = returns.index[
        (returns.index > formation) & (returns.index <= end)
    ]
    legal = _legal_returns(
        weights.index,
        pd.DatetimeIndex(days),
        returns,
        suspended,
    )
    values = weights.copy()
    output: dict[pd.Timestamp, float] = {}
    for day in days:
        before = float(values.sum())
        values = values * (1.0 + legal.loc[day])
        after = float(values.sum())
        if not np.isfinite(after) or after <= 0:
            raise DataBlocked(
                f"non-positive leg value on {pd.Timestamp(day).date()}"
            )
        output[pd.Timestamp(day)] = after / before - 1.0
    return pd.Series(output, dtype=float)


def natural_drift_leg_returns(
    initial_weights: pd.Series,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    validate_return_panels(returns, suspended)
    return _natural_drift_leg_returns_validated(
        initial_weights,
        returns,
        suspended,
        formation_date,
        end_date,
    )


def _scheduled_portfolio_returns_validated(
    schedule: list[tuple[pd.Timestamp, pd.Series]],
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> pd.Series:
    if not schedule:
        return pd.Series(dtype=float)
    ordered = [
        (_validated_date(formation, "formation_date"), weights)
        for formation, weights in schedule
    ]
    ordered.sort(key=lambda item: item[0])
    formation_dates = [item[0] for item in ordered]
    if pd.Index(formation_dates).has_duplicates:
        raise DataBlocked("portfolio schedule has duplicate formation dates")
    missing_formations = [
        formation
        for formation in formation_dates
        if formation not in returns.index
    ]
    if missing_formations:
        raise DataBlocked(
            "portfolio formation date is absent from return panel: "
            f"{missing_formations[0].date()}"
        )
    pieces = []
    for number, (formation, weights) in enumerate(ordered):
        end = (
            ordered[number + 1][0]
            if number + 1 < len(ordered)
            else returns.index.max()
        )
        pieces.append(
            _natural_drift_leg_returns_validated(
                weights,
                returns,
                suspended,
                formation,
                end,
            )
        )
    result = pd.concat(pieces).sort_index()
    if result.index.duplicated().any():
        raise RuntimeError("holding periods overlap")
    return result


def scheduled_portfolio_returns(
    schedule: list[tuple[pd.Timestamp, pd.Series]],
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> pd.Series:
    validate_return_panels(returns, suspended)
    return _scheduled_portfolio_returns_validated(
        schedule,
        returns,
        suspended,
    )


def _stock_period_returns_validated(
    members: pd.Index,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    members = _validated_members(members)
    formation = _validated_date(formation_date, "formation_date")
    end = _validated_date(end_date, "end_date")
    if end < formation:
        raise DataBlocked("end_date cannot precede formation_date")
    if formation not in returns.index:
        raise DataBlocked("portfolio formation date is absent from return panel")
    days = returns.index[
        (returns.index > formation) & (returns.index <= end)
    ]
    if len(days) == 0:
        raise DataBlocked("stock holding period has no return dates")
    legal = _legal_returns(
        members,
        pd.DatetimeIndex(days),
        returns,
        suspended,
    )
    return (1.0 + legal).prod(axis=0) - 1.0


def stock_period_returns(
    members: pd.Index,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    validate_return_panels(returns, suspended)
    return _stock_period_returns_validated(
        members,
        returns,
        suspended,
        formation_date,
        end_date,
    )


def build_portfolio_panels(
    exposures: pd.DataFrame,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    *,
    data_end: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    validate_return_panels(
        returns,
        suspended,
        data_end=data_end,
    )
    axes = [
        "style",
        "size",
        "interaction",
        "qblend",
        "q500",
        "q1000",
    ]
    required = {
        "pit_policy",
        "formation_date",
        "ticker",
        "universe_role",
        *{
            f"w_{axis}_{side}"
            for axis in axes
            for side in ("plus", "minus")
        },
    }
    if not isinstance(exposures, pd.DataFrame) or exposures.empty:
        raise DataBlocked("monthly exposures must be a nonempty DataFrame")
    missing = sorted(required.difference(exposures.columns))
    if missing:
        raise DataBlocked(
            "monthly exposures are missing columns: "
            + ", ".join(missing)
        )

    frame = exposures.copy()
    parsed_dates = pd.Series(
        [
            _validated_date(
                value,
                "monthly exposures formation_date",
            )
            for value in frame["formation_date"]
        ],
        index=frame.index,
        dtype="datetime64[ns]",
    )
    frame["formation_date"] = parsed_dates
    for column in ("pit_policy", "ticker", "universe_role"):
        invalid = ~frame[column].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        )
        if invalid.any():
            raise DataBlocked(
                f"monthly exposures contain invalid {column} values"
            )
    allowed_roles = {"model", "size_only"}
    if not frame["universe_role"].isin(allowed_roles).all():
        raise DataBlocked(
            "monthly exposures contain unsupported universe_role values"
        )
    if data_end is not None:
        cutoff = _validated_date(data_end, "data_end")
        if frame["formation_date"].max() > cutoff:
            raise DataBlocked(
                "monthly exposures contain formation dates after data_end"
            )

    for axis in axes:
        eligible = (
            pd.Series(True, index=frame.index)
            if axis == "size"
            else frame["universe_role"].eq("model")
        )
        for side in ("plus", "minus"):
            column = f"w_{axis}_{side}"
            original = frame[column]
            if original.map(
                lambda value: isinstance(value, (bool, np.bool_))
            ).any():
                raise DataBlocked(
                    f"monthly exposures contain boolean {column} weights"
                )
            numeric = pd.to_numeric(original, errors="coerce")
            invalid_numeric = original.notna() & numeric.isna()
            finite = numeric.notna() & np.isfinite(numeric)
            if invalid_numeric.any() or (
                numeric.notna() & ~finite
            ).any():
                raise DataBlocked(
                    f"monthly exposures contain invalid {column} weights"
                )
            if numeric[eligible].isna().any():
                raise DataBlocked(
                    f"monthly exposures contain missing {column} weights"
                )
            if (numeric[eligible] < 0.0).any():
                raise DataBlocked(
                    f"monthly exposures contain negative {column} weights"
                )
            excluded_nonzero = (
                ~eligible
                & numeric.notna()
                & numeric.ne(0.0)
            )
            if excluded_nonzero.any():
                raise DataBlocked(
                    f"size_only rows contain nonzero {column} weights"
                )
            frame[column] = numeric
    identity = ["pit_policy", "formation_date", "ticker"]
    if frame.duplicated(identity).any():
        raise DataBlocked("monthly exposures contain duplicate keys")
    frame = frame.sort_values(identity, kind="mergesort")

    axis_parts: list[pd.DataFrame] = []
    leg_parts: list[pd.DataFrame] = []
    stock_parts: list[pd.DataFrame] = []
    for policy, policy_frame in frame.groupby(
        "pit_policy",
        sort=True,
    ):
        formations = sorted(
            pd.DatetimeIndex(
                policy_frame["formation_date"].unique()
            )
        )
        schedules: dict[
            str,
            list[tuple[pd.Timestamp, pd.Series]],
        ] = {
            f"{axis}_{side}": []
            for axis in axes
            for side in ("plus", "minus")
        }
        for number, formation in enumerate(formations):
            month = (
                policy_frame[
                    policy_frame["formation_date"].eq(formation)
                ]
                .sort_values("ticker", kind="mergesort")
                .set_index("ticker")
            )
            for key in schedules:
                axis_name, _ = key.rsplit("_", maxsplit=1)
                eligible = (
                    pd.Series(True, index=month.index)
                    if axis_name == "size"
                    else month["universe_role"].eq("model")
                )
                schedules[key].append(
                    (
                        pd.Timestamp(formation),
                        month.loc[eligible, f"w_{key}"],
                    )
                )
            end = (
                formations[number + 1]
                if number + 1 < len(formations)
                else returns.index.max()
            )
            model_members = pd.Index(
                month.index[
                    month["universe_role"].eq("model")
                ]
            ).sort_values()
            if len(model_members) == 0:
                raise DataBlocked(
                    f"no model members for {policy} on "
                    f"{pd.Timestamp(formation).date()}"
                )
            if number + 1 == len(formations):
                continue
            holding_days = returns.index[
                (returns.index > formation) & (returns.index <= end)
            ]
            if len(holding_days) == 0:
                if number + 1 < len(formations):
                    raise DataBlocked(
                        "non-final stock holding period has no return dates"
                    )
                continue
            period = _stock_period_returns_validated(
                model_members,
                returns,
                suspended,
                pd.Timestamp(formation),
                pd.Timestamp(end),
            )
            stock_parts.append(
                pd.DataFrame(
                    {
                        "pit_policy": policy,
                        "formation_date": pd.Timestamp(formation),
                        "ticker": period.index,
                        "forward_return": period.to_numpy(),
                    }
                )
            )

        daily = {
            key: _scheduled_portfolio_returns_validated(
                schedule,
                returns,
                suspended,
            )
            for key, schedule in schedules.items()
        }
        axis = pd.DataFrame(
            {
                "style": (
                    daily["style_plus"] - daily["style_minus"]
                ),
                "size": daily["size_plus"] - daily["size_minus"],
                "interaction": (
                    daily["interaction_plus"]
                    - daily["interaction_minus"]
                ),
            }
        )
        axis.insert(0, "pit_policy", policy)
        axis_parts.append(
            axis.rename_axis("date").reset_index()
        )
        for q in ("qblend", "q500", "q1000"):
            growth = daily[f"{q}_plus"]
            value = daily[f"{q}_minus"].reindex(growth.index)
            leg_parts.append(
                pd.DataFrame(
                    {
                        "date": growth.index,
                        "pit_policy": policy,
                        "q": q,
                        "growth_ret": growth.to_numpy(),
                        "value_ret": value.to_numpy(),
                    }
                )
            )

    if not axis_parts or not leg_parts or not stock_parts:
        raise DataBlocked("portfolio assembly has no portfolio return rows")
    axis_panel = (
        pd.concat(axis_parts, ignore_index=True)
        .sort_values(
            ["pit_policy", "date"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )
    leg_panel = (
        pd.concat(leg_parts, ignore_index=True)
        .sort_values(
            ["pit_policy", "q", "date"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )
    stock_panel = (
        pd.concat(stock_parts, ignore_index=True)
        .sort_values(
            ["pit_policy", "formation_date", "ticker"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )
    if axis_panel.empty or leg_panel.empty or stock_panel.empty:
        raise DataBlocked("portfolio assembly has no portfolio return rows")
    identities = [
        (axis_panel, ["pit_policy", "date"], "axis"),
        (leg_panel, ["pit_policy", "q", "date"], "conditional leg"),
        (
            stock_panel,
            ["pit_policy", "formation_date", "ticker"],
            "stock period",
        ),
    ]
    for panel, keys, label in identities:
        if panel.duplicated(keys).any():
            raise DataBlocked(f"{label} output contains duplicate keys")
    numeric_outputs = [
        (axis_panel, ["style", "size", "interaction"], "axis"),
        (
            leg_panel,
            ["growth_ret", "value_ret"],
            "conditional leg",
        ),
        (stock_panel, ["forward_return"], "stock period"),
    ]
    for panel, columns, label in numeric_outputs:
        values = panel[columns].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise DataBlocked(f"{label} output contains non-finite returns")
    return axis_panel, leg_panel, stock_panel
