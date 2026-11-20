"""Read-only, local-only audit of CSI300 total return / secondary bond fund crossing.

Run under a memory limit. --fetch downloads Wind history and reads existing PG
benchmarks, but never writes a database or production signal file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "backtest/output/runs/20261019-equity-bond-crossing-r1"
START, END = "2004-01-01", "2026-10-16"


def fetch():
    import pandas as pd
    import psycopg2
    import requests
    from deploy.daily_signals.topup_guard import load_gateway_config
    from signals.common.config import load_db_config

    OUT.mkdir(parents=True, exist_ok=True)
    protocol = {
        "scope": "exploratory historical diagnostic; no production adoption",
        "codes": ["H00300.CSI", "885007.WI"],
        "range": [START, END],
        "primary": "raw close ratio <=1; entry from above; 60-trading-day refractory period",
        "bands": [0.97, 1.0, 1.03, 1.05],
        "horizons": [20, 60, 120, 245],
        "rebase": ["2004-12-31", "2005-01-04", "2010-01-04", "2014-01-02"],
        "increment": "2015 onward; incumbent slope20 long-flat and EW symmetric; veto short only, force spot long only, both; 500/1000 50:50 price proxy; shift(2); 3bps/side; no carry",
        "notes": "All years are retrospective; 2024-26 was reused before. No untouched test set. Delay assumes fund index observable by next close; publication time not verified.",
        "budget": {"probe_cells": 12, "history_calendar_cells": 2 * 6 * ((date.fromisoformat(END)-date.fromisoformat(START)).days+1)},
    }
    (OUT / "protocol.json").write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    path = OUT / "wind_raw.json"
    if not path.exists():
        cfg = load_gateway_config()
        s = requests.Session()
        s.trust_env = False
        try:
            r = s.get(cfg["url"].rstrip("/") + "/fetch/index_daily",
                      params={"indices": "H00300.CSI,885007.WI", "start": START, "end": END},
                      headers={"Authorization": "Bearer " + cfg["token"], "X-DBM-Project": "style_timing_signal"},
                      timeout=180)
            obj = r.json()
            if r.status_code != 200 or obj.get("status") != "ok":
                raise RuntimeError("Wind query failed; response withheld")
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            raise RuntimeError("Wind request failed: " + type(exc).__name__) from None
    obj = json.loads(path.read_text())
    raw = pd.DataFrame(obj["rows"], columns=obj["columns"])
    raw.to_csv(OUT / "wind_raw.csv", index=False)
    db = load_db_config()
    try:
        conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"], user=db["user"], password=db["password"], connect_timeout=10,
                               options="-c default_transaction_read_only=on -c statement_timeout=20000")
        with conn.cursor() as cur:
            cur.execute("SELECT index_code,trade_date,close FROM stock_selector.index_daily WHERE index_code IN (%s,%s,%s) AND trade_date<=%s ORDER BY trade_date,index_code", ("000300.SH", "000905.SH", "000852.SH", END))
            pg = pd.DataFrame(cur.fetchall(), columns=["code", "date", "close"])
        conn.close()
    except Exception as exc:
        raise RuntimeError("PG read failed: " + type(exc).__name__) from None
    pg.to_csv(OUT / "pg_benchmarks.csv", index=False)
    for name in ("slope20_longflat", "equal_weight_symmetric"):
        (OUT / f"{name}.csv").write_bytes((ROOT / "output/recommended" / f"{name}.csv").read_bytes())
    print("FETCH", raw.groupby("ts_code")["trade_date"].agg(["count", "min", "max"]).to_json(), flush=True)


def entry_indices(ratio, threshold, cooldown=60):
    import numpy as np
    hits = np.flatnonzero(((ratio <= threshold) & (ratio.shift(1) > threshold)).values)
    accepted = []
    for i in hits:
        if not accepted or i - accepted[-1] >= cooldown:
            accepted.append(int(i))
    return accepted


def metrics(ret):
    import numpy as np
    nav = (1 + ret).cumprod()
    peaks = nav.cummax().clip(lower=1)
    return {"n": len(ret), "cagr": nav.iloc[-1] ** (245 / len(ret)) - 1,
            "sharpe": ret.mean() / ret.std(ddof=1) * np.sqrt(245),
            "maxdd": (nav / peaks - 1).min(), "total": nav.iloc[-1] - 1}


def proxy_returns(position, underlying, lag=2, cost_bps=3.):
    """Close-to-close PnL; lag=2 executes a day-t signal at day-(t+1) close.

    Market return first accrues on t+2; transaction cost is booked at the t+1
    execution close. No cost is charged twice for an unchanged position.
    """
    held = position.shift(lag).fillna(0.)
    at_close = position.shift(lag-1).fillna(0.)
    return held * underlying - (at_close-held).abs() * cost_bps / 1e4


def write_report():
    """Write a self-contained local research report, with tables linked to inputs."""
    import base64
    import pandas as pd
    from html import escape

    summary = json.loads((OUT / "summary.json").read_text())
    ev = pd.read_csv(OUT / "events.csv")
    es = pd.read_csv(OUT / "event_summary.csv")
    inc = pd.read_csv(OUT / "incremental_strategies.csv")
    annual = pd.read_csv(OUT / "annual_audit.csv")
    bt = pd.read_csv(OUT / "paired_bootstrap.csv")
    yr = pd.read_csv(OUT / "increment_by_year.csv")
    primary = ev[ev.threshold.eq(1.)].copy()
    short = yr[yr.strategy.eq("1.0_veto_short_lag2")]
    concentration = short.loc[short.year.isin([2015, 2016]), "sum_daily_delta"].sum() / short.sum_daily_delta.sum()

    def table(df, percent=()):
        df = df.copy()
        for c in percent:
            df[c] = df[c].map(lambda v: f"{v:.2%}" if pd.notna(v) else "—")
        return df.to_html(index=False, border=0, float_format=lambda v: f"{v:.4f}", na_rep="—")

    results = inc[inc.lag.eq(2) & inc.period.eq("all") & inc.rule.isin(["baseline", "1.0", "1.03"])]
    image_data = base64.b64encode((OUT / "audit_chart.png").read_bytes()).decode()
    sources = [
        ("流传说法的公开版本（2026-10-18）", "https://3g.sina.com.cn/news/detail/5355220905492731.html"),
        ("指数代码及年度收益的公开材料（2023-08-31）", "https://finance.sina.cn/fund/jjgdxw/2023-08-31/detail-imzkasmx6951831.d.html"),
        ("上交所统计年鉴：沪深300发布及基日", "https://www.sse.com.cn/aboutus/publication/factbook/documents/c/10170572/files/36d0635dee474838943e931bcc04c3df.pdf"),
    ]
    links = "<ul>" + "".join(f'<li><a href="{escape(u)}">{escape(t)}</a></li>' for t,u in sources) + "</ul>"
    report = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>沪深300全收益与二级债基交点验证 · 2026-10-19</title>
<style>body{{font:16px/1.75 system-ui,sans-serif;color:#172435;max-width:1200px;margin:35px auto;padding:0 22px}}h1{{font-size:29px}}h2{{font-size:22px;margin-top:32px}}table{{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;white-space:nowrap}}td,th{{padding:7px 10px;border-bottom:1px solid #dde3ea;text-align:right}}th{{background:#eef3f8}}img{{max-width:100%}}code{{background:#eef3f8;padding:2px 4px}}.verdict{{background:#eef5fb;border-left:4px solid #365c8d;padding:15px 20px}}a{{color:#265c98}}</style>
<h1>沪深300全收益 × 二级债基：交点是否止跌，能否补充现役信号？</h1>
<p>研究日：2026-10-19；数据截至 2026-10-16。范围：历史核验和独立增量诊断，未改生产信号、数据库或调度。</p>
<div class="verdict"><b>结论：不能作为“碰线即止跌”的规则；值得保留为长期低位观察项，尚不足以接入自动仓位。</b>
<p>严格下穿后的长期回报存在描述性改善，但短期仍可能大跌。对现役两池，严格比值≤1时禁止空头出现小幅历史增益；阈值放宽、成交延后和分期结果暴露出不稳定性。没有独立样本外证据。</p></div>
<h2>数据与口径</h2>
<p>Wind 原始收盘：<code>H00300.CSI</code> / <code>885007.WI</code>。共同有效区间 2004-12-31～2026-10-16，共 {summary['common_rows']} 日；沪深300全收益在此前242行为空，直接剔除，不填充。共同区间无缺失、无重复键，价格均为有限正数。其他基准只读现有 PostgreSQL 的中证500/1000；现役持仓从本地镜像冻结副本读取。</p>
<p>定义 <code>R=沪深300全收益原始点位/二级债基原始点位</code>。向下进入 R≤1 是严格触发；相邻保留事件至少间隔60个交易日。0.97、1.03、1.05为敏感性检查，不选最优阈值。原始36次下穿合并为15次，仍不是15个独立牛熊周期。预先冻结的研究规格见 <a href="protocol.json">protocol.json</a>；后续追加的回撤对照、事后低点覆盖均只是诊断。</p>
<p>这不是股权风险溢价的严格估计：两个点位由各自基准日/基点定义，二级债基自身也有股票、可转债等风险敞口。R=1没有现金流估值或套利约束保证。原始2004-12-31点位分别1000、1026.4844，本来就不同。</p>
<h2>用户列举年份的核验</h2>
{table(annual[annual.year.isin([2012,2016,2018,2022,2024])])}
<p>2012有多次触发；7月23日触发后60日内仍下跌7.35%，随后245日收益为−5.07%。2016年1月15日后60日内仍下跌8.50%。2018年12月26日较接近低点，但2019年1月3日仍创新低。2022年最低比值为1.021531（10月31日），<b>全年没有严格相交</b>，只有放宽到3%才能计入。2024年初属于跨年低位区域，9月4日再次下穿后仍下跌2.80%。</p>
<p>被省略的反例更重要：2008年9月11日下穿后60日内仍跌21.40%；2005年2月23日下穿后120日内仍跌21.64%。两者均按收盘计算，未计盘中更低价格。规则也有漏报：2010年7月和2022年4月的局部低点未进入3%接近区，详见下方事后低点诊断。</p>
<h2>全部严格事件及未来表现</h2>
<p>收益/最大向下偏离以触发日沪深300全收益收盘为基准，最大向下偏离不是路径峰谷最大回撤。20/60/120/245均为交易日。短期8/15上涨（20日），一年13/15上涨；60日内6/15仍跌超5%。一年平均收益被2006事件的约279%拉高，<b>中位数约12%</b>更具代表性。事件窗口会重叠，不把这些胜率视作可交易的独立概率。</p>
{table(es[es.threshold.eq(1.)], ["mean_return","median_return","win_rate","mean_adverse_excursion","worst_adverse_excursion","adverse_worse_than_5pct","unconditional_mean","unconditional_win_rate"])}
{table(primary[["date","ratio","ret_20","ret_60","mae_60","ret_245","mae_245"]], ["ret_20","ret_60","mae_60","ret_245","mae_245"])}
<h2>基准日不是无关细节</h2>
<p>若都归一化到2004-12-31=1，则严格交点相当于原始R≈0.9742，<b>2018年交点消失</b>；若归一化到2005-01-04，2018和2024均没有严格交点。从2010开始归一化，两线长时间不相交。下表的0次可能代表整年低于基准阈值，不等于整年高于阈值。因此原图必须锁定原始点位和同一纵轴，其视觉规律不能无条件外推。</p>
{table(pd.read_csv(OUT / "rebasing.csv"))}
<h2>对现役两池的增量诊断</h2>
<p>2015-01-05～2026-10-16，共2862日。现货池 slope20 long-flat，期货池 equal_weight 对称，各占总资金一半、每日按半仓计。两池均用中证500/1000各半的日价格收益作代理，现金收益0；年化245日、无风险收益0、每边3bp。信号日t之后，在t+1收盘执行；该日计成本，t+2开始获得完整收盘至收盘收益。再以t+2收盘执行作延迟检查。债基指数当日值何时完整公布尚未核实。</p>
<p><b>这些不是实际期货或账户回测收益。</b>未计贴水、展期、合约执行、融资和实际股票持仓；用途是同口径检查新增信号的信息，不能把表中年化当可兑现业绩。生产研究已反复使用历史数据，本次所有分期均属回看，不称为新样本外。</p>
<p><code>veto_short</code>：低位时只把期货空头改为空仓；<code>force_spot_long</code>：低位时把现货空仓改做多；<code>both</code>：两者同时。条件退出后恢复现役信号，未额外优化持有期。</p>
{table(results[["rule","variant","cagr","sharpe","maxdd","delta_cagr","delta_sharpe"]], ["cagr","maxdd","delta_cagr"])}
<p>严格规则下，2015年以来只有97个低位状态日，实际否决空头56日、强制现货持多52日。禁止空头的日收益增量之和约{concentration:.1%}来自2015～2016，不能说跨周期优势已成立。放宽到R≤1.03并同时改两池，最大回撤从17.88%扩大至22.56%；2024年1月恰是大盘与中小盘风险不同步的反例。沪深300接近债基不等于我们的500/1000持仓已经止跌。</p>
{table(pd.read_csv(OUT / "signal_overlap.csv"))}
<h2>分期、延迟和统计区间</h2>
{table(inc[inc.lag.eq(2) & inc.rule.isin(["1.0","1.03"]) & inc.variant.eq("veto_short")][["rule","period","delta_cagr","delta_sharpe","mean_delta_annual"]], ["delta_cagr","mean_delta_annual"])}
<p>再延迟一个交易日，严格禁止空头的夏普增量约从0.065降至0.025，现货强制多头从0.068降至0.019；严格规则同时改两池，夏普增量约从0.094降至0.004。严格R≤1禁止空头的两种配对分块bootstrap区间为正，是弱正面证据，应如实保留；但区间未调整阈值、样本与现役信号历史筛选，不能替代独立前瞻验证。3%规则区间均跨0。</p>
{table(bt)}
<h2>漏报检查（事后定义，不进入策略）</h2>
<p>定义为沪深300全收益前后各60日局部最低，且比过去245日高点跌至少20%；检查低点及此前60日是否进入信号区。采用事后居中窗口，仅用于评价覆盖，绝不作为可提前获得的触发条件。有限窗口不涵盖所有社会叙事中的“大底”。</p>
{table(pd.read_csv(OUT / "expost_trough_coverage.csv"), ["drawdown_245"])}
<h2>证据边界与处置</h2>
<p>当前建议：保留R及“接近/跌穿”观察标签；不将其当成底部保证，不新增强制做多规则。若继续研究，先固定原始指数口径、实时发布日期和唯一否决规则，再保留真正未用过的前瞻记录。暂不变更自动仓位和生产数据日更。2026-10-16的R≈{summary['latest_ratio']:.4f}，当前也未触发这些低位阈值。</p>
<p>基金指数历史成分、修订与点时数据未获得证明；公开材料称该债基指数2013-12-31发布，但本次未核对Wind原始发布公告，早期历史不能作为当时可实时交易的证明。对应代码和年度收益已与公开材料及流传帖子点位交叉核对。Wind本次历史查询估算100000左右自然日字段单元格，另小探针12单元格；准确预算见protocol.json，调用带项目归属头。未从本地计数推断供应商剩余额度。</p>
<img src="data:image/png;base64,{image_data}" alt="历史点位、相对比值及现役信号增量曲线">
<h2>来源与复核文件</h2>{links}
<p>原始响应：<a href="wind_raw.json">wind_raw.json</a>；比值序列：<a href="daily_ratio.csv">daily_ratio.csv</a>；全部事件：<a href="events.csv">events.csv</a>；日收益：<a href="strategy_daily_returns.csv">strategy_daily_returns.csv</a>；校验：<a href="manifest.json">SHA256清单</a>。</p>
<p>复现（仓库根，离线）：<code>bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 tools/research_equity_bond_crossing.py'</code>。脚本默认不联网；仅显式--fetch会取数。3个小型数值测试覆盖事件边界、成交滞后和翻仓成本。研究产物不在生产output镜像目录。</p>
</html>"""
    (OUT / "report.html").write_text(report, encoding="utf-8")


