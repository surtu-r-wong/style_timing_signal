"""股指期权隐波面·入场券探针（预登记 docs/plans/2026-09-03-index-option-iv-axis-prereg.md，冻结后才可 --run）。

五族主测 O1~O5（IO）+ O6 描述（MO−IO IV30 差）；GRID_LEVEL × k∈{5,10,20,40}；两半窗 2020-2022 / 2023-2026；
三关 = leverage_probe.run_families_probe 原样。
CLI: python3 -m backtest.option_axis_probe --build [--force]   # 构建/刷新序列缓存 + 质量报告（不涉及收益）
     python3 -m backtest.option_axis_probe --run [--n-perm 1000] # 正式跑（冻结后）
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

from backtest.leverage_probe import GRID_LEVEL, level_signal, run_families_probe  # noqa: E402
from backtest.option_iv import build_series  # noqa: E402

GRID_K = (5, 10, 20, 40)
HALVES_OPTION = {"2020-2022": ("2020-01-01", "2022-12-31"), "2023-2026": ("2023-01-01", "2026-12-31")}
FAMILIES_MAIN = ("O1", "O2", "O3", "O4", "O5")
FAMILIES_DESC = ("O6",)
OUT_DIR = ROOT / "backtest" / "output"


def build_option_signals(io: pd.DataFrame, mo: pd.DataFrame | None = None) -> dict[str, dict[str, pd.Series]]:
    """五族（+O6）× GRID_LEVEL 装配；输入为 option_iv.build_series 的指标表（date 索引）。当日收盘即知，无 pit_lag。"""
    src = {
        "O1": io["iv30"],
        "O2": io["iv30"].diff(20),
        "O3": io["term"],
        "O4": io["skew"],
        "O5": io["pcr"],
    }
    if mo is not None:
        src["O6"] = (mo["iv30"] - io["iv30"]).dropna()
    return {fam: {f"{fam}_lb{lb}zw{zw}": level_signal(series.dropna(), lb, zw) for lb, zw in GRID_LEVEL}
            for fam, series in src.items()}


def quality_report(io: pd.DataFrame, mo: pd.DataFrame | None) -> dict:
    def stats(f: pd.DataFrame, name: str) -> dict:
        y = f.index.year
        return {"name": name, "days": int(len(f)), "first": str(f.index.min().date()), "last": str(f.index.max().date()),
                "nan_days": {c: int(f[c].isna().sum()) for c in ("iv30", "term", "skew", "pcr")},
                "skew_clipped_days": int(f["skew_clipped"].astype(bool).sum()),
                "iv30_by_year": {int(k): [round(float(v.min()), 3), round(float(v.median()), 3), round(float(v.max()), 3)]
                                 for k, v in f["iv30"].groupby(y) if v.notna().any()},
                "term_median_by_year": {int(k): round(float(v.median()), 4) for k, v in f["term"].groupby(y) if v.notna().any()},
                "skew_median_by_year": {int(k): round(float(v.median()), 4) for k, v in f["skew"].groupby(y) if v.notna().any()},
                "pcr_median_by_year": {int(k): round(float(v.median()), 3) for k, v in f["pcr"].groupby(y) if v.notna().any()}}
    rep = {"IO": stats(io, "IO")}
    if mo is not None:
        rep["MO"] = stats(mo, "MO")
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--run", action="store_true"); ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    a = ap.parse_args(argv)
    io = build_series("IO", force=a.force); mo = build_series("MO", force=a.force)
    if a.build:
        rep = quality_report(io, mo)
        (OUT_DIR / "option_iv_quality.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.run:
        sigs = build_option_signals(io, mo)
        panel, verdicts = run_families_probe(sigs, FAMILIES_MAIN + FAMILIES_DESC, GRID_K, a.n_perm, a.cost_bps, halves=HALVES_OPTION)
        verdicts["role"] = np.where(verdicts["family"].isin(FAMILIES_DESC), "descriptive", "main")
        panel.to_csv(OUT_DIR / "option_axis_probe.csv", index=False)
        verdicts.to_csv(OUT_DIR / "option_axis_probe_verdicts.csv", index=False)
        pd.set_option("display.width", 250)
        print(verdicts.round(4).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
