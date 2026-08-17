"""候选⑧ 双通道 —— **换统计量重跑**：配对非偏 rank IC 替代 Sharpe 差。

## 为什么换秤（P0 裁决，2026-08-17）

Sharpe 差口径的正式跑 verdict = STOP，卡在 `p_selected = 0.857`。诊断
（`2026-08-17-dual-channel-formal-results.md` §2）指出根因是**功效不足**：置换后
**标的的静态差异仍在**（中证2000 与 blend 的长期风险收益本就不同），零分布不以 0 为
中心且很宽 —— 44% 的置换样本能造出 ≥ +0.0666 的 Sharpe 差。连 `p_naive` 都是 0.441，
即**不扫网格也不显著**，所以问题不是选优惩罚，是统计量选错了。

rank IC 直接测「信号 ↔ 未来收益」的**序相关**，与标的的均值/波动水平无关，因此
**剥离了那个把零分布撑宽的静态差异**。这是本项目检验"替换命题"的既定秤
（memory：替换命题闸门 = 同秤头对头非偏 IC；⑤/⑥ 闸门⓪ 用的就是它）。

## 一个必须声明的口径后果

**IC 口径测不到 carry。** 候选优势有两个来源：(1) 现货腿用中证2000 → 信号在小盘上
更有效，这是**预测力问题**，IC 能测；(2) 期货腿用中证500 → 吃 IC 贴水，carry 单独
贡献 +0.40 Sharpe，这是**执行层的结构性收益**，与"信号预测得准不准"无关，IC 测不到
也不该测（"IC 贴水比 IM 深"是可观测事实，不是选优发现，不需要 selection-aware 检验）。

所以本模块回答的是**窄一点但更干净的问题**：**换标的是否让同一套信号预测得更准？**
目标序列一律用**不含 carry 的标的组合收益**：

    候选 = 0.4·r_2000 + 0.6·r_500        现役 = 0.5·r_500 + 0.5·r_1000

## 自由度（承接冻结的预登记，只改统计量）

  * 信号 = 现役 `equal_weight` 的 `factor_value`（**连续值**，非 0/1 仓位 —— IC 的
    标准用法，且连续因子功效更高）
  * **k=1 为唯一主判据**：现役是逐日调仓、无持有期，k=1 是唯一贴合部署的选择。
    k ∈ {5, 20} 仅作报告列，**不进闸门**（防止"k=1 不显著就改用 k=20"这种事后选优）
  * 网格仍是 15 格通道配对（同一选优空间，理由见预登记 §4 决议 2）
  * ⓪ 机器同参数：rotation、min_shift=60、n_perm=1000、seed=0
  * 闸门沿用问 1 裁决 (a) 的形状：IC 差 > 0 且 `p_selected` < 0.05 且
    `paired_ic_bootstrap` 95% CI 下沿 > 0

## 实现技巧（Spearman 对称性）

既有 `paired_ic_bootstrap(x_a, x_b, y)` 测 `IC(x_a,y) − IC(x_b,y)`（两因子 vs 一目标），
而这里要的是 `IC(x, y_a) − IC(x, y_b)`（一因子 vs 两目标）。因为 **Spearman 对称**，
把两条目标收益当 `x_a/x_b`、把信号当 `y` 传进去即完全等价 —— 零新代码复用既有机器。

    python3 -m backtest.dual_channel_ic
    python3 -m backtest.dual_channel_ic --n-perm 20 --n-boot 500   # 自检
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

from backtest.dual_channel_probe import FUT_LEGS, SPOT_LEGS  # noqa: E402
from backtest.fusion_probe import (  # noqa: E402
    forward_return, nonoverlap_grid, paired_ic_bootstrap, rank_ic,
)
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402
from backtest.underlying_probe import load_index_returns  # noqa: E402

W_SPOT, W_FUT = 0.4, 0.6
MIN_SHIFT = 60
K_MAIN = 1                    # 唯一主判据
K_REPORT = (5, 20)            # 仅报告列
OUT_DIR = ROOT / "backtest" / "output"


def build_targets() -> tuple[pd.Series, dict[tuple, pd.Series], pd.Series]:
    """(信号, {配对: 候选标的组合收益}, 现役标的组合收益) —— 全部不含 carry。"""
    _, signal = load_two_columns()
    spot = {k: load_index_returns(c) for k, (c, _) in SPOT_LEGS.items()}
    fut = {k: load_index_returns(c) for k, (c, _, _) in FUT_LEGS.items()}

    idx = signal.index
    for s in list(spot.values()) + list(fut.values()):
        idx = idx.intersection(s.index)
    idx = idx.sort_values()

    cands = {}
    for sk in SPOT_LEGS:
        for fk in FUT_LEGS:
            cands[(sk, fk)] = (W_SPOT * spot[sk].reindex(idx)
                               + W_FUT * fut[fk].reindex(idx))
    inc = 0.5 * fut["500"].reindex(idx) + 0.5 * fut["1000"].reindex(idx)
    return signal.reindex(idx), cands, inc


def ic_frame(signal: pd.Series, target: pd.Series, k: int, offset: int = 0):
    """对齐到非重叠网格的 (因子, 前瞻收益) 两列，已 dropna。"""
    fwd = forward_return(target, k)
    df = pd.concat([signal.rename("x"), fwd.rename("y")], axis=1).dropna()
    grid = nonoverlap_grid(df.index, k, offset)
    return df.loc[grid, "x"], df.loc[grid, "y"]


def make_stat_fn(signal: pd.Series, cands: dict, inc: pd.Series, k: int):
    """`stat_fn(pair, idx)` = IC(候选) − IC(现役)，置换作用于**信号**。

    先把 forward return 与非重叠网格算好并缓存（它们不随置换变化），置换只重排信号
    —— 这样每次置换只做两次 Spearman，不重算 rolling。
    """
    from scipy import stats as _st

    prepared = {}
    for pair, tgt in cands.items():
        x_c, y_c = ic_frame(signal, tgt, k)
        x_i, y_i = ic_frame(signal, inc, k)
        common = x_c.index.intersection(x_i.index)   # 同秤：两边同一批观测点
        prepared[pair] = (y_c.reindex(common).to_numpy(),
                          y_i.reindex(common).to_numpy(),
                          signal.reindex(common).to_numpy(), common)

    def stat_fn(pair, idx):
        y_c, y_i, x_obs, common = prepared[pair]
        # idx 是**全样本**长度的重排索引；映射到本配对的观测子集上
        x_perm = signal.to_numpy()[idx]
        x_ser = pd.Series(x_perm, index=signal.index).reindex(common).to_numpy()
        return (float(_st.spearmanr(x_ser, y_c).statistic)
                - float(_st.spearmanr(x_ser, y_i).statistic))

    return stat_fn, prepared


def main() -> int:
    ap = argparse.ArgumentParser(description="候选⑧ IC 口径重跑（P0 裁决）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    print("候选⑧ 双通道 —— 配对非偏 rank IC 口径（P0 裁决：换统计量）")
    signal, cands, inc = build_targets()
    n = len(signal)
    print(f"  样本 {n} 天 {signal.index[0]:%Y-%m-%d} .. {signal.index[-1]:%Y-%m-%d}；"
          f"网格 {len(cands)} 点；k_main={K_MAIN}（唯一主判据）")
    print("  目标序列**不含 carry**（IC 测预测力，carry 是执行层结构性收益）")

    # ── 观测 IC 表（主 k + 报告 k）──────────────────────────────
    rows = []
    for k in (K_MAIN, *K_REPORT):
        x_i, y_i = ic_frame(signal, inc, k)
        ic_i = rank_ic(x_i, y_i)
        rows.append({"pair": "incumbent", "k": k, **ic_i})
        for pair, tgt in cands.items():
            x_c, y_c = ic_frame(signal, tgt, k)
            ic_c = rank_ic(x_c, y_c)
            rows.append({"pair": f"spot{pair[0]}+fut{pair[1]}", "k": k, **ic_c,
                         "ic_diff_vs_inc": ic_c["ic"] - ic_i["ic"]})
    ic_tab = pd.DataFrame(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ic_tab.to_csv(OUT_DIR / "dual_channel_ic_table.csv", index=False)

    main_tab = ic_tab[ic_tab["k"] == K_MAIN].set_index("pair")
    print(f"\n── 观测 rank IC（k={K_MAIN}，n_obs={int(main_tab['n_obs'].iloc[0])}）──")
    show = main_tab[["ic", "t_stat", "p_value", "ic_diff_vs_inc"]].round(4)
    print(show.sort_values("ic", ascending=False).to_string())

    # ── ⓪ 机器 ─────────────────────────────────────────────────
    stat_fn, _ = make_stat_fn(signal, cands, inc, K_MAIN)
    print(f"\n── ⓪ 机器（rotation, min_shift={MIN_SHIFT}, n_perm={args.n_perm}）──")
    perm = selection_permutation_test(
        tuple(cands.keys()), n_obs=n, stat_fn=stat_fn, n_perm=args.n_perm,
        seed=args.seed, scheme="rotation", min_shift=MIN_SHIFT,
        max_shift=n - MIN_SHIFT, statistic_name=f"rank_ic_diff_k{K_MAIN}",
        meta={"prereg": "2026-08-17-dual-channel", "statistic": "paired_rank_ic",
              "k": K_MAIN, "carry": "excluded (IC measures predictive power only)"})
    winner = perm.variants[perm.best_index]
    wtag = f"spot{winner[0]}+fut{winner[1]}"
    print(f"  观测赢家 = {wtag}   observed_best = {perm.observed_best:+.6f}")
    print(f"  **p_selected = {perm.p_selected:.4f}**   p_min_p = {perm.p_min_p:.4f}")
    print(f"  ⚠️ p_naive = {perm.p_naive:.4f}（仅对照）")

    # ── 配对 IC bootstrap（Spearman 对称性技巧）──────────────────
    x_c, y_c = ic_frame(signal, cands[winner], K_MAIN)
    x_i, y_i = ic_frame(signal, inc, K_MAIN)
    common = x_c.index.intersection(x_i.index)
    boot = paired_ic_bootstrap(y_c.reindex(common), y_i.reindex(common),
                               signal.reindex(common),
                               n=args.n_boot, seed=args.seed)
    print(f"\n── paired_ic_bootstrap（n={args.n_boot}）──")
    print(f"  diff_ic = {boot['diff_ic']:+.6f}   95% CI = "
          f"[{boot['ci_lo']:+.6f}, {boot['ci_hi']:+.6f}]   p={boot['p_value']:.4f}")

    # ── 三关 ────────────────────────────────────────────────────
    diff = float(main_tab.loc[wtag, "ic_diff_vs_inc"])
    g1a, g1b, g1c = diff > 0, perm.p_selected < 0.05, boot["ci_lo"] > 0
    verdict = {
        "statistic": f"paired_rank_ic_k{K_MAIN}", "winner": wtag,
        "gate1": {"pass": bool(g1a and g1b and g1c), "ic_diff": diff,
                  "ic_diff_positive": bool(g1a),
                  "p_selected": float(perm.p_selected), "p_selected_lt_05": bool(g1b),
                  "boot_ci_lo": boot["ci_lo"], "boot_ci_hi": boot["ci_hi"],
                  "boot_p_value": boot["p_value"], "ci_lo_positive": bool(g1c)},
        "p_naive_for_reference_only": float(perm.p_naive),
        "ic_incumbent": float(main_tab.loc["incumbent", "ic"]),
        "ic_winner": float(main_tab.loc[wtag, "ic"]),
        "n_obs": int(main_tab.loc[wtag, "n_obs"]), "n_perm": args.n_perm,
        "n_boot": args.n_boot, "seed": args.seed,
        "carry_excluded": True,
    }
    verdict["OVERALL"] = "GO" if verdict["gate1"]["pass"] else "STOP"
    print(f"\n  gate1: {'PASS' if verdict['gate1']['pass'] else 'FAIL'}")
    print(f"  **OVERALL = {verdict['OVERALL']}**")

    print(f"\n── 报告列：其它 k（不进闸门）──")
    for k in K_REPORT:
        t = ic_tab[ic_tab["k"] == k].set_index("pair")
        print(f"  k={k:2d}  赢家 IC={t.loc[wtag, 'ic']:+.4f}  "
              f"现役 IC={t.loc['incumbent', 'ic']:+.4f}  "
              f"差={t.loc[wtag, 'ic_diff_vs_inc']:+.4f}  "
              f"n_obs={int(t.loc[wtag, 'n_obs'])}")

    (OUT_DIR / "dual_channel_ic_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2, default=float) + "\n",
        encoding="utf-8")
    np.save(OUT_DIR / "dual_channel_ic_null.npy", perm.null_selected)
    print(f"\n→ {OUT_DIR / 'dual_channel_ic_table.csv'}")
    print(f"→ {OUT_DIR / 'dual_channel_ic_verdict.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
