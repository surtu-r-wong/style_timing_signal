"""deploy/daily_signals/wait_for_inputs.py 单测（2026-09-23 立；不连库：时钟、sleep、日历、查询全部注入）。

背景：2026-09-23 起本项目 15 个输入指数由 data_manager 夜间作业（WSL2 20:00 起跑、约 20:02 结束）写入
`stock_selector.index_daily`，本链路只读。runner 第 0 步（办公室模式）用这个脚本等当日 15 码到齐：
没齐每 5 分钟再查、最迟等到 21:30，结果 OK / LATE / CHECK_ERROR 以最后三行交给 runner。

钉住的命题：
* 期望信号日 T 按**交易日历**算（节假日不等）；日历缺某天 → 那天按周一至周五推断，并在结果里注明；
* 到齐判据 = 办公室回函 01 §7.3 原样（T 日、这 15 码、close 非空的行数 = 码数）；
* 只在「T 是今天、还没到截止」时轮询，最后一轮恰在截止时刻；开跑已过截止 / --once / T 是过去某天 → 只查一次；
* 单轮出错下一轮再查；截止时仍出错 → CHECK_ERROR；
* 输出契约：最后三行 INPUTS_STATUS / INPUTS_DAY / INPUTS_REASON，退出码恒为 0。
"""
import importlib.util
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAIT_PATH = ROOT / "deploy" / "daily_signals" / "wait_for_inputs.py"
TOPUP_SCRIPT = ROOT / "tools" / "topup_index_daily.sh"


def _load():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("wait_for_inputs", WAIT_PATH)
    module = importlib.util.module_from_spec(spec)
    # 必须先登记：模块里有 dataclass + `from __future__ import annotations`（同 notify_wechat 的单测）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wfi = _load()

# 本项目 15 个输入码，顺序同 tools/topup_index_daily.sh 的 CODES（手抄字面量，下面有两条一致性判例）。
CODES = [
    "CI005917.WI", "CI005918.WI", "CI005919.WI", "CI005920.WI", "CI005921.WI",
    "000918.CSI", "000919.CSI", "H30351.CSI", "H30352.CSI",
    "932406.CSI", "932407.CSI", "932408.CSI", "932409.CSI", "932000.CSI", "000300.SH",
]
ALL = set(CODES)

# CN 交易日历 2026-09-18..10-12：2026-09-23 对 data_manager.business_calendar（calendar_id='CN'）只读实查。
# 09-25 中秋（周五）、10-01~10-07 国庆休市；周末休市（含 09-27 周日）。T = 交易日，. = 休市。
_CAL_PROBE = ("09-18T 09-19. 09-20. 09-21T 09-22T 09-23T 09-24T 09-25. 09-26. 09-27. 09-28T 09-29T "
              "09-30T 10-01. 10-02. 10-03. 10-04. 10-05. 10-06. 10-07. 10-08T 10-09T 10-10. 10-11. 10-12T")
CAL = {date.fromisoformat(f"2026-{tok[:5]}"): tok[5] == "T" for tok in _CAL_PROBE.split()}

READY, DEADLINE = time(20, 0), time(21, 30)


