# B3 True First-Disclosure Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在冻结 B3 标签上接入报告真实首披日，并按实际参与 `style_score` 的财务依赖传播验证状态；model 行覆盖100%后才原样正式重跑。

**Architecture:** 保留现有数值函数为唯一计算路径，只在输入携带 provenance 时为字段抽取、TTM 和 slope 附加兼容列。B3 raw fetch 左连接 `stock_first_disclosure`，月度快照只汇总实际非空因子的依赖；内部依赖键写入现有 diagnostics 后从正式 exposures 中剔除。

**Tech Stack:** Python 3.13、pandas、NumPy、psycopg2、pytest、PostgreSQL（只读）、Git worktree、WSL2/tmux。

---

## Frozen invariants and file map

- 权威规格：`docs/superpowers/specs/2026-08-31-b3-true-first-disclosure-provenance-approved-design.md`，提交 `8899775`。
- 实现基线：`archive/b3-wind-share-capital-tail-20260814` / `41ed581`。
- 历史 B3 工作树只读；旧 archive SHA256 保持 `a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843`。
- 不做数据库写入、DDL、参数调优、候选替换或金时6号立项。
- Modify `signals/style_basket/b3_build.py`: raw schema、join、PIT、pool、月度依赖。
- Modify `signals/common/factors.py`: 可选 provenance 字段抽取、TTM、slope。
- Modify `signals/style_basket/build.py`: pooled rows 保留 provenance。
- Modify `signals/style_basket/b3_exposures.py`: strict bool、缺口 diagnostics、内部列剔除。
- Modify `tests/test_factors.py`, `tests/test_style_basket.py`, `tests/test_b3_exposures.py`。
- 独立 coverage gate 使用 main 的 `tools/audit_b3_disclosure_coverage.py`，不复制进冻结实现分支。

### Task 1: Create an isolated frozen worktree and prove the baseline

**Files:**
- Verify only; no tracked changes.

- [ ] **Step 1: Create the worktree using `superpowers:using-git-worktrees`**

After verifying `.worktrees` is ignored:

```bash
git worktree add \
  /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-provenance \
  -b fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
```

Expected: HEAD `41ed581`; historical B3 worktree unchanged.

- [ ] **Step 2: Copy only ignored runtime configuration**

```bash
cd /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-provenance
cp /home/elfbob/claude-code/style_timing_signal/config/settings.yaml config/settings.yaml
git status --short
git rev-parse --short HEAD
```

Expected: tracked status clean; HEAD `41ed581`.

