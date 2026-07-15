"""B3 point-in-time policy handling and monthly style snapshot assembly."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from signals.common.config import load_db_config
from signals.common.factors import asof_latest
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
STAGE_OUTPUTS = {
    "preflight": {
        "coverage_audit.csv",
        "exposure_diagnostics.csv",
    },
    "exposures": {"monthly_exposures.csv.gz"},
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class B3Sources:
    snapshots: Callable[..., dict[pd.Timestamp, pd.DataFrame]]
    constituents: Callable[..., pd.DataFrame]
    stock_returns: Callable[..., pd.DataFrame]
    target_returns: Callable[..., pd.DataFrame]
    carry: Callable[..., pd.DataFrame | pd.Series]


@dataclass(frozen=True)
class PreflightOutcome:
    final_status: str
    exposures: dict[str, dict[pd.Timestamp, ExposureResult]]
    audit: pd.DataFrame
    diagnostics: pd.DataFrame


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_stage_manifest(
    output_dir: str | Path,
    stage: str,
    cfg: dict,
    data_end: pd.Timestamp,
    outputs: list[str | Path],
    status: str,
    blockers: list[dict],
) -> Path:
    root = Path(output_dir)
    manifest_dir = root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    output_hashes: dict[str, str] = {}
    for output in outputs:
        path = Path(output)
        if not path.is_file():
            raise FileNotFoundError(f"declared stage output is missing: {path}")
        root_resolved = root.resolve()
        path_resolved = path.resolve()
        if not path_resolved.is_relative_to(root_resolved):
            raise ValueError(f"stage output escapes output root: {path}")
        try:
            relative = path.absolute().relative_to(root.absolute())
        except ValueError as exc:
            raise ValueError(
                f"stage output is outside output root: {path}"
            ) from exc
        output_hashes[relative.as_posix()] = _sha256(path_resolved)

    expected_outputs = STAGE_OUTPUTS.get(stage)
    if expected_outputs is None:
        raise ValueError(f"unknown manifest stage: {stage}")
    if set(output_hashes) != expected_outputs:
        raise ValueError(
            f"{stage} manifest output set mismatch: "
            f"expected {sorted(expected_outputs)}, "
            f"got {sorted(output_hashes)}"
        )

    payload = {
        "stage": stage,
        "config_hash": config_hash(cfg),
        "data_end": str(pd.Timestamp(data_end).date()),
        "status": status,
        "blockers": list(blockers),
        "outputs": output_hashes,
    }
    manifest_path = manifest_dir / f"{stage}.json"
    temp_path = manifest_dir / f".{stage}.json.tmp"
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    try:
        temp_path.write_text(rendered, encoding="utf-8")
        temp_path.replace(manifest_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return manifest_path


def _invalidate_stage_manifest(
    output_dir: str | Path,
    stage: str,
) -> None:
    manifest_dir = Path(output_dir) / "manifests"
    (manifest_dir / f"{stage}.json").unlink(missing_ok=True)
    (manifest_dir / f".{stage}.json.tmp").unlink(missing_ok=True)


def _write_csv_atomic(
    frame: pd.DataFrame,
    path: str | Path,
    **kwargs,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        frame.to_csv(temporary, **kwargs)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _verified_manifest_output(
    root: Path,
    relative: object,
    parent: str,
) -> Path:
    if not isinstance(relative, str) or not relative:
        raise DataBlocked(f"parent output path is unsafe: {relative!r}")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise DataBlocked(f"parent output path is unsafe: {relative}")
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DataBlocked(
            f"parent output is missing: {relative}"
        ) from exc
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise DataBlocked(
            f"parent output path escapes output root: {relative}"
        )
    if not resolved.is_file():
        raise DataBlocked(f"parent output is not a file: {relative}")
    return resolved


def require_parent_manifest(
    output_dir: str | Path,
    parent: str,
    cfg: dict,
    data_end: pd.Timestamp,
) -> dict:
    root = Path(output_dir)
    path = root / "manifests" / f"{parent}.json"
    if not path.is_file():
        raise DataBlocked(f"missing parent manifest: {parent}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise DataBlocked(f"invalid parent manifest: {parent}") from exc

    if not isinstance(manifest, dict):
        raise DataBlocked(f"parent manifest must be a JSON object: {parent}")
    if manifest.get("stage") != parent:
        raise DataBlocked(f"parent manifest stage mismatch: {parent}")
    if manifest.get("config_hash") != config_hash(cfg):
        raise DataBlocked(f"parent config hash mismatch: {parent}")
    expected_end = str(pd.Timestamp(data_end).date())
    if manifest.get("data_end") != expected_end:
        raise DataBlocked(f"parent data_end mismatch: {parent}")
    if manifest.get("status") != "OK":
        raise DataBlocked(f"parent stage is not OK: {parent}")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise DataBlocked(f"parent outputs are missing: {parent}")
    for relative in outputs:
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise DataBlocked(
                f"parent output path is unsafe: {relative!r}"
            )
    expected_outputs = STAGE_OUTPUTS.get(parent)
    if expected_outputs is None:
        raise DataBlocked(f"unknown parent stage: {parent}")
    if set(outputs) != expected_outputs:
        raise DataBlocked(
            f"parent output set mismatch: {parent}; "
            f"expected {sorted(expected_outputs)}, "
            f"got {sorted(outputs)}"
        )
    for relative, expected_hash in outputs.items():
        if (
            not isinstance(expected_hash, str)
            or _SHA256_RE.fullmatch(expected_hash) is None
        ):
            raise DataBlocked(
                f"parent output hash format is invalid: {relative}"
            )
        output = _verified_manifest_output(root, relative, parent)
        if _sha256(output) != expected_hash:
            raise DataBlocked(f"parent output hash mismatch: {relative}")
    return manifest


def _require_columns(
    frame: pd.DataFrame,
    required: set[str],
    label: str,
) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise DataBlocked(
            f"{label} is missing columns: " + ", ".join(missing)
        )


def _validate_string_keys(values, label: str) -> None:
    invalid = [
        value
        for value in values
        if not isinstance(value, str) or not value.strip()
    ]
    if invalid:
        raise DataBlocked(f"{label} must contain nonempty string keys")


def _validate_allowed_values(
    values: pd.Series,
    allowed: set[str],
    label: str,
) -> None:
    invalid = ~values.map(
        lambda value: isinstance(value, str) and value in allowed
    )
    if invalid.any():
        rendered = sorted({repr(value) for value in values[invalid]})
        raise DataBlocked(
            f"{label} contains unsupported values: " + ", ".join(rendered)
        )


def _validate_datetime_columns(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
    label: str,
    *,
    nullable: set[str] | None = None,
) -> pd.DataFrame:
    nullable = nullable or set()
    out = frame.copy()
    for column in columns:
        original = out[column]
        parsed = pd.to_datetime(
            original,
            errors="coerce",
            format="mixed",
        )
        invalid = original.notna() & parsed.isna()
        if column not in nullable:
            invalid |= parsed.isna()
        if invalid.any():
            raise DataBlocked(f"{label}.{column} contains invalid dates")
        out[column] = parsed
    return out


def _deduplicate_or_block(
    frame: pd.DataFrame,
    key_columns: tuple[str, ...],
    label: str,
) -> pd.DataFrame:
    deduplicated = frame.drop_duplicates().copy()
    conflicting = deduplicated.duplicated(
        subset=list(key_columns),
        keep=False,
    )
    if conflicting.any():
        raise DataBlocked(f"{label} contains conflicting duplicate keys")
    return deduplicated.reset_index(drop=True)


def _validated_close_matrix(closes: pd.DataFrame) -> pd.DataFrame:
    matrix = closes.copy()
    _validate_string_keys(matrix.columns, "close columns")
    if matrix.columns.duplicated().any():
        raise DataBlocked("close matrix contains duplicate ticker columns")

    dates = pd.DataFrame({"formation_date": list(matrix.index)})
    dates = _validate_datetime_columns(
        dates,
        ("formation_date",),
        "close matrix",
    )
    if dates["formation_date"].duplicated().any():
        raise DataBlocked("close matrix contains duplicate formation dates")
    matrix.index = pd.DatetimeIndex(dates["formation_date"])
    return matrix


def _strict_datetime_series(
    values: pd.Series,
    label: str,
    *,
    nullable: bool,
) -> pd.Series:
    missing = values.isna()
    if not nullable and missing.any():
        raise DataBlocked(f"{label} contains missing dates")

    parsed = []
    try:
        for is_missing, value in zip(missing, values):
            if is_missing:
                parsed.append(pd.NaT)
            else:
                if isinstance(value, (bool, int, float, np.number)):
                    raise TypeError(
                        f"{label} requires date-like values, got {value!r}"
                    )
                parsed.append(pd.Timestamp(value))
        return pd.Series(
            parsed,
            index=values.index,
            dtype="datetime64[ns]",
            name=values.name,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataBlocked(f"{label} contains invalid dates") from exc


def _canonical_payload(payload, label: str) -> str:
    if not isinstance(payload, dict):
        exc = TypeError(
            f"{label} must be a dict, got {type(payload).__name__}"
        )
        raise DataBlocked(f"{label} must be a canonicalizable dict") from exc
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DataBlocked(f"{label} must be a canonicalizable dict") from exc


def _validate_raw_financial_facts(raw: pd.DataFrame) -> pd.DataFrame:
    required = {
        "ts_code",
        "end_date",
        "stored_ann_date",
        "statement_type",
        "data",
        "data_source",
    }
    _require_columns(raw, required, "raw financial facts")

    out = raw.copy()
    _validate_string_keys(out["ts_code"], "raw financial ts_code")
    _validate_string_keys(
        out["statement_type"],
        "raw financial statement_type",
    )
    _validate_allowed_values(
        out["data_source"],
        {"csmar", "wind"},
        "raw financial data_source",
    )
    out["end_date"] = _strict_datetime_series(
        out["end_date"],
        "raw financial facts.end_date",
        nullable=False,
    )
    out["stored_ann_date"] = _strict_datetime_series(
        out["stored_ann_date"],
        "raw financial facts.stored_ann_date",
        nullable=True,
    )

    canonical_payloads = pd.Series(
        [
            _canonical_payload(payload, "raw financial facts.data")
            for payload in out["data"]
        ],
        index=out.index,
        name="_canonical_payload",
        dtype=object,
    )

    semantic_key = [
        "ts_code",
        "end_date",
        "stored_ann_date",
        "statement_type",
        "data_source",
    ]
    identity = out[semantic_key].copy()
    identity["_canonical_payload"] = canonical_payloads
    unique_identities = identity.drop_duplicates(
        subset=semantic_key + ["_canonical_payload"]
    )
    if unique_identities.duplicated(
        subset=semantic_key,
        keep=False,
    ).any():
        raise DataBlocked(
            "raw financial facts contain conflicting payloads "
            "for the same semantic key"
        )

    keep = ~identity.duplicated(
        subset=semantic_key + ["_canonical_payload"],
        keep="first",
    )
    out = out.loc[keep].reset_index(drop=True)

    wind = out["data_source"].eq("wind")
    if (wind & out["stored_ann_date"].isna()).any():
        raise DataBlocked("Wind facts require stored announcement dates")

    before_period_end = (
        out["stored_ann_date"].notna()
        & out["stored_ann_date"].lt(out["end_date"])
    )
    if before_period_end.any():
        raise DataBlocked(
            "stored announcement date cannot precede financial period end"
        )
    return out


def _validated_constituents(
    constituents: pd.DataFrame,
) -> pd.DataFrame:
    if not isinstance(constituents, pd.DataFrame):
        raise DataBlocked("target constituents must be a DataFrame")
    required = {"index_code", "effective_date", "ticker"}
    _require_columns(constituents, required, "target constituents")
    pool = constituents.loc[
        :,
        ["index_code", "effective_date", "ticker"],
    ].copy()
    _validate_string_keys(pool["index_code"], "constituent index_code")
    _validate_string_keys(pool["ticker"], "constituent ticker")
    pool = _validate_datetime_columns(
        pool,
        ("effective_date",),
        "target constituents",
    ).drop_duplicates()
    return pool


def calibrate_target_coordinates(
    exposures: dict[pd.Timestamp, ExposureResult],
    constituents: pd.DataFrame,
) -> dict[str, float]:
    pool = _validated_constituents(constituents)

    targets = {
        "q500": "000905.SH",
        "q1000": "000852.SH",
    }
    available_codes = set(pool["index_code"])
    for index_code in targets.values():
        if index_code not in available_codes:
            raise DataBlocked(f"missing constituents for {index_code}")

    q500_errors: list[float] = []
    q1000_errors: list[float] = []
    ordered_months = 0
    calibrated_months = 0
    for formation_date, result in sorted(exposures.items()):
        formation = pd.Timestamp(formation_date)
        if formation < pd.Timestamp("2021-01-01"):
            continue
        if not {"ticker", "m_perp"}.issubset(result.size.columns):
            raise DataBlocked("size exposure columns are missing")
        size = pd.to_numeric(
            result.size.set_index("ticker")["m_perp"],
            errors="coerce",
        )

        actual: dict[str, float] = {}
        for coordinate, index_code in targets.items():
            history = pool[
                pool["index_code"].eq(index_code)
                & pool["effective_date"].le(formation)
            ]
            if history.empty:
                raise DataBlocked(
                    f"missing constituents for {index_code} "
                    f"at {formation.date()}"
                )
            effective = history["effective_date"].max()
            members = history.loc[
                history["effective_date"].eq(effective),
                "ticker",
            ].drop_duplicates()
            member_exposure = size.reindex(members).dropna()
            if member_exposure.empty:
                raise DataBlocked(
                    f"missing constituent exposures for {index_code} "
                    f"at {formation.date()}"
                )
            value = float(member_exposure.median())
            if not np.isfinite(value):
                raise DataBlocked(
                    f"invalid constituent exposure for {index_code} "
                    f"at {formation.date()}"
                )
            actual[coordinate] = value

        q500_errors.append(
            abs(actual["q500"] - float(result.q["q500"]))
        )
        q1000_errors.append(
            abs(actual["q1000"] - float(result.q["q1000"]))
        )
        ordered_months += int(
            float(result.q["q1000"]) > float(result.q["q500"])
        )
        calibrated_months += 1

    if calibrated_months == 0:
        raise DataBlocked("no 2021+ calibration months")

    diagnostics = {
        "q500_mean_abs_error": float(np.mean(q500_errors)),
        "q1000_mean_abs_error": float(np.mean(q1000_errors)),
        "q_order_share": float(ordered_months / calibrated_months),
    }
    if (
        diagnostics["q500_mean_abs_error"] > 0.25
        or diagnostics["q1000_mean_abs_error"] > 0.25
        or diagnostics["q_order_share"] < 0.90
    ):
        raise DataBlocked(
            "target-coordinate calibration thresholds failed: "
            + json.dumps(diagnostics, sort_keys=True)
        )
    return diagnostics


def apply_pit_policy(raw: pd.DataFrame, policy: str) -> pd.DataFrame:
    """Apply one of B3's two disclosure-date policies without losing provenance."""
    if policy not in {POLICY_MAIN, POLICY_LAG}:
        raise ValueError(
            f"unsupported PIT policy {policy!r}; expected "
            f"{POLICY_MAIN!r} or {POLICY_LAG!r}"
        )

    out = _validate_raw_financial_facts(raw)

    legal_dates = pd.Series(
        [
            pd.Timestamp(legal_disclosure_deadline(end_date.date()))
            for end_date in out["end_date"]
        ],
        index=out.index,
        dtype="datetime64[ns]",
    )
    csmar = out["data_source"].eq("csmar")
    wind = out["data_source"].eq("wind")

    out["ann_date"] = pd.Series(
        pd.NaT, index=out.index, dtype="datetime64[ns]"
    )
    if policy == POLICY_MAIN:
        csmar_known = out["stored_ann_date"].where(
            out["stored_ann_date"].le(legal_dates),
            legal_dates,
        )
    else:
        csmar_known = legal_dates + pd.offsets.MonthEnd(1)

    out.loc[csmar, "ann_date"] = csmar_known.loc[csmar]
    out.loc[wind, "ann_date"] = out.loc[wind, "stored_ann_date"]
    out["ann_date"] = pd.to_datetime(out["ann_date"])

    out["known_date_source"] = pd.Series(
        "", index=out.index, dtype=object
    )
    out.loc[csmar, "known_date_source"] = policy
    out.loc[wind, "known_date_source"] = "wind_first_disclosure"

    out["true_first_disclosure_verified"] = False
    out.loc[wind, "true_first_disclosure_verified"] = True
    out["true_first_disclosure_verified"] = out[
        "true_first_disclosure_verified"
    ].astype(bool)
    return out


