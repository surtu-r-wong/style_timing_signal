"""关 0 批量重分析（设计 docs/plans/2026-09-09-gate0-reanalysis-design.md，处置规则跑前冻结）。

三个版本共用同一份置换索引矩阵：
  V0  全窗 |IC|、argmax            —— 复算原关 0（max-T 须与登记一致）+ min-P
  V1  三窗同号的最差半窗 |IC|、argmax —— 实际代表规则进零分布（修错配）
  V3  全窗 |偏 IC（控现役）|、argmax   —— 增量版
CLI: python3 -m backtest.gate0_reanalysis --line basis_term [--n-perm 1000]   /  --merge
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

from backtest.rotation_probe import HALVES, _load_ew_signal, _nonoverlap_frame, _win, nonoverlap_ic, partial_rank_ic  # noqa: E402
from backtest.selection_permutation import adjusted_pvalue, build_index_matrix, make_stat_fn, selection_permutation_test  # noqa: E402

OUT_DIR = ROOT / "backtest" / "output" / "gate0_reanalysis"
ALPHA = 0.05


# ---------------------------------------------------------------- 纯函数（统计量）
def consistent_worst_half(ic_full: float, ic_h1: float, ic_h2: float) -> float:
    """pick_representative 的选择统计量：三窗同号 → min(|h1|, |h2|)；否则 −inf（该行不可选）。"""
    vals = (ic_full, ic_h1, ic_h2)
    if any(not np.isfinite(v) for v in vals):
        return -np.inf
    s = np.sign(ic_full)
    if s == 0 or np.sign(ic_h1) != s or np.sign(ic_h2) != s:
        return -np.inf
    return float(min(abs(ic_h1), abs(ic_h2)))


def flip_status(v1_minp: float, v3_minp: float, alpha: float = ALPHA) -> str:
    """设计 §4：V1 min-P<α = 翻转；再 ∧ V3 min-P<α = 复制候选；翻转但 V3≥α = 有形态无增量。"""
    f1 = np.isfinite(v1_minp) and v1_minp < alpha
    f3 = np.isfinite(v3_minp) and v3_minp < alpha
    if f1 and f3:
        return "replication_candidate"
    if f1:
        return "pattern_no_increment"
    return "no_flip"


# ---------------------------------------------------------------- 核心
def analyze(sigs: dict[str, dict[str, pd.Series]], families, grid_k, halves: dict, ret: pd.Series, ew: pd.Series,
            reps: dict[str, tuple[str, int]], n_perm: int = 1000, seed: int = 0) -> dict:
    idx = ret.index
    for fam in families:
        for s in sigs[fam].values():
            idx = idx.intersection(s.dropna().index)
    idx = idx.sort_values(); r = ret.reindex(idx); c = ew.reindex(idx)
    arrays = {form: sigs[fam][form].reindex(idx).to_numpy(dtype=float) for fam in families for form in sigs[fam]}
    variants = [(form, k) for form in arrays for k in grid_k]; signals = {v: arrays[v[0]] for v in variants}
    hv = list(halves.values())
    kmax, n = max(grid_k), len(idx)
    im = build_index_matrix(n, n_perm, scheme="rotation", seed=seed, min_shift=2 * kmax, max_shift=n - 2 * kmax)

    def s0(vals, v):
        ic, _ = nonoverlap_ic(pd.Series(vals, index=idx), r, v[1]); return abs(ic) if np.isfinite(ic) else -np.inf

    def s1(vals, v):
        s = pd.Series(vals, index=idx); k = v[1]
        full, _ = nonoverlap_ic(s, r, k)
        h1, _ = nonoverlap_ic(_win(s, *hv[0]), _win(r, *hv[0]), k); h2, _ = nonoverlap_ic(_win(s, *hv[1]), _win(r, *hv[1]), k)
        return consistent_worst_half(full, h1, h2)

    def s3(vals, v):
        f = _nonoverlap_frame(pd.Series(vals, index=idx), r, v[1])
        pic = partial_rank_ic(f["sig"], f["fwd"], c.reindex(f.index)); return abs(pic) if np.isfinite(pic) else -np.inf

    res = {}
    for name, fn in (("V0", s0), ("V1", s1), ("V3", s3)):
        res[name] = selection_permutation_test(variants, n_obs=n, stat_fn=make_stat_fn(signals, fn), index_matrix=im, statistic_name=name)
    rows = []
    for fam, (form, k) in reps.items():
        i = variants.index((form, int(k)))
        row = {"family": fam, "rep_form": form, "rep_k": int(k)}
        for name in ("V0", "V1", "V3"):
            row[f"{name}_obs"] = float(res[name].observed[i])
            row[f"{name}_maxT"] = adjusted_pvalue(res[name], i, "max_t"); row[f"{name}_minP"] = adjusted_pvalue(res[name], i, "min_p")
        row["status"] = flip_status(row["V1_minP"], row["V3_minP"]); rows.append(row)
    meta = {"n_obs": n, "first": str(idx.min().date()), "last": str(idx.max().date()), "n_variants": len(variants), "n_perm": n_perm, "seed": seed,
            "grid_winner": {name: {"form": res[name].variants[res[name].best_index][0], "k": int(res[name].variants[res[name].best_index][1]),
                                   "obs": float(res[name].observed_best), "p_selected": float(res[name].p_selected), "p_min_p": float(res[name].p_min_p)}
                            for name in res}}
    return {"reps": rows, "meta": meta}


# ---------------------------------------------------------------- 各线装配
def _reps_from_verdicts(path: Path) -> tuple[dict, dict]:
    v = pd.read_csv(path)
    reps = {str(r["family"]): (str(r["best_form"]), int(r["best_k"])) for _, r in v.iterrows()}
    orig = {str(r["family"]): float(r["p_vs_max_null"]) if "p_vs_max_null" in v.columns else float("nan") for _, r in v.iterrows()}
    return reps, orig


def line_basis_term():
    from backtest.basis_term import build_series
    from backtest.basis_term_probe import FAMILIES, GRID_K, build_signals
    reps, orig = _reps_from_verdicts(ROOT / "backtest/output/basis_term_probe_verdicts.csv")
    return build_signals(build_series()), FAMILIES, GRID_K, dict(HALVES), reps, orig


def line_consensus():
    from backtest.consensus_axis_probe import FAMILIES, GRID_K, HALVES as H, build_signals
    from backtest.consensus_revision import build_series
    reps, orig = _reps_from_verdicts(ROOT / "backtest/output/consensus_axis_probe_verdicts.csv")
    return build_signals(build_series()), FAMILIES, GRID_K, dict(H), reps, orig


def line_money_flow():
    from backtest.money_flow_axis_probe import FAMILIES, GRID_K, HALVES as H, build_signals
    from backtest.money_flow_series import build_series
    reps, orig = _reps_from_verdicts(ROOT / "backtest/output/money_flow_axis_probe_verdicts.csv")
    return build_signals(build_series()), FAMILIES, GRID_K, dict(H), reps, orig


def line_new_high():
    from backtest.new_high_axis_probe import FAMILIES, GRID_K, build_signals
    from backtest.new_high_breadth import build_series
    reps, orig = _reps_from_verdicts(ROOT / "backtest/output/new_high_axis_probe_verdicts.csv")
    return build_signals(build_series()), FAMILIES, GRID_K, dict(HALVES), reps, orig


def line_option():
    from backtest.option_axis_probe import FAMILIES_MAIN, GRID_K, HALVES_OPTION, build_option_signals
    io = pd.read_csv(ROOT / "backtest/output/option_iv_IO.csv", parse_dates=["date"]).set_index("date")
    v = pd.read_csv(ROOT / "backtest/output/option_axis_probe_verdicts.csv"); v = v[v["family"].isin(FAMILIES_MAIN)]
    reps = {str(r["family"]): (str(r["best_form"]), int(r["best_k"])) for _, r in v.iterrows()}
    sel = json.loads((ROOT / "backtest/output/option_axis_selection.json").read_text())
    orig = {fam: float("nan") for fam in reps}
    orig["O2"] = float(sel["representative"]["p_vs_max_null"])   # 原关 0 只算了 O2 代表
    sigs = build_option_signals(io); sigs = {f: sigs[f] for f in FAMILIES_MAIN}
    return sigs, FAMILIES_MAIN, GRID_K, dict(HALVES_OPTION), reps, orig


def line_incumbent_ew():
    import backtest.divergence_probe as dp
    from backtest.gate0_incumbent_audit import GRID_K, INCUMBENT_FORM, INCUMBENT_K, build_grid
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import load_pair_configs
    prices = load_pg_closes(dp.INDEX_NAMES, start="2014-01-01")
    forms = build_grid(prices, load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv"))
    sigs = {"EW": forms}
    audit = json.loads((ROOT / "backtest/output/gate0_incumbent_audit.json").read_text())
    orig = {"EW": float(audit["gate0"]["p_selected"])}   # 在位者审计：现役点 vs 网格最大值零分布
    return sigs, ("EW",), GRID_K, dict(HALVES), {"EW": (INCUMBENT_FORM, INCUMBENT_K)}, orig


LINES = {"basis_term": line_basis_term, "consensus": line_consensus, "money_flow": line_money_flow,
         "new_high": line_new_high, "option": line_option, "incumbent_ew": line_incumbent_ew}


def run_line(name: str, n_perm: int) -> dict:
    from backtest.data import load_underlying_returns
    sigs, fams, grid_k, halves, reps, orig = LINES[name]()
    out = analyze(sigs, fams, grid_k, halves, load_underlying_returns("blend"), _load_ew_signal(), reps, n_perm)
    for row in out["reps"]:
        row["orig_gate0_p"] = orig.get(row["family"], float("nan")); row["line"] = name
    out["line"] = name
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float))
    return out


def merge() -> pd.DataFrame:
    rows = []
    for p in sorted(OUT_DIR.glob("*.json")):
        rows += json.loads(p.read_text())["reps"]
    df = pd.DataFrame(rows)
    cols = ["line", "family", "rep_form", "rep_k", "orig_gate0_p", "V0_maxT", "V0_minP", "V1_obs", "V1_maxT", "V1_minP", "V3_obs", "V3_maxT", "V3_minP", "status"]
    df = df[cols]; df.to_csv(ROOT / "backtest/output/gate0_reanalysis_summary.csv", index=False)
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--line", choices=list(LINES)); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--merge", action="store_true"); a = ap.parse_args(argv)
    if a.merge:
        pd.set_option("display.width", 300); print(merge().round(4).to_string(index=False)); return 0
    out = run_line(a.line, a.n_perm); pd.set_option("display.width", 300)
    print(pd.DataFrame(out["reps"]).round(4).to_string(index=False)); print(json.dumps(out["meta"], ensure_ascii=False, default=float)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
