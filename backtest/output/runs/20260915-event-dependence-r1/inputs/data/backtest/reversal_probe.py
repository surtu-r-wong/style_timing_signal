"""反转面探针（预登记 docs/plans/2026-09-03-reversal-preregistration.md，冻结后才可 --run）。

A 族：风格价差 5~10 日反转 = −1 × 07-10 短 z 窗原网格的动量因子（60 点），对称口径三窗 Sharpe，
    关①三窗全正 ②worst(train,val) ≥ 现役 ③与现役相关 <0.5 ④⓪（60 点 worst_tv argmax 空分布 p_selected<0.05）。
B 族：blend 自身反转（B1：L 日收益 z>+1 触发做空、持有 k）与趋势（B2：价格低于 MA 做空、最短持有 k），只做空仓位，
    含贴水与成本；关①空头段全窗与两半窗净 Sharpe>0 ②净年化>0 ③⓪（27 点空头段净 Sharpe argmax，p_selected<0.05）。
CLI: python3 -m backtest.reversal_probe --run [--n-perm 1000]
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import ann_return, sharpe, turnover  # noqa: E402
from backtest.positions import to_position  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402

OUT_DIR = ROOT / "backtest" / "output"
COST_BPS = 3.0
WINDOWS_A = {"train_14_20": ("2014-01-01", "2020-12-31"), "val_21_23": ("2021-01-01", "2023-12-31"), "holdout_24_26": ("2024-01-01", "2026-12-31")}
SELECT_A = ("train_14_20", "val_21_23")
HALVES_B = {"2014-2019": ("2014-01-01", "2019-12-31"), "2020-2026": ("2020-01-01", "2026-12-31")}
GRID_A = [dict(family=f, length=L, skip=0, z_window=zw, smoothing=sm)
          for f in ("classic", "slope", "voladj") for L in (5, 10) for zw in (10, 15, 20, 25, 30) for sm in (0, 5)]
GRID_B1 = [("B1", L, zw, k) for L in (5, 10, 20) for zw in (20, 60) for k in (5, 10, 20)]
GRID_B2 = [("B2", M, None, k) for M in (60, 120, 250) for k in (5, 10, 20)]
Z_TRIGGER = 1.0
CORR_MAX = 0.5


# ---------------- 纯函数 ----------------
def _win(s: pd.Series, a: str, b: str) -> pd.Series:
    return s[(s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))]


def window_sharpes(pos: pd.Series, und: pd.Series, carry: pd.Series | None, windows: dict) -> dict:
    out = {}
    for name, (a, b) in windows.items():
        p = _win(pos, a, b)
        if len(p) < 20:
            out[name] = np.nan; continue
        ret = run_strategy(p, und.reindex(p.index), COST_BPS, None if carry is None else carry.reindex(p.index))["ret"]
        out[name] = sharpe(ret.dropna())
    return out


def reversal_factor(momentum_fn, **params) -> pd.Series:
    return -1.0 * momentum_fn(**params)


def short_trigger_hold(trigger: pd.Series, k: int) -> pd.Series:
    """触发后做空并持有 k 日（窗内再触发则顺延）：pos = −1 若过去 k 日内有触发。"""
    t = trigger.astype(int)
    held = t.rolling(k, min_periods=1).max()
    return -held.astype(int)


def b1_position(ret: pd.Series, L: int, zw: int, k: int) -> pd.Series:
    price = (1.0 + ret).cumprod()
    r_l = price / price.shift(L) - 1.0
    z = (r_l - r_l.rolling(zw, min_periods=zw).mean()) / r_l.rolling(zw, min_periods=zw).std()
    return short_trigger_hold((z > Z_TRIGGER).fillna(False), k)


def b2_position(ret: pd.Series, M: int, k: int) -> pd.Series:
    """价格低于 MA(M) 期间做空；跌破后最短持有 k 日（k 只延长退出，不提前进入）。"""
    price = (1.0 + ret).cumprod()
    below = (price < price.rolling(M, min_periods=M).mean()).fillna(False)
    cross = below & ~below.shift(1).fillna(False)
    min_hold = short_trigger_hold(cross, k) < 0
    return -(below | min_hold).astype(int)


def short_only_metrics(pos: pd.Series, und: pd.Series, carry: pd.Series) -> dict:
    ret = run_strategy(pos, und.reindex(pos.index), COST_BPS, carry.reindex(pos.index))["ret"].dropna()
    out = {"sharpe_full": sharpe(ret), "ann_full": ann_return(ret), "short_days": int((pos < 0).sum()), "short_share": float((pos < 0).mean()), "turnover": turnover(pos)}
    for h, (a, b) in HALVES_B.items():
        out[f"sharpe_{h}"] = sharpe(_win(ret, a, b))
    return out


# ---------------- 编排 ----------------
def run_A(n_perm: int, seed: int = 0, db=None) -> tuple[pd.DataFrame, dict]:
    from backtest.data import load_carry, load_underlying_returns
    from backtest.momentum_scan import momentum_factor_fn
    from backtest.scan import equal_weight_factor_fn
    und, car = load_underlying_returns("blend", db=db), load_carry("blend", db=db)
    mom = momentum_factor_fn()
    inc = equal_weight_factor_fn()(lookback=20, z_window=40, smoothing=5)
    factors = {i: reversal_factor(mom, **g) for i, g in enumerate(GRID_A)}
    idx = und.index
    for f in factors.values():
        idx = idx.intersection(f.dropna().index)
    idx = idx.intersection(inc.dropna().index).sort_values()
    u, c = und.reindex(idx), car.reindex(idx).fillna(0.0)
    pos = {i: to_position(f.reindex(idx), mode="discrete") for i, f in factors.items()}
    inc_pos = to_position(inc.reindex(idx), mode="discrete")
    rows = []
    for i, g in enumerate(GRID_A):
        ws = window_sharpes(pos[i], u, c, WINDOWS_A)
        rows.append({"variant": i, **g, **{f"sharpe_{k}": v for k, v in ws.items()}, "worst_tv": min(ws[w] for w in SELECT_A),
                     "corr_vs_incumbent": float(factors[i].reindex(idx).corr(inc.reindex(idx))), "turnover": turnover(pos[i])})
    panel = pd.DataFrame(rows)
    inc_ws = window_sharpes(inc_pos, u, c, WINDOWS_A); inc_worst = min(inc_ws[w] for w in SELECT_A)
    # ⓪：重排仓位侧、按日历切窗重算 worst_tv
    n = len(idx); arrays = {i: pos[i].to_numpy(dtype=float) for i in pos}
    def stat_fn(variant, index):
        p = pd.Series(arrays[variant][np.asarray(index)], index=idx)
        ws = window_sharpes(p, u, c, {w: WINDOWS_A[w] for w in SELECT_A})
        v = min(ws.values()); return v if np.isfinite(v) else -np.inf
    res = selection_permutation_test(list(pos), n_obs=n, stat_fn=stat_fn, n_perm=n_perm, seed=seed, scheme="rotation",
                                     min_shift=40, max_shift=n - 40, statistic_name="worst_tv_sharpe_reversal")
    best = int(res.best_index); rep = panel.loc[panel["variant"] == best].iloc[0]
    g1 = bool(all(rep[f"sharpe_{w}"] > 0 for w in WINDOWS_A)); g2 = bool(rep["worst_tv"] >= inc_worst)
    g3 = bool(abs(rep["corr_vs_incumbent"]) < CORR_MAX); g4 = bool(res.p_selected < 0.05)
    verdict = {"representative": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in rep.to_dict().items()},
               "incumbent": {**inc_ws, "worst_tv": inc_worst}, "gate1_three_windows_positive": g1, "gate2_worst_tv_ge_incumbent": g2,
               "gate3_corr_lt_0.5": g3, "gate4_p_selected_lt_0.05": g4, "PASS": bool(g1 and g2 and g3 and g4),
               "p_selected": float(res.p_selected), "p_naive": float(res.p_naive), "p_min_p": float(res.p_min_p),
               "selection_inflation": float(res.selection_inflation), "null_worst_tv_q50_q95": [float(np.median(res.null_selected)), float(np.quantile(res.null_selected, 0.95))],
               "n_obs": n, "n_perm": int(res.n_perm), "n_three_windows_positive": int((panel[[f"sharpe_{w}" for w in WINDOWS_A]] > 0).all(axis=1).sum())}
    return panel, verdict


def run_B(n_perm: int, seed: int = 0, db=None) -> tuple[pd.DataFrame, dict]:
    from backtest.data import load_carry, load_underlying_returns
    und, car = load_underlying_returns("blend", db=db), load_carry("blend", db=db)
    und = und[und.index >= "2013-01-01"]; car = car.reindex(und.index).fillna(0.0)
    variants = GRID_B1 + GRID_B2
    pos = {}
    for v in variants:
        pos[v] = b1_position(und, v[1], v[2], v[3]) if v[0] == "B1" else b2_position(und, v[1], v[3])
    idx = und.index[und.index >= "2014-01-01"]
    rows = []
    for v in variants:
        m = short_only_metrics(pos[v].reindex(idx), und, car)
        rows.append({"family": v[0], "param": v[1], "z_window": v[2], "k": v[3], **m})
    panel = pd.DataFrame(rows)
    u, c = und.reindex(idx), car.reindex(idx); n = len(idx); arrays = {v: pos[v].reindex(idx).to_numpy(dtype=float) for v in variants}
    def stat_fn(variant, index):
        p = pd.Series(arrays[variant][np.asarray(index)], index=idx)
        r = run_strategy(p, u, COST_BPS, c)["ret"].dropna(); s = sharpe(r); return s if np.isfinite(s) else -np.inf
    res = selection_permutation_test(variants, n_obs=n, stat_fn=stat_fn, n_perm=n_perm, seed=seed, scheme="rotation",
                                     min_shift=40, max_shift=n - 40, statistic_name="short_only_net_sharpe")
    rep = panel.iloc[int(res.best_index)]
    g1 = bool(rep["sharpe_full"] > 0 and all(rep[f"sharpe_{h}"] > 0 for h in HALVES_B)); g2 = bool(rep["ann_full"] > 0); g3 = bool(res.p_selected < 0.05)
    carry_mean = float(car.reindex(idx).mean())
    verdict = {"representative": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in rep.to_dict().items()},
               "gate1_sharpe_positive_full_and_halves": g1, "gate2_net_ann_positive": g2, "gate3_p_selected_lt_0.05": g3, "PASS": bool(g1 and g2 and g3),
               "p_selected": float(res.p_selected), "p_naive": float(res.p_naive), "p_min_p": float(res.p_min_p),
               "null_short_sharpe_q50_q95": [float(np.median(res.null_selected)), float(np.quantile(res.null_selected, 0.95))],
               "carry_wall_mean_annualized": carry_mean, "n_obs": n, "n_perm": int(res.n_perm),
               "n_variants_short_sharpe_positive": int((panel["sharpe_full"] > 0).sum())}
    return panel, verdict


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--only", choices=["A", "B"], default=None); a = ap.parse_args(argv)
    if not a.run:
        print("frozen grids: A", len(GRID_A), "points; B", len(GRID_B1) + len(GRID_B2), "points"); return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True); verdict = {}
    pd.set_option("display.width", 250)
    if a.only in (None, "A"):
        pa, va = run_A(a.n_perm); pa.to_csv(OUT_DIR / "reversal_probe_A.csv", index=False); verdict["A"] = va
        print(pa.sort_values("worst_tv", ascending=False).head(10).round(3).to_string(index=False)); print(json.dumps(va, ensure_ascii=False, default=str))
    if a.only in (None, "B"):
        pb, vb = run_B(a.n_perm); pb.to_csv(OUT_DIR / "reversal_probe_B.csv", index=False); verdict["B"] = vb
        print(pb.sort_values("sharpe_full", ascending=False).round(3).to_string(index=False)); print(json.dumps(vb, ensure_ascii=False, default=str))
    path = OUT_DIR / "reversal_probe_verdict.json"
    old = json.loads(path.read_text()) if path.exists() and a.only else {}
    old.update(verdict); path.write_text(json.dumps(old, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
