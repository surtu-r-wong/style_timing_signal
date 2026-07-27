from __future__ import annotations

import csv
import importlib.util
from datetime import date
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "data_fixes"
    / "2026-07-25-share-capital-par"
    / "verify_par_recovery.py"
)
SPEC = importlib.util.spec_from_file_location("verify_par_recovery", MODULE_PATH)
assert SPEC is not None
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERIFY)


def _write_csv(path, header, rows):
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def test_phase_after_fails_for_new_historical_gap_even_if_still_valued(
    tmp_path, monkeypatch, capsys
):
    _write_csv(
        tmp_path / "gap_before.csv",
        ["ts_code", "list_date", "first_eff"],
        [["OLD.SZ", "2010-01-01", "2025-04-01"]],
    )
    _write_csv(
        tmp_path / "valued_tickers_before.csv",
        ["ts_code"],
        [["OLD.SZ"], ["NEW.SZ"]],
    )
    gap_after = [
        ("OLD.SZ", date(2010, 1, 1), date(2025, 4, 1)),
        ("NEW.SZ", date(2011, 1, 1), date(2025, 4, 1)),
    ]
    valued_after = [("OLD.SZ",), ("NEW.SZ",)]
    answers = iter([gap_after, valued_after, []])
    monkeypatch.setattr(VERIFY, "HERE", tmp_path)
    monkeypatch.setattr(VERIFY, "_rows", lambda *args: next(answers))

    result = VERIFY.phase_after(object(), "stock_selector")

    captured = capsys.readouterr()
    assert result == 1
    assert "NEW HISTORICAL GAP REGRESSION: 1" in captured.out
    assert "NEW.SZ" in captured.err
