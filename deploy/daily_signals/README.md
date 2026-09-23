# deploy/daily_signals —— 日更信号链自动化

本仓库在 2026-08-12 之前**没有任何自动化**：三条信号脚本与 `backtest.production` 一直靠人手
触发，于是 2026-07-09 之后没人跑，三条生产信号 CSV 停在 2026-07-08、推荐持仓停在 07-09，
**停更 35 天无人发现**（归因见 `docs/plans/2026-08-12-project-review-and-priorities.md` §3.1，
用户裁决见同文档 §9）。本目录是那次裁决的落地：一个 runner + 一个 systemd user timer +
一条产出护栏（**两条命题**：不落后 + 无缺口，后者 2026-08-17 补，成因见下「产出护栏」）。

## 文件

| 文件 | 作用 |
|---|---|
| `run_daily_signals.sh` | 链路 runner：输入（办公室模式等数 / topup 模式取数）→ 各信号线 → 推荐持仓 → 护栏 → 企业微信推送；带 flock、分步计时、日志、状态文件 |
| `wait_for_inputs.py` | 等数脚本（只读 PG，办公室模式的步骤 0）：等期望信号日的 15 个输入码到齐，每 5 分钟查一次、最迟 21:30；最后三行 `INPUTS_*` 交给 runner。见下「输入指数：办公室模式」 |
| `input_codes.txt` | 15 个输入码，一行一个；与 `tools/topup_index_daily.sh` 的 `CODES` 由测试钉成一致 |
| `check_freshness.py` | 产出护栏（只读 PG）：末行不落后 + 区间无缺口，兼状态 JSON 写入器 |
| `topup_guard.py` | topup 写库护栏（topup 模式用）：前置闸门（只读 gateway+PG）+ 事后审计（只读 PG） |
| `notify_wechat.py` | 企业微信推送（步骤 8）：护栏通过后推当日持仓；`--alert` 由告警器调用推失败通知；`--dry-run` 只打印。见下「企业微信推送」 |
| `alert_on_failure.sh` | 失败告警器：写告警文件 `logs/ALERT_daily_signals` + best-effort 企业微信失败通知 + `notify-send` |
| `style-signals-daily-alert.service` | 告警单元，由主 service 的 `OnFailure=` 拉起 |
| `SKIP_TOPUP` | 模式开关（版本控制文件，第一行是原因）：在 = 办公室模式（2026-09-23 起常驻：输入由 data_manager 写入，本链路只读）；删掉 = 回到 topup 模式。见下「输入指数：办公室模式」与「Wind wsd 额度耗尽时怎么办」 |
| `style-signals-daily.service` | systemd user service（oneshot），单元副本 |
| `style-signals-daily.timer` | systemd user timer，工作日 20:30 Asia/Shanghai（2026-09-23 前是 18:30），`Persistent=true` |

## 链路

```
deploy/daily_signals/wait_for_inputs.py        # 步骤 0（办公室模式，常态）：只读等当日 15 码到齐，最迟 21:30 → OFFICE_*
tools/topup_index_daily.sh                     # 步骤 0（topup 模式，回退用）：自己经 Wind 取数写库，允许失败 → DEGRADED
signals/hybrid20/update_growth_stability.py    # 步骤 1
signals/hybrid20/update_confirmed_signal.py    # 步骤 2
signals/citic40d/generate_signal.py            # 步骤 3
signals/equal_weight/generate_signal.py        # 步骤 4（变体A / 生产口径 20d40z）
signals/equal_weight/generate_signal.py …5d20z # 步骤 5（变体B / 参考口径）
signals/slope20/generate_signal.py             # 步骤 5b（2026-09-09 第四条生产线 → slope20_signal_L20zw120.csv）
python -m backtest.production                  # 步骤 6 → output/recommended/（equal_weight 自 2026-09-09 起对称 → equal_weight_symmetric.csv；long-flat 文件作参照并行产出；slope20 对称 → slope20_symmetric.csv，另出 slope20_longflat.csv = 现货池）
deploy/daily_signals/check_freshness.py        # 步骤 7 护栏
deploy/daily_signals/notify_wechat.py          # 步骤 8 企业微信推送（护栏通过才推）
```

各生成脚本都是**全量重算覆写**（读 PG 全历史 → `to_csv` 覆盖），不是追加。因此：
断更 N 天后直接跑一次就完成补跑，无需专门的补跑模式；反过来也意味着历史段每天都会被
重算一遍，历史零变化是可验证的（见下「历史零篡改」）。

## 输入指数：办公室模式（2026-09-23 起）

15 个输入指数（`input_codes.txt`）2026-09-23 起由 data_manager 夜间作业（WSL2 `dbm-daily-wss`，20:00 起跑、
约 20:02 结束；每晚补数前先重看前 5 个交易日、有差异就纠正）写入 `stock_selector.index_daily`，本链路
**只读、零写入**（请求函、处置与回函：`data_manager/requests/2026-09-23-style-timing-signal-index-daily-takeover/`）。
标志文件 `SKIP_TOPUP` 在即办公室模式；定时器随之由 18:30 改到 20:30。

步骤 0 跑 `wait_for_inputs.py`，只回答「期望信号日 T 的 15 码到齐没有」：

- **T**：今天是交易日且已过 20:00 → 今天；否则此前最近的交易日。交易日历取 `data_manager.business_calendar`
  （`calendar_id='CN'`）；表里没有的日子（如 2027 未装载）按周一至周五推断，结果里注明「日历缺 …，按工作日推断」。
- **到齐**：T 日这 15 码 `close` 非空的行数 = 15（办公室回函 01 §7.3 的判据：直接查数据，不查作业台账）。
- **等**：只在「T 是今天、还没到 21:30」时每 5 分钟查一次，最后一轮恰在 21:30；白天手工重跑、开机补跑、
  节假日照跑时 T 是过去某天（那一晚的夜间作业早跑完了），只查一次。每轮一行进度实时进日志。
