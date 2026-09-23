# 输入移交办公室后的只读模式 —— 等数、推送文案、说明更新

> 起草：2026-09-23 傍晚。前情：请求函 `data_manager/requests/2026-09-23-style-timing-signal-index-daily-takeover/`
> （处置 + 回函 01/02）；本仓 `e6993aa` 已把 `SKIP_TOPUP` 入库、定时器改 20:30。
> **合入时点：今晚 20:31 首次只读运行跑完之后**（不干扰办公室对首跑的复核）。

## 0. 目标

办公室已接管 15 个输入指数（WSL2 夜间作业 20:00，约 20:02 结束；每晚补数前重看前 5 个交易日纠错）。本链路从今晚起只读。
办公室回函把三件代码事留给本项目：① 等数逻辑 ② 推送里常态化的「⚠ topup TOPUP_SKIPPED」③ 过时说明。

## 1. 输入码清单：`deploy/daily_signals/input_codes.txt`（新）

- 一行一个码，`#` 开头为注释，空行忽略；**顺序与内容与 `tools/topup_index_daily.sh` 的 `CODES` 完全一致**（测试钉住）。
- 头注释：本项目日更输入指数；2026-09-23 起由 data_manager 夜间作业写入；topup 脚本（已停用，留作回退）仍用它自己的 `CODES`，两者由测试保持一致。
- **不改** `tools/topup_index_daily.sh`（回退路径原样保留）。

## 2. 等数脚本：`deploy/daily_signals/wait_for_inputs.py`（新，只读 PG）

**期望信号日 T**（纯函数，可测）：今天是 CN 交易日 **且** 当前时刻 ≥ `--ready-from`（默认 20:00，办公室夜间作业起跑）→ T=今天；
否则 T = 严格早于今天的最近一个 CN 交易日。
- 日历：`data_manager.business_calendar`（`calendar_id='CN'`，`is_business_day`；实测覆盖 2000-01-01..2026-12-31，
  09-25 中秋、10-01~10-07 国庆均为休市）。**所需日期不在表里**（如 2027 未装载）→ 对缺的日期退回「周一至周五」并在结果里注明「日历缺 <日期>，按工作日推断」。
- 为什么不用 `topup_guard.expected_last_trading_day`：它只看工作日 + 15:30，不认节假日（其 docstring 自承「节假日会高估一天」），拿来等数会在国庆每晚白等到截止。

**到齐判据**（办公室回函 01 §7.3 原样）：`select count(*) from stock_selector.index_daily where trade_date=T and index_code = any(codes) and close is not null` 等于码数（15）。
缺哪些码要能列出来（结果文案用）。

**轮询**：默认每 `--interval 300` 秒查一次，截止 `--deadline 21:30`（当天本地时刻）。开跑时已过截止 → 只查一次。`--once` → 只查一次（手工重跑用）。
每次查询**新建连接**（`load_db_config()`，`connect_timeout=10`，`options="-c statement_timeout=60000 -c default_transaction_read_only=on"`），
单次查询出错记下、下一轮再查；截止时仍出错 → CHECK_ERROR。等待期间每轮打印一行进度（runner 日志里要实时可见）。

**输出契约**（最后三行，runner 靠它解析；人读的日志行在前）：
```
INPUTS_STATUS=OK|LATE|CHECK_ERROR
INPUTS_DAY=YYYY-MM-DD
INPUTS_REASON=<一行中文>
```
文案：OK「办公室日更 <T> 15 码到齐（等 <N> 秒）」；LATE「<T> 截至 <HH:MM> 仍缺 <k> 码：<码…>，按库内已有数据照算」；
CHECK_ERROR「<T> 到齐检查出错：<类型>: <消息>」（有日历退回就在末尾追加注明）。
**退出码恒为 0**（三种结果链路都照常往下走，新鲜度护栏兜底）；main 顶层兜住一切异常转成 CHECK_ERROR 三行。

依赖注入以便测试：时钟、sleep、查询函数、日历加载函数都可传入；不连真库的单测即可覆盖全部分支。

## 3. runner 接入（`run_daily_signals.sh` 第 0 步）

