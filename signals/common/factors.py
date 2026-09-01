"""方向 C · B1-T2 因子构造：财务事实 → YTD→TTM → 成长/价值分 → 截面标准化。

输入是 signals.common.financial_reader.fetch_financial_facts 的归一化输出
（friendly 字段名、PIT ann_date、csmar/wind 源已拼接）。本模块纯计算、不碰 IO，
方便合成数据单测。因子口径依设计稿 §2.5（Gen2/Gen3 指数编制方法论）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def filter_quarter_ends(s: pd.Series) -> pd.Series:
    """只保留自然季末（3-31/6-30/9-30/12-31）索引项。

    丢弃 CSMAR `01-01` 伪行（report_type='other'，值=上年年报重复，会污染
    单季差分）与 Wind 日频 TTM 行（落在任意交易日、YTD 字段为空）。
    """
    idx = pd.DatetimeIndex(s.index)
    return s[idx.is_quarter_end]


def quarterize_ytd(ytd: pd.Series) -> pd.Series:
    """YTD 累计（按季末日期索引）→ 单季值。

    A 股利润表按 YTD 累计披露：Q1=本季、H1=前两季、Q3=前三季、年报=全年。
    单季化：同一自然年内 Qn = YTD(Qn) − YTD(Qn−1)，Q1（3-31）= YTD 本身，
    跨年自动重置（新年 Q1 无减项）。同年上一季缺失 → 该季 NaN（无法差分）。

    索引须为自然季末（调用方先滤掉 01-01 伪行与日频 TTM 行）。
    """
    ytd = ytd.sort_index()
    keyed = {(d.year, (d.month - 1) // 3 + 1): v for d, v in ytd.items()}
    out: dict[pd.Timestamp, float] = {}
    for d, v in ytd.items():
        q = (d.month - 1) // 3 + 1
        if q == 1:
            out[d] = v
        else:
            prev = keyed.get((d.year, q - 1))
            out[d] = v - prev if prev is not None and pd.notna(prev) else np.nan
    return pd.Series(out).sort_index()


def ttm_from_quarterly(single_q: pd.Series) -> pd.Series:
    """单季序列 → 滚动 4 季 TTM（trailing-twelve-months）。

    先 reindex 到完整季度网格（缺季补 NaN），再 rolling(4).sum()——任一窗口
    含缺季即为 NaN，天然阻断跨缺口求和；越过缺口后自动恢复。返回按原始
    季末日期对齐（只保留输入存在的季末）。
    """
    single_q = single_q.sort_index()
    if single_q.empty:
        return single_q.astype(float)
    periods = pd.PeriodIndex(single_q.index, freq="Q")
    by_period = pd.Series(single_q.to_numpy(dtype=float), index=periods)
    full = pd.period_range(periods.min(), periods.max(), freq="Q")
    by_period = by_period.reindex(full)
    ttm = by_period.rolling(4, min_periods=4).sum()
    ttm.index = ttm.index.to_timestamp(how="end").normalize()
    return ttm.reindex(single_q.index)


def growth_slope(ttm: pd.Series, n: int = 12) -> float:
    """成长分：最近 n 季 TTM 对时间的 OLS 斜率 ÷ |均值|（设计稿 §2.5 Gen2/Gen3）。

    斜率对季序 0..n−1 做最小二乘；÷|均值| 把趋势归一为相对水平的每季增速，
    使跨公司可比。有效 TTM（去 NaN 后）不足 n 季或均值≈0 → NaN。
    """
    vals = pd.Series(ttm).dropna().to_numpy(dtype=float)
    if len(vals) < n:
        return float("nan")
    y = vals[-n:]
    x = np.arange(n, dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    mean = np.abs(y.mean())
    if mean < 1e-12:
        return float("nan")
    return float(slope / mean)


def rolling_growth_slope(ttm: pd.Series, known: pd.Series, n: int = 12) -> pd.DataFrame:
    """整段 TTM 网格 → 每季末的成长斜率 + 可知日（growth_slope 的批量预计算形态）。

    slope(q) = growth_slope(ttm[q−n+1..q])，但窗口内**任一季缺失即 NaN**（严于
    growth_slope 的 dropna 语义——网格上跨缺口回归会扭曲时间轴）。向量化：固定
    x=0..n−1 的 OLS 斜率是窗口的线性泛函 Σwᵢyᵢ，NaN 自然传播。
    known_date(q) = 窗内 n 期 TTM 可知日的最大值（斜率在最晚那期披露后才可算）。
    """
    idx = ttm.index
    out = pd.DataFrame(
        {"slope": np.nan, "known_date": pd.NaT}, index=idx
    )
    y = ttm.to_numpy(dtype=float)
    if len(y) < n:
        return out
    from numpy.lib.stride_tricks import sliding_window_view

    windows = sliding_window_view(y, n)  # (len−n+1, n)
    x = np.arange(n, dtype=float)
    w = (x - x.mean()) / ((x - x.mean()) ** 2).sum()
    slopes = windows @ w
    means = np.abs(windows.mean(axis=1))
    # 用 where= 而不是 np.where：后者会**先把整个除法算完再选**，于是 means≈0
    # 的条目照样执行除法、产生 inf 并置上浮点错误标志。默认 errstate 下那只是
    # RuntimeWarning，但 pandas 的 cast_from_unit_vectorized 工作在
    # errstate(over="raise") 里，那个待决标志会在随后一次完全无辜的 to_datetime
    # 上炸成 FloatingPointError。2026-08-07 回填退市股票后由真实数据触发
    # （TTM 序列跨零、量级 1e9，12 期窗口均值可能恰好≈0）。
    rel = np.divide(
        slopes,
        means,
        out=np.full_like(slopes, np.nan),
        where=means >= 1e-12,
    )

    # NaT 必须先变成 NaN 再进 rolling：datetime64 的 NaT 直接 astype(float)
    # 得到的是哨兵 −9.22e18，而它是个合法浮点数，于是 min_periods 把它算作
    # 有效观测、max() 又把它忽略——结果是"少了一期披露日却照样宣布斜率可知"，
    # PIT 语义被破坏；该值送进 pd.to_datetime(unit="D") 还会在乘算中溢出。
    # 2026-08-07 回填退市股票、universe 变宽后由真实数据触发。
    known_raw = known.to_numpy(dtype="datetime64[D]")
    known_days = pd.Series(
        np.where(np.isnat(known_raw), np.nan, known_raw.astype(float)),
        index=idx,
    )
    known_max = known_days.rolling(n, min_periods=n).max()

    out.iloc[n - 1 :, out.columns.get_loc("slope")] = rel

    # 直接由整数天数转 datetime64，不走 pd.to_datetime(unit="D")。
    # 后者内部在 errstate(over="raise") 里做 cast_from_unit_vectorized，会把
    # **上游遗留的待决浮点标志**在此引爆：执行机实测显示 np.geterr() 当时是正常的
    # "warn"、立即用同一数组原样重调即通过、单独回放也不炸——即数组本身无辜，
    # 炸的是别人留下的标志。这里本来就只是把天数换成日期，没有必要引入那条路径。
    days = known_max.to_numpy()
    known_dates = np.full(len(days), np.datetime64("NaT"), dtype="datetime64[ns]")
    settled = np.isfinite(days)
    if settled.any():
        known_dates[settled] = (
            days[settled].astype("int64").astype("datetime64[D]")
        )
    out["known_date"] = known_dates
    out["slope"] = out["slope"].where(out["known_date"].notna())
    return out


def asof_latest(pooled: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """pooled 长表（多股多期）→ as_of 时每股已知的最新一期（一行/股）。

    需含列 ts_code / end_date / known_date。known_date ≤ as_of 中取 end_date 最大
    的行（known 与 end 可乱序，直接掩码后取尾，不假设单调）；无已知行的股不出现。
    """
    known = pooled[pooled["known_date"] <= as_of]
    if known.empty:
        return known
    return (
        known.sort_values(["ts_code", "end_date"])
        .groupby("ts_code", as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )


def latest_pit(df: pd.DataFrame, as_of: pd.Timestamp) -> float:
    """Point-in-time：取 ann_date ≤ as_of 中最新一期的 value。

    df 需含列 end_date / ann_date / value。同一披露日多期（年报与次年 Q1 常同日
    披露）取 end_date 更晚者。无任何行已披露 → NaN。
    """
    known = df[pd.to_datetime(df["ann_date"]) <= as_of]
    if known.empty:
        return float("nan")
    known = known.sort_values(["ann_date", "end_date"])
    return float(known.iloc[-1]["value"])


def winsorize(s: pd.Series, lower: float = 0.05, upper: float = 0.95) -> pd.Series:
    """截面缩尾：把低于 lower 分位 / 高于 upper 分位的值截到分位边界（NaN 保留）。"""
    lo = s.quantile(lower)
    hi = s.quantile(upper)
    return s.clip(lower=lo, upper=hi)


def cross_section_zscore(s: pd.Series) -> pd.Series:
    """截面标准化 (x−均值)/标准差（样本标准差 ddof=1）；NaN 不参与、原样保留。

    标准差≈0（全同值）→ 全 0，避免除零放大噪声。
    """
    std = s.std()
    if not np.isfinite(std) or std < 1e-12:
        return pd.Series(0.0, index=s.index).where(s.notna())
    return (s - s.mean()) / std


def composite_score(z_frame: pd.DataFrame, min_factors: int = 1) -> pd.Series:
    """多个已标准化因子 → 合成分 = Σz / √n（设计稿 §2.5）。

    逐行按**可用**因子数归一（金融股剔 CF/P 后只用剩余三项 /√3，不被缺失稀释）；
    可用因子数 < min_factors → NaN。
    """
    count = z_frame.notna().sum(axis=1)
    composite = z_frame.sum(axis=1, min_count=1) / np.sqrt(count)
    return composite.where(count >= min_factors)


_PROVENANCE_COLUMNS = (
    "true_first_disclosure_verified",
    "unverified_dependency_keys",
)


def _has_provenance_columns(frame: pd.DataFrame) -> bool:
    """返回 provenance 是否启用；成对列只出现一列时拒绝降级。"""
    present = [column in frame.columns for column in _PROVENANCE_COLUMNS]
    if any(present) and not all(present):
        raise ValueError("provenance columns must be provided together")
    return all(present)


def _dependency_union(values) -> tuple[str, ...]:
    """依赖 key 的稳定排序去重并集。"""
    return tuple(sorted({key for value in values for key in value}))


def _validated_dependency_tuple(flag, value) -> tuple[str, ...]:
    """校验 raw fact 的 strict provenance 合同并返回依赖 tuple。"""
    if not isinstance(flag, (bool, np.bool_)):
        raise ValueError("invalid provenance: verified flag must be bool")
    if not isinstance(value, tuple):
        raise ValueError("invalid provenance: dependency keys must be a tuple")
    if any(not isinstance(key, str) or not key for key in value):
        raise ValueError("invalid provenance: dependency keys must be non-empty strings")
    if bool(flag) != (len(value) == 0):
        raise ValueError("invalid provenance: verified flag disagrees with dependency keys")
    return value


def _ttm_unverified_dependencies(
    selected: pd.DataFrame,
    full: pd.PeriodIndex,
    ttm: pd.Series,
) -> list[tuple[str, ...] | None]:
    """在既有数值网格旁传播 YTD→单季→四季 TTM 的符号依赖。"""
    periods = pd.PeriodIndex(selected["end_date"], freq="Q")
    raw_dependencies = {
        period: _validated_dependency_tuple(flag, value)
        for period, flag, value in zip(
            periods,
            selected["true_first_disclosure_verified"],
            selected["unverified_dependency_keys"],
        )
    }

    single_dependencies: list[tuple[str, ...] | None] = []
    for period in full:
        current = raw_dependencies.get(period)
        if current is None:
            single_dependencies.append(None)
        elif period.quarter == 1:
            single_dependencies.append(current)
        else:
            previous = raw_dependencies.get(period - 1)
            single_dependencies.append(
                None
                if previous is None
                else _dependency_union((current, previous))
            )

    dependencies: list[tuple[str, ...] | None] = []
    for offset, value in enumerate(ttm):
        window = single_dependencies[offset - 3 : offset + 1]
        if offset < 3 or pd.isna(value) or any(item is None for item in window):
            dependencies.append(None)
        else:
            dependencies.append(_dependency_union(window))
    return dependencies


def pit_ttm_with_known(df: pd.DataFrame) -> pd.DataFrame:
    """单只股票某 YTD 科目明细 → 完整季度网格上的 TTM + 每期可知日 known_date。

    df 需含列 end_date / ann_date / value(YTD)。链路：
    ① 过滤自然季末（去 01-01 伪行与日频行）；同季多行（重述）取**先披露**那行，
      不吃后来的重述（PIT：当时市场看到的是首披值）；
    ② 单季化 → 网格化 → 滚动 4 季 TTM（缺季窗口 NaN）；
    ③ known_date(q) = 窗内 [q−4..q] 各行 ann_date 的最大值——TTM(q) 经单季差分
      依赖到上年同季/上年年报（TTM(Q1)=Q1ytd+年报−上年Q1ytd），任一依赖行未披露
      则整期不可知。年报晚于 Q1 披露的乱序场景由此挡住前视。
    返回 index=季末（min..max 完整网格）、columns=[ttm, known_date]；不足依赖处均 NaN。
    输入若携带成对 provenance 列，则在同一首披选择与数值网格上追加精确依赖；
    legacy 输入仍严格返回原两列。
    """
    has_provenance = _has_provenance_columns(df)
    df = df.copy()
    df["end_date"] = pd.to_datetime(df["end_date"])
    df["ann_date"] = pd.to_datetime(df["ann_date"])
    df = df[df["end_date"].dt.is_quarter_end]
    if df.empty:
        columns = ["ttm", "known_date"]
        if has_provenance:
            columns.extend(
                ["unverified_dependency_keys", "true_first_disclosure_verified"]
            )
        return pd.DataFrame(columns=columns)
    df = df.sort_values(["end_date", "ann_date"])
    first_rows = df.drop_duplicates("end_date", keep="first") if has_provenance else None
    df = df.groupby("end_date", as_index=False).first()
    if first_rows is not None:
        first_rows = first_rows.set_index("end_date").reindex(df["end_date"])
        for column in _PROVENANCE_COLUMNS:
            df[column] = first_rows[column].to_numpy(copy=True)
    ytd = pd.Series(df["value"].to_numpy(dtype=float), index=pd.DatetimeIndex(df["end_date"]))
    single = quarterize_ytd(ytd)
    periods = pd.PeriodIndex(single.index, freq="Q")
    full = pd.period_range(periods.min(), periods.max(), freq="Q")
    ttm = pd.Series(single.to_numpy(dtype=float), index=periods).reindex(full)
    ttm = ttm.rolling(4, min_periods=4).sum()
    # ann_date → 自 epoch 天数（整数域 rolling max，避开 ns→float 精度损失）
    ann_days = df["ann_date"].to_numpy(dtype="datetime64[D]").astype(float)
    ann_grid = pd.Series(ann_days, index=periods).reindex(full)
    # 窗=5 期（q−4..q）；min_periods=4 容忍仅 q−4 缺席（此时 q−3 是 Q1、无差分依赖，
    # 或 ttm 本身已 NaN）；known NaN 处 ttm 防御性置 NaN
    known_days = ann_grid.rolling(5, min_periods=4).max()
    ttm = ttm.where(known_days.notna())
    idx = full.to_timestamp(how="end").normalize()
    result = pd.DataFrame(
        {
            "ttm": ttm.to_numpy(),
            "known_date": pd.to_datetime(known_days.to_numpy(), unit="D"),
        },
        index=idx,
    )
    if has_provenance:
        dependencies = _ttm_unverified_dependencies(df, full, ttm)
        result["unverified_dependency_keys"] = pd.Series(
            dependencies, index=idx, dtype=object
        )
        result["true_first_disclosure_verified"] = pd.Series(
            [bool(value is not None and not value) for value in dependencies],
            index=idx,
            dtype=object,
        )
    return result


def pit_ttm_series(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    """单只股票某 YTD 科目明细 → PIT-对齐的 TTM 序列（按季末索引）。

    pit_ttm_with_known 的单时点视图：只保留 known_date ≤ as_of 且 TTM 有效的季度。
    """
    grid = pit_ttm_with_known(df)
    if grid.empty:
        return pd.Series(dtype=float)
    keep = grid["ttm"].notna() & (grid["known_date"] <= as_of)
    return grid.loc[keep, "ttm"]


def extract_statement_field(
    facts: pd.DataFrame, statement_type: str, field: str
) -> pd.DataFrame:
    """从 fetch_financial_facts 输出按 statement_type 抽单字段 → end_date/ann_date/value。

    facts.data 是 friendly-name JSONB dict；缺该键的行 value=NaN（保留季度结构，
    让 quarterize/TTM 自行在缺口处产生 NaN）。成对 provenance 列若存在则按
    原 statement 子集行序、不做类型强转地透传；只出现一列时拒绝。
    """
    has_provenance = _has_provenance_columns(facts)
    sub = facts[facts["statement_type"] == statement_type]
    value = sub["data"].apply(
        lambda d: d.get(field) if isinstance(d, dict) else None
    )
    result = pd.DataFrame(
        {
            "end_date": pd.to_datetime(sub["end_date"].to_numpy()),
            "ann_date": pd.to_datetime(sub["ann_date"].to_numpy()),
            "value": pd.to_numeric(value.to_numpy(), errors="coerce"),
        }
    )
    if has_provenance:
        result["true_first_disclosure_verified"] = sub[
            "true_first_disclosure_verified"
        ].to_numpy(copy=True)
        result["unverified_dependency_keys"] = sub[
            "unverified_dependency_keys"
        ].to_numpy(copy=True)
    return result


def _latest(series: pd.Series) -> float:
    """去 NaN 后的最新值（pit_ttm_series 已按季末升序、PIT 过滤）；空 → NaN。"""
    series = series.dropna()
    return float(series.iloc[-1]) if len(series) else float("nan")


def stock_style_factors(
    facts: pd.DataFrame,
    as_of: pd.Timestamp,
    shares: float,
    price: float,
    is_financial: bool = False,
    growth_n: int = 12,
) -> dict[str, float]:
    """单只股票在 as_of 的风格因子原始向量（设计稿 §2.5 Gen2/Gen3 蓝本）。

    成长：营收 / 归母净利 TTM 的 growth_slope（最近 growth_n 季，PIT）。
    价值（分母=市值 mv=shares×price，PIT 股本与收盘由调用方传入）：
      EP=归母净利 TTM/mv、BP=归母权益/mv、CFP=经营现金流 TTM/mv（金融行业剔为 NaN）、
      DP=每股税前股利×股本/mv（=dps/price；2019 前分红表缺 → NaN）。
    facts 为单票 fetch_financial_facts 子集。
    """
    rev = extract_statement_field(facts, "income", "revenue")
    npf = extract_statement_field(facts, "income", "net_profit_parent_ytd")
    cfo = extract_statement_field(facts, "cashflow_direct", "cfo_net")
    equity = extract_statement_field(facts, "balance", "equity_parent")
    dps = extract_statement_field(facts, "dividend", "cash_dividend_ps_pre_tax")

    np_ttm = pit_ttm_series(npf, as_of)
    mv = shares * price
    valid_mv = np.isfinite(mv) and mv > 0

    def ratio(numerator: float) -> float:
        return numerator / mv if valid_mv and pd.notna(numerator) else float("nan")

    cfp = float("nan") if is_financial else ratio(_latest(pit_ttm_series(cfo, as_of)))
    dps_latest = latest_pit(dps, as_of)
    dp = (
        dps_latest * shares / mv
        if valid_mv and pd.notna(dps_latest)
        else float("nan")
    )
    return {
        "sal_g": growth_slope(pit_ttm_series(rev, as_of), n=growth_n),
        "pro_g": growth_slope(np_ttm, n=growth_n),
        "ep": ratio(_latest(np_ttm)),
        "bp": ratio(latest_pit(equity, as_of)),
        "cfp": cfp,
        "dp": dp,
    }
