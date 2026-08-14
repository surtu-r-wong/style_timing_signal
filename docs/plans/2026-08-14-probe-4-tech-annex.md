# 候选 ④「条件化有效性」探针 —— 技术前提附录（只读侦察）

侦察日期 2026-08-14 · 仓库 `/home/elfbob/claude-code/style_timing_signal` · 分支 main
纪律声明：侦察过程**零写入仓库、零重计算、零 git 操作**；所有 shell 输出有界。
本文件为侦察产出的存档副本，随预登记冻结文档
`2026-08-14-probe-4-conditional-prereg.md` 一并提交作证据链。

议程原文入口：`docs/plans/2026-08-12-signal-research-agenda.md` §3 候选④（line 279-331）、
§4 方法论硬约束（line 466-598）。

---

## 1. 四个状态变量的数据源

### 1.1 三个 committed 缓存 —— 路径 / 行数 / 首末日（议程称述**全部核实为真**，但有两处补充）

| 缓存 | 绝对路径 | 数据行数 | 列名 | 首日 | 末日 | 入库提交 |
|---|---|---|---|---|---|---|
| 涨停温度计 | `/home/elfbob/claude-code/style_timing_signal/backtest/output/thermometer.csv` | **8,672** | `date,n_sealed,n_burst,n_active,lu_premium,lu_ratio,burst_rate` | 1990-12-19 | **2026-07-01** | `7d01a18` |
| 全市场成交额 | `/home/elfbob/claude-code/style_timing_signal/backtest/output/market_turnover.csv` | **8,672** | `date,amt_yuan` | 1990-12-19 | **2026-07-01** | `7a758c7` |
| 广度 | `/home/elfbob/claude-code/style_timing_signal/backtest/output/breadth.csv` | **3,419** | `trade_date,pct_above_ma20,pct_above_ma60,hi_lo_diff20,hi_lo_diff60` | 2012-06-01 | **2026-06-30** | `9e356fd` |

证据（实测，`wc -l` 减表头 + `head -1` / `tail -1`）：8673/8673/3420 物理行。
议程 §3 ④(c)（line 308-313）称的 8,672 / 8,672 / 3,419 与 07-01/07-01/06-30 **逐个吻合**。

**补充发现 A（议程未提，必须写进预登记）：三个缓存的最后一行是"上游只灌了部分股票"的垃圾日。**
实测 `thermometer.csv` 2026-07-01 的 `n_active=11`（前一日 5,175）、
`market_turnover.csv` 2026-07-01 的 `amt_yuan=1.29e10`（近 250 日中位 2.39e12，仅 0.5%）。
仓库里已有守卫：`dashboard/data.py:54 trim_incomplete_tail(s, floor=0.3)`，docstring 原文即
「如 2026-07-01 仅 11 只入库导致成交额/涨停数全是垃圾」；`dashboard/data.py:127` 对温度计按
`n_active` 调用它，`:136` 对成交额调用它。
→ **三个缓存的有效末日实际上统一是 2026-06-30**；④ 的读取层必须复用 `trim_incomplete_tail`
而不是直接 `read_csv`。

**补充发现 B：闸门窗口的覆盖完全够，延展不是前提。**
样本口径以八风格指数日历 **3,065 天（2014-01-02 ~ 2026-08-11）** 为准
（`backtest/output/probe_5b_dividend_partner_panel.csv:4` 的 `n_obs_full_2014_2026=3065`）。
缓存在 2014 之后的行数实测：thermometer **3,036**、market_turnover **3,036**、breadth **3,035**
（含上述垃圾尾行）。缺口 ≈29~30 个交易日，**全部落在 2026-07-02 ~ 08-11**，即
`holdout_2024_2026` 的尾巴；而 ④ 的全部闸门只读 train/val（见 §4）。
→ **延展缓存对 GO/STOP 判定零影响**，只影响 holdout 报告段的最后 6 周。建议预登记为"不延展"。

### 1.2 已实现波动：从 `index_daily` 哪些码算

- 现役 equal_weight 的**八条腿**（`signals/equal_weight/config_4pairs.csv` 4 行 ×
  `signals/common/index_codes.csv:9-16` 映射）：
  `000918.CSI 沪深300成长 / 000919.CSI 沪深300价值 / H30351.CSI 中证500成长 /
  H30352.CSI 中证500价值 / 932407.CSI 中证1000成长 / 932406.CSI 中证1000价值 /
  932409.CSI 中证2000成长 / 932408.CSI 中证2000价值`，各 **3,065 行（2014-01-02 起）**
  （议程 §5 line 610 的只读实测）。读取入口 `signals/common/data_source.py:101 load_pg_closes()`
  （表 `stock_selector.index_daily`，见该文件 line 1 与 131）。
- **标的（收益端）**：`backtest/data.py:23 _SPOT = {"500": "000905.SH", "1000": "000852.SH"}`，
  `load_underlying_returns("blend")` = 两者 pct_change 后 50/50（`data.py:94-101`），
  各 3,265 行（2013-03-06 起，议程 §5 line 613）。
