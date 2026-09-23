"""deploy/daily_signals/notify_wechat.py 单测（不连库、不连外网：发送一律打本地 HTTP 桩）。"""
import http.client
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTIFY_PATH = ROOT / "deploy" / "daily_signals" / "notify_wechat.py"


def _load():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("notify_wechat", NOTIFY_PATH)
    module = importlib.util.module_from_spec(spec)
    # 必须先登记：模块里有 dataclass + `from __future__ import annotations`，dataclasses 解析
    # 字符串注解时会查 sys.modules[cls.__module__]，不登记就 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


nw = _load()


@pytest.fixture(autouse=True)
def _no_real_webhook(tmp_path, monkeypatch):
    """隔离真实 webhook：默认 env 文件指向不存在的路径、清掉环境变量、HOME 指到 tmp（子进程也随之隔离）；
    要桩 URL 的用例在自身里再 setenv。"""
    monkeypatch.setattr(nw, "DEFAULT_ENV_FILE", tmp_path / "no-such.env")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_env_file_default_is_read_at_call_time(tmp_path):
    """--env-file 的默认值必须在调用时读 DEFAULT_ENV_FILE，上面的隔离 fixture 才真的生效。"""
    assert nw.build_parser().parse_args([]).env_file == str(tmp_path / "no-such.env")