def test_calendar_fixture_is_the_probe():
    assert len(CAL) == 25 and min(CAL) == date(2026, 9, 18) and max(CAL) == date(2026, 10, 12)
    assert [d.isoformat() for d, b in sorted(CAL.items()) if not b and d.weekday() < 5] == [
        "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]


# ── 码表 ──────────────────────────────────────────────────────────────────────

def test_input_codes_file_is_the_default_and_matches_literal():
    assert wfi.CODES_FILE == ROOT / "deploy" / "daily_signals" / "input_codes.txt"
    assert wfi.load_codes(wfi.CODES_FILE) == CODES


def test_input_codes_match_topup_script_codes():
    """码表与 topup 脚本（已停用、留作回退）的 CODES 顺序与内容完全一致：回退时两边取的是同一批码。"""
    m = re.search(r'^CODES="([^"]*)"$', TOPUP_SCRIPT.read_text(encoding="utf-8"), re.M)
    assert m, "tools/topup_index_daily.sh 里找不到 CODES=\"…\" 行"
    assert wfi.load_codes(wfi.CODES_FILE) == m.group(1).split(",")


def test_load_codes_skips_comments_blank_lines_and_whitespace(tmp_path):
    p = tmp_path / "codes.txt"
    p.write_text("# 注释\n\n  CI005917.WI  \n#000300.SH\n\t000300.SH\n\n", encoding="utf-8")
    assert wfi.load_codes(p) == ["CI005917.WI", "000300.SH"]


@pytest.mark.parametrize("content", ["", "# 只有注释\n\n   \n"])
def test_load_codes_rejects_empty_list(tmp_path, content):
    """空码表会让「行数 = 码数」恒成立（0 = 0，永远「到齐」）——必须当错。"""
    p = tmp_path / "codes.txt"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="没有任何码"):
        wfi.load_codes(p)


def test_load_codes_rejects_duplicates(tmp_path):
    """重复码会让「行数 = 码数」永不成立（主键 (index_code, trade_date) 下同码只有一行）。"""
    p = tmp_path / "codes.txt"
    p.write_text("CI005917.WI\n000300.SH\nCI005917.WI\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CI005917.WI"):
        wfi.load_codes(p)


# ── 期望信号日 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("now, expected", [
    ("2026-09-23 20:00:00", "2026-09-23"),   # 交易日，恰到 20:00 → 今天
    ("2026-09-23 20:31:17", "2026-09-23"),   # 交易日 20:00 后 → 今天
    ("2026-09-23 19:59:59", "2026-09-22"),   # 交易日 20:00 前 → 前一交易日
    ("2026-09-21 08:00:00", "2026-09-18"),   # 周一早上（开机补跑）→ 跨周末取上周五
    ("2026-09-25 20:30:00", "2026-09-24"),   # 中秋（工作日休市）→ 周四
    ("2026-09-27 20:30:00", "2026-09-24"),   # 周日，前面还叠着中秋 → 周四
    ("2026-09-28 19:00:00", "2026-09-24"),   # 节后首个交易日 20:00 前 → 节前最后交易日
    ("2026-10-01 20:30:00", "2026-09-30"),   # 国庆（工作日休市）→ 节前最后交易日
    ("2026-10-08 20:30:00", "2026-10-08"),   # 节后首日 20:00 后 → 今天
    ("2026-10-08 12:00:00", "2026-09-30"),   # 节后首日白天 → 跨过整个国庆
])
def test_expected_signal_day_follows_cn_calendar(now, expected):
    sd = wfi.expected_signal_day(datetime.fromisoformat(now), READY, CAL)
    assert sd.day == date.fromisoformat(expected)
    assert sd.guessed == ()


def test_expected_signal_day_explains_itself():
    assert "今天是交易日且已过 20:00" in wfi.expected_signal_day(datetime(2026, 9, 23, 20, 31), READY, CAL).why
    assert "未到 20:00" in wfi.expected_signal_day(datetime(2026, 9, 23, 15, 0), READY, CAL).why
    assert "今天不是交易日" in wfi.expected_signal_day(datetime(2026, 10, 1, 20, 31), READY, CAL).why


def test_ready_from_is_a_parameter():
    assert wfi.expected_signal_day(datetime(2026, 9, 23, 19, 0), time(18, 30), CAL).day == date(2026, 9, 23)


def test_calendar_missing_today_falls_back_to_weekday():
    """2027 未装载：今天（周一）不在表里 → 按周一至周五推断为交易日，并记下推断了哪天。"""
    sd = wfi.expected_signal_day(datetime(2027, 1, 4, 20, 30), READY, CAL)
    assert sd.day == date(2027, 1, 4) and sd.guessed == (date(2027, 1, 4),)


