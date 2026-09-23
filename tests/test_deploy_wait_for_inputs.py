"""deploy/daily_signals/wait_for_inputs.py 单测（2026-09-23 立；不连库：时钟、sleep、日历、查询、哨兵全部注入）。

背景：2026-09-23 起本项目 15 个输入指数由 data_manager 夜间作业（WSL2 20:00 起跑、约 20:02 结束）写入
`stock_selector.index_daily`，本链路只读。runner 第 0 步（办公室模式）用这个脚本等当日 15 码到齐：
没齐每 5 分钟再查、最迟等到 21:30（手工早跑时由 --max-wait 截住），到齐后跑同族共动性哨兵，
结果以最后五行交给 runner。

钉住的命题：
* 期望信号日 T 按**交易日历**算（节假日不等）；日历缺某天 → 那天按周一至周五推断，并在结果里注明；
* 到齐判据 = 办公室回函 01 §7.3 原样（T 日、这 15 码、close 非空的行数 = 码数）；
* 只在「T 是今天、还没到截止」时轮询，最后一轮恰在截止时刻；截止 = min(当天 21:30, 开跑 + max-wait)；
  开跑已过截止 / --once / T 是过去某天 → 只查一次；
* 单轮出错下一轮再查；重试也不会好的错误（依赖缺失 / 配置读不到 / SQL 被拒）立即 CHECK_ERROR；
* 同族共动性哨兵只判 T、只有 CRITICAL 算数（WARN 不阻断）；没到齐不跑；哨兵自身出错 → CHECK_ERROR；
* 输出契约：最后五行 INPUTS_STATUS / DAY / REASON / SENTINEL / SENTINEL_DETAIL，退出码恒为 0；
* 当前时刻按 Asia/Shanghai 取，与本机时区设置无关。
"""
import importlib.util
import inspect
import re
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.errors
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
CLEAN_DETAIL = "2026-09-23 同族共动性：15 码日收益无 CRITICAL / WARN"


