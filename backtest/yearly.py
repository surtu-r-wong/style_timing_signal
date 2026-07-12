"""年度集中度分解——回答"全窗胜出是不是个别大年撑的"。

与 baseline 互补：baseline 回答"三窗/三口径/分段谁强"，本模块把生产口径
逐自然年拆开（long-flat / 对称 / buy&hold），并给集中度摘要（剔最强年 /
剔最强两年 / 滚动 3 年 Sharpe）。口径与生产同秤：全日历 / 3bp / carry
固定 50/50 / T+1（engine.run_strategy）；long-flat = production_position
（与 production.py 逐位一致），读 committed 信号 CSV。

CLI: python3 -m backtest.yearly [--kou-jing blend] [--cost-bps 3]
产出: backtest/output/yearly_decomposition.csv + yearly_concentration.csv + console。
设计: docs/plans/2026-07-12-yearly-concentration-design.md
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import SIGNALS  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import ANN, run_strategy  # noqa: E402
from backtest.metrics import ann_return, sharpe  # noqa: E402
from backtest.positions import production_position, to_position  # noqa: E402

ROLL_WINDOW = 3 * ANN  # 滚动 3 年 = 735 交易日，只取满窗


def yearly_table(ret_lf: pd.Series, ret_sym: pd.Series, bh: pd.Series,
                 pos_lf: pd.Series) -> pd.DataFrame:
    """逐自然年指标表。四序列须同索引已对齐；pos_lf 为目标仓位（T 收盘信号）。

    pct_long = shift(1) 生效后持多日占比（与 engine 的 T+1 口径一致）；
    log_contrib_pct = 该年 Σlog1p(ret_lf) 占全样本的百分比，全样本 ≤0 时 NaN。
    """
    pos_eff = pos_lf.astype(float).shift(1).fillna(0.0)
    logc = np.log1p(ret_lf.astype(float))
    total_log = logc.sum()

    rows = []
    for y, sl in ret_lf.groupby(ret_lf.index.year):
        in_year = ret_lf.index.year == y
        rows.append({
            "year": int(y),
            "days": int(len(sl)),
            "lf_ann": ann_return(sl),
            "lf_sharpe": sharpe(sl),
            "sym_ann": ann_return(ret_sym[in_year]),
            "sym_sharpe": sharpe(ret_sym[in_year]),
            "bh_ann": ann_return(bh[in_year]),
            "excess": ann_return(sl) - ann_return(bh[in_year]),
            "pct_long": float((pos_eff[in_year] > 0).mean()),
            "log_contrib_pct": (float(logc[in_year].sum() / total_log * 100)
                                if total_log > 0 else float("nan")),
        })
    return pd.DataFrame(rows).set_index("year")


def concentration_summary(ret: pd.Series) -> dict:
    """集中度摘要（作用于生产口径 long-flat 收益序列）。

    剔除项按"年度对数收益贡献"排序（不是按 Sharpe——命题是收益靠不靠个别大年）；
    滚动 3 年只取 735 日满窗，样本不足 → 滚动字段 NaN / min_date None。
    """
    ret = ret.astype(float)
    logc = np.log1p(ret).groupby(ret.index.year).sum()
    order = logc.sort_values(ascending=False).index.tolist()
    top1, top2 = order[:1], sorted(order[:2])

    years = pd.Series(ret.index.year, index=ret.index)
    ex1 = ret[~years.isin(top1)]
    ex2 = ret[~years.isin(top2)]

    roll = (ret.rolling(ROLL_WINDOW).mean()
            / ret.rolling(ROLL_WINDOW).std(ddof=1) * np.sqrt(ANN))
    roll = roll.replace([np.inf, -np.inf], np.nan).dropna()

    return {
        "sharpe_full": sharpe(ret),
        "sharpe_ex_top1": sharpe(ex1),
        "sharpe_ex_top2": sharpe(ex2),
        "ex_top1_year": int(top1[0]) if top1 else None,
        "ex_top2_years": ",".join(str(int(y)) for y in top2),
        "roll3y_min": float(roll.min()) if len(roll) else float("nan"),
        "roll3y_min_date": roll.idxmin() if len(roll) else None,
        "roll3y_median": float(roll.median()) if len(roll) else float("nan"),
        "roll3y_neg_share": float((roll < 0).mean()) if len(roll) else float("nan"),
    }


def _load_factor(name: str) -> pd.Series:
    path, col = SIGNALS[name]
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    return df[col]


def build_yearly_report(cost_bps: float = 3.0, db=None, kou_jing: str = "blend"):
    """三条线 × (long-flat / 对称 / buy&hold) → (逐年表, 集中度摘要表)。"""
    und_all = load_underlying_returns(kou_jing, db=db)
    car_all = load_carry(kou_jing, db=db)

    yearly_rows, conc_rows = [], []
    for name in SIGNALS:
        factor = _load_factor(name)
        idx = factor.index.intersection(und_all.index)
        factor = factor.reindex(idx)
        und, car = und_all.reindex(idx).astype(float), car_all.reindex(idx)

        pos_lf = production_position(factor)
        pos_sym = to_position(factor)
        ret_lf = run_strategy(pos_lf, und, cost_bps, car)["ret"]
        ret_sym = run_strategy(pos_sym, und, cost_bps, car)["ret"]

        tab = yearly_table(ret_lf, ret_sym, und, pos_lf).reset_index()
        tab.insert(0, "kou_jing", kou_jing)
        tab.insert(0, "signal", name)
        yearly_rows.append(tab)
        conc_rows.append({"signal": name, "kou_jing": kou_jing,
                          **concentration_summary(ret_lf)})
    return pd.concat(yearly_rows, ignore_index=True), pd.DataFrame(conc_rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="年度集中度分解（生产口径逐年 + 剔强年/滚动诊断）")
    ap.add_argument("--source", default="pg", choices=["pg"])
    ap.add_argument("--kou-jing", default="blend", choices=["500", "1000", "blend"])
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    yearly, conc = build_yearly_report(cost_bps=args.cost_bps, kou_jing=args.kou_jing)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    yearly.to_csv(out_dir / "yearly_decomposition.csv", index=False)
    conc.to_csv(out_dir / "yearly_concentration.csv", index=False)

    show = yearly.copy()
    for c in ["lf_ann", "sym_ann", "bh_ann", "excess", "pct_long"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["lf_sharpe", "sym_sharpe"]:
        show[c] = show[c].round(2)
    show["log_contrib_pct"] = show["log_contrib_pct"].round(1)
    print(show.to_string(index=False))

    cshow = conc.copy()
    for c in ["sharpe_full", "sharpe_ex_top1", "sharpe_ex_top2",
              "roll3y_min", "roll3y_median"]:
        cshow[c] = cshow[c].round(2)
    cshow["roll3y_neg_share"] = (cshow["roll3y_neg_share"] * 100).round(1)
    cshow["roll3y_min_date"] = cshow["roll3y_min_date"].astype(str).str[:10]
    print("\n集中度摘要（long-flat，剔除按年度对数贡献）：")
    print(cshow.to_string(index=False))
    print(f"\n→ {out_dir / 'yearly_decomposition.csv'}\n→ {out_dir / 'yearly_concentration.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
