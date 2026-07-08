"""自建篮子价差 → 择时信号 CSV（equal_weight 生产管线口径，schema 兼容 backtest/）。

spread_<U>[_neutral].csv 的两腿累计净值走 _compute_pair_signal(lookback=20, z_window=40)
+ rolling(5) 平滑——与 equal_weight_signal_20d40z 完全同参，产出可直接进 backtest
同秤对比。输出列：date, factor_value_raw, factor_value。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402
from signals.style_basket.build import OUT_DIR  # noqa: E402


def make_signal(uni: str = "U2", neutral: bool = False,
                lookback: int = 20, z_window: int = 40, smoothing: int = 5) -> Path:
    suffix = "_neutral" if neutral else ""
    spread = pd.read_csv(
        OUT_DIR / f"spread_{uni}{suffix}.csv", parse_dates=["date"]
    ).set_index("date")
    raw = _compute_pair_signal(
        spread["growth_index"], spread["value_index"], lookback=lookback, z_window=z_window
    )
    out = pd.DataFrame({"factor_value_raw": raw})
    out["factor_value"] = (
        raw if smoothing == 0 else raw.rolling(smoothing, min_periods=1).mean()
    )
    tag = "pure_style" if neutral else "self_mixed"
    out_file = OUT_DIR / f"signal_{tag}_{uni}.csv"
    out.to_csv(out_file, index_label="date")
    print(f"[make_signal] {tag}_{uni}: {len(out)} days "
          f"({out.index.min().date()}..{out.index.max().date()}) -> {out_file.name}")
    return out_file


def main() -> int:
    ap = argparse.ArgumentParser(description="自建篮子价差 → 择时信号 CSV")
    ap.add_argument("--universe", default="U2")
    ap.add_argument("--both", action="store_true", help="同时产出混合(v1)与纯风格(v2 neutral)")
    args = ap.parse_args()
    make_signal(args.universe, neutral=True)
    if args.both:
        make_signal(args.universe, neutral=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
