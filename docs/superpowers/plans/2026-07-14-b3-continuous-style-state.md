# B3 Continuous Size × Style State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the frozen B3 research pipeline that constructs continuous orthogonal size/style/interaction baskets, decomposes growth-versus-value returns into UU/DD/DIV states, evaluates the two pre-registered long-flat candidates against equal_weight on the same execution scale, and emits a fail-closed statistical/final verdict.

**Architecture:** Keep the existing B1/B2 and production signal code untouched. Add four pure research layers under `signals/style_basket/` (configuration/exposures, buy-and-hold portfolios, state features, staged orchestration), then add structure and production evaluation under `backtest/`. Every stage consumes a hash-checked predecessor manifest; preflight reads no forward stock/index returns, while later stages refuse to run unless the same config hash and data cutoff passed preflight.

**Tech Stack:** Python 3, Pandas, NumPy, PyYAML, PostgreSQL/psycopg2, pytest; reuse `backtest.engine`, `backtest.metrics`, `backtest.positions.production_position`, and `backtest.rotation_probe.partial_rank_ic`. Do not add statsmodels or SciPy.

**Frozen design:** `docs/superpowers/specs/2026-07-13-b3-continuous-style-state-design.md`

---

## Scope and execution facts

This is one sequential subsystem: exposures feed portfolios, portfolios feed states, states feed structure fitting, and frozen scores feed the same-scale evaluation. It therefore stays in one implementation plan.

Files to create:

- `signals/style_basket/b3_config.yaml` — the only research-parameter source.
- `signals/style_basket/b3_config.py` — strict loader, validation, canonical hash.
- `signals/style_basket/b3_exposures.py` — pure cross-sectional orthogonalization, target coordinates, capped weights.
- `signals/style_basket/b3_portfolios.py` — natural-drift holding-period returns and missing-price contract.
- `signals/style_basket/b3_states.py` — UU/DD/DIV decomposition and causal 20/40/5 transforms.
- `signals/style_basket/b3_build.py` — PIT policies, database reads, preflight, stage manifests, research caches.
- `backtest/b3_structure.py` — hard-sort audit, Fama–MacBeth-style validation, M0/M1, structure gates.
- `backtest/b3_eval.py` — candidate returns, paired moving-block tail probabilities, Holm-style adjustment, verdicts.
- `tests/test_b3_exposures.py`, `tests/test_b3_portfolios_states.py`, `tests/test_b3_structure.py`, `tests/test_b3_eval.py`.

Files to modify:

- `.gitignore` — ignore `output/style_basket/b3/`.

Files explicitly not modified:

- `signals/style_basket/build.py`, `signals/style_basket/scoring.py`, existing B1/B2 committed CSVs.
- `signals/equal_weight/`, `backtest/production.py`, and all current recommended-position outputs.
- No `b3_shadow.py`; actual shadow infrastructure requires a later plan.

Known data facts verified read-only on 2026-07-14:

1. `index_constituent` contains `000905.SH` but no code matching `000852`/中证1000. Therefore the current real-data preflight must emit `DATA_BLOCKED` for q1000 calibration before reading any return. This plan implements the honest blocker; it does not invent constituents or silently skip q1000.
2. `stock_status` is daily only from 2025 onward and sparse before that. A held-stock price gap is treated as a suspension only when the exact date has `stock_status.is_suspended=true` or an explicit zero-volume qfq row. Any other gap is `DATA_BLOCKED`.
3. True CSMAR first-disclosure dates are absent, SalG freezes after 2025Q2, and carry ends at 2026-04-29. Even after historical q1000 calibration data are supplied, these keep `final_verdict=DATA_BLOCKED`; a legally computed approximate-PIT `statistical_verdict` remains reportable.

Do not widen implementation scope to repair these upstream datasets. A separate data plan is required if the user wants the real-data pipeline to progress beyond the corresponding blocker.

### Output schemas

`output/style_basket/b3/` (gitignored):

- `monthly_exposures.csv.gz`: `pit_policy, formation_date, ticker, universe_role, industry, total_market_value, m, m_perp, s_perp, h_perp, x_qblend, x_q500, x_q1000, size_eligible, model_eligible, size_exclusion_reason, model_exclusion_reason, salg_source_end_date, true_first_disclosure_verified`, followed by `w_<axis>_<plus|minus>` for style/size/interaction/qblend/q500/q1000.
- `coverage_audit.csv`: `pit_policy, formation_date, required_formation, affects_final, check, side, eligible_count, max_weight, status, reason_code, detail`. The 2014–2023 formation months are required; earlier signal-warmup rows are reported but cannot by themselves set the final blocker.
- `exposure_diagnostics.csv`: sample sizes, design rank, residual standard deviations, orthogonality errors, q values and coordinate-calibration errors.
- `axis_returns.csv`: `date, pit_policy, style, size, interaction`.
- `conditional_leg_returns.csv`: `date, pit_policy, q, growth_ret, value_ret`.
- `state_components.csv`: daily log legs, `d/d_UU/d_DD/d_DIV`, raw 20-day components, `F_U/F_D/F_X/F_T`, state labels and external direction labels.
- `hard_sort_surface.csv`: every formation month/grid/cell, membership count, industry distribution, holding-period return and derived corner/row diagnostics.
- Internal caches `stock_period_returns.csv.gz` and `manifests/<stage>.json`; they are not committed products.

`backtest/output/b3/` (compact, only considered for Git after the user reviews the research run):

- `structure_coefficients.csv`, `model_comparison.csv`, `production_metrics.csv`, `yearly_contribution.csv`, `bootstrap.csv`, `verdicts.csv`, `run_manifest.json` with the schemas frozen in Tasks 7–10.

### Exit semantics

- Exit 0: requested stage completed.
- Exit 2: `DATA_BLOCKED`; write all legally available audit/manifest rows, then stop.
- Exit 3: `COVERAGE_BLOCKED`; write all legally available audit/manifest rows, then stop.
- Exit 1: implementation/numerical error; do not manufacture an economic verdict.

## Task 1: Freeze configuration and config hashing

**Files:**
- Create: `signals/style_basket/b3_config.yaml`
- Create: `signals/style_basket/b3_config.py`
- Create: `tests/test_b3_exposures.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing configuration-contract tests**

Add these imports and tests to `tests/test_b3_exposures.py`:

```python
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from signals.style_basket.b3_config import config_hash, load_b3_config