- → **"已实现波动"有两个自然口径，议程没钉死（见 §7 待拍板 D1）**：
  (i) **标的口径** `load_underlying_returns("blend").rolling(N).std()`——零额外 IO，
      与 engine/IC 的收益端**同一根序列**，是我推荐的默认；
  (ii) 八腿口径（用 `load_pg_closes` 的八列各自算再平均）——语义上更贴"信号自身的环境"，
      但要多一次 PG 读且与收益端不同源。
  仓库里**没有现成的 realized-vol 函数**（实测 `grep 'realized|\.std()'`：只有
  `momentum_scan.py:46 vol = ret.rolling(length).std()` 与 `leverage_probe.py:63` 的 z 分母，
  均非独立的波动状态变量）→ ④ 需自写一个 3 行的滚动标准差，属探针内自有代码。

### 1.3 carry 序列来源与末日

- 入口 `backtest/data.py:103 load_carry(kou_jing, start=None, db=None)`，
  底表 **`public.futures_daily`**（`data.py:111-115` 的 SQL），
  blend = `blend_carry(IC, IM)` **固定 50/50、缺腿 fillna(0)**（`data.py:32-40`）。
- 末日 **2026-04-29**：议程 §5 line 616 实测
  「`[public.futures_daily] max(trade_date) = 2026-04-29`，2,069,715 行；IC/IF/IH/IM 四族 max 同」。
  议程 §4 C6（line 544-548）把它立为口径：「序列止于 2026-04-29 …… 2026-05 之后的评估段没有
  carry 输入（对多头略偏悲观、对空头略偏乐观）」。
- **起点才是 ④ 的真问题**（议程未提）：C6 原文「blend carry = IC（2015-04 起）+ IM（2022-07-22 起）」
  → **train 窗前 ~15 个月（2014-01 ~ 2015-03）根本没有 carry 值**，且 2015-04~2022-07 的
  blend carry 是"IC 单腿 ÷ 2"。把 carry 当**状态变量**分桶时，这段必须显式处置
  （见 §7 待拍板 D4），否则会造出一个假的"carry≈0 低分位状态"。

### 1.4 延展缓存那条"现成 CLI" —— 找到了，但议程的说法只对 2/3

（**以下命令仅供登记，本次未运行**）

| 缓存 | 重建入口 | 现成 CLI？ |
|---|---|---|
| thermometer | `backtest/thermo_probe.py:147 build_thermometer(db=None, force=False)`，写 `CACHE_PATH`（`:36`, `:211-212`） | **有**：`python3 -m backtest.thermo_probe --rebuild-thermometer`（`thermo_probe.py:240-245`） |
| market_turnover | `backtest/leverage_probe.py:156 build_market_turnover(db=None, force=False)`，写 `CACHE_PATH`（`:50`, `:182-183`） | **有**：`python3 -m backtest.leverage_probe --rebuild-turnover`（`leverage_probe.py:264-269`） |
| breadth | `backtest/breadth.py:97 build_breadth(db=None, start="2012-06-01", cache_path=CACHE_PATH, ...)` | **没有**。`backtest/breadth.py` 实测**零 argparse / 零 `__main__`**；`breadth_dual.py:145 main()` 只有 `--mode scan/report`，**没有 rebuild 开关**，它走 `:66 load_breadth_cache()` 直接读 CSV。只能 `python3 -c "from backtest.breadth import build_breadth; build_breadth()"` |

**⚠️ 两条现成 CLI 都有副作用**：`--rebuild-*` 只是主流程的一个前置分支，其后**无条件继续跑完整探针**
（thermo 四族 × n_perm=1000、leverage 三族 × 1000），并**覆盖写** `thermo_probe{,_verdicts}.csv` /
`leverage_probe{,_verdicts}.csv` —— 那是两条已 STOP 归档轴的产物。
→ 若确定要延展，建议改用不带副作用的函数式调用
`python3 -c "from backtest.thermo_probe import build_thermometer; build_thermometer(force=True)"`
（同理 `build_market_turnover(force=True)`），并把这条纪律写进预登记。
另注：三个 build 都是**全量重建**（从 PG 拉全历史重算），不是增量 append。

---

## 2. ⓪ 置换机器 API 与 ④ 的接入

### 2.1 公开函数签名（`backtest/selection_permutation.py`，551 行）

```
selection_permutation_test(variants, *, n_obs,
    stat_fn=None, batch_stat_fn=None, n_perm=1000, seed=0, scheme="rotation",
    min_shift=1, max_shift=None, block=20, select_fn=None, index_matrix=None,
    statistic_name="statistic", meta=None) -> SelectionPermutationResult      # :295-302
```
配套件：`identity_index(:97)` / `rotation_index_matrix(n_obs, n_perm, rng, min_shift=1, max_shift=None)(:102)` /
`moving_block_index_matrix(:124)` / `build_index_matrix(:144)` / `statistic_matrix(:157)` /
`argmax_select(:182)` / `apply_selection(:195)` / `column_pvalues(:207)` /
`make_stat_fn(signals, score)(:363)` / `make_batch_stat_fn(signals, batch_score)(:375)`；
结果容器 `SelectionPermutationResult(:229)`，字段含 `p_selected / p_min_p / p_naive /
null_selected / null_winner_counts`，另有 `summary()(:265)`、`to_frame()(:283)`。

