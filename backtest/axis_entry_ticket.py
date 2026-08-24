"""新轮动轴入场券判定：四轴信号对宽基 blend 的偏 IC（控现役 ew）三件套。

判读规则冻结：docs/plans/2026-08-24-new-rotation-axes-entry-ticket.md §2
（先于运行提交）。机器复用 rotation_probe（非重叠 IC / 双侧循环移位置换 /
偏 rank IC）与 decompose._signal（生产 20d40z+rolling5，无网格）。

两锚先于判读：① partial_ic(ew, blend, 控 ew) 须精确回 0（残差退化路径）；
② rotation 负控走同一判定，预期不过闸。任一锚破 → verdict 标 ANCHOR_FAIL，
轴行不得作数。

CLI: python3 -m backtest.axis_entry_ticket --legs-csv <axis_legs_daily.csv>
     --output-dir <dir> [--n-perm 2000]
产出: axis_ticket_panel.csv + axis_ticket_verdict.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.rotation_probe import (  # noqa: E402
    _nonoverlap_frame,
    nonoverlap_ic,
    partial_ic_with_pvalue,
    partial_rank_ic,
    shift_permutation_pvalue,
)

EVAL_START = "2015-08-15"          # 跳过信号 warmup（冻结）
HALF_SPLIT = "2020-12-31"          # 参考半窗分界（冻结）
PRIMARY_K, REF_KS = 20, (5, 10)
PRIMARY_TARGET, REF_TARGETS = "blend", ("500", "1000")
ALPHA = 0.05
FAMILY_N = 5                       # 方向一全族 5 轴（批次二 §2 登记；参考列不改判）
EW_SIGNAL_FILE = ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"

PASS_WORDING = "过闸：进入正式预登记队列（非采用批准）"
FAIL_WORDING = "未过闸：当前功效下不可辨认（不得写『无增量信息』）"


# ---------------------------------------------------------------- 纯函数
def band_signals(legs: pd.DataFrame, axis: str) -> pd.DataFrame:
    """腿对长表 → 该轴各带信号宽表（date × band）。"""
    from signals.style_basket.decompose import _signal
    out = {}
    for band, g in legs[legs["axis"] == axis].groupby("band"):
        g = g.sort_values("date").set_index("date")
        nav_long = (1.0 + g["long_ret"]).cumprod()
        nav_short = (1.0 + g["short_ret"]).cumprod()
        out[band] = _signal(nav_long, nav_short)
    return pd.DataFrame(out).sort_index()


def axis_signal(bands: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """当日可用带等权平均 →（轴信号, 当日带数序列）。"""
    return bands.mean(axis=1), bands.notna().sum(axis=1)


def judge_axis(pic: float, pvalue: float, alpha: float = ALPHA,
               family_n: int = FAMILY_N) -> dict:
    """冻结判定措辞（母设计 §2；Bonferroni 参考列按全族 family_n，不改判）。"""
    ok = bool(np.isfinite(pic) and np.isfinite(pvalue) and pvalue < alpha)
    return {
        "partial_ic": float(pic) if np.isfinite(pic) else None,
        "partial_ic_pvalue": float(pvalue) if np.isfinite(pvalue) else None,
        "sign": (None if not np.isfinite(pic) or pic == 0
                 else ("+" if pic > 0 else "-")),
        "pass": ok,
        "bonferroni_family_ref": bool(ok and pvalue < alpha / family_n),
        "family_n": family_n,
        "wording": PASS_WORDING if ok else FAIL_WORDING,
    }


def _win(s: pd.Series, a: str | None, b: str | None) -> pd.Series:
    out = s
    if a:
        out = out.loc[out.index >= pd.Timestamp(a)]
    if b:
        out = out.loc[out.index <= pd.Timestamp(b)]
    return out


def partial_ic_point(sig: pd.Series, ret: pd.Series, control: pd.Series,
                     k: int) -> tuple[float, int]:
    """偏 rank IC 点估计（无置换；参考行/半窗用）。"""
    idx = sig.index.intersection(ret.index).intersection(control.index)
    frame = _nonoverlap_frame(sig.reindex(idx), ret.reindex(idx), k)
    return partial_rank_ic(frame["sig"], frame["fwd"], control.reindex(frame.index)), len(frame)


# ---------------------------------------------------------------- 编排
def run(legs: pd.DataFrame, n_perm: int = 2000, db=None) -> tuple[pd.DataFrame, dict]:
    from backtest.data import load_underlying_returns
    from backtest.rotation_target_probe import build_signals
    from signals.common.config import load_db_config

    db = db or load_db_config()
    targets = {kj: load_underlying_returns(kj, db=db)
               for kj in (PRIMARY_TARGET,) + REF_TARGETS}
    ew = (pd.read_csv(EW_SIGNAL_FILE, parse_dates=["date"])
          .set_index("date").sort_index()["factor_value"])

    def clip(s: pd.Series) -> pd.Series:
        return _win(s.dropna(), EVAL_START, None)

    blend = targets[PRIMARY_TARGET]

    # ── 锚 ①：控自身 → 残差退化 → 偏 IC 精确 0
    anchor_self, _ = partial_ic_point(clip(ew), blend, ew, PRIMARY_K)
    # ── 锚 ②：rotation 负控（预期不过闸）
    rot = clip(build_signals("U2")["rotation"])
    rot_pic, rot_p = partial_ic_with_pvalue(rot, blend, ew, PRIMARY_K, n_perm=n_perm)
    anchors = {
        "self_control_zero": {"value": float(anchor_self),
                              "ok": bool(abs(anchor_self) < 1e-12)},
        "rotation_negative_control": {
            "partial_ic": float(rot_pic), "pvalue": float(rot_p),
            "ok": bool(not (np.isfinite(rot_pic) and rot_p < ALPHA))},
    }
    anchors_ok = anchors["self_control_zero"]["ok"] and \
        anchors["rotation_negative_control"]["ok"]

    axes = tuple(dict.fromkeys(legs["axis"]))    # 从腿对数据推导本批轴集
    rows, verdicts = [], {}
    for axis in axes:
        bands = band_signals(legs, axis)
        sig_full, n_bands = axis_signal(bands)
        sig = clip(sig_full)
        if not len(sig):
            verdicts[axis] = {**judge_axis(float("nan"), float("nan")),
                              "note": "信号为空"}
            continue

        pic, p_pic = partial_ic_with_pvalue(sig, blend, ew, PRIMARY_K, n_perm=n_perm)
        ic_raw, n_win = nonoverlap_ic(sig, blend, PRIMARY_K)
        p_raw = shift_permutation_pvalue(sig, blend, PRIMARY_K, n_perm=n_perm)
        rows.append({"axis": axis, "row": "primary", "target": PRIMARY_TARGET,
                     "k": PRIMARY_K, "window": "full", "partial_ic": pic,
                     "partial_p": p_pic, "ic": ic_raw, "ic_p": p_raw,
                     "n_windows": n_win})
        for k in REF_KS:
            pic_k, n_k = partial_ic_point(sig, blend, ew, k)
            rows.append({"axis": axis, "row": "ref_k", "target": PRIMARY_TARGET,
                         "k": k, "window": "full", "partial_ic": pic_k,
                         "n_windows": n_k})
        for tgt in REF_TARGETS:
            pic_t, n_t = partial_ic_point(sig, targets[tgt], ew, PRIMARY_K)
            rows.append({"axis": axis, "row": "ref_target", "target": tgt,
                         "k": PRIMARY_K, "window": "full", "partial_ic": pic_t,
                         "n_windows": n_t})
        for wname, (a, b) in {"h1": (None, HALF_SPLIT),
                              "h2": (HALF_SPLIT, None)}.items():
            pic_h, n_h = partial_ic_point(_win(sig, a, b), _win(blend, a, b),
                                          ew, PRIMARY_K)
            rows.append({"axis": axis, "row": "ref_half", "target": PRIMARY_TARGET,
                         "k": PRIMARY_K, "window": wname, "partial_ic": pic_h,
                         "n_windows": n_h})

        verdicts[axis] = {**judge_axis(pic, p_pic),
                          "n_windows": int(n_win),
                          "bands_min": int(n_bands.reindex(sig.index).min()),
                          "bands_median": float(n_bands.reindex(sig.index).median())}

    # ── 量级锚（参考）：ew 对 blend
    ew_ic, ew_n = nonoverlap_ic(clip(ew), blend, PRIMARY_K)
    rows.append({"axis": "ew_anchor", "row": "magnitude", "target": PRIMARY_TARGET,
                 "k": PRIMARY_K, "window": "full", "ic": ew_ic, "n_windows": ew_n})

    verdict = {
        "spec": "docs/plans/2026-08-24-new-rotation-axes-entry-ticket.md",
        "eval_start": EVAL_START, "primary_k": PRIMARY_K,
        "primary_target": PRIMARY_TARGET, "n_perm": n_perm, "alpha": ALPHA,
        "anchors": anchors, "anchors_ok": bool(anchors_ok),
        "axes": verdicts,
        "OVERALL": ("ANCHOR_FAIL" if not anchors_ok else
                    ("PASS:" + ",".join(a for a in axes
                                        if verdicts.get(a, {}).get("pass"))
                     if any(verdicts.get(a, {}).get("pass") for a in axes)
                     else "ALL_FAIL")),
    }
    return pd.DataFrame(rows), verdict


def main() -> int:
    ap = argparse.ArgumentParser(description="新轮动轴入场券判定（偏 IC 控现役）")
    ap.add_argument("--legs-csv", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--n-perm", type=int, default=2000)
    args = ap.parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    legs = pd.read_csv(args.legs_csv, parse_dates=["date"])
    panel, verdict = run(legs, n_perm=args.n_perm)
    panel.to_csv(out_dir / "axis_ticket_panel.csv", index=False)
    (out_dir / "axis_ticket_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1))

    print(json.dumps(verdict, ensure_ascii=False, indent=1))
    print("AXIS TICKET DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
