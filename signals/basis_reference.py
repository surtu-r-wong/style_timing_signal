"""Daily IC/IM term structure observations for the evening report."""

import argparse
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd
from psycopg2 import sql

from backtest import basis_term
from backtest.data import _connect
from backtest.leverage_probe import level_signal
from backtest.rotation_probe import hold_position
from signals.common.config import CONFIG_FILE, ROOT, load_db_config

REFERENCE_PATH = Path("output/reference/futures_basis_daily.csv")
EW_PATH = Path("output/equal_weight/equal_weight_signal_20d40z.csv")
IC_START = "2015-04-16"
IM_START = "2022-07-22"
PARAMETERS = {"T1": (5, 60), "T2": (5, 60), "T3": (20, 250)}


class IncompleteData(ValueError):
    """A required dated market input is missing or inconsistent."""


def read_market(config, end):
    with closing(_connect(config)) as connection:
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with connection.cursor() as cursor:
            bounds = (IC_START, end.date())
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute(
                "SELECT calendar_date FROM data_manager.business_calendar "
                "WHERE calendar_id='CN' AND is_business_day AND calendar_date BETWEEN %s AND %s "
                "ORDER BY calendar_date",
                bounds,
            )
            dates = pd.DatetimeIndex([day for (day,) in cursor.fetchall()], name="date")
            cursor.execute(
                sql.SQL(
                    "SELECT trade_date, index_code, close FROM {}.index_daily "
                    "WHERE index_code = ANY(%s) AND trade_date BETWEEN %s AND %s "
                    "ORDER BY trade_date, index_code"
                ).format(sql.Identifier(config["schema"])),
                (list(basis_term.SPOT.values()), *bounds),
            )
            spot = pd.DataFrame(
                cursor.fetchall(), columns=["trade_date", "index_code", "close"]
            )
            cursor.execute(
                "SELECT trade_date, symbol, close FROM public.futures_daily "
                "WHERE symbol ~ '^(IC|IM)[0-9]{4}[.]CFE$' AND trade_date BETWEEN %s AND %s "
                "ORDER BY trade_date, symbol",
                bounds,
            )
            futures = pd.DataFrame(
                cursor.fetchall(), columns=["trade_date", "symbol", "close"]
            )
    return dates, spot, futures


def require_dates(actual, expected, label):
    if not actual.equals(expected):
        difference = actual.symmetric_difference(expected)
        at = difference[0].date() if len(difference) else "重复或乱序"
        raise IncompleteData(f"{label} 交易日不匹配（{at}）")


def basis_history(ew, dates, spot, futures):
    """Align the complete exchange calendar before transforming or sampling any series."""
    if not len(dates) or dates[0] != pd.Timestamp(IC_START):
        raise IncompleteData("缺少 IC 首个观察日")
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise IncompleteData("交易日历重复或乱序")
    require_dates(ew.index, dates, "标准 EW 与交易所")
    if not np.isfinite(ew).all():
        raise IncompleteData("标准 EW 得分缺失")
    if (
        spot.duplicated(["trade_date", "index_code"]).any()
        or futures.duplicated(["trade_date", "symbol"]).any()
    ):
        raise IncompleteData("指数或合约日期重复")

    legs = {}
    for size, prefix in basis_term.FUT.items():
        available = dates if prefix == "IC" else dates[dates >= IM_START]
        if not len(available):
            continue
        index_code = basis_term.SPOT[size]
        closes = (
            spot.loc[spot.index_code.eq(index_code)]
            .set_index("trade_date")
            .close.astype(float)
        )
        closes.index = pd.DatetimeIndex(closes.index)
        closes = closes.reindex(available)
        invalid = ~np.isfinite(closes) | closes.le(0)
        if invalid.any():
            raise IncompleteData(
                f"{index_code} {closes.index[invalid][0].date()} 缺少有效现货收盘"
            )
        quotes = futures.loc[futures.symbol.str.startswith(prefix)].copy()
        quotes["close"] = quotes.close.astype(float)
        quotes["trade_date"] = pd.to_datetime(quotes.trade_date).dt.date
        counts = (
            quotes.loc[np.isfinite(quotes.close) & quotes.close.gt(0)]
            .groupby("trade_date")
            .size()
        )
        counts.index = pd.DatetimeIndex(counts.index)
        counts = counts.reindex(available, fill_value=0)
        # Each listed leg publishes four contract months before the research maturity filter.
        if not counts.eq(4).all():
            day = counts.index[counts.ne(4)][0]
            raise IncompleteData(f"{prefix} {day.date()} 有效月份 {counts.at[day]}/4")
        leg = basis_term.build_index_series(quotes, closes)
        require_dates(leg.index, available, prefix)
        if leg.slope.isna().any() or leg.far_near.isna().any():
            raise IncompleteData(f"{prefix} 缺少足够的合格期限")
        legs[size] = leg
    raw = basis_term.combine(legs)
    require_dates(raw.index, dates, "期限结构")
    out = raw[["T1", "T2", "T3"]].add_suffix("_raw")
    out["IC_contracts_used"] = raw["n_used_500"]
    out["IM_contracts_used"] = raw.get("n_used_1000", np.nan)
    out["ew_score"] = ew
    out["ew_direction"] = np.sign(ew).astype(int)
    for name, (lookback, z_window) in PARAMETERS.items():
        native = raw[name].dropna()
        expected = dates[20:] if name == "T3" else dates
        require_dates(native.index, expected, name)
        score = level_signal(native, lookback, z_window)
        direction = hold_position(score, 40).reindex(dates, fill_value=0).astype(int)
        samples = score.index[::40]
        out[name + "_score"] = score
        out[name + "_direction"] = direction
        out[name + "_sample_date"] = pd.Series(samples, index=samples).reindex(
            dates, method="ffill"
        )
    return out


def generate(ew_file, output, config_file):
    ew = pd.read_csv(
        ew_file, usecols=["date", "factor_value"], index_col="date", parse_dates=True
    ).factor_value
    ew = ew.loc[ew.index >= IC_START]
    if ew.empty:
        raise IncompleteData("缺少标准 EW 或 IC 历史")
    data = basis_history(ew, *read_market(load_db_config(config_file), ew.index[-1]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        temp = Path(stream.name)
    try:
        data.to_csv(
            temp, index_label="date", float_format="%.15g", date_format="%Y-%m-%d"
        )
        temp.chmod(0o644)
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    print(f"期货基差参考 {data.index[-1].date()}，共 {len(data)} 个交易日")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ew-file", type=Path, default=ROOT / EW_PATH)
    parser.add_argument("--output", type=Path, default=ROOT / REFERENCE_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    args = parser.parse_args()
    try:
        generate(args.ew_file, args.output, args.config)
    except IncompleteData as exc:
        print(f"期货基差参考待补齐：{exc}")
        return 1
    except Exception as exc:
        # Connection errors can include configuration data; log only their type.
        print(f"期货基差参考未更新（{type(exc).__name__}）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
