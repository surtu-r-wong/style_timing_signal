"""现役 equal_weight 空头腿审计 —— 2026-09-09「部署空头」决策的可复核证据（此前只在会话里算过）。

同秤日频引擎：每日按符号持仓、T+1、3 bps、blend 标的、含期指贴水 carry（持空付 carry）。
产出 backtest/output/short_leg_audit.json（全部表）。CLI: python3 -m backtest.short_leg_audit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.metrics import ann_return, sharpe, turnover  # noqa: E402
from backtest.rotation_probe import _load_ew_signal  # noqa: E402

WINDOW = ("2015-04-16", "2026-09-03")   # 有期指数据的窗
OUT = ROOT / "backtest" / "output" / "short_leg_audit.json"


def mdd(r: pd.Series) -> float:
    c = (1 + r).cumprod(); return float((c / c.cummax() - 1).min())


def stats(pos: pd.Series, und: pd.Series, carry, a=WINDOW[0], b=WINDOW[1]) -> dict:
    p = pos[(pos.index >= a) & (pos.index <= b)].dropna()
    r = run_strategy(p, und.reindex(p.index), 3.0, carry)["ret"].dropna()
    return {"sharpe": round(sharpe(r), 3), "ann": round(ann_return(r), 4), "maxdd": round(mdd(r), 3), "turnover": round(turnover(p), 1),
            "short_share": round(float((p < 0).mean()), 3), "flat_share": round(float((p == 0).mean()), 3)}


def main() -> int:
    und = load_underlying_returns("blend"); carry = load_carry("blend"); ew = _load_ew_signal().dropna()
    sg = np.sign(ew); out = {"window": WINDOW, "engine": "daily sign position, T+1, 3bps, blend, carry on shorts"}
    # 1. 腿分解
    out["legs"] = {"short_with_carry": stats(sg.clip(upper=0), und, carry), "short_no_carry": stats(sg.clip(upper=0), und, None),
                   "long_flat": stats(sg.clip(lower=0), und, carry), "symmetric": stats(sg, und, carry)}
    # 2. 空头腿逐年
    ps = sg.clip(upper=0); r_s = run_strategy(ps, und.reindex(ps.index), 3.0, carry)["ret"].dropna(); r_s0 = run_strategy(ps, und.reindex(ps.index), 3.0, None)["ret"].dropna()
    y = pd.DataFrame({"s": r_s, "s0": r_s0, "pos": ps.reindex(r_s.index), "c": carry.reindex(r_s.index), "u": und.reindex(r_s.index)}).loc[WINDOW[0]:WINDOW[1]]
    out["short_leg_yearly"] = {int(k): {"ann_with_carry": round(g.s.mean() * 245, 4), "ann_no_carry": round(g.s0.mean() * 245, 4), "short_share": round(float((g.pos < 0).mean()), 3),
                                        "carry_mean": round(float(g.c.mean()), 4), "underlying_ann_on_short_days": round(float(g[g.pos < 0].u.mean() * 245), 4) if (g.pos < 0).any() else None}
                               for k, g in y.groupby(y.index.year)}
    # 3. 贴水阈值门控（描述性）
    out["carry_gated_short"] = {str(th): stats(ps.where(carry.reindex(ps.index).fillna(0) < th, 0.0), und, carry) for th in (0.03, 0.05, 0.07, 0.10, 1.0)}
    # 4. 映射变体
    def band(lo, hi_w):
        return pd.Series(np.sign(ew) * np.where(ew.abs() < lo, hi_w, 1.0), index=ew.index)
    maps = {"long_flat": sg.clip(lower=0), "symmetric": sg, "deadband_0.1": pd.Series(np.where(ew > .1, 1, np.where(ew < -.1, -1, 0)), index=ew.index, dtype=float),
            "deadband_0.2": pd.Series(np.where(ew > .2, 1, np.where(ew < -.2, -1, 0)), index=ew.index, dtype=float),
            "asym_short_-0.2": pd.Series(np.where(ew > 0, 1, np.where(ew < -.2, -1, 0)), index=ew.index, dtype=float),
            "half_short_-0.5": pd.Series(np.where(ew > 0, 1, np.where(ew < 0, -.5, 0)), index=ew.index, dtype=float),
            "continuous": ew.clip(-1, 1), "ramp_3d": sg.rolling(3, min_periods=1).mean(), "ramp_10d": sg.rolling(10, min_periods=1).mean(),
            "strength_band_0.2_half": band(.2, .5)}
    out["mappings"] = {k: {**stats(v, und, carry), "h1_sharpe": stats(v, und, carry, WINDOW[0], "2021-01-01")["sharpe"], "h2_sharpe": stats(v, und, carry, "2021-01-01", WINDOW[1])["sharpe"]} for k, v in maps.items()}
    # 5. 贴线候选的纯空头腿（同一台日频引擎）
    cands = {}
    try:
        mf = pd.read_csv(ROOT / "backtest/output/money_flow_series.csv", parse_dates=["trade_date"]).set_index("trade_date"); cands["F1_money_flow"] = (level_signal(mf["F1"].dropna(), 5, 250), ("2015-07-02", "2026-09-01"))
        bt = pd.read_csv(ROOT / "backtest/output/basis_term_series.csv", parse_dates=["date"]).set_index("date"); cands["T1_basis"] = (level_signal(bt["T1"].dropna(), 5, 60), ("2015-05-15", "2026-08-31"))
        cs = pd.read_csv(ROOT / "backtest/output/consensus_revision_series.csv"); cs = cs.set_index(pd.to_datetime(cs.iloc[:, 0])).iloc[:, 1:]; cands["R4_consensus"] = (level_signal(cs["R4"].dropna().loc[:"2026-05-29"], 20, 250), ("2019-11-19", "2026-05-29"))
        io = pd.read_csv(ROOT / "backtest/output/option_iv_IO.csv", parse_dates=["date"]).set_index("date"); cands["O4_skew"] = (level_signal(io["skew"].dropna(), 5, 250), ("2020-01-21", "2026-08-31")); cands["O2_ivdiff20_dir-1"] = (-level_signal(io["iv30"].diff(20).dropna(), 5, 60), ("2020-01-21", "2026-08-31"))
    except FileNotFoundError as e:
        out["candidates_note"] = f"缺缓存: {e}"
    out["candidate_short_legs"] = {}
    for name, (sig, (a, b)) in cands.items():
        p = np.sign(sig).clip(upper=0)
        out["candidate_short_legs"][name] = {"window": [a, b], "with_carry": stats(p, und, carry, a, b), "no_carry": stats(p, und, None, a, b), "incumbent_short_same_window": stats(sg.clip(upper=0), und, carry, a, b)}
    # 6. 承载模式（对称仓位）：IC/IM 各自 carry 在 ±1 仓位上的年化贡献
    c500, c1000 = load_carry("500"), load_carry("1000")
    idx = sg.loc[WINDOW[0]:WINDOW[1]].index
    def carry_income(cs: pd.Series) -> float:
        return round(float((sg.reindex(idx) * cs.reindex(idx).fillna(0.0) / 245).mean() * 245), 4)
    out["carrying_mode_symmetric"] = {"A_natural_50_50": carry_income(pd.concat([c500, c1000], axis=1).fillna(0.0).mean(axis=1)), "B_all_IM": carry_income(c1000.reindex(idx).fillna(c500.reindex(idx))), "C_all_IC": carry_income(c500),
                                      "note": "年化 carry 净贡献（持多收、持空付），IM 上市前 B 回退 IC；long-flat 口径见 2026-08-24 carry 文档"}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({k: out[k] for k in ("legs", "carrying_mode_symmetric")}, ensure_ascii=False, indent=1))
    print("mappings:", {k: (v["sharpe"], v["maxdd"]) for k, v in out["mappings"].items()})
    print("candidate short legs:", {k: v["with_carry"]["sharpe"] for k, v in out["candidate_short_legs"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
