"""Descriptive entry-event study; run with python3 <this file>.

Frozen signal and price inputs live beside this script. No production changes.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FILES = {
    "slope20": "slope20_longflat.csv",
    "equal_weight": "equal_weight_symmetric.csv",
    "hybrid20": "hybrid20_longflat.csv",
    "citic40d": "citic40d_longflat.csv",
}
START = pd.Timestamp("2014-01-02")
END = pd.Timestamp("2026-09-11")
HORIZONS = ["1d", "5d", "10d", "20d", "trade"]


def cluster_interval(frame, rng):
    """Resample entry calendar years, preserving within-year event dependence.

    Descriptive interval with only 13 clusters; not a prospective validation.
    """
    years = np.arange(2014, 2027)
    counts = []
    for year in years:
        f = frame[frame.date.dt.year == year]
        counts.append([
            ((f.direction == "up") & (f.ret > 0)).sum(),
            (f.direction == "up").sum(),
            ((f.direction == "down") & (f.ret > 0)).sum(),
            (f.direction == "down").sum(),
        ])
    counts = np.asarray(counts)
    draws = counts[rng.integers(0, len(years), size=(10000, len(years)))].sum(axis=1)
    draws = draws[(draws[:, 1] > 0) & (draws[:, 3] > 0)]
    if not len(draws):
        return np.nan, np.nan
    diff = draws[:, 0] / draws[:, 1] - draws[:, 2] / draws[:, 3]
    return np.quantile(diff, [0.025, 0.975])


def main():
    close = pd.read_csv(HERE / "spot_close.csv", parse_dates=["date"]).set_index("date")
    assert close.index.is_unique and close.notna().all().all()
    rets = close.pct_change(fill_method=None)
    rets["blend"] = rets[["500", "1000"]].mean(axis=1, skipna=False)
    rets = rets.loc[:END].dropna()
    nav = (1 + rets).cumprod()
    rows, counts, hashes = [], [], {}
    for name, filename in FILES.items():
        frozen = HERE / filename
        if not frozen.exists():
            frozen.write_bytes((ROOT / "output/recommended" / filename).read_bytes())
        hashes[filename] = hashlib.sha256(frozen.read_bytes()).hexdigest()
        pos = pd.read_csv(frozen, parse_dates=["date"]).set_index("date").position
        assert pos.index.is_unique and pos.index.is_monotonic_increasing
        # Exclude first observation: no observed transition into a long position.
        entries = pos.index[(pos > 0) & (pos.shift(1) <= 0)]
        entries = entries[(entries >= START) & (entries <= END)]
        calendar = rets.index[(rets.index >= max(START, pos.index.min())) & (rets.index <= END)]
        assert calendar.isin(pos.index).all(), "Signal calendar has gaps"
        assert entries.isin(rets.index).all(), "Entry missing market price"
        for underlying in ["blend", "500", "1000"]:
            t_returns = rets.loc[entries, underlying]
            counts.append(dict(signal=name, underlying=underlying, n=len(entries),
                               up=int((t_returns > 0).sum()), down=int((t_returns < 0).sum()),
                               flat=int((t_returns == 0).sum()), up_rate=(t_returns > 0).mean(),
                               baseline_up_rate=(rets.loc[calendar, underlying] > 0).mean(),
                               first_entry=entries.min(), last_entry=entries.max()))
            for t in entries:
                i = rets.index.get_loc(t)
                t_ret = rets.at[t, underlying]
                direction = "up" if t_ret > 0 else "down" if t_ret < 0 else "flat"
                exits = pos.index[(pos.index > t) & (pos <= 0)]
                exit_date = exits[0] if len(exits) else pd.NaT
                if pd.notna(exit_date) and exit_date > END:
                    exit_date = pd.NaT
                for horizon in HORIZONS:
                    if horizon == "trade":
                        end = exit_date
                    else:
                        j = i + int(horizon[:-1])
                        end = rets.index[j] if j < len(rets) else pd.NaT
                    # Outcomes start AFTER T. Unmatured observations are excluded.
                    ret = nav.at[end, underlying] / nav.at[t, underlying] - 1 if pd.notna(end) else np.nan
                    delay_ret = np.nan
                    if pd.notna(end) and i + 1 < len(rets) and end > rets.index[i + 1]:
                        delay_ret = nav.at[end, underlying] / nav.iloc[i + 1][underlying] - 1
                    rows.append(dict(signal=name, underlying=underlying, date=t, direction=direction,
                                     t_return=t_ret, horizon=horizon, end=end, ret=ret,
                                     delayed_entry_ret=delay_ret,
                                     trade_cost_ret=(1 + ret) * (1 - 0.0003) ** 2 - 1
                                     if horizon == "trade" else np.nan))
    events = pd.DataFrame(rows)
    events.to_csv(HERE / "events.csv", index=False)
    counts = pd.DataFrame(counts)
    counts.to_csv(HERE / "entry_counts.csv", index=False)
    rng = np.random.default_rng(20260915)
    result = []
    for (signal, underlying, horizon), f in events.groupby(["signal", "underlying", "horizon"], sort=False):
        for period, start, end in [("full", START, END), ("2014-2020", START, pd.Timestamp("2020-12-31")),
                                   ("2021-2023", pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31")),
                                   ("2024-2026", pd.Timestamp("2024-01-01"), END)]:
            mature = f[(f.date >= start) & (f.date <= end)].dropna(subset=["ret"])
            up = mature[mature.direction == "up"].ret
            down = mature[mature.direction == "down"].ret
            row = dict(signal=signal, underlying=underlying, horizon=horizon, period=period,
                       up_n=len(up), down_n=len(down), up_wins=int((up > 0).sum()),
                       down_wins=int((down > 0).sum()), up_win=(up > 0).mean(),
                       down_win=(down > 0).mean(), up_mean=up.mean(), down_mean=down.mean(),
                       up_median=up.median(), down_median=down.median())
            row["difference"] = row["up_win"] - row["down_win"]
            row["fisher_p"] = fisher_exact([[row["up_wins"], len(up) - row["up_wins"]],
                                            [row["down_wins"], len(down) - row["down_wins"]]]).pvalue if len(up) and len(down) else np.nan
            if period == "full" and underlying == "blend":
                row["year_cluster_ci_low"], row["year_cluster_ci_high"] = cluster_interval(mature, rng)
            result.append(row)
    summary = pd.DataFrame(result)
    # Twenty comparisons in the primary family: four lines x five horizons.
    ix = summary.index[(summary.period == "full") & (summary.underlying == "blend")]
    ordered = summary.loc[ix].sort_values("fisher_p").index
    summary.loc[ordered, "fisher_p_holm20"] = np.minimum(1, np.maximum.accumulate(
        summary.loc[ordered, "fisher_p"].to_numpy() * np.arange(len(ordered), 0, -1)))
    summary.to_csv(HERE / "summary.csv", index=False)
    hashes["spot_close.csv"] = hashlib.sha256((HERE / "spot_close.csv").read_bytes()).hexdigest()
    (HERE / "metadata.json").write_text(json.dumps(dict(
        start=str(START.date()), end=str(END.date()), input_sha256=hashes,
        primary_underlying="daily 50/50 Zhongzheng 500 and 1000 spot returns",
        entry="position(T)>0 and observed position(T-1)<=0; first row excluded",
        up="T close / previous trading close > 1",
        outcome="T close through T+h close, or first non-long signal close; gross spot returns",
        costs="primary excludes fees, carry, slippage; trade_cost_ret applies 3bps each side",
        maturity="incomplete horizons and unclosed trades excluded from outcome denominators",
        inference="two-sided Fisher diagnostic; Holm over primary 20 comparisons; 10000 entry-year cluster resamples",
        seed=20260915), indent=2) + "\n")
    print(counts[counts.underlying == "blend"].to_string(index=False))
    print(summary.loc[ix, ["signal", "horizon", "up_n", "down_n", "up_win", "down_win",
                           "difference", "fisher_p", "fisher_p_holm20", "year_cluster_ci_low",
                           "year_cluster_ci_high"]].to_string(index=False))


if __name__ == "__main__":
    main()
