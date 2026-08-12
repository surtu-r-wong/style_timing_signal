#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# style_timing_signal 日更信号链 runner
#
# 链路（顺序即 README「运行」段的顺序）：
#   0. tools/topup_index_daily.sh          —— 保鲜上游 index_daily（可优雅降级）
#   1. signals/hybrid20/update_growth_stability.py
#   2. signals/hybrid20/update_confirmed_signal.py
#   3. signals/citic40d/generate_signal.py
#   4. signals/equal_weight/generate_signal.py                    （变体A / 生产口径）
#   5. signals/equal_weight/generate_signal.py --lookback 5 …     （变体B / 参考口径）
#   6. python -m backtest.production        —— 三条线的 long-flat 推荐持仓
#   7. deploy/daily_signals/check_freshness.py —— 新鲜度护栏 + 状态文件
#
# 语义要点：
#   * 步骤 0 允许失败（gateway 不可达时降级为「用库内现有数据继续」，并在日志/状态
#     文件里记 DEGRADED）；步骤 1-7 任一失败即整链非零退出。
#   * 四个生成脚本都是**全量重算覆写**（非追加），因此断更多日后直接跑即完成补跑。
#   * flock 并发锁：已有实例在跑时直接退出 75（EX_TEMPFAIL），不排队。
#   * 护栏未过 → 退出 1 并在日志里打大写 STALE；这是「停更无人知」的直接对策。
#
# 环境变量：
#   STYLE_SIGNALS_PYTHON      python 解释器绝对路径（默认自动探测）
#   STYLE_SIGNALS_SKIP_TOPUP  =1 跳过步骤 0（状态记 SKIPPED）
#   STYLE_SIGNALS_MAX_LAG     护栏允许落后的交易日数（默认 1）
#   STYLE_SIGNALS_TOPUP_TIMEOUT  步骤 0 超时秒数（默认 900）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GUARD="${SCRIPT_DIR}/check_freshness.py"
LOG_DIR="${REPO}/logs"
STATUS_FILE="${LOG_DIR}/daily_signals_status.json"
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
TOPUP_STATUS="SKIPPED"
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
if [[ "${STYLE_SIGNALS_SKIP_TOPUP:-0}" == "1" ]]; then
  log "⏭ topup 被 STYLE_SIGNALS_SKIP_TOPUP=1 跳过；改用 index_daily 库内现有数据"
  TOPUP_STATUS="SKIPPED"
  record_step "topup" "SKIPPED" 0
else
  t0=${SECONDS}; rc=0
  log "▶ topup: tools/topup_index_daily.sh（超时 ${TOPUP_TIMEOUT}s）"
  timeout "${TOPUP_TIMEOUT}" "${REPO}/tools/topup_index_daily.sh" || rc=$?
  dt=$((SECONDS - t0))
  if [[ ${rc} -eq 0 ]]; then
    TOPUP_STATUS="OK"; log "✔ topup 完成，用时 ${dt}s"
  else
    TOPUP_STATUS="DEGRADED"
    log "⚠ DEGRADED: topup 失败（exit ${rc}，gateway 不可达或额度受限）——"
    log "⚠ 不中止链路，改用 index_daily 库内现有数据继续；新鲜度由步骤 7 护栏兜底"
  fi
  record_step "topup" "${TOPUP_STATUS}" "${dt}"
fi

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
