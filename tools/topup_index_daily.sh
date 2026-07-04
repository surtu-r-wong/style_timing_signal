#!/usr/bin/env bash
# 增量补齐 stock_selector.index_daily 的本项目指数到今天。
# 前置：Wind gateway 在线。用法：tools/topup_index_daily.sh [START_DATE]
# START 缺省取 14 天前（upsert 幂等，重叠无害，顺带自愈短缺口）。
set -euo pipefail
CODES="CI005917.WI,CI005918.WI,CI005919.WI,CI005920.WI,CI005921.WI,000918.CSI,000919.CSI,H30351.CSI,H30352.CSI,932406.CSI,932407.CSI,932408.CSI,932409.CSI,932000.CSI,000300.SH"
START="${1:-$(date -d '14 days ago' +%F)}"
END="$(date +%F)"
cd /home/elfbob/claude-code/stock_selector
exec .venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily --tickers "$CODES" --start "$START" --end "$END" --source wind
