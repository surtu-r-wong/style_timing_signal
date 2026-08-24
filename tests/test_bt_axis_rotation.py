"""新轮动轴批次一：构建纯函数 + 入场券判定 + runner fail-closed。"""
import json

import numpy as np
import pandas as pd
import pytest

from backtest.axis_entry_ticket import (
    FAIL_WORDING,
    PASS_WORDING,
    axis_signal,
    band_signals,
    judge_axis,
)
from backtest.axis_rotation_builder import (
    MIN_OBS,
    MOM_SKIP,
    MOM_WINDOW,
    axis_legs,
    drift_leg,
    price_factors,
    tercile_split,
)
from backtest.axis_ticket_runner import (
    REQUIRED_OUTPUTS,
    STEP_NAMES,
    _validate_complete_evidence,
    build_commands,
    run_ticket,
)


# ---------------------------------------------------------------- 因子纯函数
def test_price_factors_hand_case():
    # 273 行：前 29 行 0.01，后 244 行 0.02。
    # 低波窗 = 最后 244 行（全 0.02）→ std 精确 0；
    # 动量窗 = 跳过最后 21 行、取前 252 行 = 29×0.01 + 223×0.02。
    n = MOM_WINDOW + MOM_SKIP
    idx = pd.bdate_range("2020-01-01", periods=n)
    r = pd.DataFrame({"a": [0.01] * 29 + [0.02] * 244}, index=idx)
    out = price_factors(r)
    assert out.loc["a", "vol"] == 0.0
    assert np.isclose(out.loc["a", "mom"], 1.01**29 * 1.02**223 - 1.0)


def test_price_factors_min_obs_masks():
    n = MOM_WINDOW + MOM_SKIP
    idx = pd.bdate_range("2020-01-01", periods=n)
    col = pd.Series(np.nan, index=idx)
    col.iloc[-(MIN_OBS - 20):] = 0.01          # 有效日 < MIN_OBS
    out = price_factors(pd.DataFrame({"thin": col}))
    assert np.isnan(out.loc["thin", "vol"])
    assert np.isnan(out.loc["thin", "mom"])


def test_price_factors_empty():
    assert price_factors(pd.DataFrame()).empty


def test_tercile_split_hand_case():
    f = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0,
                   "e": 5.0, "f": 6.0, "g": 7.0, "h": np.nan})
    lo, hi = tercile_split(f)                   # 7 有效 → n//3 = 2
    assert lo == ["a", "b"]
    assert hi == ["f", "g"]


def test_tercile_split_too_small():
    assert tercile_split(pd.Series({"a": 1.0, "b": 2.0})) == ([], [])


def test_axis_legs_canonical_orientation():
    codes = [f"s{i}" for i in range(6)]
    fac = pd.DataFrame({
        "vol": [1, 2, 3, 4, 5, 6],
        "mom": [6, 5, 4, 3, 2, 1],
        "liq": [1, 2, 3, 4, 5, 6],
        "dp":  [6, 5, 4, 3, 2, 1],
    }, index=codes, dtype=float)
    lg, sh = axis_legs(fac, "lowvol")            # 低波腿(vol 最小) − 高波腿
    assert (lg, sh) == (["s0", "s1"], ["s4", "s5"])
    lg, sh = axis_legs(fac, "momentum")          # 高动量 − 低动量
    assert (lg, sh) == (["s1", "s0"], ["s5", "s4"])
    lg, sh = axis_legs(fac, "liquidity")         # 高换手 − 低换手
    assert (lg, sh) == (["s4", "s5"], ["s0", "s1"])
    lg, sh = axis_legs(fac, "dividend")          # 高股息 − 低股息
    assert (lg, sh) == (["s1", "s0"], ["s5", "s4"])


def test_drift_leg_hand_case():
    idx = pd.to_datetime(["2020-01-02", "2020-01-03"])
    wide = pd.DataFrame({"a": [0.1, 0.1], "b": [-0.1, 0.1]}, index=idx)
    leg = drift_leg(["a", "b"], wide)
    assert np.isclose(leg.iloc[0], 0.0)          # (0.1−0.1)/2
    # 漂移后权重 1.1/0.9 → (1.1×0.1 + 0.9×0.1)/2.0 = 0.1
    assert np.isclose(leg.iloc[1], 0.1)


