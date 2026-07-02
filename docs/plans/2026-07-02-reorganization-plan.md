# 风格择时信号项目整理实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 4 个信号版本家族整理为 signals/ + data/ + output/ + archive/ 统一结构，合并等权配对信号两个参数变体，全程数值零变化。

**Architecture:** 先在原位重跑确认基线可复现（git 树 clean 即证明），再 git mv 归位、去重，然后逐条信号线改路径常量并重跑比对，最后合并 ③ 两变体为参数化单脚本并用真实数据字节级比对。

**Tech Stack:** Python 3 + pandas + numpy，pytest，git。仓库根：`/home/elfbob/claude-code/style_timing_signal`（下称 `$ROOT`，所有命令在 $ROOT 执行）。

**设计文档:** `docs/plans/2026-07-02-reorganization-design.md`（文件去向映射以本计划为准，本计划将 `research_202606` 细化为 `old_outputs/<来源>/`）

---

## Task 1: 基线可复现验证（动任何文件之前）

**Files:** 不改任何文件，只运行与记录。

**Step 1.1: 记录当前所有输出的 md5**

```bash
md5sum growth_stability_signal.csv confirmed_signal.csv \
  citic_style_signal_40d/citic_style_signal_40d.csv \
  latest_equal_weight_signal/equal_weight_signal_contrast.csv \
  latest_equal_weight_signal_5d_20z/equal_weight_signal_contrast-5-20.csv \
  | tee /tmp/claude-1000/-home-elfbob-claude-code-20260325/8e576532-53cd-4290-a12d-21f8b7015ba8/scratchpad/baseline_md5.txt
```

**Step 1.2: 原位重跑 ①（根目录，顺序执行）**

```bash
python3 update_growth_stability.py && python3 update_confirmed_signal.py
```

预期：正常退出，打印输出文件名。

**Step 1.3: 原位重跑 ②**

```bash
cd citic_style_signal_40d && python3 optimize_signal.py && cd ..
```

（若脚本有 argparse 默认值即可直接跑；报错则看 `--help` 后带默认参数跑）

**Step 1.4: 原位重跑 ③ 两变体**

```bash
cd latest_equal_weight_signal && python3 generate_equal_weight_signal_contrast.py \
  --input data.csv --config style_factor_groups.csv --output equal_weight_signal_contrast.csv && cd ..
cd latest_equal_weight_signal_5d_20z && python3 generate_equal_weight_signal_contrast.py \
  --input data.csv --config style_factor_groups.csv --output equal_weight_signal_contrast-5-20.csv && cd ..
```

**Step 1.5: 确认 git 树 clean = 基线可复现**

```bash
git status --porcelain
```

预期：空输出。若有 diff：说明输出与数据/脚本不同步，`git diff` 查看差异——若差异仅因数据在上次运行后更新过，接受重跑结果为新基线：`git add -A && git commit -m "chore: 重跑刷新基线输出"`，并更新 Step 1.1 的 md5 文件。若差异原因不明，**停下来向用户报告**。

**Step 1.6: 跑现有测试确认绿色起点**

```bash
python3 -m pytest tests/ -q
```

预期：全部通过（对根目录旧脚本的测试）。记录通过数量。

---

## Task 2: 建目录骨架 + git mv 归位 + 去重

**Files:** 全部为移动/重命名，唯一新建是目录。重复文件（md5 已核实相同）只保留一份。

**Step 2.1: 建目录**

```bash
mkdir -p signals/hybrid20 signals/citic40d signals/equal_weight \
  data output/hybrid20 output/citic40d output/equal_weight \
  archive/old_scripts/root archive/old_scripts/latest_equal_weight_signal \
  archive/old_scripts/latest_equal_weight_signal_5d_20z \
  archive/old_data archive/old_outputs/root archive/old_outputs/citic40d \
  archive/old_outputs/equal_weight_20d40z archive/old_outputs/equal_weight_5d20z
```

**Step 2.2: ① 归位**

```bash
git mv update_growth_stability.py update_confirmed_signal.py optimize_signal.py signals/hybrid20/
git mv hybrid_20_signal说明.md signals/hybrid20/README.md
git mv growth_stability_signal.csv confirmed_signal.csv output/hybrid20/
```

**Step 2.3: ② 归位（脚本改名 generate_signal.py）**

```bash
git mv citic_style_signal_40d/optimize_signal.py signals/citic40d/generate_signal.py
git mv citic_style_signal_40d/citic_style_signal_40d.csv output/citic40d/
git mv citic_style_signal_40d/four_threshold_optimization_summary.csv archive/old_outputs/citic40d/
git mv citic_style_signal_40d/four_threshold_optimized_positions.csv archive/old_outputs/citic40d/
git rm -q citic_style_signal_40d/中信风格合并.csv   # 与根目录同 md5 (66558c81…)，去重
rmdir citic_style_signal_40d
```