- **标志文件在**（办公室模式）：不再只打 TOPUP_SKIPPED 就返回，而是跑等数脚本：
  `timeout -k 10 4500 "${PYTHON}" -u "${SCRIPT_DIR}/wait_for_inputs.py" ${STYLE_SIGNALS_INPUTS_ARGS:-}`，
  输出**实时**进日志（例如 `| tee` 到 `${LOG_DIR}/.inputs_wait.out` 再解析；注意 `set -euo pipefail` 与管道退出码），
  解析 `INPUTS_STATUS/INPUTS_REASON`，映射为 `TOPUP_STATUS` = `OFFICE_OK` / `OFFICE_LATE` / `OFFICE_CHECK_ERROR`
  （没解析到 → `OFFICE_CHECK_ERROR`，原因「wait_for_inputs 无结果（exit N）」），`TOPUP_REASON` 取其原因，`record_step "topup" …`，返回 0。
- **只有环境变量 `STYLE_SIGNALS_SKIP_TOPUP=1`、没有标志文件**：保持旧行为（TOPUP_SKIPPED，不等）。
- **没有标志文件**：旧 topup 路径原样（回退用），一行不改。
- 头注释第 0 步、环境变量段（新增 `STYLE_SIGNALS_INPUTS_ARGS`，如 `--once`）同步。
- 状态文件字段名仍叫 `topup`（兼容告警器与推送），取值新增 `OFFICE_*` 三种。

## 4. service 超时

`style-signals-daily.service` 的 `TimeoutStartSec=3600` → `5400`：等数上限约 61 分钟（20:30~21:30，含随机延迟）+ 硬兜底 4500 秒 + 链路约 20 秒 + 推送限时 130 秒。
接线测试加一条：`TimeoutStartSec ≥ 等数兜底 + 推送限时 + 600 秒余量`。**合入后控制器重装单元。**

## 5. 推送与告警文案（`notify_wechat.py`）

- `health_lines`：`OK` → 原样「topup OK · 护栏 OK（…）」；`OFFICE_OK` → 「输入 办公室日更 ✓ · 护栏 OK（…）」，**无 ⚠ 行**；
  `OFFICE_LATE` → 护栏行 +「⚠ 输入未到齐：<原因>」；`OFFICE_CHECK_ERROR` → 护栏行 +「⚠ 输入到齐检查出错：<原因>」；其余取值 → 原来的「⚠ topup <状态>：<原因>」。
- `build_alert`：`topup ∈ {None, OK, OFFICE_OK}` 不出 topup 行；其余照旧。
- 用例覆盖以上每一支。

## 6. 说明更新（只改文字）

- `check_freshness.py`：docstring / 打印里「15 个输入码现由本链路 topup 自采」「日历两个写入方」→ 2026-09-23 起 19 码全部由 data_manager 夜间作业写入（单一写入方），
  UPSTREAM_STALE 处置 →「办公室夜间作业没写进来（办公室会收到它自己的告警）——看 data_manager 状态；本链路只读」。历史叙述保留、加日期注。
- `deploy/daily_signals/README.md`：链路（第 0 步改为等数）、何时推（约 20:31，最迟等到 21:30）、失败形态表（新增「办公室日更迟到/失败」行；旧 topup 各行标「回退到 topup 模式时适用」）、
  「Wind 额度耗尽时怎么办」加现状注（归办公室；SKIP_TOPUP 常驻）、手动操作（手工重跑在交易日 20:00~21:30 之间且当天数据未到会等；不想等用 `STYLE_SIGNALS_INPUTS_ARGS=--once`）、产物（topup 字段的 OFFICE_* 取值）、文件表加两个新文件。
- 仓库 `README.md`：18:30 → 20:30、输入由办公室写入。
- `docs/plans/2026-09-23-wechat-signal-push-design.md` 顶部加一行带日期的注：推送时刻随输入移交改为约 20:31（正文不改）。

## 7. 测试

- 新 `tests/test_deploy_wait_for_inputs.py`：期望信号日（交易日 20:00 前/后、节假日、周末、日历缺失退回）；轮询（第 N 轮到齐 → OK 与等待秒数；到截止未齐 → LATE 列出缺码；
  出错后恢复 → OK；出错到截止 → CHECK_ERROR；开跑已过截止 → 只查一次；`--once`）；输出三行格式；码表解析；**`input_codes.txt` 与 topup 脚本 `CODES` 一致**。
- `tests/test_deploy_daily_wiring.py`：标志分支调用等数脚本（带 `timeout -k`、`-u`、透传参数在前）、状态映射运行期判例（桩解释器打印 OK/LATE/CHECK_ERROR/什么都不打印）、
  service 超时预算；原有判例全部保持绿。
- `tests/test_deploy_notify_wechat.py`：新增文案分支。
- 变异验证：日历退回、到齐判据、截止判断、状态映射、OFFICE_OK 不出 ⚠。
