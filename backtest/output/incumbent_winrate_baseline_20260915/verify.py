"""Check baseline against source studies and direct price products."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

p = Path(__file__).resolve().parent
meta = json.loads((p / "metadata.json").read_text())
for filename, digest in meta["input_sha256"].items():
    assert hashlib.sha256((p.parent / filename).read_bytes()).hexdigest() == digest
ledger = pd.read_csv(p / "trades.csv", parse_dates=["date", "end", "delayed_entry", "delayed_exit"])
summary = pd.read_csv(p / "summary.csv")
close = pd.read_csv(p.parent / "entry_day_direction_20260915/spot_close.csv", parse_dates=["date"]).set_index("date")
rets = close.pct_change(fill_method=None)
rets["blend"] = (rets["500"] + rets["1000"]) / 2
for row in ledger.itertuples():
    if pd.isna(row.end):
        assert pd.isna(row.gross)
        continue
    sign = 1 if row.side == "long" else -1
    r = rets.loc[(rets.index > row.date) & (rets.index <= row.end), row.underlying]
    ratio = (1 + r).prod()
    assert len(r) == row.days
    assert np.isclose(row.gross, sign * (ratio - 1), atol=1e-12)
    assert np.isclose(row.net_3bps, sign * (ratio - 1) - .0003 * (1 + ratio))
    path = np.r_[0., sign * ((1 + r).cumprod().to_numpy() - 1)]
    assert np.isclose(row.mfe, path.max()) and np.isclose(row.mae, path.min())
    if pd.notna(row.delayed_exit):
        assert row.delayed_entry == rets.index[rets.index.get_loc(row.date) + 1]
        assert row.delayed_exit == rets.index[rets.index.get_loc(row.end) + 1]
        d = rets.loc[(rets.index > row.delayed_entry) & (rets.index <= row.delayed_exit), row.underlying]
        ratio = (1 + d).prod()
        assert np.isclose(row.delayed_gross, sign * (ratio - 1))
        assert np.isclose(row.delayed_net_3bps, sign * (ratio - 1) - .0003 * (1 + ratio))
for row in summary[summary.period == "full"].itertuples():
    f = ledger[(ledger.signal == row.signal) & (ledger.underlying == row.underlying)]
    if row.side != "combined":
        f = f[f.side == row.side]
    x = f[row.basis].dropna()
    assert len(x) == row.n and (x > 0).sum() == row.wins and (x < 0).sum() == row.losses
    assert np.isclose(x.mean(), row.mean_return)
    assert np.isclose(row.win_rate * row.mean_win + row.losses / row.n * row.mean_loss, row.mean_return)
# Exact reconciliation with the preceding T-day grouped studies.
for side, directory in [("long", "entry_day_direction_20260915"), ("short", "short_entry_day_direction_20260915")]:
    old = pd.read_csv(p.parent / directory / "summary.csv")
    for row in old[(old.period == "full") & (old.horizon == "trade")].itertuples():
        new = summary[(summary.period == "full") & (summary.basis == "gross") & (summary.side == side) & (summary.signal == row.signal) & (summary.underlying == row.underlying)].iloc[0]
        assert new.n == row.up_n + row.down_n and new.wins == row.up_wins + row.down_wins
message = f"PASS: input hashes; {len(ledger)} trade rows with direct return products, both delayed endpoints, costs and excursions; all full-window counts and expectancy identities; exact reconciliation with preceding long/short studies.\n"
(p / "verification.txt").write_text(message)
print(message)
