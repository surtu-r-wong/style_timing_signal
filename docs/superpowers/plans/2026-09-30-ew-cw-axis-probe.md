# 等权/市值加权相对强弱面探针 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按冻结前预登记 `docs/plans/2026-09-30-ew-cw-axis-prereg.md` 实现入场券探针机器 `backtest/ew_cw_axis_probe.py` 与单测，过变异验证与冒烟；正式 run 由控制器在冻结提交后执行。

**Architecture:** 纯函数层（读 Wind 导出、配对信号+可用性掩码、族合成、窗内前瞻和、全起点平均 IC、偏 IC、完整循环群置换、代表规则、分批持仓、α\* 与 Holm）+ 编排层（数据闸 → 主样本 D 三关与关 0 诊断 → 过关族先校准 α\* 再开封确认段 C → 裁决与报告）。复用 `_compute_pair_signal`、`run_strategy`、`selection_permutation.adjusted_pvalue`、`research_statistics.generated_null`，与旧机器 `rotation_probe.nonoverlap_ic / partial_rank_ic` 做一致性断言。

**Tech Stack:** Python 3（`/home/elfbob/miniconda3/bin/python3`）、pandas 2.3、numpy、scipy 1.17（`rankdata(axis=)`、`binomtest`）、pytest；PG 只在正式 run 的数据闸 3 与 carry 读取时访问。

---

## 纪律（执行者必读）

1. **不得读取任何真实收益或跑任何真实数据的统计量。** 不得打开 `/home/elfbob/exchange/20260930/*.xlsx`、不得运行 `--run-id`、不得调用 `run_formal`。单测与冒烟一律用合成数据。违反即污染预登记。
2. **不提交 git**（用户只授权一笔冻结 commit，由控制器在 QA 后执行）。不要 `git add` / `git commit`。
3. 只新建 `backtest/ew_cw_axis_probe.py` 与 `tests/test_ew_cw_axis_probe.py`；不改任何已有文件。
4. 长输出一律有界（`| tail -40` 只用于已知短输出；pytest 用 `-q`）。不要跑 `tesseract`。
5. 发现预登记口径无法照实现（不是实现 bug，而是规格本身矛盾）→ 停下报告，不要自行改口径。

---

### Task 1: 写单测（先红）

**Files:**
- Create: `tests/test_ew_cw_axis_probe.py`

**Step 1: 写入下列完整内容**

