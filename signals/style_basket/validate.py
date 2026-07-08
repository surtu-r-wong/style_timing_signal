"""B1-T4 正确性闸门：自建风格篮子价差 vs equal_weight 指数对（预期 ρ~0.8）。

两个层面：
  ① 价差收益层：自建 spread（growth_ret−value_ret）vs 各指数对日收益差
     （ln 差分同阶近似），Pearson 相关（日频 + 月频聚合）。
  ② 信号层：自建两腿累计净值走 equal_weight 同一信号管线
     （_compute_pair_signal lookback=20/z=40 + smoothing=5）vs committed
     equal_weight factor_value 的相关。
产出 output/style_basket/validation_<U>.csv + 控制台摘要。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.common.financial_reader import _connect  # noqa: E402
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402
from signals.style_basket.build import OUT_DIR  # noqa: E402

# 指数对（signals/common/index_codes.csv 口径；equal_weight config_4pairs 四对）
INDEX_PAIRS = {
    "300pair": ("000918.CSI", "000919.CSI"),
    "500pair": ("H30351.CSI", "H30352.CSI"),
    "1000pair": ("932407.CSI", "932406.CSI"),
    "2000pair": ("932409.CSI", "932408.CSI"),
}
EW_SIGNAL_FILE = ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"


def _fetch_index_closes(db, codes: list[str]) -> pd.DataFrame:
    conn = _connect(db)
    try:
        df = pd.read_sql(
            f"""SELECT index_code, trade_date, close FROM {db['schema']}.index_daily
                WHERE index_code = ANY(%(cs)s) AND close IS NOT NULL""",
            conn,
            params={"cs": codes},
        )
    finally:
        conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.pivot_table(index="trade_date", columns="index_code", values="close")


def validate_universe(uni: str, db=None) -> pd.DataFrame:
    db = db or load_db_config()
    spread = pd.read_csv(OUT_DIR / f"spread_{uni}.csv", parse_dates=["date"]).set_index("date")

    codes = [c for pair in INDEX_PAIRS.values() for c in pair]
    idx_close = _fetch_index_closes(db, codes)

    rows = []
    # ① 价差收益层
    for name, (g_code, v_code) in INDEX_PAIRS.items():
        if g_code not in idx_close.columns or v_code not in idx_close.columns:
            continue
        pair_ret = (
            idx_close[g_code].pct_change(fill_method=None)
            - idx_close[v_code].pct_change(fill_method=None)
        ).dropna()
        joint = pd.concat([spread["spread"], pair_ret], axis=1, join="inner").dropna()
        joint.columns = ["ours", "pair"]
        rho_d = joint["ours"].corr(joint["pair"])
        monthly = joint.resample("ME").sum()
        rho_m = monthly["ours"].corr(monthly["pair"])
        rows.append(
            {"universe": uni, "target": name, "level": "spread_ret",
             "rho_daily": rho_d, "rho_monthly": rho_m, "n_days": len(joint)}
        )

    # ② 信号层：自建两腿净值 → equal_weight 生产管线（20/40 + smoothing 5）
    if EW_SIGNAL_FILE.exists():
        sig_ours = _compute_pair_signal(
            spread["growth_index"], spread["value_index"], lookback=20, z_window=40
        ).rolling(5, min_periods=1).mean()
        ew = pd.read_csv(EW_SIGNAL_FILE, parse_dates=["date"]).set_index("date")
        joint = pd.concat([sig_ours, ew["factor_value"]], axis=1, join="inner").dropna()
        joint.columns = ["ours", "ew"]
        rows.append(
            {"universe": uni, "target": "equal_weight_factor_value", "level": "signal",
             "rho_daily": joint["ours"].corr(joint["ew"]),
             "rho_monthly": joint.resample("ME").last().dropna()["ours"].corr(
                 joint.resample("ME").last().dropna()["ew"]),
             "n_days": len(joint)}
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="B1-T4 自建篮子 vs 指数对验证")
    parser.add_argument("--universes", default="U0,U1,U2,U3,U4")
    args = parser.parse_args()
    db = load_db_config()
    all_rows = []
    for uni in args.universes.split(","):
        uni = uni.strip()
        f = OUT_DIR / f"spread_{uni}.csv"
        if not f.exists():
            print(f"[validate] {uni}: spread 文件缺失，跳过")
            continue
        got = validate_universe(uni, db=db)
        all_rows.append(got)
        out_file = OUT_DIR / f"validation_{uni}.csv"
        got.to_csv(out_file, index=False)
        print(f"\n=== {uni} ===")
        print(got.to_string(index=False,
                            float_format=lambda x: f"{x:.3f}" if abs(x) < 10 else f"{x:.0f}"))
    if all_rows:
        combined = pd.concat(all_rows, ignore_index=True)
        combined.to_csv(OUT_DIR / "validation_all.csv", index=False)
        print(f"\n[validate] 汇总 -> validation_all.csv ({len(combined)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
