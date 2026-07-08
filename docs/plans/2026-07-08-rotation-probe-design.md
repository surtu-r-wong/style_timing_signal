# rotation 短窗本质验证（rotation probe）设计（2026-07-08，brainstorm 定稿）

> 前置：B2 分解（`2026-07-08-b2-industry-neutral-decomposition.md`）——rotation :=
> v1 混合价差 − v2 行业中性价差 = 纯行业配置分量；慢频信号化后月频 IC≈0、对指数对
> 价差 d5/d10 Spearman ~0.10-0.11。用户选先验证本质（不直接建引擎）、三关闸门宁严勿捣。

## 1. 命题

rotation 的短窗信息是否构成一条**独立、显著、成本后存活**的快频信号线。
任一关不过 → 停线归档（负结果入库，与广度背离同柜）。

## 2. 相对 decompose 初步数字的四个升级

1. **换目标**：decompose 的 IC 目标是指数对价差；本轮直接测**宽基期货口径**
   （`backtest.data.load_underlying_returns` 500/1000/blend，含 carry）——真实可交易标的；
2. **换形态**：已有 d5/d10 是慢频信号（20d40z）的残余预测；补短形态小网格
   lb∈{5,10,20}（z=2×lb）× smoothing∈{0,3} 共 6 形态 × 持有 k∈{3,5,10,20}，
   §3.2 高原原则选代表；
3. **补显著性**：重叠窗自相关膨胀 → 主判据 = **非重叠 k 日窗 Spearman + 循环移位
   置换检验**（保留双序列各自自相关，1000 次）；
4. **增量对照（闸门组成）**：rotation 短窗 IC 与 v1/v2 信号短窗 IC 量级相当
   （~0.10-0.12），可能只是风格族共有短窗残余——控制 equal_weight 生产信号后的
   **偏 rank IC** 必须仍显著，否则=重复建设。

## 3. 三关闸门（任一不过即停）

- **关1 显著且独立**：最佳高原形态对 blend 宽基，非重叠 IC 置换 p<0.05，
  且偏 IC（控 ew）仍 >0 显著；
- **关2 分窗稳健**：2014-19 / 2020-26 同号；
- **关3 成本后为正**：信号符号 → ±1 持仓 k 日持有（`hold_position`），
  3bp 单边 + carry（`backtest.engine.run_strategy`），净 Sharpe > 0。

## 4. 实现

`backtest/rotation_probe.py`（与 pure_style_eval 并列）。复用：committed
spread_U2{,_neutral}.csv（U0 稳健性副本）、backtest.data、backtest.engine。

新纯函数（TDD）：
1. `series_signal(ret, lb, zw, sm)` — 单序列短窗信号化（rolling lb 累计→z→tanh→平滑）；
2. `nonoverlap_ic(sig, fwd_ret, k)` — 非重叠 k 日窗 Spearman；
3. `shift_permutation_pvalue(sig, ret, k, n)` — 循环移位置换 p；
4. `partial_rank_ic(sig, fwd, control)` — 偏 rank IC（rank 后对 control 取残差再相关）；
5. `hold_position(signal, k)` — 每 k 日按信号符号换仓。

CLI 产出：IC 面板（6 形态×4 持有×3 口径）+ 三关判定表 →
`backtest/output/rotation_probe.csv` + 控制台 PASS/STOP 裁决。

## 5. 判定后路径

- **STOP**：结果并入 B2 记录文档 + roadmap 归档「rotation 短窗已证伪」；
- **PASS**：进快频腿引擎设计（与 long-flat 慢频引擎的组合、非对称门槛——
  另开 brainstorm，不在本轮 scope）。

## 6. ★ 结果（2026-07-08 同日落地）——✗ STOP，关 1 独立增量不过

TDD 10 新测试（全套 143 过）；`python3 -m backtest.rotation_probe --n-perm 1000`，
产出 `backtest/output/rotation_probe.csv`。

**IC 面板结构（blend）**：lb5 形态全窗**全负**（−0.01~−0.08）、lb10 中性偏正、
**lb20 全正且两半窗皆稳**（k=3/5/10/20 → 0.106/0.131/0.176/0.195）——
**「短窗信息」假设本身被证伪：rotation 的信息本质是慢频行业动量**，B2 看到的
d5/d10 IC 只是慢信号（20d40z）的残余投影，缩短形态反而摧毁信息。

**三关裁决（最佳高原 lb20sm3 / k=20）**：

| 关 | 指标 | 结果 |
|---|---|---|
| 关1a 显著 | IC 0.193，置换 p=0.036 | ✓ |
| **关1b 独立增量** | **偏 IC（控 ew）= 0.047，p=0.60** | **✗** |
| 关2 分窗 | 0.283 / 0.230 同号 | ✓ |
| 关3 成本后 | 净 Sharpe 0.52（换手 14） | ✓ |

**判读**：若无关 1b，「显著+稳定+净正」三项会误判出一条新信号——独立增量检验
揭示 rotation 对宽基的预测信息几乎全被 equal_weight 生产信号覆盖（rotation 本就
是 v1 价差的拆出分量、v1 与 ew 相关 0.91，信息同源）。**重复建设，非增量 → 停线**。

**归档结论**：①行业轮动分量无快频信息（慢频动量）②其慢频动量对宽基择时无
独立于生产信号的增量 ③方向 C 全弧（B1 复现 → B2 分解 → 切主证伪 → rotation
probe 证伪）四步全部闭环——自建篮子体系的净产出 = 风格测量仪（研究/仪表盘）
+ 「指数对混合体各半」的结构认知 + equal_weight 生产地位的双重确认。
