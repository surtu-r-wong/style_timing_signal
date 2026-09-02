"""候选 ⑥：隔夜 / 日内分解 —— citic40d 对象探针（预登记：docs/plans/2026-09-02-overnight-intraday-citic40d-prereg.md）。

每条中信风格腿把日收益拆成隔夜段 ln(O_t/C_{t-1}) 与日内段 ln(C_t/O_t)，各自累成合成价格后
**原样复用** `signals.citic40d.generate_signal.compute_mean_factor`（不碰生产代码）。
口径：overnight / intraday / fused（两 tanh 因子固定 50/50）× lb20 × zw{40,120} × sm0 = 6 点。
秤：blend 标的 + carry、3 bps、T+1；三窗 train/val/holdout；⓪ 非重叠 rank IC 头对头（fusion_probe 机器）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.fusion_probe import (  # noqa: E402
    GATE_WORST_TV_LIFT, forward_return, fuse_equal, nonoverlap_grid, paired_ic_bootstrap, rank_ic,
)
from backtest.positions import production_position, to_position  # noqa: E402
from backtest.scan import _slice, scan_grid  # noqa: E402
from signals.citic40d.generate_signal import compute_mean_factor  # noqa: E402

STYLE_NAMES = ["稳定", "成长", "金融", "周期", "消费"]
RENAME = {"稳定": "stability", "成长": "growth", "金融": "finance", "周期": "cycle", "消费": "consumption"}
KINDS = ("overnight", "intraday", "fused")
LOOKBACK, SMOOTHING, Z_WINDOWS = 20, 0, (40, 120)
INCUMBENT = "incumbent_citic20z40"
TRAIN, VAL, HOLDOUT = "2014-2020", "2021-2023", "2024-2026"
WINDOWS = {TRAIN: ("2014-01-01", "2020-12-31"), VAL: ("2021-01-01", "2023-12-31"), HOLDOUT: ("2024-01-01", "2026-12-31")}
SELECT_WINDOWS = (TRAIN, VAL)
OUT_DIR = ROOT / "backtest" / "output" / "overnight_probe"
COMMITTED = ROOT / "output" / "citic40d" / "citic_style_signal_40d.csv"


# ---------------- 数据 ----------------
def load_pg_ohlc(names: list[str], start=None, db=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(open 宽表, close 宽表)，列为中文名，索引升序；尾部参差行按 close 口径裁掉。"""
    import psycopg2
    from psycopg2 import sql
    from signals.common.config import load_db_config
    from signals.common.data_source import load_code_map

    code_map = load_code_map()
    codes = [code_map[n] for n in names]
    db = db or load_db_config()
    conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                            user=db["user"], password=db["password"], connect_timeout=10)
    try:
        with conn.cursor() as cur:
            q = sql.SQL("""SELECT index_code, trade_date, open, close FROM {schema}.index_daily
                           WHERE index_code = ANY(%s) AND (%s::date IS NULL OR trade_date >= %s::date)""").format(
                schema=sql.Identifier(db["schema"]))
            cur.execute(q, (codes, start, start))
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["code", "date", "open", "close"])
    df["date"] = pd.to_datetime(df["date"])
    df["name"] = df["code"].map(dict(zip(codes, names)))
    o = df.pivot(index="date", columns="name", values="open").astype(float).sort_index()[names]
    c = df.pivot(index="date", columns="name", values="close").astype(float).sort_index()[names]
    # 与 load_pg_closes(trim_ragged_tail=True) 同款：最后一行若有任一腿缺 close 则裁掉
    if c.iloc[-1].isna().any():
        o, c = o.iloc[:-1], c.iloc[:-1]
    return o, c