def test_calendar_fixture_is_the_probe():
    assert len(CAL) == 25 and min(CAL) == date(2026, 9, 18) and max(CAL) == date(2026, 10, 12)
    assert [d.isoformat() for d, b in sorted(CAL.items()) if not b and d.weekday() < 5] == [
        "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]


def test_defaults_follow_office_recommendation():
    """办公室回函 01 §7.4：20:00 夜间作业起跑、21:30 还不齐就照算、每 5 分钟查一次。命令行默认值同源。"""
    assert (wfi.DEFAULT_READY_FROM, wfi.DEFAULT_DEADLINE, wfi.DEFAULT_INTERVAL) == ("20:00", "21:30", 300)
    args = wfi.build_parser().parse_args([])
    assert (args.ready_from, args.deadline, args.interval) == (time(20, 0), time(21, 30), 300)
    assert args.max_wait == wfi.DEFAULT_MAX_WAIT and not args.once and args.accept_sentinel is None


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


def sentinel(critical=(), warn=(), judged=15):
    """哨兵桩：返回 (CRITICAL 明细, WARN 明细, T 有日收益的码数)，或照抛异常实例。"""
    return Seq((list(critical), list(warn), judged))


def run(clock, fetch, *, calendar=None, once=False, interval=300, deadline=DEADLINE, max_wait=None,
        check=None, accept=None):
    logs: list[str] = []

    def log(line):
        logs.append(line)
        clock.events.append(("log", line))

    result = wfi.wait_for_inputs(
        CODES, now_fn=clock, sleep_fn=clock.sleep,
        load_calendar=calendar if calendar is not None else RangeLoader(CAL),
        fetch_present=fetch, check_sentinel=check if check is not None else sentinel(),
        ready_from=READY, deadline=deadline, interval=interval,
        max_wait=wfi.DEFAULT_MAX_WAIT if max_wait is None else max_wait, once=once,
        accept_sentinel=accept, log=log)
    return result, logs


def test_all_present_on_first_check_is_ok_without_sleeping():
    clock, fetch = Clock("2026-09-23 20:31:17"), Seq(ALL)
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("OK", date(2026, 9, 23))
    assert res.reason == "2026-09-23 15 码到齐（等 0 秒）"
    assert (res.sentinel, res.sentinel_detail) == ("CLEAN", CLEAN_DETAIL)
    assert clock.slept == [] and fetch.calls == [(date(2026, 9, 23), CODES)]


def test_arrival_on_third_check_reports_seconds_waited():
    clock, fetch = Clock("2026-09-23 20:30:00"), Seq(set(), set(CODES[:12]), ALL)
    res, _ = run(clock, fetch)
    assert (res.status, res.reason) == ("OK", "2026-09-23 15 码到齐（等 600 秒）")
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
    assert (res.status, res.reason) == ("OK", "2026-09-23 15 码到齐（等 300 秒）")
    assert any("查询出错" in line and "OSError: connection reset" in line for line in logs)


def test_connection_error_is_retried_next_round():
    """连接类错误（psycopg2.OperationalError：连不上、超时、断线）下一轮再查。"""
    clock, fetch = Clock("2026-09-23 20:30:00"), Seq(psycopg2.OperationalError("could not connect"), ALL)
    res, _ = run(clock, fetch)
    assert res.status == "OK" and len(fetch.calls) == 2 and clock.slept == [300]


def test_error_until_deadline_is_check_error_in_one_line():
    clock = Clock("2026-09-23 21:20:00")
    fetch = Seq(RuntimeError("could not connect\n\tIs the server running?"))
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("CHECK_ERROR", date(2026, 9, 23))
    assert res.reason == "2026-09-23：RuntimeError: could not connect Is the server running?"
    assert clock.slept == [300, 300] and len(fetch.calls) == 3


@pytest.mark.parametrize("exc", [
    psycopg2.errors.InsufficientPrivilege("permission denied for table index_daily"),
    psycopg2.errors.UndefinedTable('relation "stock_selector.index_daily" does not exist'),
    psycopg2.ProgrammingError("syntax error"),
    ImportError("No module named 'psycopg2'"),
    wfi.ConfigError("读不到数据库配置：FileNotFoundError: config/settings.yaml 不存在"),
], ids=["no-privilege", "no-table", "programming", "import", "config"])
def test_permanent_error_stops_at_once(exc):
    """重试也不会好的错误（SQL 被拒 / 依赖缺失 / 配置读不到）：立即 CHECK_ERROR，不空等一小时。"""
    clock, fetch = Clock("2026-09-23 20:31:17"), Seq(exc)
    res, logs = run(clock, fetch)
    assert res.status == "CHECK_ERROR" and len(fetch.calls) == 1 and clock.slept == []
    assert res.reason.startswith(f"2026-09-23：{type(exc).__name__}: ")
    assert res.reason.endswith("（不可重试的错误）")
    assert any("不再等（不可重试的错误）" in line for line in logs)


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
    """信号日不是今天：那一晚的夜间作业早跑完了，等也等不来——只查一次。否则白天起跑会一路等到当晚 21:30。"""
    clock, fetch = Clock(start), Seq(set())
    res, _ = run(clock, fetch)
    assert (res.status, res.day) == ("LATE", date.fromisoformat(day))
    assert clock.slept == [] and fetch.calls == [(date.fromisoformat(day), CODES)]


def test_max_wait_caps_an_early_manual_start():
    """20:00 手工起跑、数据一直不来：截止 = min(21:30, 开跑 + max-wait)。这里 max-wait 3000 秒 → 20:50 报 LATE，
    而不是一路等到 21:30 被 runner 的兜底 timeout 杀掉（被杀就没有结果了）。"""
    clock, fetch = Clock("2026-09-23 20:00:00"), Seq(set())
    res, logs = run(clock, fetch, max_wait=3000)
    assert res.status == "LATE" and res.reason.startswith("2026-09-23 截至 20:50 仍缺 15 码：")
    assert clock.slept == [300] * 10 and clock.now == datetime(2026, 9, 23, 20, 50)
    assert "最迟等到 20:50:00（--max-wait 3000 秒）" in logs[0]
    assert "不再等（已等满 --max-wait 3000 秒）" in logs[-1]


def test_default_max_wait_from_eight_pm():
    """默认 max-wait：20:00 起跑最迟等到 21:10:40（= 4240 秒），最后一轮恰在那一刻。"""
    clock, fetch = Clock("2026-09-23 20:00:00"), Seq(set())
    res, _ = run(clock, fetch)
    assert res.status == "LATE" and res.reason.startswith("2026-09-23 截至 21:10 仍缺 15 码：")
    assert clock.slept == [300] * 14 + [40] and clock.now == datetime(2026, 9, 23, 21, 10, 40)


def test_timer_start_is_not_capped_by_max_wait():
    """定时器 20:30 起跑：开跑 + max-wait 在 21:30 之后，截止仍是 21:30。"""
    clock, fetch = Clock("2026-09-23 20:30:00"), Seq(set())
    res, logs = run(clock, fetch)
    assert clock.now == datetime(2026, 9, 23, 21, 30) and res.reason.startswith("2026-09-23 截至 21:30 ")
    assert logs[0].endswith("每 300 秒查一次，最迟等到 21:30")


def test_worst_round_query_count_matches_budget_constant():
    """时间预算按「一轮最多几次查询」算（QUERIES_PER_ROUND_MAX，runner 兜底判例用它）。最坏的一轮：日历还没读到
    要重读 + 到齐查询 + 到齐后跑哨兵 = 三次；常量写小了预算就是假的。"""
    loader, fetch, check = RangeLoader(OSError("calendar down")), Seq(ALL), sentinel()
    res, _ = run(Clock("2026-09-23 21:30:00"), fetch, calendar=loader, check=check)
    queries = len(loader.calls) + len(fetch.calls) + len(check.calls)
    assert res.status == "OK" and queries == 3 and queries <= wfi.QUERIES_PER_ROUND_MAX


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
    assert res.reason == "2026-09-30 15 码到齐（等 0 秒）"


def test_calendar_gap_is_noted_in_result():
    res, _ = run(Clock("2027-01-04 20:31:00"), Seq(ALL))
    assert (res.status, res.day) == ("OK", date(2027, 1, 4))
    assert res.reason == "2027-01-04 15 码到齐（等 0 秒）；日历缺 2027-01-04，按工作日推断"


def test_calendar_gap_is_noted_in_late_and_check_error():
    late, _ = run(Clock("2027-01-04 21:30:00"), Seq(set()))
    assert late.status == "LATE" and late.reason.endswith("；日历缺 2027-01-04，按工作日推断")
    err, _ = run(Clock("2027-01-04 21:30:00"), Seq(OSError("x")))
    assert err.reason == "2027-01-04：OSError: x；日历缺 2027-01-04，按工作日推断"


def test_calendar_read_failure_falls_back_and_retries():
    """日历读不到：先按工作日推断信号日、照查数据，下一轮再读日历；读到了就按日历改正信号日。
    10-01（周四）按工作日会被当成交易日 → 查 10-01 查不到 → 第二轮日历读到 → 改查 09-30。"""
    clock = Clock("2026-10-01 20:31:00")
    loader, fetch = RangeLoader(OSError("calendar down"), CAL), Seq(set(), ALL)
    res, logs = run(clock, fetch, calendar=loader)
    assert (res.status, res.day) == ("OK", date(2026, 9, 30))
    assert [c[0] for c in fetch.calls] == [date(2026, 10, 1), date(2026, 9, 30)]
    assert len(loader.calls) == 2 and clock.slept == [300]
    assert res.reason == "2026-09-30 15 码到齐（等 300 秒）"
    assert any("日历读取失败" in line for line in logs)


def test_calendar_read_failure_is_noted_when_it_never_recovers():
    loader = RangeLoader(PermissionError("permission denied for schema data_manager"))
    res, _ = run(Clock("2026-09-23 20:31:00"), Seq(ALL), calendar=loader)
    assert res.status == "OK"
    assert res.reason == ("2026-09-23 15 码到齐（等 0 秒）；日历读取失败（PermissionError: "
                          "permission denied for schema data_manager），按工作日推断")


def test_permanent_calendar_error_is_not_reread():
    """日历读取遇到重试也不会好的错误（无权限）：按工作日推断照用，后面几轮不再重读日历。"""
    clock = Clock("2026-09-23 20:31:00")
    loader = RangeLoader(psycopg2.errors.InsufficientPrivilege("permission denied for schema data_manager"), CAL)
    fetch = Seq(set(), set(), ALL)
    res, _ = run(clock, fetch, calendar=loader)
    assert res.status == "OK" and len(loader.calls) == 1 and len(fetch.calls) == 3
    assert res.reason.endswith("；日历读取失败（InsufficientPrivilege: permission denied for schema data_manager），"
                               "按工作日推断")


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


# ── 同族共动性哨兵（到齐后才跑，只判 T，只有 CRITICAL 算数）─────────────────────────

def test_sentinel_runs_once_on_signal_day_after_arrival():
    check = sentinel()
    res, _ = run(Clock("2026-09-23 20:30:00"), Seq(set(), ALL), check=check)
    assert check.calls == [(date(2026, 9, 23), CODES)]
    assert (res.status, res.sentinel, res.sentinel_detail) == ("OK", "CLEAN", CLEAN_DETAIL)


CRIT = "CRITICAL 2000pair 对内价差 27.07pp （932409.CSI +16.02% vs 932408.CSI -11.05%，判据 ≥8pp）"
WARN = "WARN 300pair 对内价差 6.50pp （判据 ≥6pp，需人工复核）"


def test_sentinel_critical_is_reported():
    """CRITICAL：到齐照报 OK，哨兵报 CRITICAL + 明细——阻断由 runner 做（记 OFFICE_SUSPECT、信号重算前中止）。"""
    res, _ = run(Clock("2026-09-23 20:31:17"), Seq(ALL), check=sentinel(critical=[CRIT], warn=[WARN]))
    assert (res.status, res.sentinel) == ("OK", "CRITICAL")
    assert res.sentinel_detail == f"2026-09-23：{CRIT}"


def test_sentinel_warn_only_is_warn_not_clean():
    """有 WARN 没 CRITICAL：报 WARN（不阻断，同 08-24 口径），CLEAN 只留给零发现；明细 = WARN 明细。"""
    res, _ = run(Clock("2026-09-23 20:31:17"), Seq(ALL), check=sentinel(warn=[WARN]))
    assert (res.status, res.sentinel, res.sentinel_detail) == ("OK", "WARN", f"2026-09-23：{WARN}")


def test_sentinel_error_after_arrival_is_unchecked_not_check_error():
    """到齐了、哨兵没判成（出错 / T 算不出日收益）：到齐照报 OK，哨兵 SKIPPED + 没判成的原因——runner 记
    OFFICE_UNCHECKED 照常往下走；CHECK_ERROR 只留给到齐检查本身出错。"""
    res, logs = run(Clock("2026-09-23 20:31:17"), Seq(ALL), check=Seq(RuntimeError("boom")))
    assert (res.status, res.reason, res.sentinel) == ("OK", "2026-09-23 15 码到齐（等 0 秒）", "SKIPPED")
    assert res.sentinel_detail == "RuntimeError: boom（15 码已到齐，本日数据未经同族共动性检查）"
    assert any("同族哨兵没判成" in line for line in logs)


@pytest.mark.parametrize("fetch, detail", [
    (Seq(set(CODES[:14])), "输入未到齐，未做同族哨兵"),
    (Seq(OSError("x")), "到齐检查出错，未做同族哨兵"),
], ids=["late", "check-error"])
def test_sentinel_skipped_when_not_arrived(fetch, detail):
    check = sentinel(critical=[CRIT])
    res, _ = run(Clock("2026-09-23 21:30:00"), fetch, check=check)
    assert check.calls == [] and (res.sentinel, res.sentinel_detail) == ("SKIPPED", detail)


@pytest.mark.parametrize("check, accept, sentinel_value, detail", [
    (sentinel(critical=[CRIT]), None, "CRITICAL", f"2027-01-04：{CRIT}"),
    (sentinel(critical=[CRIT]), date(2027, 1, 4), "ACCEPTED",
     f"2027-01-04：{CRIT}（已按 --accept-sentinel 2027-01-04 人工放行）"),
    (sentinel(warn=[WARN]), None, "WARN", f"2027-01-04：{WARN}"),
    (Seq(RuntimeError("boom")), None, "SKIPPED", "RuntimeError: boom（15 码已到齐，本日数据未经同族共动性检查）"),
], ids=["critical", "accepted", "warn", "unchecked"])
def test_calendar_note_follows_sentinel_detail(check, accept, sentinel_value, detail):
    """runner 拿哨兵明细当原因（OFFICE_SUSPECT / ACCEPTED / OK_WARN / UNCHECKED）：日历注记要跟在明细后面，别丢。"""
    res, _ = run(Clock("2027-01-04 22:10:00"), Seq(ALL), check=check, accept=accept)
    assert res.sentinel == sentinel_value
    assert res.sentinel_detail == f"{detail}；日历缺 2027-01-04，按工作日推断"


# ── 人工放行：--accept-sentinel <T>（只对那一天、只对 CRITICAL 生效）──────────────────────────

ACCEPTED_DETAIL = f"2026-09-23：{CRIT}（已按 --accept-sentinel 2026-09-23 人工放行）"


def test_accept_sentinel_releases_critical_on_signal_day():
    """人工核实是真实行情后放行：日期 = T、哨兵 CRITICAL → ACCEPTED，明细 = 原 CRITICAL 明细 + 放行注记，照常往下走。"""
    res, logs = run(Clock("2026-09-23 22:10:00"), Seq(ALL), check=sentinel(critical=[CRIT]),
                    accept=date(2026, 9, 23))
    assert (res.status, res.sentinel, res.sentinel_detail) == ("OK", "ACCEPTED", ACCEPTED_DETAIL)
    assert sum("人工放行" in line for line in logs) == 1


def test_accept_sentinel_for_another_day_does_not_release():
    """放行日期不是本次信号日：不生效，仍按 CRITICAL（放的是哪一天必须说清，别让旧的放行参数一路放下去）。"""
    res, logs = run(Clock("2026-09-23 22:10:00"), Seq(ALL), check=sentinel(critical=[CRIT]),
                    accept=date(2026, 9, 22))
    assert (res.sentinel, res.sentinel_detail) == ("CRITICAL", f"2026-09-23：{CRIT}")
    unused = [line for line in logs if "--accept-sentinel 2026-09-22" in line]
    assert len(unused) == 1 and "不是本次信号日 2026-09-23" in unused[0] and "仍按 CRITICAL 处理" in unused[0]


def test_accept_sentinel_unused_when_clean():
    res, logs = run(Clock("2026-09-23 22:10:00"), Seq(ALL), accept=date(2026, 9, 23))
    assert (res.sentinel, res.sentinel_detail) == ("CLEAN", CLEAN_DETAIL)
    unused = [line for line in logs if "--accept-sentinel 2026-09-23" in line]
    assert len(unused) == 1 and "没用上" in unused[0]


def test_accept_sentinel_unused_when_only_warn():
    res, logs = run(Clock("2026-09-23 22:10:00"), Seq(ALL), check=sentinel(warn=[WARN]), accept=date(2026, 9, 23))
    assert res.sentinel == "WARN"
    unused = [line for line in logs if "--accept-sentinel 2026-09-23" in line]
    assert len(unused) == 1 and "无 CRITICAL，无需放行" in unused[0]


def test_accept_sentinel_unused_when_not_arrived():
    check = sentinel(critical=[CRIT])
    res, logs = run(Clock("2026-09-23 22:10:00"), Seq(set()), check=check, accept=date(2026, 9, 23))
    assert (res.status, res.sentinel) == ("LATE", "SKIPPED") and check.calls == []
    assert sum("--accept-sentinel 2026-09-23" in line and "没用上" in line for line in logs) == 1


def test_no_accept_means_no_accept_log():
    _, logs = run(Clock("2026-09-23 22:10:00"), Seq(ALL), check=sentinel(critical=[CRIT]))
    assert not any("--accept-sentinel" in line for line in logs)


def _sentinel_rows(returns=None, *, day=date(2026, 9, 23), prev=date(2026, 9, 22)):
    """15 码在 prev 收 100、在 day 收 100×(1+r)（Decimal，同真库）；r 缺省 = 同族齐涨 1%，r=0 → 与前值逐位相等。"""
    rows = []
    for code in CODES:
        r = (returns or {}).get(code, 0.01)
        rows += [(code, prev, Decimal("100")), (code, day, Decimal("100") * (1 + Decimal(str(r))))]
    return rows


def _sentinel_check(rows, day=date(2026, 9, 23)):
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        return rows

    return wfi.sentinel_check(day, CODES, query=query), calls


def test_sentinel_check_clean_when_family_moves_together():
    (critical, warn, judged), _ = _sentinel_check(_sentinel_rows())
    assert (critical, warn, judged) == ([], [], 15)


def test_sentinel_check_pair_spread_is_critical():
    """规则 5：2000 对成长 +16% / 价值 −11%，对内价差 27pp ≥ 8pp（2026-06-16 那起事故的形状）。"""
    (critical, warn, _), _ = _sentinel_check(_sentinel_rows({"932409.CSI": 0.16, "932408.CSI": -0.11}))
    assert len(critical) == 1 and critical[0].startswith("CRITICAL 2000pair 对内价差 27.00pp") and warn == []


def test_sentinel_check_frozen_legs_are_critical():
    """规则 6：五条中信风格腿收盘价与前值逐位相等、同族齐涨 1%（2026-08-03 那起事故的形状）。"""
    frozen = {code: 0.0 for code in CODES[:5]}
    (critical, _, judged), _ = _sentinel_check(_sentinel_rows(frozen))
    assert judged == 15 and len(critical) == 1
    assert critical[0].startswith("CRITICAL 序列冻结：5 个代码收盘价与前值逐位相等")


def test_sentinel_check_warn_is_not_critical():
    (critical, warn, _), _ = _sentinel_check(_sentinel_rows({"000918.CSI": 0.075}))
    assert critical == [] and len(warn) == 1 and warn[0].startswith("WARN 300pair 对内价差 6.50pp")


def test_sentinel_check_judges_only_the_signal_day():
    """前一天有 CRITICAL（2000 对跳飞）、T 当天正常：只判 T，不报。"""
    pp, prev, day = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)
    rows = []
    for code in CODES:
        p1 = {"932409.CSI": Decimal("117"), "932408.CSI": Decimal("90")}.get(code, Decimal("101"))
        rows += [(code, pp, Decimal("100")), (code, prev, p1), (code, day, p1 * Decimal("1.01"))]
    (critical, warn, judged), _ = _sentinel_check(rows)
    assert (critical, warn, judged) == ([], [], 15)


