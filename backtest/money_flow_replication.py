"""资金流向面 F1·横截面复制探针（预登记 docs/plans/2026-09-09-money-flow-replication-prereg.md）。

P1  科创板腿 e_000680.SH → level_signal(lb5, zw250) → blend，k=20，方向 +1，单侧循环移位置换 p，闸。
描述性（只报不闸）：偏 IC（控现役）、科创腿 → 300/2000/全指、原线冻结 F1 信号 → 300/2000/全指、两半窗、逐年符号。
CLI: python3 -m backtest.money_flow_replication --run [--n-perm 1000]
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

from backtest.basis_term_replication import one_sided_shift_pvalue, yearly_ic  # noqa: E402
from backtest.leverage_probe import level_signal  # noqa: E402
from backtest.rotation_probe import _load_ew_signal, _win, nonoverlap_ic, partial_ic_with_pvalue  # noqa: E402

LB, ZW, K, DIRECTION, ALPHA = 5, 250, 20, +1, 0.05
STAR = "e_000680.SH"
SERIES = ROOT / "backtest" / "output" / "money_flow_series.csv"
AUX_TARGETS = {"300": "000300.SH", "2000": "932000.CSI", "all": "000985.CSI"}
OUT_DIR = ROOT / "backtest" / "output"


def verdict(p1_one_sided: float, alpha: float = ALPHA) -> dict:
    """预登记 §3：PASS = P1 单侧 p < α。"""
    ok = bool(np.isfinite(p1_one_sided) and p1_one_sided < alpha)
    return {"P1_pass": ok, "PASS": ok}


def eval_start(e: pd.Series, zw: int = ZW) -> pd.Timestamp:
    """评窗起点 = 首个 zw 满窗日（剔 burn-in 的 0 值）。"""
    return e.index[zw - 1]


def _load_index_returns(code: str) -> pd.Series:
    from backtest.data import _connect
    from signals.common.config import load_db_config
    db = load_db_config(); conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT trade_date, close FROM {db['schema']}.index_daily WHERE index_code=%s ORDER BY trade_date", (code,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.Series({pd.Timestamp(d): float(c) for d, c in rows}).sort_index().pct_change().dropna()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000); a = ap.parse_args(argv)
    d = pd.read_csv(SERIES, parse_dates=["trade_date"]).set_index("trade_date")
    e = d[STAR].dropna(); start = eval_start(e)
    if not a.run:
        print(e.describe().round(4).to_string()); print("eval_start", start.date()); return 0
    from backtest.data import load_underlying_returns
    blend = load_underlying_returns("blend"); ew = _load_ew_signal()
    sig = level_signal(e, LB, ZW); sig = sig[sig.index >= start]
    p1 = one_sided_shift_pvalue(sig, blend, K, DIRECTION, a.n_perm)
    pic, pp = partial_ic_with_pvalue(sig, blend, ew, K, a.n_perm)
    mid = sig.index[len(sig) // 2]
    h1 = nonoverlap_ic(sig[sig.index < mid], blend, K); h2 = nonoverlap_ic(sig[sig.index >= mid], blend, K)
    meta = {"lb": LB, "zw": ZW, "k": K, "direction": DIRECTION, "alpha": ALPHA, "n_perm": a.n_perm,
            "P1": {**p1, "first": str(sig.index.min().date()), "last": str(sig.index.max().date()), "partial_ic_vs_ew": pic, "partial_p": pp,
                   "half_split": str(mid.date()), "ic_h1": h1[0], "n_h1": h1[1], "ic_h2": h2[0], "n_h2": h2[1], "yearly_ic": yearly_ic(sig, blend, K)},
            "aux": {}}
    f1sig = level_signal(d["F1"].dropna(), LB, ZW); f1sig = f1sig[f1sig.index >= eval_start(d["F1"].dropna())]
    for name, code in AUX_TARGETS.items():
        r = _load_index_returns(code)
        ic_s, n_s = nonoverlap_ic(sig, r, K); ic_f, n_f = nonoverlap_ic(f1sig, r, K)
        meta["aux"][name] = {"code": code, "star_ic": ic_s, "star_n": n_s, "F1_ic": ic_f, "F1_n": n_f}
    meta["verdict"] = verdict(p1["p_one_sided"])
    rows = [{"test": "P1", "input": "star", "target": "blend", **{k_: v for k_, v in meta["P1"].items() if k_ != "yearly_ic"}}]
    rows += [{"test": "aux", "input": "star", "target": n, "ic": v["star_ic"], "n_windows": v["star_n"]} for n, v in meta["aux"].items()]
    rows += [{"test": "aux", "input": "F1_frozen", "target": n, "ic": v["F1_ic"], "n_windows": v["F1_n"]} for n, v in meta["aux"].items()]
    pd.DataFrame(rows).to_csv(OUT_DIR / "money_flow_replication_panel.csv", index=False)
    (OUT_DIR / "money_flow_replication_verdict.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float))
    pd.set_option("display.width", 260); print(pd.DataFrame(rows).round(4).to_string(index=False)); print(json.dumps(meta["verdict"])); print("yearly:", meta["P1"]["yearly_ic"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
