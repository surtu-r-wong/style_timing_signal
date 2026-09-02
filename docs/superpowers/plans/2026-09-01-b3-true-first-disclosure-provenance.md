# B3 True First-Disclosure Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在冻结 B3 标签上接入报告真实首披日，并把验证状态按实际参与 `style_score` 的财务依赖传播；只有 model 行覆盖100%后才原样正式重跑。

**Architecture:** 保持现有数值函数为唯一计算路径，只给 `extract_statement_field`、`pit_ttm_with_known`、`rolling_growth_slope` 增加“输入存在 provenance 时才附加输出列”的兼容扩展。B3 raw fetch 左连接 `stock_first_disclosure`，PIT policy 生成严格布尔和未验证依赖键；月度快照只汇总实际非空因子依赖，并把精确缺口写入现有 diagnostics、从正式 exposures 中剔除内部键。

**Tech Stack:** Python 3.13、pandas、NumPy、psycopg2、pytest、PostgreSQL（只读）、Git worktree、WSL2/tmux。

---

## Execution invariants

- 权威规格：`docs/superpowers/specs/2026-08-31-b3-true-first-disclosure-provenance-approved-design.md`（提交 `8899775`）。
- 实现基线：`archive/b3-wind-share-capital-tail-20260814` / `41ed581`。
- 不修改历史工作树 `/home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital`。
- 不覆盖归档 `b3-formal-run.tar.gz`（SHA256 `a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843`）。
- 不执行数据库写入、DDL、参数调优或候选筛选。
- 所有生产代码前必须先运行对应新增测试并看到预期失败。

## File map

- Modify `signals/style_basket/b3_build.py`: raw schema、首披 join、PIT policy、pool 与月度依赖汇总。
- Modify `signals/common/factors.py`: 可选 provenance 的字段抽取、TTM 和 slope 传播。
- Modify `signals/style_basket/build.py`: pooled TTM/slope/event 保留 provenance。
- Modify `signals/style_basket/b3_exposures.py`: strict bool 校验、精确缺口 diagnostics、剔除内部键。
- Modify `tests/test_factors.py`: TTM/slope 依赖拓扑。
- Modify `tests/test_style_basket.py`: ticker pool provenance 与公共 B1 兼容性。
- Modify `tests/test_b3_exposures.py`: raw/PIT/SQL/evidence/月度汇总/诊断集成。
- Verify with main `tools/audit_b3_disclosure_coverage.py`: 独立100%闸门，不移入冻结 builder 分支。
- Update after formal run `docs/plans/research_registry.yaml` and generated `docs/plans/README.md`.

### Task 1: Create the frozen implementation worktree and prove the baseline

**Files:**
- Verify only: repository and test suite; no file changes.

- [ ] **Step 1: Use the worktree skill and create an isolated branch**

From the main repository, use `superpowers:using-git-worktrees`, verify `.worktrees` is ignored, then create:

```bash
git worktree add \
  /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-provenance \
  -b fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
```

Expected: new worktree HEAD is `41ed581`; the historical B3 worktree remains unchanged.

- [ ] **Step 2: Verify isolation and copy only the ignored runtime config**

```bash
git status --short
git rev-parse --short HEAD
cp /home/elfbob/claude-code/style_timing_signal/config/settings.yaml config/settings.yaml
```

Expected: tracked status clean; HEAD `41ed581`; `config/settings.yaml` remains ignored.

- [ ] **Step 3: Run the focused baseline**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py \
  tests/test_style_basket.py \
  tests/test_b3_exposures.py -q
