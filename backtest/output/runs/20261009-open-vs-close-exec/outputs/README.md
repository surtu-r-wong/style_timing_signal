# 20261009-open-vs-close-exec：现役两池「T+1 开盘 vs T+1 收盘」执行检验（按四类动作拆）

结果文档：`docs/plans/2026-10-09-open-vs-close-exec-results.md`；登记表条目 `open-vs-close-execution-timing`。
全部数字来自冻结规格（`outputs/spec.md` + `outputs/spec_addendum_s_exact.md`）+ 两个独立实现（impl / qa），
比对记录 `compare/compare_results.txt`。

## 目录

- `../inputs/` —— 跑前冻结输入（sha256 见 `snapshot.json`）：
  - `futures.csv`（IC/IM 全部合约至 2026-10-08，库内快照；**>1 MiB 本机保留**）
  - `spot.csv`（000905/000852 收盘价 + 当时仅有的 2026-07-01 起 132 个开盘价）
  - `spot_v2.csv` / `snapshot_v2.json` —— 办公室 2026-10-09 10:37 补齐开盘价后的同库快照
    （6,470 行只填空列；close 与 `spot.csv` 逐位相同，回函见 `data_manager/requests/2026-10-09-style-timing-signal-csi500-1000-open-backfill/response-01-office-2026-10-09.md`）
  - 信号 / 仓位 CSV（`equal_weight_symmetric`、`equal_weight_signal_20d40z`、`slope20_longflat`、`slope20_signal_L20zw120`）——
    2026-10-09 08:41 从工作区拷贝
  - `code/snapshot.py`（冻结脚本）、`code/verify_fill.py`（对办公室回补的独立验收）
- `impl/` —— 实现侧：`run.py`（母规格）、`s_exact.py`（追加规格）、`report.md`、`report_s_exact.md`、
  `key_numbers.json`、`key_numbers_s_exact.json`、`checks*.json`、events / legs / policies / by_year / limit / daily CSV、
  `crosscheck.py`。`daily_F.csv`、`daily_S.csv`、`daily_S_exact.csv` **>1 MiB 本机保留**。
- `qa/` —— 独立 QA 侧（不 import impl）：`qa.py`、`qa_s_exact.py`、三个口径的 key_numbers（spec 主表 / lit / ev 变体）、
  `qa_report.md`、`checks*.json`、events / limit / daily CSV、run log。
- `diag/` —— 辅助诊断：`roll_timing.py`（换月时点混杂 +0.70pp/年）、`if300_snapshot.py` / `if300_bias.py`
  （IF vs 沪深300 真实开盘价的代理误差类比）。
- `compare/` —— `compare2.py` 与 `compare_results.txt`。

## 注意

- 归档供审阅复核，**不是原位可跑**：impl / qa 脚本里的相对路径按当时工作目录（`~/claude-code/.openexec/`）写死
  （如 `run.py` 读 `../inputs/`）。复算以正本目录为准，或按本目录结构调整路径。
- 母规格 §5 自检与追加规格锚点全部通过（E0 对 `backtest.engine.run_strategy` 逐日 0 差、E1≡p·cc、
  可加性对解析二阶项、代理重算对母规格逐位、66 日真实开盘事件对齐）。
- 本机保留大文件（4 个，>1 MiB）：路径 / 字节数 / SHA-256 见
  `backtest/output/LOCAL_ONLY_LARGE_FILES_20261009.tsv` 与 `manifest.json`。