def test_calendar_missing_walks_back_over_weekend_by_weekday():
    """20:00 前、日历全缺：跳过周末取周五——元旦会被误判成交易日，这正是结果里要注明「按工作日推断」的原因。
    20:00 前不必查今天（信号日必在今天之前），所以今天不算「推断过」。"""
    sd = wfi.expected_signal_day(datetime(2027, 1, 4, 9, 0), READY, {})
    assert sd.day == date(2027, 1, 1)
    assert sd.guessed == (date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3))


def test_calendar_partially_missing_only_guesses_missing_days():
    """表里有的日子照表（国庆照样跳过），表里没有的那天才按工作日推断。"""
    cal = {date(2026, 10, 1) + timedelta(days=i): False for i in range(7)}
    sd = wfi.expected_signal_day(datetime(2026, 10, 8, 12, 0), READY, cal)
    assert sd.day == date(2026, 9, 30) and sd.guessed == (date(2026, 9, 30),)


def test_calendar_note():
    guessed = wfi.SignalDay(date(2027, 1, 1), "…", (date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3)))
    assert wfi.calendar_note(guessed, None) == "日历缺 2027-01-01、2027-01-02、2027-01-03，按工作日推断"
    assert wfi.calendar_note(guessed, "OSError: down") == "日历读取失败（OSError: down），按工作日推断"
    assert wfi.calendar_note(wfi.SignalDay(date(2026, 9, 23), "…", ()), None) == ""


# ── 轮询 ──────────────────────────────────────────────────────────────────────

class Clock:
    """假时钟：sleep 只推进时间、不真等；events 记下 sleep 与日志的先后。"""

    def __init__(self, start: str, events: list | None = None):
        self.now = datetime.fromisoformat(start)
        self.slept: list[float] = []
        self.events = events if events is not None else []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.events.append(("sleep", seconds))
        self.now += timedelta(seconds=seconds)


