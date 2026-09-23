#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# style_timing_signal 日更信号链 runner
#
# 链路（顺序即 README「运行」段的顺序）：
#   0. 输入指数 —— 两种模式，由标志文件 deploy/daily_signals/SKIP_TOPUP 切换：
#      【办公室模式】标志文件在（2026-09-23 起的常态）：15 个输入指数由 data_manager 夜间作业
#        （WSL2 20:00 起跑，约 20:02 结束）写入，本链路**只读、零写入**。跑 wait_for_inputs.py 等
#        期望信号日的 15 码到齐（每 5 分钟查一次，最迟等到 21:30），到齐后过同族共动性哨兵；结果记
#        OFFICE_OK / OFFICE_LATE / OFFICE_CHECK_ERROR（照常往下走，用库内已有数据照算，新鲜度由步骤 7
#        护栏兜底），哨兵 CRITICAL 记 OFFICE_SUSPECT 并**在信号重算之前中止**
#      【topup 模式】标志文件不在（回退用；2026-09-23 前的做法）：保鲜上游 index_daily。
#        三层保护，原则=不可信响应零写入：
#        0a 环境变量 STYLE_SIGNALS_SKIP_TOPUP=1 即跳过（不等数），理由记进日志与 status.json
#        0b 前置闸门 topup_guard.py --mode preflight（只读 gateway + PG）：
#           不过就**不调用** topup —— 不调用即零写入，链路降级继续
#        0c tools/topup_index_daily.sh（唯一可能写 index_daily 的地方）
#        0d 事后审计 topup_guard.py --mode audit（只读 PG）：不过则**在信号重算之前
#           中止**，可疑数据进不了 committed 信号 CSV
#   1. signals/hybrid20/update_growth_stability.py
#   2. signals/hybrid20/update_confirmed_signal.py
#   3. signals/citic40d/generate_signal.py
#   4. signals/equal_weight/generate_signal.py                    （变体A / 生产口径）
#   5. signals/equal_weight/generate_signal.py --lookback 5 …     （变体B / 参考口径）
#   5b. signals/slope20/generate_signal.py                        （2026-09-09 第四条生产线）
#   6. python -m backtest.production        —— 各生产线推荐持仓（口径见 PRODUCTION_MAPPING）+ 参照/现货池文件
#   7. deploy/daily_signals/check_freshness.py —— 新鲜度护栏 + 状态文件
#   8. deploy/daily_signals/notify_wechat.py   —— 企业微信推送（护栏通过才推；失败 → 非零退出）
#
# 语义要点：
#   * 步骤 0 允许失败/跳过/迟到（记 OFFICE_LATE / OFFICE_CHECK_ERROR / DEGRADED / TOPUP_SKIPPED，
#     用库内现有数据继续）；例外是输入可能已脏：办公室模式下同族哨兵 CRITICAL（OFFICE_SUSPECT）、
#     topup 模式下事后审计不过（TOPUP_SUSPECT）——必须停在信号之前。状态文件里步骤 0 的字段仍叫
#     topup（告警器与推送按它读）。
#     步骤 1-8 任一失败即整链非零退出。
#   * 步骤 8 失败（日志 NOTIFY_FAILED）时信号与护栏都已完成、只是没送达：状态文件 result
#     仍是 OK，原因记在它的 notify 段；照样退出 1，交 OnFailure 告警器——没送达等于没人知道。
#   * 各生成脚本都是**全量重算覆写**（非追加），因此断更多日后直接跑即完成补跑。
#   * flock 并发锁：已有实例在跑时直接退出 75（EX_TEMPFAIL），不排队；service 配了
#     SuccessExitStatus=75，撞锁不触发告警（占锁的那个实例会推送）。75 只属于这里：
#     步骤自己退出 75 时 fail() 改报 1，免得失败被记成成功。
#   * 护栏未过 → 退出 1 并在日志里打大写 STALE；这是「停更无人知」的直接对策。
#
# 环境变量：
#   STYLE_SIGNALS_PYTHON      python 解释器绝对路径（默认自动探测）
#   STYLE_SIGNALS_INPUTS_ARGS 办公室模式：透传给 wait_for_inputs.py（如 --once：只查一次、不等）
#   STYLE_SIGNALS_SKIP_TOPUP  topup 模式：=1 跳过步骤 0（TOPUP_SKIPPED，不等数）；标志文件在时不看它
#   STYLE_SIGNALS_MAX_LAG     护栏允许落后的交易日数（默认 1）
#   STYLE_SIGNALS_TOPUP_TIMEOUT  topup 模式步骤 0 超时秒数（默认 900）
#   STYLE_SIGNALS_NOTIFY_ARGS 透传给 notify_wechat.py（如 --dry-run：只打印不发）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GUARD="${SCRIPT_DIR}/check_freshness.py"
TOPUP_GUARD="${SCRIPT_DIR}/topup_guard.py"
LOG_DIR="${REPO}/logs"
STATUS_FILE="${LOG_DIR}/daily_signals_status.json"
TOPUP_SNAPSHOT="${LOG_DIR}/.topup_pre_snapshot.json"
LOCK_FILE="${LOG_DIR}/.daily_signals.lock"
MAX_LAG="${STYLE_SIGNALS_MAX_LAG:-1}"
TOPUP_TIMEOUT="${STYLE_SIGNALS_TOPUP_TIMEOUT:-900}"

mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/daily_signals_$(date +%Y%m%d).log"

# ── 并发锁（非阻塞）─────────────────────────────────────────────────────────
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 另一实例正在运行（${LOCK_FILE} 被占用），本次退出" >>"${LOG_FILE}"
  exit 75
fi

# ── 日志：stdout/stderr 同时进日志文件与 journal ────────────────────────────
exec > >(tee -a "${LOG_FILE}") 2>&1

# 中文输出在 systemd（非 UTF-8 locale）下会 UnicodeEncodeError，强制 UTF-8。
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export LC_ALL="${LC_ALL:-C.UTF-8}"

log() { echo "[$(date '+%F %T')] $*"; }

# ── python 解释器 ───────────────────────────────────────────────────────────
pick_python() {
  if [[ -n "${STYLE_SIGNALS_PYTHON:-}" ]]; then echo "${STYLE_SIGNALS_PYTHON}"; return; fi
  for cand in "${REPO}/.venv/bin/python3" /home/elfbob/miniconda3/bin/python3; do
    [[ -x "${cand}" ]] && { echo "${cand}"; return; }
  done
  command -v python3
}
PYTHON="$(pick_python)"
# 传给子进程：tools/topup_index_daily.sh 要用它跑闸门的 last-trading-day 模式。
# 不传的话脚本只能退回 PATH 里的 python3，而 systemd user service 的 PATH 不含
# miniconda——同一个闸门文件会被两个不同解释器跑，没必要。
export STYLE_SIGNALS_PYTHON="${PYTHON}"

STARTED_AT="$(date --iso-8601=seconds)"
START_TS=${SECONDS}
TOPUP_STATUS="UNKNOWN"
TOPUP_REASON=""
STEPS_JSON=""   # 逗号分隔的 JSON 对象串，收尾时包成数组

record_step() {  # name status seconds
  local entry
  entry="{\"step\":\"$1\",\"status\":\"$2\",\"seconds\":$3}"
  STEPS_JSON="${STEPS_JSON:+${STEPS_JSON},}${entry}"
}
steps_json() { echo "[${STEPS_JSON}]"; }

fail() {  # step_name exit_code
  local step="$1" code="${2:-1}"
  log "FAILED: 步骤 ${step} 失败（exit ${code}），链路中止"
  "${PYTHON}" "${GUARD}" --status-file "${STATUS_FILE}" --run-log "${LOG_FILE}" \
      --started-at "${STARTED_AT}" --topup "${TOPUP_STATUS}" \
      --topup-reason "${TOPUP_REASON}" \
      --steps "$(steps_json)" --failed-step "${step}" || true
  log "本次运行结束（失败），耗时 $((SECONDS - START_TS))s"
  # 75 只属于 flock 跳过：service 配了 SuccessExitStatus=75，步骤自己退出 75 若原样透传，
  # 失败就被记成成功、不触发告警。上面的日志照记原始退出码，退出时改报 1。
  if [[ ${code} -eq 75 ]]; then code=1; fi
  exit "${code}"
}

run_step() {  # step_name cmd...
  local name="$1"; shift
  local t0=${SECONDS} rc=0
  log "▶ ${name}: $*"
  "$@" || rc=$?
  local dt=$((SECONDS - t0))
  if [[ ${rc} -ne 0 ]]; then
    record_step "${name}" "FAILED" "${dt}"
    fail "${name}" "${rc}"
  fi
  record_step "${name}" "OK" "${dt}"
  log "✔ ${name} 完成，用时 ${dt}s"
}

