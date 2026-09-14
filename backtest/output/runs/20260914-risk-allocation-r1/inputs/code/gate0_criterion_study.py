"""关 0 口径论证：max-T（`p_selected`）与 Westfall–Young min-P（`p_min_p`）该选哪个。

缘起（2026-09-03）：资金流面 F1 与现役 equal_weight 信号在各自网格上，两个口径给出相反结论
（现役网格 p_selected=0.293 不显著 / p_min_p=0.013 显著）。在位者审计
（`2026-09-03-gate0-incumbent-audit.md`）确认门槛的不对称成立，但**不对称本身不能说明该换哪个**——
换口径必须先证明新口径的检验水平是对的，否则只是把门槛调松。

## 方法：用真实联合零分布做水平与功效研究

`selection_permutation_test` 的 `null_stats` 是 (B, J) 的置换统计量矩阵——**B 次对真实联合零分布的抽样**，
保留了变体之间真实的相依结构（4 个形态高度相关、4 个 k 的噪声水平差 3 倍）。据此：

- **水平（size）**：置换行在零假设下可交换，故任取一行当「观测」、其余当参照，
  算出的 p 在零假设下应服从均匀分布。测 P(p ≤ α) 是否等于 α。
  这是对两个口径**假阳率**的直接测量，不依赖任何合成数据的分布假设。
- **功效（power）**：把某一档 k 的各列整体抬高 δ（位置移动型备择假设：真效应叠加在同一份噪声上），
  再对未改动的参照算 p，测检出率随 δ 的变化。分别把效应放在快档（k=5）与慢档（k=40），
  直接回答「效应落在不同跨度时哪个口径找得到」。

两个 p 的公式逐字复用 `selection_permutation.column_pvalues` 的房规，与生产口径同源。

两个网格独立验证：资金流面 32 变体（2,715 日）与现役 equal_weight 16 变体（3,003 日）。

CLI: python3 -m backtest.gate0_criterion_study [--grid moneyflow|incumbent|both]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "backtest" / "output" / "gate0_criterion_study.json"
ALPHAS = (0.05, 0.10)
DELTAS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20)


def _col_p(pool: np.ndarray) -> np.ndarray:
    """池内逐列置换 p（`p[r,j] = #{r': pool[r',j] >= pool[r,j]} / m`），与生产同口径。"""
    m, n_var = pool.shape
    out = np.empty((m, n_var), dtype=float)
    for j in range(n_var):
        col = pool[:, j]
        order = np.argsort(-col, kind="mergesort")
        ranks = np.empty(m, dtype=float)
        srt = col[order]
        i = 0
        while i < m:                                   # 并列按最保守（同值同 p）
            j2 = i
            while j2 + 1 < m and srt[j2 + 1] == srt[i]:
                j2 += 1
            ranks[order[i:j2 + 1]] = j2 + 1
            i = j2 + 1
        out[:, j] = ranks / m
    return out


def two_pvalues(obs_row: np.ndarray, ref: np.ndarray) -> tuple[float, float]:
    """把 obs_row 当观测、ref 当置换池，返回 (p_selected, p_min_p)。"""
    pool = np.vstack([obs_row[None, :], ref])
    m = pool.shape[0]
    sel = pool.max(axis=1)                              # select_fn = argmax
    p_selected = float(np.count_nonzero(sel >= sel[0]) / m)
    minp = _col_p(pool).min(axis=1)
    p_min_p = float(np.count_nonzero(minp <= minp[0]) / m)
    return p_selected, p_min_p


def size_study(null_stats: np.ndarray) -> dict:
    """留一法：每个置换行轮流当观测，测两个口径的实际检验水平。"""
    B = null_stats.shape[0]
    ps, pm = np.empty(B), np.empty(B)
    for i in range(B):
        ref = np.delete(null_stats, i, axis=0)
        ps[i], pm[i] = two_pvalues(null_stats[i], ref)
    return {"n_draws": int(B),
            "p_selected": {f"size_at_{a}": float((ps <= a).mean()) for a in ALPHAS},
            "p_min_p": {f"size_at_{a}": float((pm <= a).mean()) for a in ALPHAS},
            "p_selected_mean": float(ps.mean()), "p_min_p_mean": float(pm.mean())}


def power_study(null_stats: np.ndarray, ks: np.ndarray, target_k: int, alpha: float = 0.05) -> dict:
    """把 target_k 那一档的列整体抬高 δ，测两个口径的检出率。"""
    B = null_stats.shape[0]
    mask = ks == target_k
    rows = []
    for d in DELTAS:
        hit_s = hit_m = 0
        for i in range(B):
            ref = np.delete(null_stats, i, axis=0)
            obs = null_stats[i].copy()
            obs[mask] += d
            a, b = two_pvalues(obs, ref)
            hit_s += a <= alpha
            hit_m += b <= alpha
        rows.append({"delta": d, "power_p_selected": hit_s / B, "power_p_min_p": hit_m / B})
    return {"target_k": int(target_k), "alpha": alpha, "curve": rows}


def run(null_stats: np.ndarray, ks: np.ndarray, label: str) -> dict:
    print(f"\n===== {label}（B={null_stats.shape[0]}, J={null_stats.shape[1]}）=====")
    size = size_study(null_stats)
    print("检验水平（零假设下 P(p ≤ α)，应等于 α）:")
    for a in ALPHAS:
        print(f"  α={a}:  max-T {size['p_selected'][f'size_at_{a}']:.3f}   "
              f"min-P {size['p_min_p'][f'size_at_{a}']:.3f}")
    out = {"label": label, "size": size, "power": {}}
    for tk in sorted(set(int(k) for k in ks)):
        pw = power_study(null_stats, ks, int(tk))
        out["power"][f"k{int(tk)}"] = pw
        print(f"\n功效（效应放在 k={int(tk)} 那一档，α=0.05）:")
        print("   δ    max-T   min-P")
        for r in pw["curve"]:
            print(f"  {r['delta']:.2f}   {r['power_p_selected']:.3f}   {r['power_p_min_p']:.3f}")
    return out


def _moneyflow_null() -> tuple[np.ndarray, np.ndarray]:
    import pandas as pd
    from backtest.rotation_probe import nonoverlap_ic
    from backtest.selection_permutation import make_stat_fn, selection_permutation_test
    from backtest.data import load_underlying_returns
    from backtest.money_flow_series import build_series
    from backtest.money_flow_axis_probe import build_signals, FAMILIES, GRID_K

    series = build_series()
    sigs = build_signals(series)
    und = load_underlying_returns("blend")
    idx = und.index
    for fam in FAMILIES:
        for s in sigs[fam].values():
            idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values()
    ret = und.reindex(idx)
    arrays = {form: sigs[fam][form].reindex(idx).to_numpy(float) for fam in FAMILIES for form in sigs[fam]}
    variants = [(f, k) for f in arrays for k in GRID_K]
    signals = {v: arrays[v[0]] for v in variants}

    def score(vals, v):
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), ret, v[1])
        return abs(ic) if np.isfinite(ic) else -np.inf

    kmax, n = max(GRID_K), len(idx)
    res = selection_permutation_test(
        variants, n_obs=n, stat_fn=make_stat_fn(signals, score), n_perm=1000, seed=0,
        scheme="rotation", min_shift=2 * kmax, max_shift=n - 2 * kmax,
        statistic_name="abs_rank_ic_full_window")
    return res.null_stats, np.array([v[1] for v in variants])


def _incumbent_null() -> tuple[np.ndarray, np.ndarray]:
    import pandas as pd
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import load_pair_configs
    from backtest.rotation_probe import nonoverlap_ic
    from backtest.selection_permutation import make_stat_fn, selection_permutation_test
    from backtest.data import load_underlying_returns
    from backtest.gate0_incumbent_audit import build_grid, GRID_K as IK
    import backtest.divergence_probe as dp

    prices = load_pg_closes(dp.INDEX_NAMES, start="2014-01-01")
    cfgs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    forms = build_grid(prices, cfgs)
    und = load_underlying_returns("blend")
    idx = und.index
    for s in forms.values():
        idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values()
    ret = und.reindex(idx)
    arrays = {f: s.reindex(idx).to_numpy(float) for f, s in forms.items()}
    variants = [(f, k) for f in arrays for k in IK]
    signals = {v: arrays[v[0]] for v in variants}

    def score(vals, v):
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), ret, v[1])
        return abs(ic) if np.isfinite(ic) else -np.inf

    kmax, n = max(IK), len(idx)
    res = selection_permutation_test(
        variants, n_obs=n, stat_fn=make_stat_fn(signals, score), n_perm=1000, seed=0,
        scheme="rotation", min_shift=2 * kmax, max_shift=n - 2 * kmax,
        statistic_name="abs_rank_ic_full_window")
    return res.null_stats, np.array([v[1] for v in variants])


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", choices=("moneyflow", "incumbent", "both"), default="both")
    a = ap.parse_args(argv)
    reports = {}
    if a.grid in ("moneyflow", "both"):
        ns, ks = _moneyflow_null()
        reports["moneyflow"] = run(ns, ks, "资金流面网格（32 变体，2,715 日）")
    if a.grid in ("incumbent", "both"):
        ns, ks = _incumbent_null()
        reports["incumbent"] = run(ns, ks, "现役 equal_weight 网格（16 变体，3,003 日）")
    # 增量合并：只覆盖本次跑的网格，保留另一个网格已有的结果
    prev = {}
    if OUT.exists():
        try:
            loaded = json.loads(OUT.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev = {k: v for k, v in loaded.items() if k in ("moneyflow", "incumbent")}
        except json.JSONDecodeError:
            prev = {}
    OUT.write_text(json.dumps({**prev, **reports}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
