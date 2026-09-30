"""等权/市值加权相对强弱面·入场券探针（2026-09-30）。

规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md（冻结后口径不改）。
两族（F1 300 集中度 / F2 三对合成）× 现役同款相对强弱（`_compute_pair_signal`，
左=等权、右=市值加权）× (lb,zw) × k = 32 变体。主样本 D 三关 → 三关全过的族才开封
确认段 C（2013-12-31 及以前），α* 先由零假设模拟定。统计量 = 全起点平均 IC（OA-IC），
置换 = 完整循环群（下标矩阵行 0 为恒等）。

CLI:
  python3 -m backtest.ew_cw_axis_probe --preflight                        # 正式前预检：只看版式/日期/缺失 + 库连通
  python3 -m backtest.ew_cw_axis_probe --run-id 20260930-ew-cw-axis-r1   # 正式：只跑一次（内部先预检）
  python3 -m backtest.ew_cw_axis_probe --smoke --out DIR                  # 开发：只喂合成数据
"""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from scipy.stats import binomtest, rankdata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 依赖全部在模块顶部导入：被别的研究线改坏时崩在 import（run 之前），而不是主样本数落盘之后
import backtest.data as bdata  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover  # noqa: E402
from backtest.research_statistics import generated_null  # noqa: E402
from backtest.rotation_probe import hold_position, nonoverlap_ic, partial_rank_ic  # noqa: E402,F401
from backtest.run_manifest import artifact_record, create_run_dir, git_state, write_manifest  # noqa: E402
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
# §2.1 各序列有效起点（闸 1 首末覆盖：首个有效日必须等于此值、末个有效日必须等于 END）
EXPECTED_FIRST = {"ew300": DATA_START, "ew500": DATA_START, "ew1000": pd.Timestamp("2013-12-31"),
                  "cw300": DATA_START, "cw500": DATA_START, "cw1000": DATA_START}
# §0 冻结前已披露日历 5,283 / 3,101 日去掉 2026-09-30（截到 END 后）：各序列有效日数必须等于此值
EXPECTED_DAYS = {"ew300": 5282, "ew500": 5282, "ew1000": 3100, "cw300": 5282, "cw500": 5282, "cw1000": 5282}
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
# 冻结护栏覆盖的依赖闭包：import backtest.ew_cw_axis_probe 之后加载的全部仓库内模块（静态显式列表，
# 由 tests 的子进程完整性测试钉住；research_statistics 会带进 execution_audit / execution_ledger 等）
CODE_FILES = ("backtest/__init__.py", "backtest/baseline.py", "backtest/data.py", "backtest/engine.py",
              "backtest/ew_cw_axis_probe.py", "backtest/execution_audit.py", "backtest/execution_ledger.py",
              "backtest/gate0_criterion_study.py", "backtest/leverage_probe.py", "backtest/metrics.py",
              "backtest/paired_bootstrap.py", "backtest/positions.py", "backtest/research_statistics.py",
              "backtest/rotation_probe.py", "backtest/run_manifest.py", "backtest/selection_permutation.py",
              "backtest/significance.py", "signals/common/config.py", "signals/equal_weight/generate_signal.py")
TEST_FILES = ("tests/test_ew_cw_axis_probe.py", "tests/test_ew_cw_axis_probe_orchestration.py")
# 冻结护栏：CODE_FILES + TEST_FILES + 预登记必须都已被 git 跟踪且相对 HEAD 无改动，正式 run 才能开（_frozen_clean）
FROZEN_FILES = (*CODE_FILES, *TEST_FILES, "docs/plans/2026-09-30-ew-cw-axis-prereg.md")
RUN_STAGES = ("created", "inputs_frozen", "data_checks_done", "main_done", "confirm_done", "verdicts_written")
(STAGE_CREATED, STAGE_INPUTS_FROZEN, STAGE_DATA_CHECKS_DONE, STAGE_MAIN_DONE, STAGE_CONFIRM_DONE,
 STAGE_VERDICTS_WRITTEN) = RUN_STAGES