```python
"""等权/市值加权相对强弱面探针单测：手算字面量 + 与旧机器一致性 + 合成冒烟。

规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md。本文件只用合成数据。
"""
import numpy as np
import pandas as pd
import pytest

from backtest import ew_cw_axis_probe as m
from backtest.rotation_probe import nonoverlap_ic, partial_rank_ic
from signals.equal_weight.generate_signal import _compute_pair_signal


def _prices(n, seed=0, start="2005-01-03"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=idx)


def test_read_wind_export_layout(tmp_path):
    rows = [["开始日期", "2000-08-30", None], ["截止日期", "当前日期", None], ["收盘价", "close", None],
            ["日期", "300等权", "500等权"], ["Date", "000984.CSI", "000982.SH"],
            [pd.Timestamp("2004-12-30"), 999.0, 999.0],
            [pd.Timestamp("2004-12-31"), 1000.0, 1000.0],
            [pd.Timestamp("2005-01-04"), 1001.5, None]]
    path = tmp_path / "x.xlsx"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)
    df = m.read_wind_export(path, {"300": "000984.CSI", "500": "000982.SH"})
    assert list(df.columns) == ["300", "500"]
    assert list(df.index) == [pd.Timestamp("2004-12-31"), pd.Timestamp("2005-01-04")]  # DATA_START 截掉 12-30
    assert df.loc["2005-01-04", "300"] == 1001.5 and np.isnan(df.loc["2005-01-04", "500"])
    with pytest.raises(ValueError):
        m.read_wind_export(path, {"1000": "932382.CSI"})


def test_pair_signal_mask_first_available_position():
    ew, cw = _prices(40, 1), _prices(40, 2)
    lb, zw = 2, 3
    sig = m.pair_signal(ew, cw, lb, zw)
    ref = _compute_pair_signal(ew, cw, lookback=lb, z_window=zw)
    assert sig.iloc[:4].isna().all()          # lb + zw − 1 = 4
    assert sig.iloc[4:].notna().all()
    np.testing.assert_allclose(sig.iloc[4:].to_numpy(), ref.iloc[4:].to_numpy())


def test_family_f2_averages_available_pairs_only(monkeypatch):
    idx = pd.bdate_range("2014-01-01", periods=3)
    fake = {"300": pd.Series([0.2, 0.2, np.nan], idx), "500": pd.Series([np.nan, 0.4, np.nan], idx),
            "1000": pd.Series([np.nan, np.nan, np.nan], idx)}
    monkeypatch.setattr(m, "pair_signal", lambda e, c, lb, zw: fake[e.name])
    ew = pd.DataFrame({p: pd.Series(1.0, idx) for p in m.PAIRS})
    out = m.family_signal(ew, ew, "F2", 5, 60)
    assert out.iloc[0] == pytest.approx(0.2)
    assert out.iloc[1] == pytest.approx(0.3)
    assert np.isnan(out.iloc[2])


def test_forward_sums_window_internal():
    out = m.forward_sums(np.array([1.0, 2, 3, 4, 5, 6]), 2)
    np.testing.assert_array_equal(out[:4], [5.0, 7.0, 9.0, 11.0])
    assert np.isnan(out[4:]).all()


def test_block_points():
    np.testing.assert_array_equal(m.block_points(10, 3, 1), [1, 4])
    np.testing.assert_array_equal(m.block_points(10, 3, 0), [0, 3, 6])


def test_oa_ic_hand_computed():
    S = np.arange(1.0, 10.0)                                   # n = 9, k = 2
    fwd = np.array([10.0, 5, 20, 9, 30, 1, 40, np.nan, np.nan])
    # o=0 块点 {0,2,4,6}: S=[1,3,5,7] vs [10,20,30,40] → +1
    # o=1 块点 {1,3,5}:   S=[2,4,6]  vs [5,9,1] → 秩差 [−1,−1,2] → 1 − 6·6/(3·8) = −0.5
    ident = np.arange(9)[None, :]
    off = m.batch_offset_ics(S, fwd, 2, ident)
    np.testing.assert_allclose(off[0], [1.0, -0.5])
    assert m.batch_oa_ic(S, fwd, 2, ident)[0] == pytest.approx(0.25)


def test_rotation_index_roll_convention_and_identity():
    idx = m.rotation_index(7, 5, seed=1)
    assert idx.shape == (6, 7)
    np.testing.assert_array_equal(idx[0], np.arange(7))
    S = np.arange(7.0) * 10
    for row in idx[1:]:
        s = (-row[0]) % 7                                      # row[j] = (j − s) mod 7
        np.testing.assert_array_equal(S[row], np.roll(S, s))


def test_partial_and_legacy_match_rotation_probe():
    rng = np.random.default_rng(3)
    n, k = 200, 5
    idx = pd.bdate_range("2014-01-01", periods=n)
    S, r, c = (pd.Series(rng.normal(size=n), idx) for _ in range(3))
    fwd = m.forward_sums(r.to_numpy(), k)
    ident = np.arange(n)[None, :]
    off = m.batch_offset_ics(S.to_numpy(), fwd, k, ident)
    assert off[0, k - 1] == pytest.approx(nonoverlap_ic(S, r, k)[0], abs=1e-12)   # 旧口径 = 起点 k−1
    fwd_s = pd.Series(fwd, idx)
    expect = np.mean([partial_rank_ic(S.iloc[m.block_points(n, k, o)], fwd_s.iloc[m.block_points(n, k, o)],
                                      c.iloc[m.block_points(n, k, o)]) for o in range(k)])
    got = m.batch_partial_oa_ic(S.to_numpy(), fwd, c.to_numpy(), k, ident)[0]
    assert got == pytest.approx(expect, abs=1e-12)


def test_partial_is_zero_when_control_equals_signal():
    S = np.array([3.0, 1, 4, 1.5, 5, 9, 2, 6, 5.5, 3.5, 7, 8])
    fwd = m.forward_sums(np.array([0.1, -0.2, 0.3, 0.05, -0.1, 0.2, 0.0, 0.15, -0.05, 0.1, 0.2, -0.3]), 2)
    got = m.batch_partial_oa_ic(S, fwd, S, 2, np.arange(12)[None, :])[0]
    assert got == pytest.approx(0.0, abs=1e-12)


def test_perm_p_two_and_one_sided():
    pool = np.array([0.5, 0.6, -0.7, 0.1, -0.2])               # 行 0 = 观测
    assert m.perm_p_two_sided(pool) == pytest.approx(3 / 5)
    assert m.perm_p_one_sided(pool, +1.0) == pytest.approx(2 / 5)
    assert m.perm_p_one_sided(-pool, -1.0) == pytest.approx(2 / 5)


def test_tranche_position_hand_computed():
    idx = pd.bdate_range("2014-01-01", periods=6)
    sig = pd.Series([0.5, 0.2, -0.1, 0.3, 0.0, -0.4], idx)    # 符号 [+,+,−,+,0,−]
    np.testing.assert_allclose(m.tranche_position(sig, 3, 1.0).to_numpy(), [0, 0, 1 / 3, 1 / 3, 0, 0])
    np.testing.assert_allclose(m.tranche_position(sig, 3, -1.0).to_numpy(), [0, 0, -1 / 3, -1 / 3, 0, 0])
    np.testing.assert_allclose(m.tranche_position(sig, 3, 1.0, long_only=True).to_numpy(),
                               [0, 0, 2 / 3, 2 / 3, 1 / 3, 1 / 3])
    sig2 = sig.copy()
    sig2.iloc[1] = np.nan                                      # 窗内缺信号 → 空仓
    np.testing.assert_allclose(m.tranche_position(sig2, 3, 1.0).to_numpy()[:4], [0, 0, 0, 0])


def test_pick_representative_rules():
    panel = pd.DataFrame([
        {"lb": 5, "zw": 60, "k": 5, "oa_ic": 0.30, "oa_ic_H1": 0.40, "oa_ic_H2": -0.05},     # 不同号 → 排除
        {"lb": 5, "zw": 250, "k": 10, "oa_ic": 0.10, "oa_ic_H1": 0.08, "oa_ic_H2": 0.12},    # worst 0.08
        {"lb": 20, "zw": 60, "k": 20, "oa_ic": -0.15, "oa_ic_H1": -0.09, "oa_ic_H2": -0.20},  # worst 0.09 ← 选中
        {"lb": 20, "zw": 250, "k": 5, "oa_ic": -0.12, "oa_ic_H1": -0.09, "oa_ic_H2": -0.10},  # worst 0.09，|全窗| 小
    ])
    best, ok = m.pick_representative(panel)
    assert ok and (best["lb"], best["zw"], best["k"]) == (20, 60, 20)
    best2, ok2 = m.pick_representative(panel.iloc[[0]])
    assert not ok2 and best2["oa_ic"] == 0.30
    tie = pd.DataFrame([
        {"lb": 20, "zw": 60, "k": 20, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
        {"lb": 20, "zw": 60, "k": 10, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
        {"lb": 5, "zw": 60, "k": 10, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
    ])
    best3, _ = m.pick_representative(tie)
    assert (best3["lb"], best3["k"]) == (5, 10)


def test_confirmation_window_excludes_2014_returns():
    idx = pd.bdate_range("2013-12-20", "2014-01-10")
    blend = pd.Series(np.where(idx.year == 2014, 100.0, 0.001), idx)   # 2014 收益放大，混入即可见
    cdates = m.window(blend, "2013-12-20", m.C_END).index
    fwd = m.forward_sums(blend.reindex(cdates).to_numpy(), 3)
    assert np.nanmax(fwd) < 1.0
    assert np.isnan(fwd[-3:]).all()


def test_choose_alpha_star():
    rates = {"stationary": {0.05: 0.050, 0.04: 0.040}, "shared_volatility": {0.05: 0.0625, 0.04: 0.050},
             "regime": {0.05: 0.055, 0.04: 0.045}}
    rows = [{"kind": kind, "alpha": a, "rate": r.get(a, a)} for kind, r in rates.items() for a in m.ALPHA_GRID]
    tab = pd.DataFrame(rows)
    assert m.choose_alpha_star(tab) == 0.04
    assert m.choose_alpha_star(tab.assign(rate=0.2)) is None


def test_holm_pass():
    assert m.holm_pass({"F1": 0.012, "F2": 0.03}, 0.04) == {"F1": True, "F2": True}
    assert m.holm_pass({"F1": 0.025, "F2": 0.03}, 0.04) == {"F1": False, "F2": False}
    assert m.holm_pass({"F1": 0.012, "F2": 0.05}, 0.04) == {"F1": True, "F2": False}
    assert m.holm_pass({"F2": 0.04}, 0.04) == {"F2": True}


def test_data_checks_flag_stale_and_calendar():
    idx = pd.bdate_range("2014-01-01", periods=6)
    base = pd.Series([1.0, 1.1, 1.2, 1.3, 1.4, 1.5], idx)
    ew = pd.DataFrame({"300": base, "500": base * 2, "1000": base * 3})
    ok = m.data_checks(ew, ew.copy())
    assert ok["gate1_calendar_ok"] and ok["gate2_stale_ok"]
    ew2 = ew.copy()
    ew2.iloc[3, 0] = ew2.iloc[2, 0]                            # 300 等权一日未更新
    cw2 = ew.copy()
    cw2.iloc[4, 1] = np.nan                                    # 500 市值加权缺一日
    bad = m.data_checks(ew2, cw2)
    assert bad["stale_days"]["ew300"] == 1 and not bad["gate2_stale_ok"]
    assert bad["calendar_mismatch_days"]["500"] == 1 and not bad["gate1_calendar_ok"]


def test_smoke_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "EW_XLSX", tmp_path / "must_not_read.xlsx")
    monkeypatch.setattr(m, "CW_XLSX", tmp_path / "must_not_read.xlsx")
    res = m.run_smoke(tmp_path / "smoke", n_perm=9, n_cal=2)
    assert set(res["verdicts"]["family"]) == {"F1", "F2"}
    for name in ("panel_main.csv", "verdicts.csv", "confirmation.csv", "calibration.csv",
                 "calibration_raw.csv", "report_per_pair.csv", "report_incumbent_reference.csv", "REPORT.md"):
        assert (tmp_path / "smoke" / name).exists(), name
    assert len(pd.read_csv(tmp_path / "smoke" / "panel_main.csv")) == 32
```