# ---------------- 纯函数：分解与合成价格 ----------------
def decompose_log_returns(open_: pd.DataFrame, close: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """隔夜 ln(O_t/C_{t-1})、日内 ln(C_t/O_t)；首行（无 C_{t-1}）两段均记 0，使合成价格从 1 起算。
    平 K 线日（O==C）按原始数据：隔夜吸收全日收益、日内为 0（预登记 §1 冻结规则）。
    """
    o = open_.astype(float)
    c = close.astype(float)
    on = np.log(o / c.shift(1))
    intra = np.log(c / o)
    on.iloc[0] = 0.0
    intra.iloc[0] = 0.0
    return on, intra


def synthetic_price(logret: pd.DataFrame) -> pd.DataFrame:
    return np.exp(logret.cumsum())


def component_factor(open_: pd.DataFrame, close: pd.DataFrame, kind: str,
                     lookback: int = LOOKBACK, z_window: int = 40, smoothing: int = SMOOTHING) -> pd.Series:
    """kind ∈ {full, overnight, intraday, fused}。full 走 close 原路径（自检用）。"""
    if kind == "full":
        return compute_mean_factor(close.rename(columns=RENAME), n=lookback, z_window=z_window, smoothing=smoothing)
    on, intra = decompose_log_returns(open_, close)
    if kind == "fused":
        a = component_factor(open_, close, "overnight", lookback, z_window, smoothing)
        b = component_factor(open_, close, "intraday", lookback, z_window, smoothing)
        return fuse_equal(a, b)
    comp = on if kind == "overnight" else intra
    style = synthetic_price(comp).rename(columns=RENAME)
    return compute_mean_factor(style, n=lookback, z_window=z_window, smoothing=smoothing)


def reproduce_check(full: pd.Series, committed_csv: Path = COMMITTED) -> dict:
    """前置自检：全日因子须与 committed factor_20 在共同日期 round(4) 逐位一致。"""
    com = pd.read_csv(committed_csv, parse_dates=["date"]).set_index("date")["factor_20"]
    mine = full.dropna().round(4)
    common = mine.index.intersection(com.index)
    diff = (mine.reindex(common) - com.reindex(common)).abs()
    return {"n_common": int(len(common)), "n_mismatch": int((diff > 0).sum()), "max_abs_diff": float(diff.max())}


def build_candidates(open_: pd.DataFrame, close: pd.DataFrame) -> dict[str, pd.Series]:
    cands = {INCUMBENT: component_factor(open_, close, "full", LOOKBACK, 40, SMOOTHING)}
    for kind in KINDS:
        for zw in Z_WINDOWS:
            cands[f"{kind}_lb{LOOKBACK}_zw{zw}"] = component_factor(open_, close, kind, LOOKBACK, zw, SMOOTHING)
    return cands


# ---------------- 闸门 ----------------
def ic_head2head(cands: dict[str, pd.Series], und: pd.Series, k: int = 20, offset: int = 0,
                 n_boot: int = 10000, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    fwd = forward_return(und, k).dropna()
    common = fwd.index
    for f in cands.values():
        common = common.intersection(f.dropna().index)
    common = common.sort_values()
    ic_rows, diff_rows = [], []
    for win, (s, e) in WINDOWS.items():
        idx_w = _slice(pd.Series(0.0, index=common), s, e).index
        grid = nonoverlap_grid(idx_w, k, offset)
        if len(grid) < 10:
            continue
        y = fwd.reindex(grid)
        cols = {name: f.reindex(grid) for name, f in cands.items()}
        for name, x in cols.items():
            ic_rows.append({"window": win, "factor": name, "k": k, "offset": offset, **rank_ic(x, y)})
        for name in cands:
            if name == INCUMBENT:
                continue
            boot = paired_ic_bootstrap(cols[name], cols[INCUMBENT], y, n=n_boot, seed=seed)
            diff_rows.append({"window": win, "challenger": name, "reference": INCUMBENT, "k": k, **boot})
    return pd.DataFrame(ic_rows), pd.DataFrame(diff_rows)


def evaluate(cands: dict[str, pd.Series], ic_rep: pd.DataFrame, ic_diff: pd.DataFrame,
             scan_sym: pd.DataFrame, ret_rep: pd.DataFrame) -> pd.DataFrame:
    """按预登记 §5 逐点判 ⓪①②③。ret_rep 为 baseline.build_report 的输出（name 形如 <cand>_lf）。"""
    def ic_of(f, w):
        h = ic_rep[(ic_rep["factor"] == f) & (ic_rep["window"] == w)]["ic"]
        return float(h.iloc[0]) if len(h) else float("nan")

    def sig_of(f, w):
        h = ic_diff[(ic_diff["challenger"] == f) & (ic_diff["window"] == w)]["ci_excludes_zero"]
        return bool(h.iloc[0]) if len(h) else False

    def sym(f, w):
        h = scan_sym[scan_sym["candidate"] == f][f"sharpe_{w}"]
        return float(h.iloc[0]) if len(h) else float("nan")

    def lf(f, kj, w):
        h = ret_rep[(ret_rep["signal"] == f"{f}_lf") & (ret_rep["kou_jing"] == kj) & (ret_rep["window"] == w)]["sharpe"]
        return float(h.iloc[0]) if len(h) else float("nan")

    rows = []
    for name in cands:
        if name == INCUMBENT:
            continue
        g0 = all(ic_of(name, w) > ic_of(INCUMBENT, w) for w in SELECT_WINDOWS) and any(sig_of(name, w) for w in SELECT_WINDOWS)
        g1 = all(sym(name, w) > 0 for w in WINDOWS) and min(sym(name, w) for w in SELECT_WINDOWS) >= min(sym(INCUMBENT, w) for w in SELECT_WINDOWS)
        g2 = all(lf(name, kj, w) >= lf(INCUMBENT, kj, w) for kj in ("500", "1000", "blend") for w in SELECT_WINDOWS)
        lift = min(lf(name, "blend", w) for w in SELECT_WINDOWS) - min(lf(INCUMBENT, "blend", w) for w in SELECT_WINDOWS)
        g3 = lift >= GATE_WORST_TV_LIFT
        rows.append({"candidate": name,
                     "ic_train": ic_of(name, TRAIN), "ic_val": ic_of(name, VAL), "ic_holdout": ic_of(name, HOLDOUT),
                     "ic_inc_train": ic_of(INCUMBENT, TRAIN), "ic_inc_val": ic_of(INCUMBENT, VAL),
                     "sym_train": sym(name, TRAIN), "sym_val": sym(name, VAL), "sym_holdout": sym(name, HOLDOUT),
                     "sym_inc_worst_tv": min(sym(INCUMBENT, w) for w in SELECT_WINDOWS),
                     "lf_blend_train": lf(name, "blend", TRAIN), "lf_blend_val": lf(name, "blend", VAL),
                     "lf_blend_holdout": lf(name, "blend", HOLDOUT), "lf_worst_tv_lift": lift,
                     "gate0_rank_ic": g0, "gate1_sym_positive_worst": g1, "gate2_lf_not_worse_all_kj": g2,
                     "gate3_lift_ge_0.10": g3, "pass": bool(g0 and g1 and g2 and g3)})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    import argparse
    from backtest.baseline import build_report
    from backtest.data import load_carry, load_underlying_returns

    ap = argparse.ArgumentParser(description="候选⑥ 隔夜/日内分解 citic40d 探针（预登记 2026-09-02）")
    ap.add_argument("--check-only", action="store_true", help="只跑前置自检")
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--baseline-bootstrap", type=int, default=500)
    a = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    o, c = load_pg_ohlc(STYLE_NAMES)
    chk = reproduce_check(component_factor(o, c, "full"))
    print("reproduce_check:", chk)
    if chk["n_mismatch"] != 0 or chk["n_common"] < 3000:
        print("ABORT: 全日因子未能逐位复现 committed citic40d 信号", file=sys.stderr)
        return 2
    if a.check_only:
        return 0
    flat = int(((o == c) & (o.notna())).all(axis=1).loc["2014-01-01":].sum())
    cands = build_candidates(o, c)
    und, car = load_underlying_returns("blend"), load_carry("blend")
    # ① 对称口径三窗（与 scan.py 同秤）
    sym_rows = []
    for name, fac in cands.items():
        rep = scan_grid(lambda **_: fac, [{"candidate": name}], und, car, WINDOWS)
        sym_rows.append(rep.iloc[0])
    scan_sym = pd.DataFrame(sym_rows).reset_index(drop=True)
    scan_sym.to_csv(OUT_DIR / "scan_symmetric.csv", index=False)
    # ⓪ rank IC 头对头
    ic_rep, ic_diff = ic_head2head(cands, und, n_boot=a.bootstrap)
    ic_rep.to_csv(OUT_DIR / "ic_report.csv", index=False)
    ic_diff.to_csv(OUT_DIR / "ic_paired_diff.csv", index=False)
    # ②③ long-flat 全口径（baseline 同秤，含 bootstrap 列）
    positions = {}
    for name, fac in cands.items():
        positions[f"{name}_lf"] = production_position(fac)
        positions[f"{name}_sym"] = to_position(fac, mode="discrete")
    ret_rep = build_report(bootstrap_n=a.baseline_bootstrap, positions=positions)
    ret_rep.to_csv(OUT_DIR / "baseline_report.csv", index=False)
    gates = evaluate(cands, ic_rep, ic_diff, scan_sym, ret_rep)
    gates.to_csv(OUT_DIR / "gates.csv", index=False)
    corr = {n: float(f.corr(cands[INCUMBENT])) for n, f in cands.items() if n != INCUMBENT}
    verdict = {"prereg": "docs/plans/2026-09-02-overnight-intraday-citic40d-prereg.md",
               "reproduce_check": chk, "flat_bar_days_in_eval_window": flat,
               "n_candidates": int(len(gates)), "n_pass": int(gates["pass"].sum()),
               "verdict": "GO_candidate" if gates["pass"].any() else "STOP",
               "corr_vs_incumbent": corr,
               "data_end": str(c.index.max().date()), "bootstrap_n": a.bootstrap}
    (OUT_DIR / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1))
    pd.set_option("display.width", 250)
    print(gates.round(4).to_string(index=False))
    print(json.dumps(verdict, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
