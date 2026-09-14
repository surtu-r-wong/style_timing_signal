"""slope20 生产信号 —— 生产第一替补（07-11 登记）2026-09-09 用户裁决升为第四条生产线，与 equal_weight 并行。

定义（与 backtest.fusion_probe.build_factors 的 "slope20" 逐位相同）：
  每腿 20 日对数价格 OLS 斜率 → 成长减价值（config_4pairs 四对）→ 120 日 z → tanh(z/2) → 四对等权 → 不平滑。
  = momentum_scan.momentum_factor_fn()(family="slope", length=20, skip=0, z_window=120, smoothing=0)
输出 output/slope20/slope20_signal_L20zw120.csv（date, factor_value），全量重算覆写。
CLI: python3 signals/slope20/generate_signal.py [--output PATH] [--start YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAMILY, LENGTH, SKIP, Z_WINDOW, SMOOTHING = "slope", 20, 0, 120, 0
OUTPUT = ROOT / "output" / "slope20" / "slope20_signal_L20zw120.csv"


def build(start=None):
    from backtest.momentum_scan import momentum_factor_fn
    return momentum_factor_fn(start)(family=FAMILY, length=LENGTH, skip=SKIP, z_window=Z_WINDOW, smoothing=SMOOTHING)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--output", default=str(OUTPUT)); ap.add_argument("--start", default=None); a = ap.parse_args(argv)
    fac = build(a.start).rename("factor_value").dropna()
    out = Path(a.output); out.parent.mkdir(parents=True, exist_ok=True)
    fac.round(4).to_csv(out, index_label="date")
    print(f"slope20 → {out}  {len(fac)} 行  {fac.index.min().date()}..{fac.index.max().date()}  末值 {fac.iloc[-1]:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
