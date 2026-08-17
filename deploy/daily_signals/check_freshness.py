"""日更信号链的产出护栏 + 状态文件写入器（只读 PG）。

**两条独立命题**（任一不过 → 非零退出 + 状态 JSON `result: "STALE"`）：

1. **不落后**：三条生产信号 CSV 与三份推荐持仓的末行日期，距 `index_daily` 最新交易日
   不得超过 `--max-lag` 个**交易日**（默认 1）。这条存在的理由是 2026-07-09~08-12 停更
   35 天无人发现（见 docs/plans/2026-08-12-project-review-and-priorities.md §3.1）。
2. **无缺口**：每份产出在自己的 `[首行, 末行]` 区间内，必须覆盖交易日历上的**每一个**
   交易日，且不得出现日历外的日期。这条是 2026-08-17 补的——命题 1 只看末行，
   **中间缺一天它看不见**：08-12/13 两天上游晚到（08-17 11:25 才回填入库），08-14 那晚
   重算时库里还没有，八份产出于是齐齐跳过两天；而末行仍是 08-14，护栏当晚报
   `max_lag=0`、`breaches: []` 一片绿。08-13 恰好是 equal_weight 的换仓日（pos 0→1），
   缺它会让持仓序列错判换仓时点，不是完整性洁癖。

交易日历取自 `index_daily` 中本项目输入码的 distinct trade_date（不用自然日，
避免周末/长假误报）。日历随库内容浮动这点是**有意的**：库里没有的天，产出缺它不算
产出的错，命题 2 只在「库有而产出没有」时报警——正是"产出没追平库"的语义。

上游自身的两个问题分级不同：
  * `calendar_days_behind_today` 超限 → `UPSTREAM_STALE`，**参与退出码**（阈值宽到
    7 自然日，长假窗口内放宽到 15，误报成本已被这两档吸收）
  * 近窗内缺工作日 → `UPSTREAM_GAP`，**只 WARN 不参与退出码**——按工作日推算必然把
    调休放假的工作日误判成缺口，且本项目对上游缺口没有处置权（08-12/13 就是上游
    自己回填补上的）。详见 `upstream_calendar_gaps`。

用法（由 run_daily_signals.sh 调用，也可手工跑）：
    python3 deploy/daily_signals/check_freshness.py            # 只检查，打印结论
    python3 deploy/daily_signals/check_freshness.py --status-file logs/daily_signals_status.json
    python3 deploy/daily_signals/check_freshness.py --failed-step signal_citic40d ...  # 记账失败，不连库
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 硬护栏对象：三条生产信号（backtest.baseline.SIGNALS 的三个路径）+ 三份推荐持仓。
GATED = {
    "hybrid20_confirmed": "output/hybrid20/confirmed_signal.csv",
    "citic40d": "output/citic40d/citic_style_signal_40d.csv",
    "equal_weight_20d40z": "output/equal_weight/equal_weight_signal_20d40z.csv",
    "recommended_hybrid20": "output/recommended/hybrid20_longflat.csv",
    "recommended_citic40d": "output/recommended/citic40d_longflat.csv",
    "recommended_equal_weight": "output/recommended/equal_weight_longflat.csv",
}
# 只报告不拦截：中间产物与非生产口径的参数变体。
INFORMATIONAL = {
    "hybrid20_growth_stability": "output/hybrid20/growth_stability_signal.csv",
    "equal_weight_5d20z": "output/equal_weight/equal_weight_signal_5d20z.csv",
}

# 上游冻结护栏：本项目产出与 index_daily 是同步的，上游一旦不动，
# 「产出 vs 上游」的比对就恒为 0 落后、恒报 OK —— 这正是本批立项要治的盲区
# （2026-07-09~08-12 停更 35 天无人发现）。所以还要单独盯上游自己。
UPSTREAM_MAX_LAG_DAYS = 7          # 自然日；平日 A 股最长连休（含调休）也到不了
UPSTREAM_HOLIDAY_MAX_LAG_DAYS = 15  # 已知长假窗口内放宽
UPSTREAM_GAP_LOOKBACK_WORKDAYS = 15  # 上游缺口只查近窗；更早的缺口属历史，归上游
GAP_SAMPLE = 10                     # 缺口明细进状态文件/日志的条数上限（别把状态文件撑爆）
# 固定日期的长假（月, 日）→（月, 日），含首尾。春节等农历假期日期逐年变，
# 由 --holiday-window / STYLE_SIGNALS_HOLIDAY_WINDOWS 显式补进来。
FIXED_HOLIDAY_WINDOWS = [
    ((1, 1), (1, 5)),      # 元旦
    ((5, 1), (5, 6)),      # 劳动节
    ((10, 1), (10, 9)),    # 国庆（含中秋叠加年份）
]


def dates_of(csv_path: Path) -> list[str]:
    """CSV 每一行的第一列（date），原序。文件缺失/只有表头时返回 []。"""
    if not csv_path.exists():
        return []
    days: list[str] = []
    with csv_path.open("r", encoding="utf-8") as fh:
        header = fh.readline()
        if not header:
            return []
        for line in fh:
            if line.strip():
                days.append(line.split(",", 1)[0].strip())
    return days


def last_date_of(csv_path: Path) -> str | None:
    """CSV 末行第一列（date）。文件缺失/只有表头时返回 None。"""
    days = dates_of(csv_path)
    return days[-1] if days else None


def coverage_gaps(days: list[str], file_dates: list[str]) -> tuple[list[str], list[str]]:
    """产出覆盖完整性（纯函数）。返回 (区间内缺失的交易日, 不在日历上的产出日期)。

    区间下沿取产出**自己的首行**而不是日历首日：各条信号 warmup 长度不同
    （citic40d 从 2010-04-02 起、hybrid20 从 2011-04-18 起、equal_weight 从
    2014-01-02 起），拿日历首日当下沿会把 warmup 期整段误报成缺口。

    上下沿用 min/max 而非 [0]/[-1]，这样文件万一乱序也算得对。
    """
    if not file_dates:
        return [], []
    have, cal = set(file_dates), set(days)
    first, last = min(file_dates), max(file_dates)
    missing = [d for d in days if first <= d <= last and d not in have]
    off_calendar = [d for d in file_dates if d not in cal]
    return missing, off_calendar


def load_calendar() -> tuple[list[str], str]:
    """本项目输入码在 index_daily 里的 distinct trade_date（升序）与最大值。"""
    import psycopg2

    from signals.common.config import load_db_config
    from signals.common.data_source import load_code_map

    codes = sorted(set(load_code_map().values()))
    db = load_db_config()
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["name"],
        user=db["user"], password=db["password"], connect_timeout=10,
        options="-c statement_timeout=60000",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT trade_date FROM {db['schema']}.index_daily "
                "WHERE index_code = ANY(%s) ORDER BY trade_date",
                (codes,),
            )
            days = [r[0].isoformat() for r in cur.fetchall()]
    finally:
        conn.close()
    if not days:
        raise RuntimeError("index_daily 中查不到本项目任何输入码的交易日")
    return days, days[-1]


def lag_trading_days(days: list[str], last: str | None) -> int | None:
    """last 距 days[-1] 的交易日距离；last 缺失或不在日历上返回 None。"""
    if last is None:
        return None
    try:
        return len(days) - 1 - days.index(last)
    except ValueError:
        return None


def parse_holiday_windows(raw: str | None) -> list[tuple[str, str]]:
    """'2027-02-06:2027-02-17,2028-01-25:2028-02-02' → [(start, end), ...]。"""
    windows: list[tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, _, end = chunk.partition(":")
        start, end = start.strip(), end.strip()
        if not start or not end:
            raise ValueError(f"假期窗口格式应为 YYYY-MM-DD:YYYY-MM-DD，收到 {chunk!r}")
        windows.append((start, end))
    return windows


def in_holiday_window(day: str, extra: list[tuple[str, str]] | None = None) -> bool:
    """day 是否落在已知假期窗口内（固定日期长假 + 显式传入的农历假期窗口）。"""
    for start, end in extra or []:
        if start <= day <= end:
            return True
    d = date.fromisoformat(day)
    for (m1, d1), (m2, d2) in FIXED_HOLIDAY_WINDOWS:
        if (m1, d1) <= (d.month, d.day) <= (m2, d2):
            return True
    return False


def check_upstream_freeze(
    upstream_max: str, today: str, extra_windows: list[tuple[str, str]] | None = None,
    max_days: int = UPSTREAM_MAX_LAG_DAYS,
    holiday_max_days: int = UPSTREAM_HOLIDAY_MAX_LAG_DAYS,
) -> tuple[int, str | None]:
    """上游自身是否冻结。返回 (落后自然日数, 违规说明或 None)。

    落在已知假期窗口内时放宽到 holiday_max_days —— 宁可在春节多报一次假警
    （一条 --holiday-window 即可消音），也不要在上游真冻结时保持沉默。
    """
    behind = (date.fromisoformat(today) - date.fromisoformat(upstream_max)).days
    limit = max_days
    holiday = in_holiday_window(today, extra_windows) or \
        in_holiday_window(upstream_max, extra_windows)
    if holiday:
        limit = holiday_max_days
    if behind > limit:
        window = "已知假期窗口" if holiday else "非假期窗口"
        return behind, (
            f"上游 index_daily 最新交易日 {upstream_max} 距今 {behind} 个自然日 "
            f"> {limit}（{window}）—— 上游可能已冻结")
    return behind, None


def workdays_between(start: str, end: str) -> list[str]:
    """[start, end] 内的工作日（周一~周五）升序。不含节假日日历。"""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def upstream_calendar_gaps(
    days: list[str], today: str,
    lookback_workdays: int = UPSTREAM_GAP_LOOKBACK_WORKDAYS,
    extra_windows: list[tuple[str, str]] | None = None,
) -> list[str]:
    """上游 `index_daily` 近窗内缺失的工作日（已排除已知假期窗口）。

    为什么这条只 WARN（对比 `check_upstream_freeze` 参与退出码）：
      * 按工作日推算必然把**调休放假**的工作日误判成缺口——已知长假由
        `in_holiday_window` 消音，边缘调休日每年还剩几天，硬失败就是几次假警报；
      * 本项目对上游缺口**没有处置权**：2026-08-12/13 这次就是上游自己在 08-17 11:25
        回填补上的，我们能做的只是「看见」并在下次重算时把产出补齐。

    上沿钉在 `min(库内最新交易日, 今天)`：比库内最新还新的日子属于「还没落库」，
    那是 `check_upstream_freeze`（冻结）的辖区，不是「中间缺口」。
    """
    if not days or lookback_workdays <= 0:
        return []
    end = min(days[-1], today)
    # 回溯 2 倍自然日足以覆盖 lookback_workdays 个工作日（15 工作日 ≈ 21 自然日）。
    start = (date.fromisoformat(end) - timedelta(days=lookback_workdays * 2)).isoformat()
    window = workdays_between(start, end)[-lookback_workdays:]
    have = set(days)
    return [d for d in window
            if d not in have and not in_holiday_window(d, extra_windows)]


def evaluate(days: list[str], root: Path, max_lag: int) -> tuple[dict, list[str], int]:
    """纯函数：给定交易日历与数据根目录，算出每份产出的落后天数与违规清单。

    与 PG 解耦，便于测试与演示（`--root` 指向任意 output 树的副本）。
    """
    files: dict[str, dict] = {}
    breaches: list[str] = []
    worst = 0
    for label, rel in list(GATED.items()) + list(INFORMATIONAL.items()):
        gated = label in GATED
        file_dates = dates_of(root / rel)
        last = file_dates[-1] if file_dates else None
        lag = lag_trading_days(days, last)
        missing, off_calendar = coverage_gaps(days, file_dates)
        files[label] = {
            "path": rel, "last_date": last,
            "lag_trading_days": lag, "gated": gated,
            "first_date": min(file_dates) if file_dates else None,
            "row_count": len(file_dates),
            "gap_count": len(missing),
            "gap_dates": missing[:GAP_SAMPLE],
            "off_calendar_count": len(off_calendar),
            "off_calendar_dates": off_calendar[:GAP_SAMPLE],
        }
        if not gated:
            continue
        if last is None:
            breaches.append(f"{label}: 文件缺失或为空 ({rel})")
            continue
        if lag is None:
            # 末行都不在日历上 = 整个文件口径可疑，先修这个；不再叠加缺口条目。
            breaches.append(f"{label}: 末行日期 {last} 不在 index_daily 交易日历上")
            continue
        if lag > max_lag:
            breaches.append(f"{label}: 末行 {last}，落后上游 {lag} 个交易日 > {max_lag}")
        worst = max(worst, lag)
        if missing:
            more = f" 等 {len(missing)} 天" if len(missing) > GAP_SAMPLE else ""
            breaches.append(
                f"{label}: 区间 {min(file_dates)}..{last} 内缺 {len(missing)} 个交易日"
                f"（{'、'.join(missing[:GAP_SAMPLE])}{more}）")
        if off_calendar:
            more = f" 等 {len(off_calendar)} 天" if len(off_calendar) > GAP_SAMPLE else ""
            breaches.append(
                f"{label}: {len(off_calendar)} 个产出日期不在 index_daily 交易日历上"
                f"（{'、'.join(off_calendar[:GAP_SAMPLE])}{more}）")
    return files, breaches, worst


def build_report(max_lag: int, root: Path | None = None,
                 holiday_windows: list[tuple[str, str]] | None = None,
                 upstream_max_days: int = UPSTREAM_MAX_LAG_DAYS,
                 upstream_gap_lookback: int = UPSTREAM_GAP_LOOKBACK_WORKDAYS,
                 ) -> tuple[dict, bool]:
    days, upstream_max = load_calendar()
    today = date.today().isoformat()
    behind_today, upstream_breach = check_upstream_freeze(
        upstream_max, today, holiday_windows, max_days=upstream_max_days)
    upstream_gaps = upstream_calendar_gaps(
        days, today, lookback_workdays=upstream_gap_lookback,
        extra_windows=holiday_windows)
    files, breaches, worst = evaluate(days, root or ROOT, max_lag)

    report = {
        "data_root": str(root or ROOT),
        "upstream": {
            "table": "stock_selector.index_daily",
            "max_trade_date": upstream_max,
            "calendar_days_behind_today": behind_today,
            "max_days_allowed": upstream_max_days,
            "frozen": upstream_breach is not None,
            "checked_on": today,
            "gaps": upstream_gaps,                     # 只 WARN，不参与退出码
            "gap_lookback_workdays": upstream_gap_lookback,
        },
        "files": files,
        "max_lag_trading_days": worst,
        "max_lag_allowed": max_lag,
        # 只数护栏对象——这是「护栏」的账，要与 breaches 自洽；参考口径的缺口
        # 在 files[label].gap_count 里照样可见，只是不进这个合计。
        "output_gap_total": sum(f["gap_count"] for f in files.values() if f["gated"]),
        "breaches": breaches,
        "upstream_breach": upstream_breach,
    }
    return report, not breaches and upstream_breach is None


def print_report(report: dict, ok: bool) -> None:
    up = report["upstream"]
    print(f"上游 {up['table']} 最新交易日: {up['max_trade_date']}"
          f"（距今 {up['calendar_days_behind_today']} 个自然日）")
    for label, info in report["files"].items():
        mark = "护栏" if info["gated"] else "参考"
        lag = info["lag_trading_days"]
        lag_s = "N/A" if lag is None else f"{lag} 交易日"
        gap_s = "" if not info.get("gap_count") else f" 缺 {info['gap_count']} 交易日"
        off_s = "" if not info.get("off_calendar_count") else \
            f" 日历外 {info['off_calendar_count']} 天"
        print(f"  [{mark}] {label:26s} 末行 {str(info['last_date']):12s} 落后 {lag_s}"
              f"{gap_s}{off_s}")
    if report["upstream"].get("gaps"):
        up_gaps = report["upstream"]["gaps"]
        print(f"UPSTREAM_GAP: 上游近 {report['upstream']['gap_lookback_workdays']} 个工作日内"
              f"有 {len(up_gaps)} 天库里没有任何本项目输入码的数据 ——")
        print(f"  UPSTREAM_GAP  {'、'.join(up_gaps[:GAP_SAMPLE])}")
        print("  UPSTREAM_GAP  只 WARN 不参与退出码（本项目对上游缺口无处置权）。")
        print("  UPSTREAM_GAP  若确属调休/农历假期 → --holiday-window 登记该日消音；")
        print("  UPSTREAM_GAP  若确属上游漏采 → 上游补齐后本链路下次重算即自动补上产出，")
        print("  UPSTREAM_GAP  在此之前产出会一直缺这几天（2026-08-12/13 就是这个剧本）。")
    if report.get("upstream_breach"):
        print("UPSTREAM_STALE: 上游自己冻结了 ——")
        print(f"  UPSTREAM_STALE  {report['upstream_breach']}")
        print("  UPSTREAM_STALE  本项目产出与上游同步，上游不动则「产出 vs 上游」恒为 0 落后，")
        print("  UPSTREAM_STALE  只盯那一项会永远报 OK；处置 = 查 stock_selector 的 daily_index 调度器。")
        print("  UPSTREAM_STALE  若确属长假，用 --holiday-window 或 "
              "STYLE_SIGNALS_HOLIDAY_WINDOWS 登记该窗口消音。")
    elif up["calendar_days_behind_today"] > 4:
        print(f"WARN: 上游 index_daily 已 {up['calendar_days_behind_today']} 个自然日未更新"
              f"（长假期间属正常；否则检查 stock_selector 的 daily_index 调度器）")
    if report["breaches"]:
        print("STALE: 产出护栏未通过 —— 下列产出没有追平上游（落后 / 中间缺交易日）：")
        for b in report["breaches"]:
            print(f"  STALE  {b}")
        if report.get("output_gap_total"):
            print(f"  STALE  护栏对象缺口合计 {report['output_gap_total']} 天；处置 = 确认上游"
                  f"这些天有数据后重跑本链路")
            print("  STALE  （四个生成脚本都是 --source pg 全量重算覆写，跑一次即补齐）。")
    if ok:
        print(f"FRESHNESS OK（最大落后 {report['max_lag_trading_days']} 交易日 "
              f"<= {report['max_lag_allowed']}；区间内缺口 0；上游距今 "
              f"{up['calendar_days_behind_today']} 自然日 <= {up['max_days_allowed']}）")


def parse_steps(raw: str | None) -> list[dict]:
    """runner 传来的分步耗时 JSON；解析不了就原样留档 + 出声（别静默吞）。"""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"WARN: --steps 不是合法 JSON（{exc}），原样记入状态文件", file=sys.stderr)
        return [{"raw": raw}]


def main() -> int:
    ap = argparse.ArgumentParser(description="日更信号链新鲜度护栏 + 状态文件")
    ap.add_argument("--max-lag", type=int, default=1,
                    help="允许落后上游的交易日数，默认 1")
    ap.add_argument("--root", default=None,
                    help="被检查的数据根目录，默认仓库根（演示/测试时可指向 output 树副本）")
    ap.add_argument("--status-file", default=None, help="状态 JSON 写入路径")
    ap.add_argument("--run-log", default=None, help="本次运行日志路径（记入状态文件）")
    ap.add_argument("--started-at", default=None, help="运行开始时间 ISO 串")
    ap.add_argument("--topup", default="UNKNOWN",
                    help="topup 步骤结果 OK/DEGRADED/TOPUP_SKIPPED/SUSPECT")
    ap.add_argument("--topup-reason", default="",
                    help="topup 被跳过/降级/存疑的原因，原样记入状态文件")
    ap.add_argument("--holiday-window", action="append", default=None,
                    metavar="YYYY-MM-DD:YYYY-MM-DD",
                    help="已知假期窗口（可重复）；农历假期如春节须在此登记，"
                         "否则上游冻结护栏会在长假误报。也可用 "
                         "STYLE_SIGNALS_HOLIDAY_WINDOWS 环境变量（逗号分隔）")
    ap.add_argument("--upstream-max-days", type=int, default=UPSTREAM_MAX_LAG_DAYS,
                    help=f"上游允许多少自然日不更新，默认 {UPSTREAM_MAX_LAG_DAYS}")
    ap.add_argument("--upstream-gap-lookback", type=int,
                    default=UPSTREAM_GAP_LOOKBACK_WORKDAYS,
                    help="上游缺口检测回看多少个工作日（只 WARN 不参与退出码），"
                         f"默认 {UPSTREAM_GAP_LOOKBACK_WORKDAYS}；0 = 关闭")
    ap.add_argument("--steps", default=None, help="各步骤耗时 JSON 数组")
    ap.add_argument("--failed-step", default=None,
                    help="给定则记账为失败（不连库、不做新鲜度检查）")
    args = ap.parse_args()

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    status = {
        "result": None,
        "failed_step": args.failed_step,
        "started_at": args.started_at,
        "finished_at": now,
        "topup": args.topup,
        "topup_reason": args.topup_reason or None,
        "steps": parse_steps(args.steps),
        "run_log": args.run_log,
    }

    if args.failed_step:
        status["result"] = "FAILED"
        print(f"FAILED: 链路在步骤 {args.failed_step} 失败，未做新鲜度检查")
        rc = 1
    else:
        windows = parse_holiday_windows(
            ",".join(args.holiday_window) if args.holiday_window
            else os.environ.get("STYLE_SIGNALS_HOLIDAY_WINDOWS", ""))
        try:
            report, ok = build_report(
                args.max_lag, Path(args.root) if args.root else None,
                holiday_windows=windows, upstream_max_days=args.upstream_max_days,
                upstream_gap_lookback=args.upstream_gap_lookback)
        except Exception as exc:  # 连库失败等：如实记账，不吞
            status["result"] = "CHECK_ERROR"
            status["error"] = f"{type(exc).__name__}: {exc}"
            print(f"CHECK_ERROR: 新鲜度检查本身失败: {type(exc).__name__}: {exc}")
            rc = 2
        else:
            status.update(report)
            if ok:
                status["result"] = "OK"
            elif report.get("upstream_breach") and not report["breaches"]:
                status["result"] = "UPSTREAM_STALE"
            elif report.get("upstream_breach"):
                status["result"] = "STALE+UPSTREAM_STALE"
            else:
                status["result"] = "STALE"
            print_report(report, ok)
            rc = 0 if ok else 1

    if args.status_file:
        p = Path(args.status_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        print(f"状态文件: {p}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