**输入约定（模块 docstring `:31-47`，逐字）**：
- `stat_fn(variant, idx) -> float`，`idx` 是长度 n_obs 的重抽样索引；观测行 `idx = np.arange(n_obs)`；
  **统计量必须越大越好**（双侧请自行返回 `|IC|`）；
- 惯例是**重排信号侧、收益/控制变量按日历不动**（与 `rotation_probe` 一致）；
- `select_fn(stats) -> int` 默认 `argmax_select`；观测行与每个置换行走**同一条规则**；
- 三个 p 的纪律（`:232-237`）：**只有 `p_selected` 可进闸门**；`p_naive` 只准出现在
  "选择效应有多大"的证据段。

### 2.2 ② 接入时 QA 钉死的三规矩 —— 代码/文档落点

三规矩的**规范原文**在 `backtest/divergence_probe.py:15-23`（模块 docstring 小节
"## 机器接入三规矩（`2026-08-12-selection-permutation-machine.md` §6，违反即作废）"）：

| 规矩 | 原文落点 | 实现落点 | 判例落点 |
|---|---|---|---|
| 1. 48 点（3 族×16 点）**合成同一次**调用，不做 3 次族级独立调用；备选是族级 Bonferroni α=0.05/3=**0.0167** | `divergence_probe.py:17-19`；备选口径在 `docs/plans/2026-08-12-selection-permutation-machine.md:247` | `divergence_probe.py:85 N_VARIANTS = len(FAMILIES)*len(GRID_LB)*len(GRID_ZW)*len(GRID_K) # = 48`；`:132` 变体枚举；`:317` "关1/关1b 各跑一次同款机器，两次都是 48 点合成单次调用"，`:326` 与 `:332` 两次 `selection_permutation_test` | `tests/test_bt_divergence_probe.py:9-10` |
| 2. 关1 统计量**不得携带**关2 的同号约束；`-inf` **只给"该行无法评分"**（统计量退化为 NaN） | `divergence_probe.py:20-22` | `score` 为无约束 `\|非重叠 IC\|`；机器侧边界写在 `selection_permutation.py:36-43`（含"把闸门塞进统计量会双记且偏移 p 的含义"与 `leverage_probe.pick_representative` 为何形似而实质不同） | `tests/test_bt_divergence_probe.py:9` |
| 3. `min_shift = 2·max(k)`、`max_shift = n_obs − 2·max(k)`（房规 `[2k, n−2k]`） | `divergence_probe.py:23`（k∈{5,10,20,40} → **80**） | `divergence_probe.py:321-323, 330, 336` | `tests/test_bt_divergence_probe.py:256, 266, 285` |

⑤ 的同款落点（可作第二样板）：`backtest/pair_set_probe.py:38`（Bonferroni α=0.05/3）、
`:41`（`min_shift = 2·max(k) = 40`、`max_shift = n_obs − 40`）、`:47`（收益层族要改用去相关长度定下界，
= ①b `mapping_probe.choose_min_shift` 的做法）。
⑤b 因**单候选**而 `BONFERRONI_M = 1`（`pair_set_probe_5b.py:108`），并**不跑** `selection_permutation`
（`docs/plans/2026-08-13-probe-5b-dividend-partner-prereg.md:64-65`）。

### 2.3 ④ 的"置换必须重跑整套状态选择"—— API **直接支持**，只需薄包装

**结论：无需改机器。** 机器的语义就是"每个置换样本下重算整套网格的统计量并重新套用同一条选优规则"
（`selection_permutation.py:15-17, 160-163`）。④ 的"整套状态选择" = 预登记的
**≤4 状态变量 × 2 切法 = ≤8 个变体**，把它们作为 `variants` 传进**同一次**调用即可
（对齐 ② 规矩 1：**8 点合成单次调用**，不做 4 次变量级独立调用）。

需要写的**只有一个闭包**（探针内自有代码，不动 `selection_permutation.py`）：

- `stat_fn(variant, idx)`：`variant = (state_var, split)`；闭包捕获**按日历固定**的
  `state_labels[variant]`（状态标签）与 `fwd_ret`（前瞻收益），只把 **equal_weight 信号**按 `idx` 重排，
  再算该变体的"跨状态 IC 差"统计量（例：`max_s IC_s − min_s IC_s`，或三分时的 `IC_high − IC_low`；
  **必须越大越好**，双侧取 `|·|`）。
- 可选 `batch_stat_fn` 做向量化提速（`make_batch_stat_fn(:375)` 是现成组装器）。