def test_sentinel_check_needs_a_previous_day():
    """T 前 30 天内没有可比的前一交易日：算不出 T 的日收益 = 什么都没判，不能报 CLEAN。"""
    only_t = [(code, date(2026, 9, 23), Decimal("100")) for code in CODES]
    with pytest.raises(RuntimeError, match="算不出 2026-09-23 的日收益"):
        _sentinel_check(only_t)


def test_sentinel_check_does_not_round_closes():
    """收盘价按库里的精度算日收益，不先舍入：100.0040 → 100.0010 是真实小波动，舍入到分就成了「逐位相等」的假冻结。"""
    rows = _sentinel_rows()
    rows = [(c, d, Decimal("100.0040") if (c, d) == ("CI005917.WI", date(2026, 9, 22)) else
             Decimal("100.0010") if (c, d) == ("CI005917.WI", date(2026, 9, 23)) else v) for c, d, v in rows]
    (critical, warn, judged), _ = _sentinel_check(rows)
    assert (critical, warn, judged) == ([], [], 15)


def test_sentinel_check_null_close_is_missing_not_zero():
    """close 为 NULL 的行 = 那天没数，不是 0：当成 0 会算出 −100% 的日收益、把 2000 对打成 CRITICAL。"""
    rows = [(c, d, None if (c, d) == ("932409.CSI", date(2026, 9, 23)) else v) for c, d, v in _sentinel_rows()]
    (critical, warn, judged), _ = _sentinel_check(rows)
    assert (critical, warn, judged) == ([], [], 14)


