# deploy/daily_signals —— 日更信号链自动化

本仓库在 2026-08-12 之前**没有任何自动化**：三条信号脚本与 `backtest.production` 一直靠人手
触发，于是 2026-07-09 之后没人跑，三条生产信号 CSV 停在 2026-07-08、推荐持仓停在 07-09，
**停更 35 天无人发现**（归因见 `docs/plans/2026-08-12-project-review-and-priorities.md` §3.1，
用户裁决见同文档 §9）。本目录是那次裁决的落地：一个 runner + 一个 systemd user timer +
一条新鲜度护栏。

## 文件

| 文件 | 作用 |
|---|---|
| `run_daily_signals.sh` | 链路 runner：topup → 三条信号 → 推荐持仓 → 护栏；带 flock、分步计时、日志、状态文件 |
| `check_freshness.py` | 新鲜度护栏（只读 PG）+ 状态 JSON 写入器 |
| `topup_guard.py` | topup 写库护栏：前置闸门（只读 gateway+PG）+ 事后审计（只读 PG） |
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
- **新鲜度护栏**：三条生产信号 CSV + 三份推荐持仓的末行日期，距 `index_daily` 最新交易日
  不得超过 1 个**交易日**（交易日历取自 `index_daily` 本身，避开周末/长假误报）。超限 →
  日志打大写 `STALE` + 退出 1 + 状态文件 `"result": "STALE"`。
  上游自身相对今天的滞后只出 `WARN`，不影响退出码（长假期间必然变大）。
- **PG 只读**：护栏与信号脚本都只读 `stock_selector.index_daily`；链路里唯一的写库方是
  步骤 0 的 `tools/topup_index_daily.sh`（stock_selector 的 backfill CLI，幂等 upsert）。

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
```

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
| `logs/daily_signals_status.json` | 最新一次运行的状态：结果、失败步骤、各步耗时、上游最新交易日、每份产出的末行日期与落后交易日数 |
| `logs/.daily_signals.lock` | flock 锁文件 |

`logs/` 已在 `.gitignore` 中，不入库。

## 安装（本机，无需 sudo）

```bash
cd /home/elfbob/claude-code/style_timing_signal
mkdir -p ~/.config/systemd/user
cp deploy/daily_signals/style-signals-daily.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now style-signals-daily.timer
loginctl enable-linger "$USER"      # 未登录/重启后 timer 仍生效，普通用户可自设
systemctl --user list-timers style-signals-daily.timer
```

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
跑在链路里时，这条非零退出会让 systemd 把 service 记成 `failed`
（`systemctl --user status style-signals-daily.service` 一眼可见），状态文件 `"result": "STALE"`。

护栏逻辑的单测在 `tests/test_deploy_freshness_guard.py`（14 例，不连库）。
