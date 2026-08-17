"""双通道执行探针：现货一笔 + 期货一笔（探索性，**非预登记正式跑**）。

## 用户给定的执行约束（2026-08-17）

  * 现货一笔、期货一笔，两个独立下单通道
  * 权重 = **名义本金** 0.4 现货 / 0.6 期货（**不是保证金**，故总敞口 = 1.0，
    不含杠杆；期货腿按名义敞口计，保证金占用与本模块的收益口径无关）
  * 两腿各出一套仓位

## 为什么这个结构成立

`underlying_probe` 的读数显示两条路线的最优标的**不是同一个**：

  * 零 carry（现货/ETF 可执行的全集）→ 中证2000 最优 1.5300，且规模单调
    2000>1000>500>300
  * 含 carry（只有 300/500/1000 有股指期货）→ 中证500 最优 1.7351，IC 贴水
    单独贡献 **+0.40** Sharpe

双通道正好各取所长：现货腿去做**没有期货但信号最有效**的微盘，期货腿去做
**贴水最厚**的中证500。这不是把两个次优拼一起，而是两个通道各自的最优。

## 收益口径

期货腿收益 = 标的现货收益 + carry/245（`engine.run_strategy` 的既有实现：
`carry_ret = pos_eff × carry / ANN`，正 carry = 贴水，持多赚贴水收敛）。
现货腿 carry=None。**两腿分别跑引擎再按名义权重加权收益** —— 不能把仓位加权后
只跑一次，因为两腿的标的与 carry 都不同。各腿换手成本在各自引擎内已计。

**carry 覆盖限制**：IC 自 2015-04-16、IM 自 2022-07-22、IF 自 2010-04-16 起有数据，
且 `futures_daily` **断更于 2026-04-29**（Batch 12 已登记）。缺 carry 的日期
`run_strategy` 按 0 处理，即那些天期货腿退化为纯现货口径 —— 不是 bug，但比较时要
记住 2014-01~2015-04 的期货腿实际没有贴水收益。

## 纪律

探索性读数，**不支持部署变更**。本模块扫 5×3=15 个通道配对，选优偏差同样适用；
标的维度已有 ①a 的"不许摘取"判例。要动部署须走
`backtest/selection_permutation.py`（⓪ 机器）。

## 用法

    python3 -m backtest.dual_channel_probe                 # 15 配对扫描
    python3 -m backtest.dual_channel_probe --w-spot 0.5 --w-fut 0.5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import (  # noqa: E402
    _connect, annualized_basis, load_db_config, pick_main_contract,
)
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import (  # noqa: E402
    ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
)
from backtest.positions import production_position  # noqa: E402
from backtest.staged_entry_probe import load_two_columns  # noqa: E402
from backtest.underlying_probe import load_index_returns  # noqa: E402

# 现货腿候选：任何指数都行（ETF 可执行），零 carry
SPOT_LEGS = {
    "2000": ("932000.CSI", "中证2000"),
    "1000": ("000852.SH", "中证1000"),
    "500": ("000905.SH", "中证500"),
    "300": ("000300.SH", "沪深300"),
    "div": ("000922.CSI", "中证红利"),
}
# 期货腿候选：必须有股指期货
FUT_LEGS = {
    "500": ("000905.SH", "IC", "中证500"),
    "1000": ("000852.SH", "IM", "中证1000"),
    "300": ("000300.SH", "IF", "沪深300"),
}
WINDOWS = {"full": (None, None), "train": ("2014-01-01", "2020-12-31"),
           "val": ("2021-01-01", "2023-12-31"), "holdout": ("2024-01-01", "2026-12-31")}
OUT = ROOT / "backtest" / "output" / "dual_channel_probe.csv"


# 单对信号列 ← config_4pairs.csv 的 group 顺序（1 沪深300 / 2 中证500 /
# 3 中证1000 / 4 中证2000），见 signals/equal_weight/config_4pairs.csv
PAIR_COLS = {"300": "pair_01_factor_20", "500": "pair_02_factor_20",
             "1000": "pair_03_factor_20", "2000": "pair_04_factor_20"}


def matched_signal(pair_key: str, smoothing: int = 5) -> pd.Series:
    """单对「成长 vs 价值」信号，按**部署口径**做 5 日平滑后返回。

    部署口径 = 四对 tanh(z) 等权平均 → `rolling(5, min_periods=1).mean()`。
    单对信号要可比，就得走同一道平滑（少了它是拿未平滑比平滑，口径不同）。
    """
    from backtest.staged_entry_probe import SIGNAL_FILE, smooth_series

    df = pd.read_csv(ROOT / SIGNAL_FILE, parse_dates=["date"]).set_index(
        "date").sort_index()
    col = PAIR_COLS[pair_key]
    if col not in df.columns:
        raise KeyError(f"信号文件缺列 {col!r}（实际：{list(df.columns)}）")
    return smooth_series(df[col], smoothing)


def load_carry_for(index_code: str, fut_prefix: str) -> pd.Series:
    """任意 (指数, 期货品种) 的年化基差序列（正=贴水）。

    `data.load_carry` 绑死 `_FUT` 的 500/1000 两个口径，这里零侵入地推广到 IF，
    计算逐字复用 `pick_main_contract`（按持仓量选主力）+ `annualized_basis`。
    """
    db = load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT trade_date, close FROM {db['schema']}.index_daily "
                "WHERE index_code=%s ORDER BY trade_date", (index_code,))
            spot = pd.Series({pd.Timestamp(d): float(c) for d, c in cur.fetchall()})
            cur.execute(
                "SELECT trade_date, symbol, close, oi FROM public.futures_daily "
                "WHERE symbol LIKE %s", (fut_prefix + "%",))
            rows = cur.fetchall()
    finally:
        conn.close()
    fdf = pd.DataFrame(rows, columns=["trade_date", "symbol", "close", "oi"]).dropna(
        subset=["oi", "close"])
    out = {}
    for td, g in fdf.groupby("trade_date"):
        ts = pd.Timestamp(td)
        if ts not in spot.index:
            continue
        sym = pick_main_contract(g)
        fut = float(g.loc[g["symbol"] == sym, "close"].iloc[0])
        out[ts] = annualized_basis(fut, float(spot.loc[ts]), td, sym)
    return pd.Series(out).sort_index()


def _cut(s: pd.Series, a, b) -> pd.Series:
    if a:
        s = s[s.index >= pd.Timestamp(a)]
    if b:
        s = s[s.index <= pd.Timestamp(b)]
    return s


def combine(pos: pd.Series, spot_und: pd.Series, fut_und: pd.Series,
            fut_carry: pd.Series, w_spot: float, w_fut: float,
            cost_bps: float) -> tuple[pd.Series, pd.Series]:
    """(组合收益, 组合有效仓位) —— 两腿分别跑引擎再按名义权重加权。"""
    idx = pos.index.intersection(spot_und.index).intersection(fut_und.index)
    p = pos.reindex(idx)
    spot = run_strategy(p, spot_und.reindex(idx), cost_bps, None)
    fut = run_strategy(p, fut_und.reindex(idx), cost_bps,
                       fut_carry.reindex(idx) if fut_carry is not None else None)
    ret = w_spot * spot["ret"] + w_fut * fut["ret"]
    pos_eff = w_spot * spot["pos_eff"] + w_fut * fut["pos_eff"]
    return ret, pos_eff


def _row(ret: pd.Series, pos_eff: pd.Series) -> dict:
    return {"ann": ann_return(ret), "sharpe": sharpe(ret),
            "maxdd": max_drawdown(ret), "calmar": calmar(ret),
            "turnover": turnover(pos_eff), "hit": hit_rate(ret),
            "n_obs": int(len(ret))}


def run(w_spot: float, w_fut: float, cost_bps: float) -> pd.DataFrame:
    _, smooth = load_two_columns()
    pos = production_position(smooth).astype(float)

    spot_rets = {k: load_index_returns(c) for k, (c, _) in SPOT_LEGS.items()}
    fut_rets, fut_carries = {}, {}
    for k, (code, pref, _) in FUT_LEGS.items():
        fut_rets[k] = load_index_returns(code)
        fut_carries[k] = load_carry_for(code, pref)

    rows = []
    for sk in SPOT_LEGS:
        for fk in FUT_LEGS:
            for win, (a, b) in WINDOWS.items():
                p = _cut(pos, a, b)
                su, fu = _cut(spot_rets[sk], a, b), _cut(fut_rets[fk], a, b)
                fc = _cut(fut_carries[fk], a, b)
                if len(p.index.intersection(su.index).intersection(fu.index)) < 60:
                    continue
                ret, pe = combine(p, su, fu, fc, w_spot, w_fut, cost_bps)
                rows.append({"spot": f"{sk}｜{SPOT_LEGS[sk][1]}",
                             "fut": f"{fk}｜{FUT_LEGS[fk][2]}",
                             "pair": f"现货{sk}+期货{fk}", "window": win, **_row(ret, pe)})
    return pd.DataFrame(rows)


def incumbent_reference(cost_bps: float) -> pd.DataFrame:
    """现役 blend（500+1000 各半、含 carry）在同一窗口划分下的读数，作对照。"""
    from backtest.data import load_carry, load_underlying_returns

    _, smooth = load_two_columns()
    pos = production_position(smooth).astype(float)
    und, car = load_underlying_returns("blend"), load_carry("blend")
    rows = []
    for win, (a, b) in WINDOWS.items():
        p, u, c = _cut(pos, a, b), _cut(und, a, b), _cut(car, a, b)
        idx = p.index.intersection(u.index)
        r = run_strategy(p.reindex(idx), u.reindex(idx), cost_bps, c.reindex(idx))
        rows.append({"pair": "现役 blend(含carry)", "window": win,
                     **_row(r["ret"], r["pos_eff"])})
    return pd.DataFrame(rows)


def emit_positions(spot_key: str, fut_key: str, w_spot: float, w_fut: float,
                   out_path: Path) -> pd.DataFrame:
    """导出候选方案的两腿仓位序列（下单用）。

    ⚠️ 落在 `backtest/output/` 而**不是** `output/recommended/` —— 后者是部署产物
    目录，进那里必须先过预登记闸门（⓪ 机器 + 判据 + 双审）。本文件是**候选**，
    不是推荐持仓。

    两列仓位的**时序完全相同**（都由同一套 equal_weight 信号驱动），差的只是名义
    权重与执行标的。所谓"两套信号"目前是一套信号 × 两个通道；真正的两套信号
    （信号-标的匹配）尚未测。
    """
    _, smooth = load_two_columns()
    sig = production_position(smooth).astype(int)
    df = pd.DataFrame({
        "signal": sig,
        f"spot_{spot_key}_notional": sig * w_spot,
        f"fut_{fut_key}_notional": sig * w_fut,
        "total_notional": sig * (w_spot + w_fut),
    })
    df.index.name = "date"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.round(4).to_csv(out_path)
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="双通道执行探针（现货+期货，探索性）")
    ap.add_argument("--emit", nargs=2, metavar=("SPOT", "FUT"), default=None,
                    help="导出该配对的两腿仓位序列，例如 --emit 2000 500")
    ap.add_argument("--w-spot", type=float, default=0.4, help="现货腿名义权重")
    ap.add_argument("--w-fut", type=float, default=0.6, help="期货腿名义权重")
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()

    print(f"名义权重：现货 {args.w_spot} + 期货 {args.w_fut} = "
          f"{args.w_spot + args.w_fut}（名义本金，非保证金）")
    rep = run(args.w_spot, args.w_fut, args.cost_bps)
    inc = incumbent_reference(args.cost_bps)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    pd.concat([rep, inc]).to_csv(args.output, index=False)

    both = pd.concat([rep, inc])
    p = both.pivot_table(index="pair", columns="window", values="sharpe")
    a = both.pivot_table(index="pair", columns="window", values="ann") * 100
    m = both.pivot_table(index="pair", columns="window", values="maxdd") * 100
    view = pd.DataFrame({"Sharpe": p["full"], "年化%": a["full"], "MaxDD%": m["full"],
                         "train": p["train"], "val": p["val"],
                         "holdout": p["holdout"]}).round(4)
    view["worst_tv"] = view[["train", "val"]].min(axis=1)
    print("\n按 full Sharpe 排序：")
    print(view.sort_values("Sharpe", ascending=False).to_string())
    print("\n按 worst(train,val) 排序（前 8）：")
    print(view.sort_values("worst_tv", ascending=False).head(8).to_string())
    print(f"\n→ {args.output}")

    if args.emit:
        sk, fk = args.emit
        if sk not in SPOT_LEGS or fk not in FUT_LEGS:
            print(f"ERROR: --emit 的标的必须来自 {list(SPOT_LEGS)} / {list(FUT_LEGS)}")
            return 2
        path = ROOT / "backtest" / "output" / \
            f"dual_channel_candidate_spot{sk}_fut{fk}.csv"
        df = emit_positions(sk, fk, args.w_spot, args.w_fut, path)
        last = df.iloc[-1]
        print(f"\n候选仓位序列（**非部署产物**）→ {path}")
        print(f"  {len(df)} 行，{df.index[0]:%Y-%m-%d} .. {df.index[-1]:%Y-%m-%d}")
        print(f"  末行 {df.index[-1]:%Y-%m-%d}：signal={int(last['signal'])}  "
              f"现货{sk} 名义={last[f'spot_{sk}_notional']:.2f}  "
              f"期货{fk} 名义={last[f'fut_{fk}_notional']:.2f}  "
              f"合计={last['total_notional']:.2f}")
        print(f"  持仓日占比 {float((df['signal'] > 0).mean()):.1%}；"
              f"两列时序相同（同一套信号 × 两个通道权重）")

    print("⚠️ 探索性：15 配对扫描，选优偏差适用；动部署前须走 ⓪ 置换选优机器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