def _fetch_raw_financial(
    tickers,
    start,
    end,
    db,
) -> pd.DataFrame:
    """Read source-preserving financial facts across the CSMAR/Wind cutoff."""
    db = db or load_db_config()
    sql = f"""
        SELECT ts_code,
               end_date,
               ann_date AS stored_ann_date,
               statement_type,
               data,
               data_source
        FROM {db['schema']}.stock_financial
        WHERE ts_code = ANY(%s)
          AND end_date BETWEEN %s AND %s
          AND (
              (data_source = 'csmar' AND end_date <= %s)
              OR
              (data_source = 'wind' AND end_date > %s)
          )
        ORDER BY ts_code, statement_type, end_date
    """
    params = [list(tickers), start, end, CSMAR_END, CSMAR_END]

    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [description[0] for description in cur.description]
    finally:
        conn.close()

    facts = pd.DataFrame(rows, columns=columns)
    if facts.empty:
        raise DataBlocked(
            f"no financial facts for requested window {start}..{end}"
        )

    facts = _validate_raw_financial_facts(facts)
    translated = []
    for data, source, statement in zip(
        facts["data"],
        facts["data_source"],
        facts["statement_type"],
    ):
        try:
            translated.append(translate_data(data, source, statement))
        except (AttributeError, TypeError, ValueError) as exc:
            raise DataBlocked(
                "failed to translate raw financial payload"
            ) from exc
    facts["data"] = translated
    return facts.reset_index(drop=True)


