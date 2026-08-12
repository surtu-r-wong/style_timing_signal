#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# style_timing_signal 日更信号链 runner
#
# 链路（顺序即 README「运行」段的顺序）：
#   0. topup 阶段 —— 保鲜上游 index_daily。三层保护，原则=不可信响应零写入：
#        0a 标志文件 deploy/daily_signals/SKIP_TOPUP（或 STYLE_SIGNALS_SKIP_TOPUP=1）
#           存在即跳过，理由记进日志与 status.json（运维权威，最高优先级）
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
#   6. python -m backtest.production        —— 三条线的 long-flat 推荐持仓
#   7. deploy/daily_signals/check_freshness.py —— 新鲜度护栏 + 状态文件
#
# 语义要点：
#   * 步骤 0 允许失败/跳过（记 DEGRADED / TOPUP_SKIPPED，用库内现有数据继续）；
#     唯一例外是事后审计不过（TOPUP_SUSPECT）——那说明库可能已脏，必须停在信号之前。
#     步骤 1-7 任一失败即整链非零退出。
#   * 四个生成脚本都是**全量重算覆写**（非追加），因此断更多日后直接跑即完成补跑。
#   * flock 并发锁：已有实例在跑时直接退出 75（EX_TEMPFAIL），不排队。
#   * 护栏未过 → 退出 1 并在日志里打大写 STALE；这是「停更无人知」的直接对策。
#
# 环境变量：
#   STYLE_SIGNALS_PYTHON      python 解释器绝对路径（默认自动探测）
#   STYLE_SIGNALS_SKIP_TOPUP  =1 跳过步骤 0（等价于置 SKIP_TOPUP 标志文件）
#   STYLE_SIGNALS_MAX_LAG     护栏允许落后的交易日数（默认 1）
#   STYLE_SIGNALS_TOPUP_TIMEOUT  步骤 0 超时秒数（默认 900）
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

# ── 步骤 0：上游保鲜（允许降级）──────────────────────────────────────────────
SKIP_FLAG_FILE="${SCRIPT_DIR}/SKIP_TOPUP"
TOPUP_REASON=""
if [[ -f "${SKIP_FLAG_FILE}" ]]; then
  TOPUP_REASON="$(head -1 "${SKIP_FLAG_FILE}" | tr -d '\r')"
  TOPUP_REASON="标志文件 deploy/daily_signals/SKIP_TOPUP: ${TOPUP_REASON:-未写原因}"
elif [[ "${STYLE_SIGNALS_SKIP_TOPUP:-0}" == "1" ]]; then
  TOPUP_REASON="环境变量 STYLE_SIGNALS_SKIP_TOPUP=1"
fi

topup_stage() {
  local t0=${SECONDS} rc=0 dt

  # 0a. 显式跳过（标志文件 / 环境变量）——运维权威，最高优先级
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
  if [[ ${audit_rc} -ne 0 ]]; then
    TOPUP_STATUS="SUSPECT"
    record_step "topup" "SUSPECT" "$((SECONDS - t0))"
    log "TOPUP_SUSPECT: 已写入 index_daily 的内容未通过事后审计（exit ${audit_rc}）"
    log "TOPUP_SUSPECT: 本次**不重算信号**，committed CSV 维持上一次可信结果"
    log "TOPUP_SUSPECT: 处置 = 置 deploy/daily_signals/SKIP_TOPUP 标志阻断后续写入，"
    log "TOPUP_SUSPECT:        并把 index_daily 的可疑区间交 stock_selector 侧核对/回滚"
    return 1
  fi
  [[ "${TOPUP_STATUS}" == "DEGRADED" ]] || TOPUP_STATUS="OK"
  record_step "topup" "${TOPUP_STATUS}" "$((SECONDS - t0))"
  return 0
}

topup_rc=0
topup_stage || topup_rc=$?
[[ ${topup_rc} -eq 0 ]] || fail "topup_audit" 1

# ── 步骤 1-6：三条信号线 + 推荐持仓（均为全量重算覆写）────────────────────────
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

TOTAL=$((SECONDS - START_TS))
if [[ ${guard_rc} -ne 0 ]]; then
  log "STALE/CHECK_ERROR: 新鲜度护栏未通过（exit ${guard_rc}）——见上方 STALE 行与 ${STATUS_FILE}"
  log "════════ 日更信号链结束：失败，总耗时 ${TOTAL}s ════════"
  exit "${guard_rc}"
fi
log "✔ freshness_guard 通过，用时 ${guard_dt}s"
log "════════ 日更信号链结束：成功，总耗时 ${TOTAL}s ════════"
