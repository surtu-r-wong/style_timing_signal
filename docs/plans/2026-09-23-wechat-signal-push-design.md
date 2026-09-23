# 日更信号企业微信推送 —— 设计文档

> 起草：2026-09-23
> 范围：给现有 18:30 日更链补上「推送企业微信」这一环。不改信号、不改持仓口径、不新增定时器、不碰 Wind。
> 参照：`~/codex/bs-toolkit/deploy/daily_signal/`（作业末步推送 + `OnFailure` 告警器推同一 webhook）。

## 0. 一句话

定时计算已经有了（`style-signals-daily.timer` 工作日 18:30），缺的是**外推**：成功时什么都不发，失败时只写
`logs/ALERT_daily_signals` 和桌面通知，人不在电脑前就不知道。本设计在链路末尾加一步 `notify_wechat.py`：
护栏通过 → 推当日持仓（两池置顶、其余生产线作参考、链路体检一行）；链路任一环失败 → 告警器在写告警文件之外
再推一条失败通知。webhook 复用 `~/.config/market-monitor/alert.env` 的 `ALERT_WEBHOOK_URL`
（bs-toolkit 与数据管理办公室用的同一个机器人）。

## 1. 事实

| | |
|---|---|
| 定时计算 | `style-signals-daily.timer` 工作日 18:30（+≤2min 随机），整链约 20 秒：topup → 四线信号 → 推荐持仓 → 新鲜度护栏，状态写 `logs/daily_signals_status.json` |
| 现状缺口 | 成功无外推；失败只写告警文件 + `notify-send`（桌面） |
| 通道 | 企业微信群机器人；text 消息 ≤ 2048 字节，超长**整条拒收**；每机器人 20 条/分钟 |
| 代理 | `qyapi.weixin.qq.com` 是国内端点，本机 Clash 代理会让它失败或变慢（同 `ops-pip-mirror-bypass-proxy` 那个坑）→ 代码里显式绕代理，不依赖单元文件清环境变量 |
| 实盘映射 | 2026-09-10 两池裁决（`063c322`）：**期货池 = equal_weight 对称**、**现货池 = slope20 long-flat** |
| 护栏盲点 | 现货池文件 `output/recommended/slope20_longflat.csv` 在 `check_freshness.py` 里登记为「参考」（只报不拦）。推送要把它当可行动持仓发出去，它必须升为护栏对象 |
| 锁冲突 | runner 在已有实例运行时退出 75，但主 service 没配 `SuccessExitStatus=75`（bs-toolkit 配了），会误触 `OnFailure`。告警接上微信后这会变成假报警 |

## 2. 流程

### 2.1 成功路径（runner 步骤 8，护栏通过之后）

`deploy/daily_signals/notify_wechat.py`：

1. **只推护栏担保过的东西**：读状态文件，`result` 必须是 `OK`；要推的每份持仓文件都必须在状态文件
   `files` 里以 `gated: true` 出现，且其 `last_date` 与文件当前末行一致——否则拒推（exit 1）。
   这条把「推送对象 ⊆ 护栏对象」钉成运行期不变式，而不是只靠两份清单碰巧对齐。
2. **映射只有一个入口**：新增 `backtest/production.py::POOLS`（池 → (信号线, 口径)）与 `POOL_FILES`；
   其余生产线取 `PRODUCTION_MAPPING` 中不属于任何池的条目；信号值取该行持仓文件末行日期当天的值。
   不在推送脚本里另拼文件名。
3. **组消息**（≤ 2048 字节，超长从末尾整行删并注明删了几行，池子两行永远在最前）。
4. **发送**：`msgtype=text`；显式 `ProxyHandler({})` 绕代理；超时 10 秒；**网络层错误重试 1 次**
   （errcode≠0 属配置/限流问题，不重试）；`errcode≠0` 即失败。URL 含 key，**任何输出都不打印它**。
5. **回写**：成功或失败都把 `notify` 段（`sent / at / as_of / bytes / error`）写回状态文件（原子替换）；
   发送失败 → exit 1 → runner exit 1 → `OnFailure`。`--dry-run` 只打印消息，**不写状态文件**
   （白天手动预览不能覆盖掉当晚「已送达」的记录）。

### 2.2 失败路径（`OnFailure` 告警器）

`alert_on_failure.sh` 照旧写告警文件，之后 best-effort 调
`notify_wechat.py --alert --systemd-result <Result>`（`timeout 30`，`|| true`，告警器自己绝不失败）。
`--alert` 模式只读状态文件、不 import pandas（科学栈坏了也能报警），按 `result / failed_step / topup /
breaches / upstream_breach / notify.error` 拼一条短通知。状态文件缺失，或结果是 `OK` 却触发了告警
（在写状态前就被杀，如超时/OOM），都如实写出来，并附 systemd `Result`。

