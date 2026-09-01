# B3 True First-Disclosure Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在冻结 B3 标签上接入报告真实首披日，并按实际参与 `style_score` 的财务依赖传播验证状态；只有 model 行覆盖100%后才原样正式重跑。

**Architecture:** 现有数值函数仍是唯一计算路径；可选 provenance 只附加依赖列。raw fetch 左连接 `stock_first_disclosure`；TTM/slope 传播未验证依赖键；一个纯 helper 汇总实际非空因子；内部键进入 diagnostics 后从正式 exposures 中剔除。

**Tech Stack:** Python 3.13、pandas、NumPy、psycopg2、pytest、PostgreSQL（只读）、Git worktree、WSL2/tmux。

---

## Fixed boundaries

- Spec: `docs/superpowers/specs/2026-08-31-b3-true-first-disclosure-provenance-approved-design.md` at `8899775`.
- Frozen base: `archive/b3-wind-share-capital-tail-20260814` / `41ed581`.
- Never modify the historical B3 worktree or overwrite the old archive. Old archive SHA256: `a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843`.
- No database writes/DDL, formula changes, parameter changes, candidate substitution, or 金时6号 project.

### Task 1: Create the isolated frozen worktree and baseline

**Files:**
- Verify only; no tracked changes.

- [ ] **Step 1: Create isolation with `superpowers:using-git-worktrees`**

After verifying `.worktrees` is ignored:

```bash
git worktree add \
  /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-provenance \
  -b fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
cd /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-true-disclosure-provenance
cp /home/elfbob/claude-code/style_timing_signal/config/settings.yaml config/settings.yaml
git status --short
git rev-parse --short HEAD
```

Expected: tracked status clean; HEAD `41ed581`; historical worktree unchanged.

- [ ] **Step 2: Run baseline tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q
```

Expected: exit 0. Diagnose any baseline failure before continuing.

### Task 2: Add raw first-disclosure, A1 PIT, SQL join, and evidence

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`

- [ ] **Step 1: Write failing PIT tests**

Extend every explicit raw-fact fixture with the two required columns. The shared helper becomes:

```python
def _single_pit_fact(**overrides):
    row = {
        "ts_code": "X",
        "end_date": "2020-03-31",
        "stored_ann_date": "2020-04-15",
        "statement_type": "income",
        "data": {"revenue": 1.0},
        "data_source": "csmar",
        "first_disclosure_date": None,
        "disclosure_quality": None,
    }
    row.update(overrides)
    return pd.DataFrame([row])
```

Append `first_disclosure_date` and `disclosure_quality` to the missing-column parametrization and all pre-existing raw DataFrames. Wind fixtures use nulls; existing CSMAR fallback fixtures use null/sentinel. Replace the DB helper columns and extend its row:

```python
_RAW_FINANCIAL_COLUMNS = [
    "ts_code",
    "end_date",
    "stored_ann_date",
    "statement_type",
    "data",
    "data_source",
    "first_disclosure_date",
    "disclosure_quality",
]

# inside _raw_db_row
row.update(
    {
        "first_disclosure_date": None,
        "disclosure_quality": None,
    }
)
```

Make SQL capture explicit in the fake cursor:

```python
# in _RawFinancialCursor.__init__
self.executed_sql = []

# first line of _RawFinancialCursor.execute
self.executed_sql.append(sql)
```

Add:

```python
@pytest.mark.parametrize("statement_type", ["income", "balance", "cashflow_direct", "dividend"])
@pytest.mark.parametrize("policy", [POLICY_MAIN, POLICY_LAG])
def test_valid_first_disclosure_controls_both_policies(statement_type, policy):
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
def test_sentinel_falls_back_unverified(policy, expected):
    got = apply_pit_policy(
        _single_pit_fact(disclosure_quality="sentinel"),
        policy,
    )
    assert got.loc[0, "ann_date"] == expected
    assert not bool(got.loc[0, "true_first_disclosure_verified"])
    assert got.loc[0, "unverified_dependency_keys"] == (
        "X|2020-03-31|income|csmar",
    )


def test_early_first_disclosure_is_unverified_fallback():
    got = apply_pit_policy(
        _single_pit_fact(
            first_disclosure_date="2020-03-30",
            disclosure_quality="ok",
        ),
        POLICY_MAIN,
    )
    assert got.loc[0, "ann_date"] == pd.Timestamp("2020-04-15")
    assert not bool(got.loc[0, "true_first_disclosure_verified"])


def test_unknown_disclosure_quality_is_data_blocked():
    with pytest.raises(DataBlocked, match="disclosure_quality"):
        apply_pit_policy(
            _single_pit_fact(disclosure_quality="guessed"),
            POLICY_MAIN,
        )
```

