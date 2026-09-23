#!/usr/bin/env bash
# 日更信号链失败时的告警器（由 style-signals-daily.service 的 OnFailure= 拉起）。
#
# 存在理由：本链路在两种情况下会**主动中止**（topup 事后审计判可疑 / 无法验证），
# 中止本身是安全设计，但「中止只有被人知道才安全」——否则又变成一次无人发现的停摆，
# 正是本项目 2026-07-09~08-12 停更 35 天的老毛病。
#
# 动作（都不许失败传播，告警器自己绝不能成为新的失败源）：
#   1. 写显眼告警文件 logs/ALERT_daily_signals（含时间 + status.json 摘要 + 日志路径）
#   2. best-effort 企业微信失败通知（notify_wechat.py --alert；推了什么/为何没推追加进告警文件）
#   3. best-effort 桌面通知 notify-send（限时 10s；无图形会话时静默跳过）
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
print(f"  notify       = {d.get('notify')}")
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
  echo "  4. 推送失败（日志 NOTIFY_FAILED）= 信号与护栏都已完成、只是没送达。看上方 notify 行与运行日志"
  echo "     （超时 124/137 时 notify 为空，只能看日志）。担保校验不过的 REFUSED 补发同样会拒推，须重跑"
  echo "     整条链路；其余情况（网络 / webhook，含没配 webhook）修好后补发:"
  echo "     python3 ${SCRIPT_DIR}/notify_wechat.py"
  echo "  5. 处理完手动删除本文件: rm ${ALERT_FILE}"
} > "${ALERT_FILE}" 2>&1

# 企业微信失败通知：best-effort。拼消息与发送都在 notify_wechat.py --alert（只读状态文件、
# 不 import pandas——科学栈坏了也要能报警）；输出追加进告警文件，留底推了什么、为何没推。
# --run-started 是主 service 本次的启动时刻（@unix 秒）：状态文件的 finished_at 早于它，
# 就是上一次运行留下的（本次在写状态前就死了，如超时/OOM），通知不会把旧原因安到这次头上。
# STYLE_SIGNALS_NOTIFY_ARGS 与 runner 同一个变量，手工演练告警器时可带 --dry-run；它排在固定参数
# 之前（argparse 同名取最后一个，固定参数永远生效）。-u：报错行与正文在告警文件里按真实顺序交错。
# 限时 30s、再宽限 5s 强杀：告警单元 TimeoutStartSec=60，还要给后面的 notify-send 留出时间。
PYTHON="${STYLE_SIGNALS_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  for cand in "${REPO}/.venv/bin/python3" /home/elfbob/miniconda3/bin/python3; do
    [[ -x "${cand}" ]] && { PYTHON="${cand}"; break; }
  done
fi
SYSTEMD_RESULT="$(systemctl --user show style-signals-daily.service -p Result --value 2>/dev/null || true)"
RUN_STARTED="$(systemctl --user show style-signals-daily.service -p ExecMainStartTimestamp --value --timestamp=unix 2>/dev/null || true)"
{
  echo
  echo "[企业微信失败通知]"
  # shellcheck disable=SC2086
  timeout -k 5 30 "${PYTHON:-python3}" -u "${SCRIPT_DIR}/notify_wechat.py" ${STYLE_SIGNALS_NOTIFY_ARGS:-} \
      --alert --status-file "${STATUS_FILE}" --systemd-result "${SYSTEMD_RESULT}" \
      --run-started "${RUN_STARTED}" \
      || echo "  (未送达，exit $?)"
} >> "${ALERT_FILE}" 2>&1

# 桌面通知：best-effort，没有图形会话就算了。限时 10s：卡住的话 systemd 会在 TimeoutStartSec
# 把整个告警器杀掉。
if command -v notify-send >/dev/null 2>&1; then
  timeout 10 notify-send -u critical \
      "style_timing_signal 日更链失败" \
      "${NOW}｜详见 ${ALERT_FILE}" >/dev/null 2>&1 || true
fi

echo "[alert] 已写 ${ALERT_FILE}"
exit 0
