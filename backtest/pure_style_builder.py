"""中证「纯风格」（P 族）编制方案的自建实现 —— 可指向任意市值带。

预登记 = `docs/plans/2026-08-18-fifth-bucket-preregistration.md`（跑前冻结）。
**Gate 0 与尾部桶共用本模块**（预登记 §1 要求"用完全相同的代码与参数"）。

## 方法论出处

`/home/elfbob/exchange/20260702/指数编制方案/中证2000纯成长（纯价值）指数编制方案.pdf`
（2025-01 V1.0）。规则逐条照抄，见预登记 §3。

## 已登记的口径替代（预登记附录 A.2）

1. **中信一级行业代替中证行业分类**（库内无 CSI 类型）。金融 = 银行 / 非银行金融 / 综合金融；
   **房地产不算金融**（中证一级行业里房地产是独立板块）。
2. **`circ_mv`（流通市值）代替自由流通市值**（logistic 平滑的 25/50/75% 分位点）。
3. 样本空间用 **`total_mv` 排名带**代替官方成分（库内无四母指数历史成分）。
   → 2026-08-19 起提供第二条路径 `official_sample_space`（模拟官方选样，见下节）。

## 官方选样时间线（2026-08-19 补，用户指出的方向）

规模指数系列的考察窗**不是**「调样日往回一年」：《沪深300指数编制方案》(2023-09) §6.2
（系列通用架构）规定 6 月调样以**上年 5-01 → 当年 4-30**、12 月调样以**上年 11-01 →
当年 10-31** 的交易数据**及财务数据**为审核依据；期间新上市证券自**上市第六个交易日**
起算。`review_cutoff()` 给出该截止日；`official_sample_space()` 按《中证2000指数编制
方案》(V1.1) 模拟选样：中证全指样本空间（非 ST、分板上市时长、含北交所）→ 日均成交额
前 90% → 剔 000905/000852 实际成分 + 剔日均总市值前 1500 → 日均总市值取前 2000 →
缓冲区（1600 进 / 2400 保）。偏离清单见
`docs/plans/2026-08-19-replication-improvement-plan.md` §2。

## PIT

可知日（设计稿 = `docs/plans/2026-08-24-first-disclosure-pit-upgrade.md` §1 + §4.1）：
定期报告类报表行 = **`stock_first_disclosure.first_disclosure_date`（真实首披日）优先**
（须过有效性守卫：`quality=='ok'` 且首披日 ≥ 报告期末），未覆盖行回退**纯法定披露截止日**
（2026-08-25 用户裁决，与选股仓 `resolve_financial_availability` 统一）；
dividend 事件行豁免（选股仓无对应物，且红利可知日直接决定 DP 的 12 个月窗），
维持旧 `min(ann_date, 截止日)`。取 `<= 调样日` 的财务行（见 `_fetch_series` / `_knowability`）。

⚠️ 直接用 `stock_financial.ann_date` 是**错的**：96.5% 来自 CSMAR，其 `ann_date` 是数据集
批次日而非首披日，会在每个调样日丢掉最近整整一个季度（实测 Q1 通过率 0~1%），把成长因子
打成噪声。而它在 Wind 段又被上游封顶成 `min(真值, 截止日)`、真值已丢——**语义分段污染
且上游明确不修**，故 08-25 起它完全退出定期报告路径。法定截止日是真实披露日的**上界**，
故该回退保守而非前视。

⚠️ **残余限制**：超期披露的公司会被当成按时披露（无对照字段可逐票剔除）→ 结论仍须标
**provisional**（同 B3）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.common.factors import growth_slope, pit_ttm_with_known  # noqa: E402

#: 中信一级行业 → 视作「金融」（价值分剔 CF/P、除 √3）。房地产**不**计入。
FINANCE_INDUSTRIES = frozenset({"银行", "非银行金融", "综合金融"})
WINSOR = (0.05, 0.95)          # 极值调整（上证全指方案明文；纯风格方案称"极值调整"未给分位）
GROWTH_QUARTERS = 12           # SalG/ProG：过去三年季度滚动
FLOAT_QUANTILES = (0.25, 0.50, 0.75)
WEIGHT_CAP, TOP5_CAP = 0.15, 0.60


def _conn():
    import psycopg2
    db = load_db_config()
    c = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                         user=db["user"], password=db["password"], connect_timeout=15)
    return c, db["schema"]


# ---------------------------------------------------------------- 调样日
def rebalance_dates(start: str, end: str) -> list[pd.Timestamp]:
    """每年 6 月和 12 月**第二个星期五的下一交易日**（中证口径）。

    交易日取 `index_daily` 里 000300.SH 的日期集合（该指数全历史连续）。
    """
    c, schema = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT trade_date FROM {schema}.index_daily
                            WHERE index_code='000300.SH' ORDER BY trade_date""")
            days = pd.DatetimeIndex([r[0] for r in cur.fetchall()])
    finally:
        c.close()
    out = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        for m in (6, 12):
            fridays = pd.date_range(f"{y}-{m:02d}-01", f"{y}-{m:02d}-28", freq="W-FRI")
            if len(fridays) < 2:
                continue
            nxt = days[days > fridays[1]]
            if len(nxt):
                out.append(nxt[0])
    return [d for d in out if pd.Timestamp(start) <= d <= pd.Timestamp(end)]


def review_cutoff(eff: pd.Timestamp) -> pd.Timestamp:
    """调样生效日 → 数据考察截止日（沪深300方案 §6.2，规模指数系列通用）。

    6 月生效 → 当年 4-30；12 月生效 → 当年 10-31。考察窗 = 截止日往回一年
    （原文「上一年度 5 月 1 日至审核年度 4 月 30 日」）。截止日恰为月末 →
    `stock_indicator` 的月末快照（2015-2024 仅月末有行）正好覆盖。
    """
    if eff.month not in (5, 6, 7, 11, 12, 1):
        raise ValueError(f"非常规调样生效月: {eff}")
    if eff.month in (5, 6, 7):
        return pd.Timestamp(eff.year, 4, 30)
    y = eff.year if eff.month in (11, 12) else eff.year - 1   # 1 月生效属上年 12 月批
    return pd.Timestamp(y, 10, 31)


@dataclass(frozen=True)
class OfficialBand:
    """一个官方规模带的选样参数（各带编制方案原文转录，勿互相套用）。

    r3 预登记附录 A 实测教训：中证1000 与中证2000 有三处实质不同——样本空间
    （1000 仅沪深，2000 含北交所）、成交额筛（1000 剔后 20%，2000 剔后 10% 即前 90%）、
    缓冲区（800/1200 vs 1600/2400）。"""
    name: str
    adv_trim: float            # 剔除窗内日均成交额排名后 X%（分位）
    mv_prescreen: int          # 剔除窗内日均总市值排名前 N
    excl_indices: tuple        # 联动剔除的上层指数真值成分（index_code）
    target: int                # 待选按日均总市值取前 N
    buf_in: int                # 缓冲区：新样本优先进入线
    buf_keep: int              # 缓冲区：老样本优先保留线
    include_bj: bool           # 样本空间是否含北交所


#: 《中证2000指数编制方案》V1.1 2023-12（原有实现的参数原样固化）
BAND_2000 = OfficialBand("2000", 0.10, 1500, ("000905.SH", "000852.SH"), 2000, 1600, 2400, True)
#: 《中证1000指数编制方案》V1.1 2023-12（r3 预登记附录 A）。剔中证800：000905 真值 +
#: 「剔日均总市值前 300」排名筛结构性覆盖 300 区域；000300 真值库内仅 8 期（2026 起），
#: 可用期一并剔除，缓冲区边角票的漏网量由 000852 横截面验收量化（r3 §4.1 登记）。
BAND_1000 = OfficialBand("1000", 0.20, 300, ("000905.SH", "000300.SH"), 1000, 800, 1200, False)

#: 样本空间交易所护栏（中证全指 = 沪深 + 北交）。库内三张表各有 600~700 只 `.HK` 行，
#: 混入会污染排名筛。0R 锚复现阶段可临时补 ".HK" 以还原 08-19 锚值口径（gate0_runner）。
_EXCHANGE_SUFFIXES = (".SH", ".SZ", ".BJ")