def test_sentinel_query_reads_window_before_signal_day():
    _, calls = _sentinel_check(_sentinel_rows())
    (sql, params), = calls
    s = _norm(sql)
    assert "from {schema}.index_daily" in s
    assert "index_code = any(%s)" in s and "trade_date between %s and %s" in s
    assert params == (CODES, date(2026, 9, 23) - timedelta(days=wfi.SENTINEL_LOOKBACK_DAYS), date(2026, 9, 23))
    assert wfi.SENTINEL_LOOKBACK_DAYS >= 20   # 够跨过春节 / 国庆加两头周末找到前一交易日


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


# schema 故意不用生产默认名：实现里若把 stock_selector 写死，这里就露出来。
DB = {"host": "h", "port": 5432, "name": "db", "user": "u", "password": "p", "schema": "alt_schema"}


def test_connection_is_read_only_with_timeouts_and_keepalives():
    kw = wfi.connect_kwargs(DB)
    assert (kw["host"], kw["port"], kw["dbname"], kw["user"], kw["password"]) == ("h", 5432, "db", "u", "p")
    assert kw["connect_timeout"] == 10
    opts = kw["options"].split()
    assert "statement_timeout=60000" in opts and "default_transaction_read_only=on" in opts
    # 等数跨一个小时：中间网络设备掐断空闲连接时，libpq keepalive 让连接尽快报错而不是挂死
    assert (kw["keepalives"], kw["keepalives_idle"], kw["keepalives_interval"], kw["keepalives_count"]) == (1, 30, 10, 3)
    # 已发出去的数据迟迟收不到确认（对端静默消失）时，70 秒内断开，与「连库 10 + 单句 60」的单次查询上限对齐
    assert kw["tcp_user_timeout"] == 70000


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
    assert conns[0].executed == [("SELECT index_code FROM alt_schema.index_daily", (1,))]
    assert kwargs[0] == wfi.connect_kwargs(DB)


