# B3 Task 10 交接记录 — 2026-07-22

冷启动读这一份就够。计划本体：`docs/superpowers/plans/2026-07-14-b3-continuous-style-state.md`

- 分支：`feature/b3-continuous-style-state`
- 工作树：`.worktrees/b3-continuous-style-state`（**开发在这里，不在主仓库**）
- 进度：**Task 10 全部完成（2026-07-24）**——Step 1–13 走完，代码提交至 `cddfc3d`（内存修复单列 `9e71d01`）；集成检查现实 = 双命令退 2 + 阻断证据（新首要 blocker 是 DATA_CONTRACT，见文末 07-24 收尾节）；分支待用户复核与合并决策

---

## 2026-07-23 批 1：Step 6 阻断路径 + CLI（未提交，待复核）

`backtest/b3_eval.py` 文件尾新增：
- `run_evaluation(cfg, requested_data_end, research_dir, backtest_dir, *, underlying_return_loader, carry_loader, equal_weight_signal, equal_weight_path)`：起手
  `_invalidate_evaluation_outputs` 只清 eval 自有 5 产物（**不碰 structure 的 2 个**）→
  `verify_preflight_manifest(root, config_hash(cfg), requested)` → `_preflight_run_blockers`
  → 有阻断则 `blocked_verdict_rows` + `build_run_manifest` + 原子写两产物，返回
  `EvaluationRunResult(blocked=True)`，全程不读 states/structure/行情。
- `_preflight_run_blockers`：preflight blockers 按 reason_code 去重成 run-blocker 模式
  （同 reason 的 DATA_BLOCKED 压 COVERAGE_BLOCKED，affects_statistical=False），末尾并上
  `database_source_evidence_blocker`。
- `main` CLI（`--data-end/--research-output-dir/--backtest-output-dir`）：退出码
  DATA_BLOCKED→2 / COVERAGE_BLOCKED→3 / 完成→0 / `DataBlocked` 异常→2。
- 非阻断分支批 1 时是 `raise NotImplementedError`，**批 2 已实现**（见下）。当前数据（缺 db 证据
  \+ q1000 成分史）只会走阻断路径，与 Step 11 主判据一致。

`tests/test_b3_eval.py`：`_write_preflight_manifest` 加 `config_hash_value` 参数（默认旧常量
`"a"*64`，兼容既有）；新增 6 测试（阻断两产物、reason 去重、缺 db 证据仍 DATA_BLOCKED、
作废陈旧产物、CLI 退 2/退 3）。

验证：6 新测试绿；preflight/manifest/verdict/provenance/blocked/database 子集 **90 passed（187s）**。
**未跑全量 `test_b3_eval.py`**（属 Step 7 / 批 2，需非阻断路径齐了再跑）。

---

## 2026-07-23 批 2：Step 6 非阻断路径（未提交，待复核）

`backtest/b3_eval.py` 的 `run_evaluation` 非阻断分支已实现：
`require_parent_manifest`(exposures/states) + `verify_structure_provenance` 验 config hash/cutoff
一致 → `_coerce_model_comparison`（见下）→ 从 `monthly_exposures.csv.gz` 派生 formation_dates、
读 `state_components.csv` → 用注入的 loader（默认 `load_underlying_returns`/`load_carry`，缺则
惰性导入）取 blend/500/1000 收益 + 500/1000 carry + equal_weight → `build_evaluation` →
`compute_raw_carry_freshness`/`compute_true_disclosure_coverage`/`salg_valid_through` →
`freshness_blockers` 得数据依赖阻断 → `assemble_verdicts(run_blockers=...)` → 填 `RunEvidence` →
`build_run_manifest(evidence=...)` → 写 5 eval 产物（+structure 已有 2 = 七）。
退出：final ∈ {DATA_BLOCKED,COVERAGE_BLOCKED} → blocked=True，否则 STOP/MEASURE_ONLY/PASS_SHADOW。

