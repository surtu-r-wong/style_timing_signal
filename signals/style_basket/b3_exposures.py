from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signals.common.factors import cross_section_zscore, winsorize


class DataBlocked(ValueError):
    """Required source data is absent or violates the eligibility contract."""


class CoverageBlocked(ValueError):
    """Legal data exclusions leave too little usable cross-sectional coverage."""


class NumericalFailure(RuntimeError):
    """A numerical invariant failed after otherwise valid input was accepted."""


@dataclass(frozen=True)
class ExposureResult:
    size: pd.DataFrame
    model: pd.DataFrame
    q: dict[str, float]
    diagnostics: dict[str, float | int]


def _industry_design(industry: pd.Series) -> pd.DataFrame:
    labels = industry.fillna("UNKNOWN").astype(str)
    dummies = pd.get_dummies(labels, dtype=float)
    dummies = dummies.reindex(sorted(dummies.columns), axis=1)
    if dummies.shape[1] >= 1:
        dummies = dummies.iloc[:, 1:]
    dummies.columns = [f"industry={label}" for label in dummies.columns]
    intercept = pd.Series(1.0, index=industry.index, name="intercept")
    return pd.concat([intercept, dummies], axis=1)


def _residualize(
    y: pd.Series, controls: pd.DataFrame, label: str
) -> tuple[pd.Series, float]:
    joined = pd.concat([y.rename("y"), controls], axis=1)
    complete = joined.dropna()
    if len(complete) != len(joined):
        raise NumericalFailure(
            f"{label} residualization has missing inputs for "
            f"{len(joined) - len(complete)} rows"
        )
    joined = complete
    target = joined["y"].to_numpy(dtype=float)
    design = joined[controls.columns].to_numpy(dtype=float)

    if not np.isfinite(target).all() or not np.isfinite(design).all():
        raise NumericalFailure(
            f"{label} residualization contains non-finite inputs"
        )

    rank = int(np.linalg.matrix_rank(design))
    if rank < design.shape[1]:
        raise CoverageBlocked(
            f"{label} residualization is rank deficient "
            f"({rank} < {design.shape[1]})"
        )

    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    raw = pd.Series(
        target - design @ coefficients,
        index=joined.index,
        name=label,
        dtype=float,
    )
    sample_sd = float(raw.std(ddof=1))
    if not np.isfinite(sample_sd) or sample_sd < 1e-12:
        raise CoverageBlocked(f"{label} residual has no finite nonzero sample sd")

    residual = ((raw - raw.mean()) / sample_sd).rename(label)
    residual_values = residual.to_numpy(dtype=float)
    residual_norm = float(np.linalg.norm(residual_values))
    control_errors: list[float] = []
    for position, column in enumerate(controls.columns):
        if column == "intercept":
            continue
        control_values = design[:, position]
        denominator = residual_norm * float(np.linalg.norm(control_values))
        error = (
            abs(float(np.dot(residual_values, control_values))) / denominator
            if denominator > 0.0
            else 0.0
        )
        control_errors.append(error)
    return residual, max(control_errors, default=0.0)


def _capped_weights(
    exposure: pd.Series, positive: bool, cap: float, min_members: int
) -> pd.Series:
    try:
        exposure_values = exposure.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise NumericalFailure("leg exposure must be numeric and finite") from exc
    if not np.isfinite(exposure_values).all():
        raise NumericalFailure("leg exposure contains non-finite values")

    raw = exposure if positive else -exposure
    raw = raw.clip(lower=0.0)
    members = raw[raw > 0.0].sort_index().astype(float)
    member_count = int(len(members))
    if member_count < min_members:
        raise CoverageBlocked(
            f"leg has {member_count} members; minimum is {min_members}"
        )
    weights = pd.Series(0.0, index=exposure.index, dtype=float)
    remaining = members
    residual_mass = 1.0
    while not remaining.empty:
        raw_mass = float(remaining.sum())
        if not np.isfinite(raw_mass) or raw_mass <= 0.0:
            raise CoverageBlocked("leg raw mass was exhausted before normalization")

        proposed = remaining * (residual_mass / raw_mass)
        capped = proposed[proposed > cap]
        if capped.empty:
            weights.loc[remaining.index] = proposed
            residual_mass = 0.0
            remaining = remaining.iloc[0:0]
            break

        weights.loc[capped.index] = cap
        residual_mass -= cap * len(capped)
        remaining = remaining.drop(index=capped.index)
        if residual_mass < -1e-12:
            raise NumericalFailure("water filling produced negative residual mass")

    if remaining.empty and residual_mass > 1e-10:
        raise CoverageBlocked(
            f"weight cap {cap} cannot allocate the remaining mass "
            f"across {member_count} members"
        )

    total_deviation = abs(float(weights.sum()) - 1.0)
    max_weight = float(weights.max()) if len(weights) else 0.0
    if total_deviation > 1e-10 or max_weight > cap + 1e-12:
        raise NumericalFailure(
            "capped weights violate normalization or maximum-weight invariants"
        )
    return weights