#: 尾部桶（第 5 桶，r3 预登记 §2）：官方链余集，无自身缓冲区。
TAIL_MV_PRESCREEN = 3500       # 剔窗内日均总市值排名前 3500（官方递推算术 3800−300）
TAIL_ADV_TRIM = 0.10           # 全空间成交额前 90%（沿中证2000 链）
TAIL_INNER_ADV_TRIM = 0.20     # 带内再剔窗内日均成交额后 20%（r1 冻结值沿用）
TAIL_MOTHER_TRUTH = ("000905.SH", "000852.SH", "000300.SH")   # 932000 单独走真值-否则-模拟


def _apply_buffer(ranked: list[str], prev: set[str] | None, target: int = 2000,
                  in_rank: int = 1600, keep_rank: int = 2400) -> list[str]:
    """中证2000 定调缓冲区。`prev=None` → 纯排名前 `target`。

    语义（2026-08-19 由官方实际行为反推校正）：**「老样本优先保留」的优先级高于
    「新样本优先进入」** —— 先保留待选内排名 ≤`keep_rank` 的全部老样本，剩余名额按
    待选排名依次填充（含排名 >`keep_rank` 的旧样本，此时按普通候选竞争）。
    证据：2026-06 期官方仅调整 232/2000（11.6%），且保留了大量排名 2100–2400 段的
    老样本、同时新进排名 >1600 的新样本 —— 与「冲突时按总排名裁」的读法不相容，
    与本语义相容。`in_rank` 线在本语义下由排名填充自动满足（新样本排名越前越先进）。

    `ranked` = 待选样本按过去一年日均总市值**降序**。返回保持该排序。
    """
    if prev is None:
        return ranked[:target]
    old_keep = [c for c in ranked[:keep_rank] if c in prev]
    if len(old_keep) >= target:
        sel = set(old_keep[:target])
        return [c for c in ranked if c in sel]
    quota = target - len(old_keep)
    kept = set(old_keep)
    filled = [c for c in ranked if c not in kept][:quota]
    sel = kept | set(filled)
    return [c for c in ranked if c in sel]


# ---------------------------------------------------------------- 样本空间
def _avg_mv_1y_daily(px_rows: list, sh_rows: list) -> pd.Series:
    """过去 1 年**日频**日均总市值 = mean(close × 前向填充的 total_shares)。

    ## 为什么不用 `stock_indicator` 的 `avg(total_mv)`

    该表 **2015–2024 只有月末数据**（每年仅 12 个交易日有行），`avg` 只由 12 个点得到；
    且 **2025-09~2026-03 整段近乎空**（31 行/日）。而 `avg_mv_1y` 是 **EP/CFP/BP/DP 四个
    价值因子的公共分母**（原文："与过去 1 年日均总市值的比值"）。横截面实测：把分母从
    这个 12 点均值换成当日 `total_mv`，对官方成分的 top-N 命中就从 50.3% 升到 52.6%。

    ## 口径校验

    `close × total_shares` 与库内 `total_mv` **完全一致**（2024-05-31 抽 286 只：中位比值
    1.000，96.5% 落在 ±1% 内）→ 单位无需换算。

    ⚠️ 股本按 `effective_date` 前向填充（= 当时真实在外股本）；无股本记录的交易日不计入
    均值，整只无记录则返回 NaN，由调用方回退到月末口径。
    """
    if not px_rows:
        return pd.Series(dtype=float)
    px = pd.DataFrame(px_rows, columns=["ts_code", "trade_date", "close"])
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    if not sh_rows:
        return pd.Series(dtype=float)
    sh = pd.DataFrame(sh_rows, columns=["ts_code", "effective_date", "total_shares"])
    sh["effective_date"] = pd.to_datetime(sh["effective_date"])
    px = px.sort_values("trade_date")
    sh = sh.sort_values("effective_date")
    m = pd.merge_asof(px, sh, left_on="trade_date", right_on="effective_date",
                      by="ts_code", direction="backward")
    m["mv"] = m["close"] * m["total_shares"]
    return m.groupby("ts_code")["mv"].mean()


def sample_space(asof: pd.Timestamp, rank_lo: int, rank_hi: int | None,
                 min_list_years: float = 1.0, liq_drop_pct: float = 0.20,
                 codes: list[str] | None = None, apply_filters: bool = True) -> pd.DataFrame:
    """调样日的样本空间：市值排名带 → 剔新股 → 剔流动性尾部。

    `rank_lo`/`rank_hi` 为 1-based 闭区间（`rank_hi=None` 表示到最后）。

    `codes` 给定时**用这份显式名单代替排名带**（用于官方样本空间 = 母指数成分的复现；
    此时 `rank_lo/rank_hi` 被忽略）。`apply_filters=False` 跳过剔新股/剔流动性两道筛——
    母指数成分已自带这些约束，再筛一次就偏离了原文的"样本空间 = XX指数样本"。

    ⚠️ 尾部桶（3801+）**没有官方成分可用**，只能走排名带代理 → 闸门必须也用代理版判定，
    否则闸门检验不到将要走的那条路径（见预登记 r2 §1c）。

    返回 index=ts_code，列 `total_mv / circ_mv / avg_mv_1y / avg_close_1y / adv_1y / industry`。
    """
    d = asof.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            # ⚠️ 不能直接取 max(trade_date)：`stock_indicator` 存在**残缺快照**
            # （实测 2025-12-15 全天只有 26 行，正常约 5,200；2025-09~2026-03 整段如此）。
            # 旧写法拿到残缺日就直接用 → 样本空间 0 只 → build_pair 里静默 continue，
            # Gate 0 正式跑因此少建了整整一期（2025-12-15→2026-06-15）而无任何告警。
            # 改为：在过去 400 天内，取**行数达到该窗口峰值一半**的最近一个交易日。
            # codes 显式给定时不做交易所过滤（官方名单可含北交所 .BJ；排名带路径维持 .SH/.SZ）
            exch = "TRUE" if codes is not None else "(ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ')"
            cur.execute(f"""WITH cnt AS (
                              SELECT trade_date, count(*) n FROM {s}.stock_indicator
                              WHERE trade_date<=DATE '{d}' AND trade_date>DATE '{d}'-400
                                AND total_mv IS NOT NULL
                                AND {exch}
                              GROUP BY 1)
                            SELECT ts_code, total_mv::float8, circ_mv::float8
                            FROM {s}.stock_indicator
                            WHERE trade_date=(SELECT max(trade_date) FROM cnt
                                              WHERE n >= 0.5*(SELECT max(n) FROM cnt))
                              AND total_mv IS NOT NULL
                              AND {exch}""")
            snap = pd.DataFrame(cur.fetchall(), columns=["ts_code", "total_mv", "circ_mv"])
            snap = snap.sort_values("total_mv", ascending=False).set_index("ts_code")
            band = (snap.reindex(codes).dropna(subset=["total_mv"]).copy()
                    if codes is not None else snap.iloc[rank_lo - 1: rank_hi].copy())
            codes = list(band.index)
            if not codes:
                return band.assign(avg_mv_1y=np.nan)

            cur.execute(f"""SELECT ts_code, avg(total_mv)::float8 FROM {s}.stock_indicator
                            WHERE ts_code=ANY(%s) AND trade_date BETWEEN DATE '{d}'-INTERVAL '365 days'
                              AND DATE '{d}' GROUP BY 1""", (codes,))
            band["avg_mv_1y_monthend"] = pd.Series(dict(cur.fetchall()))    # 兜底用

            # 日频口径：close × 前向填充的 total_shares（见 _avg_mv_1y_daily 的 docstring）
            cur.execute(f"""SELECT ts_code, trade_date, close::float8 FROM {s}.stock_daily_price
                            WHERE ts_code=ANY(%s) AND trade_date BETWEEN DATE '{d}'-INTERVAL '365 days'
                              AND DATE '{d}' AND close IS NOT NULL AND pre_close>0 AND volume>0""",
                        (codes,))
            px_rows = cur.fetchall()
            cur.execute(f"""SELECT ts_code, effective_date, total_shares::float8
                            FROM {s}.stock_share_capital
                            WHERE ts_code=ANY(%s) AND effective_date<=DATE '{d}'
                              AND total_shares IS NOT NULL""", (codes,))
            sh_rows = cur.fetchall()
            band["avg_mv_1y"] = _avg_mv_1y_daily(px_rows, sh_rows).reindex(band.index)
            band["avg_mv_1y"] = band["avg_mv_1y"].fillna(band["avg_mv_1y_monthend"])

            cur.execute(f"""SELECT ts_code, avg(close)::float8, avg(amount)::float8, count(*)
                            FROM {s}.stock_daily_price
                            WHERE ts_code=ANY(%s) AND trade_date BETWEEN DATE '{d}'-INTERVAL '365 days'
                              AND DATE '{d}' AND close IS NOT NULL AND pre_close>0 AND volume>0
                            GROUP BY 1""", (codes,))
            px = pd.DataFrame(cur.fetchall(),
                              columns=["ts_code", "avg_close_1y", "adv_1y", "n_days"]).set_index("ts_code")
            band = band.join(px)

            cur.execute(f"""SELECT ts_code, list_date FROM {s}.stock_meta WHERE ts_code=ANY(%s)""", (codes,))
            band["list_date"] = pd.to_datetime(pd.Series(dict(cur.fetchall())), errors="coerce")

            cur.execute(f"""SELECT DISTINCT ON (ts_code) ts_code, level_1_name
                            FROM {s}.industry_classification
                            WHERE ts_code=ANY(%s) AND classification_type='CITIC'
                              AND effective_date<=DATE '{d}'
                            ORDER BY ts_code, effective_date DESC""", (codes,))
            band["industry"] = pd.Series(dict(cur.fetchall()))
    finally:
        c.close()

    if not apply_filters:
        return band
    band = band[band["list_date"].notna()]
    band = band[(asof - band["list_date"]).dt.days >= min_list_years * 365]   # 剔新股
    band = band[band["adv_1y"].notna()]
    band = band[band["adv_1y"] >= band["adv_1y"].quantile(liq_drop_pct)]       # 剔流动性尾部
    return band


