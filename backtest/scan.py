"""Walk-forward 参数扫描（设计稿 §3.2 Step 2/3）。

对一组参数组合（combos），逐个用 factor_fn 生成因子 → 离散仓位 → 各评价窗口 Sharpe。
"对外只认 OOS"：分 train/val/holdout 报，选 Sharpe 高原（跨窗口稳定）而非某窗尖峰。
不做 bootstrap（扫描阶段控时长），显著性在锁定参数后单独复核。
"""
import itertools
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.positions import to_position  # noqa: E402


def _slice(s, start, end):
    if s is None:
        return None
    if start:
        s = s[s.index >= pd.Timestamp(start)]
    if end:
        s = s[s.index <= pd.Timestamp(end)]
    return s


def scan_grid(factor_fn, combos, underlying, carry=None, windows=None,
              cost_bps=3.0, mode="discrete") -> pd.DataFrame:
    rows = []
    for params in combos:
        pos = to_position(factor_fn(**params), mode=mode)
        row = dict(params)
        for win, (s, e) in windows.items():
            p = _slice(pos, s, e)
            u = _slice(underlying, s, e)
            c = _slice(carry, s, e)
            idx = p.index.intersection(u.index)
            ret = run_strategy(
                p.reindex(idx), u.reindex(idx), cost_bps,
                c.reindex(idx) if c is not None else None,
            )["ret"]
            row[f"sharpe_{win}"] = sharpe(ret)
        rows.append(row)
    return pd.DataFrame(rows)


# ---- §3.2 参数网格 ----
def default_grid():
    """lookback × z_window(2/3/5×lb 及 250) × smoothing。z_window 依赖 lookback。"""
    combos = []
    for lb in (5, 10, 20, 40, 60):
        for zw in (2 * lb, 3 * lb, 5 * lb, 250):
            for sm in (0, 5):
                combos.append({"lookback": lb, "z_window": zw, "smoothing": sm})
    return combos


def equal_weight_factor_fn(start=None):
    """返回 fn(lookback, z_window, smoothing) → factor_value（读 PG 4 对，一次性加载）。"""
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal, load_pair_configs,
    )
    names = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
             "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
    prices = load_pg_closes(names, start=start)
    pair_configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")

    def fn(lookback, z_window, smoothing):
        out = calculate_contrast_equal_weight_signal(
            prices, lookback=lookback, z_window=z_window,
            smoothing_window=smoothing, pair_configs=pair_configs,
        )
        return out["factor_value"]

    return fn


def main() -> int:
    from backtest.data import load_carry, load_underlying_returns
    kj = "blend"
    windows = {
        "train_14_20": ("2014-01-01", "2020-12-31"),
        "val_21_23": ("2021-01-01", "2023-12-31"),
        "holdout_24_26": ("2024-01-01", "2026-12-31"),
    }
    fn = equal_weight_factor_fn()
    und = load_underlying_returns(kj)
    car = load_carry(kj)
    rep = scan_grid(fn, default_grid(), und, car, windows)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out_dir / "scan_equal_weight.csv", index=False)
    rep_show = rep.round(2).sort_values("holdout_24_26", ascending=False)
    print(rep_show.to_string(index=False))
    print(f"\n当前默认参数=lookback20/z40/smooth5。→ {out_dir / 'scan_equal_weight.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