- **结果**（状态文件 `topup` 字段，名字沿用）：

  | 取值 | 含义 | 推送体检行 |
  |---|---|---|
  | `OFFICE_OK` | T 日 15 码到齐（常态） | `输入 办公室日更 ✓ · 护栏 OK（…）`，无 ⚠ 行 |
  | `OFFICE_LATE` | 到截止仍缺码 | 另起 `⚠ 输入未到齐：<T> 截至 21:30 仍缺 k 码：…，按库内已有数据照算` |
  | `OFFICE_CHECK_ERROR` | 查询到截止仍出错，或等数脚本没给出结果（超时 / 崩溃） | 另起 `⚠ 输入到齐检查出错：<原因>` |

  三种都**不中止链路**，用库内已有数据照算；新鲜度由步骤 7 护栏兜底（产出落后超过 1 个交易日照样
  `STALE`、不推、告警）。
- **时间预算**：等数外包 `timeout -k 10 4500`（正常最多等约 61 分钟：20:30 起跑、21:30 截止），service
  `TimeoutStartSec=5400` = 等数兜底 + 推送限时 + 600 秒余量（判例钉住）。等数期间一直占着锁。

**回退**：删掉 `SKIP_TOPUP`（并提交这次删除）即回到 topup 模式；办公室回函 02 §5 另要求把定时器换回 18:30 的
备份——**换定时器时要先停定时器、改触发时间戳再启动**，否则 systemd 会把「错过的」那次立刻补跑、多推一条。
两边同源、都只补缺失的行，同时写也无害。

## 语义与护栏

- **步骤 0 可降级**：办公室模式下办公室日更迟到或到齐检查出错**不中止链路**（`OFFICE_LATE` /
  `OFFICE_CHECK_ERROR`，见上）。topup 模式下 Wind gateway 不可达 / wsd 额度受限时 topup 失败或被
  闸门拦下同样**不中止链路**，改用 `index_daily` 库内现有数据继续，日志与状态文件记 `DEGRADED` /
  `TOPUP_SKIPPED`，新鲜度由步骤 7 兜底；唯一例外是 topup 的事后审计不过（`TOPUP_SUSPECT`）——
  那说明库可能已脏，必须停在信号重算之前。
- **步骤 1–8 硬失败**：任一步非零退出即整链非零退出，状态文件记 `FAILED` + 失败步骤名
  （步骤 7 护栏不过则记 `STALE` / `CHECK_ERROR`，上游冻结另记 `UPSTREAM_STALE`，见下）。
  步骤 8（企业微信推送）只在记账上不同：它失败时信号与护栏都已完成、只是没送达——日志打
  `NOTIFY_FAILED`，状态文件 `result` 仍是 `OK`，原因记在 `notify.error`；但照样退出 1 交告警器，
  因为没送达就等于没人知道。
- **并发锁**：`logs/.daily_signals.lock` 上的 `flock -n`；已有实例在跑时立即退出 75。
  service 配 `SuccessExitStatus=75`，锁冲突不触发告警——占锁的那个实例会推送；不配的话，
  接上企业微信后每次撞锁都是一条假告警。
- **产出护栏（两条命题，任一不过 → 日志打大写 `STALE` + 退出 1 + `"result": "STALE"`）**：
  对象是各生产信号 CSV + 各推荐持仓 + 现货池文件（清单即 `check_freshness.py::GATED`），
  交易日历取自 `index_daily` 本身（避开周末/长假误报）。
  1. **不落后**：末行日期距 `index_daily` 最新交易日不超过 1 个**交易日**。
  2. **无缺口**：每份产出在自己的 `[首行, 末行]` 区间内覆盖日历上**每一个**交易日，
     且不出现日历外日期。缺口明细进 `files[label].gap_dates`（截断 10 条，计数是全量），
     护栏对象合计进顶层 `output_gap_total`。

  **命题 2 为什么是 2026-08-17 补的**：命题 1 只看末行，中间缺一天它看不见。08-12/13 两天
  上游晚到（08-17 11:25 才回填入库），08-14 那晚重算时库里还没有 → 八份产出齐齐跳过两天，
  而末行仍是 08-14，当晚状态文件报 `max_lag: 0`、`breaches: []` 一片绿。08-13 恰是
  equal_weight 的换仓日（pos 0→1），缺它会让持仓序列错判换仓时点，不是完整性洁癖。
  日历随库内容浮动这点是**有意的**：库里没有的天，产出缺它不算产出的错，命题 2 只在
  「库有而产出没有」时报警。处置 = 确认上游有数据后重跑本链路（各生成脚本都是
  `--source pg` 全量重算覆写，跑一次即补齐）。
- **PG 只读**：护栏、等数脚本与信号脚本都只读 `stock_selector.index_daily`；办公室模式（常态）下本链路
  **完全不写库**。回退到 topup 模式时，链路里唯一的写库方是步骤 0 的 `tools/topup_index_daily.sh`
  （stock_selector 的 backfill CLI，幂等 upsert）。
