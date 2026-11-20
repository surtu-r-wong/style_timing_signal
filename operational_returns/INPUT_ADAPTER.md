# 只读当前输入适配

入口：`python3 -m operational_returns.input_adapter`。不初始化或推进账户、不导出 ReturnFeed、不写正式 latest。所有运行输出须使用路径中包含 `input-staging` 的新目录；禁止覆盖已有目录以及仓库 output/logs/deploy。每次尝试生成独立 `report.json`，失败不留下 `event.json`。同一次运行的目录完成后整体发布；失败重试用新目录。

## 命令与来源

| 命令 | 行为与边界 |
|---|---|
| `inspect --root ROOT --staging NEW_DIR` | 在已有生产共享锁内读取状态与原生登记映射对应 CSV，只交结构/来源，不选择账户或生成信号 |
| `capture STRATEGY MAPPING --root ROOT --staging NEW_DIR` | 复用 `capture_signal` 的生产锁和完成状态校验，生成不可覆盖的 `signal.json`；实际调用时钟作为观测时间，另记录状态/CSV/登记文件摘要 |
| `catalog --staging NEW_DIR` | 只检查三张已有表的列结构，不选择标的、不检查行情覆盖 |
| `market SOURCE SYMBOL YYYY-MM-DD --staging NEW_DIR` | 显式单标的单日 SELECT，最多 3 行，仅生成 `market.csv` 候选，永不自动声明可成交 |
| `prepare CONFIG --staging NEW_DIR` | 读取固定摘要的信号、完整日历、价格 CSV 与外部证据；全部通过才输出既有 event 契约 |

数据库读取复用 `signals.common.config.load_db_config`，不打印配置。连接超时 8 秒，事务 `READ ONLY / REPEATABLE READ`，语句超时 8 秒，结束 rollback/close。仅固定 SELECT，没有写共享库、DDL、Wind 调用、历史回填或任意 SQL 参数入口。运行核心仍仅标准库；`catalog/market` 另复用现有 psycopg2、PyYAML。

| SOURCE | 表及字段 | 支持与不足 |
|---|---|---|
| `futures_daily` | `public.futures_daily`: `trade_date,symbol,open,close,settle,volume,oi` | 开盘/收盘/结算日线候选；不能推断具体执行时点、涨跌停可成交、来源发布时刻或到期日。不能把 close 替代缺失 settle |
| `index_daily` | `stock_selector.index_daily`: `trade_date,index_code,open,close` | 指数参考价；指数本身不是已确认的可交易标的，不能自动代理 ETF/期货 |
| `stock_status` | `stock_selector.stock_status`: `trade_date,ts_code,is_suspended,limit_up,limit_down,updated_at` | 日度状态候选；空记录不代表正常交易，布尔涨跌停也不能证明某时刻可成交或双向成交 |

`catalog` 查到的 `created_at/updated_at` 是数据库字段，不自动解释为价格发布时间或首次取得时刻；期货表时间列没有时区，更不能擅加时区。数据库 `transaction_at` 只证明查询事务时间。`market` 不为价格补会话时刻；原始候选不能直接通过 `prepare`，需要来源方提供会话语义和证据。没有现金证券真实成交价格接口或完整交易所日历时，保留缺口。

## 参数与证据约定

复制 [适配模板](../examples/operational_returns/input-adapter.template.json) 与 [证据模板](../examples/operational_returns/input-evidence.template.json)。所有 null 均待落实，不能使用模板生成可执行包。适配配置不需要资金、最终组合权重、费用或保证金；需要明确的原生键、允许的映射、唯一标的、类型、数据日与真实来源。`ready` 仅说明 event 输入结构与所提交证据一致，不表示经济参数已验收，更不授权账户推进。

四个来源描述符均需 `path,sha256,project,host`；相对路径相对于配置文件。path 为读取到的本地文件，project/host 为来源方声明；实际读取主机另在报告 `observer_host`，原始远端位置须写入证据 `reference`。SHA-256 为文件原字节摘要。配置自身也记摘要。信号沿用 `capture-signal` 输出的七字段，保存原 `available_at`，不得用今天重新读取旧 CSV 倒签昨日观测。

`calendar` 完全沿用账户 calendar 对象及 `validate_calendar`：自然日无缺口、显式休市、同日带时区的 execution_at/valuation_at；必须含当前和前一个开市会话。日历追加仍用已有 `extend-calendar`，适配器不改账户日历。仅索引观测日期或 CN 工作日不够。

`prices` 为 UTF-8 CSV。`columns` 显式映射以下七字段，不选择默认列：

