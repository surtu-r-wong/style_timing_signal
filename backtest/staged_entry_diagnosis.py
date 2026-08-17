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
from backtest.metrics import ann_return, calmar, max_drawdown, sharpe  # noqa: E402
from backtest.positions import production_position, staged_position  # noqa: E402
from backtest.staged_entry_probe import load_two_columns, smooth_series  # noqa: E402
from backtest.yearly import concentration_summary  # noqa: E402

VAL = ("2021-01-01", "2023-12-31")
OUT_DIR = ROOT / "backtest" / "output"


def build_pair(fast_window: int, w1: float, w2: float, cost_bps: float,
               kou_jing: str = "blend",
               open_threshold: float | None = None,
               close_threshold: float | None = None,
               ) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series,
                          pd.Series, pd.Series]:
    """(现役结果, 分批结果, 标的收益, carry, 现役信号仓位, 候选信号仓位)。

    最后两项是**信号仓位（未 shift）**，专供需要重跑 `run_strategy` 的调用方
    （`exposure_scaled_compare` / `scaling_ladder`）。**别用结果表里的 `pos_eff`**
    —— 那已经 shift(1) 过，再喂给 `run_strategy` 会二次 shift，等于偷偷换成
    shift(2)（"T+1 收盘成交"）口径，指标全错。2026-08-17 实际踩过这个坑。

    `open_threshold` / `close_threshold` 透传给 `staged_position`，用来诊断方向 B
    的候选（如 B2 = open=θ, close=0）。
    """
    raw, smooth = load_two_columns()
    fast = smooth_series(raw, fast_window)
    inc_pos = production_position(smooth).astype(float)
    stg_pos = staged_position(fast, smooth, w1, w2,
                             open_threshold=open_threshold,
                             close_threshold=close_threshold)

    und = load_underlying_returns(kou_jing)
    car = load_carry(kou_jing)
    idx = inc_pos.index.intersection(und.index)
    und, car = und.reindex(idx), car.reindex(idx)
    inc_pos, stg_pos = inc_pos.reindex(idx), stg_pos.reindex(idx)
    inc = run_strategy(inc_pos, und, cost_bps, car)
    stg = run_strategy(stg_pos, und, cost_bps, car)
    return inc, stg, und, car, inc_pos, stg_pos


def _slice(df, start, end):
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def exposure_scaled_compare(inc_signal_pos: pd.Series, cand_signal_pos: pd.Series,
                            und: pd.Series, car: pd.Series, cost_bps: float,
                            windows: dict[str, tuple]) -> pd.DataFrame:
    """等暴露对照：把现役等比缩到与候选相同的平均暴露，再比 MaxDD。

    **这是回撤维度唯一有意义的问法**。候选的回撤更浅可能只是因为平均暴露更低，
    而单纯降暴露用一个缩放系数就能复制——且缩放 **Sharpe 严格不变**（`run_strategy`
    里 gross/cost/carry 全部与仓位成正比，`ret` 严格成比例）。所以：

      * 候选 MaxDD ≈ 等暴露现役 MaxDD → **纯暴露效应**，回撤优势无信息量
      * 候选明显更浅 → 择时性降暴露有真实贡献
      * 候选反而更深 → 它的择时**有害**（不如无脑等比缩仓）
    """
    rows = []
    for wname, (a, b) in windows.items():
        u = und if a is None else _slice(und.to_frame("u"), a, b)["u"]
        c = car if a is None else _slice(car.to_frame("c"), a, b)["c"]
        i = inc_signal_pos if a is None else \
            _slice(inc_signal_pos.to_frame("p"), a, b)["p"]
        p = cand_signal_pos if a is None else \
            _slice(cand_signal_pos.to_frame("p"), a, b)["p"]
        k = float(p.mean() / i.mean())
        cand_mdd = max_drawdown(run_strategy(p, u, cost_bps, c)["ret"])
        scaled_mdd = max_drawdown(run_strategy(i * k, u, cost_bps, c)["ret"])
        gap = (cand_mdd - scaled_mdd) * 100
        rows.append({
            "window": wname, "inc_mean_pos": float(i.mean()),
            "cand_mean_pos": float(p.mean()), "k": k,
            "cand_maxdd": cand_mdd, "scaled_inc_maxdd": scaled_mdd,
            "gap_pp": gap,
            "verdict": "纯暴露效应" if gap < 0.5 else
                       ("择时有贡献" if gap > 1.5 else "边缘"),
        })
    return pd.DataFrame(rows).set_index("window")