```

Expected: exit 0 with no failures. If baseline fails, stop and diagnose before implementation.

### Task 2: Define raw true-disclosure and A1 PIT behavior

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Add failing PIT tests**

Extend `_single_pit_fact` with nullable `first_disclosure_date` and `disclosure_quality`, then add:

```python
@pytest.mark.parametrize("statement_type", ["income", "balance", "cashflow_direct", "dividend"])
@pytest.mark.parametrize("policy", [POLICY_MAIN, POLICY_LAG])
def test_valid_first_disclosure_overrides_both_policies(statement_type, policy):
    raw = _single_pit_fact(
        statement_type=statement_type,
        first_disclosure_date="2020-04-17",
        disclosure_quality="ok",
    )
    got = apply_pit_policy(raw, policy)
    assert got.loc[0, "ann_date"] == pd.Timestamp("2020-04-17")
    assert got.loc[0, "known_date_source"] == "stock_first_disclosure"
    assert got.loc[0, "true_first_disclosure_verified"] is np.bool_(True) or bool(
        got.loc[0, "true_first_disclosure_verified"]
    )
    assert got.loc[0, "unverified_dependency_keys"] == ()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (POLICY_MAIN, pd.Timestamp("2020-04-15")),
        (POLICY_LAG, pd.Timestamp("2020-05-31")),
    ],
)
def test_sentinel_falls_back_and_keeps_exact_dependency_key(policy, expected):
    got = apply_pit_policy(
        _single_pit_fact(
            first_disclosure_date=None,
            disclosure_quality="sentinel",
        ),
        policy,
    )
    assert got.loc[0, "ann_date"] == expected
    assert not bool(got.loc[0, "true_first_disclosure_verified"])
    assert got.loc[0, "unverified_dependency_keys"] == (
        "X|2020-03-31|income|csmar",
    )


def test_first_disclosure_before_period_end_is_unverified_fallback():
    got = apply_pit_policy(
        _single_pit_fact(
            first_disclosure_date="2020-03-30",
            disclosure_quality="ok",
        ),
        POLICY_MAIN,
    )
    assert got.loc[0, "ann_date"] == pd.Timestamp("2020-04-15")
    assert not bool(got.loc[0, "true_first_disclosure_verified"])
```

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'first_disclosure or sentinel_falls_back' -q
```

Expected: FAIL because the raw contract and policy do not yet consume first-disclosure columns.

- [ ] **Step 3: Implement the minimal raw contract and policy**

In `_validate_raw_financial_facts`, require the two columns, parse the date nullable, and validate non-null quality values:

```python
out["first_disclosure_date"] = _strict_datetime_series(
    out["first_disclosure_date"],
    "raw financial facts.first_disclosure_date",
    nullable=True,
)
quality = out["disclosure_quality"]
invalid_quality = quality.notna() & ~quality.isin({"ok", "sentinel"})
if invalid_quality.any():
    raise DataBlocked("raw financial facts.disclosure_quality is invalid")
```

Replace the CSMAR section of `apply_pit_policy` with the following decision table; keep the existing Wind branch unchanged:

```python
valid_first = (
    csmar
    & out["disclosure_quality"].eq("ok")
    & out["first_disclosure_date"].notna()
    & out["first_disclosure_date"].ge(out["end_date"])
)
fallback = csmar & ~valid_first
out.loc[valid_first, "ann_date"] = out.loc[valid_first, "first_disclosure_date"]
out.loc[valid_first, "known_date_source"] = "stock_first_disclosure"
out.loc[valid_first, "true_first_disclosure_verified"] = True
out.loc[fallback, "known_date_source"] = f"{policy}_fallback"

def dependency_key(row) -> str:
    return "|".join(
        [
            str(row.ts_code),
            str(pd.Timestamp(row.end_date).date()),
            str(row.statement_type),
            str(row.data_source),
        ]
    )

out["unverified_dependency_keys"] = [
    () if bool(verified) else (dependency_key(row),)
    for row, verified in zip(
        out.itertuples(index=False),
        out["true_first_disclosure_verified"],
    )
]
```

The main fallback date remains `min(stored_ann_date, legal_deadline)` with null stored date falling back to legal deadline; lag remains `legal_deadline + MonthEnd(1)`.

