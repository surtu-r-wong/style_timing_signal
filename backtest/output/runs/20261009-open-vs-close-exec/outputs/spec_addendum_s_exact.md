# 追加规格：现货池用真实指数开盘价重算（S-exact）——2026-10-09 跑前冻结

母规格 `spec.md` 不变；本追加只把现货池 S 的代理换成真实值。办公室已按请求函补齐 000905.SH、000852.SH 在 2013-03-06 ~ 2026-06-30 的开盘价。我方已验收：收盘价与 08:41 快照逐位相同，原有 132 个开盘价不变，OHLC 自洽。

## 输入

- 指数：`inputs_v2/spot.csv`（date, symbol, open, close；6,602 行，2013-03-06 ~ 2026-10-08；sha256 见 `inputs_v2/snapshot.json`）。
- 仓位：沿用 `inputs/slope20_longflat.csv`。
- 期货：沿用 `inputs/futures.csv`，只用来重算代理，以便逐事件比较。

## 定义（与母规格 §1~§3 相同，只换数据）

- 每腿真实值：`gap_g(t) = O_t / C_{t−1} − 1`，`intra_g(t) = C_t / O_t − 1`，`cc_g(t) = C_t / C_{t−1} − 1`。
- blend 为两腿各 0.5，日度再平衡，与母规格的公式、成本（3bp）、策略（E0、E1、E2、M_开多、M_平多）、事件归因（e、o）完全相同。
- S 是 long-flat，非事件日乘积式恒等于 p·cc，spec / lit / ev 三种口径在 S 上重合。
- 仓位的 shift 一律用仓位文件自己的日历（全序列，不先截窗）。

## 窗口

- **主窗口 main**：2016-01-04 ~ 2026-10-08，与 S-proxy full 相同，用来直接对比代理和真实值。
- **延长窗口 ext**：2014-01-06 ~ 2026-10-08。
- **分窗**：2014-2015（2014-01-06 ~ 2015-12-31）、2016-2020、2021-2023、2024-2026。

## 报告

1. main 窗口：
   - 每腿 n、e 均值（bp）、e 合计（pp）、o 均值（bp）、t 检验 p；
   - 各策略的年化、Sharpe、最大回撤；
   - E2−E1 的 ΔSharpe：配对 moving-block bootstrap，block=20、n=2000、seed=20261009；
   - **代理误差**：同一批事件上 e_exact − e_proxy 的均值和 t 值，按腿报。
2. ext 窗口：同上，不含代理误差。
3. 各分窗 E1、E2 的 Sharpe。
4. 一句话结论：真实值是否维持代理版的方向和量级（代理版：E1 0.739 → E2 0.880，开多 +21.2bp，平多 +20.7bp）。

产出 `key_numbers_s_exact.json`，结构如下：

```
{"main": {"legs": {"开多": {"n":int,"mean_e_bp":float,"sum_e_pp":float,"mean_o_bp":float,"p_t":float}, "平多": {...}},
          "policies": {"E0": {"ann":float,"sharpe":float,"maxdd":float}, "E1": {...}, "E2": {...}, "M_开多": {...}, "M_平多": {...}},
          "e2_minus_e1": {"d_sharpe":float,"ci_lo":float,"ci_hi":float,"p":float},
          "proxy_error": {"开多": {"mean_bp":float,"t":float}, "平多": {...}},
          "n_days":int, "first_day":"YYYY-MM-DD", "last_day":"YYYY-MM-DD"},
 "ext": {同 main，但没有 proxy_error},
 "sharpe_by_window": {"2014-2015": {"E1":float,"E2":float}, "2016-2020": {...}, "2021-2023": {...}, "2024-2026": {...}}}
```

指标用 house `backtest.metrics`；bootstrap 用 house `paired_block_bootstrap_sharpe_diff`。
