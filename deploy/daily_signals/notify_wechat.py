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
    python3 deploy/daily_signals/notify_wechat.py --alert --systemd-result exit-code --run-started @1790073050
设计：docs/plans/2026-09-23-wechat-signal-push-design.md
"""
from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: 企业微信 text 消息超过这么多 UTF-8 字节会被整条拒收，不是截断。
WECHAT_TEXT_LIMIT = 2048
MAPPING_LABELS = {"symmetric": "对称", "longflat": "long-flat"}
LOG_NOTE = "全文见当日运行日志"
WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
DEFAULT_ENV_FILE = Path.home() / ".config" / "market-monitor" / "alert.env"
SEND_TIMEOUT = 10.0
RETRY_WAIT = 5.0
DEFAULT_STATUS = ROOT / "logs" / "daily_signals_status.json"


class Refused(RuntimeError):
    """不该推（护栏未过 / 推送对象未经护栏担保 / 没配 webhook）。"""


def _v(value) -> str:
    """状态文件缺字段（None / 空串）时显示「—」，不显示 None。"""
    return "—" if value is None or value == "" else str(value)


def position_label(v: float) -> str:
    """持仓标签：1.0→持多 +1、-1.0→持空 -1、-0.5→持空 -0.5、0→空仓 0（分数仓位原样，不截断）。"""
    if v > 0:
        return f"持多 {v:+g}"
    if v < 0:
        return f"持空 {v:+g}"
    return "空仓 0"


@dataclass
class Line:
    name: str          # 信号线
    mapping: str       # symmetric / longflat
    rel_path: str      # 推荐持仓文件（仓库相对路径）
    last_date: str
    position: float
    run_start: str     # 当前这段持仓的起始日
    run_days: int
    prev: float | None  # 上一段持仓；序列从头就是这一段时为 None
    value: str | None  # 信号值（SIGNALS 的列，取本行持仓文件末行日期当天的值，原样字符串）


def _read_column(path: Path, col: str) -> list[tuple[str, str]]:
    """(date, 列值) 逐行；date 为空或纯空白的行跳过（与护栏 dates_of 的口径一致）。"""
    with path.open(encoding="utf-8", newline="") as fh:
        return [(row["date"].strip(), row[col]) for row in csv.DictReader(fh)
                if (row.get("date") or "").strip()]


def _load_line(root: Path, name: str, mapping: str, rel_path: str, signals: dict) -> Line:
    rows = []
    for d, v in _read_column(root / rel_path, "position"):
        try:
            rows.append((d, float(v)))
        except (TypeError, ValueError):   # pandas 把 NaN 写成空串 / 混进非数字 / 行里缺这一列
            raise Refused(f"{rel_path} 在 {d} 的持仓 {v!r} 不是数") from None
    if not rows:
        raise Refused(f"{rel_path} 是空文件")
    bad = next(((d, p) for d, p in rows if not math.isfinite(p)), None)
    if bad:
        raise Refused(f"{rel_path} 在 {bad[0]} 的持仓是 {bad[1]}，不是有限数，不推")
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
        raise Refused(f"护栏结果是 {_v(status.get('result'))}，不推持仓")
    vouched = {f.get("path"): f.get("last_date")
               for f in (status.get("files") or {}).values() if f.get("gated")}
    for line in lines:
        if line.rel_path not in vouched:
            raise Refused(f"{line.rel_path} 不是护栏对象，不推")
        if vouched[line.rel_path] != line.last_date:
            raise Refused(f"{line.rel_path} 末行 {line.last_date} 与护栏核验时的 "
                          f"{_v(vouched[line.rel_path])} 不一致（护栏之后文件又被改过），不推")


def _short(day: str, ref: str) -> str:
    return day[5:] if day[:4] == ref[:4] else day


def describe(line: Line, as_of: str) -> str:
    pos = position_label(line.position)
    if line.run_days == 1 and line.prev is not None:
        body = f"⚡翻仓 {position_label(line.prev)} → {pos}"
    else:
        body = f"{pos}（{_short(line.run_start, as_of)} 起第 {line.run_days} 日）"
    lag = f"（末行 {_short(line.last_date, as_of)}）" if line.last_date != as_of else ""
    value = line.value if line.value not in (None, "") else "—"
    text = f"{line.name} {MAPPING_LABELS.get(line.mapping, line.mapping)}：{body}{lag}"
    # 全角括号自带间距，其后不再补空格（设计 §2.3 样式：「…起第 29 日）因子 …」vs「…持多 +1 因子 …」）
    return f"{text}{'' if text.endswith('）') else ' '}信号值 {value}"


#: 第 0 步办公室模式（2026-09-23 起常态：输入由 data_manager 夜间作业写入，状态文件 topup 字段记 OFFICE_*）
#: 出问题时的 ⚠ 行措辞（OFFICE_ACCEPTED = 同族哨兵 CRITICAL 已按 --accept-sentinel 人工放行）；OFFICE_OK 是常态，
#: 不出 ⚠ 行。
OFFICE_WARNINGS = {"OFFICE_LATE": "输入未到齐", "OFFICE_CHECK_ERROR": "输入到齐检查出错",
                   "OFFICE_ACCEPTED": "输入哨兵 CRITICAL 已人工放行", "OFFICE_UNCHECKED": "输入哨兵未判定"}


def health_lines(status: dict) -> list[str]:
    """链路体检行：输入（topup 字段）+ 护栏，末尾是 ⚠ 行（输入有问题 / 上游近窗缺口）。
    输入正常时并进护栏行：topup 模式 OK →「topup OK · 护栏 …」，办公室模式 OFFICE_OK →「输入 办公室日更 ✓ · 护栏 …」。"""
    guard = (f"护栏 OK（最大落后 {_v(status.get('max_lag_trading_days'))} 交易日 · "
             f"缺口 {_v(status.get('output_gap_total'))}）")
    topup = status.get("topup")
    reason = status.get("topup_reason") or "未记原因"
    if topup == "OK":
        out = [f"topup OK · {guard}"]
    elif topup == "OFFICE_OK":
        out = [f"输入 办公室日更 ✓ · {guard}"]
    elif topup == "OFFICE_OK_WARN":   # 哨兵只有 WARN（不阻断）：不算出问题，只提示人工看一眼
        out = [f"输入 办公室日更 ✓ · {guard}", f"ℹ 输入哨兵 WARN（不阻断，建议人工看一眼）：{reason}"]
    else:
        label = OFFICE_WARNINGS.get(topup)
        out = [guard, f"⚠ {label}：{reason}" if label else f"⚠ topup {_v(topup)}：{reason}"]
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
    """极简 dotenv：KEY=VALUE、export 前缀、成对引号、# 注释；读不到就当空。

    行内注释按 bash 语义：未加引号的值里「空白 + #」起为注释（`KEY=abc  # 说明` → abc，
    `KEY=a#b` 的 # 在词中间、不是注释）；加引号的值取引号内原样（# 不动），闭引号之后的注释丢掉。
    """
    try:
        # errors="replace"：注释行被 PowerShell 写成 GBK 之类的非 UTF-8 字节时，不能让 main 在分派前崩掉
        text = path.read_text(encoding="utf-8", errors="replace")
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
        stripped = value.strip()
        quote = stripped[:1]
        close = stripped.find(quote, 1) if quote in ("'", '"') else -1
        if close > 0:
            value = stripped[1:close]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        out[key.strip()] = value
    return out


def resolve_webhook(env_file: Path) -> str:
    return os.environ.get(WEBHOOK_ENV) or load_env_file(env_file).get(WEBHOOK_ENV, "")


_KEY_PARAM = re.compile(r"key=[^&\s'\"]+")


def _scrub(text: str, url: str = "") -> str:
    """报错文本里抹掉 URL 与 key——它们会进日志、告警文件和状态文件。

    先替换整条 URL 与 key 值；再兜底抹掉任何 `key=<值>`——url 为空（还没解析出来就出错），
    或报错只带「路径+查询串」而非整条 URL（http.client 的 InvalidURL 等）时也生效。
    """
    out = text
    if url:
        out = out.replace(url, "<webhook>")
        key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("key", [""])[0]
        if key:
            out = out.replace(key, "<key>")
    return _KEY_PARAM.sub("key=<key>", out)


def send_text(url: str, text: str, *, timeout: float = SEND_TIMEOUT, retries: int = 1,
              wait: float = RETRY_WAIT, opener=None) -> None:
    """发一条 text 消息。显式绕代理；网络层错误重试 retries 次；URL 无效与 errcode≠0 不重试。"""
    body = json.dumps({"msgtype": "text", "text": {"content": text}},
                      ensure_ascii=False).encode("utf-8")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    except ValueError as exc:
        # URL 本身不合法（如缺 scheme）：报错原文带整条 URL（含 key）；属配置问题，不重试
        raise SendError(f"webhook URL 无效：{_scrub(str(exc), url)}") from None
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        # OSError 覆盖 URLError/TimeoutError/ConnectionError/ssl.SSLError；
        # HTTPException 覆盖读回包时的 IncompleteRead/BadStatusLine（它们不是 OSError）。
        except (OSError, http.client.HTTPException) as exc:
            if attempt >= retries:
                raise SendError(f"网络错误（共试 {retries + 1} 次）：{_scrub(str(exc), url)}") from None
            time.sleep(wait)
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SendError(f"回包不是 JSON：{_scrub(raw[:200].decode('utf-8', 'replace'), url)}") from None
    # fail-closed：回包必须是对象且 errcode 恰为 0 才算送达（{} / 缺 errcode / 非对象一律失败）
    if not isinstance(reply, dict) or reply.get("errcode") != 0:
        detail = (f"errcode={_v(reply.get('errcode'))} {_v(reply.get('errmsg'))}"
                  if isinstance(reply, dict) else f"回包不是对象：{str(reply)[:200]}")
        raise SendError(_scrub(f"企业微信拒收：{detail}", url))


def load_status(path: Path) -> dict:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"读不了状态文件 {path}：{type(exc).__name__}") from None
    if not isinstance(status, dict):
        raise Refused(f"状态文件 {path} 不是 JSON 对象")
    return status


def record_notify(status_path: Path, notify: dict) -> None:
    """把推送结果写回状态文件的 notify 段：重读再写 + 原子替换，其他字段原样保留。
    临时文件用隐藏名；写或替换失败就删掉它，不在 logs/ 里留半截文件。"""
    status = load_status(status_path)
    status["notify"] = notify
    tmp = status_path.with_name(f".{status_path.name}.tmp")
    try:
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, status_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


ALERT_FILE_NOTE = "处置见 logs/ALERT_daily_signals"


def parse_run_started(raw: str | None) -> float | None:
    """本次 unit 启动时刻（unix 秒）。吃 `systemctl show -p ExecMainStartTimestamp --value
    --timestamp=unix` 的原样输出（如 `@1790073050`）；空串 / None / 解析不了 → None
    （退回「日期不同」判定——告警器不能因为这个参数坏了就不报）。"""
    text = (raw or "").strip().removeprefix("@")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _status_is_stale(finished, now: str, run_started: float | None) -> bool:
    """状态文件是不是上一次运行留下的。

    给了 run_started：当且仅当 finished_at 能解析且早于本次 unit 启动才算陈旧，解析不了视为不陈旧；
    没给：退回「finished_at 的日期 ≠ now 的日期」。finished_at 缺失时无从比较，一律不陈旧。
    """
    if not finished:
        return False
    if run_started is None:
        return str(finished)[:10] != now[:10]
    try:
        return datetime.fromisoformat(str(finished)).timestamp() < run_started
    except (ValueError, OverflowError, OSError):
        return False


def build_alert(status: dict | None, *, systemd_result: str, now: str,
                run_started: float | None = None) -> str:
    """失败通知：只用状态文件里的字段，不碰产出文件、不 import pandas。

    状态文件是上一次运行留下的（判定见 _status_is_stale）：只说这一句，不引用其中的旧 result /
    topup / breaches / notify.error，免得把旧原因安到这次头上。字段缺失时显示「—」。
    """
    lines = [f"⚠ 风格择时日更链失败｜{now}"]
    finished = status.get("finished_at") if status is not None else None
    if status is None:
        lines.append("状态文件缺失或无法解析——链路可能在写状态之前就死了")
    elif _status_is_stale(finished, now, run_started):
        lines.append(f"状态文件停在 {finished}，不是本次运行写的——本次在写状态前就死了（看 systemd Result）")
    else:
        lines.append(f"结果 {_v(status.get('result'))} · 失败步骤 {_v(status.get('failed_step'))}"
                     f" · 状态写于 {_v(finished)}")
        if status.get("topup") not in (None, "OK", "OFFICE_OK", "OFFICE_OK_WARN"):   # 输入正常（含办公室日更到齐）不提
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


def _dry_run_note(url: str) -> str:
    """dry-run 结尾一句：只报 webhook 配没配（布尔），便于核对配置而不暴露密钥。"""
    return f"（--dry-run：未发送，未写状态文件；webhook {'已配置' if url else '未配置'}）"


def run_alert(args, url: str) -> int:
    try:
        status = load_status(Path(args.status_file))
    except Refused:
        status = None
    text = build_alert(status, systemd_result=args.systemd_result,
                       now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       run_started=parse_run_started(args.run_started))
    print(text)
    if args.dry_run:
        print(_dry_run_note(url))
        return 0
    if not url:
        raise Refused(f"没有 {WEBHOOK_ENV}，失败通知只进告警文件")
    send_text(url, text)
    print("已推送失败通知")
    return 0


def run_push(args, url: str) -> int:
    """推送阶段的任何失败（拒推 / 没配 webhook / 发送失败 / 意外异常）都记进 notify 段再抛出，
    告警器才能如实报原因；--dry-run 永远不写状态文件。状态文件本身读不了就没法记账，照旧拒推。
    url 由 main 解析好传进来（main 靠它统一抹掉报错与堆栈里的密钥）。"""
    status_path = Path(args.status_file)
    status = load_status(status_path)
    notify = {"sent": False, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "as_of": None, "bytes": None, "error": None}
    try:
        text, as_of = compose_message(Path(args.root), status,
                                      today=args.today or date.today().isoformat())
        notify["as_of"], notify["bytes"] = as_of, len(text.encode("utf-8"))
        print(text)
        if args.dry_run:
            print(_dry_run_note(url))
            return 0
        if not url:
            raise Refused(f"没有 {WEBHOOK_ENV}（环境变量或 {args.env_file}），拒绝静默；"
                          f"预览用 --dry-run")
        send_text(url, text)
        notify["sent"] = True
    except Exception as exc:
        error = str(exc) if isinstance(exc, (Refused, SendError)) else f"{type(exc).__name__}: {exc}"
        notify["error"] = _scrub(error, url)
        raise
    finally:
        if not args.dry_run:
            record_notify(status_path, notify)
    print(f"已推送企业微信（信号日 {notify['as_of']}，{notify['bytes']} 字节）")
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
    ap.add_argument("--run-started", default="",
                    help="--alert 用：本次 unit 启动的 unix 秒，可带前导 @（即 systemctl show -p "
                         "ExecMainStartTimestamp --value --timestamp=unix 的输出）；用来判断状态文件"
                         "是不是本次运行写的，空串 = 没给（退回按日期判断）")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD，默认今天（测试用）")
    return ap


def main(argv: list[str] | None = None) -> int:
    """输出到 stderr 的一切（含意外异常的堆栈）都先过 _scrub：它们会进 runner 日志与告警文件。"""
    args = build_parser().parse_args(argv)
    url = ""
    try:
        url = resolve_webhook(Path(args.env_file))
        return run_alert(args, url) if args.alert else run_push(args, url)
    except Refused as exc:
        print(_scrub(f"REFUSED: {exc}", url), file=sys.stderr)
        return 1
    except SendError as exc:
        print(_scrub(f"SEND_FAILED: {exc}", url), file=sys.stderr)
        return 1
    except Exception:
        # 意外异常：保留堆栈便于排查，但抹掉 webhook 与 key
        print(_scrub(traceback.format_exc(), url), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
