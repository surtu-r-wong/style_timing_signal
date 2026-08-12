"""deploy/daily_signals/topup_guard.py 的写库护栏单测（不连网、不连库）。

护栏的命题：**不可信响应零写入**。前置闸门的每条判据与事后审计的每条判据都是纯函数，
这里全部按「该拦的拦住、不该拦的放行」两侧覆盖。
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "deploy" / "daily_signals" / "topup_guard.py"


def _load_guard():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("topup_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# ───────────────────────────── 闸门 1：/ping + /health ─────────────────────────────

def test_health_passes_when_gateway_ok_and_wind_ready():
    assert guard.check_health({"status": "ok"}, {"status": "ok", "wind_ready": True}) is None


@pytest.mark.parametrize("ping,health", [
    ({"status": "error"}, {"status": "ok", "wind_ready": True}),
    ({"status": "ok"}, {"status": "error", "wind_ready": True}),
    ({"status": "ok"}, {"status": "ok", "wind_ready": False}),
    ({"status": "ok"}, {"status": "ok"}),                      # 缺 wind_ready
    ({"status": "ok"}, "<html>502 Bad Gateway</html>"),        # 非 JSON 载荷
])
def test_health_rejects_bad_payloads(ping, health):
    assert guard.check_health(ping, health) is not None


# ───────────────────────────── 闸门 2：/quota ─────────────────────────────

def test_quota_passes_with_headroom():
    assert guard.check_quota({"used": 4955403, "max": 500000000}) is None


@pytest.mark.parametrize("payload", [
    {"used": 500000000, "max": 500000000},   # 用满
    {"used": 500000001, "max": 500000000},   # 超用
    {"used": 1, "max": 0},                   # max 非正
    {"used": "many", "max": 500},            # 类型不对
    {"max": 500},                            # 缺 used
    "quota unavailable",                     # 非 JSON 对象
])
def test_quota_rejects_bad_payloads(payload):
    assert guard.check_quota(payload) is not None


def test_quota_gate_is_necessary_not_sufficient():
    """2026-08-12 实证：wsd 已耗尽时 /quota 仍报 ~1%，闸门只能否决不能背书。

    这里把那天的真实载荷钉下来——它**通过** /quota 闸门，正是为什么还需要事后审计
    与 SKIP_TOPUP 标志文件；若哪天有人想拿 /quota 当唯一依据，这条测试是反例。
    """
    assert guard.check_quota({"used": 4955403, "max": 500000000, "date": "2026-08-12"}) is None


# ───────────────────────────── 闸门 3：应有的最后一个交易日 ─────────────────────────────

@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 8, 12, 18, 30), "2026-08-12"),  # 周三收盘后 → 当天
    (datetime(2026, 8, 12, 9, 0), "2026-08-11"),    # 周三开盘前 → 前一工作日
    (datetime(2026, 8, 12, 15, 29), "2026-08-11"),  # 收盘前一分钟 → 还不算当天
    (datetime(2026, 8, 15, 20, 0), "2026-08-14"),   # 周六 → 回退到周五
    (datetime(2026, 8, 16, 20, 0), "2026-08-14"),   # 周日 → 回退到周五
    (datetime(2026, 8, 17, 9, 0), "2026-08-14"),    # 周一开盘前 → 上周五
])
def test_expected_last_trading_day(now, expected):
    assert guard.expected_last_trading_day(now).isoformat() == expected


# ───────────────────────────── 事后审计 ─────────────────────────────

def _snap(max_date, closes):
    return {"max_trade_date": max_date, "closes": closes}


BEFORE = _snap("2026-08-11", {
    "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0},
    "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0},
})


def test_audit_clean_when_one_genuine_new_day_appended():
    after = _snap("2026-08-12", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0, "2026-08-12": 102.5},
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0, "2026-08-12": 199.0},
    })
    assert guard.audit_changes(BEFORE, after, "2026-08-12") == []


def test_audit_clean_when_nothing_changed():
    assert guard.audit_changes(BEFORE, BEFORE, "2026-08-12") == []


def test_audit_flags_future_date():
    after = _snap("2026-09-30", {
        "CI005917.WI": {"2026-08-11": 101.0, "2026-09-30": 102.5},
        "CI005918.WI": {"2026-08-11": 202.0, "2026-09-30": 199.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("未来日期" in p for p in problems)


@pytest.mark.parametrize("bad", [None, 0.0, -5.0, float("nan"), float("inf")])
def test_audit_flags_illegal_close(bad):
    after = _snap("2026-08-12", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0, "2026-08-12": bad},
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0, "2026-08-12": 199.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("收盘价非法" in p for p in problems)


def test_audit_flags_previous_value_copy_placeholder():
    """所有指数的新日收盘价与前一交易日逐一相等 = 前值复制占位签名。"""
    after = _snap("2026-08-12", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0, "2026-08-12": 101.0},
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0, "2026-08-12": 202.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("前值复制占位日" in p for p in problems)


def test_audit_allows_single_index_unchanged_close():
    """只有一个指数恰好平收不是占位签名，不能误杀。"""
    after = _snap("2026-08-12", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0, "2026-08-12": 101.0},
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0, "2026-08-12": 205.0},
    })
    assert guard.audit_changes(BEFORE, after, "2026-08-12") == []


def test_audit_flags_rewritten_history():
    after = _snap("2026-08-11", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 999.0},   # 被改写
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("历史收盘价被改写" in p for p in problems)


def test_audit_flags_deleted_history():
    after = _snap("2026-08-11", {
        "CI005917.WI": {"2026-08-11": 101.0},                        # 08-10 消失
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("消失" in p for p in problems)


def test_audit_flags_backwards_max_date():
    after = _snap("2026-08-10", {
        "CI005917.WI": {"2026-08-10": 100.0, "2026-08-11": 101.0},
        "CI005918.WI": {"2026-08-10": 200.0, "2026-08-11": 202.0},
    })
    problems = guard.audit_changes(BEFORE, after, "2026-08-12")
    assert any("反而后退" in p for p in problems)


def test_audit_reports_when_snapshot_incomplete():
    problems = guard.audit_changes({"closes": {}}, {"closes": {}}, "2026-08-12")
    assert problems and "无法审计" in problems[0]


# ───────────────────────────── 退出码约定 ─────────────────────────────

def test_exit_code_constants_are_distinct():
    codes = {guard.EXIT_GO, guard.EXIT_SUSPECT, guard.EXIT_ERROR, guard.EXIT_SKIP}
    assert len(codes) == 4
    assert guard.EXIT_GO == 0


def test_preflight_is_fail_closed_on_guard_error(monkeypatch, tmp_path, capsys):
    """护栏自身抛异常时也必须判 SKIP（零写入），不能因为查不了就放行。"""
    monkeypatch.setattr(guard, "take_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PG 不可达")))
    monkeypatch.setattr(sys, "argv",
                        ["topup_guard.py", "--mode", "preflight",
                         "--snapshot", str(tmp_path / "s.json")])
    assert guard.main() == guard.EXIT_SKIP
    assert "SKIP_REASON=GUARD_ERROR" in capsys.readouterr().out


def test_preflight_skips_when_db_already_current(monkeypatch, tmp_path, capsys):
    """库内已到应有的最后一个交易日 → 无新数据可取，不必调用 topup（零 quota 消耗）。"""
    monkeypatch.setattr(guard, "take_snapshot",
                        lambda *a, **k: _snap("2026-08-12", {}))
    monkeypatch.setattr(guard, "expected_last_trading_day",
                        lambda now: __import__("datetime").date(2026, 8, 12))
    monkeypatch.setattr(sys, "argv",
                        ["topup_guard.py", "--mode", "preflight",
                         "--snapshot", str(tmp_path / "s.json")])
    assert guard.main() == guard.EXIT_SKIP
    assert "SKIP_REASON=NO_NEW_TRADING_DAY" in capsys.readouterr().out


def test_preflight_does_not_write_snapshot_when_skipping(monkeypatch, tmp_path):
    snap = tmp_path / "s.json"
    monkeypatch.setattr(guard, "take_snapshot", lambda *a, **k: _snap("2026-08-12", {}))
    monkeypatch.setattr(guard, "expected_last_trading_day",
                        lambda now: __import__("datetime").date(2026, 8, 12))
    monkeypatch.setattr(sys, "argv",
                        ["topup_guard.py", "--mode", "preflight", "--snapshot", str(snap)])
    guard.main()
    assert not snap.exists()


def test_audit_errors_without_snapshot(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["topup_guard.py", "--mode", "audit",
                         "--snapshot", str(tmp_path / "missing.json")])
    assert guard.main() == guard.EXIT_ERROR
    assert "找不到调用前快照" in capsys.readouterr().out