def analyze():
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw = pd.read_csv(OUT / "wind_raw.csv", parse_dates=["trade_date"])
    assert not raw.duplicated(["ts_code", "trade_date"]).any()
    wide = raw.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    coverage = {c: {"valid": int(wide[c].notna().sum()), "missing": int(wide[c].isna().sum()),
                    "first_valid": str(wide[c].first_valid_index().date()),
                    "last_valid": str(wide[c].last_valid_index().date())} for c in wide}
    x = wide.dropna()
    assert np.isfinite(x.values).all() and (x.values > 0).all()
    assert wide.loc[x.index[0]:].notna().all().all(), "internal missing dates"
    eq, bond = x["H00300.CSI"], x["885007.WI"]
    ratio = eq / bond
    pd.DataFrame({"equity": eq, "bond": bond, "ratio": ratio}).to_csv(OUT / "daily_ratio.csv", index_label="date")
    events = []
    for threshold in (0.97, 1.0, 1.03, 1.05):
        for i in entry_indices(ratio, threshold):
            row = {"threshold": threshold, "date": x.index[i], "ratio": ratio.iloc[i]}
            for h in (20, 60, 120, 245):
                if i + h < len(eq):
                    path = eq.iloc[i+1:i+h+1] / eq.iloc[i] - 1
                    row[f"ret_{h}"] = eq.iloc[i+h] / eq.iloc[i] - 1
                    row[f"mae_{h}"] = min(0., path.min())
                    row[f"low_date_{h}"] = path.idxmin()
                    row[f"ret_next_close_{h}"] = (eq.iloc[i+1+h] / eq.iloc[i+1] - 1) if i+1+h < len(eq) else np.nan
                else:
                    row[f"ret_{h}"] = row[f"mae_{h}"] = row[f"ret_next_close_{h}"] = np.nan
            events.append(row)
    ev = pd.DataFrame(events)
    ev.to_csv(OUT / "events.csv", index=False)
    event_summary = []
    for t, group in ev.groupby("threshold"):
        for h in (20, 60, 120, 245):
            valid = group.dropna(subset=[f"ret_{h}"])
            unconditional = eq.shift(-h) / eq - 1
            event_summary.append({"threshold": t, "horizon": h, "n": len(valid),
                                  "mean_return": valid[f"ret_{h}"].mean(),
                                  "median_return": valid[f"ret_{h}"].median(),
                                  "win_rate": valid[f"ret_{h}"].gt(0).mean(),
                                  "mean_adverse_excursion": valid[f"mae_{h}"].mean(),
                                  "worst_adverse_excursion": valid[f"mae_{h}"].min(),
                                  "adverse_worse_than_5pct": valid[f"mae_{h}"].lt(-.05).mean(),
                                  "unconditional_mean": unconditional.mean(),
                                  "unconditional_win_rate": unconditional.dropna().gt(0).mean()})
    es = pd.DataFrame(event_summary)
    es.to_csv(OUT / "event_summary.csv", index=False)
    annual = []
    for year, ry in ratio.groupby(ratio.index.year):
        strict = ((ratio <= 1) & (ratio.shift() > 1)).loc[str(year)]
        near = ((ratio <= 1.03) & (ratio.shift() > 1.03)).loc[str(year)]
        min_date = ry.idxmin()
        annual.append({"year": year, "min_ratio": ry.min(), "min_ratio_date": min_date,
                       "max_ratio": ry.max(), "days_below_1": int(ry.le(1).sum()),
                       "raw_crosses": int(strict.sum()), "first_cross": strict.index[strict][0] if strict.any() else None,
                       "first_near_cross": near.index[near][0] if near.any() else None})
    annual = pd.DataFrame(annual)
    annual.to_csv(OUT / "annual_audit.csv", index=False)
    # Ex-post coverage diagnostic only: these centered troughs never enter a strategy.
    troughs = []
    rolling_dd = eq / eq.rolling(245, min_periods=245).max() - 1
    local_min = eq.eq(eq.rolling(121, center=True, min_periods=121).min()) & rolling_dd.le(-.20)
    for d in eq.index[local_min]:
        i = eq.index.get_loc(d)
        troughs.append({"date": d, "drawdown_245": rolling_dd.loc[d], "ratio_at_trough": ratio.loc[d],
                        "strict_zone_in_prior_60d": bool(ratio.iloc[max(0,i-60):i+1].le(1).any()),
                        "near_zone_in_prior_60d": bool(ratio.iloc[max(0,i-60):i+1].le(1.03).any())})
    pd.DataFrame(troughs).to_csv(OUT / "expost_trough_coverage.csv", index=False)
    rebase_rows = []
    for base in ("2004-12-31", "2005-01-04", "2010-01-04", "2014-01-02"):
        rb = ratio.loc[base:] / ratio.loc[base]
        for year in (2012, 2016, 2018, 2022, 2024):
            ry = rb[rb.index.year == year]
            if ry.empty:
                continue
            mask = (rb.le(1) & rb.shift().gt(1))
            rebase_rows.append({"base": base, "year": year, "raw_ratio_threshold": ratio.loc[base],
                                "min_rebased_ratio": ry.min(), "cross_count": int(mask[mask.index.year == year].sum())})
    pd.DataFrame(rebase_rows).to_csv(OUT / "rebasing.csv", index=False)

    # Same 500/1000 exposure for both pools; this isolates timing information.
    # It is explicitly NOT a realizable futures backtest (no carry/roll/contracts).
    pg = pd.read_csv(OUT / "pg_benchmarks.csv", parse_dates=["date"])
    bench = pg.pivot(index="date", columns="code", values="close").sort_index()
    positions = {}
    for name in ("slope20_longflat", "equal_weight_symmetric"):
        positions[name] = pd.read_csv(OUT / f"{name}.csv", parse_dates=["date"]).set_index("date")["position"]
    pos = pd.DataFrame(positions).reindex(x.index)
    calendar = x.index[x.index >= "2015-01-01"]
    assert pos.loc[calendar].notna().all().all()
    assert bench.reindex(calendar).notna().all().all()
    und = bench.pct_change(fill_method=None)[["000905.SH", "000852.SH"]].mean(axis=1, skipna=False).reindex(x.index)

    def pnl(p, lag):
        return proxy_returns(p, und, lag=lag)

    rows, all_daily, masks = [], {}, {}
    correlations = {}
    dd = eq / eq.rolling(245, min_periods=245).max() - 1
    for name, p in positions.items():
        factor = p.reindex(ratio.index)
        correlations[name] = float(ratio.loc[calendar].corr(factor.loc[calendar], method="spearman"))
    correlations["ratio_vs_245d_drawdown_spearman"] = float(ratio.loc[calendar].corr(dd.loc[calendar], method="spearman"))
    overlap = []
    for threshold in (0.97, 1.0, 1.03, 1.05):
        m = ratio.le(threshold)
        masks[str(threshold)] = m
        eligible = m.loc[calendar]
        overlap.append({"threshold": threshold, "state_days": int(eligible.sum()),
                        "spot_flat_days": int((eligible & pos.loc[calendar, "slope20_longflat"].eq(0)).sum()),
                        "futures_short_days": int((eligible & pos.loc[calendar, "equal_weight_symmetric"].lt(0)).sum())})
    pd.DataFrame(overlap).to_csv(OUT / "signal_overlap.csv", index=False)
    masks["drawdown20"] = dd.le(-.20)
    for lag in (2, 3):
        s0, f0 = pos["slope20_longflat"], pos["equal_weight_symmetric"]
        base_s, base_f = pnl(s0, lag), pnl(f0, lag)
        base = (base_s + base_f) / 2
        all_daily[f"baseline_lag{lag}"] = base
        for period, lo, hi in (("all", "2015-01-01", END), ("2015_2020", "2015-01-01", "2020-12-31"),
                               ("2021_2023", "2021-01-01", "2023-12-31"), ("2024_2026", "2024-01-01", END)):
            b = base.loc[lo:hi].dropna()
            rows.append({"lag": lag, "rule": "baseline", "variant": "baseline", "period": period, **metrics(b), "delta_cagr": 0., "delta_sharpe": 0., "mean_delta_annual": 0.})
        for label, m in masks.items():
            ss = s0.mask(m, 1.)
            ff = f0.mask(m & f0.lt(0), 0.)
            for variant, a, b in (("veto_short", s0, ff), ("force_spot_long", ss, f0), ("both", ss, ff)):
                candidate = (pnl(a, lag) + pnl(b, lag)) / 2
                key = f"{label}_{variant}_lag{lag}"
                all_daily[key] = candidate
                for period, lo, hi in (("all", "2015-01-01", END), ("2015_2020", "2015-01-01", "2020-12-31"),
                                       ("2021_2023", "2021-01-01", "2023-12-31"), ("2024_2026", "2024-01-01", END)):
                    cr, br = candidate.loc[lo:hi].dropna(), base.loc[lo:hi].dropna()
                    cm, bm = metrics(cr), metrics(br)
                    rows.append({"lag": lag, "rule": label, "variant": variant, "period": period, **cm,
                                 "delta_cagr": cm["cagr"]-bm["cagr"], "delta_sharpe": cm["sharpe"]-bm["sharpe"],
                                 "mean_delta_annual": (cr-br).mean()*245})
    strategies = pd.DataFrame(rows)
    strategies.to_csv(OUT / "incremental_strategies.csv", index=False)
    daily = pd.DataFrame(all_daily).loc[calendar]
    assert daily.notna().all().all()
    daily.to_csv(OUT / "strategy_daily_returns.csv", index_label="date")
    # Paired circular moving-block intervals, descriptive only, no selection correction.
    rng = np.random.default_rng(20261019)
    bootstrap = []
    base = daily["baseline_lag2"].to_numpy()
    keys = [f"{t}_{v}_lag2" for t in ("1.0", "1.03") for v in ("veto_short", "force_spot_long", "both")]
    candidates = daily[keys].to_numpy()
    for block in (60, 245):
        draws = []
        for _ in range(2000):
            starts = rng.integers(0, len(base), size=int(np.ceil(len(base)/block)))
            ix = ((starts[:, None] + np.arange(block)) % len(base)).ravel()[:len(base)]
            b, c = base[ix], candidates[ix]
            ds = (c.mean(axis=0)/c.std(axis=0, ddof=1) - b.mean()/b.std(ddof=1))*np.sqrt(245)
            draws.append(ds)
        bounds = np.quantile(draws, [.025, .975], axis=0)
        for j, key in enumerate(keys):
            bootstrap.append({"strategy": key, "block": block, "draws": 2000, "delta_sharpe_lo": bounds[0,j], "delta_sharpe_hi": bounds[1,j]})
    pd.DataFrame(bootstrap).to_csv(OUT / "paired_bootstrap.csv", index=False)
    yearly = []
    for key in keys:
        delta = daily[key] - daily["baseline_lag2"]
        for year, g in delta.groupby(delta.index.year):
            yearly.append({"strategy": key, "year": year, "sum_daily_delta": g.sum(), "changed_return_days": int(g.ne(0).sum())})
    pd.DataFrame(yearly).to_csv(OUT / "increment_by_year.csv", index=False)
    # Chart uses one shared y axis; no chart-specific scaling.
    plt.rcParams["font.family"] = "Noto Sans CJK JP"
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(3, 1, figsize=(13, 11), constrained_layout=True)
    axes[0].plot(x.index, eq, label="沪深300全收益 H00300.CSI", lw=1)
    axes[0].plot(x.index, bond, label="万得混合债券型二级 885007.WI", lw=1.4)
    axes[0].set_title("原始点位叠加：相交可以接近低位，但不保证立即止跌")
    axes[0].set_ylabel("原始指数点位（同一纵轴）")
    axes[0].legend(loc="upper left")
    axes[1].plot(x.index, ratio, lw=1, color="#365c8d", label="原始点位之比")
    axes[1].axhline(1, color="#9c3030", lw=1, label="严格交点 1.00")
    axes[1].axhline(1.03, color="#b37d1d", ls="--", lw=.8, label="接近区 1.03")
    primary = ev[ev.threshold.eq(1.)]
    axes[1].scatter(primary.date, primary.ratio, s=20, c="#9c3030", zorder=3, label="下穿，60交易日去重")
    axes[1].set_ylim(.7, 1.8)
    axes[1].set_ylabel("比值（上限截至1.8）")
    axes[1].legend(loc="upper right", ncol=2)
    for key, label in (("baseline_lag2", "现役两池指数代理"), ("1.0_veto_short_lag2", "比值≤1时禁止空头"), ("1.0_force_spot_long_lag2", "比值≤1时现货强制做多")):
        axes[2].plot(daily.index, (1+daily[key]).cumprod(), label=label, lw=1.2)
    axes[2].set_yscale("log")
    axes[2].set_ylabel("累计净值（对数轴）")
    axes[2].set_title("增量诊断：次日收盘执行、单边3bp；500/1000各半，无期货贴水")
    axes[2].legend(loc="upper left")
    for ax in axes:
        ax.grid(alpha=.2)
    fig.savefig(OUT / "audit_chart.png", dpi=150)
    plt.close(fig)
    summary = {"coverage": coverage, "common_rows": len(x), "common_start": str(x.index[0].date()),
               "common_end": str(x.index[-1].date()), "latest_ratio": ratio.iloc[-1],
               "raw_downcrossings": int((ratio.le(1)&ratio.shift().gt(1)).sum()),
               "primary_events": len(primary), "correlations": correlations,
               "limitations": ["retrospective full sample; no untouched holdout", "fund-index point-in-time historical composition and publication timestamp not verified", "pre-release history may be backfilled", "event horizons overlap and cooldown does not create independent cycles", "all returns use close prices; intraday lows may be lower", "proxy strategies omit futures basis/carry/roll and actual stock holdings", "bootstrap intervals are exploratory and unadjusted for candidate selection"]}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name not in ("manifest.json", "report.html")}
    manifest["research_script"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_report()
    print("SUMMARY", json.dumps(summary, ensure_ascii=False))
    print("PRIMARY EVENTS\n", primary[["date", "ratio", "ret_20", "ret_60", "mae_60", "ret_245", "mae_245"]].round(4).to_string(index=False))
    print("EVENT SUMMARY\n", es[es.threshold.eq(1.)].round(4).to_string(index=False))
    print("INCREMENT\n", strategies[(strategies.lag.eq(2)) & strategies.period.eq("all")].round(4).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    if args.fetch:
        fetch()
    else:
        analyze()
