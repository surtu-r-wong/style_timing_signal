"""Natural-drift holding-period returns for B3 research portfolios."""

from __future__ import annotations

import numpy as np
import pandas as pd

from signals.style_basket.b3_exposures import DataBlocked


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
    flags = (
        suspended.reindex(
            index=days,
            columns=members,
            fill_value=False,
        )
        .fillna(False)
        .astype(bool)
    )
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
    return panel.mask(flags & panel.isna(), 0.0)


def natural_drift_leg_returns(
    initial_weights: pd.Series,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    weights = (
        initial_weights[initial_weights > 0]
        .astype(float)
        .sort_index()
    )
    if not np.isclose(weights.sum(), 1.0, atol=1.0e-10):
        raise ValueError("initial leg weights must sum to one")
    days = returns.index[
        (returns.index > formation_date)
        & (returns.index <= end_date)
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


def scheduled_portfolio_returns(
    schedule: list[tuple[pd.Timestamp, pd.Series]],
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> pd.Series:
    if not schedule:
        return pd.Series(dtype=float)
    ordered = sorted(schedule, key=lambda item: item[0])
    pieces = []
    for number, (formation, weights) in enumerate(ordered):
        end = (
            ordered[number + 1][0]
            if number + 1 < len(ordered)
            else returns.index.max()
        )
        pieces.append(
            natural_drift_leg_returns(
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


def stock_period_returns(
    members: pd.Index,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
    formation_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.Series:
    days = returns.index[
        (returns.index > formation_date)
        & (returns.index <= end_date)
    ]
    legal = _legal_returns(
        members,
        pd.DatetimeIndex(days),
        returns,
        suspended,
    )
    if (legal <= -1.0).any().any():
        raise DataBlocked(
            "stock return is less than or equal to -100%"
        )
    return (1.0 + legal).prod(axis=0) - 1.0


def build_portfolio_panels(
    exposures: pd.DataFrame,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    parsed_dates = pd.to_datetime(
        frame["formation_date"],
        errors="coerce",
        format="mixed",
    )
    if parsed_dates.isna().any():
        raise DataBlocked(
            "monthly exposures contain invalid formation dates"
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
                schedules[key].append(
                    (
                        pd.Timestamp(formation),
                        month[f"w_{key}"],
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
            period = stock_period_returns(
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
            key: scheduled_portfolio_returns(
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
        raise DataBlocked("portfolio panel assembly returned no rows")
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
    return axis_panel, leg_panel, stock_panel