- [ ] **Step 2: Verify PIT RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'first_disclosure or sentinel_falls_back or disclosure_quality' -q
```

Expected: FAIL because raw validation/policy ignores first disclosure.

- [ ] **Step 3: Implement raw validation and the policy table**

Require both columns by adding them to `_validate_raw_financial_facts.required`, then add:

```python
out["first_disclosure_date"] = _strict_datetime_series(
    out["first_disclosure_date"],
    "raw financial facts.first_disclosure_date",
    nullable=True,
)
quality = out["disclosure_quality"]
if (quality.notna() & ~quality.isin({"ok", "sentinel"})).any():
    raise DataBlocked("raw financial facts.disclosure_quality is invalid")
```

Keep existing legal dates and Wind behavior; replace CSMAR selection with:

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

Main fallback remains `min(stored_ann_date, legal_deadline)`; lag remains `legal_deadline + MonthEnd(1)`. A1 dividend follows the same report rule.

- [ ] **Step 4: Write the failing joined-query/evidence test**

Extend `_RAW_FINANCIAL_COLUMNS`, `_raw_db_row`, and make the fake cursor save `executed_sql`. Add:

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
    assert len(got) == 1
    payload = recorder.payload()
    assert payload is not None
    assert "public.stock_financial" in payload["consumed_sources"]
    assert "public.stock_first_disclosure" in payload["consumed_sources"]
```

Extend `test_preflight_manifest_database_evidence_contract_roundtrip` before constructing `sources`:

```python
recorder.record(
    "public.stock_first_disclosure",
    "SELECT first_disclosure_date FROM public.stock_first_disclosure",
    pd.DataFrame(
        {
            "end_date": [pd.Timestamp("2020-12-31")],
            "first_disclosure_date": [pd.Timestamp("2021-03-31")],
        }
    ),
    "end_date",
)
```

After `verify_preflight_manifest`, assert:

```python
assert (
    "public.stock_first_disclosure"
    in contract.database_source_evidence["consumed_sources"]
)
assert (
    contract.database_source_evidence["sources"][
        "public.stock_first_disclosure"
    ]["row_count"]
    == 1
)
```

- [ ] **Step 5: Verify query RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'joins_and_records_first_disclosure or fetch_raw_financial_batches' -q
```

Expected: the join/evidence test fails; existing batching still passes.

- [ ] **Step 6: Implement the joined SQL and evidence**

```sql
SELECT sf.ts_code, sf.end_date, sf.ann_date AS stored_ann_date,
       sf.statement_type, sf.data, sf.data_source,
       fd.first_disclosure_date, fd.quality AS disclosure_quality
FROM {schema}.stock_financial AS sf
LEFT JOIN {schema}.stock_first_disclosure AS fd
  ON fd.ts_code = sf.ts_code AND fd.end_date = sf.end_date
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

- [ ] **Step 7: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'pit_policy or first_disclosure or sentinel or fetch_raw_financial or database_evidence' -q
git add signals/style_basket/b3_build.py tests/test_b3_exposures.py
git commit -m "feat(b3): consume verified first disclosure evidence"
```

### Task 3: Propagate exact dependencies through YTD-to-TTM

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `signals/common/factors.py`

- [ ] **Step 1: Write failing TTM tests**

```python
def _five_quarter_ytd_with_provenance():
    dates = pd.date_range("2019-03-31", periods=5, freq="QE")
    values = [10.0, 30.0, 60.0, 100.0, 12.0]
    return pd.DataFrame(
        {
            "end_date": dates,
            "ann_date": dates + pd.Timedelta(days=30),
            "value": values,
            "true_first_disclosure_verified": [True] * 5,
            "unverified_dependency_keys": [()] * 5,
        }
    )