cd "${REPO}"
log "════════ 日更信号链开始 ════════"
log "repo=${REPO} python=${PYTHON} log=${LOG_FILE}"

# ── 步骤 0：输入指数（允许降级）──────────────────────────────────────────────
# 标志文件 SKIP_TOPUP 在 = 办公室模式（office_inputs_stage，只读等数）；不在 = topup 模式
# （topup_stage，回退用，原样保留）。标志文件是版本控制文件，置上/解除见 README。
SKIP_FLAG_FILE="${SCRIPT_DIR}/SKIP_TOPUP"
INPUTS_MODE="topup"
OFFICE_FLAG_REASON=""
TOPUP_REASON=""
if [[ -f "${SKIP_FLAG_FILE}" ]]; then
  INPUTS_MODE="office"
  OFFICE_FLAG_REASON="$(head -1 "${SKIP_FLAG_FILE}" | tr -d '\r')"
elif [[ "${STYLE_SIGNALS_SKIP_TOPUP:-0}" == "1" ]]; then
  TOPUP_REASON="环境变量 STYLE_SIGNALS_SKIP_TOPUP=1"
fi

# 办公室模式：15 个输入指数由 data_manager 夜间作业（WSL2 20:00 起跑，约 20:02 结束；补数前先重看
# 前 5 个交易日纠错）写入，本链路零写入。wait_for_inputs.py 查期望信号日的 15 码是否到齐（判据 =
# 办公室回函 01 §7.3：T 日 close 非空的行数 = 码数），没齐每 5 分钟再查、最迟等到 21:30（手工早跑时由
# --max-wait 截住）；信号日不是今天（白天重跑、开机补跑、节假日）或已过截止只查一次。到齐后跑同族共动性
# 哨兵（topup 事后审计的规则 5/6，2026-08-24 起的口径：只判 T、只有 CRITICAL 阻断）。
# 它末尾的 INPUTS_STATUS / REASON / SENTINEL / SENTINEL_DETAIL 映射成：
#   哨兵 CRITICAL              → OFFICE_SUSPECT，返回 1 → fail：在信号重算之前中止（数可能脏了），交告警器
#   OK + 哨兵 CLEAN            → OFFICE_OK
#   OK 但缺哨兵结论            → OFFICE_CHECK_ERROR（不放过去当 OFFICE_OK）
#   LATE / CHECK_ERROR         → OFFICE_LATE / OFFICE_CHECK_ERROR
#   没结果 / 结果不认识        → OFFICE_CHECK_ERROR
# 除 OFFICE_SUSPECT 外都照常往下走：迟到就用库内已有数据照算，新鲜度由步骤 7 护栏兜底，推送里带 ⚠ 行。
# 限时 4500s、再宽限 10s 强杀：定时器 20:30（+≤2min 随机）起跑、等到 21:30 截止约 61 分钟；等数脚本自己的
# --max-wait（默认 4240）再给截止那一轮的查询留足时间，到点报 LATE 而不是被这层杀掉。service 的
# TimeoutStartSec 按「等数兜底 + 推送限时 + 600s」留足。三条不等式都由判例钉住。
# -u + tee：进度行边等边进日志（一小时里每 5 分钟一行，而不是等完才一次吐出）；结果副本每次 mktemp 新建、
# 解析完即删，只从本次的副本解析。透传参数不加引号，按词拆开（如 --once）；排在任何固定参数之前。
office_inputs_stage() {
  local t0=${SECONDS} wait_rc=0 out status reason sentinel detail
  log "▶ 输入等数（办公室模式；标志文件 SKIP_TOPUP：${OFFICE_FLAG_REASON:-未写原因}）: wait_for_inputs.py ${STYLE_SIGNALS_INPUTS_ARGS:-}"
  if ! out="$(mktemp -p "${LOG_DIR}" .inputs_wait.XXXXXX)"; then
    TOPUP_STATUS="OFFICE_CHECK_ERROR"
    TOPUP_REASON="建不了等数结果副本（mktemp -p ${LOG_DIR} 失败），没有等数"
    log "⚠ ${TOPUP_STATUS}: ${TOPUP_REASON} —— 不中止链路，用 index_daily 库内已有数据照算；新鲜度由步骤 7 护栏兜底"
    record_step "topup" "${TOPUP_STATUS}" "$((SECONDS - t0))"
    return 0
  fi
  # shellcheck disable=SC2086
  timeout -k 10 4500 "${PYTHON}" -u "${SCRIPT_DIR}/wait_for_inputs.py" ${STYLE_SIGNALS_INPUTS_ARGS:-} 2>&1 \
      | tee "${out}" || wait_rc=$?
  status="$(grep '^INPUTS_STATUS=' "${out}" | tail -n 1 | cut -d= -f2-)" || true
  reason="$(grep '^INPUTS_REASON=' "${out}" | tail -n 1 | cut -d= -f2-)" || true
  sentinel="$(grep '^INPUTS_SENTINEL=' "${out}" | tail -n 1 | cut -d= -f2-)" || true
  detail="$(grep '^INPUTS_SENTINEL_DETAIL=' "${out}" | tail -n 1 | cut -d= -f2-)" || true
  rm -f "${out}"

  if [[ "${sentinel}" == "CRITICAL" ]]; then
    TOPUP_STATUS="OFFICE_SUSPECT"
    TOPUP_REASON="${detail:-同族哨兵 CRITICAL（没记明细）}"
    record_step "topup" "${TOPUP_STATUS}" "$((SECONDS - t0))"
    log "OFFICE_SUSPECT: 办公室写入的输入没过同族共动性哨兵（CRITICAL）：${TOPUP_REASON}"
    log "OFFICE_SUSPECT: 本次**不重算信号**，committed CSV 维持上一次可信结果"
    log "OFFICE_SUSPECT: 先判真假——对照母指数当日行情：数据错了 → 通知 data_manager 办公室核对（它每晚重看"
    log "OFFICE_SUSPECT:   前 5 个交易日，改正后重跑本链路即可）；确属真实行情（判据 12.6~13.5 年零误报）→ 记录，"
    log "OFFICE_SUSPECT:   次日运行只判次日，会自动恢复"
    return 1
  fi
  case "${status}" in
    OK)
      if [[ "${sentinel}" == "CLEAN" ]]; then
        TOPUP_STATUS="OFFICE_OK"
        TOPUP_REASON="${reason:-未记原因}"
      else
        TOPUP_STATUS="OFFICE_CHECK_ERROR"
        TOPUP_REASON="${reason:-未记原因}；缺同族哨兵结论（INPUTS_SENTINEL=${sentinel:-空}）"
      fi ;;
    LATE|CHECK_ERROR)
      TOPUP_STATUS="OFFICE_${status}"
      TOPUP_REASON="${reason:-未记原因}" ;;
    "")
      TOPUP_STATUS="OFFICE_CHECK_ERROR"
      TOPUP_REASON="wait_for_inputs 无结果（exit ${wait_rc}）" ;;
    *)
      TOPUP_STATUS="OFFICE_CHECK_ERROR"
      TOPUP_REASON="wait_for_inputs 结果不认识（INPUTS_STATUS=${status}，exit ${wait_rc}）" ;;
  esac
  if [[ "${TOPUP_STATUS}" == "OFFICE_OK" ]]; then
    log "✔ 输入到齐：${TOPUP_REASON}（同族哨兵：${detail:-—}）"
  else
    log "⚠ ${TOPUP_STATUS}: ${TOPUP_REASON} —— 不中止链路，用 index_daily 库内已有数据照算；新鲜度由步骤 7 护栏兜底"
  fi
  record_step "topup" "${TOPUP_STATUS}" "$((SECONDS - t0))"
  return 0
}