# ---------------------------------------------------------------- 数据
def read_wind_export(path: Path, codes: dict[str, str]) -> pd.DataFrame:
    """Wind 导出版式（行 0~3 元数据/中文名，行 4 代码行，行 5 起数据）→ 列 = 配对键，截 [DATA_START, END]。

    日期解析失败（NaT）的行：所选代码列全空 → 视为页脚/空行丢弃（如「数据来源：Wind」）；
    所选代码列带数值 → 抛 ValueError（不静默丢数）。
    """
    raw = pd.read_excel(path, header=None)
    header = [str(c).strip() for c in raw.iloc[4].tolist()]
    body = raw.iloc[5:].copy()
    body.columns = header
    out = {}
    for key, code in codes.items():
        if code not in header:
            raise ValueError(f"{Path(path).name} 缺代码 {code}（实有 {header[1:]}）")
        out[key] = pd.to_numeric(body[code], errors="coerce").to_numpy()
    idx = pd.DatetimeIndex(pd.to_datetime(body[header[0]], errors="coerce"), name="date")
    df = pd.DataFrame(out, index=idx)
    nat = np.asarray(df.index.isna())
    bad = nat & df.notna().any(axis=1).to_numpy()
    if bad.any():
        raise ValueError(f"{Path(path).name} 有 {int(bad.sum())} 行日期无法解析但所选代码列带数值")
    df = df[~nat]                       # 不排序：保持文件原顺序，闸 1 的「唯一且单调递增」检查的就是文件本身
    return df.loc[(df.index >= DATA_START) & (df.index <= END)]


def _six_series(ew: pd.DataFrame, cw: pd.DataFrame) -> dict[str, pd.Series]:
    """六条序列，键 ew300/ew500/ew1000/cw300/cw500/cw1000（与 EXPECTED_FIRST 同键）。"""
    return {f"{side}{p}": df[p] for side, df in (("ew", ew), ("cw", cw)) for p in PAIRS}


def _common_range(e: pd.Series, c: pd.Series) -> tuple[pd.Series, pd.Series]:
    """两条去缺失序列截到共同起点 lo（配对共同有效区间）。"""
    e, c = e.dropna(), c.dropna()
    if e.empty or c.empty:
        return e, c
    lo = max(e.index.min(), c.index.min())
    return e[e.index >= lo], c[c.index >= lo]


def coverage_checks(ew: pd.DataFrame, cw: pd.DataFrame) -> dict:
    """§5 闸 1：只看日期与缺失（不算任何收益、价差）。

    - 配对共同有效区间内等权 / 市值加权交易日集合不一致日数（原口径）；
    - 六条序列各自在 [首个有效日, 末个有效日] 内的 NaN 日数（internal_nan_days）；
    - 首末覆盖：首个有效日 == EXPECTED_FIRST、末个有效日 == END；
    - 两个 frame 的日期索引唯一且单调递增；各序列有效日数 == EXPECTED_DAYS。
    """
    out = {"index_unique_monotonic": {}, "calendar_mismatch_days": {}, "internal_nan_days": {},
           "first_valid": {}, "last_valid": {}, "first_last_ok": {}, "valid_days": {}, "valid_days_ok": {}}
    for side, df in (("ew", ew), ("cw", cw)):
        out["index_unique_monotonic"][side] = bool(df.index.is_unique and df.index.is_monotonic_increasing)
    for p in PAIRS:
        e, c = _common_range(ew[p], cw[p])
        out["calendar_mismatch_days"][p] = int(len(e.index.symmetric_difference(c.index)))
    for name, s in _six_series(ew, cw).items():
        a, b = s.first_valid_index(), s.last_valid_index()
        out["first_valid"][name] = None if a is None else str(pd.Timestamp(a).date())
        out["last_valid"][name] = None if b is None else str(pd.Timestamp(b).date())
        ok = s.notna().to_numpy()
        if ok.any():                    # 按位置取 [首个有效, 末个有效]：乱序或重复索引下也不依赖标签切片
            i0, i1 = int(np.argmax(ok)), len(ok) - 1 - int(np.argmax(ok[::-1]))
            out["internal_nan_days"][name] = int((~ok[i0:i1 + 1]).sum())
        else:
            out["internal_nan_days"][name] = 0
        out["first_last_ok"][name] = bool(a is not None and a == EXPECTED_FIRST[name] and b == END)
        out["valid_days"][name] = int(s.notna().sum())
        out["valid_days_ok"][name] = bool(out["valid_days"][name] == EXPECTED_DAYS[name])
    out["gate1_calendar_ok"] = bool(
        all(out["index_unique_monotonic"].values())
        and all(v == 0 for v in out["calendar_mismatch_days"].values())
        and all(v == 0 for v in out["internal_nan_days"].values())
        and all(out["first_last_ok"].values())
        and all(out["valid_days_ok"].values()))
    return out


def data_checks(ew: pd.DataFrame, cw: pd.DataFrame) -> dict:
    """§5 闸 1（coverage_checks）/ 闸 2（六条序列各自有效区间内陈旧值）+ 只报数（|r_EW − r_CW| > 3% 日数）。"""
    out = coverage_checks(ew, cw)
    out["stale_days"], out["spread_gt3pct_days"] = {}, {}
    for name, s in _six_series(ew, cw).items():
        v = s.dropna()
        out["stale_days"][name] = int((v.diff() == 0).sum())
    for p in PAIRS:
        e, c = _common_range(ew[p], cw[p])
        j = pd.concat([e.pct_change(fill_method=None), c.pct_change(fill_method=None)],
                      axis=1, join="inner").dropna()
        out["spread_gt3pct_days"][p] = int(((j.iloc[:, 0] - j.iloc[:, 1]).abs() > 0.03).sum())
    out["gate2_stale_ok"] = all(v == 0 for v in out["stale_days"].values())
    return out


