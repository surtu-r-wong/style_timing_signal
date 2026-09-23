"""日更信号链的企业微信推送（run_daily_signals.sh 步骤 8；--alert 由 alert_on_failure.sh 调用）。

成功路径：护栏通过后推当日持仓——两池置顶（backtest.production.POOLS），其余生产线作参考，
末尾一行链路体检。**只推护栏担保过的文件**：状态文件 result 必须是 OK，且每份要推的持仓文件都以
gated=true 出现在状态文件 files 里、last_date 与文件当前末行一致，否则拒推（exit 1）。

失败路径（--alert）：只读状态文件拼一条失败通知，不 import pandas——科学栈坏了也要能报警。

webhook：环境变量 ALERT_WEBHOOK_URL 优先，否则读 ~/.config/market-monitor/alert.env
（bs-toolkit、数据管理办公室同一个机器人）。URL 里带 key，任何输出都不打印它。
qyapi.weixin.qq.com 是国内端点，本机 Clash 代理会让它失败 → 显式绕代理。

用法：
    python3 deploy/daily_signals/notify_wechat.py --dry-run     # 只打印，不发、不写状态文件
    python3 deploy/daily_signals/notify_wechat.py               # 发送，并把 notify 段写回状态文件
    python3 deploy/daily_signals/notify_wechat.py --alert --systemd-result exit-code
设计：docs/plans/2026-09-23-wechat-signal-push-design.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: 企业微信 text 消息超过这么多 UTF-8 字节会被整条拒收，不是截断。
WECHAT_TEXT_LIMIT = 2048
POSITION_LABELS = {1: "持多 +1", 0: "空仓 0", -1: "持空 -1"}
MAPPING_LABELS = {"symmetric": "对称", "longflat": "long-flat"}
LOG_NOTE = "全文见当日运行日志"
WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
DEFAULT_ENV_FILE = Path.home() / ".config" / "market-monitor" / "alert.env"
SEND_TIMEOUT = 10.0
RETRY_WAIT = 5.0
DEFAULT_STATUS = ROOT / "logs" / "daily_signals_status.json"


class Refused(RuntimeError):
    """不该推（护栏未过 / 推送对象未经护栏担保 / 没配 webhook）。"""


@dataclass
class Line:
    name: str          # 信号线
    mapping: str       # symmetric / longflat
    rel_path: str      # 推荐持仓文件（仓库相对路径）
    last_date: str
    position: int
    run_start: str     # 当前这段持仓的起始日
    run_days: int
    prev: int | None   # 上一段持仓；序列从头就是这一段时为 None
    value: str | None  # 信号值（SIGNALS 的列，信号日当天，原样字符串）


def _read_column(path: Path, col: str) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [(row["date"], row[col]) for row in csv.DictReader(fh)]


def _load_line(root: Path, name: str, mapping: str, rel_path: str, signals: dict) -> Line:
    rows = [(d, int(float(v))) for d, v in _read_column(root / rel_path, "position")]
    if not rows:
        raise Refused(f"{rel_path} 是空文件")
    last_date, pos = rows[-1]
    i = len(rows) - 1
    while i > 0 and rows[i - 1][1] == pos:
        i -= 1
    sig_path, col = signals[name]
    value = dict(_read_column(root / sig_path, col)).get(last_date)
    return Line(name, mapping, rel_path, last_date, pos, rows[i][0], len(rows) - i,
                rows[i - 1][1] if i > 0 else None, value)


def collect_lines(root: Path) -> tuple[list[tuple[str, Line]], list[Line]]:
    """-> ([(池名, 行)], [其余生产线的行])。映射全部取自 backtest.production，不在这里拼文件名。"""
    from backtest.baseline import SIGNALS
    from backtest.production import POOLS, PRODUCTION_MAPPING, recommended_file
    pools = [(pool, _load_line(root, name, m, recommended_file(name, m), SIGNALS))
             for pool, (name, m) in POOLS.items()]
    pooled = set(POOLS.values())
    others = [_load_line(root, name, m, recommended_file(name, m), SIGNALS)
              for name, m in PRODUCTION_MAPPING.items() if (name, m) not in pooled]
    return pools, others


def check_vouched(status: dict, lines: list[Line]) -> None:
    """只推护栏担保过的东西：result=OK，且每份文件 gated 且末行与护栏核验时一致。"""
    if status.get("result") != "OK":
        raise Refused(f"护栏结果是 {status.get('result')}，不推持仓")
    vouched = {f.get("path"): f.get("last_date")
               for f in (status.get("files") or {}).values() if f.get("gated")}
    for line in lines:
        if line.rel_path not in vouched:
            raise Refused(f"{line.rel_path} 不是护栏对象，不推")
        if vouched[line.rel_path] != line.last_date:
            raise Refused(f"{line.rel_path} 末行 {line.last_date} 与护栏核验时的 "
                          f"{vouched[line.rel_path]} 不一致（护栏之后文件又被改过），不推")


def _short(day: str, ref: str) -> str:
    return day[5:] if day[:4] == ref[:4] else day


def describe(line: Line, as_of: str) -> str:
    pos = POSITION_LABELS.get(line.position, str(line.position))
    if line.run_days == 1 and line.prev is not None:
        body = f"⚡翻仓 {POSITION_LABELS.get(line.prev, str(line.prev))} → {pos}"
    else:
        body = f"{pos}（{_short(line.run_start, as_of)} 起第 {line.run_days} 日）"
    lag = f"（末行 {_short(line.last_date, as_of)}）" if line.last_date != as_of else ""
    value = line.value if line.value not in (None, "") else "—"
    text = f"{line.name} {MAPPING_LABELS.get(line.mapping, line.mapping)}：{body}{lag}"
    # 全角括号自带间距，其后不再补空格（设计 §2.3 样式：「…起第 29 日）因子 …」vs「…持多 +1 因子 …」）
    return f"{text}{'' if text.endswith('）') else ' '}信号值 {value}"


def health_lines(status: dict) -> list[str]:
    guard = (f"护栏 OK（最大落后 {status.get('max_lag_trading_days')} 交易日 · "
             f"缺口 {status.get('output_gap_total')}）")
    topup = status.get("topup")
    out = [f"topup OK · {guard}" if topup == "OK" else guard]
    if topup != "OK":
        out.append(f"⚠ topup {topup}：{status.get('topup_reason') or '未记原因'}")
    gaps = (status.get("upstream") or {}).get("gaps") or []
    if gaps:
        more = " 等" if len(gaps) > 5 else ""
        out.append(f"⚠ 上游缺 {len(gaps)} 天：{'、'.join(gaps[:5])}{more}")
    return out


def truncate_text(text: str, *, limit: int, note: str = "") -> str:
    """从末尾整行删到 UTF-8 字节数装得下，并注明删了几行（照搬 bs-toolkit）。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    lines = text.split("\n")
    dropped = 0
    while lines:
        dropped += 1
        lines.pop()
        tail = f"…（超长，还有 {dropped} 行{'，' + note if note else ''}）"
        candidate = "\n".join([*lines, tail])
        if len(candidate.encode("utf-8")) <= limit:
            return candidate
    return f"…（超长，还有 {dropped} 行{'，' + note if note else ''}）"


