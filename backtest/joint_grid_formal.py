"""联合网格（参数 × 阈值）的选优校正检验 —— 量"1160 点取 max"的真实上偏。

## 命题

`2026-08-18-threshold-by-underlying.md` §8 已量过 **θ 单维**的上偏（29 点膨胀 2.36×、
203 点 3.82×），并明写"该数不可搬到 lb/zw/sm 维"。本模块补上**联合网格**这一格：
每个标的 40 组参数 × 29 个 θ = **1160 点**，问两件事：

1. 各标的"优化后"的增益，**扣掉选优上偏后还剩多少**（`p_selected`）；
2. **纯噪声下**在同规模网格上取 max 能挖到多少（`null_selected` 的分布）——
   这才是"上偏"的直接读数，比 p 值更直观。

## 口径（与前两轮逐字一致，便于横向引用）

零 carry、`cost_bps=3.0`、long-flat；统计量 = `worst_tv(变体) − worst_tv(现役点)`，
**同一置换下同一标的内配对**；现役点 = `lb20/zw40/sm5, θ=0`。
置换 `scheme="rotation"`、`min_shift=60`、`max_shift=n−60`（同 ⑧ 正式跑）。

## 速度

热路径走 `backtest/perm_kernel`（`run_strategy`+`sharpe` 的批量向量化等价物，
由 `tests/test_bt_perm_kernel.py` 逐点钉死，容差 1e-12）。逐次 pandas 调用要 ~3.5 h/标的，
批量后是分钟级 —— 差别全在调用开销，不在算法。

## 用法

    python3 -m backtest.joint_grid_formal                      # 三标的，n_perm=1000
    python3 -m backtest.joint_grid_formal --n-perm 50          # 快速自检
    python3 -m backtest.joint_grid_formal --targets micro      # 只跑微盘
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS  # noqa: E402
from backtest.perm_kernel import worst_tv_batch  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.scan import default_grid, equal_weight_factor_fn  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from backtest.underlying_probe import load_index_returns  # noqa: E402

MIN_SHIFT = 60
STAT_WINDOWS = ("2014-2020", "2021-2023")
COST_BPS = 3.0
INCUMBENT = {"lookback": 20, "z_window": 40, "smoothing": 5, "theta": 0.0}
THETAS = tuple(round(float(x), 2) for x in np.arange(-0.70, 0.701, 0.05))
TARGETS = {"500": ("000905.SH", "中证500"), "1000": ("000852.SH", "中证1000"),
           "micro": ("8841431.WI", "万得微盘")}
OUT = ROOT / "backtest" / "output" / "joint_grid_verdict.json"


class Grid:
    """1160 个 (参数, θ) 的仓位矩阵 + 各标的收益 + 窗口掩码，全部按同一条索引。"""

    def __init__(self):
        raw = equal_weight_factor_fn()

        @lru_cache(maxsize=None)
        def factor(lb, zw, sm):
            return raw(lookback=lb, z_window=zw, smoothing=sm)

        combos = default_grid()
        f0 = factor(combos[0]["lookback"], combos[0]["z_window"],
                    combos[0]["smoothing"])
        self.idx = f0.index
        self.variants, rows = [], []
        for c in combos:
            f = factor(c["lookback"], c["z_window"], c["smoothing"]).reindex(self.idx)
            for th in THETAS:
                self.variants.append((c["lookback"], c["z_window"], c["smoothing"], th))
                rows.append(production_position(f, threshold=th)
                            .astype(float).to_numpy())
        self.pos = np.vstack(rows)                       # (1160, n)
        self.inc_row = self.variants.index(
            (INCUMBENT["lookback"], INCUMBENT["z_window"],
             INCUMBENT["smoothing"], INCUMBENT["theta"]))
        self.masks = [((self.idx >= pd.Timestamp(WINDOWS[w][0]))
                       & (self.idx <= pd.Timestamp(WINDOWS[w][1])))
                      for w in STAT_WINDOWS]

    def align(self, code: str) -> np.ndarray:
        return load_index_returns(code).reindex(self.idx).to_numpy(dtype=float)


def make_batch_stat_fn(g: Grid, und: np.ndarray):
    """`batch_stat_fn(variant, idx_matrix) -> (m,)`，配对减去现役点（同 idx）。"""
    cache: dict[int, np.ndarray] = {}
    row_of = {v: i for i, v in enumerate(g.variants)}

    def base(idx_matrix: np.ndarray) -> np.ndarray:
        key = id(idx_matrix)
        if key not in cache:
            cache[key] = worst_tv_batch(g.pos[g.inc_row][idx_matrix], und,
                                        g.masks, COST_BPS)
        return cache[key]

    def fn(variant, idx_matrix):
        idx_matrix = np.asarray(idx_matrix)
        cur = worst_tv_batch(g.pos[row_of[variant]][idx_matrix], und,
                             g.masks, COST_BPS)
        return cur - base(idx_matrix)

    return fn


def run_one(g: Grid, key: str, n_perm: int, seed: int) -> dict:
    code, name = TARGETS[key]
    und = g.align(code)
    n = len(g.idx)
    t0 = time.time()
    res = selection_permutation_test(
        g.variants, n_obs=n, batch_stat_fn=make_batch_stat_fn(g, und),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=MIN_SHIFT, max_shift=n - MIN_SHIFT,
        statistic_name="worst_tv_gain_vs_incumbent",
        meta={"underlying": f"{key}｜{name}", "cost_bps": COST_BPS, "carry": "none"})
    lb, zw, sm, th = res.variants[res.best_index]
    ns = np.asarray(res.null_selected)
    out = {
        "underlying": f"{key}｜{name}", "n_variants": len(g.variants),
        "n_perm": n_perm, "elapsed_s": round(time.time() - t0, 1),
        "winner": f"lb{lb}/zw{zw}/sm{sm}/θ{th:+.2f}",
        "observed_gain": float(res.observed_best),
        "p_selected": float(res.p_selected), "p_naive": float(res.p_naive),
        "p_min_p": float(res.p_min_p),
        "selection_inflation": float(res.selection_inflation),
        "null_max_gain_p50": float(np.percentile(ns, 50)),
        "null_max_gain_p90": float(np.percentile(ns, 90)),
        "null_max_gain_mean": float(ns.mean()),
        "pass_gate": bool(res.p_selected < 0.05),
    }
    print(f"\n── {out['underlying']}：{out['n_variants']} 点，n_perm={n_perm}"
          f"（{out['elapsed_s']} s）──")
    print(f"  观测赢家   = {out['winner']}   观测增益 = {out['observed_gain']:+.4f}")
    print(f"  p_selected = {out['p_selected']:.4f}   ← 判据（<0.05 才算有东西）")
    print(f"  p_naive    = {out['p_naive']:.4f}（对照·不入闸）   "
          f"选择膨胀 = {out['selection_inflation']:.2f}×")
    print(f"  ⭐纯噪声下 1160 点取 max 的增益：中位 "
          f"{out['null_max_gain_p50']:+.4f}   均值 {out['null_max_gain_mean']:+.4f}"
          f"   90% 分位 {out['null_max_gain_p90']:+.4f}")
    print(f"  → {'过闸' if out['pass_gate'] else '不过闸（与噪声不可区分）'}")
    return out


def dump_grid(g: Grid, key: str) -> Path:
    """把某标的的 1160 点**描述性**读数落盘（供人工翻看；不是选优产物）。

    描述性指标走 pandas 原路 `baseline.evaluate`（要 ann/maxdd/turnover，
    快核只做 Sharpe），1160 × 4 窗约 1~2 分钟。
    """
    from backtest.baseline import evaluate

    code, name = TARGETS[key]
    und = load_index_returns(code)
    rows = []
    for i, (lb, zw, sm, th) in enumerate(g.variants):
        pos = pd.Series(g.pos[i], index=g.idx)
        r = {"lookback": lb, "z_window": zw, "smoothing": sm, "theta": th,
             "exposure": float(g.pos[i].mean()),
             "is_incumbent": int(i == g.inc_row)}
        for win, (s, e) in WINDOWS.items():
            p_, u_ = pos, und
            if s:
                p_, u_ = p_[p_.index >= pd.Timestamp(s)], u_[u_.index >= pd.Timestamp(s)]
            if e:
                p_, u_ = p_[p_.index <= pd.Timestamp(e)], u_[u_.index <= pd.Timestamp(e)]
            if len(p_.index.intersection(u_.index)) < 60:
                continue
            m = evaluate(p_, u_, None, COST_BPS, 0)["long"]
            r[f"sharpe_{win}"] = m["sharpe"]
            if win == "full":
                r["ann_full"], r["maxdd_full"] = m["ann"], m["maxdd"]
                r["turnover_full"] = m["turnover"]
        rows.append(r)
    d = pd.DataFrame(rows)
    d["worst_tv"] = d[[f"sharpe_{w}" for w in STAT_WINDOWS]].min(axis=1)
    d = d.sort_values("worst_tv", ascending=False).reset_index(drop=True)
    d.insert(0, "rank_worst_tv", d.index + 1)
    out = ROOT / "backtest" / "output" / f"joint_grid_{key}.csv"
    d.to_csv(out, index=False)
    print(f"  描述性网格 {len(d)} 行 → {out}")
    return out


def export_signal(g: Grid, key: str, variant: tuple | None = None) -> Path:
    """把某标的**最优点**的信号时间序列落盘：因子全值 + 套阈值后的 0/1 仓位。

    列：`date, factor_value_opt, position_opt, factor_value_incumbent,
    position_incumbent, index_close, index_ret`。
    - `factor_value_opt` = 最优参数下的因子**连续值**（该点 smoothing=0 时即未平滑值）；
    - `position_opt` = `factor_value_opt > θ_opt` 的 0/1，**与回测逐日一致**
      （回测再做 T+1 生效，本文件不预先 shift —— 想看生效仓位自行 shift(1)）；
    - 现役两列并列，便于逐日对照。
    """
    from backtest.data import _connect, load_db_config

    if variant is None:
        variant = g.variants[int(np.argmax(
            worst_tv_batch(g.pos, g.align(TARGETS[key][0]), g.masks, COST_BPS)))]
    lb, zw, sm, th = variant
    raw = equal_weight_factor_fn()
    f_opt = raw(lookback=lb, z_window=zw, smoothing=sm).reindex(g.idx)
    f_inc = raw(lookback=INCUMBENT["lookback"], z_window=INCUMBENT["z_window"],
                smoothing=INCUMBENT["smoothing"]).reindex(g.idx)

    code = TARGETS[key][0]
    db = load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT trade_date, close FROM {db['schema']}.index_daily "
                        "WHERE index_code=%s ORDER BY trade_date", (code,))
            close = pd.Series({pd.Timestamp(d): float(c) for d, c in cur.fetchall()})
    finally:
        conn.close()

    out = pd.DataFrame({
        "factor_value_opt": f_opt,
        "position_opt": production_position(f_opt, threshold=th).astype(int),
        "factor_value_incumbent": f_inc,
        "position_incumbent": production_position(
            f_inc, threshold=INCUMBENT["theta"]).astype(int),
        "index_close": close.reindex(g.idx),
    })
    out["index_ret"] = out["index_close"].pct_change()
    out.index.name = "date"
    path = ROOT / "backtest" / "output" / f"signal_{key}_opt.csv"
    out.to_csv(path)
    print(f"  最优点 = lb{lb}/zw{zw}/sm{sm}/θ{th:+.2f}（{code}）")
    print(f"  在场比例 {out['position_opt'].mean()*100:.1f}%（现役 "
          f"{out['position_incumbent'].mean()*100:.1f}%）；两者逐日不同的天数 "
          f"{int((out['position_opt'] != out['position_incumbent']).sum())} / {len(out)}")
    print(f"  信号序列 {len(out)} 行 → {path}")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="联合网格（参数×阈值）选优校正检验")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--targets", default="micro,1000,500",
                    help="逗号分隔，默认 micro,1000,500（微盘优先）")
    ap.add_argument("--dump-grid", default="",
                    help="额外落盘该标的的 1160 点描述性网格（如 --dump-grid micro）")
    ap.add_argument("--export-signal", default="",
                    help="落盘该标的最优点的信号序列（因子全值 + 阈值后 0/1）")
    args = ap.parse_args(argv)

    t0 = time.time()
    g = Grid()
    print(f"网格 {len(g.variants)} 点（{len(default_grid())} 参数组 × {len(THETAS)} θ），"
          f"样本 {len(g.idx)} 日，现役点行号 {g.inc_row}（建网格 {time.time()-t0:.1f} s）")

    if args.dump_grid.strip() in TARGETS:
        dump_grid(g, args.dump_grid.strip())
    if args.export_signal.strip() in TARGETS:
        export_signal(g, args.export_signal.strip())

    results = [run_one(g, k.strip(), args.n_perm, args.seed)
               for k in args.targets.split(",") if k.strip() in TARGETS]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n总耗时 {time.time()-t0:.1f} s → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