topup_stage() {
  local t0=${SECONDS} rc=0 dt

  # 0a. 显式跳过（环境变量；标志文件已改走办公室模式，见上）——运维权威，最高优先级
  if [[ -n "${TOPUP_REASON}" ]]; then
    log "⏭ TOPUP_SKIPPED(${TOPUP_REASON}) —— 零写入，改用 index_daily 库内现有数据"
    TOPUP_STATUS="TOPUP_SKIPPED"
    record_step "topup" "TOPUP_SKIPPED" 0
    return 0
  fi

  # 0b. 前置闸门（只读；不过就不调用 topup = 零写入）。fail-closed：护栏出错也当不过。
  log "▶ topup 前置闸门: topup_guard.py --mode preflight"
  local pre_out pre_rc=0
  pre_out="$("${PYTHON}" "${TOPUP_GUARD}" --mode preflight --snapshot "${TOPUP_SNAPSHOT}" 2>&1)" || pre_rc=$?
  printf '%s\n' "${pre_out}"
  if [[ ${pre_rc} -ne 0 ]]; then
    TOPUP_REASON="$(printf '%s\n' "${pre_out}" | grep -m1 '^SKIP_REASON=' | cut -d= -f2-)"
    TOPUP_REASON="${TOPUP_REASON:-前置闸门 exit ${pre_rc}}"
    log "⏭ TOPUP_SKIPPED(${TOPUP_REASON}) —— 前置闸门未过，零写入；用库内现有数据继续"
    TOPUP_STATUS="TOPUP_SKIPPED"
    record_step "topup" "TOPUP_SKIPPED" "$((SECONDS - t0))"
    return 0
  fi

  # 0c. 真正调用 topup（此处才可能写 index_daily）
  log "▶ topup: tools/topup_index_daily.sh（超时 ${TOPUP_TIMEOUT}s）"
  timeout "${TOPUP_TIMEOUT}" "${REPO}/tools/topup_index_daily.sh" || rc=$?
  dt=$((SECONDS - t0))
  if [[ ${rc} -ne 0 ]]; then
    TOPUP_STATUS="DEGRADED"
    TOPUP_REASON="topup 调用失败 exit ${rc}（gateway 不可达 / wsd 额度耗尽 / Wind 报错）"
    log "⚠ DEGRADED: ${TOPUP_REASON}"
    log "⚠ 不中止链路，改用 index_daily 库内现有数据继续；新鲜度由步骤 7 护栏兜底"
    record_step "topup" "DEGRADED" "${dt}"
    # 失败的调用也可能已写入部分内容，照样审计
  else
    log "✔ topup 调用完成，用时 ${dt}s"
  fi

  # 0d. 事后审计（只读 PG）——不通过就在信号重算之前中止，可疑数据进不了信号 CSV
  log "▶ topup 事后审计: topup_guard.py --mode audit"
  local audit_rc=0
  "${PYTHON}" "${TOPUP_GUARD}" --mode audit --snapshot "${TOPUP_SNAPSHOT}" || audit_rc=$?
  if [[ ${audit_rc} -eq 2 ]]; then
    # 审计**没能做成**（快照丢失 / PG 抖动）≠ 写入有问题。别把"查不了"说成"脏了"，
    # 那会把人推向回滚共享表这种高成本处置。
    TOPUP_STATUS="TOPUP_VERIFY_FAILED"
    TOPUP_REASON="事后审计未能执行（exit 2，快照缺失或 PG 不可达）"
    record_step "topup" "TOPUP_VERIFY_FAILED" "$((SECONDS - t0))"
    log "TOPUP_VERIFY_FAILED: 本次写入**无法验证**（不是「已确认有问题」）"
    log "TOPUP_VERIFY_FAILED: 处置 = 重跑审计即可："
    log "TOPUP_VERIFY_FAILED:   ${PYTHON} ${TOPUP_GUARD} --mode audit --snapshot ${TOPUP_SNAPSHOT}"
    log "TOPUP_VERIFY_FAILED: 审计通过就重跑本链路；仍失败再按 TOPUP_SUSPECT 处置"
    return 1
  fi
  if [[ ${audit_rc} -ne 0 ]]; then
    TOPUP_STATUS="SUSPECT"
    TOPUP_REASON="事后审计判定写入可疑（exit ${audit_rc}）"
    record_step "topup" "SUSPECT" "$((SECONDS - t0))"
    log "TOPUP_SUSPECT: 已写入 index_daily 的内容未通过事后审计（exit ${audit_rc}）"
    log "TOPUP_SUSPECT: 本次**不重算信号**，committed CSV 维持上一次可信结果"
    log "TOPUP_SUSPECT: 先判成因——**未必是脏数据**：上游对历史的合法回溯修订"
    log "TOPUP_SUSPECT:   （如 CSI 指数重述、除权口径更正）同样会命中「历史被改写」规则"
    log "TOPUP_SUSPECT: 若确属合法修订 → 记录后重跑链路即可（新快照即新基线）"
    log "TOPUP_SUSPECT: 若不是 → 置 deploy/daily_signals/SKIP_TOPUP 阻断后续写入，"
    log "TOPUP_SUSPECT:        并把 index_daily 的可疑区间交 stock_selector 侧核对"
    return 1
  fi
  [[ "${TOPUP_STATUS}" == "DEGRADED" ]] || TOPUP_STATUS="OK"
  record_step "topup" "${TOPUP_STATUS}" "$((SECONDS - t0))"
  return 0
}

