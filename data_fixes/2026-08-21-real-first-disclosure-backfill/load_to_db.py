"""R5 阶段二·灌数：把 stm_issuingdate_2003_2025q1.csv 灌进
stock_selector.stock_first_disclosure（migration 051，单端表仅 Debian）。

规则（与 051 头注一致）：哨兵日期（<2000 年）→ first_disclosure_date NULL +
quality='sentinel'，不存哨兵值；其余存日期 + quality='ok'。幂等：ON CONFLICT
(ts_code, end_date) DO UPDATE（同 CSV 重跑结果不变）。

跑法：cd /home/elfbob/claude-code/stock_selector && .venv/bin/python \
    /home/elfbob/claude-code/style_timing_signal/data_fixes/\
2026-08-21-real-first-disclosure-backfill/load_to_db.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values

sys.path.insert(0, "/home/elfbob/claude-code/stock_selector")

from stock_selector.config import load_config, resolve_settings_path  # noqa: E402
from stock_selector.db.connection import get_connection, pg_config_from  # noqa: E402

CSV = Path(__file__).resolve().parent / "stm_issuingdate_2003_2025q1.csv"
FETCHED_AT = date(2026, 8, 21)


def main() -> int:
    df = pd.read_csv(CSV, dtype=str)
    stm = pd.to_datetime(df["stm_issuingdate"], errors="coerce")
    ok = stm.dt.year >= 2000
    rows = [
        (r.ts_code, r.end_date,
         s.date() if o else None,
         "ok" if o else "sentinel",
         FETCHED_AT)
        for r, s, o in zip(df.itertuples(), stm, ok)
    ]
    print(f"CSV {len(rows)} 行；ok {int(ok.sum())} / sentinel {int((~ok).sum())}")

    cfg = load_config(resolve_settings_path())
    with get_connection(pg_config_from(cfg)) as conn:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO stock_first_disclosure
                (ts_code, end_date, first_disclosure_date, quality, fetched_at)
            VALUES %s
            ON CONFLICT (ts_code, end_date) DO UPDATE SET
                first_disclosure_date = EXCLUDED.first_disclosure_date,
                quality = EXCLUDED.quality,
                fetched_at = EXCLUDED.fetched_at""", rows, page_size=10000)
        conn.commit()
        cur.execute("""SELECT count(*), count(first_disclosure_date),
                              count(*) FILTER (WHERE quality='sentinel'),
                              min(end_date), max(end_date)
                       FROM stock_first_disclosure""")
        total, nonnull, sent, lo, hi = cur.fetchone()
    print(f"库内: {total} 行, 非空日期 {nonnull}, sentinel {sent}, 期 {lo}..{hi}")
    assert total == len(rows) and sent == int((~ok).sum()), "行数/哨兵数不符"
    print("回执: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