def test_pit_ttm_provenance_uses_actual_ytd_dependencies():
    frame = _five_quarter_ytd_with_provenance()
    frame.at[0, "true_first_disclosure_verified"] = False
    frame.at[0, "unverified_dependency_keys"] = (
        "X|2019-03-31|income|csmar",
    )
    got = pit_ttm_with_known(frame)
    row = got.loc[pd.Timestamp("2020-03-31")]
    assert not bool(row["true_first_disclosure_verified"])
    assert row["unverified_dependency_keys"] == (
        "X|2019-03-31|income|csmar",
    )


def test_pit_ttm_rejects_inconsistent_provenance():
    frame = _five_quarter_ytd_with_provenance()
    frame.at[0, "true_first_disclosure_verified"] = False
    with pytest.raises(ValueError, match="provenance"):
        pit_ttm_with_known(frame)


def test_pit_ttm_ignores_later_unselected_restatement_provenance():
    frame = _five_quarter_ytd_with_provenance()
    later = frame.iloc[[0]].copy()
    later["ann_date"] = later["ann_date"] + pd.Timedelta(days=10)
    later["value"] = 999.0
    later["true_first_disclosure_verified"] = False
    later["unverified_dependency_keys"] = [
        ("X|2019-03-31|income|csmar|later-restatement",)
    ]
    got = pit_ttm_with_known(pd.concat([frame, later], ignore_index=True))
    row = got.loc[pd.Timestamp("2020-03-31")]
    assert bool(row["true_first_disclosure_verified"])
    assert row["unverified_dependency_keys"] == ()


def test_pit_ttm_without_provenance_keeps_legacy_schema():
    frame = _five_quarter_ytd_with_provenance().drop(
        columns=["true_first_disclosure_verified", "unverified_dependency_keys"]
    )
    assert list(pit_ttm_with_known(frame).columns) == ["ttm", "known_date"]
```

- [ ] **Step 2: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py \
  -k 'ttm_provenance or ttm_without_provenance or unselected_restatement' -q
```

- [ ] **Step 3: Implement optional extraction and validators**

`extract_statement_field` returns its existing three columns, then conditionally appends:

```python
optional = ("true_first_disclosure_verified", "unverified_dependency_keys")
if all(column in sub.columns for column in optional):
    result["true_first_disclosure_verified"] = sub[
        "true_first_disclosure_verified"
    ].to_numpy(dtype=bool)
    result["unverified_dependency_keys"] = sub[
        "unverified_dependency_keys"
    ].to_numpy(dtype=object)
```

Add:

```python
def _dependency_union(values) -> tuple[str, ...]:
    return tuple(sorted({key for value in values for key in value}))


def _validated_dependency_tuple(flag, value) -> tuple[str, ...]:
    if not isinstance(flag, (bool, np.bool_)):
        raise ValueError("disclosure provenance flag must be bool")
    if not isinstance(value, tuple) or any(
        not isinstance(key, str) or not key for key in value
    ):
        raise ValueError("disclosure provenance dependencies must be string tuple")
    if bool(flag) != (len(value) == 0):
        raise ValueError("disclosure provenance flag and dependencies disagree")
    return value
```

- [ ] **Step 4: Add symbolic dependencies alongside the existing numeric TTM**

Use the same earliest-announcement rows already selected by `pit_ttm_with_known`:

```python
def _ttm_unverified_dependencies(selected, full, ttm):
    periods = pd.PeriodIndex(selected["end_date"], freq="Q")
    ytd = pd.Series(selected["value"].to_numpy(dtype=float), index=periods)
    raw = pd.Series(
        [
            _validated_dependency_tuple(flag, value)
            for flag, value in zip(
                selected["true_first_disclosure_verified"],
                selected["unverified_dependency_keys"],
            )
        ],
        index=periods,
        dtype=object,
    )
    single = pd.Series([None] * len(full), index=full, dtype=object)
    for period in full:
        if pd.isna(ytd.get(period, np.nan)):
            continue
        if period.quarter == 1:
            single.at[period] = raw.at[period]
        else:
            previous = period - 1
            if pd.notna(ytd.get(previous, np.nan)):
                single.at[period] = _dependency_union(
                    (raw.at[period], raw.at[previous])
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

Append dependency and bool columns only when both optional inputs exist.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py -q
git add signals/common/factors.py tests/test_factors.py
git commit -m "feat: propagate disclosure provenance through TTM"
```