def _space_frame(eff: pd.Timestamp) -> pd.DataFrame:
    """官方化中证全指样本空间的个股帧（考察窗口径），index=ts_code，列 avg_mv / adv / is_bj。

    资格筛（各带共用）：非 ST/*ST；上市时长分板（科创 >1 年、北交 >2 年、其他 >1 季度）；
    新上市证券「自上市第六个交易日以来」近似为剔 `list_date`+7 自然日前的行（登记裁量）；
    ST 状态取截止日（无行时向前找 ≤10 天）。**含北交所**（带层再按 `include_bj` 裁）。

    ⚠️ 交易所护栏：只留 `.SH/.SZ/.BJ`。库内 `stock_meta`/`stock_daily_price`/
    `stock_share_capital` 各有 600~700 只 `.HK` 行（08-19 实测）——中证全指样本空间为
    沪深+北交，`.HK` 混入会污染一切排名筛。
    """
    cutoff = review_cutoff(eff)
    d = cutoff.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, trade_date, close::float8, amount::float8
                            FROM {s}.stock_daily_price
                            WHERE trade_date > DATE '{d}' - INTERVAL '365 days'
                              AND trade_date <= DATE '{d}'
                              AND close IS NOT NULL AND pre_close>0 AND volume>0""")
            px = pd.DataFrame(cur.fetchall(), columns=["ts_code", "trade_date", "close", "amount"])
            cur.execute(f"""SELECT ts_code, effective_date, total_shares::float8
                            FROM {s}.stock_share_capital
                            WHERE effective_date <= DATE '{d}' AND total_shares IS NOT NULL""")
            sh = pd.DataFrame(cur.fetchall(), columns=["ts_code", "effective_date", "total_shares"])
            cur.execute(f"""SELECT ts_code, list_date FROM {s}.stock_meta""")
            meta = pd.DataFrame(cur.fetchall(), columns=["ts_code", "list_date"])
            cur.execute(f"""SELECT ts_code, is_st FROM {s}.stock_status
                            WHERE trade_date = (SELECT max(trade_date) FROM {s}.stock_status
                                                WHERE trade_date <= DATE '{d}'
                                                  AND trade_date > DATE '{d}' - 10)""")
            st = dict(cur.fetchall())
    finally:
        c.close()

    px["trade_date"] = pd.to_datetime(px["trade_date"])
    meta["list_date"] = pd.to_datetime(meta["list_date"], errors="coerce")
    meta = meta.dropna(subset=["list_date"]).set_index("ts_code")
    meta = meta[meta.index.str.endswith(tuple(_EXCHANGE_SUFFIXES))]  # 交易所护栏（剔 .HK 等）

    age = (cutoff - meta["list_date"]).dt.days
    is_star = meta.index.str.startswith(("688", "689"))
    is_bj = meta.index.str.endswith(".BJ")
    ok_age = pd.Series(np.where(is_star, age >= 365, np.where(is_bj, age >= 730, age >= 91)),
                       index=meta.index)
    eligible = set(meta.index[ok_age]) - {k for k, v in st.items() if v}

    px = px[px["ts_code"].isin(eligible)]
    px = px.merge(meta["list_date"], left_on="ts_code", right_index=True, how="left")
    px = px[px["trade_date"] >= px["list_date"] + pd.Timedelta(days=7)]

    sh["effective_date"] = pd.to_datetime(sh["effective_date"])
    sh = sh.sort_values("effective_date")
    m = pd.merge_asof(px.sort_values("trade_date"), sh,
                      left_on="trade_date", right_on="effective_date",
                      by="ts_code", direction="backward")
    m = m.dropna(subset=["total_shares"])
    m["mv"] = m["close"] * m["total_shares"]
    g = m.groupby("ts_code").agg(avg_mv=("mv", "mean"), adv=("amount", "mean"))
    g["is_bj"] = g.index.str.endswith(".BJ")
    return g


def _linked_members(index_code: str, eff: pd.Timestamp, cutoff: pd.Timestamp,
                    verbose: bool = False) -> set[str] | None:
    """联动剔除用的上层指数成分：优先取与 `eff` 同次审核生效的**新一期**名单
    （[eff, eff+45] 窗，官方生效前公告非前视），无则回退 cutoff 前最近一期并出声，
    全库都没有 → None 并出声（调用方决定模拟或跳过）。

    ⚠️ 窗下界**含当日**：wset 回补的 932000 名单 `effective_date` 恰等于调样生效日
    （2023-12-11 等，与官方成分表「纳入日期」一致）——曾写成 `>` 把当期名单跳过、
    整条回退旧一期（2026-08-19 Gate 0B 启动日志的出声告警暴露，未产生任何 ρ 前修复）。"""
    eff_d, d = eff.date().isoformat(), cutoff.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code FROM {s}.index_constituent
                            WHERE index_code=%s AND effective_date =
                              (SELECT min(effective_date) FROM {s}.index_constituent
                               WHERE index_code=%s AND effective_date >= DATE '{eff_d}'
                                 AND effective_date <= DATE '{eff_d}' + 45)""",
                        (index_code, index_code))
            leg = {r[0] for r in cur.fetchall()}
            if not leg:
                cur.execute(f"""SELECT ts_code FROM {s}.index_constituent
                                WHERE index_code=%s AND effective_date =
                                  (SELECT max(effective_date) FROM {s}.index_constituent
                                   WHERE index_code=%s AND effective_date <= DATE '{d}')""",
                            (index_code, index_code))
                leg = {r[0] for r in cur.fetchall()}
                if verbose and leg:
                    print(f"  ⚠️ {index_code} 无 {eff_d} 后名单，联动剔除回退到旧一期", flush=True)
    finally:
        c.close()
    if not leg:
        if verbose:
            print(f"  ⚠️ {index_code} 库内无任何 ≤{d} 名单，本期无法真值剔除", flush=True)
        return None
    return leg


def _select_band(g: pd.DataFrame, band: OfficialBand, excl: set[str],
                 prev: set[str] | None) -> tuple[list[str], int, int]:
    """帧 → 一个官方带的选样（纯函数）。返回 (picked, 全空间只数, 待选只数)。

    顺序与原实现一致：交易所/北交裁剪 → 成交额筛（带内分位）→ 日均总市值排名 →
    剔上层成分 + 排名预筛 → 缓冲区。
    成交额筛无老样本放宽——曾试沪深300 §6.4 式放宽（全免 / 95% 线），横截面重合率
    均净负（86.1% → 83.9% / 84.4%）已回滚（残差项，勿重试）。
    """
    if not band.include_bj:
        g = g[~g["is_bj"]]
    g = g[g["adv"].rank(pct=True, ascending=True) > band.adv_trim]
    g = g.sort_values("avg_mv", ascending=False)
    g = g.assign(rank_mv=np.arange(1, len(g) + 1))
    cand = g[(g["rank_mv"] > band.mv_prescreen) & (~g.index.isin(excl))]
    picked = _apply_buffer(list(cand.index), prev, target=band.target,
                           in_rank=band.buf_in, keep_rank=band.buf_keep)
    return picked, len(g), len(cand)


