"""创业板综 399102.SZ / 科创综指 000680.SH 日线 → stock_selector.index_daily。

资金流表只覆盖 沪深300 / 创业板 / 科创板 三个板块（2026-09-03 探针实测），而残差化需要
各板块**自己的**收益率；库里原本只有 300/500/1000/2000 的日线，故补这两条腿。

口径选择：创业板综（全体创业板股票）而非创业板指（只有 100 只），与 chinext 板块口径一致；
科创板同理取科创综指。两条腿不进日更 topup（topup_guard / check_freshness 的代码列表来自
固定的 load_code_map()，不扫表，故不会误报）。

用法：python fetch_board_index.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.data import _connect, load_db_config  # noqa: E402

CODES = ["399102.SZ", "000680.SH"]
START = "2014-01-01"


def gateway():
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    gw = cfg["wind_gateway"]
    return gw["url"].rstrip("/"), gw["token"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    url, token = gateway()
    s = requests.Session(); s.trust_env = False
    db = load_db_config()
    total = 0
    for code in CODES:
        r = s.get(f"{url}/fetch/index_daily",
                  params={"indices": code, "start": START, "end": date.today().isoformat()},
                  headers={"Authorization": f"Bearer {token}"}, timeout=(10, 600))
        j = r.json()
        if j.get("status") != "ok":
            print(f"{code} ERR {str(j)[:200]}"); return 1
        cols, rows = j["columns"], j["rows"]
        ix = {c: i for i, c in enumerate(cols)}
        recs = []
        for x in rows:
            close = x[ix["close"]]
            if close in (None, ""):
                continue
            recs.append((code, str(x[ix["trade_date"]])[:10],
                         x[ix["open"]], x[ix["high"]], x[ix["low"]], close,
                         int(x[ix["volume"]]) if x[ix["volume"]] not in (None, "") else None,
                         x[ix["amt"]]))
        recs.sort(key=lambda t: t[1])
        print(f"{code}: {len(recs)} 行 {recs[0][1]}..{recs[-1][1]}  cells={len(rows)*len(cols)}")
        if a.dry_run:
            continue
        from psycopg2.extras import execute_values
        with _connect(db) as conn, conn.cursor() as cur:
            execute_values(cur, f"""INSERT INTO {db['schema']}.index_daily
                (index_code, trade_date, open, high, low, close, volume, amount) VALUES %s
                ON CONFLICT (index_code, trade_date) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                volume=EXCLUDED.volume, amount=EXCLUDED.amount, updated_at=NOW()""",
                recs, page_size=2000)
            conn.commit()
        total += len(recs)
    print(f"写入 {total} 行" if not a.dry_run else "dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
