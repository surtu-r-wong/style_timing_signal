"""等权/市值加权探针·编排层单测：窗口隔离与常量字面量、关 0 列映射与共享旋转、确认段 α*/Holm/方向、
预检不算收益、正式 run 合成全链路（成功 / data_blocked / 失败留痕 / 冻结护栏）。

规格：docs/plans/2026-09-30-ew-cw-axis-prereg.md。本文件只用合成数据，不读真实数据、不连库。
"""
import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from backtest import ew_cw_axis_probe as m


def _no_db(*_a, **_k):
    raise AssertionError("单测不得连库")


def _bomb(*_a, **_k):
    raise AssertionError("此处不得被调用")


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
    monkeypatch.setattr(m, "EXPECTED_DAYS", {f"{side}{p}": int(df[p].notna().sum())
                                             for side, df in (("ew", ew), ("cw", cw)) for p in m.PAIRS})


def _write_wind(path, frame, codes):
    keys = list(codes)
    pad = [None] * (len(keys) - 1)
    rows = [["开始日期", "2000-08-30", *pad], ["截止日期", "当前日期", *pad], ["收盘价", "close", *pad],
            ["日期", *keys], ["Date", *[codes[k] for k in keys]]]
    for d, vals in zip(frame.index, frame[keys].itertuples(index=False)):
        rows.append([d, *[None if np.isnan(v) else float(v) for v in vals]])
    rows += [[None] * (len(keys) + 1), ["数据来源：Wind", *[None] * len(keys)]]
    pd.DataFrame(rows).to_excel(path, header=False, index=False)


@pytest.fixture(scope="module")
def synth():
    return m.synthetic_inputs()


@pytest.fixture(scope="module")
def base_main(synth):
    ew, cw, blend, carry, c1, c2 = synth
    return m.evaluate_main(ew, cw, blend, carry, c1, c2, n_perm=9)


@pytest.fixture(scope="module")
def wind_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("wind")
    ew, cw = _full_panel()
    _write_wind(d / "ew.xlsx", ew, m.EW_CODES)
    _write_wind(d / "cw.xlsx", cw, m.CW_CODES)
    cal = cw.index
    return d / "ew.xlsx", d / "cw.xlsx", cal[(cal >= pd.Timestamp("2014-01-02")) & (cal <= m.END)]


# ---------------------------------------------------------------- T2 常量字面量与窗口隔离
def test_prereg_constants_literal():
    assert m.D_WIN == ("2014-01-02", "2026-09-29")
    assert m.HALVES == {"H1": ("2014-01-02", "2020-05-13"), "H2": ("2020-05-14", "2026-09-29")}
    assert m.C_END == "2013-12-31"
    assert m.END == pd.Timestamp("2026-09-29") and m.DATA_START == pd.Timestamp("2004-12-31")
    assert m.FORMS == ((5, 60), (5, 250), (20, 60), (20, 250)) and m.GRID_K == (5, 10, 20, 40)
    assert m.FAMILIES == ("F1", "F2")
    assert (m.SEED_D, m.SEED_CAL, m.SEED_C) == (20260930, 20260930 + 1_000_000, 20260930 + 2_000_000)
    assert (m.N_PERM, m.N_CAL) == (999, 400)
    assert m.ALPHA_GRID == (0.05, 0.04, 0.03, 0.025, 0.02, 0.015, 0.01, 0.005)
    assert m.CAL_KINDS == ("stationary", "shared_volatility", "regime") and m.CAL_SCALE == 0.005
    assert m.COST_BPS == 3.0 and m.GATE_P == 0.05
    assert m.EXPECTED_FIRST == {"ew300": pd.Timestamp("2004-12-31"), "ew500": pd.Timestamp("2004-12-31"),
                                "ew1000": pd.Timestamp("2013-12-31"), "cw300": pd.Timestamp("2004-12-31"),
                                "cw500": pd.Timestamp("2004-12-31"), "cw1000": pd.Timestamp("2004-12-31")}
    assert m.EXPECTED_DAYS == {"ew300": 5282, "ew500": 5282, "ew1000": 3100,
                               "cw300": 5282, "cw500": 5282, "cw1000": 5282}