- [ ] **Step 3: Run the focused baseline**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q
```

Expected: exit 0. Stop and diagnose any baseline failure.

### Task 2: Implement raw first-disclosure and A1 PIT behavior

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Write failing PIT tests**

Extend `_single_pit_fact` with nullable `first_disclosure_date` and `disclosure_quality`. Add:

```python
@pytest.mark.parametrize("statement_type", ["income", "balance", "cashflow_direct", "dividend"])
@pytest.mark.parametrize("policy", [POLICY_MAIN, POLICY_LAG])
def test_valid_first_disclosure_overrides_both_policies(statement_type, policy):
    got = apply_pit_policy(
        _single_pit_fact(
            statement_type=statement_type,
            first_disclosure_date="2020-04-17",
            disclosure_quality="ok",
        ),
        policy,
    )
    assert got.loc[0, "ann_date"] == pd.Timestamp("2020-04-17")
    assert got.loc[0, "known_date_source"] == "stock_first_disclosure"
    assert bool(got.loc[0, "true_first_disclosure_verified"])
    assert got.loc[0, "unverified_dependency_keys"] == ()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (POLICY_MAIN, pd.Timestamp("2020-04-15")),
        (POLICY_LAG, pd.Timestamp("2020-05-31")),
    ],
)
def test_sentinel_falls_back_and_names_dependency(policy, expected):
    got = apply_pit_policy(
        _single_pit_fact(first_disclosure_date=None, disclosure_quality="sentinel"),
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

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'first_disclosure or sentinel_falls_back' -q
```

Expected: FAIL because current raw policy ignores the new columns.

- [ ] **Step 3: Extend the raw validator**

Require both columns and add:

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

Unparsable dates block; a validly parsed date before `end_date` is unverified fallback.

- [ ] **Step 4: Implement the policy decision table**

Keep the existing legal-date calculation and Wind branch. For CSMAR:

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

def _dependency_key(row) -> str:
    return "|".join(
        (
            str(row.ts_code),
            str(pd.Timestamp(row.end_date).date()),
            str(row.statement_type),
            str(row.data_source),
        )
    )

out["unverified_dependency_keys"] = [
    () if bool(flag) else (_dependency_key(row),)
    for row, flag in zip(
        out.itertuples(index=False),
        out["true_first_disclosure_verified"],
    )
]
```

Main fallback remains `min(stored_ann_date, legal_deadline)` with null stored date using legal deadline; lag remains `legal_deadline + MonthEnd(1)`.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'pit_policy or first_disclosure or sentinel_falls_back or csmar_missing_stored' -q
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): apply verified first disclosure dates"
```

Expected: tests pass; A1 dividend uses the same report first-disclosure rule.

### Task 3: Join and bind `stock_first_disclosure` evidence

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Write the failing SQL/evidence test**

Extend `_RAW_FINANCIAL_COLUMNS` and `_raw_db_row`. Make the fake cursor retain executed SQL. Add:

```python
def test_fetch_raw_financial_joins_and_records_first_disclosure(monkeypatch):
    from backtest.b3_eval import TRADING_CALENDAR_QUERY_TEMPLATE

    recorder = DatabaseEvidenceRecorder()
    recorder.record(
        "public.trading_calendar",
        TRADING_CALENDAR_QUERY_TEMPLATE,
        pd.DataFrame(
            {"calendar_date": [pd.Timestamp("2020-03-31")], "sfe": [True]}
        ),
        "calendar_date",
    )
    connection = _patch_raw_financial_connection(
        monkeypatch,
        [_raw_db_row(first_disclosure_date="2020-04-17", disclosure_quality="ok")],
    )
    got = _fetch_raw_financial(
        ["X"], "2020-01-01", "2020-12-31", {"schema": "public"}, recorder
    )
    assert "LEFT JOIN public.stock_first_disclosure" in connection._cursor.executed_sql[0]
    assert got.loc[0, "first_disclosure_date"] == pd.Timestamp("2020-04-17")
    payload = recorder.payload()
    assert payload is not None
    assert "public.stock_financial" in payload["consumed_sources"]
    assert "public.stock_first_disclosure" in payload["consumed_sources"]
```

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'joins_and_records_first_disclosure or fetch_raw_financial_batches' -q
```

Expected: FAIL because there is no join/evidence entry.

- [ ] **Step 3: Implement the exact joined SQL**

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

For each non-empty batch:

```python
recorder.record(f"{db['schema']}.stock_financial", sql, facts, "end_date")
recorder.record(f"{db['schema']}.stock_first_disclosure", sql, facts, "end_date")
```

- [ ] **Step 4: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'fetch_raw_financial or database_evidence' -q
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): bind first disclosure query evidence"
```

Expected: batching remains identical; source list is sorted and round-trips through the manifest.

### Task 4: Propagate actual YTD dependencies through TTM

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `signals/common/factors.py`

- [ ] **Step 1: Write failing dependency-topology tests**

Build five quarterly YTD observations with provenance. Mark prior-year Q1 false and assert current-year Q1 TTM is false because its formula uses both Q1 rows. Also assert input without provenance keeps exactly `['ttm', 'known_date']`.

```python
def test_pit_ttm_provenance_uses_actual_ytd_difference_dependencies():
    frame = _five_quarter_ytd_with_provenance()
    frame.loc[0, "true_first_disclosure_verified"] = False
    frame.loc[0, "unverified_dependency_keys"] = ("X|2019-03-31|income|csmar",)
    got = pit_ttm_with_known(frame)
    row = got.loc[pd.Timestamp("2020-03-31")]
    assert not bool(row["true_first_disclosure_verified"])
    assert row["unverified_dependency_keys"] == (
        "X|2019-03-31|income|csmar",
    )


def test_pit_ttm_without_provenance_keeps_legacy_schema():
    frame = _five_quarter_ytd_with_provenance().drop(
        columns=["true_first_disclosure_verified", "unverified_dependency_keys"]
    )
    assert list(pit_ttm_with_known(frame).columns) == ["ttm", "known_date"]
```

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py \
  -k 'ttm_provenance or ttm_without_provenance' -q
```

- [ ] **Step 3: Preserve optional columns in `extract_statement_field`**

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

- [ ] **Step 4: Add dependency union to the existing numeric TTM path**

Use the rows already selected by earliest `ann_date`; do not repeat the numeric formula:

```python
def _dependency_union(values) -> tuple[str, ...]:
    return tuple(sorted({key for value in values for key in value}))


def _ttm_unverified_dependencies(selected, full, ttm):
    periods = pd.PeriodIndex(selected["end_date"], freq="Q")
    ytd = pd.Series(selected["value"].to_numpy(dtype=float), index=periods)
    raw = pd.Series(
        selected["unverified_dependency_keys"].to_numpy(dtype=object),
        index=periods,
    )
    single = pd.Series([None] * len(full), index=full, dtype=object)
    for period in full:
        current = ytd.get(period, np.nan)
        if pd.isna(current):
            continue
        if period.quarter == 1:
            single.at[period] = tuple(raw.at[period])
        else:
            previous = period - 1
            if pd.notna(ytd.get(previous, np.nan)):
                single.at[period] = _dependency_union(
                    (tuple(raw.at[period]), tuple(raw.at[previous]))
                )
    dependencies = []
    for position in range(len(full)):
        if position < 3 or pd.isna(ttm.iloc[position]):
            dependencies.append(None)
            continue
        window = single.iloc[position - 3 : position + 1]
        dependencies.append(
            None if window.isna().any() else _dependency_union(window)
        )
    return pd.Series(dependencies, index=full, dtype=object)
```

Append the two columns only when provenance input exists; valid TTM with an empty dependency tuple is verified=True.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py -q
git add signals/common/factors.py tests/test_factors.py
git commit -m "feat: propagate disclosure provenance through TTM"
```

### Task 5: Propagate through slope and pooled financial rows

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `tests/test_style_basket.py`
- Modify: `signals/common/factors.py`
- Modify: `signals/style_basket/build.py`

- [ ] **Step 1: Write failing slope and pool tests**

Use 13 TTM observations: first dependency false, remaining empty. Assert first 12-quarter slope false and next slope true. Extend `_ticker_facts` with provenance and assert TTM, slope, equity and A1 dps rows retain both columns. A second call without provenance must retain legacy pool schemas.

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py \
  -k 'slope_provenance or financial_rows_provenance or legacy_pool_schema' -q
```

- [ ] **Step 3: Extend `rolling_growth_slope` compatibly**

```python
def rolling_growth_slope(
    ttm: pd.Series,
    known: pd.Series,
    n: int = 12,
    *,
    unverified_dependencies: pd.Series | None = None,
) -> pd.DataFrame:
```

After existing numeric output:

```python
if unverified_dependencies is not None:
    aligned = unverified_dependencies.reindex(idx)
    dependencies = []
    for position in range(len(idx)):
        if position < n - 1 or pd.isna(out.iloc[position]["slope"]):
            dependencies.append(None)
            continue
        window = aligned.iloc[position - n + 1 : position + 1]
        dependencies.append(
            None if window.isna().any() else _dependency_union(window)
        )
    out["unverified_dependency_keys"] = dependencies
    out["true_first_disclosure_verified"] = out[
        "unverified_dependency_keys"
    ].map(lambda value: isinstance(value, tuple) and len(value) == 0).astype(bool)
```

- [ ] **Step 4: Preserve optional columns in `ticker_financial_rows`**

TTM rows copy the columns from the TTM grid; slope receives the grid dependency series; Wind direct cfo and event rows copy the selected raw row. Use:

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

Only add this mapping when both columns exist, preserving public B1 behavior.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py -q
git add signals/common/factors.py signals/style_basket/build.py \
  tests/test_factors.py tests/test_style_basket.py
git commit -m "feat: carry disclosure provenance into factor pools"
```

### Task 6: Aggregate only actual non-null style-factor dependencies

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Write failing aggregation tests**

Extend the existing monkeypatched monthly snapshot fixture with per-factor tuples. Add cases proving:

```python
assert bool(model_row["true_first_disclosure_verified"])
assert model_row["_unverified_dependency_keys"] == ()
```

- an older false pool row not selected as-of does not block;
- a selected false row blocks and names its key;
- financial-sector cfp is not a dependency after the frozen mask sets it null.

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'historical_sentinel or selected_sentinel or cfp_is_not_a_dependency or assembles_eligibility' -q
```

Expected: FAIL under the current ticker-wide CSMAR flag.

- [ ] **Step 3: Retain and validate provenance in all three derived pools**

Add both provenance columns to non-empty B3 pool schemas. Each non-null numeric row requires a bool and tuple; false with an empty tuple is `DataBlocked`.

- [ ] **Step 4: Build dependency frames from exact `asof_selected` rows**

Create `factor_dependencies` with the same six columns as `factors`, populated from selected rev slope, np slope, np TTM, equity, cfo TTM and dps rows. Apply the financial cfp exception safely:

```python
for ticker in financial.index[financial]:
    factor_dependencies.at[ticker, "cfp"] = ()
```

Aggregate only `factors.loc[ticker].notna()`:

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
    row_dependencies.at[ticker] = dependencies
    verified.at[ticker] = len(dependencies) == 0
```

Write `verified` and `_unverified_dependency_keys` to the snapshot. Delete the old `has_csmar_dependency` block.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'provenance or historical_sentinel or selected_sentinel or cfp_is_not_a_dependency' -q
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): verify actual style factor dependencies"
```

### Task 7: Emit exact diagnostics without polluting formal exposures

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_exposures.py`

- [ ] **Step 1: Write failing strict-bool and stripping tests**

Create a model snapshot with one false row and tuple. Assert:

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

Parametrize the public flag with `None`, strings and numbers; expect `DataBlocked`.

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'unverified_first_disclosure_dependencies or disclosure_verified_requires' -q
```

- [ ] **Step 3: Implement validation and diagnostics**

Validate public flags with `isinstance(value, (bool, np.bool_))`. If the private column is absent, synthesize empty tuples only when all model flags are true; any false model flag without its tuple is `DataBlocked`. Validate model tuples and sort details by ticker.

Change the dataclass annotation and strip the private column before returning:

```python
diagnostics: dict[str, float | int | str]

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

- [ ] **Step 4: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'unverified_first_disclosure or flatten_exposures or disclosure_verified' -q
git add signals/style_basket/b3_exposures.py tests/test_b3_exposures.py
git commit -m "feat(b3): report disclosure dependency gaps"
```

Expected: formal exposures retain the public bool and never contain the private tuple.

### Task 8: Verify the implementation before any long run

**Files:**
- Verify all modified production and test files.

- [ ] **Step 1: Run focused suites**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q
```

- [ ] **Step 2: Run frozen B3 suites**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py tests/test_b3_portfolios_states.py \
  tests/test_b3_structure.py tests/test_b3_eval.py -q
```

- [ ] **Step 3: Run full suite**

```bash
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected for all three commands: exit 0; record exact pass/warning counts.

- [ ] **Step 4: Review scope and semantics**

```bash
git diff archive/b3-wind-share-capital-tail-20260814 --check
git diff archive/b3-wind-share-capital-tail-20260814 --stat
git status --short
```

Use `superpowers:requesting-code-review` to inspect PIT/A1 semantics, TTM/slope topology, public B1 compatibility, diagnostics stripping and database evidence. Every correction begins with a failing test. Commit non-empty corrections as:

```bash
git add signals/common/factors.py signals/style_basket/build.py \
  signals/style_basket/b3_build.py signals/style_basket/b3_exposures.py \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py
git commit -m "fix(b3): harden first disclosure provenance"
```

### Task 9: Build on WSL2, gate on coverage, then formally evaluate

**Files:**
- Create review core: `data_fixes/2026-09-01-b3-true-disclosure-formal/`
- Store large archive: `/home/elfbob/claude-code/deploy_backups/2026-09-01-b3-true-disclosure-formal/`
- Modify after verdict: `docs/plans/research_registry.yaml`
- Regenerate: `docs/plans/README.md`

- [ ] **Step 1: Read the WSL runbook and verify coexistence**

Read `docs/plans/2026-08-10-wsl2-runbook.md` completely. Check H9 anchor, `ps -p 1 -o etimes=`, disk, Wind gateway and identities of existing B3 processes. Do not kill, restart or reconfigure anything.

- [ ] **Step 2: Transfer a Git bundle and create a separate WSL worktree**

```bash
git bundle create /tmp/b3-true-disclosure-20260901.bundle \
  fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
sha256sum /tmp/b3-true-disclosure-20260901.bundle
scp -P 2222 /tmp/b3-true-disclosure-20260901.bundle \
  ghls@100.120.152.1:D:/deploy_stage/wsl2/
```

In WSL verify the hash, fetch into `/home/ghls/style_timing_signal`, create `/home/ghls/style_timing_signal-b3-true-disclosure` as a separate worktree, and copy the existing ignored WSL `config/settings.yaml`. Do not modify tracked files in the existing WSL checkout.

- [ ] **Step 3: Run focused WSL tests using the existing venv**

```bash
cd /home/ghls/style_timing_signal-b3-true-disclosure
PY=/home/ghls/style_timing_signal/.venv/bin/python
"$PY" -m pytest tests/test_factors.py tests/test_style_basket.py \
  tests/test_b3_exposures.py -q -p no:cacheprovider
