"""等权/市值加权相对强弱面探针单测：手算字面量 + 与旧机器一致性 + 合成冒烟。

规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md。本文件只用合成数据。
"""
import json
import signal
import sys
import time

import numpy as np
import pandas as pd
import pytest

from backtest import ew_cw_axis_probe as m
from backtest.rotation_probe import hold_position, nonoverlap_ic, partial_rank_ic
from signals.equal_weight.generate_signal import _compute_pair_signal


def _prices(n, seed=0, start="2005-01-03"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=idx)


def _full_panel(seed=11):
    """六条合成序列，工作日日历覆盖 [DATA_START, END]；等权 1000 自 EXPECTED_FIRST 起（同真实版式）。"""
    idx = pd.bdate_range(m.DATA_START, m.END)
    rng = np.random.default_rng(seed)

    def walk():
        return pd.Series(1000.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, len(idx))), index=idx)

    ew = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk()})
    ew.loc[ew.index < m.EXPECTED_FIRST["ew1000"], "1000"] = np.nan
    cw = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk()})
    return ew, cw


def _patch_days(monkeypatch, ew, cw):
    """合成日历的有效日数与冻结常量 EXPECTED_DAYS 不同：按给定 frame 实数替换（只供单测）。"""
    monkeypatch.setattr(m, "EXPECTED_DAYS", {f"{side}{p}": int(df[p].notna().sum())
                                             for side, df in (("ew", ew), ("cw", cw)) for p in m.PAIRS})


def _write_wind(path, frame, codes):
    """把 {配对键: 序列} 写成 Wind 导出版式（行 0~3 元数据，行 4 代码行，行 5 起数据，末尾空行 + 页脚）。"""
    keys = list(codes)
    pad = [None] * (len(keys) - 1)
    rows = [["开始日期", "2000-08-30", *pad], ["截止日期", "当前日期", *pad], ["收盘价", "close", *pad],
            ["日期", *keys], ["Date", *[codes[k] for k in keys]]]
    for d, vals in zip(frame.index, frame[keys].itertuples(index=False)):
        rows.append([d, *[None if np.isnan(v) else float(v) for v in vals]])
    rows += [[None] * (len(keys) + 1), ["数据来源：Wind", *[None] * len(keys)]]
    pd.DataFrame(rows).to_excel(path, header=False, index=False)


def _no_db(*_a, **_k):
    raise AssertionError("单测不得连库")


def test_read_wind_export_layout(tmp_path):
    rows = [["开始日期", "2000-08-30", None], ["截止日期", "当前日期", None], ["收盘价", "close", None],
            ["日期", "300等权", "500等权"], ["Date", "000984.CSI", "000982.SH"],
            [pd.Timestamp("2004-12-30"), 999.0, 999.0],
            [pd.Timestamp("2004-12-31"), 1000.0, 1000.0],
            [pd.Timestamp("2005-01-04"), 1001.5, None]]
    path = tmp_path / "x.xlsx"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)
    df = m.read_wind_export(path, {"300": "000984.CSI", "500": "000982.SH"})
    assert list(df.columns) == ["300", "500"]
    assert list(df.index) == [pd.Timestamp("2004-12-31"), pd.Timestamp("2005-01-04")]  # DATA_START 截掉 12-30
    assert df.loc["2005-01-04", "300"] == 1001.5 and np.isnan(df.loc["2005-01-04", "500"])
    with pytest.raises(ValueError):
        m.read_wind_export(path, {"1000": "932382.CSI"})


def test_read_wind_export_footer_and_bad_dates(tmp_path):
    head = [["开始日期", "2000-08-30", None], ["截止日期", "当前日期", None], ["收盘价", "close", None],
            ["日期", "300等权", "500等权"], ["Date", "000984.CSI", "000982.SH"],
            [pd.Timestamp("2004-12-31"), 1000.0, 1000.0],
            [pd.Timestamp("2005-01-04"), 1001.5, 1002.0]]
    codes = {"300": "000984.CSI", "500": "000982.SH"}
    ok_path = tmp_path / "footer.xlsx"                         # 空行 + 「数据来源：Wind」页脚 → 丢弃
    pd.DataFrame(head + [[None, None, None], ["数据来源：Wind", None, None]]).to_excel(
        ok_path, header=False, index=False)
    df = m.read_wind_export(ok_path, codes)
    assert list(df.index) == [pd.Timestamp("2004-12-31"), pd.Timestamp("2005-01-04")]
    assert df.loc["2005-01-04", "500"] == 1002.0
    for name, row in (("text_date.xlsx", ["合计", 2001.5, 2002.0]), ("blank_date.xlsx", [None, 3.0, None])):
        p = tmp_path / name                                    # 日期无法解析却带数值 → 拒绝
        pd.DataFrame(head + [row]).to_excel(p, header=False, index=False)
        with pytest.raises(ValueError, match="日期无法解析"):
            m.read_wind_export(p, codes)


def test_pair_signal_mask_first_available_position():
    ew, cw = _prices(40, 1), _prices(40, 2)
    lb, zw = 2, 3
    sig = m.pair_signal(ew, cw, lb, zw)
    ref = _compute_pair_signal(ew, cw, lookback=lb, z_window=zw)
    assert sig.iloc[:4].isna().all()          # lb + zw − 1 = 4
    assert sig.iloc[4:].notna().all()
    np.testing.assert_allclose(sig.iloc[4:].to_numpy(), ref.iloc[4:].to_numpy())


