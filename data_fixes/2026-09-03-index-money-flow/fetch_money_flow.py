"""指数板块级资金流向（Wind wset marketmoneyflows）经网关取回 → raw/<index>_<year>.csv。

用法：
  python fetch_money_flow.py --probe                 # 各指数 sector 代码探针（2 天窗，验只数 = 成分数）
  python fetch_money_flow.py --start 2014 --end 2026 # 逐指数逐年取，已存在的 (index, year) 跳过
  python fetch_money_flow.py --sector 932000.CSI=csi_2000   # 覆盖默认映射

网关端点 /fetch/market_money_flow（stock_selector feat/index-money-flow 2e31f2e），
sector 是 Wind 板块代码原文。走无代理 session（Clash 会把内网服务打成假 502）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
LOG = HERE / "fetch.log"

# index_code → Wind 板块代码。csi_300 由用户给定；其余三个是探针候选，--probe 定案后写回这里。
DEFAULT_SECTORS = {
    "000300.SH": "csi_300",
    "000905.SH": "csi_500",
    "000852.SH": "csi_1000",
    "932000.CSI": "csi_2000",
}
EXPECTED_COUNT = {"000300.SH": 300, "000905.SH": 500, "000852.SH": 1000, "932000.CSI": 2000}
PROBE_CANDIDATES = {
    "000905.SH": ["csi_500", "csi500", "zz500"],
    "000852.SH": ["csi_1000", "csi1000", "zz1000"],
    "932000.CSI": ["csi_2000", "csi2000", "zz2000"],
}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def gateway() -> tuple[str, str]:
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    gw = cfg["wind_gateway"]
    return gw["url"].rstrip("/"), gw["token"]


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def fetch(s: requests.Session, url: str, token: str, sector: str, start: str, end: str) -> tuple[list[str], list[list]]:
    r = s.get(f"{url}/fetch/market_money_flow",
              params={"sector": sector, "start": start, "end": end},
              headers={"Authorization": f"Bearer {token}"}, timeout=(10, 300))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError(f"gateway status={j.get('status')}: {json.dumps(j)[:300]}")
    return j["columns"], j["rows"]


def probe(s, url, token, overrides: dict[str, str]) -> int:
    end = date.today().isoformat()
    start = "2026-08-31"
    ok = 0
    for idx, exp in EXPECTED_COUNT.items():
        cands = [overrides[idx]] if idx in overrides else ([DEFAULT_SECTORS[idx]] if idx == "000300.SH" else PROBE_CANDIDATES[idx])
        for sec in cands:
            try:
                cols, rows = fetch(s, url, token, sec, start, end)
            except Exception as e:  # noqa: BLE001
                log(f"PROBE {idx} sector={sec} ERR {e}")
                continue
            if not rows:
                log(f"PROBE {idx} sector={sec} 0 rows cols={cols}")
                continue
            ci, co = cols.index("maininflowcount"), cols.index("mainoutflowcount")
            sums = [int(r[ci] or 0) + int(r[co] or 0) for r in rows]
            verdict = "MATCH" if all(abs(x - exp) <= 5 for x in sums) else "MISMATCH"
            log(f"PROBE {idx} sector={sec} rows={len(rows)} count_sum={sums} expected={exp} {verdict} cells={len(rows)*len(cols)}")
            if verdict == "MATCH":
                ok += 1
                break
    return ok


def pull(s, url, token, sectors: dict[str, str], y0: int, y1: int) -> None:
    RAW.mkdir(exist_ok=True)
    total_cells = 0
    for idx, sec in sectors.items():
        for y in range(y0, y1 + 1):
            out = RAW / f"{idx}_{y}.csv"
            if out.exists():
                continue
            start, end = f"{y}-01-01", f"{y}-12-31" if y < date.today().year else date.today().isoformat()
            for attempt in range(3):
                try:
                    cols, rows = fetch(s, url, token, sec, start, end)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"{idx} {y} attempt {attempt+1} ERR {e}")
                    time.sleep(10 * (attempt + 1))
            else:
                log(f"{idx} {y} GIVE UP"); continue
            cells = len(rows) * len(cols); total_cells += cells
            with out.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(["index_code", "wind_sector", *cols])
                for r in rows:
                    w.writerow([idx, sec, *r])
            log(f"{idx} {y} sector={sec} rows={len(rows)} cells={cells} → {out.name}")
            time.sleep(1)
    log(f"DONE total_cells={total_cells}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--start", type=int, default=2014)
    p.add_argument("--end", type=int, default=date.today().year)
    p.add_argument("--sector", action="append", default=[], help="INDEX=SECTOR 覆盖，可重复")
    p.add_argument("--only", default="", help="逗号分隔 index_code 子集")
    a = p.parse_args(argv)
    overrides = dict(x.split("=", 1) for x in a.sector)
    sectors = {**DEFAULT_SECTORS, **overrides}
    if a.only:
        sectors = {k: v for k, v in sectors.items() if k in a.only.split(",")}
    url, token = gateway(); s = session()
    if a.probe:
        n = probe(s, url, token, overrides)
        log(f"PROBE matched {n}/{len(EXPECTED_COUNT)}")
        return 0 if n == len(EXPECTED_COUNT) else 1
    pull(s, url, token, sectors, a.start, a.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