def _read_sql(
    db: dict,
    sql: str,
    params=None,
) -> pd.DataFrame:
    conn = _connect(db)
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()


def _formation_inputs(
    db: dict,
    data_end: pd.Timestamp,
) -> dict[str, object]:
    data_end = pd.Timestamp(data_end)
    schema = db["schema"]

    calendar = _read_sql(
        db,
        f"""
            SELECT DISTINCT trade_date
            FROM {schema}.index_daily
            WHERE index_code = '000905.SH'
              AND trade_date BETWEEN '2013-05-01' AND %(end)s
            ORDER BY trade_date
        """,
        {"end": data_end.date()},
    )
    if calendar.empty:
        raise DataBlocked("formation trading calendar is empty")
    _require_columns(calendar, {"trade_date"}, "formation calendar")
    calendar = _validate_datetime_columns(
        calendar,
        ("trade_date",),
        "formation calendar",
    )
    trading = pd.DatetimeIndex(calendar["trade_date"])
    month_ends = list(
        pd.Series(trading, index=trading)
        .groupby(trading.to_period("M"))
        .max()
    )

    meta = _read_sql(
        db,
        f"""
            SELECT ts_code AS ticker, list_date, delist_date
            FROM {schema}.stock_meta
            ORDER BY ts_code
        """,
    )
    if meta.empty:
        raise DataBlocked("stock metadata is empty")
    _require_columns(
        meta,
        {"ticker", "list_date", "delist_date"},
        "stock metadata",
    )
    _validate_string_keys(meta["ticker"], "stock metadata ticker")
    tickers = meta["ticker"].tolist()

    closes = _read_sql(
        db,
        f"""
            SELECT ts_code AS ticker, trade_date, close
            FROM {schema}.stock_daily_price
            WHERE trade_date = ANY(%(dates)s)
            ORDER BY trade_date, ts_code
        """,
        {"dates": [date.date() for date in month_ends]},
    )
    _require_columns(
        closes,
        {"ticker", "trade_date", "close"},
        "formation closes",
    )
    _validate_string_keys(closes["ticker"], "formation close ticker")
    closes = _validate_datetime_columns(
        closes,
        ("trade_date",),
        "formation closes",
    )
    if closes.duplicated(["trade_date", "ticker"]).any():
        raise DataBlocked("formation closes contain duplicate close keys")
    close_wide = closes.pivot(
        index="trade_date",
        columns="ticker",
        values="close",
    )

    shares = _read_sql(
        db,
        f"""
            SELECT ts_code,
                   effective_date AS end_date,
                   available_date AS known_date,
                   total_shares
            FROM {schema}.stock_share_capital
            WHERE total_shares IS NOT NULL AND total_shares > 0
            ORDER BY ts_code, effective_date
        """,
    )
    _require_columns(
        shares,
        {"ts_code", "end_date", "known_date", "total_shares"},
        "share capital",
    )
    _validate_string_keys(shares["ts_code"], "share capital ticker")
    shares = _validate_datetime_columns(
        shares,
        ("end_date", "known_date"),
        "share capital",
        nullable={"known_date"},
    )
    shares["known_date"] = shares["known_date"].fillna(shares["end_date"])

    industry = _read_sql(
        db,
        f"""
            SELECT ts_code AS ticker,
                   effective_date,
                   level_1_name AS industry
            FROM {schema}.industry_classification
            WHERE classification_type = 'CITIC'
            ORDER BY ts_code, effective_date
        """,
    )
    _require_columns(
        industry,
        {"ticker", "effective_date", "industry"},
        "industry history",
    )
    _validate_string_keys(industry["ticker"], "industry ticker")
    industry = _validate_datetime_columns(
        industry,
        ("effective_date",),
        "industry history",
    )

    facts = _fetch_raw_financial(
        tickers,
        "2003-01-01",
        str(data_end.date()),
        db,
    )
    return {
        "month_ends": month_ends,
        "meta": meta,
        "closes": close_wide,
        "shares": shares,
        "industry": industry,
        "facts": facts,
    }


