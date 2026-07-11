"""Friendly-name field map for stock_financial JSONB.

⚠️ 本文件 COPIED from stock_selector/data/financial_field_map.py（方向 C · B1，2026-07-07；
   守 optimization-roadmap 设计稿 §4.1「本项目不 import stock_selector 包，只共享 DB」）。
   拷贝而非 import：两项目 Python 环境独立。**同步**：stock_selector 更新 CSMAR_END 或字段
   映射时手动同步此拷贝（源自带 bump 流程 + tests/test_financial_field_map.py）。

CSMAR codes (A001000000 etc.) and Wind native names (profit_ttm etc.) are
both normalized to a common friendly vocabulary so factor / filter code
doesn't have to know the source.

Source cutoff (PG reader uses this):
- end_date <= CSMAR_END → CSMAR rows
- end_date >  CSMAR_END → Wind rows

CSMAR codes verified against
``a股上市公司财务数据合集（90-25年）/字段说明.txt``.
"""

from __future__ import annotations

from datetime import date, timedelta


CSMAR_END = date(2025, 3, 31)

# Bump procedure when CSMAR data is updated to include later quarters:
#   1. Update CSMAR_END to the new last quarter end (e.g., date(2025, 6, 30) for 2025-H1).
#   2. Re-run `pytest tests/test_universe_filter.py -v` — the parametrized
#      ``test_step_2_keeps_stable_high_roe_csmar`` covers both within-window and
#      past-cutoff selection dates.
#   3. Verify selection: `python -m signals --date <YYYY-MM-DD> --source pg --no-delta`
#      — universe count should stay in the same order of magnitude.
# Without bumping, new CSMAR quarters are silently filtered out by the source-cutoff
# SQL clauses across pg_reader / universe_filter.


# CSMAR field code → friendly name, per statement_type.
# **Strict whitelist**: any field not listed is dropped during translation.
CSMAR_FIELD_MAPS: dict[str, dict[str, str]] = {
    "balance": {
        "A001000000": "total_assets",  # 资产总计
        "A001220000": "goodwill",  # 商誉净额
        "A003000000": "total_equity",  # 所有者权益合计（含少数）
        "A003100000": "equity_parent",  # 归属于母公司所有者权益合计
    },
    "profitability": {
        # F053004C = 归属于母公司净资产收益率 TTM (归母净利润 TTM / 平均归母权益).
        # 跟 Wind `fa_roe_ttmavg` 同口径。F050504C 是"全部净利润/全部股东权益"，
        # 对金融/综合类公司差异显著 (平安 11.58% vs 归母口径 13.85%)。
        "F053004C": "roe_ttm",
    },
    "income": {
        "B001100000": "revenue",  # 营业收入
        "B002000000": "net_profit_ytd",  # 净利润 YTD 累计
        "B002000101": "net_profit_parent_ytd",  # 归母净利润 YTD
    },
    "cashflow_direct": {
        "C001000000": "cfo_net",  # 经营活动产生的现金流量净额
    },
    "dividend": {
        "F110101B": "cash_dividend_ps_pre_tax",  # 每股税前现金股利
        "F110301B": "dividend_payout_ratio",  # 股利分配率
        "F110401B": "cash_dividend_ps_change",  # 每股股利变动值
        "F110501B": "cash_dividend_ps_growth",  # 每股股利变动比率
    },
}


