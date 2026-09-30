"""阈值 θ × 执行标的 的机制检验（**事前预测的可证伪检验，不是候选搜索**）。

## 为什么单独做这一条

`param_stability_probe` 扫的是 lookback / z_window / smoothing，**θ 从头到尾钉死在 0**。
而阈值维度在本项目从未扫过（部署一直是 `production_position(signal, threshold=0.0)`）。

更重要的是：**它不是"再多扫一维"，而是有事前机制预测的。**
`2026-08-18-microcap-upper-bound.md` §2 为解释"信号在微盘上增量最低"给出的机制是

> **标的自身漂移越强，long-flat 离场的机会成本越大，择时能加的相对价值就越小。**

这个机制对阈值有**直接的方向性预测**：θ 决定在场比例（θ↑ → 在场↓）。漂移越强的标的，
离场越贵 ⇒ **最优 θ 应当越低**。6 个标的的买入持有年化从 5.6%（中证1000）到
36.3%（微盘）跨了 6 倍多，若机制为真，最优 θ 应随之单调下移。

**这就把一次网格从"搜索"变成了"对一个已写下的机制做可证伪检验"** —— 项目对
"跨独立单元的单调性"有先例（⑧ 预登记的 15 格双向单调、事先可预测）。

## 读法（跑前写死，防事后择读）

- **R1** 各标的最优 θ（键 = `worst(train, val)`，高原不尖峰，沿用 §3.2）+ 现役 θ=0
  与之的绝对差 + 对应在场比例。
- **R2 机制检验（主判据，可证伪）**：把 6 个标的按**买入持有年化**排序，看最优 θ 是否
  **单调不增**。全单调 → 机制在阈值维上得到独立支持；散乱 → 机制在这一维不成立
  （或被噪声淹没），**必须如实报告为"预测未兑现"，不得改口**。
- **R3 曲线形状**：最优 θ 附近是高原还是尖峰（尖峰 = 该标的上阈值脆）。
- **R4 不产出候选。** 6 个标的两两相关 0.8~0.95（有效独立单元 1~2 个）、2024-26 已被
  历次决策消耗（无干净 OOS）、取 max 有上偏未量化。任何部署变更须走 ⓪ 机器
  （`backtest/selection_permutation.py`）对全部 θ×标的 点出 `p_selected`。

## 口径

零 carry（跨标的唯一可比，同 `underlying_probe`）、`cost_bps=3.0`、
信号固定 = 现役部署列 `factor_value`（lb20/zw40/sm5），**只变 θ**。
θ = −1 一行是**买入持有参照**（信号恒大于 −1 → 永远在场）。

## 用法

    python3 -m backtest.threshold_by_underlying_probe
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS, evaluate  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402
from backtest.underlying_probe import (  # noqa: E402
    SINGLES, equal_weight_basket, load_index_returns,
)

#: θ 网格：信号实测范围 [−0.885, +0.854]，取 [−0.7, +0.7] 步长 0.05；
#: 另加 −1.0 作为"永远在场"= 买入持有参照行。
THETAS = [-1.0] + [round(x, 2) for x in np.arange(-0.70, 0.701, 0.05)]
OUT = ROOT / "backtest" / "output" / "threshold_by_underlying.csv"


def _instruments() -> dict[str, pd.Series]:
    """6 个单标的 + **现役部署口径 blend(500+1000)** —— 后者是部署真正跑的东西。"""
    singles = {k: load_index_returns(code) for k, (code, _) in SINGLES.items()}
    out = {f"{k}｜{SINGLES[k][1]}": v for k, v in singles.items()}
    out["blend｜500+1000（现役部署口径）"] = equal_weight_basket(
        [singles["500"], singles["1000"]])
    return out


def run(cost_bps: float = 3.0) -> pd.DataFrame:
    _, smooth = load_two_columns()
    rows = []
    for uname, und in _instruments().items():
        for th in THETAS:
            pos = production_position(smooth, threshold=th).astype(float)
            row = {"underlying": uname, "theta": th,
                   "exposure": float((smooth > th).mean())}
            for win, (s, e) in WINDOWS.items():
                p, u = pos, und
                if s:
                    p, u = p[p.index >= pd.Timestamp(s)], u[u.index >= pd.Timestamp(s)]
                if e:
                    p, u = p[p.index <= pd.Timestamp(e)], u[u.index <= pd.Timestamp(e)]
                if len(p.index.intersection(u.index)) < 60:
                    continue
                m = evaluate(p, u, None, cost_bps, 0)["long"]
                row[f"sharpe_{win}"] = m["sharpe"]
                if win == "full":
                    row["ann_full"] = m["ann"]
                    row["turnover_full"] = m["turnover"]
            rows.append(row)
    out = pd.DataFrame(rows)
    out["worst_tv"] = out[["sharpe_2014-2020", "sharpe_2021-2023"]].min(axis=1)
    return out


def buyhold_drift() -> dict[str, float]:
    """各标的买入持有年化（full 窗）—— R2 的排序变量。"""
    s, e = WINDOWS["full"]
    out = {}
    for uname, r in _instruments().items():
        if s:
            r = r[r.index >= pd.Timestamp(s)]
        if e:
            r = r[r.index <= pd.Timestamp(e)]
        out[uname] = float((1 + r).prod() ** (252 / len(r)) - 1)
    return out


def report(rep: pd.DataFrame) -> None:
    drift = buyhold_drift()
    grid = rep[rep["theta"] > -1.0]          # 买入持有参照行不参与选优

    print(f"θ 网格 {len(THETAS) - 1} 点（另加买入持有参照）× 标的 "
          f"{rep['underlying'].nunique()} 个；键 = worst(train,val)，零 carry\n")

    print("── R1 各标的最优 θ vs 现役 θ=0 ──")
    rows = []
    for u, g in grid.groupby("underlying", sort=False):
        best = g.loc[g["worst_tv"].idxmax()]
        inc = g[g["theta"] == 0.0].iloc[0]
        bh = rep[(rep["underlying"] == u) & (rep["theta"] == -1.0)].iloc[0]
        rows.append({
            "标的": u, "买持年化%": round(drift[u] * 100, 2),
            "最优θ": best["theta"], "最优在场%": round(best["exposure"] * 100, 1),
            "最优 worst_tv": round(best["worst_tv"], 4),
            "现役 worst_tv": round(inc["worst_tv"], 4),
            "差": round(best["worst_tv"] - inc["worst_tv"], 4),
            "买持 worst_tv": round(bh["worst_tv"], 4),
        })
    r1 = pd.DataFrame(rows).sort_values("买持年化%", ascending=False)
    print(r1.to_string(index=False))

    print("\n── R2 机制检验：按买持年化降序，最优 θ 是否单调不增？──")
    seq = r1["最优θ"].tolist()
    mono = all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1))
    print(f"   买持年化降序对应的最优 θ 序列 = {seq}")
    print(f"   → 单调不增？**{'是（机制得到支持）' if mono else '否（预测未兑现）'}**")
    exp = r1["最优在场%"].tolist()
    mono_e = all(exp[i] >= exp[i + 1] for i in range(len(exp) - 1))
    print(f"   同序的最优在场比例 = {exp} → 单调不增？{'是' if mono_e else '否'}")

    print("\n── R3 曲线形状：worst_tv 随 θ（每标的一行，列 = θ）──")
    piv = grid.pivot_table(index="underlying", columns="theta",
                           values="worst_tv", sort=False)
    show = [c for c in piv.columns if round(c * 100) % 10 == 0]   # 每 0.1 一列
    print(piv[show].round(3).to_string())

    print("\n⚠️ R4：不产出候选。任何部署变更须走 ⓪ 机器对全部 "
          f"{len(grid)} 点出 p_selected（①a 判例：不许摘取）。")


def main() -> int:
    rep = run()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(OUT, index=False)
    report(rep)
    print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
