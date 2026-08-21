"""尾部对（第 5 桶）全窗构建 —— Gate 0 双过（2026-08-19）之后的解禁跑。

`build_tail_pair`（r3 预登记 §2/§3：官方链余集 + P 族分类、无「前 50%」条款）全窗滚动，
落盘两腿日收益与期账。窗口与 Gate 0A 同（2015-06 起，终点 2026-08-18）。

产物：
  backtest/output/tail_pair_daily.csv    （growth / value 两腿日收益）
  backtest/output/tail_pair_build.json   （期账：每期腿只数、skipped、耗时）
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from backtest.pure_style_builder import build_tail_pair, rebalance_dates

ROOT = Path(__file__).resolve().parents[1]
OUTDIR = ROOT / "backtest" / "output"
WINDOW = ("2015-01-01", "2026-08-18")
TERMINAL = pd.Timestamp("2026-08-18")

def run_full_build(output_dir: Path) -> int:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    print(f"尾部对全窗构建：{len(dates) - 1} 期，{dates[0].date()} → {TERMINAL.date()}", flush=True)
    pair = build_tail_pair(dates, verbose=True)

    df = pd.concat([pair.growth.rename("growth"), pair.value.rename("value")], axis=1)
    df.to_csv(output_dir / "tail_pair_daily.csv")
    meta = {"n_days": int(len(df)),
            "window": [str(df.index.min().date()), str(df.index.max().date())],
            "n_by_date": {k: [pair.n_growth[k], pair.n_value[k]] for k in pair.n_growth},
            "skipped": pair.skipped,
            "elapsed_s": round(time.time() - t0, 1)}
    (output_dir / "tail_pair_build.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"落盘 tail_pair_daily.csv（{len(df)} 天）+ tail_pair_build.json"
          f"（skipped={pair.skipped}，{meta['elapsed_s']}s）", flush=True)
    print("TAIL BUILD DONE", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="尾部对全窗构建")
    ap.add_argument("--output-dir", type=Path, default=OUTDIR)
    args = ap.parse_args(argv)
    return run_full_build(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
