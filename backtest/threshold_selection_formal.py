"""阈值 θ 的**选优校正检验**（⓪ 机器）—— 把"调阈值没东西"从判断变成过闸读数。

## 命题

`2026-08-18-threshold-by-underlying.md` 给出的判断是：各标的最优 θ 确实不同，但差异
主要是噪声挖掘（增益与基线强度 corr −0.867、部署组合两腿方向相反、θ>0 半边锯齿）。
那份文档的结论是**我的解读**，不是过闸读数。本模块用
`backtest/selection_permutation.py`（⓪ 机器）给它一个 `p_selected`。

## 两个检验（都在跑前定死，不得事后择读）

- **主检验 A（部署口径）**：选优空间 = 现役 blend(500+1000) 上的 29 个 θ。
  统计量 = `worst_tv(θ) − worst_tv(θ=0)`（**同一置换下配对**，标的静态特征抵消）。
  问题："**在真正跑着的口径上，调 θ 的最优增益能否与噪声区分？**"
- **副检验 B（全空间）**：选优空间 = 7 标的 × 29 θ = 203 点，同一统计量。
  问题："**若允许按标的各调各的 θ，最优组合能否与噪声区分？**"

## 判据（预先声明）

`p_selected < 0.05` 才算"调 θ 有东西"。`p_naive`（不校正选优）仅作**对照列**报告，
**不得进闸**——①a 判例与 ⑧ 收官文档都已把这条钉死（⑧ 实测选择膨胀 1.9×）。

## 口径

沿用 ⑧ 正式跑：`scheme="rotation"`、`min_shift=60`、`max_shift=n−60`、
`n_perm=1000`、`seed=0`。零 carry、`cost_bps=3.0`、信号固定 = 部署列 `factor_value`。
置换作用在**仓位序列**上（`pos_values[idx]`，与 ⑧ 的 `make_stat_fn` 同一写法）；
θ 阈值与置换可交换（先阈值后旋转 = 先旋转后阈值），故这样写不改变语义。

## 用法

    python3 -m backtest.threshold_selection_formal                  # A + B
    python3 -m backtest.threshold_selection_formal --scope blend    # 只跑 A
    python3 -m backtest.threshold_selection_formal --n-perm 30      # 快速自检
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402
from backtest.threshold_by_underlying_probe import THETAS, _instruments  # noqa: E402

MIN_SHIFT = 60                      # 与 ⑧ 正式跑同口径
STAT_WINDOWS = ("2014-2020", "2021-2023")   # worst_tv 只读这两窗（2024-26 不入）
COST_BPS = 3.0
OUT = ROOT / "backtest" / "output" / "threshold_selection_verdict.json"


class Data:
    """对齐后的仓位/标的/窗口掩码，全部按同一条 DatetimeIndex。"""

    def __init__(self, cost_bps: float = COST_BPS):
        _, smooth = load_two_columns()
        unds = _instruments()
        idx = smooth.index
        for u in unds.values():
            idx = idx.intersection(u.index)
        self.idx = idx.sort_values()
        self.cost_bps = cost_bps
        self.thetas = [t for t in THETAS if t > -1.0]      # 买入持有参照行不进选优
        self.pos = {t: production_position(smooth.reindex(self.idx), threshold=t)
                    .astype(float).to_numpy() for t in self.thetas}
        self.und = {k: v.reindex(self.idx) for k, v in unds.items()}
        self.masks = {w: ((self.idx >= pd.Timestamp(WINDOWS[w][0]))
                          & (self.idx <= pd.Timestamp(WINDOWS[w][1])))
                      for w in STAT_WINDOWS}

    def worst_tv(self, pos_arr: np.ndarray, uname: str) -> float:
        """worst(train, val) 的 Sharpe —— 复用 `run_strategy` + `metrics.sharpe`。"""
        pos = pd.Series(pos_arr, index=self.idx)
        und = self.und[uname]
        vals = []
        for w in STAT_WINDOWS:
            m = self.masks[w]
            r = run_strategy(pos[m], und[m], self.cost_bps, None)["ret"]
            vals.append(float(sharpe(r)))
        return float(min(vals))


def make_stat_fn(d: Data):
    """`stat_fn((标的, θ), idx) -> worst_tv(θ) − worst_tv(0)`，同一 idx 下配对。

    基线（θ=0）只依赖 (标的, idx)，按该键缓存 —— 否则 29 个 θ 会把基线重算 29 遍。
    """
    base: dict[tuple, float] = {}

    def stat_fn(variant, idx):
        uname, th = variant
        idx = np.asarray(idx)
        key = (uname, idx.tobytes())
        if key not in base:
            base[key] = d.worst_tv(d.pos[0.0][idx], uname)
        return d.worst_tv(d.pos[th][idx], uname) - base[key]

    return stat_fn


def run_one(d: Data, variants: list, label: str, n_perm: int, seed: int) -> dict:
    n = len(d.idx)
    res = selection_permutation_test(
        variants, n_obs=n, stat_fn=make_stat_fn(d),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=MIN_SHIFT, max_shift=n - MIN_SHIFT,
        statistic_name="worst_tv_gain_vs_theta0",
        meta={"scope": label, "cost_bps": COST_BPS, "carry": "none",
              "windows": list(STAT_WINDOWS)})
    w = res.variants[res.best_index]
    out = {
        "scope": label, "n_variants": len(variants), "n_perm": n_perm,
        "winner": f"{w[0]} @ θ={w[1]:+.2f}",
        "observed_gain": float(res.observed_best),
        "p_selected": float(res.p_selected),
        "p_naive": float(res.p_naive),
        "p_min_p": float(res.p_min_p),
        "selection_inflation": float(res.selection_inflation),
        "pass_gate": bool(res.p_selected < 0.05),
    }
    print(f"\n── {label}：{len(variants)} 点，n_perm={n_perm} ──")
    print(f"  观测赢家      = {out['winner']}")
    print(f"  观测增益      = {out['observed_gain']:+.4f}  (worst_tv 相对 θ=0)")
    print(f"  p_selected    = {out['p_selected']:.4f}   ← 唯一判据（<0.05 才算有东西）")
    print(f"  p_naive       = {out['p_naive']:.4f}   （对照·不入闸）")
    print(f"  p_min_p       = {out['p_min_p']:.4f}   （备用口径·不入闸）")
    print(f"  选择膨胀      = {out['selection_inflation']:.2f}×")
    print(f"  → {'过闸（调 θ 有东西）' if out['pass_gate'] else '不过闸（与噪声不可区分）'}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="阈值 θ 的选优校正检验（⓪ 机器）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--scope", choices=["blend", "all", "both"], default="both")
    args = ap.parse_args(argv)

    d = Data()
    print(f"样本 {len(d.idx)} 日（{d.idx.min().date()} → {d.idx.max().date()}），"
          f"θ 网格 {len(d.thetas)} 点，标的 {len(d.und)} 个")

    blend_key = next(k for k in d.und if k.startswith("blend"))
    results = []
    if args.scope in ("blend", "both"):
        results.append(run_one(d, [(blend_key, t) for t in d.thetas],
                               "A 部署口径 blend(500+1000)", args.n_perm, args.seed))
    if args.scope in ("all", "both"):
        results.append(run_one(d, [(u, t) for u in d.und for t in d.thetas],
                               "B 全空间（7 标的 × 29 θ）", args.n_perm, args.seed))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
