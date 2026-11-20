import sys

import pandas as pd
import pytest

from signals.common import data_source, style_prices


def test_regular_and_pure_are_separate_in_one_snapshot(monkeypatch):
    mapping = data_source.load_code_map()
    expected = ["932393", "932392", "932395", "932394", "932407", "932406", "932409", "932408"]
    assert [mapping[n] for n in (*style_prices.SMALL_NAMES, *style_prices.PURE_NAMES.values())] == [
        code + ".CSI" for code in expected]
    calls = []

    def load(names, **kw):
        calls.append((names, kw))
        return pd.DataFrame({n: [i] for i, n in enumerate(names)}, index=pd.to_datetime(["2026-11-19"]))

    monkeypatch.setattr(data_source, "load_pg_closes", load)
    names = ["沪深300成长", *style_prices.SMALL_NAMES]
    standard, pure = style_prices.price_panels(names)
    assert len(calls) == 1 and calls[0][1]["trim_ragged_head"] and calls[0][1]["trim_ragged_tail"]
    assert standard.columns.equals(pure.columns)
    assert standard.iloc[0].tolist() == [0, 1, 2, 3, 4]
    assert pure.iloc[0].tolist() == [0, 5, 6, 7, 8]

@pytest.mark.parametrize("include_pure", [False, True])
def test_combined_export_keeps_old_order_and_reads_archived_inputs(tmp_path, monkeypatch, include_pure):
    from backtest.production import RECOMMENDED_FILES
    from tools import export_combined_signals as export

    fields = {}
    for name, (path, column) in export.SIGNALS.items():
        fields.setdefault(path, {})[column] = 1
        if include_pure and name in ("equal_weight", "slope20"):
            fields[path]["factor_value_pure"] = -0.0123
        fields.setdefault(RECOMMENDED_FILES[name], {})["position"] = 1
    for path, column, _ in export.UPSTREAM.values():
        fields.setdefault(path, {})[column] = 1
    for path, row in fields.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"date": "2026-11-19", **row}]).to_csv(target, index=False)

    output = tmp_path / "combined.csv"
    monkeypatch.setattr(export, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["export", "--output", str(output)])
    assert export.main() == 0
    combined = pd.read_csv(output)
    assert list(combined.columns[-2:]) == ["equal_weight_pure_signal", "slope20_pure_signal"]
    assert combined.loc[0, "equal_weight_signal"] == 1
    observed = combined.iloc[0, -2:]
    assert (observed == -0.0123).all() if include_pure else observed.isna().all()
