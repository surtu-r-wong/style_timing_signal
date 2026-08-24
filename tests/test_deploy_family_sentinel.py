"""deploy/daily_signals/family_sentinel.py 的同族共动性哨兵单测（不连网、不连库）。

哨兵的命题：**同族序列应当共动**——跳飞（对内价差）与滞后（腿冻结）两侧都要抓，
且不得误伤真实行情。每条判据按「该报的报、不该报的不报」两侧覆盖；两起已知事故
（2026-06-16 level shift / 2026-08-03 腿冻结）以真实读数作回归用例钉死。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_PATH = ROOT / "deploy" / "daily_signals" / "family_sentinel.py"


def _load():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("family_sentinel", SENTINEL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = _load()


# ─────────────────────────────── returns_by_day ───────────────────────────────
def test_returns_by_day_hand_case():
    closes = {"A": {"2026-01-02": 100.0, "2026-01-05": 110.0, "2026-01-06": 99.0}}
    got = S.returns_by_day(closes)
    assert abs(got["2026-01-05"]["A"] - 0.10) < 1e-12        # 100 → 110
    assert abs(got["2026-01-06"]["A"] - (-0.10)) < 1e-12     # 110 → 99
    assert "2026-01-02" not in got                           # 首日无前值


def test_returns_by_day_skips_none_and_nonfinite():
    closes = {"A": {"2026-01-02": 100.0, "2026-01-05": None, "2026-01-06": 121.0},
              "B": {"2026-01-02": float("nan"), "2026-01-05": 50.0}}
    got = S.returns_by_day(closes)
    # A 跳过 None 那天，直接按相邻可用日 100 → 121
    assert abs(got["2026-01-06"]["A"] - 0.21) < 1e-12
    assert "2026-01-05" not in got or "A" not in got.get("2026-01-05", {})
    assert "B" not in got.get("2026-01-05", {})              # nan 前值不产生收益


def test_returns_by_day_skips_nonpositive_previous_close():
    got = S.returns_by_day({"A": {"2026-01-02": 0.0, "2026-01-05": 10.0}})
    assert got == {} or "A" not in got.get("2026-01-05", {})


# ─────────────────────────────── 规则 5：对内价差 ───────────────────────────────
def test_pair_spread_flags_the_2026_06_16_incident():
    """真实读数回归：2000对 −11.05% vs +16.02% = 27.07pp；1000对 +0.85% vs +10.67%。"""
    day = {"932409.CSI": -0.1105, "932408.CSI": 0.1602,
           "932407.CSI": 0.0085, "932406.CSI": 0.1067}
    found = S.pair_spread_findings(day)
    assert len(found) == 2
    assert all(f.startswith("CRITICAL") for f in found)
    assert any("2000pair" in f and "27.07pp" in f for f in found)
    assert any("1000pair" in f and "9.82pp" in f for f in found)


def test_pair_spread_warn_band_is_not_critical():
    day = {"932407.CSI": 0.04, "932406.CSI": -0.025}          # 价差 6.5pp
    found = S.pair_spread_findings(day)
    assert len(found) == 1 and found[0].startswith("WARN")


def test_pair_spread_silent_on_normal_rotation():
    """p99 量级（约 3pp）的正常风格轮动不得报警。"""
    day = {g: 0.02 for g, _ in S.PAIRS.values()}
    day.update({v: -0.01 for _, v in S.PAIRS.values()})       # 各对价差 3pp
    assert S.pair_spread_findings(day) == []


def test_pair_spread_thresholds_are_calibrated_values():
    """阈值改动必须伴随重新标定 —— 用例钉死已标定的两个数。"""
    assert S.PAIR_SPREAD_CRITICAL == 0.08
    assert S.PAIR_SPREAD_WARN == 0.06


def test_pair_spread_skips_pair_with_missing_leg():
    assert S.pair_spread_findings({"932409.CSI": -0.11}) == []


def test_pair_spread_is_direction_agnostic():
    a = S.pair_spread_findings({"932409.CSI": 0.16, "932408.CSI": -0.11})
    b = S.pair_spread_findings({"932409.CSI": -0.11, "932408.CSI": 0.16})
    assert len(a) == len(b) == 1 and a[0].split("（")[0] == b[0].split("（")[0]


# ─────────────────────────────── 规则 6：腿冻结 ───────────────────────────────
def test_frozen_legs_flags_the_2026_08_03_incident():
    """真实读数回归：5 条腿逐位相等，非冻结族中位 |r| = 0.85%。"""
    day = {"932406.CSI": 0.0, "932407.CSI": 0.0, "932408.CSI": 0.0,
           "932409.CSI": 0.0, "H30351.CSI": 0.0,
           "885000.WI": 0.0, "H00922.CSI": 0.0, "H11021.CSI": 0.0,
           "H30352.CSI": 0.00566, "000918.CSI": -0.01778, "000919.CSI": -0.00025,
           "000300.SH": -0.00981, "932000.CSI": 0.0085}
    found = S.frozen_leg_findings(day)
    assert len(found) == 1 and found[0].startswith("CRITICAL")
    assert "8 个代码" in found[0]  # 5 腿 + 885000.WI + H00922 + H11021


def test_frozen_legs_silent_when_market_is_calm():
    """真正的平静日：腿不动是正常的，不得报警。"""
    day = {"932406.CSI": 0.0, "932407.CSI": 0.0,
           "000918.CSI": 0.001, "000919.CSI": -0.0008, "000300.SH": 0.0012}
    assert S.frozen_leg_findings(day) == []


def test_frozen_legs_reference_excludes_frozen_codes():
    """参照必须用非冻结子集：大批冻结时若把 0 计入中位会被拖到 0 而漏报。"""
    day = {c: 0.0 for pair in S.PAIRS.values() for c in pair}   # 8 条腿全冻
    day.update({"000300.SH": 0.02, "932000.CSI": 0.018, "CI005917.WI": 0.021})
    found = S.frozen_leg_findings(day)
    assert len(found) == 1 and "8 个代码" in found[0]


def test_frozen_legs_needs_enough_reference_codes():
    """参照代码不足 → 不判（宁可不报也不瞎报）。"""
    day = {"932406.CSI": 0.0, "000300.SH": 0.02}
    assert S.frozen_leg_findings(day) == []


def test_frozen_legs_silent_when_nothing_frozen():
    day = {"932406.CSI": 0.001, "000300.SH": 0.02, "932000.CSI": 0.018,
           "CI005917.WI": 0.021}
    assert S.frozen_leg_findings(day) == []


def test_frozen_legs_threshold_is_calibrated_value():
    assert S.MARKET_MOVE_MIN == 0.005


# ─────────────────────────────── scan_findings ───────────────────────────────
def test_scan_findings_returns_only_dirty_days():
    closes = {
        "932409.CSI": {"2026-01-02": 100.0, "2026-01-05": 100.0, "2026-01-06": 89.0},
        "932408.CSI": {"2026-01-02": 100.0, "2026-01-05": 100.0, "2026-01-06": 116.0},
        "000300.SH": {"2026-01-02": 100.0, "2026-01-05": 101.0, "2026-01-06": 101.5},
    }
    got = S.scan_findings(closes)
    assert set(got) == {"2026-01-06"}                        # 只有跳飞那天脏
    assert got["2026-01-06"][0].startswith("CRITICAL")


def test_scan_findings_respects_only_days_filter():
    closes = {
        "932409.CSI": {"2026-01-02": 100.0, "2026-01-06": 89.0},
        "932408.CSI": {"2026-01-02": 100.0, "2026-01-06": 116.0},
        "000300.SH": {"2026-01-02": 100.0, "2026-01-06": 101.0},
    }
    assert S.scan_findings(closes, only_days={"2026-01-02"}) == {}
    assert set(S.scan_findings(closes, only_days={"2026-01-06"})) == {"2026-01-06"}


def test_scan_findings_clean_input_is_empty():
    closes = {c: {"2026-01-02": 100.0, "2026-01-05": 101.0}
              for pair in S.PAIRS.values() for c in pair}
    closes["000300.SH"] = {"2026-01-02": 100.0, "2026-01-05": 100.8}
    assert S.scan_findings(closes) == {}


# ─────────────────── 并入 topup_guard 审计（规则 5/6，只升级 CRITICAL）───────────────────
def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "topup_guard", ROOT / "deploy" / "daily_signals" / "topup_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_blocks_on_critical_family_finding():
    """新增日出现 2026-06-16 型跳飞 → audit_changes 必须报问题（链路中止）。"""
    guard = _load_guard()
    before = {"max_trade_date": "2026-01-05",
              "closes": {"932409.CSI": {"2026-01-05": 100.0},
                         "932408.CSI": {"2026-01-05": 100.0},
                         "000300.SH": {"2026-01-05": 100.0}}}
    after = {"max_trade_date": "2026-01-06",
             "closes": {"932409.CSI": {"2026-01-05": 100.0, "2026-01-06": 89.0},
                        "932408.CSI": {"2026-01-05": 100.0, "2026-01-06": 116.0},
                        "000300.SH": {"2026-01-05": 100.0, "2026-01-06": 101.0}}}
    problems = guard.audit_changes(before, after, "2026-01-06")
    assert any("CRITICAL" in p and "2000pair" in p for p in problems)


def test_audit_does_not_block_on_warn_only():
    """WARN 档不得阻断生产（未证实的可疑不拖停链路）。"""
    guard = _load_guard()
    before = {"max_trade_date": "2026-01-05",
              "closes": {"932409.CSI": {"2026-01-05": 100.0},
                         "932408.CSI": {"2026-01-05": 100.0},
                         "000300.SH": {"2026-01-05": 100.0}}}
    after = {"max_trade_date": "2026-01-06",
             "closes": {"932409.CSI": {"2026-01-05": 100.0, "2026-01-06": 103.5},
                        "932408.CSI": {"2026-01-05": 100.0, "2026-01-06": 97.0},
                        "000300.SH": {"2026-01-05": 100.0, "2026-01-06": 101.0}}}
    problems = guard.audit_changes(before, after, "2026-01-06")
    assert problems == []                       # 价差 6.5pp = WARN 档


def test_audit_only_judges_new_dates():
    """历史日的可疑不由本规则重复报（规则 4 管改写；这里只判新增日）。"""
    guard = _load_guard()
    closes = {"932409.CSI": {"2026-01-05": 100.0, "2026-01-06": 89.0},
              "932408.CSI": {"2026-01-05": 100.0, "2026-01-06": 116.0},
              "000300.SH": {"2026-01-05": 100.0, "2026-01-06": 101.0}}
    before = {"max_trade_date": "2026-01-06", "closes": closes}
    after = {"max_trade_date": "2026-01-06", "closes": closes}
    assert guard.audit_changes(before, after, "2026-01-06") == []