class Seq:
    """按调用次序给结果：普通值照返，异常实例照抛，用完后重复最后一项；calls 记下每次调用的参数。"""

    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, *args):
        self.calls.append(args)
        item = self.items[min(len(self.calls), len(self.items)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


class RangeLoader(Seq):
    """日历加载桩：像真表一样只返回 [start, end] 内的日子——取数范围不够时，「按工作日推断」注记会露出来。
    items 是逐次调用的日历 dict 或异常实例（同 Seq）。"""

    def __call__(self, start, end):
        self.calls.append((start, end))
        item = self.items[min(len(self.calls), len(self.items)) - 1]
        if isinstance(item, BaseException):
            raise item
        return {d: b for d, b in item.items() if start <= d <= end}


def run(clock, fetch, *, calendar=None, once=False, interval=300, deadline=DEADLINE):
    logs: list[str] = []

    def log(line):
        logs.append(line)
        clock.events.append(("log", line))

    result = wfi.wait_for_inputs(
        CODES, now_fn=clock, sleep_fn=clock.sleep,
        load_calendar=calendar if calendar is not None else RangeLoader(CAL),
        fetch_present=fetch, ready_from=READY, deadline=deadline,
        interval=interval, once=once, log=log)
    return result, logs


def test_all_present_on_first_check_is_ok_without_sleeping():
    clock, fetch = Clock("2026-09-23 20:31:17"), Seq(ALL)
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("OK", date(2026, 9, 23))
    assert res.reason == "办公室日更 2026-09-23 15 码到齐（等 0 秒）"
    assert clock.slept == [] and fetch.calls == [(date(2026, 9, 23), CODES)]


def test_arrival_on_third_check_reports_seconds_waited():
    clock, fetch = Clock("2026-09-23 20:30:00"), Seq(set(), set(CODES[:12]), ALL)
    res, _ = run(clock, fetch)
    assert (res.status, res.reason) == ("OK", "办公室日更 2026-09-23 15 码到齐（等 600 秒）")
    assert clock.slept == [300, 300] and len(fetch.calls) == 3


def test_fourteen_of_fifteen_is_not_arrived():
    """到齐 = 行数等于码数，差一个也不算。"""
    clock, fetch = Clock("2026-09-23 21:30:00"), Seq(set(CODES[:-1]))
    res, _ = run(clock, fetch)
    assert res.status == "LATE" and "仍缺 1 码：000300.SH" in res.reason


def test_rows_for_other_codes_do_not_count():
    """查询结果里混进不在码表的码（假查询才会这样），也不能凑数。"""
    clock, fetch = Clock("2026-09-23 21:30:00"), Seq(set(CODES[:-1]) | {"000001.SH"})
    res, _ = run(clock, fetch)
    assert res.status == "LATE" and "仍缺 1 码：000300.SH" in res.reason


def test_still_missing_at_deadline_is_late_and_lists_missing_codes():
    clock, fetch = Clock("2026-09-23 20:31:17"), Seq(set(CODES[:12]))
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("LATE", date(2026, 9, 23))
    assert res.reason == ("2026-09-23 截至 21:30 仍缺 3 码：932409.CSI、932000.CSI、000300.SH，"
                          "按库内已有数据照算")
    # 20:31:17 起每 5 分钟一轮，第 12 轮在 21:26:17，离截止只剩 223 秒 → 补一轮恰在 21:30:00
    assert clock.slept == [300] * 11 + [223]
    assert len(fetch.calls) == 13 and clock.now == datetime(2026, 9, 23, 21, 30)


def test_query_error_is_retried_next_round():
    clock, fetch = Clock("2026-09-23 20:30:00"), Seq(OSError("connection reset"), ALL)
    res, logs = run(clock, fetch)
    assert (res.status, res.reason) == ("OK", "办公室日更 2026-09-23 15 码到齐（等 300 秒）")
    assert any("查询出错" in line and "OSError: connection reset" in line for line in logs)


def test_error_until_deadline_is_check_error_in_one_line():
    clock = Clock("2026-09-23 21:20:00")
    fetch = Seq(RuntimeError("could not connect\n\tIs the server running?"))
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("CHECK_ERROR", date(2026, 9, 23))
    assert res.reason == "2026-09-23 到齐检查出错：RuntimeError: could not connect Is the server running?"
    assert clock.slept == [300, 300] and len(fetch.calls) == 3


def test_error_earlier_but_last_round_answered_is_late():
    """「截止时仍出错」才是 CHECK_ERROR：最后一轮查成了、只是没齐，就是 LATE。"""
    clock, fetch = Clock("2026-09-23 21:25:00"), Seq(OSError("x"), set(CODES[1:]))
    res, _ = run(clock, fetch)
    assert res.status == "LATE"
    assert res.reason == "2026-09-23 截至 21:30 仍缺 1 码：CI005917.WI，按库内已有数据照算"


def test_long_error_message_is_truncated():
    clock, fetch = Clock("2026-09-23 21:30:00"), Seq(RuntimeError("x" * 1000))
    res, _ = run(clock, fetch)
    assert res.status == "CHECK_ERROR" and len(res.reason) < 300 and res.reason.endswith("…")


@pytest.mark.parametrize("start", ["2026-09-23 21:30:00", "2026-09-23 22:10:00", "2026-09-23 23:59:00"])
def test_started_at_or_after_deadline_checks_once(start):
    clock, fetch = Clock(start), Seq(set())
    res, _ = run(clock, fetch)
    hhmm = start[11:16]
    assert res.status == "LATE" and res.reason.startswith(f"2026-09-23 截至 {hhmm} 仍缺 15 码：CI005917.WI、")
    assert clock.slept == [] and len(fetch.calls) == 1


def test_once_checks_once_even_before_deadline():
    clock, fetch = Clock("2026-09-23 20:31:00"), Seq(set(CODES[:14]))
    res, _ = run(clock, fetch, once=True)
    assert res.reason == "2026-09-23 截至 20:31 仍缺 1 码：000300.SH，按库内已有数据照算"
    assert clock.slept == [] and len(fetch.calls) == 1


@pytest.mark.parametrize("start, day", [
    ("2026-09-23 15:00:00", "2026-09-22"),   # 交易日白天手工重跑
    ("2026-09-24 08:00:00", "2026-09-23"),   # 次日早上开机补跑
    ("2026-10-01 20:31:00", "2026-09-30"),   # 国庆照跑（定时器按周一至周五触发）
])
def test_signal_day_in_the_past_checks_once(start, day):
    """信号日不是今天：那一晚的夜间作业早跑完了，等也等不来——只查一次。否则白天起跑会一路等到当晚 21:30，
    先撞上 runner 的 4500 秒兜底。"""
    clock, fetch = Clock(start), Seq(set())
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("LATE", date.fromisoformat(day))
    assert clock.slept == [] and fetch.calls == [(date.fromisoformat(day), CODES)]


def test_calendar_is_loaded_for_a_range_ending_today():
    """日历取 [今天 - 回看, 今天]；回看至少覆盖春节加两头周末（约 12 天）。"""
    loader = RangeLoader(CAL)
    run(Clock("2026-10-08 12:00:00"), Seq(ALL), calendar=loader)
    (start, end), = loader.calls
    assert end == date(2026, 10, 8) and (end - start).days >= 20


def test_holiday_walk_back_uses_calendar_without_note():
    """节后首日白天：日历取数范围够，跨过整个国庆取 09-30，不出「按工作日推断」注记。"""
    res, _ = run(Clock("2026-10-08 12:00:00"), Seq(ALL))
    assert (res.status, res.day) == ("OK", date(2026, 9, 30))
    assert res.reason == "办公室日更 2026-09-30 15 码到齐（等 0 秒）"


def test_calendar_gap_is_noted_in_result():
    res, _ = run(Clock("2027-01-04 20:31:00"), Seq(ALL))
    assert (res.status, res.day) == ("OK", date(2027, 1, 4))
    assert res.reason == "办公室日更 2027-01-04 15 码到齐（等 0 秒）；日历缺 2027-01-04，按工作日推断"


def test_calendar_gap_is_noted_in_late_and_check_error():
    late, _ = run(Clock("2027-01-04 21:30:00"), Seq(set()))
    assert late.status == "LATE" and late.reason.endswith("；日历缺 2027-01-04，按工作日推断")
    err, _ = run(Clock("2027-01-04 21:30:00"), Seq(OSError("x")))
    assert err.reason == "2027-01-04 到齐检查出错：OSError: x；日历缺 2027-01-04，按工作日推断"


def test_calendar_read_failure_falls_back_and_retries():
    """日历读不到：先按工作日推断信号日、照查数据，下一轮再读日历；读到了就按日历改正信号日。
    10-01（周四）按工作日会被当成交易日 → 查 10-01 查不到 → 第二轮日历读到 → 改查 09-30。"""
    clock = Clock("2026-10-01 20:31:00")
    loader, fetch = RangeLoader(OSError("calendar down"), CAL), Seq(set(), ALL)
    res, logs = run(clock, fetch, calendar=loader)
    assert (res.status, res.day) == ("OK", date(2026, 9, 30))
    assert [c[0] for c in fetch.calls] == [date(2026, 10, 1), date(2026, 9, 30)]
    assert len(loader.calls) == 2 and clock.slept == [300]
    assert res.reason == "办公室日更 2026-09-30 15 码到齐（等 300 秒）"
    assert any("日历读取失败" in line for line in logs)


def test_calendar_read_failure_is_noted_when_it_never_recovers():
    loader = RangeLoader(PermissionError("permission denied for schema data_manager"))
    res, _ = run(Clock("2026-09-23 20:31:00"), Seq(ALL), calendar=loader)
    assert res.status == "OK"
    assert res.reason == ("办公室日更 2026-09-23 15 码到齐（等 0 秒）；日历读取失败（PermissionError: "
                          "permission denied for schema data_manager），按工作日推断")


def test_each_round_logs_one_line_before_sleeping():
    """每轮一行进度，且在 sleep 之前打出（runner 日志里实时可见）。"""
    events: list = []
    clock = Clock("2026-09-23 20:30:00", events)
    fetch = Seq(set(CODES[:12]), OSError("x"), ALL)
    run(clock, fetch)
    rounds = [e for e in events if e[0] == "log" and re.search(r"第 \d+ 轮", e[1])]
    assert len(rounds) == 3
    assert "已到 12/15" in rounds[0][1] and "932409.CSI" in rounds[0][1] and "300 秒后再查" in rounds[0][1]
    assert "查询出错 OSError: x" in rounds[1][1] and "300 秒后再查" in rounds[1][1]
    assert "15/15 到齐" in rounds[2][1]
    kinds = [e[0] if e[0] == "sleep" else ("round" if e in rounds else "other") for e in events]
    assert [k for k in kinds if k != "other"] == ["round", "sleep", "round", "sleep", "round"]


# ── 取数（SQL 与连接；不连库，查询函数注入）──────────────────────────────────────

def _norm(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_presence_query_is_the_office_criterion():
    """回函 01 §7.3 原样：T 日、这些码、close 非空的行；返回已到的码。"""
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        return [("CI005917.WI",), ("000300.SH",)]

    assert wfi.fetch_present_codes(date(2026, 9, 23), CODES, query=query) == {"CI005917.WI", "000300.SH"}
    (sql, params), = calls
    s = _norm(sql)
    assert "from {schema}.index_daily" in s
    for clause in ("trade_date = %s", "index_code = any(%s)", "close is not null"):
        assert clause in s, clause
    assert params == (date(2026, 9, 23), CODES)


def test_calendar_query_reads_cn_business_days_in_range():
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        return [(date(2026, 9, 25), False), (date(2026, 9, 24), True)]

    got = wfi.load_business_calendar(date(2026, 9, 1), date(2026, 9, 30), query=query)
    assert got == {date(2026, 9, 25): False, date(2026, 9, 24): True}
    (sql, params), = calls
    s = _norm(sql)
    assert "from data_manager.business_calendar" in s and "is_business_day" in s
    assert "calendar_id = %s" in s and "calendar_date between %s and %s" in s
    assert params == ("CN", date(2026, 9, 1), date(2026, 9, 30))


DB = {"host": "h", "port": 5432, "name": "db", "user": "u", "password": "p", "schema": "stock_selector"}


def test_connection_is_read_only_with_timeouts():
    kw = wfi.connect_kwargs(DB)
    assert (kw["host"], kw["port"], kw["dbname"], kw["user"], kw["password"]) == ("h", 5432, "db", "u", "p")
    assert kw["connect_timeout"] == 10
    opts = kw["options"].split()
    assert "statement_timeout=60000" in opts and "default_transaction_read_only=on" in opts


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.conn.executed.append((sql, params))
        if self.conn.error:
            raise self.conn.error

    def fetchall(self):
        return [("CI005917.WI",)]


class FakeConn:
    def __init__(self, error=None):
        self.error, self.executed, self.closed = error, [], False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def test_run_query_opens_a_fresh_connection_each_time_and_fills_schema():
    conns, kwargs = [], []

    def connect(**kw):
        kwargs.append(kw)
        conns.append(FakeConn())
        return conns[-1]

    for _ in range(2):
        rows = wfi.run_query("SELECT index_code FROM {schema}.index_daily", (1,), connect=connect, db_config=DB)
        assert rows == [("CI005917.WI",)]
    assert len(conns) == 2 and all(c.closed for c in conns)
    assert conns[0].executed == [("SELECT index_code FROM stock_selector.index_daily", (1,))]
    assert kwargs[0] == wfi.connect_kwargs(DB)


def test_run_query_closes_connection_on_error():
    conn = FakeConn(error=RuntimeError("statement timeout"))
    with pytest.raises(RuntimeError, match="statement timeout"):
        wfi.run_query("SELECT 1", (), connect=lambda **kw: conn, db_config=DB)
    assert conn.closed


# ── main：输出契约 ─────────────────────────────────────────────────────────────

def _main(argv, clock, fetch, calendar=None):
    return wfi.main(argv, now_fn=clock, sleep_fn=clock.sleep,
                    load_calendar=calendar if calendar is not None else RangeLoader(CAL),
                    fetch_present=fetch)


def test_main_prints_contract_lines_last_and_exits_zero(capsys):
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), Seq(ALL))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[-3:] == ["INPUTS_STATUS=OK", "INPUTS_DAY=2026-09-23",
                        "INPUTS_REASON=办公室日更 2026-09-23 15 码到齐（等 0 秒）"]
    assert len(out) > 3 and not any(line.startswith("INPUTS_") for line in out[:-3])   # 人读的进度行在前


@pytest.mark.parametrize("status_rounds, expected", [
    ((set(),), "LATE"),
    ((OSError("x"),), "CHECK_ERROR"),
])
def test_main_exit_code_is_zero_for_every_result(capsys, status_rounds, expected):
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), Seq(*status_rounds))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and out[-3] == f"INPUTS_STATUS={expected}"


