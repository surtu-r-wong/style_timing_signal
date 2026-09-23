"""等办公室把当日输入指数写齐、再过同族共动性哨兵（只读 PG）—— run_daily_signals.sh 第 0 步（办公室模式）。

2026-09-23 起本项目 15 个输入指数（同目录 `input_codes.txt`）由 data_manager 夜间作业（WSL2
`dbm-daily-wss`，20:00 起跑、约 20:02 结束；补数前先重看前 5 个交易日纠错）写入
`stock_selector.index_daily`，本链路只读。依据：`data_manager/requests/2026-09-23-style-timing-signal-
index-daily-takeover/`（处置 + 回函 01 §7.3/§7.4）。本脚本回答两件事：**期望信号日 T 的 15 码到齐没有**；
到齐了，**T 这天的数过不过同族共动性哨兵**。

**期望信号日 T**：今天是 CN 交易日、且已过 `--ready-from`（默认 20:00，夜间作业起跑）→ 今天；
否则 = 严格早于今天的最近一个 CN 交易日。日历取 `data_manager.business_calendar`（`calendar_id='CN'`，
库内实测覆盖 2000-01-01..2026-12-31）。某天不在表里（如 2027 还没装载）→ 那天按周一至周五推断，
结果里注明「日历缺 <日期>，按工作日推断」；日历整个读不到 → 同样按工作日推断并注明，下一轮再读
（重试也不会好的错误就不再读）。不用 `topup_guard.expected_last_trading_day`：它只看工作日 + 15:30、
不认节假日（docstring 自承「节假日会高估一天」），拿来等数会在国庆每晚白等到截止。

**到齐判据**（回函 01 §7.3 原样）：T 日、这 15 码、`close` 非空的行数 = 码数。不看 `backfill_job`：
码都已在库的那天补数整天跳过、不写 job 行；纠错那步的账挂在同一个表名下，`last_cursor` 记的是 T-1。
纠错排在补数之前，所以 T 日一到齐，同一轮对 T-1..T-5 的改写也已做完。

**轮询**：只在「T 就是今天、且还没到截止」时每 `--interval`（默认 300）秒查一次，最后一轮恰在截止时刻。
截止 = min(当天 `--deadline`（默认 21:30），开跑时刻 + `--max-wait`（默认 4240 秒，来历见常量处））：
定时器 20:30 起跑时由 21:30 管着；20:00~20:15 手工起跑时由 max-wait 管着——到点报 LATE，不被 runner 的
兜底 timeout 杀掉（被杀就没有结果）。开跑已过截止、`--once`、或 T 是过去某天（白天手工重跑、开机补跑、
节假日照跑——那一晚的夜间作业早已跑完，等也等不来）→ 只查一次。每次查询新建连接（会话只读，带 libpq
keepalive），单次出错记下、下一轮再查，截止时仍出错 → CHECK_ERROR；**重试也不会好的错误**（依赖缺失、
数据库配置读不到、SQL 被拒——表不存在 / 无权限）立即 CHECK_ERROR，不空等到截止。每轮打印一行进度。

**同族共动性哨兵**（topup 事后审计的规则 5/6，2026-08-24 起；办公室模式下接回，判据与标定见
`family_sentinel.py`）：到齐后只读取 T 前 `SENTINEL_LOOKBACK_DAYS` 个自然日到 T 的收盘价，用
`family_sentinel.scan_findings(..., only_days={T})` 只判 T。只有 CRITICAL（对内价差 ≥8pp / 序列冻结）
算数——runner 记 OFFICE_SUSPECT、在信号重算之前中止；WARN 只提示不阻断（同旧口径，报 WARN，CLEAN 只留给
零发现）。判定范围是这 15 个
输入码：旧审计判新写入的那天时，纯风格 4 码要到 20:01 才由夜间作业写，审计时还不在库里，实际判的也是
这 15 码；办公室处置 §2.1 还记着 932400.CSI 晚间会取到前一日值——把它纳入只会因为生产信号不用的码
误拦生产。到齐后哨兵没判成（出错 / T 算不出日收益）→ 到齐照报 OK、哨兵报 SKIPPED 并写明原因，runner 记
OFFICE_UNCHECKED 照常往下走：本链路不写库，数据归办公室负责、它自有重看与覆盖检查，哨兵只是只读复查，复查设施
故障不该让当晚没信号。CHECK_ERROR 只留给到齐检查本身出错。**不接回「历史被改写」审计**：办公室每晚重看会合法
改写 T-1..T-5，那条规则会天天误报。人工核实是真实行情后，`--accept-sentinel T` 只放行那一天的 CRITICAL
（报 ACCEPTED，照常往下走）；日期不是 T 不生效，哨兵没判 CRITICAL 时用不上，两种情况都记一行日志。

**当前时刻**取 Asia/Shanghai（定时器按 Asia/Shanghai 触发，截止也按北京时间算），与本机时区设置无关。

**输出契约**（最后五行，runner 靠它解析；人读的进度行在前）：

    INPUTS_STATUS=OK|LATE|CHECK_ERROR
    INPUTS_DAY=YYYY-MM-DD
    INPUTS_REASON=<一行中文>
    INPUTS_SENTINEL=CLEAN|WARN|CRITICAL|ACCEPTED|SKIPPED   （CLEAN = 零发现；ACCEPTED = CRITICAL 已按
                                   --accept-sentinel 人工放行；没到齐 / 检查出错 / 哨兵没判成时 SKIPPED）
    INPUTS_SENTINEL_DETAIL=<一行：CRITICAL / WARN 明细，或没判的原因；有日历注记就跟在后面>

**退出码恒为 0**：三种结果链路都照常往下走（LATE / CHECK_ERROR 用库内已有数据照算），新鲜度由步骤 7
护栏兜底；哨兵 CRITICAL 的阻断由 runner 做。参数写错、码表读不到等一切异常也转成 CHECK_ERROR 五行。

用法：
    python3 deploy/daily_signals/wait_for_inputs.py           # 链路里的用法（runner 外包 timeout 4500）
    python3 deploy/daily_signals/wait_for_inputs.py --once    # 只查一次（手工重跑 / 只读冒烟）
    # 人工核实 2026-09-24 的 CRITICAL 是真实行情后放行重跑（经 runner 透传）：
    STYLE_SIGNALS_INPUTS_ARGS="--accept-sentinel 2026-09-24" deploy/daily_signals/run_daily_signals.sh
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SHANGHAI = ZoneInfo("Asia/Shanghai")
CODES_FILE = Path(__file__).resolve().with_name("input_codes.txt")
CALENDAR_ID = "CN"
CALENDAR_LOOKBACK_DAYS = 40   # 日历取 [今天 - 40, 今天]：最长连休（春节 + 两头周末）也就十来天
SENTINEL_LOOKBACK_DAYS = 30   # 哨兵取 [T - 30, T] 的收盘价：够找到 T 的前一交易日来算 T 的日收益
DEFAULT_READY_FROM = "20:00"  # 办公室夜间作业起跑
DEFAULT_DEADLINE = "21:30"    # 回函 01 §7.4：21:30 还不齐就用已有数据照算
DEFAULT_INTERVAL = 300        # 回函 01 §7.4：每 5 分钟查一次
CONNECT_TIMEOUT_S = 10        # 连库超时
STATEMENT_TIMEOUT_S = 60      # 单句超时
QUERIES_PER_ROUND_MAX = 3     # 一轮最多三次查询：重读日历（还没读到时）+ 到齐 + 同族哨兵（到齐那一轮）
# 等数时间上限 = runner 兜底 timeout 4500 − 截止那一轮最坏耗时 3 ×（连库 10 + 单句 60）− 余量 50。
# 到点报 LATE，不被兜底杀掉；这条不等式由 tests/test_deploy_daily_wiring.py 钉住。
DEFAULT_MAX_WAIT = 4240
MAX_ERROR_CHARS = 200         # 报错原文截断：原因会进状态文件与推送（企业微信超 2048 字节整条拒收）
TAG = "[wait_for_inputs]"

# 回函 01 §7.3 原样（多取码名，缺哪些要列出来）；{schema} 由 run_query 按 settings.yaml 填。
PRESENT_SQL = ("SELECT index_code FROM {schema}.index_daily "
               "WHERE trade_date = %s AND index_code = ANY(%s) AND close IS NOT NULL")
CALENDAR_SQL = ("SELECT calendar_date, is_business_day FROM data_manager.business_calendar "
                "WHERE calendar_id = %s AND calendar_date BETWEEN %s AND %s")
# 同 family_sentinel.load_closes 的取数形状，窗口只到 T。
SENTINEL_SQL = ("SELECT index_code, trade_date, close FROM {schema}.index_daily "
                "WHERE index_code = ANY(%s) AND trade_date BETWEEN %s AND %s")


class UsageError(ValueError):
    """参数写错（argparse 已把 usage 打到 stderr）。"""


class ConfigError(RuntimeError):
    """数据库配置读不到（settings.yaml 缺失 / 缺字段 / 格式坏了）：重试也不会好。"""


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def describe_error(exc: BaseException) -> str:
    """「类型: 消息」压成一行并截断——它会进 runner 日志、状态文件和推送。"""
    msg = one_line(str(exc))
    text = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS - 1] + "…"


def is_permanent(exc: BaseException) -> bool:
    """重试到截止也不会好的错误：依赖缺失（ImportError）、数据库配置读不到（ConfigError）、SQL 被拒
    （psycopg2.ProgrammingError：表不存在、无权限等）。连接类（OperationalError：连不上、超时、断线，
    含单句超时）不算，下一轮再查。psycopg2 若没加载过，异常就不可能是它的。"""
    if isinstance(exc, (ImportError, ConfigError)):
        return True
    psycopg2 = sys.modules.get("psycopg2")
    return psycopg2 is not None and isinstance(exc, psycopg2.ProgrammingError)


def shanghai_now() -> datetime:
    """当前北京时间（不带时区）：定时器按 Asia/Shanghai 触发、截止按北京时间算，与本机时区设置无关。"""
    return datetime.now(SHANGHAI).replace(tzinfo=None)


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


def iso_date(text: str) -> date:
    """'2026-09-24' → date。兼作 argparse 的 type=：写错了报参数错误。"""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期应为 YYYY-MM-DD，收到 {text!r}") from None


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
    status: str                    # OK / LATE / CHECK_ERROR
    day: date                      # 期望信号日 T
    reason: str                    # 一行中文
    sentinel: str = "SKIPPED"      # CLEAN / WARN / CRITICAL / ACCEPTED / SKIPPED
    sentinel_detail: str = ""      # 一行：CRITICAL / WARN 明细，或跳过的原因

    def lines(self) -> list[str]:
        return [f"INPUTS_STATUS={self.status}", f"INPUTS_DAY={self.day.isoformat()}",
                f"INPUTS_REASON={one_line(self.reason)}", f"INPUTS_SENTINEL={self.sentinel}",
                f"INPUTS_SENTINEL_DETAIL={one_line(self.sentinel_detail)}"]


def _with_note(text: str, note: str) -> str:
    return f"{text}；{note}" if note else text


def _mode(sd: SignalDay, today: date, once: bool, past_deadline: bool, interval: int,
          until: str) -> str:
    if once:
        return "只查一次（--once）"
    if sd.day != today:
        return "只查一次（信号日不是今天：那一晚的夜间作业早已跑完，等也等不来）"
    if past_deadline:
        return f"只查一次（已过截止 {until}）"
    return f"每 {interval} 秒查一次，最迟等到 {until}"


def _arrived(sd: SignalDay, codes: list[str], waited: int, note: str, check_sentinel, log,
             accept: date | None) -> Result:
    """到齐之后：同族共动性哨兵只判 T。CRITICAL → 报 CRITICAL（阻断由 runner 做）；若 accept == T（人工核实过的
    放行）→ 报 ACCEPTED、明细注明放行；只有 WARN → 报 WARN（不阻断）；零发现 → CLEAN；哨兵没判成（出错 /
    T 算不出日收益）→ 到齐照报 OK、哨兵 SKIPPED 写明原因（runner 记 OFFICE_UNCHECKED，照常往下走）。
    runner 拿哨兵明细当原因，所以日历注记也跟在明细后面。"""
    n = len(codes)
    reason = _with_note(f"{sd.day} {n} 码到齐（等 {waited} 秒）", note)
    try:
        critical, warn, judged = check_sentinel(sd.day, codes)
    except Exception as exc:
        detail = f"{describe_error(exc)}（{n} 码已到齐，本日数据未经同族共动性检查）"
        log(f"{TAG} 同族哨兵没判成：{detail}——不阻断，照常往下走")
        return Result("OK", sd.day, reason, "SKIPPED", _with_note(detail, note))
    if critical:
        detail = f"{sd.day}：" + "；".join(critical)
        if accept == sd.day:
            detail += f"（已按 --accept-sentinel {sd.day} 人工放行）"
            log(f"{TAG} 同族哨兵 CRITICAL，人工放行：{detail}")
            return Result("OK", sd.day, reason, "ACCEPTED", _with_note(detail, note))
        log(f"{TAG} 同族哨兵 CRITICAL：{detail}")
        return Result("OK", sd.day, reason, "CRITICAL", _with_note(detail, note))
    if warn:
        detail = f"{sd.day}：" + "；".join(warn)
        log(f"{TAG} 同族哨兵 WARN（不阻断）：{detail}")
        return Result("OK", sd.day, reason, "WARN", _with_note(detail, note))
    detail = f"{sd.day} 同族共动性：{judged} 码日收益无 CRITICAL / WARN"
    log(f"{TAG} 同族哨兵 CLEAN：{detail}")
    return Result("OK", sd.day, reason, "CLEAN", detail)


def wait_for_inputs(codes: list[str], *, now_fn, sleep_fn, load_calendar, fetch_present, check_sentinel,
                    ready_from: dtime, deadline: dtime, interval: int, max_wait: int, once: bool,
                    log, accept_sentinel: date | None = None) -> Result:
    """轮询到齐、再跑哨兵；给了 accept_sentinel（人工放行日）而没用上时记一行日志说明为什么。"""
    result = _poll(codes, now_fn=now_fn, sleep_fn=sleep_fn, load_calendar=load_calendar,
                   fetch_present=fetch_present, check_sentinel=check_sentinel, ready_from=ready_from,
                   deadline=deadline, interval=interval, max_wait=max_wait, once=once, log=log,
                   accept=accept_sentinel)
    if accept_sentinel is not None and result.sentinel != "ACCEPTED":
        if result.sentinel == "CRITICAL":
            why = f"它不是本次信号日 {result.day}，不生效，仍按 CRITICAL 处理"
        elif result.sentinel in ("CLEAN", "WARN"):
            why = "同族哨兵无 CRITICAL，无需放行"
        else:
            why = f"同族哨兵没判定（{result.sentinel_detail}）"
        log(f"{TAG} --accept-sentinel {accept_sentinel} 没用上：{why}")
    return result


def _poll(codes: list[str], *, now_fn, sleep_fn, load_calendar, fetch_present, check_sentinel,
          ready_from: dtime, deadline: dtime, interval: int, max_wait: int, once: bool,
          log, accept: date | None) -> Result:
    """轮询到齐、再跑哨兵（时钟、sleep、日历、到齐查询、哨兵全部注入，单测不连库）。返回结果，不打印契约五行。

    load_calendar(start, end) -> {日期: 是否交易日}；fetch_present(T, codes) -> 已到的码；
    check_sentinel(T, codes) -> (CRITICAL 明细, WARN 明细, T 有日收益的码数)。
    日历读到为止每轮都读（重试也不会好的错误就不再读），读到之前按工作日推断 T；T 固定按开跑时刻算
    （日历读到后可能被改正）。截止 = min(当天 deadline, 开跑 + max_wait)。
    """
    start = now_fn()
    today = start.date()
    day_deadline = datetime.combine(today, deadline)
    deadline_at = min(day_deadline, start + timedelta(seconds=max_wait))
    capped = deadline_at < day_deadline
    until = f"{deadline_at:%H:%M:%S}（--max-wait {max_wait} 秒）" if capped else f"{deadline:%H:%M}"
    n = len(codes)
    calendar: dict[date, bool] | None = None
    cal_error: str | None = None
    cal_given_up = False
    shown: date | None = None
    rnd = 0
    while True:
        rnd += 1
        parts: list[str] = []
        if calendar is None and not cal_given_up:
            try:
                calendar = load_calendar(today - timedelta(days=CALENDAR_LOOKBACK_DAYS), today)
                cal_error = None
            except Exception as exc:
                cal_error, cal_given_up = describe_error(exc), is_permanent(exc)
                parts.append(f"日历读取失败（{cal_error}），按工作日推断"
                             + ("，不再重读" if cal_given_up else ""))
        sd = expected_signal_day(start, ready_from, calendar if calendar is not None else {})
        note = calendar_note(sd, cal_error)
        polling = not once and sd.day == today
        if sd.day != shown:
            head = f"{TAG} {start:%F %T} 起：" if shown is None else f"{TAG} 日历读到了，"
            mode = _mode(sd, today, once, start >= deadline_at, interval, until)
            log(f"{head}期望信号日 {sd.day}（{sd.why}）；{n} 码；{mode}")
            shown = sd.day

        error, permanent = None, False
        arrived, missing = [], list(codes)
        try:
            present = fetch_present(sd.day, codes)
            arrived = [c for c in codes if c in present]
            missing = [c for c in codes if c not in present]
        except Exception as exc:
            error, permanent = describe_error(exc), is_permanent(exc)
        now = now_fn()
        stamp = f"{TAG} {now:%H:%M:%S} 第 {rnd} 轮："
        if error is None and len(arrived) == n:
            log(stamp + "；".join([*parts, f"{sd.day} {n}/{n} 到齐"]))
            return _arrived(sd, codes, int((now - start).total_seconds()), note, check_sentinel, log, accept)

        parts.append(f"查询出错 {error}" if error is not None
                     else f"{sd.day} 已到 {len(arrived)}/{n}，缺 {'、'.join(missing)}")
        remaining = (deadline_at - now).total_seconds()
        if permanent or not polling or remaining <= 0:
            stop = ("不可重试的错误" if permanent else "只查一次" if once
                    else "信号日不是今天" if sd.day != today
                    else f"已等满 --max-wait {max_wait} 秒" if capped else f"已到截止 {deadline:%H:%M}")
            log(stamp + "；".join([*parts, f"不再等（{stop}）"]))
            break
        wait = min(interval, remaining)
        log(stamp + "；".join([*parts, f"{wait:.0f} 秒后再查"]))
        sleep_fn(wait)

    if error is not None:
        text = f"{sd.day}：{error}" + ("（不可重试的错误）" if permanent else "")
        return Result("CHECK_ERROR", sd.day, _with_note(text, note), "SKIPPED", "到齐检查出错，未做同族哨兵")
    return Result("LATE", sd.day, _with_note(
        f"{sd.day} 截至 {now:%H:%M} 仍缺 {len(missing)} 码：{'、'.join(missing)}，按库内已有数据照算", note),
        "SKIPPED", "输入未到齐，未做同族哨兵")


# ── 取数（只读 PG）────────────────────────────────────────────────────────────

def connect_kwargs(db: dict) -> dict:
    """psycopg2.connect 的参数：连库 10 秒、单句 60 秒超时；会话只读——本链路只读，写不进去才放心；
    libpq keepalive——等数跨一个小时，中间网络设备掐断连接时尽快报错，不挂死到超时。"""
    return {"host": db["host"], "port": db["port"], "dbname": db["name"], "user": db["user"],
            "password": db["password"], "connect_timeout": CONNECT_TIMEOUT_S,
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT_S * 1000} -c default_transaction_read_only=on",
            "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
            # 已发出的数据迟迟收不到确认（对端静默消失）时 70 秒内断开，与单次查询上限「连库 10 + 单句 60」对齐
            "tcp_user_timeout": (CONNECT_TIMEOUT_S + STATEMENT_TIMEOUT_S) * 1000}


def run_query(sql: str, params: tuple, *, connect=None, db_config: dict | None = None) -> list[tuple]:
    """每次新建连接、查完即关（等数跨一个小时，不留长连接）。
    `{schema}` 换成 settings.yaml 的 schema（同 check_freshness / topup_guard 的写法）。
    配置读不到包成 ConfigError（重试也不会好）；import 失败原样抛 ImportError（同上）。"""
    if db_config is None:
        from signals.common.config import load_db_config
        try:
            db_config = load_db_config()
        except Exception as exc:
            raise ConfigError(f"读不到数据库配置：{describe_error(exc)}") from exc
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


def sentinel_check(day: date, codes: list[str], *, query=run_query) -> tuple[list[str], list[str], int]:
    """同族共动性哨兵只判 T → (CRITICAL 明细, WARN 明细, T 有日收益的码数)。

    判定复用 `family_sentinel.scan_findings(closes, only_days={T})`（topup 事后审计同一套判据）；取数是
    本脚本的只读连接（10 秒 / 60 秒超时 + keepalive），窗口 [T - SENTINEL_LOOKBACK_DAYS, T]。
    T 一个日收益都算不出（窗口里没有前一交易日）= 什么都没判，报错而不是报 CLEAN。
    """
    from deploy.daily_signals.family_sentinel import returns_by_day, scan_findings

    rows = query(SENTINEL_SQL, (list(codes), day - timedelta(days=SENTINEL_LOOKBACK_DAYS), day))
    closes: dict[str, dict[str, float | None]] = {}
    for code, trade_date, close in rows:
        closes.setdefault(code, {})[trade_date.isoformat()] = None if close is None else float(close)
    key = day.isoformat()
    judged = len(returns_by_day(closes).get(key, {}))
    if judged == 0:
        raise RuntimeError(f"算不出 {key} 的日收益（前 {SENTINEL_LOOKBACK_DAYS} 天内没有可比的前一交易日收盘价）")
    found = scan_findings(closes, only_days={key}).get(key, [])
    return ([f for f in found if f.startswith("CRITICAL")],
            [f for f in found if not f.startswith("CRITICAL")], judged)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="等办公室把当日输入指数写齐、再过同族共动性哨兵（只读 PG）；最后五行 INPUTS_* 是给 runner 的结果")
    ap.add_argument("--once", action="store_true", help="只查一次、不等（手工重跑 / 冒烟）")
    ap.add_argument("--interval", type=positive_int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒数，默认 {DEFAULT_INTERVAL}")
    ap.add_argument("--deadline", type=parse_hhmm, default=parse_hhmm(DEFAULT_DEADLINE),
                    help=f"当天截止时刻 HH:MM，默认 {DEFAULT_DEADLINE}；到点仍不齐 → LATE，照算")
    ap.add_argument("--max-wait", type=positive_int, default=DEFAULT_MAX_WAIT,
                    help=f"从开跑算起最多等多少秒，默认 {DEFAULT_MAX_WAIT}（截止取它与 --deadline 中早的那个）")
    ap.add_argument("--ready-from", type=parse_hhmm, default=parse_hhmm(DEFAULT_READY_FROM),
                    help=f"今天从几点起算作信号日 HH:MM，默认 {DEFAULT_READY_FROM}（办公室夜间作业起跑）")
    ap.add_argument("--accept-sentinel", type=iso_date, default=None, metavar="YYYY-MM-DD",
                    help="人工核实该信号日的同族哨兵 CRITICAL 是真实行情后放行（只对这一天生效，报 ACCEPTED）")
    ap.add_argument("--codes-file", default=str(CODES_FILE), help="码表，默认同目录 input_codes.txt")
    return ap


def _print(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None, *, now_fn=shanghai_now, sleep_fn=time.sleep,
         load_calendar=load_business_calendar, fetch_present=fetch_present_codes,
         check_sentinel=sentinel_check) -> int:
    """退出码恒为 0：任何异常（含参数写错）都转成 CHECK_ERROR 五行；--help 照常打印后退出。"""
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
            load_calendar=load_calendar, fetch_present=fetch_present, check_sentinel=check_sentinel,
            ready_from=args.ready_from, deadline=args.deadline, interval=args.interval,
            max_wait=args.max_wait, once=args.once, log=_print, accept_sentinel=args.accept_sentinel)
    except Exception as exc:
        day = expected_signal_day(start, parse_hhmm(DEFAULT_READY_FROM), {}).day
        result = Result("CHECK_ERROR", day, f"{day}：{describe_error(exc)}；信号日按工作日推断",
                        "SKIPPED", "等数脚本出错，未做同族哨兵")
    print("\n".join(result.lines()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