def blend_from_export(cw: pd.DataFrame) -> pd.Series:
    """标的 = (r_000905 + r_000852) / 2（与 backtest.data.blend_returns 同式）；缺值不补齐，传成 NaN。"""
    r = cw[["500", "1000"]].pct_change(fill_method=None)
    return ((r["500"] + r["1000"]) / 2.0).rename("blend")


def reconcile_with_db(cw: pd.DataFrame, blend: pd.Series, db=None) -> dict:
    """§5 闸 3：市值加权导出 vs index_daily；blend vs load_underlying_returns('blend')（D 段）。"""
    out = {"cw_overlap_days": {}, "cw_max_rel_diff": {}}
    for p in PAIRS:
        ref = bdata.load_spot_close(p, db=db)
        j = pd.concat([cw[p].rename("x"), ref.rename("db")], axis=1, join="inner").dropna()
        j = j[j.index <= END]
        out["cw_overlap_days"][p] = int(len(j))
        out["cw_max_rel_diff"][p] = float((j["x"] / j["db"] - 1.0).abs().max())
    d_dates = window(blend, *D_WIN).index
    ref_b = bdata.load_underlying_returns("blend", db=db).reindex(d_dates)
    out["blend_D_nan_days"] = int(blend.reindex(d_dates).isna().sum())   # 导出侧（max 会跳过 NaN，须单独数）
    out["blend_D_missing_in_db"] = int(ref_b.isna().sum())
    out["blend_D_max_abs_diff"] = float((blend.reindex(d_dates) - ref_b).abs().max())
    out["gate3_reconcile_ok"] = bool(
        all(v > 0 for v in out["cw_overlap_days"].values())
        and all(v <= 1e-6 for v in out["cw_max_rel_diff"].values())
        and out["blend_D_nan_days"] == 0
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
    """s 在闭区间 [a, b] 内的部分（按索引值筛，保持原顺序）。"""
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
    """单侧置换 p：#{direction·pool ≥ direction·obs} / len(pool)（pool[0] = 观测，含自身）。"""
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


def offset_book(sig_full: pd.Series, k: int, direction: float, o: int, anchor) -> pd.Series:
    """单起点子账户（只报数）：以 anchor（D 首日）为 0 点，相对位置 ≡ o (mod k) 的日子按
    direction × sign(sig) 换仓，其间持有（ffill），首个换仓日前记 0。

    换仓网格向 anchor 之前延伸，故 D 内相对位置 < o 的日子沿用 D 前最近换仓日（相对位置 o−k）
    的信号。换仓日信号为 NaN 时与 rotation_probe.hold_position 同口径（ffill 沿用上一仓）。
    o=0 在 D 段即 hold_position(sig_D × direction, k)；k 个子账户均值即 tranche_position。
    """
    a = sig_full.index.get_loc(pd.Timestamp(anchor))
    rel = np.arange(len(sig_full)) - a
    hit = (rel - o) % k == 0
    vals = np.full(len(sig_full), np.nan)
    vals[hit] = float(direction) * np.sign(sig_full.to_numpy(dtype=float)[hit])
    return pd.Series(vals, index=sig_full.index).ffill().fillna(0.0)


def net_returns(pos: pd.Series, r: pd.Series, carry) -> pd.Series:
    """逐日净收益：引擎原样（t 日收盘决策 → t+1 那一行记收益、3 bp、可选 carry）；区间 = pos 的索引。"""
    return run_strategy(pos, r.reindex(pos.index), COST_BPS, carry)["ret"]


def lag1_position(pos_full: pd.Series, dates=None) -> pd.Series:
    """§4 只报数「执行再晚一个交易日收盘成交」：在全历史上把决策仓位后移一日（收益落在 t+2），
    再截到 dates（D 首日沿用 D 前一日的仓位）；dates 为 None 时返回全历史。"""
    lagged = pos_full.shift(1).fillna(0.0)
    return lagged if dates is None else lagged.reindex(dates)


def net_stats(pos: pd.Series, r: pd.Series, carry) -> dict:
    """统计区间 = pos 的索引；逐日收益走 net_returns。"""
    ret = net_returns(pos, r, carry)
    return {"net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
            "max_dd": max_drawdown(ret), "turnover": turnover(pos)}


def yearly_net(pos: pd.Series, r: pd.Series, carry) -> dict:
    """逐自然年复利净收益（逐日收益走 net_returns）。"""
    ret = net_returns(pos, r, carry)
    return {str(y): float((1.0 + g).prod() - 1.0) for y, g in ret.groupby(ret.index.year)}


def main_gates(p_ic: float, partial: float, p_partial: float, direction: float, sign_ok: bool,
               net_sharpe: float) -> dict:
    """§3.5 主样本三关（纯函数）：关 1 = OA-IC 置换 p < GATE_P 且偏 OA-IC 与方向同号且其 p < GATE_P；
    关 2 = 代表存在（同号规则）；关 3 = 净 Sharpe > 0；三关全过才 main_pass。"""
    gate1 = bool(p_ic < GATE_P and partial * direction > 0 and p_partial < GATE_P)
    gate2 = bool(sign_ok)
    gate3 = bool(net_sharpe > 0)
    return {"gate1": gate1, "gate2": gate2, "gate3": gate3, "main_pass": bool(gate1 and gate2 and gate3)}


def variant_sharpe_report(sig: pd.Series, dates: pd.DatetimeIndex, k: int, direction: float,
                          r: pd.Series, carry, tag: str) -> dict:
    """§4 只报数：该变体分批持仓 Sharpe + k 个单起点子账户 Sharpe 的最小/中位/最大（对称、3 bp、含 carry、D 段）；
    旧口径 hold_position Sharpe = o=0 子账户。内置一致性断言（报错信息不带数值）：
    ① o=0 子账户 == hold_position(sig_D × 方向, k)；② k 个子账户均值 == tranche_position（容差 1e-12）。"""
    books = [offset_book(sig, k, direction, o, dates[0]).reindex(dates) for o in range(k)]
    legacy = hold_position(sig.reindex(dates) * direction, k)
    if not np.array_equal(books[0].to_numpy(), legacy.to_numpy()):
        raise AssertionError(f"子账户 o=0 与 hold_position 不一致 {tag}")
    tr = tranche_position(sig, k, direction).reindex(dates)
    if not np.allclose(np.mean([b.to_numpy() for b in books], axis=0), tr.to_numpy(), rtol=0.0, atol=1e-12):
        raise AssertionError(f"子账户均值与分批持仓不一致 {tag}")
    sh = [net_stats(b, r, carry)["net_sharpe"] for b in books]
    return {"report_direction": float(direction),
            "report_tranche_sym_sharpe": net_stats(tr, r, carry)["net_sharpe"],
            "report_sharpe_offset_min": float(np.min(sh)),
            "report_sharpe_offset_median": float(np.median(sh)),
            "report_sharpe_offset_max": float(np.max(sh)),
            "report_legacy_hold_sharpe": float(sh[0])}


# ---------------------------------------------------------------- 确认段：α* 与 Holm
def calibrate_confirmation(lb: int, zw: int, k: int, n_c: int, *, n_cal: int = N_CAL,
                           n_perm: int = N_PERM, seed_base: int = SEED_CAL, log=None) -> pd.DataFrame:
    """§3.6：零假设模拟下确认段单侧 p 的分布（只用模拟数据，不碰真实 C 段）。
    log（可选）：每 100 份数据集回调一行进度，只含规格 / 场景 / 计数，不含任何统计数值。"""
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
            done = ki * n_cal + rep + 1
            if log is not None and (done % 100 == 0 or done == len(CAL_KINDS) * n_cal):
                log(f"校准 ({lb},{zw},{k}) n_C={n_c} {kind} {done}/{len(CAL_KINDS) * n_cal}")
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
    dates = window(blend, *D_WIN).index
    n = len(dates)
    r = blend.reindex(dates).to_numpy(dtype=float)
    if np.isnan(r).any():
        raise ValueError("D 段标的收益有缺失")
    idx = rotation_index(n, n_perm, seed)
    fwd = {k: forward_sums(r, k) for k in GRID_K}
    for k in GRID_K:                        # 前瞻和与 pandas rolling 口径逐点复核（报错不带数值）
        ref = pd.Series(r).rolling(k).sum().shift(-k).to_numpy()
        ok = ~np.isnan(ref)
        if not (np.array_equal(ok, ~np.isnan(fwd[k])) and np.all(np.abs(fwd[k][ok] - ref[ok]) <= 1e-12)):
            raise AssertionError(f"前瞻和复核失败 k={k}")
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
                row = {"family": fam, "lb": lb, "zw": zw, "k": k, "oa_ic": float(pool[0]),
                       "p_two_sided_naive": perm_p_two_sided(pool),
                       "ic_offset_min": float(off[0].min()), "ic_offset_median": float(np.median(off[0])),
                       "ic_offset_max": float(off[0].max()), "legacy_ic": float(off[0, k - 1]),
                       # 旧机器原值只报数不断言：精确并列时两种求和方式的秩可能不同
                       "report_legacy_ic_rotation_probe": float(
                           nonoverlap_ic(sig.reindex(dates), blend.reindex(dates), k)[0]),
                       "n_blocks_offset0": int(len(block_points(n, k, 0)))}
                for h, (hd, hf) in halves.items():
                    Sh = sig.reindex(hd).to_numpy(dtype=float)
                    row[f"oa_ic_{h}"] = float(batch_oa_ic(Sh, hf[k], k, np.arange(len(hd))[None, :])[0])
                row.update(variant_sharpe_report(sig, dates, k, float(np.sign(pool[0])) or 1.0, blend, carry,
                                                 f"{fam} ({lb},{zw}) k={k}"))
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
        pos = tranche_position(sig, k, direction).reindex(dates)
        sym = net_stats(pos, blend, carry)
        gates = main_gates(float(best["p_two_sided_naive"]), float(ppool[0]), p_partial, direction,
                           bool(sign_ok), sym["net_sharpe"])
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
            **gates,
            "gate0_min_p_diag": adjusted_pvalue(gate0, j, "min_p"),
            "gate0_max_t_diag": adjusted_pvalue(gate0, j, "max_t"),
            "report_partial_oa_ic_vs_slope20": float(spool[0]),
            "report_p_partial_vs_slope20": perm_p_two_sided(spool),
            "report_longflat_net_sharpe": net_stats(lf, blend, None)["net_sharpe"],
            "report_lag1_net_sharpe": net_stats(lag1_position(tranche_position(sig, k, direction), dates),
                                                blend, carry)["net_sharpe"],
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
                     seed_cal: int = SEED_CAL, seed_c: int = SEED_C, log=None):
    """§3.6：先按各族代表规格做零假设校准定 α*（只用模拟），全部定完才读真实 C 段一次。
    校准按 (lb, zw, k, n_C) 缓存：种子只依赖场景与 rep，两族同规格时逐位相同、只算一次。"""
    prepared, tables, raws, cache = [], [], [], {}
    for v in passing:
        fam, lb, zw, k = v["family"], int(v["lb"]), int(v["zw"]), int(v["k"])
        sig = family_signal(ew, cw, fam, lb, zw)
        cdates = window(blend, sig.dropna().index.min(), C_END).index
        key = (lb, zw, k, len(cdates))
        if key not in cache:
            cache[key] = calibrate_confirmation(
                lb, zw, k, len(cdates), n_cal=n_cal, n_perm=n_perm, seed_base=seed_cal,
                log=None if log is None else (lambda msg, f=fam: log(f"{f} {msg}")))
        raw = cache[key]
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