```

Expected: exit 0.

- [ ] **Step 4: Run preflight and build in a new immutable root**

Transfer a hash-verified script via the runbook base64 method and run it in redirected tmux with a timestamped heartbeat. Its core commands are:

```bash
RUN_ROOT=/home/ghls/b3_runs/20260901_true_disclosure
PY=/home/ghls/style_timing_signal/.venv/bin/python
mkdir -p "$RUN_ROOT/logs" "$RUN_ROOT/research" "$RUN_ROOT/backtest"
/usr/bin/time -v "$PY" -m signals.style_basket.b3_build \
  --stage preflight --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/preflight.stdout.log" \
  2>"$RUN_ROOT/logs/preflight.stderr.log"
/usr/bin/time -v "$PY" -m signals.style_basket.b3_build \
  --stage all --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/build.stdout.log" \
  2>"$RUN_ROOT/logs/build.stderr.log"
```

Expected: both exit 0. Otherwise preserve logs and stop.

- [ ] **Step 5: Audit coverage from a clean committed audit worktree**

Copy only `monthly_exposures.csv.gz` to `/tmp/b3-20260901-monthly_exposures.csv.gz` and verify SHA256. Create a clean detached worktree:

```bash
git worktree add --detach \
  /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-coverage-audit-clean \
  8899775
```

Run there:

```bash
/home/elfbob/miniconda3/bin/python -m tools.audit_b3_disclosure_coverage \
  --input /tmp/b3-20260901-monthly_exposures.csv.gz \
  --config /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/signals/style_basket/b3_config.yaml \
  --output-dir /tmp/b3-coverage-audit-20260901
```

Expected: exit 0, `coverage_ready=true`, numerator=denominator, both policies contain exactly 111 months from 2014-10 through 2023-12.

Exit 1: do not run eval; join `uncovered_model_rows.csv` with WSL `exposure_diagnostics.csv` and report exact policy/date/ticker/dependencies. Exit 2: artifact invalid; diagnose before continuing. Neither path permits changing formulas or thresholds.

- [ ] **Step 6: Run eval only after coverage=100%**

```bash
PY=/home/ghls/style_timing_signal/.venv/bin/python
/usr/bin/time -v "$PY" -m backtest.b3_eval \
  --data-end 2023-12-31 \
  --research-output-dir /home/ghls/b3_runs/20260901_true_disclosure/research \
  --backtest-output-dir /home/ghls/b3_runs/20260901_true_disclosure/backtest \
  >/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stdout.log \
  2>/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stderr.log
```

Allowed exit codes: 0 or 2, matching the frozen harness. Read verdict only from `run_manifest.json` and `verdicts.csv`.

- [ ] **Step 7: Freeze and retrieve immutable evidence**

Create an inventory of every path, size and SHA256; tar the untouched run root; hash the archive. Retrieve the large archive into `/home/elfbob/claude-code/deploy_backups/2026-09-01-b3-true-disclosure-formal/`. Copy a small review core—inventory, coverage audit summary/uncovered CSV, preflight manifest, run manifest, verdicts and execution receipt—into `data_fixes/2026-09-01-b3-true-disclosure-formal/`. Recompute all hashes locally and recheck the old archive hash.

- [ ] **Step 8: Update registry by deterministic verdict branch**

Edit only `b3-continuous-style-state`:

- coverage100%, no remaining final blocker, statistical STOP → `status: closed`, `outcome: stop`, `evidence_level: immutable_formal_run`;
- any remaining frozen blocker → retain `status: provisional`, `outcome: data_blocked`, replace the old first-disclosure caveat with the exact blocker;
- final/statistical pass without adoption → `status: provisional`, `outcome: pass`, `production.affects: false`, with shadow/adoption explicitly separate.

All branches add the approved spec and new core evidence, and remove claims that true-disclosure coverage is pending.

```bash
/home/elfbob/miniconda3/bin/python -m tools.research_registry check
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --write
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_research_registry.py tests/test_b3_disclosure_coverage_audit.py -q
```

Expected: exit 0.

- [ ] **Step 9: Verify and commit evidence**

Use `superpowers:verification-before-completion`:

```bash
git diff --check
git status --short
sha256sum \
  /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
```

Commit only the review core, registry and rendered README:

```bash
git add data_fixes/2026-09-01-b3-true-disclosure-formal \
  docs/plans/research_registry.yaml docs/plans/README.md
git commit -m "research(b3): record true disclosure formal verdict"
```

Final report: exact coverage counts, statistical/final verdicts, blockers, both archive hashes, implementation commit range, test counts, and terminal closed versus provisional status.
