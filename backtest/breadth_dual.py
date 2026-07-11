"""广度背离 × 双引擎装配编排（Phase 4 T3/T4）。

空头腿 = 外部广度背离信号（`dual_legs_external_short`）；多头腿 = equal_weight 基因子（复用 v1 winner）。
扫描目标是【对冲价值】：dual_sharpe（加空头腿后）+ maxdd_improve + 短腿触发频率——**不是**短腿自身
Sharpe（短腿 Sharpe 天然为负，v1 已证）。定参报告 `build_breadth_report` 出全变体 + bootstrap + 避险价值。
"""
import itertools
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.breadth import breadth_divergence  # noqa: E402
from backtest.dual import dual_legs_external_short  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import max_drawdown, sharpe  # noqa: E402


def _slice(s, start, end):
    if s is None:
        return None
    if start:
        s = s[s.index >= pd.Timestamp(start)]
    if end:
        s = s[s.index <= pd.Timestamp(end)]
    return s


def _short_signal(und_price, breadth_series, params):
    return breadth_divergence(
        und_price, breadth_series.reindex(und_price.index),
        new_high_window=params["P"], div_lookback=params["Q"],
        form=params["form"], threshold=params.get("threshold", 0.2),
        hold=params.get("hold", 1),
    )


def scan_breadth(long_factor, breadth_df, und_price, und_ret, carry, combos, windows,
                 long_theta=0.10, carry_theta=0.06, cost_bps=3.0) -> pd.DataFrame:
    """逐 combo × 窗：装配 dual(long=基因子, short=广度背离) → dual_sharpe / maxdd_improve / short_frac。"""
    rows = []
    for params in combos:
        short_sig = _short_signal(und_price, breadth_df[params["measure"]], params)
        row = {k: params.get(k) for k in ("measure", "form", "P", "Q", "hold", "threshold")}
        for win, (s, e) in windows.items():
            lf, ss = _slice(long_factor, s, e), _slice(short_sig, s, e)
            u, c = _slice(und_ret, s, e), _slice(carry, s, e)
            idx = lf.index.intersection(ss.index).intersection(u.index)
            lf, ss, u = lf.reindex(idx), ss.reindex(idx), u.reindex(idx)
            c = c.reindex(idx) if c is not None else pd.Series(0.0, index=idx)
            long_leg, short_leg, net = dual_legs_external_short(lf, ss, c, long_theta, carry_theta)
            net_r = run_strategy(net, u, cost_bps, c)["ret"]
            long_r = run_strategy(long_leg, u, cost_bps, c)["ret"]
            row[f"dual_sharpe_{win}"] = sharpe(net_r)
            row[f"maxdd_improve_{win}"] = abs(max_drawdown(long_r)) - abs(max_drawdown(net_r))
            row[f"short_frac_{win}"] = float((short_leg < 0).mean())
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------- 数据装载 + 报告编排 ----------------
def load_breadth_cache(path=None):
    from backtest.breadth import CACHE_PATH
    return pd.read_csv(path or CACHE_PATH, index_col=0, parse_dates=True)


def load_underlying_price(kj, start=None, db=None):
    """新高判定用的标的价：500/1000 用现货收盘；blend 用等权日收益合成价。"""
    from backtest.data import load_spot_close, load_underlying_returns
    if kj in ("500", "1000"):
        return load_spot_close(kj, start, db)
    return (1.0 + load_underlying_returns("blend", start, db)).cumprod()


def breadth_grid():
    """§1.4 网格：度量 × 形式 × P × Q × hold（事件触发须持有穿越下跌，hold 是关键维度）。

    nonconfirm 忽略 Q（只用 P）故不铺 Q 三份；hold∈{5,10,20}（hold=1 已证 inert）。
    4 度量 × 2 P × 3 hold × (1 nonconfirm + 3 deteriorating + 3 low_pct) = 168 组。
    """
    combos = []
    for measure in ("pct_above_ma20", "pct_above_ma60", "hi_lo_diff20", "hi_lo_diff60"):
        for P in (20, 60):
            for hold in (5, 10, 20):
                combos.append({"measure": measure, "form": "nonconfirm", "P": P, "Q": P,
                               "hold": hold, "threshold": 0.2})
                for Q in (10, 20, 40):
                    combos.append({"measure": measure, "form": "deteriorating", "P": P, "Q": Q,
                                   "hold": hold, "threshold": 0.2})
                    combos.append({"measure": measure, "form": "low_pct", "P": P, "Q": Q,
                                   "hold": hold, "threshold": 0.2})
    return combos


SCAN_WINDOWS = {"train_14_20": ("2014-01-01", "2020-12-31"),
                "val_21_23": ("2021-01-01", "2023-12-31"),
                "holdout_24_26": ("2024-01-01", "2026-12-31")}