def official_sample_space(eff: pd.Timestamp, prev: set[str] | None = None,
                          verbose: bool = False, band: OfficialBand = BAND_2000,
                          frame: pd.DataFrame | None = None) -> list[str]:
    """模拟一个官方规模带的选样（默认中证2000，参数见 `OfficialBand`），
    返回按日均总市值降序的名单。

    时间线：一切数据以 `review_cutoff(eff)`（4-30 / 10-31）为界，考察窗 = 截止日往回一年
    （原文「上一年度 5 月 1 日至审核年度 4 月 30 日」）；`eff` 只用于定位截止日与联动名单。
    `frame` 给定时复用已算好的样本空间帧（同一 `eff` 内多带共用，见 `tail_sample_space`）。

    ⚠️ 2000 带「剔中证800 样本」不需要沪深300 历史成分：日均市值前 1500 的剔除覆盖
    300/500 全体（300 缓冲区至 360 名、500 至 ~1000 名，均 <1500），唯一漏网的中证1000
    缓冲尾巴由 000852 实际成分剔除。1000 带的对应论证见 `BAND_1000` 注释。
    """
    cutoff = review_cutoff(eff)
    g = _space_frame(eff) if frame is None else frame
    excl: set[str] = set()
    for idx in band.excl_indices:
        excl |= _linked_members(idx, eff, cutoff, verbose) or set()
    picked, n_all, n_cand = _select_band(g, band, excl, prev)
    if verbose:
        print(f"  official_sample_space[{band.name}] @{cutoff.date()}: 全空间 {n_all}，"
              f"待选 {n_cand}，选出 {len(picked)}"
              f"（prev={'None' if prev is None else len(prev)}）", flush=True)
    return picked


def _select_tail(g: pd.DataFrame, mothers: set[str],
                 mv_prescreen: int = TAIL_MV_PRESCREEN, adv_trim: float = TAIL_ADV_TRIM,
                 inner_adv_trim: float = TAIL_INNER_ADV_TRIM) -> tuple[list[str], int, int]:
    """帧 → 尾部桶（官方链余集，纯函数）。返回 (picked, 全空间只数, 剔成分+排名筛后只数)。

    r3 预登记 §2：含北交所 → 成交额前 90%（沿 2000 链）→ 剔四母成分 →
    剔日均总市值排名前 3500 → 带内再剔成交额后 20% → 余集全部保留，无缓冲区。
    """
    g = g[g["adv"].rank(pct=True, ascending=True) > adv_trim]
    g = g.sort_values("avg_mv", ascending=False)
    g = g.assign(rank_mv=np.arange(1, len(g) + 1))
    cand = g[(g["rank_mv"] > mv_prescreen) & (~g.index.isin(mothers))]
    keep = cand[cand["adv"].rank(pct=True, ascending=True) > inner_adv_trim]
    return list(keep.index), len(g), len(cand)


def tail_sample_space(eff: pd.Timestamp, prev2000: set[str] | None = None,
                      verbose: bool = False,
                      frame: pd.DataFrame | None = None) -> tuple[list[str], set[str]]:
    """尾部桶（第 5 桶）样本空间。返回 (picked, 本期中证2000 成员集)。

    四母剔除：000905/000852/000300 取库内真值（`_linked_members`，缺期回退并出声）；
    932000 优先真值（2023-08 起在库），**无真值期用官方化模拟**（`BAND_2000` + `prev2000`
    链——r3 §2-3，模拟保真已实测 truth−sim 仅 +0.007）。第二个返回值供调用方接力
    `prev2000` 链。成分数据洞（000300 仅 8 期、000852 有 stale 段）由排名前 3500 预筛
    结构性兜底（r3 §2-4）。
    """
    cutoff = review_cutoff(eff)
    g = _space_frame(eff) if frame is None else frame
    mothers: set[str] = set()
    for idx in TAIL_MOTHER_TRUTH:
        mothers |= _linked_members(idx, eff, cutoff, verbose) or set()
    m2000 = _linked_members("932000.CSI", eff, cutoff, verbose)
    if m2000 is None:
        prev = prev2000
        m2000 = set(official_sample_space(eff, prev, verbose, BAND_2000, frame=g))
        if verbose:
            print(f"  tail: 932000 无真值 → 官方化模拟 {len(m2000)} 只", flush=True)
    mothers |= m2000
    picked, n_all, n_cand = _select_tail(g, mothers)
    if verbose:
        n_bj = sum(c.endswith(".BJ") for c in picked)
        print(f"  tail_sample_space @{cutoff.date()}: 全空间 {n_all}，四母 {len(mothers)}，"
              f"剔成分+前{TAIL_MV_PRESCREEN} 后 {n_cand}，带内流动性筛后 {len(picked)}"
              f"（.BJ {n_bj}）", flush=True)
    return picked, set(m2000)


# ---------------------------------------------------------------- 因子面板
_STMT_FIELD = {
    "income": ("B001100000", "revenue"),
    "balance": ("A003000000", "equity"),
    "cashflow_direct": ("C001000000", "cfo"),
    "disclosed_indicators": ("F020102", "profit_ex"),      # 归母扣非净利润
    "profitability": ("F050501B", "roe"),                  # 净利润/股东权益余额
    "dividend": ("F100801A", "dps"),                       # 占位，见 _dividend_field
}
#: `dividend` 的每股税前现金红利在项目白名单里叫 `cash_dividend_ps_pre_tax`；
#: 其 CSMAR 编码从 `financial_field_map` 反查，避免在此处硬编码错。
def _dividend_field() -> str:
    from signals.common.financial_field_map import CSMAR_FIELD_MAPS
    for code, friendly in CSMAR_FIELD_MAPS.get("dividend", {}).items():
        if friendly == "cash_dividend_ps_pre_tax":
            return code
    raise KeyError("financial_field_map 里找不到 cash_dividend_ps_pre_tax")


#: R3 换源边界（2026-08-20）：CSMAR dividend 停更于 end_date 2025-03-31，2026 年起的
#: 调样日在 csmar 路径下拿不到 2025 年报分红 → D/P 自该边界起改取
#: `stock_indicator.dividend_yield`（日频直接比率，发布即可知）。边界前保持 csmar
#: 路径，且**单一截面只用单一源**（因子进截面 z，源间尺度差不影响排序）。
DP_INDICATOR_START = pd.Timestamp("2026-01-01")


def dp_source_for(asof: pd.Timestamp) -> str:
    """R3 拼接规则（纯函数）：调样日在 `DP_INDICATOR_START` 前用 csmar 红利事件行，
    此后用 stock_indicator.dividend_yield。"""
    return "indicator" if asof >= DP_INDICATOR_START else "csmar"


def dividend_ttm_events(df: pd.DataFrame, asof: pd.Timestamp,
                        window_days: int = 366) -> pd.Series:
    """红利**事件行**的滚动 12 个月每股税前股利（2026-08-20 缺陷修复）。

    ## 为什么不能走 `pit_ttm_with_known`
    红利是年度/半年度**事件序列**（每年 1~2 行、无季度 YTD 链），喂给按季差分的
    `pit_ttm_with_known` 会因单季不可构造而全量返回空——实测茅台/平安/格力在多个
    asof 全 EMPTY：**DP 因子在此前所有 Gate 0 运行中恒为 0（死因子）**，价值得分
    实际只由 BP/EP(/CFP) 构成，违背官方「价值 = D/P + B/P (+ CF/P) + E/P」规格。
    修复证据独立于 ρ（规格违背 + 空输出实测），符合修复台账纪律。

    ## 口径
    TTM 每股股利 = **修正可知日**落在 (asof − window, asof] 的事件行 value 之和
    （= 市场惯用「近 12 个月已宣告分红」，与 Wind dividend_yield 分子同口径；
    按可知日开窗而非所属期，避免年度+中期并存时的所属期重叠计数）。
    """
    if df.empty:
        return pd.Series(dtype=float)
    ann = pd.to_datetime(df["ann_date"])
    m = ann.notna() & (ann <= asof) & (ann > asof - pd.Timedelta(days=window_days))
    return df.loc[m].groupby("ts_code")["value"].sum()


