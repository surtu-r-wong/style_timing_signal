"""Pure DataFrame rules for B3 missing-close suspension evidence."""

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
    invalid = frame["ts_code"].map(lambda value: not isinstance(value, str) or not value.strip())
    if invalid.any():
        raise SuspensionEvidenceError(f"{label}: ts_code keys must be non-blank strings")


def _parse_dates(
    frame: pd.DataFrame, columns: tuple[str, ...], label: str, nullable: tuple[str, ...] = ()
) -> pd.DataFrame:
    parsed = frame.copy()
    for column in columns:
        try:
            parsed[column] = pd.to_datetime(parsed[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise SuspensionEvidenceError(f"{label}: invalid {column}") from exc
        if column not in nullable and parsed[column].isna().any():
            raise SuspensionEvidenceError(f"{label}: {column} must not be null")
    return parsed


def _normalise(frame: pd.DataFrame, *, label: str, date_columns: tuple[str, ...], nullable_dates: tuple[str, ...] = (), close: bool = False) -> pd.DataFrame:
    _validate_tickers(frame, label)
    result = _parse_dates(frame, date_columns, label, nullable_dates)
    if close:
        result["close"] = pd.to_numeric(result["close"], errors="coerce")
    return result.drop_duplicates().reset_index(drop=True)


def _deduplicate_keys(frame: pd.DataFrame, keys: list[str], label: str) -> pd.DataFrame:
    duplicates = frame.duplicated(keys, keep=False)
    if duplicates.any():
        raise SuspensionEvidenceError(f"conflicting duplicate logical keys in {label}")
    return frame


def build_missing_close_candidates(*, formations, stock_meta, exact_closes, exact_suspensions, exact_carries) -> pd.DataFrame:
    """Return mature SH/SZ formation ticker-months with missing, unusable exact closes."""
    _require_columns(formations, ("formation_date",), "formations")
    _require_columns(stock_meta, ("ts_code", "list_date", "delist_date"), "stock meta")
    _require_columns(exact_closes, ("ts_code", "formation_date", "close"), "exact closes")
    _require_columns(exact_suspensions, ("ts_code", "formation_date"), "exact suspensions")
    _require_columns(exact_carries, ("ts_code", "formation_date", "close_date", "close"), "exact carries")

    formations = _parse_dates(formations, ("formation_date",), "formations").drop_duplicates()
    stock_meta = _normalise(stock_meta, label="stock meta", date_columns=("list_date", "delist_date"), nullable_dates=("list_date", "delist_date"))
    exact_closes = _deduplicate_keys(
        _normalise(exact_closes, label="exact closes", date_columns=("formation_date",), close=True),
        ["ts_code", "formation_date"], "exact closes",
    )
    exact_suspensions = _deduplicate_keys(
        _normalise(exact_suspensions, label="exact suspensions", date_columns=("formation_date",)),
        ["ts_code", "formation_date"], "exact suspensions",
    )
    exact_carries = _deduplicate_keys(
        _normalise(exact_carries, label="exact carries", date_columns=("formation_date", "close_date"), close=True),
        ["ts_code", "formation_date"], "exact carries",
    )
    stock_meta = _deduplicate_keys(stock_meta, ["ts_code"], "stock meta")

    universe = formations.merge(stock_meta, how="cross")
    in_scope = universe["ts_code"].str.endswith((".SH", ".SZ"), na=False)
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