### Task 4: Propagate provenance through slope and pooled rows

**Files:**
- Modify: `tests/test_factors.py`
- Modify: `tests/test_style_basket.py`
- Modify: `signals/common/factors.py`
- Modify: `signals/style_basket/build.py`

- [ ] **Step 1: Write complete failing slope tests**

```python
def test_rolling_growth_slope_provenance_expires_with_window():
    index = pd.date_range("2018-03-31", periods=13, freq="QE")
    ttm = pd.Series(np.arange(1.0, 14.0), index=index)
    known = pd.Series(index + pd.Timedelta(days=30), index=index)
    dependencies = pd.Series(
        [("X|2018-03-31|income|csmar",)] + [()] * 12,
        index=index,
        dtype=object,
    )
    got = rolling_growth_slope(
        ttm,
        known,
        n=12,
        unverified_dependencies=dependencies,
    )
    assert not bool(got.iloc[11]["true_first_disclosure_verified"])
    assert got.iloc[11]["unverified_dependency_keys"] == (
        "X|2018-03-31|income|csmar",
    )
    assert bool(got.iloc[12]["true_first_disclosure_verified"])
    assert got.iloc[12]["unverified_dependency_keys"] == ()
```

- [ ] **Step 2: Write complete failing pool compatibility tests**

```python
def test_ticker_financial_rows_preserves_optional_provenance():
    from signals.style_basket.build import ticker_financial_rows

    facts = _ticker_facts()
    facts["true_first_disclosure_verified"] = True
    facts["unverified_dependency_keys"] = [()] * len(facts)
    pools = ticker_financial_rows(facts, growth_n=12)
    for name in ("ttm", "slope", "event"):
        assert "true_first_disclosure_verified" in pools[name].columns
        assert "unverified_dependency_keys" in pools[name].columns


def test_ticker_financial_rows_without_provenance_keeps_legacy_schema():
    from signals.style_basket.build import ticker_financial_rows

    pools = ticker_financial_rows(_ticker_facts(), growth_n=12)
    assert list(pools["ttm"].columns) == [
        "ts_code", "field", "end_date", "known_date", "ttm"
    ]
    assert list(pools["slope"].columns) == [
        "ts_code", "field", "end_date", "known_date", "slope"
    ]
    assert list(pools["event"].columns) == [
        "ts_code", "field", "end_date", "known_date", "value"
    ]


def test_wind_direct_ttm_cfo_inherits_only_its_own_provenance():
    from signals.style_basket.build import ticker_financial_rows

    facts = _ticker_facts()
    facts["true_first_disclosure_verified"] = True
    facts["unverified_dependency_keys"] = [()] * len(facts)
    target = facts["statement_type"].eq("cashflow") & facts["end_date"].eq(
        pd.Timestamp("2022-03-31")
    )
    facts.loc[target, "true_first_disclosure_verified"] = False
    facts.loc[target, "unverified_dependency_keys"] = [
        ("X1|2022-03-31|cashflow|wind",)
    ]
    cfo = ticker_financial_rows(facts, growth_n=12)["ttm"]
    row = cfo[cfo["field"].eq("cfo")].set_index("end_date").loc[
        pd.Timestamp("2022-03-31")
    ]
    assert not bool(row["true_first_disclosure_verified"])
    assert row["unverified_dependency_keys"] == (
        "X1|2022-03-31|cashflow|wind",
    )


def test_all_verified_provenance_does_not_change_pool_values():
    from signals.style_basket.build import ticker_financial_rows

    facts = _ticker_facts()
    legacy = ticker_financial_rows(facts, growth_n=12)
    facts["true_first_disclosure_verified"] = True
    facts["unverified_dependency_keys"] = [()] * len(facts)
    enriched = ticker_financial_rows(facts, growth_n=12)
    private = ["true_first_disclosure_verified", "unverified_dependency_keys"]
    for name in ("ttm", "slope", "event"):
        pd.testing.assert_frame_equal(
            legacy[name], enriched[name].drop(columns=private)
        )
```

- [ ] **Step 3: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py \
  -k 'slope_provenance or preserves_optional_provenance or legacy_schema or wind_direct_ttm_cfo or all_verified_provenance' -q