def test_family_f2_averages_available_pairs_only(monkeypatch):
    idx = pd.bdate_range("2014-01-01", periods=3)
    fake = {"300": pd.Series([0.2, 0.2, np.nan], idx), "500": pd.Series([np.nan, 0.4, np.nan], idx),
            "1000": pd.Series([np.nan, np.nan, np.nan], idx)}
    monkeypatch.setattr(m, "pair_signal", lambda e, c, lb, zw: fake[e.name])
    ew = pd.DataFrame({p: pd.Series(1.0, idx) for p in m.PAIRS})
    out = m.family_signal(ew, ew, "F2", 5, 60)
    assert out.iloc[0] == pytest.approx(0.2)
    assert out.iloc[1] == pytest.approx(0.3)
    assert np.isnan(out.iloc[2])


def test_forward_sums_window_internal():
    out = m.forward_sums(np.array([1.0, 2, 3, 4, 5, 6]), 2)
    np.testing.assert_array_equal(out[:4], [5.0, 7.0, 9.0, 11.0])
    assert np.isnan(out[4:]).all()


def test_block_points():
    np.testing.assert_array_equal(m.block_points(10, 3, 1), [1, 4])
    np.testing.assert_array_equal(m.block_points(10, 3, 0), [0, 3, 6])


def test_oa_ic_hand_computed():
    S = np.arange(1.0, 10.0)                                   # n = 9, k = 2
    fwd = np.array([10.0, 5, 20, 9, 30, 1, 40, np.nan, np.nan])
    # o=0 块点 {0,2,4,6}: S=[1,3,5,7] vs [10,20,30,40] → +1
    # o=1 块点 {1,3,5}:   S=[2,4,6]  vs [5,9,1] → 秩差 [−1,−1,2] → 1 − 6·6/(3·8) = −0.5
    ident = np.arange(9)[None, :]
    off = m.batch_offset_ics(S, fwd, 2, ident)
    np.testing.assert_allclose(off[0], [1.0, -0.5])
    assert m.batch_oa_ic(S, fwd, 2, ident)[0] == pytest.approx(0.25)


def test_rotation_index_roll_convention_and_identity():
    idx = m.rotation_index(7, 5, seed=1)
    assert idx.shape == (6, 7)
    np.testing.assert_array_equal(idx[0], np.arange(7))
    S = np.arange(7.0) * 10
    for row in idx[1:]:
        s = (-row[0]) % 7                                      # row[j] = (j − s) mod 7
        np.testing.assert_array_equal(S[row], np.roll(S, s))


def test_rotation_index_matches_seeded_shifts():
    idx = m.rotation_index(7, 5, seed=1)
    shifts = np.random.default_rng(1).integers(0, 7, size=5)   # 同种子同一条流 → 第 b 行 = np.roll(S, shifts[b])
    assert (shifts % 7 != 0).any()
    S = np.arange(7.0) * 10
    for b, s in enumerate(shifts):
        np.testing.assert_array_equal(S[idx[b + 1]], np.roll(S, s))


def test_partial_and_legacy_match_rotation_probe():
    rng = np.random.default_rng(3)
    n, k = 200, 5
    idx = pd.bdate_range("2014-01-01", periods=n)
    S, r, c = (pd.Series(rng.normal(size=n), idx) for _ in range(3))
    fwd = m.forward_sums(r.to_numpy(), k)
    ident = np.arange(n)[None, :]
    off = m.batch_offset_ics(S.to_numpy(), fwd, k, ident)
    assert off[0, k - 1] == pytest.approx(nonoverlap_ic(S, r, k)[0], abs=1e-12)   # 旧口径 = 起点 k−1
    fwd_s = pd.Series(fwd, idx)
    expect = np.mean([partial_rank_ic(S.iloc[m.block_points(n, k, o)], fwd_s.iloc[m.block_points(n, k, o)],
                                      c.iloc[m.block_points(n, k, o)]) for o in range(k)])
    got = m.batch_partial_oa_ic(S.to_numpy(), fwd, c.to_numpy(), k, ident)[0]
    assert got == pytest.approx(expect, abs=1e-12)


def test_partial_is_zero_when_control_equals_signal():
    S = np.array([3.0, 1, 4, 1.5, 5, 9, 2, 6, 5.5, 3.5, 7, 8])
    fwd = m.forward_sums(np.array([0.1, -0.2, 0.3, 0.05, -0.1, 0.2, 0.0, 0.15, -0.05, 0.1, 0.2, -0.3]), 2)
    got = m.batch_partial_oa_ic(S, fwd, S, 2, np.arange(12)[None, :])[0]
    assert got == pytest.approx(0.0, abs=1e-12)


def test_perm_p_two_and_one_sided():
    pool = np.array([0.5, 0.6, -0.7, 0.1, -0.2])               # 行 0 = 观测
    assert m.perm_p_two_sided(pool) == pytest.approx(3 / 5)
    assert m.perm_p_one_sided(pool, +1.0) == pytest.approx(2 / 5)
    assert m.perm_p_one_sided(-pool, -1.0) == pytest.approx(2 / 5)


def test_tranche_position_hand_computed():
    idx = pd.bdate_range("2014-01-01", periods=6)
    sig = pd.Series([0.5, 0.2, -0.1, 0.3, 0.0, -0.4], idx)    # 符号 [+,+,−,+,0,−]
    np.testing.assert_allclose(m.tranche_position(sig, 3, 1.0).to_numpy(), [0, 0, 1 / 3, 1 / 3, 0, 0])
    np.testing.assert_allclose(m.tranche_position(sig, 3, -1.0).to_numpy(), [0, 0, -1 / 3, -1 / 3, 0, 0])
    np.testing.assert_allclose(m.tranche_position(sig, 3, 1.0, long_only=True).to_numpy(),
                               [0, 0, 2 / 3, 2 / 3, 1 / 3, 1 / 3])
    sig2 = sig.copy()
    sig2.iloc[1] = np.nan                                      # 窗内缺信号 → 空仓
    np.testing.assert_allclose(m.tranche_position(sig2, 3, 1.0).to_numpy()[:4], [0, 0, 0, 0])


