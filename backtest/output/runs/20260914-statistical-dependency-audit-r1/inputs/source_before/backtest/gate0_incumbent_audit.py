"""关 0 门槛的**在位者审计**：现役 equal_weight 信号能否过它自己网格上的关 0？

缘起（2026-09-03）：资金流面 F1 三关全过、只败在关 0（|IC| 0.2599 vs 门槛 0.337）。
用户质疑「现役信号也过不了你这一系列要求」。本模块把同一台关 0 机器
（`selection_permutation_test`，max-T 口径）套到现役信号自己的参数网格上，直接检验该质疑。

网格：lookback ∈ {5,10,20,40}，z_window = 2×lookback（`generate_signal` 的内置规则），
smoothing = 5（现役值），× k ∈ {5,10,20,40} = 16 变体，含现役点 lb20zw40/k=20。
16 变体的门槛比资金流面的 32 变体**更宽松**。

重建校验：lb20zw40 的重建序列必须与部署的 `equal_weight_signal_20d40z.csv` 逐点一致
（实测 corr=1.000000，max|diff|=5.0e-05），否则审计对象就不是现役信号。

**口径边界**：本审计只回答「同一把尺子量在位者会怎样」，**不是**对现役信号有效性的再检验——
关 0 是发现阶段的多重检验校正，现役信号的证据基础是实盘部署 + 跨确认窗，两者不同类。

CLI: python3 -m backtest.gate0_incumbent_audit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOOKBACKS = (5, 10, 20, 40)
GRID_K = (5, 10, 20, 40)
INCUMBENT_FORM = "EW_lb20zw40"
INCUMBENT_K = 20
OUT = ROOT / "backtest" / "output" / "gate0_incumbent_audit.json"


def build_grid(prices, cfgs) -> dict[str, pd.Series]:
    from signals.equal_weight.generate_signal import calculate_contrast_equal_weight_signal
    forms = {}
    for lb in LOOKBACKS:
        out = calculate_contrast_equal_weight_signal(
            prices, lookback=lb, z_window=2 * lb, smoothing_window=5, pair_configs=cfgs)
        forms[f"EW_lb{lb}zw{2 * lb}"] = out["factor_value"].iloc[2 * lb - 1:]
    return forms


def main(argv=None) -> int:
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import load_pair_configs
    from backtest.rotation_probe import nonoverlap_ic, _load_ew_signal
    from backtest.selection_permutation import make_stat_fn, selection_permutation_test
    from backtest.data import load_underlying_returns
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

    ref = _load_ew_signal().reindex(idx)
    rebuilt = forms[INCUMBENT_FORM].reindex(idx)
    corr, maxdiff = float(rebuilt.corr(ref)), float((rebuilt - ref).abs().max())
    print(f"评窗 {len(idx)} 日 {idx.min().date()}..{idx.max().date()}")
    print(f"重建 {INCUMBENT_FORM} vs 部署序列: corr={corr:.6f} max|diff|={maxdiff:.2e}")
    if corr < 0.999 or maxdiff > 1e-3:
        raise SystemExit(f"重建与部署序列不一致（corr={corr}, maxdiff={maxdiff}）——审计对象不是现役信号")

    arrays = {f: s.reindex(idx).to_numpy(float) for f, s in forms.items()}
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
    q95 = float(np.quantile(res.null_selected, 0.95))
    ks = np.array([v[1] for v in variants])
    ic_map = {f"{f}_k{k}": float(abs(nonoverlap_ic(pd.Series(arrays[f], index=idx), ret, k)[0]))
              for (f, k) in variants}

    print(f"\n{'变体':>16} {'k':>4} {'|IC|':>8}")
    for (f, k) in variants:
        mark = "   ← 现役点" if (f == INCUMBENT_FORM and k == INCUMBENT_K) else ""
        print(f"{f:>16} {k:>4} {ic_map[f'{f}_k{k}']:>8.4f}{mark}")

    dep = ic_map[f"{INCUMBENT_FORM}_k{INCUMBENT_K}"]
    print(f"\n关 0（{len(variants)} 变体 max-T，1000 置换）")
    print(f"  空分布 q50={np.median(res.null_selected):.4f}  q95={q95:.4f}")
    print("  分层 q95: " + "  ".join(
        f"k={k}:{np.quantile(res.null_stats[:, ks == k].max(axis=1), 0.95):.3f}" for k in GRID_K))
    print(f"  网格最优 {res.variants[res.best_index]} |IC|={res.observed_best:.4f}"
          f"  p_selected={res.p_selected:.4f}  p_min_p={res.p_min_p:.4f}")
    print(f"\n  ★ 现役点 {INCUMBENT_FORM} k={INCUMBENT_K}: |IC|={dep:.4f} vs q95={q95:.4f}"
          f"  →  {'过' if dep >= q95 else '不过'}")

    OUT.write_text(json.dumps({
        "window": {"n": int(len(idx)), "first": str(idx.min().date()), "last": str(idx.max().date())},
        "rebuild_check": {"corr_vs_deployed": corr, "max_abs_diff": maxdiff},
        "grid": {"n_variants": len(variants), "lookbacks": list(LOOKBACKS), "k": list(GRID_K)},
        "ic": ic_map,
        "gate0": {"q50": float(np.median(res.null_selected)), "q95": q95,
                  "q95_by_k": {int(k): float(np.quantile(res.null_stats[:, ks == k].max(axis=1), 0.95))
                               for k in GRID_K},
                  "grid_best": {"variant": list(res.variants[res.best_index]),
                                "abs_ic": float(res.observed_best)},
                  "p_selected": float(res.p_selected), "p_min_p": float(res.p_min_p)},
        "incumbent": {"variant": INCUMBENT_FORM, "k": INCUMBENT_K, "abs_ic": dep,
                      "passes_gate0": bool(dep >= q95)},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