def _today_iso() -> str:
    """与 run_alert 同一个时钟：告警把「不是今天写的」状态文件当作上一次运行留下的。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 假 key 一律用 UUID 形态（真实企业微信 key 就是 UUID）：只抹到第一个连字符这类半截抹除也能抓住。
KEY_STUB = "0f8c2d4e-9a1b-4c3d-8e7f-a1b2c3d4e5f6"
KEY_SECRET = "5b7e9c1a-2d3f-4a6b-8c9d-0e1f2a3b4c5d"
KEY_FAKE = "9d8c7b6a-5f4e-4d3c-8b1a-0f9e8d7c6b5a"


def _leaked(text: str, key: str) -> bool:
    """整条 key 或它的首段/尾段出现在文本里都算泄漏。"""
    first, *_, last = key.split("-")
    return key in text or first in text or last in text


# systemctl show -p ExecMainStartTimestamp --value --timestamp=unix 的输出形如 @1790073050
# （= 2026-09-22 18:30:50 +08:00，本次 unit 启动时刻）
RUN_STARTED = 1790073050


DATES = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]
# 推荐持仓（文件 → 5 天持仓）与信号值（信号线 → 末日值）；路径一律从映射取，不在测试里拼。
POSITIONS = {
    ("equal_weight", "symmetric"): [-1, 1, 1, 1, 1],   # 期货池：09-17 起持多第 4 日
    ("slope20", "longflat"): [0, 0, 0, 0, 1],          # 现货池：09-22 翻仓
    ("slope20", "symmetric"): [-1, -1, -1, -1, 1],     # 参考：翻仓 持空→持多
    ("hybrid20", "longflat"): [0, 0, 0, 0, 0],         # 参考：首行起就空仓
    ("citic40d", "longflat"): [1, 1, 1, 1, 1],
}
SIGNAL_LAST = {"equal_weight": "0.2784", "slope20": "0.0461", "hybrid20": "0", "citic40d": "0.4318"}


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join(str(v) for v in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_tree(root: Path, *, drop_last: tuple[str, str] | None = None) -> dict:
    """造出所有推送对象 + 信号文件，返回与之匹配的 OK 状态字典。drop_last：该 (线, 口径) 少最后一天。"""
    from backtest.baseline import SIGNALS
    from backtest.production import recommended_file
    files = {}
    for (name, m), pos in POSITIONS.items():
        rel = recommended_file(name, m)
        rows = [[d, p] for d, p in zip(DATES, pos)]
        if drop_last == (name, m):
            rows = rows[:-1]
        _write_csv(root / rel, ["date", "position"], rows)
        files[f"{name}_{m}"] = {"path": rel, "last_date": rows[-1][0], "gated": True}
    for name, last in SIGNAL_LAST.items():
        rel, col = SIGNALS[name]
        _write_csv(root / rel, ["date", col], [[d, "0.1"] for d in DATES[:-1]] + [[DATES[-1], last]])
    return {
        "result": "OK", "failed_step": None, "finished_at": "2026-09-22T18:31:05+08:00",
        "topup": "OK", "topup_reason": None,
        "upstream": {"max_trade_date": DATES[-1], "gaps": []},
        "files": files, "max_lag_trading_days": 0, "max_lag_allowed": 1,
        "output_gap_total": 0, "breaches": [], "upstream_breach": None,
    }


def compose(tmp_path, status=None, today="2026-09-22", **kw):
    st = make_tree(tmp_path, **kw) if status is None else status
    text, as_of = nw.compose_message(tmp_path, st, today=today)
    return text, as_of, text.split("\n")


def test_two_pools_come_first_with_run_and_value(tmp_path):
    text, as_of, lines = compose(tmp_path)
    assert as_of == "2026-09-22"
    assert lines[0] == "风格择时 信号日 2026-09-22｜链路 OK"
    assert lines[1] == "【期货池】equal_weight 对称：持多 +1（09-17 起第 4 日）信号值 0.2784"
    assert lines[2] == "【现货池】slope20 long-flat：⚡翻仓 空仓 0 → 持多 +1 信号值 0.0461"


def test_other_production_lines_listed_once_as_reference(tmp_path):
    text, _, lines = compose(tmp_path)
    ref = lines[lines.index("【其余生产线·参考】") + 1:]
    assert "  slope20 对称：⚡翻仓 持空 -1 → 持多 +1 信号值 0.0461" in ref
    assert "  hybrid20 long-flat：空仓 0（09-16 起第 5 日）信号值 0" in ref
    assert "  citic40d long-flat：持多 +1（09-16 起第 5 日）信号值 0.4318" in ref
    assert text.count("equal_weight") == 1          # 期货池已置顶，参考区不重复
    assert "equal_weight long-flat" not in text     # 参照口径不推


def test_header_says_today_when_signal_day_differs(tmp_path):
    _, _, lines = compose(tmp_path, today="2026-10-01")
    assert lines[0] == "风格择时 信号日 2026-09-22（今天 10-01）｜链路 OK"


def test_line_behind_signal_day_is_marked(tmp_path):
    _, _, lines = compose(tmp_path, drop_last=("citic40d", "longflat"))
    assert "  citic40d long-flat：持多 +1（09-16 起第 4 日）（末行 09-21）信号值 0.1" in lines


def test_health_line_when_everything_ok(tmp_path):
    _, _, lines = compose(tmp_path)
    assert lines[-1] == "topup OK · 护栏 OK（最大落后 0 交易日 · 缺口 0）"


def test_degraded_topup_and_upstream_gaps_are_flagged(tmp_path):
    st = make_tree(tmp_path)
    st["topup"], st["topup_reason"] = "TOPUP_SKIPPED", "前置闸门：额度不足"
    st["upstream"]["gaps"] = ["2026-09-18"]
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-3] == "护栏 OK（最大落后 0 交易日 · 缺口 0）"
    assert lines[-2] == "⚠ topup TOPUP_SKIPPED：前置闸门：额度不足"
    assert lines[-1] == "⚠ 上游缺 1 天：2026-09-18"


# 2026-09-23 起输入由办公室写入（标志文件 SKIP_TOPUP 常驻），第 0 步记 OFFICE_*：到齐是常态，不该天天挂 ⚠。
OFFICE_LATE_REASON = "2026-09-22 截至 21:30 仍缺 1 码：000300.SH，按库内已有数据照算"
OFFICE_ERROR_REASON = "2026-09-22 到齐检查出错：OperationalError: timeout expired"


def test_office_ok_health_line_has_no_warning(tmp_path):
    st = make_tree(tmp_path)
    st["topup"], st["topup_reason"] = "OFFICE_OK", "办公室日更 2026-09-22 15 码到齐（等 0 秒）"
    text, _, lines = compose(tmp_path, status=st)
    assert lines[-1] == "输入 办公室日更 ✓ · 护栏 OK（最大落后 0 交易日 · 缺口 0）"
    assert "⚠" not in text and "topup" not in text


@pytest.mark.parametrize("topup, reason, flagged", [
    ("OFFICE_LATE", OFFICE_LATE_REASON, f"⚠ 输入未到齐：{OFFICE_LATE_REASON}"),
    ("OFFICE_CHECK_ERROR", OFFICE_ERROR_REASON, f"⚠ 输入到齐检查出错：{OFFICE_ERROR_REASON}"),
    ("OFFICE_LATE", None, "⚠ 输入未到齐：未记原因"),
    ("TOPUP_SKIPPED", "环境变量 STYLE_SIGNALS_SKIP_TOPUP=1", "⚠ topup TOPUP_SKIPPED：环境变量 STYLE_SIGNALS_SKIP_TOPUP=1"),
    ("DEGRADED", "topup 调用失败 exit 1", "⚠ topup DEGRADED：topup 调用失败 exit 1"),
], ids=["office-late", "office-check-error", "office-late-no-reason", "topup-skipped", "degraded"])
def test_input_problems_are_flagged(tmp_path, topup, reason, flagged):
    """办公室迟到 / 到齐检查出错各有一行 ⚠（照算的信号可能停在前一交易日，人要知道为什么）；
    topup 模式（回退）的各状态仍是原来的「⚠ topup <状态>」。"""
    st = make_tree(tmp_path)
    st["topup"], st["topup_reason"] = topup, reason
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-2:] == ["护栏 OK（最大落后 0 交易日 · 缺口 0）", flagged]


def test_office_ok_keeps_upstream_gap_warning(tmp_path):
    st = make_tree(tmp_path)
    st["topup"], st["topup_reason"] = "OFFICE_OK", "办公室日更 2026-09-22 15 码到齐（等 0 秒）"
    st["upstream"]["gaps"] = ["2026-09-18"]
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-2:] == ["输入 办公室日更 ✓ · 护栏 OK（最大落后 0 交易日 · 缺口 0）", "⚠ 上游缺 1 天：2026-09-18"]


def test_refuses_when_guard_result_not_ok(tmp_path):
    st = make_tree(tmp_path)
    st["result"] = "STALE"
    with pytest.raises(nw.Refused, match="STALE"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_file_not_vouched_by_guard(tmp_path):
    st = make_tree(tmp_path)
    st["files"]["slope20_longflat"]["gated"] = False
    with pytest.raises(nw.Refused, match="slope20_longflat.csv"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_file_changed_after_guard(tmp_path):
    st = make_tree(tmp_path)
    st["files"]["equal_weight_symmetric"]["last_date"] = "2026-09-21"
    with pytest.raises(nw.Refused, match="2026-09-21"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_file_rolled_back_after_guard(tmp_path):
    """反方向：护栏核验时末行 09-22，推送时文件末行退回 09-21（被旧版本覆盖）。"""
    st = make_tree(tmp_path, drop_last=("equal_weight", "symmetric"))
    st["files"]["equal_weight_symmetric"]["last_date"] = "2026-09-22"
    with pytest.raises(nw.Refused, match="末行 2026-09-21 与护栏核验时的 2026-09-22 不一致"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_reference_line_not_vouched(tmp_path):
    """担保校验不只管两池：参考行同样必须是护栏对象。"""
    st = make_tree(tmp_path)
    st["files"]["citic40d_longflat"]["gated"] = False
    with pytest.raises(nw.Refused, match="citic40d_longflat.csv 不是护栏对象"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_reference_line_changed_after_guard(tmp_path):
    st = make_tree(tmp_path)
    st["files"]["hybrid20_longflat"]["last_date"] = "2026-09-21"
    with pytest.raises(nw.Refused, match="hybrid20_longflat.csv 末行 2026-09-22"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_fractional_position_is_not_truncated(tmp_path):
    """分数仓位原样显示，不能被截成 0（int(float("-0.5")) == 0 会把持空报成空仓）。"""
    st = make_tree(tmp_path)
    rel = st["files"]["equal_weight_symmetric"]["path"]
    _write_csv(tmp_path / rel, ["date", "position"],
               [[d, p] for d, p in zip(DATES, [-1, 1, 1, -0.5, -0.5])])
    _, _, lines = compose(tmp_path, status=st)
    assert lines[1] == "【期货池】equal_weight 对称：持空 -0.5（09-21 起第 2 日）信号值 0.2784"


def test_non_finite_position_is_refused(tmp_path):
    """nan/inf 持仓拒推——既不能当成「空仓 0」发出去，也不能漏成裸异常。"""
    st = make_tree(tmp_path)
    rel = st["files"]["equal_weight_symmetric"]["path"]
    _write_csv(tmp_path / rel, ["date", "position"],
               [[d, p] for d, p in zip(DATES, [-1, 1, 1, 1, "nan"])])
    with pytest.raises(nw.Refused, match="equal_weight_symmetric.csv"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


@pytest.mark.parametrize("bad", ["", "abc"])
def test_unparseable_position_is_refused(tmp_path, bad):
    """pandas 把 NaN 写成空串、或混进非数字：与 nan/inf 走同一条友好拒推路径，不漏成裸异常。"""
    st = make_tree(tmp_path)
    rel = st["files"]["equal_weight_symmetric"]["path"]
    _write_csv(tmp_path / rel, ["date", "position"],
               [[d, p] for d, p in zip(DATES, [-1, 1, 1, 1, bad])])
    with pytest.raises(nw.Refused) as exc:
        nw.compose_message(tmp_path, st, today="2026-09-22")
    assert str(exc.value) == f"{rel} 在 2026-09-22 的持仓 {bad!r} 不是数"


def test_missing_signal_value_shows_dash(tmp_path):
    st = make_tree(tmp_path)
    from backtest.baseline import SIGNALS
    rel, col = SIGNALS["citic40d"]
    _write_csv(tmp_path / rel, ["date", col], [[d, "0.1"] for d in DATES[:-1]])   # 信号文件缺 09-22
    _, _, lines = compose(tmp_path, status=st)
    assert "  citic40d long-flat：持多 +1（09-16 起第 5 日）信号值 —" in lines


def test_upstream_gaps_beyond_five_are_elided(tmp_path):
    st = make_tree(tmp_path)
    st["upstream"]["gaps"] = [f"2026-09-0{i}" for i in range(1, 8)]
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-1] == "⚠ 上游缺 7 天：2026-09-01、2026-09-02、2026-09-03、2026-09-04、2026-09-05 等"


def test_blank_trailing_rows_are_ignored(tmp_path):
    """date 为空/纯空白的行跳过（与护栏 dates_of 口径一致），不能把末尾空白行当持仓。"""
    from backtest.baseline import SIGNALS
    st = make_tree(tmp_path)
    for rel in (st["files"]["equal_weight_symmetric"]["path"], SIGNALS["equal_weight"][0]):
        path = tmp_path / rel
        path.write_text(path.read_text(encoding="utf-8") + "\n   \n", encoding="utf-8")
    _, _, lines = compose(tmp_path, status=st)
    assert lines[1] == "【期货池】equal_weight 对称：持多 +1（09-17 起第 4 日）信号值 0.2784"


def test_missing_status_fields_show_dash_not_none(tmp_path):
    st = make_tree(tmp_path)
    del st["max_lag_trading_days"], st["output_gap_total"], st["topup"]
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-2:] == ["护栏 OK（最大落后 — 交易日 · 缺口 —）", "⚠ topup —：未记原因"]


def test_truncate_keeps_head_and_says_how_many_dropped():
    src = ["头一行"] + [f"第 {i} 行 " + "长" * 40 for i in range(60)]
    out = nw.truncate_text("\n".join(src), limit=2048, note="见日志")
    assert len(out.encode("utf-8")) <= 2048
    assert out.startswith("头一行\n第 0 行")
    assert out.endswith("行，见日志）") and "还有" in out
    kept = out.split("\n")[:-1]
    assert kept == src[:len(kept)]
    assert re.search(r"还有 (\d+) 行", out).group(1) == str(len(src) - len(kept))


def test_truncate_counts_bytes_not_characters():
    text = "\n".join(["汉" * 100] * 8)          # 800 个汉字：2400+ 字节，却不到 2048 个字符
    assert len(text) < 2048 < len(text.encode("utf-8"))
    out = nw.truncate_text(text, limit=2048)
    assert out != text and len(out.encode("utf-8")) <= 2048


def test_truncate_leaves_exactly_limit_bytes_alone():
    text = "汉" * 682 + "ab"                    # 2046 + 2 = 恰好 2048 字节
    assert len(text.encode("utf-8")) == 2048
    assert nw.truncate_text(text, limit=2048) == text


def test_real_message_fits_wechat_limit(tmp_path):
    text, _, _ = compose(tmp_path)
    assert len(text.encode("utf-8")) <= nw.WECHAT_TEXT_LIMIT


@pytest.fixture
def stub():
    """本地 webhook 桩：记录收到的 JSON；reply 可改成非 0 errcode。"""
    state = {"bodies": [], "reply": {"errcode": 0, "errmsg": "ok"}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            state["bodies"].append(json.loads(self.rfile.read(n).decode("utf-8")))
            data = json.dumps(state["reply"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/cgi-bin/webhook/send?key={KEY_STUB}"
    yield state
    server.shutdown()
    server.server_close()


def test_send_posts_text_message_and_bypasses_proxy(stub, monkeypatch):
    # 代理指向一个必死端口：若 send_text 走环境代理，这次请求必然失败。
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    nw.send_text(stub["url"], "你好\n第二行", wait=0)
    assert stub["bodies"] == [{"msgtype": "text", "text": {"content": "你好\n第二行"}}]


def test_send_raises_on_nonzero_errcode(stub):
    stub["reply"] = {"errcode": 93000, "errmsg": "invalid webhook url"}
    with pytest.raises(nw.SendError, match="93000"):
        nw.send_text(stub["url"], "x", wait=0)
    assert len(stub["bodies"]) == 1          # errcode 是配置问题，不重试


class _FlakyOpener:
    def __init__(self, fail_times, url):
        self.calls, self.fail_times, self.url = 0, fail_times, url

    def open(self, req, timeout):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise urllib.error.URLError(f"boom while calling {self.url}")
        return _Resp(b'{"errcode": 0, "errmsg": "ok"}')


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_retries_network_error_once():
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY_SECRET}"
    opener = _FlakyOpener(1, url)
    nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2


def test_send_error_never_leaks_webhook_key():
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY_SECRET}"
    opener = _FlakyOpener(5, url)
    with pytest.raises(nw.SendError) as exc:
        nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2
    assert not _leaked(str(exc.value), KEY_SECRET) and exc.value.__cause__ is None
    # `from None` 的真实效果：隐式 __context__（带完整 URL 的原异常）不会随 traceback 打出来
    assert exc.value.__suppress_context__


class _ScriptedOpener:
    """按剧本逐次抛出给定异常，剧本用完后回 errcode 0。"""

    def __init__(self, *errors):
        self.errors, self.calls = list(errors), 0

    def open(self, req, timeout):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _Resp(b'{"errcode": 0, "errmsg": "ok"}')


def test_send_retries_http_protocol_error_once():
    """读回包时断流：IncompleteRead 属 http.client.HTTPException（不是 OSError），同样重试一次。"""
    opener = _ScriptedOpener(http.client.IncompleteRead(b""))
    nw.send_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K", "x", wait=0, opener=opener)
    assert opener.calls == 2


def test_send_wraps_ssl_error_as_send_error():
    """ssl.SSLError 是 OSError 但不是 ConnectionError：两次都抛也要收成 SendError，不能漏成裸异常。"""
    opener = _ScriptedOpener(ssl.SSLError("x"), ssl.SSLError("x"))
    with pytest.raises(nw.SendError):
        nw.send_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K", "x", wait=0,
                     opener=opener)
    assert opener.calls == 2


@pytest.mark.parametrize("reply", [{}, [], {"errmsg": "ok"}])
def test_send_fails_closed_unless_errcode_is_zero(stub, reply):
    """回包必须是对象且 errcode 恰为 0 才算送达；{} / 非对象 / 缺 errcode 一律当失败。"""
    stub["reply"] = reply
    with pytest.raises(nw.SendError):
        nw.send_text(stub["url"], "x", wait=0)


BAD_URL = f"qyapi.example.invalid/cgi-bin/webhook/send?key={KEY_FAKE}"   # 缺 scheme：Request() 抛 ValueError


def test_send_rejects_invalid_url_without_leaking_or_retrying():
    """URL 本身不合法：报错原文带整条 URL（含 key），必须收成抹过的 SendError，且不重试。"""
    opener = _ScriptedOpener()
    with pytest.raises(nw.SendError, match="webhook URL 无效") as exc:
        nw.send_text(BAD_URL, "x", wait=0, opener=opener)
    assert not _leaked(str(exc.value), KEY_FAKE) and exc.value.__suppress_context__
    assert opener.calls == 0


def test_send_error_scrubs_key_from_http_client_messages():
    """http.client 的报错只带「路径+查询串」而非整条 URL：靠抹 key 值兜住。"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY_SECRET}"
    err = http.client.InvalidURL("URL can't contain control characters. "
                                 f"'/cgi-bin/webhook/send?key={KEY_SECRET}' (found at least ' ')")
    opener = _ScriptedOpener(err, err)
    with pytest.raises(nw.SendError) as exc:
        nw.send_text(url, "x", wait=0, opener=opener)
    assert not _leaked(str(exc.value), KEY_SECRET) and opener.calls == 2


