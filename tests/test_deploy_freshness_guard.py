"""deploy/daily_signals/check_freshness.py 的新鲜度护栏单测（不连库）。

护栏的命题：三条生产信号 + 三份推荐持仓的末行日期，距 index_daily 最新交易日
不得超过 max_lag 个交易日。这里用合成交易日历 + tmp_path 下的 output 树副本，
把 evaluate() 当纯函数测——PG 只在 load_calendar() 里用，本文件不触碰。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "deploy" / "daily_signals" / "check_freshness.py"


def _load_guard():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("check_freshness", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

# 合成交易日历：跨周末，验证「交易日距离」而非自然日距离。
DAYS = [
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    "2026-08-10", "2026-08-11",
]


def _write_tree(root: Path, last_date: str, *, only: str | None = None,
                omit: str | None = None) -> None:
    """在 root 下按护栏期望的相对路径造出所有 CSV，末行日期为 last_date。

    only: 只把该 label 的文件写成 last_date，其余写成日历最后一天。
    omit: 不生成该 label 的文件。
    """
    targets = {**guard.GATED, **guard.INFORMATIONAL}
    for label, rel in targets.items():
        if label == omit:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        date_for_file = last_date if only in (None, label) else DAYS[-1]
        lines = ["date,value"]
        for d in DAYS:
            if d > date_for_file:
                break
            lines.append(f"{d},0.1")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_last_date_reads_final_row(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("date,v\n2026-08-10,1\n2026-08-11,2\n", encoding="utf-8")
    assert guard.last_date_of(csv) == "2026-08-11"


def test_last_date_handles_missing_and_header_only(tmp_path):
    assert guard.last_date_of(tmp_path / "nope.csv") is None
    empty = tmp_path / "header_only.csv"
    empty.write_text("date,v\n", encoding="utf-8")
    assert guard.last_date_of(empty) is None


@pytest.mark.parametrize(
    "last,expected",
    [("2026-08-11", 0), ("2026-08-10", 1), ("2026-08-07", 2), ("2026-08-03", 6)],
)
def test_lag_counts_trading_days_not_calendar_days(last, expected):
    # 08-07(五) → 08-10(一) 只差 1 个交易日，但差 3 个自然日。
    assert guard.lag_trading_days(DAYS, last) == expected


def test_lag_none_for_unknown_or_missing_date():
    assert guard.lag_trading_days(DAYS, None) is None
    assert guard.lag_trading_days(DAYS, "2026-08-08") is None  # 周六，不在日历上


def test_fresh_tree_passes(tmp_path):
    _write_tree(tmp_path, DAYS[-1])
    files, breaches, worst = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert breaches == []
    assert worst == 0
    assert len(files) == len(guard.GATED) + len(guard.INFORMATIONAL)


def test_one_trading_day_behind_is_tolerated(tmp_path):
    _write_tree(tmp_path, DAYS[-2])
    _, breaches, worst = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert breaches == []
    assert worst == 1


def test_stale_gated_file_is_reported(tmp_path):
    # 只让 equal_weight 生产信号落后 6 个交易日，其余追平。
    _write_tree(tmp_path, DAYS[0], only="equal_weight_20d40z")
    _, breaches, worst = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert worst == 6
    assert len(breaches) == 1
    assert "equal_weight_20d40z" in breaches[0]


def test_informational_file_never_breaches(tmp_path):
    # 参考口径的 5d20z 停更 6 个交易日也不拦截（只报告）。
    _write_tree(tmp_path, DAYS[0], only="equal_weight_5d20z")
    files, breaches, _ = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert breaches == []
    assert files["equal_weight_5d20z"]["lag_trading_days"] == 6
    assert files["equal_weight_5d20z"]["gated"] is False


def test_missing_gated_file_is_a_breach(tmp_path):
    _write_tree(tmp_path, DAYS[-1], omit="recommended_citic40d")
    _, breaches, _ = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert len(breaches) == 1
    assert "recommended_citic40d" in breaches[0]


def test_date_off_calendar_is_a_breach(tmp_path):
    _write_tree(tmp_path, DAYS[-1])
    bad = tmp_path / guard.GATED["citic40d"]
    bad.write_text("date,v\n2026-08-08,0.1\n", encoding="utf-8")  # 周六
    _, breaches, _ = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert len(breaches) == 1
    assert "不在 index_daily 交易日历上" in breaches[0]


def test_gated_set_matches_production_signals():
    """护栏必须覆盖 backtest.baseline.SIGNALS 的三条生产信号 + 三份推荐持仓。"""
    from backtest.baseline import SIGNALS

    gated_paths = set(guard.GATED.values())
    for _, (path, _col) in SIGNALS.items():
        assert path in gated_paths, f"生产信号 {path} 不在护栏清单里"
    for name in SIGNALS:
        assert f"output/recommended/{name}_longflat.csv" in gated_paths