def test_offset_book_hand_computed():
    idx = pd.bdate_range("2013-12-25", periods=10)            # 位置 0~3 在 D 前，anchor = 位置 4
    sig = pd.Series([0.5, -0.2, -0.7, 0.1, 0.3, 0.4, -0.6, 0.0, -0.9, 0.8], idx)
    # k=3、o=1、方向 −1：换仓位置 (pos−4−1) ≡ 0 (mod 3) → {2, 5, 8}；D 首日沿用 D 前换仓日 pos 2（相对 o−k = −2）
    # pos2: −sign(−0.7)=+1；pos5: −sign(0.4)=−1；pos8: −sign(−0.9)=+1；pos0~1 无仓
    np.testing.assert_array_equal(m.offset_book(sig, 3, -1.0, 1, idx[4]).to_numpy(),
                                  [0, 0, 1, 1, 1, -1, -1, -1, 1, 1])
    # o=0、方向 +1：换仓 {1, 4, 7} → sign(−0.2)=−1、sign(0.3)=+1、sign(0.0)=0；D 段即旧口径 hold_position
    b0 = m.offset_book(sig, 3, 1.0, 0, idx[4])
    np.testing.assert_array_equal(b0.to_numpy(), [0, -1, -1, -1, 1, 1, 1, 0, 0, 0])
    np.testing.assert_array_equal(b0.iloc[4:].to_numpy(), hold_position(sig.iloc[4:], 3).to_numpy())
    sig2 = sig.copy()
    sig2.iloc[2] = np.nan                                      # 换仓日信号缺失且此前无仓 → 0
    np.testing.assert_array_equal(m.offset_book(sig2, 3, -1.0, 1, idx[4]).to_numpy(),
                                  [0, 0, 0, 0, 0, -1, -1, -1, 1, 1])


def test_offset_books_average_to_tranche():
    rng = np.random.default_rng(8)
    idx = pd.bdate_range("2013-06-03", periods=400)
    sig = pd.Series(rng.normal(size=400), idx)
    sig.iloc[:30] = np.nan                                     # 预热段
    sig.iloc[[150, 151, 260]] = 0.0                            # 零信号
    d = idx[200:]
    for k in (3, 5, 10, 20):
        for direction in (1.0, -1.0):
            books = [m.offset_book(sig, k, direction, o, d[0]).reindex(d).to_numpy() for o in range(k)]
            np.testing.assert_allclose(np.mean(books, axis=0),
                                       m.tranche_position(sig, k, direction).reindex(d).to_numpy(),
                                       rtol=0, atol=1e-12)


