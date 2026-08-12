"""候选 ⓪：选择校正置换检验机器（每次置换重跑整套选优 → 当选统计量空分布）。

预登记出处：`docs/plans/2026-07-11-external-review-fixes.md` §4——
「任何探针未来要出 PASS 结论，**必须先改造置换过程（每次置换重跑整套选优）或用独立确认窗**」。
议程定位：`docs/plans/2026-08-12-signal-research-agenda.md` §3.7 候选 ⓪ / §4 C1
（①b / ② / ④ / ⑤ 四条共用这一笔机器债；⑥ 走"钉死参数空间"分支不吃这笔债）。

## 这台机器修的是什么

既有五探针（rotation / leverage / thermo / long_axes / erp）的做法是：
**先在网格上选出赢家 → 只对赢家跑置换 p**。赢家是"网格里最极端的那个"，
拿它去比"单个变体的置换零分布"，p 被选择效应系统性压小（本模块 TDD 用合成 null 量化：16 点网格上 naive p<0.05
的假阳率 20%、40 点网格 35~44%，而不是名义 5%）。

修法（本模块）：把置换循环挪到选优**外面**——每个置换样本下**重算整套网格的统计量、
重新套用同一条选优规则**，取"当选统计量"的空分布（argmax 时即 max-statistic）。
观测赢家对着这条分布算 p，选择效应被吃进零分布里。

## 关键实现口径：置换作用在"信号↔收益的配对"上，不重算因子

"重跑整套选优" = 每个置换样本下**重算全网格统计量并重新选优**；
**变体构造本身不依赖置换**（所有变体都是同一段价格历史的确定性函数，置换只打断
信号与收益的时间配对），所以因子只需算一次、置换只重排已算好的信号。
计算量 = 原来的 × 网格点数（这也是各候选"预登记不扩网格"的第二个理由）。

代价与前提：循环移位与因果滚动变换只在**环形平稳**近似下精确可交换
（移位后的绕接点处，滚动窗口跨越了序列首尾）。本仓 n≈3,000 交易日、lookback≤60，
边缘污染 ≤2%，可忽略。若某探针的变体构造**本身依赖收益**（例如按收益分箱建的状态变量），
则必须走更贵的分支：把 stat_fn 写成"用重排后的输入重建变体再评分"——API 支持，成本 × 网格点数。

## API 契约

- `stat_fn(variant, idx) -> float`：`idx` 是长度 n_obs 的**重抽样索引**（int 数组），
  观测样本的 idx = `np.arange(n_obs)`。调用方在闭包里决定"重排哪一侧"
  （惯例：重排信号侧，收益/控制变量按日历不动，与 rotation_probe 一致）。
  统计量必须是**越大越好**（双侧口径请自行返回 |IC|）；**该行根本无法评分**的变体返回 `-inf`
  （样本不足 / 窗口不够 / 统计量为 NaN）。
  ⚠️ **边界**：若某个条件**同时是一道独立闸门**（例如 ② 关2 的"两半窗同号"），
  **不得**把它编码进 `-inf`——否则该条件被双记，且 `p_selected` 的含义从"最优统计量有多难
  靠错配得到"偏移成"最优的**合规**统计量有多难得到"，既对不上历史探针的 p，也对不上本模块
  的校准证据（校准是在无约束 argmax 上做的）。正解：机器跑无约束统计量，闸门后置独立判。
  `leverage_probe.pick_representative` 能用 `-inf` 是因为在那里同号是**选择规则本身**
  （gate2 直接复用选择结果），不是额外的独立闸门——形似而实质不同。
- `batch_stat_fn(variant, idx_matrix) -> ndarray(m,)`（可选，纯提速）：一次算完某变体
  在全部 m 个重抽样下的统计量；给了就用它，不给就逐行调 `stat_fn`。
- `select_fn(stats) -> int`：选优规则，默认 `argmax`；返回 −1 表示"本行无可行变体"。
  **观测行与每个置换行走同一条规则**——这正是本模块与旧做法的唯一实质区别。

## 三个 p（同一份 (B+1)×J 统计量矩阵派生，成本为零）

把观测行与 B 个置换行**并成 (B+1) 行池**（零假设下可交换），全部 p 都是
"池内有多少行不劣于观测行 / (B+1)"，与 `rotation_probe.shift_permutation_pvalue`、
`significance.bootstrap_pvalue` 的 `(1+count)/(n+1)` 完全同口径：

- `p_selected`（**主口径**）：当选统计量的空分布；argmax 时 = max-statistic。
- `p_min_p`（备用口径）：Westfall–Young min-P。先把每个变体各自转成置换 p（尺度无关），
  统计量取 `min_j p_j`。当各变体的零分布**尺度不同**（高方差变体会霸占 max）时比 max 更有功效。
- `p_naive`（**对照，不得用于裁决**）：旧做法——只拿赢家那一列的零分布算 p。
  同一次运行里同时报出来，就是"机器修好了"的现场证据（`selection_inflation` = 两者之比）。

## 置换方案

- `scheme="rotation"`（默认）：循环移位。保留信号与收益**各自**的自相关结构、只打断配对，
  比 iid 置换保守（`rotation_probe.shift_permutation_pvalue` docstring 的原话），
  且在环形平稳下与观测行**精确可交换** → p 精确校准。
- `scheme="block"`：重叠块重抽样（`paired_bootstrap.moving_block_indices` 同构）。
  有放回抽样会改变信号的成分，观测行与置换行**不精确可交换** → p 只是近似校准，
  **仅作敏感性对照**，不作主口径。适用面：信号非平稳到循环移位的绕接不可接受时。

CLI（演示，**不出裁决**）: python3 -m backtest.selection_permutation --demo equal_weight

本模块**不改动** `significance.py` / `scan.py` / `momentum_scan.py` / 任何既有探针 / 任何冻结根。
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

StatFn = Callable[[Any, np.ndarray], float]
BatchStatFn = Callable[[Any, np.ndarray], np.ndarray]
SelectFn = Callable[[np.ndarray], int]

SCHEMES = ("rotation", "block")


# ---------------------------------------------------------------- 重抽样索引（纯函数）
def identity_index(n_obs: int) -> np.ndarray:
    """观测样本的索引（恒等重排）——观测行与置换行走同一条 stat_fn 代码路径。"""
    return np.arange(int(n_obs), dtype=np.int32)


def rotation_index_matrix(n_obs: int, n_perm: int, rng, min_shift: int = 1,
                          max_shift: int | None = None) -> np.ndarray:
    """循环移位索引矩阵 (n_perm, n_obs)：第 b 行满足 `x[idx[b]] == np.roll(x, shift_b)`。

    shift 从 `[min_shift, max_shift)` 均匀抽（半开区间，与 `rotation_probe` 的
    `rng.integers(2k, n-2k)`、`significance` 的 `rng.integers(1, length)` 逐字同口径；
    numpy Generator 的批量 `integers(lo, hi, size=B)` 与逐次调用产生**同一条流**，
    故同 seed 下本函数的 shift 序列与那两处**逐个相同**——见 tests 的口径一致性判例）。

    `min_shift` 建议给 IC 类统计量传 `2k`（避免小位移残留配对，rotation_probe 的做法）。
    """
    n_obs = int(n_obs)
    lo = int(min_shift)
    hi = n_obs if max_shift is None else int(max_shift)
    if not 1 <= lo < hi <= n_obs:
        raise ValueError(f"需要 1 <= min_shift < max_shift <= n_obs，得到 "
                         f"({lo}, {hi}, {n_obs})")
    shifts = rng.integers(lo, hi, size=int(n_perm))
    base = np.arange(n_obs, dtype=np.int64)
    return ((base[None, :] - shifts[:, None]) % n_obs).astype(np.int32)


def moving_block_index_matrix(n_obs: int, block: int, n_perm: int, rng) -> np.ndarray:
    """重叠（moving）块重抽样索引矩阵 (n_perm, n_obs)。

    与 `paired_bootstrap.moving_block_indices` 同构（同 seed 同参数逐元素相同，
    tests 有交叉判例）；这里独立实现只为避免 import 既有探针模块时连带拉起数据层。

    ⚠️ **这是一份副本，不是共享实现**：若日后有人改了 `paired_bootstrap` 那一侧
    （块长语义、starts 的抽法、尾部截断方式），两边会**静默漂移**——
    交叉判例 `test_moving_block_index_matrix_matches_paired_bootstrap` 是唯一的守卫，
    它红了就是这里要跟着改，不是把判例删掉。
    """
    n_obs, block, n_perm = int(n_obs), int(block), int(n_perm)
    if block < 1 or block > n_obs:
        raise ValueError(f"block 必须落在 [1, {n_obs}]，得到 {block}")
    k = int(np.ceil(n_obs / block))
    starts = rng.integers(0, n_obs - block + 1, size=(n_perm, k))
    idx = starts[:, :, None] + np.arange(block)[None, None, :]
    return idx.reshape(n_perm, k * block)[:, :n_obs].astype(np.int32)


def build_index_matrix(n_obs: int, n_perm: int, *, scheme: str = "rotation", seed: int = 0,
                       min_shift: int = 1, max_shift: int | None = None,
                       block: int = 20) -> np.ndarray:
    """按方案生成 (n_perm, n_obs) 重抽样索引矩阵。显式 seed → 同输入同输出。"""
    if scheme not in SCHEMES:
        raise ValueError(f"未知 scheme={scheme!r}，可选 {SCHEMES}")
    rng = np.random.default_rng(seed)
    if scheme == "rotation":
        return rotation_index_matrix(n_obs, n_perm, rng, min_shift, max_shift)
    return moving_block_index_matrix(n_obs, block, n_perm, rng)


# ---------------------------------------------------------------- 统计量矩阵与选优
def statistic_matrix(variants: Sequence[Any], index_matrix: np.ndarray,
                     stat_fn: StatFn | None = None,
                     batch_stat_fn: BatchStatFn | None = None) -> np.ndarray:
    """(m, J) 统计量矩阵：第 r 行 = 第 r 个重抽样下**整套网格**的统计量。

    这一行就是"每次置换重跑整套选优"的全部内容——旧做法只算得到赢家那一列。
    """
    if stat_fn is None and batch_stat_fn is None:
        raise ValueError("stat_fn 与 batch_stat_fn 至少给一个")
    index_matrix = np.asarray(index_matrix)
    m = index_matrix.shape[0]
    out = np.empty((m, len(variants)), dtype=float)
    for j, variant in enumerate(variants):
        if batch_stat_fn is not None:
            col = np.asarray(batch_stat_fn(variant, index_matrix), dtype=float)
            if col.shape != (m,):
                raise ValueError(f"batch_stat_fn 对 {variant!r} 返回形状 {col.shape}，"
                                 f"期望 ({m},)")
            out[:, j] = col
        else:
            for r in range(m):
                out[r, j] = float(stat_fn(variant, index_matrix[r]))
    return np.nan_to_num(out, nan=-np.inf, posinf=np.inf, neginf=-np.inf)


def argmax_select(stats: np.ndarray) -> int:
    """默认选优规则：取统计量最大者；全部不可行（−inf/空）→ −1。

    并列时 `np.argmax` 取**最小下标** → `null_winner_counts` 这个**诊断**列会略微偏向
    网格前部（只在统计量大量并列时可见）。**不影响任何 p**：p 只看当选统计量的**值**，
    与"并列时挑了谁"无关。
    """
    stats = np.asarray(stats, dtype=float)
    if stats.size == 0 or not np.any(np.isfinite(stats)):
        return -1
    return int(np.argmax(stats))


def apply_selection(pool: np.ndarray, select_fn: SelectFn) -> tuple[np.ndarray, np.ndarray]:
    """逐行套用选优规则 → (当选下标 (m,), 当选统计量 (m,))；无可行变体 → (−1, −inf)。"""
    m = pool.shape[0]
    picks = np.empty(m, dtype=np.int64)
    vals = np.empty(m, dtype=float)
    for r in range(m):
        j = int(select_fn(pool[r]))
        picks[r] = j
        vals[r] = pool[r, j] if j >= 0 else -np.inf
    return picks, vals


def column_pvalues(pool: np.ndarray) -> np.ndarray:
    """池内逐列置换 p：`p[r, j] = #{r': pool[r', j] >= pool[r, j]} / m`（含自身）。

    m = B+1（观测行 + B 个置换行）。对观测行即 `(1 + #{置换 ≥ 观测}) / (B+1)`——
    与 `rotation_probe.shift_permutation_pvalue` / `significance.bootstrap_pvalue`
    的房规逐字同口径（tests 有精确一致性判例）。并列按"最保守"处理（同值同 p）。

    注：min-P 只用到这个矩阵的**行内排序**，而"留一"口径 `(count−1)/(m−1)` 是本口径
    的严格单调变换 → min-P 的结果与选哪个口径**完全无关**（实测两者逐位相同）。
    """
    pool = np.asarray(pool, dtype=float)
    m, n_var = pool.shape
    out = np.empty((m, n_var), dtype=float)
    for j in range(n_var):
        col = pool[:, j]
        ordered = np.sort(col)
        out[:, j] = (m - np.searchsorted(ordered, col, side="left")) / m
    return out


# ---------------------------------------------------------------- 结果容器
@dataclass(frozen=True)
class SelectionPermutationResult:
    """一次"选优 + 选择校正置换"的完整结果（所有 p 同口径 `count/(B+1)`）。

    ⚠️ **三个 p 的取用纪律**（字段名保持稳定，纪律写在这里）：
    - `p_selected` = **唯一可进闸门的主口径**；
    - `p_min_p` = 备用口径，改用它必须在探针文档写明理由（且需 B ≫ J，见 `column_pvalues`）；
    - **`p_naive` / `p_naive_per_variant` = 被本模块替换掉的旧做法，只准出现在
      "选择效应有多大"的证据段，任何 STOP/GO 闸门都不得读它**。它就是勘误 §4 认定
      系统性偏乐观的那个量；留在结果里是为了每次运行自带对照，不是为了给人用。
    """
    variants: tuple
    observed: np.ndarray            # (J,) 观测统计量（整套网格）
    null_stats: np.ndarray          # (B, J) 置换统计量
    best_index: int                 # 观测赢家（−1 = 无可行变体）
    observed_best: float
    p_selected: float               # 主口径：当选统计量空分布（argmax 时 = max-statistic）
    p_min_p: float                  # 备用口径：Westfall–Young min-P
    p_naive: float                  # ⚠️ 对照量：旧做法（只对赢家算 p）——**不得用于裁决**
    p_naive_per_variant: np.ndarray  # ⚠️ (J,) 同上，逐变体版，同样不得用于裁决
    null_selected: np.ndarray       # (B,) 当选统计量的空分布
    null_winner_counts: np.ndarray  # (J,) 各变体在空分布下的当选次数
    n_perm: int
    seed: int | None                # None = 索引矩阵由调用方注入，本次运行没有自己的 seed
    scheme: str
    statistic_name: str = "statistic"
    meta: dict = field(default_factory=dict)

    @property
    def best_variant(self):
        return self.variants[self.best_index] if self.best_index >= 0 else None

    @property
    def selection_inflation(self) -> float:
        """选择效应放大倍数 = 校正 p / naive p（>1 表示旧做法把 p 压小了这么多倍）。"""
        return float(self.p_selected / self.p_naive) if self.p_naive > 0 else float("nan")

    def summary(self) -> dict:
        return {
            "statistic": self.statistic_name,
            "n_variants": len(self.variants),
            "n_perm": self.n_perm,
            "seed": self.seed,
            "scheme": self.scheme,
            "best_variant": str(self.best_variant),
            "observed_best": self.observed_best,
            "p_selected": self.p_selected,
            "p_min_p": self.p_min_p,
            "p_naive_UNCORRECTED": self.p_naive,
            "selection_inflation": self.selection_inflation,
            "null_selected_mean": float(np.mean(self.null_selected)),
            "null_selected_p95": float(np.percentile(self.null_selected, 95)),
            "n_distinct_null_winners": int(np.count_nonzero(self.null_winner_counts)),
        }

    def to_frame(self) -> pd.DataFrame:
        """逐变体明细：观测统计量 / naive p / 空分布下当选次数。"""
        return pd.DataFrame({
            "variant": [str(v) for v in self.variants],
            "observed": self.observed,
            "p_naive_per_variant_UNCORRECTED": self.p_naive_per_variant,
            "null_winner_count": self.null_winner_counts,
            "is_observed_winner": np.arange(len(self.variants)) == self.best_index,
        })


# ---------------------------------------------------------------- 主入口
def selection_permutation_test(
    variants: Sequence[Any], *, n_obs: int,
    stat_fn: StatFn | None = None, batch_stat_fn: BatchStatFn | None = None,
    n_perm: int = 1000, seed: int = 0, scheme: str = "rotation",
    min_shift: int = 1, max_shift: int | None = None, block: int = 20,
    select_fn: SelectFn | None = None, index_matrix: np.ndarray | None = None,
    statistic_name: str = "statistic", meta: dict | None = None,
) -> SelectionPermutationResult:
    """每次置换重跑整套选优 → 给观测赢家一个**选择校正后**的 p。

    参数见模块 docstring 的"API 契约"。`index_matrix` 可直接注入（跳过 seed 采样），
    用于口径一致性判例与复现。
    """
    variants = tuple(variants)
    if not variants:
        raise ValueError("variants 不能为空")
    select_fn = select_fn or argmax_select

    if index_matrix is None:
        index_matrix = build_index_matrix(
            n_obs, n_perm, scheme=scheme, seed=seed,
            min_shift=min_shift, max_shift=max_shift, block=block)
    else:
        index_matrix = np.asarray(index_matrix)
        if index_matrix.ndim != 2 or index_matrix.shape[1] != n_obs:
            raise ValueError(f"index_matrix 形状应为 (n_perm, {n_obs})，"
                             f"得到 {index_matrix.shape}")
        n_perm = index_matrix.shape[0]
        seed = None   # 索引由调用方注入 → 本次运行的 seed 不是复现依据，别记个假的

    # 观测行排在池首 → 与置换行走同一条 stat_fn 代码路径（可交换性的前提）
    pool_index = np.vstack([identity_index(n_obs)[None, :], index_matrix])
    pool = statistic_matrix(variants, pool_index, stat_fn, batch_stat_fn)
    m = pool.shape[0]

    picks, sel_vals = apply_selection(pool, select_fn)
    best_index = int(picks[0])
    observed_best = float(sel_vals[0])

    # 主口径：当选统计量的空分布（观测行含在池内 → p ≥ 1/(B+1)）
    p_selected = float(np.count_nonzero(sel_vals >= sel_vals[0]) / m)

    # 对照：旧做法只看赢家那一列
    colp = column_pvalues(pool)
    p_naive = float(colp[0, best_index]) if best_index >= 0 else 1.0

    # 备用口径：Westfall–Young min-P（对各变体零分布尺度不同更稳）
    minp = colp.min(axis=1)
    p_min_p = float(np.count_nonzero(minp <= minp[0]) / m)

    null_picks = picks[1:]
    counts = np.zeros(len(variants), dtype=np.int64)
    valid = null_picks[null_picks >= 0]
    if valid.size:
        counts += np.bincount(valid, minlength=len(variants))

    return SelectionPermutationResult(
        variants=variants, observed=pool[0].copy(), null_stats=pool[1:].copy(),
        best_index=best_index, observed_best=observed_best,
        p_selected=p_selected, p_min_p=p_min_p, p_naive=p_naive,
        p_naive_per_variant=colp[0].copy(), null_selected=sel_vals[1:].copy(),
        null_winner_counts=counts, n_perm=int(m - 1),
        seed=(None if seed is None else int(seed)), scheme=scheme,
        statistic_name=statistic_name, meta=dict(meta or {}),
    )


# ---------------------------------------------------------------- 调用方适配器
def make_stat_fn(signals: Mapping[Any, np.ndarray],
                 score: Callable[[np.ndarray, Any], float]) -> StatFn:
    """把"每变体一条**预先算好**的信号数组" + `score(重排后的信号, variant)` 组装成 stat_fn。

    这是本机器的推荐用法：因子只算一次（`signals[variant]`），置换只重排已算好的信号
    → 成本 = 网格点数 × n_perm 次**评分**，而不是 × n_perm 次**重算因子**。
    """
    def _fn(variant: Any, idx: np.ndarray) -> float:
        return float(score(np.asarray(signals[variant])[idx], variant))
    return _fn


def make_batch_stat_fn(signals: Mapping[Any, np.ndarray],
                       batch_score: Callable[[np.ndarray, Any], np.ndarray]) -> BatchStatFn:
    """`make_stat_fn` 的向量化版：`batch_score(信号矩阵 (m, n), variant) -> (m,)`。"""
    def _fn(variant: Any, idx_matrix: np.ndarray) -> np.ndarray:
        return np.asarray(batch_score(np.asarray(signals[variant])[idx_matrix], variant),
                          dtype=float)
    return _fn


# ---------------------------------------------------------------- 演示（**非探针**）
DEMO_BANNER = (
    "⚠️ 本运行是**机器演示**，不是探针：不构成任何轴的 PASS / STOP，"
    "不进任何裁决表（候选 ⓪ 是基建，无 STOP/GO 闸门）。"
)
DEMO_WINDOWS = {"train_14_20": ("2014-01-01", "2020-12-31"),
                "val_21_23": ("2021-01-01", "2023-12-31")}


def _demo_grid() -> list[dict]:
    """与 `backtest.scan.default_grid()` 同构的 40 点网格（committed
    scan_equal_weight.csv 的 40 行即此网格）。此处独立列出，不改 scan.py。"""
    return [{"lookback": lb, "z_window": zw, "smoothing": sm}
            for lb in (5, 10, 20, 40, 60)
            for zw in (2 * lb, 3 * lb, 5 * lb, 250)
            for sm in (0, 5)]


def _demo_factor_fn(db=None, start=None):
    """`scan.equal_weight_factor_fn` 的同构副本，只多一个 `db` 透传（不改 scan.py）。

    存在理由：`scan.equal_weight_factor_fn` 不收 db 参数，演示要在**只读备端**上跑。
    因子计算本身仍走冻结根的 `calculate_contrast_equal_weight_signal`，零口径漂移。
    """
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal, load_pair_configs,
    )
    names = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
             "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
    prices = load_pg_closes(names, start=start, db=db)
    pair_configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")

    def fn(lookback, z_window, smoothing):
        out = calculate_contrast_equal_weight_signal(
            prices, lookback=lookback, z_window=z_window,
            smoothing_window=smoothing, pair_configs=pair_configs,
        )
        return out["factor_value"]

    return fn


def run_demo(n_perm: int = 200, seed: int = 0, cost_bps: float = 3.0, db=None,
             grid: list[dict] | None = None) -> tuple[SelectionPermutationResult, pd.DataFrame]:
    """真数据端到端演示：equal_weight 40 点网格 × `worst(train, val) Sharpe` 选优规则。

    统计量与选优规则**照抄 `scan.py` 的现行做法**（discrete 仓位、blend 口径、
    展示排序键 `worst_tv = min(sharpe_train, sharpe_val)`，holdout 不进选择），
    唯一的改动就是"每次置换重跑整套选优"。**只读、不出裁决。**
    """
    from backtest.data import load_carry, load_underlying_returns
    from backtest.engine import run_strategy
    from backtest.metrics import sharpe
    from backtest.positions import to_position

    grid = grid if grid is not None else _demo_grid()
    fn = _demo_factor_fn(db=db)
    raw = {}
    for params in grid:
        raw[tuple(sorted(params.items()))] = fn(**params)

    und = load_underlying_returns("blend", db=db)
    car = load_carry("blend", db=db)
    common = None
    for s in raw.values():
        idx = s.dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.intersection(und.index).sort_values()
    n_obs = len(common)

    und_v = und.reindex(common)
    car_v = car.reindex(common)
    win_mask = {w: ((common >= pd.Timestamp(a)) & (common <= pd.Timestamp(b)))
                for w, (a, b) in DEMO_WINDOWS.items()}
    signals = {k: v.reindex(common).to_numpy(dtype=float) for k, v in raw.items()}

    def score(vals: np.ndarray, variant) -> float:
        fac = pd.Series(vals, index=common)
        pos = to_position(fac, mode="discrete")
        out = []
        for w, mask in win_mask.items():
            ret = run_strategy(pos[mask], und_v[mask], cost_bps, car_v[mask])["ret"]
            out.append(sharpe(ret))
        return float(min(out))

    variants = [tuple(sorted(p.items())) for p in grid]
    res = selection_permutation_test(
        variants, n_obs=n_obs, stat_fn=make_stat_fn(signals, score),
        n_perm=n_perm, seed=seed, scheme="rotation",
        statistic_name="worst_tv_sharpe(train,val)",
        meta={"n_obs": n_obs, "cost_bps": cost_bps,
              "first_date": str(common[0].date()), "last_date": str(common[-1].date())},
    )
    return res, res.to_frame()


def write_demo_sidecar(res: SelectionPermutationResult, out_dir: Path,
                       db_host: str | None = None) -> Path:
    """演示的 summary sidecar（JSON）。

    存在理由：逐变体 CSV 里**没有** p_selected / p_min_p / 空分布水位 / 运行参数，
    单看 CSV 无法复述结论、也无法判断它是哪次运行的产物。sidecar 把这些一并落盘，
    并把"非裁决"横幅写进文件本身。
    """
    import json

    null_sel = np.asarray(res.null_selected, dtype=float)
    finite = null_sel[np.isfinite(null_sel)]
    payload = {
        "WARNING_NOT_A_PROBE": DEMO_BANNER,
        "summary": res.summary(),
        "run": {**res.meta, "db_host": db_host or "settings.yaml default",
                "windows": {k: list(v) for k, v in DEMO_WINDOWS.items()},
                "selection_rule": "argmax over worst(train, val) sharpe（照抄 scan.py）"},
        "null_selected_quantiles": {
            f"p{q}": float(np.percentile(finite, q)) for q in (5, 25, 50, 75, 95, 99)
        } if finite.size else {},
    }
    path = out_dir / "selection_permutation_demo_summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               default=str) + "\n", encoding="utf-8")
    return path


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="候选 ⓪ 选择校正置换机器（演示模式，不出裁决）")
    ap.add_argument("--demo", choices=["equal_weight"], default="equal_weight")
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--db-host", default=None, help="覆盖库主机（只读演示用）")
    args = ap.parse_args()

    db = None
    if args.db_host:
        from signals.common.config import load_db_config
        db = dict(load_db_config())
        db["host"] = args.db_host

    print(DEMO_BANNER)
    res, frame = run_demo(n_perm=args.n_perm, seed=args.seed,
                          cost_bps=args.cost_bps, db=db)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "selection_permutation_demo.csv"
    frame.round(6).to_csv(out_path, index=False)
    side_path = write_demo_sidecar(res, out_dir, db_host=args.db_host)

    show = frame.sort_values("observed", ascending=False).head(10).copy()
    show["observed"] = show["observed"].round(3)
    show["p_naive_per_variant_UNCORRECTED"] = show[
        "p_naive_per_variant_UNCORRECTED"].round(4)
    print(show.to_string(index=False))
    print("\n=== 选择校正结果 ===")
    for k, v in res.summary().items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    for k, v in res.meta.items():
        print(f"  {k}: {v}")
    print(f"\n{DEMO_BANNER}\n→ {out_path}\n→ {side_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