def scaling_ladder(inc_signal_pos: pd.Series, und: pd.Series, car: pd.Series,
                   cost_bps: float, ks=(1.0, 0.93, 0.83, 0.7, 0.5)) -> pd.DataFrame:
    """现役等比缩仓阶梯 —— 想要更浅回撤时的零成本基线。

    Sharpe 那一列应当**逐行完全相同**：缩放只是沿同一条风险收益线滑动，按比例卖
    收益买回撤，不是 alpha。任何新结构若在"同年化收益下回撤更深或 Sharpe 更低"，
    就是被这条基线支配了。
    """
    rows = []
    for k in ks:
        r = run_strategy(inc_signal_pos * k, und, cost_bps, car)["ret"]
        rows.append({"k": k, "ann": ann_return(r), "sharpe": sharpe(r),
                     "maxdd": max_drawdown(r), "calmar": calmar(r)})
    return pd.DataFrame(rows).set_index("k")


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
    ap.add_argument("--open-threshold", type=float, default=None,
                    help="笔1 开仓阈值（方向 B）；不给则用 0")
    ap.add_argument("--close-threshold", type=float, default=None,
                    help="笔2 平仓阈值（方向 B 的 B2 口径给 0）")
    args = ap.parse_args()

    inc, stg, und, car, inc_pos, stg_pos = build_pair(
        args.fast, args.w1, args.w2, args.cost_bps,
        open_threshold=args.open_threshold, close_threshold=args.close_threshold)
    th = "" if args.open_threshold is None else \
        f" open={args.open_threshold:g}/close={args.close_threshold or 0:g}"
    tag = f"staged_sm{args.fast}_5 (w1={args.w1}/w2={args.w2}{th})"
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

    # ── 回撤维度：等暴露对照 + 缩仓阶梯 ──────────────────────────────
    wins = {"full": (None, None), "val": VAL}
    print("\n══ 等暴露对照（回撤优势是择时还是单纯降暴露？）══")
    esc = exposure_scaled_compare(inc_pos, stg_pos, und, car, args.cost_bps, wins)
    disp = esc.copy()
    for c in ("cand_maxdd", "scaled_inc_maxdd"):
        disp[c] = (disp[c] * 100).round(2)
    disp[["inc_mean_pos", "cand_mean_pos", "k", "gap_pp"]] = \
        disp[["inc_mean_pos", "cand_mean_pos", "k", "gap_pp"]].round(4)
    print(disp.to_string())

    print("\n══ 现役缩仓阶梯（零成本基线；Sharpe 那列应逐行相同）══")
    lad = scaling_ladder(inc_pos, und, car, args.cost_bps)
    show_l = lad.copy()
    for c in ("ann", "maxdd"):
        show_l[c] = (show_l[c] * 100).round(2)
    for c in ("sharpe", "calmar"):
        show_l[c] = show_l[c].round(4)
    print(show_l.to_string())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 文件名带参数标识——否则跑不同候选会互相覆盖，文档引用的产物会被冲掉
    # （2026-08-17 实际踩过：方向 B 的诊断冲掉了 base_sm2 的那份）。
    sfx = f"sm{args.fast}" if args.open_threshold is None else \
        f"sm{args.fast}_t{args.open_threshold:g}".replace("0.", "")
    for name, df in (("yearly", yc), ("crosstab_val", ct), ("exposure_scaled", esc),
                     ("scaling_ladder", lad)):
        path = OUT_DIR / f"staged_entry_{name}_{sfx}.csv"
        df.to_csv(path)
        print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