**两条必须写进预登记的边界（否则会踩机器已知的雷）**：
1. **状态变量必须是"不依赖收益"的确定性外生序列**（波动/成交额/涨停温度/carry 都满足）。
   一旦有人把状态改成"按收益分箱"，机器 docstring `:26-30` 明写要走更贵的分支
   （"把 stat_fn 写成用重排后的输入重建变体再评分"），成本 × 网格点数。
   → 预登记应明文禁止收益派生的状态变量。
2. **④(f) 的闸门 ①（两窗同号）与闸门 ③（早/晚期余弦为正）绝不可编码进 `-inf`**——
   这正是 ② 规矩 2 拦下的错误；`-inf` 只留给"样本不足/窗口不够/统计量 NaN"。
   两条一致性闸必须作为**后置独立判据**单跑。
3. `min_shift/max_shift`：④ 若沿用 k=20 单一前瞻期，则 `min_shift = 2·20 = 40`、
   `max_shift = n_obs − 40`（房规 `[2k, n−2k]`，`selection_permutation.py:111` 亦如此建议）。

**⑤ 早/晚期余弦一致性闸没有现成机器**：`grep` 全仓无 `cosine` 类通用工具（B3 的余弦在
`b3_structure.py`，而 ④(b) 明文"不碰 `b3_eval`/`b3_structure`"）→ 这是 ④ 要自写的第二块代码。

---

## 3. 同秤基线机器（非重叠头对头 rank IC + 配对 bootstrap）

### 3.1 `backtest/fusion_probe.py`（496 行）—— 非重叠同秤**非偏** rank IC

```
fuse_equal(a, b)                                              # :56  因子层等权，固定 50/50
forward_return(underlying, k) -> Series                       # :68  t 处 = t+1..t+k 累计收益（与引擎 T+1 口径一致）
nonoverlap_grid(index, k, offset=0) -> DatetimeIndex          # :76  offset 起每隔 k 行取样
rank_ic(x, y) -> dict{ic,n_obs,t_stat,p_value}                # :83  Spearman + t 检验
spearman_rows(x_mat, y_mat) -> ndarray                        # :97  逐行 Spearman（向量化内核）
paired_ic_bootstrap(x_a, x_b, y, n=10000, seed=0, alpha=0.05) # :110 配对：同一组行索引同时作用于 a/b/y
    -> dict{diff_ic, ci_lo, ci_hi, p_value, boot_mean, boot_sd, n_boot, n_obs, seed, ci_excludes_zero}
build_ic_report(factors, k=20, offset=0, n_boot=10000, seed=0, # :156 编排：三因子 × 五窗
                kou_jing="blend", db=None) -> (ic_df, diff_df)
```
口径常量：`fusion_probe.py:49 TRAIN, VAL, HOLDOUT, FULL = "2014-2020","2021-2023","2024-2026","full"`；
`:51 GATE_WORST_TV_LIFT = 0.10`（③ 的收益层阈值）。
`paired_ic_bootstrap` 的抽样单位是**非重叠 k 日观测（近独立）**，故用 **i.i.d. bootstrap 而非 block**
（`:114` docstring 原文）。

### 3.2 `backtest/paired_bootstrap.py`（185 行）—— 配对 block bootstrap Sharpe 差

```
sharpe_matrix(mat)                                            # :48
moving_block_indices(length, block, draws, rng)               # :58
paired_block_bootstrap_sharpe_diff(ret_a, ret_b, block=20, n=10000, seed=0, alpha=0.05)  # :68
    -> dict{diff_sharpe, ci_lo, ci_hi, p_value, boot_mean, boot_sd, block, n_boot, seed, n_obs}
load_primary_signal(name="equal_weight") -> Series             # :107（读 baseline.SIGNALS 表）
build_report(block=20, n=10000, seed=0, cost_bps=3.0, db=None, signal_name="equal_weight")  # :113
```
`:73` 明写 "a / b 必须已按同一日期索引对齐（同一组块索引同时作用于两者 = 配对）"。
`moving_block_index_matrix`（机器侧）与 `moving_block_indices`（这里）是**两份副本**，
守卫是交叉判例 `test_moving_block_index_matrix_matches_paired_bootstrap`
（`selection_permutation.py:130-133` 的警告）。

### 3.3 ⑤b 是怎么复用它们的（可直接抄的样板）

`backtest/pair_set_probe_5b.py:69-71`：
```python
from backtest.fusion_probe import (
    forward_return, nonoverlap_grid, paired_ic_bootstrap, rank_ic, spearman_rows,
)
```
`:72-78` 再从 `pair_set_probe` import 全部口径常量与同秤机器
（`ALPHA, DIV_CORR_GATE, GATE_WORST_TV_LIFT, IC_OFFSET, K_FORWARD, LOOKBACK, MAPPINGS,
PRIMARY_KOU_JING, SELECTION, SMOOTHING, STAT_WINDOWS, WINDOWS_REPORT, Z_WINDOW, _slice,
evaluate_windows, load_production_signal, map_position`），并在 `:83` 附近立了
"同秤清单 re-export"以便 QA 独立复算取到**同一支实现**；`:364` 注明
「配对差走 `fusion_probe.paired_ic_bootstrap`：**同一组抽样行同时作用于两侧**」。
→ **④ 应照此模式**：不 import `pair_set_probe`（那是配对集合专用），但**必须**
`from backtest.fusion_probe import forward_return, nonoverlap_grid, rank_ic, paired_ic_bootstrap`，
并从 `backtest.baseline` / `backtest.engine` / `backtest.positions` 取收益层同秤件，**零平行实现**。

