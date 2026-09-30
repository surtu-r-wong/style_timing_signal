"""② 探针：四对配对的横截面分歧度 / 离散度（48 点**合成一次**调用 · 四关闸门）。

预登记出处：`docs/plans/2026-08-12-signal-research-agenda.md` §3 ②(f)——
> 族：D1 大小盘分歧（300 对 − 1000 对）、D2 四对截面标准差、D3 四对符号一致度（多少对同向）。
> 网格沿用 `level_signal` lb∈{5,20}×zw∈{60,250}，持有 k∈{5,10,20,40}，**预登记不扩**。
> **GO**：三关全过——关1 置换 p<0.05 **且**偏 IC（控 ew）同号 p<0.05；关2 两半窗同号；
> 关3 成本后净 Sharpe>0。**任一不过 → STOP 归档**。

渊源：design `2026-07-03-optimization-roadmap-design.md` §2.2 原案「300 对不入等权，
改造成**大小盘风格分歧变量**（300 对与 1000 对的差值），归入环境变量池」；
retrospective `2026-07-10-...-retrospective.md` 第 38-43 行「300 对未单独转分歧变量」、
「市值轴与离散度未单独立探针（…后者与广度同面，**重叠度判定跳过**）」——
**跳过是判断不是实测**，故本探针必须同时报 corr(分歧度, 广度) 作为诊断列。

## 机器接入三规矩（`2026-08-12-selection-permutation-machine.md` §6，违反即作废）

1. **48 点（3 族 × 16 点）合成同一次 `selection_permutation_test` 调用**——不做 3 次族级
   独立调用（否则"3 次独立的 PASS 机会"未被校正，等于把选优搬到族层）；
2. **关1 的统计量不携带关2 的同号约束**：本模块的 `score` 是**无约束** `|非重叠 IC|`，
   `-inf` 只可能来自"该行无法评分"（统计量退化为 NaN）；关2 作为独立后置闸门单独判
   （§9 第 11 条边界：把闸门塞进统计量会双记且偏移 p 的含义）；
3. **`min_shift = 2·max(k) = 80`，`max_shift = n_obs − 2·max(k)`**（房规 [2k, n−2k]）。

## 选择端纪律（§7.3 第 3 条 / §4 C2）

选择与全部闸门只用 **train(2014-2020) + val(2021-2023)** 这段样本；
**2024-2026 只报告不选择**（07-11 勘误 §3 已把它降级为第二验证窗）。
机器的 n_obs 因此就是这段选择样本的长度，循环移位也只在这段内部发生。

## 口径

- 分歧度的输入 = **生产参数下的四对配对信号**（lookback=20 / z_window=40 / 不平滑，
  即 `calculate_contrast_equal_weight_signal` 的 `pair_01..04_factor_20` 四列），
  与生产 equal_weight（= 这四列的均值再 sm5）是同一批数；②(a) 问的正是这批数的
  **二阶结构**是否独立于其**一阶均值**；
- 三族原始水平量 → `leverage_probe.level_signal(lb, zw)` 信号化（与 C/B 两轴同机器）；
- IC = `rotation_probe.nonoverlap_ic` 口径（块末信号 vs 下一块**算术和**收益，双侧取 |·|）；
- 偏 IC = `rotation_probe.partial_rank_ic` 口径（控 `output/equal_weight/...20d40z.csv`
  的 `factor_value`，移信号、控/收益不动）；
- 收益层同秤 = `backtest.engine.run_strategy`（3 bps + blend carry）+ `backtest.metrics.sharpe`。

本模块的向量化统计量与上述两个房规函数有**逐位一致性判例**
（`tests/test_bt_divergence_probe.py`），不是另起口径。

CLI: python3 -m backtest.divergence_probe [--n-perm 1000] [--seed 0] [--db-host 100.75.102.44]
产出: backtest/output/probe_2_divergence_{panel,permutation,diagnostics,verdict}.csv
      + probe_2_divergence_summary.json（置换 sidecar）

本模块**不改动** `signals/` / `selection_permutation.py` / `rotation_probe.py` /
`leverage_probe.py` / 任何冻结根。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.rotation_probe import (  # noqa: E402
    _load_ew_signal, hold_position, nonoverlap_ic,
)
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

# ---------------------------------------------------------------- 预登记常量（不得扩）
PAIR_LOOKBACK = 20          # 生产参数：四对配对信号的 lookback
PAIR_Z_WINDOW = 40          # 生产参数：四对配对信号的 z_window
PAIR_NAMES = ("300", "500", "1000", "2000")   # config_4pairs.csv 的 group 1..4
INDEX_NAMES = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
               "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]

FAMILIES = ("D1", "D2", "D3")
GRID_LB = (5, 20)
GRID_ZW = (60, 250)
GRID_K = (5, 10, 20, 40)
N_VARIANTS = len(FAMILIES) * len(GRID_LB) * len(GRID_ZW) * len(GRID_K)   # = 48

SELECTION = ("2014-01-01", "2023-12-31")      # 选择端样本（train + val）
TRAIN = ("2014-01-01", "2020-12-31")
VAL = ("2021-01-01", "2023-12-31")
HOLDOUT = ("2024-01-01", "2026-12-31")        # 只报告，不选择、不进闸门
WINDOWS_REPORT = {
    "selection_2014_2023": SELECTION,
    "train_2014_2020": TRAIN,
    "val_2021_2023": VAL,
    "holdout_2024_2026": HOLDOUT,
    "full_2014_2026": (None, None),
}
GATE_ALPHA = 0.05
PRIMARY_KOU_JING = "blend"
KOU_JING_REPORT = ("500", "1000", "blend")


# ---------------------------------------------------------------- 纯函数：三族水平量
def divergence_levels(pairs: pd.DataFrame) -> dict[str, pd.Series]:
    """四对配对信号的横截面二阶结构三族（②(f) 原文定义）。

    - **D1 大小盘分歧** = 300 对 − 1000 对（design §2.2 原案的"差值"，有符号）；
    - **D2 截面离散度** = 四对的横截面标准差（ddof=1；`level_signal` 的 z 化对尺度不变，
      故 ddof 取值不影响任何下游数字）；
    - **D3 符号一致度** = `max(#正, #负)` ∈ [0, 4]，字面即"多少对同向"。
      它与 `|Σ sign|` 是**仿射变换**关系（无零号时 `max = 2 + |Σ sign| / 2`），
      而 `level_signal` 的滚动 z 化对仿射变换不变 → 两种写法给出**逐位相同**的信号；
      取 `max(#正, #负)` 是因为它是**方向无关**的共识强度（二阶量），
      而"多少对为正"是一阶量（生产信号的粗版），会与被控变量混同。

    `pairs` 的列顺序须为 `PAIR_NAMES`。
    """
    if list(pairs.columns) != list(PAIR_NAMES):
        raise ValueError(f"pairs 列须为 {PAIR_NAMES}，得到 {list(pairs.columns)}")
    sgn = np.sign(pairs.to_numpy(dtype=float))
    n_pos = (sgn > 0).sum(axis=1)
    n_neg = (sgn < 0).sum(axis=1)
    return {
        "D1": (pairs["300"] - pairs["1000"]).rename("D1"),
        "D2": pairs.std(axis=1, ddof=1).rename("D2"),
        "D3": pd.Series(np.maximum(n_pos, n_neg).astype(float),
                        index=pairs.index, name="D3"),
    }


def variant_grid(families: tuple[str, ...] = FAMILIES) -> list[tuple]:
    """预登记 48 点（族 × lb × zw × k），**顺序固定**（判例与产物可对表）。"""
    return [(fam, lb, zw, k)
            for fam in families for lb in GRID_LB for zw in GRID_ZW for k in GRID_K]


def warmup_masked_level_signal(level: pd.Series, lookback: int, z_window: int) -> pd.Series:
    """`level_signal` + **暖机段置 NaN**（房规的 `fillna(0)` 在这里会造假样本）。

    `level_signal` 对暖机段（滚动 z 的窗口未满）把 z 填 0 → 输出一段**常数 0 的假信号**。
    单变体探针里这只是钝化；但本探针要求**全网格共用一条时间索引**，
    zw=60 与 zw=250 的暖机长度不同，若不置 NaN 就会出现"同一条索引上各变体假样本量不等"
    的不可比，且常数段会给 Spearman 灌入大量并列。
    → 按位置把前 `z_window + lookback − 2` 行置 NaN（这正是"z 窗未满**或**滚动均值窗未满"
    的全部行），交给共用索引的交集丢掉——即机器文档 §6 共同纪律描述的做法
    （"取全网格非 NaN 日期的交集，40 点网格因 zw=250 的暖机而损失约 1 年样本"）。
    """
    sig = level_signal(level, lookback, z_window).copy()
    n_warm = min(len(sig), z_window + lookback - 2)
    sig.iloc[:n_warm] = np.nan
    return sig


def build_variant_signals(levels: dict[str, pd.Series],
                          families: tuple[str, ...] = FAMILIES) -> dict[tuple, pd.Series]:
    """48 个变体信号（k 不改信号本身，同 (fam,lb,zw) 的四个 k 共用同一条序列对象）。"""
    base = {(fam, lb, zw): warmup_masked_level_signal(levels[fam].dropna(), lb, zw)
            for fam in families for lb in GRID_LB for zw in GRID_ZW}
    return {(fam, lb, zw, k): base[(fam, lb, zw)]
            for fam, lb, zw, k in variant_grid(families)}


def nonoverlap_points(n: int, k: int) -> np.ndarray:
    """非重叠采样的块末位置 k−1, 2k−1, …（`rotation_probe._nonoverlap_frame` 同口径）。"""
    return np.arange(k - 1, n, k)


# ---------------------------------------------------------------- 向量化统计量（有一致性判例）
@dataclass(frozen=True)
class ScoreFrame:
    """某个 k 下**固定不动**的一侧：块末位置 + 前瞻收益/控制变量的秩与残差，预算一次。

    置换只重排信号侧（房规：控/收益按日历不动），故这些量在整次运行里是常量。
    """
    k: int
    pts: np.ndarray            # (n_pts,) 有效块末位置（fwd 非 NaN）
    fwd_rank_c: np.ndarray     # 前瞻收益秩（中心化）
    fwd_ss: float
    ctrl_rank_c: np.ndarray    # 控制变量秩（中心化）
    ctrl_ss: float
    fwd_resid: np.ndarray      # 前瞻收益秩对控制变量秩的 OLS 残差
    fwd_resid_ss: float


def build_score_frames(und: pd.Series, ctrl: pd.Series, index: pd.DatetimeIndex,
                       grid_k: tuple[int, ...] = GRID_K) -> dict[int, ScoreFrame]:
    """逐 k 预算固定侧。`index` = 全变体共用的时间索引（机器要求 idx 跨变体共享）。"""
    r = und.reindex(index).to_numpy(dtype=float)
    c = ctrl.reindex(index).to_numpy(dtype=float)
    if not np.isfinite(c).all():
        raise ValueError("控制变量在共用索引上有缺失——共用索引构造有误")
    out: dict[int, ScoreFrame] = {}
    for k in grid_k:
        # 与 `rotation_probe._nonoverlap_frame` 逐字同口径：算术和 + shift(−k)
        fwd = pd.Series(r, index=index).rolling(k).sum().shift(-k).to_numpy()
        pts = nonoverlap_points(len(index), k)
        pts = pts[np.isfinite(fwd[pts])]
        if len(pts) < 10:
            raise ValueError(f"k={k} 的非重叠样本仅 {len(pts)} 点，不足以评分")
        fr = rankdata(fwd[pts]).astype(float)
        fr -= fr.mean()
        cr = rankdata(c[pts]).astype(float)
        cr -= cr.mean()
        cr_ss = float(cr @ cr)
        f_res = fr - (float(cr @ fr) / cr_ss) * cr if cr_ss > 0 else fr.copy()
        out[k] = ScoreFrame(k=k, pts=pts, fwd_rank_c=fr, fwd_ss=float(fr @ fr),
                            ctrl_rank_c=cr, ctrl_ss=cr_ss,
                            fwd_resid=f_res, fwd_resid_ss=float(f_res @ f_res))
    return out


def abs_spearman_batch(x_mat: np.ndarray, sf: ScoreFrame) -> np.ndarray:
    """逐行 |Spearman(信号, 前瞻收益)|——`nonoverlap_ic` 的向量化等价物（取绝对值）。"""
    rx = rankdata(np.asarray(x_mat, dtype=float), axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    num = rx @ sf.fwd_rank_c
    den = np.sqrt((rx * rx).sum(axis=1) * sf.fwd_ss)
    out = np.full(len(num), np.nan)
    ok = den > 0
    out[ok] = num[ok] / den[ok]
    return np.abs(out)


def abs_partial_batch(x_mat: np.ndarray, sf: ScoreFrame) -> np.ndarray:
    """逐行 |偏 rank IC(信号, 前瞻收益 | 控制)|——`partial_rank_ic` 的向量化等价物。

    残差方差退化时房规返回 0.0（"被 control 完全解释 → 独立增量确定为 0"），此处照抄。
    """
    rx = rankdata(np.asarray(x_mat, dtype=float), axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    beta = (rx @ sf.ctrl_rank_c) / sf.ctrl_ss
    rs = rx - beta[:, None] * sf.ctrl_rank_c[None, :]
    num = rs @ sf.fwd_resid
    den = np.sqrt((rs * rs).sum(axis=1) * sf.fwd_resid_ss)
    out = np.zeros(len(num))
    ok = den >= 1e-9
    out[ok] = num[ok] / den[ok]
    return np.abs(out)


def make_batch_scorer(signals: dict[tuple, np.ndarray], frames: dict[int, ScoreFrame],
                      scorer):
    """机器的 `batch_stat_fn`：按变体的 k 取固定侧，一次算完全部重抽样行。"""
    def _fn(variant, idx_matrix: np.ndarray) -> np.ndarray:
        sf = frames[variant[3]]
        x = signals[variant][np.asarray(idx_matrix)[:, sf.pts]]
        return scorer(x, sf)
    return _fn


# ---------------------------------------------------------------- 数据装载
def load_pair_signals(db=None, start=None) -> tuple[pd.DataFrame, pd.Series, float]:
    """四对配对信号（生产参数）+ 重建的 equal_weight + 与 committed 生产 CSV 的最大逐日差。

    走冻结根 `calculate_contrast_equal_weight_signal`，零口径漂移；
    burn-in（前 z_window−1 行 zscore 被 `fillna(0)` 成常数 0）按位置截掉，
    否则 D2/D3 会被一段人造的"零离散度"污染。
    """
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal, load_pair_configs,
    )

    prices = load_pg_closes(INDEX_NAMES, start=start, db=db)
    pair_configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    out = calculate_contrast_equal_weight_signal(
        prices, lookback=PAIR_LOOKBACK, z_window=PAIR_Z_WINDOW,
        smoothing_window=5, pair_configs=pair_configs)

    cols = [f"pair_{i:02d}_factor_{PAIR_LOOKBACK}" for i in range(1, 5)]
    pairs = out[cols].iloc[PAIR_Z_WINDOW - 1:].copy()
    pairs.columns = list(PAIR_NAMES)

    ref = _load_ew_signal()
    idx = out.index.intersection(ref.index)
    max_diff = float((out["factor_value"].reindex(idx) - ref.reindex(idx)).abs().max())
    return pairs, out["factor_value"], max_diff


def load_breadth() -> pd.DataFrame:
    from backtest.breadth_dual import load_breadth_cache
    return load_breadth_cache()


def _slice(s: pd.Series, win: tuple) -> pd.Series:
    a, b = win
    out = s
    if a is not None:
        out = out[out.index >= pd.Timestamp(a)]
    if b is not None:
        out = out[out.index <= pd.Timestamp(b)]
    return out


# ---------------------------------------------------------------- 编排
def build_ic_panel(signals: dict[tuple, pd.Series], und: dict[str, pd.Series],
                   common: pd.DatetimeIndex) -> pd.DataFrame:
    """48 变体 × 三口径 × 五窗的**有符号** IC 面板（房规 `nonoverlap_ic`，非向量化）。

    机器只吃 blend / selection 窗的 |IC|；其余是报告列（2024-26 只报告不选择）。
    """
    rows = []
    for fam, lb, zw, k in variant_grid():
        sig = signals[(fam, lb, zw, k)].reindex(common)
        for kj, u in und.items():
            row = {"family": fam, "lb": lb, "zw": zw, "k": k, "kou_jing": kj}
            for wname, win in WINDOWS_REPORT.items():
                ic, n = nonoverlap_ic(_slice(sig, win), _slice(u, win), k)
                row[f"ic_{wname}"] = ic
                row[f"n_{wname}"] = n
            rows.append(row)
    return pd.DataFrame(rows)


def run_machines(signals: dict[tuple, pd.Series], frames: dict[int, ScoreFrame],
                 common_sel: pd.DatetimeIndex, n_perm: int, seed: int):
    """关1 / 关1b 各跑一次**同款**机器，两次都是 48 点合成单次调用。"""
    variants = variant_grid()
    arrs = {v: signals[v].reindex(common_sel).to_numpy(dtype=float) for v in variants}
    n_obs = len(common_sel)
    min_shift = 2 * max(GRID_K)
    max_shift = n_obs - 2 * max(GRID_K)
    meta = {"n_obs": n_obs, "min_shift": min_shift, "max_shift": max_shift,
            "first_date": str(common_sel[0].date()), "last_date": str(common_sel[-1].date())}

    res_ic = selection_permutation_test(
        variants, n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="abs_nonoverlap_ic", meta=meta)
    res_pic = selection_permutation_test(
        variants, n_obs=n_obs,
        batch_stat_fn=make_batch_scorer(arrs, frames, abs_partial_batch),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="abs_partial_ic_vs_equal_weight", meta=meta)
    return res_ic, res_pic


def p_at_threshold(null_selected: np.ndarray, tau: float) -> float:
    """当选统计量空分布在阈值 tau 处的房规 p = `(1 + #{null ≥ tau}) / (B + 1)`。

    用于把关1 的赢家**原样**拿到关1b 的零分布上判——赢家不是关1b 机器自选的那个时，
    这是保守读法（tau ≤ 关1b 的观测最大值 → 本 p ≥ 关1b 自己的 `p_selected`）。
    """
    null_selected = np.asarray(null_selected, dtype=float)
    if not np.isfinite(tau):
        return float("nan")
    return float((1 + int(np.count_nonzero(null_selected >= tau))) / (len(null_selected) + 1))


def net_sharpe_by_window(sig: pd.Series, direction: float, k: int, und: pd.Series,
                         carry: pd.Series, cost_bps: float) -> dict[str, dict]:
    """关3：逐窗成本后净表现（3 bps + blend carry，同秤 `run_strategy`）。"""
    from backtest.engine import run_strategy
    from backtest.metrics import ann_return, max_drawdown, sharpe, turnover

    out = {}
    for wname, win in WINDOWS_REPORT.items():
        s = _slice(sig, win)
        if len(s) < 60:
            continue
        pos = hold_position(s * direction, k)
        u = und.reindex(pos.index)
        strat = run_strategy(pos, u, cost_bps, carry.reindex(pos.index))
        ret = strat["ret"].dropna()
        out[wname] = {"net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
                      "maxdd": max_drawdown(ret), "turnover": turnover(pos),
                      "n_obs": int(len(ret))}
    return out


def build_diagnostics(levels: dict[str, pd.Series], signals: dict[tuple, pd.Series],
                      ew: pd.Series, breadth: pd.DataFrame,
                      common_sel: pd.DatetimeIndex, winner: tuple) -> pd.DataFrame:
    """②(b) 要求的诊断：corr(分歧度, 广度) 第一次有数字；外加 corr(分歧度, 生产信号)。"""
    rows = []
    b_cols = ["pct_above_ma20", "pct_above_ma60", "hi_lo_diff20", "hi_lo_diff60"]

    def _pair(name_a, a, name_b, b, kind):
        j = pd.concat([a.rename("a"), b.rename("b")], axis=1, join="inner").dropna()
        j = j[j.index.isin(common_sel)]
        if len(j) < 30:
            return
        rows.append({"kind": kind, "left": name_a, "right": name_b,
                     "pearson": float(j["a"].corr(j["b"])),
                     "spearman": float(j["a"].corr(j["b"], method="spearman")),
                     "n_obs": int(len(j))})

    for fam, lvl in levels.items():
        for c in b_cols:
            _pair(fam, lvl, f"breadth_{c}", breadth[c], "level_vs_breadth")
        _pair(fam, lvl, "equal_weight_factor", ew, "level_vs_production")
        _pair(fam, lvl, "abs_equal_weight_factor", ew.abs(), "level_vs_abs_production")

    # 赢家变体口径下的同面判定（同 lb/zw 信号化后再比，最锋利的一版）
    fam, lb, zw, k = winner
    wsig = signals[winner]
    for c in b_cols:
        _pair(f"signal_{fam}_lb{lb}zw{zw}", wsig,
              f"signal_breadth_{c}_lb{lb}zw{zw}",
              warmup_masked_level_signal(breadth[c].dropna(), lb, zw),
              "winner_signal_vs_breadth_signal")
    _pair(f"signal_{fam}_lb{lb}zw{zw}", wsig, "equal_weight_factor", ew,
          "winner_signal_vs_production")
    return pd.DataFrame(rows)


def evaluate_gates(winner: tuple, panel: pd.DataFrame, res_ic, res_pic,
                   pic_signed: float, p_pic_at_winner: float,
                   sharpe_by_win: dict) -> pd.DataFrame:
    """②(f) 四关逐条判定（关1 / 关1b / 关2 / 关3），任一不过 → STOP。

    `pass=None` 的行是**对照量**（`p_naive`），按纪律不得进任何闸门，只作"选择效应有多大"的证据。
    """
    fam, lb, zw, k = winner
    row = panel[(panel["family"] == fam) & (panel["lb"] == lb) & (panel["zw"] == zw)
                & (panel["k"] == k) & (panel["kou_jing"] == PRIMARY_KOU_JING)].iloc[0]
    ic_sel = float(row["ic_selection_2014_2023"])
    ic_tr = float(row["ic_train_2014_2020"])
    ic_val = float(row["ic_val_2021_2023"])
    direction = float(np.sign(ic_sel))

    g1 = bool(np.isfinite(res_ic.p_selected) and res_ic.p_selected < GATE_ALPHA)
    same_sign = bool(pic_signed != 0.0 and np.isfinite(pic_signed)
                     and np.sign(pic_signed) == direction)
    g1b_p = bool(np.isfinite(p_pic_at_winner) and p_pic_at_winner < GATE_ALPHA)
    g2_tr = bool(direction != 0 and np.sign(ic_tr) == direction)
    g2_val = bool(direction != 0 and np.sign(ic_val) == direction)
    sh_sel = float(sharpe_by_win.get("selection_2014_2023", {}).get("net_sharpe", np.nan))
    g3 = bool(np.isfinite(sh_sel) and sh_sel > 0)
    overall = bool(g1 and same_sign and g1b_p and g2_tr and g2_val and g3)

    rows = [
        {"gate": "1_permutation_significant", "metric": "p_selected",
         "value": res_ic.p_selected, "threshold": GATE_ALPHA, "pass": g1},
        {"gate": "1_permutation_significant", "metric": "p_naive_UNCORRECTED(对照·不入闸)",
         "value": res_ic.p_naive, "threshold": float("nan"), "pass": None},
        {"gate": "1b_partial_ic_same_sign", "metric": "partial_ic_signed_at_winner",
         "value": pic_signed, "threshold": 0.0, "pass": same_sign},
        {"gate": "1b_partial_ic_same_sign", "metric": "p_selected_at_winner",
         "value": p_pic_at_winner, "threshold": GATE_ALPHA, "pass": g1b_p},
        {"gate": "2_stable_halves", "metric": "ic_train_2014_2020",
         "value": ic_tr, "threshold": 0.0, "pass": g2_tr},
        {"gate": "2_stable_halves", "metric": "ic_val_2021_2023",
         "value": ic_val, "threshold": 0.0, "pass": g2_val},
        {"gate": "3_net_positive", "metric": "net_sharpe_selection_2014_2023",
         "value": sh_sel, "threshold": 0.0, "pass": g3},
        {"gate": "OVERALL", "metric": "GO(四关全过)" if overall else "STOP(任一关不过)",
         "value": float(overall), "threshold": float("nan"), "pass": overall},
    ]
    return pd.DataFrame(rows)


def build_metadata(summary: dict) -> pd.DataFrame:
    """裁决 CSV 的 META 行：赢家全参数 + 样本 + 溯源锚点（CSV 单看即可复述结论）。"""
    w = summary["winner_variant"]
    run = summary["run"]
    g1, g1b = summary["gate1_ic_machine"], summary["gate1b_partial_ic_machine"]
    items = {
        "verdict": summary["verdict"],
        "winner_family": w["family"], "winner_lb": w["lb"], "winner_zw": w["zw"],
        "winner_k": w["k"],
        "winner_abs_ic": g1["observed_best"],
        "winner_ic_signed_selection": summary["winner_ic_by_window"]["selection_2014_2023"],
        "p_selected_ic": g1["p_selected"],
        "p_naive_ic_UNCORRECTED": g1["p_naive_UNCORRECTED"],
        "p_min_p_ic": g1["p_min_p"],
        "selection_inflation_ic": g1["selection_inflation"],
        "partial_ic_signed_at_winner": g1b["partial_ic_signed_at_gate1_winner"],
        "p_selected_partial_at_winner": g1b["p_selected_at_gate1_winner"],
        "p_selected_partial_own_winner": g1b["p_selected"],
        "partial_machine_own_winner": g1b["best_variant"],
        "null_selected_p95_ic": summary["null_selected_quantiles"]["ic"]["p95"],
        "n_distinct_null_winners_ic": g1["n_distinct_null_winners"],
        **{f"winner_ic_{k}": v for k, v in summary["winner_ic_by_window"].items()},
        **{f"winner_net_sharpe_{k}": v["net_sharpe"]
           for k, v in summary["winner_net_by_window"].items()},
        **{k: run[k] for k in (
            "n_perm", "seed", "cost_bps", "n_obs_selection", "sample_first_date",
            "sample_last_date", "min_shift", "max_shift", "n_variants",
            "max_abs_diff_rebuilt_ew_vs_production_csv",
            "vectorized_vs_house_ic_max_abs_diff", "db_host", "holdout_policy")},
    }
    return pd.DataFrame([{"gate": "META", "metric": k, "value": v,
                          "threshold": float("nan"), "pass": None}
                         for k, v in items.items()])


def run_probe(n_perm: int = 1000, seed: int = 0, cost_bps: float = 3.0, db=None) -> dict:
    """端到端：造三族 → 48 点合成一次机器（×2 统计量）→ 四关判定 → 产物字典。"""
    from backtest.data import load_carry, load_underlying_returns

    pairs, _rebuilt_ew, max_diff_vs_prod = load_pair_signals(db=db)
    levels = divergence_levels(pairs)
    signals = build_variant_signals(levels)
    ew = _load_ew_signal()
    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    carry = load_carry(PRIMARY_KOU_JING, db=db)
    breadth = load_breadth()

    # 全变体共用一条时间索引（机器硬要求）；zw=250 的暖机决定起点
    common = None
    for v in variant_grid():
        idx = signals[v].dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.intersection(und[PRIMARY_KOU_JING].index).intersection(ew.dropna().index)
    common = common.sort_values()
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]

    panel = build_ic_panel(signals, {k: v.reindex(common).dropna() for k, v in und.items()},
                           common)
    frames = build_score_frames(und[PRIMARY_KOU_JING], ew, common_sel)
    res_ic, res_pic = run_machines(signals, frames, common_sel, n_perm, seed)

    winner = res_ic.best_variant
    j_win = res_ic.best_index
    # 运行期口径校验：向量化统计量 == 房规 nonoverlap_ic 的 |·|（selection 窗、blend）
    panel_abs = (panel[(panel["kou_jing"] == PRIMARY_KOU_JING)]
                 .set_index(["family", "lb", "zw", "k"])["ic_selection_2014_2023"].abs())
    obs_check = float(np.nanmax(np.abs(
        np.array([panel_abs.loc[v] for v in variant_grid()]) - res_ic.observed)))
    if not (obs_check < 1e-9):
        raise AssertionError(f"向量化 IC 与房规 nonoverlap_ic 不一致：max|diff|={obs_check:.3e}")

    # 关1b：赢家的**有符号**偏 IC（向量化口径只给 |·|）+ 在关1b 零分布上的保守 p
    sf = frames[winner[3]]
    x = signals[winner].reindex(common_sel).to_numpy(dtype=float)[None, sf.pts]
    rx = rankdata(x, axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    beta = (rx @ sf.ctrl_rank_c) / sf.ctrl_ss
    rs = rx - beta[:, None] * sf.ctrl_rank_c[None, :]
    den = np.sqrt((rs * rs).sum(axis=1) * sf.fwd_resid_ss)
    pic_signed = float((rs @ sf.fwd_resid)[0] / den[0]) if den[0] >= 1e-9 else 0.0
    ic_sel_signed = float(panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                                & (panel["family"] == winner[0]) & (panel["lb"] == winner[1])
                                & (panel["zw"] == winner[2]) & (panel["k"] == winner[3])
                                ]["ic_selection_2014_2023"].iloc[0])
    same_sign = bool(np.sign(pic_signed) == np.sign(ic_sel_signed) and pic_signed != 0.0)
    p_pic_at_winner = p_at_threshold(res_pic.null_selected, res_pic.observed[j_win])

    direction = float(np.sign(ic_sel_signed))
    sharpe_by_win = net_sharpe_by_window(
        signals[winner].reindex(common).dropna(), direction, winner[3],
        und[PRIMARY_KOU_JING], carry, cost_bps)

    verdict = evaluate_gates(winner, panel, res_ic, res_pic, pic_signed,
                             p_pic_at_winner, sharpe_by_win)
    overall = bool(verdict[verdict["gate"] == "OVERALL"]["pass"].iloc[0])

    diagnostics = build_diagnostics(levels, signals, ew, breadth, common_sel, winner)

    perm_frame = pd.concat([
        res_ic.to_frame().assign(machine="abs_nonoverlap_ic"),
        res_pic.to_frame().assign(machine="abs_partial_ic_vs_equal_weight"),
    ], ignore_index=True)

    summary = {
        "PROBE": "候选 ② 四对分歧度/离散度（预登记 48 点，合成一次调用）",
        "verdict": "GO" if overall else "STOP",
        "winner_variant": {"family": winner[0], "lb": winner[1], "zw": winner[2],
                           "k": winner[3]},
        "gate1_ic_machine": res_ic.summary(),
        "gate1b_partial_ic_machine": {
            **res_pic.summary(),
            "observed_at_gate1_winner": float(res_pic.observed[j_win]),
            "p_selected_at_gate1_winner": p_pic_at_winner,
            "partial_ic_signed_at_gate1_winner": pic_signed,
            "same_sign_as_ic": same_sign,
        },
        "null_selected_quantiles": {
            name: {f"p{q}": float(np.percentile(r.null_selected[np.isfinite(r.null_selected)], q))
                   for q in (5, 25, 50, 75, 95, 99)}
            for name, r in (("ic", res_ic), ("partial_ic", res_pic))
        },
        "winner_ic_by_window": {w: float(panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                                               & (panel["family"] == winner[0])
                                               & (panel["lb"] == winner[1])
                                               & (panel["zw"] == winner[2])
                                               & (panel["k"] == winner[3])][f"ic_{w}"].iloc[0])
                               for w in WINDOWS_REPORT},
        "winner_net_by_window": sharpe_by_win,
        "run": {
            "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
            "n_obs_selection": len(common_sel),
            "selection_window": list(SELECTION),
            "sample_first_date": str(common_sel[0].date()),
            "sample_last_date": str(common_sel[-1].date()),
            "common_full_first": str(common[0].date()), "common_full_last": str(common[-1].date()),
            "min_shift": 2 * max(GRID_K), "max_shift": len(common_sel) - 2 * max(GRID_K),
            "n_variants": len(variant_grid()),
            "pair_lookback": PAIR_LOOKBACK, "pair_z_window": PAIR_Z_WINDOW,
            "max_abs_diff_rebuilt_ew_vs_production_csv": max_diff_vs_prod,
            "vectorized_vs_house_ic_max_abs_diff": obs_check,
            "db_host": (db or {}).get("host", "settings.yaml default"),
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门（勘误 §3 / 议程 §7.3）",
        },
    }
    return {"panel": panel, "permutation": perm_frame, "diagnostics": diagnostics,
            "verdict": verdict, "summary": summary, "res_ic": res_ic, "res_pic": res_pic,
            "levels": levels, "winner": winner}


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="② 四对分歧度/离散度探针（48 点合成一次调用）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--db-host", default=None, help="覆盖库主机（主库拒连时走只读备端）")
    args = ap.parse_args()

    db = None
    if args.db_host:
        from signals.common.config import load_db_config
        db = dict(load_db_config())
        db["host"] = args.db_host

    out = run_probe(n_perm=args.n_perm, seed=args.seed, cost_bps=args.cost_bps, db=db)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out["panel"].round(6).to_csv(out_dir / "probe_2_divergence_panel.csv", index=False)
    out["permutation"].round(6).to_csv(out_dir / "probe_2_divergence_permutation.csv",
                                       index=False)
    out["diagnostics"].round(6).to_csv(out_dir / "probe_2_divergence_diagnostics.csv",
                                       index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        out_dir / "probe_2_divergence_verdict.csv", index=False)
    side = out_dir / "probe_2_divergence_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    fam, lb, zw, k = out["winner"]
    blend = out["panel"][out["panel"]["kou_jing"] == PRIMARY_KOU_JING].copy()
    blend["abs_ic_sel"] = blend["ic_selection_2014_2023"].abs()
    top = blend.sort_values("abs_ic_sel", ascending=False).head(10)
    print("=== 选择窗（2014-2023 · blend）|IC| 前 10（48 点全表见 CSV） ===")
    print(top[["family", "lb", "zw", "k", "ic_selection_2014_2023",
               "ic_train_2014_2020", "ic_val_2021_2023", "ic_holdout_2024_2026",
               "n_selection_2014_2023"]].round(4).to_string(index=False))
    print(f"\n=== 赢家：{fam} lb{lb} zw{zw} k{k} ===")
    for key, val in out["summary"]["gate1_ic_machine"].items():
        print(f"  [关1] {key}: {val}")
    for key in ("observed_at_gate1_winner", "p_selected_at_gate1_winner",
                "partial_ic_signed_at_gate1_winner", "same_sign_as_ic",
                "p_selected", "p_naive_UNCORRECTED", "best_variant"):
        print(f"  [关1b] {key}: {out['summary']['gate1b_partial_ic_machine'][key]}")
    print("\n=== 四关裁决 ===")
    print(out["verdict"].round(6).to_string(index=False))
    print("\n=== 赢家逐窗净表现（3bps + blend carry） ===")
    print(pd.DataFrame(out["summary"]["winner_net_by_window"]).T.round(4).to_string())
    print("\n=== 诊断：分歧度 vs 广度（②(b) 要求的第一次实测） ===")
    dg = out["diagnostics"]
    print(dg[dg["kind"].isin(["level_vs_breadth", "winner_signal_vs_breadth_signal"])]
          [["kind", "left", "right", "pearson", "spearman", "n_obs"]].round(3).to_string(index=False))
    verdict = out["summary"]["verdict"]
    print(f"\n{'★ GO：四关全过' if verdict == 'GO' else '✗ STOP：任一关不过 → 归档'}")
    print(f"→ {out_dir}/probe_2_divergence_{{panel,permutation,diagnostics,verdict}}.csv")
    print(f"→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
