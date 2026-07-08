"""EDB 序列回填：gateway /fetch/edb → stock_selector.edb_daily（幂等 upsert）。

用途（方向 B 换轴数据解锁）：两融余额/买入额（杠杆轴）、10Y 国债 YTM（ERP 债腿）
等任何 Wind EDB 序列。EDB 码由用户在 Wind 终端 EDB 浏览器核对后传入——本脚本
不内置任何码（gateway 哑管道原则的客户端侧延伸）。

用法：
  python3 tools/backfill_edb.py \\
      --codes "M0XXXXXX,M0YYYYYY" \\
      --names "融资余额_沪,融资余额_深" \\
      --start 2014-01-01 --end 2026-07-08
  # names 与 codes 一一对应（可省略）；--options 默认 Fill=Previous
  # 前置：config/settings.yaml 填 wind_gateway.url/token；edb_daily 表已建
  #      （tools/ddl_edb_daily.sql，执行前读 DDL 安全卡）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from signals.common.config import CONFIG_FILE, load_db_config  # noqa: E402
from signals.common.financial_reader import _connect  # noqa: E402


def load_gateway_config() -> dict:
    cfg = yaml.safe_load(Path(CONFIG_FILE).read_text(encoding="utf-8")) or {}
    gw = cfg.get("wind_gateway") or {}
    missing = [k for k in ("url", "token") if not gw.get(k)]
    if missing:
        raise ValueError(
            f"settings.yaml wind_gateway 段缺 {missing}（参照 settings.example.yaml）")
    return gw


def fetch_edb(gw: dict, codes: str, start: str, end: str,
              options: str = "Fill=Previous") -> list[list]:
    resp = requests.get(
        f"{gw['url'].rstrip('/')}/fetch/edb",
        params={"codes": codes, "start": start, "end": end, "options": options},
        headers={"Authorization": f"Bearer {gw['token']}"},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"gateway 返回异常: {payload}")
    return payload["rows"]  # [edb_code, trade_date, value]


def upsert_edb_daily(rows: list[list], names: dict[str, str], db=None) -> int:
    db = db or load_db_config()
    sql = f"""
        INSERT INTO {db['schema']}.edb_daily (edb_code, trade_date, value, series_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (edb_code, trade_date)
        DO UPDATE SET value = EXCLUDED.value,
                      series_name = COALESCE(EXCLUDED.series_name, edb_daily.series_name),
                      updated_at = now()
    """
    clean = [
        (code, day, value, names.get(code))
        for code, day, value in rows
        if value is not None
    ]
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, clean)
        conn.commit()
    finally:
        conn.close()
    return len(clean)


def main() -> int:
    ap = argparse.ArgumentParser(description="EDB 序列回填 → stock_selector.edb_daily")
    ap.add_argument("--codes", required=True, help="EDB 码逗号分隔（用户在 Wind 终端核对）")
    ap.add_argument("--names", default="", help="与 codes 一一对应的人读标签，逗号分隔")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--options", default="Fill=Previous")
    ap.add_argument("--gateway-url", default=None,
                    help="覆盖 settings.yaml 的 gateway url（如 ssh 隧道 http://localhost:18080）")
    args = ap.parse_args()

    code_list = [c.strip() for c in args.codes.split(",") if c.strip()]
    name_list = [n.strip() for n in args.names.split(",") if n.strip()]
    if name_list and len(name_list) != len(code_list):
        ap.error(f"--names 数量 ({len(name_list)}) 与 --codes ({len(code_list)}) 不符")
    names = dict(zip(code_list, name_list)) if name_list else {}

    gw = load_gateway_config()
    if args.gateway_url:
        gw = {**gw, "url": args.gateway_url}
    rows = fetch_edb(gw, args.codes, args.start, args.end, args.options)
    n = upsert_edb_daily(rows, names)
    print(f"[backfill_edb] gateway 返回 {len(rows)} 行 → upsert {n} 行"
          f"（{len(code_list)} 条序列, {args.start}..{args.end}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
