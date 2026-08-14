"""④ 探针：条件化有效性（现役 equal_weight 信号 × 四个市场状态的**交互**）。

预登记（**冻结，本模块只对表执行、无权改动**）：
`docs/plans/2026-08-14-probe-4-conditional-prereg.md` §1-§12。
技术前提附录（数据实测 / API 落点 / 基线出处）：
`docs/plans/2026-08-14-probe-4-tech-annex.md`。

## 这条命题不是八轴、不是 B3

八轴当年测的是各变量的**边际增量**（`partial_ic(X | ew)`，全部不显著）；
本探针测的是**交互**——`IC(ew | state=s)` 的**跨状态差异**。零边际增量与非零交互
在数学上不矛盾（预登记首行声明）。不用股票级财务、不做市值排序、
不碰 `b3_eval`/`b3_structure`、不引用 B3 闸门。

## 机器接入三规矩（`2026-08-12-selection-permutation-machine.md` §6，违反即作废）

1. **8 点（4 状态变量 × 2 切法）合成同一次 `selection_permutation_test` 调用**——
   不做 4 次变量级独立调用（否则"4 次独立的 PASS 机会"未被校正）；
2. **统计量不携带任何闸门约束**：进机器的是**无约束** `|跨状态 IC 差|`；
   `-inf` 只可能来自"该行无法评分"（某桶在选择窗内 < `MIN_BUCKET_DAYS` 天，
   或统计量退化为 NaN）。闸① 两窗同号 / 闸③ 早晚期余弦一律**后置独立判**；
3. **`min_shift = 2·k = 40`，`max_shift = n_obs − 40`**（房规 `[2k, n−2k]`）。

## 选择端纪律（预登记 §2 / §4 C2）

选择与全部闸门只用 **train(2014-2020) + val(2021-2023)**；
**2024-2026 只报告不选择**。窗口定义唯一权威 = `backtest.baseline.WINDOWS`
（本模块不写窗口字面量，`SELECTION` 由 train 首日 + val 末日派生）。

## 口径（预登记 §3-§5，逐条硬点）

| 项 | 取值 |
|---|---|
| 信号 | 生产 CSV `output/equal_weight/equal_weight_signal_20d40z.csv` 列 `factor_value`，**不重算因子** |
| 前瞻 | `fusion_probe.forward_return(load_underlying_returns("blend"), k=20)`（t 处 = t+1..t+k） |
| S1 已实现波动 | `load_underlying_returns("blend")` 的 20 日滚动 std（**不年化**，ddof=1） |
| S2 成交额水位 | `backtest/output/market_turnover.csv` 列 `amt_yuan`（**先去趋势**，见下） |
| S3 涨停温度 | `backtest/output/thermometer.csv` 列 `lu_ratio` |
| S4 carry 深浅 | `backtest.data.load_carry("blend")` 原值（正=贴水），自 2015-04 首个有 carry 日起算 |
| 切桶 | 四条状态**一律**先取 250 日滚动分位再切（预登记 §3 "所有滚动分位窗长统一 250 日" + §4 切法以分位 p 定义）；三分 p<1/3 / [1/3,2/3) / ≥2/3，二分 p<0.5 / ≥0.5 |
| PIT | 状态与信号同时点（T 收盘），**不额外 lag**，与 `forward_return` 自洽 |

**S2 必须去趋势**（预登记 §3 明文陷阱）：A 股成交额有强趋势，绝对额直接切桶
≈ 按时间切桶，会把"状态交互"读成"早晚期效应"，恰好污染闸③。250 日滚动分位即去趋势。

**carry 的 NaN 严禁并入"低 carry"桶**（预登记 §3 S4 / D4）：2015-04 前根本没有
carry 值，`load_carry("blend")` 的索引里就没有那些日子 → reindex 后为 NaN →
`bucket_labels` 返回 NaN → 不进任何桶。逐变量单独记 n_obs 与首末日。

**垃圾尾**（附录 §1.1 补充发现 A）：三个 committed 缓存的最后一行 2026-07-01 是
"上游只灌了部分股票"的垃圾日（`n_active=11`、`amt_yuan` 仅中位的 0.5%）。
读取层复用 `dashboard.data.trim_incomplete_tail`（温度计按 `n_active` 判、
成交额按自身判，与 `dashboard/data.py:126,135` 同做法）→ 有效末日统一 2026-06-30。
缓存**不延展**、**禁 `--rebuild-*` CLI**（会覆盖两条已归档轴的产物）。

## 条件 IC 口径（预登记 §5，本探针的技术核心）

对窗口 W、变体 v、桶 s：取全部 `k=20` 个 offset 的非重叠网格
（`nonoverlap_grid(index, 20, offset)`，offset=0..19），每个 offset 在
`grid(o) ∩ bucket(s)` 上算 Spearman rank IC，再按各 cell 样本数 `n_{o,s}`
**加权平均**得 `IC_s`。理由：沿用单 offset 非重叠会让 val 三分后每桶 ~12 点
（⑤b 已被同一问题咬过）；推断走置换（rotation 打断配对，自动吸收重叠相关），
不依赖独立性假设。

- Spearman 内核**零平行实现**：直接调 `fusion_probe.spearman_rows`
  （前瞻收益按日历不动 → 用 `np.broadcast_to` 铺成同形矩阵喂进去）。
  非重叠格点也由 `fusion_probe.nonoverlap_grid` 给出，本模块不自写 `arange(o, n, k)`。
- **cell 级最小样本 `MIN_CELL_POINTS = 3`**：Spearman 在 <3 点上无定义
  （`fusion_probe.rank_ic` 同样在 n<3 时返回 NaN）；这类 cell 不进加权平均
  （权重 0），不是闸门、不产生 `-inf`。
- **桶级最小样本 `MIN_BUCKET_DAYS = 100`**（预登记硬下限）：某变体在**选择窗**内
  任一桶总天数 < 100 → 该变体统计量 `-inf`（规矩 2 允许的唯一 `-inf` 用法）。
- **交互统计量（有符号）**：三分 = `IC_高 − IC_低`（中桶不参与）；二分 = `IC_上 − IC_下`。
  进机器的是 `|有符号值|`（双侧）；闸① 用有符号值后置独立判。

## 四条闸门（预登记 §7；全过才 GO，任一不过 → STOP 归档）

| 闸 | 判据（冻结数值） |
|---|---|
| ① 两窗同号 | 赢家变体的**有符号**统计量在 train 与 val 两窗同号 |
| ② 置换显著 | `p_selected < 0.05`（8 点合成单次调用，无 Bonferroni）；`p_naive` 只入证据段 |
| ③ 早/晚期一致性（**stability 硬闸，失败即刻中止不跑闸④**） | 选择窗按中位交易日对切，赢家各桶条件 IC 向量**去均值后余弦 > 0** |
| ④ 收益层 | 仓位调节版成本后 `worst(train,val)` Sharpe ≥ `GATE4_THRESHOLD = 1.100931` |

**仓位调节器（单点，无自由参数）**：`pos_adj(t) = pos_longflat(t) × w(s_t)`，
`w(低效力桶)=0.5`、`w(其余)=1.0`。"低效力桶" = 赢家有符号统计量指示的 **IC 较低
一侧的极端桶**（三分中桶恒 1.0），由 IC 层同一次选优结果指定，不扫 w。
先 long-flat 映射（`positions.production_position`，θ=0）再乘 `w`。
**状态标签为 NaN 的日子不属于"低效力桶"→ `w=1.0`**（"其余"的字面读法；
这些日子调节版与现役逐日相同）。换手变化只报告不设闸。

## 既有怪癖（预登记 §8，原样保留、不"顺手修"）

- `engine.py:24` `pos_eff = position.shift(1)` 配 `close.pct_change()` = 经济上
  **T 收盘成交**（Batch 12 定性为文档债）；`engine.py:27-28` 首日成本落第二天。
  两者对现役与调节版是**同引擎同日期的非差分偏差**，对差分闸门影响在小数第三位以下
  （`exec_price_probe.py:133` 同款纪律）。
- blend carry 缺腿按 0（2015-04~2022-07 为 IC 单腿 ÷2）、序列止于 2026-04-29 →
  holdout 段最后一年无 carry 输入（对多头略偏悲观）。闸门只读 train/val，不受染；
  holdout 报告段带此标注。
- 生产因子 CSV 前 39 行（z 窗未满）是常数 0 —— 生产口径原样，不裁剪、不 mask。

## 复用清单（零平行实现）

`fusion_probe`: `forward_return / nonoverlap_grid / rank_ic / spearman_rows /
paired_ic_bootstrap`；`selection_permutation.selection_permutation_test`（不动一行）；
`baseline.WINDOWS / baseline._slice`；`engine.run_strategy` + `metrics.sharpe` 等 +
`positions.production_position`；`data.load_underlying_returns / load_carry`；
`dashboard.data.rolling_percentile / trim_incomplete_tail`。
**禁止 import `pair_set_probe`**（配对集合专用）。

CLI: python3 -m backtest.conditional_probe [--n-perm 1000] [--seed 0] [--market-cache DIR]
产出: backtest/output/probe4_conditional_{panel,verdict}.csv

本模块**不改动** `selection_permutation.py` / `fusion_probe.py` / `paired_bootstrap.py` /
`engine.py` / 任何 `b3_*` / 任何冻结根（`signals/ selection/ research/ factors/
stock_selector/data/`）。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS as BASELINE_WINDOWS  # noqa: E402
from backtest.baseline import _slice  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.fusion_probe import (  # noqa: E402
    forward_return, nonoverlap_grid, paired_ic_bootstrap, rank_ic, spearman_rows,
)
from backtest.metrics import ann_return, max_drawdown, sharpe, turnover  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from dashboard.data import rolling_percentile, trim_incomplete_tail  # noqa: E402

#: 同秤清单 re-export：QA 独立复算时从本模块取到的必须是**同一支实现**
#: （⑤b `pair_set_probe_5b.py:81-85` 同款做法）。
SAME_SCALE_FUNCTIONS = (forward_return, nonoverlap_grid, rank_ic, spearman_rows,
                        paired_ic_bootstrap, production_position, run_strategy, sharpe,
                        rolling_percentile, trim_incomplete_tail)

BT_OUT = ROOT / "backtest" / "output"

# ---------------------------------------------------------------- 预登记常量（不得扩）
#: 四个状态变量（预登记 §3；广度**不入**，留在未测登记，守 ≤4 上限）
STATE_VARS = ("S1_realized_vol", "S2_turnover", "S3_limitup", "S4_carry")
#: 两种切法（预登记 §4）
SPLITS = ("tercile", "binary")
N_VARIANTS = len(STATE_VARS) * len(SPLITS)          # = 8
N_BUCKETS = {"tercile": 3, "binary": 2}
BUCKET_NAMES = {"tercile": ("low", "mid", "high"), "binary": ("down", "up")}
#: 三分切点（预登记 §4 切法 A）与二分切点（切法 B）
TERCILE_CUTS = (1.0 / 3.0, 2.0 / 3.0)
BINARY_CUT = 0.5

K_FORWARD = 20                  # 前瞻期固定 k=20（同秤，不扫 k）
IC_OFFSETS = tuple(range(K_FORWARD))   # 全部 20 个 offset 的非重叠网格（预登记 §5）
PCT_WINDOW = 250                # 所有滚动分位窗长统一 250 日（预登记 §3 / D2）
VOL_WINDOW = 20                 # S1：20 日滚动 std，不年化（预登记 D1）
MIN_BUCKET_DAYS = 100           # 每桶最小样本硬下限 → 不足则该变体 -inf（预登记 §5）
MIN_CELL_POINTS = 3             # Spearman 定义域下限（`fusion_probe.rank_ic` n<3 → NaN）

# 窗口：唯一权威 = `backtest.baseline.WINDOWS`（本模块不写窗口字面量）
TRAIN = BASELINE_WINDOWS["2014-2020"]
VAL = BASELINE_WINDOWS["2021-2023"]
HOLDOUT = BASELINE_WINDOWS["2024-2026"]
SELECTION = (TRAIN[0], VAL[1])                       # = ("2014-01-01", "2023-12-31")
WINDOWS_REPORT = {
    "selection_2014_2023": SELECTION,
    "train_2014_2020": TRAIN,
    "val_2021_2023": VAL,
    "holdout_2024_2026": HOLDOUT,
    "full_2014_2026": BASELINE_WINDOWS["full"],
}
STAT_WINDOWS = ("train_2014_2020", "val_2021_2023")  # 闸门只读这两窗

GATE_ALPHA = 0.05                       # 闸②：p_selected < 0.05（无 Bonferroni）
GATE3_COSINE_FLOOR = 0.0                # 闸③：去均值余弦 > 0
#: 闸④ 阈值 = 现役 long-flat worst(train,val) 1.000931 + 0.10（预登记 §7，六位小数）
INCUMBENT_WORST_TV_SHARPE = 1.000931
GATE_WORST_TV_LIFT = 0.10
GATE4_THRESHOLD = 1.100931
#: 仓位调节器：单点、无自由参数（预登记 §7）
W_LOW_EFFICACY = 0.5
W_OTHER = 1.0

COST_BPS = 3.0
PRIMARY_KOU_JING = "blend"
PRODUCTION_CSV = "output/equal_weight/equal_weight_signal_20d40z.csv"
PRODUCTION_COL = "factor_value"
OUTPUT_PREFIX = "probe4_conditional"
N_BOOT_DIAGNOSTIC = 10000       # 诊断列专用（`paired_ic_bootstrap`），**不进闸门**
_MIN_WINDOW_OBS = 60

#: 闸门顺序（预登记 §7 / §11）：闸③ 不过即刻中止，不跑闸④
GATE_ORDER = ("1_two_window_same_sign", "2_permutation_significant",
              "3_early_late_cosine", "4_return_layer")


# ---------------------------------------------------------------- 纯函数：状态变量
def realized_vol(und: pd.Series, window: int = VOL_WINDOW) -> pd.Series:
    """S1：标的日收益的滚动标准差，**不年化**（预登记 D1，单口径不得双报）。

    与收益端同源（`load_underlying_returns("blend")`）、零额外 IO；
    仓库里没有现成的 realized-vol 函数（附录 §1.2 实测），故此处自有 1 行实现。
    """
    return und.astype(float).rolling(int(window), min_periods=int(window)).std()


def bucket_labels(pct: pd.Series, split: str) -> pd.Series:
    """滚动分位 → 桶标签（预登记 §4）。分位 NaN → 标签 NaN → **不进任何桶**。

    - `tercile`：p < 1/3 → 0(low)；1/3 ≤ p < 2/3 → 1(mid)；p ≥ 2/3 → 2(high)
    - `binary`： p < 0.5 → 0(down)；p ≥ 0.5 → 1(up)

    NaN 的两个来源都必须落在桶外：(i) 250 日窗未满的前段；
    (ii) 该状态变量在该日**根本没有值**（carry 2015-04 之前）——
    预登记 §3 S4 明文严禁把无 carry 日并入"低 carry"桶。
    """
    if split not in SPLITS:
        raise ValueError(f"未知 split={split!r}，可选 {SPLITS}")
    p = pct.astype(float)
    out = pd.Series(np.nan, index=p.index, dtype=float)
    ok = p.notna().to_numpy()
    v = p.to_numpy()
    lab = np.full(len(v), np.nan)
    if split == "tercile":
        lo, hi = TERCILE_CUTS
        lab[ok] = np.where(v[ok] < lo, 0.0, np.where(v[ok] < hi, 1.0, 2.0))
    else:
        lab[ok] = np.where(v[ok] < BINARY_CUT, 0.0, 1.0)
    out.iloc[:] = lab
    return out


def variant_grid() -> list[tuple[str, str]]:
    """预登记 8 点（4 状态变量 × 2 切法），**顺序固定**（判例与产物可对表）。"""
    return [(sv, sp) for sv in STATE_VARS for sp in SPLITS]


# ---------------------------------------------------------------- 纯函数：条件 IC
@dataclass(frozen=True)
class BucketCells:
    """某 (变体, 窗口, 桶) 下 20 个 offset 的**固定侧**：格点位置 + 前瞻收益值。

    置换只重排信号侧（房规：收益按日历不动），故这些量在整次运行里是常量。
    `positions` 是相对**该窗口共用索引**的整数位置（0-based）。
    """
    bucket: int
    n_days: int                     # 该桶在该窗口共用索引上的总天数（含所有 offset）
    positions: tuple                # tuple[np.ndarray]，逐 offset 的格点位置
    fwd: tuple                      # tuple[np.ndarray]，逐 offset 对应的前瞻收益


def build_bucket_cells(index: pd.DatetimeIndex, fwd_arr: np.ndarray,
                       label_arr: np.ndarray, split: str,
                       k: int = K_FORWARD) -> list[BucketCells]:
    """按预登记 §5 把窗口切成 (桶 × offset) 的 cell 集合。

    格点由 `fusion_probe.nonoverlap_grid(index, k, offset)` 给出（**不自写 arange**），
    再用 `index.get_indexer` 映回整数位置；`grid(o) ∩ bucket(s)` 即该 cell。
    """
    n_bkt = N_BUCKETS[split]
    grids = []
    for off in range(k):
        dates = nonoverlap_grid(index, k, off)
        grids.append(index.get_indexer(dates))
    out = []
    for b in range(n_bkt):
        in_b = label_arr == float(b)
        pos_list, fwd_list = [], []
        for g in grids:
            sel = g[in_b[g]]
            if len(sel) >= MIN_CELL_POINTS:
                pos_list.append(sel)
                fwd_list.append(fwd_arr[sel])
        out.append(BucketCells(bucket=b, n_days=int(np.count_nonzero(in_b)),
                               positions=tuple(pos_list), fwd=tuple(fwd_list)))
    return out


def conditional_ic_batch(sig_arr: np.ndarray, idx_matrix: np.ndarray,
                         cells: BucketCells) -> np.ndarray:
    """(m,) 条件 IC：逐 offset 的 Spearman 按 cell 样本数加权平均（预登记 §5 主口径）。

    Spearman 内核 = `fusion_probe.spearman_rows`（零平行实现）；前瞻收益按日历不动，
    用 `np.broadcast_to` 铺成同形矩阵喂进去。非有限的 cell IC（全并列 → 分母 0）
    权重记 0——这是"该 cell 无信息"，不是闸门、不产生 `-inf`。
    """
    idx_matrix = np.asarray(idx_matrix)
    m = idx_matrix.shape[0]
    num = np.zeros(m, dtype=float)
    den = np.zeros(m, dtype=float)
    for pos, y in zip(cells.positions, cells.fwd):
        x = sig_arr[idx_matrix[:, pos]]
        ic = spearman_rows(x, np.broadcast_to(y, x.shape))
        ok = np.isfinite(ic)
        w = float(len(pos))
        num += np.where(ok, ic * w, 0.0)
        den += np.where(ok, w, 0.0)
    out = np.full(m, np.nan)
    good = den > 0
    out[good] = num[good] / den[good]
    return out


def signed_statistic(bucket_ic: list[float] | np.ndarray, split: str) -> float:
    """**有符号**交互统计量（预登记 §5，符号约定在此冻结）。

    - `tercile` = `IC_高 − IC_低`（中桶不参与）
    - `binary`  = `IC_上 − IC_下`
    """
    ic = np.asarray(bucket_ic, dtype=float)
    if split == "tercile":
        return float(ic[2] - ic[0])
    if split == "binary":
        return float(ic[1] - ic[0])
    raise ValueError(f"未知 split={split!r}，可选 {SPLITS}")


def signed_statistic_batch(bucket_ic_mat: np.ndarray, split: str) -> np.ndarray:
    """`signed_statistic` 的逐行版（`bucket_ic_mat` 形状 (m, n_buckets)）。"""
    ic = np.asarray(bucket_ic_mat, dtype=float)
    if split == "tercile":
        return ic[:, 2] - ic[:, 0]
    if split == "binary":
        return ic[:, 1] - ic[:, 0]
    raise ValueError(f"未知 split={split!r}，可选 {SPLITS}")


def window_conditional_ic(sig: pd.Series, fwd: pd.Series, labels: pd.Series,
                          split: str, k: int = K_FORWARD) -> dict:
    """某窗口上的逐桶条件 IC + 有符号统计量（观测行口径，供面板与闸①/③ 使用）。

    三条序列须已对齐到同一条索引（调用方负责）。
    """
    index = sig.index
    sig_arr = sig.to_numpy(dtype=float)
    fwd_arr = fwd.to_numpy(dtype=float)
    lab_arr = labels.to_numpy(dtype=float)
    cells = build_bucket_cells(index, fwd_arr, lab_arr, split, k)
    ident = np.arange(len(index), dtype=np.int64)[None, :]
    ics, n_days, n_used, n_cells = [], [], [], []
    for bc in cells:
        ics.append(float(conditional_ic_batch(sig_arr, ident, bc)[0]))
        n_days.append(bc.n_days)
        n_used.append(int(sum(len(p) for p in bc.positions)))
        n_cells.append(len(bc.positions))
    feasible = all(n >= MIN_BUCKET_DAYS for n in n_days) and all(np.isfinite(ics))
    return {"bucket_ic": ics, "bucket_n_days": n_days, "bucket_n_grid": n_used,
            "bucket_n_cells": n_cells, "signed_stat": signed_statistic(ics, split),
            "feasible_min_bucket": bool(all(n >= MIN_BUCKET_DAYS for n in n_days)),
            "feasible": bool(feasible), "n_index": int(len(index))}


def make_batch_stat_fn(sig_arr: np.ndarray, cells_by_variant: dict) -> callable:
    """机器的 `batch_stat_fn`：一次算完某变体在全部重抽样下的 `|有符号统计量|`。

    **规矩 2**：这里是**无约束**的双侧统计量；`-inf` 只可能来自"该变体某桶在选择窗内
    < `MIN_BUCKET_DAYS` 天"或"统计量退化为 NaN"，绝不携带闸①/闸③ 的一致性约束。
    """
    def _fn(variant, idx_matrix: np.ndarray) -> np.ndarray:
        split = variant[1]
        cells = cells_by_variant[variant]
        m = np.asarray(idx_matrix).shape[0]
        if any(bc.n_days < MIN_BUCKET_DAYS for bc in cells):
            return np.full(m, -np.inf)          # 该行无法评分（规矩 2 的唯一合法用法）
        mat = np.column_stack([conditional_ic_batch(sig_arr, idx_matrix, bc)
                               for bc in cells])
        stat = np.abs(signed_statistic_batch(mat, split))
        return np.where(np.isfinite(stat), stat, -np.inf)
    return _fn


# ---------------------------------------------------------------- 纯函数：闸③ 余弦
def demeaned_cosine(u, v) -> float:
    """两条桶-IC 向量**去均值后**的余弦相似度（预登记 §7 闸③ / D8）。

    二分（2 维）时天然退化为"两桶排序是否一致"（±1），预登记明文接受此退化。
    任一向量去均值后为零向量（两桶 IC 相等）→ 方向无定义 → NaN（判为不过）。
    """
    a = np.asarray(u, dtype=float)
    b = np.asarray(v, dtype=float)
    if a.shape != b.shape or a.ndim != 1 or a.size < 2:
        raise ValueError(f"两条向量须同形且至少 2 维，得到 {a.shape} / {b.shape}")
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        return float("nan")
    tol_a = 1e-12 * max(1.0, float(np.abs(a).max()))
    tol_b = 1e-12 * max(1.0, float(np.abs(b).max()))
    a = a - a.mean()
    b = b - b.mean()
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na <= tol_a or nb <= tol_b:
        return float("nan")     # 判不过（"> 0" 对 NaN 与 0.0 同样是 False）
    return float(a @ b / (na * nb))


# ---------------------------------------------------------------- 纯函数：仓位调节器
def low_efficacy_bucket(signed_stat: float, split: str) -> int:
    """由**有符号**统计量指定的"低效力桶"（预登记 §7；三分的中桶恒不入选）。

    - `tercile`：`IC_高 − IC_低 > 0` → 低桶(0) 效力低；< 0 → 高桶(2) 效力低
    - `binary`： `IC_上 − IC_下 > 0` → 下桶(0) 效力低；< 0 → 上桶(1) 效力低

    统计量恰为 0 或 NaN → 无方向可指 → `-1`（无桶降权，调节版 ≡ 现役）。
    """
    if split not in SPLITS:
        raise ValueError(f"未知 split={split!r}，可选 {SPLITS}")
    if not np.isfinite(signed_stat) or signed_stat == 0:
        return -1
    top = N_BUCKETS[split] - 1
    return 0 if signed_stat > 0 else top


def adjusted_position(pos_longflat: pd.Series, labels: pd.Series,
                      low_bucket: int) -> pd.Series:
    """`pos_adj(t) = pos_longflat(t) × w(s_t)`，`w(低效力桶)=0.5`、`w(其余)=1.0`。

    **单点映射、无自由参数**（预登记 §7 / D9）：不扫 w、不另开网格。
    先 long-flat 映射再乘 `w`（调用方负责传入已 long-flat 的仓位）。
    状态标签 NaN 的日子不属于低效力桶 → `w=1.0`（"其余"的字面读法）。
    """
    pos = pos_longflat.astype(float)
    if low_bucket < 0:
        return pos.copy()
    lab = labels.reindex(pos.index).to_numpy(dtype=float)
    w = np.where(lab == float(low_bucket), W_LOW_EFFICACY, W_OTHER)
    return pd.Series(pos.to_numpy(dtype=float) * w, index=pos.index)


def evaluate_windows(pos: pd.Series, und: pd.Series, carry: pd.Series,
                     cost_bps: float = COST_BPS) -> dict[str, dict]:
    """逐窗成本后指标（切窗 → 取交集 → `engine.run_strategy`，与 ①b/⑤/⑤b 同做法）。"""
    out = {}
    for name, win in WINDOWS_REPORT.items():
        p = _slice(pos, win[0], win[1])
        u = _slice(und, win[0], win[1])
        idx = p.index.intersection(u.index)
        if len(idx) < _MIN_WINDOW_OBS:
            continue
        p, u = p.reindex(idx), u.reindex(idx)
        ret = run_strategy(p, u, cost_bps, carry.reindex(idx))["ret"]
        out[name] = {"net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
                     "maxdd": max_drawdown(ret), "turnover": turnover(p),
                     "n_obs": int(len(ret))}
    return out


def worst_tv(metrics: dict[str, dict], field: str = "net_sharpe") -> float:
    """闸④ 的 `worst(train, val)`（只读 `STAT_WINDOWS`，2024-26 不入）。"""
    vals = [metrics[w][field] for w in STAT_WINDOWS if w in metrics]
    return float(min(vals)) if len(vals) == len(STAT_WINDOWS) else float("nan")


# ---------------------------------------------------------------- 数据装载
def load_production_signal() -> pd.Series:
    """生产 equal_weight 因子（20/40/5 锁死），**不重算因子**（预登记 §1）。"""
    df = pd.read_csv(ROOT / PRODUCTION_CSV, parse_dates=["date"]).set_index("date")
    return df.sort_index()[PRODUCTION_COL].astype(float)


def load_turnover_level() -> pd.Series:
    """S2 原值：`market_turnover.csv` 的 `amt_yuan`，尾部垃圾日按自身活跃度剔除。"""
    s = (pd.read_csv(BT_OUT / "market_turnover.csv", parse_dates=["date"])
         .set_index("date")["amt_yuan"].sort_index().astype(float))
    return trim_incomplete_tail(s)


def load_limitup_level() -> pd.Series:
    """S3 原值：`thermometer.csv` 的 `lu_ratio`，尾部垃圾日按 `n_active` 剔除。

    判据列取 `n_active` 而非 `lu_ratio` 本身 —— 与 `dashboard/data.py:126` **逐字同做法**：
    `n_active`（当日入库股票数）才是"上游只灌了部分股票"的直接证据列，
    `lu_ratio` 只是它的后果。两者在 2026-07-01 上恰好给出同一结论，
    但换一个"入库不全却碰巧有涨停"的日子就会分道扬镳 → 按房规取 `n_active`。
    """
    t = (pd.read_csv(BT_OUT / "thermometer.csv", parse_dates=["date"])
         .set_index("date").sort_index())
    t = t.loc[trim_incomplete_tail(t["n_active"].astype(float)).index]
    return t["lu_ratio"].astype(float)


def load_market_series(db=None, cache_dir: str | Path | None = None) -> tuple:
    """标的日收益 + carry（blend 口径）。**PG 只在这里触达，一次性拉下复用**。

    `cache_dir` 给了就先读本地副本、没有再拉库并写副本（Tailscale 黑洞纪律：
    不滚动重试、不反复触库）。副本只用于同一次研究批次内的复算，不进仓库。
    """
    cache_dir = Path(cache_dir) if cache_dir else None
    if cache_dir is not None:
        u_p, c_p = cache_dir / "und_blend.csv", cache_dir / "carry_blend.csv"
        if u_p.exists() and c_p.exists():
            und = (pd.read_csv(u_p, parse_dates=["date"]).set_index("date")
                   .iloc[:, 0].astype(float).sort_index())
            car = (pd.read_csv(c_p, parse_dates=["date"]).set_index("date")
                   .iloc[:, 0].astype(float).sort_index())
            und.index.name = car.index.name = None
            return und, car
    und = load_underlying_returns(PRIMARY_KOU_JING, db=db)
    car = load_carry(PRIMARY_KOU_JING, db=db)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        und.rename_axis("date").to_csv(cache_dir / "und_blend.csv", header=["ret"])
        car.rename_axis("date").to_csv(cache_dir / "carry_blend.csv", header=["carry"])
    return und, car


def build_state_levels(und: pd.Series, carry: pd.Series) -> dict[str, pd.Series]:
    """四条状态**原值**序列（切桶前的水平量），键 = `STATE_VARS`。"""
    return {
        "S1_realized_vol": realized_vol(und).dropna(),
        "S2_turnover": load_turnover_level(),
        "S3_limitup": load_limitup_level().dropna(),
        "S4_carry": carry.dropna(),
    }


def build_state_percentiles(levels: dict[str, pd.Series],
                            window: int = PCT_WINDOW) -> dict[str, pd.Series]:
    """四条状态的 250 日滚动分位（`dashboard.data.rolling_percentile`，窗未满 → NaN）。

    分位在**各变量自己的完整历史**上算（缓存回溯到 1990 → 2014 起点不损失样本；
    S1 受 20 日 std 暖机、S4 受 carry 2015-04 起点各自损失前段，逐变量记账）。
    """
    return {k: rolling_percentile(v.dropna(), window) for k, v in levels.items()}


# ---------------------------------------------------------------- 编排
def build_panel(sig: pd.Series, fwd: pd.Series, labels: dict[str, pd.Series],
                common: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    """8 变体 × 5 窗 × 桶的条件 IC 面板 + 逐 (变体, 窗) 的有符号统计量索引。"""
    rows = []
    stats: dict[tuple, dict] = {}
    for sv, sp in variant_grid():
        lab_full = labels[(sv, sp)]
        for wname, win in WINDOWS_REPORT.items():
            idx = _slice(pd.Series(0.0, index=common), win[0], win[1]).index
            if len(idx) < _MIN_WINDOW_OBS:
                continue
            res = window_conditional_ic(sig.reindex(idx), fwd.reindex(idx),
                                        lab_full.reindex(idx), sp)
            stats[(sv, sp, wname)] = res
            for b in range(N_BUCKETS[sp]):
                rows.append({
                    "state_var": sv, "split": sp,
                    "variant": f"{sv}|{sp}", "window": wname,
                    "bucket": b, "bucket_name": BUCKET_NAMES[sp][b],
                    "cond_ic": res["bucket_ic"][b],
                    "n_days": res["bucket_n_days"][b],
                    "n_grid_points": res["bucket_n_grid"][b],
                    "n_offset_cells": res["bucket_n_cells"][b],
                    "signed_stat": res["signed_stat"],
                    "abs_stat": abs(res["signed_stat"]),
                    "feasible_min_bucket": res["feasible_min_bucket"],
                    "n_window_days": res["n_index"],
                })
    return pd.DataFrame(rows), stats


def run_machine(sig_arr: np.ndarray, fwd_arr: np.ndarray, label_arrs: dict,
                index: pd.DatetimeIndex, n_perm: int, seed: int):
    """⓪ 机器：**8 点合成同一次调用**（规矩 1），rotation，`[2k, n−2k]`（规矩 3）。"""
    variants = variant_grid()
    cells_by_variant = {
        (sv, sp): build_bucket_cells(index, fwd_arr, label_arrs[(sv, sp)], sp, K_FORWARD)
        for sv, sp in variants
    }
    n_obs = len(index)
    min_shift = 2 * K_FORWARD
    max_shift = n_obs - 2 * K_FORWARD
    meta = {"n_obs": n_obs, "min_shift": min_shift, "max_shift": max_shift,
            "k_forward": K_FORWARD, "n_offsets": len(IC_OFFSETS),
            "first_date": str(index[0].date()), "last_date": str(index[-1].date())}
    res = selection_permutation_test(
        variants, n_obs=n_obs,
        batch_stat_fn=make_batch_stat_fn(sig_arr, cells_by_variant),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="abs_conditional_ic_gap", meta=meta)
    return res, cells_by_variant, meta


def halves_cosine(sig: pd.Series, fwd: pd.Series, labels: pd.Series, split: str,
                  common_sel: pd.DatetimeIndex) -> dict:
    """闸③：选择窗按**中位交易日**对切，两半桶-IC 向量去均值余弦（预登记 §7 / D8）。"""
    n = len(common_sel)
    cut = n // 2
    early, late = common_sel[:cut], common_sel[cut:]
    res_e = window_conditional_ic(sig.reindex(early), fwd.reindex(early),
                                  labels.reindex(early), split)
    res_l = window_conditional_ic(sig.reindex(late), fwd.reindex(late),
                                  labels.reindex(late), split)
    cos = demeaned_cosine(res_e["bucket_ic"], res_l["bucket_ic"])
    return {"cosine": cos, "early": res_e, "late": res_l,
            "cut_date": str(late[0].date()), "n_early": len(early), "n_late": len(late)}


def diagnostic_paired_bootstrap(sig: pd.Series, fwd: pd.Series, labels: pd.Series,
                                low_bucket: int, common_sel: pd.DatetimeIndex,
                                n_boot: int = N_BOOT_DIAGNOSTIC, seed: int = 0) -> dict:
    """**诊断列，不进闸门**（预登记 §5）：调节版因子 vs 原因子的配对 IC 差。

    `paired_ic_bootstrap` 的 i.i.d. 抽样与本探针 20-offset 平均口径不匹配，故只作诊断。
    这里取**单 offset=0** 的非重叠网格（近独立抽样单位，符合该函数前提），
    x_a = `signal × w(s)`（IC 层选中的调节映射作用在因子上）、x_b = 原因子，
    同一组抽样行同时作用于两侧。
    """
    grid = nonoverlap_grid(common_sel, K_FORWARD, 0)
    y = fwd.reindex(grid)
    x_b = sig.reindex(grid)
    lab = labels.reindex(grid).to_numpy(dtype=float)
    w = np.where(lab == float(low_bucket), W_LOW_EFFICACY, W_OTHER) if low_bucket >= 0 \
        else np.ones(len(grid))
    x_a = pd.Series(x_b.to_numpy(dtype=float) * w, index=grid)
    out = paired_ic_bootstrap(x_a, x_b, y, n=n_boot, seed=seed)
    out["n_grid"] = int(len(grid))
    return out


def gate_rows(res, stats: dict, winner: tuple, cos_info: dict,
              ret_metrics: dict | None, ret_incumbent: dict | None) -> list[dict]:
    """四闸逐条判定（闸③ 不过 → 闸④ 记 SKIPPED_BY_GATE3，预登记 §11 预算纪律）。"""
    sv, sp = winner
    st_train = stats[(sv, sp, "train_2014_2020")]["signed_stat"]
    st_val = stats[(sv, sp, "val_2021_2023")]["signed_stat"]
    same_sign = bool(np.isfinite(st_train) and np.isfinite(st_val)
                     and st_train * st_val > 0)
    rows = [
        {"gate": GATE_ORDER[0], "metric": "signed_stat_train_2014_2020",
         "value": st_train, "threshold": "", "pass": ""},
        {"gate": GATE_ORDER[0], "metric": "signed_stat_val_2021_2023",
         "value": st_val, "threshold": "", "pass": ""},
        {"gate": GATE_ORDER[0], "metric": "two_window_same_sign",
         "value": float(same_sign), "threshold": 1.0, "pass": same_sign},
        {"gate": GATE_ORDER[1], "metric": "p_selected",
         "value": res.p_selected, "threshold": GATE_ALPHA,
         "pass": bool(res.p_selected < GATE_ALPHA)},
        {"gate": GATE_ORDER[1], "metric": "p_naive_UNCORRECTED(对照·不入闸)",
         "value": res.p_naive, "threshold": "", "pass": ""},
        {"gate": GATE_ORDER[1], "metric": "p_min_p(备用口径·不入闸)",
         "value": res.p_min_p, "threshold": "", "pass": ""},
        {"gate": GATE_ORDER[1], "metric": "selection_inflation(p_selected/p_naive)",
         "value": res.selection_inflation, "threshold": "", "pass": ""},
        {"gate": GATE_ORDER[2], "metric": "demeaned_cosine_early_vs_late",
         "value": cos_info["cosine"], "threshold": GATE3_COSINE_FLOOR,
         "pass": bool(np.isfinite(cos_info["cosine"])
                      and cos_info["cosine"] > GATE3_COSINE_FLOOR)},
    ]
    gate2 = bool(res.p_selected < GATE_ALPHA)
    gate3 = bool(np.isfinite(cos_info["cosine"])
                 and cos_info["cosine"] > GATE3_COSINE_FLOOR)
    if ret_metrics is None:
        rows.append({"gate": GATE_ORDER[3], "metric": "SKIPPED_BY_GATE3(预算纪律 §11)",
                     "value": "", "threshold": GATE4_THRESHOLD, "pass": False})
        gate4 = False
    else:
        w_tv = worst_tv(ret_metrics)
        gate4 = bool(np.isfinite(w_tv) and w_tv >= GATE4_THRESHOLD)
        for wname in STAT_WINDOWS:
            rows.append({"gate": GATE_ORDER[3], "metric": f"net_sharpe_{wname}",
                         "value": ret_metrics[wname]["net_sharpe"],
                         "threshold": "", "pass": ""})
        rows.append({"gate": GATE_ORDER[3], "metric": "worst_tv_net_sharpe_adjusted",
                     "value": w_tv, "threshold": GATE4_THRESHOLD, "pass": gate4})
        if ret_incumbent is not None:
            rows.append({"gate": GATE_ORDER[3],
                         "metric": "worst_tv_net_sharpe_incumbent(重算·同秤诊断)",
                         "value": worst_tv(ret_incumbent), "threshold": "", "pass": ""})
    overall = same_sign and gate2 and gate3 and gate4
    rows.append({"gate": "OVERALL", "metric": "四闸全过才 GO",
                 "value": "GO" if overall else "STOP", "threshold": "", "pass": overall})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="④ 条件化有效性探针（预登记冻结口径）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-boot", type=int, default=N_BOOT_DIAGNOSTIC,
                    help="诊断列 paired_ic_bootstrap 的抽样数（不进闸门）")
    ap.add_argument("--market-cache", default=None,
                    help="标的收益/carry 的本地副本目录（PG 只拉一次，见 docstring）")
    ap.add_argument("--out-prefix", default=OUTPUT_PREFIX)
    args = ap.parse_args(argv)

    # ---- 数据 ----
    sig = load_production_signal()
    und, carry = load_market_series(cache_dir=args.market_cache)
    fwd = forward_return(und, K_FORWARD).dropna()
    levels = build_state_levels(und, carry)
    pcts = build_state_percentiles(levels)
    labels = {(sv, sp): bucket_labels(pcts[sv], sp)
              for sv, sp in variant_grid()}

    # 共用日历 = 生产信号非空 ∩ 前瞻收益非空（窗口切分全在下游做）
    common = sig.dropna().index.intersection(fwd.index).sort_values()
    common_sel = _slice(pd.Series(0.0, index=common), SELECTION[0], SELECTION[1]).index

    # ---- 面板（8 变体 × 5 窗 × 桶） ----
    panel, stats = build_panel(sig, fwd, labels, common)

    # ---- ⓪ 机器（8 点合成单次调用） ----
    sig_sel = sig.reindex(common_sel).to_numpy(dtype=float)
    fwd_sel = fwd.reindex(common_sel).to_numpy(dtype=float)
    lab_sel = {v: labels[v].reindex(common_sel).to_numpy(dtype=float)
               for v in variant_grid()}
    res, _cells, meta = run_machine(sig_sel, fwd_sel, lab_sel, common_sel,
                                    args.n_perm, args.seed)
    if res.best_index < 0:
        raise RuntimeError("全部 8 变体均不可评分（-inf）——检查状态标签构造")
    winner = res.best_variant
    sv_w, sp_w = winner
    sel_stat = stats[(sv_w, sp_w, "selection_2014_2023")]["signed_stat"]
    low_bkt = low_efficacy_bucket(sel_stat, sp_w)

    # ---- 闸③（stability 硬闸；不过即刻中止不跑闸④） ----
    cos_info = halves_cosine(sig, fwd, labels[winner], sp_w, common_sel)
    gate3_pass = bool(np.isfinite(cos_info["cosine"])
                      and cos_info["cosine"] > GATE3_COSINE_FLOOR)

    # ---- 闸④（收益层，仅当闸③ 过） ----
    ret_adj = ret_inc = None
    pos_lf = production_position(sig).astype(float)
    if gate3_pass:
        pos_adj = adjusted_position(pos_lf, labels[winner], low_bkt)
        ret_adj = evaluate_windows(pos_adj, und, carry)
        ret_inc = evaluate_windows(pos_lf, und, carry)

    # ---- 诊断列（不进闸门） ----
    diag = diagnostic_paired_bootstrap(sig, fwd, labels[winner], low_bkt,
                                       common_sel, args.n_boot, args.seed)
    diag_rank_ic = {}
    for wname in WINDOWS_REPORT:
        idx = _slice(pd.Series(0.0, index=common), *WINDOWS_REPORT[wname]).index
        if len(idx) < _MIN_WINDOW_OBS:
            continue
        grid = nonoverlap_grid(idx, K_FORWARD, 0)
        lab_g = labels[winner].reindex(grid)
        for b in range(N_BUCKETS[sp_w]):
            m = (lab_g == float(b)).to_numpy()
            diag_rank_ic[(wname, b)] = rank_ic(sig.reindex(grid)[m], fwd.reindex(grid)[m])

    # ---- 产物 ----
    out_dir = BT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    panel["is_winner_variant"] = panel["variant"] == f"{sv_w}|{sp_w}"
    panel["rank_ic_offset0"] = [
        diag_rank_ic.get((r.window, r.bucket), {}).get("ic", np.nan)
        if r.variant == f"{sv_w}|{sp_w}" else np.nan for r in panel.itertuples()]
    panel["rank_ic_offset0_n"] = [
        diag_rank_ic.get((r.window, r.bucket), {}).get("n_obs", np.nan)
        if r.variant == f"{sv_w}|{sp_w}" else np.nan for r in panel.itertuples()]
    panel_path = out_dir / f"{args.out_prefix}_panel.csv"
    panel.to_csv(panel_path, index=False)

    rows = gate_rows(res, stats, winner, cos_info, ret_adj, ret_inc)
    meta_rows = _meta_rows(res, meta, winner, sel_stat, low_bkt, cos_info, diag,
                           levels, pcts, labels, common, common_sel, sig, und, carry,
                           ret_adj, ret_inc, args)
    verdict = pd.DataFrame(rows + meta_rows)
    verdict_path = out_dir / f"{args.out_prefix}_verdict.csv"
    verdict.to_csv(verdict_path, index=False)

    print(verdict.to_string(index=False))
    print(f"\n→ {panel_path}\n→ {verdict_path}")
    return 0


def _meta_rows(res, meta, winner, sel_stat, low_bkt, cos_info, diag, levels, pcts,
               labels, common, common_sel, sig, und, carry, ret_adj, ret_inc,
               args) -> list[dict]:
    """metadata 行：seed / n_perm / 缓存实际末日 / 逐变量 n_obs 与首末日 / 损失天数。"""
    sv_w, sp_w = winner
    m = []

    def add(metric, value, note=""):
        m.append({"gate": "META", "metric": metric, "value": value,
                  "threshold": note, "pass": ""})

    add("seed", args.seed)
    add("n_perm", res.n_perm)
    add("scheme", res.scheme)
    add("n_variants", len(res.variants))
    add("k_forward", K_FORWARD, "非重叠 offset 数 = 20（全 offset 加权平均）")
    add("min_shift", meta["min_shift"], "房规 [2k, n−2k]")
    add("max_shift", meta["max_shift"], "房规 [2k, n−2k]")
    add("n_obs_selection", meta["n_obs"], f'{meta["first_date"]} ~ {meta["last_date"]}')
    add("winner_variant", f"{sv_w}|{sp_w}")
    add("winner_signed_stat_selection", sel_stat)
    add("winner_abs_stat_observed", res.observed_best)
    add("low_efficacy_bucket", BUCKET_NAMES[sp_w][low_bkt] if low_bkt >= 0 else "none",
        "w=0.5 的桶；其余（含状态 NaN 日）w=1.0")
    add("gate3_cut_date", cos_info["cut_date"],
        f'early={cos_info["n_early"]}d / late={cos_info["n_late"]}d')
    add("min_bucket_days_floor", MIN_BUCKET_DAYS)
    add("min_cell_points", MIN_CELL_POINTS)
    add("pct_window", PCT_WINDOW)
    add("vol_window", VOL_WINDOW)
    add("incumbent_worst_tv_sharpe_prereg", INCUMBENT_WORST_TV_SHARPE,
        "probe_5b_..._panel.csv:4 / baseline_metrics.csv:105 双证")
    add("gate4_threshold", GATE4_THRESHOLD, "= 1.000931 + 0.10")
    add("signal_csv", PRODUCTION_CSV, PRODUCTION_COL)
    add("signal_first_last", f"{sig.index[0].date()} ~ {sig.index[-1].date()}",
        f"n={len(sig)}")
    add("underlying_first_last", f"{und.index[0].date()} ~ {und.index[-1].date()}",
        f"n={len(und)}（blend = 000905/000852 各半）")
    add("carry_first_last", f"{carry.index[0].date()} ~ {carry.index[-1].date()}",
        f"n={len(carry)}；缺腿按 0、止于 2026-04-29（holdout 末段无 carry）")
    add("common_index_first_last",
        f"{common[0].date()} ~ {common[-1].date()}", f"n={len(common)}")
    for sv in STATE_VARS:
        lv, pc = levels[sv], pcts[sv].dropna()
        add(f"level_{sv}", f"{lv.index[0].date()} ~ {lv.index[-1].date()}",
            f"n_obs={len(lv)}（原值序列，垃圾尾已剔）")
        add(f"pct_{sv}", f"{pc.index[0].date()} ~ {pc.index[-1].date()}",
            f"n_obs={len(pc)}（250d 分位）")
        lab = labels[(sv, "tercile")].reindex(common_sel)
        add(f"selection_labelled_days_{sv}", int(lab.notna().sum()),
            f"选择窗 {len(common_sel)} 天中因分位 NaN 丢弃 "
            f"{int(lab.isna().sum())} 天")
    add("cache_thermometer_last", str(levels["S3_limitup"].index[-1].date()),
        "trim_incomplete_tail 后（2026-07-01 垃圾行已剔）")
    add("cache_turnover_last", str(levels["S2_turnover"].index[-1].date()),
        "trim_incomplete_tail 后（2026-07-01 垃圾行已剔）")
    add("diag_paired_ic_diff(调节版因子−原因子·offset0·不进闸)", diag["diff_ic"],
        f'CI=[{diag["ci_lo"]:.4f}, {diag["ci_hi"]:.4f}] p={diag["p_value"]:.4f} '
        f'n_grid={diag["n_grid"]}')
    add("diag_n_distinct_null_winners", int(np.count_nonzero(res.null_winner_counts)))
    add("diag_null_selected_p95", float(np.percentile(res.null_selected, 95)))
    for v, obs, pn in zip(res.variants, res.observed, res.p_naive_per_variant):
        add(f"observed_abs_stat[{v[0]}|{v[1]}]", float(obs),
            f"p_naive_UNCORRECTED={pn:.4f}（对照·不入闸）")
    if ret_adj is not None:
        for wname, mm in ret_adj.items():
            add(f"ret_adjusted[{wname}]", mm["net_sharpe"],
                f'ann={mm["net_ann"]:.4f} maxdd={mm["maxdd"]:.4f} '
                f'turnover={mm["turnover"]:.3f} n={mm["n_obs"]}')
        for wname, mm in ret_inc.items():
            add(f"ret_incumbent[{wname}]", mm["net_sharpe"],
                f'ann={mm["net_ann"]:.4f} maxdd={mm["maxdd"]:.4f} '
                f'turnover={mm["turnover"]:.3f} n={mm["n_obs"]}')
    else:
        add("ret_layer", "SKIPPED", "闸③ 未过 → 预算纪律 §11 中止，不跑闸④")
    add("caveat_engine_exec_price",
        "engine.py:24 shift(1)=T 收盘成交（文档债）+ 首日成本落第二天",
        "对现役与调节版是同引擎同日期的非差分偏差，原样保留不顺手修")
    add("caveat_holdout_carry", "holdout 2024-2026 末段无 carry 输入（止于 2026-04-29）",
        "只报告不进闸；对多头略偏悲观")
    add("caveat_cache_not_extended", "三缓存不延展，有效末日 2026-06-30",
        "缺口 29~30 天全在 holdout 尾巴，闸门零影响（附录 §1.1 补充发现 B）")
    return m


if __name__ == "__main__":
    raise SystemExit(main())
