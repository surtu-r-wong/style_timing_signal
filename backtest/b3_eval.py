"""Pure same-scale production evaluation for frozen B3 candidates."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Any, TypedDict, cast

import numpy as np
import pandas as pd

from backtest.engine import run_strategy
from backtest.b3_structure import (
    MODEL_COMPARISON_COLUMNS,
    MODEL_ROW_ID_COLUMNS,
    apply_model,
    fit_model,
    next_formation_targets,
)
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover
from backtest.positions import production_position
from backtest.rotation_probe import partial_rank_ic
from signals.style_basket.b3_exposures import DataBlocked


PRODUCTION_METRICS_COLUMNS = [
    "pit_policy",
    "candidate",
    "component",
    "is_candidate",
    "window",
    "executable",
    "n_obs",
    "ann_return",
    "sharpe",
    "maxdd",
    "turnover",
    "baseline_sharpe",
    "sharpe_difference",
    "baseline_maxdd",
    "maxdd_difference",
    "baseline_turnover",
    "turnover_ratio",
    "partial_ic",
    "gate_name",
    "gate_pass",
    "affects_verdict",
]
PRODUCTION_ROW_ID_COLUMNS = [
    "pit_policy",
    "candidate",
    "component",
    "window",
    "gate_name",
]
BOOTSTRAP_COLUMNS = [
    "pit_policy",
    "candidate",
    "draws",
    "block_days",
    "seed",
    "tail_prob",
    "holm_adjusted_tail",
    "ci05",
    "ci50",
    "ci95",
    "structure_pass",
    "gate_pass",
]
BOOTSTRAP_ROW_ID_COLUMNS = ["pit_policy", "candidate"]
YEARLY_COLUMNS = [
    "pit_policy",
    "candidate",
    "window",
    "row_type",
    "year",
    "excluded_year",
    "n_obs",
    "signed_log_pnl",
    "absolute_pnl_share",
    "strongest_year",
    "ann_return",
    "sharpe",
    "maxdd",
    "is_in_sample",
    "affects_verdict",
]
YEARLY_ROW_ID_COLUMNS = [
    "pit_policy",
    "candidate",
    "window",
    "row_type",
    "year",
    "excluded_year",
]


@dataclass(frozen=True)
class EvaluationFrames:
    production_metrics: pd.DataFrame
    bootstrap: pd.DataFrame
    yearly: pd.DataFrame


class EvaluationSettings(TypedDict):
    confirmation: tuple[pd.Timestamp, pd.Timestamp]
    report: tuple[pd.Timestamp, pd.Timestamp]
    ic_launch: pd.Timestamp
    im_launch: pd.Timestamp
    cost_bps: float
    sharpe_improvement: float
    maxdd_worsening: float
    turnover_multiple: float
    post_im_min_days: int
    block_days: int
    draws: int
    seed: int
    tail_threshold: float


def _validate_datetime_index(index: pd.Index, label: str) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise DataBlocked(f"{label} index must be a DatetimeIndex")
    if index.empty:
        raise DataBlocked(f"{label} index must not be empty")
    if index.tz is not None:
        raise DataBlocked(f"{label} index must be timezone-naive")
    if not index.equals(index.normalize()):
        raise DataBlocked(f"{label} index must contain normalized dates")
    if not index.is_unique:
        raise DataBlocked(f"{label} index must be unique")
    if not index.is_monotonic_increasing:
        raise DataBlocked(f"{label} index must be strictly increasing")
    return index


def _validate_finite_series(
    values: pd.Series,
    label: str,
    *,
    returns: bool = False,
    positions: bool = False,
) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise DataBlocked(f"{label} must be a Series")
    _validate_datetime_index(values.index, label)
    try:
        array = values.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise DataBlocked(f"{label} must be numeric") from exc
    if not np.isfinite(array).all():
        raise DataBlocked(f"{label} must contain only finite values")
    if returns and np.any(array <= -1.0):
        raise DataBlocked(f"{label} returns must be greater than -100%")
    if positions and not np.isin(array, [0.0, 1.0]).all():
        raise DataBlocked(f"{label} must contain only long-flat positions")
    return pd.Series(array, index=values.index, name=values.name, dtype=float)


def _require_same_grid(reference: pd.DatetimeIndex, values: pd.Series, label: str) -> None:
    if not values.index.equals(reference):
        raise DataBlocked(f"{label} must exactly match the benchmark calendar")


def materialize_carry(
    raw: pd.Series,
    calendar: pd.DatetimeIndex,
    launch_date: pd.Timestamp,
) -> pd.Series:
    calendar = _validate_datetime_index(calendar, "cash calendar")
    raw = _validate_finite_series(raw, "raw carry")
    if not isinstance(launch_date, pd.Timestamp):
        raise DataBlocked("carry launch date must be a Timestamp")
    if launch_date.tz is not None or launch_date != launch_date.normalize():
        raise DataBlocked("carry launch date must be timezone-naive and normalized")
    if not raw.index.isin(calendar).all():
        raise DataBlocked("raw carry contains dates outside the cash calendar")

    post_launch_calendar = calendar[calendar >= launch_date]
    observed_post_launch = raw.index[raw.index >= launch_date]
    if len(post_launch_calendar) and not len(observed_post_launch):
        raise DataBlocked("raw carry has no post-launch observations")

    common_end = raw.index.max()
    usable_calendar = calendar[calendar <= common_end]
    result = raw.reindex(usable_calendar)
    pre_launch = usable_calendar < launch_date
    result.loc[pre_launch] = 0.0
    if result.loc[~pre_launch].isna().any():
        raise DataBlocked("internal post-launch carry gap")
    return result.astype(float).rename(raw.name)


def two_leg_candidate_returns(
    position_500: pd.Series,
    position_1000: pd.Series,
    return_500: pd.Series,
    return_1000: pd.Series,
    carry_500: pd.Series,
    carry_1000: pd.Series,
    cost_bps: float,
) -> pd.Series:
    if isinstance(cost_bps, (bool, np.bool_)):
        raise DataBlocked("cost_bps must be a finite nonnegative number")
    try:
        cost = float(cost_bps)
    except (TypeError, ValueError) as exc:
        raise DataBlocked("cost_bps must be a finite nonnegative number") from exc
    if not np.isfinite(cost) or cost < 0.0:
        raise DataBlocked("cost_bps must be a finite nonnegative number")

    return_500 = _validate_finite_series(return_500, "500 cash returns", returns=True)
    return_1000 = _validate_finite_series(
        return_1000, "1000 cash returns", returns=True
    )
    position_500 = _validate_finite_series(
        position_500, "500 positions", positions=True
    )
    position_1000 = _validate_finite_series(
        position_1000, "1000 positions", positions=True
    )
    carry_500 = _validate_finite_series(carry_500, "500 carry")
    carry_1000 = _validate_finite_series(carry_1000, "1000 carry")

    calendar = return_500.index
    for values, label in [
        (return_1000, "1000 cash returns"),
        (position_500, "500 positions"),
        (position_1000, "1000 positions"),
        (carry_500, "500 carry"),
        (carry_1000, "1000 carry"),
    ]:
        _require_same_grid(calendar, values, label)

    leg_500 = run_strategy(position_500, return_500, cost, carry_500)["ret"]
    leg_1000 = run_strategy(position_1000, return_1000, cost, carry_1000)["ret"]
    combined = (0.5 * leg_500 + 0.5 * leg_1000).rename("ret")
    if not np.isfinite(combined.to_numpy(dtype=float)).all():
        raise DataBlocked("candidate returns are non-finite")
    return combined


def paired_moving_block_tail(
    candidate: pd.Series,
    baseline: pd.Series,
    block_days: int,
    draws: int,
    seed: int,
) -> dict[str, float]:
    candidate = _validate_finite_series(
        candidate, "candidate bootstrap returns", returns=True
    )
    baseline = _validate_finite_series(
        baseline, "baseline bootstrap returns", returns=True
    )
    _require_same_grid(candidate.index, baseline, "baseline bootstrap returns")

    for value, label, allow_zero in [
        (block_days, "block_days", False),
        (draws, "draws", False),
        (seed, "seed", True),
    ]:
        if (
            isinstance(value, (bool, np.bool_))
            or not isinstance(value, Integral)
            or (value < 0 if allow_zero else value <= 0)
        ):
            qualifier = "nonnegative" if allow_zero else "positive"
            raise ValueError(f"{label} must be a {qualifier} integer")
    if block_days > len(candidate):
        raise ValueError("paired bootstrap sample shorter than block")

    values = np.column_stack(
        [
            candidate.to_numpy(dtype=float),
            baseline.to_numpy(dtype=float),
        ]
    )
    n_obs = len(values)
    starts = np.arange(n_obs - block_days + 1)
    blocks_per_draw = int(np.ceil(n_obs / block_days))
    rng = np.random.default_rng(seed)
    differences: np.ndarray = np.empty(draws, dtype=float)
    for draw in range(draws):
        selected = rng.choice(starts, size=blocks_per_draw, replace=True)
        sample = np.concatenate(
            [values[start : start + block_days] for start in selected],
            axis=0,
        )[:n_obs]
        differences[draw] = sharpe(pd.Series(sample[:, 0])) - sharpe(
            pd.Series(sample[:, 1])
        )
    if not np.isfinite(differences).all():
        raise DataBlocked("paired bootstrap produced non-finite Sharpe differences")
    return {
        "tail_prob": float(
            (1 + np.count_nonzero(differences <= 0.0)) / (draws + 1)
        ),
        "ci05": float(np.quantile(differences, 0.05)),
        "ci50": float(np.quantile(differences, 0.50)),
        "ci95": float(np.quantile(differences, 0.95)),
    }


def holm_style_adjust(raw: dict[str, float]) -> dict[str, float]:
    family = {"B3_unified", "B3_dual_target"}
    if not isinstance(raw, dict) or set(raw) != family:
        raise ValueError("Holm-style family must contain exactly two candidates")
    checked: dict[str, float] = {}
    for candidate, value in raw.items():
        if isinstance(value, (bool, np.bool_)):
            raise ValueError("Holm-style tail probabilities must be finite in [0, 1]")
        try:
            probability = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Holm-style tail probabilities must be finite in [0, 1]"
            ) from exc
        if not np.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(
                "Holm-style tail probabilities must be finite in [0, 1]"
            )
        checked[candidate] = probability

    first, second = sorted(
        checked,
        key=lambda candidate: (checked[candidate], candidate),
    )
    first_adjusted = min(1.0, 2.0 * checked[first])
    second_adjusted = min(1.0, max(first_adjusted, checked[second]))
    return {first: first_adjusted, second: second_adjusted}


def passes_tail_gate(adjusted_tail: float, threshold: float) -> bool:
    checked: list[float] = []
    for value, label in [
        (adjusted_tail, "adjusted tail probability"),
        (threshold, "tail threshold"),
    ]:
        if isinstance(value, (bool, np.bool_)):
            raise ValueError(f"{label} must be finite")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be finite") from exc
        if not np.isfinite(numeric):
            raise ValueError(f"{label} must be finite")
        checked.append(numeric)
    probability, cutoff = checked
    if not 0.0 <= probability <= 1.0:
        raise ValueError("adjusted tail probability must be in [0, 1]")
    if not 0.0 < cutoff <= 1.0:
        raise ValueError("tail threshold must be in (0, 1]")
    return probability < cutoff


def _strict_timestamp(value: object, label: str) -> pd.Timestamp:
    if isinstance(value, (bool, int, float, np.number)):
        raise DataBlocked(f"{label} must be a normalized date")
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} must be a normalized date") from exc
    if pd.isna(timestamp) or timestamp.tz is not None or timestamp != timestamp.normalize():
        raise DataBlocked(f"{label} must be a normalized date")
    return timestamp


def _validate_formations(
    formation_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
) -> pd.DatetimeIndex:
    formations = _validate_datetime_index(formation_dates, "formation dates")
    if len(formations) < 2:
        raise DataBlocked("formation dates must contain at least two dates")
    periods = formations.to_period("M")
    if periods.has_duplicates or not periods.equals(
        pd.period_range(periods[0], periods[-1], freq="M")
    ):
        raise DataBlocked("formation dates must be monthly continuous")
    if not formations.isin(calendar).all():
        raise DataBlocked("formation dates must lie on the daily calendar")
    required_periods = pd.period_range("2014-01", "2023-12", freq="M")
    required_formations = formations[periods.isin(required_periods)]
    if not required_formations.to_period("M").equals(required_periods):
        raise DataBlocked(
            "formation dates must cover every fixed-window month"
        )
    calendar_periods = calendar.to_period("M")
    expected_month_ends = pd.DatetimeIndex(
        [
            calendar[calendar_periods == period][-1]
            for period in required_periods
            if (calendar_periods == period).any()
        ]
    )
    if (
        len(expected_month_ends) != len(required_periods)
        or not required_formations.equals(expected_month_ends)
    ):
        raise DataBlocked(
            "formation dates must equal fixed-window monthly last trading days"
        )
    return formations


def _validate_score_inputs(
    state_components: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> tuple[
    pd.DataFrame,
    dict[str, pd.Series],
    pd.Series,
    pd.DatetimeIndex,
    tuple[str, ...],
    tuple[pd.Timestamp, pd.Timestamp],
]:
    if not isinstance(cfg, dict):
        raise DataBlocked("B3 configuration must be a mapping")
    _evaluation_config(cfg)
    try:
        policies = tuple(cfg["pit"]["policies"])
        discovery_values = cfg["windows"]["discovery"]
    except (KeyError, TypeError) as exc:
        raise DataBlocked("B3 configuration lacks PIT or discovery settings") from exc
    if (
        not policies
        or len(set(policies)) != len(policies)
        or any(
            not isinstance(policy, str)
            or not policy
            or policy != policy.strip()
            for policy in policies
        )
    ):
        raise DataBlocked("B3 PIT policies must be unique canonical strings")
    if not isinstance(discovery_values, list) or len(discovery_values) != 2:
        raise DataBlocked("B3 discovery window must contain two dates")
    discovery = (
        _strict_timestamp(discovery_values[0], "discovery start"),
        _strict_timestamp(discovery_values[1], "discovery end"),
    )
    if discovery[0] > discovery[1]:
        raise DataBlocked("B3 discovery window is reversed")

    if not isinstance(target_returns, dict) or set(target_returns) != {
        "blend",
        "500",
        "1000",
    }:
        raise DataBlocked("target returns must contain blend, 500 and 1000")
    targets = {
        name: _validate_finite_series(values, f"{name} target returns", returns=True)
        for name, values in target_returns.items()
    }
    calendar = targets["500"].index
    for name, values in targets.items():
        _require_same_grid(calendar, values, f"{name} target returns")
    expected_blend = 0.5 * targets["500"] + 0.5 * targets["1000"]
    if not np.allclose(
        targets["blend"].to_numpy(dtype=float),
        expected_blend.to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-15,
    ):
        raise DataBlocked("blend target returns must equal the 50/50 cash blend")
    control = _validate_finite_series(equal_weight_signal, "equal_weight signal")
    _require_same_grid(calendar, control, "equal_weight signal")
    formations = _validate_formations(formation_dates, calendar)

    required = {"date", "pit_policy", "q", "state", "F_U", "F_D", "F_X"}
    if (
        not isinstance(state_components, pd.DataFrame)
        or state_components.empty
        or state_components.columns.has_duplicates
        or not required.issubset(state_components.columns)
    ):
        raise DataBlocked("state components schema is incomplete")
    states = state_components.copy()
    states["date"] = [
        _strict_timestamp(value, "state component date")
        for value in states["date"]
    ]
    for column in ("pit_policy", "q", "state"):
        if not states[column].map(
            lambda value: isinstance(value, str)
            and bool(value)
            and value == value.strip()
        ).all():
            raise DataBlocked(f"state components {column} is invalid")
    if set(states["pit_policy"]) != set(policies):
        raise DataBlocked("state component policy set mismatch")
    if set(states["q"]) != {"qblend", "q500", "q1000"}:
        raise DataBlocked("state component q set mismatch")
    if not states["state"].isin({"UU", "DD", "DIV"}).all():
        raise DataBlocked("state component state label is invalid")
    keys = ["date", "pit_policy", "q"]
    if states.duplicated(keys).any():
        raise DataBlocked("state components contain duplicate keys")
    for column in ("F_U", "F_D", "F_X"):
        if states[column].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise DataBlocked(f"state components {column} cannot be boolean")
        numeric = pd.to_numeric(states[column], errors="coerce")
        if numeric.isna().any() or not np.isfinite(numeric.to_numpy()).all():
            raise DataBlocked(f"state components {column} must be finite")
        states[column] = numeric.astype(float)
    for policy in policies:
        for q in ("qblend", "q500", "q1000"):
            dates = pd.DatetimeIndex(
                states.loc[
                    states["pit_policy"].eq(policy) & states["q"].eq(q),
                    "date",
                ].sort_values(kind="mergesort")
            )
            if not dates.equals(calendar):
                raise DataBlocked("state component daily grids must match cash returns")
    return states, targets, control, formations, policies, discovery


def fit_frozen_m1_scores(
    state_components: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> dict[tuple[str, str], pd.Series]:
    states, targets, _, formations, policies, discovery = _validate_score_inputs(
        state_components,
        target_returns,
        equal_weight_signal,
        formation_dates,
        cfg,
    )
    target_for_q = {"qblend": "blend", "q500": "500", "q1000": "1000"}
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    )
    output: dict[tuple[str, str], pd.Series] = {}
    for policy in policies:
        for q, target_name in target_for_q.items():
            features = (
                states[
                    states["pit_policy"].eq(policy) & states["q"].eq(q)
                ]
                .sort_values("date", kind="mergesort")
                .set_index("date")[["F_U", "F_D", "F_X"]]
            )
            monthly_target = next_formation_targets(targets[target_name], formations)
            monthly = features.reindex(monthly_target.index).copy()
            if monthly.isna().any().any():
                raise DataBlocked("formation-date M1 features are incomplete")
            monthly["target"] = monthly_target
            target_end = period_end.reindex(monthly.index)
            discovery_mask = (
                monthly.index.to_series().ge(discovery[0])
                & target_end.le(discovery[1])
            )
            training = monthly.loc[discovery_mask.to_numpy()].copy()
            if training.empty:
                raise DataBlocked("M1 discovery training window is empty")
            model = fit_model(
                training,
                ["F_U", "F_D", "F_X"],
                "target",
            )
            output[(policy, q)] = apply_model(
                features,
                model,
                include_intercept=False,
            )
    return output


def yearly_contributions(
    pit_policy: str,
    candidate: str,
    returns: pd.Series,
    window: str,
    is_in_sample: bool,
) -> pd.DataFrame:
    for value, label in [
        (pit_policy, "yearly PIT policy"),
        (candidate, "yearly candidate"),
        (window, "yearly window"),
    ]:
        if not isinstance(value, str) or not value or value != value.strip():
            raise DataBlocked(f"{label} must be a canonical nonempty string")
    if not isinstance(is_in_sample, (bool, np.bool_)):
        raise DataBlocked("yearly is_in_sample must be boolean")
    clean = _validate_finite_series(returns, "yearly returns", returns=True)
    years = clean.index.year
    unique_years = np.unique(years)
    single_year_report = (
        len(unique_years) == 1
        and window == "2024-2026-report-only"
        and not bool(is_in_sample)
    )
    if len(unique_years) < 2 and not single_year_report:
        raise DataBlocked("yearly contributions require at least two years")

    log_returns = pd.Series(
        np.log1p(clean.to_numpy(dtype=float)),
        index=clean.index,
    )
    log_pnl = log_returns.groupby(years).sum()
    if not np.isfinite(log_pnl.to_numpy(dtype=float)).all():
        raise DataBlocked("yearly log P&L is non-finite")
    denominator = float(log_pnl.abs().sum())
    strongest_year = int(log_pnl.abs().idxmax())
    rows: list[dict[str, object]] = []
    for year, signed_value in log_pnl.items():
        year = int(year)
        sample = clean.loc[years == year]
        rows.append(
            {
                "pit_policy": pit_policy,
                "candidate": candidate,
                "window": window,
                "row_type": "year",
                "year": year,
                "excluded_year": pd.NA,
                "n_obs": int(len(sample)),
                "signed_log_pnl": float(signed_value),
                "absolute_pnl_share": (
                    float(abs(signed_value) / denominator)
                    if denominator > 0.0
                    else float("nan")
                ),
                "strongest_year": strongest_year,
                "ann_return": ann_return(sample),
                "sharpe": sharpe(sample),
                "maxdd": max_drawdown(sample),
                "is_in_sample": bool(is_in_sample),
                "affects_verdict": False,
            }
        )
    remaining = clean.loc[years != strongest_year]
    if remaining.empty and not single_year_report:
        raise DataBlocked("strongest-year exclusion leaves no observations")
    rows.append(
        {
            "pit_policy": pit_policy,
            "candidate": candidate,
            "window": window,
            "row_type": "excluding_strongest",
            "year": pd.NA,
            "excluded_year": strongest_year,
            "n_obs": int(len(remaining)),
            "signed_log_pnl": float("nan"),
            "absolute_pnl_share": float("nan"),
            "strongest_year": strongest_year,
            "ann_return": ann_return(remaining) if not remaining.empty else float("nan"),
            "sharpe": sharpe(remaining) if not remaining.empty else float("nan"),
            "maxdd": max_drawdown(remaining) if not remaining.empty else float("nan"),
            "is_in_sample": bool(is_in_sample),
            "affects_verdict": False,
        }
    )
    output = pd.DataFrame(rows, columns=YEARLY_COLUMNS)
    if output.duplicated(YEARLY_ROW_ID_COLUMNS).any():
        raise RuntimeError("yearly contribution row identity is not unique")
    return output.reset_index(drop=True)


def _evaluation_config(cfg: dict) -> EvaluationSettings:
    expected_windows = {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    expected_execution = {
        "annualization": 245,
        "cost_bps": 3.0,
        "im_launch_date": "2022-07-22",
        "ic_launch_date": "2015-04-16",
    }
    expected_gates = {
        "sharpe_improvement": 0.10,
        "maxdd_worsening": 0.02,
        "turnover_multiple": 1.50,
        "post_im_min_days": 252,
    }
    expected_bootstrap = {
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "adjusted_tail_max": 0.10,
    }
    try:
        pit = cfg["pit"]
        exact = (
            cfg["candidates"] == ["B3_unified", "B3_dual_target"]
            and cfg["windows"] == expected_windows
            and isinstance(pit, dict)
            and set(pit) == {"policies", "industry_pit_start"}
            and pit["policies"]
            == ["legal_deadline", "legal_deadline_plus_one_month_end"]
            and pit["industry_pit_start"] == "2021-01-01"
            and cfg["execution"] == expected_execution
            and cfg["production_gates"] == expected_gates
            and cfg["bootstrap"] == expected_bootstrap
        )
    except (KeyError, TypeError) as exc:
        raise DataBlocked("B3 evaluation configuration is incomplete") from exc
    if not exact:
        raise DataBlocked("B3 evaluation configuration differs from preregistration")
    return {
        "confirmation": (
            pd.Timestamp("2021-01-01"),
            pd.Timestamp("2023-12-31"),
        ),
        "report": (pd.Timestamp("2024-01-01"), pd.Timestamp("2026-12-31")),
        "ic_launch": pd.Timestamp("2015-04-16"),
        "im_launch": pd.Timestamp("2022-07-22"),
        "cost_bps": 3.0,
        "sharpe_improvement": 0.10,
        "maxdd_worsening": 0.02,
        "turnover_multiple": 1.50,
        "post_im_min_days": 252,
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "tail_threshold": 0.10,
    }


def _validated_model_evidence(
    model_comparison: pd.DataFrame,
    policies: tuple[str, ...],
) -> tuple[
    dict[tuple[str, str], bool],
    dict[tuple[str, str], tuple[float, dict[int, float], bool, int]],
]:
    if (
        not isinstance(model_comparison, pd.DataFrame)
        or model_comparison.empty
        or list(model_comparison.columns) != MODEL_COMPARISON_COLUMNS
    ):
        raise DataBlocked("model comparison schema mismatch")
    evidence = model_comparison.copy()
    if evidence.duplicated(MODEL_ROW_ID_COLUMNS).any():
        raise DataBlocked("model comparison row identity is duplicated")
    for column in (
        "pit_policy",
        "candidate",
        "q",
        "target",
        "window",
        "model",
        "gate_name",
    ):
        if not evidence[column].map(
            lambda value: isinstance(value, str) and value == value.strip()
        ).all():
            raise DataBlocked(f"model comparison {column} is invalid")
    for column in ("is_in_sample", "affects_verdict"):
        if not evidence[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise DataBlocked(f"model comparison {column} must be boolean")
        evidence[column] = evidence[column].astype(bool)
    gate_mask = evidence["gate_name"].ne("")
    if not evidence.loc[gate_mask, "gate_pass"].map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).all():
        raise DataBlocked("model comparison gate values must be boolean")
    if evidence.loc[gate_mask, "is_in_sample"].any():
        raise DataBlocked("model comparison gates cannot be in-sample")
    if evidence.loc[~gate_mask, "gate_pass"].notna().any():
        raise DataBlocked("model comparison metric rows cannot carry gate values")

    public_windows = {
        "beta_h_same_sign": "structure",
        "interaction_axis_corr": "2021-2023",
        "hard_sort_complete": "structure",
    }
    candidate_names = {
        "m1_increment",
        "partial_ic",
        "stability",
        "state_coverage",
    }
    structure: dict[tuple[str, str], bool] = {}
    for policy in policies:
        public = evidence[
            evidence["pit_policy"].eq(policy)
            & evidence["candidate"].eq("PUBLIC")
            & evidence["q"].eq("")
            & evidence["target"].eq("")
            & evidence["model"].eq("")
            & evidence["affects_verdict"]
        ]
        if (
            len(public) != 3
            or set(
                public[["gate_name", "window"]].itertuples(
                    index=False,
                    name=None,
                )
            )
            != set(public_windows.items())
            or public["gate_name"].duplicated().any()
        ):
            raise DataBlocked(
                f"{policy} must contain exactly three PUBLIC structure gates"
            )
        public_pass = bool(public["gate_pass"].map(bool).all())
        for candidate in ("B3_unified", "B3_dual_target"):
            aggregate = evidence[
                evidence["pit_policy"].eq(policy)
                & evidence["candidate"].eq(candidate)
                & evidence["q"].eq("")
                & evidence["target"].eq("")
                & evidence["model"].eq("")
                & evidence["affects_verdict"]
            ]
            if (
                len(aggregate) != 4
                or set(aggregate["gate_name"]) != candidate_names
                or aggregate["gate_name"].duplicated().any()
                or not aggregate["window"].eq("2021-2023").all()
            ):
                raise DataBlocked(
                    f"{policy}/{candidate} must contain exactly four "
                    "aggregate candidate structure gates"
                )
            structure[(policy, candidate)] = bool(
                public_pass and aggregate["gate_pass"].map(bool).all()
            )

    q_specs = {
        "qblend": ("B3_unified", "blend"),
        "q500": ("B3_dual_target", "500"),
        "q1000": ("B3_dual_target", "1000"),
    }
    partials: dict[
        tuple[str, str], tuple[float, dict[int, float], bool, int]
    ] = {}
    for policy in policies:
        for q, (candidate, target) in q_specs.items():
            values: dict[str, float] = {}
            sample_sizes: dict[str, int] = {}
            for window in ("2021-2023", "2021", "2022", "2023"):
                row = evidence[
                    evidence["pit_policy"].eq(policy)
                    & evidence["candidate"].eq(candidate)
                    & evidence["q"].eq(q)
                    & evidence["target"].eq(target)
                    & evidence["window"].eq(window)
                    & evidence["model"].eq("M1")
                    & evidence["gate_name"].eq("")
                ]
                if len(row) != 1:
                    raise DataBlocked(
                        f"model comparison lacks one concrete {policy}/{q}/{window} "
                        "M1 partial-IC metric row"
                    )
                if (
                    not bool(row["affects_verdict"].iloc[0])
                    or bool(row["is_in_sample"].iloc[0])
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} M1 partial-IC "
                        "flags changed"
                    )
                value = row["partial_ic"].iloc[0]
                if (
                    isinstance(value, (bool, np.bool_))
                    or pd.isna(value)
                    or not np.isfinite(float(value))
                    or not -1.0 <= float(value) <= 1.0
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} partial IC "
                        "must be finite"
                    )
                values[window] = float(value)
                raw_n = row["n"].iloc[0]
                try:
                    numeric_n = float(cast(Any, raw_n))
                except (TypeError, ValueError) as exc:
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} n "
                        "must be a positive integer"
                    ) from exc
                if (
                    isinstance(raw_n, (bool, np.bool_))
                    or isinstance(raw_n, (str, bytes))
                    or not np.isfinite(numeric_n)
                    or numeric_n <= 0
                    or numeric_n != np.floor(numeric_n)
                ):
                    raise DataBlocked(
                        f"model comparison {policy}/{q}/{window} n "
                        "must be a positive integer"
                    )
                sample_sizes[window] = int(numeric_n)
            yearly = {int(year): values[year] for year in ("2021", "2022", "2023")}
            passed = bool(
                values["2021-2023"] > 0.0
                and sum(value > 0.0 for value in yearly.values()) >= 2
            )
            partials[(policy, q)] = (
                values["2021-2023"],
                yearly,
                passed,
                sample_sizes["2021-2023"],
            )
    return structure, partials


def _monthly_partial_for_window(
    score: pd.Series,
    daily_target: pd.Series,
    control: pd.Series,
    formations: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[float, int]:
    _require_same_grid(daily_target.index, score, "post-IM score")
    _require_same_grid(daily_target.index, control, "post-IM equal_weight control")
    target = next_formation_targets(daily_target, formations)
    period_end = pd.Series(
        formations[1:],
        index=formations[:-1],
        dtype="datetime64[ns]",
    ).reindex(target.index)
    mask = target.index.to_series().ge(start) & period_end.le(end)
    dates = target.index[mask.to_numpy()]
    if len(dates) < 10:
        return float("nan"), int(len(dates))
    score_monthly = score.reindex(dates)
    control_monthly = control.reindex(dates)
    target_monthly = target.reindex(dates)
    if (
        score_monthly.isna().any()
        or control_monthly.isna().any()
        or target_monthly.isna().any()
    ):
        raise DataBlocked("post-IM monthly partial-IC inputs are incomplete")
    return (
        float(partial_rank_ic(score_monthly, target_monthly, control_monthly)),
        int(len(dates)),
    )


def _window_metrics(
    returns: pd.Series,
    positions: tuple[pd.Series, ...],
) -> dict[str, float | int]:
    if returns.empty:
        raise DataBlocked("evaluation window has no common trading dates")
    if any(not position.index.equals(returns.index) for position in positions):
        raise DataBlocked("metric position and return grids differ")
    position_turnover = float(
        sum(turnover(position) for position in positions) / len(positions)
    )
    return {
        "n_obs": int(len(returns)),
        "ann_return": ann_return(returns),
        "sharpe": sharpe(returns),
        "maxdd": max_drawdown(returns),
        "turnover": position_turnover,
    }


def _production_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        column: (
            ""
            if column in {
                "pit_policy",
                "candidate",
                "component",
                "window",
                "gate_name",
            }
            else False
            if column in {"is_candidate", "executable", "affects_verdict"}
            else np.nan
        )
        for column in PRODUCTION_METRICS_COLUMNS
    }
    row.update(updates)
    return row


def _metric_row(
    policy: str,
    candidate: str,
    component: str,
    is_candidate: bool,
    window: str,
    executable: bool,
    metrics: dict[str, float | int],
    baseline: dict[str, float | int],
) -> dict[str, object]:
    baseline_turnover = float(baseline["turnover"])
    candidate_turnover = float(metrics["turnover"])
    turnover_ratio = (
        candidate_turnover / baseline_turnover
        if baseline_turnover > 0.0
        else 1.0
        if candidate_turnover == 0.0
        else float("nan")
    )
    return _production_row(
        pit_policy=policy,
        candidate=candidate,
        component=component,
        is_candidate=is_candidate,
        window=window,
        executable=executable,
        n_obs=int(metrics["n_obs"]),
        ann_return=float(metrics["ann_return"]),
        sharpe=float(metrics["sharpe"]),
        maxdd=float(metrics["maxdd"]),
        turnover=candidate_turnover,
        baseline_sharpe=float(baseline["sharpe"]),
        sharpe_difference=float(metrics["sharpe"]) - float(baseline["sharpe"]),
        baseline_maxdd=float(baseline["maxdd"]),
        maxdd_difference=float(metrics["maxdd"]) - float(baseline["maxdd"]),
        baseline_turnover=baseline_turnover,
        turnover_ratio=turnover_ratio,
    )


def _gate_row(
    metric_row: dict[str, object],
    gate_name: str,
    gate_pass: bool,
) -> dict[str, object]:
    row = dict(metric_row)
    row.update(
        component="aggregate",
        is_candidate=True,
        gate_name=gate_name,
        gate_pass=bool(gate_pass),
        affects_verdict=True,
    )
    return row


def _row_float(row: dict[str, object], key: str) -> float:
    value = row[key]
    if isinstance(value, (bool, np.bool_)):
        raise RuntimeError(f"production row {key} is not numeric")
    try:
        numeric = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"production row {key} is not numeric") from exc
    if not np.isfinite(numeric):
        raise RuntimeError(f"production row {key} is non-finite")
    return numeric


def _row_int(row: dict[str, object], key: str) -> int:
    numeric = _row_float(row, key)
    if numeric < 0.0 or numeric != np.floor(numeric):
        raise RuntimeError(f"production row {key} is not a nonnegative integer")
    return int(numeric)


def _validate_production_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
    *,
    report_present: bool,
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != PRODUCTION_METRICS_COLUMNS:
        raise RuntimeError("production metrics output schema mismatch")
    output = frame.copy()
    if not isinstance(report_present, (bool, np.bool_)):
        raise RuntimeError("production report presence must be boolean")
    actual_report_present = output["window"].eq(
        "2024-2026-report-only"
    ).any()
    if bool(actual_report_present) != bool(report_present):
        raise RuntimeError("production report presence changed")
    if output.duplicated(PRODUCTION_ROW_ID_COLUMNS).any():
        raise RuntimeError("production metrics row identity is not unique")
    for column in ("pit_policy", "candidate", "component", "window", "gate_name"):
        if not output[column].map(
            lambda value: isinstance(value, str) and value == value.strip()
        ).all():
            raise RuntimeError(f"production metrics {column} is invalid")
    for column in ("is_candidate", "executable", "affects_verdict"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"production metrics {column} must be boolean")
        output[column] = output[column].astype(bool)
    gate = output["gate_name"].ne("")
    if not output.loc[gate, "gate_pass"].map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).all() or output.loc[~gate, "gate_pass"].notna().any():
        raise RuntimeError("production gate rows must have bool-only gate values")
    if output.loc[output["affects_verdict"], "gate_name"].eq("").any():
        raise RuntimeError("verdict-affecting production rows must be gates")
    if output.loc[
        output["window"].isin({"pre-IM", "2024-2026-report-only"}),
        "affects_verdict",
    ].any():
        raise RuntimeError("pre-IM and report-only rows cannot affect verdict")
    numeric_columns = [
        "n_obs",
        "ann_return",
        "sharpe",
        "maxdd",
        "turnover",
        "baseline_sharpe",
        "sharpe_difference",
        "baseline_maxdd",
        "maxdd_difference",
        "baseline_turnover",
        "turnover_ratio",
        "partial_ic",
    ]
    for column in numeric_columns:
        invalid_type = output[column].map(
            lambda value: (
                not pd.isna(value)
                and isinstance(value, (bool, np.bool_, str, bytes))
            )
        )
        if invalid_type.any():
            raise RuntimeError(f"production metrics {column} is not numeric")
        numeric = pd.to_numeric(output[column], errors="coerce")
        invalid = output[column].notna() & numeric.isna()
        if invalid.any() or not np.isfinite(numeric.dropna().to_numpy()).all():
            raise RuntimeError(f"production metrics {column} is non-finite")
        output[column] = numeric

    metric_specs = (
        ("equal_weight", "blend"),
        ("B3_unified", "blend"),
        ("B3_dual_target", "blend"),
        ("B3_dual_target", "B3_500"),
        ("B3_dual_target", "B3_1000"),
    )
    windows = ["2021-2023", "pre-IM", "post-IM"]
    if report_present:
        windows.append("2024-2026-report-only")
    full_gate_names = (
        "sharpe_improvement",
        "maxdd_worsening",
        "turnover_multiple",
        "partial_ic",
    )
    post_gate_names = (
        "post_im_min_days",
        "post_im_sharpe_difference",
        "post_im_maxdd_difference",
        "post_im_partial_ic",
    )
    expected_ids: set[tuple[str, str, str, str, str]] = set()
    for policy in policies:
        for window in windows:
            expected_ids.update(
                (policy, candidate, component, window, "")
                for candidate, component in metric_specs
            )
        for candidate in ("B3_unified", "B3_dual_target"):
            expected_ids.update(
                (
                    policy,
                    candidate,
                    "aggregate",
                    "2021-2023",
                    gate_name,
                )
                for gate_name in full_gate_names
            )
        expected_ids.update(
            {
                (
                    policy,
                    "B3_unified",
                    "qblend",
                    "2021-2023",
                    "partial_ic_leg",
                ),
                (
                    policy,
                    "B3_dual_target",
                    "q500",
                    "2021-2023",
                    "partial_ic_leg",
                ),
                (
                    policy,
                    "B3_dual_target",
                    "q1000",
                    "2021-2023",
                    "partial_ic_leg",
                ),
            }
        )
        expected_ids.update(
            (
                policy,
                "B3_dual_target",
                "aggregate",
                "post-IM",
                gate_name,
            )
            for gate_name in post_gate_names
        )
        expected_ids.update(
            (
                policy,
                "B3_dual_target",
                component,
                "post-IM",
                "post_im_partial_ic_leg",
            )
            for component in ("q500", "q1000")
        )
    actual_ids = set(
        output[PRODUCTION_ROW_ID_COLUMNS].itertuples(index=False, name=None)
    )
    if actual_ids != expected_ids:
        raise RuntimeError("production metrics exact row identities changed")

    leg_gate_names = {"partial_ic_leg", "post_im_partial_ic_leg"}
    aggregate_gate_names = set(full_gate_names) | set(post_gate_names)
    for _, row in output.iterrows():
        gate_name = str(row["gate_name"])
        expected_affects = gate_name in aggregate_gate_names
        expected_executable = (
            gate_name == "" and str(row["component"]) == "B3_500"
        ) or str(row["window"]) in {
            "post-IM",
            "2024-2026-report-only",
        }
        expected_is_candidate = expected_affects or (
            gate_name == ""
            and str(row["component"]) == "blend"
            and str(row["candidate"]) in {"B3_unified", "B3_dual_target"}
        )
        if (
            bool(row["affects_verdict"]) != expected_affects
            or bool(row["executable"]) != expected_executable
            or bool(row["is_candidate"]) != expected_is_candidate
        ):
            raise RuntimeError("production row flags changed")

    performance_columns = [
        "ann_return",
        "sharpe",
        "maxdd",
        "turnover",
        "baseline_sharpe",
        "sharpe_difference",
        "baseline_maxdd",
        "maxdd_difference",
        "baseline_turnover",
        "turnover_ratio",
    ]
    leg_rows = output["gate_name"].isin(leg_gate_names)
    if (
        output.loc[leg_rows, performance_columns].notna().any(axis=None)
        or output.loc[~leg_rows, performance_columns].isna().any(axis=None)
        or output.loc[leg_rows, "partial_ic"].isna().any()
        or output.loc[~leg_rows, "partial_ic"].notna().any()
    ):
        raise RuntimeError("production row numeric nullability changed")
    if (
        output["n_obs"].isna().any()
        or (output["n_obs"] <= 0).any()
        or not np.equal(output["n_obs"], np.floor(output["n_obs"])).all()
    ):
        raise RuntimeError("production metrics n_obs must be a positive integer")
    if not output.loc[leg_rows, "partial_ic"].between(-1.0, 1.0).all():
        raise RuntimeError("production partial IC must be in [-1, 1]")
    full_leg_rows = output["gate_name"].eq("partial_ic_leg")
    passing_full_leg = output.loc[full_leg_rows, "gate_pass"].astype(bool)
    if (
        passing_full_leg
        & output.loc[full_leg_rows, "partial_ic"].le(0.0)
    ).any():
        raise RuntimeError(
            "production full-confirmation leg gate semantics changed"
        )
    performance_rows = output.loc[~leg_rows]
    if (
        (performance_rows[["turnover", "baseline_turnover", "turnover_ratio"]] < 0.0)
        .any(axis=None)
        or (performance_rows[["maxdd", "baseline_maxdd"]] > 0.0).any(axis=None)
    ):
        raise RuntimeError("production performance metric domain changed")

    row_lookup: dict[tuple[str, str, str, str, str], pd.Series] = {}
    for _, row in output.iterrows():
        key = tuple(str(row[column]) for column in PRODUCTION_ROW_ID_COLUMNS)
        row_lookup[cast(tuple[str, str, str, str, str], key)] = row

    def same_number(left: object, right: object) -> bool:
        return bool(
            np.isclose(
                float(cast(Any, left)),
                float(cast(Any, right)),
                rtol=0.0,
                atol=1e-12,
            )
        )

    def require_gate(row: pd.Series, expected: bool) -> None:
        if bool(row["gate_pass"]) != bool(expected):
            raise RuntimeError("production aggregate gate semantics changed")

    for policy in policies:
        for window in windows:
            baseline = row_lookup[
                (policy, "equal_weight", "blend", window, "")
            ]
            baseline_n = int(baseline["n_obs"])
            baseline_sharpe = float(baseline["sharpe"])
            baseline_maxdd = float(baseline["maxdd"])
            baseline_turnover = float(baseline["turnover"])
            for candidate, component in metric_specs:
                metric = row_lookup[
                    (policy, candidate, component, window, "")
                ]
                turnover_value = float(metric["turnover"])
                if int(metric["n_obs"]) != baseline_n:
                    raise RuntimeError("production window observation counts changed")
                if not (
                    same_number(metric["baseline_sharpe"], baseline_sharpe)
                    and same_number(metric["baseline_maxdd"], baseline_maxdd)
                    and same_number(
                        metric["baseline_turnover"],
                        baseline_turnover,
                    )
                    and same_number(
                        metric["sharpe_difference"],
                        float(metric["sharpe"]) - baseline_sharpe,
                    )
                    and same_number(
                        metric["maxdd_difference"],
                        float(metric["maxdd"]) - baseline_maxdd,
                    )
                ):
                    raise RuntimeError("production baseline differences changed")
                if baseline_turnover > 0.0:
                    expected_ratio = turnover_value / baseline_turnover
                elif turnover_value == 0.0:
                    expected_ratio = 1.0
                else:
                    raise RuntimeError("production turnover ratio is undefined")
                if not same_number(metric["turnover_ratio"], expected_ratio):
                    raise RuntimeError("production turnover ratio changed")

        for candidate in ("B3_unified", "B3_dual_target"):
            metric = row_lookup[
                (policy, candidate, "blend", "2021-2023", "")
            ]
            for gate_name in full_gate_names:
                gate_row = row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        gate_name,
                    )
                ]
                if int(gate_row["n_obs"]) != int(metric["n_obs"]) or any(
                    not same_number(gate_row[column], metric[column])
                    for column in performance_columns
                ):
                    raise RuntimeError("production gate metric copy changed")
            leg_components = (
                ("qblend",)
                if candidate == "B3_unified"
                else ("q500", "q1000")
            )
            leg_pass = all(
                bool(
                    row_lookup[
                        (
                            policy,
                            candidate,
                            component,
                            "2021-2023",
                            "partial_ic_leg",
                        )
                    ]["gate_pass"]
                )
                for component in leg_components
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "sharpe_improvement",
                    )
                ],
                float(metric["sharpe_difference"]) >= 0.10,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "maxdd_worsening",
                    )
                ],
                float(metric["maxdd_difference"]) >= -0.02,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "turnover_multiple",
                    )
                ],
                float(metric["turnover_ratio"]) <= 1.50,
            )
            require_gate(
                row_lookup[
                    (
                        policy,
                        candidate,
                        "aggregate",
                        "2021-2023",
                        "partial_ic",
                    )
                ],
                leg_pass,
            )

        post_metric = row_lookup[
            (policy, "B3_dual_target", "blend", "post-IM", "")
        ]
        for gate_name in post_gate_names:
            gate_row = row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    gate_name,
                )
            ]
            if int(gate_row["n_obs"]) != int(post_metric["n_obs"]) or any(
                not same_number(gate_row[column], post_metric[column])
                for column in performance_columns
            ):
                raise RuntimeError("production post-IM gate metric copy changed")
        post_leg_passes = []
        for component in ("q500", "q1000"):
            leg = row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    component,
                    "post-IM",
                    "post_im_partial_ic_leg",
                )
            ]
            expected_leg_pass = float(leg["partial_ic"]) >= 0.0
            if bool(leg["gate_pass"]) != expected_leg_pass:
                raise RuntimeError("production post-IM leg gate semantics changed")
            post_leg_passes.append(expected_leg_pass)
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_min_days",
                )
            ],
            int(post_metric["n_obs"]) >= 252,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_sharpe_difference",
                )
            ],
            float(post_metric["sharpe_difference"]) > 0.0,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_maxdd_difference",
                )
            ],
            float(post_metric["maxdd_difference"]) >= -0.02,
        )
        require_gate(
            row_lookup[
                (
                    policy,
                    "B3_dual_target",
                    "aggregate",
                    "post-IM",
                    "post_im_partial_ic",
                )
            ],
            all(post_leg_passes),
        )
    return output.sort_values(
        PRODUCTION_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validate_bootstrap_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != BOOTSTRAP_COLUMNS:
        raise RuntimeError("bootstrap output schema mismatch")
    output = frame.copy()
    if output.duplicated(BOOTSTRAP_ROW_ID_COLUMNS).any():
        raise RuntimeError("bootstrap row identity is not unique")
    expected = {
        (policy, candidate)
        for policy in policies
        for candidate in ("B3_unified", "B3_dual_target")
    }
    if set(
        output[BOOTSTRAP_ROW_ID_COLUMNS].itertuples(index=False, name=None)
    ) != expected:
        raise RuntimeError("bootstrap fixed candidate family changed")
    for column in ("structure_pass", "gate_pass"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"bootstrap {column} must be boolean")
        output[column] = output[column].astype(bool)
    for column in ("draws", "block_days", "seed"):
        numeric = pd.to_numeric(output[column], errors="coerce")
        if numeric.isna().any() or not np.equal(numeric, np.floor(numeric)).all():
            raise RuntimeError(f"bootstrap {column} must be integral")
        output[column] = numeric.astype(int)
    frozen_sampling = {
        "draws": 5000,
        "block_days": 20,
        "seed": 20260713,
    }
    for column, expected_value in frozen_sampling.items():
        if not output[column].eq(expected_value).all():
            raise RuntimeError(f"bootstrap {column} changed from frozen contract")
    for column in ("tail_prob", "holm_adjusted_tail"):
        numeric = pd.to_numeric(output[column], errors="coerce")
        if (
            numeric.isna().any()
            or not np.isfinite(numeric.to_numpy()).all()
            or not numeric.between(0.0, 1.0).all()
        ):
            raise RuntimeError(f"bootstrap {column} is invalid")
        output[column] = numeric.astype(float)
    empirical_rank = output["tail_prob"] * (output["draws"] + 1)
    nearest_rank = np.rint(empirical_rank)
    on_empirical_grid = np.isclose(
        empirical_rank,
        nearest_rank,
        rtol=0.0,
        atol=1e-10,
    )
    rank_in_range = (nearest_rank >= 1) & (
        nearest_rank <= output["draws"] + 1
    )
    if not (on_empirical_grid & rank_in_range).all():
        raise RuntimeError(
            "bootstrap tail_prob must lie on empirical grid"
        )
    ci_columns = ["ci05", "ci50", "ci95"]
    for index, row in output.iterrows():
        ci = pd.to_numeric(row[ci_columns], errors="coerce")
        if bool(row["structure_pass"]):
            if ci.isna().any() or not np.isfinite(ci.to_numpy()).all():
                raise RuntimeError("structure-passing bootstrap CI is invalid")
            if not float(ci["ci05"]) <= float(ci["ci50"]) <= float(ci["ci95"]):
                raise RuntimeError("structure-passing bootstrap CI is unordered")
        elif ci.notna().any() or float(row["tail_prob"]) != 1.0:
            raise RuntimeError("structure-failed bootstrap must have tail 1 and blank CI")
    for policy in policies:
        family = output.loc[output["pit_policy"].eq(policy)]
        adjusted = holm_style_adjust(
            dict(
                zip(
                    family["candidate"],
                    family["tail_prob"].astype(float),
                    strict=True,
                )
            )
        )
        for _, row in family.iterrows():
            expected_adjusted = adjusted[str(row["candidate"])]
            if float(row["holm_adjusted_tail"]) != expected_adjusted:
                raise RuntimeError("bootstrap Holm adjustment changed")
            expected_gate = bool(row["structure_pass"]) and passes_tail_gate(
                expected_adjusted,
                0.10,
            )
            if bool(row["gate_pass"]) != expected_gate:
                raise RuntimeError("bootstrap gate semantics changed")
    return output.sort_values(
        BOOTSTRAP_ROW_ID_COLUMNS,
        kind="mergesort",
    ).reset_index(drop=True)


def _validate_yearly_output(
    frame: pd.DataFrame,
    policies: tuple[str, ...],
    *,
    report_present: bool,
) -> pd.DataFrame:
    if frame.empty or list(frame.columns) != YEARLY_COLUMNS:
        raise RuntimeError("yearly output schema mismatch")
    output = frame.copy()
    if not isinstance(report_present, (bool, np.bool_)):
        raise RuntimeError("yearly report presence must be boolean")
    actual_report_present = output["window"].eq(
        "2024-2026-report-only"
    ).any()
    if bool(actual_report_present) != bool(report_present):
        raise RuntimeError("yearly report presence changed")
    if output.duplicated(YEARLY_ROW_ID_COLUMNS).any():
        raise RuntimeError("yearly row identity is not unique")
    for column in ("is_in_sample", "affects_verdict"):
        if not output[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        ).all():
            raise RuntimeError(f"yearly {column} must be strictly boolean")
        output[column] = output[column].astype(bool)
    if output["affects_verdict"].any():
        raise RuntimeError("yearly output cannot affect verdict")
    for column in ("pit_policy", "candidate", "window", "row_type"):
        if not output[column].map(
            lambda value: (
                isinstance(value, str)
                and bool(value)
                and value == value.strip()
            )
        ).all():
            raise RuntimeError(f"yearly {column} is invalid")
    if set(output["pit_policy"]) != set(policies):
        raise RuntimeError("yearly PIT policy set changed")
    if set(output["candidate"]) != {"B3_unified", "B3_dual_target"}:
        raise RuntimeError("yearly candidate family changed")
    mandatory_windows = {"2021-2023", "2014-2023"}
    allowed_windows = mandatory_windows | {"2024-2026-report-only"}
    actual_windows = set(output["window"])
    if not mandatory_windows.issubset(actual_windows) or not actual_windows.issubset(
        allowed_windows
    ):
        raise RuntimeError("yearly diagnostic windows changed")
    if set(output["row_type"]) != {"year", "excluding_strongest"}:
        raise RuntimeError("yearly row type is invalid")
    for column in (
        "n_obs",
        "year",
        "excluded_year",
        "signed_log_pnl",
        "absolute_pnl_share",
        "strongest_year",
        "ann_return",
        "sharpe",
        "maxdd",
    ):
        numeric = pd.to_numeric(output[column], errors="coerce")
        invalid = output[column].notna() & numeric.isna()
        if invalid.any() or not np.isfinite(numeric.dropna().to_numpy()).all():
            raise RuntimeError(f"yearly {column} row semantics are invalid")
        output[column] = numeric

    expected_years = {
        "2021-2023": {2021, 2022, 2023},
        "2014-2023": set(range(2014, 2024)),
    }
    if "2024-2026-report-only" in actual_windows:
        report_years = set(
            output.loc[
                output["window"].eq("2024-2026-report-only")
                & output["row_type"].eq("year"),
                "year",
            ].astype(int)
        )
        ordered_report_years = sorted(report_years)
        if (
            len(report_years) < 1
            or not report_years.issubset({2024, 2025, 2026})
            or ordered_report_years[0] != 2024
            or ordered_report_years
            != list(
                range(
                    ordered_report_years[0],
                    ordered_report_years[-1] + 1,
                )
            )
        ):
            raise RuntimeError(
                "yearly row semantics have invalid report year coverage"
            )
        expected_years["2024-2026-report-only"] = report_years
    for policy in policies:
        for candidate in ("B3_unified", "B3_dual_target"):
            for window in actual_windows:
                group = output[
                    output["pit_policy"].eq(policy)
                    & output["candidate"].eq(candidate)
                    & output["window"].eq(window)
                ]
                if group.empty:
                    raise RuntimeError("yearly row semantics require every group")
                year_rows = group[group["row_type"].eq("year")]
                summary = group[group["row_type"].eq("excluding_strongest")]
                actual_years = set(year_rows["year"].astype(int))
                required_years = expected_years[window]
                if (
                    actual_years != required_years
                    or len(summary) != 1
                    or len(group) != len(required_years) + 1
                ):
                    raise RuntimeError("yearly row semantics have wrong year coverage")
                expected_in_sample = window == "2014-2023"
                if not group["is_in_sample"].eq(expected_in_sample).all():
                    raise RuntimeError("yearly row semantics have wrong sample label")
                if (
                    year_rows["excluded_year"].notna().any()
                    or summary["year"].notna().any()
                    or year_rows["signed_log_pnl"].isna().any()
                    or summary["signed_log_pnl"].notna().any()
                    or summary["absolute_pnl_share"].notna().any()
                ):
                    raise RuntimeError("yearly row semantics are inconsistent")
                integral_columns = ["year", "strongest_year", "n_obs"]
                for column in integral_columns:
                    values = group[column].dropna()
                    if (
                        (values < 0).any()
                        or not np.equal(values, np.floor(values)).all()
                    ):
                        raise RuntimeError("yearly row semantics require integers")
                single_year_report = (
                    window == "2024-2026-report-only"
                    and len(required_years) == 1
                )
                summary_n = int(summary["n_obs"].iloc[0])
                if (year_rows["n_obs"] <= 0).any() or (
                    not single_year_report and summary_n <= 0
                ) or (single_year_report and summary_n != 0):
                    raise RuntimeError("yearly row semantics require observations")
                strongest_values = set(group["strongest_year"].astype(int))
                if len(strongest_values) != 1:
                    raise RuntimeError("yearly row semantics disagree on strongest year")
                strongest_year = strongest_values.pop()
                if strongest_year not in required_years:
                    raise RuntimeError("yearly row semantics have invalid strongest year")
                signed_by_year = (
                    year_rows.set_index("year")["signed_log_pnl"].sort_index()
                )
                expected_strongest = int(signed_by_year.abs().idxmax())
                if strongest_year != expected_strongest:
                    raise RuntimeError(
                        "yearly row semantics have wrong strongest year"
                    )
                excluded = summary["excluded_year"].iloc[0]
                if (
                    pd.isna(excluded)
                    or float(excluded) != strongest_year
                    or float(excluded) != np.floor(float(excluded))
                ):
                    raise RuntimeError("yearly row semantics have wrong excluded year")
                strongest_n = int(
                    year_rows.loc[
                        year_rows["year"].eq(strongest_year), "n_obs"
                    ].iloc[0]
                )
                if int(summary["n_obs"].iloc[0]) != int(
                    year_rows["n_obs"].sum()
                ) - strongest_n:
                    raise RuntimeError("yearly row semantics have wrong summary n")
                year_metrics = year_rows[["ann_return", "sharpe", "maxdd"]]
                if year_metrics.isna().any(axis=None):
                    raise RuntimeError(
                        "yearly row semantics require year metrics"
                    )
                if (group["maxdd"].dropna() > 0.0).any():
                    raise RuntimeError(
                        "yearly row semantics require nonpositive maxdd"
                    )
                summary_metrics = summary[["ann_return", "sharpe", "maxdd"]]
                if single_year_report:
                    if summary_metrics.notna().any(axis=None):
                        raise RuntimeError(
                            "yearly row semantics require an empty report summary"
                        )
                elif summary_metrics.isna().any(axis=None):
                    raise RuntimeError("yearly row semantics require summary metrics")
                shares_by_year = (
                    year_rows.set_index("year")["absolute_pnl_share"].sort_index()
                )
                denominator = float(signed_by_year.abs().sum())
                if denominator > 0.0:
                    expected_shares = signed_by_year.abs() / denominator
                    if shares_by_year.isna().any() or not np.allclose(
                        shares_by_year.to_numpy(dtype=float),
                        expected_shares.to_numpy(dtype=float),
                        rtol=0.0,
                        atol=1e-12,
                    ):
                        raise RuntimeError("yearly row semantics have invalid shares")
                elif shares_by_year.notna().any():
                    raise RuntimeError("yearly row semantics have invalid zero shares")
    return output.sort_values(
        YEARLY_ROW_ID_COLUMNS,
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)


def build_evaluation(
    state_components: pd.DataFrame,
    model_comparison: pd.DataFrame,
    target_returns: dict[str, pd.Series],
    carry_returns: dict[str, pd.Series],
    equal_weight_signal: pd.Series,
    formation_dates: pd.DatetimeIndex,
    cfg: dict,
) -> EvaluationFrames:
    settings = _evaluation_config(cfg)
    (
        states,
        targets,
        control,
        formations,
        policies,
        _,
    ) = _validate_score_inputs(
        state_components,
        target_returns,
        equal_weight_signal,
        formation_dates,
        cfg,
    )
    structure, full_partials = _validated_model_evidence(
        model_comparison,
        policies,
    )
    if not isinstance(carry_returns, dict) or set(carry_returns) != {"500", "1000"}:
        raise DataBlocked("carry returns must contain exactly 500 and 1000")
    cash_calendar = targets["500"].index
    carry_500_full = materialize_carry(
        carry_returns["500"],
        cash_calendar,
        settings["ic_launch"],
    )
    carry_1000_full = materialize_carry(
        carry_returns["1000"],
        cash_calendar,
        settings["im_launch"],
    )
    common_end = min(carry_500_full.index.max(), carry_1000_full.index.max())
    calendar = cash_calendar[cash_calendar <= common_end]
    confirmation_start, confirmation_end = settings["confirmation"]
    expected_confirmation = cash_calendar[
        (cash_calendar >= confirmation_start)
        & (cash_calendar <= confirmation_end)
    ]
    actual_confirmation = calendar[
        (calendar >= confirmation_start) & (calendar <= confirmation_end)
    ]
    if (
        not len(expected_confirmation)
        or not actual_confirmation.equals(expected_confirmation)
    ):
        raise DataBlocked(
            "common carry tail does not cover the full cash confirmation calendar"
        )
    carry_500 = carry_500_full.reindex(calendar)
    carry_1000 = carry_1000_full.reindex(calendar)
    if carry_500.isna().any() or carry_1000.isna().any():
        raise DataBlocked("common carry calendar contains an internal gap")
    cash_500 = targets["500"].loc[calendar]
    cash_1000 = targets["1000"].loc[calendar]
    control = control.loc[calendar]
    full_scores = fit_frozen_m1_scores(
        states,
        targets,
        equal_weight_signal,
        formations,
        cfg,
    )
    scores = {key: value.loc[calendar] for key, value in full_scores.items()}
    baseline_position = production_position(control).astype(int)
    cost_bps = settings["cost_bps"]

    im_launch = settings["im_launch"]
    window_specs: list[tuple[str, pd.DatetimeIndex, bool]] = []
    confirmation_index = actual_confirmation
    pre_im_index = confirmation_index[confirmation_index < im_launch]
    post_im_index = confirmation_index[confirmation_index >= im_launch]
    if not len(pre_im_index) or not len(post_im_index):
        raise DataBlocked("confirmation window must straddle the IM launch boundary")
    window_specs.extend(
        [
            ("2021-2023", confirmation_index, False),
            ("pre-IM", pre_im_index, False),
            ("post-IM", post_im_index, True),
        ]
    )
    report_start, report_end = settings["report"]
    report_index = calendar[(calendar >= report_start) & (calendar <= report_end)]
    if len(report_index):
        window_specs.append(("2024-2026-report-only", report_index, True))

    production_rows: list[dict[str, object]] = []
    bootstrap_rows: list[dict[str, object]] = []
    yearly_frames: list[pd.DataFrame] = []
    for policy in policies:
        unified_position = production_position(scores[(policy, "qblend")]).astype(int)
        position_500 = production_position(scores[(policy, "q500")]).astype(int)
        position_1000 = production_position(scores[(policy, "q1000")]).astype(int)
        candidate_positions = {
            "B3_unified": (unified_position, unified_position),
            "B3_dual_target": (position_500, position_1000),
        }
        window_candidate_returns: dict[tuple[str, str], pd.Series] = {}
        window_baseline_returns: dict[str, pd.Series] = {}
        metric_rows: dict[tuple[str, str], dict[str, object]] = {}
        for window, index, executable in window_specs:
            baseline_window_position = baseline_position.loc[index]
            baseline_return = two_leg_candidate_returns(
                baseline_window_position,
                baseline_window_position,
                cash_500.loc[index],
                cash_1000.loc[index],
                carry_500.loc[index],
                carry_1000.loc[index],
                cost_bps,
            )
            window_baseline_returns[window] = baseline_return
            baseline_metrics = _window_metrics(
                baseline_return,
                (baseline_window_position, baseline_window_position),
            )
            baseline_row = _metric_row(
                policy,
                "equal_weight",
                "blend",
                False,
                window,
                executable,
                baseline_metrics,
                baseline_metrics,
            )
            production_rows.append(baseline_row)
            for candidate, positions in candidate_positions.items():
                window_positions = tuple(
                    position.loc[index] for position in positions
                )
                returns = two_leg_candidate_returns(
                    window_positions[0],
                    window_positions[1],
                    cash_500.loc[index],
                    cash_1000.loc[index],
                    carry_500.loc[index],
                    carry_1000.loc[index],
                    cost_bps,
                )
                window_candidate_returns[(candidate, window)] = returns
                metrics = _window_metrics(
                    returns,
                    window_positions,
                )
                row = _metric_row(
                    policy,
                    candidate,
                    "blend",
                    True,
                    window,
                    executable,
                    metrics,
                    baseline_metrics,
                )
                production_rows.append(row)
                metric_rows[(candidate, window)] = row
            for component, leg_position, leg_cash, leg_carry, component_executable in [
                (
                    "B3_500",
                    position_500,
                    cash_500,
                    carry_500,
                    bool(index.min() >= settings["ic_launch"]),
                ),
                (
                    "B3_1000",
                    position_1000,
                    cash_1000,
                    carry_1000,
                    bool(index.min() >= im_launch),
                ),
            ]:
                component_position = leg_position.loc[index]
                leg_return = run_strategy(
                    component_position,
                    leg_cash.loc[index],
                    cost_bps,
                    leg_carry.loc[index],
                )["ret"]
                component_metrics = _window_metrics(
                    leg_return,
                    (component_position,),
                )
                production_rows.append(
                    _metric_row(
                        policy,
                        "B3_dual_target",
                        component,
                        False,
                        window,
                        component_executable,
                        component_metrics,
                        baseline_metrics,
                    )
                )

        for candidate in ("B3_unified", "B3_dual_target"):
            metric = metric_rows[(candidate, "2021-2023")]
            production_rows.extend(
                [
                    _gate_row(
                        metric,
                        "sharpe_improvement",
                        _row_float(metric, "sharpe_difference")
                        >= settings["sharpe_improvement"],
                    ),
                    _gate_row(
                        metric,
                        "maxdd_worsening",
                        _row_float(metric, "maxdd_difference")
                        >= -settings["maxdd_worsening"],
                    ),
                    _gate_row(
                        metric,
                        "turnover_multiple",
                        _row_float(metric, "turnover_ratio")
                        <= settings["turnover_multiple"],
                    ),
                ]
            )
            q_legs = (
                ("qblend",)
                if candidate == "B3_unified"
                else ("q500", "q1000")
            )
            leg_passes = []
            for q in q_legs:
                full_partial, _, leg_pass, full_n = full_partials[(policy, q)]
                leg_passes.append(leg_pass)
                production_rows.append(
                    _production_row(
                        pit_policy=policy,
                        candidate=candidate,
                        component=q,
                        is_candidate=False,
                        window="2021-2023",
                        executable=False,
                        n_obs=full_n,
                        partial_ic=full_partial,
                        gate_name="partial_ic_leg",
                        gate_pass=bool(leg_pass),
                        affects_verdict=False,
                    )
                )
            partial_gate = _gate_row(
                metric,
                "partial_ic",
                all(leg_passes),
            )
            partial_gate["partial_ic"] = np.nan
            production_rows.append(partial_gate)

        post_metric = metric_rows[("B3_dual_target", "post-IM")]
        post_partial: dict[str, float] = {}
        for q, target_name in (("q500", "500"), ("q1000", "1000")):
            value, partial_n = _monthly_partial_for_window(
                full_scores[(policy, q)],
                targets[target_name],
                equal_weight_signal,
                formations,
                im_launch,
                confirmation_end,
            )
            post_partial[q] = value
            leg_pass = bool(np.isfinite(value) and value >= 0.0)
            production_rows.append(
                _production_row(
                    pit_policy=policy,
                    candidate="B3_dual_target",
                    component=q,
                    is_candidate=False,
                    window="post-IM",
                    executable=True,
                    n_obs=partial_n,
                    partial_ic=value,
                    gate_name="post_im_partial_ic_leg",
                    gate_pass=leg_pass,
                    affects_verdict=False,
                )
            )
        production_rows.extend(
            [
                _gate_row(
                    post_metric,
                    "post_im_min_days",
                    _row_int(post_metric, "n_obs")
                    >= settings["post_im_min_days"],
                ),
                _gate_row(
                    post_metric,
                    "post_im_sharpe_difference",
                    _row_float(post_metric, "sharpe_difference") > 0.0,
                ),
                _gate_row(
                    post_metric,
                    "post_im_maxdd_difference",
                    _row_float(post_metric, "maxdd_difference")
                    >= -settings["maxdd_worsening"],
                ),
            ]
        )
        post_partial_gate = _gate_row(
            post_metric,
            "post_im_partial_ic",
            all(np.isfinite(value) and value >= 0.0 for value in post_partial.values()),
        )
        post_partial_gate["partial_ic"] = np.nan
        production_rows.append(post_partial_gate)

        raw_evidence: dict[str, dict[str, float]] = {}
        raw_tails: dict[str, float] = {}
        for candidate in ("B3_unified", "B3_dual_target"):
            if structure[(policy, candidate)]:
                result = paired_moving_block_tail(
                    window_candidate_returns[(candidate, "2021-2023")],
                    window_baseline_returns["2021-2023"],
                    settings["block_days"],
                    settings["draws"],
                    settings["seed"],
                )
                raw_evidence[candidate] = result
                raw_tails[candidate] = result["tail_prob"]
            else:
                raw_evidence[candidate] = {
                    "tail_prob": 1.0,
                    "ci05": float("nan"),
                    "ci50": float("nan"),
                    "ci95": float("nan"),
                }
                raw_tails[candidate] = 1.0
        adjusted = holm_style_adjust(raw_tails)
        for candidate in ("B3_unified", "B3_dual_target"):
            evidence = raw_evidence[candidate]
            bootstrap_rows.append(
                {
                    "pit_policy": policy,
                    "candidate": candidate,
                    "draws": settings["draws"],
                    "block_days": settings["block_days"],
                    "seed": settings["seed"],
                    "tail_prob": float(evidence["tail_prob"]),
                    "holm_adjusted_tail": float(adjusted[candidate]),
                    "ci05": float(evidence["ci05"]),
                    "ci50": float(evidence["ci50"]),
                    "ci95": float(evidence["ci95"]),
                    "structure_pass": bool(structure[(policy, candidate)]),
                    "gate_pass": bool(
                        structure[(policy, candidate)]
                        and passes_tail_gate(
                            adjusted[candidate],
                            settings["tail_threshold"],
                        )
                    ),
                }
            )

        history_end = min(pd.Timestamp("2023-12-31"), calendar.max())
        full_history = calendar[
            (calendar >= pd.Timestamp("2014-01-01"))
            & (calendar <= history_end)
        ]
        if (
            not len(full_history)
            or full_history.min().to_period("M") != pd.Period("2014-01", freq="M")
            or full_history.max().to_period("M") != pd.Period("2023-12", freq="M")
        ):
            raise DataBlocked("2014-2023 yearly diagnostic window is incomplete")
        for candidate, positions in candidate_positions.items():
            history_positions = tuple(
                position.loc[full_history] for position in positions
            )
            history_returns = two_leg_candidate_returns(
                history_positions[0],
                history_positions[1],
                cash_500.loc[full_history],
                cash_1000.loc[full_history],
                carry_500.loc[full_history],
                carry_1000.loc[full_history],
                cost_bps,
            )
            yearly_frames.append(
                yearly_contributions(
                    policy,
                    candidate,
                    window_candidate_returns[(candidate, "2021-2023")],
                    "2021-2023",
                    False,
                )
            )
            yearly_frames.append(
                yearly_contributions(
                    policy,
                    candidate,
                    history_returns,
                    "2014-2023",
                    True,
                )
            )
            if len(np.unique(report_index.year)) >= 1:
                yearly_frames.append(
                    yearly_contributions(
                        policy,
                        candidate,
                        window_candidate_returns[
                            (candidate, "2024-2026-report-only")
                        ],
                        "2024-2026-report-only",
                        False,
                    )
                )

    production = _validate_production_output(
        pd.DataFrame(production_rows, columns=PRODUCTION_METRICS_COLUMNS),
        policies,
        report_present=bool(len(report_index)),
    )
    bootstrap = _validate_bootstrap_output(
        pd.DataFrame(bootstrap_rows, columns=BOOTSTRAP_COLUMNS),
        policies,
    )
    yearly = _validate_yearly_output(
        pd.concat(yearly_frames, ignore_index=True)[YEARLY_COLUMNS],
        policies,
        report_present=bool(len(report_index)),
    )
    return EvaluationFrames(
        production_metrics=production,
        bootstrap=bootstrap,
        yearly=yearly,
    )
