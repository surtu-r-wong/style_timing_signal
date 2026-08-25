"""IC/IM 股指期货日线补齐：Wind gateway → market-monitor `public.futures_daily`。

## 为什么需要它（2026-08-25 立）

`futures_daily` **从来没有日更自动化**。写入方是 market-monitor 的
`data-collecter/backfill/backfill_optimized.py` —— 一个交互式、只能在 Windows
桌面会话跑的手动回填脚本（WindPy 直连）。实测最近一次日志 `backfill_20260424`、
表停在 2026-04-29 ⇒ 断更 118 天纯粹是「没人再跑批次」。
本脚本走 gateway + writer API，可无人值守，让 carry 序列不再靠人工。

**生产零依赖**：日更信号链不消费 carry（`backtest.production` → `production_position`
是纯阈值，输入只有信号 CSV）。断更影响的只有 C1 基差率复检与 carry 描述性分析。

## 写入路径（遵 market-monitor 客户端硬规）

不直连 SQL：POST 到 writer 的 `/api/data/daily`，`{"table": "futures_daily",
"data": [...]}`，primary 失败退 fallback。host 从 `config/settings.yaml` 读，
**不硬编码**（market-monitor `CLAUDE.md`「客户端硬规」）。后端按
`(symbol, trade_date)` 唯一键 upsert ⇒ 幂等，重叠区间无害。

## ⚠️ 前置条件（2026-08-25 实测，未满足则本脚本取不到 oi）

gateway 是**哑管道**：每个端点向 Wind 要哪些字段，由 Windows 机上
`wind_gateway/config.yaml` 的 `fetchers.<name>.wsd_fields` 决定。
现有 `/fetch/price` 是**股票口径**（open/high/low/close/volume/amt/turn/adjfactor），
**不含 `oi` / `settle`**，而 carry 的主力合约判定恰恰要 `oi`。

⇒ 需要在网关侧新增一个 futures fetcher（用户在 Windows 机操作）：

    fetchers:
      futures:
        wsd_fields: "open,high,low,close,volume,oi,amt,settle"
        wsd_options: ""

并在 `wind_gateway/endpoints.py` 加对应 `/fetch/futures`（与 `/fetch/price` 同形），
重新部署后 `POST /admin/reload`。在此之前用 `--endpoint /fetch/price` 亦可跑通，
但 **oi/settle 会是空**，carry 无法计算 —— 脚本会显式警告而不是静默落空值。

用法：
    python3 tools/topup_futures_daily.py \\
        --codes "IC2609.CFE,IM2609.CFE" --start 2026-04-30 --end 2026-08-25
    # 合约名单不内置（哑管道原则的客户端侧延伸）；缺口补齐的 16 行清单见
    # docs/plans/2026-08-25-futures-daily-gap-runbook.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from signals.common.config import CONFIG_FILE  # noqa: E402

#: Wind 字段 → `public.futures_daily` 列名。
FIELD_RENAME = {"amt": "turnover", "ts_code": "symbol", "code": "symbol"}

#: 后端 `build_insert_sql` 会强制取用的列 —— 缺了会 KeyError，故补 None 而非省略。
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume", "oi", "turnover", "settle")

#: carry 计算的必要列（`backtest/data.py`：主力合约 = oi 最大者，基差用 close）。
CARRY_COLUMNS = ("close", "oi")


def _no_proxy_session() -> requests.Session:
    """绕开本机 Clash 代理。

    2026-08-25 实测：带代理时 gateway 与两个 writer **全部返回 502**，
    看起来像服务挂了；`env -u HTTP_PROXY ...` 后全部 200。这个假象已让人误判过一次
    （见 `ops-pip-mirror-bypass-proxy` / `ops-tailscale-blackhole-diagnosis`）。
    """
    s = requests.Session()
    s.trust_env = False
    return s


def load_config() -> dict:
    return yaml.safe_load(Path(CONFIG_FILE).read_text(encoding="utf-8")) or {}


def gateway_conf(cfg: dict) -> dict:
    gw = cfg.get("wind_gateway") or {}
    missing = [k for k in ("url", "token") if not gw.get(k)]
    if missing:
        raise ValueError(f"settings.yaml wind_gateway 段缺 {missing}")
    return gw


def writer_conf(cfg: dict) -> dict:
    w = cfg.get("market_monitor_writer") or {}
    missing = [k for k in ("primary_url", "api_key") if not w.get(k)]
    if missing:
        raise ValueError(
            f"settings.yaml market_monitor_writer 段缺 {missing}（参照 settings.example.yaml）")
    return w


def fetch_futures(gw: dict, codes: str, start: str, end: str,
                  endpoint: str = "/fetch/futures", session=None) -> tuple[list[str], list[list]]:
    """→ (列名, 行)。gateway 原样返回 Wind 的列名，重命名放到 `to_records`。"""
    session = session or _no_proxy_session()
    resp = session.get(
        f"{gw['url'].rstrip('/')}{endpoint}",
        params={"codes": codes, "start": start, "end": end},
        headers={"Authorization": f"Bearer {gw['token']}"},
        timeout=180,
    )
    if resp.status_code != 200:
        # 网关把可操作的原因放在 body 里（如 quota_exceeded / Wind ErrorCode）；
        # 裸 raise_for_status 只留一个 429，运维看不出该等配额还是该改配置。
        detail = resp.text[:300]
        raise RuntimeError(f"gateway {resp.status_code}: {detail}")
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"gateway 返回异常: {payload}")
    return payload.get("columns") or payload.get("cols") or [], payload["rows"]


def to_records(columns: list[str], rows: list[list]) -> list[dict]:
    """gateway 行 → writer API 记录。列名小写化 + 重命名 + 补齐后端必需列。"""
    cols = [FIELD_RENAME.get(c.lower(), c.lower()) for c in columns]
    out = []
    for row in rows:
        rec = dict(zip(cols, row))
        for col in REQUIRED_COLUMNS:
            rec.setdefault(col, None)
        out.append(rec)
    return out


def carry_readiness(records: list[dict]) -> dict:
    """carry 能不能算 —— 逐列统计非空率。`oi` 全空 = 网关字段没配对（见模块 docstring）。"""
    n = len(records)
    return {c: sum(1 for r in records if r.get(c) is not None) for c in CARRY_COLUMNS} | {"rows": n}


def push(records: list[dict], w: dict, session=None, batch: int = 500) -> int:
    """POST 到 writer；primary 连接失败才退 fallback（5xx/4xx 是业务错，不退）。"""
    session = session or _no_proxy_session()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {w['api_key']}"}
    sent = 0
    for i in range(0, len(records), batch):
        chunk = records[i:i + batch]
        payload = {"table": "futures_daily", "data": chunk}
        try:
            resp = session.post(w["primary_url"], json=payload, headers=headers, timeout=(5, 60))
        except requests.exceptions.ConnectionError:
            if not w.get("fallback_url"):
                raise
            resp = session.post(w["fallback_url"], json=payload, headers=headers, timeout=(8, 90))
        if resp.status_code != 200:
            raise RuntimeError(f"writer 拒收 [{resp.status_code}]: {resp.text[:200]}")
        sent += len(chunk)
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IC/IM 日线补齐 → public.futures_daily")
    ap.add_argument("--codes", required=True, help="合约码逗号分隔，如 IC2609.CFE,IM2609.CFE")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--endpoint", default="/fetch/futures",
                    help="网关端点；futures fetcher 未配好前可用 /fetch/price（但 oi 会空）")
    ap.add_argument("--gateway-url", default=None, help="覆盖 settings.yaml（如 ssh 隧道）")
    ap.add_argument("--dry-run", action="store_true", help="只取数并体检，不写库")
    args = ap.parse_args(argv)

    cfg = load_config()
    gw = gateway_conf(cfg)
    if args.gateway_url:
        gw = {**gw, "url": args.gateway_url}

    columns, rows = fetch_futures(gw, args.codes, args.start, args.end, args.endpoint)
    records = to_records(columns, rows)
    health = carry_readiness(records)
    print(f"[topup_futures] gateway 返回 {health['rows']} 行，列 = {columns}")
    for col in CARRY_COLUMNS:
        got = health[col]
        flag = "" if got else "   ⛔ 全空 —— carry 算不出，检查网关 fetchers 字段配置"
        print(f"    {col:9s} 非空 {got}/{health['rows']}{flag}")
    if not health["rows"]:
        print("[topup_futures] 无行可写")
        return 0
    if args.dry_run:
        print(f"[topup_futures] --dry-run：不写库。样例 = {records[0]}")
        return 0
    n = push(records, writer_conf(cfg))
    print(f"[topup_futures] 已 POST {n} 行 → public.futures_daily（按 (symbol,trade_date) upsert）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
