"""rotation 短窗本质验证：三关闸门（显著且独立 / 分窗稳健 / 成本后为正）。

设计：docs/plans/2026-07-08-rotation-probe-design.md。rotation := v1 混合价差 −
v2 行业中性价差（纯行业配置分量）。本模块回答：其短窗信息是否构成独立、显著、
成本后存活的快频信号线——任一关不过即停线归档。

CLI: python3 -m backtest.rotation_probe [--universe U2] [--n-perm 1000]
产出: backtest/output/rotation_probe.csv + 控制台三关裁决。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402


def series_signal(ret: pd.Series, lookback: int, z_window: int,
                  smoothing: int = 0) -> pd.Series:
    """单收益序列 → 短窗信号（生产管线 _compute_pair_signal 的单序列薄包装）。

    构造 nav=cumprod(1+ret) 对常数腿走同一管线（rolling lb 累计 → z(zw) → tanh），
    与 equal_weight 生产口径零漂移；smoothing>0 时 rolling mean 平滑。
    """
    nav = (1.0 + ret).cumprod()
    ones = pd.Series(1.0, index=ret.index)
    sig = _compute_pair_signal(nav, ones, lookback=lookback, z_window=z_window)
    if smoothing > 0:
        sig = sig.rolling(smoothing, min_periods=1).mean()
    return sig


def _nonoverlap_frame(sig: pd.Series, ret: pd.Series, k: int) -> pd.DataFrame:
    """非重叠采样帧：块末（位置 k−1, 2k−1, …）的 [sig, fwd(k 日收益和)]。"""
    idx = sig.index.intersection(ret.index)
    s = sig.reindex(idx)
    r = ret.reindex(idx)
    fwd = r.rolling(k).sum().shift(-k)
    pts = np.arange(k - 1, len(idx), k)
    return pd.concat(
        [s.iloc[pts].rename("sig"), fwd.iloc[pts].rename("fwd")], axis=1
    ).dropna()


def nonoverlap_ic(sig: pd.Series, ret: pd.Series, k: int) -> tuple[float, int]:
    """非重叠 k 日窗 Spearman IC：块末信号 vs 下一块收益和。

    重叠窗 IC 因窗口共享收益而自相关膨胀；非重叠采样每个前瞻窗互不相交，
    Spearman 的名义显著性才有意义。返回 (ic, n_windows)。
    """
    joint = _nonoverlap_frame(sig, ret, k)
    if len(joint) < 3:
        return float("nan"), len(joint)
    ic = joint["sig"].corr(joint["fwd"], method="spearman")
    return float(ic), len(joint)


def shift_permutation_pvalue(sig: pd.Series, ret: pd.Series, k: int,
                             n_perm: int = 1000, seed: int = 0) -> float:
    """循环移位置换检验（双侧）：|IC_perm| ≥ |IC_obs| 的占比。

    循环移位保留信号与收益**各自**的自相关结构、只打断二者配对——比 iid 置换
    更保守（时序 IC 显著性的干净零假设）。移位量取 [2k, n−2k] 避免小位移残留配对。
    """
    idx = sig.index.intersection(ret.index)
    s = sig.reindex(idx)
    r = ret.reindex(idx)
    obs, _ = nonoverlap_ic(s, r, k)
    if not np.isfinite(obs):
        return float("nan")
    rng = np.random.default_rng(seed)
    vals = s.to_numpy()
    lo, hi = 2 * k, len(idx) - 2 * k
    count = 0
    for _ in range(n_perm):
        shift = int(rng.integers(lo, hi))
        ic, _ = nonoverlap_ic(pd.Series(np.roll(vals, shift), index=idx), r, k)
        if np.isfinite(ic) and abs(ic) >= abs(obs):
            count += 1
    return (1 + count) / (n_perm + 1)


def partial_rank_ic(sig: pd.Series, fwd: pd.Series, control: pd.Series) -> float:
    """偏 rank IC：控制 control 后 sig 对 fwd 的残差相关（增量信息判定）。

    三序列对齐 → rank → sig/fwd 各对 control 一元 OLS 取残差 → Pearson(残差, 残差)
    （偏 Spearman 的标准近似）。≈0 = 信息全被 control 解释（无独立增量）。
    """
    joint = pd.concat(
        [sig.rename("s"), fwd.rename("f"), control.rename("c")], axis=1
    ).dropna()
    if len(joint) < 10:
        return float("nan")
    rk = joint.rank()

    def _resid(y: pd.Series, x: pd.Series) -> np.ndarray:
        xm = x - x.mean()
        beta = (xm * (y - y.mean())).sum() / (xm ** 2).sum()
        return (y - y.mean() - beta * xm).to_numpy()

    rs = _resid(rk["s"], rk["c"])
    rf = _resid(rk["f"], rk["c"])
    denom = np.sqrt((rs ** 2).sum() * (rf ** 2).sum())
    if denom < 1e-9:
        # 任一残差方差退化 = 该变量被 control 完全解释 → 独立增量确定为 0
        return 0.0
    return float((rs * rf).sum() / denom)


def hold_position(signal: pd.Series, k: int) -> pd.Series:
    """每 k 日按信号符号换仓、其间保持的仓位序列（{−1,0,+1}）。

    换仓点=位置 0, k, 2k, …（T 日决策仓位；backtest.engine 内部 shift(1) 做 T+1
    生效，此处不重复滞后）。持有期与 IC 前瞻窗 k 对齐。
    """
    pos = pd.Series(np.nan, index=signal.index)
    pts = np.arange(0, len(signal), k)
    pos.iloc[pts] = np.sign(signal.iloc[pts])
    return pos.ffill().fillna(0.0)


# ---------------------------------------------------------------- 编排（三关裁决）
GRID_LB = [5, 10, 20]
GRID_SM = [0, 3]
GRID_K = [3, 5, 10, 20]
HALVES = {"2014-2019": ("2014-01-01", "2019-12-31"),
          "2020-2026": ("2020-01-01", "2026-12-31")}


def _win(s: pd.Series, a: str, b: str) -> pd.Series:
    return s[(s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))]


def _load_rotation(uni: str) -> pd.Series:
    from signals.style_basket.build import OUT_DIR
    v1 = pd.read_csv(OUT_DIR / f"spread_{uni}.csv", parse_dates=["date"]).set_index("date")
    v2 = pd.read_csv(OUT_DIR / f"spread_{uni}_neutral.csv", parse_dates=["date"]).set_index("date")
    joint = pd.concat([v1["spread"].rename("v1"), v2["spread"].rename("v2")],
                      axis=1, join="inner").dropna()
    return (joint["v1"] - joint["v2"]).rename("rotation")


def _load_ew_signal() -> pd.Series:
    df = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                     parse_dates=["date"]).set_index("date")
    return df["factor_value"]


def partial_ic_with_pvalue(sig: pd.Series, ret: pd.Series, control: pd.Series,
                           k: int, n_perm: int = 1000, seed: int = 0) -> tuple[float, float]:
    """非重叠采样口径的偏 rank IC（控 control）+ 循环移位置换 p（移 sig、控/收益不动）。"""
    idx = sig.index.intersection(ret.index).intersection(control.index)
    s, r, c = sig.reindex(idx), ret.reindex(idx), control.reindex(idx)
    frame = _nonoverlap_frame(s, r, k)
    ctrl_pts = c.reindex(frame.index)
    obs = partial_rank_ic(frame["sig"], frame["fwd"], ctrl_pts)
    if not np.isfinite(obs):
        return obs, float("nan")
    rng = np.random.default_rng(seed)
    vals = s.to_numpy()
    lo, hi = 2 * k, len(idx) - 2 * k
    count = 0
    for _ in range(n_perm):
        shift = int(rng.integers(lo, hi))
        f = _nonoverlap_frame(pd.Series(np.roll(vals, shift), index=idx), r, k)
        pic = partial_rank_ic(f["sig"], f["fwd"], c.reindex(f.index))
        if np.isfinite(pic) and abs(pic) >= abs(obs):
            count += 1
    return obs, (1 + count) / (n_perm + 1)


def run_probe(uni: str = "U2", n_perm: int = 1000, cost_bps: float = 3.0,
              db=None) -> tuple[pd.DataFrame, dict]:
    from backtest.data import load_carry, load_underlying_returns
    from backtest.engine import run_strategy
    from backtest.metrics import ann_return, sharpe, turnover

    rotation = _load_rotation(uni)
    und = {kj: load_underlying_returns(kj, db=db) for kj in ["500", "1000", "blend"]}
    carry_blend = load_carry("blend", db=db)
    ew = _load_ew_signal()

    signals = {
        f"lb{lb}sm{sm}": series_signal(rotation, lb, 2 * lb, sm)
        for lb in GRID_LB for sm in GRID_SM
    }

    # ① IC 面板（全窗 + 两半窗）
    rows = []
    for form, sig in signals.items():
        for k in GRID_K:
            for kj, u in und.items():
                ic, n = nonoverlap_ic(sig, u, k)
                row = {"universe": uni, "form": form, "k": k, "kou_jing": kj,
                       "ic": ic, "n_windows": n}
                for half, (a, b) in HALVES.items():
                    row[f"ic_{half}"] = nonoverlap_ic(_win(sig, a, b), _win(u, a, b), k)[0]
                rows.append(row)
    panel = pd.DataFrame(rows)

    # ② 高原代表：blend 上 worst-half 最大且全窗 ic>0
    blend = panel[panel["kou_jing"] == "blend"].copy()
    blend["worst_half"] = blend[[f"ic_{h}" for h in HALVES]].min(axis=1)
    cand = blend[blend["ic"] > 0]
    best = (cand.sort_values("worst_half", ascending=False).iloc[0]
            if len(cand) else blend.sort_values("ic", ascending=False).iloc[0])
    form, k = best["form"], int(best["k"])
    sig = signals[form]

    # ③ 三关
    p_ic = shift_permutation_pvalue(sig, und["blend"], k, n_perm=n_perm)
    pic, p_pic = partial_ic_with_pvalue(sig, und["blend"], ew, k, n_perm=n_perm)
    gate1 = bool(best["ic"] > 0 and p_ic < 0.05 and pic > 0 and p_pic < 0.05)
    halves_ic = [best[f"ic_{h}"] for h in HALVES]
    gate2 = bool(min(halves_ic) > 0)
    pos = hold_position(sig, k)
    strat = run_strategy(pos, und["blend"].reindex(pos.index), cost_bps, carry_blend)
    net_sharpe = sharpe(strat["ret"].dropna())
    gate3 = bool(net_sharpe > 0)

    verdict = {
        "universe": uni, "best_form": form, "best_k": k,
        "ic": float(best["ic"]), "ic_pvalue": p_ic,
        "partial_ic_vs_ew": pic, "partial_ic_pvalue": p_pic,
        **{f"ic_{h}": float(best[f"ic_{h}"]) for h in HALVES},
        "net_sharpe": net_sharpe, "net_ann": ann_return(strat["ret"].dropna()),
        "turnover": turnover(pos),
        "gate1_significant_and_independent": gate1,
        "gate2_stable_halves": gate2,
        "gate3_net_positive": gate3,
        "PASS": gate1 and gate2 and gate3,
    }
    return panel, verdict


def main() -> int:
    ap = argparse.ArgumentParser(description="rotation 短窗本质验证（三关闸门）")
    ap.add_argument("--universe", default="U2")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    panel, verdict = run_probe(args.universe, args.n_perm, args.cost_bps)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_file = out_dir / "rotation_probe.csv"
    panel.to_csv(panel_file, index=False)

    show = panel[panel["kou_jing"] == "blend"].copy()
    for c in ["ic"] + [f"ic_{h}" for h in HALVES]:
        show[c] = show[c].round(3)
    print("=== IC 面板（blend 口径） ===")
    print(show.drop(columns=["universe", "kou_jing"]).to_string(index=False))
    print("\n=== 三关裁决 ===")
    for key, val in verdict.items():
        print(f"  {key}: {val:.4f}" if isinstance(val, float) else f"  {key}: {val}")
    print(f"\n{'★ PASS：三关全过 → 进快频腿引擎设计' if verdict['PASS'] else '✗ STOP：停线归档（负结果入库）'}")
    print(f"→ {panel_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
