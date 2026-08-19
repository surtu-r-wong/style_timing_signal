"""北交所(.BJ)数据覆盖探查 —— 第 5 桶预登记 r3 §2-1 / §8-③ 前置项。

只读；每语句 statement_timeout=120s；输出有界（只打汇总数字）。
"""
import sys
from pathlib import Path

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from signals.common.config import load_db_config  # noqa: E402
import psycopg2  # noqa: E402

db = load_db_config()
conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                        user=db["user"], password=db["password"], connect_timeout=15)
conn.set_session(readonly=True, autocommit=True)
s = db["schema"]
cur = conn.cursor()
cur.execute("SET statement_timeout = '120s'")

def q(label, sql, args=None):
    try:
        cur.execute(sql, args)
        rows = cur.fetchall()
        print(f"{label}: {rows[:12]}")
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"{label}: ERROR {type(e).__name__}: {str(e)[:120]}")

print(f"host={db['host']} schema={s}")

# 1) 各表 .BJ 覆盖：只数 + 日期范围
q("meta_.BJ(只数,最早/最晚上市)",
  f"SELECT count(*), min(list_date), max(list_date) FROM {s}.stock_meta WHERE ts_code LIKE '%%.BJ'")
q("daily_price_.BJ(只数,起,止)",
  f"SELECT count(DISTINCT ts_code), min(trade_date), max(trade_date) FROM {s}.stock_daily_price WHERE ts_code LIKE '%%.BJ'")
q("indicator_.BJ(只数,起,止)",
  f"SELECT count(DISTINCT ts_code), min(trade_date), max(trade_date) FROM {s}.stock_indicator WHERE ts_code LIKE '%%.BJ'")
q("share_capital_.BJ(只数)",
  f"SELECT count(DISTINCT ts_code) FROM {s}.stock_share_capital WHERE ts_code LIKE '%%.BJ'")
q("financial_.BJ(只数)",
  f"SELECT count(DISTINCT ts_code) FROM {s}.stock_financial WHERE ts_code LIKE '%%.BJ'")
q("status_.BJ(只数)",
  f"SELECT count(DISTINCT ts_code) FROM {s}.stock_status WHERE ts_code LIKE '%%.BJ'")
q("industry_.BJ(只数)",
  f"SELECT count(DISTINCT ts_code) FROM {s}.industry_classification WHERE ts_code LIKE '%%.BJ'")

# 2) 时效：近两月 .BJ 行情是否在采
q("daily_price_.BJ 近期(只数 @>=2026-07-01)",
  f"SELECT count(DISTINCT ts_code) FROM {s}.stock_daily_price WHERE ts_code LIKE '%%.BJ' AND trade_date >= DATE '2026-07-01'")

# 3) 关键调样日截面：上市>2年的 .BJ 里，当日有行情/indicator 的比例
#    （2023-12-15 与 2026-06-15 两个生效日附近）
for d in ("2023-12-15", "2026-06-15"):
    q(f".BJ 截面@{d}(上市>2y总数, 有daily行, 有indicator行)",
      f"""SELECT
            (SELECT count(*) FROM {s}.stock_meta
              WHERE ts_code LIKE '%%.BJ' AND list_date <= DATE %s - 730),
            (SELECT count(DISTINCT p.ts_code) FROM {s}.stock_daily_price p
              JOIN {s}.stock_meta m ON m.ts_code = p.ts_code
              WHERE p.ts_code LIKE '%%.BJ' AND m.list_date <= DATE %s - 730
                AND p.trade_date BETWEEN DATE %s - 7 AND DATE %s),
            (SELECT count(DISTINCT i.ts_code) FROM {s}.stock_indicator i
              JOIN {s}.stock_meta m ON m.ts_code = i.ts_code
              WHERE i.ts_code LIKE '%%.BJ' AND m.list_date <= DATE %s - 730
                AND i.trade_date BETWEEN DATE %s - 7 AND DATE %s)""",
      (d, d, d, d, d, d, d))

# 4) 官方名单里的 .BJ：932000 全期 + 2000 两腿
q("932000 名单 .BJ(期, .BJ只数)",
  f"""SELECT effective_date, count(*) FILTER (WHERE ts_code LIKE '%%.BJ')
      FROM {s}.index_constituent WHERE index_code = '932000.CSI'
      GROUP BY effective_date ORDER BY effective_date""")
q("932409/932408 名单 .BJ(码, 期, .BJ只数)",
  f"""SELECT index_code, effective_date, count(*) FILTER (WHERE ts_code LIKE '%%.BJ')
      FROM {s}.index_constituent WHERE index_code IN ('932409.CSI','932408.CSI')
      GROUP BY index_code, effective_date ORDER BY index_code, effective_date""")

conn.close()
print("done")