**踩到的 crux（计划未预见）**：`verify_structure_provenance` 用 `pd.read_csv` 读 model_comparison，
空串→NaN、bool→字符串，而 `_validated_model_comparison_output`/`_validate_model_domain` 严格拒绝。
新增 `_coerce_model_comparison` 还原 7 个 string 列 ""、bool 列真 bool、gate_pass 三态（gate 行 bool /
metric 行 NaN），再喂 `build_evaluation`。这是 eval 首次从 CSV 读 model_comparison，无既有代码可依。

**`stock_price_max_date` 记为 null**（provenance-only，不产 blocker、不影响判定）：eval 只加载 index/carry
序列，不读个股价；其真源应是 preflight 的 `database_source_evidence` 个股表 max_date，属那条已 deferred 的
生产端缺口。`common_historical_end` 因此在完整运行下也是 null（可接受，待生产端补齐）。

两个新集成测试（用 test_b3_eval 自有 `_evaluation_inputs()`/`_model_comparison()` 落盘 + 注入行情）：
退 0 写七产物；post-data coverage=0 → 七产物 + DATA_BLOCKED 但保留 family_statistical_verdict。
`_write_preflight_manifest` 的 `mkdir` 改 `exist_ok=True`（与 `_write_stage_manifest` 同目录共存）。

验证：**全量 `test_b3_eval.py` 298 passed（304s），零回归**。

**批 3 从这里接**：Step 7 跑齐 4 个 B3 文件（test_b3_eval 已过，剩 exposures/portfolios_states/
structure）→ Step 8 抓生产输出 sha256 → Step 9 全量 `tests/` → Step 10 证明 B1/B2/equal_weight
committed 输出零漂移 → Step 11 当前数据 fail-closed 集成（两命令退 2、只写阻断证据）→ Step 12 schema
检查（不 git add 生成物）→ Step 13 提交 4 文件（`b3_eval.py`/`test_b3_eval.py`/`b3_structure.py`/
`test_b3_structure.py`）。

---

## 2026-07-23 批 3 前置：Step 6 评审（code-reviewer）+ 修复（未提交，随批 3 一起走）

评审结论 **No（修完 C1–C3 再合）**，3 Critical / 4 Important / 5 Minor。全部逐条对源码核实后处理：

**C1 — CLI `--data-end` 默认 `2026-12-31` × preflight 严格相等 = 计划 Step 11 裸跑必挂**（退 2 零产物
还先删旧产物，违反「exit 2/3 必须带 explicit audited blocker」）。修：默认 `None`；None 时以 hash
已验证的 preflight 自记 `data_end` 为 resolved 值贯穿下游四处（structure 校验 / freshness / 两处
manifest）；显式传值仍严格相等。显式不匹配走「pre-audit rejection」异常路径（见 I3）。

**C2 — universe_role 词表断裂**：producer 写 `"model"/"size_only"`（`b3_build.py:2149-2152`），eval 两
校验器只收 `{"model","size"}`。数据修好后第一次合法运行必炸。修：校验器词表改 `{"model","size_only"}`
（改 producer 会漂移已提交产物哈希，不可取）+ 3 处旧夹具翻转 + 新增 **producer→eval 契约测试**
（真 `compute_month_exposures` → `flatten_exposures` → CSV 往返 → 两校验器；顺带修掉了夹具穷版
掩盖面：canonical ticker、Task 3 provenance 列、整月网格）。

**C3 — loader 序列未按 cutoff 截断**：`load_underlying_returns` 无 end 参数返回到 DB 最大日，撞
`state component daily grids must match cash returns` 精确相等（`b3_eval.py:2615`）与
`expected_cash > requested` 掀桌（`:1037`）。修：三条 target + 注入的 equal_weight 照 `run_structure`
前例过 `_validated_runner_series(..., cutoff)`；**carry freshness 用 raw 全序列（计划 Step 5），但喂
`build_evaluation` 的 carry 另做 cutoff 截断副本**——评审建议里「materialize_carry 会自行裁剪」不成立，
它是拒绝不是裁剪（`:2275`），实测坐实后改为双轨。

