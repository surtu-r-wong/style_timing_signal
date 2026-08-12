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

- **步骤 0 可降级**：Wind gateway 不可达 / wsd 额度受限时 topup 失败**不中止链路**，
  改用 `index_daily` 库内现有数据继续，日志与状态文件记 `DEGRADED`，新鲜度由步骤 7 兜底。
- **步骤 1–7 硬失败**：任一步非零退出即整链非零退出，状态文件记 `FAILED` + 失败步骤名。
- **并发锁**：`logs/.daily_signals.lock` 上的 `flock -n`；已有实例在跑时立即退出 75。
- **新鲜度护栏**：三条生产信号 CSV + 三份推荐持仓的末行日期，距 `index_daily` 最新交易日
  不得超过 1 个**交易日**（交易日历取自 `index_daily` 本身，避开周末/长假误报）。超限 →
  日志打大写 `STALE` + 退出 1 + 状态文件 `"result": "STALE"`。
  上游自身相对今天的滞后只出 `WARN`，不影响退出码（长假期间必然变大）。
- **PG 只读**：护栏与信号脚本都只读 `stock_selector.index_daily`；链路里唯一的写库方是
  步骤 0 的 `tools/topup_index_daily.sh`（stock_selector 的 backfill CLI，幂等 upsert）。

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
