"""日更信号链的新鲜度护栏 + 状态文件写入器（只读 PG）。

护栏命题：三条生产信号 CSV 与三份推荐持仓的末行日期，距 `index_daily` 最新交易日
不得超过 `--max-lag` 个**交易日**（默认 1）。超限即以非零码退出，并在 stdout 与
状态 JSON 里打大写 `STALE` 标记——这条护栏存在的唯一理由是：2026-07-09~08-12 停更
35 天无人发现（见 docs/plans/2026-08-12-project-review-and-priorities.md §3.1）。

交易日历取自 `index_daily` 中本项目输入码的 distinct trade_date（不用自然日，
避免周末/长假误报）。上游自身相对今天的滞后只做 WARN，不参与退出码——A 股长假
期间自然日滞后必然变大，硬失败会制造假警报。

用法（由 run_daily_signals.sh 调用，也可手工跑）：
    python3 deploy/daily_signals/check_freshness.py            # 只检查，打印结论
    python3 deploy/daily_signals/check_freshness.py --status-file logs/daily_signals_status.json
    python3 deploy/daily_signals/check_freshness.py --failed-step signal_citic40d ...  # 记账失败，不连库
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
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


def last_date_of(csv_path: Path) -> str | None:
    """CSV 末行第一列（date）。文件缺失/只有表头时返回 None。"""
    if not csv_path.exists():
        return None
    last = None
    with csv_path.open("r", encoding="utf-8") as fh:
        header = fh.readline()
        if not header:
            return None
        for line in fh:
            if line.strip():
                last = line
    if last is None:
        return None
    return last.split(",", 1)[0].strip()


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


def evaluate(days: list[str], root: Path, max_lag: int) -> tuple[dict, list[str], int]:
    """纯函数：给定交易日历与数据根目录，算出每份产出的落后天数与违规清单。

    与 PG 解耦，便于测试与演示（`--root` 指向任意 output 树的副本）。
    """
    files: dict[str, dict] = {}
    breaches: list[str] = []
    worst = 0
    for label, rel in list(GATED.items()) + list(INFORMATIONAL.items()):
        gated = label in GATED
        last = last_date_of(root / rel)
        lag = lag_trading_days(days, last)
        files[label] = {
            "path": rel, "last_date": last,
            "lag_trading_days": lag, "gated": gated,
        }
        if not gated:
            continue
        if last is None:
            breaches.append(f"{label}: 文件缺失或为空 ({rel})")
        elif lag is None:
            breaches.append(f"{label}: 末行日期 {last} 不在 index_daily 交易日历上")
        elif lag > max_lag:
            breaches.append(f"{label}: 末行 {last}，落后上游 {lag} 个交易日 > {max_lag}")
            worst = max(worst, lag)
        else:
            worst = max(worst, lag)
    return files, breaches, worst


def build_report(max_lag: int, root: Path | None = None) -> tuple[dict, bool]:
    days, upstream_max = load_calendar()
    today = date.today().isoformat()
    behind_today = (date.fromisoformat(today) - date.fromisoformat(upstream_max)).days
    files, breaches, worst = evaluate(days, root or ROOT, max_lag)

    report = {
        "data_root": str(root or ROOT),
        "upstream": {
            "table": "stock_selector.index_daily",
            "max_trade_date": upstream_max,
            "calendar_days_behind_today": behind_today,
            "checked_on": today,
        },
        "files": files,
        "max_lag_trading_days": worst,
        "max_lag_allowed": max_lag,
        "breaches": breaches,
    }
    return report, not breaches


def print_report(report: dict, ok: bool) -> None:
    up = report["upstream"]
    print(f"上游 {up['table']} 最新交易日: {up['max_trade_date']}"
          f"（距今 {up['calendar_days_behind_today']} 个自然日）")
    for label, info in report["files"].items():
        mark = "护栏" if info["gated"] else "参考"
        lag = info["lag_trading_days"]
        lag_s = "N/A" if lag is None else f"{lag} 交易日"
        print(f"  [{mark}] {label:26s} 末行 {str(info['last_date']):12s} 落后 {lag_s}")
    if up["calendar_days_behind_today"] > 4:
        print(f"WARN: 上游 index_daily 已 {up['calendar_days_behind_today']} 个自然日未更新"
              f"（长假期间属正常；否则检查 stock_selector 的 daily_index 调度器）")
    if ok:
        print(f"FRESHNESS OK（最大落后 {report['max_lag_trading_days']} 交易日 "
              f"<= {report['max_lag_allowed']}）")
    else:
        print("STALE: 新鲜度护栏未通过 —— 下列产出没有追平上游：")
        for b in report["breaches"]:
            print(f"  STALE  {b}")


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
        try:
            report, ok = build_report(args.max_lag, Path(args.root) if args.root else None)
        except Exception as exc:  # 连库失败等：如实记账，不吞
            status["result"] = "CHECK_ERROR"
            status["error"] = f"{type(exc).__name__}: {exc}"
            print(f"CHECK_ERROR: 新鲜度检查本身失败: {type(exc).__name__}: {exc}")
            rc = 2
        else:
            status.update(report)
            status["result"] = "OK" if ok else "STALE"
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