def test_b3_config_freezes_candidates_windows_and_execution():
    cfg = load_b3_config()
    assert cfg["candidates"] == ["B3_unified", "B3_dual_target"]
    assert cfg["windows"] == {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    assert cfg["execution"]["cost_bps"] == 3.0
    assert cfg["execution"]["annualization"] == 245
    assert cfg["portfolio"]["weight_cap"] == 0.01
    assert cfg["portfolio"]["min_leg_size"] == 100
    assert cfg["bootstrap"] == {
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "adjusted_tail_max": 0.10,
    }


def test_b3_config_hash_is_order_independent_and_value_sensitive():
    cfg = load_b3_config()
    reordered = {key: cfg[key] for key in reversed(list(cfg))}
    assert config_hash(cfg) == config_hash(reordered)
    changed = deepcopy(cfg)
    changed["signal"]["z_window"] = 41
    assert config_hash(cfg) != config_hash(changed)


def test_b3_config_rejects_candidate_expansion(tmp_path: Path):
    cfg = load_b3_config()
    cfg["candidates"].append("B3_after_the_fact")
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly"):
        load_b3_config(path)
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: collection fails with `ModuleNotFoundError: signals.style_basket.b3_config`.

- [ ] **Step 3: Add the frozen YAML**

Create `signals/style_basket/b3_config.yaml` exactly as:

```yaml
version: 1
candidates:
  - B3_unified
  - B3_dual_target
windows:
  discovery: ["2014-01-01", "2020-12-31"]
  confirmation: ["2021-01-01", "2023-12-31"]
  report_only: ["2024-01-01", "2026-12-31"]
pit:
  policies: ["legal_deadline", "legal_deadline_plus_one_month_end"]
  industry_pit_start: "2021-01-01"
exposure:
  winsor_lower: 0.05
  winsor_upper: 0.95
  orthogonality_tolerance: 1.0e-8
  identity_tolerance: 1.0e-12
portfolio:
  weight_cap: 0.01
  min_leg_size: 100
  q_bands:
    q500: [301, 800]
    q1000: [801, 1800]
signal:
  raw_window: 20
  z_window: 40
  tanh_scale: 2.0
  smoothing_window: 5
model:
  newey_west_lag: 3
  state_min_coverage: 0.10
  stability_score_spearman_min: 0.50
  interaction_axis_corr_max: 0.80
execution:
  annualization: 245
  cost_bps: 3.0
  im_launch_date: "2022-07-22"
  ic_launch_date: "2015-04-16"
production_gates:
  sharpe_improvement: 0.10
  maxdd_worsening: 0.02
  turnover_multiple: 1.50
  post_im_min_days: 252
shadow:
  unified_min_days: 120
  dual_target_min_days: 252
bootstrap:
  block_days: 20
  draws: 5000
  seed: 20260713
  adjusted_tail_max: 0.10
```

- [ ] **Step 4: Implement strict loading and canonical hashing**

Create `signals/style_basket/b3_config.py`:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("b3_config.yaml")
EXPECTED_CANDIDATES = ["B3_unified", "B3_dual_target"]
EXPECTED_TOP_LEVEL = {
    "version", "candidates", "windows", "pit", "exposure", "portfolio",
    "signal", "model", "execution", "production_gates", "shadow", "bootstrap",
}


def validate_b3_config(cfg: dict) -> None:
    if set(cfg) != EXPECTED_TOP_LEVEL:
        raise ValueError(
            f"B3 config top-level keys must be exactly {sorted(EXPECTED_TOP_LEVEL)}"
        )
    if cfg["version"] != 1:
        raise ValueError("B3 config version must be 1")
    if cfg["candidates"] != EXPECTED_CANDIDATES:
        raise ValueError(f"B3 candidates must be exactly {EXPECTED_CANDIDATES}")
    if cfg["pit"]["policies"] != [
        "legal_deadline", "legal_deadline_plus_one_month_end"
    ]:
        raise ValueError("B3 PIT policies are frozen")
    if cfg["portfolio"]["weight_cap"] != 0.01:
        raise ValueError("B3 weight_cap must be 0.01")
    if cfg["portfolio"]["min_leg_size"] != 100:
        raise ValueError("B3 min_leg_size must be 100")
    if cfg["bootstrap"]["draws"] != 5000:
        raise ValueError("B3 bootstrap draws must be 5000")


def load_b3_config(path: str | Path = CONFIG_PATH) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("B3 config must be a mapping")
    validate_b3_config(cfg)
    return cfg


def config_hash(cfg: dict) -> str:
    canonical = json.dumps(
        cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Append this exact ignore rule to `.gitignore`:

```gitignore
output/style_basket/b3/
```

- [ ] **Step 5: Run the configuration tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add .gitignore signals/style_basket/b3_config.yaml signals/style_basket/b3_config.py tests/test_b3_exposures.py
git commit -m "feat(b3): freeze research configuration"
```

## Task 2: Implement continuous orthogonal exposures and capped legs

**Files:**
- Create: `signals/style_basket/b3_exposures.py`
- Modify: `tests/test_b3_exposures.py`

- [ ] **Step 1: Add failing exposure, q-coordinate, and cap tests**

Append:

```python
import numpy as np
import pandas as pd

from signals.style_basket.b3_exposures import (
    CoverageBlocked,
    compute_month_exposures,
)


def _synthetic_snapshot(n: int = 2200) -> pd.DataFrame:
    rng = np.random.default_rng(20260713)
    ticker = [f"S{i:04d}" for i in range(n)]
    log_mv = np.linspace(16.0, 8.0, n) + rng.normal(0.0, 0.03, n)
    industry = np.where(np.arange(n) % 2 == 0, "电子", "医药")
    style = (
        0.4 * (industry == "电子").astype(float)
        + 0.25 * (log_mv - log_mv.mean())
        + rng.normal(0.0, 1.0, n)
    )
    return pd.DataFrame({
        "ticker": ticker,
        "formation_date": pd.Timestamp("2021-01-29"),
        "total_market_value": np.exp(log_mv),
        "industry": industry,
        "style_score": style,
    })


def test_month_exposures_are_orthogonal_and_row_order_invariant():
    cfg = load_b3_config()
    snap = _synthetic_snapshot()
    got = compute_month_exposures(snap, cfg)
    shuffled = compute_month_exposures(
        snap.sample(frac=1.0, random_state=7), cfg
    )
    assert got.diagnostics["max_orthogonality_error"] <= 1.0e-8
    assert abs(got.model["s_perp"].mean()) <= 1.0e-12
    assert got.model["s_perp"].std(ddof=1) == pytest.approx(1.0)
    assert got.model["h_perp"].std(ddof=1) == pytest.approx(1.0)
    pd.testing.assert_frame_equal(
        got.model.sort_index(), shuffled.model.sort_index(), check_like=True
    )


def test_target_coordinates_use_rank_bands_but_weights_use_full_model_universe():
    got = compute_month_exposures(_synthetic_snapshot(), load_b3_config())
    assert got.q["q1000"] > got.q["q500"]
    outside = got.model.iloc[1900:]
    assert (outside["w_q1000_plus"] > 0).any() or (
        outside["w_q1000_minus"] > 0
    ).any()


def test_every_leg_is_normalized_capped_and_has_at_least_100_names():
    got = compute_month_exposures(_synthetic_snapshot(), load_b3_config())
    for axis in ["style", "size", "interaction", "qblend", "q500", "q1000"]:
        for side in ["plus", "minus"]:
            universe = got.size if axis == "size" else got.model
            weights = universe[f"w_{axis}_{side}"]
            assert weights.sum() == pytest.approx(1.0)
            assert weights.max() <= 0.01 + 1.0e-12
            assert (weights > 0).sum() >= 100


def test_thin_legal_cross_section_raises_coverage_blocked():
    with pytest.raises(CoverageBlocked, match="100"):
        compute_month_exposures(_synthetic_snapshot(180), load_b3_config())


def test_missing_source_field_is_data_blocked_not_coverage_blocked():
    snap = _synthetic_snapshot()
    snap["size_eligible"] = True
    snap["model_eligible"] = True
    snap["size_exclusion_reason"] = ""
    snap["model_exclusion_reason"] = ""
    snap.loc[0, ["size_eligible", "model_eligible"]] = False
    snap.loc[0, "size_exclusion_reason"] = "DATA_MISSING_CLOSE"
    snap.loc[0, "model_exclusion_reason"] = "DATA_MISSING_CLOSE"
    with pytest.raises(DataBlocked, match="DATA_MISSING_CLOSE"):
        compute_month_exposures(snap, load_b3_config())


def test_explained_legal_exclusions_can_end_as_coverage_blocked():
    snap = _synthetic_snapshot()
    snap["size_eligible"] = False
    snap["model_eligible"] = False
    snap["size_exclusion_reason"] = "LISTED_LT_180D"
    snap["model_exclusion_reason"] = "LISTED_LT_180D"
    snap.loc[:179, ["size_eligible", "model_eligible"]] = True
    snap.loc[:179, ["size_exclusion_reason", "model_exclusion_reason"]] = ""
    with pytest.raises(CoverageBlocked):
        compute_month_exposures(snap, load_b3_config())
```

- [ ] **Step 2: Run the new tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: collection fails with `ModuleNotFoundError: signals.style_basket.b3_exposures`.

- [ ] **Step 3: Implement residualization, q coordinates and deterministic water-filling**

Create `signals/style_basket/b3_exposures.py` with these public contracts:

```python
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signals.common.factors import cross_section_zscore, winsorize


class DataBlocked(ValueError):
    pass


class CoverageBlocked(ValueError):
    pass


class NumericalFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class ExposureResult:
    size: pd.DataFrame
    model: pd.DataFrame
    q: dict[str, float]
    diagnostics: dict[str, float | int]


def _industry_design(industry: pd.Series) -> pd.DataFrame:
    labels = industry.fillna("UNKNOWN").astype(str)
    dummies = pd.get_dummies(labels, dtype=float).sort_index(axis=1)
    if dummies.shape[1] > 1:
        dummies = dummies.iloc[:, 1:]
    return pd.concat(
        [pd.Series(1.0, index=labels.index, name="intercept"), dummies], axis=1
    )


def _residualize(
    y: pd.Series, controls: pd.DataFrame, label: str
) -> tuple[pd.Series, float]:
    frame = pd.concat([y.rename("y"), controls], axis=1).dropna()
    x = frame.drop(columns="y").to_numpy(dtype=float)
    values = frame["y"].to_numpy(dtype=float)
    rank = np.linalg.matrix_rank(x)
    if rank != x.shape[1]:
        raise CoverageBlocked(f"{label} design matrix rank deficient")
    beta, _, _, _ = np.linalg.lstsq(x, values, rcond=None)
    resid = pd.Series(values - x @ beta, index=frame.index, name=label)
    sd = resid.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        raise CoverageBlocked(f"{label} residual standard deviation is zero")
    resid = (resid - resid.mean()) / sd
    denom = np.sqrt((x * x).sum(axis=0) * (resid.to_numpy() ** 2).sum())
    inner = np.divide(
        np.abs(x.T @ resid.to_numpy()),
        denom,
        out=np.zeros(x.shape[1], dtype=float),
        where=denom > 0,
    )
    return resid, float(inner[1:].max(initial=0.0))


def _capped_weights(
    exposure: pd.Series, positive: bool, cap: float, min_members: int
) -> pd.Series:
    raw = exposure.clip(lower=0.0) if positive else (-exposure).clip(lower=0.0)
    raw = raw[raw > 0].sort_index()
    if len(raw) < min_members:
        raise CoverageBlocked(
            f"leg has {len(raw)} names; at least {min_members} are required"
        )
    weights = pd.Series(0.0, index=exposure.index)
    remaining = raw.copy()
    residual_mass = 1.0
    while len(remaining):
        proposed = remaining / remaining.sum() * residual_mass
        capped = proposed > cap + 1.0e-15
        if not capped.any():
            weights.loc[proposed.index] = proposed
            break
        hit = proposed.index[capped]
        weights.loc[hit] = cap
        residual_mass = 1.0 - float(weights.sum())
        remaining = remaining.drop(index=hit)
    if len(remaining) == 0 and residual_mass > 1.0e-10:
        raise CoverageBlocked("weight cap cannot be normalized")
    if abs(float(weights.sum()) - 1.0) > 1.0e-10:
        raise NumericalFailure("leg weights do not sum to one")
    if float(weights.max()) > cap + 1.0e-12:
        raise NumericalFailure("leg weight exceeds cap")
    return weights


def _target_coordinates(
    size: pd.DataFrame, bands: dict[str, list[int]]
) -> dict[str, float]:
    ranked = size.sort_values(
        ["total_market_value", "ticker"], ascending=[False, True]
    ).reset_index(drop=True)
    if len(ranked) < bands["q1000"][1]:
        raise DataBlocked("q1000 rank band requires at least 1800 size names")
    q = {}
    for name, (lo, hi) in bands.items():
        q[name] = float(ranked.iloc[lo - 1:hi]["m_perp"].median())
    q["qblend"] = (q["q500"] + q["q1000"]) / 2.0
    return q


def compute_month_exposures(snapshot: pd.DataFrame, cfg: dict) -> ExposureResult:
    required = {
        "ticker", "formation_date", "total_market_value", "industry", "style_score"
    }
    missing = required - set(snapshot)
    if missing:
        raise DataBlocked(f"snapshot missing columns: {sorted(missing)}")
    if snapshot["ticker"].duplicated().any():
        raise DataBlocked("snapshot ticker must be unique")
    snap = snapshot.copy().sort_values("ticker").set_index("ticker", drop=False)
    input_n = len(snap)
    if "size_eligible" in snap:
        required_flags = {
            "model_eligible", "size_exclusion_reason", "model_exclusion_reason"
        }
        if required_flags - set(snap):
            raise DataBlocked("snapshot eligibility contract is incomplete")
        known_size_reasons = {"", "LISTED_LT_180D"}
        reasons = set(snap["size_exclusion_reason"].fillna(""))
        unknown = reasons - known_size_reasons
        data_reasons = sorted(reason for reason in reasons if reason.startswith("DATA_"))
        if data_reasons:
            raise DataBlocked(f"source exclusions present: {data_reasons}")
        if unknown:
            raise DataBlocked(f"unknown size exclusion reasons: {sorted(unknown)}")
        unexplained = ~snap["size_eligible"] & (
            snap["size_exclusion_reason"].fillna("") == ""
        )
        if unexplained.any():
            raise DataBlocked("size exclusion lacks a registered reason")
        size_mask = snap["size_eligible"].astype(bool)
        model_mask = snap["model_eligible"].astype(bool)
        if (model_mask & ~size_mask).any():
            raise DataBlocked("model universe must be a subset of size universe")
        model_reasons = set(snap["model_exclusion_reason"].fillna(""))
        allowed_model_reasons = {"", "LISTED_LT_180D", "MISSING_STYLE_SCORE"}
        model_data_reasons = sorted(
            reason for reason in model_reasons if reason.startswith("DATA_")
        )
        if model_data_reasons:
            raise DataBlocked(
                f"model source exclusions present: {model_data_reasons}"
            )
        unknown_model = model_reasons - allowed_model_reasons - set(
            model_data_reasons
        )
        if unknown_model:
            raise DataBlocked(
                f"unknown model exclusion reasons: {sorted(unknown_model)}"
            )
        unexplained_model = ~model_mask & (
            snap["model_exclusion_reason"].fillna("") == ""
        )
        if unexplained_model.any():
            raise DataBlocked("model exclusion lacks a registered reason")
    else:
        size_mask = pd.Series(True, index=snap.index)
        model_mask = snap["style_score"].notna()
    valid_mv = np.isfinite(snap.loc[size_mask, "total_market_value"]) & (
        snap.loc[size_mask, "total_market_value"] > 0
    )
    if not valid_mv.all():
        raise DataBlocked("size universe contains invalid total_market_value")
    lower = cfg["exposure"]["winsor_lower"]
    upper = cfg["exposure"]["winsor_upper"]

    size = snap.loc[size_mask].copy()
    log_mv = np.log(size["total_market_value"])
    size["m"] = -cross_section_zscore(winsorize(log_mv, lower, upper))
    m_perp, m_error = _residualize(
        size["m"], _industry_design(size["industry"]), "m_perp"
    )
    size["m_perp"] = m_perp

    model = snap.loc[model_mask].copy()
    if len(model) < cfg["portfolio"]["min_leg_size"] * 2:
        raise CoverageBlocked("model universe cannot support two 100-name legs")
    model["m"] = size["m"].reindex(model.index)
    model["m_perp"] = size["m_perp"].reindex(model.index)
    style_z = cross_section_zscore(
        winsorize(model["style_score"], lower, upper)
    )
    s_controls = pd.concat(
        [_industry_design(model["industry"]), model[["m"]]], axis=1
    )
    model["s_perp"], s_error = _residualize(
        style_z, s_controls, "s_perp"
    )
    h_raw = winsorize(model["s_perp"] * model["m_perp"], lower, upper)
    h_controls = pd.concat(
        [
            _industry_design(model["industry"]),
            model[["s_perp", "m_perp"]],
        ],
        axis=1,
    )
    model["h_perp"], h_error = _residualize(
        h_raw, h_controls, "h_perp"
    )

    q = _target_coordinates(size, cfg["portfolio"]["q_bands"])
    model["x_qblend"] = model["s_perp"] + q["qblend"] * model["h_perp"]
    model["x_q500"] = model["s_perp"] + q["q500"] * model["h_perp"]
    model["x_q1000"] = model["s_perp"] + q["q1000"] * model["h_perp"]
    model_axes = {
        "style": model["s_perp"],
        "interaction": model["h_perp"],
        "qblend": model["x_qblend"],
        "q500": model["x_q500"],
        "q1000": model["x_q1000"],
    }
    cap = cfg["portfolio"]["weight_cap"]
    minimum = cfg["portfolio"]["min_leg_size"]
    size["w_size_plus"] = _capped_weights(
        size["m_perp"], True, cap, minimum
    )
    size["w_size_minus"] = _capped_weights(
        size["m_perp"], False, cap, minimum
    )
    model["w_size_plus"] = size["w_size_plus"].reindex(model.index)
    model["w_size_minus"] = size["w_size_minus"].reindex(model.index)
    for name, exposure in model_axes.items():
        model[f"w_{name}_plus"] = _capped_weights(
            exposure, True, cap, minimum
        )
        model[f"w_{name}_minus"] = _capped_weights(
            exposure, False, cap, minimum
        )
    max_error = max(m_error, s_error, h_error)
    if max_error > cfg["exposure"]["orthogonality_tolerance"]:
        raise NumericalFailure(
            f"orthogonality error {max_error} exceeds tolerance"
        )
    diagnostics = {
        "input_n": input_n,
        "size_excluded_n": input_n - len(size),
        "model_excluded_n": input_n - len(model),
        "size_n": len(size),
        "model_n": len(model),
        "m_orthogonality_error": m_error,
        "s_orthogonality_error": s_error,
        "h_orthogonality_error": h_error,
        "max_orthogonality_error": max_error,
        **q,
    }
    return ExposureResult(size=size, model=model, q=q, diagnostics=diagnostics)
```

- [ ] **Step 4: Run the exposure tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add signals/style_basket/b3_exposures.py tests/test_b3_exposures.py
git commit -m "feat(b3): add orthogonal exposure engine"
```

#### Task 2 implementation-review corrections

This subsection supersedes any conflicting Task 2 line-level pseudocode above.

- `_industry_design(industry)` fills missing labels with `UNKNOWN`, converts labels
  to strings, and creates float dummy columns in sorted-label order. Whenever at
  least one dummy exists, it drops the sorted reference dummy. A single-industry
  universe therefore uses the intercept only. Retained columns are named
  `industry=<label>` so real industry labels cannot collide with reserved controls
  such as `intercept`, `m`, `s_perp`, or `m_perp`.

  ```python
  labels = industry.fillna("UNKNOWN").astype(str)
  dummies = pd.get_dummies(labels, dtype=float)
  dummies = dummies.reindex(sorted(dummies.columns), axis=1)
  if dummies.shape[1] >= 1:
      dummies = dummies.iloc[:, 1:]
  dummies.columns = [f"industry={label}" for label in dummies.columns]
  intercept = pd.Series(1.0, index=industry.index, name="intercept")
  return pd.concat([intercept, dummies], axis=1)
  ```

- `_residualize(y, controls, label)` concatenates `y` and the controls and rejects
  any row containing a missing input with `NumericalFailure`; accepted compute
  inputs must already satisfy the source-data contract. It likewise rejects
  nonfinite inputs with `NumericalFailure`, then performs the existing full-rank
  OLS, sample-standardization, and normalized orthogonality checks unchanged.
  Malformed source-level `style_score` values are classified as `DataBlocked`
  before this internal helper is called.

- `_capped_weights` validates that the complete exposure vector is numeric and
  finite before clipping or sign filtering. An internally generated nonfinite
  exposure raises `NumericalFailure`; the existing deterministic water-filling,
  membership, cap, and normalization rules remain unchanged.

- Presence of `size_eligible` continues to activate the explicit four-column
  eligibility contract. Explicit size/model flags must be non-null instances of
  `bool` or `np.bool_`. `DATA_*` reason codes retain first precedence. Eligible
  rows require blank reasons; ineligible rows require registered nonblank reasons;
  and the model universe must remain a subset of the size universe.

- Every model-universe `style_score` must coerce to a finite numeric value before
  winsorization. Invalid explicitly eligible values and invalid non-null legacy
  values raise `DataBlocked` with the affected ticker(s).

Regression coverage includes the single-industry case, reserved industry labels,
explicit `None`/text/positive-infinity/negative-infinity styles, legacy nonnumeric
style, residual and weight nonfinite invariants, string/null eligibility flags,
eligible rows carrying exclusion reasons, and accepted NumPy boolean flags.

These are correctness corrections found during implementation review. They affect
only malformed-input and single-industry boundary behavior; they do not change the
pre-registered economic signal, formulas, q bands, universe rules, weight names, or
exception inheritance.

## Task 3: Implement the two PIT policies and monthly style snapshots

**Files:**
- Create: `signals/style_basket/b3_build.py`
- Modify: `tests/test_b3_exposures.py`

- [ ] **Step 1: Add failing PIT-policy tests**

Append:

```python
from signals.style_basket.b3_build import apply_pit_policy


def test_csmar_pit_policies_use_legal_deadline_and_next_month_end():
    facts = pd.DataFrame({
        "ts_code": ["X", "X"],
        "end_date": pd.to_datetime(["2020-03-31", "2020-06-30"]),
        "stored_ann_date": pd.to_datetime(["2023-07-29", "2023-07-29"]),
        "statement_type": ["income", "income"],
        "data_source": ["csmar", "csmar"],
        "data": [{"revenue": 1.0}, {"revenue": 2.0}],
    })
    main = apply_pit_policy(facts, "legal_deadline")
    lag = apply_pit_policy(facts, "legal_deadline_plus_one_month_end")
    assert list(main["ann_date"]) == [
        pd.Timestamp("2020-04-30"), pd.Timestamp("2020-08-31")
    ]
    assert list(lag["ann_date"]) == [
        pd.Timestamp("2020-05-31"), pd.Timestamp("2020-09-30")
    ]
    assert not main["true_first_disclosure_verified"].any()


def test_wind_date_is_unchanged_by_both_approximate_policies():
    facts = pd.DataFrame({
        "ts_code": ["X"],
        "end_date": pd.to_datetime(["2025-06-30"]),
        "stored_ann_date": pd.to_datetime(["2025-08-20"]),
        "statement_type": ["income"],
        "data_source": ["wind"],
        "data": [{"revenue": 1.0}],
    })
    for policy in load_b3_config()["pit"]["policies"]:
        got = apply_pit_policy(facts, policy)
        assert got.loc[0, "ann_date"] == pd.Timestamp("2025-08-20")
        assert bool(got.loc[0, "true_first_disclosure_verified"])
```

- [ ] **Step 2: Run the tests and verify the function is absent**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: import fails because `signals.style_basket.b3_build` does not exist.

- [ ] **Step 3: Implement provenance-preserving policy conversion**

Start `signals/style_basket/b3_build.py` with:

```python
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from signals.common.config import load_db_config
from signals.common.financial_field_map import (
    CSMAR_END,
    legal_disclosure_deadline,
    translate_data,
)
from signals.common.financial_reader import _connect
from signals.style_basket.b3_config import config_hash, load_b3_config
from signals.style_basket.b3_exposures import (
    CoverageBlocked,
    DataBlocked,
    ExposureResult,
    compute_month_exposures,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ROOT / "output" / "style_basket" / "b3"
POLICY_MAIN = "legal_deadline"
POLICY_LAG = "legal_deadline_plus_one_month_end"


def apply_pit_policy(raw: pd.DataFrame, policy: str) -> pd.DataFrame:
    if policy not in {POLICY_MAIN, POLICY_LAG}:
        raise ValueError(f"unknown PIT policy: {policy}")
    out = raw.copy()
    out["end_date"] = pd.to_datetime(out["end_date"])
    out["stored_ann_date"] = pd.to_datetime(out["stored_ann_date"])
    known = []
    verified = []
    sources = []
    for row in out.itertuples(index=False):
        if row.data_source == "csmar":
            legal = pd.Timestamp(legal_disclosure_deadline(row.end_date.date()))
            date = (
                min(row.stored_ann_date, legal)
                if policy == POLICY_MAIN
                else legal + pd.offsets.MonthEnd(1)
            )
            known.append(date)
            verified.append(False)
            sources.append(policy)
        elif row.data_source == "wind":
            known.append(row.stored_ann_date)
            verified.append(True)
            sources.append("wind_first_disclosure")
        else:
            raise DataBlocked(f"unknown financial data_source: {row.data_source}")
    out["ann_date"] = pd.to_datetime(known)
    out["known_date_source"] = sources
    out["true_first_disclosure_verified"] = verified
    return out


def _fetch_raw_financial(
    tickers: list[str], start: str, end: str, db: dict
) -> pd.DataFrame:
    conn = _connect(db)
    try:
        frame = pd.read_sql(
            f"""SELECT ts_code, end_date, ann_date AS stored_ann_date,
                       statement_type, data, data_source
                FROM {db['schema']}.stock_financial
                WHERE ts_code = ANY(%(tickers)s)
                  AND end_date BETWEEN %(start)s AND %(end)s
                  AND ((data_source='csmar' AND end_date <= %(cutoff)s)
                    OR (data_source='wind' AND end_date > %(cutoff)s))
                ORDER BY ts_code, statement_type, end_date""",
            conn,
            params={
                "tickers": tickers,
                "start": start,
                "end": end,
                "cutoff": CSMAR_END,
            },
        )
    finally:
        conn.close()
    if frame.empty:
        raise DataBlocked("stock_financial returned no B3 facts")
    frame["data"] = [
        translate_data(data, source, statement)
        for data, source, statement in zip(
            frame["data"], frame["data_source"], frame["statement_type"]
        )
    ]
    return frame


def _industry_snapshot(pool: pd.DataFrame, formation_date: pd.Timestamp) -> pd.Series:
    pool = pool.copy()
    pool["effective_date"] = pd.to_datetime(pool["effective_date"])
    first = (
        pool.sort_values(["ticker", "effective_date"])
        .groupby("ticker", as_index=False)
        .first()
        .assign(effective_date=pd.Timestamp("1900-01-01"))
    )
    extended = pd.concat([first, pool], ignore_index=True)
    known = extended[extended["effective_date"] <= formation_date]
    latest = (
        known.sort_values(["ticker", "effective_date"])
        .groupby("ticker", as_index=False)
        .tail(1)
    )
    return latest.set_index("ticker")["industry"].fillna("UNKNOWN")
```

Use `ticker_financial_rows` from `signals.style_basket.build` and `style_scores` from `signals.style_basket.scoring` when assembling policy-specific snapshots. Do not call `fetch_financial_facts`, because it drops `data_source` and the stored announcement date. Implement:

```python
def build_policy_snapshots(
    raw_facts: pd.DataFrame,
    month_ends: list[pd.Timestamp],
    closes: pd.DataFrame,
    shares_pool: pd.DataFrame,
    industry_pool: pd.DataFrame,
    stock_meta: pd.DataFrame,
    policy: str,
) -> dict[pd.Timestamp, pd.DataFrame]:
    from signals.common.factors import asof_latest
    from signals.style_basket.build import (
        FINANCIAL_INDUSTRIES,
        MIN_LISTED_DAYS,
        ticker_financial_rows,
    )
    from signals.style_basket.scoring import style_scores

    facts = apply_pit_policy(raw_facts, policy)
    pools = {"ttm": [], "slope": [], "event": []}
    for _, ticker_facts in facts.groupby("ts_code", sort=False):
        built = ticker_financial_rows(ticker_facts)
        for key in pools:
            if not built[key].empty:
                pools[key].append(built[key])
    pooled = {
        key: pd.concat(parts, ignore_index=True)
        if parts else pd.DataFrame()
        for key, parts in pools.items()
    }

    def asof_selected(
        pool: pd.DataFrame, date: pd.Timestamp, field: str
    ) -> pd.DataFrame:
        if pool.empty:
            return pd.DataFrame()
        selected = asof_latest(pool[pool["field"] == field], date)
        return selected.set_index("ts_code")

    def asof_field(
        pool: pd.DataFrame, date: pd.Timestamp, field: str, col: str
    ) -> pd.Series:
        selected = asof_selected(pool, date, field)
        return selected[col] if col in selected else pd.Series(dtype=float)

    snapshots = {}
    meta = stock_meta.copy().set_index("ticker")
    meta["list_date"] = pd.to_datetime(meta["list_date"])
    meta["delist_date"] = pd.to_datetime(meta["delist_date"])
    for date in month_ends:
        if date not in closes.index:
            raise DataBlocked(f"missing formation close snapshot: {date.date()}")
        active = (
            (meta["list_date"].isna() | (meta["list_date"] <= date))
            & (meta["delist_date"].isna() | (meta["delist_date"] >= date))
        )
        base = meta.index[active]
        close = closes.loc[date].reindex(base)
        shares_frame = asof_latest(shares_pool, date)
        shares = pd.Series(
            shares_frame["total_shares"].to_numpy(),
            index=shares_frame["ts_code"],
        ).reindex(base)
        mv = shares * close
        size_reason = pd.Series("", index=base, dtype=object)
        missing_list = meta.loc[base, "list_date"].isna()
        size_reason.loc[missing_list] = "DATA_MISSING_LIST_DATE"
        too_new = (
            meta.loc[base, "list_date"].notna()
            & (
                meta.loc[base, "list_date"]
                + pd.Timedelta(days=MIN_LISTED_DAYS)
                > date
            )
        )
        size_reason.loc[(size_reason == "") & too_new] = "LISTED_LT_180D"
        size_reason.loc[(size_reason == "") & close.isna()] = "DATA_MISSING_CLOSE"
        size_reason.loc[(size_reason == "") & shares.isna()] = "DATA_MISSING_SHARES"
        invalid_mv = ~np.isfinite(mv) | (mv <= 0)
        size_reason.loc[
            (size_reason == "") & invalid_mv
        ] = "DATA_INVALID_MARKET_VALUE"
        size_eligible = size_reason == ""
        eligible = base[size_eligible]
        industry = _industry_snapshot(industry_pool, date).reindex(base).fillna(
            "UNKNOWN"
        )

        np_ttm = asof_field(pooled["ttm"], date, "np", "ttm")
        sal_selected = asof_selected(pooled["slope"], date, "rev")
        factors = pd.DataFrame(index=eligible)
        factors["sal_g"] = (
            sal_selected["slope"].reindex(eligible)
            if "slope" in sal_selected else np.nan
        )
        factors["pro_g"] = asof_field(pooled["slope"], date, "np", "slope")
        factors["ep"] = np_ttm.reindex(eligible) / mv.reindex(eligible)
        factors["bp"] = (
            asof_field(pooled["event"], date, "equity", "value").reindex(eligible)
            / mv.reindex(eligible)
        )
        factors["cfp"] = (
            asof_field(pooled["ttm"], date, "cfo", "ttm").reindex(eligible)
            / mv.reindex(eligible)
        )
        factors.loc[
            industry.reindex(eligible).isin(FINANCIAL_INDUSTRIES), "cfp"
        ] = np.nan
        factors["dp"] = (
            asof_field(pooled["event"], date, "dps", "value").reindex(eligible)
            * shares.reindex(eligible)
            / mv.reindex(eligible)
        )
        scored = style_scores(factors)
        style = scored["style_score"].reindex(base)
        model_eligible = size_eligible & style.notna()
        model_reason = size_reason.copy()
        model_reason.loc[
            size_eligible & ~model_eligible
        ] = "MISSING_STYLE_SCORE"
        salg_end = pd.to_datetime(
            sal_selected["end_date"] if "end_date" in sal_selected else pd.Series(dtype="datetime64[ns]")
        ).reindex(base)
        has_unverified_csmar_history = bool(
            ((facts["data_source"] == "csmar") & (facts["end_date"] <= date)).any()
        )
        snapshots[date] = pd.DataFrame({
            "ticker": base,
            "formation_date": date,
            "total_market_value": mv.reindex(base).to_numpy(),
            "industry": industry.reindex(base).to_numpy(),
            "style_score": style.to_numpy(),
            "size_eligible": size_eligible.to_numpy(),
            "model_eligible": model_eligible.to_numpy(),
            "size_exclusion_reason": size_reason.to_numpy(),
            "model_exclusion_reason": model_reason.to_numpy(),
            "salg_source_end_date": salg_end.to_numpy(),
            "true_first_disclosure_verified": (
                not has_unverified_csmar_history
            ),
        })
    return snapshots
```

- [ ] **Step 4: Run PIT tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): preserve PIT policy provenance"
```

## Task 4: Add return-blind preflight, coordinate calibration and stage manifests

**Files:**
- Modify: `signals/style_basket/b3_build.py`
- Modify: `tests/test_b3_exposures.py`

- [ ] **Step 1: Add failing blocker-classification and no-forward-read tests**

Append:

```python
from signals.style_basket.b3_build import (
    B3Sources,
    calibrate_target_coordinates,
    run_preflight,
)


def _constituents_for_snapshot(snapshot: pd.DataFrame) -> pd.DataFrame:
    ranked = snapshot.sort_values(
        ["total_market_value", "ticker"], ascending=[False, True]
    )
    rows = []
    for code, members in [
        ("000905.SH", ranked.iloc[300:800]["ticker"]),
        ("000852.SH", ranked.iloc[800:1800]["ticker"]),
    ]:
        rows.extend({
            "index_code": code,
            "ticker": ticker,
            "effective_date": pd.Timestamp("2021-01-29"),
        } for ticker in members)
    return pd.DataFrame(rows)


def test_coordinate_calibration_accepts_matching_rank_proxy():
    cfg = load_b3_config()
    snap = _synthetic_snapshot()
    exposure = compute_month_exposures(snap, cfg)
    diagnostics = calibrate_target_coordinates(
        {pd.Timestamp("2021-01-29"): exposure},
        _constituents_for_snapshot(snap),
    )
    assert diagnostics["q500_mean_abs_error"] <= 0.25
    assert diagnostics["q1000_mean_abs_error"] <= 0.25
    assert diagnostics["q_order_share"] >= 0.90


def test_missing_q1000_constituents_is_data_blocked():
    cfg = load_b3_config()
    snap = _synthetic_snapshot()
    exposure = compute_month_exposures(snap, cfg)
    only_500 = _constituents_for_snapshot(snap)
    only_500 = only_500[only_500["index_code"] == "000905.SH"]
    with pytest.raises(DataBlocked, match="000852.SH"):
        calibrate_target_coordinates(
            {pd.Timestamp("2021-01-29"): exposure}, only_500
        )


def test_preflight_does_not_call_any_forward_return_loader(tmp_path: Path):
    cfg = load_b3_config()
    snap = _synthetic_snapshot()

    def forbidden(*args):
        raise AssertionError("preflight read a forward return")

    sources = B3Sources(
        snapshots=lambda policy, data_end: {
            pd.Timestamp("2021-01-29"): snap
        },
        constituents=lambda: _constituents_for_snapshot(snap),
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )
    outcome = run_preflight(
        cfg, sources, pd.Timestamp("2023-12-31"), tmp_path
    )
    assert outcome.final_status == "OK"
    assert (tmp_path / "coverage_audit.csv").exists()
    assert (tmp_path / "manifests" / "preflight.json").exists()

    def unavailable_snapshots(policy, data_end):
        raise DataBlocked("raw snapshot source unavailable")

    blocked_dir = tmp_path / "blocked"
    blocked_sources = B3Sources(
        snapshots=unavailable_snapshots,
        constituents=lambda: _constituents_for_snapshot(snap),
        stock_returns=forbidden,
        target_returns=forbidden,
        carry=forbidden,
    )
    blocked = run_preflight(
        cfg, blocked_sources, pd.Timestamp("2023-12-31"), blocked_dir
    )
    assert blocked.final_status == "DATA_BLOCKED"
    assert (blocked_dir / "coverage_audit.csv").exists()
    assert (blocked_dir / "manifests" / "preflight.json").exists()
```

- [ ] **Step 2: Run tests and verify the new APIs fail**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: import fails for `B3Sources`, `calibrate_target_coordinates`, or `run_preflight`.

- [ ] **Step 3: Implement calibration and stage manifest primitives**

Add:

```python
@dataclass(frozen=True)
class B3Sources:
    snapshots: Callable[[str, pd.Timestamp], dict[pd.Timestamp, pd.DataFrame]]
    constituents: Callable[[], pd.DataFrame]
    stock_returns: Callable[[pd.Timestamp], object]
    target_returns: Callable[[pd.Timestamp], object]
    carry: Callable[[pd.Timestamp], object]


@dataclass(frozen=True)
class PreflightOutcome:
    final_status: str
    exposures: dict[str, dict[pd.Timestamp, ExposureResult]]
    audit: pd.DataFrame
    diagnostics: pd.DataFrame


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_stage_manifest(
    output_dir: Path,
    stage: str,
    cfg: dict,
    data_end: pd.Timestamp,
    outputs: list[Path],
    status: str,
    blockers: list[dict],
) -> Path:
    manifest_dir = output_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": stage,
        "config_hash": config_hash(cfg),
        "data_end": str(data_end.date()),
        "status": status,
        "blockers": blockers,
        "outputs": {
            str(path.relative_to(output_dir)): _sha256(path)
            for path in outputs if path.exists()
        },
    }
    path = manifest_dir / f"{stage}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path


