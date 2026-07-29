"""Pure DataFrame rules for B3 missing-close suspension evidence."""

import math

from numbers import Number, Real

import pandas as pd


MIN_LISTED_DAYS = 180
EXCLUDED_MARKET_SUFFIXES = (".BJ", ".HK")
CANDIDATE_COLUMNS = ("ts_code", "formation_date", "list_date", "delist_date")


class SuspensionEvidenceError(RuntimeError):
    """Raised when B3 suspension evidence has an invalid structure."""


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise SuspensionEvidenceError(f"{label}: missing required columns {missing}")


def _validate_tickers(frame: pd.DataFrame, label: str) -> None:
    if frame.empty:
        return
    invalid = frame["ts_code"].map(
        lambda value: not isinstance(value, str) or not value.strip() or value != value.strip()
    )
    if invalid.any():
        raise SuspensionEvidenceError(f"{label}: ts_code keys must be non-blank strings")


def _parse_dates(
    frame: pd.DataFrame, columns: tuple[str, ...], label: str, nullable: tuple[str, ...] = ()
) -> pd.DataFrame:
    parsed = frame.copy()
    for column in columns:
        values = []
        for value in parsed[column]:
            try:
                missing = bool(pd.isna(value))
            except (TypeError, ValueError) as exc:
                raise SuspensionEvidenceError(f"{label}: invalid {column}") from exc
            if missing:
                values.append(pd.NaT)
                continue
            if isinstance(value, (bool, Number)):
                raise SuspensionEvidenceError(f"{label}: invalid {column}")
            try:
                timestamp = pd.Timestamp(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SuspensionEvidenceError(f"{label}: invalid {column}") from exc
            if pd.isna(timestamp) or timestamp.tzinfo is not None or timestamp != timestamp.normalize():
                raise SuspensionEvidenceError(f"{label}: invalid {column}")
            values.append(timestamp)
        try:
            parsed[column] = pd.Series(values, index=parsed.index, dtype="datetime64[ns]")
        except (TypeError, ValueError, OverflowError) as exc:
            raise SuspensionEvidenceError(f"{label}: invalid {column}") from exc
        if column not in nullable and parsed[column].isna().any():
            raise SuspensionEvidenceError(f"{label}: {column} must not be null")
    return parsed


def _normalise(frame: pd.DataFrame, *, label: str, date_columns: tuple[str, ...], nullable_dates: tuple[str, ...] = ()) -> pd.DataFrame:
    _validate_tickers(frame, label)
    result = _parse_dates(frame.drop_duplicates(), date_columns, label, nullable_dates)
    return result.reset_index(drop=True)


def _deduplicate_keys(frame: pd.DataFrame, keys: list[str], label: str) -> pd.DataFrame:
    duplicates = frame.duplicated(keys, keep=False)
    if duplicates.any():
        raise SuspensionEvidenceError(f"conflicting duplicate logical keys in {label}")
    return frame


def build_missing_close_candidates(*, formations, stock_meta, exact_closes, exact_suspensions, exact_carries) -> pd.DataFrame:
    """Return mature non-BJ/HK formation ticker-months with missing exact closes."""
    _require_columns(formations, ("formation_date",), "formations")
    _require_columns(stock_meta, ("ts_code", "list_date", "delist_date"), "stock meta")
    _require_columns(exact_closes, ("ts_code", "formation_date", "close"), "exact closes")
    _require_columns(exact_suspensions, ("ts_code", "formation_date"), "exact suspensions")
    _require_columns(exact_carries, ("ts_code", "formation_date", "close_date", "close"), "exact carries")

    formations = _deduplicate_keys(
        _parse_dates(formations.drop_duplicates(), ("formation_date",), "formations"),
        ["formation_date"], "formations",
    )[["formation_date"]]
    stock_meta = _normalise(stock_meta, label="stock meta", date_columns=("list_date", "delist_date"), nullable_dates=("list_date", "delist_date"))
    exact_closes = _deduplicate_keys(
        _normalise(exact_closes, label="exact closes", date_columns=("formation_date",)),
        ["ts_code", "formation_date"], "exact closes",
    )
    exact_closes = exact_closes[["ts_code", "formation_date", "close"]]
    exact_closes["close"] = pd.to_numeric(exact_closes["close"], errors="coerce")
    exact_suspensions = _deduplicate_keys(
        _normalise(exact_suspensions, label="exact suspensions", date_columns=("formation_date",)),
        ["ts_code", "formation_date"], "exact suspensions",
    )
    exact_carries = _deduplicate_keys(
        _normalise(
            exact_carries, label="exact carries", date_columns=("formation_date", "close_date"),
            nullable_dates=("close_date",),
        ),
        ["ts_code", "formation_date"], "exact carries",
    )
    exact_suspensions = exact_suspensions[["ts_code", "formation_date"]]
    exact_carries = exact_carries[["ts_code", "formation_date", "close_date", "close"]]
    exact_carries["close"] = pd.to_numeric(exact_carries["close"], errors="coerce")
    stock_meta = _deduplicate_keys(stock_meta, ["ts_code"], "stock meta")[
        ["ts_code", "list_date", "delist_date"]
    ]

    universe = formations.merge(stock_meta, how="cross")
    in_scope = ~universe["ts_code"].str.endswith(EXCLUDED_MARKET_SUFFIXES, na=False)
    active = universe["list_date"].notna() & (universe["list_date"] <= universe["formation_date"])
    active &= universe["delist_date"].isna() | (universe["formation_date"] <= universe["delist_date"])
    mature = universe["list_date"] + pd.Timedelta(days=MIN_LISTED_DAYS) <= universe["formation_date"]
    universe = universe.loc[in_scope & active & mature]

    close_data = exact_closes[["ts_code", "formation_date", "close"]]
    candidates = universe.merge(close_data, on=["ts_code", "formation_date"], how="left")
    candidates = candidates.loc[candidates["close"].isna()].drop(columns="close")

    usable_carries = exact_carries.merge(exact_suspensions, on=["ts_code", "formation_date"], how="inner")
    usable_carries = usable_carries.loc[usable_carries["close"].notna(), ["ts_code", "formation_date"]]
    candidates = candidates.merge(usable_carries.assign(_usable=True), on=["ts_code", "formation_date"], how="left")
    candidates = candidates.loc[candidates["_usable"].isna(), CANDIDATE_COLUMNS]
    return candidates.sort_values(["formation_date", "ts_code"], kind="stable").reset_index(drop=True)


CORE_EVIDENCE_COLUMNS = (
    "ts_code", "formation_date", "list_date", "delist_date", "suspension_start", "previous_official_trade_date", "previous_close_date", "previous_close", "suspend_type", "suspend_reason", "evidence_method", "accepted", "rejection_reason", "next_trade_date", "next_nonnull_close", "exact_stock_status_confirmed",
)
SUSPENSION_INTERVAL_ARTIFACT_COLUMNS = (
    "ts_code",
    "formation_date",
    "required_formation",
    *CORE_EVIDENCE_COLUMNS[2:],
)
INTERVAL_METHOD = "CONTINUOUS_SUSPENSION_INTERVAL"
INTERVAL_REJECTION_REASONS = frozenset(
    {
        "NO_EXPLICIT_SUSPENSION_START",
        "START_NOT_OFFICIAL_TRADING_DAY",
        "PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY",
        "INVALID_PREVIOUS_CLOSE",
        "PRICE_OBSERVED_DURING_INTERVAL",
        "OUTSIDE_LEGAL_LISTING_INTERVAL",
        "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE",
    }
)


def empty_interval_evidence() -> pd.DataFrame:
    return pd.DataFrame(columns=CORE_EVIDENCE_COLUMNS)


def _normalise_interval_frame(frame, *, label, date_columns, keys, nullable_dates=(), tickers=True):
    raw = frame.drop_duplicates()
    if tickers:
        _validate_tickers(raw, label)
    return _deduplicate_keys(_parse_dates(raw, date_columns, label, nullable_dates), keys, label).reset_index(drop=True)


def _normalise_bool_column(frame, *, column, label):
    if frame[column].map(lambda value: not isinstance(value, bool)).any():
        raise SuspensionEvidenceError(f"{label}: {column} keys must be boolean")
    return frame


def _strict_source_start(value):
    return _parse_dates(pd.DataFrame({"suspension_source_start": [value]}), ("suspension_source_start",), "suspension source start").iloc[0, 0]


def _normalise_price_closes(frame):
    values = []
    for value in frame["close"]:
        try:
            missing = bool(pd.isna(value))
        except (TypeError, ValueError) as exc:
            raise SuspensionEvidenceError("prices: close values must be real numeric scalars") from exc
        if missing:
            values.append(float("nan"))
        elif isinstance(value, bool) or not isinstance(value, Real):
            raise SuspensionEvidenceError("prices: close values must be real numeric scalars")
        else:
            values.append(value)
    result = frame.copy()
    result["close"] = values
    return result


def _report_text(value):
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _last_nonnull_price(prices, ts_code, date, *, strict):
    comparison = prices["trade_date"] < date if strict else prices["trade_date"] <= date
    matches = prices.loc[(prices["ts_code"] == ts_code) & comparison & prices["close"].notna()]
    return None if matches.empty else matches.sort_values("trade_date", kind="stable").iloc[-1]


def _valid_close(value):
    return value is not None and not pd.isna(value) and math.isfinite(float(value)) and float(value) > 0


def _interval_row(candidate):
    row = {column: pd.NA for column in CORE_EVIDENCE_COLUMNS}
    row.update({
        "ts_code": candidate["ts_code"], "formation_date": candidate["formation_date"], "list_date": candidate["list_date"], "delist_date": candidate["delist_date"],
        "suspend_type": "", "suspend_reason": "", "evidence_method": "", "accepted": False, "rejection_reason": "", "exact_stock_status_confirmed": pd.NA,
    })
    return row


def build_continuous_suspension_evidence(*, candidates, trading_calendar, prices, suspension_events, suspension_source_start, stock_status=None):
    """Classify candidate missing closes using only formation-date evidence."""
    _require_columns(candidates, CANDIDATE_COLUMNS, "candidates")
    _require_columns(trading_calendar, ("calendar_date", "sfe"), "trading calendar")
    _require_columns(prices, ("ts_code", "trade_date", "close"), "prices")
    _require_columns(suspension_events, ("ts_code", "trade_date", "suspend_type", "suspend_reason"), "suspension events")
    if stock_status is not None:
        _require_columns(stock_status, ("ts_code", "trade_date", "is_suspended"), "stock status")
    source_start = _strict_source_start(suspension_source_start)
    candidates = _normalise_interval_frame(candidates, label="candidates", date_columns=("formation_date", "list_date", "delist_date"), nullable_dates=("delist_date",), keys=["ts_code", "formation_date"])
    calendar = _normalise_bool_column(trading_calendar, column="sfe", label="trading calendar")
    calendar = _normalise_interval_frame(calendar, label="trading calendar", date_columns=("calendar_date",), keys=["calendar_date"], tickers=False)
    prices = _normalise_price_closes(prices)
    prices = _normalise_interval_frame(prices, label="prices", date_columns=("trade_date",), keys=["ts_code", "trade_date"])
    events = _normalise_interval_frame(suspension_events, label="suspension events", date_columns=("trade_date",), keys=["ts_code", "trade_date"])
    if stock_status is None:
        status = pd.DataFrame(columns=["ts_code", "trade_date", "is_suspended"])
    else:
        status = _normalise_bool_column(stock_status, column="is_suspended", label="stock status")
        status = _normalise_interval_frame(status, label="stock status", date_columns=("trade_date",), keys=["ts_code", "trade_date"])
    if candidates.empty:
        return empty_interval_evidence()

    official_dates = calendar.loc[calendar["sfe"], "calendar_date"].sort_values(kind="stable")
    official_set = set(official_dates)
    rows = []
    for _, candidate in candidates.sort_values(["formation_date", "ts_code"], kind="stable").iterrows():
        row = _interval_row(candidate)
        ts_code, formation = candidate["ts_code"], candidate["formation_date"]
        legal = candidate["list_date"] <= formation and (pd.isna(candidate["delist_date"]) or formation <= candidate["delist_date"])
        if not legal:
            row["rejection_reason"] = "OUTSIDE_LEGAL_LISTING_INTERVAL"
        else:
            starts = events.loc[(events["ts_code"] == ts_code) & (events["trade_date"] <= formation) & (events["suspend_type"] == "今起停牌")].sort_values("trade_date", kind="stable")
            if starts.empty:
                previous = _last_nonnull_price(prices, ts_code, formation, strict=False)
                if previous is None or not _valid_close(previous["close"]):
                    row["rejection_reason"] = "INVALID_PREVIOUS_CLOSE"
                else:
                    row["previous_close_date"], row["previous_close"] = previous["trade_date"], float(previous["close"])
                    expected = official_dates.loc[official_dates > previous["trade_date"]]
                    row["rejection_reason"] = "SUSPENSION_START_PRECEDES_SOURCE_COVERAGE" if not expected.empty and expected.iloc[0] < source_start else "NO_EXPLICIT_SUSPENSION_START"
            else:
                selected = starts.iloc[-1]
                start = selected["trade_date"]
                row["suspension_start"], row["suspend_type"], row["suspend_reason"] = start, _report_text(selected["suspend_type"]), _report_text(selected["suspend_reason"])
                previous = _last_nonnull_price(prices, ts_code, start, strict=True)
                if previous is not None:
                    row["previous_close_date"], row["previous_close"] = previous["trade_date"], float(previous["close"])
                prior_price_date = None if previous is None else previous["trade_date"]
                unended = starts if prior_price_date is None else starts.loc[starts["trade_date"] > prior_price_date]
                if len(unended) > 1:
                    raise SuspensionEvidenceError("overlapping unended suspension starts")
                prior_official = official_dates.loc[official_dates < start]
                if not prior_official.empty:
                    row["previous_official_trade_date"] = prior_official.iloc[-1]
                if start not in official_set:
                    row["rejection_reason"] = "START_NOT_OFFICIAL_TRADING_DAY"
                elif previous is None or not _valid_close(previous["close"]):
                    row["rejection_reason"] = "INVALID_PREVIOUS_CLOSE"
                elif prior_official.empty or previous["trade_date"] != prior_official.iloc[-1]:
                    row["rejection_reason"] = "PREVIOUS_CLOSE_NOT_PRIOR_TRADING_DAY"
                elif not prices.loc[(prices["ts_code"] == ts_code) & (prices["trade_date"] >= start) & (prices["trade_date"] <= formation) & prices["close"].notna()].empty:
                    row["rejection_reason"] = "PRICE_OBSERVED_DURING_INTERVAL"
                else:
                    row["accepted"], row["evidence_method"] = True, INTERVAL_METHOD
        future = prices.loc[(prices["ts_code"] == ts_code) & (prices["trade_date"] > formation) & prices["close"].notna()].sort_values("trade_date", kind="stable")
        if not future.empty:
            row["next_trade_date"], row["next_nonnull_close"] = future.iloc[0]["trade_date"], float(future.iloc[0]["close"])
        exact_status = status.loc[(status["ts_code"] == ts_code) & (status["trade_date"] == formation)]
        if not exact_status.empty:
            row["exact_stock_status_confirmed"] = bool(exact_status.iloc[0]["is_suspended"])
        rows.append(row)
    result = pd.DataFrame(rows, columns=CORE_EVIDENCE_COLUMNS)
    result["accepted"] = result["accepted"].astype(object)
    result["exact_stock_status_confirmed"] = result["exact_stock_status_confirmed"].astype(object)
    return result.sort_values(["formation_date", "ts_code"], kind="stable").reset_index(drop=True)