def test_scrub_masks_key_params_even_without_url():
    """url 为空（还没解析出来就出错）也要兜底抹掉任何 key=…。"""
    assert (nw._scrub(f"x key={KEY_SECRET}&y=1 'key={KEY_FAKE}' key=", "")
            == "x key=<key>&y=1 'key=<key>' key=")


def test_scrub_masks_bare_key_value_when_url_known():
    """报错里只有裸 key 值、不带 key=（如服务端回显）：url 已知就按值抹掉，不能只靠 key= 正则。"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY_SECRET}"
    assert nw._scrub(f"token {KEY_SECRET} 无效", url) == "token <key> 无效"


def test_webhook_from_env_file_and_env_var_precedence(tmp_path, monkeypatch):
    env = tmp_path / "alert.env"
    env.write_text("# 注释\n\nexport OTHER=1\nALERT_WEBHOOK_URL='https://example.invalid/x?key=abc'\n",
                   encoding="utf-8")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert nw.load_env_file(env)["OTHER"] == "1"          # export 前缀
    assert nw.resolve_webhook(env) == "https://example.invalid/x?key=abc"
    assert nw.resolve_webhook(tmp_path / "missing.env") == ""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/y?key=def")
    assert nw.resolve_webhook(env) == "https://example.invalid/y?key=def"


def test_env_file_inline_comment_follows_bash_semantics(tmp_path):
    """未加引号：空白之后的 # 起为行内注释（bash 语义）；加引号：引号内原样，# 不动。"""
    env = tmp_path / "alert.env"
    env.write_text("PLAIN=abc  # 说明\n"
                   "HASH_IN_WORD=a#b\n"
                   "QUOTED='x#y # 不是注释'\n"
                   "QUOTED_THEN_COMMENT=\"abc\"  # 说明\n", encoding="utf-8")
    got = nw.load_env_file(env)
    assert got["PLAIN"] == "abc"
    assert got["HASH_IN_WORD"] == "a#b"                   # 词中间的 # 不是注释
    assert got["QUOTED"] == "x#y # 不是注释"
    assert got["QUOTED_THEN_COMMENT"] == "abc"