def assemble_verdicts(main_df: pd.DataFrame, conf) -> pd.DataFrame:
    """§3.7 逐族裁决：STOP / STOP_NOT_REPLICATED / GO_ENTRY。"""
    out = main_df.copy()
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
                 n_cal: int = N_CAL, force_confirm: bool = False, log=print, stage: dict | None = None) -> dict:
    """D 段三关 → 过关族开封 C。force_confirm 只供冒烟（合成数据）走通 C 段代码，正式 run 禁用。

    stage（可选，失败留痕用）：evaluate_main 一返回即 main_done（先于写文件）；confirm_families 一返回即
    confirm_done；verdicts.csv 与 REPORT.md 写完才 verdicts_written。主样本逐族裁决先落 verdicts_main.csv，
    再进确认段（α* 先定、C 段后读的顺序不变）。
    """
    stage = {} if stage is None else stage
    log("主样本：32 变体 + 逐族三关 ...")
    main_res = evaluate_main(ew, cw, blend, carry, ctrl_ew, ctrl_sl, n_perm=n_perm)
    stage["stage"] = STAGE_MAIN_DONE
    mv = main_res["verdicts"]
    mv.to_csv(out_dir / "verdicts_main.csv", index=False)
    main_res["panel"].to_csv(out_dir / "panel_main.csv", index=False)
    main_res["per_pair"].to_csv(out_dir / "report_per_pair.csv", index=False)
    main_res["incumbents"].to_csv(out_dir / "report_incumbent_reference.csv", index=False)
    (out_dir / "report_yearly_net.json").write_text(json.dumps(main_res["yearly"], indent=2), encoding="utf-8")
    passing = [row for row in mv.to_dict("records") if row["main_pass"] or force_confirm]
    conf = None
    if passing:
        log(f"确认段：{[p['family'] for p in passing]} → 先校准 α*，再读 C 段 ...")
        conf, table, raw = confirm_families(passing, ew, cw, blend, n_cal=n_cal, n_perm=n_perm, log=log)
        stage["stage"] = STAGE_CONFIRM_DONE
        conf.to_csv(out_dir / "confirmation.csv", index=False)
        table.to_csv(out_dir / "calibration.csv", index=False)
        raw.to_csv(out_dir / "calibration_raw.csv", index=False)
    else:
        log("无族通过主样本三关 → 确认段不开封")
    verdicts = assemble_verdicts(mv, conf)
    verdicts.to_csv(out_dir / "verdicts.csv", index=False)
    write_report(out_dir, verdicts, conf)
    stage["stage"] = STAGE_VERDICTS_WRITTEN
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


