# deploy/daily_signals —— 日更信号链自动化

本仓库在 2026-08-12 之前**没有任何自动化**：三条信号脚本与 `backtest.production` 一直靠人手
触发，于是 2026-07-09 之后没人跑，三条生产信号 CSV 停在 2026-07-08、推荐持仓停在 07-09，
**停更 35 天无人发现**（归因见 `docs/plans/2026-08-12-project-review-and-priorities.md` §3.1，
用户裁决见同文档 §9）。本目录是那次裁决的落地：一个 runner + 一个 systemd user timer +
一条产出护栏（**两条命题**：不落后 + 无缺口，后者 2026-08-17 补，成因见下「产出护栏」）。

## 文件

| 文件 | 作用 |
|---|---|
| `run_daily_signals.sh` | 链路 runner：topup → 三条信号 → 推荐持仓 → 护栏；带 flock、分步计时、日志、状态文件 |
| `check_freshness.py` | 产出护栏（只读 PG）：末行不落后 + 区间无缺口，兼状态 JSON 写入器 |
| `topup_guard.py` | topup 写库护栏：前置闸门（只读 gateway+PG）+ 事后审计（只读 PG） |
| `alert_on_failure.sh` | 失败告警器：写 `logs/ALERT_daily_signals` + best-effort `notify-send` |
| `style-signals-daily-alert.service` | 告警单元，由主 service 的 `OnFailure=` 拉起 |
| `SKIP_TOPUP`（可选） | 存在即跳过 topup，文件第一行是原因；见下「Wind wsd 额度耗尽时怎么办」 |
| `style-signals-daily.service` | systemd user service（oneshot），单元副本 |
| `style-signals-daily.timer` | systemd user timer，工作日 18:30 Asia/Shanghai，`Persistent=true` |

## 链路

```
tools/topup_index_daily.sh                     # 步骤 0，允许失败 → DEGRADED
signals/hybrid20/update_growth_stability.py    # 步骤 1
signals/hybrid20/update_confirmed_signal.py    # 步骤 2
signals/citic40d/generate_signal.py            # 步骤 3
signals/equal_weight/generate_signal.py        # 步骤 4（变体A / 生产口径 20d40z）
signals/equal_weight/generate_signal.py …5d20z # 步骤 5（变体B / 参考口径）
python -m backtest.production                  # 步骤 6 → output/recommended/
deploy/daily_signals/check_freshness.py        # 步骤 7 护栏
```

四个生成脚本都是**全量重算覆写**（读 PG 全历史 → `to_csv` 覆盖），不是追加。因此：
断更 N 天后直接跑一次就完成补跑，无需专门的补跑模式；反过来也意味着历史段每天都会被
重算一遍，历史零变化是可验证的（见下「历史零篡改」）。

## 语义与护栏

- **步骤 0 可降级**：Wind gateway 不可达 / wsd 额度受限时 topup 失败或被闸门拦下**不中止链路**，
  改用 `index_daily` 库内现有数据继续，日志与状态文件记 `DEGRADED` / `TOPUP_SKIPPED`，
  新鲜度由步骤 7 兜底。唯一例外是事后审计不过（`TOPUP_SUSPECT`）——那说明库可能已脏，
  必须停在信号重算之前。
- **步骤 1–7 硬失败**：任一步非零退出即整链非零退出，状态文件记 `FAILED` + 失败步骤名。
- **并发锁**：`logs/.daily_signals.lock` 上的 `flock -n`；已有实例在跑时立即退出 75。
- **产出护栏（两条命题，任一不过 → 日志打大写 `STALE` + 退出 1 + `"result": "STALE"`）**：
  对象都是三条生产信号 CSV + 三份推荐持仓，交易日历取自 `index_daily` 本身（避开周末/长假误报）。
  1. **不落后**：末行日期距 `index_daily` 最新交易日不超过 1 个**交易日**。
  2. **无缺口**：每份产出在自己的 `[首行, 末行]` 区间内覆盖日历上**每一个**交易日，
     且不出现日历外日期。缺口明细进 `files[label].gap_dates`（截断 10 条，计数是全量），
     护栏对象合计进顶层 `output_gap_total`。

  **命题 2 为什么是 2026-08-17 补的**：命题 1 只看末行，中间缺一天它看不见。08-12/13 两天
  上游晚到（08-17 11:25 才回填入库），08-14 那晚重算时库里还没有 → 八份产出齐齐跳过两天，
  而末行仍是 08-14，当晚状态文件报 `max_lag: 0`、`breaches: []` 一片绿。08-13 恰是
  equal_weight 的换仓日（pos 0→1），缺它会让持仓序列错判换仓时点，不是完整性洁癖。
  日历随库内容浮动这点是**有意的**：库里没有的天，产出缺它不算产出的错，命题 2 只在
  「库有而产出没有」时报警。处置 = 确认上游有数据后重跑本链路（四个生成脚本都是
  `--source pg` 全量重算覆写，跑一次即补齐）。
- **PG 只读**：护栏与信号脚本都只读 `stock_selector.index_daily`；链路里唯一的写库方是
  步骤 0 的 `tools/topup_index_daily.sh`（stock_selector 的 backfill CLI，幂等 upsert）。