def test_pick_representative_rules():
    panel = pd.DataFrame([
        {"lb": 5, "zw": 60, "k": 5, "oa_ic": 0.30, "oa_ic_H1": 0.40, "oa_ic_H2": -0.05},     # 不同号 → 排除
        {"lb": 5, "zw": 250, "k": 10, "oa_ic": 0.10, "oa_ic_H1": 0.08, "oa_ic_H2": 0.12},    # worst 0.08
        {"lb": 20, "zw": 60, "k": 20, "oa_ic": -0.15, "oa_ic_H1": -0.09, "oa_ic_H2": -0.20},  # worst 0.09 ← 选中
        {"lb": 20, "zw": 250, "k": 5, "oa_ic": -0.12, "oa_ic_H1": -0.09, "oa_ic_H2": -0.10},  # worst 0.09，|全窗| 小
    ])
    best, ok = m.pick_representative(panel)
    assert ok and (best["lb"], best["zw"], best["k"]) == (20, 60, 20)
    best2, ok2 = m.pick_representative(panel.iloc[[0]])
    assert not ok2 and best2["oa_ic"] == 0.30
    tie = pd.DataFrame([
        {"lb": 20, "zw": 60, "k": 20, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
        {"lb": 20, "zw": 60, "k": 10, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
        {"lb": 5, "zw": 60, "k": 10, "oa_ic": 0.1, "oa_ic_H1": 0.1, "oa_ic_H2": 0.1},
    ])
    best3, _ = m.pick_representative(tie)
    assert (best3["lb"], best3["k"]) == (5, 10)


def test_pick_representative_worst_half_precedes_full_window():
    panel = pd.DataFrame([
        {"lb": 5, "zw": 250, "k": 10, "oa_ic": 0.20, "oa_ic_H1": 0.08, "oa_ic_H2": 0.30},    # |全窗| 最大但 worst 0.08
        {"lb": 20, "zw": 60, "k": 20, "oa_ic": -0.15, "oa_ic_H1": -0.09, "oa_ic_H2": -0.20},  # worst 0.09 ← 应选
    ])
    best, ok = m.pick_representative(panel)
    assert ok and (best["lb"], best["zw"], best["k"]) == (20, 60, 20)


def test_confirmation_window_excludes_2014_returns():
    # M10（C_END 常量）的靶子；编排层的 C 段隔离由 test_confirm_families_immune_to_2014_on 承担
    idx = pd.bdate_range("2013-12-20", "2014-01-10")
    blend = pd.Series(np.where(idx.year == 2014, 100.0, 0.001), idx)   # 2014 收益放大，混入即可见
    cdates = m.window(blend, "2013-12-20", m.C_END).index
    fwd = m.forward_sums(blend.reindex(cdates).to_numpy(), 3)
    assert np.nanmax(fwd) < 1.0
    assert np.isnan(fwd[-3:]).all()


def test_choose_alpha_star():
    rates = {"stationary": {0.05: 0.050, 0.04: 0.040}, "shared_volatility": {0.05: 0.0625, 0.04: 0.050},
             "regime": {0.05: 0.055, 0.04: 0.045}}
    rows = [{"kind": kind, "alpha": a, "rate": r.get(a, a)} for kind, r in rates.items() for a in m.ALPHA_GRID]
    tab = pd.DataFrame(rows)
    assert m.choose_alpha_star(tab) == 0.04
    assert m.choose_alpha_star(tab.assign(rate=0.2)) is None


def test_holm_pass():
    assert m.holm_pass({"F1": 0.012, "F2": 0.03}, 0.04) == {"F1": True, "F2": True}
    assert m.holm_pass({"F1": 0.025, "F2": 0.03}, 0.04) == {"F1": False, "F2": False}
    assert m.holm_pass({"F1": 0.012, "F2": 0.05}, 0.04) == {"F1": True, "F2": False}
    assert m.holm_pass({"F2": 0.04}, 0.04) == {"F2": True}


def test_choose_alpha_star_uses_max_not_mean():
    # 0.05 档：三场景均值 0.0467 ≤ 5% 但最大 8% > 5% → 不选；0.04 档起全 ≤ 5%
    rates = {"stationary": {0.05: 0.03}, "shared_volatility": {0.05: 0.03}, "regime": {0.05: 0.08}}
    rows = [{"kind": kind, "alpha": a, "rate": r.get(a, 0.01)} for kind, r in rates.items() for a in m.ALPHA_GRID]
    assert m.choose_alpha_star(pd.DataFrame(rows)) == 0.04


def test_main_gates_boundaries():
    assert m.main_gates(0.049, 0.1, 0.049, 1.0, True, 0.01) == {
        "gate1": True, "gate2": True, "gate3": True, "main_pass": True}
    assert not m.main_gates(0.05, 0.1, 0.01, 1.0, True, 0.5)["gate1"]         # OA-IC p = 0.05 不过（严格小于）
    assert not m.main_gates(0.01, 0.1, 0.05, 1.0, True, 0.5)["gate1"]         # 偏 IC 的 p = 0.05 不过
    assert not m.main_gates(0.01, -0.1, 0.01, 1.0, True, 0.5)["gate1"]        # 偏 IC 与方向反号不过
    assert m.main_gates(0.01, -0.1, 0.01, -1.0, True, 0.5)["gate1"]           # 方向 −1 时负偏 IC 为同号
    g2 = m.main_gates(0.01, 0.1, 0.01, 1.0, False, 0.5)
    assert g2["gate1"] and g2["gate3"] and not g2["gate2"] and not g2["main_pass"]
    g3 = m.main_gates(0.01, 0.1, 0.01, 1.0, True, 0.0)
    assert g3["gate1"] and not g3["gate3"] and not g3["main_pass"]           # Sharpe = 0 不过


def test_assemble_verdicts_labels():
    main = pd.DataFrame([{"family": "F1", "main_pass": False}, {"family": "F2", "main_pass": True}])
    go = m.assemble_verdicts(main, pd.DataFrame([{"family": "F2", "confirm_pass": True}]))
    assert list(go["verdict"]) == ["STOP", "GO_ENTRY"] and go["confirm_pass"].iloc[1] is True
    no = m.assemble_verdicts(main, pd.DataFrame([{"family": "F2", "confirm_pass": False}]))
    assert list(no["verdict"]) == ["STOP", "STOP_NOT_REPLICATED"]
    assert list(m.assemble_verdicts(main.assign(main_pass=False), None)["verdict"]) == ["STOP", "STOP"]


def test_permutation_rows_match_scipy_reference():
    from scipy.stats import spearmanr
    rng = np.random.default_rng(13)
    n, k = 120, 4
    S, ctrl = rng.normal(size=n), rng.normal(size=n)
    fwd = m.forward_sums(rng.normal(size=n), k)
    shifts = [0, 1, 7, 55, 119]
    idx = np.vstack([np.arange(n)] + [(np.arange(n) - s) % n for s in shifts])   # 行 b = np.roll(S, s_b)
    oa = m.batch_oa_ic(S, fwd, k, idx)
    pa = m.batch_partial_oa_ic(S, fwd, ctrl, k, idx)
    for row, s in enumerate([0] + shifts):
        Sr = np.roll(S, s)                                      # 只旋转信号，fwd 与 ctrl 不动
        pts = [m.block_points(n, k, o) for o in range(k)]
        exp_oa = np.mean([spearmanr(Sr[p], fwd[p]).correlation for p in pts])
        exp_pa = np.mean([partial_rank_ic(pd.Series(Sr[p]), pd.Series(fwd[p]), pd.Series(ctrl[p])) for p in pts])
        assert oa[row] == pytest.approx(exp_oa, abs=1e-12)
        assert pa[row] == pytest.approx(exp_pa, abs=1e-12)


def test_family_f2_real_pairs_join_after_1000_warmup():
    idx = pd.bdate_range("2012-06-01", "2015-06-30")
    rng = np.random.default_rng(21)

    def walk():
        return pd.Series(1000.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, len(idx))), index=idx)

    ew = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk()})
    ew.loc[ew.index < pd.Timestamp("2013-12-31"), "1000"] = np.nan              # 1000 等权自 2013-12-31 起
    cw = pd.DataFrame({"300": walk(), "500": walk(), "1000": walk()})
    for lb, zw in m.FORMS:
        ref = {}
        for p in m.PAIRS:                                       # 独立参照：生产函数 + 自身首个对齐日起的掩码
            j = pd.concat([ew[p], cw[p]], axis=1).dropna()
            s = _compute_pair_signal(j.iloc[:, 0], j.iloc[:, 1], lookback=lb, z_window=zw).astype(float)
            s.iloc[: lb + zw - 1] = np.nan
            ref[p] = s.reindex(idx)
        avail = ref["1000"].first_valid_index()
        assert avail == pd.concat([ew["1000"], cw["1000"]], axis=1).dropna().index[lb + zw - 1]
        f2 = m.family_signal(ew, cw, "F2", lb, zw).reindex(idx)
        before = idx[(idx >= ref["300"].first_valid_index()) & (idx < avail)]
        after = idx[idx >= avail]
        assert len(before) > 0 and len(after) > 0
        np.testing.assert_allclose(f2[before], ((ref["300"] + ref["500"]) / 2)[before], rtol=0, atol=1e-15)
        np.testing.assert_allclose(f2[after], ((ref["300"] + ref["500"] + ref["1000"]) / 3)[after],
                                   rtol=0, atol=1e-15)


