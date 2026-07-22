# B3 Task 10 交接记录 — 2026-07-22

冷启动读这一份就够。计划本体：`docs/superpowers/plans/2026-07-14-b3-continuous-style-state.md`

- 分支：`feature/b3-continuous-style-state`
- 工作树：`.worktrees/b3-continuous-style-state`（**开发在这里，不在主仓库**）
- 进度：Task 1–9 已提交；**Task 10 的 Step 1–5 完成，Step 6 起未做**

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