- **上游冻结护栏**：三条产出是从 `index_daily` 算出来的，上游一冻结，「产出 vs 上游」
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
- **上游缺口（`UPSTREAM_GAP`，只 WARN 不参与退出码）**：上游冻结护栏只盯最新交易日，
  **库内中间缺天它也看不见**（这就是 08-12/13 的上游侧剧本，记忆里 collector 的
  「回填缝隙」模式）。所以再加一条：近 15 个工作日内、排除已知假期窗口后，
  `index_daily` 里没有任何本项目输入码数据的工作日 → 打 `UPSTREAM_GAP` +
  `upstream.gaps` 字段。**为什么这条不参与退出码**（与上一条不同）：按工作日推算必然把
  调休放假的工作日误判成缺口，且本项目对上游缺口没有处置权——08-12/13 就是上游自己
  在 08-17 11:25 回填补上的，我们能做的只是「看见」，并在下次重算时把产出补齐。
  回看窗口用 `--upstream-gap-lookback N` 调（`0` = 关闭）。
- **失败告警**：主 service 的 `OnFailure=` 会拉起 `style-signals-daily-alert.service`，
  写 `logs/ALERT_daily_signals`（时间 + `status.json` 摘要 + 日志路径 + 处置指引）并
  best-effort 弹 `notify-send`。链路会在 topup 审计判可疑/无法验证时**主动中止**——
  中止只有被人知道才安全，否则又是一次无人发现的停摆。
  **告警文件不自动清除**（下次成功也不清），处置完手动 `rm logs/ALERT_daily_signals`，
  免得夜里失败、白天自愈、没人看见。

## Wind wsd 额度耗尽时怎么办 ⚠️

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

```bash
# 置上（内容第一行会被当作原因记进日志与 status.json）
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

置上后日志会出现 `TOPUP_SKIPPED(标志文件 …: <原因>)`，`status.json` 里
`"topup": "TOPUP_SKIPPED"` + `"topup_reason": "..."`。**信号侧零损失**：额度耗尽当天本就
取不到可信新数据，三条信号照常用 `index_daily` 库内数据重算，新鲜度护栏仍以库内
`max(trade_date)` 为基准比对，结果照样 `OK`。

### 第二天如何恢复

1. `rm deploy/daily_signals/SKIP_TOPUP`（只需这一步）。
2. **不需要手工补昨天**：`tools/topup_index_daily.sh` 的补跑语义是**范围补齐**，不是只取当日——
   它调用 `stock_selector … backfill date-range --start <默认 14 天前> --end <今天>`，
   而 `backfill_date_range` 按整个区间取数 + 幂等 upsert。所以第二天正常跑一次，
   **落下的那一天会连同区间一起补上**。
3. 缺口超过 14 天时显式给起点：`tools/topup_index_daily.sh 2026-07-20`。
4. 恢复后确认：`cat logs/daily_signals_status.json` 应看到 `"topup": "OK"`，
   且 `upstream.max_trade_date` 已推进。

## 产物

| 产物 | 说明 |
|---|---|
| `logs/daily_signals_YYYYMMDD.log` | 按日滚动的运行日志（同时进 journal） |
| `logs/daily_signals_status.json` | 最新一次运行的状态：结果、失败步骤、各步耗时、topup 结果与原因、上游最新交易日与是否冻结、每份产出的末行日期与落后交易日数 |
| `logs/ALERT_daily_signals` | 失败告警文件（只在失败时出现，**不自动清除**，处置完手动 `rm`） |
| `logs/.topup_pre_snapshot.json` | topup 调用前的 PG 快照，供事后审计比对 |
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

## 手动操作

```bash
systemctl --user start style-signals-daily.service      # 立即跑一次
systemctl --user status style-signals-daily.service     # 上次结果
journalctl --user -u style-signals-daily.service -n 50  # 日志（或看 logs/ 下的文件）
python3 deploy/daily_signals/check_freshness.py         # 只跑护栏，不改任何产出
deploy/daily_signals/run_daily_signals.sh               # 不经 systemd 直接跑
```

环境变量（都可在 `systemctl --user edit style-signals-daily.service` 里覆盖）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `STYLE_SIGNALS_PYTHON` | 自动探测（`.venv` → miniconda → PATH） | 指定解释器 |
| `STYLE_SIGNALS_SKIP_TOPUP` | `0` | `1` = 跳过步骤 0（不写库，只用库内现有数据） |
| `STYLE_SIGNALS_MAX_LAG` | `1` | 护栏允许落后的交易日数 |
| `STYLE_SIGNALS_TOPUP_TIMEOUT` | `900` | 步骤 0 超时秒数 |

## 历史零篡改

因为是全量重算覆写，每次运行都可以用 `git diff --stat output/` 直接验证：正常情况下
只应看到尾部新增行，历史段字节不变。补跑/改动前建议先备份三条信号 CSV 与
`output/recommended/`（2026-08-12 首次补跑的备份在 `~/backups/style_timing_signal/`）。

**一个例外必须知道：补一个中间缺口会合法修订缺口之后那些行的值。** 三条信号都是
滚动窗口（citic40d 40 日 z、equal_weight 20 日 lookback×40 日 z、hybrid20 同族），
缺口被补上后窗口成员变化，**缺口之后、窗口长度之内的行会重算出不同的值**。
2026-08-17 补 08-12/13 两天时实测：逐日比对回填前后，3974/3066/3724 个共同日期里
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
