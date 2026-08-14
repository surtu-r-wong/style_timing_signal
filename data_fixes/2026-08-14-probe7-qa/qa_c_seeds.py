"""QA C：换 seed（0 复现 + 1/2 新种子）复跑两族两台置换机器。

判据内稳健性检查：p_selected 是否维持"远不显著"的同判；顺带核对 seed=0 与产物
逐位可复现、零分布"长 k 当选占比"。用实现自身的信号构建与 ⓪ 机器（不是重判）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))

from backtest.data import load_db_config, load_underlying_returns  # noqa: E402
from backtest.divergence_probe import (  # noqa: E402
    SELECTION, abs_partial_batch, abs_spearman_batch, build_score_frames,
    make_batch_scorer, p_at_threshold,
)
from backtest.fund_crowding_probe import (  # noqa: E402
    A_GRID_K, B_GRID_K, PRIMARY_KOU_JING, a_variant_grid, b_variant_grid,
    build_a_signals, build_b_signals, build_beta_daily, build_quarterly,
    family_min_shift,
)
from backtest.rotation_probe import _load_ew_signal  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

REF = {  # 产物 seed=0 基准
    "B": {"p_sel_ic": 0.4035964035964036, "p_naive_ic": 0.2087912087912088,
          "p_minp_ic": 0.44255744255744256, "p_sel_pic_at_winner": 0.42357642357642356,
          "winner": ("size", 240, 500, 40)},
    "A": {"p_sel_ic": 0.6473526473526473, "p_naive_ic": 0.27172827172827174,
          "p_minp_ic": 0.7212787212787213, "p_sel_pic_at_winner": 0.6053946053946054,
          "winner": ("A2", "expanding", "annP95", 40)},
}


def main():
    db = load_db_config()
    beta = build_beta_daily(db=db)          # 走缓存
    q = build_quarterly(db=db)              # 走缓存
    ew = _load_ew_signal()
    und = load_underlying_returns(PRIMARY_KOU_JING, db=db)

    fams = {
        "B": (b_variant_grid(), build_b_signals(beta), B_GRID_K),
        "A": (a_variant_grid(), build_a_signals(q, und.index), A_GRID_K),
    }
    for name, (variants, signals, grid_k) in fams.items():
        common = None
        for v in variants:
            idx = signals[v].dropna().index
            common = idx if common is None else common.intersection(idx)
        common = (common.intersection(und.index)
                  .intersection(ew.dropna().index).sort_values())
        sel = common[(common >= pd.Timestamp(SELECTION[0]))
                     & (common <= pd.Timestamp(SELECTION[1]))]
        n_obs = len(sel)
        ms = family_min_shift(grid_k)
        frames = build_score_frames(und, ew, sel, grid_k=tuple(grid_k))
        arrs = {v: signals[v].reindex(sel).to_numpy(dtype=float) for v in variants}
        print(f"\n=== 族 {name}: n_obs={n_obs}, min_shift={ms}, max_shift={n_obs - ms} ===")
        for seed in (0, 1, 2):
            r_ic = selection_permutation_test(
                variants, n_obs=n_obs,
                batch_stat_fn=make_batch_scorer(arrs, frames, abs_spearman_batch),
                n_perm=1000, seed=seed, scheme="rotation",
                min_shift=ms, max_shift=n_obs - ms)
            r_pic = selection_permutation_test(
                variants, n_obs=n_obs,
                batch_stat_fn=make_batch_scorer(arrs, frames, abs_partial_batch),
                n_perm=1000, seed=seed, scheme="rotation",
                min_shift=ms, max_shift=n_obs - ms)
            p_pic_w = p_at_threshold(r_pic.null_selected, r_pic.observed[r_ic.best_index])
            # 长 k 当选占比
            kmax = max(grid_k)
            share = sum(c for v, c in zip(r_ic.variants, r_ic.null_winner_counts)
                        if v[3] == kmax)
            print(f"  seed={seed}: winner={r_ic.best_variant} |IC|={r_ic.observed_best:.6f} "
                  f"p_sel={r_ic.p_selected:.4f} p_naive={r_ic.p_naive:.4f} "
                  f"p_minp={r_ic.p_min_p:.4f} | pic@winner p={p_pic_w:.4f} "
                  f"| null 赢家 k={kmax} 占 {share}/1000")
            if seed == 0:
                ref = REF[name]
                checks = [
                    ("p_sel_ic", r_ic.p_selected), ("p_naive_ic", r_ic.p_naive),
                    ("p_minp_ic", r_ic.p_min_p), ("p_sel_pic_at_winner", p_pic_w)]
                for k2, v in checks:
                    assert abs(v - ref[k2]) < 1e-12, (name, k2, v, ref[k2])
                assert r_ic.best_variant == ref["winner"]
                print(f"    [复现] seed=0 与产物逐位一致 ✓")
            assert r_ic.p_selected > 0.025 and p_pic_w > 0.025, "换 seed 后判定翻转?!"
    print("\n[DONE] qa_c_seeds：全部 seed 下 关1/关1b(p) 均不过 α=0.025，STOP 判定稳健")


if __name__ == "__main__":
    main()
