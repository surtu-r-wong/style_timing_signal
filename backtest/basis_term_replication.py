"""期指基差期限结构·横截面复制探针（预登记 docs/plans/2026-09-09-basis-term-replication-prereg.md，用户 09-09 冻结）。

原线代表 T1 lb5zw60 / k=40 / 方向 +1 全部冻结；换品种做机制复制：
  P1  T1_IF → 000300.SH（2014-01-02 起）      单侧循环移位置换 p，闸
  P2  T1_IH → 000016.SH（2021-01-04 起）      回填前只作同号条件；回填到 2015-04-16 后升格为显著性检验
无网格、无选优 → 关 0 不适用。描述性副产物（只报不闸）：T2、→blend 的 IC 与控现役偏 IC、两半窗、IF 评窗前段、逐年符号。
CLI: python3 -m backtest.basis_term_replication --run [--n-perm 1000]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.basis_term import build_replication_series  # noqa: E402
from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.rotation_probe import HALVES, _load_ew_signal, _win, nonoverlap_ic, partial_ic_with_pvalue  # noqa: E402

LB, ZW, K, DIRECTION = 5, 60, 40, +1          # 冻结自原线代表
ALPHA = 0.05
TESTS = {"P1": ("IF", "300", "2014-01-02"), "P2": ("IH", "50", "2021-01-04")}
IH_FULL_START = "2015-04-16"                    # 000016 回填到此日 → P2 升格
IF_PRE_WINDOW = ("2014-01-02", "2015-04-15")    # 原线评窗前段
OUT_DIR = ROOT / "backtest" / "output"


# ---------------------------------------------------------------- 纯函数
def one_sided_shift_pvalue(sig: pd.Series, ret: pd.Series, k: int, direction: int,
                           n_perm: int = 1000, seed: int = 0) -> dict:
    """冻结方向的单侧循环移位置换 p：P(direction·IC_perm ≥ direction·IC_obs)。同时给双侧 p 作参考。

    移位方案与 rotation_probe.shift_permutation_pvalue 逐字一致（[2k, n−2k]），只把判据改成单侧。
    """
    idx = sig.index.intersection(ret.index)
    s, r = sig.reindex(idx), ret.reindex(idx)
    obs, n = nonoverlap_ic(s, r, k)
    if not np.isfinite(obs):
        return {"ic": float("nan"), "n_windows": n, "p_one_sided": float("nan"), "p_two_sided": float("nan")}
    rng = np.random.default_rng(seed); vals = s.to_numpy(); lo, hi = 2 * k, len(idx) - 2 * k
    c1 = c2 = 0
    for _ in range(n_perm):
        ic, _ = nonoverlap_ic(pd.Series(np.roll(vals, int(rng.integers(lo, hi))), index=idx), r, k)
        if not np.isfinite(ic):
            continue
        c1 += direction * ic >= direction * obs
        c2 += abs(ic) >= abs(obs)
    return {"ic": float(obs), "n_windows": int(n), "p_one_sided": (1 + c1) / (n_perm + 1), "p_two_sided": (1 + c2) / (n_perm + 1)}


def verdict(p1_one_sided: float, ic2: float, p2_one_sided: float | None, ih_backfilled: bool,
            direction: int = DIRECTION, alpha: float = ALPHA) -> dict:
    """预登记 §3.2：PASS = P1 单侧 p < α 且 P2 的 IC 与冻结方向同号；000016 回填后 P2 亦须 p < α。"""
    p1_ok = bool(np.isfinite(p1_one_sided) and p1_one_sided < alpha)
    ic2_ok = bool(np.isfinite(ic2) and np.sign(ic2) == np.sign(direction))
    p2_ok = True if not ih_backfilled else bool(p2_one_sided is not None and np.isfinite(p2_one_sided) and p2_one_sided < alpha)
    return {"P1_pass": p1_ok, "P2_same_sign": ic2_ok, "P2_pass_required": ih_backfilled, "P2_pass": p2_ok,
            "PASS": p1_ok and ic2_ok and p2_ok}


def yearly_ic(sig: pd.Series, ret: pd.Series, k: int) -> dict[int, float]:
    out = {}
    for y, s in sig.groupby(sig.index.year):
        ic, n = nonoverlap_ic(s, ret, k)
        if n >= 3:
            out[int(y)] = round(float(ic), 3)
    return out


# ---------------------------------------------------------------- 编排
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--force", action="store_true"); a = ap.parse_args(argv)
    series = build_replication_series(force=a.force)
    if not a.run:
        print(series.filter(like="T1_").describe().round(4).to_string()); return 0
    from backtest.data import load_underlying_returns
    ew = _load_ew_signal(); blend = load_underlying_returns("blend")
    rows, meta = [], {"lb": LB, "zw": ZW, "k": K, "direction": DIRECTION, "alpha": ALPHA, "n_perm": a.n_perm, "tests": {}}
    for tid, (pre, kj, start) in TESTS.items():
        t1 = series[f"T1_{pre}"].dropna(); t1 = t1[t1.index >= pd.Timestamp(start)]
        ret = load_underlying_returns(kj)
        sig = level_signal(t1, LB, ZW)
        main_res = one_sided_shift_pvalue(sig, ret, K, DIRECTION, a.n_perm)
        halves = {h: nonoverlap_ic(_win(sig, *HALVES[h]), ret, K) for h in HALVES}
        t2sig = level_signal(series[f"T2_{pre}"].dropna().loc[t1.index], LB, ZW)
        t2 = one_sided_shift_pvalue(t2sig, ret, K, DIRECTION, a.n_perm)
        ic_b, n_b = nonoverlap_ic(sig, blend, K); pic_b, pp_b = partial_ic_with_pvalue(sig, blend, ew, K, a.n_perm)
        info = {**main_res, "family": "T1", "test": tid, "instrument": pre, "target": kj, "first": str(sig.index.min().date()), "last": str(sig.index.max().date()),
                "ic_h1": round(halves["2014-2019"][0], 4) if np.isfinite(halves["2014-2019"][0]) else None, "n_h1": halves["2014-2019"][1],
                "ic_h2": round(halves["2020-2026"][0], 4), "n_h2": halves["2020-2026"][1],
                "T2_ic": t2["ic"], "T2_p_one_sided": t2["p_one_sided"],
                "blend_ic": ic_b, "blend_n": n_b, "blend_partial_ic_vs_ew": pic_b, "blend_partial_p": pp_b,
                "yearly_ic": yearly_ic(sig, ret, K)}
        if tid == "P1":
            pre_sig = _win(sig, *IF_PRE_WINDOW); ic_pre, n_pre = nonoverlap_ic(pre_sig, ret, K)
            info["pre_window_ic"] = ic_pre; info["pre_window_n"] = n_pre
        if tid == "P2":
            info["ih_backfilled"] = bool(t1.index.min() <= pd.Timestamp(IH_FULL_START))
        meta["tests"][tid] = info; rows.append({k_: v for k_, v in info.items() if k_ != "yearly_ic"})
    p1, p2 = meta["tests"]["P1"], meta["tests"]["P2"]
    meta["verdict"] = verdict(p1["p_one_sided"], p2["ic"], p2["p_one_sided"], p2["ih_backfilled"])
    pd.DataFrame(rows).to_csv(OUT_DIR / "basis_term_replication_panel.csv", index=False)
    (OUT_DIR / "basis_term_replication_verdict.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float))
    pd.set_option("display.width", 260); print(pd.DataFrame(rows).round(4).to_string(index=False)); print(json.dumps(meta["verdict"], ensure_ascii=False))
    for tid in TESTS: print(tid, "yearly:", meta["tests"][tid]["yearly_ic"])
    print("P1 pre-window:", round(p1["pre_window_ic"], 4) if np.isfinite(p1["pre_window_ic"]) else None, p1["pre_window_n"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
