"""hybrid20 状态机 4 阈值 walk-forward 扫描（Phase 3 T6）。

bespoke（非 §3.2 lookback/z/smoothing 网格）：hybrid20 的可调量是非对称状态机阈值。
在**干净的等权 CITIC 复合因子**（= citic40d 因子，避开 hybrid20 IC 加权的全样本前视）上，
用 hybrid20.make_signal 状态机跑各阈值组合的 walk-forward Sharpe + 空头段诊断，
独立检验"CITIC 轴短腿是否也无价值 / 空头门槛是否须更高"。
"""
import itertools
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy, segment_returns  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.scan import _slice  # noqa: E402


def threshold_grid(open_longs=(0.1, 0.2, 0.3, 0.4),
                   open_shorts=(-0.1, -0.2, -0.3, -0.4),
                   hysteresis: float = 0.1) -> list[dict]:
    """开多/开空阈两轴网格，平多/平空阈由固定 hysteresis 带派生（满足状态机约束）。"""
    combos = []
    for ol, os_ in itertools.product(open_longs, open_shorts):
        combos.append({
            "open_long": ol,
            "close_long": max(0.0, ol - hysteresis),
            "open_short": os_,
            "close_short": min(0.0, os_ + hysteresis),
        })
    return combos


def scan_thresholds(factor, underlying, carry, combos, windows, cost_bps=3.0) -> pd.DataFrame:
    import signals.hybrid20.optimize_signal as hyb

    rows = []
    for t in combos:
        sig = hyb.make_signal(factor, t).fillna(0.0)
        row = dict(t)
        for win, (s, e) in windows.items():
            p = _slice(sig, s, e)
            u = _slice(underlying, s, e)
            idx = p.index.intersection(u.index)
            c = _slice(carry, s, e).reindex(idx) if carry is not None else None
            ret = run_strategy(p.reindex(idx), u.reindex(idx), cost_bps, c)["ret"]
            row[f"sharpe_{win}"] = sharpe(ret)

        idx = sig.index.intersection(underlying.index)
        c_all = carry.reindex(idx) if carry is not None else None
        _, short_ret = segment_returns(sig.reindex(idx), underlying.reindex(idx), cost_bps, c_all)
        row["short_sharpe"] = sharpe(short_ret)
        row["short_frac"] = float((sig.reindex(idx) < 0).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    from backtest.data import load_carry, load_underlying_returns
    from signals.citic40d.generate_signal import compute_mean_factor
    from signals.common.data_source import load_pg_closes

    style = load_pg_closes(
        ["稳定", "成长", "金融", "周期", "消费"], trim_ragged_tail=True,
    ).rename(columns={"稳定": "stability", "成长": "growth", "金融": "finance",
                      "周期": "cycle", "消费": "consumption"})
    factor = compute_mean_factor(style, n=20, z_window=250, smoothing=0)  # hybrid20 因子族(等权)

    kj = "blend"
    und = load_underlying_returns(kj)
    car = load_carry(kj)
    windows = {
        "train_14_20": ("2014-01-01", "2020-12-31"),
        "val_21_23": ("2021-01-01", "2023-12-31"),
        "holdout_24_26": ("2024-01-01", "2026-12-31"),
    }
    rep = scan_thresholds(factor, und, car, threshold_grid(), windows)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "scan_hybrid20_thresholds.csv"
    rep.to_csv(out_path, index=False)

    show = rep.round(2).sort_values("sharpe_holdout_24_26", ascending=False)
    print(show.to_string(index=False))
    print(f"\nhybrid20 现默认阈值: open_long .35 / close_long .1 / open_short −.15 / close_short −.1")
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
