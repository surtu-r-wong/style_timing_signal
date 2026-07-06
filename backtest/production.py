"""推荐 production 持仓生成器 —— long-flat 口径（Phase 3 v1 采纳，方向A）。

把三条信号线的产出映射到推荐持仓（砍空头，只多/空仓），写 output/recommended/。
依据：双引擎 v1 实证空头段无独立盈利也无避险价值（long-flat 优于对称多空）。
信号 CSV 本身不改（字节回归护栏）——本模块只做下游持仓口径，读committed 信号产出。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import SIGNALS  # noqa: E402
from backtest.positions import production_position  # noqa: E402


def recommended_position_frame(name: str, threshold: float = 0.0) -> pd.DataFrame:
    """读 committed 信号产出 → long-flat 推荐持仓 DataFrame(date, position∈{0,1})。"""
    path, col = SIGNALS[name]
    raw = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    pos = production_position(raw[col], threshold=threshold)
    return pos.rename("position").reset_index().rename(columns={"index": "date"})


def write_recommended_positions(out_dir: Path | None = None) -> dict[str, Path]:
    out_dir = out_dir or (ROOT / "output" / "recommended")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name in SIGNALS:
        df = recommended_position_frame(name)
        path = out_dir / f"{name}_longflat.csv"
        df.to_csv(path, index=False)
        written[name] = path
    return written


def main() -> int:
    written = write_recommended_positions()
    for name, path in written.items():
        df = pd.read_csv(path)
        share = df["position"].mean()
        print(f"{name:14s} → {path.name}  ({len(df)} 行, 持多占比 {share:.0%}, "
              f"末日 {df['date'].iloc[-1]} pos={int(df['position'].iloc[-1])})")
    print("\n推荐口径 = long-flat（砍空头）。依据见 dual_engine_metrics.csv 的 long_engine 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