**Step 2.4: ③ 归位（A 版脚本为合并基底）**

```bash
git mv latest_equal_weight_signal/generate_equal_weight_signal_contrast.py signals/equal_weight/generate_signal.py
git mv latest_equal_weight_signal/style_factor_groups.csv signals/equal_weight/config_6pairs.csv
git mv latest_equal_weight_signal_5d_20z/style_factor_groups.csv signals/equal_weight/config_5pairs.csv
git mv latest_equal_weight_signal/data.csv data/成长价值指数_2019.csv
git mv "latest_equal_weight_signal/data (副本).csv" data/成长价值指数_2014.csv
git rm -q latest_equal_weight_signal_5d_20z/data.csv   # 与 成长价值指数_2014.csv 同 md5 (13dbe3ba…)
git mv latest_equal_weight_signal/equal_weight_signal_contrast.csv output/equal_weight/equal_weight_signal_20d40z.csv
git mv latest_equal_weight_signal_5d_20z/equal_weight_signal_contrast-5-20.csv output/equal_weight/equal_weight_signal_5d20z.csv
# B 版脚本与两份旧 README 归档
git mv latest_equal_weight_signal_5d_20z/generate_equal_weight_signal_contrast.py archive/old_scripts/latest_equal_weight_signal_5d_20z/
git mv latest_equal_weight_signal_5d_20z/README.md archive/old_scripts/latest_equal_weight_signal_5d_20z/
git mv latest_equal_weight_signal/README.md archive/old_scripts/latest_equal_weight_signal/
# 旧研究输出归档
git mv latest_equal_weight_signal/smooth_vs_raw_*.csv archive/old_outputs/equal_weight_20d40z/
git mv "latest_equal_weight_signal/equal_weight_signal_contrast (副本).csv" archive/old_outputs/equal_weight_20d40z/
git mv latest_equal_weight_signal/equal_weight_signal_contrast.csv.0621 archive/old_outputs/equal_weight_20d40z/
git mv latest_equal_weight_signal/data.csv.0621 archive/old_data/
git mv latest_equal_weight_signal_5d_20z/four_threshold_optimization_summary.csv archive/old_outputs/equal_weight_5d20z/
git mv latest_equal_weight_signal_5d_20z/four_threshold_optimized_positions.csv archive/old_outputs/equal_weight_5d20z/
git rm -q latest_equal_weight_signal_5d_20z/data0621.csv   # 与 data.csv.0621 同 md5 (961cb39b…)
rm -rf latest_equal_weight_signal/__pycache__ latest_equal_weight_signal_5d_20z/__pycache__
rmdir latest_equal_weight_signal latest_equal_weight_signal_5d_20z
```

**Step 2.5: 共享输入数据归位**

```bash
git mv 中信风格合并.csv 沪深300.csv 指数.xlsx data/
```

**Step 2.6: ④ 与旧版脚本、旧数据、旧输出归档**

```bash
git mv 对比 archive/对比
git rm -q data.csv   # 指数代码宽表，与 archive/对比/data.csv 同 md5 (26009330…)，那份保留
git mv generate_equal_weight_signal.py generate_equal_weight_signal_contrast.py archive/old_scripts/root/
git mv style_factor_groups.csv archive/old_scripts/root/   # 旧 15 组指数代码配置
git mv tests/test_generate_equal_weight_signal.py archive/old_scripts/root/
git mv equity_index_futures_daily.csv archive/old_data/
git mv citic_vs_config_signal_nav_comparison.png citic_vs_config_signal_performance.csv \
  equal_weight_signal.csv equal_weight_signal_contrast.csv \
  four_threshold_optimization_summary.csv four_threshold_optimization_summary_equal_weight_40d.csv \
  four_threshold_optimized_positions.csv four_threshold_optimized_positions_equal_weight_40d.csv \
  multi_sleeve_timing_signal.csv optimized_signal.csv optimized_threshold_positions.csv \
  threshold_optimization_summary.csv smallcap_signal_backtest_summary.csv \
  two_signal_common_period_performance.csv two_signal_nav_comparison.png \
  archive/old_outputs/root/
```

**Step 2.7: docs 收拢 + 清缓存**

```bash
git mv docs/superpowers/specs/2026-06-18-equal-weight-signal-design.md docs/plans/
rmdir docs/superpowers/specs docs/superpowers/plans docs/superpowers 2>/dev/null; rm -rf .pytest_cache tests/__pycache__
```

