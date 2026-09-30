"""ERP 轴探针：股债性价比（面7 ⭐，initiative 最后一个可测轴）。

ERP = 1/PE_TTM(万得全A, M0330161) − 10Y YTM(货币网 M1002626)/100，
两腿均在 stock_selector.edb_daily（PE 2026-07-09 用户给码回填 2005 起；
10Y 2010-01 起 → 有效样本 2010+）。设计稿语义：长周期性价比锚，极值分位处
均值回归强 → 多（仓位维度）。当日收盘即知，无 pit_lag。

族：E1 水平 / E2 20d 变化，lb{5,20}×zw{60,250}；持有 k∈{5,10,20,40}
（慢频锚语义）。双侧三关（run_families_probe 原样，控 equal_weight）。

CLI: python3 -m backtest.erp_probe [--n-perm 1000]
产出: backtest/output/erp_probe{,_verdicts}.csv。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.leverage_probe import (  # noqa: E402
    GRID_LEVEL,
    level_signal,
    run_families_probe,
)
from backtest.rotation_probe import HALVES  # noqa: E402

GRID_K_ERP = (5, 10, 20, 40)
_PE_CODE = "M0330161"    # 万得全A 滚动市盈率（主）；M0330156 静态留库备用
_Y10_CODE = "M1002626"   # 中债10Y YTM（货币网，主源）


# ---------------------------------------------------------------- 纯函数
def build_erp(pe: pd.Series, y10_pct: pd.Series) -> pd.Series:
    """ERP = 1/PE − 10Y/100（10Y 为百分点口径，对齐成比例）；PE≤0 剔除。"""
    pe = pe[pe > 0]
    joint = pd.concat([pe.rename("pe"), y10_pct.rename("y10")], axis=1,
                      join="inner").dropna()
    return (1.0 / joint["pe"] - joint["y10"] / 100.0).rename("erp")


def build_erp_signals(pe: pd.Series, y10_pct: pd.Series
                      ) -> dict[str, dict[str, pd.Series]]:
    """两族×网格装配：E1=ERP 水平 / E2=20d 变化。无 pit_lag。"""
    erp = build_erp(pe, y10_pct)
    src = {"E1": erp, "E2": erp.diff(20)}
    return {
        fam: {f"{fam}_lb{lb}zw{zw}": level_signal(series.dropna(), lb, zw)
              for lb, zw in GRID_LEVEL}
        for fam, series in src.items()
    }


# ---------------------------------------------------------------- 数据装载
def _load_edb_series(code: str, db=None) -> pd.Series:
    from signals.common.config import load_db_config
    from backtest.data import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT trade_date, value FROM {db['schema']}.edb_daily
                    WHERE edb_code = %s ORDER BY trade_date""", (code,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.Series({pd.Timestamp(d): float(v) for d, v in rows}).sort_index()


# ---------------------------------------------------------------- 编排
def run_probe(families: tuple[str, ...] = ("E1", "E2"), n_perm: int = 1000,
              cost_bps: float = 3.0, db=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    pe = _load_edb_series(_PE_CODE, db)
    y10 = _load_edb_series(_Y10_CODE, db)
    sigs_all = build_erp_signals(pe, y10)
    return run_families_probe(sigs_all, families, GRID_K_ERP, n_perm, cost_bps, db)


def main() -> int:
    ap = argparse.ArgumentParser(description="ERP 轴探针（股债性价比，两族×三关）")
    ap.add_argument("--families", default="E1,E2")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    panel, verdicts = run_probe(families, args.n_perm, args.cost_bps)

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out_dir / "erp_probe.csv", index=False)
    verdicts.to_csv(out_dir / "erp_probe_verdicts.csv", index=False)

    show = panel[panel["kou_jing"] == "blend"].copy()
    for c in ["ic"] + [f"ic_{h}" for h in HALVES]:
        show[c] = show[c].round(3)
    print("=== IC 面板（blend 口径） ===")
    print(show.drop(columns=["kou_jing"]).to_string(index=False))
    print("\n=== 逐族三关裁决 ===")
    for _, v in verdicts.iterrows():
        print(f"\n-- {v['family']} --")
        for key, val in v.items():
            if key == "family":
                continue
            print(f"  {key}: {val:.4f}" if isinstance(val, float) else f"  {key}: {val}")
    n_pass = int(verdicts["PASS"].sum())
    print(f"\n{'★ PASS：' + str(n_pass) + ' 族过闸 → 进多头装配设计（对照 long-flat 1.42）'
          if n_pass else '✗ STOP：全族停线 → ERP 轴归档（第八轴）'}")
    print(f"→ {out_dir / 'erp_probe.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