def compose_message(root: Path, status: dict, *, today: str) -> tuple[str, str]:
    """-> (消息正文, 信号日)。推送对象未经护栏担保则 raise Refused。"""
    pools, others = collect_lines(root)
    check_vouched(status, [line for _, line in pools] + others)
    as_of = max(line.last_date for line in [line for _, line in pools] + others)
    head = f"风格择时 信号日 {as_of}"
    if as_of != today:
        head += f"（今天 {_short(today, as_of)}）"
    lines = [head + "｜链路 OK"]
    lines += [f"【{pool}】{describe(line, as_of)}" for pool, line in pools]
    lines.append("【其余生产线·参考】")
    lines += [f"  {describe(line, as_of)}" for line in others]
    lines += health_lines(status)
    return truncate_text("\n".join(lines), limit=WECHAT_TEXT_LIMIT, note=LOG_NOTE), as_of


class SendError(RuntimeError):
    """企业微信没收下（网络错误重试后仍失败 / errcode≠0 / 回包不是 JSON）。"""


def load_env_file(path: Path) -> dict[str, str]:
    """极简 dotenv：KEY=VALUE、export 前缀、成对引号、# 注释；读不到就当空。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def resolve_webhook(env_file: Path) -> str:
    return os.environ.get(WEBHOOK_ENV) or load_env_file(env_file).get(WEBHOOK_ENV, "")


def _scrub(text: str, url: str) -> str:
    """报错文本里抹掉 URL 与 key——它们会进日志、告警文件和状态文件。"""
    out = text.replace(url, "<webhook>")
    key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("key", [""])[0]
    return out.replace(key, "<key>") if key else out


def send_text(url: str, text: str, *, timeout: float = SEND_TIMEOUT, retries: int = 1,
              wait: float = RETRY_WAIT, opener=None) -> None:
    """发一条 text 消息。显式绕代理；网络层错误重试 retries 次；errcode≠0 不重试。"""
    body = json.dumps({"msgtype": "text", "text": {"content": text}},
                      ensure_ascii=False).encode("utf-8")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= retries:
                raise SendError(f"网络错误（共试 {retries + 1} 次）：{_scrub(str(exc), url)}") from None
            time.sleep(wait)
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SendError(f"回包不是 JSON：{_scrub(raw[:200].decode('utf-8', 'replace'), url)}") from None
    if reply.get("errcode", 0) != 0:
        raise SendError(_scrub(f"企业微信拒收：errcode={reply.get('errcode')} "
                               f"{reply.get('errmsg')}", url))


def load_status(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"读不了状态文件 {path}：{type(exc).__name__}") from None


def record_notify(status_path: Path, notify: dict) -> None:
    """把推送结果写回状态文件的 notify 段：重读再写 + 原子替换，其他字段原样保留。"""
    status = load_status(status_path)
    status["notify"] = notify
    tmp = status_path.with_name(status_path.name + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, status_path)


ALERT_FILE_NOTE = "处置见 logs/ALERT_daily_signals"


def build_alert(status: dict | None, *, systemd_result: str, now: str) -> str:
    """失败通知：只用状态文件里的字段，不碰产出文件、不 import pandas。"""
    lines = [f"⚠ 风格择时日更链失败｜{now}"]
    if status is None:
        lines.append("状态文件缺失或无法解析——链路可能在写状态之前就死了")
    else:
        lines.append(f"结果 {status.get('result')} · 失败步骤 {status.get('failed_step') or '—'}"
                     f" · 状态写于 {status.get('finished_at')}")
        if status.get("topup") not in (None, "OK"):
            lines.append(f"topup {status.get('topup')}：{status.get('topup_reason') or '未记原因'}")
        lines += [f"护栏：{b}" for b in (status.get("breaches") or [])[:3]]
        if status.get("upstream_breach"):
            lines.append(f"上游：{status['upstream_breach']}")
        if status.get("error"):
            lines.append(f"检查出错：{status['error']}")
        notify_error = (status.get("notify") or {}).get("error")
        if notify_error:
            lines.append(f"推送失败：{notify_error}")
        elif status.get("result") == "OK":
            lines.append("状态文件没记下失败原因——可能在写状态前就被杀了（看 systemd Result）")
    if systemd_result:
        lines.append(f"systemd Result={systemd_result}")
    lines.append(f"持仓未更新/未送达，以上一次推送为准；{ALERT_FILE_NOTE}")
    return truncate_text("\n".join(lines), limit=WECHAT_TEXT_LIMIT, note=ALERT_FILE_NOTE)


def run_alert(args) -> int:
    try:
        status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
    text = build_alert(status, systemd_result=args.systemd_result,
                       now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(text)
    if args.dry_run:
        return 0
    url = resolve_webhook(Path(args.env_file))
    if not url:
        raise Refused(f"没有 {WEBHOOK_ENV}，失败通知只进告警文件")
    send_text(url, text)
    print("已推送失败通知")
    return 0


def run_push(args) -> int:
    status_path = Path(args.status_file)
    status = load_status(status_path)
    text, as_of = compose_message(Path(args.root), status,
                                  today=args.today or date.today().isoformat())
    print(text)
    if args.dry_run:
        print("（--dry-run：未发送，未写状态文件）")
        return 0
    notify = {"sent": False, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "as_of": as_of, "bytes": len(text.encode("utf-8")), "error": None}
    url = resolve_webhook(Path(args.env_file))
    try:
        if not url:
            raise Refused(f"没有 {WEBHOOK_ENV}（环境变量或 {args.env_file}），拒绝静默；"
                          f"预览用 --dry-run")
        send_text(url, text)
        notify["sent"] = True
    except (Refused, SendError) as exc:
        notify["error"] = str(exc)
        raise
    finally:
        record_notify(status_path, notify)
    print(f"已推送企业微信（信号日 {as_of}，{notify['bytes']} 字节）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="日更信号链企业微信推送")
    ap.add_argument("--status-file", default=str(DEFAULT_STATUS), help="状态 JSON（护栏写的那份）")
    ap.add_argument("--root", default=str(ROOT), help="数据根目录，默认仓库根（测试/演示可指向副本）")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                    help=f"{WEBHOOK_ENV} 所在 env 文件（环境变量优先）")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息：不发送、不写状态文件")
    ap.add_argument("--alert", action="store_true", help="失败通知模式（由 alert_on_failure.sh 调用）")
    ap.add_argument("--systemd-result", default="", help="--alert 用：主 service 的 systemd Result")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD，默认今天（测试用）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_alert(args) if args.alert else run_push(args)
    except (Refused, SendError) as exc:
        print(f"REFUSED: {exc}" if isinstance(exc, Refused) else f"SEND_FAILED: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
