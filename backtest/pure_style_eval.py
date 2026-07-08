"""B2 切主评估：纯风格(U2 行业中性) / 自建混合(U2) / equal_weight 生产基线 三方同秤。

同秤 = backtest.baseline 全套口径（离散 {−1,0,+1} 仓位、3bp+空头 carry、三口径、
分段窗口、整段/多头段/空头段、bootstrap 显著性）+ **公共窗对齐**（三信号起点不同，
full 窗必须同窗才可比——align_common_window 取交集索引）。

判据（设计稿 §2.4 / B2 记录文档）：纯风格的月频 IC 优势（0.179 vs 0.126）能否在
换手/成本后转化为净值优势；production 口径重点看多头段（long-flat 已是生产推荐）。

CLI: python3 -m backtest.pure_style_eval [--bootstrap N] [--universe U2]
产出: backtest/output/pure_style_eval.csv + console。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import SIGNALS, build_report, load_signal  # noqa: E402


def align_common_window(signals: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """多条信号对齐到公共交集窗（同秤前提：同窗对比）。保持传入顺序。"""
    common = None
    for s in signals.values():
        common = s.index if common is None else common.intersection(s.index)
    return {name: s.reindex(common) for name, s in signals.items()}


def eval_signals(universe: str = "U2", mode: str = "discrete",
                 bootstrap_n: int = 500, cost_bps: float = 3.0, db=None) -> pd.DataFrame:
    extra = {
        f"self_mixed_{universe}": (f"output/style_basket/signal_self_mixed_{universe}.csv",
                                   "factor_value"),
        f"pure_style_{universe}": (f"output/style_basket/signal_pure_style_{universe}.csv",
                                   "factor_value"),
    }
    sigs = {"equal_weight": SIGNALS["equal_weight"], **extra}
    positions = align_common_window(
        {name: load_signal(name, mode, sigs) for name in sigs}
    )
    return build_report(mode=mode, bootstrap_n=bootstrap_n, cost_bps=cost_bps,
                        db=db, signals=sigs, positions=positions)


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 切主评估：三方同秤")
    ap.add_argument("--universe", default="U2")
    ap.add_argument("--mode", default="discrete", choices=["discrete", "proportional"])
    ap.add_argument("--bootstrap", type=int, default=500)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    rep = eval_signals(args.universe, args.mode, args.bootstrap, args.cost_bps)
    out = ROOT / "backtest" / "output" / "pure_style_eval.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out, index=False)

    show = rep.copy()
    for c in ["ann", "maxdd", "hit"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["sharpe", "calmar", "turnover"]:
        show[c] = show[c].round(2)
    if "pvalue" in show:
        show["pvalue"] = show["pvalue"].round(3)
    print(show.to_string(index=False))
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