**I2 — 「family=null ⇒ evidence 全空」上锁**：`build_run_manifest` 现在对该组合抛 RuntimeError（实现
错误级）。两个借「blocked+full evidence」隔离测数学的旧测试改成 post-data 阻断真实形状（final 行
statistical_verdict=STOP），语义反而更贴生产。

**I3 — exit 2 两义区分**：`DataBlocked` 异常路径 stderr 现标注 `(pre-audit rejection, no audit
evidence written)`，与「已封存审计证据的阻断」可辨。**I4 — `input_file_hashes` 与计划 Step 5 清单的
口径偏差（记录，待用户确认）**：计划列了 portfolios 三件套 + frozen config；实现只记 eval 实际消费的
4 文件（exposures/states/structure×2）。理由：该字段语义是「every **consumed** materialized input」，
eval 不读 portfolios 产物，记未消费文件反成错误声明；config 身份已由 `config_hash` 字段覆盖。如需
按字面补齐请说话，改动很小。

**Minor 已修**：`_invalidate_evaluation_outputs` 顺带清 `.{name}.tmp` 遗留；`equal_weight_path` 改
None 哨兵（call-time 解析默认路径，CLI 非阻断路径 main() 全覆盖，退出码矩阵四格齐）。**Minor 记录不修**：
argparse 用法错误按惯例退 2 与 DATA_BLOCKED 撞码（评审认可可接受）；`_coerce_model_comparison` 的
dtype=str 根治方案（现实现够用）。

新增 10 测试（C1×2、C2×3、C3×1、I2×1、I3×1、Minor×2），全量 `test_b3_eval.py` **308 passed
（8:58）零回归**（298 既有 + 10 新增）。

---

## 本次会话做了什么

### 1. 修掉唯一的红灯（Task 10 遗留）

`backtest/b3_eval.py:500` 有一行在验证 manifest **之前**就去 hash 未经验证的产物，返回值还直接丢弃：

```python
_sha256_file(root / "coverage_audit.csv")
```

它违反 `test_missing_preflight_manifest_never_hashes_untrusted_outputs`。删掉即绿。可能是残留调试行，也可能是上一轮故意注入用来验证该测试会咬人的变异——两种情况处理相同。

### 2. 补上 structure 阶段清单写入者（计划外，用户批准，提交 `461fbac`）

**发现的问题**：`verify_structure_provenance`（`b3_eval.py:627`）强制要求
`backtest/output/b3/structure_manifest.json`，但**全仓库没有任何代码写它**。
`structure_manifest` 这个词在计划和 spec 里一次都没出现，测试全绿是因为
`test_b3_eval.py:3854` 的 `_write_structure_manifest` 自己造了个假的喂进去。

信任链 `preflight → exposures → portfolios → states → structure → eval` 前四跳由
`b3_build.py` 的 `_write_stage_manifest` / `require_parent_manifest` 管，**第五跳断了**。

**修法**：`run_structure` 收尾时照抄 `_write_stage_manifest` 的原子写法产出清单
（stage / config_hash / data_end / status / 两个产出的 SHA-256），并在运行开始时
把陈旧清单一并作废，避免失败重跑留下指向已删文件的封条。

**为什么当时没炸**：当前数据快照下 preflight 就是 `DATA_BLOCKED`，CLI 按设计在读
structure 之前退出 2，碰不到这段。数据一修好，第一次完整运行必炸。

### 3. Task 10 Step 5 完成

| 函数 | 位置 | 作用 |
|---|---|---|
| `blocked_verdict_rows` | `b3_eval.py` | 阻断运行唯一合法的判定行 |
| `git_commit` | 同上 | 解析并校验完整 40 位 commit |
| `build_run_manifest` | 同上 | 19 字段封条组装 |
| `write_run_manifest` | 同上 | 原子写出 + 冻结 schema 校验 |

