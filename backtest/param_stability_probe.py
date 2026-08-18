"""参数 × 标的 稳健性诊断（**防守性读数，不是候选搜索**）。

## 问题（一个真实的空白）

勿重开清单 #4 说"lb=20 甜点跨 equal_weight/citic40d 两条独立线交叉验证，现有参数近
最优，增益是结构性不是参数性"。但**两条线都只跑在 500/1000/blend 口径上**
（`scan.py --kj` 只接受这三个）。lb=20 在中证2000 / 万得微盘 / 沪深300 / 中证红利上
是落在**高原**还是**悬崖边**，从未测过。

这是关于**现役部署脆不脆**的问题，不是找增量的问题。两者的区别决定了读法。

## 读法（跑前写死，防事后择读；本 docstring 由判例钉死）

- **R1 主读数** = 现役点 `(lb=20, zw=40, sm=5)` 在每个标的的 40 格里**排第几**、
  与该标的网格最优的**绝对差**是多少。判据是"现役点是否仍在前列且离最优不远"，
  **不是**"哪一格最高"。
- **R2 邻域平坦度** = 沿 `zw=2·lb, sm=5` 这条线看 lb ∈ {5,10,20,40,60} 的曲线，
  lb=20 与其左右邻居的绝对差。差小 = 高原；某一侧断崖 = 该标的上参数脆。
- **R3 argmax 是否系统性移动**：若各标的的最优 lb 随规模/波动**单调**移动，才算
  机制（项目对"跨独立单元的单调性"有先例：⑧ 预登记的 15 格双向单调）；
  **散乱的 argmax = 噪声，不得解释**。
- **R4 不产出"最优组合"作为候选、不进任何预登记网格。** 任何部署变更必须走
  `backtest/selection_permutation.py`（⓪ 机器）对**全部 40×6=240 点**出 `p_selected`
  —— ①a 判例原话"不许摘取"。本模块的输出**不支持部署变更**。
- **R5 排序键 = worst(train, val)**（高原不尖峰，沿用 `scan.py` §3.2 方法论）；
  holdout 列仅报告，**不进任何选择或排序**。

## 口径

零 carry（跨标的唯一可比口径，同 `underlying_probe`）、`cost_bps=3.0`、
仓位映射 = 现役 **long-flat θ=0**（`production_position`）—— 注意 `scan.py` 默认的
`to_position(mode="discrete")` 是**对称 ±1**，与部署口径不同，故这里先把因子映射成
long-flat 再喂进去（对 0/1 序列 `to_position` 恰是恒等，零侵入复用 `scan_grid`）。

## 用法

    python3 -m backtest.param_stability_probe
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.scan import default_grid, equal_weight_factor_fn, scan_grid  # noqa: E402
from backtest.underlying_probe import SINGLES, load_index_returns  # noqa: E402

#: 现役部署点（信号文件名 `equal_weight_signal_20d40z` = lb 20 / zw 40；部署用平滑列）
INCUMBENT = {"lookback": 20, "z_window": 40, "smoothing": 5}
OUT = ROOT / "backtest" / "output" / "param_stability_probe.csv"


def longflat_factor_fn():
    """`scan_grid` 要的 fn(**params) → 仓位序列，口径 = 现役 long-flat θ=0。

    记忆化：因子**完全不依赖标的**，40 组只算一次，6 个标的复用。
    """
    raw = equal_weight_factor_fn()

    @lru_cache(maxsize=None)
    def _cached(lookback: int, z_window: int, smoothing: int) -> pd.Series:
        return production_position(raw(lookback=lookback, z_window=z_window,
                                       smoothing=smoothing)).astype(float)

    def fn(**params) -> pd.Series:
        return _cached(params["lookback"], params["z_window"], params["smoothing"])

    return fn


def run(cost_bps: float = 3.0) -> pd.DataFrame:
    fn = longflat_factor_fn()
    combos = default_grid()
    frames = []
    for key, (code, name) in SINGLES.items():
        und = load_index_returns(code)
        rep = scan_grid(fn, combos, und, carry=None, windows=WINDOWS,
                        cost_bps=cost_bps)          # 零 carry：跨标的唯一可比口径
        rep.insert(0, "underlying", f"{key}｜{name}")
        frames.append(rep)
    out = pd.concat(frames, ignore_index=True)
    out["worst_tv"] = out[["sharpe_2014-2020", "sharpe_2021-2023"]].min(axis=1)
    return out


def _is_incumbent(df: pd.DataFrame) -> pd.Series:
    return ((df["lookback"] == INCUMBENT["lookback"])
            & (df["z_window"] == INCUMBENT["z_window"])
            & (df["smoothing"] == INCUMBENT["smoothing"]))


def report(rep: pd.DataFrame) -> None:
    print(f"网格 {len(default_grid())} 组 × 标的 {rep['underlying'].nunique()} 个 "
          f"= {len(rep)} 点；排序键 = worst(train,val)，零 carry\n")

    print("── R1 现役点 (lb=20, zw=40, sm=5) 在各标的网格中的位置 ──")
    rows = []
    for u, g in rep.groupby("underlying", sort=False):
        g = g.sort_values("worst_tv", ascending=False).reset_index(drop=True)
        inc = g[_is_incumbent(g)]
        best = g.iloc[0]
        rank = int(inc.index[0]) + 1
        rows.append({
            "标的": u, "现役 worst_tv": round(float(inc.iloc[0]["worst_tv"]), 4),
            "网格最优": round(float(best["worst_tv"]), 4),
            "绝对差": round(float(best["worst_tv"] - inc.iloc[0]["worst_tv"]), 4),
            "现役排名": f"{rank}/{len(g)}",
            "最优参数": f"lb{int(best['lookback'])}/zw{int(best['z_window'])}"
                       f"/sm{int(best['smoothing'])}",
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n── R2 邻域平坦度：沿 zw=2·lb, sm=5 看 lb 曲线（worst_tv）──")
    line = rep[(rep["z_window"] == 2 * rep["lookback"]) & (rep["smoothing"] == 5)]
    print(line.pivot_table(index="underlying", columns="lookback",
                           values="worst_tv", sort=False).round(3).to_string())

    print("\n── R3 各标的 argmax 的 lookback（看是否系统性移动，散乱=噪声）──")
    am = rep.loc[rep.groupby("underlying", sort=False)["worst_tv"].idxmax()]
    print(am[["underlying", "lookback", "z_window", "smoothing", "worst_tv"]]
          .round(4).to_string(index=False))

    print("\n⚠️ R4：以上不产出候选。任何部署变更须走 ⓪ 机器对全部 "
          f"{len(rep)} 点出 p_selected（①a 判例：不许摘取）。")


def main() -> int:
    rep = run()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(OUT, index=False)
    report(rep)
    print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
