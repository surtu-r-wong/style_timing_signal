#!/usr/bin/env bash
# 增量补齐 stock_selector.index_daily 的本项目指数到**闸门认可的最后一个交易日**。
# 前置：Wind gateway 在线。用法：tools/topup_index_daily.sh [START_DATE]
# START 缺省取 14 天前（upsert 幂等，重叠无害，顺带自愈短缺口）。
#
# END 为什么不是 `date +%F`（2026-09-10 事故，判例 tests/test_tools_topup_index_daily.py）：
# 那天 09:07 盘中日更链路跑起来，前置闸门按 15:30 正确判出「应有的最后交易日 = 09-09」
# 并放行补跑（库内当时停在 09-03），而本脚本自己取到 09-10 —— 闸门根本没打算让它取的
# 日子。结果 15 个指数的 09-10 收盘价全是前一日复制的占位值，进了库。那次被事后审计的
# 「前值复制」规则接住，但接住它靠运气：开盘才 7 分钟，Wind 还在返回前一日收盘；同样的
# 补跑发生在 10:30，Wind 返回的是实时价，既不等于前值也是个正常数字，事后审计与同族
# 共动性哨兵全都抓不到。**闸门与执行体必须同源**，所以 END 直接问闸门要。
# 闸门取不到（退出非 0）→ `set -e` 让本脚本一并失败，即零写入（fail-closed）。
#
# TOPUP_DRY_RUN=1：只打印将要补的区间后退出，不联网、不写库。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${STYLE_SIGNALS_PYTHON:-python3}"
CODES="CI005917.WI,CI005918.WI,CI005919.WI,CI005920.WI,CI005921.WI,000918.CSI,000919.CSI,H30351.CSI,H30352.CSI,932406.CSI,932407.CSI,932408.CSI,932409.CSI,932000.CSI,000300.SH"
START="${1:-$(date -d '14 days ago' +%F)}"
END="$("${PYTHON}" "${REPO}/deploy/daily_signals/topup_guard.py" --mode last-trading-day)"

if [[ -n "${TOPUP_DRY_RUN:-}" ]]; then
  echo "DRY_RUN: --start ${START} --end ${END}（不联网、不写库）"
  exit 0
fi

cd /home/elfbob/claude-code/stock_selector
exec .venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily --tickers "$CODES" --start "$START" --end "$END" --source wind