**Step 2.8: 检查无遗漏并提交**

```bash
ls   # 根目录应只剩: README相关尚无, signals/ data/ output/ archive/ docs/ tests/ .claude/ .gitignore
git status --porcelain | grep -v '^R\|^D\|^A' ; git add -A
git commit -m "chore: 文件归位——signals/data/output/archive 四区结构，md5 重复文件去重"
```

预期：根目录只剩目录 + .gitignore；git status 中全部是 rename/delete，无意外 modify。

---

## Task 3: ① hybrid20 路径适配 + 字节级验证

**Files:**
- Modify: `signals/hybrid20/update_growth_stability.py`（顶部常量区）
- Modify: `signals/hybrid20/update_confirmed_signal.py`（顶部常量区）
- Modify: `signals/hybrid20/optimize_signal.py`（4 处硬编码路径）

**Step 3.1: 统一锚定模式改 update_growth_stability.py**

原：

```python
INPUT_FILE = "中信风格合并.csv"
OUTPUT_FILE = "growth_stability_signal.csv"
```

改为（`parents[2]` = 仓库根，因脚本在 signals/hybrid20/ 两层之下）：

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "data" / "中信风格合并.csv"
OUTPUT_FILE = ROOT / "output" / "hybrid20" / "growth_stability_signal.csv"
```

（若文件已 import pathlib 则不重复；pd.read_csv/to_csv 接受 Path 对象，调用处无需改。）

**Step 3.2: 同模式改 update_confirmed_signal.py**

```python
ROOT = Path(__file__).resolve().parents[2]
STYLE_FILE = ROOT / "data" / "中信风格合并.csv"
ORIG_SIGNAL_FILE = ROOT / "output" / "hybrid20" / "growth_stability_signal.csv"
OUTPUT_FILE = ROOT / "output" / "hybrid20" / "confirmed_signal.csv"
```

**Step 3.3: 改 optimize_signal.py 的 4 处内联路径**

先 `grep -n 'read_csv\|to_csv' signals/hybrid20/optimize_signal.py` 确认全部 I/O 点（已知 4 处：中信风格合并.csv、沪深300.csv、growth_stability_signal.csv、optimized_signal.csv）。顶部加 ROOT 常量，4 处分别替换为 `ROOT / "data" / "中信风格合并.csv"`、`ROOT / "data" / "沪深300.csv"`、`ROOT / "output" / "hybrid20" / "growth_stability_signal.csv"`、`ROOT / "output" / "hybrid20" / "optimized_signal.csv"`。

**Step 3.4: 从仓库根重跑 ① 并验证字节一致**

```bash
python3 signals/hybrid20/update_growth_stability.py && python3 signals/hybrid20/update_confirmed_signal.py
md5sum output/hybrid20/growth_stability_signal.csv output/hybrid20/confirmed_signal.csv
```

预期：两个 md5 与 `baseline_md5.txt` 中对应值**完全相同**；`git status` 只显示 .py 修改，csv 无变化。不一致则停下排查（禁止"看起来差不多"就继续）。

**Step 3.5: optimize_signal.py 冒烟验证（编译 + 试跑）**

```bash
python3 -m py_compile signals/hybrid20/optimize_signal.py && echo COMPILE_OK
timeout 300 python3 signals/hybrid20/optimize_signal.py && md5sum output/hybrid20/optimized_signal.csv
```

预期：能跑通并写出 optimized_signal.csv（研究脚本，数据比 6 月新，数值与旧归档**不同是预期**，不做 md5 对比；跑完 `git checkout -- output/hybrid20/optimized_signal.csv` 不适用——该文件未入 output 基线，直接保留新产物并入库）。超时则报告用户，不算失败。

**Step 3.6: 提交**

```bash
git add -A && git commit -m "refactor(hybrid20): 路径锚定仓库根，输出字节级一致验证通过"
```

---

## Task 4: ② citic40d 路径适配 + 字节级验证

**Files:**
- Modify: `signals/citic40d/generate_signal.py`（INPUT_FILE / OUTPUT_FILE 常量，该脚本已有 argparse，只改默认值）

**Step 4.1: 改常量**

```python
ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "data" / "中信风格合并.csv"
OUTPUT_FILE = ROOT / "output" / "citic40d" / "citic_style_signal_40d.csv"
```

注意：argparse 若把常量作为 default，需确认 default 用的是这两个常量名；`load_style_data` 的参数默认值同步。

**Step 4.2: 重跑验证**

```bash
python3 signals/citic40d/generate_signal.py
md5sum output/citic40d/citic_style_signal_40d.csv
```

预期：md5 与基线相同，`git status` csv 无变化。

**Step 4.3: 提交**

```bash
git add -A && git commit -m "refactor(citic40d): optimize_signal.py→generate_signal.py，路径锚定，输出字节级一致"
```

---

## Task 5: ③ 合并两变体为参数化脚本（先改测试再改实现）

**Files:**
- Modify: `signals/equal_weight/generate_signal.py`（A 版基底上参数化）
- Modify: `tests/test_generate_equal_weight_signal_contrast.py`（MODULE_PATH + 新参数用例）

两变体已知全部差异（diff 核实）：A 版 `LOOKBACK=20`、z 窗口=`lookback*2`（40）、`SMOOTHING_WINDOW=5`、列后缀 `_20`；B 版 `LOOKBACK=5`、`Z_WINDOW=20`、无平滑（`factor_value = factor_value_raw`）、列后缀 `_5`。

**Step 5.1: 更新测试指向 + 补两个失败用例**

`tests/test_generate_equal_weight_signal_contrast.py`：

```python
MODULE_PATH = Path(__file__).resolve().parents[1] / "signals" / "equal_weight" / "generate_signal.py"
```

新增用例（放文件末尾；`_prices`/`_config` 等已有 fixture 风格照用）：

```python
def test_no_smoothing_keeps_raw_value():
    module = _load_module()
    # 用现有用例同样的合成数据构造 prices/config
    result = module.calculate_signals(prices, config, lookback=5, z_window=20, smoothing_window=0)
    pd.testing.assert_series_equal(
        result["factor_value"], result["factor_value_raw"], check_names=False
    )