def _industry_snapshot(
    pool: pd.DataFrame,
    formation_date: pd.Timestamp,
) -> pd.Series:
    """Return deterministic CITIC labels, extending each earliest label backward."""
    empty = pd.Series(
        index=pd.Index([], name="ticker"),
        dtype=object,
        name="industry",
    )
    if pool.empty:
        return empty

    required = {"ticker", "effective_date", "industry"}
    _require_columns(pool, required, "industry pool")
    history = pool.loc[
        :,
        ["ticker", "effective_date", "industry"],
    ].copy()
    _validate_string_keys(history["ticker"], "industry ticker")
    history = _validate_datetime_columns(
        history,
        ("effective_date",),
        "industry pool",
    )
    history = _deduplicate_or_block(
        history,
        ("ticker", "effective_date"),
        "industry pool",
    )
    history["_label_sort"] = (
        history["industry"].fillna("UNKNOWN").astype(str)
    )
    history = history.sort_values(
        ["ticker", "effective_date", "_label_sort"],
        kind="mergesort",
    )

    earliest = (
        history.groupby("ticker", sort=True, as_index=False)
        .head(1)
        .copy()
    )
    earliest["effective_date"] = pd.Timestamp("1900-01-01")
    extended = pd.concat([earliest, history], ignore_index=True)

    known = extended[
        extended["effective_date"] <= pd.Timestamp(formation_date)
    ]
    if known.empty:
        return empty

    selected = (
        known.sort_values(
            ["ticker", "effective_date", "_label_sort"],
            kind="mergesort",
        )
        .groupby("ticker", sort=True, as_index=False)
        .tail(1)
        .sort_values("ticker", kind="mergesort")
    )
    result = (
        selected.set_index("ticker")["industry"]
        .fillna("UNKNOWN")
        .astype(str)
    )
    result.index.name = "ticker"
    result.name = "industry"
    return result