def test_confirm_families_immune_to_2014_on(synth):
    ew, cw, blend, *_ = synth
    passing = [{"family": "F1", "lb": 20, "zw": 250, "k": 20, "direction": -1.0},
               {"family": "F2", "lb": 5, "zw": 60, "k": 5, "direction": 1.0}]
    rng = np.random.default_rng(3)
    cut = pd.Timestamp("2014-01-01")                            # 字面量：确认段不得取 2014 年任何收益
    b2 = blend.copy()
    b2[b2.index >= cut] = rng.normal(0.0, 0.05, int((b2.index >= cut).sum()))
    ew2, cw2 = ew.copy(), cw.copy()
    for df in (ew2, cw2):                                       # 2014 起的六条价格也乱改
        sel = df.index >= cut
        df.loc[sel] = df.loc[sel].to_numpy() * rng.uniform(0.5, 1.5, size=(int(sel.sum()), 3))
    a = m.confirm_families(passing, ew, cw, blend, n_cal=2, n_perm=19)
    b = m.confirm_families(passing, ew2, cw2, b2, n_cal=2, n_perm=19)
    for x, y in zip(a, b):
        pd.testing.assert_frame_equal(x, y, check_exact=True)
    assert set(a[0]["c_end"]) == {"2013-12-31"}


def test_evaluate_main_immune_to_pre_D_returns(synth, base_main):
    ew, cw, blend, carry, c1, c2 = synth
    b2 = blend.copy()
    pre = b2.index < pd.Timestamp("2014-01-02")                 # 字面量：D 首日
    b2[pre] = np.random.default_rng(4).normal(0.0, 0.05, int(pre.sum()))
    out = m.evaluate_main(ew, cw, b2, carry, c1, c2, n_perm=9)
    for key in ("panel", "verdicts", "per_pair", "incumbents"):
        pd.testing.assert_frame_equal(out[key], base_main[key], check_exact=True)


def test_half_windows_isolated(synth, base_main):
    ew, cw, blend, carry, c1, c2 = synth
    b2 = blend.copy()
    h2 = b2.index >= pd.Timestamp("2020-05-14")                 # 字面量：H2 首日
    b2[h2] = np.random.default_rng(5).normal(0.0, 0.05, int(h2.sum()))
    out = m.evaluate_main(ew, cw, b2, carry, c1, c2, n_perm=9)
    np.testing.assert_array_equal(out["panel"]["oa_ic_H1"].to_numpy(), base_main["panel"]["oa_ic_H1"].to_numpy())
    assert not np.array_equal(out["panel"]["oa_ic_H2"].to_numpy(), base_main["panel"]["oa_ic_H2"].to_numpy())


