"""横截面验收：`official_sample_space` vs 932000 官方真实成分的重合率。

改进循环的固定验收工具（独立于 ρ 的证据），每次动选样逻辑后跑一次。
台账（2026-08-19，2026-06 期）：旧排名带代理 63.4% → 官方选样 T1-T8 76.9%
→ +联动剔除 80.0% → +缓冲区语义校正 **86.1%**。
证伪记录：老样本成交额免筛 83.9% / 95% 线 84.4%（均净负，已回滚）。

用法：python backtest/oss_xsection_check.py [生效日，默认 2026-06-15]
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import (  # noqa: E402
    _conn, _official_2000_members, official_sample_space, review_cutoff,
)


def run(eff: pd.Timestamp) -> float:
    cutoff = review_cutoff(eff)
    prev = _official_2000_members(cutoff)
    print(f"生效日 {eff.date()} → 考察截止 {cutoff.date()}；prev="
          f"{len(prev) if prev else None}（官方上期名单）", flush=True)
    mine = set(official_sample_space(eff, prev, verbose=True))

    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code FROM {s}.index_constituent
                            WHERE index_code='932000.CSI' AND effective_date =
                              (SELECT min(effective_date) FROM {s}.index_constituent
                               WHERE index_code='932000.CSI'
                                 AND effective_date > DATE '{eff.date()}')""")
            official = {r[0] for r in cur.fetchall()}
    finally:
        c.close()
    if not official:
        print("⚠️ 库内无该期官方 932000 成分（覆盖 2026-02 起）—— 无法验收", flush=True)
        return float("nan")

    inter = mine & official
    rate = len(inter) / len(official)
    miss = official - mine
    print(f"我方 {len(mine)} / 官方 {len(official)} / 交集 {len(inter)} → "
          f"重合率 {rate:.1%}（基线 63.4%，当前台账 86.1%）")
    print(f"官方有我没有 {len(miss)} 只（.BJ {sum(1 for x in miss if x.endswith('.BJ'))}）")
    return rate


if __name__ == "__main__":
    eff = pd.Timestamp(sys.argv[1]) if len(sys.argv) > 1 else pd.Timestamp("2026-06-15")
    run(eff)
