"""多头增强轴探针·第一批：基差率 + 广度多头向（五族 × 三关闸门）。

设计：docs/plans/2026-07-09-long-axes-probe-design.md。背景：五轴空头证伪
收官后，方向一的多头半边（生产 long-flat 腿的增强候选）从未测过——本轮补
设计稿 §2 两个零数据等待的 P1 轴：

- 基差率轴（面5 ⭐）：C1 水平 / C2 20d 变化（`backtest.data.load_carry`，
  blend，IC 2015-04 起、止 2026-04-29 历史段）；
- 广度多头向轴（面2）：B1 参与面水平 / B2 扩张启动(diff20) / B3 新高新低差
  （breadth.csv 缓存；Phase4 只证伪了其空头背离事件形态，连续方向信息未测）。

两轴皆当日收盘即知 → 无 pit_lag。双侧检验，方向由数据裁决；探针机器全复用
（level_signal / run_families_probe）。任一族过闸 → 多头装配设计（对照 =
long-flat Sharpe 1.42）；全负 → 多头增强第一批归档。

CLI: python3 -m backtest.long_axes_probe [--families C1,C2,B1,B2,B3] [--n-perm 1000]
产出: backtest/output/long_axes_probe{,_verdicts}.csv。
运行需 PG（carry/标的收益）；链路抖动期带 PGCONNECT_TIMEOUT 快速失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import (  # noqa: E402
    GRID_LEVEL,
    level_signal,
    run_families_probe,
)
from backtest.rotation_probe import HALVES  # noqa: E402

GRID_K_LONG = (5, 10, 20, 40)
FAMILIES_ALL = ("C1", "C2", "B1", "B2", "B3")


# ---------------------------------------------------------------- 纯函数
def build_long_signals(carry: pd.Series, breadth: pd.DataFrame
                       ) -> dict[str, dict[str, pd.Series]]:
    """五族×网格装配（当日收盘即知 → 无 pit_lag）。

    C1=carry 水平 / C2=carry 20d 变化 / B1=pct_above_ma20 水平 /
    B2=其 20d 变化（"扩张启动"）/ B3=hi_lo_diff20。
    """
    src = {
        "C1": carry,
        "C2": carry.diff(20),
        "B1": breadth["pct_above_ma20"],
        "B2": breadth["pct_above_ma20"].diff(20),
        "B3": breadth["hi_lo_diff20"],
    }
    return {
        fam: {f"{fam}_lb{lb}zw{zw}": level_signal(series.dropna(), lb, zw)
              for lb, zw in GRID_LEVEL}
        for fam, series in src.items()
    }


# ---------------------------------------------------------------- 编排
def run_probe(families: tuple[str, ...] = FAMILIES_ALL, n_perm: int = 1000,
              cost_bps: float = 3.0, db=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    from backtest.breadth_dual import load_breadth_cache
    from backtest.data import load_carry

    carry = load_carry("blend", db=db)
    breadth = load_breadth_cache()
    sigs_all = build_long_signals(carry, breadth)
    return run_families_probe(sigs_all, families, GRID_K_LONG, n_perm, cost_bps, db)


def main() -> int:
    ap = argparse.ArgumentParser(description="多头增强轴探针（基差率+广度，五族×三关）")
    ap.add_argument("--families", default=",".join(FAMILIES_ALL))
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    panel, verdicts = run_probe(families, args.n_perm, args.cost_bps)

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out_dir / "long_axes_probe.csv", index=False)
    verdicts.to_csv(out_dir / "long_axes_probe_verdicts.csv", index=False)

    show = panel[panel["kou_jing"] == "blend"].copy()
    for c in ["ic"] + [f"ic_{h}" for h in HALVES]:
        show[c] = show[c].round(3)
    print("=== IC 面板（blend 口径） ===")
    print(show.drop(columns=["kou_jing"]).to_string(index=False))
    print("\n=== 逐族三关裁决 ===")
    for _, v in verdicts.iterrows():
        print(f"\n-- {v['family']} --")
        for key, val in v.items():
            if key == "family":
                continue
            print(f"  {key}: {val:.4f}" if isinstance(val, float) else f"  {key}: {val}")
    n_pass = int(verdicts["PASS"].sum())
    print(f"\n{'★ PASS：' + str(n_pass) + ' 族过闸 → 进多头装配设计（对照 long-flat 1.42）'
          if n_pass else '✗ STOP：全族停线 → 多头增强第一批归档'}")
    print(f"→ {out_dir / 'long_axes_probe.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