def _fetch_dp_indicator(codes: list[str], asof: pd.Timestamp,
                        lookback_days: int = 30) -> pd.Series:
    """`stock_indicator.dividend_yield`（%）→ D/P：每票取 asof 前 `lookback_days`
    内最近一行；`dividend_yield` 为 NULL 视为未分红 → 0，无行的票留 NaN
    （由调用侧 fillna(0) 兜底，与 csmar 路径语义一致）。"""
    d = asof.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT ON (ts_code) ts_code, dividend_yield
                    FROM {s}.stock_indicator
                    WHERE ts_code = ANY(%s) AND trade_date <= DATE '{d}'
                      AND trade_date > DATE '{d}' - INTERVAL '{int(lookback_days)} days'
                    ORDER BY ts_code, trade_date DESC""",
                (codes,))
            rows = cur.fetchall()
    finally:
        c.close()
    out = pd.Series({r[0]: (float(r[1]) if r[1] is not None else 0.0) for r in rows},
                    dtype=float)
    return out


#: 真实首披日适用的报表类型（定期报告内容，披露粒度=整份报告；设计稿 §1.1）。
#: dividend 不在内：红利事件行的可知日是分红公告时点，套报告首披日是错的（§1.2）。
FD_STATEMENTS = frozenset(
    {"income", "balance", "cashflow_direct", "disclosed_indicators", "profitability"})

#: A 股定期报告的**法定披露截止日**（报告期月份 → (跨年数, 月, 日)）。
#: 依据《证券法》与交易所规则：一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30。
_DEADLINE = {3: (0, 4, 30), 6: (0, 8, 31), 9: (0, 10, 31), 12: (1, 4, 30)}


def _statutory_deadline(end_date: pd.Series) -> pd.Series:
    """报告期 → 法定披露截止日；**非自然季末 → NaT**。

    2026-08-25 收紧：原实现只按**月份**判（`_DEADLINE` 以月为键），于是 `2020-03-28`
    这类季末月非季末日也会拿到 Q1 的 04-30 截止日 —— 库里实测 5,350 行（09-02/09-01/
    03-28/03-27/…）。与选股仓 `is_standard_quarter_end` 对齐后，只有 03-31 / 06-30 /
    09-30 / 12-31 才有截止日，其余（01-01 伪行、04-22 日频行、季末月错日）一律 NaT，
    经 `_fetch_series` 末行的 `<= asof` 比较被整行剔除。
    """
    e = pd.to_datetime(pd.Series(end_date))
    m = e.dt.month
    dm, dd = m.map({k: v[1] for k, v in _DEADLINE.items()}), m.map({k: v[2] for k, v in _DEADLINE.items()})
    add_y = m.map({k: v[0] for k, v in _DEADLINE.items()})
    ok = dm.notna() & e.dt.is_quarter_end
    out = pd.Series(pd.NaT, index=e.index, dtype="datetime64[ns]")
    if ok.any():
        out[ok] = pd.to_datetime(pd.DataFrame({"year": (e.dt.year + add_y)[ok].astype(int),
                                               "month": dm[ok].astype(int),
                                               "day": dd[ok].astype(int)}))
    return out


def _fetch_series(codes: list[str], asof: pd.Timestamp, stmt: str, field: str,
                  years: int = 6) -> pd.DataFrame:
    """PIT 拉取 (ts_code, end_date, ann_date, value)；`ann_date` 已换成**修正可知日**。

    ## ⚠️ 为什么不能直接用 `stock_financial.ann_date`

    该表 96.5% 来自 CSMAR，其 `ann_date` 是**数据集批次日**而非首披日，可比法定截止日晚
    数月至数年（实测：2018Q1 的 ann_date 铺到 2021-02-03，2022Q1 铺到 2025-01-03）。
    直接按 `ann_date <= asof` 过滤，会在**每一个调样日丢掉最近整整一个季度**——2018/2020/
    2022/2024 各年 6 月调样日，Q1 报告在库里有 1,137~1,902 行，能通过过滤的只有 3~8 行（0~1%）。
    后果实测：成长因子（趋势/差分，命脉在最新一季）被打成噪声（对官方成分的 top-N 命中
    12.4%，随机基线 10.6%），价值因子（水平）尚可（50.3% vs 15.7%）。

    ## 2026-08-24 首披日升级（设计稿 §1）

    定期报告类报表（`FD_STATEMENTS`）用 `stock_first_disclosure` 的**真实首披日**作
    可知日（96.0% 覆盖，R5 回填资产）。**两源规则：首披日一律优先**，即便批次日更早
    （5.82% 疑快报行，宁晚勿早，不静默取 min）。

    ## 2026-08-25 跨仓口径统一（用户裁决）

    未命中首披日时回退**纯法定截止日**，`ann_date` 就此退出定期报告路径（事件行
    dividend 不在射程内，仍走旧 min 规则）。**法定截止日是真实披露日的上界**
    （合规公司必在其前披露），故用它当可知日**只会晚于、不会早于**真实可知时刻
    —— 是保守，不是前视。判据与取舍见 `_knowability` docstring。

    `years=6`：SalG/ProG 需 12 个 TTM 点，而 TTM 要 4 季暖机 + CSMAR 的 01-01 伪行被剔除，
    4 年只剩 11 个非空 TTM（实测 600110.SH），故必须拉够 6 年。
    """
    d = asof.date().isoformat()
    use_fd = stmt in FD_STATEMENTS
    fd_join = (f"LEFT JOIN {{s}}.stock_first_disclosure fd "
               "ON fd.ts_code=f.ts_code AND fd.end_date=f.end_date") if use_fd else ""
    fd_col = "fd.first_disclosure_date" if use_fd else "NULL::date"
    fq_col = "fd.quality" if use_fd else "NULL::text"
    c, s = _conn()
    try:
        with c.cursor() as cur:
            # SQL 只按 end_date 粗筛（报告期结束前不可能可知）；PIT 过滤放到修正可知日之后
            cur.execute(f"""SELECT f.ts_code, f.end_date, f.ann_date,
                                   (f.data->>%s)::float8, {fd_col}, {fq_col}
                            FROM {s}.stock_financial f
                            {fd_join.format(s=s)}
                            WHERE f.ts_code=ANY(%s) AND f.statement_type=%s
                              AND f.end_date <= DATE '{d}'
                              AND f.end_date >= DATE '{d}' - INTERVAL '{years} years'
                              AND f.data ? %s""", (field, codes, stmt, field))
            rows = cur.fetchall()
    finally:
        c.close()
    df = pd.DataFrame(rows, columns=["ts_code", "end_date", "ann_date", "value", "fd", "fdq"])
    if df.empty:
        return df.drop(columns=["fd", "fdq"])
    df["ann_date"] = _knowability(df["ann_date"], df["end_date"], df["fd"], df["fdq"], use_fd)
    df = df.drop(columns=["fd", "fdq"])
    # NaT（非自然季末）在此比较下为 False → 整行剔除，与选股仓 is_standard_quarter_end 同解
    return df[df["ann_date"] <= asof].reset_index(drop=True)


def _knowability(ann_date: pd.Series, end_date: pd.Series,
                 first_disclosure: pd.Series, quality: pd.Series,
                 use_fd: bool) -> pd.Series:
    """修正可知日（纯函数）。2026-08-25 用户裁决：**定期报告口径统一到选股仓**。

    ## 定期报告五类（`use_fd=True`）—— 与 `resolve_financial_availability` 逐条对齐

    可用首披日 → 用它（即便晚于法定截止日，见 08-24 两源规则：宁晚勿早）；
    否则回退**纯法定截止日**，不再看 `ann_date`。
    「可用」= `quality == 'ok'` 且非空 且 **首披日 ≥ 报告期末**（有效性守卫）。

    ### 为什么弃用 `min(ann_date, 截止日)`（用户 2026-08-25 裁决）

    `stock_financial.ann_date` 的语义**分段污染**且不打算修（采集计划 §3 明写划界）：
    CSMAR 段是数据集**批次日**（可晚数月至数年），Wind 段被上游 `load_wind_quarterly.py`
    封顶成 `min(stm_issuingdate, 截止日)`、真值已丢。把 PIT 正确性押在这样一个字段上，
    等于把 reader 的语义绑给一个随时可能改口径的上游——而首披日回填正是为了摆脱它。
    纯截止日是**密闭**的：只依赖日历，任何上游改动都动不了它。

    代价 = Wind 段月度消费方每季晚一个月看到真实已公开信息（实测 2026-03-31 差 2,087 行），
    随 2025Q2~2026Q1 首披日回填落地而自然消失。**对腿工厂代价为 0**：考察截止日
    （6 月调样用 04-30、12 月用 10-31）恰等于法定披露截止日，cutoff ≥ 截止日时两口径同解。

    ### 有效性守卫防的是什么

    坏行（`首披日 < 报告期末`，实案 = 非日历财年港股，Wind 返回的是另一份报告的发布日）
    若被直接采信，就是**真前视**。本仓此前**裸奔**，安全只靠上游不写坏行；今日 D2 裁定
    让这类行落成 `NULL + quality='sentinel'`，但正确性不该依赖上游的防御性写入。

    ## 事件行（`use_fd=False`，当前只有 dividend）—— 维持旧 `min` 规则，**不在统一射程内**

    选股仓 reader 只处理定期报告，事件行没有对应物，统一过去买不到任何一致性；
    而红利的可知日直接决定 DP 的 12 个月滚动窗，2026-08-20 刚修好死因子。
    实测若强行套截止日：9.26% 的行平移，**中位/p10/p90 均为 1 天** —— 代价虽小但纯是浪费。
    """
    e = pd.to_datetime(end_date)
    dl = _statutory_deadline(e)
    if not use_fd:
        ann = pd.to_datetime(ann_date)
        return dl.where(dl.notna() & (dl < ann), ann)
    fd = pd.to_datetime(first_disclosure)
    # np.asarray：按**位置**取值再挂 e 的索引。直接 `pd.Series(quality, index=e.index)`
    # 会对已是 Series 的入参按标签**重索引**，索引不同就静默错位成全 NaN（=全部退回截止日）
    q = pd.Series(np.asarray(quality), index=e.index)
    usable = fd.notna() & (fd >= e) & q.eq("ok")
    return fd.where(usable & dl.notna(), dl)


def _ttm_latest(df: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """每只股票的最新可知 TTM 值（走 `factors.pit_ttm_with_known`，零平行实现）。"""
    out = {}
    for code, g in df.groupby("ts_code"):
        t = pit_ttm_with_known(g.rename(columns={"value": "value"}))
        t = t[t["known_date"].notna() & (t["known_date"] <= asof)]
        if len(t) and pd.notna(t["ttm"].iloc[-1]):
            out[code] = float(t["ttm"].iloc[-1])
    return pd.Series(out, dtype=float)


def _growth(df: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """SalG / ProG：过去 12 季 TTM 对时间的 OLS 斜率 ÷ |均值|（`factors.growth_slope`）。"""
    out = {}
    for code, g in df.groupby("ts_code"):
        t = pit_ttm_with_known(g)
        t = t[t["known_date"].notna() & (t["known_date"] <= asof)]
        if len(t) >= GROWTH_QUARTERS:
            v = growth_slope(t["ttm"], n=GROWTH_QUARTERS)
            if np.isfinite(v):
                out[code] = v
    return pd.Series(out, dtype=float)


def _delta_roe(codes: list[str], asof: pd.Timestamp) -> pd.Series:
    """ΔROE = 当期季报 ROE − 去年同期季报 ROE（CSI 要的是**差**，不是 `F080702B` 那个比率）。"""
    df = _fetch_series(codes, asof, "profitability", "F050501B", years=3)
    out = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("end_date")
        s = pd.Series(g["value"].to_numpy(dtype=float),
                      index=pd.DatetimeIndex(g["end_date"]))
        s = s[~s.index.duplicated(keep="last")]
        s = s[pd.DatetimeIndex(s.index).is_quarter_end]
        if len(s) < 5:
            continue
        cur_end = s.index[-1]
        prev = s.index[(s.index.month == cur_end.month) & (s.index.year == cur_end.year - 1)]
        if len(prev) and np.isfinite(s.iloc[-1]) and np.isfinite(s[prev[0]]):
            out[code] = float(s.iloc[-1] - s[prev[0]])
    return pd.Series(out, dtype=float)


def factor_panel(sp: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """七个 CSI 因子的原始值面板（index=ts_code）。

    价值四项分母统一用**过去 1 年日均总市值**（原文："与过去 1 年日均总市值的比值"）；
    D/P 用每股税前现金红利 ÷ 过去 1 年日均收盘价（等价形式）。
    """
    codes = list(sp.index)
    amv = sp["avg_mv_1y"]
    panel = pd.DataFrame(index=sp.index)
    panel["EP"] = _ttm_latest(_fetch_series(codes, asof, "income", "B002000000"), asof) / amv
    panel["CFP"] = _ttm_latest(_fetch_series(codes, asof, "cashflow_direct", "C001000000"), asof) / amv
    eq = _fetch_series(codes, asof, "balance", "A003000000")
    eq_latest = (eq.sort_values("end_date").groupby("ts_code")["value"].last())
    panel["BP"] = eq_latest / amv
    if dp_source_for(asof) == "indicator":
        # R3 换源（2026-08-20）：csmar dividend 停更后走日频股息率；分母=当日价
        # vs 官方的 1 年日均价，截面 z 后尺度无关；单一截面单一源无混源
        panel["DP"] = _fetch_dp_indicator(codes, asof).reindex(panel.index).fillna(0.0)
    else:
        # 2026-08-20 缺陷修复：红利是事件行，不能走季度差分 TTM（此前恒空→DP 死因子）
        dps = dividend_ttm_events(
            _fetch_series(codes, asof, "dividend", _dividend_field()), asof)
        panel["DP"] = (dps / sp["avg_close_1y"]).reindex(panel.index).fillna(0.0)   # 未分红 → 0
    panel["SalG"] = _growth(_fetch_series(codes, asof, "income", "B001100000"), asof)
    panel["ProG"] = _growth(_fetch_series(codes, asof, "disclosed_indicators", "F020102"), asof)
    panel["dROE"] = _delta_roe(codes, asof)
    panel["industry"] = sp["industry"]
    panel["circ_mv"] = sp["circ_mv"]
    return panel


def _winsor_z(s: pd.Series) -> pd.Series:
    lo, hi = s.quantile(WINSOR[0]), s.quantile(WINSOR[1])
    w = s.clip(lo, hi)
    sd = w.std(ddof=1)
    return (w - w.mean()) / sd if np.isfinite(sd) and sd > 0 else pd.Series(0.0, index=s.index)


def style_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """价值得分 / 成长得分（区分金融·非金融；缺失用同行业均值代替）。"""
    p = panel.copy()
    for c in ("EP", "CFP", "BP", "DP", "SalG", "ProG", "dROE"):
        ind_mean = p.groupby("industry")[c].transform("mean")
        p[c] = p[c].fillna(ind_mean).fillna(p[c].mean())          # 同行业均值 → 全场均值兜底
    z = pd.DataFrame({c: _winsor_z(p[c]) for c in
                      ("EP", "CFP", "BP", "DP", "SalG", "ProG", "dROE")}, index=p.index)
    fin = p["industry"].isin(FINANCE_INDUSTRIES)
    value = pd.Series(np.nan, index=p.index)
    value[fin] = (z.loc[fin, "DP"] + z.loc[fin, "BP"] + z.loc[fin, "EP"]) / np.sqrt(3)
    value[~fin] = (z.loc[~fin, "DP"] + z.loc[~fin, "BP"]
                   + z.loc[~fin, "CFP"] + z.loc[~fin, "EP"]) / 2.0
    growth = (z["SalG"] + z["ProG"] + z["dROE"]) / np.sqrt(3)
    return pd.DataFrame({"value_score": value, "growth_score": growth,
                         "circ_mv": p["circ_mv"], "industry": p["industry"]})


def _logistic_prob(x: pd.Series, xl: float, xm: float, xu: float) -> pd.Series:
    """原文平滑函数：X≤X_M 用 (X_M−X_L) 作尺度，X>X_M 用 (X_U−X_M)。"""
    x = x.astype(float)
    scale = np.where(x <= xm, max(xm - xl, 1e-12), max(xu - xm, 1e-12))
    y = 1.0 / (1.0 + np.exp(4.0 * (xm - x) / scale))
    return pd.Series(y, index=x.index)


def style_probabilities(sc: pd.DataFrame) -> pd.DataFrame:
    """得分 → 综合价值/成长概率（含 Y≤0.1→0、Y≥0.9→1 的截断）。"""
    d = sc.dropna(subset=["value_score", "growth_score"]).copy()
    # 步骤(3)：先把两个得分平滑成 0~1 概率 —— 原文未给函数，此处用**经验秩分位**（自由裁量，已登记）
    d["value_prob"] = d["value_score"].rank(pct=True)
    d["growth_prob"] = d["growth_score"].rank(pct=True)
    d["comb_value_score"] = (d["value_prob"] + (1.0 - d["growth_prob"])) / 2.0
    d["comb_growth_score"] = 1.0 - d["comb_value_score"]
    # 步骤(4)：X_L/X_M/X_U = 按综合价值得分升序、累计自由流通市值达 25/50/75% 处的得分
    o = d.sort_values("comb_value_score")
    cw = o["circ_mv"].fillna(0).cumsum() / max(o["circ_mv"].fillna(0).sum(), 1e-12)
    xs = [float(o["comb_value_score"].iloc[int(np.searchsorted(cw.to_numpy(), q))])
          for q in FLOAT_QUANTILES]
    y = _logistic_prob(d["comb_value_score"], *xs)
    y = y.mask(y <= 0.1, 0.0).mask(y >= 0.9, 1.0)
    d["comb_value_prob"] = y
    d["comb_growth_prob"] = 1.0 - y
    return d


def _cap_weights(score: pd.Series) -> pd.Series:
    """综合风格得分加权 + 单样本 ≤15%、前五合计 ≤60%（迭代压顶再归一）。"""
    w = score.clip(lower=0)
    if w.sum() <= 0:
        w = pd.Series(1.0, index=score.index)
    w = w / w.sum()
    for _ in range(100):
        over = w > WEIGHT_CAP
        if over.any():
            excess = (w[over] - WEIGHT_CAP).sum()
            w[over] = WEIGHT_CAP
            free = ~over
            if free.any() and w[free].sum() > 0:
                w[free] += excess * w[free] / w[free].sum()
        top5 = w.nlargest(min(5, len(w)))
        if top5.sum() > TOP5_CAP:
            w[top5.index] *= TOP5_CAP / top5.sum()
            w = w / w.sum()
            continue
        if not over.any():
            break
    return w / w.sum()


def select_legs(prob: pd.DataFrame, take_top_half: bool) -> tuple[pd.Series, pd.Series]:
    """→ (成长腿权重, 价值腿权重)。`take_top_half` = 2000 带的"再取前 50%"附加条款。"""
    legs = {}
    for side, pcol, scol in (("growth", "comb_growth_prob", "comb_growth_score"),
                             ("value", "comb_value_prob", "comb_value_score")):
        sel = prob[prob[pcol] >= 1.0]
        if take_top_half and len(sel) > 1:
            sel = sel.nlargest(max(1, len(sel) // 2), scol)
        legs[side] = _cap_weights(sel[scol]) if len(sel) else pd.Series(dtype=float)
    return legs["growth"], legs["value"]


def _daily_returns(codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """窗口内成分股日收益宽表（close/pre_close − 1，沿用 UNIVERSE_SQL 的行级有效性）。"""
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, trade_date, (close/pre_close-1.0)::float8
                            FROM {s}.stock_daily_price
                            WHERE ts_code=ANY(%s) AND trade_date> DATE '{start.date()}'
                              AND trade_date<=DATE '{end.date()}'
                              AND close IS NOT NULL AND pre_close>0 AND volume>0""", (codes,))
            rows = cur.fetchall()
    finally:
        c.close()
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows, columns=["ts_code", "trade_date", "ret"])
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    return d.pivot(index="trade_date", columns="ts_code", values="ret").sort_index()


