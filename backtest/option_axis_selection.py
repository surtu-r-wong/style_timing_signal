"""候选 ⓪ 应用于期权隐波面探针：每次置换重跑整套选优（5 族 × 4 形态 × 4 k = 80 变体）。

统计量 = 全窗非重叠 rank |IC|（双侧），选优 = argmax（max-statistic）。置换 = 循环移位信号侧，
移位量 ∈ [2·k_max, n − 2·k_max]（与 rotation_probe.shift_permutation_pvalue 同口径，取最大 k）。
产出 p_selected（网格最优 |IC| 的选择校正 p）+ 指定代表统计量在同一空分布下的 p。
CLI: python3 -m backtest.option_axis_selection [--n-perm 1000] [--rep O2_lb5zw60:40]
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

from backtest.option_axis_probe import FAMILIES_MAIN, GRID_K, build_option_signals  # noqa: E402
from backtest.rotation_probe import nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import make_stat_fn, selection_permutation_test  # noqa: E402

OUT = ROOT / "backtest" / "output" / "option_axis_selection.json"


def run(n_perm: int = 1000, seed: int = 0, rep: tuple[str, int] = ("O2_lb5zw60", 40), db=None) -> dict:
    from backtest.data import load_underlying_returns
    io = pd.read_csv(ROOT / "backtest" / "output" / "option_iv_IO.csv", parse_dates=["date"]).set_index("date")
    sigs = build_option_signals(io)
    und = load_underlying_returns("blend", db=db)
    idx = und.index
    for fam in FAMILIES_MAIN:
        for s in sigs[fam].values():
            idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values()
    ret = und.reindex(idx)
    arrays = {form: sigs[fam][form].reindex(idx).to_numpy(dtype=float) for fam in FAMILIES_MAIN for form in sigs[fam]}
    variants = [(form, k) for form in arrays for k in GRID_K]
    signals = {v: arrays[v[0]] for v in variants}

    def score(vals: np.ndarray, variant) -> float:
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), ret, variant[1])
        return abs(ic) if np.isfinite(ic) else -np.inf

    kmax = max(GRID_K); n = len(idx)
    res = selection_permutation_test(variants, n_obs=n, stat_fn=make_stat_fn(signals, score),
                                     n_perm=n_perm, seed=seed, scheme="rotation",
                                     min_shift=2 * kmax, max_shift=n - 2 * kmax,
                                     statistic_name="abs_rank_ic_full_window",
                                     meta={"families": list(FAMILIES_MAIN), "grid_k": list(GRID_K), "n_obs": n})
    rep_i = variants.index(rep)
    rep_obs = float(res.observed[rep_i])
    p_rep_vs_max_null = float((np.count_nonzero(res.null_selected >= rep_obs) + 1) / (len(res.null_selected) + 1))
    out = {"n_variants": len(variants), "n_perm": int(res.n_perm), "n_obs": n, "seed": seed,
           "first": str(idx.min().date()), "last": str(idx.max().date()),
           "winner": {"form": res.variants[res.best_index][0], "k": int(res.variants[res.best_index][1]), "abs_ic": float(res.observed_best)},
           "p_selected": float(res.p_selected), "p_naive_winner": float(res.p_naive), "p_min_p": float(res.p_min_p),
           "selection_inflation": float(res.selection_inflation),
           "null_max_abs_ic": {"q50": float(np.median(res.null_selected)), "q95": float(np.quantile(res.null_selected, 0.95)), "q99": float(np.quantile(res.null_selected, 0.99))},
           "representative": {"form": rep[0], "k": rep[1], "abs_ic": rep_obs, "p_vs_max_null": p_rep_vs_max_null,
                              "p_naive_column": float(res.p_naive_per_variant[rep_i])},
           "null_winner_counts_top": sorted(((f"{v[0]}:k{v[1]}", int(c)) for v, c in zip(variants, res.null_winner_counts)), key=lambda x: -x[1])[:8]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--n-perm", type=int, default=1000); ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rep", default="O2_lb5zw60:40"); a = ap.parse_args(argv)
    form, k = a.rep.split(":")
    out = run(a.n_perm, a.seed, (form, int(k)))
    print(json.dumps(out, ensure_ascii=False, indent=1)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
