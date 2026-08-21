"""等比 5 桶的正式判定：**现役四对 vs 等比 5 桶自建五对等权（全替换）**，单点候选。

预登记 = `docs/plans/2026-08-19-geometric-5buckets-preregistration.md`（已冻结）§2/§3。
前置 = 0R' preflight 过（500 带真值锚 ≥0.9536）+ 构建健全性核对过。
**读法 §3 写死**：情形② =「两种划法在现役架构下不可辨认」（否定力强于 r3 情形②，
须如实写明——本候选没有权重稀释与短窗两个借口）；情形③ = 首选解释「候选丢掉了
现役自带的官方真值信息」，不得升格为「对数等距结构无价值」。

口径（§2 冻结）：信号构造完全不动（每对 tanh(z) → 等权 → rolling5 → θ=0；lb20/zw40）；
执行 blend、3bp、含 carry；⓪ 机器 rotation/min_shift=60/n_perm=1000/seed=0；
公共窗 = 自建收益首日 + 60 交易日暖机，对照同窗重算；
关2 = worst(train=起点~2020, val=2021-2023) ≥ 现役 − 0.02（裁决点 4，日历窗）。

结论 provisional；有效窗截至 2025-03-31。
用法：python3 -m backtest.geometric_5b_formal [--n-perm 1000]
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

from backtest.baseline import evaluate  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

LOOKBACK, Z_WINDOW, SMOOTHING = 20, 40, 5
COST_BPS = 3.0
KOU_JING = "blend"
MIN_SHIFT = 60
WARMUP_DAYS = LOOKBACK + Z_WINDOW
WINS = {"full": (None, None),
        "2015-2020": (None, "2021-01-01"),
        "2021-2023": ("2021-01-01", "2024-01-01"),
        "2024-2026": ("2024-01-01", None)}          # 2024-2026 描述性，不进判据
STAT_WINDOWS = ("2015-2020", "2021-2023")
GATE_ALPHA = 0.05
GATE2_TOLERANCE = 0.02

INCUMBENT = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
             "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
GEO_CSV = ROOT / "backtest" / "output" / "geo5_pairs_daily.csv"
OUT = ROOT / "backtest" / "output" / "geo5_verdict.json"


def _json_safe(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def load_prices(geo_csv: Path = GEO_CSV) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    from signals.common.data_source import load_pg_closes
    closes = load_pg_closes(INCUMBENT)
    geo = pd.read_csv(geo_csv, index_col=0, parse_dates=True).dropna(how="all")
    nav = (1.0 + geo.fillna(0.0)).cumprod()
    cand_cols = list(nav.columns)                    # g1_growth..g5_value
    return pd.concat([closes, nav], axis=1), cand_cols, geo.index.min()


def build_factor(prices: pd.DataFrame, names: list[str]) -> pd.Series:
    from signals.equal_weight.generate_signal import (
        PairConfig, calculate_contrast_equal_weight_signal,
    )
    pcs = [PairConfig(group=i + 1, left_column=names[2 * i],
                      right_column=names[2 * i + 1], direction="forward")
           for i in range(len(names) // 2)]
    out = calculate_contrast_equal_weight_signal(
        prices[names], lookback=LOOKBACK, z_window=Z_WINDOW,
        smoothing_window=SMOOTHING, pair_configs=pcs)
    return out["factor_value"]


class Data:
    def __init__(self, geo_csv: Path = GEO_CSV):
        prices, cand_cols, geo_start = load_prices(geo_csv)
        inc_f = build_factor(prices, INCUMBENT)
        cand_f = build_factor(prices, cand_cols)
        und, car = load_underlying_returns(KOU_JING), load_carry(KOU_JING)
        idx = inc_f.index.intersection(cand_f.index).intersection(und.index).sort_values()
        warm = idx[idx >= geo_start]
        start = warm[WARMUP_DAYS]
        idx = idx[idx >= start]
        self.idx, self.geo_start = idx, geo_start
        self.inc_pos = production_position(inc_f.reindex(idx)).astype(float).to_numpy()
        self.cand_pos = production_position(cand_f.reindex(idx)).astype(float).to_numpy()
        self.und = und.reindex(idx)
        self.car = car.reindex(idx) if car is not None else None

    def _ret(self, pos_arr: np.ndarray, carry: bool) -> pd.Series:
        pos = pd.Series(pos_arr, index=self.idx)
        return run_strategy(pos, self.und, COST_BPS, self.car if carry else None)["ret"]

    def sharpe_full(self, pos_arr: np.ndarray, carry: bool = True) -> float:
        return float(sharpe(self._ret(pos_arr, carry)))

    def metrics(self, pos_arr: np.ndarray) -> dict:
        pos = pd.Series(pos_arr, index=self.idx)
        out = {}
        for win, (s, e) in WINS.items():
            p, u, cc = pos, self.und, self.car
            if s is not None:
                m = p.index >= pd.Timestamp(s)
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            if e is not None:
                m = p.index < pd.Timestamp(e)
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            out[win] = evaluate(p, u, cc, COST_BPS, 0)["long"]
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="等比 5 桶正式判定（全替换 vs 现役四对）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--geo-csv", type=Path, default=GEO_CSV)
    ap.add_argument("--output-dir", type=Path, default=OUT.parent)
    args = ap.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    d = Data(geo_csv=args.geo_csv)
    n = len(d.idx)
    print(f"公共窗 {n} 日（{d.idx.min().date()} → {d.idx.max().date()}；"
          f"自建收益首日 {d.geo_start.date()} + 暖机 {WARMUP_DAYS}d），口径 {KOU_JING}（含 carry）")
    print(f"平均暴露：现役 {d.inc_pos.mean()*100:.1f}%   候选 {d.cand_pos.mean()*100:.1f}%"
          f"   仓位逐日不同 {int((d.inc_pos != d.cand_pos).sum())} / {n} 天\n")

    inc_m, cand_m = d.metrics(d.inc_pos), d.metrics(d.cand_pos)
    rows = []
    for tag, m in (("现役（四对官方带）", inc_m), ("候选（等比5桶全替换）", cand_m)):
        rows.append({"方案": tag,
                     **{f"S_{w}": round(m[w]["sharpe"], 4) for w in WINS},
                     "年化%": round(m["full"]["ann"] * 100, 2),
                     "MaxDD%": round(m["full"]["maxdd"] * 100, 2),
                     "换手": round(m["full"]["turnover"], 2)})
    print(pd.DataFrame(rows).to_string(index=False))

    inc_wtv = min(inc_m[w]["sharpe"] for w in STAT_WINDOWS)
    cand_wtv = min(cand_m[w]["sharpe"] for w in STAT_WINDOWS)
    zc = d.sharpe_full(d.cand_pos, carry=False) - d.sharpe_full(d.inc_pos, carry=False)
    print(f"\n零 carry 对照差：{zc:+.4f}")

    def stat_fn(_variant, idx):
        idx = np.asarray(idx)
        return d.sharpe_full(d.cand_pos[idx]) - d.sharpe_full(d.inc_pos[idx])

    res = selection_permutation_test(
        [("geo5_replace",)], n_obs=n, stat_fn=stat_fn,
        n_perm=args.n_perm, seed=args.seed, scheme="rotation",
        min_shift=MIN_SHIFT, max_shift=n - MIN_SHIFT,
        statistic_name="sharpe_diff_candidate_minus_incumbent",
        meta={"kou_jing": KOU_JING, "carry": "with", "cost_bps": COST_BPS,
              "candidate": "geometric_5buckets_full_replacement_r2"})

    diff = float(res.observed_best)
    g1 = bool(diff > 0 and res.p_selected < GATE_ALPHA)
    g2 = bool(cand_wtv >= inc_wtv - GATE2_TOLERANCE)
    g3 = bool(cand_m["full"]["maxdd"] >= inc_m["full"]["maxdd"]
              and cand_m["full"]["turnover"] <= inc_m["full"]["turnover"] * 1.05)
    overall = g1 and g2 and g3

    if g1:
        verdict = "① 候选过闸 → 等比重划携带现行划法没有的信息（provisional）"
    elif res.p_selected >= GATE_ALPHA:
        verdict = ("② 差异不显著 → 两种划法在现役架构下不可辨认；不得表述为「重划无价值」，"
                   "但本候选无权重稀释/短窗借口，此情形对「换划法」方向的否定力显著强于 "
                   "r3 的情形②，报告须如实写明")
    else:
        verdict = ("③ 显著变差 → 首选解释=候选丢掉了现役自带的官方真值信息"
                   "（真实成分/缓冲区/官方口径细节）；结论=「自建等比重划劣于官方带组合」"
                   "并附实现差异清单，不得升格为「对数等距结构无价值」")

    out = {
        "n_obs": n, "n_perm": args.n_perm, "seed": args.seed,
        "common_window": [str(d.idx.min().date()), str(d.idx.max().date())],
        "position_diff_days": int((d.inc_pos != d.cand_pos).sum()),
        "position_diff_ratio": round(float((d.inc_pos != d.cand_pos).mean()), 6),
        "metrics_incumbent": inc_m,
        "metrics_candidate": cand_m,
        "sharpe_incumbent_full": round(inc_m["full"]["sharpe"], 4),
        "sharpe_candidate_full": round(cand_m["full"]["sharpe"], 4),
        "sharpe_diff": round(diff, 4),
        "p_selected": round(float(res.p_selected), 4),
        "p_naive": round(float(res.p_naive), 4),
        "worst_tv_incumbent": round(inc_wtv, 4), "worst_tv_candidate": round(cand_wtv, 4),
        "s_2024_2026": [round(inc_m["2024-2026"]["sharpe"], 4),
                        round(cand_m["2024-2026"]["sharpe"], 4)],
        "gate1": g1, "gate2": g2, "gate3": g3, "OVERALL": "GO" if overall else "STOP",
        "verdict_case": verdict,
        "zero_carry_diff": round(zc, 4),
        "caveats": ["provisional: approximate-PIT",
                    "有效窗截至 2025-03-31（stock_financial 停更）",
                    "候选全自建 vs 现役官方发布序列的信息不对称（§4-5）"],
    }
    print(f"\n── ⓪ 机器（单点候选，n_perm={args.n_perm}）──")
    print(f"  full Sharpe：现役 {out['sharpe_incumbent_full']}  候选 {out['sharpe_candidate_full']}"
          f"  差 {diff:+.4f}")
    print(f"  p_selected = {out['p_selected']:.4f}   p_naive = {out['p_naive']:.4f}（单点应相等）")
    print(f"  关1 {'✓' if g1 else '✗'}  关2 {'✓' if g2 else '✗'}"
          f"（worst_tv 候选 {cand_wtv:.4f} vs 现役 {inc_wtv:.4f}）  关3 {'✓' if g3 else '✗'}")
    print(f"  OVERALL = {out['OVERALL']}")
    print(f"\n  ⭐ 预登记 §3 判定：{verdict}")

    output = args.output_dir / "geo5_verdict.json"
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_json_safe), encoding="utf-8")
    print(f"\n→ {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