```

- [ ] **Step 4: Extend slope and pool construction**

Add a keyword-only `unverified_dependencies: pd.Series | None = None` to `rolling_growth_slope`. Reindex it to the TTM index, union each actual n-window, and append bool/tuple columns only when supplied.

In `ticker_financial_rows`, TTM copies optional columns from its grid; slope receives the grid dependency series; direct Wind cfo and event rows copy their selected raw columns. The legacy no-provenance path must not add columns.

Core slope append:

```python
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

- [ ] **Step 5: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py -q
git add signals/common/factors.py signals/style_basket/build.py \
  tests/test_factors.py tests/test_style_basket.py
git commit -m "feat: carry disclosure provenance into factor pools"
```

### Task 5: Aggregate actual factor dependencies and publish safe diagnostics

**Files:**
- Modify: `tests/test_b3_exposures.py`
- Modify: `signals/style_basket/b3_build.py`
- Modify: `signals/style_basket/b3_exposures.py`

- [ ] **Step 1: Write complete failing pure-helper tests**

Create a pure `_aggregate_style_dependency_provenance(factors, dependencies, style_score)` in the wished-for API:

```python
def test_style_dependency_aggregation_ignores_unused_factor():
    factors = pd.DataFrame(
        {"sal_g": [0.1], "pro_g": [0.2], "ep": [0.3], "bp": [np.nan],
         "cfp": [np.nan], "dp": [0.4]},
        index=["X"],
    )
    dependencies = pd.DataFrame(
        {"sal_g": [()], "pro_g": [()], "ep": [()],
         "bp": [("OLD",)], "cfp": [("BANK_CFP",)], "dp": [()]},
        index=["X"],
    )
    verified, keys = _aggregate_style_dependency_provenance(
        factors, dependencies, pd.Series({"X": 1.0})
    )
    assert bool(verified.loc["X"])
    assert keys.loc["X"] == ()


def test_style_dependency_aggregation_blocks_selected_dependency():
    factors = pd.DataFrame(
        {"sal_g": [0.1], "pro_g": [0.2], "ep": [0.3], "bp": [0.4],
         "cfp": [0.5], "dp": [0.6]},
        index=["X"],
    )
    dependencies = pd.DataFrame(
        {column: [()] for column in factors.columns},
        index=["X"],
    )
    dependencies.at["X", "ep"] = ("X|2019-03-31|income|csmar",)
    verified, keys = _aggregate_style_dependency_provenance(
        factors, dependencies, pd.Series({"X": 1.0})
    )
    assert not bool(verified.loc["X"])
    assert keys.loc["X"] == ("X|2019-03-31|income|csmar",)
```

- [ ] **Step 2: Write complete failing exposure diagnostics test**

```python
def test_month_exposure_reports_and_strips_unverified_dependencies():
    snapshot = _explicit_snapshot()
    ticker = snapshot.loc[0, "ticker"]
    snapshot["true_first_disclosure_verified"] = True
    snapshot["_unverified_dependency_keys"] = [()] * len(snapshot)
    snapshot.loc[0, "true_first_disclosure_verified"] = False
    snapshot.at[0, "_unverified_dependency_keys"] = (
        f"{ticker}|2019-03-31|income|csmar",
    )
    result = compute_month_exposures(snapshot, load_b3_config())
    details = json.loads(
        result.diagnostics["unverified_first_disclosure_dependencies_json"]
    )
    assert details == [
        {
            "ticker": ticker,
            "dependencies": [f"{ticker}|2019-03-31|income|csmar"],
        }
    ]
    assert "_unverified_dependency_keys" not in result.size.columns
    assert "_unverified_dependency_keys" not in result.model.columns
    assert "true_first_disclosure_verified" in result.size.columns


def test_empty_provenance_does_not_change_exposure_numbers():
    snapshot = _explicit_snapshot()
    legacy = compute_month_exposures(snapshot, load_b3_config())
    snapshot["true_first_disclosure_verified"] = True
    snapshot["_unverified_dependency_keys"] = [()] * len(snapshot)
    enriched = compute_month_exposures(snapshot, load_b3_config())
    pd.testing.assert_frame_equal(
        legacy.size,
        enriched.size.drop(columns=["true_first_disclosure_verified"]),
    )
    pd.testing.assert_frame_equal(
        legacy.model,
        enriched.model.drop(columns=["true_first_disclosure_verified"]),
    )
    assert legacy.q == enriched.q