> 口径瑕疵留痕（不影响 ④，但抄文案时别抄错）：⑤b 预登记文档
> `docs/plans/2026-08-13-probe-5b-dividend-partner-prereg.md:57-58` 把配对 IC 差写成
> "moving-block bootstrap"，而实际调用的 `fusion_probe.paired_ic_bootstrap:114` 是
> **i.i.d. bootstrap**（因抽样单位已非重叠）。④ 的预登记应按代码写。

---

## 4. 现役基线数与 train/val 窗口定义

### 4.1 权威已提交出处：现役 equal_weight long-flat 的 worst(train,val) Sharpe = **1.001**

**两个 committed 产物，数值到小数点后 6 位一致**：

1. `backtest/output/probe_5b_dividend_partner_panel.csv:4`（Batch 13，最近一次正式跑）
   行 `A_four_pairs_incumbent,longflat,blend,blend,4,True,...`：
   `net_sharpe_train_2014_2020 = 1.705347`、`net_sharpe_val_2021_2023 = **1.000931**`、
   `net_sharpe_selection_2014_2023 = 1.538854`、`net_sharpe_holdout_2024_2026 = 1.858890`、
   `net_sharpe_full_2014_2026 = 1.609892`，末列 **`worst_tv_sharpe = 1.000931`**；
   n_obs：selection **2434** / train **1707** / val **727** / holdout **631** / full **3065**。
2. `backtest/output/baseline_metrics.csv:105`
   `equal_weight,blend,2021-2023,long → sharpe = 1.000930606530241`（ann 11.72%、maxdd −13.12%、
   turnover 8.425、n_obs 727）；同文件 `:102` 给 train 段 `1.7053473108535933`。
   （`baseline.evaluate` 的 `long` 段 = 仓位 `clip(lower=0)`，对 long-flat 恒等 → 与 (1) 同格。）

文档侧的记述（口径一致，均为 blend · long-flat · 3bp+carry · 全日历）：
- `docs/plans/2026-08-12-probe-1a-symmetric-vs-longflat.md:61, 86`（①a，Batch 7 那份）
- `docs/plans/2026-08-12-probe-3-fusion-slope20.md:20`「现任 worst = 1.001（val）」、`:144`
- `docs/plans/2026-08-13-probe-5-pair-set.md:41`「B 1.098 / C 1.091 vs A **1.001**」
- `docs/plans/2026-08-13-exec-price-audit.md:190`（2021-2023 longflat 727 天 **1.001**）
- `docs/superpowers/specs/2026-07-13-b3-continuous-style-state-design.md:461`

→ **④ 的闸门 ④ 阈值 = 1.000931 + 0.10 = 1.100931**（用 6 位而非 1.001，避免四舍五入争议；
⑤b 就是这么写的：`prereg:176` 记 "worst(train,val) **1.1032** vs 门槛 **1.1509**"，
即 1.000931+0.15）。
**同秤纪律（§4 C4, line 526-533）**：只以 `backtest/baseline.py` 的 long-flat 基线为唯一对照，
禁止引用 1.42 / 1.62 / 1.78 / 1.81 那几个历史数字。

### 4.2 train/val 窗口定义（近期批次现成口径）

代码内唯一权威（⑤/⑤b 共用，③ 亦同值）：`backtest/pair_set_probe.py:125-133`
```python
SELECTION = ("2014-01-01", "2023-12-31")
WINDOWS_REPORT = {
    "selection_2014_2023": SELECTION,
    "train_2014_2020":  ("2014-01-01", "2020-12-31"),
    "val_2021_2023":    ("2021-01-01", "2023-12-31"),
    "holdout_2024_2026":("2024-01-01", "2026-12-31"),
    "full_2014_2026":   (None, None),
}
STAT_WINDOWS = ("train_2014_2020", "val_2021_2023")   # 闸门只读这两窗
```
等价定义另见 `backtest/baseline.py:30-35 WINDOWS`、`backtest/fusion_probe.py:49`、
`backtest/selection_permutation.py:389 DEMO_WINDOWS`。
选择端纪律：`divergence_probe.py:25-29`「选择与全部闸门只用 train+val；**2024-2026 只报告不选择**」
（依据 §4 C2，line 508-515，07-11 勘误已把 2024-26 降级为第二验证窗）。
⑤b 预登记的同款表述在 `prereg:60`（"选择端只读 train/val/选择窗"）与 `:66`（"holdout 只报告不进闸"）。
生产参数锁死值：`LOOKBACK, Z_WINDOW, SMOOTHING = 20, 40, 5`（`pair_set_probe.py:117`），
生产因子 CSV `output/equal_weight/equal_weight_signal_20d40z.csv` 列 `factor_value`（`:118-119`）。

