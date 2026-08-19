"""第 5 桶「官方链余集」定义定标 —— 只算成分构成，不算任何收益读数。

对照 A(余集) vs B(排名3801+) vs A2(余集∩排名筛)，为预登记 r3 §1 裁决点 1 提供数字。
只读；有界输出。
"""
import sys
from pathlib import Path

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from signals.common.config import load_db_config  # noqa: E402
import psycopg2  # noqa: E402
import pandas as pd  # noqa: E402

db = load_db_config()
conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                        user=db["user"], password=db["password"], connect_timeout=15)
conn.set_session(readonly=True, autocommit=True)
s = db["schema"]
cur = conn.cursor()
cur.execute("SET statement_timeout = '180s'")

MOTHERS = ["000300.SH", "000905.SH", "000852.SH", "932000.CSI"]

cur.execute(f"""SELECT index_code, max(effective_date), count(DISTINCT effective_date)
                FROM {s}.index_constituent WHERE index_code = ANY(%s)
                GROUP BY index_code ORDER BY index_code""", (MOTHERS,))
print("四母快照可用性 (code, 最新期, 期数):")
for r in cur.fetchall():
    print("   ", r)


def mother_list(code, d):
    """取 <= d 的最近一期（与管线 fallback 同）。"""
    cur.execute(f"""SELECT ts_code, effective_date FROM {s}.index_constituent
                    WHERE index_code=%s AND effective_date =
                      (SELECT max(effective_date) FROM {s}.index_constituent
                       WHERE index_code=%s AND effective_date <= DATE %s)""",
                (code, code, d))
    rows = cur.fetchall()
    return {r[0] for r in rows}, (rows[0][1] if rows else None)


def snapshot(d):
    """带快照护栏的 total_mv 截面（窗口峰值一半以上行数的最近交易日）。"""
    cur.execute(f"""WITH cnt AS (
                      SELECT trade_date, count(*) n FROM {s}.stock_indicator
                      WHERE trade_date BETWEEN DATE %s - 40 AND DATE %s GROUP BY trade_date)
                    SELECT ts_code, total_mv::float8, trade_date FROM {s}.stock_indicator
                    WHERE trade_date = (SELECT max(trade_date) FROM cnt
                                        WHERE n >= 0.5*(SELECT max(n) FROM cnt))
                      AND total_mv IS NOT NULL""", (d, d))
    df = pd.DataFrame(cur.fetchall(), columns=["ts_code", "total_mv", "trade_date"])
    df = df.sort_values("total_mv", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def pct(x, tot):
    return f"{100.0*x/tot:.2f}%" if tot else "n/a"


for d in ("2024-06-17", "2026-06-15"):
    print(f"\n{'='*66}\n截面 {d}")
    df = snapshot(d)
    snap_date = df["trade_date"].iloc[0]
    tot_mv = df["total_mv"].sum()
    print(f"  快照日={snap_date}  全市场 {len(df)} 只  总市值 {tot_mv/1e4:.0f} 亿(万元单位下)")

    covered, stale = set(), []
    for code in MOTHERS:
        lst, eff = mother_list(code, d)
        covered |= lst
        stale.append(f"{code}:{eff}({len(lst)})")
    print("  四母用期:", " ".join(stale))
    print(f"  四母并集 {len(covered)} 只")

    df["covered"] = df["ts_code"].isin(covered)
    rem = df[~df["covered"]]           # A1 = 纯余集
    b = df[df["rank"] >= 3801]         # B  = 排名尾
    print(f"\n  A1 纯余集      : {len(rem):5d} 只  市值 {pct(rem['total_mv'].sum(), tot_mv)}")
    print(f"  B  排名3801+   : {len(b):5d} 只  市值 {pct(b['total_mv'].sum(), tot_mv)}")
    inter = set(rem["ts_code"]) & set(b["ts_code"])
    print(f"  A1∩B = {len(inter)}  → A1 独有 {len(rem)-len(inter)} / B 独有 {len(b)-len(inter)}"
          f"  (Jaccard {len(inter)/max(1,len(set(rem['ts_code'])|set(b['ts_code']))):.3f})")

    if len(rem):
        q = rem["rank"].quantile([0.0, 0.05, 0.25, 0.5]).astype(int).tolist()
        print(f"  A1 排名分布: min={q[0]} 5%={q[1]} 25%={q[2]} 中位={q[3]}")
        for K in (1800, 3000, 3500, 3800):
            cut = rem[rem["rank"] <= K]
            print(f"    排名 ≤{K} 的余集成员: {len(cut):4d} 只 "
                  f"({pct(len(cut), len(rem))} 只数) 市值占余集 "
                  f"{pct(cut['total_mv'].sum(), rem['total_mv'].sum())}")
        bj = rem[rem["ts_code"].str.endswith(".BJ")]
        bj_hi = bj[bj["rank"] <= 3500]
        print(f"  A1 中 .BJ: {len(bj)} 只（其中排名≤3500 的 {len(bj_hi)} 只）")
        top = rem.nsmallest(8, "rank")[["ts_code", "rank", "total_mv"]]
        print("  A1 里排名最高的 8 只:",
              ", ".join(f"{r.ts_code}#{r.rank}" for r in top.itertuples()))
        # A2 = 余集 ∩ 排名筛
        for K in (3000, 3500, 3800):
            a2 = rem[rem["rank"] > K]
            print(f"  A2(K={K}): {len(a2):5d} 只  市值 {pct(a2['total_mv'].sum(), tot_mv)}"
                  f"  中位排名 {int(a2['rank'].median()) if len(a2) else 0}"
                  f"  与B重合 {len(set(a2['ts_code']) & set(b['ts_code']))}")

conn.close()
print("\ndone")
