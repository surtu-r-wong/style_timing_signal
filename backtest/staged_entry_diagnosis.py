"""分批候选的机制诊断（只读归因，**零选优风险**）。

## 为什么先做这个而不是先扫参数

`--mode sweep-fast` 扫出 8 候选 × 4 窗 = 32 格后，`staged_sm2_5` 在
worst(train,val) 判据上以 1.3607 vs 现役 1.0009 领先，但 full 窗它反而劣
0.18，且 8 个候选里有 6 个都超越了现役的 worst_tv。这种"全样本劣、子窗判据优
+ 一大批候选同时超越"的形态，历史上（②⑤⑦）最后都是 STOP。

所以先回答机制问题，再决定值不值得预登记：

  Q1 现役在 2021-2023 为什么掉到 Sharpe 1.0？集中在哪一年？
  Q2 `staged_sm2_5` 在那段到底改善了什么？
  Q3 改善集中在少数几天（脆弱）还是分散（稳健）？
  Q4 改善来自「提前进场」还是「提前减仓」？

Q3/Q4 是决定性的：若改善由个别几天撑着、或只来自单一方向的仓位差，那它更像
样本内巧合而非可复用机制。

## 用法

    python3 -m backtest.staged_entry_diagnosis
    python3 -m backtest.staged_entry_diagnosis --fast 3      # 换快腿窗
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import ann_return, max_drawdown, sharpe  # noqa: E402
from backtest.positions import production_position, staged_position  # noqa: E402
from backtest.staged_entry_probe import load_two_columns, smooth_series  # noqa: E402
from backtest.yearly import concentration_summary  # noqa: E402

VAL = ("2021-01-01", "2023-12-31")
OUT_DIR = ROOT / "backtest" / "output"


def build_pair(fast_window: int, w1: float, w2: float, cost_bps: float,
               kou_jing: str = "blend") -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """(现役结果, 分批结果, 标的收益) —— 全部对齐到公共索引。"""
    raw, smooth = load_two_columns()
    fast = smooth_series(raw, fast_window)
    inc_pos = production_position(smooth).astype(float)
    stg_pos = staged_position(fast, smooth, w1, w2)

    und = load_underlying_returns(kou_jing)
    car = load_carry(kou_jing)
    idx = inc_pos.index.intersection(und.index)
    und, car = und.reindex(idx), car.reindex(idx)
    inc = run_strategy(inc_pos.reindex(idx), und, cost_bps, car)
    stg = run_strategy(stg_pos.reindex(idx), und, cost_bps, car)
    return inc, stg, und


def _slice(df, start, end):
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def yearly_compare(inc: pd.DataFrame, stg: pd.DataFrame) -> pd.DataFrame:
    """逐自然年：两者的 Sharpe / 年化 / MaxDD，定位弱点年份。"""
    rows = []
    for year, idx in inc.groupby(inc.index.year).groups.items():
        i, s = inc.loc[idx, "ret"], stg.loc[idx, "ret"]
        rows.append({
            "year": year, "n": len(i),
            "inc_sharpe": sharpe(i), "stg_sharpe": sharpe(s),
            "inc_ann": ann_return(i), "stg_ann": ann_return(s),
            "inc_maxdd": max_drawdown(i), "stg_maxdd": max_drawdown(s),
        })
    return pd.DataFrame(rows).set_index("year")


def diff_concentration(inc: pd.DataFrame, stg: pd.DataFrame) -> dict:
    """日收益差的集中度：top-N 天占累计差的比例。

    命题：若少数几天就撑起全部优势，这个优势是脆弱的。用 |diff| 排序取 top-N，
    再看它们的**带符号**和占总和的份额（份额 >1 意味着其余天数在反向抵消）。
    """
    d = (stg["ret"] - inc["ret"]).dropna()
    total = float(d.sum())
    ordered = d.reindex(d.abs().sort_values(ascending=False).index)
    out = {
        "n_days": int(len(d)),
        "total_diff_sum": total,
        "nav_inc": float((1 + inc["ret"]).prod()),
        "nav_stg": float((1 + stg["ret"]).prod()),
        "days_stg_better": int((d > 0).sum()),
        "days_stg_worse": int((d < 0).sum()),
        "days_identical": int((d == 0).sum()),
    }
    for n in (1, 5, 10, 20):
        head = float(ordered.iloc[:n].sum())
        out[f"top{n}_share"] = head / total if abs(total) > 1e-12 else float("nan")
    return out


def position_crosstab(inc: pd.DataFrame, stg: pd.DataFrame,
                      und: pd.Series) -> pd.DataFrame:
    """按 (现役有效仓位, 分批有效仓位) 分组 → 天数 / 标的均值 / 对差额的贡献。

    用 `pos_eff`（shift(1) 后真正吃收益的那个仓位），不是信号日仓位。
    这张表直接回答 Q4：优势来自提前进场（现役 0 / 分批 >0）还是提前减仓
    （现役 1 / 分批 <1）。
    """
    d = stg["ret"] - inc["ret"]
    g = pd.DataFrame({
        "inc_pos": inc["pos_eff"].round(4),
        "stg_pos": stg["pos_eff"].round(4),
        "und": und.reindex(inc.index),
        "diff": d,
    }).dropna()
    agg = g.groupby(["inc_pos", "stg_pos"]).agg(
        days=("diff", "size"),
        und_mean=("und", "mean"),
        diff_sum=("diff", "sum"),
    )
    agg["diff_share"] = agg["diff_sum"] / g["diff"].sum()
    kind = []
    for inc_p, stg_p in agg.index:
        if stg_p > inc_p:
            kind.append("分批暴露更高（提前/多进）")
        elif stg_p < inc_p:
            kind.append("分批暴露更低（提前/多减）")
        else:
            kind.append("相同")
    agg["kind"] = kind
    return agg.sort_values("diff_sum", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="分批候选机制诊断（只读）")
    ap.add_argument("--fast", type=int, default=2, help="快腿平滑窗，默认 2")
    ap.add_argument("--w1", type=float, default=0.4)
    ap.add_argument("--w2", type=float, default=0.6)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    inc, stg, und = build_pair(args.fast, args.w1, args.w2, args.cost_bps)
    tag = f"staged_sm{args.fast}_5 (w1={args.w1}/w2={args.w2})"
    print(f"对比：现役 lf_sm5  vs  {tag}   blend 口径, cost_bps={args.cost_bps}")
    print(f"样本 {len(inc)} 天  {inc.index[0]:%Y-%m-%d} .. {inc.index[-1]:%Y-%m-%d}")

    print("\n══ Q1/Q2 逐年对比（Sharpe / 年化% / MaxDD%）══")
    yc = yearly_compare(inc, stg)
    show = yc.copy()
    for c in ["inc_ann", "stg_ann", "inc_maxdd", "stg_maxdd"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["inc_sharpe", "stg_sharpe"]:
        show[c] = show[c].round(3)
    print(show.to_string())

    for label, (s, e) in {"val 2021-2023": VAL, "full": (inc.index[0], inc.index[-1])}.items():
        i, s_ = _slice(inc, s, e), _slice(stg, s, e)
        print(f"\n══ Q3 日收益差集中度 — {label} ══")
        for k, v in diff_concentration(i, s_).items():
            print(f"  {k:18s} {v:.6f}" if isinstance(v, float) else f"  {k:18s} {v}")

    print("\n══ Q4 仓位状态交叉表 — val 2021-2023 ══")
    ct = position_crosstab(_slice(inc, *VAL), _slice(stg, *VAL), und)
    disp = ct.copy()
    disp["und_mean"] = (disp["und_mean"] * 1e4).round(2)      # bps/日
    disp["diff_sum"] = (disp["diff_sum"] * 100).round(3)      # 累计 pp
    disp["diff_share"] = disp["diff_share"].round(4)
    disp = disp.rename(columns={"und_mean": "标的均值(bps/日)", "diff_sum": "差额累计(pp)"})
    print(disp.to_string())

    print("\n══ 集中度摘要（剔最强年 / 滚动3年）══")
    for name, df in (("现役 lf_sm5", inc), (tag, stg)):
        for scope, (s, e) in {"full": (inc.index[0], inc.index[-1]), "val": VAL}.items():
            c = concentration_summary(_slice(df, s, e)["ret"])
            print(f"  {name:34s} [{scope:4s}] full={c['sharpe_full']:.4f} "
                  f"ex_top1={c['sharpe_ex_top1']:.4f}(剔{c['ex_top1_year']}) "
                  f"ex_top2={c['sharpe_ex_top2']:.4f}  "
                  f"roll3y_min={c['roll3y_min']:.4f}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    yc.to_csv(OUT_DIR / "staged_entry_yearly.csv")
    ct.to_csv(OUT_DIR / "staged_entry_crosstab_val.csv")
    print(f"\n→ {OUT_DIR / 'staged_entry_yearly.csv'}")
    print(f"→ {OUT_DIR / 'staged_entry_crosstab_val.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