| 标准字段 | 要求 |
|---|---|
| `trade_date / symbol` | 对配置日期/唯一标的精确匹配，必须恰好一行；无记录或重复均阻塞 |
| `execution_price / valuation_price` | 有限正数，不允许 NaN、缺失或填 0；期货 valuation 必须有 settlement 证据 |
| `execution_at / valuation_at` | 来源核验后的真实定价会话时刻，带时区且等于日历会话；不能仅凭日线 open/close 推造 |
| `available_at` | 来源价格发布时间/可用时刻，须不早于估值且不晚于外部核验时刻；该字段不代替本机取得时刻 |

外部 `evidence` 需：

- 同一原生键/映射/标的/类型/日期/synthetic；`input_sha256` 精确绑定 signal、calendar、prices 三文件。
- `authority/reference/checked_at`：证据责任方、可追溯记录和带时区核验时间；`execution_basis/valuation_basis` 说明定价规则。
- `calendar_complete/session_complete/execution_verified/corporate_actions_complete/external_flows_complete` 必须由上游明确提供 true；`executable=true` 还须有同一执行时刻可成交证据。**适配器不会生成这些证明，填 true 本身不是独立核验。** 谁有权签署、使用什么记录应由来源/账户负责人落实；摘要绑定防止混批，不提供数字签名或抵御有权改写全部输入者。
- `external_flow=0, corporate_actions=[]` 须由上游明确确认；当前账户契约不支持其他值。
- 期货额外要求 `fixed_contract=true, requires_roll=false, expires_at`，且本次估值严格早于到期时刻，`valuation_basis=settlement`；缺到期证据、到期当天估值触及到期或要求换月全部阻塞。不解析主力排名或自动延续合约。

跨文件保护：所有来源按配置摘要校验；证据再次绑定三个输入摘要；读取完成及写包前重读比对。生产来源另有共享非阻塞锁。文件已稳定但属于不同批次也会因摘要绑定不匹配被拒。每个文件上限 32 MiB；更大源需由来源方提供有证据的小切片，不能截断冒充完整。

时间关系：前次估值 ≤ signal.available_at < 当前执行 < 当前估值 ≤ 价格 available_at ≤ evidence.checked_at ≤ 本次观测时刻。event.available_at 使用适配器实际取得全部输入后的时钟；报告分别保留信号观测、源价格可用、证据核验和实际读取时间。不修改 production finished_at，也不把它当信号 available_at。

## 失败与运行限制

`blocked` 返回码 1；原因包括 `SOURCE_HASH_MISMATCH/SOURCE_CHANGED/EVIDENCE_INPUT_MISMATCH`、`STALE_SIGNAL/SIGNAL_NOT_OBSERVED_BEFORE_EXECUTION`、`INCOMPLETE_EXTERNAL_EVIDENCE/EXECUTION_BLOCKED`、`CONTRACT_ROLL_BLOCKED/CONTRACT_EXPIRED_OR_EXPIRING`、`READ_ONLY_DATABASE_UNAVAILABLE`。其他格式/文件/既有捕获校验失败统一为 `INVALID_OR_MISSING_INPUT`，不打印底层异常以免暴露凭据。目标目录不合法或已存在时无新包，CLI 返回 `INVALID_STAGING_OR_INPUT`。

`inspection_only/candidate_only/signal_observed` 返回码 0 表示检查或捕获已完成，**只有 `status=ready AND event_ready=true` 才存在 event**。不能把命令退出 0 当作可核算。未安装 timer、未连订单/企微，也未在 Debian 部署新代码。

## 安全复现

在仓根运行，目标目录每次换新名字：

```bash
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter inspect --staging /tmp/input-staging/local-check-01'
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter catalog --staging /tmp/input-staging/catalog-check-01'
# 模板应失败且只留 report.json：
bash -c 'ulimit -v 2000000; python3 -m operational_returns.input_adapter prepare examples/operational_returns/input-adapter.template.json --staging /tmp/input-staging/template-check-01'
```

已确认实际配置后，`capture` 必须在执行前观察并持久保存；`market` 的 SOURCE/SYMBOL/DATE 与 `prepare` 的 CONFIG 均由来源负责人显式提供，不能拿示例资金或合约作生产默认值。来源缺失就等待有效输入，不能回填观测时间。

Debian 只读结构复现（脚本经 stdin 执行，不安装、不写远端）：

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 elfbob@100.65.111.79 \
  "bash -c 'ulimit -v 2000000; python3 - /home/elfbob/jobs/style-signals/current'" \
  < tools/observe_operational_source.py
```

行为测试（本机复用已有系统 jsonschema，无安装）：

```bash
bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 -' <<'PY'
import sys
sys.path.append('/usr/lib/python3/dist-packages')
import pytest
raise SystemExit(pytest.main(['tests/test_input_adapter.py', 'tests/test_operational_returns.py', '-q']))
PY
```