def test_drift_leg_missing_code_zero_fill():
    idx = pd.to_datetime(["2020-01-02"])
    wide = pd.DataFrame({"a": [0.1]}, index=idx)
    leg = drift_leg(["a", "x"], wide)            # x 缺列 → 当日 0 收益
    assert np.isclose(leg.iloc[0], 0.05)


def test_drift_leg_empty():
    assert drift_leg([], pd.DataFrame()).empty


# ---------------------------------------------------------------- 信号装配
def test_band_signals_direction_and_shape():
    idx = pd.bdate_range("2020-01-01", periods=90)
    accel = np.linspace(0.0, 0.02, len(idx))     # 加速价差（恒定价差 z 分数恒 0）
    legs = pd.concat([
        pd.DataFrame({"date": idx, "axis": "lowvol", "band": b,
                      "long_ret": accel, "short_ret": 0.0})
        for b in ("300", "500")
    ], ignore_index=True)
    bands = band_signals(legs, "lowvol")
    assert list(bands.columns) == ["300", "500"]
    # 近 20 日相对动量高出其 40 日历史 → warmup 后信号为正
    assert bands["300"].iloc[-1] > 0
    assert bands["500"].iloc[-1] > 0


def test_axis_signal_partial_band_average():
    idx = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    bands = pd.DataFrame({"300": [0.2, 0.4, 0.6],
                          "500": [np.nan, 0.0, 0.2]}, index=idx)
    sig, n = axis_signal(bands)
    assert np.isclose(sig.iloc[0], 0.2)          # 仅 300 带可用
    assert np.isclose(sig.iloc[1], 0.2)          # (0.4+0.0)/2
    assert list(n) == [1, 2, 2]


# ---------------------------------------------------------------- 判定措辞
def test_judge_axis_frozen_wording():
    v = judge_axis(0.2, 0.01)
    assert v["pass"] is True and v["sign"] == "+"
    assert v["bonferroni_x4_ref"] is True        # 0.01 < 0.05/4
    assert v["wording"] == PASS_WORDING

    v = judge_axis(-0.2, 0.03)
    assert v["pass"] is True and v["sign"] == "-"
    assert v["bonferroni_x4_ref"] is False

    v = judge_axis(0.1, 0.2)
    assert v["pass"] is False
    assert v["wording"] == FAIL_WORDING

    v = judge_axis(float("nan"), float("nan"))
    assert v["pass"] is False and v["sign"] is None


# ---------------------------------------------------------------- runner
def _make_outputs(run_dir):
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_OUTPUTS:
        p = run_dir / "outputs" / name
        if name.endswith(".json"):
            payload = ({"OVERALL": "ALL_FAIL", "anchors_ok": True}
                       if name == "axis_ticket_verdict.json" else {"n_periods": 1})
            p.write_text(json.dumps(payload))
        else:
            p.write_text("date,axis\n2020-01-02,lowvol\n")
    for i, step in enumerate(STEP_NAMES, start=1):
        (run_dir / "logs" / f"step-{i}-{step}.log").write_text("ok")


def test_validate_complete_evidence(tmp_path):
    _make_outputs(tmp_path)
    _validate_complete_evidence(tmp_path)        # 不抛 = 通过

    (tmp_path / "outputs" / "axis_ticket_verdict.json").write_text(
        json.dumps({"OVERALL": "ALL_FAIL"}))     # 缺 anchors_ok
    with pytest.raises(RuntimeError, match="anchors_ok"):
        _validate_complete_evidence(tmp_path)


def test_build_commands_two_steps(tmp_path):
    cmds = build_commands(tmp_path, 2000, python="python")
    assert [s for s, _ in cmds] == list(STEP_NAMES)
    assert "backtest.axis_rotation_builder" in cmds[0][1]
    assert "--n-perm" in cmds[1][1] and "2000" in cmds[1][1]


def test_run_ticket_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.axis_ticket_runner.database_cutoffs", lambda: {})

    def failing_runner(command, log_path):
        log_path.write_text("boom")
        return 1

    with pytest.raises(RuntimeError, match="build failed"):
        run_ticket(tmp_path, "t1", runner=failing_runner)
    manifest = json.loads((tmp_path / "t1" / "manifest.json").read_text())
    assert manifest["status"] == "failed"

    def ok_runner(command, log_path):
        _make_outputs(tmp_path / "t2")
        log_path.write_text("ok")
        return 0

    run_dir = run_ticket(tmp_path, "t2", runner=ok_runner)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["seed"] == 0
    assert len(manifest["artifacts"]) >= len(REQUIRED_OUTPUTS) + len(STEP_NAMES)