def test_calibration_dataset_independent_recompute():
    from scipy.stats import spearmanr
    from backtest.research_statistics import generated_null
    lb, zw, k, n_c, n_perm = 5, 60, 5, 150, 39
    raw = m.calibrate_confirmation(lb, zw, k, n_c, n_cal=2, n_perm=n_perm)
    warm = lb + zw - 1
    for ki, kind in enumerate(("stationary", "shared_volatility", "regime")):
        for rep in range(2):
            seed = 20260930 + 1_000_000 + 10_000 * ki + rep       # 预登记 §3.6 种子规则（字面量）
            x, y = generated_null(kind, warm + n_c, seed)
            nav = pd.Series(np.cumprod(1.0 + 0.005 * x))
            sig = _compute_pair_signal(nav, pd.Series(np.ones(len(nav))), lookback=lb, z_window=zw).to_numpy()[warm:]
            fwd = pd.Series(y[warm:]).rolling(k).sum().shift(-k).to_numpy()

            def oa(s_arr):
                return np.mean([spearmanr(s_arr[np.arange(o, n_c - k, k)], fwd[np.arange(o, n_c - k, k)]).correlation
                                for o in range(k)])

            obs = oa(sig)
            null = np.array([oa(np.roll(sig, s))
                             for s in np.random.default_rng(seed + 100_000).integers(0, n_c, size=n_perm)])
            p = (1 + np.sum(null >= obs)) / (n_perm + 1)       # 单侧 +1 方向，含观测自身
            got = raw[(raw["kind"] == kind) & (raw["rep"] == rep)]["p_one_sided"].iloc[0]
            assert got == pytest.approx(p, abs=1e-12)


def test_data_checks_flag_stale_and_calendar(monkeypatch):
    ew, cw = _full_panel()
    _patch_days(monkeypatch, ew, cw)
    ok = m.data_checks(ew, cw)
    assert ok["gate1_calendar_ok"] and ok["gate2_stale_ok"]
    assert ok["first_valid"]["ew1000"] == "2013-12-31" and ok["last_valid"]["cw300"] == str(m.END.date())
    ew2, cw2 = ew.copy(), cw.copy()
    ew2.iloc[3, 0] = ew2.iloc[2, 0]                            # 300 等权一日未更新
    cw2.iloc[4, 1] = np.nan                                    # 500 市值加权区间内缺一日
    i = cw2.index.get_loc(pd.Timestamp("2010-06-01"))
    cw2.iloc[i, 2] = cw2.iloc[i - 1, 2]                        # 1000 市值加权 2010 年陈旧（等权 1000 尚未开始）
    bad = m.data_checks(ew2, cw2)
    assert bad["stale_days"]["ew300"] == 1 and bad["stale_days"]["cw1000"] == 1 and not bad["gate2_stale_ok"]
    assert bad["calendar_mismatch_days"]["500"] == 1 and bad["internal_nan_days"]["cw500"] == 1
    assert not bad["gate1_calendar_ok"]


def test_coverage_checks_first_last_and_internal_nan(monkeypatch):
    # 每个情形都把 EXPECTED_DAYS 换成该情形的实际日数，使只有被测的那一项能让闸 1 不过
    ew, cw = _full_panel()
    _patch_days(monkeypatch, ew, cw)
    assert m.coverage_checks(ew, cw)["gate1_calendar_ok"]
    ew3 = ew.copy()
    ew3.loc[ew3.index < pd.Timestamp("2005-01-05"), "300"] = np.nan   # 300 等权首日晚到 2005 年（共同区间日历仍一致）
    _patch_days(monkeypatch, ew3, cw)
    r3 = m.coverage_checks(ew3, cw)
    assert r3["calendar_mismatch_days"]["300"] == 0 and r3["internal_nan_days"]["ew300"] == 0
    assert r3["first_valid"]["ew300"] == "2005-01-05" and not r3["first_last_ok"]["ew300"]
    assert all(r3["valid_days_ok"].values()) and not r3["gate1_calendar_ok"]
    ew4, cw4 = ew.copy(), cw.copy()
    ew4.iloc[-1, 2] = np.nan
    cw4.iloc[-1, 2] = np.nan                                   # 1000 两边末日同缺（日历一致）→ 末日覆盖不过
    _patch_days(monkeypatch, ew4, cw4)
    r4 = m.coverage_checks(ew4, cw4)
    assert r4["calendar_mismatch_days"]["1000"] == 0 and not r4["first_last_ok"]["cw1000"]
    assert not r4["gate1_calendar_ok"]
    ew5, cw5 = ew.copy(), cw.copy()
    ew5.iloc[100, 1] = np.nan
    cw5.iloc[100, 1] = np.nan                                  # 500 两边同日缺（日历一致）→ 只有区间内缺失能抓到
    _patch_days(monkeypatch, ew5, cw5)
    r5 = m.coverage_checks(ew5, cw5)
    assert r5["calendar_mismatch_days"]["500"] == 0 and all(r5["first_last_ok"].values())
    assert r5["internal_nan_days"]["ew500"] == 1 and r5["internal_nan_days"]["cw500"] == 1
    assert not r5["gate1_calendar_ok"]