def test_main_passes_options_through(capsys):
    """--interval / --deadline / --ready-from 生效：18:30 起算、18:40 截止、每 120 秒一轮。"""
    clock, fetch = Clock("2026-09-23 18:35:00"), Seq(set())
    rc = _main(["--ready-from", "18:30", "--deadline", "18:40", "--interval", "120"], clock, fetch)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and out[-3:-1] == ["INPUTS_STATUS=LATE", "INPUTS_DAY=2026-09-23"]
    assert clock.slept == [120, 120, 60] and len(fetch.calls) == 4


def test_main_reads_codes_file_option(tmp_path, capsys):
    p = tmp_path / "codes.txt"
    p.write_text("000300.SH\n", encoding="utf-8")
    fetch = Seq({"000300.SH"})
    _main(["--once", "--codes-file", str(p)], Clock("2026-09-23 20:31:17"), fetch)
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "INPUTS_REASON=办公室日更 2026-09-23 1 码到齐（等 0 秒）"
    assert fetch.calls == [(date(2026, 9, 23), ["000300.SH"])]


def test_main_turns_unexpected_errors_into_check_error(tmp_path, capsys):
    rc = _main(["--codes-file", str(tmp_path / "missing.txt")], Clock("2026-09-23 20:31:17"), Seq(ALL))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and out[-3:-1] == ["INPUTS_STATUS=CHECK_ERROR", "INPUTS_DAY=2026-09-23"]
    assert out[-1].startswith("INPUTS_REASON=2026-09-23 到齐检查出错：FileNotFoundError: ")
    assert out[-1].endswith("按工作日推断")


@pytest.mark.parametrize("argv", [["--bogus"], ["--deadline", "25:99"], ["--ready-from", "8pm"],
                                  ["--interval", "0"], ["--interval", "abc"]])
def test_main_turns_bad_arguments_into_check_error(capsys, argv):
    """透传参数写错（STYLE_SIGNALS_INPUTS_ARGS）也照打三行、退出码 0：链路照常往下走。"""
    rc = _main(argv, Clock("2026-09-23 20:31:17"), Seq(ALL))
    captured = capsys.readouterr()
    out = captured.out.splitlines()
    assert rc == 0 and out[-3] == "INPUTS_STATUS=CHECK_ERROR" and "参数错误" in out[-1]
    assert "usage" in captured.err


@pytest.mark.parametrize("text, expected", [("20:00", time(20, 0)), ("7:05", time(7, 5)), ("21:30", time(21, 30))])
def test_parse_hhmm(text, expected):
    assert wfi.parse_hhmm(text) == expected
