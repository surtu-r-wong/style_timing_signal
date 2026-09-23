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
