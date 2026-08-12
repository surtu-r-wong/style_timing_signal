"""③ 探针：现任 equal_weight × 第一替补 slope L20 zw120 sm0 的**单点等权融合**。

预登记出处：`docs/plans/2026-08-12-signal-research-agenda.md` §3 ③(f)——
> 融合口径预登记为**因子层等权**（z 后平均），只测 1 个点，**不扫权重**。
> **⓪ 先过同秤头对头**：融合因子 vs 现任的**非偏 rank IC**（同 harness、同窗、非重叠 k 日），
> **先于收益层判据**。**GO**：① train 与 val 双窗均不劣于现任；② worst(train,val) 提升 ≥ 0.10；
> ③ 换手不增。任一不中 → 维持现任、slope20 保留为替补不融合。

设计要点：
- **融合权重是硬编码 50/50，函数签名里没有权重参数**——扫权重在实现层面即不可达
  （§7 三条"放行≠免闸"之一）。`fuse_equal` 用**交集 + 显式相加**而非 `mean(skipna)`，
  防止 07-11 勘误 §2 那种"缺腿被放大一倍"的静默错误。
- **第一替补的精确定义**（07-11 勘误 §3 登记变更）：`slope_L20s0_zw120_sm0`
  = `momentum_scan.momentum_pair_factor(family="slope", length=20, skip=0,
    z_window=120, smoothing=0)`，与现任共用 `config_4pairs.csv` 四对与 z→tanh→等权下游。
- **同秤**（§4 C4）：收益层唯一对照 harness = `backtest.baseline.build_report`，
  部署口径 = `positions.production_position`（long-flat, θ=0）× blend。
- **IC 的 T+1 对齐**与引擎一致（`engine.run_strategy` 用 `position.shift(1)`）：
  因子在 t，前瞻收益取 t+1..t+k 的累计现货收益（不含 carry/成本——carry 是与信号无关的
  持有收益，进 IC 只会污染"预测力"口径）。
- 三个因子在**同一张非重叠 k 日抽样网格**上算 rank IC（同窗同样本，头对头才成立）。

CLI: python3 -m backtest.fusion_probe [--k 20] [--n 10000] [--seed 0]
产出: backtest/output/probe_3_fusion_{ic,returns,paired_bootstrap,verdict}.csv + console。

本模块**不改动** `significance.py` / `baseline.py` / `paired_bootstrap.py` / 任何冻结根。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import KOU_JING, WINDOWS, _slice, build_report  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.positions import production_position, to_position  # noqa: E402

INCUMBENT = "incumbent_ew"          # 现役主信号 equal_weight 20d/z40/sm5
SUBSTITUTE = "slope20"              # 第一替补 slope L20 zw120 sm0（勘误 §3 登记）
FUSED = "fused_ew_slope20"          # 预登记单点 (ew + slope20)/2
PRIMARY_KOU_JING = "blend"          # headline 口径
TRAIN, VAL, HOLDOUT, FULL = "2014-2020", "2021-2023", "2024-2026", "full"
SELECT_WINDOWS = (TRAIN, VAL)       # 选择端只用 train/val；2024-26 仅报告（§4 C2）
GATE_WORST_TV_LIFT = 0.10           # ③(f) 收益层判据 ②
_CHUNK = 2000                       # bootstrap 分块，控内存


# ---------------- 纯函数：融合 ----------------
def fuse_equal(a: pd.Series, b: pd.Series) -> pd.Series:
    """因子层等权融合 = (a + b) / 2，**固定 50/50**。

    没有权重参数：预登记只测 1 个点，扫权重即违背 §7。
    对齐用两序列**非空索引的交集**再显式相加——`DataFrame.mean(axis=1)` 默认 skip-NaN，
    缺腿时会把单腿按全额计（07-11 勘误 §2 的 blend carry bug 就是这个形状）。
    """
    idx = a.dropna().index.intersection(b.dropna().index).sort_values()
    return (a.reindex(idx).astype(float) + b.reindex(idx).astype(float)) / 2.0


# ---------------- 纯函数：非重叠 rank IC ----------------
def forward_return(underlying: pd.Series, k: int) -> pd.Series:
    """t 处的值 = t+1..t+k 的累计收益（与引擎 T+1 生效口径一致）。"""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    logsum = np.log1p(underlying.astype(float)).rolling(k, min_periods=k).sum()
    return (np.expm1(logsum)).shift(-k)


def nonoverlap_grid(index: pd.DatetimeIndex, k: int, offset: int = 0) -> pd.DatetimeIndex:
    """从 offset 起每隔 k 行取一个样本点 → 相邻样本的前瞻窗互不重叠。"""
    if not 0 <= offset < max(k, 1):
        raise ValueError(f"offset must be in [0, {k}), got {offset}")
    return index[offset::k]


def rank_ic(x: pd.Series, y: pd.Series) -> dict:
    """非偏 rank IC = Spearman(因子, 前瞻收益) + 其 t 检验（样本已非重叠→近独立）。"""
    n = len(x)
    if n < 3:
        return {"ic": float("nan"), "n_obs": n, "t_stat": float("nan"),
                "p_value": float("nan")}
    r = float(stats.spearmanr(x.to_numpy(), y.to_numpy()).statistic)
    if not np.isfinite(r) or abs(r) >= 1.0:
        return {"ic": r, "n_obs": n, "t_stat": float("nan"), "p_value": float("nan")}
    t = r * np.sqrt((n - 2) / (1.0 - r ** 2))
    p = float(2.0 * stats.t.sf(abs(t), df=n - 2))
    return {"ic": r, "n_obs": n, "t_stat": float(t), "p_value": p}


def spearman_rows(x_mat: np.ndarray, y_mat: np.ndarray) -> np.ndarray:
    """逐行 Spearman（平均秩处理并列），与 `scipy.stats.spearmanr` 同口径、向量化。"""
    rx = stats.rankdata(x_mat, axis=1)
    ry = stats.rankdata(y_mat, axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    ry = ry - ry.mean(axis=1, keepdims=True)
    den = np.sqrt((rx ** 2).sum(axis=1) * (ry ** 2).sum(axis=1))
    out = np.full(len(den), np.nan)
    ok = den > 0
    out[ok] = (rx * ry).sum(axis=1)[ok] / den[ok]
    return out


def paired_ic_bootstrap(x_a: pd.Series, x_b: pd.Series, y: pd.Series,
                        n: int = 10000, seed: int = 0, alpha: float = 0.05) -> dict:
    """配对重抽样检验 rank_IC(a) − rank_IC(b)：同一组行索引同时作用于 a/b/y。

    抽样单位是**非重叠 k 日观测**（近独立），故用 i.i.d. bootstrap 而非 block。
    双侧 p 用 bootstrap 分布平移到 0 近似零分布（与 `paired_bootstrap` 同约定）。
    """
    a = x_a.to_numpy(dtype=float)
    b = x_b.to_numpy(dtype=float)
    yy = y.to_numpy(dtype=float)
    m = len(yy)
    d_hat = (float(stats.spearmanr(a, yy).statistic)
             - float(stats.spearmanr(b, yy).statistic))

    rng = np.random.default_rng(seed)
    diffs = np.empty(n, dtype=float)
    done = 0
    while done < n:
        draws = min(_CHUNK, n - done)
        rows = rng.integers(0, m, size=(draws, m))
        ymat = yy[rows]
        diffs[done:done + draws] = (spearman_rows(a[rows], ymat)
                                    - spearman_rows(b[rows], ymat))
        done += draws
    diffs = diffs[np.isfinite(diffs)]
    lo, hi = np.percentile(diffs, [alpha / 2 * 100, (1 - alpha / 2) * 100])
    p = (int(np.sum(np.abs(diffs - d_hat) >= abs(d_hat))) + 1) / (len(diffs) + 1)
    return {"diff_ic": d_hat, "ci_lo": float(lo), "ci_hi": float(hi),
            "p_value": float(p), "boot_mean": float(diffs.mean()),
            "boot_sd": float(diffs.std(ddof=1)), "n_boot": int(len(diffs)),
            "n_obs": m, "seed": seed,
            "ci_excludes_zero": bool(lo > 0 or hi < 0)}


# ---------------- 编排 ----------------
def build_factors(start=None) -> dict:
    """三个因子：现任 / 第一替补 / 预登记单点融合（同一 PG 价格底座、同一四对配置）。"""
    from backtest.momentum_scan import momentum_factor_fn
    from backtest.scan import equal_weight_factor_fn

    ew = equal_weight_factor_fn(start)(lookback=20, z_window=40, smoothing=5)
    slope = momentum_factor_fn(start)(family="slope", length=20, skip=0,
                                      z_window=120, smoothing=0)
    return {INCUMBENT: ew, SUBSTITUTE: slope, FUSED: fuse_equal(ew, slope)}


def build_ic_report(factors: dict, k: int = 20, offset: int = 0, n_boot: int = 10000,
                    seed: int = 0, kou_jing: str = PRIMARY_KOU_JING, db=None) -> tuple:
    """闸门 ⓪：三因子在**同一张非重叠 k 日网格**上的非偏 rank IC + 配对差检验。"""
    und = load_underlying_returns(kou_jing, db=db)
    fwd = forward_return(und, k).dropna()

    common = fwd.index
    for f in factors.values():
        common = common.intersection(f.dropna().index)
    common = common.sort_values()

    ic_rows, diff_rows = [], []
    for win, (s, e) in WINDOWS.items():
        idx_w = _slice(pd.Series(0.0, index=common), s, e).index
        grid = nonoverlap_grid(idx_w, k, offset)
        if len(grid) < 10:
            continue
        y = fwd.reindex(grid)
        cols = {name: f.reindex(grid) for name, f in factors.items()}
        for name, x in cols.items():
            ic_rows.append({"window": win, "factor": name, "kou_jing": kou_jing,
                            "k": k, "offset": offset, **rank_ic(x, y)})
        for challenger in (FUSED, SUBSTITUTE):
            boot = paired_ic_bootstrap(cols[challenger], cols[INCUMBENT], y,
                                       n=n_boot, seed=seed)
            diff_rows.append({"window": win, "challenger": challenger,
                              "reference": INCUMBENT, "kou_jing": kou_jing,
                              "k": k, "offset": offset, **boot})
    return pd.DataFrame(ic_rows), pd.DataFrame(diff_rows)


def build_divergence_report(factors: dict, kou_jing: str = PRIMARY_KOU_JING,
                            db=None) -> pd.DataFrame:
    """分歧日逐日明细（诊断，非闸门）：两腿 long-flat 仓位、|因子值|、融合站哪边、T+1 收益。

    long-flat 下两腿在分歧日**严格互补**（一腿 1、一腿 0），故
    `capture(现任) + capture(替补) = Σ fwd(分歧日)` = "可得收益"，抛硬币期望为其一半。
    融合的仲裁规则是恒等式：`(a+b)/2 > 0` ⇔ 跟随 |·| 较大的那一腿（并列 → 跟随空仓腿）。
    """
    und = load_underlying_returns(kou_jing, db=db)
    idx = factors[FUSED].index.intersection(und.index).sort_values()
    vals = {name: f.reindex(idx).astype(float) for name, f in factors.items()}
    pos = {name: production_position(v).astype(int) for name, v in vals.items()}
    fwd = und.shift(-1).reindex(idx)                      # T 信号 → T+1 生效

    divergent = pos[INCUMBENT] != pos[SUBSTITUTE]
    follows = np.where(pos[FUSED] == pos[INCUMBENT], INCUMBENT, SUBSTITUTE)
    larger = np.where(vals[INCUMBENT].abs() > vals[SUBSTITUTE].abs(), INCUMBENT, SUBSTITUTE)
    out = pd.DataFrame({
        "date": idx,
        "divergent": divergent.to_numpy(),
        "pos_incumbent": pos[INCUMBENT].to_numpy(),
        "pos_slope20": pos[SUBSTITUTE].to_numpy(),
        "pos_fused": pos[FUSED].to_numpy(),
        "abs_incumbent": vals[INCUMBENT].abs().to_numpy(),
        "abs_slope20": vals[SUBSTITUTE].abs().to_numpy(),
        "follows": follows,
        "larger_abs_leg": larger,
        "fwd_ret_t1": fwd.to_numpy(),
    })
    out["window"] = "other"
    for win, (s, e) in WINDOWS.items():
        if win == FULL:
            continue
        m = (out["date"] >= pd.Timestamp(s)) & (out["date"] <= pd.Timestamp(e))
        out.loc[m, "window"] = win
    return out


def summarize_divergence(div: pd.DataFrame, n_boot: int = 10000, seed: int = 0) -> dict:
    """分歧日捕获率 + 随机仲裁基准的随机化检验（诊断，非闸门）。"""
    d = div[div["divergent"] & div["fwd_ret_t1"].notna()]
    n = len(d)
    fwd = d["fwd_ret_t1"].to_numpy(dtype=float)
    cap = {name: float((d[f"pos_{tag}"].to_numpy() * fwd).sum())
           for name, tag in ((INCUMBENT, "incumbent"), (SUBSTITUTE, "slope20"),
                             (FUSED, "fused"))}
    available = cap[INCUMBENT] + cap[SUBSTITUTE]      # 两腿互补 → 等于 Σ fwd

    rng = np.random.default_rng(seed)
    long_leg = np.where(d["pos_incumbent"].to_numpy() == 1, 1.0, 0.0)
    sims = np.empty(n_boot)
    done = 0
    while done < n_boot:
        draws = min(_CHUNK, n_boot - done)
        coin = rng.integers(0, 2, size=(draws, n))     # 1=跟现任，0=跟替补
        pick = np.where(coin == 1, long_leg, 1.0 - long_leg)
        sims[done:done + draws] = (pick * fwd).sum(axis=1)
        done += draws
    obs = cap[FUSED]
    p_left = float(np.mean(sims <= obs))
    p_two = float(min(1.0, 2.0 * min(p_left, float(np.mean(sims >= obs)))))

    good = fwd > 0
    bad = fwd < 0
    correct = int(((d["pos_fused"].to_numpy() == 1) & good).sum()
                  + ((d["pos_fused"].to_numpy() == 0) & bad).sum())
    n_decided = int((good | bad).sum())
    binom_p = float(stats.binomtest(correct, n_decided, 0.5).pvalue) if n_decided else float("nan")

    return {
        "n_days_total": int(len(div)), "n_days_divergent": n,
        "divergent_share": n / len(div) if len(div) else float("nan"),
        "available_return": available,
        "capture_incumbent": cap[INCUMBENT], "capture_slope20": cap[SUBSTITUTE],
        "capture_fused": obs,
        "capture_ratio_fused": obs / available if available else float("nan"),
        "random_expectation": available / 2.0,
        "random_p_two_sided": p_two, "random_p_left": p_left,
        "random_ci_lo": float(np.percentile(sims, 2.5)),
        "random_ci_hi": float(np.percentile(sims, 97.5)),
        "follows_incumbent_days": int((d["follows"] == INCUMBENT).sum()),
        "follows_slope20_days": int((d["follows"] == SUBSTITUTE).sum()),
        "follows_equals_larger_abs": bool((d["follows"] == d["larger_abs_leg"]).all()),
        "correct_choice_days": correct, "n_decided": n_decided,
        "correct_choice_rate": correct / n_decided if n_decided else float("nan"),
        "correct_binomial_p": binom_p,
        "median_abs_incumbent_divergent": float(d["abs_incumbent"].median()),
        "median_abs_slope20_divergent": float(d["abs_slope20"].median()),
        "median_abs_incumbent_all": float(div["abs_incumbent"].median()),
        "median_abs_slope20_all": float(div["abs_slope20"].median()),
        "n_boot": n_boot, "seed": seed,
    }


def build_return_report(factors: dict, cost_bps: float = 3.0, db=None) -> pd.DataFrame:
    """收益层：唯一对照 harness `baseline.build_report`，两映射同秤（§4 C4）。"""
    positions = {}
    for name, fac in factors.items():
        positions[f"{name}_lf"] = production_position(fac).astype(float)
        positions[f"{name}_sym"] = to_position(fac, mode="discrete").astype(float)
    return build_report(bootstrap_n=0, cost_bps=cost_bps, db=db, positions=positions)


def build_paired_sharpe_report(factors: dict, block: int = 20, n: int = 10000, seed: int = 0,
                               cost_bps: float = 3.0, db=None) -> pd.DataFrame:
    """诊断列（非闸门）：融合 vs 现任的 Sharpe 差配对 block bootstrap，部署口径 long-flat。"""
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}
    maps = {name: production_position(f).astype(float) for name, f in factors.items()}

    rows = []
    for kj in KOU_JING:
        for win, (s, e) in WINDOWS.items():
            und = _slice(und_all[kj], s, e)
            car = _slice(car_all[kj], s, e)
            legs = {}
            for name, pos_full in maps.items():
                pos = _slice(pos_full, s, e)
                idx = pos.index.intersection(und.index)
                legs[name] = run_strategy(pos.reindex(idx), und.reindex(idx),
                                          cost_bps, car.reindex(idx))["ret"]
            if min(len(r) for r in legs.values()) < 60:
                continue
            for challenger in (FUSED, SUBSTITUTE):
                boot = paired_block_bootstrap_sharpe_diff(
                    legs[challenger], legs[INCUMBENT], block=block, n=n, seed=seed)
                rows.append({"kou_jing": kj, "window": win, "mapping": "longflat",
                             "challenger": challenger, "reference": INCUMBENT, **boot,
                             "ci_excludes_zero": bool(boot["ci_lo"] > 0 or boot["ci_hi"] < 0)})
    return pd.DataFrame(rows)


# ---------------- 闸门判定 ----------------
def _cell(ret_rep, factor, mapping, kj, win, col):
    m = ((ret_rep["signal"] == f"{factor}_{mapping}") & (ret_rep["kou_jing"] == kj)
         & (ret_rep["window"] == win) & (ret_rep["seg"] == "full"))
    hit = ret_rep.loc[m, col]
    return float(hit.iloc[0]) if len(hit) else float("nan")


def evaluate_gates(ic_rep: pd.DataFrame, ic_diff: pd.DataFrame, ret_rep: pd.DataFrame,
                   kou_jing: str = PRIMARY_KOU_JING, mapping: str = "lf") -> pd.DataFrame:
    """③(f) 两道闸门逐条判定；⓪ 的通过读法见模块 docstring 与报告"自由度"清单。"""
    def ic_of(factor, win):
        hit = ic_rep[(ic_rep["factor"] == factor) & (ic_rep["window"] == win)]["ic"]
        return float(hit.iloc[0]) if len(hit) else float("nan")

    def diff_of(win, col):
        hit = ic_diff[(ic_diff["challenger"] == FUSED) & (ic_diff["window"] == win)][col]
        return hit.iloc[0] if len(hit) else float("nan")

    rows = []
    # 闸门 ⓪：同秤头对头非偏 rank IC，train/val 双窗均严格高于现任，且"明确优于而非平手"
    gate0_higher = all(ic_of(FUSED, w) > ic_of(INCUMBENT, w) for w in SELECT_WINDOWS)
    gate0_sig = any(bool(diff_of(w, "ci_excludes_zero")) for w in SELECT_WINDOWS)
    for w in SELECT_WINDOWS:
        rows.append({"gate": "0_rank_ic", "window": w,
                     "metric": "rank_ic", "incumbent": ic_of(INCUMBENT, w),
                     "fused": ic_of(FUSED, w), "delta": ic_of(FUSED, w) - ic_of(INCUMBENT, w),
                     "threshold": 0.0, "pass": bool(ic_of(FUSED, w) > ic_of(INCUMBENT, w))})
    rows.append({"gate": "0_rank_ic", "window": "train+val", "metric": "verdict",
                 "incumbent": float("nan"), "fused": float("nan"), "delta": float("nan"),
                 "threshold": float("nan"), "pass": bool(gate0_higher and gate0_sig)})

    # 收益层：① 双窗不劣；② worst(train,val) 提升 ≥ 0.10；③ 换手不增
    sh = {w: (_cell(ret_rep, INCUMBENT, mapping, kou_jing, w, "sharpe"),
              _cell(ret_rep, FUSED, mapping, kou_jing, w, "sharpe"))
          for w in (TRAIN, VAL, HOLDOUT, FULL)}
    for w in SELECT_WINDOWS:
        inc, fus = sh[w]
        rows.append({"gate": "1_sharpe_not_worse", "window": w, "metric": "sharpe",
                     "incumbent": inc, "fused": fus, "delta": fus - inc,
                     "threshold": 0.0, "pass": bool(fus >= inc)})
    worst_inc = min(sh[w][0] for w in SELECT_WINDOWS)
    worst_fus = min(sh[w][1] for w in SELECT_WINDOWS)
    rows.append({"gate": "2_worst_tv_lift", "window": "train+val", "metric": "worst_sharpe",
                 "incumbent": worst_inc, "fused": worst_fus,
                 "delta": worst_fus - worst_inc, "threshold": GATE_WORST_TV_LIFT,
                 "pass": bool(worst_fus - worst_inc >= GATE_WORST_TV_LIFT)})
    for w in (TRAIN, VAL, FULL):
        inc = _cell(ret_rep, INCUMBENT, mapping, kou_jing, w, "turnover")
        fus = _cell(ret_rep, FUSED, mapping, kou_jing, w, "turnover")
        rows.append({"gate": "3_turnover_not_up", "window": w, "metric": "turnover",
                     "incumbent": inc, "fused": fus, "delta": fus - inc,
                     "threshold": 0.0, "pass": bool(fus <= inc)})

    rep = pd.DataFrame(rows)
    gate0 = bool(rep[(rep["gate"] == "0_rank_ic") & (rep["metric"] == "verdict")]["pass"].iloc[0])
    revenue = bool(rep[rep["gate"].str.startswith(("1_", "2_", "3_"))]["pass"].all())
    rep = pd.concat([rep, pd.DataFrame([
        {"gate": "OVERALL", "window": f"{kou_jing}/{mapping}", "metric": "verdict",
         "incumbent": float("nan"), "fused": float("nan"), "delta": float("nan"),
         "threshold": float("nan"), "pass": bool(gate0 and revenue)}])], ignore_index=True)
    return rep


def production_csv_max_abs_diff(incumbent: pd.Series) -> float:
    """现任重建 vs committed 生产信号 CSV 的逐日最大绝对差（溯源锚点，落盘存证）。"""
    from backtest.paired_bootstrap import load_primary_signal

    ref = load_primary_signal()
    idx = incumbent.index.intersection(ref.index)
    return float((incumbent.reindex(idx) - ref.reindex(idx)).abs().max())


def build_metadata(factors: dict, div_summary: dict, params: dict) -> pd.DataFrame:
    """落盘元数据行：溯源锚点 + 全部运行参数 + 分歧日诊断标量（独立方可复核）。"""
    inc = factors[INCUMBENT]
    items = {
        "corr_slope20_vs_incumbent": float(factors[SUBSTITUTE].corr(inc)),
        "corr_fused_vs_incumbent": float(factors[FUSED].corr(inc)),
        "max_abs_diff_incumbent_vs_production_csv": production_csv_max_abs_diff(inc),
        "sample_start_yyyymmdd": int(inc.index.min().strftime("%Y%m%d")),
        "sample_end_yyyymmdd": int(inc.index.max().strftime("%Y%m%d")),
        "n_days_sample": int(len(inc)),
        **{k: float(v) for k, v in params.items()},
        **{f"divergence_{k}": float(v) for k, v in div_summary.items()
           if isinstance(v, (int, float, bool, np.floating, np.integer))},
    }
    return pd.DataFrame([{"gate": "META", "window": "-", "metric": k, "value": v}
                         for k, v in items.items()])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="③：现任 equal_weight × 第一替补 slope L20 zw120 sm0 的单点等权融合")
    ap.add_argument("--k", type=int, default=20, help="非重叠前瞻天数（预登记未钉死，取 20）")
    ap.add_argument("--offset", type=int, default=0, help="非重叠抽样起点，默认 0")
    ap.add_argument("--n", type=int, default=10000, help="bootstrap 次数")
    ap.add_argument("--block", type=int, default=20, help="Sharpe 差 block bootstrap 块长")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    factors = build_factors()
    corr = {name: float(f.corr(factors[INCUMBENT])) for name, f in factors.items()}

    ic_rep, ic_diff = build_ic_report(factors, k=args.k, offset=args.offset,
                                      n_boot=args.n, seed=args.seed)
    ret_rep = build_return_report(factors, cost_bps=args.cost_bps)
    pair_rep = build_paired_sharpe_report(factors, block=args.block, n=args.n,
                                          seed=args.seed, cost_bps=args.cost_bps)
    div_rep = build_divergence_report(factors)
    div_sum = summarize_divergence(div_rep, n_boot=args.n, seed=args.seed)
    verdict = evaluate_gates(ic_rep, ic_diff, ret_rep)
    meta = build_metadata(factors, div_sum, {
        "cost_bps": args.cost_bps, "k_forward_days": args.k,
        "nonoverlap_offset": args.offset, "n_boot": args.n,
        "sharpe_bootstrap_block": args.block, "seed": args.seed})

    ic_out = pd.concat([ic_rep.assign(row="level"), ic_diff.assign(row="diff")],
                       ignore_index=True)
    ic_out.to_csv(out_dir / "probe_3_fusion_ic.csv", index=False)
    ret_rep.to_csv(out_dir / "probe_3_fusion_returns.csv", index=False)
    pair_rep.to_csv(out_dir / "probe_3_fusion_paired_bootstrap.csv", index=False)
    div_rep.to_csv(out_dir / "probe_3_fusion_divergence.csv", index=False)
    pd.concat([verdict, meta], ignore_index=True).to_csv(
        out_dir / "probe_3_fusion_verdict.csv", index=False)

    print("因子相关（vs 现任）：" + "  ".join(
        f"{k}={v:.3f}" for k, v in corr.items() if k != INCUMBENT))
    print("\n[闸门 ⓪] 非偏 rank IC（同秤·非重叠 %d 日·blend）" % args.k)
    print(ic_rep.round(4).to_string(index=False))
    print("\n配对 IC 差（挑战者 − 现任）")
    print(ic_diff[["window", "challenger", "diff_ic", "ci_lo", "ci_hi", "p_value",
                   "n_obs"]].round(4).to_string(index=False))
    show = ret_rep[(ret_rep["kou_jing"] == PRIMARY_KOU_JING) & (ret_rep["seg"] == "full")
                   & ret_rep["signal"].str.endswith("_lf")]
    print("\n[收益层] blend · long-flat · full 段（部署口径）")
    print(show[["signal", "window", "sharpe", "ann", "maxdd", "calmar",
                "turnover", "hit", "n_obs"]].round(3).to_string(index=False))
    print("\n[诊断] 融合/替补 vs 现任 Sharpe 差配对 bootstrap（blend · long-flat）")
    print(pair_rep[pair_rep["kou_jing"] == PRIMARY_KOU_JING][
        ["window", "challenger", "diff_sharpe", "ci_lo", "ci_hi", "p_value"]
    ].round(4).to_string(index=False))
    print("\n[诊断] 分歧日仲裁（long-flat 两腿互补）")
    print("  分歧日 %d / %d (%.1f%%)；融合跟现任 %d 天、跟替补 %d 天；"
          "跟随=|·|较大腿 恒等成立=%s"
          % (div_sum["n_days_divergent"], div_sum["n_days_total"],
             100 * div_sum["divergent_share"], div_sum["follows_incumbent_days"],
             div_sum["follows_slope20_days"], div_sum["follows_equals_larger_abs"]))
    print("  可得收益 %+.1f%%（现任 %+.1f%% + 替补 %+.1f%%）；融合捕获 %+.1f%% "
          "= 可得的 %.1f%%（随机期望 50%% / %+.1f%%，随机 95%% CI [%+.1f%%, %+.1f%%]，"
          "p=%.4f）"
          % (100 * div_sum["available_return"], 100 * div_sum["capture_incumbent"],
             100 * div_sum["capture_slope20"], 100 * div_sum["capture_fused"],
             100 * div_sum["capture_ratio_fused"], 100 * div_sum["random_expectation"],
             100 * div_sum["random_ci_lo"], 100 * div_sum["random_ci_hi"],
             div_sum["random_p_two_sided"]))
    print("  择边正确率 %d/%d = %.1f%%（二项 p=%.4f）；分歧日 |现任| 中位 %.3f / "
          "|替补| 中位 %.3f（全样本 %.3f / %.3f）"
          % (div_sum["correct_choice_days"], div_sum["n_decided"],
             100 * div_sum["correct_choice_rate"], div_sum["correct_binomial_p"],
             div_sum["median_abs_incumbent_divergent"],
             div_sum["median_abs_slope20_divergent"],
             div_sum["median_abs_incumbent_all"], div_sum["median_abs_slope20_all"]))
    print("\n[裁决] ③(f) 闸门逐条")
    print(verdict.round(4).to_string(index=False))
    print("\n[溯源/参数元数据]（同落 verdict CSV）")
    print(meta[["metric", "value"]].to_string(index=False))
    print(f"\n→ {out_dir}/probe_3_fusion_"
          f"{{ic,returns,paired_bootstrap,divergence,verdict}}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