# Wind native name → friendly name, per statement_type.
# Non-listed keys are passed through unchanged (back-compat for other consumers).
#
# Two generations of Wind fields coexist:
#   - Legacy daily-TTM rows (wsd Period=D): goodwill / fa_totequity /
#     equity_new / profit_ttm. These live at trading-day end_dates in
#     stock_financial; kept for backward read compatibility.
#   - Quarter-end rows (wsd Period=Q, written by scripts/load_wind_quarterly.py):
#     goodwill / tot_equity / eqy_belongto_parcomsh / net_profit_is /
#     np_belongto_parcomsh / fa_roe_ttmavg. End_date is quarter-end and
#     caliber matches CSMAR's friendly_name conventions exactly (Wind 归母
#     ROE / 归母权益 / YTD 净利润).
WIND_FIELD_PASSTHROUGH: dict[str, dict[str, str]] = {
    "balance": {
        "goodwill": "goodwill",
        # legacy daily-TTM
        "fa_totequity": "total_equity",
        "equity_new": "equity_parent",
        # quarter-end (Period=Q)
        "tot_assets": "total_assets",
        "tot_equity": "total_equity",
        "eqy_belongto_parcomsh": "equity_parent",
    },
    "income": {
        # legacy daily-TTM
        "profit_ttm": "net_profit_ttm",
        # quarter-end (Period=Q)
        "oper_rev": "revenue",
        "tot_oper_rev": "revenue",
        "net_profit_is": "net_profit_ytd",
        "np_belongto_parcomsh": "net_profit_parent_ytd",
    },
    "cashflow_direct": {
        "net_cash_flows_oper_act": "cfo_net",
    },
    "cashflow": {
        # Wind quarter-end cashflow rows (statement_type='cashflow') expose
        # operating CF as an already-TTM value. Map to cfo_ttm (distinct from
        # CSMAR's YTD cfo_net) so CFO consumes it directly instead of pushing a
        # TTM number through the YTD→quarterize→4Q-sum path.
        "operatecashflow_ttm2": "cfo_ttm",
    },
    "profitability": {
        # quarter-end (Period=Q) — CSMAR also has profitability;
        # value is stored *post-transform* (Wind percentage ÷ 100 = decimal)
        # so it reads on the same scale as CSMAR F053004C.
        "fa_roe_ttmavg": "roe_ttm",
    },
}


def legal_disclosure_deadline(end_date: date) -> date:
    """A 股法定披露上限。

    Q1 (3-31) 必须 4-30 前披露；H1 (6-30) → 8-31；
    Q3 (9-30) → 10-31；年报 (12-31) → 次年 4-30。

    Used to cap stock_financial.ann_date at read time: MIN(stored, legal).

    Why cap: stored ann_date is unreliable — CSMAR's DeclareDate is largely a
    dataset batch/export date, not the first-disclosure date (2026-07-11
    checked against prod stock_financial: 73% of CSMAR quarter-end rows have
    ann_date > legal deadline, clustering on ~8 batch dates such as
    2025-07-29 / 2024-01-28; max ~17 years "late"). Capping restores a
    usable known-date.

    ⚠️ NOT strictly PIT-safe: for genuinely late filers (reports disclosed
    after the statutory deadline — a small, mostly ST/troubled minority,
    ~1%/yr), MIN() marks data available at the deadline before it was
    actually public → look-ahead on those names. A real fix needs true
    first-announcement dates (e.g. Wind wss). Effective semantics for most
    CSMAR history: known_date ≈ legal deadline (conservative for on-time
    filers, look-ahead for the late tail).

    Caller must pass a quarter-end date. Non-quarter-end inputs (months
    1/2/4/5/7/8/10/11) return ``end_date + 120 days`` as a safety fallback;
    this path should be treated as a data-quality bug rather than a
    statutory deadline.
    """
    y, m = end_date.year, end_date.month
    if m == 3:
        return date(y, 4, 30)
    if m == 6:
        return date(y, 8, 31)
    if m == 9:
        return date(y, 10, 31)
    if m == 12:
        return date(y + 1, 4, 30)
    return end_date + timedelta(days=120)


def translate_data(data: dict, source: str, statement_type: str) -> dict:
    """Translate a stock_financial.data JSONB dict to friendly names.

    CSMAR: **strict whitelist** — only keys in CSMAR_FIELD_MAPS pass through,
    renamed. Other CSMAR codes are dropped to keep the friendly-name space clean.

    Wind: mapped keys are renamed; unmapped keys pass through unchanged so
    other consumers reading raw Wind fields keep working.

    Returns a new dict; does not mutate input.
    """
    if not data:
        return {}
    if source == "csmar":
        m = CSMAR_FIELD_MAPS.get(statement_type, {})
        return {m[k]: v for k, v in data.items() if k in m}
    # wind (and any other source defaults to passthrough behavior)
    m = WIND_FIELD_PASSTHROUGH.get(statement_type, {})
    return {m.get(k, k): v for k, v in data.items()}