def _non_utf8_env(tmp_path, url) -> Path:
    """注释行被 PowerShell 写成 GBK 的 env 文件：一行非 UTF-8 字节 + 一行合法的 webhook。"""
    env = tmp_path / "alert.env"
    env.write_bytes(b"# \xff\xfe " + "坏注释".encode("gbk") + b"\n"
                    + f"ALERT_WEBHOOK_URL={url}\n".encode("utf-8"))
    return env


def test_env_file_with_non_utf8_bytes_still_resolves(tmp_path, stub):
    assert nw.resolve_webhook(_non_utf8_env(tmp_path, stub["url"])) == stub["url"]


def test_alert_dry_run_survives_non_utf8_env_file(tmp_path, stub, capsys):
    """env 文件编码坏了，main 也不能在分派前崩掉——告警正文照样打出来。"""
    env = _non_utf8_env(tmp_path, stub["url"])
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d"})
    assert nw.main(["--alert", "--dry-run", "--status-file", str(p), "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "⚠ 风格择时日更链失败" in out and "失败步骤 citic40d" in out
    assert "webhook 已配置" in out and not _leaked(out, KEY_STUB) and stub["bodies"] == []


def _status_file(tmp_path, status):
    p = tmp_path / "logs" / "daily_signals_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def _argv(tmp_path, status_path, *extra):
    return ["--root", str(tmp_path), "--status-file", str(status_path),
            "--env-file", str(tmp_path / "no-such.env"), "--today", "2026-09-22", *extra]


def test_dry_run_prints_sends_nothing_and_leaves_status_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    p = _status_file(tmp_path, make_tree(tmp_path))
    before = p.read_bytes()
    assert nw.main(_argv(tmp_path, p, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "风格择时 信号日 2026-09-22｜链路 OK" in out
    assert out.rstrip("\n").endswith("（--dry-run：未发送，未写状态文件；webhook 未配置）")
    assert p.read_bytes() == before


def test_dry_run_reports_webhook_configured_without_leaking(tmp_path, monkeypatch, stub, capsys):
    """dry-run 只报「已配置/未配置」这个布尔，便于核对配置而不暴露密钥。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "（--dry-run：未发送，未写状态文件；webhook 已配置）" in out
    assert not _leaked(out, KEY_STUB) and stub["bodies"] == []


def test_push_sends_and_records_notify_block(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 0
    content = stub["bodies"][0]["text"]["content"]
    assert content.startswith("风格择时 信号日 2026-09-22")
    st = json.loads(p.read_text(encoding="utf-8"))
    assert st["notify"]["sent"] is True and st["notify"]["as_of"] == "2026-09-22"
    assert st["notify"]["error"] is None and st["result"] == "OK" and "files" in st
    assert st["notify"]["bytes"] == len(content.encode("utf-8"))


def test_push_with_schemeless_webhook_never_leaks_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", BAD_URL)
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    out, err = capsys.readouterr()
    assert not _leaked(out + err, KEY_FAKE) and "SEND_FAILED: webhook URL 无效" in err
    assert not _leaked(p.read_text(encoding="utf-8"), KEY_FAKE)
    assert "webhook URL 无效" in json.loads(p.read_text(encoding="utf-8"))["notify"]["error"]


def test_record_notify_uses_hidden_temp_and_cleans_up_on_failure(tmp_path, monkeypatch):
    p = _status_file(tmp_path, {"result": "OK"})
    before = p.read_bytes()
    seen = []

    def failing_replace(src, dst):
        seen.append(Path(src).name)
        raise OSError("disk full")

    monkeypatch.setattr(nw.os, "replace", failing_replace)
    with pytest.raises(OSError):
        nw.record_notify(p, {"sent": True})
    assert seen == [".daily_signals_status.json.tmp"]
    assert [x.name for x in p.parent.iterdir()] == ["daily_signals_status.json"]
    assert p.read_bytes() == before


def test_push_without_webhook_fails_and_records(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and "ALERT_WEBHOOK_URL" in notify["error"]


def test_push_rejected_by_wechat_records_error(tmp_path, monkeypatch, stub):
    stub["reply"] = {"errcode": 45009, "errmsg": "api freq out of limit"}
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and "45009" in notify["error"]
    assert not _leaked(json.dumps(notify), KEY_STUB)


def test_push_refused_when_guard_not_ok(tmp_path, monkeypatch, stub, capsys):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    st = make_tree(tmp_path)
    st["result"] = "STALE"
    p = _status_file(tmp_path, st)
    assert nw.main(_argv(tmp_path, p)) == 1
    assert stub["bodies"] == [] and "REFUSED" in capsys.readouterr().err


def test_push_refused_by_vouching_is_recorded(tmp_path, monkeypatch, stub):
    """拒推（推送对象未经护栏担保）也要记进 notify 段——否则告警器会误报「状态文件没记下失败原因」。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    st = make_tree(tmp_path)
    st["files"]["slope20_longflat"]["gated"] = False
    p = _status_file(tmp_path, st)
    assert nw.main(_argv(tmp_path, p)) == 1
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and "不是护栏对象" in notify["error"]
    assert stub["bodies"] == []


def test_push_unexpected_error_is_recorded_and_traceback_scrubbed(tmp_path, monkeypatch, stub, capsys):
    """Refused/SendError 以外的异常：记账（类型名 + 抹掉 URL）；main 打出抹过密钥的堆栈并返回 1。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])

    def boom(url, text, **kw):
        raise ValueError(f"boom {url}")

    monkeypatch.setattr(nw, "send_text", boom)
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "ValueError: boom <webhook>" in err and not _leaked(err, KEY_STUB)
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and notify["error"] == "ValueError: boom <webhook>"
    assert stub["bodies"] == []


def test_dry_run_refusal_leaves_status_alone(tmp_path, monkeypatch, capsys):
    """--dry-run 永远不写状态文件，拒推时也一样（白天预览不能覆盖当晚的记账）。"""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    st = make_tree(tmp_path)
    st["files"]["slope20_longflat"]["gated"] = False
    p = _status_file(tmp_path, st)
    before = p.read_bytes()
    assert nw.main(_argv(tmp_path, p, "--dry-run")) == 1
    assert "REFUSED" in capsys.readouterr().err
    assert p.read_bytes() == before


NOW = "2026-09-23 18:31:07"


def test_alert_for_failed_step():
    st = {"result": "FAILED", "failed_step": "topup_audit(SUSPECT)",
          "finished_at": "2026-09-23T18:31:05+08:00", "topup": "SUSPECT",
          "topup_reason": "事后审计判定写入可疑（exit 1）"}
    lines = nw.build_alert(st, systemd_result="exit-code", now=NOW).split("\n")
    assert lines[0] == "⚠ 风格择时日更链失败｜2026-09-23 18:31:07"
    assert lines[1] == "结果 FAILED · 失败步骤 topup_audit(SUSPECT) · 状态写于 2026-09-23T18:31:05+08:00"
    assert "topup SUSPECT：事后审计判定写入可疑（exit 1）" in lines
    assert "systemd Result=exit-code" in lines
    assert lines[-1] == "持仓未更新/未送达，以上一次推送为准；处置见 logs/ALERT_daily_signals"


def test_alert_for_stale_lists_first_three_breaches():
    st = {"result": "STALE", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK",
          "breaches": [f"b{i}" for i in range(5)]}
    lines = nw.build_alert(st, systemd_result="", now=NOW).split("\n")
    assert [l for l in lines if l.startswith("护栏：")] == ["护栏：b0", "护栏：b1", "护栏：b2"]
    assert not any(l.startswith("systemd") for l in lines)


def test_alert_skips_office_ok_topup_line():
    """输入到齐（OFFICE_OK）是常态，失败通知里不提；失败原因在护栏行。"""
    st = {"result": "STALE", "finished_at": "2026-09-23T20:31:05+08:00", "topup": "OFFICE_OK",
          "topup_reason": "办公室日更 2026-09-23 15 码到齐（等 0 秒）", "breaches": ["b0"]}
    text = nw.build_alert(st, systemd_result="exit-code", now="2026-09-23 20:31:07")
    assert "topup" not in text and "办公室日更" not in text and "护栏：b0" in text


@pytest.mark.parametrize("topup", ["OFFICE_LATE", "OFFICE_CHECK_ERROR", "OFFICE_SUSPECT"])
def test_alert_keeps_office_problem_topup_line(topup):
    """办公室迟到 / 检查出错照旧出 topup 行：它多半就是护栏落后的原因；同族哨兵拦下的中止（OFFICE_SUSPECT）
    也出——那一行就是中止的原因。"""
    st = {"result": "STALE", "finished_at": "2026-09-23T21:31:05+08:00", "topup": topup,
          "topup_reason": OFFICE_LATE_REASON, "breaches": ["b0"]}
    lines = nw.build_alert(st, systemd_result="exit-code", now="2026-09-23 21:31:07").split("\n")
    assert f"topup {topup}：{OFFICE_LATE_REASON}" in lines


def test_alert_when_status_missing():
    text = nw.build_alert(None, systemd_result="timeout", now=NOW)
    assert "状态文件缺失或无法解析" in text and "systemd Result=timeout" in text


def test_alert_when_status_ok_but_unit_failed():
    st = {"result": "OK", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK"}
    text = nw.build_alert(st, systemd_result="timeout", now=NOW)
    assert "状态文件没记下失败原因" in text


def test_alert_reports_notify_error_instead():
    st = {"result": "OK", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK",
          "notify": {"sent": False, "error": "网络错误（共试 2 次）：timed out"}}
    text = nw.build_alert(st, systemd_result="exit-code", now=NOW)
    assert "推送失败：网络错误（共试 2 次）：timed out" in text
    assert "状态文件没记下失败原因" not in text


def test_alert_shows_upstream_breach_and_check_error():
    upstream = {"result": "UPSTREAM_STALE", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK",
                "upstream_breach": "上游 index_daily 最新交易日 2026-09-10 距今 13 个自然日 > 7"}
    lines = nw.build_alert(upstream, systemd_result="exit-code", now=NOW).split("\n")
    assert "上游：上游 index_daily 最新交易日 2026-09-10 距今 13 个自然日 > 7" in lines
    check_error = {"result": "CHECK_ERROR", "finished_at": "2026-09-23T18:31:05+08:00",
                   "topup": "OK", "error": "OperationalError: could not connect"}
    lines = nw.build_alert(check_error, systemd_result="exit-code", now=NOW).split("\n")
    assert "检查出错：OperationalError: could not connect" in lines


def test_alert_does_not_blame_stale_status_file():
    """状态文件不是今天写的 = 上一次运行留下的：只说这一句，不把旧原因安到这次头上。"""
    st = {"result": "STALE", "failed_step": "citic40d", "finished_at": "2026-09-22T18:31:05+08:00",
          "topup": "SUSPECT", "topup_reason": "旧原因", "breaches": ["旧 breach"],
          "upstream_breach": "旧上游", "error": "旧错误", "notify": {"sent": False, "error": "旧推送错误"}}
    text = nw.build_alert(st, systemd_result="timeout", now=NOW)
    assert text.split("\n") == [
        "⚠ 风格择时日更链失败｜2026-09-23 18:31:07",
        "状态文件停在 2026-09-22T18:31:05+08:00，不是本次运行写的——本次在写状态前就死了（看 systemd Result）",
        "systemd Result=timeout",
        "持仓未更新/未送达，以上一次推送为准；处置见 logs/ALERT_daily_signals",
    ]


def test_alert_missing_fields_show_dash_not_none():
    text = nw.build_alert({"failed_step": "citic40d"}, systemd_result="", now=NOW)
    assert "结果 — · 失败步骤 citic40d · 状态写于 —" in text and "None" not in text


STALE_LINE = "状态文件停在 {}，不是本次运行写的——本次在写状态前就死了（看 systemd Result）"


def test_alert_stale_when_status_written_before_this_run_started():
    """(a) 同日但早于本次 unit 启动 = 上一次运行留下的：日期判定看不出来，run_started 看得出来。"""
    st = {"result": "STALE", "finished_at": "2026-09-22T18:10:00+08:00", "topup": "OK",
          "breaches": ["旧 breach"]}
    text = nw.build_alert(st, systemd_result="timeout", now="2026-09-22 18:40:00", run_started=RUN_STARTED)
    assert text.split("\n")[1] == STALE_LINE.format("2026-09-22T18:10:00+08:00")
    assert "旧 breach" not in text
    # (d) 不给 run_started：退回「日期不同」判定——同日即照常引用（正是 run_started 要补的盲区）
    assert "护栏：旧 breach" in nw.build_alert(st, systemd_result="timeout", now="2026-09-22 18:40:00")


def test_alert_not_stale_across_midnight_when_written_after_start():
    """(b) 23:59 启动、23:59:30 写状态、00:00 才告警：日期不同但确是本次写的 → 照常报字段。"""
    start = datetime.fromisoformat("2026-09-22T23:59:00+08:00").timestamp()
    st = {"result": "STALE", "finished_at": "2026-09-22T23:59:30+08:00", "topup": "OK", "breaches": ["b0"]}
    lines = nw.build_alert(st, systemd_result="", now="2026-09-23 00:00:10", run_started=start).split("\n")
    assert lines[1] == "结果 STALE · 失败步骤 — · 状态写于 2026-09-22T23:59:30+08:00" and "护栏：b0" in lines
    # (d) 不给 run_started：退回「日期不同」判定 → 陈旧（既有行为不变）
    assert "不是本次运行写的" in nw.build_alert(st, systemd_result="", now="2026-09-23 00:00:10")


def test_alert_unparseable_finished_at_is_not_stale_with_run_started():
    """给了 run_started 但 finished_at 解析不了：视为不陈旧，照常报字段。"""
    st = {"result": "FAILED", "failed_step": "citic40d", "finished_at": "昨天晚上", "topup": "OK"}
    text = nw.build_alert(st, systemd_result="", now="2026-09-22 18:40:00", run_started=RUN_STARTED)
    assert "结果 FAILED · 失败步骤 citic40d · 状态写于 昨天晚上" in text


def test_run_started_accepts_systemctl_unix_forms():
    """(c) `@<unix 秒>`（systemctl --timestamp=unix 的原样输出）与裸秒数等价；空串 = 没给。"""
    assert nw.parse_run_started(f"@{RUN_STARTED}") == nw.parse_run_started(str(RUN_STARTED)) == RUN_STARTED
    assert nw.parse_run_started("") is None and nw.parse_run_started(None) is None
    assert nw.parse_run_started("n/a") is None          # 解析不了：退回日期判定，告警器不能因此失败


def test_alert_cli_takes_run_started(tmp_path, capsys):
    p = _status_file(tmp_path, {"result": "STALE", "finished_at": "2026-09-22T18:10:00+08:00", "topup": "OK"})
    assert nw.main(["--alert", "--dry-run", "--status-file", str(p), "--run-started", f"@{RUN_STARTED}"]) == 0
    assert STALE_LINE.format("2026-09-22T18:10:00+08:00") in capsys.readouterr().out


def test_alert_cli_sends(tmp_path, monkeypatch, stub):
    """不依赖当天日期：状态写于 18:31:05，晚于本次 unit 启动（18:30:50）→ 确是本次写的。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, {"result": "STALE", "finished_at": "2026-09-22T18:31:05+08:00", "topup": "OK",
                                "breaches": ["recommended_slope20 落后 2 交易日"]})
    assert nw.main(["--alert", "--status-file", str(p), "--env-file", str(tmp_path / "x.env"),
                    "--systemd-result", "exit-code", "--run-started", str(RUN_STARTED)]) == 0
    assert "护栏：recommended_slope20 落后 2 交易日" in stub["bodies"][0]["text"]["content"]


def test_alert_with_schemeless_webhook_never_leaks_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", BAD_URL)
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d", "finished_at": _today_iso()})
    before = p.read_bytes()
    assert nw.main(["--alert", "--status-file", str(p), "--systemd-result", "exit-code"]) == 1
    out, err = capsys.readouterr()
    assert not _leaked(out + err, KEY_FAKE) and "SEND_FAILED: webhook URL 无效" in err
    assert p.read_bytes() == before                      # 告警模式从不写状态文件


def test_alert_without_webhook_fails(tmp_path, capsys):
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d", "finished_at": _today_iso()})
    assert nw.main(["--alert", "--status-file", str(p)]) == 1
    assert "REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize("content", ['{"result": "OK", "fil', "null", "[]"])
def test_alert_dry_run_survives_broken_status_file(tmp_path, capsys, content):
    """告警器的职责是永远能报：截断的 JSON、合法但不是对象的 JSON 都按「无法解析」报出去。"""
    p = tmp_path / "status.json"
    p.write_text(content, encoding="utf-8")
    assert nw.main(["--alert", "--dry-run", "--status-file", str(p)]) == 0
    out = capsys.readouterr().out
    assert "状态文件缺失或无法解析" in out
    assert out.rstrip("\n").endswith("（--dry-run：未发送，未写状态文件；webhook 未配置）")


def test_alert_mode_never_imports_pandas(tmp_path):
    """科学栈坏了也得能报警：pandas / numpy / backtest 都不可导入，--alert 仍须跑通。
    子进程与真实 webhook 隔离：--env-file 指向不存在的文件，环境里去掉 ALERT_WEBHOOK_URL、HOME 指到 tmp。"""
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d"})
    code = ("import sys, runpy; "
            "sys.modules['pandas'] = None; sys.modules['numpy'] = None; sys.modules['backtest'] = None; "
            f"sys.argv = ['notify_wechat.py', '--alert', '--dry-run', '--status-file', {str(p)!r}, "
            f"'--env-file', {str(tmp_path / 'none.env')!r}]; "
            f"runpy.run_path({str(NOTIFY_PATH)!r}, run_name='__main__')")
    env = {k: v for k, v in os.environ.items() if k != "ALERT_WEBHOOK_URL"}
    env["HOME"] = str(tmp_path)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "失败步骤 citic40d" in proc.stdout and "webhook 未配置" in proc.stdout