def _target_coordinates(
    size: pd.DataFrame, bands: dict[str, list[int]]
) -> dict[str, float]:
    q1000_upper = int(bands["q1000"][1])
    if len(size) < q1000_upper:
        raise DataBlocked(
            f"q1000 target rank requires {q1000_upper} names; got {len(size)}"
        )

    ordered = size.reset_index(drop=True).copy()
    ordered["_target_market_value"] = pd.to_numeric(
        ordered["total_market_value"], errors="coerce"
    )
    ordered = ordered.sort_values(
        ["_target_market_value", "ticker"],
        ascending=[False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    q: dict[str, float] = {}
    for name in ("q500", "q1000"):
        lower, upper = (int(value) for value in bands[name])
        q[name] = float(ordered.iloc[lower - 1 : upper]["m_perp"].median())
    q["qblend"] = float((q["q500"] + q["q1000"]) / 2.0)
    return q


def _eligibility_masks(
    frame: pd.DataFrame,
) -> tuple[pd.Series, pd.Series]:
    if "size_eligible" not in frame.columns:
        size_mask = pd.Series(True, index=frame.index, dtype=bool)
        model_mask = frame["style_score"].notna()
        return size_mask, model_mask

    contract_columns = {
        "size_eligible",
        "model_eligible",
        "size_exclusion_reason",
        "model_exclusion_reason",
    }
    missing = sorted(contract_columns.difference(frame.columns))
    if missing:
        raise DataBlocked(
            "eligibility contract is missing columns: " + ", ".join(missing)
        )

    size_reasons = frame["size_exclusion_reason"].fillna("").astype(str)
    model_reasons = frame["model_exclusion_reason"].fillna("").astype(str)

    data_codes = sorted(
        {
            reason
            for reason in pd.concat(
                [size_reasons, model_reasons], ignore_index=True
            ).unique()
            if reason.startswith("DATA_")
        }
    )
    if data_codes:
        raise DataBlocked("data-blocked exclusion reasons: " + ", ".join(data_codes))

    for column in ("size_eligible", "model_eligible"):
        valid_flags = frame[column].map(
            lambda value: isinstance(value, (bool, np.bool_))
        )
        if not valid_flags.all():
            invalid_tickers = ", ".join(
                map(str, frame.index[~valid_flags][:5])
            )
            raise DataBlocked(
                f"{column} must contain only non-null bool values; "
                f"invalid tickers: {invalid_tickers}"
            )

    size_mask = frame["size_eligible"].astype(bool)
    model_mask = frame["model_eligible"].astype(bool)

    valid_size_reasons = {"", "LISTED_LT_180D"}
    unknown_size = sorted(set(size_reasons).difference(valid_size_reasons))
    if unknown_size:
        raise DataBlocked(
            "unknown size exclusion reasons: " + ", ".join(unknown_size)
        )

    valid_model_reasons = {"", "LISTED_LT_180D", "MISSING_STYLE_SCORE"}
    unknown_model = sorted(set(model_reasons).difference(valid_model_reasons))
    if unknown_model:
        raise DataBlocked(
            "unknown model exclusion reasons: " + ", ".join(unknown_model)
        )

    if (size_mask & size_reasons.ne("")).any():
        raise DataBlocked("size-eligible names require a blank exclusion reason")
    if (model_mask & model_reasons.ne("")).any():
        raise DataBlocked("model-eligible names require a blank exclusion reason")
    if ((~size_mask) & size_reasons.eq("")).any():
        raise DataBlocked("size exclusion is unexplained by an allowed reason")
    if ((~model_mask) & model_reasons.eq("")).any():
        raise DataBlocked("model exclusion is unexplained by an allowed reason")
    if (model_mask & ~size_mask).any():
        raise DataBlocked("model eligibility cannot include a size-ineligible name")
    return size_mask, model_mask


def compute_month_exposures(
    snapshot: pd.DataFrame, cfg: dict
) -> ExposureResult:
    required = {
        "ticker",
        "formation_date",
        "total_market_value",
        "industry",
        "style_score",
    }
    if not isinstance(snapshot, pd.DataFrame):
        raise DataBlocked("snapshot must be a DataFrame")
    missing = sorted(required.difference(snapshot.columns))
    if missing:
        raise DataBlocked("snapshot is missing required columns: " + ", ".join(missing))

    frame = snapshot.copy()
    if frame["ticker"].isna().any():
        raise DataBlocked("ticker contains missing values")
    if frame["ticker"].duplicated().any():
        raise DataBlocked("snapshot contains duplicate ticker values")
    try:
        frame = frame.sort_values("ticker", kind="mergesort")
    except (TypeError, ValueError) as exc:
        raise DataBlocked("ticker values cannot be sorted deterministically") from exc
    frame = frame.set_index("ticker", drop=False)

    size_mask, model_mask = _eligibility_masks(frame)
    size = frame.loc[size_mask].copy()
    model = frame.loc[model_mask].copy()

    numeric_style = pd.to_numeric(model["style_score"], errors="coerce")
    valid_style = np.isfinite(numeric_style.to_numpy(dtype=float))
    if not valid_style.all():
        invalid_tickers = ", ".join(map(str, model.index[~valid_style][:5]))
        raise DataBlocked(
            "model-universe style_score must be numeric and finite; "
            f"invalid tickers: {invalid_tickers}"
        )

    market_value = pd.to_numeric(size["total_market_value"], errors="coerce")
    market_values = market_value.to_numpy(dtype=float)
    if not (
        np.isfinite(market_values).all() and (market_values > 0.0).all()
    ):
        raise DataBlocked(
            "size-universe total_market_value must be finite and positive"
        )

    exposure_cfg = cfg["exposure"]
    lower = float(exposure_cfg["winsor_lower"])
    upper = float(exposure_cfg["winsor_upper"])
    tolerance = float(exposure_cfg["orthogonality_tolerance"])
    portfolio_cfg = cfg["portfolio"]
    cap = float(portfolio_cfg["weight_cap"])
    min_leg_size = int(portfolio_cfg["min_leg_size"])

    size["m"] = -cross_section_zscore(
        winsorize(np.log(market_value), lower=lower, upper=upper)
    )
    size["m_perp"], m_error = _residualize(
        size["m"], _industry_design(size["industry"]), "m_perp"
    )

    if len(model) < 2 * min_leg_size:
        raise CoverageBlocked(
            f"model universe has {len(model)} names; two {min_leg_size}-name "
            f"legs require at least {2 * min_leg_size}"
        )

    model["m"] = size["m"].reindex(model.index)
    model["m_perp"] = size["m_perp"].reindex(model.index)
    model["style_z"] = cross_section_zscore(
        winsorize(numeric_style, lower=lower, upper=upper)
    )

    style_controls = _industry_design(model["industry"])
    style_controls["m"] = model["m"]
    model["s_perp"], s_error = _residualize(
        model["style_z"], style_controls, "s_perp"
    )

    model["h_raw"] = winsorize(
        model["s_perp"] * model["m_perp"], lower=lower, upper=upper
    )
    interaction_controls = _industry_design(model["industry"])
    interaction_controls["s_perp"] = model["s_perp"]
    interaction_controls["m_perp"] = model["m_perp"]
    model["h_perp"], h_error = _residualize(
        model["h_raw"], interaction_controls, "h_perp"
    )

    q = _target_coordinates(size, portfolio_cfg["q_bands"])
    for name in ("qblend", "q500", "q1000"):
        model[f"x_{name}"] = model["s_perp"] + q[name] * model["h_perp"]

    size["w_size_plus"] = _capped_weights(
        size["m_perp"], positive=True, cap=cap, min_members=min_leg_size
    )
    size["w_size_minus"] = _capped_weights(
        size["m_perp"], positive=False, cap=cap, min_members=min_leg_size
    )
    model["w_size_plus"] = size["w_size_plus"].reindex(model.index)
    model["w_size_minus"] = size["w_size_minus"].reindex(model.index)

    axes = {
        "style": model["s_perp"],
        "interaction": model["h_perp"],
        "qblend": model["x_qblend"],
        "q500": model["x_q500"],
        "q1000": model["x_q1000"],
    }
    for axis, exposure in axes.items():
        model[f"w_{axis}_plus"] = _capped_weights(
            exposure, positive=True, cap=cap, min_members=min_leg_size
        )
        model[f"w_{axis}_minus"] = _capped_weights(
            exposure, positive=False, cap=cap, min_members=min_leg_size
        )

    max_error = max(m_error, s_error, h_error)
    if max_error > tolerance:
        raise NumericalFailure(
            f"orthogonality error {max_error:.6g} exceeds tolerance {tolerance:.6g}"
        )

    diagnostics: dict[str, float | int] = {
        "input_n": int(len(frame)),
        "size_excluded_n": int(len(frame) - len(size)),
        "model_excluded_n": int(len(frame) - len(model)),
        "size_n": int(len(size)),
        "model_n": int(len(model)),
        "m_orthogonality_error": float(m_error),
        "s_orthogonality_error": float(s_error),
        "h_orthogonality_error": float(h_error),
        "max_orthogonality_error": float(max_error),
        **{name: float(value) for name, value in q.items()},
    }
    return ExposureResult(size=size, model=model, q=q, diagnostics=diagnostics)