def test_run_query_closes_connection_on_error():
    conn = FakeConn(error=RuntimeError("statement timeout"))
    with pytest.raises(RuntimeError, match="statement timeout"):
        wfi.run_query("SELECT 1", (), connect=lambda **kw: conn, db_config=DB)
    assert conn.closed


def test_run_query_turns_config_failure_into_permanent_error(monkeypatch):
    """settings.yaml 缺失 / 缺字段：包成 ConfigError（重试也不会好）。"""
    def boom():
        raise FileNotFoundError("config/settings.yaml 不存在")

    monkeypatch.setattr("signals.common.config.load_db_config", boom)
    with pytest.raises(wfi.ConfigError, match="settings.yaml") as exc:
        wfi.run_query("SELECT 1", (), connect=lambda **kw: FakeConn())
    assert wfi.is_permanent(exc.value)


@pytest.mark.parametrize("exc, permanent", [
    (psycopg2.errors.InsufficientPrivilege("x"), True),
    (psycopg2.ProgrammingError("x"), True),
    (ImportError("x"), True),
    (wfi.ConfigError("x"), True),
    (psycopg2.OperationalError("x"), False),
    (psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"), False),
    (OSError("x"), False),
    (RuntimeError("x"), False),
])
def test_is_permanent(exc, permanent):
    assert wfi.is_permanent(exc) is permanent


# ── 时钟 ──────────────────────────────────────────────────────────────────────

def test_default_clock_is_shanghai_wall_time(monkeypatch):
    """本机时区设成 UTC 时，默认时钟照样给北京时间（定时器按 Asia/Shanghai 触发，截止也按它算）。"""
    monkeypatch.setenv("TZ", "UTC")
    time_mod.tzset()
    try:
        got = wfi.shanghai_now()
        ref = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        local = datetime.now()
    finally:
        monkeypatch.undo()
        time_mod.tzset()
    assert got.tzinfo is None and abs((ref - got).total_seconds()) < 5
    assert abs((got - local).total_seconds() - 8 * 3600) < 5
    assert inspect.signature(wfi.main).parameters["now_fn"].default is wfi.shanghai_now


def test_main_uses_real_sentinel_by_default():
    assert inspect.signature(wfi.main).parameters["check_sentinel"].default is wfi.sentinel_check


# ── main：输出契约 ─────────────────────────────────────────────────────────────

KEYS = ["INPUTS_STATUS", "INPUTS_DAY", "INPUTS_REASON", "INPUTS_SENTINEL", "INPUTS_SENTINEL_DETAIL"]


def _contract(out: list[str]) -> dict:
    """最后五行必须恰是五个键、按序；返回 {键: 值}。"""
    tail = out[-5:]
    assert [line.split("=", 1)[0] for line in tail] == KEYS, tail
    assert not any(line.startswith("INPUTS_") for line in out[:-5]), out
    return dict(line.split("=", 1) for line in tail)


def _main(argv, clock, fetch, calendar=None, check=None):
    return wfi.main(argv, now_fn=clock, sleep_fn=clock.sleep,
                    load_calendar=calendar if calendar is not None else RangeLoader(CAL),
                    fetch_present=fetch, check_sentinel=check if check is not None else sentinel())


def test_main_prints_contract_lines_last_and_exits_zero(capsys):
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), Seq(ALL))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and len(out) > 5   # 人读的进度行在前
    assert out[-5:] == ["INPUTS_STATUS=OK", "INPUTS_DAY=2026-09-23", "INPUTS_REASON=2026-09-23 15 码到齐（等 0 秒）",
                        "INPUTS_SENTINEL=CLEAN", f"INPUTS_SENTINEL_DETAIL={CLEAN_DETAIL}"]


