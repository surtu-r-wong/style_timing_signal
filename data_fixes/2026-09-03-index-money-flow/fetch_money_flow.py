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

# index_code → Wind 板块代码。2026-09-03 探针实测：marketmoneyflows 的 sector 是白名单枚举，
# 只支持 沪深300 / 创业板 / 科创板（csi_300 / chinext / star，中英文两套写法结论一致；
# 中证500/1000/2000、上证50、全部A股 等 50 个候选全部 -40521008）。index_code 取该板块
# 对应的收益率指数代码，便于与 index_daily 直接对齐。
DEFAULT_SECTORS = {
    "000300.SH": "csi_300",    # 沪深300
    "399102.SZ": "chinext",    # 创业板综（全体创业板股票，与板块口径一致；创业板指 399006 只有 100 只）
    "000680.SH": "star",       # 科创综指
}
# 数据起点实测：csi_300 / chinext 均自 2015-01-05；star 自 2019-07-22（开板日）。2014 全年 0 行。
DATA_START = {"000300.SH": 2015, "399102.SZ": 2015, "000680.SH": 2019}
# 只数上界（当日有资金流的股票数；停牌股不计入，故只有上界是硬的）。板块股票数随 IPO 增长，
# 不设固定期望值，改由 load_to_db.py 用「日环比变动 ≤5%」查截断。
COUNT_CAP = {"000300.SH": 300}
# Wind wset 静默截断上限（实测整年请求只回 62~66 行）。取窗必须安全低于它。
TRUNC_CAP = 60


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
    """通路验证：各板块取最近 2 天，记行数与只数（sector 代码已由 2026-09-03 探针定案）。"""
    end = date.today().isoformat()
    start = "2026-08-31"
    ok = 0
    sectors = {**DEFAULT_SECTORS, **overrides}
    for idx, sec in sectors.items():
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
        cap = COUNT_CAP.get(idx)
        verdict = "OK" if (cap is None or max(sums) <= cap) else f"OVER_CAP({cap})"
        log(f"PROBE {idx} sector={sec} rows={len(rows)} count={sums} {verdict} cells={len(rows)*len(cols)}")
        if verdict == "OK":
            ok += 1
    return ok


def trading_calendar() -> list[date]:
    """A 股交易日历（index_daily 的 000300.SH 日期集合，2014-01-02 起连续）。"""
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from backtest.data import _connect, load_db_config  # noqa: PLC0415
    cfg = load_db_config()
    with _connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT trade_date FROM {cfg['schema']}.index_daily "
                    "WHERE index_code = '000300.SH' ORDER BY trade_date")
        return [r[0] for r in cur.fetchall()]


def windows(first: date, last: date, months: int):
    """按 months 个自然月切窗。Wind wset 静默截断到最后约 3 个月（2026-09-03 实测：
    整年请求只回 62~66 行 = 窗口末尾一个季度），故必须分块并逐块校验覆盖。"""
    y, m = first.year, first.month
    while date(y, m, 1) <= last:
        start = max(first, date(y, m, 1))
        m2, y2 = m + months, y
        while m2 > 12:
            m2 -= 12; y2 += 1
        end = min(last, date(y2, m2, 1) - __import__("datetime").timedelta(days=1))
        yield start, end
        y, m = y2, m2


def pull(s, url, token, sectors: dict[str, str], y0: int, y1: int, months: int = 2) -> None:
    RAW.mkdir(exist_ok=True)
    cal = trading_calendar()
    cal_max = cal[-1]
    total_cells = 0
    today = date.today()
    for idx, sec in sectors.items():
        first = date(max(y0, DATA_START.get(idx, y0)), 1, 1)
        last = min(date(y1, 12, 31), today)
        for w0, w1 in windows(first, last, months):
            out = RAW / f"{idx}_{w0:%Y%m}.csv"
            if out.exists():
                continue
            exp = [d for d in cal if w0 <= d <= w1]
            for attempt in range(3):
                try:
                    cols, rows = fetch(s, url, token, sec, w0.isoformat(), w1.isoformat())
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"{idx} {w0:%Y-%m} attempt {attempt+1} ERR {e}")
                    time.sleep(10 * (attempt + 1))
            else:
                log(f"{idx} {w0:%Y-%m} GIVE UP"); continue
            if not rows:
                # 板块尚未开板 / 该窗无数据：留空标记文件，避免重跑时反复请求
                out.write_text("index_code,wind_sector\n", encoding="utf-8")
                log(f"{idx} {w0:%Y-%m}..{w1:%Y-%m} 0 rows（期望 {len(exp)} 个交易日）")
                time.sleep(0.6)
                continue
            di = cols.index("date")
            got = sorted({r[di][:10] for r in rows})
            if exp and got:
                # 覆盖闸。Wind 静默截断到窗口末尾约 62~66 行（2026-09-03 实测），截断总是丢前段。
                # 只要窗口交易日数安全低于该上限，前段缺失就只能是真实无数据（如科创板 2019-07-22
                # 才开板），不能判截断；窗口一旦逼近上限就必须硬失败，否则无法与截断区分。
                if len(exp) >= TRUNC_CAP:
                    log(f"{idx} {w0:%Y-%m} 窗口 {len(exp)} 个交易日 ≥ 截断上限 {TRUNC_CAP} — 中止")
                    raise SystemExit(f"窗口过大：{idx} {w0}~{w1}，请减小 --chunk-months")
                miss = [d for d in exp if d.isoformat() not in set(got)]
                if w1 <= cal_max and miss:
                    log(f"{idx} {w0:%Y-%m} 缺 {len(miss)}/{len(exp)} 个交易日 "
                        f"({miss[0]}..{miss[-1]})")
            cells = len(rows) * len(cols); total_cells += cells
            with out.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(["index_code", "wind_sector", *cols])
                for r in rows:
                    w.writerow([idx, sec, *r])
            log(f"{idx} {w0:%Y-%m}..{w1:%Y-%m} sector={sec} rows={len(rows)}/{len(exp)} cells={cells}")
            time.sleep(0.6)
    log(f"DONE total_cells={total_cells}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--start", type=int, default=2014)
    p.add_argument("--end", type=int, default=date.today().year)
    p.add_argument("--sector", action="append", default=[], help="INDEX=SECTOR 覆盖，可重复")
    p.add_argument("--chunk-months", type=int, default=2)
    p.add_argument("--only", default="", help="逗号分隔 index_code 子集")
    a = p.parse_args(argv)
    overrides = dict(x.split("=", 1) for x in a.sector)
    sectors = {**DEFAULT_SECTORS, **overrides}
    if a.only:
        sectors = {k: v for k, v in sectors.items() if k in a.only.split(",")}
    url, token = gateway(); s = session()
    if a.probe:
        n = probe(s, url, token, overrides)
        log(f"PROBE ok {n}/{len(sectors)}")
        return 0 if n == len(sectors) else 1
    pull(s, url, token, sectors, a.start, a.end, a.chunk_months)
    return 0


if __name__ == "__main__":
    sys.exit(main())
