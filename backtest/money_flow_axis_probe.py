"""资金流向面·入场券探针（预登记 docs/plans/2026-09-03-money-flow-axis-prereg.md，冻结后才可 --run）。
两族 F1/F2 × GRID_LEVEL × k = 32 变体；两半窗 2015-2020/2021-2026（数据起点 2015-01，
预登记 §3；初稿的 2014-2019 因此作废）；关 1~3 原样 + 关 0（候选 ⓪）。
CLI: python3 -m backtest.money_flow_axis_probe --run [--n-perm 1000]
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

from backtest.money_flow_series import build_series  # noqa: E402
from backtest.leverage_probe import GRID_LEVEL, level_signal, run_families_probe  # noqa: E402

# 资金流数据自 2015-01-05，250 日 beta 预热后首个可用日 2015-07-02；按可用样本中点切两半窗。
HALVES = {"2015-2020": ("2015-07-01", "2020-12-31"), "2021-2026": ("2021-01-01", "2026-12-31")}
from backtest.rotation_probe import nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import make_stat_fn, selection_permutation_test  # noqa: E402

GRID_K = (5, 10, 20, 40)
FAMILIES = ("F1", "F2")
OUT_DIR = ROOT / "backtest" / "output"


def build_signals(series: pd.DataFrame) -> dict[str, dict[str, pd.Series]]:
    return {fam: {f"{fam}_lb{lb}zw{zw}": level_signal(series[fam].dropna(), lb, zw) for lb, zw in GRID_LEVEL} for fam in FAMILIES}


# 关 0 口径（2026-09-03 补注）：本探针用的是 **max-T**（下方 `obs >= q95`）。
# 它不是选出来的，是从 `new_high_axis_probe` 起逐个克隆下来的默认。口径论证见
# `docs/plans/2026-09-03-gate0-criterion-argument.md`：max-T 对落在低噪声档的真效应
# 功效等于零假设本底，此后**默认改用 min-P**（`selection_permutation.adjusted_pvalue`
# 的 criterion 参数无默认值，强制显式选）。
# 本行注释只是把既成事实记下来，**不改本探针的计算，也不改任何已登记裁决**。
GATE0_CRITERION = "max_t"   # 本次运行实际使用的口径（历史默认，非论证后选择）


def gate0(sigs, und, verdicts, n_perm, seed=0):
    idx = und.index
    for fam in FAMILIES:
        for s in sigs[fam].values():
            idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values(); ret = und.reindex(idx)
    arrays = {form: sigs[fam][form].reindex(idx).to_numpy(dtype=float) for fam in FAMILIES for form in sigs[fam]}
    variants = [(form, k) for form in arrays for k in GRID_K]; signals = {v: arrays[v[0]] for v in variants}

    def score(vals, variant):
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), ret, variant[1]); return abs(ic) if np.isfinite(ic) else -np.inf
    kmax = max(GRID_K); n = len(idx)
    res = selection_permutation_test(variants, n_obs=n, stat_fn=make_stat_fn(signals, score), n_perm=n_perm, seed=seed, scheme="rotation",
                                     min_shift=2 * kmax, max_shift=n - 2 * kmax, statistic_name="abs_rank_ic_full_window")
    q95 = float(np.quantile(res.null_selected, 0.95)); rows = []
    for _, v in verdicts.iterrows():
        i = variants.index((str(v["best_form"]), int(v["best_k"]))); obs = float(res.observed[i])
        p = float((np.count_nonzero(res.null_selected >= obs) + 1) / (len(res.null_selected) + 1))
        rows.append({"family": v["family"], "rep_abs_ic": obs, "null_max_q95": q95, "p_vs_max_null": p, "gate0_selection_corrected": bool(obs >= q95)})
    meta = {"n_variants": len(variants), "n_perm": int(res.n_perm), "n_obs": n, "first": str(idx.min().date()), "last": str(idx.max().date()),
            "grid_winner": {"form": res.variants[res.best_index][0], "k": int(res.variants[res.best_index][1]), "abs_ic": float(res.observed_best)},
            "p_selected_grid_max": float(res.p_selected), "p_min_p": float(res.p_min_p), "selection_inflation": float(res.selection_inflation),
            "null_max_abs_ic": {"q50": float(np.median(res.null_selected)), "q95": q95, "q99": float(np.quantile(res.null_selected, 0.99))}}
    return pd.DataFrame(rows), meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0); ap.add_argument("--force", action="store_true"); a = ap.parse_args(argv)
    series = build_series(force=a.force)
    if not a.run:
        print(series[["F1", "F2"]].describe().round(4).to_string()); print("nan:", series[["F1", "F2"]].isna().sum().to_dict()); return 0
    from backtest.data import load_underlying_returns
    sigs = build_signals(series)
    panel, verdicts = run_families_probe(sigs, FAMILIES, GRID_K, a.n_perm, a.cost_bps, halves=HALVES)
    g0, meta = gate0(sigs, load_underlying_returns("blend"), verdicts, a.n_perm)
    verdicts = verdicts.merge(g0, on="family"); verdicts["PASS_probe"] = verdicts["PASS"]
    verdicts["PASS"] = verdicts["PASS_probe"] & verdicts["gate0_selection_corrected"]
    panel.to_csv(OUT_DIR / "money_flow_axis_probe.csv", index=False); verdicts.to_csv(OUT_DIR / "money_flow_axis_probe_verdicts.csv", index=False)
    (OUT_DIR / "money_flow_axis_selection.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    pd.set_option("display.width", 260); print(verdicts.round(4).to_string(index=False)); print(json.dumps(meta, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
