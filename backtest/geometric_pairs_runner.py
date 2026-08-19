"""等比 5 桶 × P 族对的构建 runner（预登记 `2026-08-19-geometric-5buckets`，已冻结）。

模式：
  --smoke   单期人工核对（§5-③）：2015-06 与 2026-06 两期，只建腿打印只数/非空率，
            **不算任何收益**。
  （默认）  全窗构建：落盘 10 列日收益 CSV + 期账 JSON。

产物：backtest/output/geo5_pairs_daily.csv（g1_growth..g5_value）+ geo5_pairs_build.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import build_geometric_pairs, rebalance_dates  # noqa: E402

OUTDIR = ROOT / "backtest" / "output"
WINDOW = ("2015-01-01", "2026-08-18")
TERMINAL = pd.Timestamp("2026-08-18")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="等比 5 桶构建")
    ap.add_argument("--smoke", action="store_true", help="单期核对模式（不算收益）")
    args = ap.parse_args(argv)
    t0 = time.time()

    if args.smoke:
        for d in (pd.Timestamp("2015-06-15"), pd.Timestamp("2026-06-15")):
            print(f"\n== 单期核对 @{d.date()}", flush=True)
            build_geometric_pairs([d, TERMINAL], verbose=True, legs_only=True)
        print(f"\nsmoke 完成（{time.time() - t0:.0f}s）", flush=True)
        return 0

    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    print(f"等比 5 桶全窗构建：{len(dates) - 1} 期，{dates[0].date()} → {TERMINAL.date()}",
          flush=True)
    pairs = build_geometric_pairs(dates, verbose=True)
    cols = {}
    for k, p in enumerate(pairs, start=1):
        cols[f"g{k}_growth"], cols[f"g{k}_value"] = p.growth, p.value
    df = pd.DataFrame(cols)
    df.to_csv(OUTDIR / "geo5_pairs_daily.csv")
    meta = {"n_days": int(len(df)),
            "window": [str(df.index.min().date()), str(df.index.max().date())],
            "n_by_date": {f"g{k}": {d: [p.n_growth[d], p.n_value[d]] for d in p.n_growth}
                          for k, p in enumerate(pairs, start=1)},
            "skipped": pairs[0].skipped,
            "elapsed_s": round(time.time() - t0, 1)}
    (OUTDIR / "geo5_pairs_build.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"落盘 geo5_pairs_daily.csv（{len(df)} 天）+ geo5_pairs_build.json"
          f"（skipped={pairs[0].skipped}，{meta['elapsed_s']}s）", flush=True)
    print("GEO5 BUILD DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