- **上游冻结护栏**：各份产出都是从 `index_daily` 算出来的，上游一冻结，「产出 vs 上游」
  恒为 0 落后、恒报 OK —— 正是本项目停更 35 天没被发现的那种盲区。所以还单独盯上游：
  `index_daily` 最新交易日距今 > 7 个自然日且不在已知假期窗口 → `result: UPSTREAM_STALE`
  + 非零退出。固定日期长假（元旦/劳动节/国庆）内置放宽到 15 天；**春节等农历假期日期
  逐年变，须显式登记**，否则会在长假误报：

  ```bash
  # 二选一：CLI 参数（可重复）或环境变量（逗号分隔）
  python3 deploy/daily_signals/check_freshness.py --holiday-window 2027-02-06:2027-02-17
  systemctl --user edit style-signals-daily.service   # Environment=STYLE_SIGNALS_HOLIDAY_WINDOWS=...
  ```

  宁可长假多报一次假警（一条窗口登记即可消音），也不要在上游真冻结时保持沉默。

  **「上游」的写入方**：护栏日历 = `signals/common/index_codes.csv` 全部 19 码在 `index_daily` 里的
  distinct trade_date（`check_freshness.py` 的 `load_calendar()`）。**2026-09-23 起是单一写入方**：19 码全部由
  数据管理办公室 20:00 夜间作业（stock_selector `scripts/relay_backfill/nightly_wss.sh`）写入——932400~932403
  四个纯风格码 2026-09-21 起，15 个输入码 2026-09-23 起。所以「没取到数」有两种表现，处置相同（办公室会收到
  它自己的告警；看 data_manager 状态，本链路只读）：
  - 夜间作业整晚没写成（WSD 额度耗尽 / 周日 Wind 掉登录 / WSL2 没开）→ 19 码一起停，日历冻结，「产出 vs
    上游」恒为 0 落后，护栏照报 OK——但步骤 0 会记 `OFFICE_LATE`、推送带「⚠ 输入未到齐」行，不再像以前
    那样没人看得见；上游距今 > 7 个自然日才报 `UPSTREAM_STALE`。
  - 只缺本项目输入码、别的码有数把日历推进了 → 产出逐日落后；落后超过 `--max-lag`（默认 1，连续缺的
    第 3 晚）报 `STALE`，不推 + 告警。
  〔2026-09-23 之前是两个写入方，回退到 topup 模式时仍适用〕15 码由本链路 topup 在 18:30 写，4 个纯风格码
  由夜间作业写（2026-09-21 前靠不定期补录），两边取指数都走网关的 wsd `/fetch/index_daily`、吃同一个 Wind
  WSD 额度池：仅 topup 失败 = 上面第二种（按 18:30 准点跑即连续失败的第 3 晚 `STALE`），两边都没取到 =
  第一种。各形态在群里的样子见下「失败形态」表。
- **上游缺口（`UPSTREAM_GAP`，只 WARN 不参与退出码）**：上游冻结护栏只盯最新交易日，
  **库内中间缺天它也看不见**（这就是 08-12/13 的上游侧剧本，记忆里 collector 的
  「回填缝隙」模式）。所以再加一条：近 15 个工作日内、排除已知假期窗口后，
  `index_daily` 里没有任何本项目输入码数据的工作日 → 打 `UPSTREAM_GAP` +
  `upstream.gaps` 字段。**为什么这条不参与退出码**（与上一条不同）：按工作日推算必然把
  调休放假的工作日误判成缺口，且本项目对上游缺口没有处置权——08-12/13 就是上游自己
  在 08-17 11:25 回填补上的，我们能做的只是「看见」，并在下次重算时把产出补齐。
  〔2026-09-23 现状注〕19 码 2026-09-23 起全部由 data_manager 夜间作业写入（每晚回看 11 天补缺），本链路
  只读，「没有处置权」重新成立——缺口归办公室补，看 data_manager 状态。15 个输入码由本链路 topup 自采的那段
  时间（及回退到 topup 模式时）它不确切：topup 每晚回看 14 个自然日、短缺口下一轮自愈，更早的缺口用
  `tools/topup_index_daily.sh <start>` 补。只 WARN 不参与退出码的另一条理由始终成立：按工作日推算会把调休日
  误判成缺口。
  回看窗口用 `--upstream-gap-lookback N` 调（`0` = 关闭）。
- **失败告警**：主 service 的 `OnFailure=` 会拉起 `style-signals-daily-alert.service`，
  写 `logs/ALERT_daily_signals`（时间 + `status.json` 摘要（含 `notify` 段）+ 日志路径 + 处置指引），
  再 best-effort 推一条企业微信失败通知（`notify_wechat.py --alert`，外包 `timeout -k 5 30`、后跟 `||`：
  推不出去只把原因追加进告警文件，告警器自己绝不失败），最后 best-effort 弹 `notify-send`（限时 10 秒）。
  告警器把主 service 本次的启动时刻（`ExecMainStartTimestamp`）经 `--run-started` 传给失败通知，
  用来判断状态文件是不是本次运行写的（见下「企业微信推送」的陈旧告警句）。
  链路会在 topup 审计判可疑/无法验证时**主动中止**——
  中止只有被人知道才安全，否则又是一次无人发现的停摆。
  **告警文件不自动清除**（下次成功也不清），处置完手动 `rm logs/ALERT_daily_signals`，
  免得夜里失败、白天自愈、没人看见。

## Wind wsd 额度耗尽时怎么办 ⚠️

> 〔2026-09-23 现状〕输入指数已移交 data_manager 办公室：取数、额度与纠错都归办公室，额度耗尽时是办公室的
> 夜间作业没写进来（办公室会收到它自己的告警），本链路表现为步骤 0 的 `OFFICE_LATE`，见上「输入指数：办公室
> 模式」。`SKIP_TOPUP` **常驻**，它现在是「办公室模式」的开关。本节以下讲的是 topup 模式（回退用，删掉
> `SKIP_TOPUP` 即回到它）下本链路自己取数时的情形，保留作回退时的操作依据。

### 表现

- 额度耗尽是**常态**，不是异常事件：wsd 与 wss 是**两本独立的账**
  （`stock_selector/data/wind_source.py:1196`："WSD quota may be exhausted independently of WSS"）。
- **网关的 `/quota` 不可信作为放行依据**：2026-08-12 用户通报 wsd 已耗尽，同一时刻
  `GET /quota` 仍返回 `{"used":4955403,"max":500000000}`（≈1%），`/health` 也返回
  `wind_ready:true`。所以 `/quota` 闸门只能**否决**（它自报耗尽时拦住），不能**背书**。
- 真正耗尽时 gateway 对取数请求返回 429 或 `{"status":"error","error":"quota_exceeded"}`，
  stock_selector 侧抛 `QuotaExceeded`，topup 非零退出 → 本链路记 `DEGRADED` 并继续。
- 危险不在"取不到"，而在"取回来的东西不可信却被写进共享生产表 `index_daily`"
  （典型脏数据形态：前值复制占位日——所有指数当日收盘价与前一交易日逐一相等）。

### 三层保护（原则：不可信响应零写入）

