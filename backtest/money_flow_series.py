"""资金流向面·序列构造（预登记 docs/plans/2026-09-03-money-flow-axis-prereg.md §2）。

输入：stock_selector.index_money_flow（Wind wset marketmoneyflows，migration 055）
      + stock_selector.index_daily（收盘价 → 日收益）。
输出：backtest/output/money_flow_series.csv，列 F1 / F2（两族）+ 各指数 f_/e_ 供描述。

  f_it = main_inflow_money / (main_in_money + main_out_money)      净流入占主力成交比，量纲无关
  e_it = f_it − (a + b·r_it)，(a,b) 只用 t−1 及以前 250 日 OLS（min 120 日）  → 剔除同日涨跌的残差流，PIT 干净
  F1   = 四指数 e_it 均值（可用 ≥3 才记，否则 NaN）                          市场级「聪明钱残差流」
  F2   = mean(e_500, e_1000) − e_300                                          小盘减大盘残差流差
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDICES = ("000300.SH", "000905.SH", "000852.SH", "932000.CSI")
BETA_WINDOW = 250
BETA_MIN = 120
OUT = ROOT / "backtest" / "output" / "money_flow_series.csv"


def net_ratio(main_inflow: pd.Series, main_in: pd.Series, main_out: pd.Series) -> pd.Series:
    gross = main_in + main_out
    return (main_inflow / gross.where(gross > 0)).astype(float)


def residualize_pit(f: pd.Series, r: pd.Series, window: int = BETA_WINDOW, min_obs: int = BETA_MIN) -> pd.Series:
    """e_t = f_t − a_{t−1} − b_{t−1}·r_t，(a,b) 由 [t−window, t−1] 的 OLS 给出。

    严格只用过去：t 日的系数不含 t 日本身，故 e_t 在 t 收盘即可算、且不随未来样本改写。
    """
    df = pd.concat({"f": f, "r": r}, axis=1).dropna()
    x, y = df["r"], df["f"]
    mx = x.rolling(window, min_periods=min_obs).mean().shift(1)
    my = y.rolling(window, min_periods=min_obs).mean().shift(1)
    mxy = (x * y).rolling(window, min_periods=min_obs).mean().shift(1)
    mxx = (x * x).rolling(window, min_periods=min_obs).mean().shift(1)
    var = mxx - mx * mx
    b = (mxy - mx * my) / var.where(var > 0)
    a = my - b * mx
    return (y - a - b * x).rename(f.name)


def families(e: dict[str, pd.Series]) -> pd.DataFrame:
    E = pd.DataFrame(e)
    f1 = E.mean(axis=1).where(E.notna().sum(axis=1) >= 3)
    f2 = (E["000905.SH"] + E["000852.SH"]) / 2 - E["000300.SH"]
    return pd.DataFrame({"F1": f1, "F2": f2})


def _load_db() -> tuple[pd.DataFrame, pd.DataFrame]:
    from backtest.data import _connect, load_db_config
    db = load_db_config(); conn = _connect(db)
    try:
        mf = pd.read_sql(f"SELECT index_code, trade_date, main_inflow_money, main_in_money, main_out_money "
                         f"FROM {db['schema']}.index_money_flow ORDER BY index_code, trade_date", conn)
        px = pd.read_sql(f"SELECT index_code, trade_date, close FROM {db['schema']}.index_daily "
                         f"WHERE index_code IN {INDICES} ORDER BY index_code, trade_date", conn)
    finally:
        conn.close()
    for d in (mf, px):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    return mf, px


def build_series(force: bool = False) -> pd.DataFrame:
    if OUT.exists() and not force:
        return pd.read_csv(OUT, index_col=0, parse_dates=True)
    mf, px = _load_db()
    cols = {}; e = {}
    for idx in INDICES:
        m = mf[mf.index_code == idx].set_index("trade_date")
        p = px[px.index_code == idx].set_index("trade_date")["close"].astype(float)
        if m.empty:
            continue
        f = net_ratio(m.main_inflow_money.astype(float), m.main_in_money.astype(float), m.main_out_money.astype(float)).rename(idx)
        r = p.pct_change().rename(idx)
        cols[f"f_{idx}"] = f; cols[f"r_{idx}"] = r
        e[idx] = residualize_pit(f, r)
        cols[f"e_{idx}"] = e[idx]
    for idx in INDICES:
        e.setdefault(idx, pd.Series(dtype=float, name=idx))
    out = pd.concat([families(e), pd.DataFrame(cols)], axis=1).sort_index()
    out.index.name = "trade_date"
    OUT.parent.mkdir(parents=True, exist_ok=True); out.to_csv(OUT)
    return out


if __name__ == "__main__":
    s = build_series(force="--force" in sys.argv)
    print(s[["F1", "F2"]].describe().round(4).to_string()); print("nan:", s[["F1", "F2"]].isna().sum().to_dict())
