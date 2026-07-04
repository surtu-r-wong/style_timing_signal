"""逐日逐列比对 PG index_daily 与本地 CSV，输出差异报告。

用法（仓库根）: python3 tools/diff_pg_vs_csv.py
报告: output/phase1_diff/summary.csv + 明细 *_mismatch.csv（仅当有差异）
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_pg_closes  # noqa: E402

OUT_DIR = ROOT / "output" / "phase1_diff"
TOL = 1e-4  # CSV 导出精度容差；严格目标是 0


def load_citic_csv() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "中信风格合并.csv", skiprows=5, usecols=[0, 1, 2, 3, 4, 5],
                     names=["date", "稳定", "成长", "金融", "周期", "消费"], parse_dates=["date"])
    return df.dropna(subset=["date"]).set_index("date").sort_index().astype(float)


def load_gv_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / name, encoding="utf-8-sig")
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).set_index("date").sort_index().apply(pd.to_numeric, errors="coerce")


def load_hs300_csv() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "沪深300.csv", skiprows=6, usecols=[0, 1],
                     names=["date", "沪深300"], parse_dates=["date"])
    return df.dropna(subset=["date"]).set_index("date").sort_index().astype(float)


def compare(tag: str, csv_df: pd.DataFrame, rows: list[dict]) -> None:
    names = [c for c in csv_df.columns]
    try:
        pg_df = load_pg_closes(names)
    except (KeyError, ValueError) as e:  # 代码未映射/未回填（如创业板/科创）
        rows.append({"数据集": tag, "状态": f"跳过: {e}"})
        return
    common = csv_df.index.intersection(pg_df.index)
    pg_missing = csv_df.index.difference(pg_df.index)
    for col in names:
        both = pd.DataFrame({"csv": csv_df.loc[common, col], "pg": pg_df.loc[common, col]}).dropna()
        diff = (both["csv"] - both["pg"]).abs()
        n_bad = int((diff > TOL).sum())
        rows.append({
            "数据集": tag, "列": col, "共同日数": len(both),
            "最大绝对差": float(diff.max()) if len(both) else None,
            "超容差行数": n_bad, "PG缺日数": len(pg_missing),
            "状态": "OK" if n_bad == 0 and len(pg_missing) == 0 else "DIFF",
        })
        if n_bad:
            bad = both[diff > TOL]
            bad.to_csv(OUT_DIR / f"{tag}_{col}_mismatch.csv")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    compare("中信风格", load_citic_csv(), rows)
    compare("成长价值2019", load_gv_csv("成长价值指数_2019.csv"), rows)
    compare("成长价值2014", load_gv_csv("成长价值指数_2014.csv"), rows)
    compare("沪深300", load_hs300_csv(), rows)
    report = pd.DataFrame(rows)
    report.to_csv(OUT_DIR / "summary.csv", index=False)
    print(report.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