# ---------------------------------------------------------------- T6 关 0 列映射与共享旋转
def test_gate0_columns_and_shared_rotation(synth, monkeypatch):
    ew, cw, blend, carry, c1, c2 = synth
    n_perm = 9
    seen_adj, seen_partial, seen_offset, seen_gates, seen_best = [], [], [], [], []
    real_adj, real_partial, real_offset = m.adjusted_pvalue, m.batch_partial_oa_ic, m.batch_offset_ics
    real_gates, real_pick = m.main_gates, m.pick_representative
    monkeypatch.setattr(m, "adjusted_pvalue",
                        lambda res, j, crit: seen_adj.append((res, j, crit)) or real_adj(res, j, crit))
    monkeypatch.setattr(m, "batch_partial_oa_ic", lambda S, f, c, k, ix: seen_partial.append(
        (np.asarray(c, dtype=float).copy(), ix)) or real_partial(S, f, c, k, ix))
    monkeypatch.setattr(m, "batch_offset_ics", lambda S, f, k, ix: seen_offset.append(ix) or real_offset(S, f, k, ix))
    monkeypatch.setattr(m, "main_gates", lambda *a: seen_gates.append(a) or real_gates(*a))

    def pick(panel_f):                  # 哨兵：只报数列 legacy_ic 与 OA-IC 反号、p 设成特殊值，接错必现
        best, ok = real_pick(panel_f)
        best = best.copy()
        best["legacy_ic"] = -0.5 * (float(np.sign(best["oa_ic"])) or 1.0)
        best["p_two_sided_naive"] = 0.0123
        seen_best.append(best)
        return best, ok

    monkeypatch.setattr(m, "pick_representative", pick)
    out = m.evaluate_main(ew, cw, blend, carry, c1, c2, n_perm=n_perm)
    panel, verdicts = out["panel"], out["verdicts"]
    dates = m.window(blend, "2014-01-02", "2026-09-29").index
    n = len(dates)
    ref_idx = m.rotation_index(n, n_perm, 20260930)             # 按 SEED_D 字面量独立重建
    main_calls = [ix for ix in seen_offset if ix.shape[0] == n_perm + 1]
    assert len(main_calls) == 32 and all(np.array_equal(ix, ref_idx) for ix in main_calls)
    assert len(seen_partial) == 4 and all(np.array_equal(ix, ref_idx) for _c, ix in seen_partial)
    res = seen_adj[0][0]
    np.testing.assert_array_equal(res.observed, np.abs(panel["oa_ic"].to_numpy()))    # 面板行序 = 列序
    assert [c for *_, c in seen_adj] == ["min_p", "max_t", "min_p", "max_t"]
    for (res_, j, _crit), fam in zip(seen_adj, ["F1", "F1", "F2", "F2"]):
        v = verdicts[verdicts["family"] == fam].iloc[0]
        row = panel.iloc[j]
        assert (row["family"], row["lb"], row["zw"], row["k"]) == (fam, v["lb"], v["zw"], v["k"])
        assert res_.observed[j] == abs(v["oa_ic"])
    reps = {j for _r, j, _c in seen_adj}
    jj = next(i for i in range(len(panel)) if i not in reps and panel.iloc[i]["k"] == 5)
    fam, lb, zw, k = panel.iloc[jj][["family", "lb", "zw", "k"]]
    S = m.family_signal(ew, cw, fam, int(lb), int(zw)).reindex(dates).to_numpy()
    fwd = pd.Series(blend.reindex(dates).to_numpy()).rolling(int(k)).sum().shift(-int(k)).to_numpy()
    shifts = np.random.default_rng(20260930).integers(0, n, size=n_perm)
    pts = [np.arange(o, n - int(k), int(k)) for o in range(int(k))]
    exp = [abs(np.mean([spearmanr(np.roll(S, s)[p], fwd[p]).correlation for p in pts])) for s in shifts]
    np.testing.assert_allclose(res.null_stats[:, jj], exp, rtol=0, atol=1e-12)   # 非代表变体的零分布列
    # T12 接线：每族第 1 次偏 IC 控 equal_weight、第 2 次控 slope20；三关实参来自代表行与关 1 控制
    c_ew, c_sl = c1.reindex(dates).to_numpy(), c2.reindex(dates).to_numpy()
    for i in range(2):
        np.testing.assert_array_equal(seen_partial[2 * i][0], c_ew)
        np.testing.assert_array_equal(seen_partial[2 * i + 1][0], c_sl)
    assert len(seen_gates) == 2 and len(seen_best) == 2
    ident = np.arange(n)[None, :]
    for (p_ic, partial, _pp, direction, _sok, net_sharpe), best, (_i, v) in zip(seen_gates, seen_best,
                                                                              verdicts.iterrows()):
        assert p_ic == best["p_two_sided_naive"] == 0.0123              # 关 1 用代表行的 OA-IC p
        assert direction == (float(np.sign(best["oa_ic"])) or 1.0)       # 方向取全窗 OA-IC 符号
        sig = m.family_signal(ew, cw, v["family"], int(v["lb"]), int(v["zw"]))
        S, k_ = sig.reindex(dates).to_numpy(), int(v["k"])
        fwd_k = m.forward_sums(blend.reindex(dates).to_numpy(), k_)
        assert partial == pytest.approx(real_partial(S, fwd_k, c_ew, k_, ident)[0], abs=1e-12)
        assert partial != pytest.approx(real_partial(S, fwd_k, c_sl, k_, ident)[0], abs=1e-12)
        pos_full = m.tranche_position(sig, k_, direction)
        assert net_sharpe == pytest.approx(m.net_stats(pos_full.reindex(dates), blend, carry)["net_sharpe"], abs=1e-12)
        # T14：再晚一日口径 = lag1_position 在全历史平移后截 D 段；现货池口径只多不空、不含 carry
        assert v["report_lag1_net_sharpe"] == pytest.approx(
            m.net_stats(m.lag1_position(pos_full, dates), blend, carry)["net_sharpe"], abs=1e-12)
        lf = m.tranche_position(sig, k_, direction, long_only=True).reindex(dates)
        assert v["report_longflat_net_sharpe"] == pytest.approx(m.net_stats(lf, blend, None)["net_sharpe"], abs=1e-12)


