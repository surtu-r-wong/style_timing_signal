#!/usr/bin/env bash
# 日更信号链失败时的告警器（由 style-signals-daily.service 的 OnFailure= 拉起）。
#
# 存在理由：本链路在两种情况下会**主动中止**（topup 事后审计判可疑 / 无法验证），
# 中止本身是安全设计，但「中止只有被人知道才安全」——否则又变成一次无人发现的停摆，
# 正是本项目 2026-07-09~08-12 停更 35 天的老毛病。
#
# 动作（都不许失败传播，告警器自己绝不能成为新的失败源）：
#   1. 写显眼告警文件 logs/ALERT_daily_signals（含时间 + status.json 摘要 + 日志路径）
#   2. best-effort 桌面通知 notify-send（无图形会话时静默跳过）
#
# 告警文件不会自动清除，下一次成功运行也不清——留给人处置后手动 rm，
# 免得夜里失败、白天自愈、没人看见。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOG_DIR="${REPO}/logs"
STATUS_FILE="${LOG_DIR}/daily_signals_status.json"
ALERT_FILE="${LOG_DIR}/ALERT_daily_signals"
NOW="$(date '+%F %T %Z')"

mkdir -p "${LOG_DIR}"

{
  echo "════════════════════════════════════════════════════════════"
  echo "  style_timing_signal 日更信号链失败告警"
  echo "  时间: ${NOW}"
  echo "════════════════════════════════════════════════════════════"
  echo
  echo "[systemd]"
  systemctl --user show style-signals-daily.service \
      -p Result -p ExecMainStatus -p InvocationID 2>/dev/null \
      || echo "  (取不到 systemd 状态)"
  echo
  echo "[状态文件 ${STATUS_FILE}]"
  if [[ -f "${STATUS_FILE}" ]]; then
    python3 - "${STATUS_FILE}" <<'PY' 2>/dev/null || cat "${STATUS_FILE}"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"  result       = {d.get('result')}")
print(f"  failed_step  = {d.get('failed_step')}")
print(f"  topup        = {d.get('topup')}")
print(f"  topup_reason = {d.get('topup_reason')}")
print(f"  started_at   = {d.get('started_at')}")
print(f"  finished_at  = {d.get('finished_at')}")
up = d.get("upstream") or {}
if up:
    print(f"  upstream     = max_trade_date={up.get('max_trade_date')} "
          f"距今 {up.get('calendar_days_behind_today')} 自然日")
for b in (d.get("breaches") or []):
    print(f"  BREACH       {b}")
if d.get("upstream_breach"):
    print(f"  UPSTREAM     {d['upstream_breach']}")
PY
  else
    echo "  (状态文件不存在——链路可能在写状态之前就死了)"
  fi
  echo
  echo "[运行日志]"
  echo "  ${LOG_DIR}/daily_signals_$(date +%Y%m%d).log"
  echo "  journalctl --user -u style-signals-daily.service -n 80"
  echo
  echo "[处置]"
  echo "  1. 看上面的 result / failed_step 定位"
  echo "  2. TOPUP_VERIFY_FAILED = 写入无法验证，重跑审计即可"
  echo "  3. SUSPECT = 先判是否上游合法回溯修订，再决定是否置 SKIP_TOPUP"
  echo "  4. 处理完手动删除本文件: rm ${ALERT_FILE}"
} > "${ALERT_FILE}" 2>&1

# 桌面通知：best-effort，没有图形会话就算了
if command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical \
      "style_timing_signal 日更链失败" \
      "${NOW}｜详见 ${ALERT_FILE}" >/dev/null 2>&1 || true
fi

echo "[alert] 已写 ${ALERT_FILE}"
exit 0