def require_parent_manifest(
    output_dir: Path, parent: str, cfg: dict, data_end: pd.Timestamp
) -> dict:
    path = output_dir / "manifests" / f"{parent}.json"
    if not path.exists():
        raise DataBlocked(f"missing parent manifest: {parent}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["config_hash"] != config_hash(cfg):
        raise DataBlocked(f"{parent} config hash mismatch")
    if payload["data_end"] != str(data_end.date()):
        raise DataBlocked(f"{parent} data_end mismatch")
    if payload["status"] != "OK":
        raise DataBlocked(f"{parent} did not complete successfully")
    for relative, expected in payload["outputs"].items():
        if _sha256(output_dir / relative) != expected:
            raise DataBlocked(f"{parent} output hash mismatch: {relative}")
    return payload


def calibrate_target_coordinates(
    exposures: dict[pd.Timestamp, ExposureResult],
    constituents: pd.DataFrame,
) -> dict[str, float]:
    required = {"index_code", "ticker", "effective_date"}
    if required - set(constituents):
        raise DataBlocked("index_constituent schema incomplete")
    available = set(constituents["index_code"])
    for code in ["000905.SH", "000852.SH"]:
        if code not in available:
            raise DataBlocked(f"missing index_constituent history for {code}")
    table = constituents.copy()
    table["effective_date"] = pd.to_datetime(table["effective_date"])
    errors = {"q500": [], "q1000": []}
    ordering = []
    for date, result in sorted(exposures.items()):
        if date < pd.Timestamp("2021-01-01"):
            continue
        actual = {}
        for qname, code in [("q500", "000905.SH"), ("q1000", "000852.SH")]:
            history = table[
                (table["index_code"] == code)
                & (table["effective_date"] <= date)
            ]
            if history.empty:
                raise DataBlocked(
                    f"no {code} constituent snapshot on or before {date.date()}"
                )
            effective = history["effective_date"].max()
            members = history.loc[
                history["effective_date"] == effective, "ticker"
            ]
            values = result.size["m_perp"].reindex(members).dropna()
            if values.empty:
                raise DataBlocked(f"{code} constituents cannot map to size universe")
            actual[qname] = float(values.median())
            errors[qname].append(abs(result.q[qname] - actual[qname]))
        ordering.append(result.q["q1000"] > result.q["q500"])
    if not errors["q500"] or not errors["q1000"]:
        raise DataBlocked("no 2021+ coordinate calibration months")
    diagnostics = {
        "q500_mean_abs_error": float(np.mean(errors["q500"])),
        "q1000_mean_abs_error": float(np.mean(errors["q1000"])),
        "q_order_share": float(np.mean(ordering)),
    }
    if diagnostics["q500_mean_abs_error"] > 0.25:
        raise DataBlocked("q500 coordinate calibration exceeds 0.25")
    if diagnostics["q1000_mean_abs_error"] > 0.25:
        raise DataBlocked("q1000 coordinate calibration exceeds 0.25")
    if diagnostics["q_order_share"] < 0.90:
        raise DataBlocked("q1000 > q500 in fewer than 90% of months")
    return diagnostics
```

- [ ] **Step 4: Implement preflight output and blocker precedence**

Add `run_preflight`. It must catch data and coverage blockers per month, continue only long enough to write complete audit rows for all months that can be inspected without returns, and never call the three return loaders:

```python
def run_preflight(
    cfg: dict,
    sources: B3Sources,
    data_end: pd.Timestamp,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> PreflightOutcome:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    exposures: dict[str, dict[pd.Timestamp, ExposureResult]] = {}
    audit_rows: list[dict] = []
    diagnostic_rows: list[dict] = []
    blockers: list[dict] = []
    for policy in cfg["pit"]["policies"]:
        exposures[policy] = {}
        try:
            snapshots = sources.snapshots(policy, data_end)
        except (DataBlocked, CoverageBlocked) as exc:
            is_data = isinstance(exc, DataBlocked)
            row = {
                "pit_policy": policy,
                "formation_date": pd.NaT,
                "required_formation": True,
                "affects_final": True,
                "check": "snapshot_source",
                "side": "",
                "eligible_count": 0,
                "max_weight": float("nan"),
                "status": "DATA_BLOCKED" if is_data else "COVERAGE_BLOCKED",
                "reason_code": (
                    "DATA_CONTRACT" if is_data
                    else "LEGAL_CROSS_SECTION_INFEASIBLE"
                ),
                "detail": str(exc),
            }
            audit_rows.append(row)
            blockers.append(row)
            continue
        for date, snapshot in sorted(snapshots.items()):
            required = (
                pd.Timestamp(cfg["windows"]["discovery"][0])
                <= date
                <= pd.Timestamp(cfg["windows"]["confirmation"][1])
            )
            for role in ["size", "model"]:
                reason_column = f"{role}_exclusion_reason"
                if reason_column not in snapshot:
                    continue
                counts = snapshot[reason_column].fillna("").value_counts()
                for reason, count in counts.items():
                    audit_rows.append({
                        "pit_policy": policy,
                        "formation_date": date,
                        "required_formation": required,
                        "affects_final": False,
                        "check": f"{role}_exclusion",
                        "side": reason or "ELIGIBLE",
                        "eligible_count": int(count),
                        "max_weight": float("nan"),
                        "status": "REPORT_ONLY",
                        "reason_code": reason,
                        "detail": "exclusion distribution",
                    })
            try:
                result = compute_month_exposures(snapshot, cfg)
                exposures[policy][date] = result
                for axis in [
                    "style", "size", "interaction", "qblend", "q500", "q1000"
                ]:
                    for side in ["plus", "minus"]:
                        weights = result.model[f"w_{axis}_{side}"]
                        audit_rows.append({
                            "pit_policy": policy,
                            "formation_date": date,
                            "required_formation": required,
                            "affects_final": required,
                            "check": axis,
                            "side": side,
                            "eligible_count": int((weights > 0).sum()),
                            "max_weight": float(weights.max()),
                            "status": "OK",
                            "reason_code": "",
                            "detail": "",
                        })
                diagnostic_rows.append({
                    "pit_policy": policy,
                    "formation_date": date,
                    **result.diagnostics,
                })
            except DataBlocked as exc:
                row = {
                    "pit_policy": policy,
                    "formation_date": date,
                    "required_formation": required,
                    "affects_final": required,
                    "check": "snapshot",
                    "side": "",
                    "eligible_count": 0,
                    "max_weight": float("nan"),
                    "status": "DATA_BLOCKED",
                    "reason_code": "DATA_CONTRACT",
                    "detail": str(exc),
                }
                audit_rows.append(row)
                if required:
                    blockers.append(row)
            except CoverageBlocked as exc:
                row = {
                    "pit_policy": policy,
                    "formation_date": date,
                    "required_formation": required,
                    "affects_final": required,
                    "check": "snapshot",
                    "side": "",
                    "eligible_count": 0,
                    "max_weight": float("nan"),
                    "status": "COVERAGE_BLOCKED",
                    "reason_code": "LEGAL_CROSS_SECTION_INFEASIBLE",
                    "detail": str(exc),
                }
                audit_rows.append(row)
                if required:
                    blockers.append(row)

    if not blockers:
        try:
            constituent_history = sources.constituents()
            for policy in cfg["pit"]["policies"]:
                calibration = calibrate_target_coordinates(
                    exposures[policy], constituent_history
                )
                diagnostic_rows.append({
                    "pit_policy": policy,
                    "formation_date": pd.NaT,
                    **calibration,
                })
        except DataBlocked as exc:
            row = {
                "pit_policy": "all",
                "formation_date": pd.NaT,
                "required_formation": True,
                "affects_final": True,
                "check": "target_coordinate_calibration",
                "side": "",
                "eligible_count": 0,
                "max_weight": float("nan"),
                "status": "DATA_BLOCKED",
                "reason_code": "TARGET_COORDINATE_CALIBRATION",
                "detail": str(exc),
            }
            audit_rows.append(row)
            blockers.append(row)

    audit = pd.DataFrame(audit_rows)
    diagnostics = pd.DataFrame(diagnostic_rows)
    audit_path = output_dir / "coverage_audit.csv"
    diagnostics_path = output_dir / "exposure_diagnostics.csv"
    audit.to_csv(audit_path, index=False)
    diagnostics.to_csv(diagnostics_path, index=False)
    statuses = {row["status"] for row in blockers}
    final_status = (
        "DATA_BLOCKED" if "DATA_BLOCKED" in statuses
        else "COVERAGE_BLOCKED" if "COVERAGE_BLOCKED" in statuses
        else "OK"
    )
    _write_stage_manifest(
        output_dir,
        "preflight",
        cfg,
        data_end,
        [audit_path, diagnostics_path],
        final_status,
        blockers,
    )
    return PreflightOutcome(
        final_status, exposures, audit, diagnostics
    )
```

Import NumPy in `b3_build.py` because calibration uses `np.mean`.

- [ ] **Step 5: Implement exact default sources and exposure serialization**

Use one thin SQL helper and cache the formation inputs so the two PIT policies consume identical non-financial snapshots:

```python
def _read_sql(db: dict, sql: str, params: dict | None = None) -> pd.DataFrame:
    conn = _connect(db)
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()


def _formation_inputs(
    db: dict, data_end: pd.Timestamp
) -> dict[str, object]:
    calendar = _read_sql(
        db,
        f"""SELECT DISTINCT trade_date
            FROM {db['schema']}.index_daily
            WHERE index_code='000905.SH'
              AND trade_date BETWEEN '2013-05-01' AND %(end)s
            ORDER BY trade_date""",
        {"end": data_end.date()},
    )
    trading = pd.DatetimeIndex(pd.to_datetime(calendar["trade_date"]))
    month_ends = list(
        pd.Series(trading, index=trading)
        .groupby(trading.to_period("M"))
        .max()
    )
    meta = _read_sql(
        db,
        f"""SELECT ts_code AS ticker, list_date, delist_date
            FROM {db['schema']}.stock_meta
            ORDER BY ts_code""",
    )
    tickers = meta["ticker"].tolist()
    closes = _read_sql(
        db,
        f"""SELECT ts_code AS ticker, trade_date, close
            FROM {db['schema']}.stock_daily_price
            WHERE trade_date = ANY(%(dates)s)
            ORDER BY trade_date, ts_code""",
        {"dates": [date.date() for date in month_ends]},
    )
    closes["trade_date"] = pd.to_datetime(closes["trade_date"])
    close_wide = closes.pivot(
        index="trade_date", columns="ticker", values="close"
    )
    shares = _read_sql(
        db,
        f"""SELECT ts_code, effective_date AS end_date,
                   available_date AS known_date, total_shares
            FROM {db['schema']}.stock_share_capital
            WHERE total_shares IS NOT NULL AND total_shares > 0
            ORDER BY ts_code, effective_date""",
    )
    shares["end_date"] = pd.to_datetime(shares["end_date"])
    shares["known_date"] = pd.to_datetime(shares["known_date"]).fillna(
        shares["end_date"]
    )
    industry = _read_sql(
        db,
        f"""SELECT ts_code AS ticker, effective_date,
                   level_1_name AS industry
            FROM {db['schema']}.industry_classification
            WHERE classification_type='CITIC'
            ORDER BY ts_code, effective_date""",
    )
    facts = _fetch_raw_financial(
        tickers, "2003-01-01", str(data_end.date()), db
    )
    return {
        "month_ends": month_ends,
        "meta": meta,
        "closes": close_wide,
        "shares": shares,
        "industry": industry,
        "facts": facts,
    }


def default_sources(db: dict) -> B3Sources:
    cached_inputs: dict[str, dict[str, object]] = {}

    def inputs(data_end: pd.Timestamp) -> dict[str, object]:
        key = str(data_end.date())
        if key not in cached_inputs:
            cached_inputs[key] = _formation_inputs(db, data_end)
        return cached_inputs[key]

    def snapshots(
        policy: str, data_end: pd.Timestamp
    ) -> dict[pd.Timestamp, pd.DataFrame]:
        source = inputs(data_end)
        return build_policy_snapshots(
            source["facts"],
            source["month_ends"],
            source["closes"],
            source["shares"],
            source["industry"],
            source["meta"],
            policy,
        )

    def constituents() -> pd.DataFrame:
        return _read_sql(
            db,
            f"""SELECT index_code, ts_code AS ticker, effective_date
                FROM {db['schema']}.index_constituent
                WHERE effective_date >= '2021-01-01'
                  AND index_code IN ('000905.SH', '000852.SH')
                ORDER BY index_code, effective_date, ts_code""",
        )

    def targets(data_end: pd.Timestamp) -> dict[str, pd.Series]:
        from backtest.data import load_underlying_returns
        return {
            target: load_underlying_returns(
                target, start="2013-01-01", db=db
            ).loc[:data_end]
            for target in ["500", "1000", "blend"]
        }

    def carries(data_end: pd.Timestamp) -> dict[str, pd.Series]:
        from backtest.data import load_carry
        return {
            target: load_carry(
                target, start="2013-01-01", db=db
            ).loc[:data_end]
            for target in ["500", "1000"]
        }

    return B3Sources(
        snapshots=snapshots,
        constituents=constituents,
        stock_returns=lambda data_end: _fetch_stock_return_status(
            db, data_end
        ),
        target_returns=targets,
        carry=carries,
    )


def flatten_exposures(
    exposures: dict[str, dict[pd.Timestamp, ExposureResult]]
) -> pd.DataFrame:
    rows = []
    model_columns = [
        "s_perp", "h_perp", "x_qblend", "x_q500", "x_q1000",
        "w_style_plus", "w_style_minus",
        "w_interaction_plus", "w_interaction_minus",
        "w_qblend_plus", "w_qblend_minus",
        "w_q500_plus", "w_q500_minus",
        "w_q1000_plus", "w_q1000_minus",
    ]
    for policy, months in exposures.items():
        for formation_date, result in sorted(months.items()):
            frame = result.size.copy()
            frame["universe_role"] = np.where(
                frame.index.isin(result.model.index), "model", "size_only"
            )
            for column in model_columns:
                frame[column] = result.model[column].reindex(frame.index)
            frame.insert(0, "pit_policy", policy)
            frame["formation_date"] = formation_date
            rows.append(frame.reset_index(drop=True))
    return pd.concat(rows, ignore_index=True)


def run_exposures_stage(
    cfg: dict,
    data_end: pd.Timestamp,
    output_dir: Path,
    outcome: PreflightOutcome,
) -> Path:
    require_parent_manifest(output_dir, "preflight", cfg, data_end)
    frame = flatten_exposures(outcome.exposures)
    path = output_dir / "monthly_exposures.csv.gz"
    frame.to_csv(path, index=False, compression="gzip")
    _write_stage_manifest(
        output_dir, "exposures", cfg, data_end, [path], "OK", []
    )
    return path
```

`_fetch_stock_return_status` is added with its complete SQL in Task 5 before `stock_returns` becomes an accepted CLI path. The Task 4 parser accepts only preflight/exposures, so every accepted choice is executable at that commit.

- [ ] **Step 6: Add a CLI that cannot override research parameters**

Add a `main()` with only the permitted arguments. Wire `default_sources(db)` to exact read-only SQL loaders for raw financial facts, month-end unadjusted closes, share capital, CITIC industry, stock metadata and index constituents. Do not add threshold, lookback, candidate or config-path CLI flags.

```python
def main() -> int:
    parser = argparse.ArgumentParser(description="B3 staged research builder")
    parser.add_argument(
        "--stage",
        choices=["preflight", "exposures"],
        default="exposures",
    )
    parser.add_argument("--data-end", default="2026-12-31")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    cfg = load_b3_config()
    sources = default_sources(load_db_config())
    data_end = pd.Timestamp(args.data_end)
    outcome = run_preflight(cfg, sources, data_end, args.output_dir)
    if outcome.final_status == "DATA_BLOCKED":
        return 2
    if outcome.final_status == "COVERAGE_BLOCKED":
        return 3
    if args.stage == "preflight":
        return 0
    return run_post_preflight_stages(
        args.stage, cfg, sources, data_end, args.output_dir, outcome
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

At this task, `run_post_preflight_stages` has one legal branch, `exposures`, and writes `monthly_exposures.csv.gz`. Task 5 extends the parser and dispatcher with `portfolios`; Task 6 adds `states` and `all`. No accepted CLI choice may point to an unavailable branch.

- [ ] **Step 7: Run tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py -q
```

Expected: 14 passed.

- [ ] **Step 8: Exercise current real-data preflight**

Run:

```bash
python3 -m signals.style_basket.b3_build --stage preflight --data-end 2023-12-31
```

Expected on the data snapshot verified 2026-07-14: exit 2; `coverage_audit.csv` and `manifests/preflight.json` exist; the manifest blocker includes missing `000852.SH`; no axis/state/backtest output exists. If the constituent table is repaired before execution, exit 0 is the correct outcome and the manifest must show both calibration errors at or below 0.25 and q-order share at or above 0.90.

- [ ] **Step 9: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): add return-blind preflight"
```

## Task 5: Implement natural-drift portfolios and explicit missing-price handling

**Files:**
- Create: `signals/style_basket/b3_portfolios.py`
- Create: `tests/test_b3_portfolios_states.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Write failing holding-period tests**

Create `tests/test_b3_portfolios_states.py`:

```python
import numpy as np
import pandas as pd
import pytest

from signals.style_basket.b3_exposures import DataBlocked
from signals.style_basket.b3_portfolios import (
    natural_drift_leg_returns,
    scheduled_portfolio_returns,
)


def test_natural_drift_uses_formation_weights_and_does_not_re_equal_weight():
    dates = pd.bdate_range("2021-01-29", periods=3)
    weights = pd.Series({"A": 0.75, "B": 0.25})
    returns = pd.DataFrame({
        "A": [0.99, 0.10, 0.00],
        "B": [0.99, 0.00, 0.20],
    }, index=dates)
    suspended = pd.DataFrame(False, index=dates, columns=returns.columns)
    got = natural_drift_leg_returns(
        weights, returns, suspended, dates[0], dates[-1]
    )
    assert list(got.index) == list(dates[1:])
    assert got.iloc[0] == pytest.approx(0.075)
    assert got.iloc[1] == pytest.approx(1.125 / 1.075 - 1.0)


def test_exact_suspension_keeps_value_and_unexplained_gap_blocks():
    dates = pd.bdate_range("2021-01-29", periods=2)
    weights = pd.Series({"A": 0.5, "B": 0.5})
    returns = pd.DataFrame(
        {"A": [0.0, 0.02], "B": [0.0, np.nan]}, index=dates
    )
    suspended = pd.DataFrame(False, index=dates, columns=returns.columns)
    suspended.loc[dates[1], "B"] = True
    got = natural_drift_leg_returns(
        weights, returns, suspended, dates[0], dates[1]
    )
    assert got.iloc[0] == pytest.approx(0.01)
    suspended.loc[dates[1], "B"] = False
    with pytest.raises(DataBlocked, match="unexplained price gap"):
        natural_drift_leg_returns(
            weights, returns, suspended, dates[0], dates[1]
        )


def test_next_formation_day_belongs_to_old_portfolio():
    dates = pd.bdate_range("2021-01-29", periods=5)
    returns = pd.DataFrame({
        "A": [0.0, 0.01, 0.01, 0.01, 0.01],
        "B": [0.0, 0.02, 0.02, 0.02, 0.02],
    }, index=dates)
    suspended = pd.DataFrame(False, index=dates, columns=returns.columns)
    schedule = [
        (dates[0], pd.Series({"A": 1.0})),
        (dates[2], pd.Series({"B": 1.0})),
    ]
    got = scheduled_portfolio_returns(schedule, returns, suspended)
    assert got.loc[dates[2]] == pytest.approx(0.01)
    assert got.loc[dates[3]] == pytest.approx(0.02)
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_portfolios_states.py -q
```

Expected: collection fails with `ModuleNotFoundError: signals.style_basket.b3_portfolios`.

- [ ] **Step 3: Implement the natural-drift primitives**

Create `signals/style_basket/b3_portfolios.py`:

```python
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
            f"held tickers absent from return panel: {list(missing_columns[:5])}"
        )
    panel = returns.reindex(index=days, columns=members).astype(float)
    flags = suspended.reindex(
        index=days, columns=members, fill_value=False
    ).fillna(False).astype(bool)
    unexplained = panel.isna() & ~flags
    if unexplained.any().any():
        day, ticker = unexplained.stack().loc[lambda s: s].index[0]
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
    weights = initial_weights[initial_weights > 0].astype(float).sort_index()
    if not np.isclose(weights.sum(), 1.0, atol=1.0e-10):
        raise ValueError("initial leg weights must sum to one")
    days = returns.index[
        (returns.index > formation_date) & (returns.index <= end_date)
    ]
    legal = _legal_returns(weights.index, days, returns, suspended)
    values = weights.copy()
    output = {}
    for day in days:
        before = float(values.sum())
        values = values * (1.0 + legal.loc[day])
        after = float(values.sum())
        if not np.isfinite(after) or after <= 0:
            raise DataBlocked(f"non-positive leg value on {day.date()}")
        output[day] = after / before - 1.0
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
                weights, returns, suspended, formation, end
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
        (returns.index > formation_date) & (returns.index <= end_date)
    ]
    legal = _legal_returns(members, days, returns, suspended)
    if (legal <= -1.0).any().any():
        raise DataBlocked("stock return is less than or equal to -100%")
    return (1.0 + legal).prod(axis=0) - 1.0
```

- [ ] **Step 4: Add the portfolio-stage assembler**

Add a pure assembler that takes the long-form `monthly_exposures` cache and produces the public returns plus the internal stock-period cache:

```python
def build_portfolio_panels(
    exposures: pd.DataFrame,
    returns: pd.DataFrame,
    suspended: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    axis_parts = []
    leg_parts = []
    stock_parts = []
    for policy, policy_frame in exposures.groupby("pit_policy"):
        formations = sorted(pd.to_datetime(policy_frame["formation_date"].unique()))
        schedules: dict[str, list[tuple[pd.Timestamp, pd.Series]]] = {}
        for axis in [
            "style", "size", "interaction", "qblend", "q500", "q1000"
        ]:
            for side in ["plus", "minus"]:
                schedules[f"{axis}_{side}"] = []
        for number, formation in enumerate(formations):
            month = policy_frame[
                pd.to_datetime(policy_frame["formation_date"]) == formation
            ].set_index("ticker")
            for key in schedules:
                schedules[key].append((formation, month[f"w_{key}"]))
            end = (
                formations[number + 1]
                if number + 1 < len(formations)
                else returns.index.max()
            )
            model_members = month.index[
                month["universe_role"] == "model"
            ]
            period = stock_period_returns(
                model_members, returns, suspended, formation, end
            )
            stock_parts.append(pd.DataFrame({
                "pit_policy": policy,
                "formation_date": formation,
                "ticker": period.index,
                "forward_return": period.to_numpy(),
            }))
        daily = {
            key: scheduled_portfolio_returns(schedule, returns, suspended)
            for key, schedule in schedules.items()
        }
        axis = pd.DataFrame({
            "style": daily["style_plus"] - daily["style_minus"],
            "size": daily["size_plus"] - daily["size_minus"],
            "interaction": (
                daily["interaction_plus"] - daily["interaction_minus"]
            ),
        })
        axis.insert(0, "pit_policy", policy)
        axis_parts.append(axis.rename_axis("date").reset_index())
        for q in ["qblend", "q500", "q1000"]:
            legs = pd.DataFrame({
                "date": daily[f"{q}_plus"].index,
                "pit_policy": policy,
                "q": q,
                "growth_ret": daily[f"{q}_plus"].to_numpy(),
                "value_ret": daily[f"{q}_minus"].reindex(
                    daily[f"{q}_plus"].index
                ).to_numpy(),
            })
            leg_parts.append(legs)
    return (
        pd.concat(axis_parts, ignore_index=True),
        pd.concat(leg_parts, ignore_index=True),
        pd.concat(stock_parts, ignore_index=True),
    )
```

- [ ] **Step 5: Wire the database return/status loader and portfolios stage**

In `b3_build.py`, implement a loader using only these existing columns:

```sql
SELECT ts_code, trade_date, close, pre_close, volume
FROM <schema>.stock_daily_price_qfq
WHERE trade_date > %(start)s AND trade_date <= %(end)s
ORDER BY trade_date, ts_code
```

```sql
SELECT ts_code, trade_date, is_suspended
FROM <schema>.stock_status
WHERE trade_date > %(start)s AND trade_date <= %(end)s
ORDER BY trade_date, ts_code
```

Materialize `return = close / pre_close - 1`. If a qfq row has `volume=0` and either price field is missing, set that exact row to zero and mark it suspended. Merge exact-date `stock_status.is_suspended`; do not forward-fill status. Reindex both panels to the 500 cash-index trading calendar, but leave all unexplained return gaps as NaN for `_legal_returns` to reject.

Implement the loader exactly as:

```python
def _fetch_stock_return_status(
    db: dict, data_end: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = _read_sql(
        db,
        f"""SELECT ts_code AS ticker, trade_date, close, pre_close, volume
            FROM {db['schema']}.stock_daily_price_qfq
            WHERE trade_date BETWEEN '2013-05-01' AND %(end)s
            ORDER BY trade_date, ts_code""",
        {"end": data_end.date()},
    )
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    numeric = prices[["close", "pre_close"]].astype(float)
    prices["return"] = numeric["close"] / numeric["pre_close"] - 1.0
    explicit_zero = prices["volume"].fillna(-1).eq(0)
    prices.loc[
        explicit_zero & prices["return"].isna(), "return"
    ] = 0.0
    returns = prices.pivot(
        index="trade_date", columns="ticker", values="return"
    )
    suspended_from_price = prices.assign(
        is_suspended=explicit_zero
    ).pivot(
        index="trade_date", columns="ticker", values="is_suspended"
    )

    status = _read_sql(
        db,
        f"""SELECT ts_code AS ticker, trade_date, is_suspended
            FROM {db['schema']}.stock_status
            WHERE trade_date BETWEEN '2013-05-01' AND %(end)s
            ORDER BY trade_date, ts_code""",
        {"end": data_end.date()},
    )
    status["trade_date"] = pd.to_datetime(status["trade_date"])
    status_wide = status.pivot(
        index="trade_date", columns="ticker", values="is_suspended"
    )
    calendar_frame = _read_sql(
        db,
        f"""SELECT trade_date
            FROM {db['schema']}.index_daily
            WHERE index_code='000905.SH'
              AND trade_date BETWEEN '2013-05-01' AND %(end)s
            ORDER BY trade_date""",
        {"end": data_end.date()},
    )
    calendar = pd.DatetimeIndex(
        pd.to_datetime(calendar_frame["trade_date"])
    )
    columns = returns.columns.union(status_wide.columns)
    returns = returns.reindex(index=calendar, columns=columns)
    suspended = (
        suspended_from_price.reindex(index=calendar, columns=columns)
        .fillna(False)
        | status_wide.reindex(index=calendar, columns=columns).fillna(False)
    )
    return returns, suspended
```

After `require_parent_manifest(output_dir, "exposures", cfg, data_end)`, call `build_portfolio_panels` and write:

```python
axis.to_csv(output_dir / "axis_returns.csv", index=False)
legs.to_csv(output_dir / "conditional_leg_returns.csv", index=False)
periods.to_csv(
    output_dir / "stock_period_returns.csv.gz",
    index=False,
    compression="gzip",
)
```

Hash all three in `manifests/portfolios.json`. Extend the parser choices to `preflight`, `exposures`, and `portfolios`, and implement the new branch as preflight → exposures → portfolios. Do not expose `states` or `all` as accepted choices until Task 6 adds both working branches.

- [ ] **Step 6: Run portfolio tests**

Run:

```bash
python3 -m pytest tests/test_b3_portfolios_states.py -q
```

Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add signals/style_basket/b3_portfolios.py signals/style_basket/b3_build.py tests/test_b3_portfolios_states.py
git commit -m "feat(b3): add natural-drift portfolio returns"
```

## Task 6: Implement UU/DD/DIV decomposition and causal state signals

**Files:**
- Create: `signals/style_basket/b3_states.py`
- Modify: `signals/style_basket/b3_build.py`
- Modify: `tests/test_b3_portfolios_states.py`

- [ ] **Step 1: Add failing state-boundary and causality tests**

Append:

```python
from signals.style_basket.b3_states import build_state_features, decompose_states


def test_state_decomposition_covers_uu_dd_div_and_zero_boundary():
    legs = pd.DataFrame({
        "growth_ret": [0.02, -0.01, 0.02, 0.00, -0.01],
        "value_ret": [0.01, -0.03, -0.01, -0.02, 0.00],
    })
    got = decompose_states(legs, tolerance=1.0e-12)
    assert list(got["state"]) == ["UU", "DD", "DIV", "DIV", "DIV"]
    np.testing.assert_allclose(
        got["d"], got["d_UU"] + got["d_DD"] + got["d_DIV"], atol=1.0e-12
    )


def test_state_transform_uses_full_past_windows_and_never_future_data():
    idx = pd.bdate_range("2019-01-01", periods=120)
    legs = pd.DataFrame({
        "growth_ret": np.sin(np.arange(120) / 8.0) / 100.0,
        "value_ret": np.cos(np.arange(120) / 9.0) / 120.0,
    }, index=idx)
    cfg = load_b3_config()
    original = build_state_features(legs, cfg)
    mutated = legs.copy()
    mutated.loc[idx[100]:, "growth_ret"] = 0.20
    changed = build_state_features(mutated, cfg)
    pd.testing.assert_frame_equal(
        original.loc[:idx[99]], changed.loc[:idx[99]]
    )
    assert original[["F_U", "F_D", "F_X", "F_T"]].iloc[:62].isna().all().all()
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_portfolios_states.py -q
```

Expected: import fails for `signals.style_basket.b3_states`.

- [ ] **Step 3: Implement state decomposition and 20/40/5 transforms**

Create `signals/style_basket/b3_states.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd


def decompose_states(
    legs: pd.DataFrame, tolerance: float = 1.0e-12
) -> pd.DataFrame:
    required = {"growth_ret", "value_ret"}
    if required - set(legs):
        raise ValueError("legs require growth_ret and value_ret")
    if (legs[list(required)] <= -1.0).any().any():
        raise ValueError("leg return must be greater than -100%")
    out = legs.copy()
    out["g"] = np.log1p(out["growth_ret"])
    out["v"] = np.log1p(out["value_ret"])
    if not np.isfinite(out[["g", "v"]].to_numpy()).all():
        raise ValueError("leg log return is non-finite")
    out["d"] = out["g"] - out["v"]
    uu = (out["g"] >= 0.0) & (out["v"] >= 0.0)
    dd = (out["g"] < 0.0) & (out["v"] < 0.0)
    out["d_UU"] = out["d"].where(uu, 0.0)
    out["d_DD"] = out["d"].where(dd, 0.0)
    out["d_DIV"] = out["d"] - out["d_UU"] - out["d_DD"]
    out["state"] = np.select([uu, dd], ["UU", "DD"], default="DIV")
    error = (
        out["d"] - out["d_UU"] - out["d_DD"] - out["d_DIV"]
    ).abs().max()
    if error > tolerance:
        raise RuntimeError(f"state identity error {error} exceeds {tolerance}")
    return out


def _causal_transform(
    component: pd.Series,
    raw_window: int,
    z_window: int,
    tanh_scale: float,
    smoothing_window: int,
) -> tuple[pd.Series, pd.Series]:
    raw = component.rolling(raw_window, min_periods=raw_window).sum()
    mean = raw.rolling(z_window, min_periods=z_window).mean()
    std = raw.rolling(z_window, min_periods=z_window).std(ddof=1)
    z = (raw - mean) / std.where(std >= 1.0e-8)
    transformed = np.tanh(z / tanh_scale)
    smoothed = transformed.rolling(
        smoothing_window, min_periods=smoothing_window
    ).mean()
    return raw, smoothed


def build_state_features(legs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = decompose_states(
        legs, tolerance=cfg["exposure"]["identity_tolerance"]
    )
    signal_cfg = cfg["signal"]
    mapping = {
        "U": "d_UU",
        "D": "d_DD",
        "X": "d_DIV",
        "T": "d",
    }
    for suffix, source in mapping.items():
        raw, feature = _causal_transform(
            out[source],
            signal_cfg["raw_window"],
            signal_cfg["z_window"],
            signal_cfg["tanh_scale"],
            signal_cfg["smoothing_window"],
        )
        out[f"raw_{suffix}"] = raw
        out[f"F_{suffix}"] = feature
    return out
```

- [ ] **Step 4: Wire the states stage**

In `b3_build.py`, require `manifests/portfolios.json`, group `conditional_leg_returns.csv` by `pit_policy,q`, call `build_state_features`, and add:

```python
target = {
    "qblend": target_returns["blend"],
    "q500": target_returns["500"],
    "q1000": target_returns["1000"],
}[q]
features["external_market_direction"] = np.where(
    target.reindex(features.index) > 0.0, "up", "non_positive"
)
```

Write one long `state_components.csv` with `date,pit_policy,q` plus every state column. Hash it in `manifests/states.json`. Finish the dispatcher:

- `preflight`: preflight only.
- `exposures`: preflight and exposures.
- `portfolios`: preflight, exposures and portfolios.
- `states` and `all`: preflight, exposures, portfolios and states.

Each stage must load and verify its predecessor manifest rather than trusting in-memory results when invoked directly.

- [ ] **Step 5: Run portfolio/state tests**

Run:

```bash
python3 -m pytest tests/test_b3_portfolios_states.py -q
```

Expected: 5 passed.

- [ ] **Step 6: Run all B3 signal-layer tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py tests/test_b3_portfolios_states.py -q
```

Expected: 19 passed.

- [ ] **Step 7: Commit**

```bash
git add signals/style_basket/b3_states.py signals/style_basket/b3_build.py tests/test_b3_portfolios_states.py
git commit -m "feat(b3): add market-state decomposition"
```

## Task 7: Add hard-sort audit and Fama–MacBeth-style structure evidence

**Files:**
- Create: `backtest/b3_structure.py`
- Create: `tests/test_b3_structure.py`

- [ ] **Step 1: Write failing hard-sort and cross-sectional-regression tests**

Create `tests/test_b3_structure.py`:

```python
import numpy as np
import pandas as pd
import pytest

from backtest.b3_structure import (
    assign_hard_sort_cells,
    fama_macbeth_coefficients,
    newey_west_mean_t,
    ordinary_mean_t,
)


def _structure_panel(with_interaction: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(19)
    rows = []
    returns = []
    for date in pd.date_range("2014-01-31", periods=36, freq="ME"):
        for number in range(240):
            ticker = f"S{number:03d}"
            s = rng.normal()
            m = rng.normal()
            h = s * m + rng.normal(scale=0.1)
            value = 0.02 * s + 0.01 * m + rng.normal(scale=0.03)
            if with_interaction:
                value += 0.04 * h
            rows.append({
                "formation_date": date,
                "ticker": ticker,
                "s_perp": s,
                "m_perp": m,
                "h_perp": h,
                "industry": "A" if number % 2 else "B",
            })
            returns.append({
                "formation_date": date,
                "ticker": ticker,
                "forward_return": value,
            })
    return pd.DataFrame(rows), pd.DataFrame(returns)


def test_hard_sort_assigns_every_name_to_2x3_and_5x5_cells():
    exposures, _ = _structure_panel(True)
    month = exposures[exposures["formation_date"] == exposures["formation_date"].min()]
    cells = assign_hard_sort_cells(month)
    assert cells["cell_2x3"].notna().all()
    assert cells["cell_5x5"].notna().all()
    assert cells["cell_2x3"].nunique() == 6
    assert cells["cell_5x5"].nunique() == 25


def test_fama_macbeth_recovers_positive_interaction_and_zero_control():
    positive_x, positive_r = _structure_panel(True)
    null_x, null_r = _structure_panel(False)
    positive = fama_macbeth_coefficients(positive_x, positive_r)
    null = fama_macbeth_coefficients(null_x, null_r)
    assert positive["beta_h"].mean() > 0.03
    assert abs(null["beta_h"].mean()) < 0.01
    assert ordinary_mean_t(positive["beta_h"]) > 2.0
    assert newey_west_mean_t(positive["beta_h"], lag=3) > 2.0
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: collection fails with `ModuleNotFoundError: backtest.b3_structure`.

- [ ] **Step 3: Implement deterministic hard-sort membership**

Create `backtest/b3_structure.py` with:

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _percentile_position(
    values: pd.Series, tickers: pd.Series
) -> pd.Series:
    order = pd.DataFrame({
        "value": values.to_numpy(),
        "ticker": tickers.astype(str).to_numpy(),
    }, index=values.index).sort_values(
        ["value", "ticker"], ascending=[True, True]
    )
    percentile = pd.Series(
        (np.arange(len(order)) + 0.5) / len(order), index=order.index
    )
    return percentile.reindex(values.index)


def assign_hard_sort_cells(month: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "m_perp", "s_perp"}
    if required - set(month):
        raise ValueError("hard sort input schema incomplete")
    out = month.copy()
    p_size = _percentile_position(out["m_perp"], out["ticker"])
    p_style = _percentile_position(out["s_perp"], out["ticker"])
    size_2 = np.where(p_size < 0.5, "big", "small")
    style_3 = np.select(
        [p_style < 0.3, p_style >= 0.7],
        ["value", "growth"],
        default="middle",
    )
    out["cell_2x3"] = pd.Series(size_2, index=out.index) + "_" + pd.Series(
        style_3, index=out.index
    )
    size_5 = np.minimum((p_size * 5).astype(int) + 1, 5)
    style_5 = np.minimum((p_style * 5).astype(int) + 1, 5)
    out["cell_5x5"] = (
        "S" + size_5.astype(str) + "_V" + style_5.astype(str)
    )
    return out
```

- [ ] **Step 4: Implement monthly cross-sectional OLS and Newey–West lag 3**

Add:

```python
def fama_macbeth_coefficients(
    exposures: pd.DataFrame, forward_returns: pd.DataFrame
) -> pd.DataFrame:
    joint = exposures.merge(
        forward_returns,
        on=["formation_date", "ticker"],
        how="inner",
        validate="one_to_one",
    )
    rows = []
    for date, month in joint.groupby("formation_date"):
        clean = month[
            ["forward_return", "s_perp", "m_perp", "h_perp"]
        ].dropna()
        x = np.column_stack([
            np.ones(len(clean)),
            clean[["s_perp", "m_perp", "h_perp"]].to_numpy(dtype=float),
        ])
        if np.linalg.matrix_rank(x) != 4:
            raise RuntimeError(f"cross-sectional OLS rank failure: {date}")
        beta, _, _, _ = np.linalg.lstsq(
            x, clean["forward_return"].to_numpy(dtype=float), rcond=None
        )
        rows.append({
            "formation_date": pd.Timestamp(date),
            "alpha": beta[0],
            "beta_s": beta[1],
            "beta_m": beta[2],
            "beta_h": beta[3],
            "n": len(clean),
        })
    return pd.DataFrame(rows).sort_values("formation_date")


def ordinary_mean_t(values: pd.Series) -> float:
    clean = values.dropna().to_numpy(dtype=float)
    if len(clean) < 2:
        return float("nan")
    standard_error = clean.std(ddof=1) / np.sqrt(len(clean))
    if standard_error <= 0.0:
        return float("nan")
    return float(clean.mean() / standard_error)


def newey_west_mean_t(values: pd.Series, lag: int = 3) -> float:
    clean = values.dropna().to_numpy(dtype=float)
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
```

- [ ] **Step 5: Build the complete hard-sort surface**

Implement `build_hard_sort_surface(exposures, stock_period_returns)` by merging on `pit_policy,formation_date,ticker`, applying `assign_hard_sort_cells`, and taking the equal-weight mean of member `forward_return` for every 2×3 and 5×5 cell. This is exactly the end-of-period return of a natural-drift portfolio initialized at equal weights. Emit all 31 cell rows per valid month even when a cell is empty; empty cells carry `member_count=0,status=COVERAGE_BLOCKED,holding_return=NaN`, because a complete input contract with an empty legal cell is a design-coverage failure rather than missing source data.

Add derived rows with exact `diagnostic` values:

```python
corner = (
    cell_return["small_growth"] - cell_return["small_value"]
    - cell_return["big_growth"] + cell_return["big_value"]
)
```

For 5×5, output each size row's `growth_minus_value = V5 - V1`, adjacent row differences, and the residual from the linear prediction based on the month's continuous `beta_h`. Never expose a “best cell” return or selector.

- [ ] **Step 6: Add the structure CLI output**

`python3 -m backtest.b3_structure` must:

1. Load and hash-check `manifests/states.json`.
2. Read `monthly_exposures.csv.gz`, `stock_period_returns.csv.gz`, axis/state caches.
3. Write `hard_sort_surface.csv` under the research output directory.
4. Write `backtest/output/b3/structure_coefficients.csv` with columns:

```text
pit_policy,row_type,formation_date,window,alpha,beta_s,beta_m,beta_h,n,
ordinary_t_beta_h,nw_lag3_t_beta_h,affects_verdict
```

Monthly rows use `row_type=monthly`; summary rows use `row_type=summary` and windows `2014-2017`, `2018-2020`, `2021-2023`, `2024-2026-report-only`. Only the first three windows affect verdict.

- [ ] **Step 7: Run structure tests**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add backtest/b3_structure.py tests/test_b3_structure.py
git commit -m "feat(b3): add hard-sort and cross-sectional structure audit"
```

## Task 8: Fit frozen M0/M1 models and enforce structure gates

**Files:**
- Modify: `backtest/b3_structure.py`
- Modify: `tests/test_b3_structure.py`

- [ ] **Step 1: Add failing frozen-model, stability and coverage tests**

Append:

```python
from backtest.b3_structure import (
    ModelFit,
    apply_model,
    fit_model,
    oos_r_squared,
    stability_gate,
    state_coverage_gate,
)


def _monthly_model_frame() -> pd.DataFrame:
    dates = pd.date_range("2014-01-31", "2026-12-31", freq="ME")
    rng = np.random.default_rng(23)
    frame = pd.DataFrame(index=dates)
    frame["F_U"] = rng.normal(size=len(frame))
    frame["F_D"] = rng.normal(size=len(frame))
    frame["F_X"] = rng.normal(size=len(frame))
    frame["F_T"] = rng.normal(size=len(frame))
    frame["target"] = (
        0.35 * frame["F_U"]
        - 0.20 * frame["F_D"]
        + 0.15 * frame["F_X"]
        + rng.normal(scale=0.05, size=len(frame))
    )
    return frame


def test_discovery_coefficients_are_frozen_and_m1_beats_m0_oos():
    frame = _monthly_model_frame()
    train = frame.loc[:"2020-12-31"]
    confirm = frame.loc["2021-01-01":"2023-12-31"]
    m0 = fit_model(train, ["F_T"], "target")
    m1 = fit_model(train, ["F_U", "F_D", "F_X"], "target")
    pred0 = apply_model(confirm, m0, include_intercept=False)
    pred1 = apply_model(confirm, m1, include_intercept=False)
    assert oos_r_squared(
        confirm["target"], pred1, m1.train_target_mean
    ) > oos_r_squared(confirm["target"], pred0, m0.train_target_mean)
    changed = confirm.copy()
    changed["target"] *= -100.0
    assert fit_model(train, ["F_U", "F_D", "F_X"], "target") == m1


def test_stability_and_three_window_state_coverage_are_hard_gates():
    dates = pd.bdate_range("2021-01-01", periods=300)
    features = pd.DataFrame({
        "F_U": np.linspace(-1.0, 1.0, 300),
        "F_D": np.sin(np.arange(300) / 20.0),
        "F_X": np.cos(np.arange(300) / 25.0),
    }, index=dates)
    assert stability_gate(
        np.array([1.0, 0.5, -0.2]),
        np.array([0.9, 0.4, -0.1]),
        features,
        min_score_spearman=0.50,
    )["pass"]
    assert not stability_gate(
        np.array([1.0, 0.5, -0.2]),
        np.array([-1.0, -0.5, 0.2]),
        features,
        min_score_spearman=0.50,
    )["pass"]
    coverage = pd.DataFrame({
        "date": pd.to_datetime([
            "2014-01-02", "2014-01-03", "2018-01-02",
            "2018-01-03", "2021-01-04", "2021-01-05",
        ]),
        "state": ["UU", "DD", "UU", "DIV", "DD", "DIV"],
    }).set_index("date")
    assert not state_coverage_gate(coverage["state"], 0.10)["pass"]
```

- [ ] **Step 2: Run tests and verify the APIs are absent**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: import fails for at least one new model API.

- [ ] **Step 3: Implement model fitting, score application and OOS R-squared**

Add:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFit:
    features: tuple[str, ...]
    intercept: float
    slopes: tuple[float, ...]
    train_target_mean: float
    n: int


def fit_model(
    frame: pd.DataFrame, feature_columns: list[str], target_column: str
) -> ModelFit:
    clean = frame[[target_column, *feature_columns]].dropna()
    x = np.column_stack([
        np.ones(len(clean)),
        clean[feature_columns].to_numpy(dtype=float),
    ])
    if len(clean) <= len(feature_columns) + 1:
        raise RuntimeError("model has insufficient observations")
    if np.linalg.matrix_rank(x) != x.shape[1]:
        raise RuntimeError("model design is rank deficient")
    beta, _, _, _ = np.linalg.lstsq(
        x, clean[target_column].to_numpy(dtype=float), rcond=None
    )
    return ModelFit(
        features=tuple(feature_columns),
        intercept=float(beta[0]),
        slopes=tuple(float(value) for value in beta[1:]),
        train_target_mean=float(clean[target_column].mean()),
        n=len(clean),
    )


def apply_model(
    frame: pd.DataFrame, model: ModelFit, include_intercept: bool = False
) -> pd.Series:
    values = frame[list(model.features)].to_numpy(dtype=float) @ np.asarray(
        model.slopes
    )
    if include_intercept:
        values = values + model.intercept
    return pd.Series(values, index=frame.index, name="score")


def oos_r_squared(
    target: pd.Series, prediction: pd.Series, train_target_mean: float
) -> float:
    joint = pd.concat(
        [target.rename("target"), prediction.rename("prediction")], axis=1
    ).dropna()
    sse = float(((joint["target"] - joint["prediction"]) ** 2).sum())
    denominator = float(
        ((joint["target"] - train_target_mean) ** 2).sum()
    )
    return float("nan") if denominator == 0.0 else 1.0 - sse / denominator
```

The intercept is written to diagnostics but every production and stability score calls `include_intercept=False`.

- [ ] **Step 4: Implement fixed stability and coverage gates**

Add:

```python
def stability_gate(
    early_slopes: np.ndarray,
    late_slopes: np.ndarray,
    confirmation_daily_features: pd.DataFrame,
    min_score_spearman: float,
) -> dict[str, float | bool]:
    denominator = float(
        np.linalg.norm(early_slopes) * np.linalg.norm(late_slopes)
    )
    cosine = (
        float(early_slopes @ late_slopes / denominator)
        if denominator > 0.0 else float("nan")
    )
    early_score = confirmation_daily_features.to_numpy() @ early_slopes
    late_score = confirmation_daily_features.to_numpy() @ late_slopes
    score_spearman = pd.Series(early_score).corr(
        pd.Series(late_score), method="spearman"
    )
    passed = bool(
        np.isfinite(cosine)
        and cosine > 0.0
        and np.isfinite(score_spearman)
        and score_spearman >= min_score_spearman
    )
    return {
        "cosine": cosine,
        "score_spearman": float(score_spearman),
        "pass": passed,
    }


def state_coverage_gate(
    state: pd.Series, minimum: float
) -> dict[str, float | bool]:
    windows = {
        "2014-2017": ("2014-01-01", "2017-12-31"),
        "2018-2020": ("2018-01-01", "2020-12-31"),
        "2021-2023": ("2021-01-01", "2023-12-31"),
    }
    result: dict[str, float | bool] = {}
    passed = True
    for window, (start, end) in windows.items():
        sample = state.loc[start:end].dropna()
        for label in ["UU", "DD", "DIV"]:
            share = float((sample == label).mean()) if len(sample) else 0.0
            result[f"{window}_{label}"] = share
            passed &= share >= minimum
    result["pass"] = bool(passed)
    return result
```

- [ ] **Step 5: Assemble monthly targets and the frozen model comparison**

Implement:

```python
def next_formation_targets(
    daily_return: pd.Series, formations: pd.DatetimeIndex
) -> pd.Series:
    output = {}
    ordered = pd.DatetimeIndex(sorted(formations))
    for number in range(len(ordered) - 1):
        start, end = ordered[number], ordered[number + 1]
        sample = daily_return[(daily_return.index > start) & (daily_return.index <= end)]
        if sample.empty or sample.isna().any():
            raise RuntimeError(f"invalid target holding period: {start.date()}")
        output[start] = float((1.0 + sample).prod() - 1.0)
    return pd.Series(output, name="target")
```

For each `pit_policy,q,target`:

1. Take formation-date `F_U/F_D/F_X/F_T`.
2. Fit full discovery M0/M1, plus discovery subwindow M1 models.
3. Freeze full-discovery coefficients and score 2021–2023 and 2024–2026.
4. Compute M0/M1 OOS R-squared and monthly Spearman IC.
5. Compute `partial_rank_ic(score_M1, target, equal_weight_month_end)` for discovery (marked `is_in_sample=true,affects_verdict=false`), confirmation combined, and each confirmation year.
6. Compute stability and state-coverage dictionaries.

Write `model_comparison.csv` with this exact wide schema:

```text
pit_policy,candidate,q,target,window,model,n,oos_r2,spearman_ic,partial_ic,
cosine_early_late,confirmation_score_spearman,state_UU_share,state_DD_share,
state_DIV_share,gate_name,gate_pass,is_in_sample,affects_verdict
```

Map q to candidates exactly: qblend→B3_unified; q500 and q1000 are two mandatory legs of B3_dual_target. M0 remains an ablation row and never becomes a candidate.

- [ ] **Step 6: Implement the public and candidate structure gates**

The returned `structure_gate_rows` must contain:

- Public `beta_h_same_sign`: beta_h mean is non-zero and the same sign in 2014–2017, 2018–2020 and 2021–2023.
- Public `interaction_axis_corr`: absolute 2021–2023 monthly correlation with style axis is below 0.80.
- Public `hard_sort_complete`: all required 2×3 and 5×5 month/cell rows exist.
- Candidate `m1_increment`: confirmation M1 OOS R-squared exceeds M0 and M1 Spearman IC is at least M0.
- Candidate `partial_ic`: combined confirmation partial IC is positive and at least two calendar years are positive.
- Candidate `stability` and `state_coverage`.

B3_dual_target passes only when both q500 and q1000 pass every candidate gate. A report-only row may never change `gate_pass`.

Compare the two approximate PIT policies after both are built. Any sign flip in beta_h, M1 increment sign, or candidate pass direction adds a run-level `DATA_BLOCKED` row with `reason_code=PIT_POLICY_FLIP`.

- [ ] **Step 7: Add a report-only firewall regression test**

Append:

```python
def test_report_only_mutation_cannot_change_frozen_model():
    frame = _monthly_model_frame()
    train = frame.loc["2014-01-01":"2020-12-31"]
    baseline = fit_model(train, ["F_U", "F_D", "F_X"], "target")
    mutated = frame.copy()
    report_dates = mutated.index >= pd.Timestamp("2024-01-01")
    mutated.loc[report_dates, ["F_U", "F_D", "F_X", "F_T", "target"]] = 1.0e9
    changed = fit_model(
        mutated.loc["2014-01-01":"2020-12-31"],
        ["F_U", "F_D", "F_X"],
        "target",
    )
    assert baseline == changed
```

The CLI must use the same explicit window slices as this test; report-only rows are appended only after every `affects_verdict=true` field has been finalized.

- [ ] **Step 8: Run structure tests**

Run:

```bash
python3 -m pytest tests/test_b3_structure.py -q
```

Expected: 5 passed.

- [ ] **Step 9: Commit**

```bash
git add backtest/b3_structure.py tests/test_b3_structure.py
git commit -m "feat(b3): add frozen state model gates"
```

## Task 9: Add same-scale candidate returns and paired bootstrap evidence

**Files:**
- Create: `backtest/b3_eval.py`
- Create: `tests/test_b3_eval.py`

- [ ] **Step 1: Write failing execution, bootstrap and Holm-style tests**

Create `tests/test_b3_eval.py`:

```python
import numpy as np
import pandas as pd
import pytest

from backtest.b3_eval import (
    holm_style_adjust,
    materialize_carry,
    paired_moving_block_tail,
    passes_tail_gate,
    two_leg_candidate_returns,
)
from signals.style_basket.b3_exposures import DataBlocked


def _market_inputs():
    index = pd.bdate_range("2021-01-01", periods=80)
    r500 = pd.Series(np.linspace(-0.01, 0.012, 80), index=index)
    r1000 = pd.Series(np.linspace(0.009, -0.008, 80), index=index)
    carry500 = pd.Series(0.10, index=index)
    carry1000 = pd.Series(0.05, index=index)
    return index, r500, r1000, carry500, carry1000


def test_unified_and_dual_use_two_half_weight_legs_with_same_engine():
    index, r500, r1000, c500, c1000 = _market_inputs()
    unified = pd.Series((np.arange(80) % 7) < 4, index=index).astype(int)
    p500 = pd.Series((np.arange(80) % 5) < 3, index=index).astype(int)
    p1000 = pd.Series((np.arange(80) % 9) < 2, index=index).astype(int)
    unified_ret = two_leg_candidate_returns(
        unified, unified, r500, r1000, c500, c1000, 3.0
    )
    dual_ret = two_leg_candidate_returns(
        p500, p1000, r500, r1000, c500, c1000, 3.0
    )
    assert unified_ret.index.equals(index)
    assert dual_ret.index.equals(index)
    assert not unified_ret.equals(dual_ret)


def test_paired_moving_block_tail_is_reproducible_and_detects_dominance():
    index = pd.bdate_range("2021-01-01", periods=240)
    baseline = pd.Series(
        np.tile([-0.001, 0.001], 120), index=index
    )
    candidate = baseline + 0.0005
    first = paired_moving_block_tail(
        candidate, baseline, block_days=20, draws=499, seed=20260713
    )
    second = paired_moving_block_tail(
        candidate, baseline, block_days=20, draws=499, seed=20260713
    )
    assert first == second
    assert first["tail_prob"] <= 0.01
    assert first["ci05"] > 0.0


def test_holm_style_uses_two_fixed_candidates_and_strict_boundary():
    adjusted = holm_style_adjust({
        "B3_unified": 0.04,
        "B3_dual_target": 0.20,
    })
    assert adjusted == {
        "B3_unified": pytest.approx(0.08),
        "B3_dual_target": pytest.approx(0.20),
    }
    failed_structure = holm_style_adjust({
        "B3_unified": 0.02,
        "B3_dual_target": 1.0,
    })
    assert failed_structure["B3_unified"] == pytest.approx(0.04)
    assert failed_structure["B3_dual_target"] == pytest.approx(1.0)
    assert passes_tail_gate(0.0999, 0.10)
    assert not passes_tail_gate(0.10, 0.10)


def test_carry_crops_stale_tail_but_rejects_internal_post_launch_gap():
    calendar = pd.bdate_range("2022-07-20", periods=8)
    raw = pd.Series(0.05, index=calendar[:6])
    got = materialize_carry(raw, calendar, pd.Timestamp("2022-07-22"))
    assert got.index.equals(calendar[:6])
    broken = raw.drop(index=calendar[4])
    with pytest.raises(DataBlocked, match="internal post-launch carry gap"):
        materialize_carry(broken, calendar, pd.Timestamp("2022-07-22"))
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -q
```

Expected: collection fails with `ModuleNotFoundError: backtest.b3_eval`.

- [ ] **Step 3: Implement common-calendar two-leg net returns**

Create `backtest/b3_eval.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine import run_strategy
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover
from backtest.positions import production_position
from signals.style_basket.b3_exposures import DataBlocked

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "backtest" / "output" / "b3"


def _common_index(series: list[pd.Series]) -> pd.DatetimeIndex:
    index = series[0].index
    for value in series[1:]:
        index = index.intersection(value.index)
    return pd.DatetimeIndex(index).sort_values()


def two_leg_candidate_returns(
    position_500: pd.Series,
    position_1000: pd.Series,
    return_500: pd.Series,
    return_1000: pd.Series,
    carry_500: pd.Series,
    carry_1000: pd.Series,
    cost_bps: float,
) -> pd.Series:
    index = _common_index([
        position_500, position_1000, return_500, return_1000,
        carry_500, carry_1000,
    ])
    leg500 = run_strategy(
        position_500.reindex(index),
        return_500.reindex(index),
        cost_bps,
        carry_500.reindex(index),
    )["ret"]
    leg1000 = run_strategy(
        position_1000.reindex(index),
        return_1000.reindex(index),
        cost_bps,
        carry_1000.reindex(index),
    )["ret"]
    return (0.5 * leg500 + 0.5 * leg1000).rename("ret")


def materialize_carry(
    raw: pd.Series,
    calendar: pd.DatetimeIndex,
    launch_date: pd.Timestamp,
) -> pd.Series:
    observed = raw.dropna()
    if observed.empty:
        raise DataBlocked("carry series has no observations")
    usable_calendar = calendar[calendar <= observed.index.max()]
    aligned = raw.reindex(usable_calendar)
    aligned.loc[usable_calendar < launch_date] = 0.0
    missing = aligned.index[
        (usable_calendar >= launch_date) & aligned.isna()
    ]
    if len(missing):
        raise DataBlocked(
            f"internal post-launch carry gap starts {missing[0].date()}"
        )
    return aligned
```

Use individual 500/1000 legs for equal_weight, unified and dual-target. This makes costs and carry mechanically identical when the two scores are identical. Build positions with `production_position(score)`; never pass M0 as a candidate.

- [ ] **Step 4: Implement paired moving-block tail probability and adjustment**

Add:

```python
def paired_moving_block_tail(
    candidate: pd.Series,
    baseline: pd.Series,
    block_days: int,
    draws: int,
    seed: int,
) -> dict[str, float]:
    joint = pd.concat(
        [candidate.rename("candidate"), baseline.rename("baseline")], axis=1
    ).dropna()
    values = joint.to_numpy(dtype=float)
    n = len(values)
    if n < block_days:
        raise ValueError("paired bootstrap sample shorter than block")
    starts = np.arange(n - block_days + 1)
    blocks_per_draw = int(np.ceil(n / block_days))
    rng = np.random.default_rng(seed)
    differences = np.empty(draws, dtype=float)
    for draw in range(draws):
        selected = rng.choice(starts, size=blocks_per_draw, replace=True)
        sample = np.concatenate([
            values[start:start + block_days] for start in selected
        ], axis=0)[:n]
        differences[draw] = (
            sharpe(pd.Series(sample[:, 0]))
            - sharpe(pd.Series(sample[:, 1]))
        )
    return {
        "tail_prob": float(
            (1 + np.count_nonzero(differences <= 0.0)) / (draws + 1)
        ),
        "ci05": float(np.quantile(differences, 0.05)),
        "ci50": float(np.quantile(differences, 0.50)),
        "ci95": float(np.quantile(differences, 0.95)),
    }


def holm_style_adjust(raw: dict[str, float]) -> dict[str, float]:
    if set(raw) != {"B3_unified", "B3_dual_target"}:
        raise ValueError("Holm-style family must contain exactly two candidates")
    ordered = sorted(raw, key=raw.get)
    first, second = ordered
    first_adjusted = min(1.0, 2.0 * raw[first])
    second_adjusted = min(1.0, max(first_adjusted, raw[second]))
    return {first: first_adjusted, second: second_adjusted}


def passes_tail_gate(adjusted_tail: float, threshold: float) -> bool:
    return bool(np.isfinite(adjusted_tail) and adjusted_tail < threshold)
```

Run the bootstrap only after building both candidate structure outcomes. Set a structure-failed candidate's raw `tail_prob` to 1.0, call `holm_style_adjust` once on the fixed pair, and write `bootstrap.csv` with:

```text
pit_policy,candidate,draws,block_days,seed,tail_prob,holm_adjusted_tail,
ci05,ci50,ci95,structure_pass,gate_pass
```

`gate_pass` is `passes_tail_gate(holm_adjusted_tail, 0.10)`. Do not name either probability a p-value in code, columns or console output.

- [ ] **Step 5: Implement same-scale metrics and IM-boundary evidence**

For each policy and the main-policy headline:

1. Restrict to the common 2021–2023 calendar.
2. Recompute equal_weight long-flat from committed `factor_value`; do not read its committed metric row as a gate input.
3. Evaluate B3_unified with qblend on both legs.
4. Evaluate B3_dual_target with q500 on IC and q1000 on IM.
5. Report B3_500/B3_1000 components as `is_candidate=false`.
6. Calculate `ann_return,sharpe,maxdd,turnover,n_obs`; dual turnover is `0.5*turnover(pos500)+0.5*turnover(pos1000)`.
7. Repeat dual-target diagnostics for pre-IM and 2022-07-22 through 2023-12-31. Post-IM requires at least 252 common dates, positive Sharpe difference, no more than 0.02 MaxDD worsening, and non-negative q500/q1000 monthly partial IC.

Write `production_metrics.csv` with:

```text
pit_policy,candidate,component,is_candidate,window,executable,n_obs,ann_return,
sharpe,maxdd,turnover,baseline_sharpe,sharpe_difference,
baseline_maxdd,maxdd_difference,baseline_turnover,turnover_ratio,
partial_ic,gate_name,gate_pass,affects_verdict
```

The full-confirmation gates are strict as specified: Sharpe difference `>=0.10`, MaxDD difference `>=-0.02`, turnover ratio `<=1.50`, partial IC positive in the combined sample and at least two years, adjusted tail probability `<0.10`.

- [ ] **Step 6: Add yearly report-only contribution output**

Implement additive log-return contribution:

```python
def yearly_contributions(
    candidate: str,
    returns: pd.Series,
    window: str,
    is_in_sample: bool,
) -> pd.DataFrame:
    log_pnl = np.log1p(returns).groupby(returns.index.year).sum()
    denominator = float(log_pnl.abs().sum())
    strongest = int(log_pnl.abs().idxmax())
    rows = []
    for year, value in log_pnl.items():
        rows.append({
            "candidate": candidate,
            "window": window,
            "year": int(year),
            "signed_log_pnl": float(value),
            "absolute_pnl_share": (
                float(abs(value) / denominator) if denominator > 0 else float("nan")
            ),
            "strongest_year": strongest,
            "is_in_sample": is_in_sample,
            "affects_verdict": False,
        })
    return pd.DataFrame(rows)
```

Write 2021–2023 and 2014–2023 rows to `yearly_contribution.csv`. For each candidate/window also report metrics after removing the strongest absolute-log-P&L year. No concentration value enters `gate_pass`.

- [ ] **Step 7: Run eval tests**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -q
```

Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add backtest/b3_eval.py tests/test_b3_eval.py
git commit -m "feat(b3): add same-scale candidate evaluation"
```

## Task 10: Add three-layer verdicts, manifests and end-to-end regression guards

**Files:**
- Modify: `backtest/b3_eval.py`
- Modify: `tests/test_b3_eval.py`

- [ ] **Step 1: Add failing verdict-precedence tests**

Append:

```python
from backtest.b3_eval import (
    candidate_statistical_label,
    family_best_wins,
    final_verdict,
)


def test_candidate_labels_are_mutually_exclusive():
    assert candidate_statistical_label(False, False, False) == "STOP"
    assert candidate_statistical_label(True, False, False) == "MEASURE_ONLY"
    assert candidate_statistical_label(True, True, True) == "PASS_SHADOW"
    assert candidate_statistical_label(True, True, False) == "MEASURE_ONLY"


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["PASS_SHADOW", "STOP"], "PASS_SHADOW"),
        (["MEASURE_ONLY", "STOP"], "MEASURE_ONLY"),
        (["STOP", "STOP"], "STOP"),
    ],
)
def test_family_aggregation_is_best_wins(labels, expected):
    assert family_best_wins(labels) == expected


def test_run_blockers_override_only_final_not_statistical():
    assert final_verdict("PASS_SHADOW", data_blocked=True, coverage_blocked=True) == (
        "DATA_BLOCKED"
    )
    assert final_verdict("PASS_SHADOW", data_blocked=False, coverage_blocked=True) == (
        "COVERAGE_BLOCKED"
    )
    assert final_verdict("PASS_SHADOW", data_blocked=False, coverage_blocked=False) == (
        "PASS_SHADOW"
    )
```

- [ ] **Step 2: Run tests and verify verdict functions are absent**

Run:

```bash
python3 -m pytest tests/test_b3_eval.py -q
```

Expected: import fails for the verdict functions.

- [ ] **Step 3: Implement the frozen three-layer semantics**

Add:

```python
_FAMILY_RANK = {"STOP": 0, "MEASURE_ONLY": 1, "PASS_SHADOW": 2}


def candidate_statistical_label(
    structure_pass: bool,
    production_pass: bool,
    executable_boundary_pass: bool,
) -> str:
    if not structure_pass:
        return "STOP"
    if production_pass and executable_boundary_pass:
        return "PASS_SHADOW"
    return "MEASURE_ONLY"


def family_best_wins(labels: list[str]) -> str:
    if not labels or any(label not in _FAMILY_RANK for label in labels):
        raise ValueError("family requires evaluated candidate labels")
    return max(labels, key=_FAMILY_RANK.__getitem__)


def final_verdict(
    statistical_verdict: str | None,
    data_blocked: bool,
    coverage_blocked: bool,
) -> str:
    if data_blocked:
        return "DATA_BLOCKED"
    if coverage_blocked:
        return "COVERAGE_BLOCKED"
    if statistical_verdict is None:
        raise ValueError("unblocked run requires statistical verdict")
    return statistical_verdict
```

If a run-level blocker occurs before legal candidate evaluation, store SQL/CSV null in `statistical_verdict`; do not invent a sixth label such as NOT_EVALUATED. If approximate-PIT evaluation is legal, preserve its family statistical verdict even when final is blocked.

- [ ] **Step 4: Assemble verdict rows and shadow semantics**

Write `verdicts.csv` with:

```text
scope,subject,gate,gate_pass,status,reason_code,detail,provisional,
affects_statistical,statistical_verdict,final_verdict,
shadow_candidate,shadow_start_allowed
```

Rules:

- Candidate labels come only from structure/production/IM-boundary gates.
- Structure-failed candidates receive raw bootstrap tail probability 1 before the fixed two-candidate Holm-style adjustment.
- `shadow_candidate=true` only for a candidate whose statistical label is PASS_SHADOW.
- `shadow_start_allowed=true` only when that candidate passes statistically and the run-level `final_verdict=PASS_SHADOW`.
- Approximate-PIT STOP/MEASURE_ONLY rows have `provisional=true`.
- PIT policy flips, missing q calibration, unexplained prices, true-disclosure coverage, SalG freshness and carry freshness are run-level data blockers.
- DATA_BLOCKED dominates COVERAGE_BLOCKED only in final verdict; neither erases legal statistical evidence.

- [ ] **Step 5: Build the final manifest**

Write `run_manifest.json` after every compact CSV is closed. Include:

```python
def git_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
```

Manifest fields:

```text
config_hash,code_commit,requested_data_end,common_historical_end,
stock_price_max_date,index_500_max_date,index_1000_max_date,
ic_carry_max_date,im_carry_max_date,salg_valid_through,
true_first_disclosure_coverage,im_launch_date,
invalid_formation_months,stage_manifest_hashes,input_file_hashes,
candidate_statistical_verdicts,family_statistical_verdict,final_verdict
```

Populate `input_file_hashes` with SHA-256 for every consumed materialized input (`monthly_exposures.csv.gz`, `stock_period_returns.csv.gz`, `axis_returns.csv`, `conditional_leg_returns.csv`, `state_components.csv`, structure outputs and the frozen config). Populate `stage_manifest_hashes` with the byte hash of each verified parent manifest. Database-backed preflight inputs are not mislabeled as files: add `database_source_evidence` containing the exact query-template hash plus row count, minimum date and maximum date for every source table read. These fields are evidence only; changing them never bypasses the configured gates.

Set `true_first_disclosure_coverage` from the model-row provenance field produced in Task 3 and record its verified numerator, required denominator and `coverage_basis`. The first-round conservative rule marks a style observation unverified whenever its dependency history contains CSMAR; on current 2014–2023 data the coverage is therefore 0 rather than an invented partial estimate. A later first-date backfill must replace this conservative field with fact-level verified dependencies before coverage can become 1. Compute SalG freshness from `salg_source_end_date`, not from non-null repeated values; compute carry freshness from the raw individual carry series, not the filled pre-launch panel.

- [ ] **Step 6: Add the eval CLI and stop conditions**

`python3 -m backtest.b3_eval` must:

1. Read and verify `preflight.json` first.
2. If preflight is blocked, emit only the legally available `verdicts.csv` and `run_manifest.json`, then exit 2 or 3 without requesting states, structure files or returns.
3. Otherwise verify `states.json` and the compact structure outputs share config hash/data cutoff, then build scores, candidate returns, bootstrap, metrics and all verdict rows.
4. Keep 2024–2026 rows `affects_verdict=false`.
5. Exit 0 for STOP, MEASURE_ONLY or PASS_SHADOW because those are completed research outcomes; exit 2/3 only for blockers; exit 1 for implementation errors.

- [ ] **Step 7: Run B3 unit tests**

Run:

```bash
python3 -m pytest tests/test_b3_exposures.py tests/test_b3_portfolios_states.py tests/test_b3_structure.py tests/test_b3_eval.py -q
```

Expected: all B3 tests pass with zero failures.

- [ ] **Step 8: Capture production-output hashes before the full regression run**

Run:

```bash
git ls-files -z output/style_basket output/equal_weight output/recommended | xargs -0 sha256sum > /tmp/b3-production-before.sha256
```

Expected: the hash file is non-empty.

- [ ] **Step 9: Run the full existing suite**

Run:

```bash
python3 -m pytest tests/ -q
```

Expected: zero failures.

- [ ] **Step 10: Prove B1/B2/equal_weight committed outputs did not drift**

Run:

```bash
git ls-files -z output/style_basket output/equal_weight output/recommended | xargs -0 sha256sum > /tmp/b3-production-after.sha256
diff -u /tmp/b3-production-before.sha256 /tmp/b3-production-after.sha256
```

Expected: `diff` exits 0 with no output.

- [ ] **Step 11: Run the current-data fail-closed integration check**

Run:

```bash
python3 -m signals.style_basket.b3_build --stage preflight --data-end 2023-12-31
python3 -m backtest.b3_eval
```

Expected on the 2026-07-14 database snapshot: both commands exit 2 for missing `000852.SH` constituent history, with no forward-return file read and no production output modified. The eval command writes only blocked-run evidence. Inspect:

```bash
python3 -m json.tool output/style_basket/b3/manifests/preflight.json
python3 -m json.tool backtest/output/b3/run_manifest.json
```

Expected: preflight `status` is `DATA_BLOCKED`, its blocker `reason_code` is `TARGET_COORDINATE_CALIBRATION`, the detail names `000852.SH`, and the final run manifest has null family statistical verdict plus `final_verdict=DATA_BLOCKED`.

If q1000 constituent history has been restored before this task, run the full sequence instead:

```bash
python3 -m signals.style_basket.b3_build --stage all --data-end 2026-07-10
python3 -m backtest.b3_structure
python3 -m backtest.b3_eval
```

Expected: each command either exits 0 with all declared products or exits 2/3 with an explicit audited blocker. A missing product without a blocker is a failure.

- [ ] **Step 12: Check generated schemas without committing research results**

Run:

```bash
python3 -c "import json, pathlib; root=pathlib.Path('backtest/output/b3'); present={p.name for p in root.iterdir()}; blocked={'verdicts.csv','run_manifest.json'}; full=blocked|{'structure_coefficients.csv','model_comparison.csv','production_metrics.csv','yearly_contribution.csv','bootstrap.csv'}; manifest=json.loads((root/'run_manifest.json').read_text()); print(sorted(present)); assert blocked <= present; assert 'family_statistical_verdict' in manifest and 'final_verdict' in manifest; assert manifest['family_statistical_verdict'] is None or full <= present"
```

Expected: on an unblocked historical run all seven compact products exist and manifest verdict fields are present. On the currently known preflight block, `verdicts.csv` and `run_manifest.json` exist, family statistical verdict is null, and no performance product is required.

Do not `git add` generated compact outputs in this implementation task. The design requires user review of the research result before deciding whether those artifacts become committed evidence.

- [ ] **Step 13: Commit implementation**

```bash
git add backtest/b3_eval.py tests/test_b3_eval.py
git commit -m "feat(b3): add fail-closed verdict pipeline"
```

## Execution handoff

When all implementation tasks are complete, report separately:

1. software verification status and test count;
2. real-data stage reached;
3. any DATA/COVERAGE blocker with its exact audit row;
4. approximate-PIT candidate/family statistical verdict if legally computable;
5. final verdict and whether any shadow start is actually allowed.

Never summarize a statistical PASS_SHADOW candidate as “shadow started.” The latter requires `final_verdict=PASS_SHADOW` and a separate shadow-infrastructure plan.