# ---------------------------------------------------------------- T9 确认段 α* / Holm / 方向
def _fake_cal(alpha_by_k):
    """按代表 k 造一份校准原始表，使 rejection_table + choose_alpha_star 得到指定 α*（None = 不可校准）。"""
    def fake(lb, zw, k, n_c, *, n_cal, n_perm, seed_base, log=None):
        target = alpha_by_k[k]
        if target is None:
            ps = [0.001] * 10 + [0.9] * 90                      # 各档拒绝率 10% → 不可校准
        else:
            above = min(a for a in m.ALPHA_GRID if a > target)  # 高一档拒绝率 11%，目标档 5%
            ps = [0.001] * 5 + [(target + above) / 2] * 6 + [0.9] * 89
        return pd.DataFrame([{"kind": kind, "rep": i, "p_one_sided": p}
                             for kind in m.CAL_KINDS for i, p in enumerate(ps)])
    return fake


def _pool(obs, n_more):
    """长 1000 的置换池：观测 + n_more 个同向更极端值 + 其余 0 → 同向单侧 p = (1 + n_more) / 1000。"""
    pool = np.zeros(1000)
    pool[0] = obs
    pool[1:1 + n_more] = 2.0 * obs
    return pool


def test_confirm_families_alpha_holm_direction(synth, monkeypatch):
    ew, cw, blend, *_ = synth
    passing = [{"family": "F1", "lb": 20, "zw": 60, "k": 5, "direction": 1.0},
               {"family": "F2", "lb": 5, "zw": 60, "k": 10, "direction": -1.0}]

    def run(alpha_by_k, pools):
        monkeypatch.setattr(m, "calibrate_confirmation", _fake_cal(alpha_by_k))
        monkeypatch.setattr(m, "batch_oa_ic", lambda S, fwd, k, ix: pools[k])
        conf, table, _raw = m.confirm_families(passing, ew, cw, blend, n_cal=100, n_perm=9)
        return conf.set_index("family")

    # 两族都可校准：α* 0.04 / 0.02 → 用较小者 0.02；Holm：0.009 ≤ 0.01、0.015 ≤ 0.02
    c = run({5: 0.04, 10: 0.02}, {5: _pool(0.2, 8), 10: _pool(-0.2, 14)})
    assert c.loc["F1", "alpha_star_own"] == 0.04 and c.loc["F2", "alpha_star_own"] == 0.02
    assert set(c["alpha_star_used"]) == {0.02}
    assert c.loc["F1", "p_one_sided_C"] == pytest.approx(0.009) and c.loc["F2", "p_one_sided_C"] == pytest.approx(0.015)
    assert c["p_ok_holm"].all() and c["sign_ok"].all()          # F2 方向 −1：负 OA-IC 同号、单侧 p 取负向尾
    # 一族不可校准：其 p 记 1；另一族须 p ≤ α*/2（0.03 > 0.02 不过；0.015 过）
    c2 = run({5: None, 10: 0.04}, {5: _pool(0.3, 0), 10: _pool(-0.2, 29)})
    assert set(c2["alpha_star_used"]) == {0.04} and not c2["p_ok_holm"].any()
    c3 = run({5: None, 10: 0.04}, {5: _pool(0.3, 0), 10: _pool(-0.2, 14)})
    assert not c3.loc["F1", "p_ok_holm"] and c3.loc["F2", "p_ok_holm"]
    # C 段 Sharpe 用方向 −1 的分批持仓、不含 carry
    sig = m.family_signal(ew, cw, "F2", 5, 60)
    cdates = m.window(blend, sig.dropna().index.min(), "2013-12-31").index
    exp = m.net_stats(m.tranche_position(sig, 10, -1.0).reindex(cdates), blend, None)["net_sharpe"]
    assert c.loc["F2", "spot_net_sharpe_C"] == pytest.approx(exp, abs=1e-12)


