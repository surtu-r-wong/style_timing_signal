"""换执行标的探针（探索性，**非预登记正式跑**）。

## 命题

现役策略 = 用「四对成长 vs 价值」的等权信号（`equal_weight` 的 `factor_value`）择时
**做多中证500/1000 指数**（`backtest/data.py:23` 的 `_SPOT`，blend = 两者收益各半）。
用户 2026-08-17 提出：信号侧已测尽，换**执行标的**再找最优 —— 沪深300、中证500、
中证1000、微盘（中证2000），单独或组合。

数据在 `index_daily` 库内。**额度说明**：前 5 个标的零 Wind 额度；
`8841431.WI`（万得微盘，2026-08-18 为 P1-2 补入库）一次性花费约 **18,400 格**
（6 字段 × 3,070 交易日；登记时"≈3,000 格"的估计是按单字段算的，实际端点固定 6 字段）。
入库后同样零额度复用。

## 两个必须先声明的口径问题

**(1) carry 不可比 —— 本模块横向对比一律用零 carry。**
现役读数含 carry（IC/IM 年化基差，持多在贴水中赚 carry，对多头是**正贡献**）。但
沪深300 的 IF 只到 2026-04-29（futures_daily 断更），**中证2000 根本没有对应期货**。
若让有期货的标的带 carry、没期货的不带，横向比就是拿"现货+贴水收益"比"纯现货"。
故：**主表零 carry**，含 carry 版本只对有期货的标的单列参考（`--with-carry`）。

**(2) 信号与标的的匹配度。** 信号是四对（300/500/1000/2000 各自的成长vs价值）等权
平均，所以拿它交易任一单一规模指数时，信号里有 3/4 的信息来自别的规模层。本模块
第一阶段**固定用现役四对等权信号**，只换标的——把"换标的"与"换信号"两件事分开，
否则读数无法归因。信号-标的匹配（如用 500 那一对的信号交易 500）留作第二阶段。

## 纪律声明

这是探索性读数，**不支持部署变更**。标的维度同样有选优偏差，且项目对它已有前例：
①a 的裁决原话是「500 口径两格名义显著 = 嵌套 + 多重比较，**不许摘取**」。现有三
口径实测就已呈现判据冲突（full 排 500>blend>1000，worst_tv 排完全相反），任何
"挑最好的标的"都必须走 `backtest/selection_permutation.py`（⓪ 机器）出 `p_selected`。

## 用法

    python3 -m backtest.underlying_probe
    python3 -m backtest.underlying_probe --with-carry     # 附含 carry 参考列
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import WINDOWS, evaluate  # noqa: E402
from backtest.data import _connect, load_carry, load_db_config  # noqa: E402
from backtest.positions import production_position  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402

# 单一标的：index_daily 的 index_code → 显示名
SINGLES = {
    "300":  ("000300.SH", "沪深300"),
    "500":  ("000905.SH", "中证500（现役分量）"),
    "1000": ("000852.SH", "中证1000（现役分量）"),
    "2000": ("932000.CSI", "中证2000（微盘代理）"),
    "div":  ("000922.CSI", "中证红利"),
    # 上界参考（P1-2）：**不可执行**——无 ETF 无期货，自建 400 成分即已 STOP 的 B3，
    # 且指数不扣交易成本 → 表观收益系统性高估。只回答"若能完美复制，超额还剩多少"，
    # **不构成候选、不进任何篮子、不得被"挑最好的标的"摘走**（见模块纪律声明）。
    "micro": ("8841431.WI", "万得微盘（上界参考·不可执行）"),
}
# 组合：等权平均日收益（沿用 data.blend_returns 的 50/50 语义，推广到 n 腿）
BASKETS = {
    "500+1000（=现役 blend）": ["500", "1000"],
    "1000+2000（小盘）": ["1000", "2000"],
    "300+500（大中盘）": ["300", "500"],
    "300+500+1000+2000（四层等权）": ["300", "500", "1000", "2000"],
}
# 有对应股指期货、可算 carry 的标的（IF 只到 2026-04-29；中证2000 无期货）
CARRY_OK = {"500": "500", "1000": "1000"}
OUT = ROOT / "backtest" / "output" / "underlying_probe.csv"


def load_index_returns(index_code: str) -> pd.Series:
    """按 index_code 直接取收盘价日收益。

    不走 `data.load_spot_close`（它绑死 `_SPOT` 的两个口径），也不改那个既有映射
    —— 零侵入地支持任意指数码。
    """
    db = load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT trade_date, close FROM {db['schema']}.index_daily "
                "WHERE index_code=%s ORDER BY trade_date", (index_code,))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise RuntimeError(f"index_daily 里没有 {index_code} 的数据")
    px = pd.Series({pd.Timestamp(d): float(c) for d, c in rows}).sort_index()
    return px.pct_change().dropna()


def equal_weight_basket(legs: list[pd.Series]) -> pd.Series:
    """n 腿等权平均日收益（`data.blend_returns` 的 50/50 推广）。

    先 `dropna` 对齐再平均 —— 缺腿日直接剔除，不用 fillna(0)：那会把缺腿当"当日
    零收益"，等于偷偷降暴露（`blend_carry` 对 carry 用 fillna(0) 是另一回事，
    carry 缺失确实等于没有贴水收益）。
    """
    df = pd.concat(legs, axis=1).dropna()
    return df.mean(axis=1)


def build_underlyings() -> dict[str, pd.Series]:
    singles = {k: load_index_returns(code) for k, (code, _) in SINGLES.items()}
    out = {f"{k}｜{SINGLES[k][1]}": v for k, v in singles.items()}
    for name, legs in BASKETS.items():
        out[f"篮子｜{name}"] = equal_weight_basket([singles[l] for l in legs])
    return out


def run(cost_bps: float = 3.0, with_carry: bool = False) -> pd.DataFrame:
    _, smooth = load_two_columns()
    pos_full = production_position(smooth).astype(float)   # 现役 long-flat 映射
    unds = build_underlyings()

    rows = []
    for uname, und in unds.items():
        for win, (s, e) in WINDOWS.items():
            p, u = pos_full, und
            if s:
                p, u = p[p.index >= pd.Timestamp(s)], u[u.index >= pd.Timestamp(s)]
            if e:
                p, u = p[p.index <= pd.Timestamp(e)], u[u.index <= pd.Timestamp(e)]
            if len(p.index.intersection(u.index)) < 60:
                continue
            m = evaluate(p, u, None, cost_bps, 0)["long"]   # 零 carry，可比
            rows.append({"underlying": uname, "window": win, "carry": "none", **m})
            if with_carry and uname.split("｜")[0] in CARRY_OK:
                car = load_carry(CARRY_OK[uname.split("｜")[0]])
                mc = evaluate(p, u, car, cost_bps, 0)["long"]
                rows.append({"underlying": uname, "window": win,
                             "carry": "with", **mc})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="换执行标的探针（探索性）")
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--with-carry", action="store_true",
                    help="附带含 carry 的参考行（仅 500/1000 有期货）")
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()

    rep = run(args.cost_bps, args.with_carry)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(args.output, index=False)

    z = rep[rep["carry"] == "none"]
    p = z.pivot_table(index="underlying", columns="window", values="sharpe")
    a = z.pivot_table(index="underlying", columns="window", values="ann") * 100
    m = z.pivot_table(index="underlying", columns="window", values="maxdd") * 100
    t = z.pivot_table(index="underlying", columns="window", values="turnover")
    view = pd.DataFrame({
        "Sharpe": p["full"].round(4), "年化%": a["full"].round(2),
        "MaxDD%": m["full"].round(2), "换手": t["full"].round(2),
        "train": p["2014-2020"].round(4), "val": p["2021-2023"].round(4),
        "holdout": p["2024-2026"].round(4),
    })
    view["worst_tv"] = view[["train", "val"]].min(axis=1)
    print(f"零 carry 口径（可比），cost_bps={args.cost_bps}，"
          f"信号固定 = 现役 equal_weight long-flat\n")
    print(view.sort_values("Sharpe", ascending=False).to_string())
    print("\n按 worst(train,val) 排序：")
    print(view.sort_values("worst_tv", ascending=False)[
        ["Sharpe", "worst_tv", "train", "val", "holdout"]].to_string())
    if args.with_carry:
        w = rep[rep["carry"] == "with"]
        if len(w):
            print("\n含 carry 参考（仅 500/1000，full 窗）：")
            print(w[w["window"] == "full"][
                ["underlying", "sharpe", "ann", "maxdd"]].round(4).to_string(index=False))
    print(f"\n→ {args.output}")
    print("⚠️ 探索性读数：标的维度同样有选优偏差（①a 已有'不许摘取'的判例），"
          "动部署前须走 ⓪ 置换选优机器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
