"""同族共动性哨兵 —— 抓 `index_daily` 的两类源头缺陷（现有护栏抓不到的）。

## 为什么需要它（两起真实事故，现有四条审计规则都漏了）

1. **批次缝 level shift（2026-06-16）**：932408 单日 +16.02%、932409 −11.05%，
   而母指数 000852 只 +1.47%。现有规则 3 只抓「**所有**指数与前值逐一相等」的
   前值复制占位日，抓不到「个别指数跳飞」。
2. **腿滞后（2026-08-03，至今未修正；2026-08-06 同型已于 08-20 修复）**：
   五条风格腿收盘价与前一交易日**逐位相同**，而沪深300 当天动 −0.98%。
   同样逃过规则 3（母指数正常，不是全体相等）。

两者是同一个原语的两面：**同族序列应当共动**。跳飞 = 动得离谱；滞后 = 该动不动。

## 判别式与阈值标定（全部实测，非拍脑袋）

### 规则 5：对内价差（`|r_成长腿 − r_价值腿|`）

同一市值带的成长/价值腿由构造高度相关，价差是极干净的判别式。
全库标定（2014-01 → 2026-08，3,073 交易日 × 4 对）：

| 分位 | 300对 | 500对 | 1000对 | 2000对 |
|---|---|---|---|---|
| p99 | 3.21 | 2.99 | 3.28 | 2.62 |
| p99.9 | 5.30 | 4.95 | 6.17 | 4.69 |

**> 8pp 的历史事件只有 2026-06-16 的两条**（2000对 27.07pp、1000对 9.82pp），
第三名骤降到 7.45pp → `CRITICAL = 8pp` 在 12.6 年里零误报且抓住已知事故。
`WARN = 6pp` 共 7 次（约 1.8 年/次），供人工复核。

**为什么不用「偏离全市场中位」**：实测会误伤天生高波动序列——万得微盘
（`8841431.WI`）在 2024-02 踩踏与 04 国九条期间偏离中位 8~12pp，那是**真实行情**；
按序列自身尺度归一（z 分数）也分不开（真实行情 z 达 32，事故 z 45，同量级）。
对内价差则直接利用「两腿必然共动」这一构造性约束，干净得多。

### 规则 6：腿冻结（收盘价与前值逐位相等，而同族在动）

全库标定（2013-03 → 2026-08）：腿收益恰为 0 的总次数 = **6**；其中「同族中位
|r| > 0.5%」的事件日 = **1**（2026-08-03，5 条腿同时冻结）。故本规则在 13.5 年里
只会响一次——正是那次真事故。`MARKET_MOVE_MIN = 0.005`。

⚠️ 浮点相等是**有意为之**：真实收盘价连续两日逐位相同的概率极低，而
「上游把前一日的值又送了一遍」必然逐位相同。用容差反而会误伤真实小波动。

## 用法

    # 并入日更链（topup_guard audit 会调用本模块的纯函数，见 audit_changes 规则 5/6）
    # 全历史回扫（定期体检 / 事后取证）：
    python3 deploy/daily_signals/family_sentinel.py --mode scan [--since 2024-01-01]
        exit 0 → 干净；exit 1 → 发现可疑日（stdout 逐条）；exit 2 → 自身出错
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EXIT_CLEAN = 0
EXIT_SUSPECT = 1
EXIT_ERROR = 2

#: 生产信号的四对成长/价值腿（与 `signals.style_basket.validate.INDEX_PAIRS` 同源）。
PAIRS = {
    "300pair": ("000918.CSI", "000919.CSI"),
    "500pair": ("H30351.CSI", "H30352.CSI"),
    "1000pair": ("932407.CSI", "932406.CSI"),
    "2000pair": ("932409.CSI", "932408.CSI"),
}

PAIR_SPREAD_CRITICAL = 0.08     # 12.6 年零误报，抓住 2026-06-16
PAIR_SPREAD_WARN = 0.06         # 约 1.8 年/次，人工复核
MARKET_MOVE_MIN = 0.005         # 腿冻结规则的「同族确实在动」门槛
MIN_REFERENCE_CODES = 3         # 少于此数无法构造可信的同族参照 → 不判


# ─────────────────────────── 纯函数（可单测，不连库） ───────────────────────────
def returns_by_day(closes: dict[str, dict[str, float | None]]
                   ) -> dict[str, dict[str, float]]:
    """收盘价字典 → {日期: {代码: 日收益}}（按各代码自身的相邻可用日计算）。

    `closes` 结构同 `topup_guard.take_snapshot`：{code: {"YYYY-MM-DD": float|None}}。
    非有限值、前值 ≤ 0 的日期直接跳过（不产生该代码当日的收益）。
    """
    out: dict[str, dict[str, float]] = {}
    for code, series in closes.items():
        days = sorted(d for d, v in series.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)
                      and math.isfinite(v))
        for prev, cur in zip(days, days[1:]):
            p, c = series[prev], series[cur]
            if p is None or p <= 0 or c is None:
                continue
            out.setdefault(cur, {})[code] = c / p - 1.0
    return out


def pair_spread_findings(day_returns: dict[str, float],
                         pairs: dict[str, tuple[str, str]] = PAIRS,
                         warn: float = PAIR_SPREAD_WARN,
                         critical: float = PAIR_SPREAD_CRITICAL) -> list[str]:
    """规则 5：某日各对的 `|r_成长 − r_价值|` 超阈值 → findings（空 = 干净）。"""
    found = []
    for name, (growth, value) in sorted(pairs.items()):
        rg, rv = day_returns.get(growth), day_returns.get(value)
        if rg is None or rv is None:
            continue
        spread = abs(rg - rv)
        if spread >= critical:
            found.append(f"CRITICAL {name} 对内价差 {spread * 100:.2f}pp "
                         f"（{growth} {rg * 100:+.2f}% vs {value} {rv * 100:+.2f}%，"
                         f"判据 ≥{critical * 100:.0f}pp）")
        elif spread >= warn:
            found.append(f"WARN {name} 对内价差 {spread * 100:.2f}pp "
                         f"（判据 ≥{warn * 100:.0f}pp，需人工复核）")
    return found


def frozen_leg_findings(day_returns: dict[str, float],
                        legs: tuple[str, ...] | None = None,
                        market_move_min: float = MARKET_MOVE_MIN,
                        min_reference: int = MIN_REFERENCE_CODES) -> list[str]:
    """规则 6：收益恰为 0（收盘价与前值逐位相等）而同族确实在动 → findings。

    `legs=None` 时检查**当日全部代码**，不只四对腿——2026-08-03 实测冻结 8 个码
    （5 条腿 + `885000.WI` 万得全A + `H00922.CSI` + `H11021.CSI`），只盯腿会低报。

    同族动静 = **非冻结**代码的 |r| 中位。用非冻结子集做参照，避免大批冻结时
    参照自身被拖到 0 而漏报。
    """
    candidates = legs if legs is not None else tuple(day_returns)
    frozen = sorted(c for c in candidates if day_returns.get(c) == 0.0)
    if not frozen:
        return []
    reference = sorted(abs(r) for c, r in day_returns.items() if r != 0.0)
    if len(reference) < min_reference:
        return []
    median = reference[len(reference) // 2] if len(reference) % 2 else \
        (reference[len(reference) // 2 - 1] + reference[len(reference) // 2]) / 2
    if median < market_move_min:
        return []
    return [f"CRITICAL 序列冻结：{len(frozen)} 个代码收盘价与前值逐位相等 "
            f"（{', '.join(frozen)}），而同族中位 |r| = {median * 100:.2f}% "
            f"（判据 >{market_move_min * 100:.1f}%）"]


def scan_findings(closes: dict[str, dict[str, float | None]],
                  only_days: set[str] | None = None) -> dict[str, list[str]]:
    """对收盘价字典全窗（或指定日集）跑两条规则 → {日期: findings}（只含非空）。"""
    out = {}
    for day, day_returns in returns_by_day(closes).items():
        if only_days is not None and day not in only_days:
            continue
        found = pair_spread_findings(day_returns) + frozen_leg_findings(day_returns)
        if found:
            out[day] = found
    return out


# ─────────────────────────────── 只读 IO ───────────────────────────────
def load_closes(since: str | None = None,
                all_codes: bool = False) -> dict[str, dict[str, float | None]]:
    """只读 PG：收盘价（`take_snapshot` 同源码表，不同窗口）。

    默认只取**本项目 19 个输入码**（护栏该保护的范围）。`all_codes=True` 取全表——
    用于事后取证：2026-08-03 实测本项目输入内冻结 5 个，全表另有 `885000.WI`
    （万得全A）、`H00922.CSI`、`H11021.CSI` 三个同样冻结，共 8 个。
    """
    import psycopg2

    from signals.common.config import load_db_config
    from signals.common.data_source import load_code_map

    codes = None if all_codes else sorted(set(load_code_map().values()))
    db = load_db_config()
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["name"],
        user=db["user"], password=db["password"], connect_timeout=10,
        options="-c statement_timeout=120000",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT index_code, trade_date, close FROM {db['schema']}.index_daily "
                "WHERE (%s::text[] IS NULL OR index_code = ANY(%s)) "
                "AND (%s::date IS NULL OR trade_date >= %s::date)",
                (codes, codes, since, since),
            )
            closes: dict[str, dict[str, float | None]] = {}
            for code, day, close in cur.fetchall():
                closes.setdefault(code, {})[day.isoformat()] = (
                    None if close is None else float(close))
    finally:
        conn.close()
    return closes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同族共动性哨兵（对内价差 + 腿冻结）")
    ap.add_argument("--mode", choices=["scan"], default="scan")
    ap.add_argument("--since", default=None, help="只扫该日期起（默认全历史）")
    ap.add_argument("--all-codes", action="store_true",
                    help="扫 index_daily 全表而非本项目 19 个输入码（事后取证用）")
    args = ap.parse_args(argv)

    try:
        closes = load_closes(args.since, all_codes=args.all_codes)
    except Exception as exc:                                  # noqa: BLE001
        print(f"SENTINEL_ERROR: 取数失败 {exc}")
        return EXIT_ERROR

    findings = scan_findings(closes)
    n_days = len({d for s in closes.values() for d in s})
    if not findings:
        print(f"CLEAN：{n_days} 个交易日无可疑")
        return EXIT_CLEAN
    print(f"发现 {len(findings)} 个可疑日 / {n_days} 个交易日：")
    for day in sorted(findings):
        for line in findings[day]:
            print(f"  {day}  {line}")
    return EXIT_SUSPECT


if __name__ == "__main__":
    raise SystemExit(main())