def test_column_suffix_follows_lookback():
    module = _load_module()
    result = module.calculate_signals(prices, config, lookback=5, z_window=20, smoothing_window=0)
    assert "raw_signal_5" in result.columns
    assert any(c.startswith("pair_01_factor_5") for c in result.columns)
```

（具体函数签名以现脚本 `calculate_signals`/等价函数为准，执行时先读脚本再定参数名；原则：lookback、z_window、smoothing_window 三参数入函数签名，smoothing_window=0 表示不平滑。）

**Step 5.2: 跑测试确认新用例失败、旧用例通过**

```bash
python3 -m pytest tests/ -q
```

预期：旧用例 PASS（MODULE_PATH 已指向 A 版基底，行为不变），新 2 例 FAIL（参数不存在）。

**Step 5.3: 参数化实现**

`signals/equal_weight/generate_signal.py` 修改点：
1. 常量区：`LOOKBACK = 20`、`Z_WINDOW = None`（None → lookback*2）、`SMOOTHING_WINDOW = 5` 保留为默认。
2. 计算函数签名加 `lookback`、`z_window`、`smoothing_window`；z 窗口全部引用处（min_periods 判断）用 `z_window or lookback * 2`。
3. `smoothing_window == 0` 时 `output["factor_value"] = output["factor_value_raw"]`（不再要求 >0，改为 >=0 校验，负数报错）。
4. 列名后缀：现脚本的 `OUTPUT_WINDOW_LABEL` 改为跟随 lookback。
5. argparse 加 `--lookback`（int，默认 20）、`--z-window`（int，默认 None）、`--smoothing`（int，默认 5）；打印摘要同步。

**Step 5.4: 测试全绿**

```bash
python3 -m pytest tests/ -q
```

预期：全部 PASS。

**Step 5.5: 提交**

```bash
git add -A && git commit -m "feat(equal_weight): 合并 20d40z/5d20z 两变体为参数化单脚本（--lookback/--z-window/--smoothing）"
```

---

## Task 6: ③ 真实数据字节级验证

**Step 6.1: 变体 A 参数重现 20d40z 输出**

```bash
python3 signals/equal_weight/generate_signal.py \
  --input data/成长价值指数_2019.csv \
  --config signals/equal_weight/config_6pairs.csv \
  --lookback 20 --z-window 40 --smoothing 5 \
  --output output/equal_weight/equal_weight_signal_20d40z.csv
md5sum output/equal_weight/equal_weight_signal_20d40z.csv
```

预期：md5 与 baseline_md5.txt 中 `latest_equal_weight_signal/equal_weight_signal_contrast.csv` 一致（列名 A 版本就带 `_20` 后缀，应字节级相同）。

**Step 6.2: 变体 B 参数重现 5d20z 输出**

```bash
python3 signals/equal_weight/generate_signal.py \
  --input data/成长价值指数_2014.csv \
  --config signals/equal_weight/config_5pairs.csv \
  --lookback 5 --z-window 20 --smoothing 0 \
  --output output/equal_weight/equal_weight_signal_5d20z.csv