---

## 5. 执行环境判断：**本机跑，不必上 WSL2**

**结论：本机（开发机）即可，无需投 Windows/WSL2。** 依据三条：

1. **数据量级是"几千行 × 个位数网格"**：样本 3,065 天（train 1,707 / val 727），
   状态网格 ≤4 变量 × 2 切法 = **≤8 个变体**（对比 ② 的 48 点、①b 的 32 点，两者都在本机跑过）。
   置换成本 = 8 变体 × 1,000 次 × 一次 Spearman(≤几百点) ≈ 10^4 次小规模秩相关，秒级~分钟级。
2. **内存无风险**：全部输入是三个 ≤8,672 行的 CSV + 一张 3,065×8 的价格宽表 + 两条标的收益序列，
   合计 < 50 MB。与 `ops-b3-preflight-oom` 记的股票级面板（数百万行）不是一个量级；
   ④(b) 明文"不用股票级财务、不碰 `b3_eval`/`b3_structure`"，也就不会触发 B3 那条内存路径。
3. **CLAUDE.md 的重量级规则针对"大回测/全量重算/长跑批处理/吃内存的数据加工"**——
   ④ 三样都不沾。可比先例：② 探针（48 变体 × 2 次机器调用 × n_perm=1000）与 ⑤/⑤b
   （n_boot=10000 的配对 bootstrap × 五窗）产物均已在库，走的都是本机路径。

**唯一需要注意的不是算力而是 IO**：因子构造要连 PG（`stock_selector.index_daily`，Debian
`100.65.111.79`），历史上有 Tailscale MTU 黑洞（memory `ops-tailscale-blackhole-diagnosis`）。
建议预登记"先探连接、带 `connect_timeout`/`statement_timeout`；价格宽表一次性拉下来复用"，
与议程 §5 的只读实测姿势一致。

---

## 6. 已知怪癖：`engine.py:24` 是否影响本探针

**结论：影响，必须登记——因为 ④(f) 的闸门 ④ 要"成本后的仓位调节版 worst(train,val) Sharpe"，
这一步绕不开 `engine.run_strategy`。**

- 闸门原文（议程 `:328-329`）：「④ **成本后的**仓位调节版 worst(train,val) Sharpe ≥ 现役 +0.10」。
  "成本后 Sharpe"在本仓只有一条实现路径：`backtest/engine.py:19 run_strategy(position, underlying,
  cost_bps=3.0, carry=None)` + `backtest/metrics.sharpe`（⑤b 的同秤清单 `prereg:25` 逐字列的就是它）。
  → IC/交互层确实不碰 engine，但**闸门 ④ 碰**，所以怪癖照登。
- 怪癖 1（执行价口径，`engine.py:4-8` docstring 自述）：`pos_eff = position.shift(1)`（`:24`）配上
  `close.pct_change()`，**经济上等价于"按 T 收盘价成交"**，而不是设计稿写的 T+1 收盘成交
  （那需要 `shift(2)`）。已由 Batch 12 审计定性为**文档债不是代码错**，
  `docs/plans/2026-08-13-exec-price-audit.md`（同秤下两者无法分辨）。
- 怪癖 2（首日成本落第二天）：`engine.py:27-28` 的 `trade.iloc[0] = abs(pos_eff.iloc[0])`，
  而 `pos_eff.iloc[0]` 经 `.fillna(0.0)` 恒为 0 → 首日建仓成本实际记在第二天。
  留痕：`backtest/exec_price_probe.py:133`「首日建仓成本落第二天的既有怪癖（engine.py:24）
  **原样保留，不"顺手修"**」；`docs/plans/2026-08-12-probe-1a-symmetric-vs-longflat.md:233`；
  `docs/plans/2026-08-13-exec-price-audit.md:416` 第 12 条。
- **对 ④ 的实际杀伤力：可忽略但要写。** 两个怪癖对"现役 long-flat"与"仓位调节版"是
  **同一台引擎、同一批日期**，属**非差分**偏差；每窗只影响 1 天的成本项（3bp × 换手），
  对 727~1,707 天的 Sharpe 影响在小数第三位以下。
  → 建议预登记原文照抄 exec_price_probe 的口径：「原样保留，不顺手修；差分闸门不受影响」。
- 附带 carry 怪癖（同样属既有口径）：`data.py:32-40` 的 blend carry 缺腿按 0，
  且序列止于 2026-04-29 → **holdout 段最后一年没有 carry 输入**，方向为
  "对多头略偏悲观"（§4 C6 勘误，line 550-558）。因 ④ 的闸门只读 train/val，**不进闸**，
  但 holdout 报告段必须带这个标注。

---

## 7. 【预登记待拍板项】—— 议程未钉死、但预登记文档必须钉死的自由度

> 每项给 1~2 个建议默认值。全部属"预登记冻结前必须由用户/控制器拍板"的自由度；
> 不钉死就会在跑完之后变成事后自由度（= ④ 最怕的多重比较）。