- [ ] **Step 4: Run GREEN and the existing PIT contract tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'pit_policy or first_disclosure or sentinel_falls_back or csmar_missing_stored' -q
```

Expected: PASS, including A1 dividend and both fallback policies.

- [ ] **Step 5: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): apply verified first disclosure dates"
```

### Task 3: Join and bind `stock_first_disclosure` database evidence

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Add failing SQL and recorder assertions**

Update `_RAW_FINANCIAL_COLUMNS` and `_raw_db_row` to include `first_disclosure_date` and `disclosure_quality`. Add:

```python
def test_fetch_raw_financial_joins_and_records_first_disclosure(monkeypatch):
    recorder = DatabaseEvidenceRecorder()
    connection = _patch_raw_financial_connection(
        monkeypatch,
        [_raw_db_row(first_disclosure_date="2020-04-17", disclosure_quality="ok")],
    )
    got = _fetch_raw_financial(
        ["X"], "2020-01-01", "2020-12-31", {"schema": "public"}, recorder
    )
    sql = connection._cursor.executed_sql[0]
    assert "LEFT JOIN public.stock_first_disclosure" in sql
    assert got.loc[0, "first_disclosure_date"] == pd.Timestamp("2020-04-17")
    payload = recorder.payload()
    assert "public.stock_financial" in payload["consumed_sources"]
    assert "public.stock_first_disclosure" in payload["consumed_sources"]
```

Adapt the fake cursor to retain each executed SQL string without changing its batch behavior.

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'joins_and_records_first_disclosure or fetch_raw_financial_batches' -q
```

Expected: FAIL because the query has no join and the recorder has no disclosure source.

- [ ] **Step 3: Implement the exact joined query and evidence entries**

Use aliases and keep all filters on `sf`:

```sql
SELECT sf.ts_code,
       sf.end_date,
       sf.ann_date AS stored_ann_date,
       sf.statement_type,
       sf.data,
       sf.data_source,
       fd.first_disclosure_date,
       fd.quality AS disclosure_quality
FROM {schema}.stock_financial AS sf
LEFT JOIN {schema}.stock_first_disclosure AS fd
  ON fd.ts_code = sf.ts_code
 AND fd.end_date = sf.end_date
WHERE sf.ts_code = ANY(%s)
  AND sf.end_date BETWEEN %s AND %s
  AND ((sf.data_source = 'csmar' AND sf.end_date <= %s)
    OR (sf.data_source = 'wind' AND sf.end_date > %s))
ORDER BY sf.ts_code, sf.statement_type, sf.end_date
```

For every non-empty batch, record the same bound join result under both consumed source names:

```python
recorder.record(f"{db['schema']}.stock_financial", sql, facts, "end_date")
recorder.record(f"{db['schema']}.stock_first_disclosure", sql, facts, "end_date")
```

- [ ] **Step 4: Run GREEN and evidence round-trip tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'fetch_raw_financial or database_evidence' -q
```

Expected: PASS; batching remains identical and the source list is sorted.

- [ ] **Step 5: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): bind first disclosure query evidence"
```

### Task 4: Propagate provenance through YTD-to-TTM

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `signals/common/factors.py`

- [ ] **Step 1: Add failing TTM topology tests**

Create a 5-quarter YTD frame with every fact verified, then mark only prior-year Q1 false. Assert current-year Q1 TTM is false because its formula uses both Q1 observations; mark a fact outside a later valid four-quarter dependency set and assert no effect. Also assert a frame without provenance still returns exactly `['ttm', 'known_date']`.

```python
def test_pit_ttm_provenance_uses_only_actual_ytd_difference_dependencies():
    frame = _five_quarter_ytd_with_provenance()
    frame.loc[0, "true_first_disclosure_verified"] = False
    frame.loc[0, "unverified_dependency_keys"] = ("X|2019-03-31|income|csmar",)
    got = pit_ttm_with_known(frame)
    q1 = got.loc[pd.Timestamp("2020-03-31")]
    assert not bool(q1["true_first_disclosure_verified"])
    assert q1["unverified_dependency_keys"] == (
        "X|2019-03-31|income|csmar",
    )


