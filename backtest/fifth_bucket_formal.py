"""第 5 桶（尾部对）的正式判定：**现役四对 vs 四对+尾部对五对等权**（单点候选）。

预登记 = `docs/plans/2026-08-19-fifth-bucket-preregistration-r3.md`（已冻结）§5/§6。
前置 = OVERALL Gate 0 通过（0A 0.8957 / 0B 0.8710，`2026-08-19-gate0-execution-record.md`）。
**读法在 §6 写死**：情形② 只能表述为"在现役架构与本实现下尾部的增量不可辨认"，
不得说"尾部无信息"；情形③ 找到机制前记为"异常，待解释"。

## 口径（§5 冻结）

信号构造完全不动：每对 tanh(z) → **五对等权** → rolling(5).mean() → long-flat θ=0；
lb20/zw40；执行 = 现役部署 `blend(500+1000)`、cost 3bp、含 carry；
⓪ 机器 rotation / min_shift=60 / n_perm=1000 / seed=0，统计量 = 配对 Sharpe 差。
尾部对腿 = `tail_pair_runner` 落盘的日收益 → 累积净值（信号只用收益，基点无关）。

## 公共窗（r1 §4 条款的落实，登记）

起点 = 尾部对日收益首日（≈2015-06-16）后第 **lookback+z_window = 60** 个交易日
（信号暖机；尾部对之前的日子候选信号会把该对当 0 算，等于四对×4/5，不是"五对"），
对照现役在**同一公共窗重算**，不引用其它文档的全窗数字。

## 限定（§7）

结论标 **provisional**；具体 PIT 限制由输入数据相邻的构建元数据校验后写入判定结果。

用法：python3 -m backtest.fifth_bucket_formal [--n-perm 1000]
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
from backtest.pit_metadata import load_build_pit_metadata  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

LOOKBACK, Z_WINDOW, SMOOTHING = 20, 40, 5
COST_BPS = 3.0
KOU_JING = "blend"
MIN_SHIFT = 60
WARMUP_DAYS = LOOKBACK + Z_WINDOW          # 公共窗起点 = 尾部收益首日 + 60 交易日
#: 关2 的 worst(train,val) 窗：r1 冻结的日历窗（2014-2020/2021-2023）与短公共窗
#: （2023-03 起）结构性不相容（前者过滤后为空）→ 最小适配 = 公共窗**机械对半拆**
#: （H1/H2，无自由参数），忠实于"不能只赢在某一段"的本意；跑前定死，登记于执行记录 §6。
STAT_WINDOWS = ("H1", "H2")
GATE_ALPHA = 0.05
GATE2_TOLERANCE = 0.02

PAIR_NAMES = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
              "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
TAIL_CSV = ROOT / "backtest" / "output" / "tail_pair_daily.csv"
OUT = ROOT / "backtest" / "output" / "fifth_bucket_verdict.json"


def _json_safe(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def load_prices_with_tail(tail_csv: Path = TAIL_CSV) -> tuple[pd.DataFrame, pd.Timestamp]:
    """八腿 close + 尾部两腿净值（cumprod）。返回 (prices, 尾部收益首日)。"""
    from signals.common.data_source import load_pg_closes
    closes = load_pg_closes(PAIR_NAMES)
    tail = pd.read_csv(tail_csv, index_col=0, parse_dates=True).dropna()
    nav = (1.0 + tail).cumprod()
    nav.columns = ["尾部成长", "尾部价值"]
    return pd.concat([closes, nav], axis=1), tail.index.min()


def build_factor(prices: pd.DataFrame, names: list[str]) -> pd.Series:
    """N 对腿列名 → 生产函数的 `factor_value`（零平行实现；N 对等权在生产函数内完成）。"""
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
    def __init__(self, start_override: str | None = None, tail_csv: Path = TAIL_CSV):
        prices, tail_start = load_prices_with_tail(tail_csv)
        inc_f = build_factor(prices, PAIR_NAMES)
        cand_f = build_factor(prices, PAIR_NAMES + ["尾部成长", "尾部价值"])
        und, car = load_underlying_returns(KOU_JING), load_carry(KOU_JING)
        idx = inc_f.index.intersection(cand_f.index).intersection(und.index).sort_values()
        tail_days = idx[idx >= tail_start]
        if len(tail_days) <= WARMUP_DAYS:
            raise RuntimeError("尾部序列太短，无法暖机")
        # 公共窗起点：--start（用户裁决的调样期起点，见执行记录 §6——尾部带 2022 年前
        # 物理不存在，起点须在跑 ⓪ 机器前定死）+ 暖机；默认 = 尾部收益首日 + 暖机。
        anchor = max(tail_start, pd.Timestamp(start_override)) if start_override else tail_start
        warm = idx[idx >= anchor]
        start = warm[WARMUP_DAYS] if len(warm) > WARMUP_DAYS else warm[-1]
        idx = idx[idx >= start]
        self.idx, self.tail_start = idx, tail_start
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
        mid = self.idx[len(self.idx) // 2]
        wins = {"full": (None, None), "H1": (None, mid), "H2": (mid, None)}
        out = {}
        for win, (s, e) in wins.items():
            p, u, cc = pos, self.und, self.car
            if s is not None:
                m = p.index >= s
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            if e is not None:
                m = p.index < e
                p, u = p[m], u[m]
                cc = cc[m] if cc is not None else None
            out[win] = evaluate(p, u, cc, COST_BPS, 0)["long"]
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="第 5 桶正式判定（五对 vs 现役四对）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--start", default=None,
                    help="公共窗起点（调样期锚，用户裁决后传入；默认=尾部收益首日）")
    ap.add_argument("--tail-csv", type=Path, default=TAIL_CSV)
    ap.add_argument("--build-metadata", type=Path, default=None)
    ap.add_argument("--output-dir", type=Path, default=OUT.parent)
    args = ap.parse_args(argv)

    build_metadata = args.build_metadata or args.tail_csv.with_name("tail_pair_build.json")
    pit_metadata = load_build_pit_metadata(
        build_metadata, args.tail_csv, "tail_pair_build"
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    d = Data(start_override=args.start, tail_csv=args.tail_csv)
    n = len(d.idx)
    print(f"公共窗 {n} 日（{d.idx.min().date()} → {d.idx.max().date()}；"
          f"尾部收益首日 {d.tail_start.date()} + 暖机 {WARMUP_DAYS}d），口径 {KOU_JING}（含 carry）")
    print(f"平均暴露：现役 {d.inc_pos.mean()*100:.1f}%   候选 {d.cand_pos.mean()*100:.1f}%"
          f"   仓位逐日不同 {int((d.inc_pos != d.cand_pos).sum())} / {n} 天\n")

    inc_m, cand_m = d.metrics(d.inc_pos), d.metrics(d.cand_pos)
    rows = []
    for tag, m in (("现役（四对）", inc_m), ("候选（五对+尾部）", cand_m)):
        rows.append({"方案": tag,
                     **{f"S_{w}": round(m[w]["sharpe"], 4) for w in ("full", "H1", "H2")},
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
        [("plus_tail",)], n_obs=n, stat_fn=stat_fn,
        n_perm=args.n_perm, seed=args.seed, scheme="rotation",
        min_shift=MIN_SHIFT, max_shift=n - MIN_SHIFT,
        statistic_name="sharpe_diff_candidate_minus_incumbent",
        meta={"kou_jing": KOU_JING, "carry": "with", "cost_bps": COST_BPS,
              "candidate": "4_pairs_plus_tail_equal_weight"})

    diff = float(res.observed_best)
    g1 = bool(diff > 0 and res.p_selected < GATE_ALPHA)
    g2 = bool(cand_wtv >= inc_wtv - GATE2_TOLERANCE)
    g3 = bool(cand_m["full"]["maxdd"] >= inc_m["full"]["maxdd"]
              and cand_m["full"]["turnover"] <= inc_m["full"]["turnover"] * 1.05)
    overall = g1 and g2 and g3

    if g1:
        verdict = "① 候选过闸 → 尾部规模带携带现有四对没有的信息（provisional）"
    elif res.p_selected >= GATE_ALPHA:
        verdict = ("② 差异不显著 → 在现役架构与本实现下，尾部的增量不可辨认；"
                   "**不得**表述为「尾部无信息」（首披缺失回退限制 / 实现差异 / 1/5 权重稀释"
                   "都在压效应）")
    else:
        verdict = "③ 显著为负 → 记为「异常，待解释」，找到机制前**不得**宣称「尾部有害」"

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
        "gate1": g1, "gate2": g2, "gate3": g3, "OVERALL": "GO" if overall else "STOP",
        "verdict_case": verdict,
        "pit_metadata": pit_metadata,
        "zero_carry_diff": round(zc, 4),
        "caveats": [item["text"] for item in pit_metadata["limitations"]] + [
            "尾部对加入后存在实现差异与五对等权的 1/5 权重稀释。"
        ],
    }
    print(f"\n── ⓪ 机器（单点候选，n_perm={args.n_perm}）──")
    print(f"  full Sharpe：现役 {out['sharpe_incumbent_full']}  候选 {out['sharpe_candidate_full']}"
          f"  差 {diff:+.4f}")
    print(f"  p_selected = {out['p_selected']:.4f}   p_naive = {out['p_naive']:.4f}（单点应相等）")
    print(f"  关1 {'✓' if g1 else '✗'}  关2 {'✓' if g2 else '✗'}"
          f"（worst_tv 候选 {cand_wtv:.4f} vs 现役 {inc_wtv:.4f}）  关3 {'✓' if g3 else '✗'}")
    print(f"  OVERALL = {out['OVERALL']}")
    print(f"\n  ⭐ r3 §6 判定：{verdict}")

    output = args.output_dir / "fifth_bucket_verdict.json"
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_json_safe), encoding="utf-8")
    print(f"\n→ {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