**D1｜"已实现波动"的口径三件套（窗长 / 底层序列 / 是否年化）**
议程只说"已实现波动水位"。缺三个数。
→ 建议默认：**`load_underlying_returns("blend")` 的 20 日滚动标准差**（与信号 lookback=20、
IC 前瞻 k=20 同频，且与收益端同源、零额外 IO）；不年化（分位切分对单调变换不变）。
备选：60 日窗（更稳、更贴"波动水位"而非"波动脉冲"）。**只准选一个，不得两个都报。**

**D2｜滚动分位的窗长**
议程写"滚动分位三分：低/中/高；以及中位二分"，但没给分位窗长。
→ 建议默认 **250 日**，理由是仓库既有唯一实现 `dashboard/data.py:32
rolling_percentile(s, window=250)` 就是这个默认，且 style dashboard 三面板
（`docs/plans/2026-07-08-style-dashboard-design.md:38-40`）全部按 250d 分位着色 = 已有口径。
备选 500 日（跨牛熊更稳，但 2014 起前两年全 NaN，train 窗会缩水 ~40%）。
**必须同时钉死"窗口未满的前段怎么处理"** → 建议：丢弃（`rolling_percentile` 本就返回 NaN），
并在预登记里写清由此损失的 train 天数。

**D3｜四个状态变量各用缓存的哪一列**
缓存都是多列的，议程只给了中文轴名。
→ 建议默认：
- 涨停温度 = `thermometer.csv` 的 **`lu_ratio`**（涨停占比；`lu_premium`/`burst_rate` 各有 1 天缺值，
  且 dashboard 把三列都做了 `_pct`，选一列即可，选最不易受口径争议的占比列）；
- 成交额 = `market_turnover.csv` 的 **`amt_yuan`**（唯一列，无自由度；见 D5）；
- 已实现波动 = D1；
- carry 深浅 = `load_carry("blend")` 的**原值**（正=贴水，`data.py:11-12`）。
备选（涨停温度）：`burst_rate`（炸板率，语义更贴"情绪过热"）——**二选一，不得都测**。
**广度 `breadth.csv` 是否入选也要拍板**：议程点名的四轴里没有广度，但 ④(b) 的先验来自
long_axes 的"面2"；若要用，`pct_above_ma20` 是唯一自然默认，且会把状态变量数顶到 5（超 ≤4 上限）。
→ 建议：**不入**，把广度留在未测登记。

**D4｜carry 状态在 2015-04 之前无值的处置**
train 窗前 ~15 个月无 carry；2015-04~2022-07 是 IC 单腿 ÷2。
→ 建议默认：**carry 这一条状态变量的样本自 2015-04-16（首个有 carry 日）起算**，
其 n_obs 与另外三条不同，产物里逐变量单独记 n_obs 与首末日；
**不得**把无 carry 日按 0 并入"低 carry"桶（会造假状态）。
备选：整条 ④ 统一截到 2015-04 起以保四变量同窗——代价是 train 少 ~300 天，不建议。

**D5｜成交额用哪个口径：绝对额 / 换手代理 / 相对自身分位**
`market_turnover.csv` 只有绝对元数，而 A 股成交额有强趋势（1990 年 49 万元 → 2026 年 3.5 万亿）。
绝对额直接三分 = 几乎等价于"按时间三分"，会把"状态交互"读成"早晚期效应"，
**恰好污染 ④(f) 的闸门 ③（早/晚期一致性）**。
→ 建议默认：**用 D2 的 250 日滚动分位**（去趋势后才切桶），并在预登记里把
"绝对额直接切桶 = 时间代理"这个陷阱写明。
备选：`log(amt).diff(20)`（成交额动能）——语义变成"放量/缩量"而非"水位"，与议程措辞不符，不建议。

**D6｜条件 IC 的采样口径与样本量（我判断这是 ④ 最大的技术风险）**
沿用同秤的**非重叠 k=20** 会让 val 窗只剩 **727/20 ≈ 36 个**样本点，三分后
**每桶 ~12 点** —— 12 点的 Spearman 基本是噪声。（⑤b 已被同一问题咬过：
`prereg:155-156` 记"val 窗只有 37 个非重叠样本点"，并写进了"边缘读法"。）
→ 建议默认：**主口径改用全部 20 个 offset 的非重叠网格取平均**
（`fusion_probe.nonoverlap_grid(index, k, offset)` 的 `offset` 参数原生支持 0..k−1），
推断仍走置换（rotation 打断配对，自动吸收重叠带来的相关）；
CI 用 `paired_ic_bootstrap` 时必须相应改成 block 抽样或明示只作诊断。
备选：把 k 降到 **5**（train 341 / val 145 点，三分后每桶 ~48 点），但这会偏离
③/⑤/⑤b 的 `K_FORWARD=20` 同秤口径，需要在预登记里明写"同秤例外"及理由。
**无论选哪个，都必须预登记"每桶最小样本量"硬下限**（建议 ≥30，不足即该变体返回 `-inf`
——这正是 `-inf` 的合法用法："该行无法评分"，不违反 ② 规矩 2）。

