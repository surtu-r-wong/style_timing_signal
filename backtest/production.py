"""推荐 production 持仓生成器 —— 逐线映射口径见 PRODUCTION_MAPPING。

把三条信号线的产出映射到推荐持仓，写 output/recommended/<name>_<mapping>.csv。
- hybrid20 / citic40d：long-flat（Phase 3 v1 采纳，方向A；空头腿 Sharpe 0.49 / 0.05）。
- equal_weight：**对称**（2026-09-09 用户裁决"部署空头"，决策记录
  docs/plans/2026-09-09-deploy-symmetric-equal-weight-decision.md）。long-flat 文件继续并行产出作参照。
信号 CSV 本身不改（字节回归护栏）——本模块只做下游持仓口径，读 committed 信号产出。
回滚 = 把 PRODUCTION_MAPPING["equal_weight"] 改回 "longflat"。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import SIGNALS  # noqa: E402
from backtest.positions import production_position, symmetric_position  # noqa: E402

PRODUCTION_MAPPING = {"hybrid20": "longflat", "citic40d": "longflat", "equal_weight": "symmetric", "slope20": "symmetric"}   # slope20 2026-09-09 上线，空头腿 0.62 同形态
MAPPERS = {"longflat": production_position, "symmetric": symmetric_position}
# 下游（仪表盘 / 新鲜度护栏 / 合并导出）一律从这里取推荐持仓文件，不要自己拼文件名。
RECOMMENDED_FILES = {name: f"output/recommended/{name}_{m}.csv" for name, m in PRODUCTION_MAPPING.items()}
# 参照产出：非现役口径也照常写，便于对照与回滚（equal_weight 的 long-flat）。
REFERENCE_OUTPUTS = {"equal_weight": "longflat"}


def recommended_position_frame(name: str, threshold: float = 0.0, mapping: str | None = None) -> pd.DataFrame:
    """读 committed 信号产出 → 推荐持仓 DataFrame(date, position)。mapping 缺省取 PRODUCTION_MAPPING。"""
    path, col = SIGNALS[name]
    raw = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    pos = MAPPERS[mapping or PRODUCTION_MAPPING[name]](raw[col], threshold=threshold)
    return pos.rename("position").reset_index().rename(columns={"index": "date"})


def write_recommended_positions(out_dir: Path | None = None) -> dict[str, Path]:
    out_dir = out_dir or (ROOT / "output" / "recommended")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, m in PRODUCTION_MAPPING.items():
        path = out_dir / f"{name}_{m}.csv"
        recommended_position_frame(name).to_csv(path, index=False)
        written[name] = path
    for name, m in REFERENCE_OUTPUTS.items():
        path = out_dir / f"{name}_{m}.csv"
        recommended_position_frame(name, mapping=m).to_csv(path, index=False)
        written[f"{name}[{m} 参照]"] = path
    return written


def main() -> int:
    written = write_recommended_positions()
    for name, path in written.items():
        df = pd.read_csv(path)
        print(f"{name:24s} → {path.name}  ({len(df)} 行, 持多 {(df['position'] > 0).mean():.0%} / 持空 {(df['position'] < 0).mean():.0%}, "
              f"末日 {df['date'].iloc[-1]} pos={int(df['position'].iloc[-1])})")
    print("\n推荐口径：" + ", ".join(f"{k}={v}" for k, v in PRODUCTION_MAPPING.items()) + "（equal_weight 对称 = 2026-09-09 用户裁决）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