| 层 | 何时生效 | 效果 |
|---|---|---|
| `SKIP_TOPUP` 标志文件 / `STYLE_SIGNALS_SKIP_TOPUP=1` | 运维手动置上 | 根本不进入 topup，零写入；理由记进日志与 `status.json` |
| 前置闸门 `topup_guard.py --mode preflight` | 每次自动 | `/ping`+`/health`+`/quota`+"是否真有新交易日"四查，任一不过 → 不调用 topup（零写入），链路降级继续；护栏自身出错也判不过（fail-closed） |
| 事后审计 `topup_guard.py --mode audit` | topup 调用后 | 只读比对调用前后快照；发现未来日期/非法价/前值复制/历史被改写 → `TOPUP_SUSPECT`，**在信号重算之前中止链路**，可疑数据进不了 committed 信号 CSV |

审计的两种失败要分开读（别把「查不了」说成「脏了」）：

| 退出码 | 状态 | 含义 | 处置 |
|---|---|---|---|
| 1 | `SUSPECT` | 审计做成了，**判定写入可疑** | 先判成因：上游对历史的**合法回溯修订**（如 CSI 指数重述、除权口径更正）同样会命中「历史被改写」规则。确属合法修订 → 记录后重跑链路（新快照即新基线）；否则置 `SKIP_TOPUP` 并交 stock_selector 侧核对 |
| 2 | `TOPUP_VERIFY_FAILED` | 审计**没能执行**（快照丢失 / PG 抖动），写入**无法验证**≠ 已确认有问题 | 重跑审计即可：`python3 deploy/daily_signals/topup_guard.py --mode audit --snapshot logs/.topup_pre_snapshot.json`；通过就重跑链路 |

**审计窗口下沿必须对齐**（2026-08-12 修）：`take_snapshot()` 默认取「最近 30 个交易日」，
窗口下沿随数据浮动。topup 每补进一个新交易日，after 窗口整体右移一格，before 最老的
那天就掉出 after，「历史不得被删」规则于是必然命中 —— 审计会变成「topup 什么都没干才
通过、一旦真补上数据就判 SUSPECT」。所以 `run_audit` 用
`take_snapshot(since=before["window_start"])` 把 after 的下沿钉在 before 的下沿，
after 成为 before 的时间超集，规则恢复成它本来的语义：**同一段历史**有没有被改写。
回归测试见 `tests/test_deploy_topup_guard.py::test_audit_false_positive_when_after_window_slides`
（钉住缺陷本身）与 `::test_audit_clean_when_after_window_is_anchored_to_before`（钉住修法）。

前置闸门为什么不是"先落临时文件再校验后导入"：topup 把取数与写库融在 stock_selector 的
一次 CLI 调用里，本项目不得改动 stock_selector，所以可得的最强保证是**存疑就不调用**。

### SKIP_TOPUP 用法

> 〔2026-09-23 起〕标志文件在 = **办公室模式**：步骤 0 不再只打 `TOPUP_SKIPPED`，而是等办公室日更到齐
> （`OFFICE_*`），第一行原因只进日志、不进 `status.json`（`topup_reason` 记的是等数结果）。只想「跳过
> topup、不等数」而不置标志文件，用环境变量 `STYLE_SIGNALS_SKIP_TOPUP=1`（记 `TOPUP_SKIPPED`）。下面的
> 置上 / 解除写法不变，「解除」就是回退到 topup 模式。

```bash
# 置上（内容第一行会被当作原因记进日志；topup 模式时代还记进 status.json）
cat > deploy/daily_signals/SKIP_TOPUP <<'EOF'
2026-08-12 wsd 额度耗尽（用户通报）——今晚跳过 topup，零写入
EOF

# 解除
rm deploy/daily_signals/SKIP_TOPUP
git commit -m "..." deploy/daily_signals/SKIP_TOPUP    # ← 必须一起提交删除
```

> ⚠️ **`SKIP_TOPUP` 是版本控制文件**（有意为之：置上/解除都留审计痕迹）。因此
> **`rm` 之后必须提交这次删除**——否则任何 `git checkout` / `git restore` /
> 切分支都会把它**静默复活**，topup 从此再不执行而链路照样报绿（只是 `topup` 字段
> 一直是 `TOPUP_SKIPPED`）。排查"topup 怎么又不跑了"时，第一件事是
> `git status deploy/daily_signals/SKIP_TOPUP` 和 `ls` 它。

〔2026-09-23 之前的表现〕置上后日志会出现 `TOPUP_SKIPPED(标志文件 …: <原因>)`，`status.json` 里
`"topup": "TOPUP_SKIPPED"` + `"topup_reason": "..."`。**信号侧零损失**：额度耗尽当天本就
取不到可信新数据，各信号线照常用 `index_daily` 库内数据重算，新鲜度护栏仍以库内
`max(trade_date)` 为基准比对，结果照样 `OK`。现在置上后日志出现 `▶ 输入等数（办公室模式；标志文件
SKIP_TOPUP：<原因>）`，`status.json` 里是 `OFFICE_*`。

### 第二天如何恢复

> 〔2026-09-23 起〕下面是 topup 模式下的恢复步骤；办公室模式下「恢复」归办公室，本链路什么都不用做。
> 第 1 步的 `rm` 现在等于**回退到 topup 模式**（连同定时器换回 18:30，见上「输入指数：办公室模式」的回退）。

1. `rm deploy/daily_signals/SKIP_TOPUP`（只需这一步）。
2. **不需要手工补昨天**：`tools/topup_index_daily.sh` 的补跑语义是**范围补齐**，不是只取当日——
   它调用 `stock_selector … backfill date-range --start <默认 14 天前> --end <闸门认可的最后一个交易日>`，
   而 `backfill_date_range` 按整个区间取数 + 幂等 upsert。所以第二天正常跑一次，
   **落下的那一天会连同区间一起补上**。