def build_policy_snapshots(
    raw_facts: pd.DataFrame,
    month_ends,
    closes: pd.DataFrame,
    shares_pool: pd.DataFrame,
    industry_pool: pd.DataFrame,
    stock_meta: pd.DataFrame,
    policy: str,
) -> dict[pd.Timestamp, pd.DataFrame]:
    """Assemble B3 monthly style snapshots under one PIT policy."""
    from signals.style_basket.build import (
        FINANCIAL_INDUSTRIES,
        MIN_LISTED_DAYS,
        ticker_financial_rows,
    )
    from signals.style_basket.scoring import style_scores

    facts = apply_pit_policy(raw_facts, policy)

    empty_columns = {
        "ttm": ["ts_code", "field", "end_date", "known_date", "ttm"],
        "slope": [
            "ts_code",
            "field",
            "end_date",
            "known_date",
            "slope",
        ],
        "event": [
            "ts_code",
            "field",
            "end_date",
            "known_date",
            "value",
        ],
    }
    pool_parts: dict[str, list[pd.DataFrame]] = {
        "ttm": [],
        "slope": [],
        "event": [],
    }
    for _, ticker_facts in facts.groupby("ts_code", sort=True):
        rows = ticker_financial_rows(ticker_facts)
        for name in pool_parts:
            part = rows.get(name)
            if part is not None and not part.empty:
                pool_parts[name].append(part.copy())

    pools: dict[str, pd.DataFrame] = {}
    for name, parts in pool_parts.items():
        if parts:
            pool = pd.concat(parts, ignore_index=True)
            _require_columns(
                pool,
                set(empty_columns[name]),
                f"derived {name} pool",
            )
            _validate_string_keys(
                pool["ts_code"],
                f"derived {name} ts_code",
            )
            _validate_string_keys(
                pool["field"],
                f"derived {name} field",
            )
            pool = _validate_datetime_columns(
                pool,
                ("end_date", "known_date"),
                f"derived {name} pool",
            )
            pool = _deduplicate_or_block(
                pool,
                ("ts_code", "field", "end_date"),
                f"derived {name} pool",
            )
        else:
            pool = pd.DataFrame(columns=empty_columns[name])
            pool["end_date"] = pd.to_datetime(pool["end_date"])
            pool["known_date"] = pd.to_datetime(pool["known_date"])
        pools[name] = pool

    def asof_selected(
        pool: pd.DataFrame,
        date: pd.Timestamp,
        field: str,
    ) -> pd.DataFrame:
        selected_pool = pool[pool["field"].eq(field)]
        selected = asof_latest(selected_pool, date)
        if selected.empty:
            return selected.set_index("ts_code")
        return selected.set_index("ts_code").sort_index()

    def asof_field(
        pool: pd.DataFrame,
        date: pd.Timestamp,
        field: str,
        column: str,
    ) -> pd.Series:
        selected = asof_selected(pool, date, field)
        if selected.empty:
            return pd.Series(dtype=float, name=column)
        return selected[column].rename(column)

    required_meta = {"ticker", "list_date", "delist_date"}
    _require_columns(stock_meta, required_meta, "stock metadata")
    meta = stock_meta.loc[
        :,
        ["ticker", "list_date", "delist_date"],
    ].copy()
    _validate_string_keys(meta["ticker"], "stock metadata ticker")
    meta = _validate_datetime_columns(
        meta,
        ("list_date", "delist_date"),
        "stock metadata",
        nullable={"list_date", "delist_date"},
    )
    meta = _deduplicate_or_block(
        meta,
        ("ticker",),
        "stock metadata",
    )
    meta = (
        meta.sort_values("ticker", kind="mergesort")
        .set_index("ticker")
        .sort_index()
    )

    close_matrix = _validated_close_matrix(closes)

    share_columns = {
        "ts_code",
        "end_date",
        "known_date",
        "total_shares",
    }
    if shares_pool.empty:
        share_history = pd.DataFrame(columns=sorted(share_columns))
    else:
        _require_columns(shares_pool, share_columns, "shares pool")
        share_history = shares_pool.loc[
            :,
            ["ts_code", "end_date", "known_date", "total_shares"],
        ].copy()
        _validate_string_keys(
            share_history["ts_code"],
            "shares pool ts_code",
        )
        share_history = _validate_datetime_columns(
            share_history,
            ("end_date", "known_date"),
            "shares pool",
        )
        share_history = _deduplicate_or_block(
            share_history,
            ("ts_code", "end_date"),
            "shares pool",
        )
    if share_history.empty:
        share_history["end_date"] = pd.to_datetime(
            share_history["end_date"]
        )
        share_history["known_date"] = pd.to_datetime(
            share_history["known_date"]
        )

    snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
    formation_dates = sorted(
        {pd.Timestamp(date) for date in month_ends}
    )
    for formation_date in formation_dates:
        if formation_date not in close_matrix.index:
            raise DataBlocked(
                "missing formation close row for "
                f"{formation_date.date()}"
            )

        active = (
            (meta["list_date"].isna() | meta["list_date"].le(formation_date))
            & (
                meta["delist_date"].isna()
                | meta["delist_date"].ge(formation_date)
            )
        )
        base = meta.index[active].sort_values()

        close_row = close_matrix.loc[formation_date]
        if isinstance(close_row, pd.DataFrame):
            raise DataBlocked(
                f"duplicate formation close rows for {formation_date.date()}"
            )
        close = pd.to_numeric(
            close_row.reindex(base),
            errors="coerce",
        )

        selected_shares = asof_latest(share_history, formation_date)
        if selected_shares.empty:
            shares = pd.Series(
                np.nan,
                index=base,
                dtype=float,
                name="total_shares",
            )
        else:
            shares = pd.to_numeric(
                selected_shares.set_index("ts_code")[
                    "total_shares"
                ].reindex(base),
                errors="coerce",
            )
        market_value = shares * close

        list_dates = meta["list_date"].reindex(base)
        listed_lt_180 = list_dates.notna() & (
            list_dates + pd.Timedelta(days=MIN_LISTED_DAYS)
            > formation_date
        )
        size_reason = pd.Series("", index=base, dtype=object)
        size_reason.loc[list_dates.isna()] = "DATA_MISSING_LIST_DATE"
        size_reason.loc[
            size_reason.eq("") & listed_lt_180
        ] = "LISTED_LT_180D"
        size_reason.loc[
            size_reason.eq("") & close.isna()
        ] = "DATA_MISSING_CLOSE"
        size_reason.loc[
            size_reason.eq("") & shares.isna()
        ] = "DATA_MISSING_SHARES"

        invalid_market_value = pd.Series(
            ~np.isfinite(market_value.to_numpy(dtype=float))
            | market_value.le(0.0).to_numpy(),
            index=base,
        )
        size_reason.loc[
            size_reason.eq("") & invalid_market_value
        ] = "DATA_INVALID_MARKET_VALUE"
        size_eligible = size_reason.eq("")

        industry = (
            _industry_snapshot(industry_pool, formation_date)
            .reindex(base)
            .fillna("UNKNOWN")
            .astype(str)
        )

        rev_slope_selected = asof_selected(
            pools["slope"],
            formation_date,
            "rev",
        )
        salg_source_end_date = pd.to_datetime(
            rev_slope_selected["end_date"],
            errors="coerce",
        ).reindex(base)

        sal_g = pd.to_numeric(
            asof_field(
                pools["slope"],
                formation_date,
                "rev",
                "slope",
            ),
            errors="coerce",
        )
        pro_g = pd.to_numeric(
            asof_field(
                pools["slope"],
                formation_date,
                "np",
                "slope",
            ),
            errors="coerce",
        )
        np_ttm = pd.to_numeric(
            asof_field(
                pools["ttm"],
                formation_date,
                "np",
                "ttm",
            ),
            errors="coerce",
        )
        cfo_ttm = pd.to_numeric(
            asof_field(
                pools["ttm"],
                formation_date,
                "cfo",
                "ttm",
            ),
            errors="coerce",
        )
        equity = pd.to_numeric(
            asof_field(
                pools["event"],
                formation_date,
                "equity",
                "value",
            ),
            errors="coerce",
        )
        dps = pd.to_numeric(
            asof_field(
                pools["event"],
                formation_date,
                "dps",
                "value",
            ),
            errors="coerce",
        )

        eligible_tickers = base[size_eligible]
        factors = pd.DataFrame(index=eligible_tickers)
        eligible_market_value = market_value.reindex(eligible_tickers)
        eligible_shares = shares.reindex(eligible_tickers)
        factors["sal_g"] = sal_g.reindex(eligible_tickers)
        factors["pro_g"] = pro_g.reindex(eligible_tickers)
        factors["ep"] = (
            np_ttm.reindex(eligible_tickers) / eligible_market_value
        )
        factors["bp"] = (
            equity.reindex(eligible_tickers) / eligible_market_value
        )
        factors["cfp"] = (
            cfo_ttm.reindex(eligible_tickers) / eligible_market_value
        )
        financial = industry.reindex(eligible_tickers).isin(
            FINANCIAL_INDUSTRIES
        )
        factors.loc[financial, "cfp"] = np.nan
        factors["dp"] = (
            dps.reindex(eligible_tickers)
            * eligible_shares
            / eligible_market_value
        )

        style_score = pd.Series(
            np.nan,
            index=base,
            dtype=float,
            name="style_score",
        )
        if len(factors):
            scored = style_scores(factors)
            if "style_score" not in scored.columns:
                raise DataBlocked(
                    "style scoring did not return style_score"
                )
            style_score.loc[eligible_tickers] = scored[
                "style_score"
            ].reindex(eligible_tickers)

        model_eligible = size_eligible & style_score.notna()
        model_reason = size_reason.copy()
        model_reason.loc[
            size_eligible & ~model_eligible
        ] = "MISSING_STYLE_SCORE"

        known_facts = facts[facts["ann_date"].le(formation_date)]
        if known_facts.empty:
            has_csmar_dependency = pd.Series(
                False,
                index=base,
                dtype=bool,
            )
        else:
            has_csmar = (
                known_facts.assign(
                    _is_csmar=known_facts["data_source"].eq("csmar")
                )
                .groupby("ts_code")["_is_csmar"]
                .any()
            )
            has_csmar_dependency = has_csmar.reindex(
                base,
                fill_value=False,
            ).astype(bool)
        verified = ~has_csmar_dependency

        snapshot = pd.DataFrame(
            {
                "ticker": base.to_numpy(),
                "formation_date": [formation_date] * len(base),
                "total_market_value": market_value.to_numpy(),
                "industry": industry.to_numpy(),
                "style_score": style_score.to_numpy(),
                "size_eligible": size_eligible.to_numpy(dtype=bool),
                "model_eligible": model_eligible.to_numpy(dtype=bool),
                "size_exclusion_reason": size_reason.to_numpy(),
                "model_exclusion_reason": model_reason.to_numpy(),
                "salg_source_end_date": (
                    salg_source_end_date.to_numpy()
                ),
                "true_first_disclosure_verified": verified.to_numpy(
                    dtype=bool
                ),
            }
        )
        snapshots[formation_date] = snapshot

    return snapshots