def test_main_reports_sentinel_critical(capsys):
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), Seq(ALL), check=sentinel(critical=[CRIT]))
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and (got["INPUTS_STATUS"], got["INPUTS_SENTINEL"]) == ("OK", "CRITICAL")
    assert got["INPUTS_SENTINEL_DETAIL"] == f"2026-09-23：{CRIT}"


def test_main_passes_accept_sentinel_through(capsys):
    rc = _main(["--once", "--accept-sentinel", "2026-09-23"], Clock("2026-09-23 22:10:00"), Seq(ALL),
               check=sentinel(critical=[CRIT]))
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and (got["INPUTS_STATUS"], got["INPUTS_SENTINEL"]) == ("OK", "ACCEPTED")
    assert got["INPUTS_SENTINEL_DETAIL"] == ACCEPTED_DETAIL


@pytest.mark.parametrize("fetch, expected", [
    (Seq(set()), "LATE"),
    (Seq(OSError("x")), "CHECK_ERROR"),
], ids=["late", "check-error"])
def test_main_exit_code_is_zero_for_every_result(capsys, fetch, expected):
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), fetch)
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and (got["INPUTS_STATUS"], got["INPUTS_SENTINEL"]) == (expected, "SKIPPED")


def test_main_passes_options_through(capsys):
    """--interval / --deadline / --ready-from 生效：18:30 起算、18:40 截止、每 120 秒一轮。"""
    clock, fetch = Clock("2026-09-23 18:35:00"), Seq(set())
    rc = _main(["--ready-from", "18:30", "--deadline", "18:40", "--interval", "120"], clock, fetch)
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and (got["INPUTS_STATUS"], got["INPUTS_DAY"]) == ("LATE", "2026-09-23")
    assert clock.slept == [120, 120, 60] and len(fetch.calls) == 4