def test_pit_ttm_without_provenance_keeps_legacy_schema():
    got = pit_ttm_with_known(_five_quarter_ytd_with_provenance().drop(
        columns=["true_first_disclosure_verified", "unverified_dependency_keys"]
    ))
    assert list(got.columns) == ["ttm", "known_date"]
```

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py \
  -k 'ttm_provenance or ttm_without_provenance' -q
```

Expected: FAIL because provenance columns are currently dropped.

- [ ] **Step 3: Preserve optional columns in field extraction**

In `extract_statement_field`, append optional columns only when both exist:

```python
result = pd.DataFrame(
    {
        "end_date": pd.to_datetime(sub["end_date"].to_numpy()),
        "ann_date": pd.to_datetime(sub["ann_date"].to_numpy()),
        "value": pd.to_numeric(value.to_numpy(), errors="coerce"),
    }
)
optional = ("true_first_disclosure_verified", "unverified_dependency_keys")
if all(column in sub.columns for column in optional):
    result["true_first_disclosure_verified"] = sub[
        "true_first_disclosure_verified"
    ].to_numpy(dtype=bool)
    result["unverified_dependency_keys"] = sub[
        "unverified_dependency_keys"
    ].to_numpy(dtype=object)
return result
```

- [ ] **Step 4: Add exact dependency union to `pit_ttm_with_known`**

After the existing earliest-row selection, validate each tuple and use the same selected rows and numeric TTM grid:

```python
def _dependency_union(values) -> tuple[str, ...]:
    return tuple(sorted({key for value in values for key in value}))


def _ttm_unverified_dependencies(selected, full, ttm):
    by_period = selected.set_index(pd.PeriodIndex(selected["end_date"], freq="Q"))
    ytd = pd.Series(selected["value"].to_numpy(dtype=float), index=by_period.index)
    raw = pd.Series(by_period["unverified_dependency_keys"], index=by_period.index)
    single = pd.Series([None] * len(full), index=full, dtype=object)
    for period in full:
        current = ytd.get(period, np.nan)
        quarter = period.quarter
        if pd.isna(current):
            continue
        if quarter == 1:
            single.at[period] = tuple(raw.at[period])
            continue
        previous = period - 1
        if pd.notna(ytd.get(previous, np.nan)):
            single.at[period] = _dependency_union(
                [tuple(raw.at[period]), tuple(raw.at[previous])]
            )
    out = []
    for position in range(len(full)):
        if position < 3 or pd.isna(ttm.iloc[position]):
            out.append(None)
            continue
        window = single.iloc[position - 3 : position + 1]
        out.append(None if window.isna().any() else _dependency_union(window))
    return pd.Series(out, index=full, dtype=object)
```

When provenance input is present, append for valid TTM rows:

```python
dependencies = _ttm_unverified_dependencies(df, full, ttm)
result["unverified_dependency_keys"] = dependencies.to_numpy(dtype=object)
result["true_first_disclosure_verified"] = dependencies.map(
    lambda value: isinstance(value, tuple) and len(value) == 0
).to_numpy(dtype=bool)
```

- [ ] **Step 5: Run GREEN and all factor tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py -q
```

Expected: PASS; legacy schema test proves B1 compatibility.

- [ ] **Step 6: Commit**

```bash
git add signals/common/factors.py tests/test_factors.py
git commit -m "feat: propagate disclosure provenance through TTM"
```

### Task 5: Propagate provenance through slope and ticker pools

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `tests/test_style_basket.py`
- Modify: `signals/common/factors.py`
- Modify: `signals/style_basket/build.py`

- [ ] **Step 1: Add failing slope and pool tests**

Add a 13-observation TTM grid where the first observation is false. Assert the first 12-quarter slope is false and the next slope is true after the false observation leaves the window. Extend `_ticker_facts` with verified/tuple columns and assert TTM, slope, equity and A1 dps rows contain the propagated columns. Keep a second call without provenance and assert the legacy pool column lists are unchanged.

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py \
  -k 'slope_provenance or financial_rows_provenance or legacy_pool_schema' -q
```