def default_sources(db: dict) -> B3Sources:
    """Create the frozen lazy data-source bundle."""
    from backtest.data import load_carry, load_underlying_returns

    cached_inputs: dict[str, dict[str, object]] = {}

    def inputs(data_end: pd.Timestamp) -> dict[str, object]:
        key = str(pd.Timestamp(data_end).date())
        if key not in cached_inputs:
            cached_inputs[key] = _formation_inputs(
                db,
                pd.Timestamp(data_end),
            )
        return cached_inputs[key]

    def snapshots(
        policy: str,
        data_end: pd.Timestamp,
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
        frame = _read_sql(
            db,
            f"""
                SELECT index_code,
                       ts_code AS ticker,
                       effective_date
                FROM {db['schema']}.index_constituent
                WHERE effective_date >= '2021-01-01'
                  AND index_code IN ('000905.SH', '000852.SH')
                ORDER BY index_code, effective_date, ts_code
            """,
        )
        return _validated_constituents(frame)

    def targets(data_end: pd.Timestamp) -> dict[str, pd.Series]:
        end = pd.Timestamp(data_end)
        return {
            target: load_underlying_returns(
                target,
                start="2013-01-01",
                db=db,
            ).loc[:end]
            for target in ["500", "1000", "blend"]
        }

    def carries(data_end: pd.Timestamp) -> dict[str, pd.Series]:
        end = pd.Timestamp(data_end)
        return {
            target: load_carry(
                target,
                start="2013-01-01",
                db=db,
            ).loc[:end]
            for target in ["500", "1000"]
        }

    return B3Sources(
        snapshots=snapshots,
        constituents=constituents,
        stock_returns=lambda data_end: _fetch_stock_return_status(
            db,
            data_end,
        ),
        target_returns=targets,
        carry=carries,
    )


def run_preflight(
    cfg: dict,
    sources: B3Sources,
    data_end: pd.Timestamp,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> PreflightOutcome:
    """Run return-blind data, coverage, and target-coordinate checks."""
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    _invalidate_stage_manifest(output_root, "preflight")
    data_end = pd.Timestamp(data_end)

    required_start = pd.Timestamp(cfg["windows"]["discovery"][0])
    required_end = pd.Timestamp(cfg["windows"]["confirmation"][1])
    policies = list(cfg["pit"]["policies"])
    axes = [
        "style",
        "size",
        "interaction",
        "qblend",
        "q500",
        "q1000",
    ]
    audit_columns = [
        "pit_policy",
        "formation_date",
        "required_formation",
        "affects_final",
        "check",
        "side",
        "eligible_count",
        "max_weight",
        "status",
        "reason_code",
        "detail",
    ]

    exposures: dict[str, dict[pd.Timestamp, ExposureResult]] = {
        policy: {} for policy in policies
    }
    audit_rows: list[dict] = []
    diagnostic_rows: list[dict] = []
    blockers: list[dict] = []

    def add_audit(
        *,
        policy: str,
        formation_date,
        required: bool,
        affects_final: bool | None = None,
        check: str,
        side: str = "",
        eligible_count=None,
        max_weight=None,
        status: str = "OK",
        reason_code: str = "",
        detail: str = "",
    ) -> dict:
        row = {
            "pit_policy": policy,
            "formation_date": formation_date,
            "required_formation": bool(required),
            "affects_final": (
                bool(required)
                if affects_final is None
                else bool(affects_final)
            ),
            "check": check,
            "side": side,
            "eligible_count": eligible_count,
            "max_weight": max_weight,
            "status": status,
            "reason_code": reason_code,
            "detail": detail,
        }
        audit_rows.append(row)
        return row

    def add_blocker(row: dict) -> None:
        blockers.append(
            {
                column: row.get(column)
                for column in audit_columns
            }
        )

    def audit_exclusions(
        policy: str,
        formation_date: pd.Timestamp,
        required: bool,
        snapshot: pd.DataFrame,
    ) -> None:
        for role, column in (
            ("size", "size_exclusion_reason"),
            ("model", "model_exclusion_reason"),
        ):
            if column not in snapshot.columns:
                continue
            reasons = snapshot[column].fillna("").astype(str)
            counts = reasons.value_counts().sort_index()
            for reason, count in counts.items():
                add_audit(
                    policy=policy,
                    formation_date=formation_date,
                    required=required,
                    affects_final=False,
                    check=f"{role}_exclusion",
                    side=str(reason) if reason else "ELIGIBLE",
                    eligible_count=int(count),
                    status="REPORT_ONLY",
                    reason_code=str(reason),
                    detail="exclusion distribution",
                )

    required_periods = set(
        pd.period_range(
            required_start.to_period("M"),
            required_end.to_period("M"),
            freq="M",
        )
    )
    constituents = pd.DataFrame()
    normalized_by_policy: dict[
        str,
        dict[pd.Timestamp, pd.DataFrame],
    ] = {}
    required_keys: dict[str, set[pd.Timestamp]] = {}

    if data_end < required_end:
        row = add_audit(
            policy="all",
            formation_date=pd.NaT,
            required=True,
            check="required_calendar",
            status="DATA_BLOCKED",
            reason_code="DATA_CONTRACT",
            detail=(
                f"data_end {data_end.date()} precedes required end "
                f"{required_end.date()}"
            ),
        )
        add_blocker(row)

    if not blockers:
        try:
            constituents = _validated_constituents(
                sources.constituents()
            )
            available_codes = set(constituents["index_code"])
            missing_codes = [
                code
                for code in ("000905.SH", "000852.SH")
                if code not in available_codes
            ]
            if missing_codes:
                raise DataBlocked(
                    "missing constituents for "
                    + ", ".join(missing_codes)
                )
        except (DataBlocked, CoverageBlocked) as exc:
            status = (
                "DATA_BLOCKED"
                if isinstance(exc, DataBlocked)
                else "COVERAGE_BLOCKED"
            )
            row = add_audit(
                policy="all",
                formation_date=pd.NaT,
                required=True,
                check="target_coordinate_calibration",
                status=status,
                reason_code="TARGET_COORDINATE_CALIBRATION",
                detail=str(exc),
            )
            add_blocker(row)

    if not blockers:
        for policy in policies:
            try:
                supplied = sources.snapshots(policy, data_end)
                if not isinstance(supplied, dict) or not supplied:
                    raise DataBlocked(
                        "snapshot source returned no snapshots"
                    )
                snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
                for raw_date, snapshot in supplied.items():
                    try:
                        formation_date = pd.Timestamp(raw_date)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise DataBlocked(
                            f"invalid snapshot formation date: {raw_date!r}"
                        ) from exc
                    if (
                        pd.isna(formation_date)
                        or formation_date.tz is not None
                        or formation_date != formation_date.normalize()
                    ):
                        raise DataBlocked(
                            f"invalid snapshot formation date: {raw_date!r}"
                        )
                    if not isinstance(snapshot, pd.DataFrame):
                        raise DataBlocked(
                            f"snapshot for {formation_date.date()} "
                            "must be a DataFrame"
                        )
                    if formation_date in snapshots:
                        raise DataBlocked(
                            "duplicate normalized snapshot date: "
                            f"{formation_date.date()}"
                        )
                    if formation_date <= data_end:
                        snapshots[formation_date] = snapshot

                policy_required_keys = {
                    date
                    for date in snapshots
                    if required_start <= date <= required_end
                }
                period_counts: dict[pd.Period, int] = {}
                for date in policy_required_keys:
                    period = date.to_period("M")
                    period_counts[period] = (
                        period_counts.get(period, 0) + 1
                    )
                missing_periods = sorted(
                    required_periods.difference(period_counts)
                )
                duplicate_periods = sorted(
                    period
                    for period, count in period_counts.items()
                    if count != 1
                )
                if missing_periods or duplicate_periods:
                    details = []
                    if missing_periods:
                        details.append(
                            "missing="
                            + ",".join(map(str, missing_periods))
                        )
                    if duplicate_periods:
                        details.append(
                            "duplicate="
                            + ",".join(map(str, duplicate_periods))
                        )
                    raise DataBlocked(
                        "required monthly snapshot grid invalid: "
                        + "; ".join(details)
                    )
                normalized_by_policy[policy] = snapshots
                required_keys[policy] = policy_required_keys
            except (DataBlocked, CoverageBlocked) as exc:
                status = (
                    "DATA_BLOCKED"
                    if isinstance(exc, DataBlocked)
                    else "COVERAGE_BLOCKED"
                )
                reason_code = (
                    "DATA_CONTRACT"
                    if status == "DATA_BLOCKED"
                    else "LEGAL_CROSS_SECTION_INFEASIBLE"
                )
                row = add_audit(
                    policy=policy,
                    formation_date=pd.NaT,
                    required=True,
                    check="snapshot_source",
                    status=status,
                    reason_code=reason_code,
                    detail=str(exc),
                )
                add_blocker(row)

    if not blockers and policies:
        reference = required_keys[policies[0]]
        if any(
            required_keys[policy] != reference
            for policy in policies[1:]
        ):
            row = add_audit(
                policy="all",
                formation_date=pd.NaT,
                required=True,
                check="snapshot_alignment",
                status="DATA_BLOCKED",
                reason_code="DATA_CONTRACT",
                detail=(
                    "required formation keys differ across PIT policies"
                ),
            )
            add_blocker(row)

    for policy in policies if not blockers else []:
        snapshots = normalized_by_policy[policy]
        for raw_date, snapshot in sorted(
            snapshots.items(),
            key=lambda item: pd.Timestamp(item[0]),
        ):
            formation_date = pd.Timestamp(raw_date)
            if formation_date > data_end:
                continue
            required = required_start <= formation_date <= required_end
            audit_exclusions(
                policy,
                formation_date,
                required,
                snapshot,
            )
            try:
                result = compute_month_exposures(snapshot, cfg)
            except DataBlocked as exc:
                detail = str(exc)
                row = add_audit(
                    policy=policy,
                    formation_date=formation_date,
                    required=required,
                    check="monthly_exposure",
                    status="DATA_BLOCKED",
                    reason_code="DATA_CONTRACT",
                    detail=detail,
                )
                if required:
                    add_blocker(row)
                continue
            except CoverageBlocked as exc:
                detail = str(exc)
                row = add_audit(
                    policy=policy,
                    formation_date=formation_date,
                    required=required,
                    check="monthly_exposure",
                    status="COVERAGE_BLOCKED",
                    reason_code="LEGAL_CROSS_SECTION_INFEASIBLE",
                    detail=detail,
                )
                if required:
                    add_blocker(row)
                continue

            exposures[policy][formation_date] = result
            for axis in axes:
                frame = result.size if axis == "size" else result.model
                for side in ("plus", "minus"):
                    weights = frame[f"w_{axis}_{side}"]
                    add_audit(
                        policy=policy,
                        formation_date=formation_date,
                        required=required,
                        check=axis,
                        side=side,
                        eligible_count=int(weights.gt(0.0).sum()),
                        max_weight=float(weights.max()),
                    )
            diagnostic_rows.append(
                {
                    "pit_policy": policy,
                    "formation_date": formation_date,
                    "scope": "exposure",
                    **result.diagnostics,
                }
            )

    if not blockers:
        try:
            for policy in policies:
                calibration = calibrate_target_coordinates(
                    exposures[policy],
                    constituents,
                )
                diagnostic_rows.append(
                    {
                        "pit_policy": policy,
                        "formation_date": pd.NaT,
                        "scope": "target_calibration",
                        **calibration,
                    }
                )
        except (DataBlocked, CoverageBlocked) as exc:
            status = (
                "DATA_BLOCKED"
                if isinstance(exc, DataBlocked)
                else "COVERAGE_BLOCKED"
            )
            detail = str(exc)
            row = add_audit(
                policy="all",
                formation_date=pd.NaT,
                required=True,
                check="target_coordinate_calibration",
                status=status,
                reason_code="TARGET_COORDINATE_CALIBRATION",
                detail=detail,
            )
            add_blocker(row)

    statuses = {blocker["status"] for blocker in blockers}
    if "DATA_BLOCKED" in statuses:
        final_status = "DATA_BLOCKED"
    elif "COVERAGE_BLOCKED" in statuses:
        final_status = "COVERAGE_BLOCKED"
    else:
        final_status = "OK"

    audit = pd.DataFrame(audit_rows).reindex(columns=audit_columns)
    diagnostics = pd.DataFrame(diagnostic_rows)
    if diagnostics.empty:
        diagnostics = pd.DataFrame(
            columns=["pit_policy", "formation_date", "scope"]
        )

    audit_path = output_root / "coverage_audit.csv"
    diagnostics_path = output_root / "exposure_diagnostics.csv"
    _write_csv_atomic(audit, audit_path, index=False)
    _write_csv_atomic(diagnostics, diagnostics_path, index=False)
    _write_stage_manifest(
        output_root,
        "preflight",
        cfg,
        data_end,
        [audit_path, diagnostics_path],
        final_status,
        blockers,
    )

    return PreflightOutcome(
        final_status=final_status,
        exposures=exposures,
        audit=audit,
        diagnostics=diagnostics,
    )


def flatten_exposures(
    exposures: dict[str, dict[pd.Timestamp, ExposureResult]],
) -> pd.DataFrame:
    model_columns = [
        "s_perp",
        "h_perp",
        "x_qblend",
        "x_q500",
        "x_q1000",
        "w_style_plus",
        "w_style_minus",
        "w_interaction_plus",
        "w_interaction_minus",
        "w_qblend_plus",
        "w_qblend_minus",
        "w_q500_plus",
        "w_q500_minus",
        "w_q1000_plus",
        "w_q1000_minus",
    ]
    rows: list[pd.DataFrame] = []
    for policy in sorted(exposures):
        months = exposures[policy]
        for formation_date, result in sorted(
            months.items(),
            key=lambda item: pd.Timestamp(item[0]),
        ):
            missing = sorted(
                set(model_columns).difference(result.model.columns)
            )
            if missing:
                raise DataBlocked(
                    "model exposure columns are missing: "
                    + ", ".join(missing)
                )

            frame = result.size.copy()
            frame["universe_role"] = np.where(
                frame.index.isin(result.model.index),
                "model",
                "size_only",
            )
            for column in model_columns:
                frame[column] = result.model[column].reindex(frame.index)
            frame.insert(0, "pit_policy", policy)
            frame["formation_date"] = pd.Timestamp(formation_date)
            frame = frame.reset_index(drop=True)
            rows.append(
                frame.sort_values(
                    "ticker",
                    kind="mergesort",
                ).reset_index(drop=True)
            )

    if not rows:
        raise DataBlocked("no monthly exposures to flatten")
    return (
        pd.concat(rows, ignore_index=True)
        .sort_values(
            ["pit_policy", "formation_date", "ticker"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )


def run_exposures_stage(
    cfg: dict,
    data_end: pd.Timestamp,
    output_dir: str | Path,
    outcome: PreflightOutcome,
) -> Path:
    output_root = Path(output_dir)
    _invalidate_stage_manifest(output_root, "exposures")
    require_parent_manifest(
        output_root,
        "preflight",
        cfg,
        data_end,
    )
    if outcome.final_status != "OK":
        raise DataBlocked(
            f"preflight outcome is not OK: {outcome.final_status}"
        )

    frame = flatten_exposures(outcome.exposures)
    path = output_root / "monthly_exposures.csv.gz"
    _write_csv_atomic(
        frame,
        path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
    )
    _write_stage_manifest(
        output_root,
        "exposures",
        cfg,
        pd.Timestamp(data_end),
        [path],
        "OK",
        [],
    )
    return path


def run_post_preflight_stages(
    stage: str,
    cfg: dict,
    sources: B3Sources,
    data_end: pd.Timestamp,
    output_dir: str | Path,
    outcome: PreflightOutcome,
) -> int:
    del sources
    if stage != "exposures":
        raise DataBlocked(f"unsupported post-preflight stage: {stage}")
    run_exposures_stage(
        cfg,
        data_end,
        output_dir,
        outcome,
    )
    return 0


def _parse_cli_date(value: str) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date: {value!r}"
        ) from exc
    if (
        pd.isna(parsed)
        or parsed.tz is not None
        or parsed != parsed.normalize()
    ):
        raise argparse.ArgumentTypeError(f"invalid date: {value!r}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="B3 staged research builder"
    )
    parser.add_argument(
        "--stage",
        choices=["preflight", "exposures"],
        default="exposures",
    )
    parser.add_argument(
        "--data-end",
        type=_parse_cli_date,
        default="2026-12-31",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    args = parser.parse_args()

    cfg = load_b3_config()
    sources = default_sources(load_db_config())
    data_end = args.data_end
    outcome = run_preflight(
        cfg,
        sources,
        data_end,
        args.output_dir,
    )
    if outcome.final_status == "DATA_BLOCKED":
        return 2
    if outcome.final_status == "COVERAGE_BLOCKED":
        return 3
    if args.stage == "preflight":
        return 0
    return run_post_preflight_stages(
        args.stage,
        cfg,
        sources,
        data_end,
        args.output_dir,
        outcome,
    )


if __name__ == "__main__":
    raise SystemExit(main())
