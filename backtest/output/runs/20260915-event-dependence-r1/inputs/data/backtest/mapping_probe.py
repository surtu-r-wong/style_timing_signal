"""①b 探针：持仓映射 32 点网格（θ_L × h × w_S），**合成单次**选择校正置换调用。

预登记出处：`docs/plans/2026-08-12-signal-research-agenda.md` §3 ①(f) 第 1 步——
> **①b GO 判据（三条全中才动部署）**：① worst(train, val) Sharpe ≥ 现役 **+0.10**；
> ② 换手不高于现役；③ MaxDD 不劣于 −16.7%。**任一不中 → 维持 θ=0 long-flat 并归档**
> （"映射维度已被检验，现役是稳健最优"）。2024-26 与集中度仅报告。

**双前置（①(f) 明列，缺一不可）**：
1. **C1 机器债** = 候选 ⓪ `backtest/selection_permutation.py`（commit `2d716db`）**已付清**；
2. **用户放行** = 议程 §7.1 问 2「**放行，排在 ⓪ 之后**」**已获得**。
   放行的同时用户重申「**未付机器债前只出报告、不得动部署**」的硬约束不松动（§7.3 第 3 条）
   —— 机器债已付，故本探针**可以**给出部署建议**草案**，但**部署变更须由用户另行拍板**：
   本模块不写 `deploy/`、不改 `production.py`、不改任何生产文件。

## 与「勿重开第 3 条」的关系（原文引用，**论证不是授权**）

retrospective §3 不变结论 3 原文：
> 「**production 口径 = long-flat**（Sharpe 1.42 / MaxDD −13.9% / 换手 10.8）；
> **加空头腿在任何已测形态下都是拖累**。」

本网格的 `w_S ∈ {−0.25, −0.5, −1}` 就是"加空头腿"。议程 ①(b) 给的三条"值得测一次"的
理由（已测形态只有两点 / 勘误已降级为"无**显著独立**价值" / 不引入新空头信号族）
**是提案方的论证，不是授权**；授权来自 §7.1 问 2 的用户放行，且**只放行"测一次"**。

## 机器接入四规矩（`2026-08-12-selection-permutation-machine.md` §6 + 上两批 QA）

1. **32 点合成同一次 `selection_permutation_test` 调用**（不分族/分轴多次调用，否则
   "多次独立 PASS 机会"未被校正）；闸门只读 `p_selected`，`p_naive` 只作对照列；
2. **变体 = 已算好的仓位序列**：32 种映射作用在**同一条现役因子**上先得到 32 条仓位序列，
   置换（循环移位）作用在"仓位 × 收益"的错配上。移位**精确保持换手**
   （成本项随仓位一起移，绕接点外逐日成本结构原样保留）→ 成本后净 Sharpe 不失真。
   **不得**"先移因子再重跑滞回映射"——那样换手会随移位改变，成本项失真；
3. `-inf` 只给**无法评分**的行；GO 判据里的换手 / MaxDD 两条是**独立后置闸门**，
   **不编进统计量**（机器文档 §9 第 11 条边界：塞进去会双记且偏移 p 的含义）；
4. **移位区间**：本统计量**无前瞻窗**（k 概念不存在），故不照抄 IC 类的 `[2k, n−2k]`；
   下界改取**仓位序列的去相关长度**——见 `choose_min_shift` 的规则与保守性论证。

## 口径

- 因子：committed `output/equal_weight/equal_weight_signal_20d40z.csv` 的 `factor_value`
  （现役主信号，**全程不动**——①b 是映射维度命题，不是替换命题）；
- 收益 / carry / 引擎：`backtest.data` + `backtest.engine.run_strategy`（3 bps/边 + blend carry，
  T+1 生效、全日历），与 ①a / ② **同秤**；
- 选择端只用 **train(2014-2020) + val(2021-2023)**；**2024-2026 只报告不选择、不进闸门**。

CLI: python3 -m backtest.mapping_probe [--n-perm 1000] [--seed 0] [--db-host 100.75.102.44]
产出: backtest/output/probe_1b_mapping_{panel,permutation,verdict}.csv
      + probe_1b_mapping_summary.json（sidecar）

本模块**不改动** `positions.py` / `engine.py` / `selection_permutation.py` /
任何既有探针 / 任何冻结根，只读 committed 信号 CSV 与只读库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.engine import ANN  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

# ---------------------------------------------------------------- 预登记常量（不得扩）
THETA_L = (0.0, 0.1, 0.2, 0.3)          # 4 点：0 = 现役；其余为仓内既有 0.1 步长阈值梯
H_BAND = (0.0, 0.1)                     # 2 点：0 = 无滞回（退化为无记忆映射）；0.1 = 仓内既有带宽
W_SHORT = (0.0, -0.25, -0.5, -1.0)      # 4 点：0 = 现役 long-flat；三个负值为议程 ①(b) 原文
N_VARIANTS = len(THETA_L) * len(H_BAND) * len(W_SHORT)   # = 32
INCUMBENT = (0.0, 0.0, 0.0)             # 现役 = production_position(θ=0)，网格内的**对照点**
SYMMETRIC = (0.0, 0.0, -1.0)            # ①a 的对称口径，网格内的第二个已知点

SIGNAL_CSV = "output/equal_weight/equal_weight_signal_20d40z.csv"
SIGNAL_COL = "factor_value"
PRIMARY_KOU_JING = "blend"
KOU_JING_REPORT = ("500", "1000", "blend")

SELECTION = ("2014-01-01", "2023-12-31")
TRAIN = ("2014-01-01", "2020-12-31")
VAL = ("2021-01-01", "2023-12-31")
HOLDOUT = ("2024-01-01", "2026-12-31")
WINDOWS_REPORT = {
    "selection_2014_2023": SELECTION,
    "train_2014_2020": TRAIN,
    "val_2021_2023": VAL,
    "holdout_2024_2026": HOLDOUT,
    "full_2014_2026": (None, None),
}
STAT_WINDOWS = ("train_2014_2020", "val_2021_2023")   # 统计量 = 这两窗净 Sharpe 的 worst

# GO 判据阈值（①(f) 原文，**不得改**）
GATE1_MARGIN = 0.10        # worst(train,val) Sharpe ≥ 现役 + 0.10
GATE3_MAXDD = -0.167       # MaxDD 不劣于 −16.7%

# 移位下界的**选取规则**（一次选定，见 `choose_min_shift`）：结构性候选梯 + 白噪声带
SHIFT_LADDER = (20, 40, 60, 120, 250)   # 现役因子 lookback=20 / z_window=40 的结构性倍数
ACF_MAX_LAG = 400                       # 诊断用的 ACF 观察长度


# ---------------------------------------------------------------- 纯函数：映射
def _sticky(enter: np.ndarray, reset: np.ndarray) -> np.ndarray:
    """粘滞状态：`enter` 处置 1，`reset` 处置 0，两者皆非处**沿用前值**（起始为 0）。

    向量化写法 = "最近一次事件前向填充"。与逐日 for 循环状态机逐位等价（tests 有判例）。
    """
    marker = np.where(enter, 1.0, np.where(reset, 0.0, np.nan))
    idx = np.where(~np.isnan(marker), np.arange(len(marker)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = marker[idx]
    out[np.isnan(out)] = 0.0        # 首个事件之前一律 flat
    return out


def hysteresis_position(signal: pd.Series, theta_l: float, h: float,
                        w_short: float) -> pd.Series:
    """滞回映射：**进场 ±(θ_L+h)、出场 ±θ_L**，多头权重 +1、空头权重 `w_short`。

    状态机（θ_L ≥ 0、h ≥ 0）：
    - `s > θ_L + h` → 多头；`s < −(θ_L + h)` → 空头；
    - 多头**保持**到 `s ≤ θ_L`；空头**保持**到 `s ≥ −θ_L`；
    - 其余 → 空仓。

    这一参数化的两个理由：
    ① **h=0 时精确退化**为无记忆映射 `positions.to_position_asym(s, θ_L, θ_L)`
       （w_short=−1 时逐位相同；w_short=0 时即 `production_position(θ_L)`）——
       故 (θ_L=0, h=0, w_S=0) **就是现役**，(0, 0, −1) 就是 ①a 的对称口径，两个已知点都在网格里；
    ② **无歧义**：四条水位天然有序 `−(θ_L+h) ≤ −θ_L ≤ θ_L ≤ θ_L+h`，多空的"保持区"
       （`s>θ_L` 与 `s<−θ_L`）互斥 → 不存在"多头未出场而空头已进场"的冲突分支。
       （另一种写法"进场 ±θ_L、出场 ±(θ_L−h)"在 |s| 落 (θ_L−h, θ_L) 时正是这种冲突。）
    """
    if theta_l < 0 or h < 0:
        raise ValueError("theta_l / h 必须非负")
    s = signal.to_numpy(dtype=float)
    enter = theta_l + h
    long_state = _sticky(s > enter, s <= theta_l)
    short_state = _sticky(s < -enter, s >= -theta_l)
    if np.any((long_state > 0) & (short_state > 0)):   # 参数化保证互斥，越界即实现有误
        raise AssertionError("多空状态同时为真——滞回状态机实现有误")
    return pd.Series(long_state + w_short * short_state, index=signal.index, dtype=float)


def variant_grid() -> list[tuple[float, float, float]]:
    """预登记 32 点（θ_L × h × w_S），**顺序固定**（判例与产物可对表）。"""
    return [(t, h, w) for t in THETA_L for h in H_BAND for w in W_SHORT]


def build_variant_positions(signal: pd.Series) -> dict[tuple, pd.Series]:
    """32 条仓位序列（在**完整信号历史**上跑状态机，之后再切窗，避免状态暖机被截断）。"""
    return {v: hysteresis_position(signal, *v) for v in variant_grid()}


# ---------------------------------------------------------------- 纯函数：向量化引擎
def net_return_matrix(pos: np.ndarray, und: np.ndarray, carry: np.ndarray,
                      cost_bps: float) -> np.ndarray:
    """`engine.run_strategy` 的逐行向量化等价物（tests + 运行期断言双重钉死）。

    `pos` (m, n) 为**目标仓位**矩阵，`und` / `carry` (n,) 按日历不动。
    口径逐行照抄引擎：`pos_eff = shift(1).fillna(0)`；`trade=|Δpos_eff|` 且
    `trade[0]=|pos_eff[0]|`（=0）；`cost=bps/1e4·trade`；`carry_ret=pos_eff·carry/245`。
    """
    pos = np.asarray(pos, dtype=float)
    pos_eff = np.zeros_like(pos)
    pos_eff[:, 1:] = pos[:, :-1]
    trade = np.zeros_like(pos)
    trade[:, 1:] = np.abs(np.diff(pos_eff, axis=1))
    trade[:, 0] = np.abs(pos_eff[:, 0])
    return pos_eff * und[None, :] - cost_bps / 1e4 * trade + pos_eff * carry[None, :] / ANN


def sharpe_rows(ret: np.ndarray) -> np.ndarray:
    """逐行 Sharpe（与 `metrics.sharpe` 同口径：ddof=1、×√245、std 非有限或 0 → 0.0）。"""
    sd = ret.std(axis=1, ddof=1)
    mu = ret.mean(axis=1)
    out = np.zeros_like(mu)
    ok = np.isfinite(sd) & (sd != 0)
    out[ok] = mu[ok] / sd[ok] * np.sqrt(ANN)
    return out


def autocorr(x: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """样本自相关（去均值、除以 lag0 方差；分母用全样本，标准 biased 估计）。"""
    x = np.asarray(x, dtype=float)
    xc = x - x.mean()
    denom = float(xc @ xc)
    if denom <= 0:
        return np.zeros(len(lags))
    return np.array([float(xc[k:] @ xc[:-k]) / denom if k else 1.0 for k in lags])


def choose_min_shift(series: dict[tuple, np.ndarray],
                     ladder: tuple[int, ...] = SHIFT_LADDER) -> tuple[int, pd.DataFrame, dict]:
    """移位下界 = 梯子上**第一个**让全部 32 条仓位序列的 |ACF| 都落进白噪声带的滞后。

    **规则（一次选定，先于任何统计量结果）**：
    - 候选梯 `SHIFT_LADDER` 是**结构量**：现役因子 lookback=20 / z_window=40 的倍数
      （20 / 40 / 60 / 120 / 250），不是按结果挑的自由参数；
    - 判据 = 白噪声带 `2/√n`（n = 选择窗长度）——即"该滞后上的残留自相关已不可与 0 区分"；
    - 取 **32 变体的最大 |ACF|**（最保守的一条），第一个达标的梯级即下界。

    **为什么不照抄 IC 类的 [2k, n−2k]**（机器文档 §6 房规）：那条规矩防的是"移位后前瞻窗
    与原窗仍重叠"的**机械残留配对**；本统计量把 `pos_eff[t]` 与 `und[t]` **同日**相乘，
    没有前瞻窗，k 概念不存在 → 只剩**统计性**残留（仓位序列自身的自相关）。

    **残留的方向是保守的**：H0 下循环移位与观测行精确可交换（p 精确校准，与残留无关）；
    H1 下残留只会让置换行**保留一部分真效应** → 零分布上移 → `p_selected` 偏大。
    故本探针即使残留未清干净，也只会**低估**显著性，不会造假阳。

    返回 `(min_shift, 逐变体诊断表, 规则说明)`。
    """
    n = min(len(a) for a in series.values())
    band = 2.0 / np.sqrt(n)
    lags = np.arange(0, min(n - 1, ACF_MAX_LAG))
    acfs = {v: np.abs(autocorr(a, lags)) for v, a in series.items()}
    env = np.vstack([acfs[v] for v in series])          # (J, n_lag) 各滞后的 |ACF| 包络
    pick = next((int(lag) for lag in ladder
                 if lag < len(lags) and env[:, lag].max() < band), None)
    if pick is None:
        raise AssertionError(f"梯子 {ladder} 上无一滞后使全部变体 |ACF| < {band:.4f}——"
                             "须停下重新定移位下界（不得就地放宽）")
    rows = [{"variant": str(v), **{f"acf_lag{lag}": float(acfs[v][lag])
                                   for lag in ladder if lag < len(lags)},
             f"max_abs_acf_beyond_lag{pick}": float(acfs[v][pick:].max()),
             f"mean_abs_acf_beyond_lag{pick}": float(acfs[v][pick:].mean())}
            for v in series]
    rule = {"n_obs": n, "white_noise_band_2_over_sqrt_n": float(band),
            "ladder": list(ladder), "chosen_min_shift": pick,
            "max_abs_acf_at_chosen_lag_across_variants": float(env[:, pick].max()),
            "max_abs_acf_beyond_chosen_lag_across_variants": float(env[:, pick:].max()),
            "mean_abs_acf_beyond_chosen_lag_across_variants": float(env[:, pick:].mean()),
            "rule": "梯子上第一个使 32 变体 |ACF| 全部 < 2/√n 的滞后；残留方向保守（见 docstring）"}
    return pick, pd.DataFrame(rows), rule


# ---------------------------------------------------------------- 数据装载
def load_signal() -> pd.Series:
    df = pd.read_csv(ROOT / SIGNAL_CSV, parse_dates=["date"]).set_index("date").sort_index()
    return df[SIGNAL_COL].astype(float)


def _slice(s: pd.Series, win: tuple) -> pd.Series:
    a, b = win
    out = s
    if a is not None:
        out = out[out.index >= pd.Timestamp(a)]
    if b is not None:
        out = out[out.index <= pd.Timestamp(b)]
    return out


def window_masks(index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    out = {}
    for name, (a, b) in WINDOWS_REPORT.items():
        m = np.ones(len(index), dtype=bool)
        if a is not None:
            m &= index >= pd.Timestamp(a)
        if b is not None:
            m &= index <= pd.Timestamp(b)
        out[name] = m
    return out


# ---------------------------------------------------------------- 统计量（机器接口）
def make_batch_scorer(pos_arrays: dict[tuple, np.ndarray], und: np.ndarray,
                      carry: np.ndarray, masks: dict[str, np.ndarray],
                      cost_bps: float):
    """机器的 `batch_stat_fn`：一次算完某变体在全部重抽样行下的 `worst(train,val)` 净 Sharpe。

    **重排的是仓位序列**（收益 / carry 按日历不动，与房规"重排信号侧"同侧），
    循环移位对仓位的作用是整体搬家 → 换手（`Σ|Δpos|`）除绕接点外**逐日保持**。
    """
    def _fn(variant, idx_matrix: np.ndarray) -> np.ndarray:
        pos = pos_arrays[variant][np.asarray(idx_matrix)]
        vals = []
        for w in STAT_WINDOWS:
            m = masks[w]
            vals.append(sharpe_rows(net_return_matrix(pos[:, m], und[m], carry[m], cost_bps)))
        return np.minimum(*vals) if len(vals) == 2 else np.min(vals, axis=0)
    return _fn


# ---------------------------------------------------------------- 面板（房规口径）
def evaluate_variant(pos: pd.Series, und: pd.Series, carry: pd.Series,
                     cost_bps: float) -> dict[str, dict]:
    """逐窗房规指标（切窗后再跑 `run_strategy`，与 `scan.py` / 机器 demo 同做法）。"""
    from backtest.engine import run_strategy
    from backtest.metrics import (
        ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
    )

    out = {}
    for name, win in WINDOWS_REPORT.items():
        p = _slice(pos, win)
        u = _slice(und, win)
        idx = p.index.intersection(u.index)
        if len(idx) < 60:
            continue
        p, u = p.reindex(idx), u.reindex(idx)
        ret = run_strategy(p, u, cost_bps, carry.reindex(idx))["ret"]
        out[name] = {"net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
                     "maxdd": max_drawdown(ret), "calmar": calmar(ret),
                     "turnover": turnover(p), "hit": hit_rate(ret),
                     "long_frac": float((p > 0).mean()), "short_frac": float((p < 0).mean()),
                     "n_obs": int(len(ret))}
    return out


def build_panel(positions: dict[tuple, pd.Series], und: dict[str, pd.Series],
                carry: dict[str, pd.Series], common: pd.DatetimeIndex,
                cost_bps: float) -> pd.DataFrame:
    """32 变体 × 三口径 × 五窗（blend 是主口径；500/1000 为报告列，不进任何闸门）。

    ⚠️ **carry 必须按口径取用**：`carry[kj]` 与 `und[kj]` 配对（500→IC、1000→IM、blend→50/50）。
    早期版本对三口径一律喂 blend carry，导致 500/1000 两口径与 ①a **不同秤**（独立 QA I-1 修）。
    """
    rows = []
    for v in variant_grid():
        pos = positions[v].reindex(common)
        for kj in KOU_JING_REPORT:
            met = evaluate_variant(pos, und[kj], carry[kj], cost_bps)
            row = {"theta_L": v[0], "h": v[1], "w_S": v[2], "kou_jing": kj,
                   "carry_source": kj,
                   "is_incumbent": v == INCUMBENT, "is_symmetric_1a": v == SYMMETRIC}
            for wname, m in met.items():
                for key, val in m.items():
                    row[f"{key}_{wname}"] = val
            row["worst_tv_sharpe"] = min(met["train_2014_2020"]["net_sharpe"],
                                         met["val_2021_2023"]["net_sharpe"])
            rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 闸门
def evaluate_gates(winner_row: pd.Series, incumbent_row: pd.Series,
                   res) -> pd.DataFrame:
    """①(f) 三条判据逐条判定（**任一不中 → STOP**）。

    读法（开跑前钉死，见探针文档 §"判据读法"）：
    - 判据①：`worst(train,val)` 净 Sharpe，赢家 vs 现役**同秤同窗**，阈值 +0.10；
    - 判据②：换手取**选择窗**（2014-2023）年化换手，赢家 ≤ 现役**同窗**值
      （原文"不高于现役"是**比较式**，故取同窗现役实测值而非历史 headline 10.8）；
    - 判据③：MaxDD 取**选择窗**，阈值为原文的**绝对数** −16.7%
      （2024-26 不得进闸门 → 不能按 full 样本判；现役同窗值另列为诊断行）。
    `pass=None` 的行是**诊断 / 对照量**，不参与 OVERALL。
    """
    w_worst = float(winner_row["worst_tv_sharpe"])
    i_worst = float(incumbent_row["worst_tv_sharpe"])
    w_to = float(winner_row["turnover_selection_2014_2023"])
    i_to = float(incumbent_row["turnover_selection_2014_2023"])
    w_dd = float(winner_row["maxdd_selection_2014_2023"])
    i_dd = float(incumbent_row["maxdd_selection_2014_2023"])

    g1 = bool(w_worst >= i_worst + GATE1_MARGIN)
    g2 = bool(w_to <= i_to)
    g3 = bool(w_dd >= GATE3_MAXDD)
    overall = bool(g1 and g2 and g3)

    rows = [
        {"gate": "1_worst_tv_sharpe", "metric": "winner_worst_tv_sharpe",
         "value": w_worst, "threshold": i_worst + GATE1_MARGIN, "pass": g1},
        {"gate": "1_worst_tv_sharpe", "metric": "incumbent_worst_tv_sharpe(同秤)",
         "value": i_worst, "threshold": float("nan"), "pass": None},
        {"gate": "1_worst_tv_sharpe", "metric": "margin_vs_incumbent",
         "value": w_worst - i_worst, "threshold": GATE1_MARGIN, "pass": g1},
        {"gate": "2_turnover", "metric": "winner_turnover_selection",
         "value": w_to, "threshold": i_to, "pass": g2},
        {"gate": "2_turnover", "metric": "incumbent_turnover_selection(同窗)",
         "value": i_to, "threshold": float("nan"), "pass": None},
        {"gate": "3_maxdd", "metric": "winner_maxdd_selection",
         "value": w_dd, "threshold": GATE3_MAXDD, "pass": g3},
        {"gate": "3_maxdd", "metric": "incumbent_maxdd_selection(同窗·诊断)",
         "value": i_dd, "threshold": float("nan"), "pass": None},
        {"gate": "DIAGNOSTIC", "metric": "p_selected(机器主口径·非判据)",
         "value": res.p_selected, "threshold": float("nan"), "pass": None},
        {"gate": "DIAGNOSTIC", "metric": "p_naive_UNCORRECTED(对照·不得入闸)",
         "value": res.p_naive, "threshold": float("nan"), "pass": None},
        {"gate": "OVERALL", "metric": "GO(三条全中)" if overall else "STOP(任一条不中)",
         "value": float(overall), "threshold": float("nan"), "pass": overall},
    ]
    return pd.DataFrame(rows)


def build_metadata(summary: dict) -> pd.DataFrame:
    w, run = summary["winner_variant"], summary["run"]
    mach = summary["machine"]
    items = {
        "verdict": summary["verdict"],
        "winner_theta_L": w["theta_L"], "winner_h": w["h"], "winner_w_S": w["w_S"],
        "winner_is_incumbent": w["is_incumbent"],
        "observed_best_worst_tv": mach["observed_best"],
        "p_selected": mach["p_selected"],
        "p_naive_UNCORRECTED": mach["p_naive_UNCORRECTED"],
        "p_min_p": mach["p_min_p"],
        "selection_inflation": mach["selection_inflation"],
        "null_selected_p95": summary["null_selected_quantiles"]["p95"],
        "n_distinct_null_winners": mach["n_distinct_null_winners"],
        **{f"winner_net_sharpe_{k}": v["net_sharpe"]
           for k, v in summary["winner_by_window"].items()},
        **{f"incumbent_net_sharpe_{k}": v["net_sharpe"]
           for k, v in summary["incumbent_by_window"].items()},
        **{k: run[k] for k in (
            "n_perm", "seed", "cost_bps", "n_obs_selection", "sample_first_date",
            "sample_last_date", "min_shift", "max_shift", "n_variants",
            "vectorized_vs_house_max_abs_diff",
            "db_host", "carry_policy", "holdout_policy", "deploy_policy")},
        "shift_rule_max_abs_acf_at_chosen_lag":
            run["shift_rule"]["max_abs_acf_at_chosen_lag_across_variants"],
    }
    return pd.DataFrame([{"gate": "META", "metric": k, "value": v,
                          "threshold": float("nan"), "pass": None}
                         for k, v in items.items()])


# ---------------------------------------------------------------- 编排
def run_probe(n_perm: int = 1000, seed: int = 0, cost_bps: float = 3.0, db=None) -> dict:
    from backtest.data import load_carry, load_underlying_returns

    signal = load_signal()
    positions = build_variant_positions(signal)
    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    # carry **按口径各取各的**（500→IC、1000→IM、blend→50/50）；混用会让次级口径与 ①a 不同秤
    carry = {kj: load_carry(kj, db=db) for kj in KOU_JING_REPORT}

    common = signal.dropna().index.intersection(und[PRIMARY_KOU_JING].index).sort_values()
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]

    panel = build_panel(positions, und, carry, common, cost_bps)

    # ---- 机器（32 点合成单次调用，仓位序列为变体）
    pos_sel = {v: positions[v].reindex(common_sel).to_numpy(dtype=float)
               for v in variant_grid()}
    min_shift, acf_frame, shift_rule = choose_min_shift(pos_sel)
    und_sel = und[PRIMARY_KOU_JING].reindex(common_sel).to_numpy(dtype=float)
    carry_sel = carry[PRIMARY_KOU_JING].reindex(common_sel).fillna(0.0).to_numpy(dtype=float)
    masks = window_masks(common_sel)
    n_obs = len(common_sel)
    max_shift = n_obs - min_shift

    scorer = make_batch_scorer(pos_sel, und_sel, carry_sel, masks, cost_bps)
    res = selection_permutation_test(
        variant_grid(), n_obs=n_obs, batch_stat_fn=scorer,
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="worst_tv_net_sharpe(train,val)",
        meta={"n_obs": n_obs, "min_shift": min_shift, "max_shift": max_shift,
              "first_date": str(common_sel[0].date()),
              "last_date": str(common_sel[-1].date())})

    # ---- 运行期口径校验：向量化统计量 == 房规 run_strategy + sharpe（逐变体）
    blend = panel[panel["kou_jing"] == PRIMARY_KOU_JING].set_index(["theta_L", "h", "w_S"])
    house = np.array([float(blend.loc[v, "worst_tv_sharpe"]) for v in variant_grid()])
    max_diff = float(np.nanmax(np.abs(house - res.observed)))
    if not (max_diff < 1e-9):
        raise AssertionError(f"向量化净 Sharpe 与房规引擎不一致：max|diff|={max_diff:.3e}")

    winner = res.best_variant
    winner_row = blend.loc[winner]
    incumbent_row = blend.loc[INCUMBENT]
    verdict = evaluate_gates(winner_row, incumbent_row, res)
    overall = bool(verdict[verdict["gate"] == "OVERALL"]["pass"].iloc[0])

    def _by_window(row) -> dict:
        return {w: {k: float(row[f"{k}_{w}"]) for k in
                    ("net_sharpe", "net_ann", "maxdd", "calmar", "turnover",
                     "hit", "long_frac", "short_frac")}
                for w in WINDOWS_REPORT}

    null_sel = np.asarray(res.null_selected, dtype=float)
    finite = null_sel[np.isfinite(null_sel)]
    summary = {
        "PROBE": "候选 ①b 持仓映射 32 点网格（θ_L × h × w_S，合成单次调用）",
        "verdict": "GO" if overall else "STOP",
        "prerequisites": {
            "C1_machine_debt": "已付清（候选 ⓪ backtest/selection_permutation.py, commit 2d716db）",
            "user_release": "已放行（议程 §7.1 问 2：放行，排在 ⓪ 之后）",
            "deploy_constraint": "本探针不动任何生产文件；GO 亦只出建议草案，部署由用户另行拍板",
        },
        "winner_variant": {"theta_L": winner[0], "h": winner[1], "w_S": winner[2],
                           "is_incumbent": winner == INCUMBENT,
                           "is_symmetric_1a": winner == SYMMETRIC},
        "machine": res.summary(),
        "null_selected_quantiles": {f"p{q}": float(np.percentile(finite, q))
                                    for q in (5, 25, 50, 75, 95, 99)} if finite.size else {},
        "winner_by_window": _by_window(winner_row),
        "incumbent_by_window": _by_window(incumbent_row),
        "gate_readings": {
            "gate1": "worst(train,val) 净 Sharpe，赢家 vs 现役同秤，阈值 +0.10",
            "gate2": "选择窗年化换手，赢家 ≤ 现役同窗（原文'不高于现役'为比较式）",
            "gate3": "选择窗 MaxDD ≥ −16.7%（原文绝对数；holdout 不得进闸门）",
        },
        "run": {
            "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
            "n_obs_selection": n_obs, "selection_window": list(SELECTION),
            "sample_first_date": str(common_sel[0].date()),
            "sample_last_date": str(common_sel[-1].date()),
            "common_full_first": str(common[0].date()),
            "common_full_last": str(common[-1].date()),
            "min_shift": min_shift, "max_shift": max_shift,
            "shift_rule": shift_rule,
            "n_variants": N_VARIANTS,
            "theta_L_grid": list(THETA_L), "h_grid": list(H_BAND), "w_S_grid": list(W_SHORT),
            "vectorized_vs_house_max_abs_diff": max_diff,
            "db_host": (db or {}).get("host", "settings.yaml default"),
            "carry_policy": "每口径各用各的 carry（500→IC、1000→IM、blend→50/50）；"
                            "机器与三条判据只用 blend",
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门（勘误 §3 / 议程 §7.3）",
            "deploy_policy": "不改 deploy/ 与 production.py；GO 只出草案，用户另行拍板",
        },
    }
    return {"panel": panel, "permutation": res.to_frame(), "acf": acf_frame,
            "verdict": verdict, "summary": summary, "res": res, "winner": winner}


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="①b 持仓映射 32 点网格探针（合成单次调用）")
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
    out["panel"].round(6).to_csv(out_dir / "probe_1b_mapping_panel.csv", index=False)
    out["permutation"].round(6).to_csv(out_dir / "probe_1b_mapping_permutation.csv",
                                       index=False)
    out["acf"].round(6).to_csv(out_dir / "probe_1b_mapping_acf.csv", index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        out_dir / "probe_1b_mapping_verdict.csv", index=False)
    side = out_dir / "probe_1b_mapping_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    blend = out["panel"][out["panel"]["kou_jing"] == PRIMARY_KOU_JING].copy()
    cols = ["theta_L", "h", "w_S", "worst_tv_sharpe", "net_sharpe_train_2014_2020",
            "net_sharpe_val_2021_2023", "turnover_selection_2014_2023",
            "maxdd_selection_2014_2023", "is_incumbent"]
    print("=== 32 点全表（blend·按 worst(train,val) 降序） ===")
    print(blend.sort_values("worst_tv_sharpe", ascending=False)[cols]
          .round(4).to_string(index=False))
    w = out["winner"]
    print(f"\n=== 赢家：θ_L={w[0]} h={w[1]} w_S={w[2]}"
          f"{'（= 现役对照点）' if w == INCUMBENT else ''} ===")
    for k, v in out["summary"]["machine"].items():
        print(f"  {k}: {v}")
    print("\n=== 三条判据 ===")
    print(out["verdict"].round(6).to_string(index=False))
    print("\n=== 赢家 vs 现役 逐窗（blend, 3bps + carry） ===")
    cmp = pd.concat([pd.DataFrame(out["summary"]["winner_by_window"]).T.add_prefix("win_"),
                     pd.DataFrame(out["summary"]["incumbent_by_window"]).T.add_prefix("inc_")],
                    axis=1)
    print(cmp[["win_net_sharpe", "inc_net_sharpe", "win_turnover", "inc_turnover",
               "win_maxdd", "inc_maxdd"]].round(4).to_string())
    v = out["summary"]["verdict"]
    print(f"\n{'★ GO：三条全中' if v == 'GO' else '✗ STOP：任一条不中 → 维持 θ=0 long-flat 并归档'}")
    print("⚠️ 本探针不动任何生产文件；部署变更须由用户另行拍板。")
    print(f"→ {out_dir}/probe_1b_mapping_{{panel,permutation,acf,verdict}}.csv\n→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
