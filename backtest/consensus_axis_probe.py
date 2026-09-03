"""分析师一致预期修正面·入场券探针（预登记 docs/plans/2026-09-03-consensus-revision-axis-prereg.md，冻结后才可 --run）。

四族 R1 广度 / R2 幅度 / R3 广度 diff20 / R4 规模差（300−1000）× GRID_LEVEL × k∈{5,10,20,40} = 64 变体；
两半窗 2020-2022 / 2023-2026（有效段止 2026-05-29）；关 1~3 = run_families_probe 原样；
关 0 = 候选 ⓪（64 变体全窗 |IC| argmax 的循环移位空分布，代表 |IC| ≥ 95% 分位才过）。
CLI: python3 -m backtest.consensus_axis_probe --run [--n-perm 1000]
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

from backtest.consensus_revision import build_series  # noqa: E402
from backtest.leverage_probe import GRID_LEVEL, level_signal, run_families_probe  # noqa: E402
from backtest.rotation_probe import nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import make_stat_fn, selection_permutation_test  # noqa: E402

GRID_K = (5, 10, 20, 40)
FAMILIES = ("R1", "R2", "R3", "R4")
DATA_END = "2026-05-29"           # 预登记 §6：2026-06 以后三个孤立日不进入探针
HALVES = {"2020-2022": ("2020-01-01", "2022-12-31"), "2023-2026": ("2023-01-01", DATA_END)}
OUT_DIR = ROOT / "backtest" / "output"


def build_signals(series: pd.DataFrame) -> dict[str, dict[str, pd.Series]]:
    s = series.loc[:DATA_END]
    return {fam: {f"{fam}_lb{lb}zw{zw}": level_signal(s[fam].dropna(), lb, zw) for lb, zw in GRID_LEVEL} for fam in FAMILIES}


def gate0(sigs: dict, und: pd.Series, verdicts: pd.DataFrame, n_perm: int, seed: int = 0) -> tuple[pd.DataFrame, dict]:
    idx = und.index
    for fam in FAMILIES:
        for s in sigs[fam].values():
            idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values(); ret = und.reindex(idx)
    arrays = {form: sigs[fam][form].reindex(idx).to_numpy(dtype=float) for fam in FAMILIES for form in sigs[fam]}
    variants = [(form, k) for form in arrays for k in GRID_K]
    signals = {v: arrays[v[0]] for v in variants}

    def score(vals, variant):
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), ret, variant[1])
        return abs(ic) if np.isfinite(ic) else -np.inf

    kmax = max(GRID_K); n = len(idx)
    res = selection_permutation_test(variants, n_obs=n, stat_fn=make_stat_fn(signals, score), n_perm=n_perm, seed=seed,
                                     scheme="rotation", min_shift=2 * kmax, max_shift=n - 2 * kmax, statistic_name="abs_rank_ic_full_window")
    q95 = float(np.quantile(res.null_selected, 0.95))
    rows = []
    for _, v in verdicts.iterrows():
        key = (str(v["best_form"]), int(v["best_k"])); i = variants.index(key); obs = float(res.observed[i])
        p = float((np.count_nonzero(res.null_selected >= obs) + 1) / (len(res.null_selected) + 1))
        rows.append({"family": v["family"], "rep_abs_ic": obs, "null_max_q95": q95, "p_vs_max_null": p, "gate0_selection_corrected": bool(obs >= q95)})
    g0 = pd.DataFrame(rows)
    meta = {"n_variants": len(variants), "n_perm": int(res.n_perm), "n_obs": n, "first": str(idx.min().date()), "last": str(idx.max().date()),
            "grid_winner": {"form": res.variants[res.best_index][0], "k": int(res.variants[res.best_index][1]), "abs_ic": float(res.observed_best)},
            "p_selected_grid_max": float(res.p_selected), "p_min_p": float(res.p_min_p), "selection_inflation": float(res.selection_inflation),
            "null_max_abs_ic": {"q50": float(np.median(res.null_selected)), "q95": q95, "q99": float(np.quantile(res.null_selected, 0.99))}}
    return g0, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0); a = ap.parse_args(argv)
    series = build_series()
    if not a.run:
        print(series.loc[:DATA_END, ["R1", "R2", "R3", "R4"]].describe().round(4).to_string()); return 0
    from backtest.data import load_underlying_returns
    sigs = build_signals(series)
    panel, verdicts = run_families_probe(sigs, FAMILIES, GRID_K, a.n_perm, a.cost_bps, halves=HALVES)
    und = load_underlying_returns("blend")
    g0, meta = gate0(sigs, und, verdicts, a.n_perm)
    verdicts = verdicts.merge(g0, on="family")
    verdicts["PASS_probe"] = verdicts["PASS"]
    verdicts["PASS"] = verdicts["PASS_probe"] & verdicts["gate0_selection_corrected"]
    panel.to_csv(OUT_DIR / "consensus_axis_probe.csv", index=False)
    verdicts.to_csv(OUT_DIR / "consensus_axis_probe_verdicts.csv", index=False)
    (OUT_DIR / "consensus_axis_selection.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    pd.set_option("display.width", 260)
    print(verdicts.round(4).to_string(index=False)); print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
