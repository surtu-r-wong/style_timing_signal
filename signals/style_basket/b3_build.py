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


def apply_pit_policy(raw: pd.DataFrame, policy: str) -> pd.DataFrame:
    """Apply one of B3's two disclosure-date policies without losing provenance."""
    if policy not in {POLICY_MAIN, POLICY_LAG}:
        raise ValueError(
            f"unsupported PIT policy {policy!r}; expected "
            f"{POLICY_MAIN!r} or {POLICY_LAG!r}"
        )

    out = raw.copy()
    out["end_date"] = pd.to_datetime(out["end_date"])
    out["stored_ann_date"] = pd.to_datetime(out["stored_ann_date"])

    unknown_source = ~out["data_source"].isin({"csmar", "wind"})
    if unknown_source.any():
        values = sorted(
            {repr(value) for value in out.loc[unknown_source, "data_source"]}
        )
        raise DataBlocked(
            "unknown financial data_source: " + ", ".join(values)
        )

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

    facts["end_date"] = pd.to_datetime(facts["end_date"])
    facts["stored_ann_date"] = pd.to_datetime(facts["stored_ann_date"])
    facts["data"] = [
        translate_data(data, source, statement)
        for data, source, statement in zip(
            facts["data"],
            facts["data_source"],
            facts["statement_type"],
        )
    ]
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
    missing = sorted(required.difference(pool.columns))
    if missing:
        raise DataBlocked(
            "industry pool is missing columns: " + ", ".join(missing)
        )

    history = pool.copy()
    history["ticker"] = history["ticker"].astype(str)
    history["effective_date"] = pd.to_datetime(
        history["effective_date"]
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
    missing_meta = sorted(required_meta.difference(stock_meta.columns))
    if missing_meta:
        raise DataBlocked(
            "stock metadata is missing columns: " + ", ".join(missing_meta)
        )
    meta = stock_meta.copy()
    meta["ticker"] = meta["ticker"].astype(str)
    meta["list_date"] = pd.to_datetime(meta["list_date"])
    meta["delist_date"] = pd.to_datetime(meta["delist_date"])
    meta = (
        meta.sort_values("ticker", kind="mergesort")
        .drop_duplicates("ticker", keep="last")
        .set_index("ticker")
        .sort_index()
    )

    close_matrix = closes.copy()
    close_matrix.index = pd.to_datetime(close_matrix.index)

    share_columns = {
        "ts_code",
        "end_date",
        "known_date",
        "total_shares",
    }
    if shares_pool.empty:
        share_history = pd.DataFrame(columns=sorted(share_columns))
    else:
        missing_shares = sorted(share_columns.difference(shares_pool.columns))
        if missing_shares:
            raise DataBlocked(
                "shares pool is missing columns: "
                + ", ".join(missing_shares)
            )
        share_history = shares_pool.copy()
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