# ---------------------------------------------------------------- T4 预检不算收益 / 正式 run 合成全链路
def test_preflight_computes_no_returns(tmp_path, monkeypatch, wind_files):
    ew_x, cw_x, d_dates = wind_files
    _patch_days(monkeypatch, *_full_panel())
    monkeypatch.setattr("backtest.data._connect", _no_db)
    monkeypatch.setattr(m, "_db_ping", lambda db=None: None)
    for name in ("blend_from_export", "data_checks", "reconcile_with_db", "forward_sums", "batch_offset_ics",
                 "batch_oa_ic", "batch_partial_oa_ic", "net_returns", "net_stats", "run_strategy", "pair_signal",
                 "family_signal", "tranche_position", "evaluate_main", "confirm_families", "calibrate_confirmation"):
        monkeypatch.setattr(m, name, _bomb)
    monkeypatch.setattr(pd.Series, "pct_change", _bomb)
    monkeypatch.setattr(pd.DataFrame, "pct_change", _bomb)
    for p in (tmp_path / "a.csv", tmp_path / "b.csv"):
        pd.DataFrame({"date": d_dates, "factor_value": 0.1}).to_csv(p, index=False)
    pf = m.preflight(ew_x, cw_x, tmp_path / "a.csv", tmp_path / "b.csv")
    assert pf["ok"] and not pf["errors"], pf["errors"]
    # 输出只含版式 / 日期 / 缺失 / 连通类字段（白名单）：任何收益口径字段混进来即失败
    assert set(pf) == {"ok", "errors", "coverage", "controls", "db_ok", "coverage_ok", "controls_ok"}
    assert set(pf["coverage"]) == {"index_unique_monotonic", "calendar_mismatch_days", "internal_nan_days",
                                   "first_valid", "last_valid", "first_last_ok", "valid_days", "valid_days_ok",
                                   "gate1_calendar_ok"}
    assert all(set(v) == {"last_date", "d_days", "d_missing_days", "d_nan_days"} for v in pf["controls"].values())


@pytest.fixture
def formal_env(tmp_path, monkeypatch, wind_files):
    """正式 run 的全部外部依赖换成合成物：路径、库、git、冻结护栏、预检、规模。"""
    import backtest.data as bd
    ew_x, cw_x, d_dates = wind_files
    _patch_days(monkeypatch, *_full_panel())
    cw_read = m.read_wind_export(cw_x, m.CW_CODES)              # 与 run 内读回的同一份数
    blend_ref = m.blend_from_export(cw_read)
    monkeypatch.setattr(bd, "_connect", _no_db)
    monkeypatch.setattr(bd, "load_spot_close", lambda p, db=None: cw_read[p].dropna())
    monkeypatch.setattr(bd, "load_underlying_returns", lambda kj, db=None: blend_ref.dropna())
    carry = pd.Series(0.05 + 0.01 * np.sin(np.arange(len(d_dates))), index=d_dates)
    carry = carry[carry.index >= pd.Timestamp("2015-04-16")]
    monkeypatch.setattr(bd, "load_carry", lambda kj, db=None: carry)
    git_calls = []

    def fake_git(root):                                         # 第 1 次 = 开局，第 2 次 = 结束（git_end）
        git_calls.append(root)
        return {"commit": f"call{len(git_calls)}"}

    monkeypatch.setattr(m, "git_state", fake_git)
    monkeypatch.setattr(m, "_frozen_clean", lambda: True)
    monkeypatch.setattr(m, "preflight", lambda *a, **k: {"ok": True, "stub": True})
    monkeypatch.setattr(m, "_db_ping", _bomb)
    rng = np.random.default_rng(9)
    n = len(d_dates)                                            # 两份控制序列分布不同：对调可见
    pd.DataFrame({"date": d_dates, "factor_value": np.tanh(rng.normal(size=n))}).to_csv(tmp_path / "ew_sig.csv",
                                                                                       index=False)
    pd.DataFrame({"date": d_dates, "factor_value": np.sin(np.arange(n) / 37.0)}).to_csv(tmp_path / "sl_sig.csv",
                                                                                      index=False)
    for name, val in (("EW_XLSX", ew_x), ("CW_XLSX", cw_x), ("EW_SIGNAL_CSV", tmp_path / "ew_sig.csv"),
                      ("SLOPE20_CSV", tmp_path / "sl_sig.csv"), ("RUN_ROOT", tmp_path / "runs"),
                      ("N_PERM", 9), ("N_CAL", 2)):
        monkeypatch.setattr(m, name, val)
    reads = []
    real_read, real_sig = m.read_wind_export, m._read_signal
    monkeypatch.setattr(m, "read_wind_export", lambda p, c: reads.append(Path(p)) or real_read(p, c))
    monkeypatch.setattr(m, "_read_signal", lambda p: reads.append(Path(p)) or real_sig(p))
    return {"tmp": tmp_path, "reads": reads, "d_dates": d_dates, "git_calls": git_calls,
            "real_read": real_read, "real_sig": real_sig}