topup_rc=0
if [[ "${INPUTS_MODE}" == "office" ]]; then
  office_inputs_stage || topup_rc=$?
else
  topup_stage || topup_rc=$?
fi
[[ ${topup_rc} -eq 0 ]] || fail "topup_audit(${TOPUP_STATUS})" 1

# ── 步骤 1-6：四条信号线（slope20 2026-09-09 加入）+ 推荐持仓（均为全量重算覆写）────────────────────────
run_step "hybrid20_growth_stability" \
  "${PYTHON}" signals/hybrid20/update_growth_stability.py
run_step "hybrid20_confirmed" \
  "${PYTHON}" signals/hybrid20/update_confirmed_signal.py
run_step "citic40d" \
  "${PYTHON}" signals/citic40d/generate_signal.py
run_step "equal_weight_20d40z" \
  "${PYTHON}" signals/equal_weight/generate_signal.py
run_step "equal_weight_5d20z" \
  "${PYTHON}" signals/equal_weight/generate_signal.py \
      --lookback 5 --z-window 20 --smoothing 0 \
      --output output/equal_weight/equal_weight_signal_5d20z.csv
run_step "slope20_L20zw120" \
  "${PYTHON}" signals/slope20/generate_signal.py
run_step "recommended_positions" \
  "${PYTHON}" -m backtest.production