3. 缺口超过 14 天时显式给起点：`tools/topup_index_daily.sh 2026-07-20`。
4. **盘中补跑只补到上一个交易日**（2026-09-11 起）：END 不是 `date +%F`，而是问
   `topup_guard.py --mode last-trading-day` 要——与前置闸门同一个 `expected_last_trading_day()`。
   15:30 前跑，当天的行根本不去取。
   要看将补哪段而不联网、不写库：`TOPUP_DRY_RUN=1 tools/topup_index_daily.sh`。

   起因是 2026-09-10 09:07 的实录：闸门按 15:30 正确判出「应有的最后交易日 = 09-09」
   并放行补跑（库内当时停在 09-03），而脚本自己 `END=$(date +%F)` 取到 09-10，于是
   15 个指数的当日收盘价全是前值复制的占位行，进了库。那次被事后审计的「前值复制」
   规则接住，但**接住它靠运气**：开盘才 7 分钟，Wind 还在返回前一日收盘；同样的补跑
   发生在 10:30，Wind 返回的是实时价——既不等于前值、也是个有限的正常数字——事后审计
   的每一条规则与同族共动性哨兵全都抓不到。判例见 `tests/test_tools_topup_index_daily.py`。
5. 恢复后确认：`cat logs/daily_signals_status.json` 应看到 `"topup": "OK"`，
   且 `upstream.max_trade_date` 已推进。

## 企业微信推送

2026-09-23 上线（设计 `docs/plans/2026-09-23-wechat-signal-push-design.md`）。此前链路成功时
什么都不发、失败时只写告警文件 + 桌面通知，人不在电脑前就不知道。现在**每个工作日都推**，
不做「持仓变了才推」——静默才不会被误读成成功。

**何时推**：链路步骤 8，护栏通过之后。2026-09-23 起定时器 20:30 + ≤2 分钟随机延迟 + `AccuracySec` 1 分钟起跑，
步骤 0 等办公室日更到齐——夜间作业正常 20:02 前写完，一查即过——整链十几秒，**约 20:31 到**；办公室迟到时
最迟等到 21:30 再用已有数据照算，约 21:31 到（此前定时器 18:30，约 18:31 到）。
护栏未过或任一步失败都**不推持仓**，改由告警器推失败通知。

**推什么**：两池置顶，其余生产线作参考，末尾一行链路体检。池子映射只有一个入口
`backtest/production.py::POOLS`（期货池 = equal_weight 对称、现货池 = slope20 long-flat，
2026-09-10 裁决 `063c322`）；信号值取该持仓文件末行那天的值。2026-09-22 收盘的真实产出：

```
风格择时 信号日 2026-09-22｜链路 OK
【期货池】equal_weight 对称：持多 +1（08-13 起第 29 日）信号值 0.2784
【现货池】slope20 long-flat：持多 +1（09-21 起第 2 日）信号值 0.0461
【其余生产线·参考】
  hybrid20 long-flat：空仓 0（09-02 起第 15 日）信号值 0
  citic40d long-flat：持多 +1（08-14 起第 28 日）信号值 0.4318
  slope20 对称：持多 +1（09-21 起第 2 日）信号值 0.0461
topup OK · 护栏 OK（最大落后 0 交易日 · 缺口 0）
```

（上面是 topup 模式下的实录。办公室模式下末行是 `输入 办公室日更 ✓ · 护栏 OK（最大落后 0 交易日 · 缺口 0）`。）

- 翻仓当日：`【现货池】slope20 long-flat：⚡翻仓 空仓 0 → 持多 +1 信号值 0.0122`
- 信号日不是今天（假日照跑 / 关机后补跑 / 办公室日更迟到或 topup 没取到新数据）：首行 `风格择时 信号日 2026-09-30（今天 10-01）｜链路 OK`
- 某条线末行落后于信号日（护栏允许落后 1 个交易日）：该行附 `（末行 09-29）`
- 办公室日更（步骤 0 办公室模式）：到齐时体检行以 `输入 办公室日更 ✓ ·` 开头、无 ⚠ 行；到 21:30 仍缺码另起一行
  `⚠ 输入未到齐：2026-09-23 截至 21:30 仍缺 3 码：932409.CSI、932000.CSI、000300.SH，按库内已有数据照算`；
  到齐检查出错另起一行 `⚠ 输入到齐检查出错：<原因>`
- topup 降级/跳过（回退到 topup 模式时）：体检行不再以 `topup OK ·` 开头，另起一行 `⚠ topup <状态>：<原因>`
- 上游近窗缺口：加 `⚠ 上游缺 N 天：…`
- 超过 2048 字节（企业微信是整条拒收，不是截断）：从末尾整行删并注明删了几行，池子两行永远在最前

失败通知（链路任一环失败时由告警器推；格式样例，首行是告警器运行时刻）：

```
⚠ 风格择时日更链失败｜2026-09-23 18:31:07
结果 FAILED · 失败步骤 topup_audit(SUSPECT) · 状态写于 2026-09-23T18:31:05+08:00
topup SUSPECT：事后审计判定写入可疑（exit 1）
systemd Result=exit-code
持仓未更新/未送达，以上一次推送为准；处置见 logs/ALERT_daily_signals
```

**只推护栏担保过的文件**：状态文件 `result` 必须是 `OK`，要推的每份持仓文件都必须以
`gated: true` 出现在状态文件 `files` 里、且末行日期与护栏核验时一致——否则拒推（`REFUSED`，
exit 1 → 告警）。这把「推送对象 ⊆ 护栏对象」钉成运行期不变式，而不是靠两份清单碰巧对齐；
现货池文件 `slope20_longflat.csv` 正是为此由「参考」升为护栏对象。

