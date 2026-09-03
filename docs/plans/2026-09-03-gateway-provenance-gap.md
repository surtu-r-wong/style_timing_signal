# 网关溯源缺口：现网跑着两个仓里没有源码的端点

2026-09-03 发现（部署资金流端点时顺带查出），**未处置，待用户裁决**。

## 事实

现网网关（Windows `D:\wind_gateway`，:8080）与 stock_selector `master` 的端点集**双向不一致**：

| 方向 | 端点 | 含义 |
|---|---|---|
| **现网有、master 无源码** | `/fetch/bonus`、`/fetch/futures` | **跑着的东西在仓里找不到出处** |
| master 有、现网未部署 | `/fetch/etf_option_contract`、`/fetch/etf_option_daily`、`/fetch/etf_option_quote_snapshot` | 现网这部分落后于 master |
| 在分支上、已部署 | `/fetch/market_money_flow` | `feat/index-money-flow`（本次新增，基于旧 master `cb475ed7`） |

核对方式：现网 `/openapi.json` 的 paths 集合 vs `wind_gateway/endpoints.py` 里的 `@r.get/post` 装饰器。
（`/health` `/ping` `/quota` `/admin/reload` 在 `wind_gateway/main.py` 而非 `endpoints.py`，**不属于漂移**——
初次比对时只扫了 `endpoints.py` 误报过一次，已更正。）

## 为什么要紧

**`/fetch/futures` 是有人在 Windows 机上直接改现网文件加的，源码从未回到仓里。** 后果：

1. **任何人从 master 重新部署网关，这两个端点就消失**。`/fetch/futures` 正是 08-25 carry 线卡了很久才补上的
   （见 `project-four-rulings-2026-08-25` 记忆：「⛔网关无期货端点、`/fetch/price` 不含 `oi`」），丢了要重来。
2. 现网文件是唯一副本，那台机器上没有版本控制。
3. 我这次的部署件是**以现网文件为基**插入新端点做的——因为不能以 master 为基（会抹掉那两个端点）。
   这等于把漂移又固化了一层。

## 处置建议（未执行，等裁决）

1. **先把现网文件取回仓里**：`scp` 下现网 `endpoints.py` + `config.yaml`，作为一笔「记录既成事实」的提交
   （提交信息写清这是从生产机回收的、作者不明），**不做任何修改**。
2. 再在其上合入 `feat/index-money-flow`（该分支基于旧 master `cb475ed7`，master 已走到 `b2d479e3`，需先并进来）。
3. 三个 etf_option 端点是否要部署，单独裁——它们在 master 里躺着但从未上线，可能是有意的。
4. 长期：网关部署改成「从仓里某个 tag 部署」，禁止直接改现网文件。这一条超出本次范围。

**本次未做任何合并或部署**——master 已前移、现网是唯一副本、且涉及别人加的端点，
不适合我单方面动。