Expected: FAIL because slope and pool builders do not retain provenance.

- [ ] **Step 3: Extend `rolling_growth_slope` compatibly**

Add optional keyword-only series while leaving all existing calls valid:

```python
def rolling_growth_slope(
    ttm: pd.Series,
    known: pd.Series,
    n: int = 12,
    *,
    unverified_dependencies: pd.Series | None = None,
) -> pd.DataFrame:
```

After the existing numeric result is complete, append only when the optional series is supplied:

```python
dependencies = []
for position in range(len(idx)):
    if position < n - 1 or pd.isna(out.iloc[position]["slope"]):
        dependencies.append(None)
        continue
    window = unverified_dependencies.iloc[position - n + 1 : position + 1]
    dependencies.append(
        None if window.isna().any() else _dependency_union(window)
    )
out["unverified_dependency_keys"] = dependencies
out["true_first_disclosure_verified"] = out[
    "unverified_dependency_keys"
].map(lambda value: isinstance(value, tuple) and len(value) == 0).astype(bool)
```

- [ ] **Step 4: Preserve the columns in `ticker_financial_rows`**

For TTM rows, copy both optional columns from the grid. Pass `grid['unverified_dependency_keys']` to slope. For Wind direct TTM and event rows, copy the selected raw fact columns directly. Only include the optional columns when the input facts contain them, so public B1 retains its historical schema.

The added row construction must use this exact pair:

```python
provenance = {
    "true_first_disclosure_verified": valid[
        "true_first_disclosure_verified"
    ].to_numpy(dtype=bool),
    "unverified_dependency_keys": valid[
        "unverified_dependency_keys"
    ].to_numpy(dtype=object),
}
```

- [ ] **Step 5: Run GREEN and full style-basket unit tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py -q
```

Expected: PASS with legacy and provenance paths both covered.

- [ ] **Step 6: Commit**

```bash
git add signals/common/factors.py signals/style_basket/build.py \
  tests/test_factors.py tests/test_style_basket.py
git commit -m "feat: carry disclosure provenance into factor pools"
```

### Task 6: Aggregate only actual non-null factor dependencies

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Add failing monthly aggregation tests**

Use the existing monkeypatched `test_build_policy_snapshots_assembles_eligibility_and_provenance` fixture. Return per-factor tuples from fake pools and assert:

```python
assert bool(got.set_index("ticker").loc["A", "true_first_disclosure_verified"])
assert got.set_index("ticker").loc["A", "_unverified_dependency_keys"] == ()
```

Add three focused cases:

```python
def test_unselected_historical_sentinel_does_not_block_model_row(...):
    # false dependency exists in an older pool row; as-of selects a later verified row
    assert bool(model_row["true_first_disclosure_verified"])


def test_selected_sentinel_blocks_model_row_and_names_dependency(...):
    assert not bool(model_row["true_first_disclosure_verified"])
    assert model_row["_unverified_dependency_keys"] == (
        "X|2019-03-31|income|csmar",
    )


def test_financial_cfp_is_not_a_dependency_after_cfp_is_masked(...):
    assert pd.isna(factors.loc["BANK", "cfp"])
    assert bool(model_row["true_first_disclosure_verified"])
```

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'historical_sentinel or selected_sentinel or cfp_is_not_a_dependency or assembles_eligibility' -q
```

Expected: FAIL because current code marks every ticker with any historical CSMAR fact false.

- [ ] **Step 3: Keep provenance in the derived pools**

Add `true_first_disclosure_verified` and `unverified_dependency_keys` to the B3 pool schema. Validate that every non-null numeric pool row has a bool and a tuple; a false row with an empty tuple is `DataBlocked`.

