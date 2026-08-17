"""候选⑧ 双通道执行 —— **正式跑**（预登记 2026-08-17，四问全裁决后冻结）。

预登记 = `docs/plans/2026-08-17-dual-channel-preregistration-draft.md`。
本模块严格按其 §4 自由度决议与 §5 闸门执行，不引入任何新自由度。

## 闸门（§5，问 1 裁决为 (a)）

  关1（主判据，三条都要）：full 窗 Sharpe 提升 > 0
                          且 ⓪ 机器 `p_selected` < 0.05
                          且 `paired_bootstrap` Sharpe 差 95% CI 下沿 > 0
  关2（护栏）：worst(train,val) Sharpe ≥ 现役同窗 − 0.02
  关3：成本后净 Sharpe > 现役，且换手不恶化、MaxDD 不恶化

**一律用扣换月成本后的口径**（§10）。`p_naive` 只作"选择效应有多大"的对照登记，
**不得进闸门**（`selection_permutation` 的取用纪律）。

## 置换设计（§4 决议 8）+ 零假设的含义

`scheme="rotation"`、`min_shift=60`、`max_shift=n−60`、`n_perm=1000`、`seed=0`。
**同一重排索引同时作用于候选与现役的信号**（两者本就共用一套信号 = 配对）。

置换破坏的是「信号 ↔ 标的收益」的时间对齐，**不改变标的本身**。所以置换后 15 个配对
的 Sharpe 差**不趋于 0**，而趋于「若信号无择时能力，纯标的静态差异能造成多大的
Sharpe 差」。这正是我们要的零假设：观测值显著超过它 ⇒ 候选的优势**不只是**中证2000
长期收益更高这种静态 beta 差异，而是**信号在该标的组合上确实更有效**。

## 用法

    python3 -m backtest.dual_channel_formal                  # 正式跑（预登记口径）
    python3 -m backtest.dual_channel_formal --n-perm 100      # 快速自检
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

from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.dual_channel_probe import (  # noqa: E402
    FUT_LEGS, SPOT_LEGS, load_futures_daily_frames, roll_cost_series,
)
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import (  # noqa: E402
    ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
)
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402
from backtest.underlying_probe import load_index_returns  # noqa: E402
from backtest.yearly import concentration_summary  # noqa: E402

W_SPOT, W_FUT = 0.4, 0.6          # §4 决议 3，冻结不扫
COST_BPS = 3.0                    # §4 决议 6 主口径
MIN_SHIFT = 60                    # §4 决议 8
WINDOWS = {"full": (None, None), "train": ("2014-01-01", "2020-12-31"),
           "val": ("2021-01-01", "2023-12-31"), "holdout": ("2024-01-01", "2026-12-31")}
OUT_DIR = ROOT / "backtest" / "output"


class Data:
    """一次性加载并对齐到公共索引，之后所有计算都在 numpy 上做（置换要跑上万次）。"""

    def __init__(self, cost_bps: float = COST_BPS):
        self.cost_bps = cost_bps
        _, smooth = load_two_columns()
        pos = production_position(smooth).astype(float)

        spot = {k: load_index_returns(c) for k, (c, _) in SPOT_LEGS.items()}
        fut, carry, held = {}, {}, {}
        for k, (code, pref, _) in FUT_LEGS.items():
            fut[k] = load_index_returns(code)
            carry[k], held[k] = load_futures_daily_frames(code, pref)

        inc_und, inc_car = load_underlying_returns("blend"), load_carry("blend")

        idx = pos.index
        for s in list(spot.values()) + list(fut.values()) + [inc_und]:
            idx = idx.intersection(s.index)
        self.idx = idx.sort_values()
        self.pos = pos.reindex(self.idx)
        self.spot = {k: v.reindex(self.idx).fillna(0.0) for k, v in spot.items()}
        self.fut = {k: v.reindex(self.idx).fillna(0.0) for k, v in fut.items()}
        self.carry = {k: v.reindex(self.idx).fillna(0.0) for k, v in carry.items()}
        self.held = held
        self.inc_und = inc_und.reindex(self.idx).fillna(0.0)
        self.inc_car = inc_car.reindex(self.idx).fillna(0.0)
        self.pairs = tuple((sk, fk) for sk in SPOT_LEGS for fk in FUT_LEGS)

    # ── 收益构造（扣换月成本口径）─────────────────────────────────
    def candidate_ret(self, pos: pd.Series, sk: str, fk: str) -> pd.Series:
        spot_leg = run_strategy(pos, self.spot[sk], self.cost_bps, None)
        fut_leg = run_strategy(pos, self.fut[fk], self.cost_bps, self.carry[fk])
        ret = W_SPOT * spot_leg["ret"] + W_FUT * fut_leg["ret"]
        roll = roll_cost_series(W_FUT * fut_leg["pos_eff"], self.held[fk], self.cost_bps)
        return ret - roll

    def incumbent_ret(self, pos: pd.Series) -> pd.Series:
        r = run_strategy(pos, self.inc_und, self.cost_bps, self.inc_car)
        # 现役 = 100% 期货、500/1000 各半 → 两腿换月都扣（§10）
        roll = (roll_cost_series(0.5 * r["pos_eff"], self.held["500"], self.cost_bps)
                + roll_cost_series(0.5 * r["pos_eff"], self.held["1000"], self.cost_bps))
        return r["ret"] - roll

    def candidate_pos_eff(self, pos: pd.Series, sk: str, fk: str) -> pd.Series:
        s = run_strategy(pos, self.spot[sk], self.cost_bps, None)["pos_eff"]
        f = run_strategy(pos, self.fut[fk], self.cost_bps, self.carry[fk])["pos_eff"]
        return W_SPOT * s + W_FUT * f


def _cut(s, a, b):
    if a:
        s = s[s.index >= pd.Timestamp(a)]
    if b:
        s = s[s.index <= pd.Timestamp(b)]
    return s


def make_stat_fn(d: Data):
    """`stat_fn(pair, idx) -> Sharpe(候选) − Sharpe(现役)`，full 窗、扣换月成本。

    同一 `idx` 同时作用于两者的信号（配对）。现役部分只依赖 `idx`，故按 idx 缓存
    —— 否则 15 变体 × B 次会把现役重算 15 遍。
    """
    inc_cache: dict[bytes, float] = {}
    pos_values = d.pos.to_numpy()

    def stat_fn(pair, idx):
        key = np.asarray(idx).tobytes()
        p = pd.Series(pos_values[idx], index=d.idx)
        if key not in inc_cache:
            inc_cache[key] = float(sharpe(d.incumbent_ret(p)))
        sk, fk = pair
        return float(sharpe(d.candidate_ret(p, sk, fk))) - inc_cache[key]

    return stat_fn


def descriptive_table(d: Data) -> pd.DataFrame:
    """观测口径的完整指标表（15 配对 + 现役 × 四窗），扣换月成本。"""
    rows = []
    for sk, fk in d.pairs:
        ret = d.candidate_ret(d.pos, sk, fk)
        pe = d.candidate_pos_eff(d.pos, sk, fk)
        for w, (a, b) in WINDOWS.items():
            r, p = _cut(ret, a, b), _cut(pe, a, b)
            if len(r) < 60:
                continue
            rows.append({"pair": f"spot{sk}+fut{fk}", "window": w,
                         "ann": ann_return(r), "sharpe": sharpe(r),
                         "maxdd": max_drawdown(r), "calmar": calmar(r),
                         "turnover": turnover(p), "hit": hit_rate(r), "n_obs": len(r)})
    inc = d.incumbent_ret(d.pos)
    inc_pe = run_strategy(d.pos, d.inc_und, d.cost_bps, d.inc_car)["pos_eff"]
    for w, (a, b) in WINDOWS.items():
        r, p = _cut(inc, a, b), _cut(inc_pe, a, b)
        rows.append({"pair": "incumbent_blend", "window": w,
                     "ann": ann_return(r), "sharpe": sharpe(r),
                     "maxdd": max_drawdown(r), "calmar": calmar(r),
                     "turnover": turnover(p), "hit": hit_rate(r), "n_obs": len(r)})
    return pd.DataFrame(rows)


def gates(d: Data, desc: pd.DataFrame, winner: tuple, perm, boot: dict) -> dict:
    """三关判定（§5）。返回逐关明细 + OVERALL。"""
    tag = f"spot{winner[0]}+fut{winner[1]}"
    piv = desc.pivot_table(index="pair", columns="window", values="sharpe")
    mdd = desc.pivot_table(index="pair", columns="window", values="maxdd")
    tno = desc.pivot_table(index="pair", columns="window", values="turnover")

    full_gain = float(piv.loc[tag, "full"] - piv.loc["incumbent_blend", "full"])
    g1a = full_gain > 0
    g1b = perm.p_selected < 0.05
    g1c = boot["ci_lo"] > 0
    gate1 = g1a and g1b and g1c

    cand_wtv = float(min(piv.loc[tag, "train"], piv.loc[tag, "val"]))
    inc_wtv = float(min(piv.loc["incumbent_blend", "train"],
                        piv.loc["incumbent_blend", "val"]))
    gate2 = cand_wtv >= inc_wtv - 0.02

    mdd_ok = float(mdd.loc[tag, "full"]) >= float(mdd.loc["incumbent_blend", "full"])
    tno_ok = float(tno.loc[tag, "full"]) <= float(tno.loc["incumbent_blend", "full"])
    gate3 = full_gain > 0 and mdd_ok and tno_ok

    return {
        "winner": tag,
        "gate1": {"pass": bool(gate1), "full_sharpe_gain": full_gain,
                  "full_gain_positive": bool(g1a),
                  "p_selected": float(perm.p_selected), "p_selected_lt_05": bool(g1b),
                  "boot_ci_lo": boot["ci_lo"], "boot_ci_hi": boot["ci_hi"],
                  "boot_diff_sharpe": boot["diff_sharpe"],
                  "boot_p_value": boot["p_value"], "ci_lo_positive": bool(g1c)},
        "gate2": {"pass": bool(gate2), "cand_worst_tv": cand_wtv,
                  "inc_worst_tv": inc_wtv, "floor": inc_wtv - 0.02},
        "gate3": {"pass": bool(gate3), "maxdd_not_worse": bool(mdd_ok),
                  "turnover_not_worse": bool(tno_ok),
                  "cand_maxdd": float(mdd.loc[tag, "full"]),
                  "inc_maxdd": float(mdd.loc["incumbent_blend", "full"]),
                  "cand_turnover": float(tno.loc[tag, "full"]),
                  "inc_turnover": float(tno.loc["incumbent_blend", "full"])},
        "OVERALL": "GO" if (gate1 and gate2 and gate3) else "STOP",
        "p_naive_for_reference_only": float(perm.p_naive),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="候选⑧ 双通道 正式跑（预登记口径）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    args = ap.parse_args()

    print("候选⑧ 双通道执行 正式跑 —— 预登记 2026-08-17（四问全裁决）")
    print(f"  名义权重 现货 {W_SPOT} / 期货 {W_FUT}；cost_bps={args.cost_bps}；"
          f"**扣换月成本口径**")
    d = Data(args.cost_bps)
    n = len(d.idx)
    print(f"  样本 {n} 天 {d.idx[0]:%Y-%m-%d} .. {d.idx[-1]:%Y-%m-%d}；"
          f"网格 {len(d.pairs)} 点")

    desc = descriptive_table(d)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    desc.to_csv(OUT_DIR / "dual_channel_formal_metrics.csv", index=False)

    print(f"\n── ⓪ 机器（selection-aware，rotation，min_shift={MIN_SHIFT}，"
          f"n_perm={args.n_perm}，seed={args.seed}）──")
    perm = selection_permutation_test(
        d.pairs, n_obs=n, stat_fn=make_stat_fn(d), n_perm=args.n_perm,
        seed=args.seed, scheme="rotation", min_shift=MIN_SHIFT,
        max_shift=n - MIN_SHIFT, statistic_name="sharpe_diff_vs_incumbent",
        meta={"prereg": "2026-08-17-dual-channel", "w_spot": W_SPOT, "w_fut": W_FUT,
              "cost_bps": args.cost_bps, "roll_cost": "Batch12 §7 口径，两边都扣"})
    winner = perm.variants[perm.best_index]
    print(f"  观测赢家 = spot{winner[0]}+fut{winner[1]}   "
          f"observed_best = {perm.observed_best:+.4f}")
    print(f"  **p_selected = {perm.p_selected:.4f}**（主口径）   "
          f"p_min_p = {perm.p_min_p:.4f}")
    print(f"  ⚠️ p_naive = {perm.p_naive:.4f}（仅作选择效应对照，不得进闸门）"
          f"  → 膨胀 {perm.p_selected / max(perm.p_naive, 1e-9):.1f}×")

    print(f"\n── paired_bootstrap（moving-block，block=20，n={args.n_boot}）──")
    boot = paired_block_bootstrap_sharpe_diff(
        d.candidate_ret(d.pos, *winner), d.incumbent_ret(d.pos),
        block=20, n=args.n_boot, seed=args.seed)
    print(f"  diff_sharpe = {boot['diff_sharpe']:+.4f}   95% CI = "
          f"[{boot['ci_lo']:+.4f}, {boot['ci_hi']:+.4f}]   "
          f"p_value = {boot['p_value']:.4f}")

    verdict = gates(d, desc, winner, perm, boot)
    print("\n── 三关判定 ──")
    for k in ("gate1", "gate2", "gate3"):
        print(f"  {k}: {'PASS' if verdict[k]['pass'] else 'FAIL'}   "
              + json.dumps({kk: vv for kk, vv in verdict[k].items() if kk != 'pass'},
                           ensure_ascii=False, default=float))
    print(f"\n  **OVERALL = {verdict['OVERALL']}**")

    print("\n── 报告列（不设闸）──")
    piv = desc.pivot_table(index="pair", columns="window", values="sharpe")
    tag = verdict["winner"]
    print(f"  {tag:22s} " + "  ".join(
        f"{w}={piv.loc[tag, w]:.4f}" for w in WINDOWS))
    print(f"  {'incumbent_blend':22s} " + "  ".join(
        f"{w}={piv.loc['incumbent_blend', w]:.4f}" for w in WINDOWS))
    for name, ret in (("winner", d.candidate_ret(d.pos, *winner)),
                      ("incumbent", d.incumbent_ret(d.pos))):
        c = concentration_summary(ret)
        print(f"  concentration[{name:9s}] full={c['sharpe_full']:.4f} "
              f"ex_top1={c['sharpe_ex_top1']:.4f}(剔{c['ex_top1_year']}) "
              f"ex_top2={c['sharpe_ex_top2']:.4f} roll3y_min={c['roll3y_min']:.4f}")
    corr = float(pd.concat([
        run_strategy(d.pos, d.spot[winner[0]], args.cost_bps, None)["ret"],
        run_strategy(d.pos, d.fut[winner[1]], args.cost_bps,
                     d.carry[winner[1]])["ret"]], axis=1).corr().iloc[0, 1])
    print(f"  corr(两腿收益) = {corr:.4f}")

    verdict["corr_two_legs"] = corr
    verdict["n_obs"] = n
    verdict["n_perm"] = args.n_perm
    verdict["n_boot"] = args.n_boot
    verdict["seed"] = args.seed
    (OUT_DIR / "dual_channel_formal_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2, default=float) + "\n",
        encoding="utf-8")
    np.save(OUT_DIR / "dual_channel_formal_null.npy", perm.null_selected)
    print(f"\n→ {OUT_DIR / 'dual_channel_formal_metrics.csv'}")
    print(f"→ {OUT_DIR / 'dual_channel_formal_verdict.json'}")
    print(f"→ {OUT_DIR / 'dual_channel_formal_null.npy'}（当选统计量空分布 B={args.n_perm}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
