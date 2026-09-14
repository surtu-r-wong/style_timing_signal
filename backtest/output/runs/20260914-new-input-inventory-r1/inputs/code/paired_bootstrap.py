"""①a 探针：部署口径 long-flat vs 对称口径的**同秤配对 block bootstrap**。

预登记出处：`docs/plans/2026-07-11-external-review-fixes.md` §2 末句
「若未来重议对称口径，先做两口径 Sharpe 差的同秤 bootstrap」；
闸门参数出处：`docs/plans/2026-08-12-signal-research-agenda.md` ①(f) 第 0 步
——**block=20 日、n≥1000，报 Sharpe 差的 CI 与 p，无论结果如何都不改部署**。

设计要点：
- **同一因子、同一 harness**（`baseline.evaluate` / `engine.run_strategy`），只换映射函数：
  long-flat = `positions.production_position(θ=0)`；对称 = `positions.to_position(discrete, θ=0)`。
- **配对**：两条净日收益序列按日期对齐后，bootstrap **对同一组日期块重抽样**
  （同一 index 同时作用于两序列）→ 保留两口径的同日相关性，检验的是**差**而不是各自的水平。
- 重抽样对象是**引擎产出的净日收益**（成本与 carry 已内嵌在每日收益里），
  因此块内的换手成本结构原样保留；块与块的拼接处不重算建仓成本。
- 零假设 H0：两口径总体 Sharpe 差 = 0。用 bootstrap 分布**平移到 0** 近似零分布，
  双侧 p =(#{|d* − d̂| ≥ |d̂|} + 1)/(n + 1)；CI = bootstrap 分布的 2.5/97.5 分位。

CLI: python3 -m backtest.paired_bootstrap [--block 20] [--n 10000] [--seed 0]
产出: backtest/output/probe_1a_paired_bootstrap.csv + console。

本模块**不改动** `significance.py` / `baseline.py` / 任何冻结根，只读 committed 信号 CSV。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import KOU_JING, SIGNALS, WINDOWS, _slice  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import (  # noqa: E402
    ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
)
from backtest.positions import production_position, to_position  # noqa: E402

ANN = 245
PRIMARY_SIGNAL = "equal_weight"   # 现役主信号（勿重开清单第 2 条）
PRIMARY_KOU_JING = "blend"        # headline 口径（勘误 §2 的 1.62 / 1.41 即此格）
_CHUNK = 1000                     # bootstrap 分块，控内存


# ---------------- 纯函数 ----------------
def sharpe_matrix(mat: np.ndarray) -> np.ndarray:
    """逐行 Sharpe（与 metrics.sharpe 同口径：ddof=1、×√245、std=0 → 0）。"""
    sd = mat.std(axis=1, ddof=1)
    mu = mat.mean(axis=1)
    out = np.zeros_like(mu)
    ok = np.isfinite(sd) & (sd != 0)
    out[ok] = mu[ok] / sd[ok] * np.sqrt(ANN)
    return out


def moving_block_indices(length: int, block: int, draws: int, rng) -> np.ndarray:
    """重叠（moving）block bootstrap 的行索引矩阵，形状 (draws, length)。"""
    if block < 1 or block > length:
        raise ValueError(f"block must be in [1, {length}], got {block}")
    k = int(np.ceil(length / block))
    starts = rng.integers(0, length - block + 1, size=(draws, k))
    idx = starts[:, :, None] + np.arange(block)[None, None, :]
    return idx.reshape(draws, k * block)[:, :length]


def paired_block_bootstrap_sharpe_diff(ret_a: pd.Series, ret_b: pd.Series,
                                       block: int = 20, n: int = 10000, seed: int = 0,
                                       alpha: float = 0.05) -> dict:
    """配对 block bootstrap 检验 Sharpe(a) − Sharpe(b)。

    a / b 必须已按同一日期索引对齐（同一组块索引同时作用于两者 = 配对）。
    """
    idx = ret_a.index.intersection(ret_b.index)
    a = ret_a.reindex(idx).to_numpy(dtype=float)
    b = ret_b.reindex(idx).to_numpy(dtype=float)
    length = len(a)
    d_hat = float(sharpe(pd.Series(a)) - sharpe(pd.Series(b)))

    rng = np.random.default_rng(seed)
    diffs = np.empty(n, dtype=float)
    done = 0
    while done < n:
        draws = min(_CHUNK, n - done)
        rows = moving_block_indices(length, block, draws, rng)
        diffs[done:done + draws] = sharpe_matrix(a[rows]) - sharpe_matrix(b[rows])
        done += draws

    lo, hi = np.percentile(diffs, [alpha / 2 * 100, (1 - alpha / 2) * 100])
    p = (int(np.sum(np.abs(diffs - d_hat) >= abs(d_hat))) + 1) / (n + 1)
    return {
        "diff_sharpe": d_hat,
        "ci_lo": float(lo), "ci_hi": float(hi),
        "p_value": float(p),
        "boot_mean": float(diffs.mean()), "boot_sd": float(diffs.std(ddof=1)),
        "block": block, "n_boot": n, "seed": seed, "n_obs": length,
    }


# ---------------- 编排 ----------------
def _metrics(ret: pd.Series, pos: pd.Series) -> dict:
    return {"ann": ann_return(ret), "sharpe": sharpe(ret), "maxdd": max_drawdown(ret),
            "calmar": calmar(ret), "turnover": turnover(pos), "hit": hit_rate(ret)}


def load_primary_signal(name: str = PRIMARY_SIGNAL) -> pd.Series:
    path, col = SIGNALS[name]
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    return df[col]


def build_report(block: int = 20, n: int = 10000, seed: int = 0, cost_bps: float = 3.0,
                 db=None, signal_name: str = PRIMARY_SIGNAL) -> pd.DataFrame:
    """两个**固定**口径 × 三口径 × 四窗：同秤指标 + 配对 bootstrap。"""
    raw = load_primary_signal(signal_name)
    maps = {
        "longflat": production_position(raw, threshold=0.0).astype(float),
        "symmetric": to_position(raw, mode="discrete", threshold=0.0).astype(float),
    }
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}

    rows = []
    for kj in KOU_JING:
        for win, (s, e) in WINDOWS.items():
            und = _slice(und_all[kj], s, e)
            car = _slice(car_all[kj], s, e)
            legs = {}
            for tag, pos_full in maps.items():
                pos = _slice(pos_full, s, e)
                idx = pos.index.intersection(und.index)
                p = pos.reindex(idx)
                ret = run_strategy(p, und.reindex(idx), cost_bps, car.reindex(idx))["ret"]
                legs[tag] = (ret, p)
            boot = paired_block_bootstrap_sharpe_diff(
                legs["longflat"][0], legs["symmetric"][0], block=block, n=n, seed=seed)
            row = {"signal": signal_name, "kou_jing": kj, "window": win,
                   "n_obs": boot["n_obs"]}
            for tag in ("longflat", "symmetric"):
                for k, v in _metrics(*legs[tag]).items():
                    row[f"{k}_{tag}"] = v
            row.update({k: boot[k] for k in
                        ("diff_sharpe", "ci_lo", "ci_hi", "p_value",
                         "boot_mean", "boot_sd", "block", "n_boot", "seed")})
            row["ci_excludes_zero"] = bool(row["ci_lo"] > 0 or row["ci_hi"] < 0)
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="①a：long-flat vs 对称的同秤配对 block bootstrap")
    ap.add_argument("--block", type=int, default=20, help="块长（预登记 20 日）")
    ap.add_argument("--n", type=int, default=10000, help="重抽样次数（预登记 ≥1000）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()
    if args.n < 1000:
        raise SystemExit("预登记要求 n≥1000")

    rep = build_report(block=args.block, n=args.n, seed=args.seed, cost_bps=args.cost_bps)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "probe_1a_paired_bootstrap.csv"
    rep.to_csv(out_path, index=False)

    show = rep[["kou_jing", "window", "n_obs", "sharpe_longflat", "sharpe_symmetric",
                "diff_sharpe", "ci_lo", "ci_hi", "p_value",
                "maxdd_longflat", "maxdd_symmetric",
                "turnover_longflat", "turnover_symmetric"]].copy()
    for c in ["maxdd_longflat", "maxdd_symmetric"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["sharpe_longflat", "sharpe_symmetric", "diff_sharpe", "ci_lo", "ci_hi",
              "turnover_longflat", "turnover_symmetric"]:
        show[c] = show[c].round(3)
    show["p_value"] = show["p_value"].round(4)
    print(show.to_string(index=False))
    print(f"\nblock={args.block} n={args.n} seed={args.seed} · 主判据格 = "
          f"{PRIMARY_KOU_JING} / full")
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