13 个新测试，全部走完 RED→GREEN。钉死的约束：

- manifest 字段集必须**恰好**等于 `RUN_MANIFEST_FIELDS`，多一个少一个都 `RuntimeError`
- 阻断运行下 8 个证据字段全为 null，`family_statistical_verdict` 为 null
- `common_historical_end` 五个源日期缺任一即为 null（不允许拿部分数据算出看似完整的边界）
- JSON 往返必须相等（杜绝 numpy 标量混进封条）

---

## 本次定下的两个口径（计划里只有字段名，无定义）

### `common_historical_end`

= 五个源最大日期的 **min**：`stock_price_max_date`、`index_500_max_date`、
`index_1000_max_date`、`ic_carry_max_date`、`im_carry_max_date`。任一为 null 则整体 null。

语义 = 「所有必需历史**同时**可得的最后一天」。它和 `requested_data_end` 成对，专防
「你以为跑到了 7 月、其实 carry 4 月就断了」——本机 `futures_daily` 正是冻在 04-29。

**取数位置**：这五个日期取自**实际加载的序列**，不是 preflight 的按表库证据。
原因：证据是按表记的，而 500 和 1000 同住 `index_daily` 一张表、靠 index code 区分，
按表 max_date 分不出这两个。取实际序列反而更准——反映真正进入计算的数据边界。

### `invalid_formation_months`

= 从 preflight blockers 的 `formation_date` 去重取月（`YYYY-MM`）。

**成功运行下恒为空，这是构造使然，不是漏记**。两处代码堵死了「部分有效」：

- `_validate_preflight_blockers`（`b3_eval.py:360-365`）强制每条 blocker
  `required_formation=True` 且 `affects_final=True`——不存在「记录在案但可容忍」的 blocker
- `_validate_formations`（`b3_eval.py:2206-2211`）要求 2014-01…2023-12 逐月齐全，
  缺一个月直接掀桌

所以这个字段的真实定位是**阻断运行的诊断信息**，不是成功运行的覆盖度注脚。

---

## ⚠️ 未解决的生产端缺口（继续之前先决定）

**`signals/style_basket/b3_build.py` 从不写 `database_source_evidence`。**

`verify_preflight_manifest` 把它当可选字段；缺席时由
`database_source_evidence_blocker`（`b3_eval.py:588`）产出一条
`DATABASE_SOURCE_EVIDENCE_MISSING` 的 DATA_BLOCKED。

和 structure 清单不同，**它 fail-soft**——产 blocker 而非抛异常，符合「无法证明读了
什么数据就不给结论」的设计。所以 Task 10 能照常做完。

**但代价**：即使 000852.SH 成分股历史修好，运行**依然是 DATA_BLOCKED**，直到 preflight
补上这段证据。想真正跑出一次非阻断结论，必须先补生产端（要动 Task 1–6 范围的
`b3_build.py`）。本次按用户决定记为后续。

---

## 从这里继续：Step 6

`python3 -m backtest.b3_eval` 需要做到（计划 line 3299-3317）：

1. 先读并验证 `preflight.json`
2. 若 preflight 阻断 → 只产出 `verdicts.csv` + `run_manifest.json`，退出 2 或 3，
   **不得**请求 states / structure / 收益序列
3. 否则校验 `states.json` 与 structure 产出共享同一 config hash / data cutoff，
   再建 scores、候选收益、bootstrap、metrics 与全部判定行
4. 2024–2026 行保持 `affects_verdict=false`
5. 退出码：0 = STOP / MEASURE_ONLY / PASS_SHADOW（都是完成的研究结论）；
   2/3 = 阻断；1 = 实现错误

**编排照抄 `run_structure`（`b3_structure.py:2599`）的形状**：
`require_parent_manifest` 校验上游 → `_read_cache` 读缓存 → 算 → 原子写。

数据加载器已齐备，无需再补生产端：
`backtest/data.py` 的 `load_underlying_returns(kou_jing)` 与 `load_carry(kou_jing)`。