def test_coverage_checks_index_order_and_valid_days(monkeypatch):
    ew, cw = _full_panel()
    _patch_days(monkeypatch, ew, cw)
    days = dict(m.EXPECTED_DAYS)
    monkeypatch.setattr(m, "EXPECTED_DAYS", {**days, "cw300": days["cw300"] + 1})   # 有效日数差 1 日 → 不过
    r = m.coverage_checks(ew, cw)
    assert r["valid_days"]["cw300"] == days["cw300"] and not r["valid_days_ok"]["cw300"]
    assert not r["gate1_calendar_ok"]
    monkeypatch.setattr(m, "EXPECTED_DAYS", days)
    zero = {"300": 0, "500": 0, "1000": 0}
    order = list(range(len(cw)))
    order[100], order[101] = 101, 100
    sw = cw.iloc[order]                                         # 相邻两日对调：首末日、日数、日历集合都不变
    rs = m.coverage_checks(ew, sw)
    assert rs["calendar_mismatch_days"] == zero and all(rs["first_last_ok"].values())
    assert all(rs["valid_days_ok"].values()) and not any(rs["internal_nan_days"].values())
    assert not rs["index_unique_monotonic"]["cw"] and not rs["gate1_calendar_ok"]
    x, y = ew.index[200], ew.index[300]                         # 两边同样「x 重复、y 缺」：日数净变化 0

    def dup_drop(df):
        return pd.concat([df.drop(index=y), df.loc[[x]]]).sort_index()

    rd = m.coverage_checks(dup_drop(ew), dup_drop(cw))
    assert rd["calendar_mismatch_days"] == zero and all(rd["first_last_ok"].values())
    assert all(rd["valid_days_ok"].values()) and not any(rd["internal_nan_days"].values())
    assert not rd["index_unique_monotonic"]["ew"] and not rd["gate1_calendar_ok"]


def test_reconcile_with_db_flags_export_nan_and_diff(monkeypatch):
    import backtest.data as bd
    monkeypatch.setattr(bd, "_connect", _no_db)
    ew, cw = _full_panel()
    blend = m.blend_from_export(cw)
    monkeypatch.setattr(bd, "load_spot_close", lambda p, db=None: cw[p].dropna())
    monkeypatch.setattr(bd, "load_underlying_returns", lambda kj, db=None: blend.dropna())
    ok = m.reconcile_with_db(cw, blend)
    assert ok["gate3_reconcile_ok"] and ok["blend_D_nan_days"] == 0 and ok["blend_D_max_abs_diff"] == 0.0
    b2 = blend.copy()
    b2.loc[pd.Timestamp("2016-03-01")] = np.nan                # 导出侧 D 段一日 NaN（库里有）：max 会跳过它
    bad = m.reconcile_with_db(cw, b2)
    assert bad["blend_D_nan_days"] == 1 and bad["blend_D_max_abs_diff"] == 0.0 and not bad["gate3_reconcile_ok"]
    cw3 = cw.copy()
    cw3.iloc[500, 0] *= 1.00001                                 # 市值加权导出与库相对差 1e-5 > 1e-6
    assert not m.reconcile_with_db(cw3, blend)["gate3_reconcile_ok"]


def test_freeze_carry_round_trip(tmp_path):
    rng = np.random.default_rng(4)
    idx = pd.bdate_range("2015-04-16", periods=300)
    carry = pd.Series(rng.normal(0.05, 0.03, 300) / 7.0, index=idx)
    back = m.freeze_carry(carry, tmp_path / "carry.csv")
    assert back.index.equals(carry.index) and np.array_equal(back.to_numpy(), carry.to_numpy())
    assert (tmp_path / "carry.csv").read_text(encoding="utf-8").splitlines()[0] == "date,carry"


def test_net_returns_execution_timing():
    idx = pd.bdate_range("2014-01-01", periods=8)
    r = pd.Series([0.01, -0.02, 0.03, 0.015, -0.005, 0.02, 0.01, -0.01], idx)
    pos = pd.Series(0.0, idx)
    pos.iloc[2] = 1.0                                           # 位置 2（t）收盘决策的一日脉冲
    carry = pd.Series(0.049, idx)                               # 年化 4.9% → 持仓日 0.049/245
    got = m.net_returns(pos, r, carry).to_numpy()
    exp = np.zeros(8)
    exp[3] = r.iloc[3] - 3e-4 + 0.049 / 245                     # 收益、建仓成本、carry 都落在 t+1
    exp[4] = -3e-4                                              # t+2 平仓成本
    np.testing.assert_allclose(got, exp, rtol=0, atol=1e-15)
    lag = m.net_returns(m.lag1_position(pos), r, None).to_numpy()
    exp2 = np.zeros(8)
    exp2[4] = r.iloc[4] - 3e-4                                  # 再晚一日收盘成交：收益落在 t+2
    exp2[5] = -3e-4
    np.testing.assert_allclose(lag, exp2, rtol=0, atol=1e-15)
    full = pd.Series([0.0, 1.0, -1.0, 0.5, 0.25], index=pd.bdate_range("2013-12-27", periods=5))
    d_idx = full.index[2:]                                      # D 从位置 2 起：D 首日沿用 D 前一日（位置 1）的仓位
    np.testing.assert_array_equal(m.lag1_position(full, d_idx).to_numpy(), [1.0, -1.0, 0.5])
    st = m.net_stats(pos, r, carry)                             # net_stats / yearly_net 走同一条逐日收益
    assert st["net_ann"] == pytest.approx(got.mean() * 245, abs=1e-15)
    assert m.yearly_net(pos, r, carry) == pytest.approx({"2014": float(np.prod(1.0 + got) - 1.0)}, abs=1e-15)


