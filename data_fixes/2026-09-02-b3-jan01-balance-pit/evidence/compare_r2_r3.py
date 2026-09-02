"""Compare r2 (pre-fix) and r3 (Jan-01 filter) B3 exposure artifacts."""
import sys
import pandas as pd

r2_dir, r3_dir, uncovered_csv = sys.argv[1:4]
key = ["pit_policy", "ticker", "formation_date"]
cols = key + ["style_score", "model_eligible", "size_eligible",
              "true_first_disclosure_verified", "model_exclusion_reason"]
r2 = pd.read_csv(f"{r2_dir}/monthly_exposures.csv.gz", usecols=cols)
r3 = pd.read_csv(f"{r3_dir}/monthly_exposures.csv.gz", usecols=cols)
print(f"rows r2={len(r2):,} r3={len(r3):,}")
m = r2.merge(r3, on=key, how="outer", suffixes=("_r2", "_r3"), indicator=True)
print("row-set:", m["_merge"].value_counts().to_dict())
both = m[m["_merge"] == "both"]
score_changed = both[
    ~((both["style_score_r2"].isna() & both["style_score_r3"].isna())
      | (both["style_score_r2"].sub(both["style_score_r3"]).abs() < 1e-12))
]
print(f"style_score changed: {len(score_changed):,} rows, "
      f"{score_changed['ticker'].nunique()} tickers, "
      f"{score_changed['formation_date'].nunique()} formation dates")
for c in ["model_eligible", "size_eligible", "true_first_disclosure_verified"]:
    d = both[both[f"{c}_r2"].ne(both[f"{c}_r3"])]
    print(f"{c} changed: {len(d):,}")
unc = pd.read_csv(uncovered_csv)
u = unc.merge(r3, on=key, how="left")
print(f"r2 uncovered rows={len(unc)}; in r3: model_eligible={int(u['model_eligible'].fillna(False).sum())}, "
      f"verified={int(u['true_first_disclosure_verified'].fillna(False).sum())}, "
      f"missing={int(u['style_score'].isna().sum())}")
diag2 = pd.read_csv(f"{r2_dir}/exposure_diagnostics.csv")
diag3 = pd.read_csv(f"{r3_dir}/exposure_diagnostics.csv")
print("unverified model rows: r2 =", int(diag2["unverified_first_disclosure_model_rows"].sum()),
      "r3 =", int(diag3["unverified_first_disclosure_model_rows"].sum()))
print("model_n total: r2 =", int(diag2["model_n"].sum()), "r3 =", int(diag3["model_n"].sum()))
# per-year summary of changed scores
score_changed = score_changed.assign(year=score_changed["formation_date"].str[:4])
print(score_changed.groupby(["pit_policy", "year"]).size().to_string())