# ── 步骤 7：新鲜度护栏 + 状态文件 ────────────────────────────────────────────
log "▶ freshness_guard: check_freshness.py --max-lag ${MAX_LAG}"
guard_t0=${SECONDS}; guard_rc=0
"${PYTHON}" "${GUARD}" --max-lag "${MAX_LAG}" \
    --status-file "${STATUS_FILE}" --run-log "${LOG_FILE}" \
    --started-at "${STARTED_AT}" --topup "${TOPUP_STATUS}" \
      --topup-reason "${TOPUP_REASON}" \
    --steps "$(steps_json)" \
    || guard_rc=$?
guard_dt=$((SECONDS - guard_t0))

if [[ ${guard_rc} -ne 0 ]]; then
  log "STALE/CHECK_ERROR: 新鲜度护栏未通过（exit ${guard_rc}）——见上方 STALE 行与 ${STATUS_FILE}"
  log "════════ 日更信号链结束：失败，总耗时 $((SECONDS - START_TS))s ════════"
  exit "${guard_rc}"
fi
log "✔ freshness_guard 通过，用时 ${guard_dt}s"

# ── 步骤 8：企业微信推送（护栏通过才推；推送失败 → 非零退出，交 OnFailure 告警器）──────
# 限时 120s、再宽限 10s 强杀：send_text 的 10s socket 超时只管单次阻塞操作，总时长并不封顶——
# DNS 解析根本不受它管，慢回包 / TLS 握手也能一段段拖下去；真正封顶的是这层限时，否则卡住会
# 一直占锁到 service 的 TimeoutStartSec（5400）。超时由 timeout 杀进程（124；SIGTERM 后 10s 仍不退则 SIGKILL，
# 137——被别的 SIGKILL 如 OOM 杀掉也是 137，一并按超时报），来不及写状态文件的 notify 段，
# 原因只在本日志里。
# 透传参数放在固定参数之前：argparse 同名参数取最后一个，--status-file 永远是护栏刚写的那份。
# -u：不缓冲，报错行（stderr）与消息正文（stdout）在日志里按真实顺序交错。
log "▶ notify_wechat: notify_wechat.py ${STYLE_SIGNALS_NOTIFY_ARGS:-}"
notify_rc=0
# shellcheck disable=SC2086
timeout -k 10 120 "${PYTHON}" -u "${SCRIPT_DIR}/notify_wechat.py" ${STYLE_SIGNALS_NOTIFY_ARGS:-} \
    --status-file "${STATUS_FILE}" || notify_rc=$?
if [[ ${notify_rc} -ne 0 ]]; then
  if [[ ${notify_rc} -eq 124 || ${notify_rc} -eq 137 ]]; then
    log "NOTIFY_FAILED: 企业微信推送超时（120s，多半卡在 DNS/网络；exit ${notify_rc}）——信号与护栏均已完成，只是没送达；进程被杀，状态文件 notify 段来不及写"
  else
    log "NOTIFY_FAILED: 企业微信推送失败（exit ${notify_rc}）——信号与护栏均已完成，只是没送达；见上方输出与 ${STATUS_FILE} 的 notify 段"
  fi
  log "════════ 日更信号链结束：推送失败，总耗时 $((SECONDS - START_TS))s ════════"
  exit 1
fi
log "════════ 日更信号链结束：成功，总耗时 $((SECONDS - START_TS))s ════════"