@pytest.mark.parametrize("bad_flag", [None, "True", 1, np.nan])
def test_disclosure_verification_flag_must_be_strict_boolean(bad_flag):
    snapshot = _explicit_snapshot()
    snapshot["true_first_disclosure_verified"] = True
    snapshot["_unverified_dependency_keys"] = [()] * len(snapshot)
    snapshot["true_first_disclosure_verified"] = snapshot[
        "true_first_disclosure_verified"
    ].astype(object)
    snapshot.loc[0, "true_first_disclosure_verified"] = bad_flag
    with pytest.raises(DataBlocked, match="bool"):
        compute_month_exposures(snapshot, load_b3_config())


def test_false_model_flag_without_private_dependencies_is_data_blocked():
    snapshot = _explicit_snapshot()
    snapshot["true_first_disclosure_verified"] = True
    snapshot.loc[0, "true_first_disclosure_verified"] = False
    with pytest.raises(DataBlocked, match="dependencies"):
        compute_month_exposures(snapshot, load_b3_config())
```

- [ ] **Step 3: Verify RED**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'style_dependency_aggregation or reports_and_strips or empty_provenance or disclosure_verification_flag or false_model_flag' -q
```

- [ ] **Step 4: Implement the pure helper and wire exact as-of rows**

```python
def _aggregate_style_dependency_provenance(factors, dependencies, style_score):
    if not factors.index.equals(dependencies.index):
        raise DataBlocked("factor provenance index mismatch")
    if list(factors.columns) != list(dependencies.columns):
        raise DataBlocked("factor provenance columns mismatch")
    verified = pd.Series(False, index=factors.index, dtype=bool)
    keys = pd.Series([()] * len(factors), index=factors.index, dtype=object)
    for ticker in factors.index:
        if pd.isna(style_score.reindex(factors.index).loc[ticker]):
            continue
        used = list(factors.columns[factors.loc[ticker].notna()])
        selected = dependencies.loc[ticker, used]
        if selected.map(lambda value: isinstance(value, tuple)).eq(False).any():
            raise DataBlocked(f"missing factor provenance for {ticker}")
        union = _dependency_union(selected)
        keys.at[ticker] = union
        verified.at[ticker] = len(union) == 0
    return verified, keys
```

In `build_policy_snapshots`, keep provenance in all pools and retain `asof_selected` frames for the six factor inputs. Construct the exact dependency frame without a second as-of path:

```python
selected_by_factor = {
    "sal_g": asof_selected(pools["slope"], formation_date, "rev"),
    "pro_g": asof_selected(pools["slope"], formation_date, "np"),
    "ep": asof_selected(pools["ttm"], formation_date, "np"),
    "bp": asof_selected(pools["event"], formation_date, "equity"),
    "cfp": asof_selected(pools["ttm"], formation_date, "cfo"),
    "dp": asof_selected(pools["event"], formation_date, "dps"),
}
factor_dependencies = pd.DataFrame(index=eligible_tickers)
for factor_name, selected in selected_by_factor.items():
    if selected.empty:
        factor_dependencies[factor_name] = pd.Series(
            np.nan, index=eligible_tickers, dtype=object
        )
    else:
        factor_dependencies[factor_name] = selected[
            "unverified_dependency_keys"
        ].reindex(eligible_tickers)
factor_dependencies.loc[financial, "cfp"] = pd.Series(
    [()] * int(financial.sum()),
    index=financial.index[financial],
    dtype=object,
)
```

Call the helper, remove the ticker-wide `has_csmar_dependency` block, and expand results explicitly:

```python
verified = pd.Series(False, index=base, dtype=bool)
dependency_keys = pd.Series([()] * len(base), index=base, dtype=object)
eligible_verified, eligible_keys = _aggregate_style_dependency_provenance(
    factors,
    factor_dependencies,
    style_score.reindex(eligible_tickers),
)
verified.loc[eligible_tickers] = eligible_verified
dependency_keys.loc[eligible_tickers] = eligible_keys
snapshot["true_first_disclosure_verified"] = verified.to_numpy(dtype=bool)
snapshot["_unverified_dependency_keys"] = dependency_keys.to_numpy(dtype=object)
```