**webhook**：`~/.config/market-monitor/alert.env` 里的 `ALERT_WEBHOOK_URL`（600 权限、在所有仓
之外，与 bs-toolkit、数据管理办公室是同一个机器人）；环境变量 `ALERT_WEBHOOK_URL` 优先。
`qyapi.weixin.qq.com` 是国内端点，本机 Clash 代理会让它失败或变慢，所以代码里显式绕代理，
不依赖单元文件清环境变量。URL 里带 key，**任何输出都抹掉它**（报错、堆栈、状态文件
`notify.error`、告警文件）。网络层错误重试 1 次；`errcode≠0` 属配置/限流问题，不重试。
runner 对推送调用限时 120 秒、再宽限 10 秒强杀（`timeout -k 10 120`）：socket 超时只管单次阻塞
操作、总时长不封顶（DNS 解析根本不受它管，慢回包 / TLS 握手也能一段段拖），真正封顶的是这层限时，
否则卡住会占锁到 service 的 `TimeoutStartSec`（5400 秒）。超时按推送失败处理（日志 `NOTIFY_FAILED` 写明超时，推送调用退出码 124，
宽限后强杀为 137）；这时进程是被杀的、来不及写 `notify` 段，失败通知会落到「状态文件没记下失败
原因」那句兜底，原因看运行日志。

### 失败形态

失败的形态常常和成功长得一样，下表是每种异常在群里的样子：

| 形态 | 表现 |
|---|---|
| 机器没开 | `Persistent=true` 开机补跑，照推；首行注明「今天 X」（补跑时信号日是过去某天，步骤 0 只查一次、不等） |
| 工作日休市（国庆等） | 链路照跑、信号日不变，照推并注明「今天 X」——静默不等于成功（步骤 0 按交易日历判信号日，节假日不等） |
| 办公室日更迟到 / 失败（夜间作业没写进来：WSD 额度耗尽、周日 Wind 掉登录、WSL2 没开；办公室会收到它自己的告警） | 步骤 0 每 5 分钟查一次、等到 21:30 仍不齐 → `OFFICE_LATE`，用库内已有数据照算、约 21:31 照推：首行「信号日 X（今天 Y）」并带「⚠ 输入未到齐：…」行。19 码一起停时日历冻结，护栏照报 OK，超过 7 天 `UPSTREAM_STALE`；只缺本项目码、日历被别的码推进时，连续缺的第 3 晚落后 2 > `max_lag` 1 → `STALE`，**不推持仓** + `OnFailure` 告警 |
| 等数中途才到齐（办公室晚了但 21:30 前写完） | 到齐那一轮即往下走，照推；体检行 `输入 办公室日更 ✓`，原因里记「等 N 秒」（只进日志与状态文件） |
| 到齐检查出错（查询到截止仍失败 / 等数脚本超时或崩溃） | `OFFICE_CHECK_ERROR`，照算、照推，附「⚠ 输入到齐检查出错」行；PG 真连不上时步骤 1 起就会失败 → 不推 + 告警 |
| topup 降级/跳过（回退到 topup 模式时适用） | 照推，附 ⚠ topup 行 |
| 仅 topup 失败（DEGRADED），20:00 夜间作业照常写纯风格 4 码（回退到 topup 模式时适用；描述的是 2026-09-21~09-22 夜间作业只写这 4 码时的形态——回退后若夜间作业仍写 15 个输入码，缺的数当晚 20:00 就会被补上） | 日历仍被夜间作业推进，产出逐日落后：前两晚落后 0 / 1 个交易日，护栏照报 OK、照推，首行「信号日 X（今天 Y）」并带「⚠ topup DEGRADED」行；连续失败的第 3 晚落后 2 > `max_lag` 1 → `STALE`，**不推持仓** + `OnFailure` 告警（见上「上游冻结护栏」的写入方说明） |
| topup 与夜间作业都没取到（回退到 topup 模式时适用：同一个 Wind WSD 额度池耗尽 / 网关不可达） | 日历冻结，护栏照报 OK、照推，但首行显示「信号日 X（今天 Y）」并带「⚠ topup DEGRADED」行——2026-09-14/15 Wind 日额度耗尽两晚就是这个形态（那时夜间作业还没写纯风格码），以前没人看得见（超过 7 天由上游冻结护栏判 `UPSTREAM_STALE`，转为不推 + 告警） |
| 上游近窗缺口（只 WARN） | 照推，附 ⚠ 上游缺口行 |
| 护栏未过 / 任一步失败 / 审计可疑 | **不推持仓**；`OnFailure` → 告警文件 + 失败通知 |
| 推送对象未经护栏担保 | 拒推，exit 1 → `OnFailure` |
| webhook 失败 | 重试 1 次仍败 → `notify.error` 记账、exit 1 → `OnFailure`（告警器再推失败通知；webhook 本身坏了时它多半也送不到，只留在告警文件与桌面通知里） |
| 没配 webhook | exit 1 → `OnFailure`（拒绝静默；`--dry-run` 可预览） |
| 锁冲突（75） | `SuccessExitStatus=75`，不告警——占锁的那个实例会推（办公室迟到时它在步骤 0 最多等到 21:30，期间一直占着锁） |
| 定时器被删 / user manager 没起 | 既有盲区；接上推送后表现为「当天没收到消息」，人能察觉 |

09-14 那晚按当晚产出与护栏读数重放（日志实录：`DEGRADED … exit 1`、`FRESHNESS OK … 上游距今 3 自然日`、
链路「成功」），群里会看到：

```
风格择时 信号日 2026-09-11（今天 09-14）｜链路 OK
【期货池】equal_weight 对称：持多 +1（08-13 起第 22 日）信号值 0.1146
…
护栏 OK（最大落后 0 交易日 · 缺口 0）
⚠ topup DEGRADED：topup 调用失败 exit 1（gateway 不可达 / wsd 额度耗尽 / Wind 报错）
```

**陈旧告警句**：失败通知第二行若是「状态文件停在 …，不是本次运行写的——本次在写状态前就死了
（看 systemd Result）」，说明本次运行在写状态文件之前就被杀了（超时、OOM 等），文件里还是上一次的
内容。判据是告警器传入的 `--run-started`（主 service 本次的 `ExecMainStartTimestamp`，`@unix 秒`）：
`finished_at` 早于它即陈旧；取不到时退回「`finished_at` 的日期 ≠ 今天」。这时通知只说这一句加
systemd `Result`，**不引用**文件里旧的 result / topup / breaches——免得把上一次的原因安到这次头上。
同类兜底句还有「状态文件缺失或无法解析」，以及结果是 `OK` 却触发了告警、又没有推送错误记录时的
「状态文件没记下失败原因——可能在写状态前就被杀了」。