md5sum output/equal_weight/equal_weight_signal_5d20z.csv
```

预期：md5 与基线 `…contrast-5-20.csv` 一致。**已知可能差异**：B 版原脚本输出列可能与 A 基底列集有差（A 多 smoothing 打印；两版都输出 factor_value_raw+factor_value）。若 md5 不一致：用 pandas 逐列数值比对（`pd.testing.assert_frame_equal(..., check_exact=True)` 对齐共同列），数值全等 + 仅列序/表头差异 → 记录说明后接受；数值有差 → **停，逐步 diff 中间量（spread→z→tanh→平均→平滑）定位**，不许放过。

**Step 6.3: git 确认 + 提交**

```bash
git status --porcelain   # 预期：干净（输出与 Task 2 移入的旧文件字节相同）或仅记录说明文件
git add -A && git commit -m "test(equal_weight): 合并脚本双参数组真实数据字节级复现验证" --allow-empty
```

---

## Task 7: 文档收尾

**Files:**
- Create: `README.md`（根，总索引）
- Create: `data/README.md`
- Create: `archive/README.md`
- Create: `signals/equal_weight/README.md`（两旧 README 合并改写）
- Modify: `signals/hybrid20/README.md`（原说明.md 里的文件路径改到新位置）

**Step 7.1: 根 README.md** — 内容骨架：项目一句话；三条信号线表格（目录 / 一句话逻辑 / 输入 / 输出 / 运行命令）；数据流图（data/ → signals/ → output/）；archive 说明一行；docs/plans 索引。运行命令均为从仓库根执行的完整命令（Task 3/4/6 已验证的原句）。

**Step 7.2: data/README.md** — 每个文件一节：`中信风格合并.csv`（中信五风格指数收盘价，跳过前 5 行元数据、6 列、①② 使用、人工从 Wind 导出更新）；`成长价值指数_2019.csv` / `_2014.csv`（12 列成长/价值配对收盘价、③ 使用、两份历史深度差异及科创 2019 前为空的原因）；`沪深300.csv`（① optimize 用，跳过前 6 行）；`指数.xlsx`（人工源表备查，无脚本引用）。更新方式一句话：追加新行保存为 UTF-8 CSV 即可，脚本自动排序。

**Step 7.3: archive/README.md** — 四个子区各一句话 + 三处 md5 去重记录（哪个文件删了、与哪个保留文件相同、md5 前 8 位）。

**Step 7.4: signals/equal_weight/README.md** — 以 A 版旧 README 为底（archive 里），改：脚本名/路径、两套预设参数表（A: 20/40/5 默认，B: 5/20/0）、两份 config 文件说明（6 组含科创 / 5 组无科创，**修正旧文档"15 组"过时表述**）、输入数据指向 data/ 两份 CSV、验证过的运行命令。

**Step 7.5: signals/hybrid20/README.md 路径勘误** — 原说明.md 中 `中信风格合并.csv`→`data/`、输出→`output/hybrid20/`、运行命令改 `python3 signals/hybrid20/update_growth_stability.py` 等；`20260401/backtest_report01.xlsx` 一处外部引用保持原文。

**Step 7.6: 提交**

```bash
git add -A && git commit -m "docs: 根/数据/归档/信号线 README 收尾"
```

---

## Task 8: 终验 + 汇报

**Step 8.1: 全量重跑三条线 + 测试**

```bash
python3 signals/hybrid20/update_growth_stability.py && \
python3 signals/hybrid20/update_confirmed_signal.py && \
python3 signals/citic40d/generate_signal.py && \
python3 signals/equal_weight/generate_signal.py --input data/成长价值指数_2019.csv --config signals/equal_weight/config_6pairs.csv --output output/equal_weight/equal_weight_signal_20d40z.csv && \
python3 signals/equal_weight/generate_signal.py --input data/成长价值指数_2014.csv --config signals/equal_weight/config_5pairs.csv --lookback 5 --z-window 20 --smoothing 0 --output output/equal_weight/equal_weight_signal_5d20z.csv && \
python3 -m pytest tests/ -q && git status --porcelain
```

预期：五个输出全部再生且 git 树 clean（幂等），测试全绿。

**Step 8.2: 向用户汇报** — 新结构树、验证结果（哪些字节级一致）、GitHub 推送待办（用户建好 `surtu-r-wong/style_timing_signal` 后执行 `git remote add origin git@github.com:surtu-r-wong/style_timing_signal.git && git push -u origin main`）。
