"""四对合成层权重网格（预登记 docs/plans/2026-09-09-pair-weighting-prereg.md）。
统计量 Δ|IC| vs 等权，argmax 选优，min-P/max-T 校正；收益层同秤日频对称引擎 worst(train,val) 门槛 +0.10。
CLI: python3 -m backtest.pair_weighting_probe --n-perm 1000
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.rotation_probe import nonoverlap_ic  # noqa: E402
from backtest.selection_permutation import adjusted_pvalue, build_index_matrix, make_stat_fn, selection_permutation_test  # noqa: E402

K, SMOOTH, LEVELS = 20, 5, (0, 1, 2)
TRAIN, VAL, HOLD = ("2015-04-16", "2020-12-31"), ("2021-01-01", "2023-12-31"), ("2024-01-01", "2026-09-03")
OUT_DIR = ROOT / "backtest" / "output"


def weight_grid(levels=LEVELS, n_pairs: int = 4) -> list[tuple[float, ...]]:
    """去全零、按比例去重（归一化后相同者只留一个）。"""
    seen, out = set(), []
    for w in itertools.product(levels, repeat=n_pairs):
        if sum(w) == 0:
            continue
        key = tuple(round(x / sum(w), 6) for x in w)
        if key not in seen:
            seen.add(key); out.append(key)
    return out


def combine(P: pd.DataFrame, w) -> pd.Series:
    return (P @ np.asarray(w, dtype=float)).rolling(SMOOTH, min_periods=1).mean()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--n-perm", type=int, default=1000); a = ap.parse_args(argv)
    from backtest.data import load_carry, load_underlying_returns
    from backtest.engine import run_strategy
    from backtest.metrics import sharpe
    d = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv", parse_dates=["date"]).set_index("date")
    P = d[[f"pair_0{i}_factor_20" for i in range(1, 5)]].dropna()
    und = load_underlying_returns("blend"); carry = load_carry("blend")
    idx = P.index.intersection(und.index).sort_values(); P = P.reindex(idx); ret = und.reindex(idx)
    grid = weight_grid(); eq = tuple(round(0.25, 6) for _ in range(4)); i_eq = grid.index(eq)
    Parr = P.to_numpy(float)

    # stat_fn：idx 为重抽样索引，四对同一移位后合成
    def stat(variant, ridx):
        Ps = Parr[ridx]
        sig = pd.Series(Ps @ np.asarray(variant), index=idx).rolling(SMOOTH, min_periods=1).mean()
        sig_eq = pd.Series(Ps @ np.asarray(eq), index=idx).rolling(SMOOTH, min_periods=1).mean()
        ic, _ = nonoverlap_ic(sig, ret, K); ic0, _ = nonoverlap_ic(sig_eq, ret, K)
        return abs(ic) - abs(ic0) if np.isfinite(ic) and np.isfinite(ic0) else -np.inf

    n = len(idx); im = build_index_matrix(n, a.n_perm, scheme="rotation", seed=0, min_shift=2 * K, max_shift=n - 2 * K)
    res = selection_permutation_test(grid, n_obs=n, stat_fn=stat, index_matrix=im, statistic_name="delta_abs_ic_vs_equal")
    win = res.best_index; w_win = grid[win]
    p_minp = adjusted_pvalue(res, win, "min_p"); p_maxt = adjusted_pvalue(res, win, "max_t")

    def sh(w, a_, b_):
        s = combine(P, w); s = s[(s.index >= a_) & (s.index <= b_)]; pos = np.sign(s)
        r = run_strategy(pos, und.reindex(pos.index), 3.0, carry)["ret"].dropna(); return float(sharpe(r))
    rows = []
    for j, w in enumerate(grid):
        tr, va, ho = sh(w, *TRAIN), sh(w, *VAL), sh(w, *HOLD)
        rows.append({"w": "/".join(f"{x:.2f}" for x in w), "delta_abs_ic": float(res.observed[j]), "sharpe_train": tr, "sharpe_val": va, "sharpe_holdout": ho, "worst_tv": min(tr, va), "is_equal": j == i_eq, "is_winner": j == win})
    panel = pd.DataFrame(rows).sort_values("delta_abs_ic", ascending=False)
    eq_row = panel[panel.is_equal].iloc[0]; win_row = panel[panel.is_winner].iloc[0]
    gate_ic = bool(p_minp < 0.05); gate_ret = bool(win_row.worst_tv >= eq_row.worst_tv + 0.10)
    meta = {"n_variants": len(grid), "n_obs": n, "n_perm": a.n_perm, "first": str(idx.min().date()), "last": str(idx.max().date()),
            "winner": {"w": win_row.w, "delta_abs_ic": float(res.observed_best), "p_min_p": p_minp, "p_max_t": p_maxt, "p_selected_machine": float(res.p_selected), "worst_tv": float(win_row.worst_tv), "holdout": float(win_row.sharpe_holdout)},
            "equal": {"worst_tv": float(eq_row.worst_tv), "holdout": float(eq_row.sharpe_holdout), "abs_ic_rank_of_equal": int((panel.delta_abs_ic > 0).sum()) + 1},
            "null_max_delta": {"q50": float(np.median(res.null_selected)), "q95": float(np.quantile(res.null_selected, 0.95))},
            "gate_ic_minp": gate_ic, "gate_return_worst_tv_plus_0.10": gate_ret, "PASS": gate_ic and gate_ret}
    panel.to_csv(OUT_DIR / "pair_weighting_panel.csv", index=False); (OUT_DIR / "pair_weighting_verdict.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    pd.set_option("display.width", 250); print(panel.head(8).round(4).to_string(index=False)); print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
