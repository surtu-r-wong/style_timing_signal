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


# ────────────────── 产出缺口护栏（2026-08-17）──────────────────
#
# 盲区：命题 1 只看末行，中间缺一天看不见。2026-08-12/13 上游晚到（08-17 11:25 才
# 回填入库），08-14 那晚重算时库里还没有 → 八份产出齐齐跳过两天，而末行仍是 08-14，
# 当晚 status.json 报 max_lag=0、breaches: [] 一片绿。08-13 恰是 equal_weight 的
# 换仓日（pos 0→1），缺它会让持仓序列错判换仓时点。

def test_dates_of_reads_every_row(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("date,v\n2026-08-10,1\n2026-08-11,2\n", encoding="utf-8")
    assert guard.dates_of(csv) == ["2026-08-10", "2026-08-11"]


def test_dates_of_handles_missing_and_header_only(tmp_path):
    assert guard.dates_of(tmp_path / "nope.csv") == []
    empty = tmp_path / "header_only.csv"
    empty.write_text("date,v\n", encoding="utf-8")
    assert guard.dates_of(empty) == []


def test_coverage_gaps_clean_when_complete():
    assert guard.coverage_gaps(DAYS, DAYS) == ([], [])


def test_coverage_gaps_finds_interior_hole():
    """08-12/13 剧本：末行追平，中间缺两天。"""
    holed = [d for d in DAYS if d not in ("2026-08-05", "2026-08-06")]
    missing, off = guard.coverage_gaps(DAYS, holed)
    assert missing == ["2026-08-05", "2026-08-06"]
    assert off == []


def test_coverage_gaps_ignores_warmup_before_first_row():
    """区间下沿取产出自己的首行——各条信号 warmup 长度不同，不是缺口。"""
    late_start = DAYS[3:]
    assert guard.coverage_gaps(DAYS, late_start) == ([], [])


def test_coverage_gaps_flags_off_calendar_dates():
    with_saturday = DAYS + ["2026-08-08"]      # 周六不在日历上
    missing, off = guard.coverage_gaps(DAYS, with_saturday)
    assert off == ["2026-08-08"]
    assert missing == []


def test_coverage_gaps_survives_unsorted_file():
    """上下沿用 min/max，文件乱序也算得对。"""
    shuffled = [DAYS[4], DAYS[0], DAYS[2], DAYS[1], DAYS[3]]
    missing, off = guard.coverage_gaps(DAYS, shuffled)
    assert missing == [] and off == []


def test_coverage_gaps_empty_file_is_not_a_gap():
    assert guard.coverage_gaps(DAYS, []) == ([], [])


def _hole_in(root: Path, label: str, *holes: str) -> None:
    """把某个 label 的文件重写成「缺 holes 这几天、末行照旧追平」。"""
    path = root / {**guard.GATED, **guard.INFORMATIONAL}[label]
    lines = ["date,value"] + [f"{d},0.1" for d in DAYS if d not in holes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_interior_gap_in_gated_file_is_a_breach(tmp_path):
    """老护栏的盲区钉死：lag 仍是 0，但必须报 breach。"""
    _write_tree(tmp_path, DAYS[-1])
    _hole_in(tmp_path, "citic40d", "2026-08-05", "2026-08-06")
    files, breaches, worst = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert worst == 0, "末行追平，落后仍是 0——正因如此老护栏漏过了它"
    assert files["citic40d"]["lag_trading_days"] == 0
    assert files["citic40d"]["gap_count"] == 2
    assert files["citic40d"]["gap_dates"] == ["2026-08-05", "2026-08-06"]
    assert len(breaches) == 1
    assert "citic40d" in breaches[0] and "缺 2 个交易日" in breaches[0]


def test_interior_gap_in_informational_file_never_breaches(tmp_path, monkeypatch):
    """参考口径缺天照样可见（gap_count），但不 breach、也不进 output_gap_total。"""
    monkeypatch.setattr(guard, "load_calendar", lambda: (DAYS, DAYS[-1]))
    _write_tree(tmp_path, DAYS[-1])
    _hole_in(tmp_path, "equal_weight_5d20z", "2026-08-05")
    files, breaches, _ = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert breaches == []
    assert files["equal_weight_5d20z"]["gap_count"] == 1
    report, ok = guard.build_report(
        max_lag=1, root=tmp_path, upstream_max_days=10 ** 6, upstream_gap_lookback=0)
    assert ok is True
    assert report["output_gap_total"] == 0, "参考口径不进护栏的账"
    assert report["files"]["equal_weight_5d20z"]["gap_count"] == 1


def test_off_calendar_row_in_gated_file_is_a_breach(tmp_path):
    """日历外日期出现在中间（末行仍合法）→ 报 off_calendar，不与末行分支重复。"""
    _write_tree(tmp_path, DAYS[-1])
    path = tmp_path / guard.GATED["recommended_hybrid20"]
    lines = ["date,value"] + [f"{d},0.1" for d in DAYS[:3]] \
        + ["2026-08-08,0.1"] + [f"{d},0.1" for d in DAYS[3:]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    files, breaches, _ = guard.evaluate(DAYS, tmp_path, max_lag=1)
    assert files["recommended_hybrid20"]["off_calendar_count"] == 1
    assert len(breaches) == 1
    assert "不在 index_daily 交易日历上" in breaches[0]


def test_gap_dates_truncated_but_count_is_full(tmp_path):
    """明细进状态文件要截断（别撑爆），但计数必须是全量。"""
    long_days = [f"2026-06-{d:02d}" for d in range(1, 26)]   # 25 个"交易日"
    root = tmp_path
    for rel in {**guard.GATED, **guard.INFORMATIONAL}.values():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("date,value\n" + "".join(f"{d},0.1\n" for d in long_days),
                     encoding="utf-8")
    holed = [long_days[0]] + long_days[13:]                  # 中间挖掉 12 天
    p = root / guard.GATED["citic40d"]
    p.write_text("date,value\n" + "".join(f"{d},0.1\n" for d in holed), encoding="utf-8")
    files, breaches, _ = guard.evaluate(long_days, root, max_lag=1)
    assert files["citic40d"]["gap_count"] == 12
    assert len(files["citic40d"]["gap_dates"]) == guard.GAP_SAMPLE
    assert f"缺 12 个交易日" in breaches[0] and "等 12 天" in breaches[0]


# ────────────────── 上游库缺口（WARN 级，2026-08-17）──────────────────

def test_workdays_between_skips_weekends():
    # 08-07(五) → 08-10(一)，跳过 08-08/09 周末
    assert guard.workdays_between("2026-08-07", "2026-08-10") == [
        "2026-08-07", "2026-08-10"]


def test_upstream_gaps_none_when_calendar_complete():
    assert guard.upstream_calendar_gaps(DAYS, "2026-08-11", lookback_workdays=7) == []


def test_upstream_gaps_finds_missing_workdays():
    """08-12/13 剧本的上游侧：库内从 08-11 直接跳到 08-14。"""
    days = DAYS + ["2026-08-14"]
    gaps = guard.upstream_calendar_gaps(days, "2026-08-17", lookback_workdays=5)
    assert gaps == ["2026-08-12", "2026-08-13"]


def test_upstream_gaps_ignores_days_after_upstream_max():
    """上沿钉在库内最新交易日：更新的日子属「冻结」辖区，不是中间缺口。"""
    gaps = guard.upstream_calendar_gaps(DAYS, "2026-08-17", lookback_workdays=5)
    assert "2026-08-12" not in gaps and "2026-08-14" not in gaps
    assert gaps == []


def test_upstream_gaps_silenced_by_fixed_holiday_window():
    """国庆内置窗口：库内缺 10-01~10-02 不报。"""
    days = ["2026-09-29", "2026-09-30", "2026-10-05", "2026-10-06"]
    assert guard.upstream_calendar_gaps(days, "2026-10-06", lookback_workdays=6) == []


def test_upstream_gaps_silenced_by_registered_lunar_window():
    days = ["2027-02-04", "2027-02-05", "2027-02-18", "2027-02-19"]
    gaps = guard.upstream_calendar_gaps(days, "2027-02-19", lookback_workdays=11)
    assert gaps, "未登记春节窗口时应当报缺口"
    silenced = guard.upstream_calendar_gaps(
        days, "2027-02-19", lookback_workdays=11,
        extra_windows=[("2027-02-06", "2027-02-17")])
    assert silenced == []


def test_upstream_gaps_disabled_with_zero_lookback():
    days = DAYS + ["2026-08-14"]
    assert guard.upstream_calendar_gaps(days, "2026-08-17", lookback_workdays=0) == []


def test_upstream_gaps_are_warn_only_and_do_not_flip_exit_code(tmp_path, monkeypatch):
    """分级契约：产出缺口 = 硬失败；上游库缺口 = 只 WARN。"""
    days = DAYS + ["2026-08-14"]
    monkeypatch.setattr(guard, "load_calendar", lambda: (days, days[-1]))
    _write_tree(tmp_path, days[-1])
    path = tmp_path / guard.GATED["citic40d"]
    path.write_text("date,value\n" + "".join(f"{d},0.1\n" for d in days),
                    encoding="utf-8")
    report, ok = guard.build_report(
        max_lag=1, root=tmp_path,
        upstream_max_days=10 ** 6,          # 屏蔽「上游冻结」这条，单看缺口
        upstream_gap_lookback=5)
    assert report["upstream"]["gaps"] == ["2026-08-12", "2026-08-13"]
    assert report["breaches"] == []
    assert report["output_gap_total"] == 0
    assert ok is True, "上游库缺口不得翻转退出码"


def test_output_gap_flips_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "load_calendar", lambda: (DAYS, DAYS[-1]))
    _write_tree(tmp_path, DAYS[-1])
    _hole_in(tmp_path, "recommended_equal_weight", "2026-08-06")
    report, ok = guard.build_report(
        max_lag=1, root=tmp_path, upstream_max_days=10 ** 6,
        upstream_gap_lookback=0)          # 关掉上游那条，单看产出缺口
    assert ok is False
    assert report["output_gap_total"] == 1
    assert report["upstream"]["gaps"] == []
    assert len(report["breaches"]) == 1
    assert "recommended_equal_weight" in report["breaches"][0]


def test_gated_set_matches_production_signals():
    """护栏必须覆盖 backtest.baseline.SIGNALS 的三条生产信号 + 三份推荐持仓。"""
    from backtest.baseline import SIGNALS

    gated_paths = set(guard.GATED.values())
    for _, (path, _col) in SIGNALS.items():
        assert path in gated_paths, f"生产信号 {path} 不在护栏清单里"
    for name in SIGNALS:
        assert f"output/recommended/{name}_longflat.csv" in gated_paths
