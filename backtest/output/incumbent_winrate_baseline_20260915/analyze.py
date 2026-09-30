"""Descriptive incumbent baseline from verified frozen event studies."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
INPUTS = {"long": PARENT / "entry_day_direction_20260915", "short": PARENT / "short_entry_day_direction_20260915"}
END = pd.Timestamp("2026-09-11")


def max_loss_streak(values):
    best = run = 0
    for value in values:
        run = run + 1 if value < 0 else 0
        best = max(best, run)
    return best


def metrics(f, col="gross"):
    f = f.sort_values("date").dropna(subset=[col])
    x = f[col]
    wins, losses = x[x > 0], x[x < 0]
    return dict(n=len(f), wins=len(wins), losses=len(losses), flat=int((x == 0).sum()),
                win_rate=(x > 0).mean(), mean_win=wins.mean(), mean_loss=losses.mean(),
                payoff_ratio=wins.mean() / -losses.mean() if len(wins) and len(losses) else np.nan,
                profit_factor=wins.sum() / -losses.sum() if len(losses) else np.nan,
                mean_return=x.mean(), median_return=x.median(),
                avg_holding_days=f.days.mean(), winner_days=f.loc[x > 0, "days"].mean(),
                loser_days=f.loc[x < 0, "days"].mean(), max_loss_streak=max_loss_streak(x),
                loser_had_1pct_gain=int(((x < 0) & (f.mfe >= .01)).sum()),
                loser_had_1pct_gain_rate=((f.loc[x < 0, "mfe"] >= .01).mean()),
                top5_profit_share=wins.nlargest(5).sum() / wins.sum() if len(wins) else np.nan)


def main():
    hashes, all_events, trades = {}, [], []
    for side, path in INPUTS.items():
        meta = json.loads((path / "metadata.json").read_text())
        for name, expected in meta["input_sha256"].items():
            assert hashlib.sha256((path / name).read_bytes()).hexdigest() == expected
        src = path / "events.csv"
        hashes[str(src.relative_to(PARENT))] = hashlib.sha256(src.read_bytes()).hexdigest()
        e = pd.read_csv(src, parse_dates=["date", "end"])
        e["side"] = side
        all_events.append(e)
    events = pd.concat(all_events, ignore_index=True)
    source = INPUTS["long"] / "spot_close.csv"
    close = pd.read_csv(source, parse_dates=["date"]).set_index("date").loc[:END]
    ret = close.pct_change(fill_method=None)
    ret["blend"] = (ret["500"] + ret["1000"]) / 2
    nav = (1 + ret.dropna()).cumprod()
    for row in events[events.horizon == "trade"].itertuples():
        record = dict(signal=row.signal, side=row.side, underlying=row.underlying,
                      date=row.date, end=row.end, t_direction=row.direction, gross=row.ret)
        if pd.notna(row.end):
            sign = 1 if row.side == "long" else -1
            path = nav.loc[row.date:row.end, row.underlying] / nav.at[row.date, row.underlying]
            pnl = sign * (path - 1)
            assert np.isclose(pnl.iloc[-1], row.ret, atol=1e-12)
            record.update(days=len(path) - 1, mfe=pnl.max(), mae=pnl.min(),
                          net_3bps=row.ret - .0003 * (1 + path.iloc[-1]),
                          daily_win_rate=(sign * ret.loc[(ret.index > row.date) & (ret.index <= row.end), row.underlying] > 0).mean())
            # Delay BOTH entry and exit to next trading close; never only entry.
            i, j = nav.index.get_loc(row.date) + 1, nav.index.get_loc(row.end) + 1
            if j < len(nav):
                ratio = nav.iloc[j][row.underlying] / nav.iloc[i][row.underlying]
                record.update(delayed_entry=nav.index[i], delayed_exit=nav.index[j],
                              delayed_gross=sign * (ratio - 1), delayed_net_3bps=sign * (ratio - 1) - .0003 * (1 + ratio))
        trades.append(record)
    ledger = pd.DataFrame(trades)
    ledger.to_csv(HERE / "trades.csv", index=False)
    windows = {"full": ("2014-01-02", "2026-09-11"), "2014-2020": ("2014-01-02", "2020-12-31"),
               "2021-2023": ("2021-01-01", "2023-12-31"), "2024-2026": ("2024-01-01", "2026-09-11")}
    rows = []
    for (signal, underlying), f in ledger.groupby(["signal", "underlying"]):
        for side in [*f.side.unique(), "combined"] if f.side.nunique() > 1 else list(f.side.unique()):
            g = f if side == "combined" else f[f.side == side]
            for period, (start, end) in windows.items():
                selected = g[(g.date >= start) & (g.date <= end)]
                for basis in ["gross", "net_3bps", "delayed_gross", "delayed_net_3bps"]:
                    rows.append(dict(signal=signal, side=side, underlying=underlying, period=period, basis=basis,
                                     total_entries=len(selected), open_or_unmatured=int(selected[basis].isna().sum()),
                                     **metrics(selected, basis)))
    summary = pd.DataFrame(rows)
    summary.to_csv(HERE / "summary.csv", index=False)
    fixed = []
    for (signal, side, underlying, horizon), f in events.groupby(["signal", "side", "underlying", "horizon"]):
        x = f.ret.dropna()
        fixed.append(dict(signal=signal, side=side, underlying=underlying, horizon=horizon, n=len(x),
                          wins=int((x > 0).sum()), win_rate=(x > 0).mean(), mean_return=x.mean()))
    pd.DataFrame(fixed).to_csv(HERE / "horizon_summary.csv", index=False)
    hashes[str(source.relative_to(PARENT))] = hashlib.sha256(source.read_bytes()).hexdigest()
    (HERE / "metadata.json").write_text(json.dumps(dict(
        input_sha256=hashes, window=windows["full"], primary="gross fixed-unit blend proxy completed-trade win rate",
        costs="3bps of entry and exit notionals; no carry or actual contracts",
        delayed="both entry and exit delayed one trading close; incomplete endpoints excluded",
        payoff_ratio="mean winning return / abs(mean losing return)",
        profit_factor="sum positive returns / abs(sum negative returns), equal starting notional per trade",
        periods="grouped by entry date; exits can lie beyond period boundary",
        excursion="close-only, gross, known ex post; never usable as entry-time feature",
        inference="descriptive historical baseline; no independence or prospective evidence claims",
    ), indent=2) + "\n")
    cols = ["signal", "side", "n", "wins", "win_rate", "mean_win", "mean_loss", "payoff_ratio", "profit_factor", "mean_return", "winner_days", "loser_days", "loser_had_1pct_gain", "loser_had_1pct_gain_rate", "top5_profit_share"]
    print("PRIMARY BASELINE")
    print(summary[(summary.underlying == "blend") & (summary.period == "full") & (summary.basis == "gross")][cols].to_string(index=False))
    print("PERIOD AND EXECUTION")
    print(summary[(summary.underlying == "blend") & summary.signal.isin(["slope20", "equal_weight"]) & ((summary.period == "full") | (summary.basis == "gross"))][["signal", "side", "period", "basis", "n", "win_rate", "mean_return", "payoff_ratio"]].to_string(index=False))


if __name__ == "__main__":
    main()