def test_main_passes_max_wait_through(capsys):
    clock, fetch = Clock("2026-09-23 20:31:17"), Seq(set())
    _main(["--max-wait", "600"], clock, fetch)
    got = _contract(capsys.readouterr().out.splitlines())
    assert got["INPUTS_REASON"].startswith("2026-09-23 截至 20:41 仍缺 15 码：")
    assert clock.slept == [300, 300]


def test_main_reads_codes_file_option(tmp_path, capsys):
    p = tmp_path / "codes.txt"
    p.write_text("000300.SH\n", encoding="utf-8")
    fetch, check = Seq({"000300.SH"}), sentinel(judged=1)
    _main(["--once", "--codes-file", str(p)], Clock("2026-09-23 20:31:17"), fetch, check=check)
    got = _contract(capsys.readouterr().out.splitlines())
    assert got["INPUTS_REASON"] == "2026-09-23 1 码到齐（等 0 秒）"
    assert fetch.calls == [(date(2026, 9, 23), ["000300.SH"])] and check.calls == [(date(2026, 9, 23), ["000300.SH"])]


def test_main_turns_unexpected_errors_into_check_error(tmp_path, capsys):
    rc = _main(["--codes-file", str(tmp_path / "missing.txt")], Clock("2026-09-23 20:31:17"), Seq(ALL))
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and (got["INPUTS_STATUS"], got["INPUTS_DAY"]) == ("CHECK_ERROR", "2026-09-23")
    assert got["INPUTS_REASON"].startswith("2026-09-23：FileNotFoundError: ")
    assert got["INPUTS_REASON"].endswith("按工作日推断")
    assert (got["INPUTS_SENTINEL"], got["INPUTS_SENTINEL_DETAIL"]) == ("SKIPPED", "等数脚本出错，未做同族哨兵")