def _check_artifacts(run):
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    files = sorted(str(p.relative_to(run)) for sub in ("inputs", "outputs", "logs")
                   for p in (run / sub).rglob("*") if p.is_file())
    assert sorted(a["path"] for a in man["artifacts"]) == files        # 无遗漏、manifest 之后无新增文件
    for a in man["artifacts"]:                                          # 含 logs/run.log：哈希与落盘内容逐一相符
        assert hashlib.sha256((run / a["path"]).read_bytes()).hexdigest() == a["sha256"], a["path"]
    return man, files


def _code_frozen(files):
    return all(f"inputs/code/{rel.replace('/', '__')}" in files for rel in (*m.CODE_FILES, *m.TEST_FILES))


def test_run_formal_synthetic_success(formal_env):
    run = m.run_formal("syn-ok")
    man, files = _check_artifacts(run)
    assert man["status"] == "complete" and man["stage"] == "verdicts_written" and (man["n_perm"], man["n_cal"]) == (9, 2)
    assert man["git"] == {"commit": "call1"} and man["git_end"] == {"commit": "call2"} and man["created_utc"]
    assert "logs/run.log" in files and "完成" in (run / "logs/run.log").read_text(encoding="utf-8").splitlines()[-1]
    assert formal_env["reads"] and all(p.is_relative_to(run / "inputs") for p in formal_env["reads"])
    assert _code_frozen(files) and "inputs/prereg.md" in files and "inputs/carry_blend.csv" in files
    assert "outputs/verdicts_main.csv" in files
    checks = json.loads((run / "outputs/data_checks.json").read_text(encoding="utf-8"))
    assert checks["data_ok"] and checks["carry_first"] == "2015-04-16"
    d = formal_env["d_dates"]
    assert checks["carry_missing_D_days"] == int((d < pd.Timestamp("2015-04-16")).sum())
    ver = pd.read_csv(run / "outputs/verdicts.csv")
    assert set(ver["verdict"]) <= {"STOP", "STOP_NOT_REPLICATED", "GO_ENTRY"}
    # T12：用 run/inputs 下的冻结副本独立重算代表的偏 IC（控 equal_weight）、关 3 Sharpe（含 carry）、现货池 Sharpe
    ew_i = formal_env["real_read"](run / "inputs/ew.xlsx", m.EW_CODES)
    cw_i = formal_env["real_read"](run / "inputs/cw.xlsx", m.CW_CODES)
    blend_i = m.blend_from_export(cw_i)
    dates = m.window(blend_i, "2014-01-02", "2026-09-29").index
    carry_i = pd.read_csv(run / "inputs/carry_blend.csv", parse_dates=["date"], index_col="date",
                          float_precision="round_trip")["carry"]
    c_ew = formal_env["real_sig"](run / "inputs/ew_sig.csv").reindex(dates).to_numpy()
    for _i, v in ver.iterrows():
        sig = m.family_signal(ew_i, cw_i, v["family"], int(v["lb"]), int(v["zw"]))
        k_, d_ = int(v["k"]), float(v["direction"])
        fwd = m.forward_sums(blend_i.reindex(dates).to_numpy(), k_)
        partial = m.batch_partial_oa_ic(sig.reindex(dates).to_numpy(), fwd, c_ew, k_, np.arange(len(dates))[None, :])[0]
        assert v["partial_oa_ic_vs_ew"] == pytest.approx(partial, abs=1e-12)
        sym = m.net_stats(m.tranche_position(sig, k_, d_).reindex(dates), blend_i, carry_i)["net_sharpe"]
        assert v["sym_net_sharpe"] == pytest.approx(sym, abs=1e-12)
        lf = m.net_stats(m.tranche_position(sig, k_, d_, long_only=True).reindex(dates), blend_i, None)["net_sharpe"]
        assert v["report_longflat_net_sharpe"] == pytest.approx(lf, abs=1e-12)