- [ ] **Step 4: Build factor dependency frames from the exact as-of rows**

Next to `factors`, create an object frame with the same six columns and fill it from `asof_selected` results:

```python
factor_dependencies = pd.DataFrame(index=eligible_tickers, columns=factors.columns, dtype=object)
factor_dependencies["sal_g"] = rev_slope_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies["pro_g"] = np_slope_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies["ep"] = np_ttm_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies["bp"] = equity_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies["cfp"] = cfo_ttm_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies["dp"] = dps_selected[
    "unverified_dependency_keys"
].reindex(eligible_tickers)
factor_dependencies.loc[financial, "cfp"] = ()
```

Aggregate only where `factors.notna()` is true:

```python
verified = pd.Series(False, index=base, dtype=bool)
row_dependencies = pd.Series([()] * len(base), index=base, dtype=object)
for ticker in eligible_tickers:
    if pd.isna(style_score.loc[ticker]):
        continue
    used = list(factors.columns[factors.loc[ticker].notna()])
    selected = factor_dependencies.loc[ticker, used]
    if selected.map(lambda value: isinstance(value, tuple)).eq(False).any():
        raise DataBlocked(f"missing factor provenance for {ticker}")
    dependencies = _dependency_union(selected)
    row_dependencies.loc[ticker] = dependencies
    verified.loc[ticker] = len(dependencies) == 0
```

Write `verified` and the private `_unverified_dependency_keys` column into the snapshot. Remove the old `has_csmar_dependency` block completely.

- [ ] **Step 5: Run GREEN**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'provenance or historical_sentinel or selected_sentinel or cfp_is_not_a_dependency' -q
```

Expected: PASS; actual selected false facts block, irrelevant facts do not.

- [ ] **Step 6: Commit**

```bash
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): verify actual style factor dependencies"
```

### Task 7: Publish precise diagnostics without polluting exposures

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_exposures.py`

- [ ] **Step 1: Add failing strict-bool and diagnostics tests**

Create a valid model snapshot with one false row and a private tuple. Assert `compute_month_exposures` retains the public boolean, drops the private column from both `result.size` and `result.model`, and emits deterministic JSON:

```python
details = json.loads(
    result.diagnostics["unverified_first_disclosure_dependencies_json"]
)
assert details == [
    {
        "ticker": "X",
        "dependencies": ["X|2019-03-31|income|csmar"],
    }
]
assert "_unverified_dependency_keys" not in result.size.columns
assert "_unverified_dependency_keys" not in result.model.columns
assert "true_first_disclosure_verified" in result.size.columns
```

Parametrize the public flag with `None`, string values and numeric values; expect `DataBlocked` before exposure calculation.

- [ ] **Step 2: Run RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'unverified_first_disclosure_dependencies or disclosure_verified_requires' -q
```

Expected: FAIL because no strict validation or diagnostic stripping exists.

- [ ] **Step 3: Implement validation, diagnostics and stripping**

At the start of `compute_month_exposures`, validate the public flag with `isinstance(value, (bool, np.bool_))`. Validate private tuples for model rows and construct sorted JSON with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(',', ':'))`.

Change the dataclass annotation to:

```python
diagnostics: dict[str, float | int | str]
```

Before returning, drop only the private column:

```python
size = size.drop(columns=["_unverified_dependency_keys"], errors="ignore")
model = model.drop(columns=["_unverified_dependency_keys"], errors="ignore")
diagnostics["unverified_first_disclosure_model_rows"] = len(details)
diagnostics["unverified_first_disclosure_dependencies_json"] = json.dumps(
    details,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

- [ ] **Step 4: Run GREEN and prove flattened artifact schema**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'unverified_first_disclosure or flatten_exposures or disclosure_verified' -q
```

Expected: PASS; `monthly_exposures` contains the public bool but never the private tuple.

- [ ] **Step 5: Commit**

