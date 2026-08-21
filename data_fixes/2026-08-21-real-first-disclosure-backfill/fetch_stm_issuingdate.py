"""R5 阶段一：为 stock_financial 的 CSMAR 历史段取真实首披日（Wind stm_issuingdate）。

范围（2026-08-21 实测钉死）：data_source='csmar'、end_date ∈ 2003-03-31..2025-03-31
且为季末日 → 89 期、286,784 个 (ts_code, end_date) 对。
出界项（README 登记）：68,096 个 '01-01' 伪行对（非报告期，wss 无法按 rptDate 取）、
22,493 个 2003 前对（提案 2026-08-14 §三 已收窄到 2003+）。

计费：wss 每调用 = 码数 × 1 字段 → 全程 ≈ 286,784 格。
检查点 = 每 (季度, 1000 码批) 一个 CSV，已完成的批次跳过——中途失败/断额度
重跑最多重复计费 1,000 格。额度耗尽（QuotaExceeded）exit 2，次日续跑即可。

跑法（stock_selector venv + cwd）：
    cd /home/elfbob/claude-code/stock_selector && .venv/bin/python \
        /home/elfbob/claude-code/style_timing_signal/data_fixes/\
2026-08-21-real-first-disclosure-backfill/fetch_stm_issuingdate.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, "/home/elfbob/claude-code/stock_selector")

from stock_selector.config import load_config, resolve_settings_path  # noqa: E402
from stock_selector.data.wind_source import QuotaExceeded, WindDataSource  # noqa: E402
from stock_selector.db.connection import get_connection, pg_config_from  # noqa: E402

HERE = Path(__file__).resolve().parent
CKPT = HERE / "checkpoints"
MERGED = HERE / "stm_issuingdate_2003_2025q1.csv"
BATCH = 1000
FIELD = "stm_issuingdate"


def scope_from_db(cfg: dict) -> dict[str, list[str]]:
    """{季末 end_date → 该期 distinct ts_code 升序}（仅 CSMAR 段、2003+、季末日）。"""
    with get_connection(pg_config_from(cfg)) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT end_date, array_agg(DISTINCT ts_code ORDER BY ts_code)
            FROM stock_financial
            WHERE data_source = 'csmar' AND end_date >= '2003-01-01'
              AND (EXTRACT(month FROM end_date), EXTRACT(day FROM end_date))
                  IN ((3,31),(6,30),(9,30),(12,31))
            GROUP BY end_date ORDER BY end_date""")
        return {str(d): list(codes) for d, codes in cur.fetchall()}


def main() -> int:
    cfg = load_config(resolve_settings_path())
    scope = scope_from_db(cfg)
    n_pairs = sum(len(v) for v in scope.values())
    print(f"范围：{len(scope)} 期，{n_pairs} 对（≈{n_pairs} 格）", flush=True)

    CKPT.mkdir(exist_ok=True)
    wind = WindDataSource.from_settings(cfg)
    t0, fetched = time.time(), 0
    for end_date, codes in scope.items():
        q = pd.Timestamp(end_date).date()
        for b in range(0, len(codes), BATCH):
            ck = CKPT / f"{end_date}_b{b // BATCH:02d}.csv"
            if ck.exists():
                continue
            chunk = codes[b:b + BATCH]
            try:
                df = wind.fetch_financial_snapshot(chunk, [FIELD], q)
            except QuotaExceeded as e:
                print(f"⛔ 额度耗尽于 {end_date} b{b // BATCH}：{e}；"
                      f"已取 {fetched} 格，重跑本脚本续传", flush=True)
                return 2
            # wss 对无数据的码也返回行（NaN）——保留，作 D2 未覆盖显式标注
            out = pd.DataFrame({"ts_code": chunk})
            if not df.empty and FIELD in df.columns:
                out = out.merge(df[["ts_code", FIELD]], on="ts_code", how="left")
            else:
                out[FIELD] = pd.NA
            out.insert(1, "end_date", end_date)
            out.to_csv(ck, index=False)
            fetched += len(chunk)
            print(f"  {end_date} b{b // BATCH:02d}: {len(chunk)} 码 "
                  f"(累计 {fetched}, {time.time() - t0:.0f}s)", flush=True)

    parts = sorted(CKPT.glob("*.csv"))
    merged = pd.concat([pd.read_csv(p, dtype=str) for p in parts], ignore_index=True)
    merged.to_csv(MERGED, index=False)
    nn = merged[FIELD].notna()
    sentinel = nn & (merged[FIELD].str[:4].fillna("9999").astype(int) < 2000)
    print(f"\n合并 {len(parts)} 检查点 → {MERGED.name}：{len(merged)} 行；"
          f"非空 {int(nn.sum())}（{nn.mean():.1%}）、"
          f"哨兵(<2000年) {int(sentinel.sum())}、空 {int((~nn).sum())}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
