"""两笔分批建仓探针（探索性，**非预登记正式跑**）。

## 这是什么

用户 2026-08-17 提出：equal_weight 的未平滑值 `factor_value_raw`（raw）领先 5 日
平滑值 `factor_value`（smooth）约 3 个交易日，能不能用这个时滞做分批进出——
raw 出信号建第一笔、smooth 出信号建第二笔；raw 反向先平第二笔、smooth 反向平第一笔；
两笔权重 0.4 / 0.6。规则实现见 `backtest.positions.staged_position`（含三个自由度登记）。

## 纪律声明（重要）

本模块跑出来的数字是**探索性读数，不能支持部署变更**：

  * 权重 0.4/0.6、两笔、阈值 0 都是**拍板给定**的，没有预登记；
  * 一旦开始扫这些参数找最优，就落入选优偏差，必须走
    `backtest/selection_permutation.py`（⓪ 机器，Batch 8 已付清）出 `p_selected`，
    否则 naive p 会膨胀数倍（②④⑦ 三次实测膨胀 1.9~4.6×）；
  * 对照口径固定为现役 long-flat（`production_position(factor_value)`），
    同 engine、同 cost_bps、同窗口、同 carry —— 靠 `baseline.build_report(positions=...)`
    这个既有入口保证同秤，本模块不自建评价逻辑。

## 用法

    python3 -m backtest.staged_entry_probe                    # 默认 0.4/0.6
    python3 -m backtest.staged_entry_probe --w1 0.5 --w2 0.5
    python3 -m backtest.staged_entry_probe --bootstrap 500    # 慢，默认 0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import build_report  # noqa: E402
from backtest.positions import (  # noqa: E402
    crossings, production_position, staged_position,
)

SIGNAL_FILE = "output/equal_weight/equal_weight_signal_20d40z.csv"
RAW_COL = "factor_value_raw"
SMOOTH_COL = "factor_value"
OUT_DIR = ROOT / "backtest" / "output"


def load_two_columns(path: str = SIGNAL_FILE) -> tuple[pd.Series, pd.Series]:
    """(raw, smooth) —— 同一份 committed 信号文件的两列，索引天然逐位一致。"""
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    for col in (RAW_COL, SMOOTH_COL):
        if col not in df.columns:
            raise KeyError(f"{path} 缺列 {col!r}（实际：{list(df.columns)}）")
    return df[RAW_COL], df[SMOOTH_COL]


def diagnostics(raw: pd.Series, smooth: pd.Series, w1: float, w2: float,
                threshold: float = 0.0) -> dict:
    """暴露规则的结构性性质：事件次数、仓位状态分布、卡死天数、同日冲突。"""
    raw_up, raw_down = crossings(raw, threshold)
    sm_up, sm_down = crossings(smooth, threshold)
    pos = staged_position(raw, smooth, w1, w2, threshold)

    # 卡死 = 该笔的开仓条件在水平上成立，但因为没有穿越事件而空着
    leg1_on = pos.isin([w1, w1 + w2])
    leg2_on = pos.isin([w2, w1 + w2])
    stuck1 = int(((raw > threshold) & ~leg1_on).sum())
    stuck2 = int(((smooth > threshold) & ~leg2_on).sum())

    counts = pos.value_counts().sort_index()
    n = len(pos)
    return {
        "n_obs": n,
        "leg1_open_events": int(raw_up.sum()), "leg1_close_events": int(sm_down.sum()),
        "leg2_open_events": int(sm_up.sum()), "leg2_close_events": int(raw_down.sum()),
        "same_day_conflict_leg1": int((raw_up & sm_down).sum()),
        "same_day_conflict_leg2": int((sm_up & raw_down).sum()),
        "days_flat": int((pos == 0).sum()),
        "days_leg1_only": int((pos == w1).sum()),
        "days_leg2_only": int((pos == w2).sum()),
        "days_both": int((pos == w1 + w2).sum()),
        "mean_position": float(pos.mean()),
        "stuck_leg1_days": stuck1, "stuck_leg2_days": stuck2,
        "position_state_share": {float(k): round(v / n, 4) for k, v in counts.items()},
    }


def build_candidates(raw: pd.Series, smooth: pd.Series, w1: float, w2: float,
                     threshold: float = 0.0) -> dict[str, pd.Series]:
    """三条同秤对比的仓位序列。

    `raw_longflat` 是关键对照：如果分批的好处其实只是"用了更快的信号"，那纯 raw
    驱动的 long-flat 就该同样好——不放这一条，分批与提速两件事分不开。
    """
    return {
        "incumbent_longflat": production_position(smooth).astype(float),
        "raw_longflat": production_position(raw).astype(float),
        "staged_two_leg": staged_position(raw, smooth, w1, w2, threshold),
        "staged_refill": staged_position(raw, smooth, w1, w2, threshold,
                                         leg2_refill=True),
    }


SLOW_WINDOW = 5          # 慢腿 = 现役生产口径的平滑窗
FAST_WINDOWS = (1, 2, 3, 4)   # 1 = raw 本身（已知劣），2~4 = 方向 A 要看的


def smooth_series(raw: pd.Series, window: int) -> pd.Series:
    """与生产口径逐字一致的平滑：`rolling(window, min_periods=1).mean()`。

    `min_periods=1` 是关键——生产脚本就是这么写的（`signals/equal_weight/
    generate_signal.py:230`），所以开头不产生 NaN，而是用可得行数先平均。
    window ≤ 1 即不平滑。
    """
    if window <= 1:
        return raw.copy()
    return raw.rolling(window, min_periods=1).mean()


def build_fast_sweep(raw: pd.Series, committed_smooth: pd.Series,
                     w1: float, w2: float, threshold: float = 0.0,
                     fast_windows: tuple[int, ...] = FAST_WINDOWS,
                     slow: int = SLOW_WINDOW) -> dict[str, pd.Series]:
    """方向 A：快腿改用「更短但非零」的平滑，慢腿固定现役的 5 日。

    每个快腿窗 N 出**两条**候选，缺一不可：
      * `lf_smN`        —— 纯 long-flat，只用 sm{N} 驱动，不分批
      * `staged_smN_5`  —— 分批，快腿 sm{N} + 慢腿 sm5

    没有前者就分不清「读数变好是因为分批结构」还是「因为 sm{N} 这个信号本身就更好」。
    真要证明分批有独立价值，必须 `staged_smN_5` 同时打赢 `lf_smN` 和现役 `lf_sm5`。
    """
    # 口径自证：自算 sm5 应当复现 committed 的 factor_value。容差 2e-4 而非 0——
    # committed CSV 存的是 round(4)，生产是 round(平滑(未舍入 raw))，这里是
    # 平滑(round(raw,4))，两者必然差 ≤1e-4 量级（实测 max|Δ|=8e-5）。
    # 注意慢腿在下面用的是 **committed_smooth 本身**（零偏差），所以与现役同秤
    # 不受这个舍入影响；这条断言只验证 smooth_series 的实现没写错。
    own_slow = smooth_series(raw, slow)
    delta = (own_slow - committed_smooth).abs().max()
    if delta > 2e-4:
        raise AssertionError(
            f"自算 sm{slow} 与 committed factor_value 差 {delta:.3e} > 2e-4——"
            f"超出 round(4) 能解释的范围，平滑口径对不上")

    cands = {"lf_sm5_incumbent": production_position(committed_smooth).astype(float)}
    for n in fast_windows:
        fast = smooth_series(raw, n)
        cands[f"lf_sm{n}"] = production_position(fast).astype(float)
        cands[f"staged_sm{n}_5"] = staged_position(fast, committed_smooth,
                                                  w1, w2, threshold)
    return cands


THETAS = (0.1, 0.2, 0.3)       # 方向 B 的快腿阈值档
B_FAST_WINDOWS = (1, 2)        # 快腿窗：1=用户原始 raw；2=方向 A 的 worst_tv 赢家


def build_threshold_sweep(raw: pd.Series, committed_smooth: pd.Series,
                          w1: float, w2: float,
                          thetas: tuple[float, ...] = THETAS,
                          fast_windows: tuple[int, ...] = B_FAST_WINDOWS,
                          ) -> dict[str, pd.Series]:
    """方向 B：抬高快腿阈值以过滤噪声进场。两种口径各一族（见 staged_position）。

      * `B1_smN_tX`：`open=close=θ` —— 字面版，进场更严 + 减仓更早同时发生
      * `B2_smN_tX`：`open=θ, close=0` —— 纯粹版，只过滤进场噪声

    带上 θ=0 的基线（`base_smN`）与现役，才能看出抬阈值到底动了什么。
    """
    cands = {"lf_sm5_incumbent": production_position(committed_smooth).astype(float)}
    for n in fast_windows:
        fast = smooth_series(raw, n)
        cands[f"base_sm{n}"] = staged_position(fast, committed_smooth, w1, w2)
        for th in thetas:
            tag = f"{th:g}".replace("0.", "")
            cands[f"B1_sm{n}_t{tag}"] = staged_position(
                fast, committed_smooth, w1, w2,
                open_threshold=th, close_threshold=th)
            cands[f"B2_sm{n}_t{tag}"] = staged_position(
                fast, committed_smooth, w1, w2,
                open_threshold=th, close_threshold=0.0)
    return cands


def main() -> int:
    ap = argparse.ArgumentParser(description="两笔分批建仓探针（探索性）")
    ap.add_argument("--mode", default="single",
                    choices=["single", "sweep-fast", "sweep-threshold"],
                    help="single = 用户原始规则；sweep-fast = 方向 A（快腿平滑窗）；"
                         "sweep-threshold = 方向 B（快腿阈值）")
    ap.add_argument("--w1", type=float, default=0.4, help="笔1 权重（raw 开 / smooth 平）")
    ap.add_argument("--w2", type=float, default=0.6, help="笔2 权重（smooth 开 / raw 平）")
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--bootstrap", type=int, default=0, help="0 = 不跑 bootstrap（快）")
    ap.add_argument("--output", default=str(OUT_DIR / "staged_entry_probe.csv"))
    args = ap.parse_args()

    raw, smooth = load_two_columns()
    print(f"信号源 {SIGNAL_FILE}：{len(raw)} 行，"
          f"{raw.index[0]:%Y-%m-%d} .. {raw.index[-1]:%Y-%m-%d}")

    if args.mode == "sweep-threshold":
        cands = build_threshold_sweep(raw, smooth, args.w1, args.w2)
        print(f"\n方向 B：快腿阈值 {list(THETAS)} × 快腿窗 {list(B_FAST_WINDOWS)} "
              f"× 两种口径（B1 open=close=θ / B2 open=θ,close=0）"
              f"，w1={args.w1}/w2={args.w2}，慢腿阈值固定 0")
        print(f"  候选 {len(cands)} 条（含 θ=0 基线与现役）")
    elif args.mode == "sweep-fast":
        cands = build_fast_sweep(raw, smooth, args.w1, args.w2, args.threshold)
        print(f"\n方向 A：快腿平滑窗 {list(FAST_WINDOWS)} × 慢腿 {SLOW_WINDOW} "
              f"（w1={args.w1} / w2={args.w2}）；自算 sm5 与 committed 逐位相等已断言")
        print("  每个 N 两条：lf_smN = 纯 long-flat 只用 smN；staged_smN_5 = 分批")
        for n in FAST_WINDOWS:
            d = diagnostics(smooth_series(raw, n), smooth, args.w1, args.w2,
                            args.threshold)
            print(f"  sm{n}: 卡死笔2 {d['stuck_leg2_days']:4d} 天  "
                  f"均仓 {d['mean_position']:.4f}  "
                  f"笔2 开/平 {d['leg2_open_events']}/{d['leg2_close_events']}")
    else:
        diag = diagnostics(raw, smooth, args.w1, args.w2, args.threshold)
        print(f"\n── 规则诊断（w1={args.w1} / w2={args.w2} / θ={args.threshold}）──")
        for k, v in diag.items():
            print(f"  {k:26s} {v}")
        cands = build_candidates(raw, smooth, args.w1, args.w2, args.threshold)
    print(f"\n── 同秤评估（cost_bps={args.cost_bps}, bootstrap={args.bootstrap}）──")
    rep = build_report(bootstrap_n=args.bootstrap, cost_bps=args.cost_bps,
                       positions=cands)
    rep = rep[rep["signal"] != "buy_hold"]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out, index=False)

    # 部署口径视图：blend × long 段（现役 1.6163 / −16.67% 就在这一格）
    view = rep[(rep["kou_jing"] == "blend") & (rep["seg"] == "long")].copy()
    for c in ["ann", "maxdd", "hit"]:
        view[c] = (view[c] * 100).round(2)
    for c in ["sharpe", "calmar", "turnover"]:
        view[c] = view[c].round(4)
    cols = ["signal", "window", "ann", "sharpe", "maxdd", "calmar", "turnover", "hit", "n_obs"]
    print(view[cols].to_string(index=False))
    print(f"\n→ 全表（三口径 × 四窗 × 三段）{out}")
    print("⚠️ 探索性读数：参数未预登记，不支持部署变更；扫参数前先接 ⓪ 置换选优机器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