产出目录 `backtest/output/b3/`，完整运行共七个产物：
`verdicts.csv`、`run_manifest.json`、`structure_coefficients.csv`、`model_comparison.csv`、
`production_metrics.csv`、`yearly_contribution.csv`、`bootstrap.csv`。
阻断运行只有前两个。

之后是 Step 7–13（跑测试、抓生产输出哈希、全量回归、证明 B1/B2/equal_weight 输出无漂移、
当前数据 fail-closed 集成检查、schema 检查、提交）。

**注意 Step 13 的提交范围要扩到 4 个文件**（原计划只写了 2 个）：
`backtest/b3_eval.py`、`tests/test_b3_eval.py`，加上本次已单独提交的
`backtest/b3_structure.py`、`tests/test_b3_structure.py`。

---

## 测试与运行

```bash
cd ~/claude-code/style_timing_signal/.worktrees/b3-continuous-style-state

# B3 四个测试文件（test_b3_eval.py 单跑约 7 分钟，structure 约 6 分钟）
python3 -m pytest tests/test_b3_exposures.py tests/test_b3_portfolios_states.py \
                  tests/test_b3_structure.py tests/test_b3_eval.py -q
```

**探查仓库务必用有界命令**——本仓库单文件 4000+ 行、150KB，计划文档 3400 行。
2026-07-22 就是因为无界 shell 输出撑爆内存，用户丢了整段会话上下文。
用 `git log --oneline -N`、`git diff --stat`、`grep ... | head -N`、`wc -l` 加 Read 分片；
不要 `cat` 大文件，不要 `git log -p`。

---

## 2026-07-24 进度找回：Step 7–10 完成证据 + Step 11 OOM 阻断

07-23 下午两个会话相继被全局 OOM 杀死（18:18 / 19:08，kernel journal 可查），元凶不是
shell 输出，是 **Step 11 的 `python3 -m signals.style_basket.b3_build --stage preflight
--data-end 2023-12-31` 自身膨胀到 ~13GB RSS**（机器共 15GB），跑约 13 分钟后拖死整机。
两次输出都已重定向到文件，照样崩——与 07-22 的无界输出事故是两种不同机制。

**崩溃前已完成并于 07-24 复核（证据在第二个会话的 /tmp scratchpad，关键数字抄录于此，
不依赖 /tmp 存活）**：

- Step 7：`test_b3_exposures.py + test_b3_portfolios_states.py + test_b3_structure.py`
  → **273 passed（4:37）**
- Step 8/10：`git ls-files -z output/style_basket output/equal_weight output/recommended |
  xargs -0 sha256sum` 前后两份哈希 **diff 零差异**（07-24 复核 `DIFF_EXIT=0`）——
  B1/B2/equal_weight 生产输出零漂移
- Step 9：全量 `pytest tests/ -q` → **799 passed（14:26）零失败**
- 未提交改动完好：`b3_eval.py +422 / test_b3_eval.py +650 / 本文档 +105`（diff --stat 口径）

**Step 11 阻断根因**（读码定位，未修）：`b3_build.py:743` `_fetch_stock_return_status`
的 SQL 对 `stock_daily_price_qfq` **全市场无 ticker 过滤**，拉 2013-05-01 至 data_end 的
全部日线（~1300 万行 × 5 列），psycopg2 先把所有行物化成 Python 对象（Decimal/str/date）
再转 pandas，峰值 ~13GB。preflight「return-blind、便宜地 fail-closed」的设计意图与
「先吃下全市场行情」的实现相悖。step11a.log 只有 6 条 `_read_sql`（`:727`）的 pandas
UserWarning、无任何阶段输出，死于数据加载/校验途中。其余 loader（`:1034` closes、
`:1063` shares 等）是否同样无界，修复时一并核查。

**恢复 Step 11 前需用户拍板**：不修内存，Step 11 在本机（15GB）物理上跑不完；修内存要动
Task 1–6 范围的已提交文件 `b3_build.py`（查询聚合/分块/限定 universe 或 server-side
cursor + dtype 收紧）。

