"""等办公室把当日输入指数写齐（只读 PG）—— run_daily_signals.sh 第 0 步（办公室模式）。

2026-09-23 起本项目 15 个输入指数（同目录 `input_codes.txt`）由 data_manager 夜间作业（WSL2
`dbm-daily-wss`，20:00 起跑、约 20:02 结束；补数前先重看前 5 个交易日纠错）写入
`stock_selector.index_daily`，本链路只读。依据：`data_manager/requests/2026-09-23-style-timing-signal-
index-daily-takeover/`（处置 + 回函 01 §7.3/§7.4）。本脚本只回答一件事：**期望信号日 T 的 15 码到齐没有**。

**期望信号日 T**：今天是 CN 交易日、且已过 `--ready-from`（默认 20:00，夜间作业起跑）→ 今天；
否则 = 严格早于今天的最近一个 CN 交易日。日历取 `data_manager.business_calendar`（`calendar_id='CN'`，
库内实测覆盖 2000-01-01..2026-12-31）。某天不在表里（如 2027 还没装载）→ 那天按周一至周五推断，
结果里注明「日历缺 <日期>，按工作日推断」；日历整个读不到 → 同样按工作日推断并注明，下一轮再读。
不用 `topup_guard.expected_last_trading_day`：它只看工作日 + 15:30、不认节假日（docstring 自承「节假日
会高估一天」），拿来等数会在国庆每晚白等到截止。

**到齐判据**（回函 01 §7.3 原样）：T 日、这 15 码、`close` 非空的行数 = 码数。不看 `backfill_job`：
码都已在库的那天补数整天跳过、不写 job 行；纠错那步的账挂在同一个表名下，`last_cursor` 记的是 T-1。
纠错排在补数之前，所以 T 日一到齐，同一轮对 T-1..T-5 的改写也已做完。

**轮询**：只在「T 就是今天、且还没到 `--deadline`（默认 21:30）」时每 `--interval`（默认 300）秒查一次，
最后一轮恰在截止时刻；开跑已过截止、`--once`、或 T 是过去某天（白天手工重跑、开机补跑、节假日照跑——
那一晚的夜间作业早已跑完，等也等不来）→ 只查一次。每次查询新建连接（跨一个小时的长连接会被中间网络
设备掐断），会话只读；单次出错记下、下一轮再查，截止时仍出错 → CHECK_ERROR。每轮打印一行进度。

**输出契约**（最后三行，runner 靠它解析；人读的进度行在前）：

    INPUTS_STATUS=OK|LATE|CHECK_ERROR
    INPUTS_DAY=YYYY-MM-DD
    INPUTS_REASON=<一行中文>

**退出码恒为 0**：三种结果链路都照常往下走（LATE / CHECK_ERROR 用库内已有数据照算），新鲜度由步骤 7
护栏兜底；参数写错、码表读不到等一切异常也转成 CHECK_ERROR 三行。

用法：
    python3 deploy/daily_signals/wait_for_inputs.py           # 链路里的用法（runner 外包 timeout 4500）
    python3 deploy/daily_signals/wait_for_inputs.py --once    # 只查一次（手工重跑 / 只读冒烟）
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CODES_FILE = Path(__file__).resolve().with_name("input_codes.txt")
CALENDAR_ID = "CN"
CALENDAR_LOOKBACK_DAYS = 40   # 日历取 [今天 - 40, 今天]：最长连休（春节 + 两头周末）也就十来天
DEFAULT_READY_FROM = "20:00"  # 办公室夜间作业起跑
DEFAULT_DEADLINE = "21:30"    # 回函 01 §7.4：21:30 还不齐就用已有数据照算
DEFAULT_INTERVAL = 300        # 回函 01 §7.4：每 5 分钟查一次
MAX_ERROR_CHARS = 200         # 报错原文截断：原因会进状态文件与推送（企业微信超 2048 字节整条拒收）
TAG = "[wait_for_inputs]"

# 回函 01 §7.3 原样（多取码名，缺哪些要列出来）；{schema} 由 run_query 按 settings.yaml 填。
PRESENT_SQL = ("SELECT index_code FROM {schema}.index_daily "
               "WHERE trade_date = %s AND index_code = ANY(%s) AND close IS NOT NULL")
CALENDAR_SQL = ("SELECT calendar_date, is_business_day FROM data_manager.business_calendar "
                "WHERE calendar_id = %s AND calendar_date BETWEEN %s AND %s")


class UsageError(ValueError):
    """参数写错（argparse 已把 usage 打到 stderr）。"""


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def describe_error(exc: BaseException) -> str:
    """「类型: 消息」压成一行并截断——它会进 runner 日志、状态文件和推送。"""
    msg = one_line(str(exc))
    text = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS - 1] + "…"


# ── 码表与参数 ─────────────────────────────────────────────────────────────────

def load_codes(path: Path = CODES_FILE) -> list[str]:
    """一行一个码；`#` 开头为注释，空行忽略。空表与重复码直接报错：空表让「行数 = 码数」恒成立
    （0 = 0，永远「到齐」），重复码让它永不成立（主键 (index_code, trade_date) 下同码只有一行）。"""
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    codes = [line for line in lines if line and not line.startswith("#")]
    if not codes:
        raise ValueError(f"{path} 里没有任何码")
    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        raise ValueError(f"{path} 有重复码: {', '.join(dup)}")
    return codes


def parse_hhmm(text: str) -> dtime:
    """'20:00' → time(20, 0)。兼作 argparse 的 type=：写错了报参数错误。"""
    try:
        hh, mm = text.strip().split(":")
        return dtime(int(hh), int(mm))
    except ValueError:
        raise argparse.ArgumentTypeError(f"时刻应为 HH:MM，收到 {text!r}") from None


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"应为正整数秒，收到 {text!r}")
    return value


# ── 期望信号日 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalDay:
    day: date
    why: str                          # 人读：为什么是这天
    guessed: tuple[date, ...] = ()    # 日历里没有、按周一至周五推断过的日子（升序）


def expected_signal_day(now: datetime, ready_from: dtime, calendar: dict[date, bool]) -> SignalDay:
    """纯函数。今天是交易日且 now ≥ ready_from → 今天；否则严格早于今天的最近一个交易日。

    calendar：{日期: 是否交易日}；不在里面的日子按周一至周五推断，记进 guessed。
    ready_from 之前不必看今天是不是交易日（信号日反正在今天之前），今天也就不算「推断过」。
    """
    guessed: set[date] = set()

    def is_business_day(day: date) -> bool:
        if day in calendar:
            return calendar[day]
        guessed.add(day)
        return day.weekday() < 5

    today = now.date()
    if now.time() < ready_from:
        why = f"未到 {ready_from:%H:%M}"
    elif is_business_day(today):
        return SignalDay(today, f"今天是交易日且已过 {ready_from:%H:%M}", tuple(sorted(guessed)))
    else:
        why = "今天不是交易日"
    day = today
    for _ in range(366):
        day -= timedelta(days=1)
        if is_business_day(day):
            return SignalDay(day, f"{why}，取此前最近的交易日", tuple(sorted(guessed)))
    raise RuntimeError(f"{today} 之前一年内找不到交易日（日历有误？）")


def calendar_note(signal_day: SignalDay, calendar_error: str | None) -> str:
    """结果末尾的日历注记；日历齐全时为空串。"""
    if calendar_error is not None:
        return f"日历读取失败（{calendar_error}），按工作日推断"
    if signal_day.guessed:
        return f"日历缺 {'、'.join(d.isoformat() for d in signal_day.guessed)}，按工作日推断"
    return ""


# ── 轮询 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Result:
    status: str      # OK / LATE / CHECK_ERROR
    day: date        # 期望信号日 T
    reason: str      # 一行中文

    def lines(self) -> list[str]:
        return [f"INPUTS_STATUS={self.status}", f"INPUTS_DAY={self.day.isoformat()}",
                f"INPUTS_REASON={one_line(self.reason)}"]


def _with_note(text: str, note: str) -> str:
    return f"{text}；{note}" if note else text


def _mode(sd: SignalDay, today: date, once: bool, past_deadline: bool, interval: int,
          deadline: dtime) -> str:
    if once:
        return "只查一次（--once）"
    if sd.day != today:
        return "只查一次（信号日不是今天：那一晚的夜间作业早已跑完，等也等不来）"
    if past_deadline:
        return f"只查一次（已过截止 {deadline:%H:%M}）"
    return f"每 {interval} 秒查一次，最迟等到 {deadline:%H:%M}"


def wait_for_inputs(codes: list[str], *, now_fn, sleep_fn, load_calendar, fetch_present,
                    ready_from: dtime, deadline: dtime, interval: int, once: bool, log) -> Result:
    """轮询到齐（时钟、sleep、日历加载、到齐查询全部注入，单测不连库）。返回结果，不打印契约三行。

    load_calendar(start, end) -> {日期: 是否交易日}；fetch_present(T, codes) -> 已到的码。
    日历读到为止每轮都读，读到之前按工作日推断 T；T 固定按开跑时刻算（日历读到后可能被改正）。
    """
    start = now_fn()
    today = start.date()
    deadline_at = datetime.combine(today, deadline)
    n = len(codes)
    calendar: dict[date, bool] | None = None
    shown: date | None = None
    rnd = 0
    while True:
        rnd += 1
        parts: list[str] = []
        cal_error = None
        if calendar is None:
            try:
                calendar = load_calendar(today - timedelta(days=CALENDAR_LOOKBACK_DAYS), today)
            except Exception as exc:
                cal_error = describe_error(exc)
                parts.append(f"日历读取失败（{cal_error}），按工作日推断")
        sd = expected_signal_day(start, ready_from, calendar if calendar is not None else {})
        note = calendar_note(sd, cal_error)
        polling = not once and sd.day == today
        if sd.day != shown:
            head = f"{TAG} {start:%F %T} 起：" if shown is None else f"{TAG} 日历读到了，"
            mode = _mode(sd, today, once, start >= deadline_at, interval, deadline)
            log(f"{head}期望信号日 {sd.day}（{sd.why}）；{n} 码；{mode}")
            shown = sd.day

        error = None
        arrived, missing = [], list(codes)
        try:
            present = fetch_present(sd.day, codes)
            arrived = [c for c in codes if c in present]
            missing = [c for c in codes if c not in present]
        except Exception as exc:
            error = describe_error(exc)
        now = now_fn()
        stamp = f"{TAG} {now:%H:%M:%S} 第 {rnd} 轮："
        if error is None and len(arrived) == n:
            log(stamp + "；".join([*parts, f"{sd.day} {n}/{n} 到齐"]))
            waited = int((now - start).total_seconds())
            return Result("OK", sd.day, _with_note(f"办公室日更 {sd.day} {n} 码到齐（等 {waited} 秒）", note))

        parts.append(f"查询出错 {error}" if error is not None
                     else f"{sd.day} 已到 {len(arrived)}/{n}，缺 {'、'.join(missing)}")
        remaining = (deadline_at - now).total_seconds()
        if not polling or remaining <= 0:
            stop = ("只查一次" if once else "信号日不是今天" if sd.day != today
                    else f"已到截止 {deadline:%H:%M}")
            log(stamp + "；".join([*parts, f"不再等（{stop}）"]))
            break
        wait = min(interval, remaining)
        log(stamp + "；".join([*parts, f"{wait:.0f} 秒后再查"]))
        sleep_fn(wait)

    if error is not None:
        return Result("CHECK_ERROR", sd.day, _with_note(f"{sd.day} 到齐检查出错：{error}", note))
    return Result("LATE", sd.day, _with_note(
        f"{sd.day} 截至 {now:%H:%M} 仍缺 {len(missing)} 码：{'、'.join(missing)}，按库内已有数据照算", note))


# ── 取数（只读 PG）────────────────────────────────────────────────────────────

def connect_kwargs(db: dict) -> dict:
    """psycopg2.connect 的参数：连库 10 秒、单句 60 秒超时；会话只读——本链路只读，写不进去才放心。"""
    return {"host": db["host"], "port": db["port"], "dbname": db["name"], "user": db["user"],
            "password": db["password"], "connect_timeout": 10,
            "options": "-c statement_timeout=60000 -c default_transaction_read_only=on"}


def run_query(sql: str, params: tuple, *, connect=None, db_config: dict | None = None) -> list[tuple]:
    """每次新建连接、查完即关（等数跨一个小时，长连接会被中间网络设备掐断）。
    `{schema}` 换成 settings.yaml 的 schema（同 check_freshness / topup_guard 的写法）。"""
    if db_config is None:
        from signals.common.config import load_db_config
        db_config = load_db_config()
    if connect is None:
        import psycopg2
        connect = psycopg2.connect
    conn = connect(**connect_kwargs(db_config))
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace("{schema}", db_config["schema"]), params)
            return list(cur.fetchall())
    finally:
        conn.close()


def load_business_calendar(start: date, end: date, *, query=run_query) -> dict[date, bool]:
    """CN 交易日历 [start, end] → {日期: 是否交易日}。表里没有的日子不在返回里（由调用方按工作日推断）。"""
    return {day: bool(flag) for day, flag in query(CALENDAR_SQL, (CALENDAR_ID, start, end))}


def fetch_present_codes(day: date, codes: list[str], *, query=run_query) -> set[str]:
    """T 日已到（close 非空）的码。到齐 ⇔ 其中属于码表的码数 = 码表码数。"""
    return {row[0] for row in query(PRESENT_SQL, (day, list(codes)))}


# ── 入口 ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="等办公室把当日输入指数写齐（只读 PG）；最后三行 INPUTS_* 是给 runner 的结果")
    ap.add_argument("--once", action="store_true", help="只查一次、不等（手工重跑 / 冒烟）")
    ap.add_argument("--interval", type=positive_int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒数，默认 {DEFAULT_INTERVAL}")
    ap.add_argument("--deadline", type=parse_hhmm, default=parse_hhmm(DEFAULT_DEADLINE),
                    help=f"当天截止时刻 HH:MM，默认 {DEFAULT_DEADLINE}；到点仍不齐 → LATE，照算")
    ap.add_argument("--ready-from", type=parse_hhmm, default=parse_hhmm(DEFAULT_READY_FROM),
                    help=f"今天从几点起算作信号日 HH:MM，默认 {DEFAULT_READY_FROM}（办公室夜间作业起跑）")
    ap.add_argument("--codes-file", default=str(CODES_FILE), help="码表，默认同目录 input_codes.txt")
    return ap


def _print(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None, *, now_fn=datetime.now, sleep_fn=time.sleep,
         load_calendar=load_business_calendar, fetch_present=fetch_present_codes) -> int:
    """退出码恒为 0：任何异常（含参数写错）都转成 CHECK_ERROR 三行；--help 照常打印后退出。"""
    start = now_fn()
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            if exc.code in (0, None):
                raise
            raise UsageError(f"参数错误（exit {exc.code}），见上方 usage") from None
        result = wait_for_inputs(
            load_codes(Path(args.codes_file)), now_fn=now_fn, sleep_fn=sleep_fn,
            load_calendar=load_calendar, fetch_present=fetch_present, ready_from=args.ready_from,
            deadline=args.deadline, interval=args.interval, once=args.once, log=_print)
    except Exception as exc:
        day = expected_signal_day(start, parse_hhmm(DEFAULT_READY_FROM), {}).day
        result = Result("CHECK_ERROR", day,
                        f"{day} 到齐检查出错：{describe_error(exc)}；信号日按工作日推断")
    print("\n".join(result.lines()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
