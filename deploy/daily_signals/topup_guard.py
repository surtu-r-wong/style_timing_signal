"""topup 写库护栏 —— 原则：不可信响应零写入。

**为什么是前置闸门而不是「先落临时文件再导入」**：topup（`tools/topup_index_daily.sh`
→ stock_selector 的 backfill CLI）把「向 gateway 取数」与「写 `index_daily`」融在同一次
调用里，而本项目不得改动 stock_selector。因此可得的最强保证是**在调用之前**把能查的
都查一遍：任何一项存疑就**不调用**——不调用即零写入。调用之后再做一次**只读事后审计**，
拿调用前后的快照比对；发现异常就在信号生成**之前**中止链路，可疑数据进不了 committed
信号 CSV。

## 三条前置闸门（只读；任一不过 → SKIP，零写入、零 quota 消耗）

1. `/ping` + `/health`：网关活着且 `wind_ready == true`。
2. `/quota`：`used < max`。**必要不充分**——网关的计数器与 Wind 服务端的 wsd 额度是
   两本账，wsd 可以独立耗尽（`stock_selector/data/wind_source.py:1196` 原话：
   "WSD quota may be exhausted independently of WSS"）。2026-08-12 用户通报 wsd 已耗尽，
   而同一时刻 `/quota` 仍报 `used=4,955,403 / max=500,000,000`（≈1%）。所以这条闸门
   只能**否决**，不能**背书**：它拦得住"网关自己都知道没额度了"，拦不住 wsd 独立耗尽。
   后者由事后审计 + 运维标志文件兜底。
3. `index_daily` 已覆盖到「应有的最后一个交易日」→ 无新数据可取，不必调用。
   （不含节假日日历，节假日会高估一天：那天 topup 拿到空结果，空结果不写库，无害。）

## 事后审计（只读 PG，比对调用前后快照；不过 → 链路中止，信号不重算）

- 新增日期必须落在 `(调用前 max_date, 今天]` 窗口内
- 新增收盘价必须有限且 > 0
- 不得是「前值复制」占位日：某新日**所有**指数的收盘价与前一交易日逐一相等
- 调用前已存在的近 30 个交易日收盘价不得被改写

## 用法

    python3 deploy/daily_signals/topup_guard.py --mode preflight --snapshot /path/snap.json
        exit 0  → GO（可以调用 topup），快照已写入
        exit 10 → SKIP，stdout 有一行 SKIP_REASON=...
        exit 2  → 护栏自身出错；调用方按 SKIP 处理（fail-closed）

    python3 deploy/daily_signals/topup_guard.py --mode audit --snapshot /path/snap.json
        exit 0 → 干净；exit 1 → SUSPECT（stdout 逐条列出）；exit 2 → 护栏自身出错
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EXIT_GO = 0
EXIT_SUSPECT = 1
EXIT_ERROR = 2
EXIT_SKIP = 10

SNAPSHOT_DAYS = 30          # 事后审计比对的历史窗口（交易日）
MARKET_CLOSE_MINUTE = 15 * 60 + 30   # 15:30，收盘后才可能有当日数据


# ─────────────────────────────── 纯函数（可单测，不连网不连库） ───────────────────────────────

def check_health(ping: object, health: object) -> str | None:
    """网关健康检查。返回 None = 通过，否则返回不通过的原因。"""
    if not isinstance(ping, dict) or ping.get("status") != "ok":
        return f"/ping 未返回 status=ok: {ping!r}"
    if not isinstance(health, dict):
        return f"/health 返回非 JSON 对象: {health!r}"
    if health.get("status") != "ok":
        return f"/health 未返回 status=ok: {health!r}"
    if health.get("wind_ready") is not True:
        return f"/health 的 wind_ready 不是 true（Wind 终端未就绪）: {health!r}"
    return None


def check_quota(quota: object, min_headroom: int = 1) -> str | None:
    """网关额度检查（必要不充分，见模块 docstring）。None = 通过。"""
    if not isinstance(quota, dict):
        return f"/quota 返回非 JSON 对象: {quota!r}"
    used, cap = quota.get("used"), quota.get("max")
    if not isinstance(used, (int, float)) or not isinstance(cap, (int, float)):
        return f"/quota 缺 used/max 或类型不对: {quota!r}"
    if cap <= 0:
        return f"/quota 的 max 非正: {quota!r}"
    if used >= cap - min_headroom + 1:
        return f"网关自报额度已耗尽: used={used} / max={cap}"
    return None


def expected_last_trading_day(now: datetime) -> date:
    """「今天为止应该已经有数据的最后一个交易日」（只按工作日，不含节假日日历）。

    收盘（15:30）之后当天才算数；否则回退到上一个工作日。节假日会高估一天——
    那天 topup 拿到空结果，空结果不写库，属无害误判。
    """
    day = now.date()
    if now.weekday() < 5 and now.hour * 60 + now.minute >= MARKET_CLOSE_MINUTE:
        candidate = day
    else:
        candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def audit_changes(before: dict, after: dict, today: str) -> list[str]:
    """比对调用前后快照，返回可疑之处清单（空 = 干净）。

    before/after 结构：{"max_trade_date": "YYYY-MM-DD",
                       "closes": {code: {"YYYY-MM-DD": float|None}}}
    """
    problems: list[str] = []
    prev_max = before.get("max_trade_date")
    new_max = after.get("max_trade_date")
    if prev_max is None or new_max is None:
        return ["快照缺 max_trade_date，无法审计"]
    if new_max < prev_max:
        problems.append(f"调用后 max_trade_date 反而后退: {prev_max} → {new_max}")

    before_closes = before.get("closes", {})
    after_closes = after.get("closes", {})

    # 1) 新增日期必须落在 (prev_max, today] 窗口内
    new_dates: set[str] = set()
    for code, series in after_closes.items():
        for day in series:
            if day > prev_max:
                new_dates.add(day)
    for day in sorted(new_dates):
        if day > today:
            problems.append(f"出现未来日期 {day}（> 今天 {today}）")

    # 2) 新增收盘价必须有限且 > 0
    for code, series in after_closes.items():
        for day, value in series.items():
            if day <= prev_max:
                continue
            if value is None or not isinstance(value, (int, float)) \
                    or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                problems.append(f"{code} {day} 收盘价非法: {value!r}")

    # 3) 前值复制占位日：某新日所有指数收盘价与前一交易日逐一相等
    for day in sorted(new_dates):
        codes_checked = 0
        codes_identical = 0
        for code, series in after_closes.items():
            earlier = [d for d in series if d < day]
            if not earlier:
                continue
            prev_day = max(earlier)
            cur, prv = series.get(day), series.get(prev_day)
            if cur is None or prv is None:
                continue
            codes_checked += 1
            if cur == prv:
                codes_identical += 1
        if codes_checked >= 2 and codes_identical == codes_checked:
            problems.append(
                f"{day} 疑似前值复制占位日：{codes_checked} 个指数的收盘价与前一交易日完全相等")

    # 4) 调用前已存在的历史不得被改写
    for code, series in before_closes.items():
        after_series = after_closes.get(code, {})
        for day, old in series.items():
            if day not in after_series:
                problems.append(f"{code} {day} 调用后消失（历史被删）")
                continue
            new = after_series[day]
            if old is None and new is None:
                continue
            if old is None or new is None or old != new:
                problems.append(f"{code} {day} 历史收盘价被改写: {old!r} → {new!r}")

    return problems


# ─────────────────────────────── 只读 IO ───────────────────────────────

def fetch_gateway_json(url: str, path: str, token: str, timeout: float = 10.0) -> object:
    import requests

    resp = requests.get(
        f"{url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def load_gateway_config() -> dict:
    import yaml

    from signals.common.config import CONFIG_FILE

    cfg = yaml.safe_load(Path(CONFIG_FILE).read_text(encoding="utf-8")) or {}
    gw = cfg.get("wind_gateway") or {}
    missing = [k for k in ("url", "token") if not gw.get(k)]
    if missing:
        raise ValueError(f"settings.yaml wind_gateway 段缺 {missing}")
    return gw


def take_snapshot(days: int = SNAPSHOT_DAYS, since: str | None = None) -> dict:
    """只读 PG：本项目输入码的 max(trade_date) + 一段窗口内的收盘价。

    `since=None`（调用前快照）：取最近 `days` 个交易日，窗口下沿随数据浮动。
    `since='YYYY-MM-DD'`（调用后快照）：**下沿钉死在给定日期**。

    为什么必须能钉死下沿：滑动窗口是有毒的。topup 每补进一个新交易日，若 after 仍取
    "最近 30 个交易日"，窗口整体右移一格，before 里最老的那天就掉出 after ——
    审计规则 4「历史不得被删」于是必然命中，得出「topup 一旦真的补上数据就判 SUSPECT、
    只有它什么都没干时才通过」的荒谬结论。对齐下沿后，after 是 before 的时间超集，
    规则 4 恢复成它本来的语义：**同一段历史**有没有被改写。
    """
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
            if since is None:
                cur.execute(
                    f"SELECT DISTINCT trade_date FROM {db['schema']}.index_daily "
                    "WHERE index_code = ANY(%s) ORDER BY trade_date DESC LIMIT %s",
                    (codes, days),
                )
                recent = sorted(r[0].isoformat() for r in cur.fetchall())
                if not recent:
                    raise RuntimeError("index_daily 中查不到本项目任何输入码的交易日")
                window_start = recent[0]
            else:
                window_start = since
            cur.execute(
                f"SELECT index_code, trade_date, close FROM {db['schema']}.index_daily "
                "WHERE index_code = ANY(%s) AND trade_date >= %s::date",
                (codes, window_start),
            )
            closes: dict[str, dict[str, float | None]] = {}
            for code, day, close in cur.fetchall():
                closes.setdefault(code, {})[day.isoformat()] = (
                    None if close is None else float(close)
                )
    finally:
        conn.close()
    all_days = {day for series in closes.values() for day in series}
    if not all_days:
        raise RuntimeError(
            f"index_daily 中 {window_start} 之后查不到本项目任何输入码的收盘价")
    return {
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "max_trade_date": max(all_days),
        "window_start": window_start,
        "closes": closes,
    }


# ─────────────────────────────── 模式 ───────────────────────────────

def run_preflight(snapshot_path: Path, now: datetime) -> int:
    snapshot = take_snapshot()
    db_max = snapshot["max_trade_date"]
    print(f"index_daily 现有最新交易日: {db_max}")

    expected = expected_last_trading_day(now).isoformat()
    print(f"应有的最后一个交易日（按工作日推算）: {expected}")
    if db_max >= expected:
        print(f"SKIP_REASON=NO_NEW_TRADING_DAY（库内已到 {db_max} >= 应有 {expected}，无新数据可取）")
        return EXIT_SKIP

    gw = load_gateway_config()
    try:
        ping = fetch_gateway_json(gw["url"], "/ping", gw["token"])
        health = fetch_gateway_json(gw["url"], "/health", gw["token"])
    except Exception as exc:
        print(f"SKIP_REASON=GATEWAY_UNREACHABLE({type(exc).__name__}: {exc})")
        return EXIT_SKIP
    reason = check_health(ping, health)
    if reason:
        print(f"SKIP_REASON=GATEWAY_UNHEALTHY({reason})")
        return EXIT_SKIP
    print(f"gateway /health OK: {health}")

    try:
        quota = fetch_gateway_json(gw["url"], "/quota", gw["token"])
    except Exception as exc:
        print(f"SKIP_REASON=QUOTA_UNKNOWN({type(exc).__name__}: {exc})")
        return EXIT_SKIP
    reason = check_quota(quota)
    if reason:
        print(f"SKIP_REASON=QUOTA_EXHAUSTED({reason})")
        return EXIT_SKIP
    print(f"gateway /quota OK: {quota}"
          f"  ← 注意：这条只能否决不能背书，wsd 可独立于此计数器耗尽")

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    print(f"前置闸门全过 → GO；调用前快照已写入 {snapshot_path}")
    return EXIT_GO


def run_audit(snapshot_path: Path, now: datetime) -> int:
    if not snapshot_path.exists():
        print(f"AUDIT_ERROR: 找不到调用前快照 {snapshot_path}")
        return EXIT_ERROR
    before = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # 关键：after 的窗口下沿必须与 before 对齐，否则 topup 每补一天都会把 before
    # 最老的一天挤出 after，规则 4 必然误报「历史被删」（详见 take_snapshot docstring）。
    after = take_snapshot(since=before.get("window_start"))
    problems = audit_changes(before, after, now.date().isoformat())
    print(f"审计窗口: {before['window_start']}..{after['max_trade_date']}；"
          f"调用前 max={before['max_trade_date']} → 调用后 max={after['max_trade_date']}")
    if problems:
        print("TOPUP_SUSPECT: topup 写入的内容未通过事后审计 ——")
        for p in problems:
            print(f"  TOPUP_SUSPECT  {p}")
        return EXIT_SUSPECT
    print("TOPUP_AUDIT_OK: 写入内容通过事后审计（无未来日期/无非法价/无前值复制/历史未被改写）")
    return EXIT_GO


def main() -> int:
    ap = argparse.ArgumentParser(description="topup 写库护栏（前置闸门 + 事后审计）")
    ap.add_argument("--mode", choices=["preflight", "audit"], required=True)
    ap.add_argument("--snapshot", required=True, help="调用前快照 JSON 路径")
    args = ap.parse_args()

    now = datetime.now()
    snapshot_path = Path(args.snapshot)
    try:
        if args.mode == "preflight":
            return run_preflight(snapshot_path, now)
        return run_audit(snapshot_path, now)
    except Exception as exc:  # fail-closed：护栏自身出错也不放行
        print(f"GUARD_ERROR({type(exc).__name__}: {exc})")
        if args.mode == "preflight":
            print(f"SKIP_REASON=GUARD_ERROR({type(exc).__name__}: {exc})")
            return EXIT_SKIP
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