Non-model/size-only rows therefore retain a strict `False` public flag but do not enter the model-only coverage denominator.

- [ ] **Step 5: Implement strict diagnostics and stripping**

In `compute_month_exposures`, preserve the legacy path when both provenance columns are absent. If the private column exists without the public column, block. When the public column exists, require every value to be an actual `bool`/`np.bool_` with no nulls. If the private column is absent, synthesize empty tuples only when every model flag is true; false model rows without tuples block. When both exist, validate model tuples and require `flag == (len(tuple) == 0)`; size-only flags remain strict booleans but do not enter details or the gate. Sort detail by ticker and change the dataclass annotation to `dict[str, float | int | str]`.

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

- [ ] **Step 6: Verify GREEN and commit**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_exposures.py \
  -k 'provenance or style_dependency_aggregation or reports_and_strips or disclosure_verification_flag or false_model_flag or flatten_exposures' -q
git add signals/style_basket/b3_build.py signals/style_basket/b3_exposures.py \
  tests/test_b3_exposures.py
git commit -m "feat(b3): verify actual style factor dependencies"
```

### Task 6: Verify and review before long-running data access

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run focused, B3, then full suites**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py tests/test_b3_portfolios_states.py \
  tests/test_b3_structure.py tests/test_b3_eval.py -q
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected: all exit 0; record exact counts and warnings.

- [ ] **Step 2: Review scope and code**

```bash
git diff archive/b3-wind-share-capital-tail-20260814 --check
git diff archive/b3-wind-share-capital-tail-20260814 --stat
git status --short
```

Use `superpowers:requesting-code-review` for PIT/A1 semantics, dependency topology, B1 compatibility, diagnostics stripping and evidence. Every correction starts with a failing test. Commit non-empty corrections:

```bash
git add signals/common/factors.py signals/style_basket/build.py \
  signals/style_basket/b3_build.py signals/style_basket/b3_exposures.py \
  tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py
git commit -m "fix(b3): harden first disclosure provenance"
```

### Task 7: Build exposures on WSL2 and enforce the independent 100% coverage gate

**Files:**
- Generate only in new WSL run root and `/tmp` audit root.

- [ ] **Step 1: Read and obey the WSL runbook**

Read `docs/plans/2026-08-10-wsl2-runbook.md` completely. Check H9 anchor, `ps -p 1 -o etimes=`, disk, Wind gateway and existing B3 process identities. Do not kill, restart or reconfigure anything.

- [ ] **Step 2: Transfer a Git bundle and create a separate WSL worktree**

```bash
git bundle create /tmp/b3-true-disclosure-20260901.bundle \
  fix/b3-true-disclosure-provenance \
  archive/b3-wind-share-capital-tail-20260814
sha256sum /tmp/b3-true-disclosure-20260901.bundle
scp -P 2222 /tmp/b3-true-disclosure-20260901.bundle \
  ghls@100.120.152.1:D:/deploy_stage/wsl2/
```

In WSL verify the hash, fetch into `/home/ghls/style_timing_signal`, create `/home/ghls/style_timing_signal-b3-true-disclosure` as a separate worktree, and copy the existing ignored WSL settings file.

- [ ] **Step 3: Test and run the new immutable root**

Use the existing venv by absolute path:

```bash
cd /home/ghls/style_timing_signal-b3-true-disclosure
PY=/home/ghls/style_timing_signal/.venv/bin/python
"$PY" -m pytest tests/test_factors.py tests/test_style_basket.py \
  tests/test_b3_exposures.py -q -p no:cacheprovider
RUN_ROOT=/home/ghls/b3_runs/20260901_true_disclosure
mkdir -p "$RUN_ROOT/logs" "$RUN_ROOT/research" "$RUN_ROOT/backtest"
/usr/bin/time -v "$PY" -m signals.style_basket.b3_build \
  --stage preflight --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/preflight.stdout.log" \
  2>"$RUN_ROOT/logs/preflight.stderr.log"
/usr/bin/time -v "$PY" -m signals.style_basket.b3_build \
  --stage exposures --data-end 2023-12-31 \
  --output-dir "$RUN_ROOT/research" \
  >"$RUN_ROOT/logs/build.stdout.log" \
  2>"$RUN_ROOT/logs/build.stderr.log"