def build_breadth_report(measure="pct_above_ma20", form="deteriorating", P=20, Q=20, threshold=0.2,
                         hold=10, long_theta=0.10, carry_theta=0.06, cost_bps=3.0, bootstrap_n=500,
                         seed=0, db=None) -> pd.DataFrame:
    """定参：三口径 × 分段窗 × 变体{long_engine, short_bd_engine, dual_bd, buy_hold} + 避险价值 + bootstrap。"""
    from backtest.baseline import KOU_JING, WINDOWS, _MIN_OBS, _row
    from backtest.data import load_carry, load_underlying_returns
    from backtest.dual import hedge_value, load_factor
    from backtest.significance import bootstrap_pvalue

    factor = load_factor("equal_weight")
    breadth = load_breadth_cache()[measure]
    params = {"measure": measure, "form": form, "P": P, "Q": Q, "threshold": threshold, "hold": hold}
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}
    price_all = {kj: load_underlying_price(kj, db=db) for kj in KOU_JING}

    rows = []
    for kj in KOU_JING:
        short_full = _short_signal(price_all[kj], breadth, params)
        for win, (s, e) in WINDOWS.items():
            lf, ss = _slice(factor, s, e), _slice(short_full, s, e)
            u, c = _slice(und_all[kj], s, e), _slice(car_all[kj], s, e)
            idx = lf.index.intersection(ss.index).intersection(u.index)
            if len(idx) < _MIN_OBS:
                continue
            lf, ss, u, c = lf.reindex(idx), ss.reindex(idx), u.reindex(idx), c.reindex(idx)
            long_leg, short_leg, net = dual_legs_external_short(lf, ss, c, long_theta, carry_theta)
            variants = {"long_engine": long_leg, "short_bd_engine": short_leg, "dual_bd": net}
            rets = {}
            for vname, p in variants.items():
                rets[vname] = run_strategy(p, u, cost_bps, c)["ret"]
                r = {"variant": vname, "kou_jing": kj, "window": win, **_row(rets[vname], p)}
                if bootstrap_n and vname in ("dual_bd", "short_bd_engine"):
                    r["pvalue"] = bootstrap_pvalue(p, u, "sharpe", bootstrap_n, seed, c, cost_bps)
                if vname == "dual_bd":
                    r.update(hedge_value(rets["dual_bd"], rets["long_engine"], u))
                rows.append(r)
            rows.append({"variant": "buy_hold", "kou_jing": kj, "window": win,
                         **_row(u, pd.Series(0.0, index=idx))})
    return pd.DataFrame(rows)


def main() -> int:
    import argparse
    from backtest.data import load_carry, load_underlying_returns
    from backtest.dual import load_factor

    ap = argparse.ArgumentParser(description="Phase4 广度背离 × 双引擎（扫描/定参报告）")
    ap.add_argument("--mode", choices=["scan", "report"], default="report")
    ap.add_argument("--kj", default="blend", choices=["500", "1000", "blend"])
    ap.add_argument("--measure", default="pct_above_ma20")
    ap.add_argument("--form", default="deteriorating",
                    choices=["deteriorating", "nonconfirm", "low_pct"])
    ap.add_argument("--P", type=int, default=20)
    ap.add_argument("--Q", type=int, default=20)
    ap.add_argument("--threshold", type=float, default=0.2)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--bootstrap", type=int, default=500)
    args = ap.parse_args()

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "scan":
        factor = load_factor("equal_weight")
        breadth = load_breadth_cache()
        rep = scan_breadth(factor, breadth, load_underlying_price(args.kj),
                           load_underlying_returns(args.kj), load_carry(args.kj),
                           breadth_grid(), SCAN_WINDOWS)
        out_path = out_dir / "scan_breadth.csv"
        rep.to_csv(out_path, index=False)
        show = rep.copy()
        show["worst_tv"] = show[
            ["dual_sharpe_train_14_20", "dual_sharpe_val_21_23"]].min(axis=1)
        show = show.round(3).sort_values("worst_tv", ascending=False)  # 排序不看第二验证窗(24-26)
        print(show.head(20).to_string(index=False))
    else:
        rep = build_breadth_report(args.measure, args.form, args.P, args.Q, args.threshold,
                                   hold=args.hold, bootstrap_n=args.bootstrap)
        out_path = out_dir / "breadth_divergence_metrics.csv"
        rep.to_csv(out_path, index=False)
        show = rep.copy()
        for col in ("ann", "maxdd", "hit", "down_month_hit", "maxdd_improve"):
            if col in show:
                show[col] = (show[col] * 100).round(1)
        for col in ("sharpe", "calmar", "turnover"):
            show[col] = show[col].round(2)
        if "pvalue" in show:
            show["pvalue"] = show["pvalue"].round(3)
        print(f"[广度背离 measure={args.measure} form={args.form} P={args.P} Q={args.Q}]")
        print(show.to_string(index=False))
    print(f"\n→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
