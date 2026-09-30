"""Independent loop/direct-product checks of the frozen short event study."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

p = Path(__file__).resolve().parent
meta = json.loads((p / "metadata.json").read_text())
for filename, digest in meta["input_sha256"].items():
    assert hashlib.sha256((p / filename).read_bytes()).hexdigest() == digest
close = pd.read_csv(p / "spot_close.csv", parse_dates=["date"]).set_index("date")
returns = close.pct_change(fill_method=None)
returns["blend"] = (returns["500"] + returns["1000"]) / 2
positions = {
    name: pd.read_csv(p / f"{name}_symmetric.csv", parse_dates=["date"]).set_index("date").position
    for name in ["slope20", "equal_weight"]
}
events = pd.read_csv(p / "events.csv", parse_dates=["date", "end"])
for name, pos in positions.items():
    expected = []
    for i in range(1, len(pos)):
        if pos.iloc[i] < 0 and pos.iloc[i - 1] >= 0 and meta["start"] <= str(pos.index[i].date()) <= meta["end"]:
            expected.append(pos.index[i])
    actual = events[(events.signal == name) & (events.underlying == "blend") & (events.horizon == "1d")].date.tolist()
    assert expected == actual
for row in events.itertuples():
    assert np.isclose(row.t_return, returns.at[row.date, row.underlying])
    assert row.direction == ("up" if row.t_return > 0 else "down" if row.t_return < 0 else "flat")
    if pd.notna(row.end):
        path = returns.loc[(returns.index > row.date) & (returns.index <= row.end), row.underlying]
        expected = 1 - (1 + path).prod()
        assert np.isclose(row.ret, expected, atol=1e-12)
        if row.horizon == "trade":
            pos = positions[row.signal]
            assert (pos.loc[(pos.index >= row.date) & (pos.index < row.end)] < 0).all()
            assert pos.at[row.end] >= 0
            assert np.isclose(row.trade_cost_ret, expected - .0003 * (1 + (1 + path).prod()))
        else:
            assert len(path) == int(row.horizon[:-1])
        if pd.notna(row.delayed_entry_ret):
            assert np.isclose(row.delayed_entry_ret, 1 - (1 + path.iloc[1:]).prod())
summary = pd.read_csv(p / "summary.csv")
for row in summary[summary.period == "full"].itertuples():
    f = events[(events.signal == row.signal) & (events.underlying == row.underlying) & (events.horizon == row.horizon)].dropna(subset=["ret"])
    for group in ["up", "down"]:
        g = f[f.direction == group]
        assert len(g) == getattr(row, f"{group}_n")
        assert (g.ret > 0).sum() == getattr(row, f"{group}_wins")
message = f"PASS: input SHA256, independently reconstructed short transitions, all {len(events)} event-horizon rows via direct return products, trade exits, cost/delayed columns and full-window summary counts.\n"
(p / "verification.txt").write_text(message)
print(message)
print("PERIOD AND UNDERLYING SENSITIVITY")
print(summary[((summary.underlying == "blend") | (summary.period == "full")) & summary.horizon.isin(["5d", "20d", "trade"])][["signal", "underlying", "horizon", "period", "up_n", "down_n", "up_win", "down_win", "fisher_p"]].to_string(index=False))
print("UNMATURED")
print(events[(events.underlying == "blend") & events.ret.isna()][["signal", "date", "direction", "horizon"]].to_string(index=False))
print("COST SENSITIVITY")
for (name, direction), f in events[(events.underlying == "blend") & (events.horizon == "trade")].groupby(["signal", "direction"]):
    x = f.trade_cost_ret.dropna()
    print(name, direction, len(x), (x > 0).mean())
