"""deploy/daily_signals/notify_wechat.py 单测（不连库、不连外网：发送一律打本地 HTTP 桩）。"""
import importlib.util
import json
import sys
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


def test_truncate_keeps_head_and_says_how_many_dropped():
    text = "\n".join(["头一行"] + [f"第 {i} 行 " + "长" * 40 for i in range(60)])
    out = nw.truncate_text(text, limit=2048, note="见日志")
    assert len(out.encode("utf-8")) <= 2048
    assert out.startswith("头一行\n第 0 行")
    assert out.endswith("行，见日志）") and "还有" in out


def test_real_message_fits_wechat_limit(tmp_path):
    text, _, _ = compose(tmp_path)
    assert len(text.encode("utf-8")) <= nw.WECHAT_TEXT_LIMIT


import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer


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
    state["url"] = f"http://127.0.0.1:{server.server_port}/cgi-bin/webhook/send?key=TESTKEY123"
    yield state
    server.shutdown()


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
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY42"
    opener = _FlakyOpener(1, url)
    nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2


def test_send_error_never_leaks_webhook_key():
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY42"
    opener = _FlakyOpener(5, url)
    with pytest.raises(nw.SendError) as exc:
        nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2
    assert "SECRETKEY42" not in str(exc.value) and exc.value.__cause__ is None


def test_webhook_from_env_file_and_env_var_precedence(tmp_path, monkeypatch):
    env = tmp_path / "alert.env"
    env.write_text("# 注释\n\nexport OTHER=1\nALERT_WEBHOOK_URL='https://example.invalid/x?key=abc'\n",
                   encoding="utf-8")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert nw.resolve_webhook(env) == "https://example.invalid/x?key=abc"
    assert nw.resolve_webhook(tmp_path / "missing.env") == ""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/y?key=def")
    assert nw.resolve_webhook(env) == "https://example.invalid/y?key=def"


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
    assert "风格择时 信号日 2026-09-22｜链路 OK" in capsys.readouterr().out
    assert p.read_bytes() == before


def test_push_sends_and_records_notify_block(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 0
    assert stub["bodies"][0]["text"]["content"].startswith("风格择时 信号日 2026-09-22")
    st = json.loads(p.read_text(encoding="utf-8"))
    assert st["notify"]["sent"] is True and st["notify"]["as_of"] == "2026-09-22"
    assert st["notify"]["error"] is None and st["result"] == "OK" and "files" in st


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
    assert "TESTKEY123" not in json.dumps(notify)


def test_push_refused_when_guard_not_ok(tmp_path, monkeypatch, stub, capsys):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    st = make_tree(tmp_path)
    st["result"] = "STALE"
    p = _status_file(tmp_path, st)
    assert nw.main(_argv(tmp_path, p)) == 1
    assert stub["bodies"] == [] and "REFUSED" in capsys.readouterr().err


import subprocess

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


def test_alert_cli_sends(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, {"result": "STALE", "finished_at": "x", "topup": "OK",
                                "breaches": ["recommended_slope20 落后 2 交易日"]})
    assert nw.main(["--alert", "--status-file", str(p), "--env-file", str(tmp_path / "x.env"),
                    "--systemd-result", "exit-code"]) == 0
    assert "护栏：recommended_slope20 落后 2 交易日" in stub["bodies"][0]["text"]["content"]


def test_alert_mode_never_imports_pandas(tmp_path):
    """科学栈坏了也得能报警：把 pandas 设成不可导入，--alert 仍须跑通。"""
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d"})
    code = ("import sys, runpy; sys.modules['pandas'] = None; "
            f"sys.argv = ['notify_wechat.py', '--alert', '--dry-run', '--status-file', {str(p)!r}]; "
            f"runpy.run_path({str(NOTIFY_PATH)!r}, run_name='__main__')")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "失败步骤 citic40d" in proc.stdout