### 手动命令

```bash
# 预览：只打印，不发、不写状态文件；结尾报 webhook 是否已配置
python3 deploy/daily_signals/notify_wechat.py --dry-run
# 补发：发送并把 notify 段写回状态文件
python3 deploy/daily_signals/notify_wechat.py
# 告警演练：按当前状态文件拼失败通知，不发
python3 deploy/daily_signals/notify_wechat.py --alert --dry-run \
    --run-started "$(systemctl --user show style-signals-daily.service -p ExecMainStartTimestamp --value --timestamp=unix)"
```

告警演练不带 `--run-started` 时按日期判陈旧：隔天再跑，得到的就是「状态文件停在 …，不是本次运行
写的」那句。

补发照样过担保校验，但它只比对末行日期：推荐持仓文件的末行日期与护栏记录不一致才拒推（拒推时
重跑整条链路）。同一末行日期下内容被改是发现不了的；只重跑信号脚本也不会碰推荐持仓文件（那是
步骤 6 写的），校验照过。`--dry-run` 永远不写状态文件：白天手动预览不能覆盖掉当晚「已送达」的记录。

## 产物

| 产物 | 说明 |
|---|---|
| `logs/daily_signals_YYYYMMDD.log` | 按日滚动的运行日志（同时进 journal） |
| `logs/daily_signals_status.json` | 最新一次运行的状态：结果、失败步骤、各步耗时、步骤 0 结果与原因、上游最新交易日与是否冻结、每份产出的末行日期与落后交易日数；`notify` 段：推送结果 `sent / at / as_of / bytes / error`（步骤 8 写，`--dry-run` 不写）。步骤 0 结果的字段名仍叫 `topup`（告警器与推送按它读）：办公室模式取 `OFFICE_OK` / `OFFICE_LATE` / `OFFICE_CHECK_ERROR`（`topup_reason` = 等数结果原文，如「办公室日更 2026-09-23 15 码到齐（等 0 秒）」），topup 模式取 `OK` / `DEGRADED` / `TOPUP_SKIPPED` / `SUSPECT` / `TOPUP_VERIFY_FAILED`；`steps` 里这一步也仍叫 `topup` |
| `logs/ALERT_daily_signals` | 失败告警文件（只在失败时出现，**不自动清除**，处置完手动 `rm`） |
| `logs/.inputs_wait.out` | 办公室模式：本次等数脚本输出的副本（进度行 + 末尾三行 `INPUTS_*`，runner 从中解析结果；每次运行先删后写） |
| `logs/.topup_pre_snapshot.json` | topup 模式：topup 调用前的 PG 快照，供事后审计比对 |
| `logs/.daily_signals.lock` | flock 锁文件 |

`logs/` 已在 `.gitignore` 中，不入库。

## 安装（本机，无需 sudo）

```bash
cd /home/elfbob/claude-code/style_timing_signal
mkdir -p ~/.config/systemd/user
cp deploy/daily_signals/style-signals-daily.{service,timer} ~/.config/systemd/user/
cp deploy/daily_signals/style-signals-daily-alert.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now style-signals-daily.timer   # 只 enable timer
loginctl enable-linger "$USER"      # 未登录/重启后 timer 仍生效，普通用户可自设
systemctl --user list-timers style-signals-daily.timer
```

**只 enable timer，不要 enable service。** 两个 service 单元都**故意不带 `[Install]` 段**
（`systemctl --user is-enabled style-signals-daily.service` 应显示 `static`）：
主 service 由 timer 拉起，告警 service 由主 service 的 `OnFailure=` 拉起。
给它们加 `[Install]` 再 enable 会导致每次登录额外跑一次（双跑）。

单元文件里的路径是绝对路径（本机 `/home/elfbob/claude-code/style_timing_signal`）；
换机器部署需同步改 `WorkingDirectory` 与 `ExecStart`。

**改了单元文件要重装**：systemd 跑的是 `~/.config/systemd/user/` 下那份副本，不是仓库里这份。
改了 `.service` / `.timer` 之后要重新 `cp` 过去并 `daemon-reload`，否则不生效（如 2026-09-23 给主
service 加的 `SuccessExitStatus=75`，以及同日办公室模式把 `TimeoutStartSec` 3600 → 5400——不重装，
办公室迟到那晚等满到 21:30 之后，余下的信号重算与推送可能撞上 3600 秒被 systemd 整条杀掉）。脚本（`.sh` / `.py`）则由单元按仓库绝对路径直接
执行，改了下次运行即生效，不用重装。改 `.timer` 的触发时刻另有一坑：systemd 按「上次触发时刻」推算，直接
换文件重载会把「错过的」那次立刻补跑一次——先停定时器、改触发时间戳再启动（办公室回函 02 §2 的做法）。

