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


# ────────────────── 上游冻结护栏（QA 2026-08-12 Important 4）──────────────────
#
# 盲区：三条产出是从 index_daily 算出来的，上游一冻结，「产出 vs 上游」恒为 0 落后、
# 恒报 OK —— 恰恰是本批立项要治的那种「停摆无人知」。所以要单独盯上游自己。

def test_upstream_fresh_passes():
    behind, breach = guard.check_upstream_freeze("2026-08-11", "2026-08-12")
    assert behind == 1 and breach is None


def test_upstream_at_threshold_passes():
    _, breach = guard.check_upstream_freeze("2026-08-05", "2026-08-12")  # 7 天
    assert breach is None


def test_upstream_frozen_beyond_threshold_breaches():
    behind, breach = guard.check_upstream_freeze("2026-08-04", "2026-08-12")  # 8 天
    assert behind == 8
    assert breach is not None and "可能已冻结" in breach


def test_upstream_holiday_window_relaxes_threshold():
    """国庆窗口内 8 天不报警（固定日期长假内置）。"""
    _, breach = guard.check_upstream_freeze("2026-09-30", "2026-10-08")
    assert breach is None


def test_upstream_still_breaches_even_in_holiday_when_way_too_stale():
    _, breach = guard.check_upstream_freeze("2026-09-10", "2026-10-08")  # 28 天
    assert breach is not None


def test_upstream_extra_holiday_window_silences_lunar_new_year():
    """农历假期靠 --holiday-window 登记（春节日期逐年变，不内置）。"""
    _, breach = guard.check_upstream_freeze("2027-02-05", "2027-02-16")
    assert breach is not None, "未登记春节窗口时应当报警"
    _, breach = guard.check_upstream_freeze(
        "2027-02-05", "2027-02-16", [("2027-02-06", "2027-02-17")])
    assert breach is None, "登记窗口后应消音"


@pytest.mark.parametrize("day,expected", [
    ("2026-01-02", True), ("2026-05-03", True), ("2026-10-05", True),
    ("2026-08-12", False), ("2026-03-15", False),
])
def test_fixed_holiday_windows(day, expected):
    assert guard.in_holiday_window(day) is expected


def test_parse_holiday_windows():
    assert guard.parse_holiday_windows("2027-02-06:2027-02-17, 2028-01-25:2028-02-02") == [
        ("2027-02-06", "2027-02-17"), ("2028-01-25", "2028-02-02")]
    assert guard.parse_holiday_windows("") == []
    assert guard.parse_holiday_windows(None) == []


def test_parse_holiday_windows_rejects_malformed():
    with pytest.raises(ValueError):
        guard.parse_holiday_windows("2027-02-06")


def test_gated_set_matches_production_signals():
    """护栏必须覆盖 backtest.baseline.SIGNALS 的三条生产信号 + 三份推荐持仓。"""
    from backtest.baseline import SIGNALS

    gated_paths = set(guard.GATED.values())
    for _, (path, _col) in SIGNALS.items():
        assert path in gated_paths, f"生产信号 {path} 不在护栏清单里"
    for name in SIGNALS:
        assert f"output/recommended/{name}_longflat.csv" in gated_paths