**实现增补（2026-09-23）**：告警器另传 `--run-started`（主 service 本次的 `ExecMainStartTimestamp`，
`--timestamp=unix` 取 `@unix 秒`）：状态文件的 `finished_at` 早于它，即判为上一次运行留下的，通知只说
这一句、不引用旧原因；取不到时退回按日期判断。告警器的实际调用是
`timeout -k 5 30 … notify_wechat.py … --alert … || echo …`（输出追加进告警文件，`notify-send` 另限时
10 秒）。runner 步骤 8 的推送调用外包 `timeout -k 10 120`——socket 超时只管单次阻塞操作、总时长不封顶
（DNS 根本不受它管）；超时按推送失败处理（`NOTIFY_FAILED`，推送调用退出码 124 / 137，链路 exit 1）。

### 2.3 消息样式

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

- 翻仓当日：`【现货池】slope20 long-flat：⚡翻仓 空仓 0 → 持多 +1 信号值 0.0122`
- 信号日不是今天（假日照跑 / 关机后补跑）：首行 `风格择时 信号日 2026-09-30（今天 10-01）｜链路 OK`
- 某条线末行落后于信号日（护栏允许落后 1 个交易日）：该行附 `（末行 09-29）`
- topup 降级/跳过：加 `⚠ topup TOPUP_SKIPPED：<原因>`；上游近窗缺口：加 `⚠ 上游缺 N 天：…`

失败通知：

```
⚠ 风格择时日更链失败｜2026-09-23 18:31:07
结果 FAILED · 失败步骤 topup_audit(SUSPECT) · 状态写于 2026-09-23T18:31:05+08:00
topup SUSPECT：事后审计判定写入可疑（exit 1）
systemd Result=exit-code
持仓未更新/未送达，以上一次推送为准；处置见 logs/ALERT_daily_signals
```

## 3. 部署件

| 文件 | 改动 |
|---|---|
| `deploy/daily_signals/notify_wechat.py` | 新增（成功推送 + `--alert` 失败通知 + `--dry-run`） |
| `deploy/daily_signals/run_daily_signals.sh` | 护栏通过后加步骤 8；`STYLE_SIGNALS_NOTIFY_ARGS` 透传（如 `--dry-run`） |
| `deploy/daily_signals/alert_on_failure.sh` | 写告警文件后 best-effort 推失败通知 |
| `deploy/daily_signals/style-signals-daily.service` | `SuccessExitStatus=75`（需重装单元 + `daemon-reload`） |
| `deploy/daily_signals/check_freshness.py` | 现货池文件由「参考」升「护栏」 |
| `backtest/production.py` | 新增 `POOLS` / `POOL_FILES` |
| `deploy/daily_signals/README.md` | 新增「企业微信推送」一节 + 失败形态 |

## 4. 失败形态（「失败的形态和成功的形态长得一样」）

| 形态 | 表现 |
|---|---|
| 机器没开 | `Persistent=true` 开机补跑，照推；首行注明「今天 X」 |
| 工作日休市（国庆等） | 链路照跑、信号日不变，照推并注明「今天 X」——静默不等于成功 |
| topup 降级/跳过 | 照推，附 ⚠ topup 行 |
| 上游近窗缺口（只 WARN） | 照推，附 ⚠ 上游缺口行 |
| 护栏未过 / 任一步失败 / 审计可疑 | **不推持仓**；`OnFailure` → 告警文件 + 失败通知 |
| 推送对象未经护栏担保 | 拒推，exit 1 → `OnFailure` |
| webhook 失败 | 重试 1 次仍败 → `notify.error` 记账、exit 1 → `OnFailure`（告警器再推失败通知） |
| 没配 webhook | exit 1 → `OnFailure`（拒绝静默；`--dry-run` 可预览） |
| 锁冲突（75） | `SuccessExitStatus=75`，不告警——占锁的那个实例会推 |
| 定时器被删 / user manager 没起 | 既有盲区；接上推送后表现为「当天没收到消息」，人能察觉 |

## 5. 测试

`tests/test_deploy_notify_wechat.py`：消息内容（两池置顶、翻仓、连续天数、今天≠信号日、末行落后、降级行）、
2048 字节截断保池子行、担保校验（非 OK / 非 gated / 末行不一致 → 拒推）、发送（请求体、绕代理、
errcode≠0、网络错误重试一次、异常文本不含 URL）、env 文件解析、`--dry-run` 不写状态、`--alert` 各分支。
另补：`POOL_FILES` 必须是 `write_recommended_positions` 实际写出的文件、且都在护栏清单里。
对关键判断做变异验证（改坏被测逻辑确认用例变红）。

## 6. 明确不做

- 不新增定时器、不改推送时刻（链路跑完即推，约 18:31）。
- 不用 markdown 消息（企业微信与微信互通群里显示不了），不 @ 人。
- 不做「持仓变了才推」——每天推，静默才不会被误读成成功。
- 不落推送历史（全文在当日运行日志里）。
