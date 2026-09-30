"""资金流向面·序列构造（预登记 docs/plans/2026-09-03-money-flow-axis-prereg.md §2）。

输入：stock_selector.index_money_flow（Wind wset marketmoneyflows，migration 055）
      + stock_selector.index_daily（收盘价 → 日收益）。
输出：backtest/output/money_flow_series.csv，列 F1 / F2（两族）+ 各指数 f_/e_ 供描述。

  f_it = (main_in_money − main_out_money) / (main_in_money + main_out_money)  主力净流入占主力成交比 ∈ [−1,1]
  e_it = f_it − (a + b·r_it)，(a,b) 只用 t−1 及以前 250 日 OLS（min 120 日）  → 剔除同日涨跌的残差流，PIT 干净
  F1   = mean(e_300, e_chinext)                                              市场级「聪明钱残差流」
  F2   = e_chinext − e_300                                                   成长板减蓝筹残差流差

分子用 in − out 而非 Wind 的 maininflowmoney 字段：2026-09-03 实测前者恒等于
超大单+大单（7403 行零违反），而后者在 25% 的行与之不符（p99 6.5 万元，尾部 7.9 万）；
取机械一致的那一个。板块口径见 data_fixes/2026-09-03-index-money-flow/README.md。

科创板（000680.SH）资金流自 2019-07-22，但科创综指基期 2019-12-31，故其腿自 2020-01
才有收益率可配——只作稳健性补充，不进主族。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 主族两腿：沪深300（蓝筹）+ 创业板综（成长板）。wset marketmoneyflows 只支持
# 沪深300/创业板/科创板三个板块（2026-09-03 实测，中证500/1000/2000 中英文两套写法一致被拒）。
INDICES = ("000300.SH", "399102.SZ")
STAR = "000680.SH"   # 稳健性腿，自 2020-01
BETA_WINDOW = 250
BETA_MIN = 120
OUT = ROOT / "backtest" / "output" / "money_flow_series.csv"


def net_ratio(main_in: pd.Series, main_out: pd.Series) -> pd.Series:
    """主力净流入占主力成交比 = (in − out)/(in + out) ∈ [−1, 1]，量纲无关。"""
    gross = main_in + main_out
    return ((main_in - main_out) / gross.where(gross > 0)).astype(float)


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
    """F1 = 两腿残差流均值（两腿都在才记）；F2 = 成长板 − 蓝筹。"""
    E = pd.DataFrame({k: e[k] for k in INDICES})
    f1 = E.mean(axis=1).where(E.notna().all(axis=1))
    f2 = E["399102.SZ"] - E["000300.SH"]
    return pd.DataFrame({"F1": f1, "F2": f2})


def _load_db() -> tuple[pd.DataFrame, pd.DataFrame]:
    from backtest.data import _connect, load_db_config
    db = load_db_config(); conn = _connect(db)
    try:
        mf = pd.read_sql(f"SELECT index_code, trade_date, main_in_money, main_out_money "
                         f"FROM {db['schema']}.index_money_flow ORDER BY index_code, trade_date", conn)
        px = pd.read_sql(f"SELECT index_code, trade_date, close FROM {db['schema']}.index_daily "
                         f"WHERE index_code IN {(*INDICES, STAR)} ORDER BY index_code, trade_date", conn)
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
    for idx in (*INDICES, STAR):
        m = mf[mf.index_code == idx].set_index("trade_date")
        p = px[px.index_code == idx].set_index("trade_date")["close"].astype(float)
        if m.empty:
            continue
        f = net_ratio(m.main_in_money.astype(float), m.main_out_money.astype(float)).rename(idx)
        r = p.pct_change().rename(idx)
        cols[f"f_{idx}"] = f; cols[f"r_{idx}"] = r
        e[idx] = residualize_pit(f, r)
        cols[f"e_{idx}"] = e[idx]
    for idx in (*INDICES, STAR):
        e.setdefault(idx, pd.Series(dtype=float, name=idx))
    out = pd.concat([families(e), pd.DataFrame(cols)], axis=1).sort_index()
    out.index.name = "trade_date"
    OUT.parent.mkdir(parents=True, exist_ok=True); out.to_csv(OUT)
    return out


if __name__ == "__main__":
    s = build_series(force="--force" in sys.argv)
    print(s[["F1", "F2"]].describe().round(4).to_string()); print("nan:", s[["F1", "F2"]].isna().sum().to_dict())