```bash
git add signals/style_basket/b3_exposures.py tests/test_b3_exposures.py
git commit -m "feat(b3): report disclosure dependency gaps"
```

### Task 8: Run integration verification and review before long-run deployment

**Files:**
- Verify: all modified files and B3 tests.

- [ ] **Step 1: Run focused suites**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py \
  tests/test_style_basket.py \
  tests/test_b3_exposures.py -q
```

Expected: exit 0, no failures.

- [ ] **Step 2: Run the frozen B3 suites**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  tests/test_b3_portfolios_states.py \
  tests/test_b3_structure.py \
  tests/test_b3_eval.py -q
```

Expected: exit 0, no failures.

- [ ] **Step 3: Run the full repository suite**

```bash
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected: exit 0. Record the exact passed count and warnings in the execution log.

- [ ] **Step 4: Verify scope and request code review**

```bash
git diff archive/b3-wind-share-capital-tail-20260814 --check
git diff archive/b3-wind-share-capital-tail-20260814 --stat
git status --short
```

Use `superpowers:requesting-code-review` to check PIT semantics, A1 dividend, dependency topology, B1 compatibility, diagnostics stripping and database evidence. Resolve all important findings with new failing tests first.

- [ ] **Step 5: Commit review-only corrections if any**

```bash
git add signals/common/factors.py signals/style_basket/build.py \
  signals/style_basket/b3_build.py signals/style_basket/b3_exposures.py \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py
git commit -m "fix(b3): harden first disclosure provenance"
```

Skip this commit when the index is empty.

### Task 9: Build on WSL2, audit coverage, then formally evaluate

**Files:**
- Create run artifacts under: `data_fixes/2026-09-01-b3-true-disclosure-formal/`
- Verify with: `tools/audit_b3_disclosure_coverage.py`
- Modify after verdict: `docs/plans/research_registry.yaml`
- Regenerate after verdict: `docs/plans/README.md`

- [ ] **Step 1: Read the runbook and verify coexistence**

Read `docs/plans/2026-08-10-wsl2-runbook.md` completely. Verify the H9 anchor, systemd elapsed time, available space, Wind gateway and absence/identity of any existing B3 process. Do not kill, restart or reconfigure any process.

- [ ] **Step 2: Transfer the implementation as a Git bundle**

On the development machine:

```bash
git bundle create /tmp/b3-true-disclosure-20260901.bundle \
  fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
sha256sum /tmp/b3-true-disclosure-20260901.bundle
scp -P 2222 /tmp/b3-true-disclosure-20260901.bundle \
  ghls@100.120.152.1:D:/deploy_stage/wsl2/
```

In WSL, verify the hash, fetch the branch into `/home/ghls/style_timing_signal`, create `/home/ghls/style_timing_signal-b3-true-disclosure` as a separate worktree, and copy the existing WSL runtime `config/settings.yaml` into that ignored path. Do not alter `/home/ghls/style_timing_signal` tracked files.

- [ ] **Step 3: Run WSL tests before data access**

```bash
cd /home/ghls/style_timing_signal-b3-true-disclosure
.venv/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q \
  -p no:cacheprovider
```

Expected: exit 0, no failures.

- [ ] **Step 4: Run preflight and build into a new root**

Use a base64-transferred, hash-verified script in tmux with timestamped heartbeat and `/usr/bin/time -v`. The commands inside the script are:

```bash
RUN_ROOT=/home/ghls/b3_runs/20260901_true_disclosure
mkdir -p "$RUN_ROOT/logs" "$RUN_ROOT/research" "$RUN_ROOT/backtest"
/usr/bin/time -v .venv/bin/python -m signals.style_basket.b3_build \
  --stage preflight --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/preflight.stdout.log" \
  2>"$RUN_ROOT/logs/preflight.stderr.log"
/usr/bin/time -v .venv/bin/python -m signals.style_basket.b3_build \
  --stage all --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/build.stdout.log" \
  2>"$RUN_ROOT/logs/build.stderr.log"