def test_main_turns_runtime_error_into_check_error(capsys):
    """日历有误到一年内找不到交易日：expected_signal_day 抛 RuntimeError，main 顶层兜住。"""
    all_closed = {date(2026, 9, 23) - timedelta(days=i): False for i in range(400)}
    rc = _main(["--once"], Clock("2026-09-23 20:31:17"), Seq(ALL), calendar=Seq(all_closed))
    got = _contract(capsys.readouterr().out.splitlines())
    assert rc == 0 and got["INPUTS_STATUS"] == "CHECK_ERROR"
    assert got["INPUTS_REASON"].startswith("2026-09-23：RuntimeError: ") and "一年内找不到交易日" in got["INPUTS_REASON"]


@pytest.mark.parametrize("argv", [["--bogus"], ["--deadline", "25:99"], ["--ready-from", "8pm"],
                                  ["--interval", "0"], ["--interval", "abc"], ["--max-wait", "0"],
                                  ["--accept-sentinel", "2026/09/23"], ["--accept-sentinel", "yesterday"]])
def test_main_turns_bad_arguments_into_check_error(capsys, argv):
    """透传参数写错（STYLE_SIGNALS_INPUTS_ARGS）也照打五行、退出码 0：链路照常往下走。"""
    rc = _main(argv, Clock("2026-09-23 20:31:17"), Seq(ALL))
    captured = capsys.readouterr()
    got = _contract(captured.out.splitlines())
    assert rc == 0 and got["INPUTS_STATUS"] == "CHECK_ERROR" and "参数错误" in got["INPUTS_REASON"]
    assert "usage" in captured.err


@pytest.mark.parametrize("text, expected", [("20:00", time(20, 0)), ("7:05", time(7, 5)), ("21:30", time(21, 30))])
def test_parse_hhmm(text, expected):
    assert wfi.parse_hhmm(text) == expected