**Step 2: 跑，确认因模块不存在而失败**

Run: `cd /home/elfbob/claude-code/style_timing_signal && python3 -m pytest tests/test_ew_cw_axis_probe.py -q 2>&1 | tail -5`
Expected: collection error `ModuleNotFoundError: No module named 'backtest.ew_cw_axis_probe'`

---

### Task 2: 实现模块（转绿）

**Files:**
- Create: `backtest/ew_cw_axis_probe.py`

**Step 1: 写入下列完整内容**

```python
"""等权/市值加权相对强弱面·入场券探针（2026-09-30）。

规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md（冻结后口径不改）。
两族（F1 300 集中度 / F2 三对合成）× 现役同款相对强弱（`_compute_pair_signal`，
左=等权、右=市值加权）× (lb,zw) × k = 32 变体。主样本 D 三关 → 三关全过的族才开封
确认段 C（2013-12-31 及以前），α* 先由零假设模拟定。统计量 = 全起点平均 IC（OA-IC），
置换 = 完整循环群（下标矩阵行 0 为恒等）。

CLI:
  python3 -m backtest.ew_cw_axis_probe --run-id 20260930-ew-cw-axis-r1   # 正式：只跑一次
  python3 -m backtest.ew_cw_axis_probe --smoke --out DIR                  # 开发：只喂合成数据
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from scipy.stats import binomtest, rankdata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover  # noqa: E402
from backtest.selection_permutation import adjusted_pvalue  # noqa: E402
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402

# ---------------------------------------------------------------- 冻结常量（预登记 §2/§3）
EW_XLSX = Path("/home/elfbob/exchange/20260930/等权指数.xlsx")
CW_XLSX = Path("/home/elfbob/exchange/20260930/指数.xlsx")
EW_CODES = {"300": "000984.CSI", "500": "000982.SH", "1000": "932382.CSI"}
CW_CODES = {"300": "000300.SH", "500": "000905.SH", "1000": "000852.SH"}
PAIRS = ("300", "500", "1000")
DATA_START = pd.Timestamp("2004-12-31")
END = pd.Timestamp("2026-09-29")
D_WIN = ("2014-01-02", "2026-09-29")
HALVES = {"H1": ("2014-01-02", "2020-05-13"), "H2": ("2020-05-14", "2026-09-29")}
C_END = "2013-12-31"
FORMS = ((5, 60), (5, 250), (20, 60), (20, 250))
GRID_K = (5, 10, 20, 40)
FAMILIES = ("F1", "F2")
COST_BPS = 3.0
GATE_P = 0.05
N_PERM = 999
N_CAL = 400
SEED_D = 20260930
SEED_CAL = SEED_D + 1_000_000
SEED_C = SEED_D + 2_000_000
ALPHA_GRID = (0.05, 0.04, 0.03, 0.025, 0.02, 0.015, 0.01, 0.005)
CAL_KINDS = ("stationary", "shared_volatility", "regime")
CAL_SCALE = 0.005
EW_SIGNAL_CSV = ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv"
SLOPE20_CSV = ROOT / "output/slope20/slope20_signal_L20zw120.csv"
PREREG = ROOT / "docs/plans/2026-09-30-ew-cw-axis-prereg.md"
RUN_ROOT = ROOT / "backtest/output/runs"
CODE_FILES = ("backtest/ew_cw_axis_probe.py", "backtest/rotation_probe.py",
              "backtest/selection_permutation.py", "backtest/research_statistics.py",
              "backtest/engine.py", "backtest/metrics.py", "backtest/data.py",
              "backtest/run_manifest.py", "signals/equal_weight/generate_signal.py")


# ---------------------------------------------------------------- 数据
def read_wind_export(path: Path, codes: dict[str, str]) -> pd.DataFrame:
    """Wind 导出版式（行 0~3 元数据/中文名，行 4 代码行，行 5 起数据）→ 列 = 配对键，截 [DATA_START, END]。"""
    raw = pd.read_excel(path, header=None)
    header = [str(c).strip() for c in raw.iloc[4].tolist()]
    body = raw.iloc[5:].copy()
    body.columns = header
    out = {}
    for key, code in codes.items():
        if code not in header:
            raise ValueError(f"{Path(path).name} 缺代码 {code}（实有 {header[1:]}）")
        out[key] = pd.to_numeric(body[code], errors="coerce").to_numpy()
    idx = pd.DatetimeIndex(pd.to_datetime(body[header[0]]), name="date")
    df = pd.DataFrame(out, index=idx).sort_index()
    return df.loc[(df.index >= DATA_START) & (df.index <= END)]


def data_checks(ew: pd.DataFrame, cw: pd.DataFrame) -> dict:
    """§5 闸 1（交易日历）/ 闸 2（陈旧值）+ 只报数（|r_EW − r_CW| > 3% 日数）。"""
    out = {"calendar_mismatch_days": {}, "stale_days": {}, "spread_gt3pct_days": {}}
    for p in PAIRS:
        e, c = ew[p].dropna(), cw[p].dropna()
        lo = max(e.index.min(), c.index.min())
        e, c = e[e.index >= lo], c[c.index >= lo]
        out["calendar_mismatch_days"][p] = int(len(e.index.symmetric_difference(c.index)))
        out["stale_days"][f"ew{p}"] = int((e.diff() == 0).sum())
        out["stale_days"][f"cw{p}"] = int((c.diff() == 0).sum())
        j = pd.concat([e.pct_change(), c.pct_change()], axis=1, join="inner").dropna()
        out["spread_gt3pct_days"][p] = int(((j.iloc[:, 0] - j.iloc[:, 1]).abs() > 0.03).sum())
    out["gate1_calendar_ok"] = all(v == 0 for v in out["calendar_mismatch_days"].values())
    out["gate2_stale_ok"] = all(v == 0 for v in out["stale_days"].values())
    return out


def blend_from_export(cw: pd.DataFrame) -> pd.Series:
    """标的 = (r_000905 + r_000852) / 2（与 backtest.data.blend_returns 同式）。"""
    r = cw[["500", "1000"]].pct_change()
    return ((r["500"] + r["1000"]) / 2.0).rename("blend")


def reconcile_with_db(cw: pd.DataFrame, blend: pd.Series, db=None) -> dict:
    """§5 闸 3：市值加权导出 vs index_daily；blend vs load_underlying_returns('blend')（D 段）。"""
    from backtest.data import load_spot_close, load_underlying_returns
    out = {"cw_overlap_days": {}, "cw_max_rel_diff": {}}
    for p in PAIRS:
        ref = load_spot_close(p, db=db)
        j = pd.concat([cw[p].rename("x"), ref.rename("db")], axis=1, join="inner").dropna()
        j = j[j.index <= END]
        out["cw_overlap_days"][p] = int(len(j))
        out["cw_max_rel_diff"][p] = float((j["x"] / j["db"] - 1.0).abs().max())
    d_dates = window(blend, *D_WIN).index
    ref_b = load_underlying_returns("blend", db=db).reindex(d_dates)
    out["blend_D_missing_in_db"] = int(ref_b.isna().sum())
    out["blend_D_max_abs_diff"] = float((blend.reindex(d_dates) - ref_b).abs().max())
    out["gate3_reconcile_ok"] = bool(
        all(v > 0 for v in out["cw_overlap_days"].values())
        and all(v <= 1e-6 for v in out["cw_max_rel_diff"].values())
        and out["blend_D_missing_in_db"] == 0
        and out["blend_D_max_abs_diff"] <= 1e-12)
    return out


# ---------------------------------------------------------------- 信号
def pair_signal(ew: pd.Series, cw: pd.Series, lb: int, zw: int) -> pd.Series:
    """现役 `_compute_pair_signal`（左=等权、右=市值加权；正=等权跑赢）+ 可用性掩码（§2.2）。

    位置 i（自该配对首个对齐日起）< lb+zw−1 记 NaN：z 窗须全由满 lb 窗、真实收益构成的
    复合值组成（位置 0 的收益是 pct_change 的 fillna(0)，不是真实收益）。
    """
    joint = pd.concat([ew.rename("ew"), cw.rename("cw")], axis=1).dropna()
    sig = _compute_pair_signal(joint["ew"], joint["cw"], lookback=lb, z_window=zw).astype(float)
    sig.iloc[: lb + zw - 1] = np.nan
    return sig


def family_signal(ew: pd.DataFrame, cw: pd.DataFrame, family: str, lb: int, zw: int) -> pd.Series:
    """F1 = 300 一对；F2 = 当日可用配对（300/500/1000）的算术平均，全不可用记 NaN（不拿 0 摊薄）。"""
    if family == "F1":
        return pair_signal(ew["300"], cw["300"], lb, zw)
    if family == "F2":
        sigs = pd.concat({p: pair_signal(ew[p], cw[p], lb, zw) for p in PAIRS}, axis=1)
        return sigs.mean(axis=1, skipna=True)
    raise ValueError(f"未知族 {family}")


# ---------------------------------------------------------------- 窗口与统计量
def window(s, a, b):
    return s[(s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))]


def forward_sums(r: np.ndarray, k: int) -> np.ndarray:
    """fwd[i] = r[i+1] + … + r[i+k]，只用窗内收益；i+k 越窗记 NaN。"""
    r = np.asarray(r, dtype=float)
    n = len(r)
    out = np.full(n, np.nan)
    if n > k:
        c = np.concatenate([[0.0], np.cumsum(r)])
        i = np.arange(n - k)
        out[: n - k] = c[i + k + 1] - c[i + 1]
    return out


def block_points(n: int, k: int, o: int) -> np.ndarray:
    """起点 o 的非重叠块点 i = o, o+k, …，要求 i+k ≤ n−1（前瞻窗不越窗）。"""
    return np.arange(o, n - k, k)


def rotation_index(n: int, n_perm: int, seed: int) -> np.ndarray:
    """(n_perm+1, n) 下标矩阵：行 0 恒等；其余行 s ~ U{0,…,n−1}（完整循环群，含 0）。

    S_rot[j] = S[(j − s) mod n]，即 np.roll(S, s)；与 research_statistics.calibration 同约定。
    """
    rng = np.random.default_rng(seed)
    shifts = rng.integers(0, n, size=n_perm)
    base = np.arange(n)
    rows = (base[None, :] - shifts[:, None]) % n
    return np.vstack([base[None, :], rows]).astype(np.int64)


def batch_offset_ics(S, fwd, k: int, index_matrix: np.ndarray) -> np.ndarray:
    """(m, k)：每行一组重排、每列一个起点的非重叠 Spearman IC（平均秩）。"""
    S = np.asarray(S, dtype=float)
    fwd = np.asarray(fwd, dtype=float)
    n, m = len(S), index_matrix.shape[0]
    out = np.zeros((m, k))
    for o in range(k):
        pts = block_points(n, k, o)
        sr = rankdata(S[index_matrix[:, pts]], axis=1)
        yr = rankdata(fwd[pts])
        sr = sr - sr.mean(axis=1, keepdims=True)
        yr = yr - yr.mean()
        den = np.sqrt((sr * sr).sum(axis=1) * (yr * yr).sum())
        out[:, o] = np.divide(sr @ yr, den, out=np.zeros(m), where=den > 0)
    return out


def batch_oa_ic(S, fwd, k: int, index_matrix: np.ndarray) -> np.ndarray:
    """(m,) 全起点平均 IC（OA-IC）。"""
    return batch_offset_ics(S, fwd, k, index_matrix).mean(axis=1)


def _resid(x: np.ndarray, c: np.ndarray) -> np.ndarray:
    """x（(m,p) 或 (p,)）对 c（(p,)）一元 OLS 残差；与 rotation_probe.partial_rank_ic 同式。"""
    cm = c - c.mean()
    xm = x - x.mean(axis=-1, keepdims=True)
    beta = (xm @ cm) / (cm @ cm)
    return xm - np.multiply.outer(beta, cm)


def batch_partial_oa_ic(S, fwd, ctrl, k: int, index_matrix: np.ndarray) -> np.ndarray:
    """(m,) 偏 OA-IC：各起点上秩化后 sig、fwd 分别对 ctrl 残差化再相关，对起点平均；只重排 sig。"""
    S = np.asarray(S, dtype=float)
    fwd = np.asarray(fwd, dtype=float)
    ctrl = np.asarray(ctrl, dtype=float)
    n, m = len(S), index_matrix.shape[0]
    total = np.zeros(m)
    for o in range(k):
        pts = block_points(n, k, o)
        cr = rankdata(ctrl[pts])
        rs = _resid(rankdata(S[index_matrix[:, pts]], axis=1), cr)
        rf = _resid(rankdata(fwd[pts]), cr)
        den = np.sqrt((rs * rs).sum(axis=1) * (rf * rf).sum())
        total += np.divide(rs @ rf, den, out=np.zeros(m), where=den >= 1e-9)
    return total / k


def perm_p_two_sided(pool) -> float:
    """pool[0] = 观测、其余 = 置换；p = #{|pool| ≥ |obs|} / len(pool)（含观测自身 = (1+#)/(B+1)）。

    这是逐变体的未校正 p（历次 run_families_probe 关 1 的同一量），不是选优校正 p；
    选优由确认段承担，关 0 另报 min-P。
    """
    pool = np.asarray(pool, dtype=float)
    return float(np.count_nonzero(np.abs(pool) >= np.abs(pool[0])) / len(pool))


def perm_p_one_sided(pool, direction: float) -> float:
    pool = np.asarray(pool, dtype=float) * float(direction)
    return float(np.count_nonzero(pool >= pool[0]) / len(pool))


def pick_representative(panel: pd.DataFrame) -> tuple[pd.Series, bool]:
    """§3.4：全窗/H1/H2 三个 OA-IC 同号且非 0 → worst-half |OA-IC| 最大；
    平局依次 |全窗| 大、k 小、lb 小、zw 小。无候选 → (|全窗| 最大行, False)。"""
    p = panel.assign(_abs=panel["oa_ic"].abs(),
                     _worst=panel[["oa_ic_H1", "oa_ic_H2"]].abs().min(axis=1))
    sgn = np.sign(p["oa_ic"])
    ok = (sgn != 0) & (np.sign(p["oa_ic_H1"]) == sgn) & (np.sign(p["oa_ic_H2"]) == sgn)
    if ok.any():
        best = p[ok].sort_values(["_worst", "_abs", "k", "lb", "zw"],
                                 ascending=[False, False, True, True, True]).iloc[0]
        return best.drop(["_abs", "_worst"]), True
    best = p.sort_values(["_abs", "k", "lb", "zw"], ascending=[False, True, True, True]).iloc[0]
    return best.drop(["_abs", "_worst"]), False


# ---------------------------------------------------------------- 持仓与账本
def tranche_position(sig: pd.Series, k: int, direction: float, long_only: bool = False) -> pd.Series:
    """§3.3 分批持仓：pos(t) = mean_{j<k}[方向 × sign(sig(t−j))]（long_only：各子账户先截到 ≥0）；
    窗内不足 k 个有效信号记 0（空仓）。t 日决策、引擎内 shift(1) 于 t+1 生效。"""
    s = np.sign(sig) * float(direction)
    if long_only:
        s = s.clip(lower=0.0)
    return s.rolling(k, min_periods=k).mean().fillna(0.0)


def net_stats(pos: pd.Series, r: pd.Series, carry) -> dict:
    """引擎原样（T+1 生效、3 bp、可选 carry）；统计区间 = pos 的索引。"""
    ret = run_strategy(pos, r.reindex(pos.index), COST_BPS, carry)["ret"]
    return {"net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
            "max_dd": max_drawdown(ret), "turnover": turnover(pos)}


def yearly_net(pos: pd.Series, r: pd.Series, carry) -> dict:
    ret = run_strategy(pos, r.reindex(pos.index), COST_BPS, carry)["ret"]
    return {str(y): float((1.0 + g).prod() - 1.0) for y, g in ret.groupby(ret.index.year)}


# ---------------------------------------------------------------- 确认段：α* 与 Holm
def calibrate_confirmation(lb: int, zw: int, k: int, n_c: int, *, n_cal: int = N_CAL,
                           n_perm: int = N_PERM, seed_base: int = SEED_CAL) -> pd.DataFrame:
    """§3.6：零假设模拟下确认段单侧 p 的分布（只用模拟数据，不碰真实 C 段）。"""
    from backtest.research_statistics import generated_null
    warm = lb + zw - 1
    n_tot = warm + n_c
    rows = []
    for ki, kind in enumerate(CAL_KINDS):
        for rep in range(n_cal):
            seed = seed_base + ki * 10_000 + rep
            x, y = generated_null(kind, n_tot, seed)
            nav = pd.Series(np.cumprod(1.0 + CAL_SCALE * np.asarray(x, dtype=float)))
            sig = pair_signal(nav, pd.Series(np.ones(n_tot)), lb, zw).to_numpy()[warm:]
            fwd = forward_sums(np.asarray(y, dtype=float)[warm:], k)
            pool = batch_oa_ic(sig, fwd, k, rotation_index(n_c, n_perm, seed + 100_000))
            rows.append({"kind": kind, "rep": rep, "p_one_sided": perm_p_one_sided(pool, 1.0)})
    return pd.DataFrame(rows)


def rejection_table(cal: pd.DataFrame) -> pd.DataFrame:
    """各场景 × 各 α 档拒绝率及 95% 二项区间。"""
    rows = []
    for kind, g in cal.groupby("kind", sort=False):
        for a in ALPHA_GRID:
            hits = int((g["p_one_sided"] <= a).sum())
            ci = binomtest(hits, len(g)).proportion_ci()
            rows.append({"kind": kind, "alpha": a, "n": int(len(g)), "hits": hits,
                         "rate": hits / len(g), "ci_lo": float(ci.low), "ci_hi": float(ci.high)})
    return pd.DataFrame(rows)


def choose_alpha_star(table: pd.DataFrame) -> float | None:
    """α* = ALPHA_GRID 中「所有场景拒绝率 ≤ 5%」的最大值；都不满足 → None（不可校准）。"""
    worst = table.groupby("alpha")["rate"].max()
    good = [a for a in ALPHA_GRID if a in worst.index and worst[a] <= 0.05]
    return max(good) if good else None


def holm_pass(pvals: dict, alpha: float) -> dict:
    """两族 Holm：较小 p ≤ α/2 才拒，拒后较大 p ≤ α 再拒；单族即 p ≤ α。"""
    if len(pvals) == 1:
        (fam, p), = pvals.items()
        return {fam: bool(p <= alpha)}
    (f1, p1), (f2, p2) = sorted(pvals.items(), key=lambda kv: kv[1])
    first = bool(p1 <= alpha / 2)
    return {f1: first, f2: bool(first and p2 <= alpha)}


# ---------------------------------------------------------------- 主样本与确认段
def evaluate_main(ew, cw, blend, carry, ctrl_ew, ctrl_sl, *, n_perm: int = N_PERM,
                  seed: int = SEED_D) -> dict:
    """§3.3~3.5：32 变体面板、逐族代表与三关、关 0 诊断、只报数项。"""
    from backtest.rotation_probe import hold_position, nonoverlap_ic
    dates = window(blend, *D_WIN).index
    n = len(dates)
    r = blend.reindex(dates).to_numpy(dtype=float)
    if np.isnan(r).any():
        raise ValueError("D 段标的收益有缺失")
    idx = rotation_index(n, n_perm, seed)
    fwd = {k: forward_sums(r, k) for k in GRID_K}
    halves = {}
    for h, (a, b) in HALVES.items():
        hd = window(blend, a, b).index
        halves[h] = (hd, {k: forward_sums(blend.reindex(hd).to_numpy(dtype=float), k) for k in GRID_K})

    sigs, pools, rows = {}, {}, []
    for fam in FAMILIES:
        for lb, zw in FORMS:
            sig = family_signal(ew, cw, fam, lb, zw)
            S = sig.reindex(dates).to_numpy(dtype=float)
            if np.isnan(S).any():
                raise ValueError(f"{fam} ({lb},{zw}) 在 D 段有缺失")
            sigs[(fam, lb, zw)] = sig
            for k in GRID_K:
                off = batch_offset_ics(S, fwd[k], k, idx)
                pool = off.mean(axis=1)
                legacy = nonoverlap_ic(sig.reindex(dates), blend.reindex(dates), k)[0]
                if not abs(legacy - off[0, k - 1]) <= 1e-10:
                    raise AssertionError(f"旧口径复核失败 {fam} ({lb},{zw}) k={k}: {legacy} vs {off[0, k - 1]}")
                row = {"family": fam, "lb": lb, "zw": zw, "k": k, "oa_ic": float(pool[0]),
                       "p_two_sided_naive": perm_p_two_sided(pool),
                       "ic_offset_min": float(off[0].min()), "ic_offset_median": float(np.median(off[0])),
                       "ic_offset_max": float(off[0].max()), "legacy_ic": float(off[0, k - 1]),
                       "n_blocks_offset0": int(len(block_points(n, k, 0)))}
                for h, (hd, hf) in halves.items():
                    Sh = sig.reindex(hd).to_numpy(dtype=float)
                    row[f"oa_ic_{h}"] = float(batch_oa_ic(Sh, hf[k], k, np.arange(len(hd))[None, :])[0])
                rows.append(row)
                pools[(fam, lb, zw, k)] = pool
    panel = pd.DataFrame(rows)

    keys = list(pools)
    abs_pool = np.abs(np.column_stack([pools[kk] for kk in keys]))
    gate0 = SimpleNamespace(observed=abs_pool[0], null_stats=abs_pool[1:])   # adjusted_pvalue 只读这两个字段

    c_ew = ctrl_ew.reindex(dates).to_numpy(dtype=float)
    c_sl = ctrl_sl.reindex(dates).to_numpy(dtype=float)
    if np.isnan(c_ew).any() or np.isnan(c_sl).any():
        raise ValueError("控制信号在 D 段有缺失")

    verdicts, yearly = [], {}
    for fam in FAMILIES:
        best, sign_ok = pick_representative(panel[panel["family"] == fam])
        lb, zw, k = int(best["lb"]), int(best["zw"]), int(best["k"])
        direction = float(np.sign(best["oa_ic"])) or 1.0
        sig = sigs[(fam, lb, zw)]
        S = sig.reindex(dates).to_numpy(dtype=float)
        ppool = batch_partial_oa_ic(S, fwd[k], c_ew, k, idx)
        p_partial = perm_p_two_sided(ppool)
        gate1 = bool(best["p_two_sided_naive"] < GATE_P and ppool[0] * direction > 0 and p_partial < GATE_P)
        pos = tranche_position(sig, k, direction).reindex(dates)
        sym = net_stats(pos, blend, carry)
        gate3 = bool(sym["net_sharpe"] > 0)
        spool = batch_partial_oa_ic(S, fwd[k], c_sl, k, idx)
        lf = tranche_position(sig, k, direction, long_only=True).reindex(dates)
        j = keys.index((fam, lb, zw, k))
        verdicts.append({
            "family": fam, "lb": lb, "zw": zw, "k": k, "direction": direction,
            "oa_ic": float(best["oa_ic"]), "oa_ic_H1": float(best["oa_ic_H1"]),
            "oa_ic_H2": float(best["oa_ic_H2"]), "p_two_sided_naive": float(best["p_two_sided_naive"]),
            "partial_oa_ic_vs_ew": float(ppool[0]), "p_partial_vs_ew": p_partial,
            "sym_net_sharpe": sym["net_sharpe"], "sym_net_ann": sym["net_ann"],
            "sym_max_dd": sym["max_dd"], "sym_turnover": sym["turnover"],
            "gate1": gate1, "gate2": bool(sign_ok), "gate3": gate3,
            "main_pass": bool(gate1 and sign_ok and gate3),
            "gate0_min_p_diag": adjusted_pvalue(gate0, j, "min_p"),
            "gate0_max_t_diag": adjusted_pvalue(gate0, j, "max_t"),
            "report_partial_oa_ic_vs_slope20": float(spool[0]),
            "report_p_partial_vs_slope20": perm_p_two_sided(spool),
            "report_longflat_net_sharpe": net_stats(lf, blend, None)["net_sharpe"],
            "report_lag1_net_sharpe": net_stats(pos.shift(1).fillna(0.0), blend, carry)["net_sharpe"],
            "report_legacy_hold_net_sharpe": net_stats(
                hold_position(sig.reindex(dates) * direction, k), blend, carry)["net_sharpe"],
            "report_corr_with_ew": float(np.corrcoef(S, c_ew)[0, 1]),
            "report_corr_with_slope20": float(np.corrcoef(S, c_sl)[0, 1]),
        })
        yearly[fam] = yearly_net(pos, blend, carry)

    per_pair = []
    for p in PAIRS:
        for lb, zw in FORMS:
            S = pair_signal(ew[p], cw[p], lb, zw).reindex(dates).to_numpy(dtype=float)
            ok = ~np.isnan(S)
            if not ok.any():
                continue
            first = int(np.argmax(ok))
            if not ok[first:].all():
                raise ValueError(f"配对 {p} ({lb},{zw}) 在 D 段可用后又出现缺失")
            for k in GRID_K:
                v = batch_oa_ic(S[first:], forward_sums(r[first:], k), k,
                                np.arange(n - first)[None, :])[0]
                per_pair.append({"pair": p, "lb": lb, "zw": zw, "k": k, "report_oa_ic": float(v),
                                 "start": str(dates[first].date()), "n_days": int(n - first)})
    incumbents = []
    for name, c in (("equal_weight_20d40z", c_ew), ("slope20_L20zw120", c_sl)):
        for k in GRID_K:
            v = batch_oa_ic(c, fwd[k], k, np.arange(n)[None, :])[0]
            incumbents.append({"signal": name, "k": k, "report_oa_ic": float(v)})
    return {"panel": panel, "verdicts": pd.DataFrame(verdicts), "per_pair": pd.DataFrame(per_pair),
            "incumbents": pd.DataFrame(incumbents), "yearly": yearly}


def confirm_families(passing: list, ew, cw, blend, *, n_cal: int = N_CAL, n_perm: int = N_PERM,
                     seed_cal: int = SEED_CAL, seed_c: int = SEED_C):
    """§3.6：先按各族代表规格做零假设校准定 α*（只用模拟），全部定完才读真实 C 段一次。"""
    prepared, tables, raws = [], [], []
    for v in passing:
        fam, lb, zw, k = v["family"], int(v["lb"]), int(v["zw"]), int(v["k"])
        sig = family_signal(ew, cw, fam, lb, zw)
        cdates = window(blend, sig.dropna().index.min(), C_END).index
        raw = calibrate_confirmation(lb, zw, k, len(cdates), n_cal=n_cal, n_perm=n_perm, seed_base=seed_cal)
        tab = rejection_table(raw)
        raws.append(raw.assign(family=fam))
        tables.append(tab.assign(family=fam))
        prepared.append((v, sig, cdates, choose_alpha_star(tab)))
    own = [a for *_, a in prepared if a is not None]
    alpha_used = min(own) if own else None

    rows, pvals = [], {}
    for v, sig, cdates, a_own in prepared:
        fam, k, d = v["family"], int(v["k"]), float(v["direction"])
        S = sig.reindex(cdates).to_numpy(dtype=float)
        rc = blend.reindex(cdates).to_numpy(dtype=float)
        if np.isnan(S).any() or np.isnan(rc).any():
            raise ValueError(f"{fam} 确认段有缺失")
        pool = batch_oa_ic(S, forward_sums(rc, k), k, rotation_index(len(cdates), n_perm, seed_c))
        p1 = perm_p_one_sided(pool, d)
        st = net_stats(tranche_position(sig, k, d).reindex(cdates), blend, None)
        pvals[fam] = p1 if a_own is not None else 1.0
        rows.append({"family": fam, "c_start": str(cdates[0].date()), "c_end": str(cdates[-1].date()),
                     "n_days": int(len(cdates)), "alpha_star_own": a_own, "oa_ic_C": float(pool[0]),
                     "p_one_sided_C": p1, "spot_net_sharpe_C": st["net_sharpe"],
                     "spot_net_ann_C": st["net_ann"], "sign_ok": bool(pool[0] * d > 0),
                     "sharpe_ok": bool(st["net_sharpe"] > 0)})
    holm = holm_pass(pvals, alpha_used) if alpha_used is not None else {f: False for f in pvals}
    for row in rows:
        row["alpha_star_used"] = alpha_used
        row["p_ok_holm"] = bool(holm[row["family"]])
        row["confirm_pass"] = bool(row["sign_ok"] and row["p_ok_holm"] and row["sharpe_ok"])
    return pd.DataFrame(rows), pd.concat(tables, ignore_index=True), pd.concat(raws, ignore_index=True)


def assemble_verdicts(main: pd.DataFrame, conf) -> pd.DataFrame:
    """§3.7 逐族裁决：STOP / STOP_NOT_REPLICATED / GO_ENTRY。"""
    out = main.copy()
    verdict, cpass = [], []
    for _, row in out.iterrows():
        if not row["main_pass"]:
            verdict.append("STOP")
            cpass.append(None)
            continue
        c = conf[conf["family"] == row["family"]].iloc[0]
        cpass.append(bool(c["confirm_pass"]))
        verdict.append("GO_ENTRY" if c["confirm_pass"] else "STOP_NOT_REPLICATED")
    out["confirm_pass"] = cpass
    out["verdict"] = verdict
    return out


# ---------------------------------------------------------------- 报告与编排
def _md_table(df: pd.DataFrame) -> str:
    def fmt(v):
        return f"{v:.4f}" if isinstance(v, (float, np.floating)) else str(v)
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "---|" * len(df.columns)
    body = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def write_report(out_dir: Path, verdicts: pd.DataFrame, conf) -> None:
    cols = ["family", "lb", "zw", "k", "direction", "oa_ic", "oa_ic_H1", "oa_ic_H2", "p_two_sided_naive",
            "partial_oa_ic_vs_ew", "p_partial_vs_ew", "sym_net_sharpe", "gate1", "gate2", "gate3",
            "gate0_min_p_diag", "gate0_max_t_diag", "verdict"]
    lines = ["# 等权/市值加权相对强弱面·run 摘要", "",
             "规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md", "", "## 主样本（逐族代表）", "",
             _md_table(verdicts[cols])]
    if conf is not None:
        lines += ["", "## 确认段", "", _md_table(conf)]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_pipeline(ew, cw, blend, carry, ctrl_ew, ctrl_sl, out_dir: Path, *, n_perm: int = N_PERM,
                 n_cal: int = N_CAL, force_confirm: bool = False, log=print) -> dict:
    """D 段三关 → 过关族开封 C。force_confirm 只供冒烟（合成数据）走通 C 段代码，正式 run 禁用。"""
    log("主样本：32 变体 + 逐族三关 ...")
    main = evaluate_main(ew, cw, blend, carry, ctrl_ew, ctrl_sl, n_perm=n_perm)
    main["panel"].to_csv(out_dir / "panel_main.csv", index=False)
    main["per_pair"].to_csv(out_dir / "report_per_pair.csv", index=False)
    main["incumbents"].to_csv(out_dir / "report_incumbent_reference.csv", index=False)
    (out_dir / "report_yearly_net.json").write_text(json.dumps(main["yearly"], indent=2), encoding="utf-8")
    mv = main["verdicts"]
    passing = [row for row in mv.to_dict("records") if row["main_pass"] or force_confirm]
    conf = None
    if passing:
        log(f"确认段：{[p['family'] for p in passing]} → 先校准 α*，再读 C 段 ...")
        conf, table, raw = confirm_families(passing, ew, cw, blend, n_cal=n_cal, n_perm=n_perm)
        conf.to_csv(out_dir / "confirmation.csv", index=False)
        table.to_csv(out_dir / "calibration.csv", index=False)
        raw.to_csv(out_dir / "calibration_raw.csv", index=False)
    else:
        log("无族通过主样本三关 → 确认段不开封")
    verdicts = assemble_verdicts(mv, conf)
    verdicts.to_csv(out_dir / "verdicts.csv", index=False)
    write_report(out_dir, verdicts, conf)
    return {"verdicts": verdicts, "confirmation": conf}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def _read_signal(path: Path) -> pd.Series:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    return df["factor_value"].astype(float)


def run_formal(run_id: str, db=None) -> Path:
    """正式 run：目录已存在即拒（只跑一次）；输入先冻结进 run 再从副本读取。"""
    from backtest.data import load_carry
    from backtest.run_manifest import artifact_record, create_run_dir, git_state, write_manifest
    run = create_run_dir(RUN_ROOT, run_id)
    log_path = run / "logs" / "run.log"

    def log(msg: str) -> None:
        line = f"{_now()} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    prereg_rel = str(PREREG.relative_to(ROOT))
    write_manifest(run, {"status": "running", "created_utc": _now(), "git": git_state(ROOT),
                         "prereg": prereg_rel})
    inp, out = run / "inputs", run / "outputs"
    ew_x = _copy(EW_XLSX, inp / EW_XLSX.name)
    cw_x = _copy(CW_XLSX, inp / CW_XLSX.name)
    _copy(PREREG, inp / "prereg.md")
    ew_csv = _copy(EW_SIGNAL_CSV, inp / EW_SIGNAL_CSV.name)
    sl_csv = _copy(SLOPE20_CSV, inp / SLOPE20_CSV.name)
    log("读冻结副本、做数据前置 ...")
    ew = read_wind_export(ew_x, EW_CODES)
    cw = read_wind_export(cw_x, CW_CODES)
    blend = blend_from_export(cw)
    checks = data_checks(ew, cw)
    checks.update(reconcile_with_db(cw, blend, db=db))
    carry = load_carry("blend", db=db)
    carry.rename("carry").rename_axis("date").to_csv(inp / "carry_blend.csv")
    ctrl_ew, ctrl_sl = _read_signal(ew_csv), _read_signal(sl_csv)
    checks["control_last_dates"] = {"equal_weight": str(ctrl_ew.index.max().date()),
                                    "slope20": str(ctrl_sl.index.max().date())}
    checks["gate4_frozen_ok"] = bool(ctrl_ew.index.max() >= END and ctrl_sl.index.max() >= END)
    gates = ("gate1_calendar_ok", "gate2_stale_ok", "gate3_reconcile_ok", "gate4_frozen_ok")
    checks["data_ok"] = bool(all(checks[g] for g in gates))
    (out / "data_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    if checks["data_ok"]:
        res = run_pipeline(ew, cw, blend, carry, ctrl_ew, ctrl_sl, out, log=log)
        summary = dict(zip(res["verdicts"]["family"], res["verdicts"]["verdict"]))
    else:
        log("数据前置不过 → data_blocked，不跑任何闸")
        summary = {fam: "DATA_BLOCKED" for fam in FAMILIES}
        pd.DataFrame({"family": list(summary), "verdict": list(summary.values())}).to_csv(
            out / "verdicts.csv", index=False)
    for rel in CODE_FILES:
        _copy(ROOT / rel, inp / "code" / rel.replace("/", "__"))
    files = [p for sub in ("inputs", "outputs", "logs") for p in sorted((run / sub).rglob("*")) if p.is_file()]
    write_manifest(run, {"status": "complete", "completed_utc": _now(), "git": git_state(ROOT),
                         "prereg": prereg_rel, "verdicts": summary, "n_perm": N_PERM, "n_cal": N_CAL,
                         "seeds": {"main": SEED_D, "calibration": SEED_CAL, "confirmation": SEED_C},
                         "artifacts": [artifact_record(p, run) for p in files]})
    log(f"完成：{summary}")
    return run


def synthetic_inputs(seed: int = 7):
    """冒烟用合成六条指数 + 控制信号 + carry（工作日日历、同一冻结窗口），不读任何真实数据。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(DATA_START, END)
    n = len(idx)
    common = rng.normal(0.0003, 0.012, n)

    def walk(start_pos: int = 0) -> pd.Series:
        s = pd.Series(1000.0 * np.cumprod(1.0 + common + rng.normal(0.0, 0.004, n)), index=idx)
        s.iloc[:start_pos] = np.nan
        return s

    start_1000 = int(idx.searchsorted(pd.Timestamp("2013-12-31")))
    ew = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk(start_1000)})
    cw = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk()})
    ctrl_idx = idx[idx >= pd.Timestamp(D_WIN[0])]
    ctrl_ew = pd.Series(np.tanh(rng.normal(0.0, 1.0, len(ctrl_idx))), index=ctrl_idx)
    ctrl_sl = pd.Series(np.tanh(rng.normal(0.0, 1.0, len(ctrl_idx))), index=ctrl_idx)
    carry = pd.Series(rng.normal(0.05, 0.02, n), index=idx)
    return ew, cw, blend_from_export(cw), carry, ctrl_ew, ctrl_sl


def run_smoke(out_dir: Path, n_perm: int = 49, n_cal: int = 5) -> dict:
    """开发冒烟：合成数据走通全流程（含确认段，force_confirm），写到 out_dir，不碰 RUN_ROOT/数据库。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ew, cw, blend, carry, ctrl_ew, ctrl_sl = synthetic_inputs()
    (out_dir / "data_checks.json").write_text(json.dumps(data_checks(ew, cw), indent=2), encoding="utf-8")
    return run_pipeline(ew, cw, blend, carry, ctrl_ew, ctrl_sl, out_dir,
                        n_perm=n_perm, n_cal=n_cal, force_confirm=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="等权/市值加权相对强弱面·入场券探针")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id", help="正式 run（只跑一次；目录已存在即拒绝）")
    g.add_argument("--smoke", action="store_true", help="开发冒烟：只喂合成数据")
    ap.add_argument("--out", type=Path, help="--smoke 的输出目录")
    a = ap.parse_args()
    if a.smoke:
        if a.out is None:
            ap.error("--smoke 需要 --out")
        res = run_smoke(a.out)
        print(res["verdicts"][["family", "verdict"]].to_string(index=False))
        return 0
    run = run_formal(a.run_id)
    print((run / "outputs" / "verdicts.csv").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 2: 跑单测，全绿**

Run: `cd /home/elfbob/claude-code/style_timing_signal && python3 -m pytest tests/test_ew_cw_axis_probe.py -q 2>&1 | tail -8`
Expected: `17 passed`

若有失败：先判断是计划代码的 bug 还是测试字面量算错——**两者都要手算核对后再改，并在报告中逐条写明改了什么、为什么**。不得为了转绿而放宽断言。

---

### Task 3: 变异验证（每条都必须变红，然后恢复）

对下表每一条：临时改动 → 跑指定测试 → 记录是否失败 → **恢复原样**（用 `git diff --no-index` 或备份文件确认已恢复）。全部做完后再跑一次整文件确认 17 passed。

| # | 位置 | 变异 | 应变红的测试 |
|---|---|---|---|
| M1 | `pair_signal` | `sig.iloc[: lb + zw - 1]` → `sig.iloc[: lb + zw - 2]` | `test_pair_signal_mask_first_available_position` |
| M2 | `family_signal` | `sigs.mean(axis=1, skipna=True)` → `sigs.fillna(0.0).mean(axis=1)` | `test_family_f2_averages_available_pairs_only` |
| M3 | `forward_sums` | `c[i + k + 1] - c[i + 1]` → `c[i + k] - c[i]`（混入决策日收益） | `test_forward_sums_window_internal` |
| M4 | `block_points` | `np.arange(o, n - k, k)` → `np.arange(o, n - k + 1, k)` | `test_block_points` |
| M5 | `batch_oa_ic` | `.mean(axis=1)` → `[:, 0]`（只用单起点） | `test_oa_ic_hand_computed` |
| M6 | `rotation_index` | `base[None, :] - shifts[:, None]` → `base[None, :] + shifts[:, None]` | `test_rotation_index_roll_convention_and_identity` |
| M7 | `perm_p_two_sided` | `>=` → `>`（不含观测自身） | `test_perm_p_two_and_one_sided` |
| M8 | `tranche_position` | `min_periods=k` → `min_periods=1` | `test_tranche_position_hand_computed` |
| M9 | `pick_representative` | 排序键 `["_worst", "_abs", …]` → `["_abs", "_worst", …]` | `test_pick_representative_rules` |
| M10 | 常量 | `C_END = "2013-12-31"` → `"2014-01-10"` | `test_confirmation_window_excludes_2014_returns` |
| M11 | `choose_alpha_star` | `max(good)` → `min(good)` | `test_choose_alpha_star` |
| M12 | `holm_pass` | `p1 <= alpha / 2` → `p1 <= alpha` | `test_holm_pass` |
| M13 | `_resid` | `beta = (xm @ cm) / (cm @ cm)` → `beta = 0.0 * (xm @ cm)` | `test_partial_and_legacy_match_rotation_probe` |
| M14 | `data_checks` | `symmetric_difference` → `intersection` 后取 `len(...) * 0` | `test_data_checks_flag_stale_and_calendar` |

Run（每条）: `python3 -m pytest tests/test_ew_cw_axis_probe.py::<测试名> -q 2>&1 | tail -3`
Expected: 变异时 `1 failed`；恢复后 `1 passed`。

---

### Task 4: 冒烟与全套件收集

**Step 1: CLI 冒烟（合成数据）**

Run: `cd /home/elfbob/claude-code/style_timing_signal && timeout 900 python3 -m backtest.ew_cw_axis_probe --smoke --out /tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/00b306d1-8b4d-4b3d-865a-dc1bf44d6f99/scratchpad/ew_cw_smoke 2>&1 | tail -8`
Expected: 打印 F1/F2 两行 verdict；输出目录含 `panel_main.csv`（32 行）、`confirmation.csv`、`calibration.csv`、`REPORT.md`。记录耗时。

**Step 2: 全套件只收集（查 import 错）**

Run: `python3 -m pytest tests/ --collect-only -q 2>&1 | tail -3`
Expected: 无 error；报告收集到的用例总数。

---

### Task 5（控制器）: 两路独立 QA

控制器派两个互不知情的审查代理：A 查 PIT 与窗口边界（信号只用 ≤t、前瞻 t+1..t+k 不越窗、C 段不取 2014 收益、校准不读真实 C、分批持仓无前视、可用性掩码、F2 构成、数据闸）；B 查统计口径（OA-IC 与预登记逐式一致、完整循环群与行 0 恒等、p 公式、偏 IC 与旧机器等价、min-P 调用、代表规则、三关、α\* 与 Holm、种子、裁决组装）。二者均自己重跑单测并各自追加至少 2 条变异；均不得读真实数据。

### Task 6（控制器）: 回填 §2.4 并冻结提交

回填模块/单测/变异结果到预登记 §2.4（只写实现与测试）；标题改为「用户已授权冻结」；
`git add docs/plans/2026-09-30-ew-cw-axis-prereg.md backtest/ew_cw_axis_probe.py tests/test_ew_cw_axis_probe.py` 后单独提交（先 `git diff --cached --stat` 确认只有这三个文件）。

### Task 7（控制器）: 正式 run（只一次）

`python3 -m backtest.ew_cw_axis_probe --run-id 20260930-ew-cw-axis-r1`（`setsid nohup`，日志落 run 目录；判活用 `ps -o etimes=`）；完成后核 `manifest.json` status=complete。

### Task 8（控制器）: 结果文档与登记表

写 `docs/plans/2026-09-30-ew-cw-axis-results.md`；登记表新增 `ew-vs-cw-relative-strength`；`tools/research_registry.py` 重生成 README 索引。