**D7｜交互统计量的确切定义（"越大越好"的那个标量）**
机器要求单一标量且越大越好（`selection_permutation.py:36`）。议程只说"交互项显著"。
→ 建议默认：三分切法用 **`IC_high − IC_low`（有符号，先验方向由 (b) 的 B3 碎片给出）**，
二分切法用 **`IC_above − IC_below`**；双侧则统一取 `|·|`——**符号约定必须预登记**，
不能跑完再决定看哪一侧。
备选：跨状态 IC 的极差 `max_s IC_s − min_s IC_s`（天然非负，但丢方向，且与闸门 ①"两窗同号"语义冲突）。
→ 我倾向**有符号差 + 双侧机器统计量取 |·|**，闸门 ① 用有符号值判同号（后置独立判，符合 ② 规矩 2）。

**D8｜早/晚期一致性闸的切点与"余弦"的确切定义**
议程 `:327-328` 只说"发现期前后半段的状态-效力斜率余弦必须为正"。
→ 建议默认：切点 = **train+val 选择窗（2014-01-01~2023-12-31）的中位交易日**（≈2018 年末，
两半各 ~1,217 天，与 ②/thermo 的 `HALVES` 两半窗做法同源）；
"状态-效力斜率向量" = 各状态桶的 IC 组成的向量（三分 → 3 维、二分 → 2 维），
余弦 = 两半窗该向量去均值后的余弦相似度。**必须预登记为闸门（硬条件），不是事后诊断**
（§4 C8 line 594-598 与 ④(f) 原文皆如此要求）。

**D9｜"仓位调节器"的确切映射（闸门 ④ 的被测对象）**
议程 (d) 举例"在状态 s 下把仓位降到 0.5"，但没钉死调节表。这是**最容易变成隐形网格**的一处。
→ 建议默认：**单点、无自由参数**——`pos_adj = pos_longflat × w(s)`，其中
`w(低效力状态) = 0.5`、`w(其余) = 1.0`，"低效力状态"由 IC 层的**同一次**预登记选优结果指定，
不另扫 w。（②/③/⑤ 的"预登记单点、不扫权重"同款纪律。）
备选：`w ∈ {0, 0.5, 1}` 三档——**不建议**，等于新开一层网格且必须并入机器变体数。
**另需钉死**：调节后的仓位是否重新过 `positions.production_position`（long-flat θ=0）、
换手是否设上限。建议：先 long-flat 映射再乘 `w(s)`，换手变化只报告不设闸
（④(f) 原文没有换手闸，别自行加码）。

**D10｜三缓存的尾部处置与是否延展（对应 §1 补充发现 A/B）**
→ 建议默认：**(a) 不延展**（缺口全在 holdout 尾巴，闸门零影响，见 §1.1 补充发现 B）；
**(b) 读取层强制走 `dashboard.data.trim_incomplete_tail`（或探针内等价实现），
把 2026-07-01 的垃圾行剔掉**，并在产物 metadata 里记录每个缓存的实际末日（预期统一 2026-06-30）。
备选：延展——则必须用无副作用的函数式调用（`build_thermometer(force=True)` /
`build_market_turnover(force=True)` / `build_breadth()`），
**禁止**用 `--rebuild-*` CLI（会顺带覆盖两条已归档轴的产物，见 §1.4）。

**D11｜状态标签的 PIT 后移**
温度计/成交额/广度都是**当日收盘后才能算全**的量。既有先例：`leverage_probe.py:81 pit_lag()`
把两融信号后移一格；④ 的状态若用"T 日的波动/成交额"去解释"T→T+20 的 IC"，
严格说 T 日收盘时是可得的（与信号同时点），**不需要**额外 lag；但涨停温度计依赖
`stock_daily_price` 的当日全量入库（实测 2026-07-01 就没灌全）。
→ 建议默认：**状态与信号同时点（T 日收盘），不额外 lag**，与 `forward_return` 的
"t 处 = t+1..t+k"口径自洽（`fusion_probe.py:69`）；
但预登记须写明"实盘部署时状态变量的可得性依赖当日全量入库，若上游未灌全则当日不生成调节"。

---

## 附：本次实测命令清单（全部只读、有界）

```
wc -l / head -1 / tail -1 / git log --oneline -1  →  三个缓存
python3 -c pandas 读三缓存计行数与非空数（≥2014 段：3036 / 3036 / 3035）
grep -n "^def |^class |^[A-Z_]+ = "  →  selection_permutation / fusion_probe / paired_bootstrap
sed -n 区间读  →  engine.py 全 52 行、data.py:20-120、pair_set_probe.py:110-150、
                  divergence_probe.py:1-60、dashboard/data.py:28-45,118-145
grep -rn "1\.001"  →  定位基线数的 committed 出处
head -1 + grep -n 行  →  probe_5b_..._panel.csv:4、baseline_metrics.csv:98-109
```
未执行：任何写入仓库、任何 git 操作、任何探针/引擎运行、任何 PG 查询。