def test_run_formal_synthetic_data_blocked(formal_env, monkeypatch):
    sl = formal_env["tmp"] / "sl_sig.csv"
    pd.read_csv(sl).iloc[:-1].to_csv(sl, index=False)                   # 控制信号末日 < END → 闸 4 不过
    monkeypatch.setattr(m, "evaluate_main", _bomb)                        # data_blocked：不跑任何闸
    monkeypatch.setattr(m, "confirm_families", _bomb)
    calls = formal_env["git_calls"]

    def git_end_fails(root):                                            # 结束时取 git 失败不影响 status
        calls.append(root)
        if len(calls) > 1:
            raise RuntimeError("git 不可用")
        return {"commit": "call1"}

    monkeypatch.setattr(m, "git_state", git_end_fails)
    run = m.run_formal("syn-blocked")
    man, _files = _check_artifacts(run)
    assert man["status"] == "complete" and man["stage"] == "verdicts_written" and man["git"] == {"commit": "call1"}
    assert "git 不可用" in man["git_end"]["error"]
    assert set(pd.read_csv(run / "outputs/verdicts.csv")["verdict"]) == {"DATA_BLOCKED"}
    assert set(man["verdicts"].values()) == {"DATA_BLOCKED"}
    assert not json.loads((run / "outputs/data_checks.json").read_text(encoding="utf-8"))["gate4_frozen_ok"]


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def test_run_formal_synthetic_failure_manifest(formal_env, monkeypatch):
    monkeypatch.setattr(m, "evaluate_main", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        m.run_formal("syn-fail")
    run = formal_env["tmp"] / "runs" / "syn-fail"
    man, files = _check_artifacts(run)
    assert man["status"] == "failed" and man["stage"] == "data_checks_done" and man["error_type"] == "RuntimeError"
    assert "outputs/data_checks.json" in files and "inputs/carry_blend.csv" in files and _code_frozen(files)
    assert man["git"] == {"commit": "call1"} and man["created_utc"]


def test_run_formal_failure_stages(formal_env, monkeypatch):
    real_gates = m.main_gates
    monkeypatch.setattr(m, "main_gates", lambda *a: {**real_gates(*a), "gate1": True, "gate2": True,
                                                     "gate3": True, "main_pass": True})   # 两族都进确认段
    monkeypatch.setattr(m, "confirm_families", _boom)                   # 确认段失败 → main_done
    with pytest.raises(RuntimeError):
        m.run_formal("syn-confirm-fail")
    man, files = _check_artifacts(formal_env["tmp"] / "runs" / "syn-confirm-fail")
    assert man["status"] == "failed" and man["stage"] == "main_done" and _code_frozen(files)
    assert "outputs/verdicts_main.csv" in files and "outputs/confirmation.csv" not in files
    monkeypatch.undo()


def test_run_formal_failure_stage_confirm_done(formal_env, monkeypatch):
    real_gates = m.main_gates
    monkeypatch.setattr(m, "main_gates", lambda *a: {**real_gates(*a), "gate1": True, "gate2": True,
                                                     "gate3": True, "main_pass": True})
    monkeypatch.setattr(m, "write_report", _boom)                       # 报告写失败 → confirm_done
    with pytest.raises(RuntimeError):
        m.run_formal("syn-report-fail")
    man, files = _check_artifacts(formal_env["tmp"] / "runs" / "syn-report-fail")
    assert man["status"] == "failed" and man["stage"] == "confirm_done" and _code_frozen(files)
    assert "outputs/confirmation.csv" in files and "outputs/verdicts_main.csv" in files


def test_failed_manifest_written_even_if_hashing_fails(formal_env, monkeypatch):
    monkeypatch.setattr(m, "evaluate_main", _boom)
    monkeypatch.setattr(m, "artifact_record", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        m.run_formal("syn-hash-fail")
    man = json.loads((formal_env["tmp"] / "runs" / "syn-hash-fail" / "manifest.json").read_text(encoding="utf-8"))
    assert man["status"] == "failed" and "boom" in man["artifacts_error"] and "artifacts" not in man


def test_sigterm_becomes_failed_manifest(formal_env, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        m._sigterm_to_exit(signal.SIGTERM, None)
    assert ei.value.code == 143
    before = signal.getsignal(signal.SIGTERM)
    seen = {}

    def killed(*_a, **_k):                                              # 主样本进行中被 SIGTERM
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        seen["handler"](signal.SIGTERM, None)

    monkeypatch.setattr(m, "evaluate_main", killed)
    with pytest.raises(SystemExit) as ei2:
        m.run_formal("syn-term")
    assert ei2.value.code == 143 and seen["handler"] is m._sigterm_to_exit
    assert signal.getsignal(signal.SIGTERM) is before                   # 结束后恢复原处理器
    man = json.loads((formal_env["tmp"] / "runs" / "syn-term" / "manifest.json").read_text(encoding="utf-8"))
    assert man["status"] == "failed" and man["error_type"] == "SystemExit" and man["stage"] == "data_checks_done"


def test_confirm_families_calibrates_same_spec_once(synth, monkeypatch):
    ew, cw, blend, *_ = synth
    calls, real = [], m.calibrate_confirmation
    monkeypatch.setattr(m, "calibrate_confirmation", lambda *a, **k: calls.append(a) or real(*a, **k))
    passing = [{"family": "F1", "lb": 5, "zw": 60, "k": 5, "direction": 1.0},
               {"family": "F2", "lb": 5, "zw": 60, "k": 5, "direction": -1.0}]
    _conf, table, raw = m.confirm_families(passing, ew, cw, blend, n_cal=2, n_perm=9)
    assert len(calls) == 1                                              # 同规格只校准一次
    t1, t2 = (table[table["family"] == f].drop(columns="family").reset_index(drop=True) for f in ("F1", "F2"))
    pd.testing.assert_frame_equal(t1, t2, check_exact=True)
    direct = real(*calls[0], n_cal=2, n_perm=9, seed_base=m.SEED_CAL)   # 缓存不改任何数
    pd.testing.assert_frame_equal(raw[raw["family"] == "F1"].drop(columns="family").reset_index(drop=True),
                                  direct, check_exact=True)


def test_frozen_file_lists_literal():
    code = ("backtest/__init__.py", "backtest/baseline.py", "backtest/data.py", "backtest/engine.py",
            "backtest/ew_cw_axis_probe.py", "backtest/execution_audit.py", "backtest/execution_ledger.py",
            "backtest/gate0_criterion_study.py", "backtest/leverage_probe.py", "backtest/metrics.py",
            "backtest/paired_bootstrap.py", "backtest/positions.py", "backtest/research_statistics.py",
            "backtest/rotation_probe.py", "backtest/run_manifest.py", "backtest/selection_permutation.py",
            "backtest/significance.py", "signals/common/config.py", "signals/equal_weight/generate_signal.py")
    tests = ("tests/test_ew_cw_axis_probe.py", "tests/test_ew_cw_axis_probe_orchestration.py")
    assert m.CODE_FILES == code and m.TEST_FILES == tests
    assert m.FROZEN_FILES == (*code, *tests, "docs/plans/2026-09-30-ew-cw-axis-prereg.md")


def test_code_files_cover_import_closure():
    probe = ("import json, sys; from pathlib import Path; root = Path.cwd().resolve(); sys.path.insert(0, str(root)); "
             "import backtest.ew_cw_axis_probe; "
             "print(json.dumps(sorted({str(Path(x.__file__).resolve().relative_to(root)) "
             "for x in list(sys.modules.values()) if getattr(x, '__file__', None) "
             "and Path(x.__file__).resolve().is_relative_to(root)})))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=m.ROOT, capture_output=True, text=True, check=True)
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert "backtest/ew_cw_axis_probe.py" in loaded
    assert loaded == set(m.CODE_FILES), (sorted(loaded - set(m.CODE_FILES)), sorted(set(m.CODE_FILES) - loaded))


def test_frozen_clean_false_when_git_unavailable(monkeypatch):
    def no_git(*_a, **_k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(m.subprocess, "run", no_git)
    assert m._frozen_clean() is False


def test_run_formal_frozen_guard_first(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(m, "_frozen_clean", lambda: False)
    monkeypatch.setattr(m, "preflight", _bomb)                          # 护栏在预检之前
    with pytest.raises(SystemExit):
        m.run_formal("r1")
    assert not (tmp_path / "runs").exists()


def test_frozen_clean_requires_tracked_and_clean(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                       capture_output=True)

    git("init", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(m, "ROOT", repo)
    monkeypatch.setattr(m, "FROZEN_FILES", ("a.py",))
    assert not m._frozen_clean()                                        # 未跟踪
    git("add", "a.py")
    assert not m._frozen_clean()                                        # 已暂存未提交（无 HEAD）
    git("commit", "-q", "-m", "c")
    assert m._frozen_clean()                                            # 已提交且干净
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert not m._frozen_clean()                                        # 工作区有改动
    git("add", "a.py")
    assert not m._frozen_clean()                                        # 暂存区有改动
