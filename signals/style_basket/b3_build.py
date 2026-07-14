"""B3 point-in-time policy handling and monthly style snapshot assembly."""

from __future__ import annotations

import argparse
import hashlib
import json
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
