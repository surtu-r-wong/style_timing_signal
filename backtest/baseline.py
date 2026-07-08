"""基线编排器：三条信号线 × 三口径 × 分段窗口 × (整段/多头段/空头段) → 指标表。

CLI: python3 -m backtest.baseline [--source pg] [--mode discrete|proportional]
     [--bootstrap N]
产出: backtest/output/baseline_metrics.csv + console。
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy, segment_returns  # noqa: E402
from backtest.metrics import (  # noqa: E402
    ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
)
from backtest.positions import to_position  # noqa: E402
from backtest.significance import bootstrap_pvalue  # noqa: E402

SIGNALS = {
    "hybrid20": ("output/hybrid20/confirmed_signal.csv", "hybrid_20"),
    "citic40d": ("output/citic40d/citic_style_signal_40d.csv", "factor_20"),
    "equal_weight": ("output/equal_weight/equal_weight_signal_20d40z.csv", "factor_value"),
}
KOU_JING = ["500", "1000", "blend"]
WINDOWS = {
    "full": (None, None),
    "2014-2020": ("2014-01-01", "2020-12-31"),
    "2021-2023": ("2021-01-01", "2023-12-31"),
    "2024-2026": ("2024-01-01", "2026-12-31"),
}
_MIN_OBS = 60


def _row(ret, position):
    return {
        "ann": ann_return(ret), "sharpe": sharpe(ret), "maxdd": max_drawdown(ret),
        "calmar": calmar(ret), "turnover": turnover(position), "hit": hit_rate(ret),
        "n_obs": int(len(ret)),
    }


def evaluate(position, underlying, carry=None, cost_bps=3.0, bootstrap_n=0, seed=0) -> dict:
    """整段 + 多头段 + 空头段指标（对齐两序列的公共日期）。"""
    idx = position.index.intersection(underlying.index)
    pos = position.reindex(idx).astype(float)
    und = underlying.reindex(idx)
    car = carry.reindex(idx) if carry is not None else None

    full = run_strategy(pos, und, cost_bps, car)["ret"]
    long_ret, short_ret = segment_returns(pos, und, cost_bps, car)
    segs = {
        "full": (full, pos),
        "long": (long_ret, pos.clip(lower=0)),
        "short": (short_ret, pos.clip(upper=0)),
    }
    out = {}
    for seg, (ret, p) in segs.items():
        row = _row(ret, p)
        if bootstrap_n:
            row["pvalue"] = bootstrap_pvalue(p, und, "sharpe", bootstrap_n, seed, car, cost_bps)
        out[seg] = row
    return out


def _slice(s, start, end):
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    return s


def load_signal(name, mode="discrete", signals=None):
    path, col = (signals or SIGNALS)[name]
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    return to_position(df[col], mode=mode)


def build_report(mode="discrete", bootstrap_n=500, seed=0, cost_bps=3.0, db=None,
                 signals=None, positions=None) -> pd.DataFrame:
    """signals: {name: (path, col)}，默认三条生产线；positions: 直接传已映射仓位
    dict（跳过文件加载，供外部信号同秤评估）。两者都给时 positions 优先。"""
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}
    signals = signals or SIGNALS
    sig_all = positions or {name: load_signal(name, mode, signals) for name in signals}

    rows = []
    for name, pos_full in sig_all.items():
        for kj in KOU_JING:
            for win, (s, e) in WINDOWS.items():
                pos = _slice(pos_full, s, e)
                und = _slice(und_all[kj], s, e)
                car = _slice(car_all[kj], s, e)
                if len(pos.index.intersection(und.index)) < _MIN_OBS:
                    continue
                for seg, m in evaluate(pos, und, car, cost_bps, bootstrap_n, seed).items():
                    rows.append({"signal": name, "kou_jing": kj, "window": win, "seg": seg, **m})

    # buy-hold 基准（满仓多头，无换手）
    for kj in KOU_JING:
        for win, (s, e) in WINDOWS.items():
            u = _slice(und_all[kj], s, e)
            if len(u) < _MIN_OBS:
                continue
            rows.append({"signal": "buy_hold", "kou_jing": kj, "window": win, "seg": "full",
                         **_row(u, pd.Series(1.0, index=u.index) * 0)})  # turnover 0
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase2 修秤：三条线真实基线")
    ap.add_argument("--source", default="pg", choices=["pg"])
    ap.add_argument("--mode", default="discrete", choices=["discrete", "proportional"])
    ap.add_argument("--bootstrap", type=int, default=500)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    rep = build_report(mode=args.mode, bootstrap_n=args.bootstrap, cost_bps=args.cost_bps)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out_dir / "baseline_metrics.csv", index=False)

    show = rep.copy()
    for c in ["ann", "maxdd", "hit"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["sharpe", "calmar", "turnover"]:
        show[c] = show[c].round(2)
    if "pvalue" in show:
        show["pvalue"] = show["pvalue"].round(3)
    print(show.to_string(index=False))
    print(f"\n→ {out_dir / 'baseline_metrics.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