def _db_ping(db=None, timeout_s: int = 60) -> None:
    """数据库连通性（不读任何研究数据）：`SELECT 1` + 大包探针 `SELECT repeat('x', 200000)`，
    SIGALRM 限时 timeout_s 秒，挂住即判不通（Tailscale MTU 黑洞先例：小包通、大包黑洞）。单测打桩替换。"""
    def _timeout(signum, frame):
        raise TimeoutError(f"数据库探针 {timeout_s}s 未返回（疑似大包黑洞）")

    prev = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(int(timeout_s))
    try:
        conn = bdata._connect(db or bdata.load_db_config())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute("SELECT repeat('x', 200000)")
                if len(cur.fetchone()[0]) != 200000:
                    raise RuntimeError("大包探针返回长度不符")
        finally:
            conn.close()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def preflight(ew_path: Path, cw_path: Path, ew_csv: Path, sl_csv: Path, db=None) -> dict:
    """正式 run 前的预检：只看版式 / 日期 / 缺失与数据库连通，不算任何收益、价差、IC、Sharpe、相关。

    两个 xlsx 走 read_wind_export + coverage_checks（不调用 data_checks）；两个控制信号在 D 段
    （市值加权文件日历 ∩ D_WIN）每个交易日都在且非 NaN；数据库只做 SELECT 1。
    不抛异常：异常收成字符串写进 errors；ok = 无 errors 且覆盖、控制信号、数据库三项全过。
    """
    out = {"ok": False, "errors": {}, "coverage": None, "controls": {}, "db_ok": False}
    frames = {}
    for name, path, codes in (("ew_xlsx", ew_path, EW_CODES), ("cw_xlsx", cw_path, CW_CODES)):
        try:
            frames[name] = read_wind_export(path, codes)
        except Exception as exc:  # noqa: BLE001 —— 预检不抛，收成字符串
            out["errors"][name] = _err(exc)
    if len(frames) == 2:
        try:
            out["coverage"] = coverage_checks(frames["ew_xlsx"], frames["cw_xlsx"])
        except Exception as exc:  # noqa: BLE001
            out["errors"]["coverage"] = _err(exc)
    d_dates = None
    if "cw_xlsx" in frames:
        cal = frames["cw_xlsx"].index
        d_dates = cal[(cal >= pd.Timestamp(D_WIN[0])) & (cal <= pd.Timestamp(D_WIN[1]))]
    for name, path in (("equal_weight", ew_csv), ("slope20", sl_csv)):
        if d_dates is None:
            out["errors"][f"control_{name}"] = "市值加权文件不可读，无法取 D 段日历"
            continue
        try:
            s = _read_signal(path)
            out["controls"][name] = {
                "last_date": str(s.index.max().date()) if len(s) else None,
                "d_days": int(len(d_dates)),
                "d_missing_days": int((~d_dates.isin(s.index)).sum()),
                "d_nan_days": int(s.reindex(d_dates).isna().sum())}   # 含缺日
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"control_{name}"] = _err(exc)
    try:
        _db_ping(db)
        out["db_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["errors"]["db"] = _err(exc)
    out["coverage_ok"] = bool(out["coverage"] is not None and out["coverage"]["gate1_calendar_ok"])
    out["controls_ok"] = bool(len(out["controls"]) == 2 and d_dates is not None and len(d_dates) > 0
                              and all(v["d_nan_days"] == 0 for v in out["controls"].values()))
    out["ok"] = bool(not out["errors"] and out["coverage_ok"] and out["controls_ok"] and out["db_ok"])
    return out


def _frozen_clean() -> bool:
    """冻结护栏：FROZEN_FILES（CODE_FILES 依赖闭包 + 两份测试 + 预登记）须已被 git 跟踪，且
    `git diff --quiet HEAD -- <全部>` 干净；git 不可用或任何异常 → False（按不干净处理）。"""
    try:
        if subprocess.run(["git", "ls-files", "--error-unmatch", "--", *FROZEN_FILES], cwd=ROOT,
                          capture_output=True).returncode != 0:
            return False
        return subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *FROZEN_FILES], cwd=ROOT,
                              capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001 —— 查不了就当不干净
        return False