```

Expected: both commands exit 0. If either exits nonzero, preserve logs and stop.

- [ ] **Step 5: Independently audit coverage on the committed main audit code**

Copy only `monthly_exposures.csv.gz` back to a new local `/tmp` path and verify its SHA256. From the calendar/audit worktree at commit containing `12689cc`, run:

```bash
/home/elfbob/miniconda3/bin/python -m tools.audit_b3_disclosure_coverage \
  --input /tmp/b3-20260901-monthly_exposures.csv.gz \
  --config /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/signals/style_basket/b3_config.yaml \
  --output-dir /tmp/b3-coverage-audit-20260901
```

Expected: exit 0; `coverage_ready=true`; denominator equals the model-row count; numerator equals denominator; each policy covers exactly 2014-10 through 2023-12.

If exit is 1, do not run eval. Read `uncovered_model_rows.csv` and the WSL `exposure_diagnostics.csv`, report exact `(policy, formation_date, ticker, dependencies)`, and return to the first incorrect TDD task without changing formulas or thresholds. Exit 2 is an invalid artifact and must be diagnosed before any further stage.

- [ ] **Step 6: Run formal eval only after the100% gate**

In the same immutable WSL run root:

```bash
/usr/bin/time -v .venv/bin/python -m backtest.b3_eval \
  --data-end 2023-12-31 \
  --research-output-dir /home/ghls/b3_runs/20260901_true_disclosure/research \
  --backtest-output-dir /home/ghls/b3_runs/20260901_true_disclosure/backtest \
  >/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stdout.log \
  2>/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stderr.log
```

Allowed exit codes are 0 and 2, matching the frozen formal harness. Interpret the verdict only from `backtest/run_manifest.json` and `backtest/verdicts.csv`, not from process exit alone.

- [ ] **Step 7: Freeze and retrieve the new immutable evidence**

Create an inventory with every file path, byte size and SHA256, tar the run root without modifying it, compute archive SHA256, and retrieve the archive plus inventory to `data_fixes/2026-09-01-b3-true-disclosure-formal/`. Recompute hashes locally. Re-verify the old archive hash remains `a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843`.

- [ ] **Step 8: Update the research registry deterministically**

Edit only the `b3-continuous-style-state` entry:

- coverage=100%, no remaining final blocker, statistical verdict STOP: `status: closed`, `outcome: stop`, `evidence_level: immutable_formal_run`, claim the true-PIT frozen rerun is terminal negative, and set a reopen condition requiring a genuinely new preregistered hypothesis or material data/method change.
- any remaining frozen data blocker: keep `status: provisional`, `outcome: data_blocked`, replace the old true-disclosure caveat with the exact remaining blocker and its immutable evidence.
- statistical/final pass without production adoption: `status: provisional`, `outcome: pass`, `production.affects: false`, and state that shadow/adoption is a separate decision.

In every branch, add the approved spec, new run manifest, verdicts, coverage audit and archive inventory to `documents`; remove claims that true-disclosure coverage is still pending.

Validate and render:

```bash
/home/elfbob/miniconda3/bin/python -m tools.research_registry check
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --write
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_research_registry.py tests/test_b3_disclosure_coverage_audit.py -q
```

Expected: all commands exit 0.

- [ ] **Step 9: Final verification and evidence commit**

Use `superpowers:verification-before-completion` and run:

```bash
git diff --check
git status --short
sha256sum \
  /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
```

Commit only the new audit core, inventory, registry and rendered README; do not commit the large archive if repository policy keeps it external:

```bash
git add data_fixes/2026-09-01-b3-true-disclosure-formal \
  docs/plans/research_registry.yaml docs/plans/README.md
git commit -m "research(b3): record true disclosure formal verdict"
```

The final report must state exact coverage counts, formal statistical/final verdicts, remaining blockers, new and old archive hashes, implementation commit range, test counts, and whether B3 is now terminally closed or still provisional.