```

Run through a hash-verified base64 script in redirected tmux with heartbeat. Expected: tests, preflight and exposures exit 0. Do not run portfolios, states, structure or eval yet.

- [ ] **Step 4: Audit from a clean committed audit worktree**

Copy `monthly_exposures.csv.gz` to `/tmp/b3-20260901-monthly_exposures.csv.gz` and hash it. Create:

```bash
git worktree add --detach \
  /home/elfbob/claude-code/style_timing_signal/.worktrees/b3-coverage-audit-clean \
  8899775
```

Run from that clean worktree:

```bash
/home/elfbob/miniconda3/bin/python -m tools.audit_b3_disclosure_coverage \
  --input /tmp/b3-20260901-monthly_exposures.csv.gz \
  --config /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/signals/style_basket/b3_config.yaml \
  --output-dir /tmp/b3-coverage-audit-20260901
```

Expected: exit 0, `coverage_ready=true`, numerator=denominator, two policies × 111 months from 2014-10 through 2023-12.

Exit 1: stop before eval and join uncovered rows with `exposure_diagnostics.csv` for exact dependencies. Exit 2: invalid artifact; diagnose. No formula/threshold changes are allowed.

### Task 8: Post-gate states/eval, immutable evidence, and registry verdict

**Files:**
- Create review core in main: `data_fixes/2026-09-01-b3-true-disclosure-formal/`
- Store large archive: `/home/elfbob/claude-code/deploy_backups/2026-09-01-b3-true-disclosure-formal/`
- Modify in main: `docs/plans/research_registry.yaml`
- Regenerate in main: `docs/plans/README.md`

- [ ] **Step 1: Run portfolios/states and eval only after coverage=100%**

```bash
PY=/home/ghls/style_timing_signal/.venv/bin/python
/usr/bin/time -v "$PY" -m signals.style_basket.b3_build \
  --stage states --data-end 2023-12-31 \
  --output-dir /home/ghls/b3_runs/20260901_true_disclosure/research \
  >/home/ghls/b3_runs/20260901_true_disclosure/logs/states.stdout.log \
  2>/home/ghls/b3_runs/20260901_true_disclosure/logs/states.stderr.log
/usr/bin/time -v "$PY" -m backtest.b3_eval \
  --data-end 2023-12-31 \
  --research-output-dir /home/ghls/b3_runs/20260901_true_disclosure/research \
  --backtest-output-dir /home/ghls/b3_runs/20260901_true_disclosure/backtest \
  >/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stdout.log \
  2>/home/ghls/b3_runs/20260901_true_disclosure/logs/eval.stderr.log
```

The states build must exit 0. Eval allowed exit codes: 0 or 2. Interpret only `run_manifest.json` and `verdicts.csv`.

- [ ] **Step 2: Freeze and retrieve evidence**

Create a path/size/SHA256 inventory, tar the untouched WSL run root, and hash it. Retrieve the large archive to the deploy backup directory. Copy inventory, coverage summary/uncovered CSV, preflight manifest, run manifest, verdicts and execution receipt into the main review-core directory. Recompute all hashes and the old archive hash.

- [ ] **Step 3: Update only the B3 registry entry in main**

Deterministic branches:

- coverage100%, no final blocker, statistical STOP: `status: closed`, `outcome: stop`, `evidence_level: immutable_formal_run`;
- any remaining frozen blocker: `status: provisional`, `outcome: data_blocked`, caveat names exact blocker;
- final/statistical pass without adoption: `status: provisional`, `outcome: pass`, `production.affects: false`.

Every branch adds approved spec/new evidence and removes “真首披待补” claims.

```bash
/home/elfbob/miniconda3/bin/python -m tools.research_registry check
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --write
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_research_registry.py tests/test_b3_disclosure_coverage_audit.py -q
```

- [ ] **Step 4: Verify and commit evidence in main**

Use `superpowers:verification-before-completion`:

```bash
git diff --check
git status --short
sha256sum \
  /home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
git add data_fixes/2026-09-01-b3-true-disclosure-formal \
  docs/plans/research_registry.yaml docs/plans/README.md
git commit -m "research(b3): record true disclosure formal verdict"
```

Final report: exact coverage counts, statistical/final verdicts, remaining blockers, both archive hashes, implementation commit range, test counts, and terminal closed versus provisional status.