def _artifacts(run: Path) -> list:
    """run 下 inputs/outputs/logs 当前全部文件的路径、大小、SHA-256。"""
    files = [p for sub in ("inputs", "outputs", "logs") for p in sorted((run / sub).rglob("*")) if p.is_file()]
    return [artifact_record(p, run) for p in files]


def freeze_carry(carry: pd.Series, path: Path) -> pd.Series:
    """carry 写冻结副本后按 round_trip 读回，须与内存对象逐位相等；之后一律用读回的序列。"""
    carry.rename("carry").rename_axis("date").to_csv(path)
    back = pd.read_csv(path, parse_dates=["date"], index_col="date", float_precision="round_trip")["carry"]
    mem = carry.astype(float)
    if not (back.index.equals(mem.index)
            and np.array_equal(back.to_numpy(dtype=float), mem.to_numpy(), equal_nan=True)):
        raise AssertionError("carry 冻结副本读回与内存不一致")
    return back


def _sigterm_to_exit(signum, frame):
    """SIGTERM → SystemExit(143)：被杀时照样走失败留痕（写 failed manifest）。"""
    sys.exit(143)


def _write_failed(run: Path, prereg_rel: str, state: dict, exc: BaseException) -> None:
    """失败留痕：阶段、异常、traceback 与当时已落盘文件的哈希清单；哈希清单出错也照样写出 manifest。"""
    payload = {"status": "failed", "failed_utc": _now(), "created_utc": state.get("created_utc"),
               "git": state.get("git"), "prereg": prereg_rel, "stage": state["stage"],
               "error_type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc()}
    try:
        payload["artifacts"] = _artifacts(run)
    except Exception as aexc:  # noqa: BLE001
        payload["artifacts_error"] = _err(aexc)
    write_manifest(run, payload)


def run_formal(run_id: str, db=None) -> Path:
    """正式 run：冻结护栏（_frozen_clean）→ 预检（不过 → SystemExit，不建目录）→ 建目录（已存在即拒，只跑一次）；
    输入、代码、测试、预登记开局即冻结进 run 并从副本读取；建目录后任何异常（含 SIGTERM → exit 143）→
    manifest 写 status=failed（阶段 stage、traceback、已落盘文件哈希清单）后原样抛出。"""
    try:
        prev = signal.signal(signal.SIGTERM, _sigterm_to_exit)
    except ValueError:                                          # 非主线程装不了处理器：照常运行
        prev = None
    try:
        if not _frozen_clean():
            raise SystemExit("冻结护栏不过，未建 run 目录：CODE_FILES / TEST_FILES / 预登记须已提交且相对 HEAD 无改动")
        pf = preflight(EW_XLSX, CW_XLSX, EW_SIGNAL_CSV, SLOPE20_CSV, db=db)
        if not pf["ok"]:
            raise SystemExit("预检不过，未建 run 目录：\n" + json.dumps(pf, ensure_ascii=False, indent=2))
        run = create_run_dir(RUN_ROOT, run_id)
        prereg_rel = str(PREREG.relative_to(ROOT))
        state = {"stage": STAGE_CREATED}
        try:
            return _run_formal_body(run, prereg_rel, pf, db, state)
        except BaseException as exc:
            _write_failed(run, prereg_rel, state, exc)
            raise
    finally:
        if prev is not None:
            signal.signal(signal.SIGTERM, prev)


def _run_formal_body(run: Path, prereg_rel: str, pf: dict, db, state: dict) -> Path:
    log_path = run / "logs" / "run.log"

    def log(msg: str) -> None:
        line = f"{_now()} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    state["created_utc"], state["git"] = _now(), git_state(ROOT)     # 开局取一次 git 状态，最终 manifest 复用
    write_manifest(run, {"status": "running", "created_utc": state["created_utc"], "git": state["git"],
                         "prereg": prereg_rel})
    inp, out = run / "inputs", run / "outputs"
    (out / "preflight.json").write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
    ew_x = _copy(EW_XLSX, inp / EW_XLSX.name)                 # 开局冻结：输入、预登记、代码闭包与测试
    cw_x = _copy(CW_XLSX, inp / CW_XLSX.name)
    _copy(PREREG, inp / "prereg.md")
    ew_csv = _copy(EW_SIGNAL_CSV, inp / EW_SIGNAL_CSV.name)
    sl_csv = _copy(SLOPE20_CSV, inp / SLOPE20_CSV.name)
    for rel in (*CODE_FILES, *TEST_FILES):
        _copy(ROOT / rel, inp / "code" / rel.replace("/", "__"))
    state["stage"] = STAGE_INPUTS_FROZEN
    log("读冻结副本、做数据前置 ...")
    ew = read_wind_export(ew_x, EW_CODES)
    cw = read_wind_export(cw_x, CW_CODES)
    blend = blend_from_export(cw)
    checks = data_checks(ew, cw)
    checks.update(reconcile_with_db(cw, blend, db=db))
    carry = freeze_carry(bdata.load_carry("blend", db=db), inp / "carry_blend.csv")
    d_dates = window(blend, *D_WIN).index
    checks["carry_first"] = str(carry.index.min().date()) if len(carry) else None     # 只报数
    checks["carry_last"] = str(carry.index.max().date()) if len(carry) else None
    checks["carry_missing_D_days"] = int((~d_dates.isin(carry.dropna().index)).sum())
    ctrl_ew, ctrl_sl = _read_signal(ew_csv), _read_signal(sl_csv)
    checks["control_last_dates"] = {"equal_weight": str(ctrl_ew.index.max().date()),
                                    "slope20": str(ctrl_sl.index.max().date())}
    checks["gate4_frozen_ok"] = bool(ctrl_ew.index.max() >= END and ctrl_sl.index.max() >= END)
    gates = ("gate1_calendar_ok", "gate2_stale_ok", "gate3_reconcile_ok", "gate4_frozen_ok")
    checks["data_ok"] = bool(all(checks[g] for g in gates))
    (out / "data_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    state["stage"] = STAGE_DATA_CHECKS_DONE
    if checks["data_ok"]:
        res = run_pipeline(ew, cw, blend, carry, ctrl_ew, ctrl_sl, out, n_perm=N_PERM, n_cal=N_CAL,
                           log=log, stage=state)
        summary = dict(zip(res["verdicts"]["family"], res["verdicts"]["verdict"]))
    else:
        log("数据前置不过 → data_blocked，不跑任何闸")
        summary = {fam: "DATA_BLOCKED" for fam in FAMILIES}
        pd.DataFrame({"family": list(summary), "verdict": list(summary.values())}).to_csv(
            out / "verdicts.csv", index=False)
        state["stage"] = STAGE_VERDICTS_WRITTEN
    log(f"完成：{summary}")                                   # 最后一行日志先写完，再算哈希、写 manifest
    try:
        git_end = git_state(ROOT)                             # 只记录，失败不影响 status
    except Exception as exc:  # noqa: BLE001
        git_end = {"error": _err(exc)}
    write_manifest(run, {"status": "complete", "created_utc": state["created_utc"], "completed_utc": _now(),
                         "git": state["git"], "git_end": git_end, "prereg": prereg_rel,
                         "stage": state["stage"], "verdicts": summary, "n_perm": N_PERM, "n_cal": N_CAL,
                         "seeds": {"main": SEED_D, "calibration": SEED_CAL, "confirmation": SEED_C},
                         "artifacts": _artifacts(run)})
    return run                                                  # manifest 写定后不再写任何文件


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
    """开发冒烟：合成数据走通全流程（含确认段，force_confirm），写到 out_dir，不碰 RUN_ROOT/数据库。

    合成工作日日历的天数不等于 EXPECTED_DAYS，故冒烟 data_checks.json 的 gate1_calendar_ok 恒为 False；
    冒烟不设闸，该文件只作参考。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ew, cw, blend, carry, ctrl_ew, ctrl_sl = synthetic_inputs()
    (out_dir / "data_checks.json").write_text(json.dumps(data_checks(ew, cw), indent=2), encoding="utf-8")
    return run_pipeline(ew, cw, blend, carry, ctrl_ew, ctrl_sl, out_dir,
                        n_perm=n_perm, n_cal=n_cal, force_confirm=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="等权/市值加权相对强弱面·入场券探针")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id", help="正式 run（只跑一次；目录已存在即拒绝；内部先预检）")
    g.add_argument("--smoke", action="store_true", help="开发冒烟：只喂合成数据")
    g.add_argument("--preflight", action="store_true",
                   help="正式前预检：只看版式/日期/缺失与数据库连通，不算任何收益（ok → 退出码 0，否则 1）")
    ap.add_argument("--out", type=Path, help="--smoke 的输出目录")
    a = ap.parse_args()
    if a.out is not None and not a.smoke:
        ap.error("--out 只用于 --smoke")
    if a.preflight:
        pf = preflight(EW_XLSX, CW_XLSX, EW_SIGNAL_CSV, SLOPE20_CSV)
        print(json.dumps(pf, ensure_ascii=False, indent=2))
        return 0 if pf["ok"] else 1
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