def _leg_returns(w0: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """一条腿在 (start, end] 的日收益：权重随价格漂移（调样日固定权重因子，期间不再平衡）。"""
    if not len(w0):
        return pd.Series(dtype=float)
    r = _daily_returns(list(w0.index), start, end)
    if r.empty:
        return pd.Series(dtype=float)
    r = r.reindex(columns=w0.index).fillna(0.0)          # 停牌/缺行 → 当日 0 收益
    w = w0.reindex(r.columns).fillna(0.0).to_numpy(dtype=float)
    out = {}
    for dt, row in zip(r.index, r.to_numpy(dtype=float)):
        tot = w.sum()
        out[dt] = float((w * row).sum() / tot) if tot > 0 else 0.0
        w = w * (1.0 + row)                               # 权重按持有漂移
    return pd.Series(out).sort_index()


@dataclass
class PairResult:
    growth: pd.Series          # 日收益
    value: pd.Series
    n_growth: dict
    n_value: dict
    skipped: list = None       # 因样本空间为空而未建的调样期（正常应为空）


def _official_members(index_code: str, cutoff: pd.Timestamp) -> set[str] | None:
    """截止日前最近一期官方成分（prev 链的真值注入用）；库内没有则 None。"""
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code FROM {s}.index_constituent
                            WHERE index_code=%s AND effective_date =
                              (SELECT max(effective_date) FROM {s}.index_constituent
                               WHERE index_code=%s
                                 AND effective_date <= DATE '{cutoff.date()}')""",
                        (index_code, index_code))
            rows = {r[0] for r in cur.fetchall()}
    finally:
        c.close()
    return rows or None


def build_pair(rank_lo: int, rank_hi: int | None, dates: list[pd.Timestamp],
               take_top_half: bool, verbose: bool = True,
               codes_by_date: dict | None = None, apply_filters: bool = True,
               official_space: bool = False, band: OfficialBand = BAND_2000,
               truth_prev_index: str | None = "932000.CSI") -> PairResult:
    """按调样日滚动构建一对纯风格腿的**日收益序列**。

    `codes_by_date`：{调样日字符串 → 该期显式样本空间名单}，给定时覆盖排名带
    （见 `sample_space` 的 `codes`）。缺某期则该期回退到排名带。

    `official_space=True`：样本空间改走 `official_sample_space`（模拟官方选样，含缓冲区
    滚动；有官方 932000 成分的期用官方名单做上期状态），且**样本空间与七因子全部以
    `review_cutoff(d)` 为数据界**（T1–T9 修复）；腿收益仍从生效日 `d` 起算。
    此时 `rank_lo/rank_hi/apply_filters` 被忽略；`codes_by_date` 给出的期**直通官方真值
    名单**（真实成分版，2026-08-19 wset 回补后 2023-08 起可用），未给的期走模拟。
    `band` 选择模拟哪个官方带（默认 2000）；`truth_prev_index` 是 prev 链的真值注入源
    （置 None = 纯自举模拟，Gate 0A 判定版口径）。
    """
    gs, vs, ng, nv, skipped = [], [], {}, {}, []
    prev_members: set[str] | None = None
    for i, d in enumerate(dates[:-1]):
        if official_space:
            cutoff = review_cutoff(d)
            explicit = (codes_by_date or {}).get(str(d.date()))
            if explicit is not None:
                picked = list(explicit)          # 官方真值名单直通（真实成分版）
            else:
                # truth_prev_index=None → 纯自举 prev 链（Gate 0A 判定版：不用 000852 真值，
                # 与尾部桶"无自身真值"的处境同构，r3 §4.1）
                truth = _official_members(truth_prev_index, cutoff) if truth_prev_index else None
                prev = truth or prev_members
                picked = official_sample_space(d, prev, verbose=verbose, band=band)
            prev_members = set(picked)
            sp = sample_space(cutoff, None, None, codes=picked, apply_filters=False)
            asof_data = cutoff
        else:
            sp = sample_space(d, rank_lo, rank_hi, apply_filters=apply_filters,
                              codes=(codes_by_date or {}).get(str(d.date())))
            asof_data = d
        if not len(sp):
            # 静默跳过会让"少建一期"完全不可见（Gate 0 正式跑吃过一次）→ 必须出声
            print(f"  ⚠️ {d.date()}: 样本空间为空，整期跳过（检查 stock_indicator 覆盖）", flush=True)
            skipped.append(str(d.date()))
            continue
        prob = style_probabilities(style_scores(factor_panel(sp, asof_data)))
        wg, wv = select_legs(prob, take_top_half)
        ng[str(d.date())], nv[str(d.date())] = len(wg), len(wv)
        nxt = dates[i + 1]
        gs.append(_leg_returns(wg, d, nxt))
        vs.append(_leg_returns(wv, d, nxt))
        if verbose:
            print(f"  {d.date()} → {nxt.date()}: 样本空间 {len(sp)}，"
                  f"成长 {len(wg)} / 价值 {len(wv)}", flush=True)
    if skipped:
        print(f"  ⚠️ 共跳过 {len(skipped)} 期：{skipped}", flush=True)
    return PairResult(pd.concat(gs).sort_index() if gs else pd.Series(dtype=float),
                      pd.concat(vs).sort_index() if vs else pd.Series(dtype=float), ng, nv,
                      skipped)


def build_tail_pair(dates: list[pd.Timestamp], verbose: bool = True) -> PairResult:
    """尾部桶（第 5 桶）纯风格对的日收益序列（r3 预登记 §2/§3）。

    与 `build_pair(official_space=True)` 同一因子/得分/选样/加权/漂移管线，仅样本空间
    换成 `tail_sample_space`（官方链余集）。`take_top_half` 恒为 False（r3 裁决点 4：
    尾部不采用 2000P 的「前 50%」条款）。932000 无真值期的模拟 prev 链在期间滚动接力。
    """
    gs, vs, ng, nv, skipped = [], [], {}, {}, []
    prev2000: set[str] | None = None
    for i, d in enumerate(dates[:-1]):
        cutoff = review_cutoff(d)
        truth2000 = _official_members("932000.CSI", cutoff)
        picked, m2000 = tail_sample_space(d, prev2000=truth2000 or prev2000, verbose=verbose)
        prev2000 = m2000
        sp = sample_space(cutoff, None, None, codes=picked, apply_filters=False)
        if not len(sp):
            print(f"  ⚠️ {d.date()}: 尾部样本空间为空，整期跳过（检查 stock_indicator 覆盖）",
                  flush=True)
            skipped.append(str(d.date()))
            continue
        if len(sp) < 0.5 * len(picked):
            # 快照缺行会静默吞掉名单里的票（sample_space 按快照日 reindex）——必须出声
            print(f"  ⚠️ {d.date()}: 名单 {len(picked)} 只经快照对齐只剩 {len(sp)}，"
                  f"疑 stock_indicator 覆盖洞", flush=True)
        prob = style_probabilities(style_scores(factor_panel(sp, cutoff)))
        wg, wv = select_legs(prob, take_top_half=False)
        ng[str(d.date())], nv[str(d.date())] = len(wg), len(wv)
        nxt = dates[i + 1]
        gs.append(_leg_returns(wg, d, nxt))
        vs.append(_leg_returns(wv, d, nxt))
        if verbose:
            print(f"  {d.date()} → {nxt.date()}: 尾部空间 {len(sp)}，"
                  f"成长 {len(wg)} / 价值 {len(wv)}", flush=True)
    if skipped:
        print(f"  ⚠️ 共跳过 {len(skipped)} 期：{skipped}", flush=True)
    return PairResult(pd.concat(gs).sort_index() if gs else pd.Series(dtype=float),
                      pd.concat(vs).sort_index() if vs else pd.Series(dtype=float), ng, nv,
                      skipped)


# ---------------------------------------------------------------- 等比 5 桶（2026-08-19 预登记）
#: 只数占比几何 r=2（1:2:4:8:16，共 31 份）——用户裁决 A；其他切法须另立预登记。
GEO_BUCKET_WEIGHTS = (1, 2, 4, 8, 16)
GEO_ADV_TRIM = 0.10            # 全空间成交额前 90% 即止，桶内不加剔（裁决点 2-1）


def _split_geometric(codes_ranked: list[str],
                     weights: tuple = GEO_BUCKET_WEIGHTS) -> list[list[str]]:
    """已按日均总市值降序的名单 → 按只数占比几何切段（纯函数，累计边界取整，无缝无重叠）。"""
    n, tot = len(codes_ranked), sum(weights)
    bounds, acc = [], 0
    for w in weights[:-1]:
        acc += w
        bounds.append(round(n * acc / tot))
    out, prev = [], 0
    for b in bounds + [n]:
        out.append(codes_ranked[prev:b])
        prev = b
    return out


def geometric_buckets(eff: pd.Timestamp, verbose: bool = False,
                      frame: pd.DataFrame | None = None) -> list[list[str]]:
    """等比 5 桶名单（预登记 `2026-08-19-geometric-5buckets` §1/§2）。

    官方化全指样本空间（含北交所，交易所护栏）→ 考察窗日均成交额前 90% →
    按考察窗日均总市值降序 → 只数 1:2:4:8:16 切 5 段。无缓冲区（裁决点 2-2）。
    """
    g = _space_frame(eff) if frame is None else frame
    g = g[g["adv"].rank(pct=True, ascending=True) > GEO_ADV_TRIM]
    g = g.sort_values("avg_mv", ascending=False)
    buckets = _split_geometric(list(g.index))
    if verbose:
        sizes = "/".join(str(len(b)) for b in buckets)
        print(f"  geometric_buckets @{review_cutoff(eff).date()}: 空间 {len(g)} → {sizes}",
              flush=True)
    return buckets


def build_geometric_pairs(dates: list[pd.Timestamp], verbose: bool = True,
                          legs_only: bool = False) -> list[PairResult]:
    """等比 5 桶 × P 族纯风格对的日收益序列（每桶一个 PairResult，公用一份帧/期）。

    与 Gate 0 同一因子/得分/选样/加权/漂移管线；`take_top_half` 恒 False（裁决点 2-3）。
    `legs_only=True` = 单期核对模式：只建腿、打印只数与非空率，**不算收益**。
    """
    acc = [dict(gs=[], vs=[], ng={}, nv={}) for _ in range(5)]
    skipped: list = []
    for i, d in enumerate(dates[:-1]):
        cutoff = review_cutoff(d)
        frame = _space_frame(d)
        buckets = geometric_buckets(d, verbose=verbose, frame=frame)
        for k, picked in enumerate(buckets):
            sp = sample_space(cutoff, None, None, codes=picked, apply_filters=False)
            if not len(sp):
                print(f"  ⚠️ {d.date()} 桶{k+1}: 快照对齐后为空，该桶该期跳过", flush=True)
                skipped.append(f"{d.date()}#b{k+1}")
                continue
            if len(sp) < 0.5 * len(picked):
                print(f"  ⚠️ {d.date()} 桶{k+1}: 名单 {len(picked)} 对齐后只剩 {len(sp)}，"
                      f"疑快照覆盖洞", flush=True)
            panel = factor_panel(sp, cutoff)
            if legs_only:
                nn = (panel.notna().mean() * 100).round(1).to_dict()
                print(f"    桶{k+1}: 名单 {len(picked)} 对齐 {len(sp)} 非空率% {nn}", flush=True)
            prob = style_probabilities(style_scores(panel))
            wg, wv = select_legs(prob, take_top_half=False)
            acc[k]["ng"][str(d.date())], acc[k]["nv"][str(d.date())] = len(wg), len(wv)
            if verbose or legs_only:
                print(f"    桶{k+1} 腿：成长 {len(wg)} / 价值 {len(wv)}", flush=True)
            if legs_only:
                continue
            nxt = dates[i + 1]
            acc[k]["gs"].append(_leg_returns(wg, d, nxt))
            acc[k]["vs"].append(_leg_returns(wv, d, nxt))
    if skipped:
        print(f"  ⚠️ 桶×期 跳过清单：{skipped}", flush=True)
    return [PairResult(pd.concat(a["gs"]).sort_index() if a["gs"] else pd.Series(dtype=float),
                       pd.concat(a["vs"]).sort_index() if a["vs"] else pd.Series(dtype=float),
                       a["ng"], a["nv"], skipped) for a in acc]