```bash
cp deploy/daily_signals/style-signals-daily.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

## 手动操作

> ⚠️ **手动跑 runner 或 `systemctl --user start` 会真推到群里**（与 bs-toolkit、数据管理办公室共用
> 同一个机器人）。只重算不推送：`STYLE_SIGNALS_NOTIFY_ARGS=--dry-run deploy/daily_signals/run_daily_signals.sh`
> ——步骤 8 只打印不发，前面的步骤 0（等数 / topup）、信号重算、护栏照常。

> **手工重跑会不会等**（办公室模式）：只有**交易日 20:00~21:30 之间、且当天数据还没到**时会等（每 5 分钟查
> 一次，最迟到 21:30，期间一直占着锁）；其余时刻（白天、次日补跑、节假日）信号日是过去某天，只查一次不等。
> 不想等：`STYLE_SIGNALS_INPUTS_ARGS=--once deploy/daily_signals/run_daily_signals.sh`（只查一次，没齐就记
> `OFFICE_LATE`、用库内已有数据照算）。

```bash
systemctl --user start style-signals-daily.service      # 立即跑一次
systemctl --user status style-signals-daily.service     # 上次结果
journalctl --user -u style-signals-daily.service -n 50  # 日志（或看 logs/ 下的文件）
python3 deploy/daily_signals/check_freshness.py         # 只跑护栏，不改任何产出
python3 deploy/daily_signals/wait_for_inputs.py --once  # 只看输入到齐没有（只读 PG，不改任何产出）
deploy/daily_signals/run_daily_signals.sh               # 不经 systemd 直接跑
```

环境变量（都可在 `systemctl --user edit style-signals-daily.service` 里覆盖）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `STYLE_SIGNALS_PYTHON` | 自动探测（`.venv` → miniconda → PATH） | 指定解释器 |
| `STYLE_SIGNALS_INPUTS_ARGS` | 空 | 办公室模式：透传给 `wait_for_inputs.py`，如 `--once`（只查一次、不等）；也认 `--deadline HH:MM` / `--interval 秒` / `--ready-from HH:MM`。写错了步骤 0 记 `OFFICE_CHECK_ERROR`，链路照常往下走 |
| `STYLE_SIGNALS_SKIP_TOPUP` | `0` | topup 模式：`1` = 跳过步骤 0（`TOPUP_SKIPPED`，不写库、不等数，只用库内现有数据）；标志文件在时不看它 |
| `STYLE_SIGNALS_MAX_LAG` | `1` | 护栏允许落后的交易日数 |
| `STYLE_SIGNALS_TOPUP_TIMEOUT` | `900` | topup 模式：步骤 0 超时秒数 |
| `STYLE_SIGNALS_NOTIFY_ARGS` | 空 | runner 与告警器都透传给 `notify_wechat.py`，如 `--dry-run`（只打印不发、不写状态文件）。告警器是另一个单元，在主 service 里设的值传不到它 |

## 历史零篡改

因为是全量重算覆写，每次运行都可以用 `git diff --stat output/` 直接验证：正常情况下
只应看到尾部新增行，历史段字节不变。补跑/改动前建议先备份各信号线 CSV 与
`output/recommended/`（2026-08-12 首次补跑的备份在 `~/backups/style_timing_signal/`）。

**一个例外必须知道：补一个中间缺口会合法修订缺口之后那些行的值。** 各信号线都是
滚动窗口（citic40d 40 日 z、equal_weight 20 日 lookback×40 日 z、slope20 20 日斜率×120 日 z、
hybrid20 同族），缺口被补上后窗口成员变化，**缺口之后、窗口长度之内的行会重算出不同的值**。
2026-08-17 补 08-12/13 两天时实测（slope20 当时尚未进日更链路）：逐日比对回填前后，3974/3066/3724 个共同日期里
**各只有 08-14 一行变**（更早历史逐位不变），但那一行两条线的仓位直接翻了 ——

    citic40d      signal -0.0642 → +0.2880    仓位 0 → 1
    equal_weight  signal -0.0399 → +0.1970    仓位 0 → 1
    hybrid20      signal -1.0000 →  0.0000    仓位 0 → 0（不变）

缺数据时算出的旧值是被污染的窗口产物，补齐后的新值才是正确值 —— 这是**修正而非
篡改**。但「已发布的信号被事后修订」这件事本身必须可见，所以：**补缺口后要主动
比对回填前后的差异并登记**，别只看 `git diff --stat` 的行数（相对 HEAD 它可能
全算「新增」，把值的变化完全藏住 —— 这次就是：`+3 −0` 看着像纯追加）。

## 护栏怎么演示

`--root` 让护栏检查任意 `output` 树副本，因此可以在**不碰生产文件**的前提下演示：

```bash
DEMO=/tmp/stale_demo && rm -rf "$DEMO" && mkdir -p "$DEMO" && cp -a output "$DEMO"/
head -n -10 "$DEMO/output/equal_weight/equal_weight_signal_20d40z.csv" > /tmp/t \
  && mv /tmp/t "$DEMO/output/equal_weight/equal_weight_signal_20d40z.csv"
python3 deploy/daily_signals/check_freshness.py --root "$DEMO" --max-lag 1; echo "exit=$?"
```

2026-08-12 实测：砍掉 `equal_weight_signal_20d40z.csv` 与 `equal_weight_longflat.csv` 末 10 行后，
护栏打印

```
  STALE  equal_weight_20d40z: 末行 2026-07-28，落后上游 10 个交易日 > 1
  STALE  recommended_equal_weight: 末行 2026-07-28，落后上游 10 个交易日 > 1
```

并 `exit=1`；同一时刻对真实 `output/` 跑同一条命令 `exit=0` / `FRESHNESS OK`。

**命题 2（缺口）的演示**——挖掉中间两天，末行不动，这正是 08-12/13 的形状：

```bash
DEMO=/tmp/gap_demo && rm -rf "$DEMO" && mkdir -p "$DEMO" && cp -a output "$DEMO"/
for f in "$DEMO"/output/*/*.csv; do grep -v "^2026-08-1[23]," "$f" > /tmp/t && mv /tmp/t "$f"; done
python3 deploy/daily_signals/check_freshness.py --root "$DEMO" --max-lag 1; echo "exit=$?"
```

2026-08-17 实测（就是回填前那份产物的真实副本）：六份护栏对象各报

```
  [护栏] citic40d                   末行 2026-08-14   落后 0 交易日 缺 2 交易日
  STALE  citic40d: 区间 2010-04-02..2026-08-14 内缺 2 个交易日（2026-08-12、2026-08-13）
  STALE  护栏对象缺口合计 12 天；处置 = 确认上游这些天有数据后重跑本链路
```

`exit=1`。**注意 `落后 0 交易日`**——命题 1 在这份产物上完全通过，当晚也确实报了绿；
抓住它的是命题 2。回填后对真实 `output/` 跑同一命令 `exit=0` / `区间内缺口 0`。
跑在链路里时，这条非零退出会让 systemd 把 service 记成 `failed`
（`systemctl --user status style-signals-daily.service` 一眼可见），状态文件 `"result": "STALE"`。

护栏逻辑的单测在 `tests/test_deploy_freshness_guard.py`（14 例，不连库）。
