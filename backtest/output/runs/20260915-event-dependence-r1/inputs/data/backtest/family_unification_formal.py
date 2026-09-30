"""风格指数族统一的正式跑：**现役混族 vs 全纯风格 P 族**（单点候选，非网格）。

预登记 = `docs/plans/2026-08-18-family-unification-preregistration.md`（跑前已冻结提交）。
**读法在预登记 §5 写死，事后不得更改** —— 尤其"现役胜出"必须记为"混族优势没有理论支撑"，
而不是"现役经受住了挑战"。

## 命题

现役四对横跨两套不兼容的编制方法（`2026-08-18-style-index-methodology-audit.md` §7）：
300/500 是**老版规模风格**（价值分 `(4项Z)/4`、不分金融、固定名次、市值加权）；
1000/2000 是**纯风格 P 族**（价值分分金融、取概率=1、按风格得分加权）。
候选把 300/500 也换成 P 族，使四对**同族**。

## 候选（唯一，选优空间 = 1 点）

    现役：000918/000919  H30351/H30352  932407/932406  932409/932408
    候选：932401/932400  932403/932402  932407/932406  932409/932408
                ↑ 只换这两对，1000/2000 与现役逐字相同 → 最小差异、功效最大

单点 → `p_selected` 与 `p_naive` 相等，**无选优惩罚**。这正是它比网格有力之处：
同日实测 1160 点网格的过闸门槛 +1.27~+1.55，而网格最大增益仅 +0.39~+0.61。

## 口径（预登记 §2 冻结）

信号构造完全不动（四对 tanh(z) 等权 → `rolling(5).mean()` → long-flat θ=0；lb20/zw40）；
执行标的 = 现役部署 `blend(500+1000)`、`cost_bps=3.0`、**含 carry**（零 carry 作对照列）；
⓪ 机器 `rotation` / `min_shift=60` / `n_perm=1000` / `seed=0`；
统计量 = 配对 Sharpe 差（候选 − 现役），full 窗，同一置换同时作用于两条仓位序列。

⚠️ **命名陷阱**：`index_codes.csv` 里的"中证1000成长"→`932407` 实为**中证1000纯成长**
（老命名，早于 932393 真·中证1000成长的发布）。本模块沿用该映射不改，但引用时以代码为准。

## 用法

    python3 -m backtest.family_unification_formal
    python3 -m backtest.family_unification_formal --n-perm 50    # 快速自检
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

from backtest.baseline import WINDOWS, evaluate  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

LOOKBACK, Z_WINDOW, SMOOTHING = 20, 40, 5
COST_BPS = 3.0
KOU_JING = "blend"                      # 现役部署口径
MIN_SHIFT = 60
STAT_WINDOWS = ("2014-2020", "2021-2023")
GATE_ALPHA = 0.05
GATE2_TOLERANCE = 0.02                  # 关2：worst_tv ≥ 现役 − 0.02

INCUMBENT_NAMES = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
                   "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
CANDIDATE_NAMES = ["沪深300纯成长", "沪深300纯价值", "中证500纯成长", "中证500纯价值",
                   "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
OUT = ROOT / "backtest" / "output" / "family_unification_verdict.json"


def build_factor(names: list[str]) -> pd.Series:
    """八条腿（成长,价值 × 4）→ 现役口径的 `factor_value`（零平行实现，走生产函数）。"""
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import (
        PairConfig, calculate_contrast_equal_weight_signal,
    )
    prices = load_pg_closes(names)
    pcs = [PairConfig(group=i + 1, left_column=names[2 * i],
                      right_column=names[2 * i + 1], direction="forward")
           for i in range(4)]
    out = calculate_contrast_equal_weight_signal(
        prices, lookback=LOOKBACK, z_window=Z_WINDOW,
        smoothing_window=SMOOTHING, pair_configs=pcs)
    return out["factor_value"]


class Data:
    def __init__(self):
        inc_f, cand_f = build_factor(INCUMBENT_NAMES), build_factor(CANDIDATE_NAMES)
        und, car = load_underlying_returns(KOU_JING), load_carry(KOU_JING)
        idx = inc_f.index.intersection(cand_f.index).intersection(und.index).sort_values()
        self.idx = idx
        self.inc_pos = production_position(inc_f.reindex(idx)).astype(float).to_numpy()
        self.cand_pos = production_position(cand_f.reindex(idx)).astype(float).to_numpy()
        self.und = und.reindex(idx)
        self.car = car.reindex(idx) if car is not None else None

    def _ret(self, pos_arr: np.ndarray, carry: bool) -> pd.Series:
        pos = pd.Series(pos_arr, index=self.idx)
        c = self.car if carry else None
        return run_strategy(pos, self.und, COST_BPS, c)["ret"]

    def sharpe_full(self, pos_arr: np.ndarray, carry: bool = True) -> float:
        return float(sharpe(self._ret(pos_arr, carry)))

    def metrics(self, pos_arr: np.ndarray, carry: bool = True) -> dict:
        """四窗 + 辅助指标（描述性，不进闸门统计量）。"""
        pos = pd.Series(pos_arr, index=self.idx)
        c = self.car if carry else None
        out = {}
        for win, (s, e) in WINDOWS.items():
            p, u = pos, self.und
            cc = c
            if s:
                m = p.index >= pd.Timestamp(s)
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            if e:
                m = p.index <= pd.Timestamp(e)
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            out[win] = evaluate(p, u, cc, COST_BPS, 0)["long"]
        return out


def make_stat_fn(d: Data):
    """`stat_fn(_, idx) -> Sharpe(候选) − Sharpe(现役)`，**同一 idx 同时作用于两条仓位**。

    置换后两条仓位的静态差异（平均暴露、换手）仍在 → 零分布不以 0 为中心，这是对的：
    检验问的正是"差值能否与静态差异本身产生的波动区分"（同 ⑧ 收官文档 §2）。
    """
    def stat_fn(_variant, idx):
        idx = np.asarray(idx)
        return d.sharpe_full(d.cand_pos[idx]) - d.sharpe_full(d.inc_pos[idx])
    return stat_fn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="族统一正式跑（现役 vs 全 P 族）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    d = Data()
    n = len(d.idx)
    print(f"样本 {n} 日（{d.idx.min().date()} → {d.idx.max().date()}），口径 {KOU_JING}（含 carry）")
    print(f"平均暴露：现役 {d.inc_pos.mean()*100:.1f}%   候选 {d.cand_pos.mean()*100:.1f}%"
          f"   仓位逐日不同 {int((d.inc_pos != d.cand_pos).sum())} / {n} 天\n")

    inc_m, cand_m = d.metrics(d.inc_pos), d.metrics(d.cand_pos)
    rows = []
    for tag, m in (("现役（混族）", inc_m), ("候选（全 P 族）", cand_m)):
        rows.append({"方案": tag,
                     **{f"S_{w}": round(m[w]["sharpe"], 4) for w in WINDOWS},
                     "年化%": round(m["full"]["ann"] * 100, 2),
                     "MaxDD%": round(m["full"]["maxdd"] * 100, 2),
                     "换手": round(m["full"]["turnover"], 2)})
    desc = pd.DataFrame(rows)
    print(desc.to_string(index=False))

    inc_wtv = min(inc_m[w]["sharpe"] for w in STAT_WINDOWS)
    cand_wtv = min(cand_m[w]["sharpe"] for w in STAT_WINDOWS)
    zc_inc = d.sharpe_full(d.inc_pos, carry=False)
    zc_cand = d.sharpe_full(d.cand_pos, carry=False)
    print(f"\n零 carry 对照：现役 {zc_inc:.4f}   候选 {zc_cand:.4f}   差 {zc_cand-zc_inc:+.4f}")

    res = selection_permutation_test(
        [("all_pure",)], n_obs=n, stat_fn=make_stat_fn(d),
        n_perm=args.n_perm, seed=args.seed, scheme="rotation",
        min_shift=MIN_SHIFT, max_shift=n - MIN_SHIFT,
        statistic_name="sharpe_diff_candidate_minus_incumbent",
        meta={"kou_jing": KOU_JING, "carry": "with", "cost_bps": COST_BPS})

    diff = float(res.observed_best)
    g1 = bool(diff > 0 and res.p_selected < GATE_ALPHA)
    g2 = bool(cand_wtv >= inc_wtv - GATE2_TOLERANCE)
    g3 = bool(cand_m["full"]["maxdd"] >= inc_m["full"]["maxdd"]
              and cand_m["full"]["turnover"] <= inc_m["full"]["turnover"] * 1.05)
    overall = g1 and g2 and g3

    if diff > 0 and res.p_selected < GATE_ALPHA:
        verdict = "① 候选过闸 → 同质化改善了信号"
    elif res.p_selected >= GATE_ALPHA:
        verdict = ("② 差异不显著 → 同质与否对信号无可辨认影响；"
                   "**不得**表述为「现役更优」")
    else:
        verdict = ("③ 现役显著胜出 → **必须**记为「混族优势没有理论支撑」，"
                   "按运气处理；须触发预登记 §1b 的候选 A 去定位机制；"
                   "该情形**削弱**而非加强现役立足点")

    out = {
        "n_obs": n, "n_perm": args.n_perm,
        "sharpe_incumbent_full": round(inc_m["full"]["sharpe"], 4),
        "sharpe_candidate_full": round(cand_m["full"]["sharpe"], 4),
        "sharpe_diff": round(diff, 4),
        "p_selected": round(float(res.p_selected), 4),
        "p_naive": round(float(res.p_naive), 4),
        "worst_tv_incumbent": round(inc_wtv, 4), "worst_tv_candidate": round(cand_wtv, 4),
        "gate1": g1, "gate2": g2, "gate3": g3, "OVERALL": "GO" if overall else "STOP",
        "verdict_case": verdict,
        "zero_carry_diff": round(zc_cand - zc_inc, 4),
    }
    print(f"\n── ⓪ 机器（单点候选，n_perm={args.n_perm}）──")
    print(f"  full Sharpe：现役 {out['sharpe_incumbent_full']}  候选 {out['sharpe_candidate_full']}"
          f"  差 {diff:+.4f}")
    print(f"  p_selected = {out['p_selected']:.4f}   p_naive = {out['p_naive']:.4f}（单点应相等）")
    print(f"  关1 {'✓' if g1 else '✗'}  关2 {'✓' if g2 else '✗'}"
          f"（worst_tv 候选 {cand_wtv:.4f} vs 现役 {inc_wtv:.4f}）  关3 {'✓' if g3 else '✗'}")
    print(f"  OVERALL = {out['OVERALL']}")
    print(f"\n  ⭐ 预登记 §5 判定：{verdict}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