def test_blend_from_export_propagates_nan():
    idx = pd.bdate_range("2014-01-01", periods=6)
    cw = pd.DataFrame({"300": 1.0, "500": [100.0, 101, 102, np.nan, 104, 105],
                       "1000": [50.0, 51, 52, 53, 54, 55]}, index=idx)
    b = m.blend_from_export(cw)
    assert np.isnan(b.iloc[3]) and np.isnan(b.iloc[4])        # 缺值不补齐：缺值日与其后一日都传成 NaN
    assert b.iloc[[1, 2, 5]].notna().all()
    assert b.iloc[5] == pytest.approx((105 / 104 - 1 + 55 / 54 - 1) / 2)


@pytest.fixture(scope="module")
def wind_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("wind")
    ew, cw = _full_panel()
    _write_wind(d / "ew.xlsx", ew, m.EW_CODES)
    _write_wind(d / "cw.xlsx", cw, m.CW_CODES)
    cal = cw.index
    return d / "ew.xlsx", d / "cw.xlsx", cal[(cal >= pd.Timestamp(m.D_WIN[0])) & (cal <= pd.Timestamp(m.D_WIN[1]))]


def test_preflight_layout_dates_missing_only(tmp_path, monkeypatch, capsys, wind_files):
    ew_x, cw_x, d_dates = wind_files
    _patch_days(monkeypatch, *_full_panel())                  # 与 wind_files 同一合成日历
    monkeypatch.setattr("backtest.data._connect", _no_db)     # 双保险：任何真实连库都炸
    pings = []
    monkeypatch.setattr(m, "_db_ping", lambda db=None: pings.append(db))
    ew_c, sl_c = tmp_path / "ew_sig.csv", tmp_path / "sl_sig.csv"
    for p in (ew_c, sl_c):
        pd.DataFrame({"date": d_dates, "factor_value": 0.1}).to_csv(p, index=False)
    for name, val in (("EW_XLSX", ew_x), ("CW_XLSX", cw_x), ("EW_SIGNAL_CSV", ew_c), ("SLOPE20_CSV", sl_c)):
        monkeypatch.setattr(m, name, val)
    monkeypatch.setattr(sys, "argv", ["ew_cw_axis_probe", "--preflight"])
    good = m.preflight(ew_x, cw_x, ew_c, sl_c)
    assert good["ok"] and good["coverage_ok"] and good["controls_ok"] and good["db_ok"] and pings == [None]
    assert good["controls"]["slope20"]["d_days"] == len(d_dates)
    assert good["controls"]["slope20"]["last_date"] == str(m.END.date())
    assert m.main() == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    pd.read_csv(sl_c).drop(index=100).to_csv(sl_c, index=False)          # slope20 在 D 段缺一日
    miss = m.preflight(ew_x, cw_x, ew_c, sl_c)
    assert not miss["ok"] and not miss["controls_ok"] and miss["coverage_ok"] and miss["db_ok"]
    assert miss["controls"]["slope20"]["d_missing_days"] == 1
    assert m.main() == 1 and json.loads(capsys.readouterr().out)["ok"] is False
    e = pd.read_csv(ew_c)
    e.loc[5, "factor_value"] = np.nan                           # equal_weight 在 D 段一日为 NaN（日期仍在）
    e.to_csv(ew_c, index=False)
    nan = m.preflight(ew_x, cw_x, ew_c, sl_c)
    assert nan["controls"]["equal_weight"]["d_missing_days"] == 0
    assert nan["controls"]["equal_weight"]["d_nan_days"] == 1 and not nan["ok"]

    def boom(db=None):
        raise RuntimeError("PG unreachable")
    monkeypatch.setattr(m, "_db_ping", boom)                   # 文件不可读 + 数据库不通 → 不抛，收成字符串
    worse = m.preflight(tmp_path / "nope.xlsx", cw_x, ew_c, sl_c)
    assert not worse["ok"] and "ew_xlsx" in worse["errors"] and "PG unreachable" in worse["errors"]["db"]
    json.dumps(worse, ensure_ascii=False)