**从今往后，真实库上跑 b3_build / b3_eval 任何 stage 一律套内存护栏，禁止裸跑**：

```bash
systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -m signals.style_basket.b3_build --stage preflight --data-end 2023-12-31
```

超限只杀该 scope，不再拖死整机。

---

## 2026-07-24 收尾：内存修复落地 + Step 11–13 完成

**内存修复（`9e71d01`）**：`_fetch_raw_financial` 改为按 100-ticker 排序整批加载，
逐批验证+翻译、只驻留白名单翻译结果。语义键以 ts_code 开头 ⇒ 整 ticker 分批下跨批
去重/冲突不可能发生，`_validate_raw_financial_facts` 逐批原样复用。TDD 3 新测试
（分批行为 + 与单查询等价 + 空批跳过）。实测：500 票真实库冒烟峰值 **847MB**、
完整 preflight 稳态 **~530MB**（旧实现 13GB OOM）；全量套件 **802 passed（10:24）
零回归**（799 基线 + 3 新）。

**新数据事实（preflight 首次在真实库跑通后暴露）**：`stock_selector.stock_financial`
有 **`ann_date < end_date` 脏行 144 行 / 101 票**（income 92 / balance 48 /
cashflow_indirect 4，全 CSMAR；如 000049.SZ FY2016 ann=2016-08-16）。
`_validate_raw_financial_facts` 按 fail-closed 设计抛 DataBlocked，被 run_preflight
的 `snapshot_source` handler 捕获记账。**07-14 计划预期的首要 blocker
（TARGET_COORDINATE_CALIBRATION / 000852.SH 零成分）已不是现实**——
index_constituent 两指数 2021+ 成分已存在（07-23 OOM 能跑进 facts 加载即为证明），
现在的首要 blocker 是 `DATA_CONTRACT`。

**Step 11**（8G systemd-run 护栏下）：
- `b3_build --stage preflight --data-end 2023-12-31` → **exit 2**；写
  coverage_audit.csv（两 policy 各一条 `snapshot_source`/`DATA_BLOCKED`/`DATA_CONTRACT`）
  + exposure_diagnostics.csv + manifests/preflight.json（status=DATA_BLOCKED，
  config_hash=`14f68b34…`）
- `python3 -m backtest.b3_eval` 裸跑（C1：data_end 由 hash 校验过的 preflight 解析为
  2023-12-31）→ **exit 2**；backtest/output/b3/ 恰好两件 verdicts.csv +
  run_manifest.json；final_verdict=DATA_BLOCKED、family_statistical_verdict=null、
  8 个证据字段全 null、code_commit=913614e；run_blocker =
  `DATABASE_SOURCE_EVIDENCE_MISSING` + `DATA_CONTRACT`（reason 去重生效）
- 生产输出（style_basket/equal_weight/recommended）零改动

**Step 12**：计划原文脚本 exit 0（present = 恰好 blocked 两件套）。
backtest/output/b3/ 按计划**保持 untracked、不 git add**（待用户复核）。

**Step 13 提交**：`9e71d01` fix(b3) 内存修复（b3_build.py + test_b3_exposures.py）、
`cddfc3d` feat(b3) fail-closed verdict pipeline（b3_eval.py + test_b3_eval.py）；
b3_structure.py / test_b3_structure.py 已在更早提交（`2b71440`/`bfe4ace`/`461fbac`）。

**留给用户决定**：
1. ann_date 脏行处置：修库里 144 行数据，或调 validator 语义（当前 fail-closed 阻断
   即设计行为）。处置后 preflight 才能走到下一层 blocker（q1000 覆盖/坐标校准）。
2. `database_source_evidence` 生产端缺口依旧挂账（b3_build 不写该字段 ⇒ 数据全修好
   也 DATA_BLOCKED）。
3. 分支复核与合并方式。