def test_run_formal_preflight_first_and_failed_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.data._connect", _no_db)
    monkeypatch.setattr(m, "git_state", lambda root: {"commit": "test"})
    for name in ("EW_XLSX", "CW_XLSX", "EW_SIGNAL_CSV", "SLOPE20_CSV"):
        monkeypatch.setattr(m, name, tmp_path / f"missing_{name}")
    monkeypatch.setattr(m, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(m, "_frozen_clean", lambda: True)
    monkeypatch.setattr(m, "preflight", lambda *a, **k: {"ok": False, "errors": {"db": "x"}})
    with pytest.raises(SystemExit):
        m.run_formal("r1")
    assert not (tmp_path / "runs" / "r1").exists()            # 预检不过：不建目录
    monkeypatch.setattr(m, "preflight", lambda *a, **k: {"ok": True})
    with pytest.raises(FileNotFoundError):                     # 建目录后失败：manifest 留痕后原样抛出
        m.run_formal("r1")
    man = json.loads((tmp_path / "runs" / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert man["status"] == "failed" and man["error_type"] == "FileNotFoundError" and man["failed_utc"]
    assert "FileNotFoundError" in man["traceback"] and man["stage"] == "created"
    assert [a["path"] for a in man["artifacts"]] == ["outputs/preflight.json"]


def test_smoke_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "EW_XLSX", tmp_path / "must_not_read.xlsx")
    monkeypatch.setattr(m, "CW_XLSX", tmp_path / "must_not_read.xlsx")
    res = m.run_smoke(tmp_path / "smoke", n_perm=9, n_cal=2)
    assert set(res["verdicts"]["family"]) == {"F1", "F2"}
    for name in ("panel_main.csv", "verdicts.csv", "confirmation.csv", "calibration.csv",
                 "calibration_raw.csv", "report_per_pair.csv", "report_incumbent_reference.csv", "REPORT.md"):
        assert (tmp_path / "smoke" / name).exists(), name
    panel = pd.read_csv(tmp_path / "smoke" / "panel_main.csv")
    assert len(panel) == 32
    cols = ["report_direction", "report_tranche_sym_sharpe", "report_sharpe_offset_min",
            "report_sharpe_offset_median", "report_sharpe_offset_max", "report_legacy_hold_sharpe"]
    assert panel[cols].notna().all().all()
    assert (panel["report_sharpe_offset_min"] <= panel["report_sharpe_offset_median"]).all()
    assert (panel["report_sharpe_offset_median"] <= panel["report_sharpe_offset_max"]).all()
    for _, v in res["verdicts"].iterrows():                    # 代表行：面板逐变体 Sharpe == 关 3 / 旧口径同一数
        row = panel[(panel["family"] == v["family"]) & (panel["lb"] == v["lb"]) & (panel["zw"] == v["zw"])
                    & (panel["k"] == v["k"])].iloc[0]
        assert row["report_tranche_sym_sharpe"] == pytest.approx(v["sym_net_sharpe"], abs=1e-12)
        assert row["report_legacy_hold_sharpe"] == pytest.approx(v["report_legacy_hold_net_sharpe"], abs=1e-12)
        g = m.main_gates(v["p_two_sided_naive"], v["partial_oa_ic_vs_ew"], v["p_partial_vs_ew"],
                         v["direction"], bool(v["gate2"]), v["sym_net_sharpe"])
        assert {kk: bool(v[kk]) for kk in g} == g               # 代表的三关与纯函数同一判定
    assert (panel["legacy_ic"] - panel["report_legacy_ic_rotation_probe"]).abs().max() <= 1e-12


def test_calibration_progress_log_has_no_statistics():
    msgs = []
    raw = m.calibrate_confirmation(5, 60, 5, 100, n_cal=100, n_perm=9, log=msgs.append)
    assert len(raw) == 300
    assert msgs == [f"校准 (5,60,5) n_C=100 {kind} {c}/300"                      # 只含规格 / 场景 / 计数
                    for kind, c in (("stationary", 100), ("shared_volatility", 200), ("regime", 300))]


def test_db_ping_big_packet_and_timeout(monkeypatch):
    executed = []

    class Cur:
        def __init__(self, delay):
            self.delay, self.last = delay, None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql):
            executed.append(sql)
            if "repeat" in sql and self.delay:
                time.sleep(self.delay)
            self.last = sql

        def fetchone(self):
            return ("x" * 200000,) if "repeat" in self.last else (1,)

    class Conn:
        def __init__(self, delay=0.0):
            self.delay = delay

        def cursor(self):
            return Cur(self.delay)

        def close(self):
            pass

    monkeypatch.setattr(m.bdata, "load_db_config", lambda: {"fake": True})
    monkeypatch.setattr(m.bdata, "_connect", lambda db: Conn())
    m._db_ping()
    assert executed == ["SELECT 1", "SELECT repeat('x', 200000)"]           # 小包 + 20 万字节大包
    monkeypatch.setattr(m.bdata, "_connect", lambda db: Conn(delay=5.0))    # 大包挂住（MTU 黑洞）
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        m._db_ping(timeout_s=1)
    assert time.monotonic() - t0 < 4
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0                      # 闹钟已清


def test_preflight_rejects_out_of_order_export(tmp_path, monkeypatch, wind_files):
    ew, cw = _full_panel()
    _patch_days(monkeypatch, ew, cw)
    monkeypatch.setattr("backtest.data._connect", _no_db)
    monkeypatch.setattr(m, "_db_ping", lambda db=None: None)
    order = list(range(len(cw)))
    order[100], order[101] = 101, 100                          # 文件里相邻两日顺序对调
    _write_wind(tmp_path / "cw_swapped.xlsx", cw.iloc[order], m.CW_CODES)
    ew_x, _cw_x, d_dates = wind_files
    for p in (tmp_path / "a.csv", tmp_path / "b.csv"):
        pd.DataFrame({"date": d_dates, "factor_value": 0.1}).to_csv(p, index=False)
    pf = m.preflight(ew_x, tmp_path / "cw_swapped.xlsx", tmp_path / "a.csv", tmp_path / "b.csv")
    assert not pf["ok"] and not pf["coverage"]["gate1_calendar_ok"]
    assert not pf["coverage"]["index_unique_monotonic"]["cw"] and pf["coverage"]["index_unique_monotonic"]["ew"]
