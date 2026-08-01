# B3 Wind Share-Capital Tail Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the frozen B3 window's remaining 5,781 `DATA_MISSING_SHARES` coordinates with guarded, directly observed Wind month-end share-capital facts, then rerun the unchanged B3 pipeline to a machine-readable statistical and final verdict.

**Architecture:** `stock_selector` owns an evidence-recording Wind read path, pure target/normalization/compression helpers, deterministic proposal publication, and a narrowly guarded PostgreSQL apply/rollback transaction. `style_timing_signal` owns the immutable blocker anchors, proposal and rollback artifacts, post-write coverage verifier, guarded B3 runner, and final evidence. The proposal is read-only; production apply and rollback are separate human gates.

**Tech Stack:** Python 3.11+, pandas, httpx/respx, psycopg2, PostgreSQL, pytest, JSON/CSV/SHA-256 manifests, systemd-run with an 8 GiB memory cap.

---

## Frozen contracts and repository boundaries

The implementation must preserve these facts without reinterpretation:

- `style_timing_signal` input/code baseline: `7e1720e` (contains the approved design); the B3 CLOSE binding baseline is its ancestor `c1adb29`.
- `stock_selector` starting commit: `48b17a7214b49148c0053a0c181004d08712db02`.
- Target anchors and SHA-256 values:
  - `tail.csv`: `93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223`
  - `shares_tail_impact_by_ticker.csv`: `ff8a9123504d430225c0ea0618c6a07c06373b07f2826af8bff450d6a55d7a40`
  - `shares_tail_impact_detail.csv`: `a2fd07c9cdf5ba6b1defb89850eaf5bea629c7e76cf372c95931c745eedead13`
  - `impact_audit_manifest.json`: `777818dd48a0fc4513f74391ce9c57b8bca0f5750173590ba894c93be22b1f74`
- Exact target: 5,781 unique coordinates, 5,445 required coordinates, 56 tickers, and no target row for `688347.SH`.
- Wind request: `total_shares,mkt_cap_ard,close`, `2013-05-01..2023-12-31`, `Period=M;PriceAdj=U;unit=1`.
- Cross-field tolerance: `abs(mkt_cap_ard / close - total_shares) / total_shares <= 1e-4` on every target coordinate.
- Canonical target: PostgreSQL `stock_selector.stock_share_capital`; never write the Pi5 replica directly.
- B3 strategy, configuration, formation grid, PIT policy, exclusion precedence, portfolios, costs, and verdict gates are not code-change targets.

The current repositories contain user-owned untracked files. Do not add, delete, move, or overwrite them. In particular, do not use the existing untracked `backtest/output/b3/`; all new real-run B3 output goes below the new campaign directory.

## Task 1: Prepare isolated implementation worktrees

**Files:**

- Read: `/home/elfbob/claude-code/style_timing_signal/.git`
- Read: `/home/elfbob/claude-code/stock_selector/.git`
- Create through the `using-git-worktrees` skill: isolated worktrees for both repositories

- [ ] **Step 1: Invoke the required worktree skill and inspect both repositories**

Use `using-git-worktrees` before editing implementation files. Confirm the exact starting commits and list all untracked files:

```bash
git -C /home/elfbob/claude-code/style_timing_signal status --short --branch
git -C /home/elfbob/claude-code/style_timing_signal rev-parse HEAD
git -C /home/elfbob/claude-code/style_timing_signal merge-base --is-ancestor 7e1720e HEAD
git -C /home/elfbob/claude-code/stock_selector status --short --branch
git -C /home/elfbob/claude-code/stock_selector rev-parse HEAD
```

Expected: the style ancestor check exits `0` and its exact current HEAD is recorded; upstream HEAD is `48b17a7214b49148c0053a0c181004d08712db02`. Existing untracked files remain untouched.

- [ ] **Step 2: Create isolated branches/worktrees**

Use these branch names:

```text
style_timing_signal: fix/b3-wind-share-capital-tail
stock_selector:      fix/b3-wind-share-capital-tail
```

Let `using-git-worktrees` select an ignored safe directory. If the managed filesystem requires approval to create the adjacent upstream worktree, request it; do not fall back to editing a dirty user worktree silently.

Use these exact preferred worktree paths when they pass the skill's ignore and safety checks:

```text
/home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital
/home/elfbob/claude-code/.worktrees/stock_selector/b3-wind-share-capital
```

- [ ] **Step 3: Run clean baselines in each worktree**

```bash
cd /home/elfbob/claude-code/.worktrees/stock_selector/b3-wind-share-capital
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_wind_source_http.py tests/test_writers_share_capital.py -q

cd /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_exposures.py \
  tests/test_b3_portfolios_states.py \
  tests/test_b3_eval.py \
  tests/test_b3_impact_audit.py -q
```

Expected: all selected baseline tests pass. A real failure is diagnosed with `systematic-debugging` before implementation.

## Task 2: Record Wind health, quota, and raw HTTP response hashes

**Files:**

- Modify: `stock_selector/data/wind_source.py`
- Modify: `tests/test_wind_source_http.py`

- [ ] **Step 1: Write failing HTTP-evidence tests**

Add tests that prove:

```python
@respx.mock
def test_gateway_status_and_fetch_capture_body_hash_without_auth_header():
    health = respx.get(f"{GW}/health").mock(
        return_value=httpx.Response(200, content=b'{"status":"ok","wind_ready":true}')
    )
    fetch = respx.get(f"{GW}/fetch/financial_quarterly").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'{"status":"ok","columns":["ts_code","trade_date",'
                b'"total_shares"],"rows":[["A.SH","2023-12-29",1000]]}'
            ),
        )
    )
    client = _client()
    assert client.fetch_gateway_status("/health")["wind_ready"] is True
    client.fetch_financial_quarterly(
        ["A.SH"], ["total_shares"], date(2023, 12, 1), date(2023, 12, 31),
        options="Period=M;PriceAdj=U;unit=1",
    )
    evidence = client.drain_response_evidence()
    assert [item["path"] for item in evidence] == [
        "/health", "/fetch/financial_quarterly"
    ]
    assert all(len(item["body_sha256"]) == 64 for item in evidence)
    assert all("authorization" not in str(item).lower() for item in evidence)
    assert health.called and fetch.called


def test_gateway_status_rejects_non_status_path():
    with pytest.raises(ValueError, match="status path"):
        _client().fetch_gateway_status("/fetch/price")
```

Run:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_wind_source_http.py \
  -k 'gateway_status or response_evidence' -q
```

Expected: fail because the public methods do not exist.

- [ ] **Step 2: Implement a bounded evidence recorder**

Add `hashlib` and a per-client evidence list. The public status method must allow only `/health` and `/quota`; it must use the same bearer-authenticated `httpx.Client` and must never retain headers or the token. `_get` records the exact response body hash before JSON normalization.

The public contract is:

```python
_STATUS_PATHS = frozenset({"/health", "/quota"})


def fetch_gateway_status(self, path: str) -> dict[str, Any]:
    if path not in _STATUS_PATHS:
        raise ValueError(f"unsupported gateway status path: {path}")
    response = self._client.get(path)
    self._record_response(response)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"gateway status path {path} returned non-object JSON")
    return body


def _record_response(self, response: httpx.Response) -> None:
    raw = response.content
    self._response_evidence.append({
        "path": response.request.url.path,
        "status_code": int(response.status_code),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "content_length": len(raw),
    })


def drain_response_evidence(self) -> list[dict[str, Any]]:
    evidence = [dict(item) for item in self._response_evidence]
    self._response_evidence.clear()
    return evidence
```

Ensure `_get` calls `_record_response` exactly once for every completed HTTP response, including typed error responses. Avoid double-recording the status calls.

- [ ] **Step 3: Run the focused and full HTTP tests**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_wind_source_http.py -q
```

Expected: all tests pass with no network access.

- [ ] **Step 4: Commit the bounded client change**

```bash
git add stock_selector/data/wind_source.py tests/test_wind_source_http.py
git diff --cached --check
git commit -m "feat: record Wind gateway response evidence"
```

## Task 3: Implement immutable-target loading and Wind normalization

**Files:**

- Create: `stock_selector/backfill/b3_wind_share_capital.py`
- Create: `tests/test_b3_wind_share_capital.py`

- [ ] **Step 1: Write failing anchor-contract tests**

Cover all four exact hashes, strict boolean parsing, exact counts, uniqueness, reason code, and the excluded zero-impact ticker. The main success test copies a minimal generated fixture but passes explicit expected hashes/counts; a separate repository-fixture test points at the real style anchor directory and asserts `5_781 / 5_445 / 56`.

The data object must be:

```python
@dataclass(frozen=True)
class TargetContract:
    coordinates: pd.DataFrame
    tickers: tuple[str, ...]
    hashes: dict[str, str]


ANCHOR_HASHES = {
    "tail.csv": "93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223",
    "shares_tail_impact_by_ticker.csv": "ff8a9123504d430225c0ea0618c6a07c06373b07f2826af8bff450d6a55d7a40",
    "shares_tail_impact_detail.csv": "a2fd07c9cdf5ba6b1defb89850eaf5bea629c7e76cf372c95931c745eedead13",
    "impact_audit_manifest.json": "777818dd48a0fc4513f74391ce9c57b8bca0f5750173590ba894c93be22b1f74",
}
```

Run:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_b3_wind_share_capital.py \
  -k 'anchor or target' -q
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 2: Implement `load_target_contract`**

The function signature and postconditions are fixed:

```python
def load_target_contract(
    anchor_dir: Path,
    *,
    expected_hashes: Mapping[str, str] = ANCHOR_HASHES,
) -> TargetContract:
    """Validate immutable artifacts before any Wind or database call."""
```

It must:

1. hash all four files before parsing;
2. reject duplicate JSON keys in the manifest;
3. require the manifest's output hashes/counts to agree with the files;
4. parse only literal `True`/`False` for `required_formation`;
5. normalize `formation_date` to `datetime.date` and `ts_code` to stripped uppercase;
6. require exactly 5,781 unique keys, 5,445 required rows, 56 tickers, and only `DATA_MISSING_SHARES`; and
7. reject `688347.SH`.

- [ ] **Step 3: Write failing numeric-normalization tests**

Test lowercase and uppercase Wind field names, exact integer normalization, duplicate/missing target keys, booleans, strings, NaN/Inf, zero/negative values, fractional shares, and a relative error just below/above `1e-4`.

The normalized output columns are exactly:

```python
WIND_OBSERVATION_COLUMNS = [
    "ts_code", "formation_date", "total_shares", "mkt_cap_ard", "close",
    "implied_shares", "relative_error", "is_target_coordinate",
]
```

- [ ] **Step 4: Implement strict normalization**

Use exact coercion helpers that reject booleans rather than allowing Python's `bool`-as-`int` behavior:

```python
def strict_real(value: object, label: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ProposalContractError(f"{label} must not be boolean")
    number = float(value)
    if not math.isfinite(number):
        raise ProposalContractError(f"{label} must be finite")
    return number


def strict_positive_integral_shares(value: object) -> int:
    number = strict_real(value, "total_shares")
    rounded = round(number)
    if number <= 0 or abs(number - rounded) > 1e-6:
        raise ProposalContractError("total_shares must be positive and integral")
    return int(rounded)
```

`normalize_wind_observations(raw, target)` may retain non-target pre-listing null rows in the observation artifact, but every target key must occur once and pass all three positive-value checks plus the `1e-4` cross-field check. Non-target diagnostics use blank numeric fields instead of fabricating values.

- [ ] **Step 5: Run and commit the pure contract slice**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_b3_wind_share_capital.py \
  -k 'anchor or target or normalize or duplicate or relative_error' -q
git add stock_selector/backfill/b3_wind_share_capital.py \
  tests/test_b3_wind_share_capital.py
git diff --cached --check
git commit -m "feat: validate B3 Wind share targets"
```

Expected: all selected tests pass.

## Task 4: Implement sparse-node round-trip and action planning

**Files:**

- Modify: `stock_selector/backfill/b3_wind_share_capital.py`
- Modify: `tests/test_b3_wind_share_capital.py`

- [ ] **Step 1: Write failing compression tests**

Prove first-node retention, change-node retention, stable ticker/date ordering, cross-ticker isolation, and exact reconstruction over all target coordinates. A deliberately dropped change node must raise `ProposalContractError`.

The proposal schema is fixed:

```python
PROPOSAL_COLUMNS = [
    "ts_code", "effective_date", "total_shares", "par_value",
    "available_date", "source", "quality_flag",
]


def compress_target_observations(target_observations: pd.DataFrame) -> pd.DataFrame:
    # effective_date == available_date == formation_date
    # par_value and quality_flag are None
    # source == "wind_total_shares_month_end"
```

- [ ] **Step 2: Implement compression and `verify_round_trip`**

The comparison must use integral share counts, not floating tolerances:

```python
def verify_round_trip(
    proposal: pd.DataFrame,
    target_observations: pd.DataFrame,
) -> pd.DataFrame:
    """As-of reconstruct every target key and require exact integer equality."""
```

Return a stably sorted diagnostic frame with direct and reconstructed values; raise before publication if any key is absent or different.

- [ ] **Step 3: Write failing four-action tests**

Use `Decimal`-compatible existing rows and cover exactly:

```text
INSERT
UPGRADE_PAR_UNKNOWN
KEEP_IDENTICAL
BLOCK_CONFLICT
```

Test conflicting positive values, null rows with the wrong flag, non-integral/invalid existing values, duplicate existing evidence, and preservation of existing `source`/`updated_at` for `KEEP_IDENTICAL`.

- [ ] **Step 4: Implement `classify_proposal_actions`**

The classifier signature is:

```python
def classify_proposal_actions(
    proposal: pd.DataFrame,
    existing_rows: pd.DataFrame,
) -> pd.DataFrame:
    """Return one action per proposal key; never mutate canonical data."""
```

`BLOCK_CONFLICT` is a publication blocker, not merely a report row. `KEEP_IDENTICAL` is never updated. Include old business fields and `old_updated_at` in `proposal_actions.csv` so review does not depend on joining files mentally.

- [ ] **Step 5: Run and commit the pure planning slice**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest tests/test_b3_wind_share_capital.py \
  -k 'compress or round_trip or action or conflict' -q
git add stock_selector/backfill/b3_wind_share_capital.py \
  tests/test_b3_wind_share_capital.py
git diff --cached --check
git commit -m "feat: plan guarded B3 share nodes"
```

## Task 5: Implement the guarded database transaction and rollback

**Files:**

- Modify: `stock_selector/backfill/b3_wind_share_capital.py`
- Create: `tests/test_b3_wind_share_capital_db.py`
- Read only: `data/schema/026_stock_share_capital.sql`

- [ ] **Step 1: Write failing test-schema integration tests**

Use sentinel tickers prefixed `TSSC_B3W_` and the existing `test_conn` fixture. Cover:

1. one absent row is inserted;
2. one exact null/null/`par_unknown` row is upgraded;
3. one identical valued row retains its original `source` and exact `updated_at`;
4. one conflicting row aborts the complete transaction;
5. a forced affected-row mismatch aborts the complete transaction;
6. a second application is idempotent (`KEEP_IDENTICAL`, zero writes); and
7. rollback deletes only inserted rows, restores upgraded business fields, and refuses current-state drift.

Run with integration tests enabled:

```bash
env STOCK_SELECTOR_INTEGRATION_STRICT=1 \
  /home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -o addopts='' \
  tests/test_b3_wind_share_capital_db.py -m integration -q
```

Expected: fail before the writer exists. A skipped test is not a pass.

- [ ] **Step 2: Implement exact-key reads and transactional reclassification**

Use a temporary proposal table populated with `psycopg2.extras.execute_values`, then acquire a campaign-scoped transaction lock:

```sql
SELECT pg_advisory_xact_lock(hashtextextended(
  'b3-wind-share-capital-2026-08-01', 0
));
```

Select every present proposal key `FOR UPDATE`, reconstruct the absent keys, and call the same pure classifier used by the dry run. Require the inside-transaction actions and backup fields to match the manifest exactly before any DML.

- [ ] **Step 3: Implement guarded insert/update SQL**

The only legal write statements are structurally equivalent to:

```sql
INSERT INTO stock_share_capital
  (ts_code, effective_date, total_shares, par_value,
   available_date, source, quality_flag)
SELECT ts_code, effective_date, total_shares, NULL,
       available_date, 'wind_total_shares_month_end', NULL
FROM b3_wind_share_proposal
WHERE planned_action = 'INSERT'
ON CONFLICT (ts_code, effective_date) DO NOTHING;

UPDATE stock_share_capital AS current
SET total_shares = proposal.total_shares,
    par_value = NULL,
    available_date = proposal.available_date,
    source = 'wind_total_shares_month_end',
    quality_flag = NULL
FROM b3_wind_share_proposal AS proposal
WHERE proposal.planned_action = 'UPGRADE_PAR_UNKNOWN'
  AND current.ts_code = proposal.ts_code
  AND current.effective_date = proposal.effective_date
  AND current.total_shares IS NULL
  AND current.par_value IS NULL
  AND current.quality_flag = 'par_unknown';
```

Require `inserted + upgraded == planned INSERT + planned UPGRADE_PAR_UNKNOWN`. Do not call `write_share_capital`, do not commit in the helper, and never update `KEEP_IDENTICAL` rows.

- [ ] **Step 4: Implement guarded rollback**

Rollback takes the proposal manifest, `rollback_insert_keys.csv`, and `existing_rows_backup.csv`; it revalidates current rows before DML. Delete an inserted key only when all proposal business fields and source still match. Restore upgraded rows' five business fields from backup. The normal `updated_at` trigger deliberately advances the timestamp so the rollback propagates through the existing sync path; retain the original timestamp in the backup and rollback receipt as audit evidence rather than hiding the rollback behind an old timestamp.

- [ ] **Step 5: Run integration and writer regressions**

```bash
env STOCK_SELECTOR_INTEGRATION_STRICT=1 \
  /home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -o addopts='' \
  tests/test_b3_wind_share_capital_db.py \
  tests/test_writers_share_capital.py \
  tests/test_pg_reader_derived_mv.py \
  -m integration -q
```

Expected: all selected tests pass, no skips.

- [ ] **Step 6: Commit the guarded writer**

```bash
git add stock_selector/backfill/b3_wind_share_capital.py \
  tests/test_b3_wind_share_capital_db.py
git diff --cached --check
git commit -m "feat: guard B3 share-capital writes"
```

## Task 6: Implement deterministic proposal and operational CLI

**Files:**

- Modify: `stock_selector/backfill/b3_wind_share_capital.py`
- Create: `scripts/backfill_b3_wind_share_capital.py`
- Modify: `tests/test_b3_wind_share_capital.py`
- Create: `tests/test_backfill_b3_wind_share_capital_cli.py`

- [ ] **Step 1: Write failing artifact-publication tests**

Use `tmp_path` and assert fixed columns, stable sort, LF line endings, deterministic hashes for identical frames, manifest-last behavior, rejection of an existing manifest, no secret values, and rejection of a partial or hash-mismatched package.

The proposal files are exactly:

```python
PROPOSAL_FILES = (
    "wind_monthly_observations.csv",
    "proposal_nodes.csv",
    "proposal_actions.csv",
    "existing_rows_backup.csv",
    "rollback_insert_keys.csv",
    "README.md",
    "proposal_manifest.json",
)
```

The manifest is written last with `os.replace`. A missing manifest means “not applicable”; rerunning over an existing valid manifest is refused instead of silently replacing reviewed evidence.

- [ ] **Step 2: Implement manifest validation and publication**

`proposal_manifest.json` must contain:

- schema/version and generated UTC time;
- all four anchor paths, hashes, row counts, and immutable invariants;
- sanitized Wind request parameters and before/after `/health` and `/quota` payloads;
- raw HTTP response evidence hashes;
- observation/target/proposal/action/backup/rollback counts;
- maximum cross-field relative error and round-trip mismatch count;
- artifact hashes and fixed column lists;
- `stock_selector_code_commit` and `style_input_commit`;
- source table `stock_selector.stock_share_capital`; and
- a declaration that no `BLOCK_CONFLICT` exists.

The manifest must never contain the settings path's contents, bearer token, password, request headers, or environment dump.

- [ ] **Step 3: Write failing CLI tests**

Patch the Wind source, database connector, and git probes. Prove that:

- `propose` validates anchors before constructing Wind or PostgreSQL clients;
- `propose` performs `/health`, `/quota`, the exact WSD request, then `/quota`;
- a failed health/quota/normalization/conflict gate publishes no manifest;
- `verify-proposal` is read-only;
- `apply --target production` requires `--confirm-production`;
- `rollback --target production` requires `--confirm-rollback`; and
- receipts are written only after a committed transaction and readback.

- [ ] **Step 4: Implement the CLI with local heavy imports**

The parser contract is:

```text
propose
  --anchor-dir PATH --campaign-dir PATH --settings PATH
  --style-repo PATH
verify-proposal
  --manifest PATH
apply
  --manifest PATH --settings PATH --target {test,production}
  [--confirm-production]
verify-applied
  --manifest PATH --settings PATH --target {test,production} --output PATH
rollback
  --manifest PATH --settings PATH --target {test,production}
  [--confirm-rollback]
```

`propose` uses `WindDataSource.fetch_financial_quarterly` with the exact frozen fields/dates/options. `apply` verifies both code commits, all artifact hashes, the complete-output declaration, backup equality, zero conflicts, and round-trip counts before entering one transaction. `apply_receipt.json`, `post_write_canonical_verification.json`, and `rollback_receipt.json` use atomic writes and bind the proposal-manifest hash.

- [ ] **Step 5: Run all upstream feature tests**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest \
  tests/test_wind_source_http.py \
  tests/test_b3_wind_share_capital.py \
  tests/test_backfill_b3_wind_share_capital_cli.py -q

env STOCK_SELECTOR_INTEGRATION_STRICT=1 \
  /home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -o addopts='' \
  tests/test_b3_wind_share_capital_db.py -m integration -q
```

Expected: all tests pass and the integration run has no skip.

- [ ] **Step 6: Commit the operational CLI**

```bash
git add stock_selector/backfill/b3_wind_share_capital.py \
  scripts/backfill_b3_wind_share_capital.py \
  tests/test_b3_wind_share_capital.py \
  tests/test_backfill_b3_wind_share_capital_cli.py
git diff --cached --check
git commit -m "feat: publish B3 Wind share proposals"
```

Record this resulting commit as `stock_selector_code_commit`; the proposal must be generated only from this committed tree.

## Task 7: Add the downstream post-write verifier and guarded B3 runner

**Files:**

- Create: `data_fixes/2026-08-01-b3-wind-share-capital/verify_post_write.py`
- Create: `data_fixes/2026-08-01-b3-wind-share-capital/run_guarded_b3.py`
- Create: `tests/test_b3_wind_share_capital_postwrite.py`
- Do not modify: `signals/style_basket/b3_build.py`
- Do not modify: `backtest/b3_eval.py`

- [ ] **Step 1: Write failing verifier tests**

Create synthetic coverage/preflight/run manifests and test:

- `DATA_MISSING_SHARES` is zero for all and required rows under both PIT policies;
- `DATA_MISSING_CLOSE` is zero under both policies;
- each policy has exactly 120 distinct required `monthly_exposure` formations and none is data-blocked;
- preflight status is `OK` and its declared files match hashes;
- `candidate_statistical_verdicts` is a nonempty object;
- `family_statistical_verdict` is non-null/nonempty;
- `final_verdict` is preserved exactly, including a legal `DATA_BLOCKED` result;
- proposal/apply/canonical-verification hashes bind to one chain; and
- a malformed, duplicated-key, partial, stale, or hash-mismatched file fails closed.

The summary object must contain:

```python
{
    "data_missing_shares": {"all": 0, "required": 0},
    "data_missing_close": {"all": 0, "required": 0},
    "required_formations_by_policy": {
        "legal_deadline": 120,
        "legal_deadline_plus_one_month_end": 120,
    },
    "family_statistical_verdict": "copied verbatim from B3",
    "final_verdict": "copied verbatim from B3",
}
```

The strings shown above describe copied values; the implementation must not hard-code or preselect either verdict.

- [ ] **Step 2: Write failing guarded-runner tests**

Mock `subprocess.run` and prove three exact stages run in order:

1. `b3_build --stage preflight --data-end 2023-12-31`;
2. `b3_build --stage all --data-end 2023-12-31`; and
3. `b3_eval --data-end 2023-12-31`.

Every stage is wrapped by `systemd-run --user --scope -p MemoryMax=8G` and `/usr/bin/time -v`. Stop immediately on nonzero preflight/build exit. Evaluation exit `2` is retained as evidence rather than erased, because a non-share run-level gate may legally leave the final verdict `DATA_BLOCKED` after statistics exist.

- [ ] **Step 3: Implement atomic run receipts and final verification**

`run_guarded_b3.py` writes per-stage stdout, stderr, GNU-time files, command arrays, exit codes, wall time, peak RSS, and file hashes. It publishes `b3_execution_receipt.json` last. It uses campaign-specific paths and never overwrites the user's existing output directories.

`verify_post_write.py` reads the receipt and standard B3 manifests, validates every declared hash, emits `final_verification.json` atomically, and returns nonzero on any failed acceptance condition.

- [ ] **Step 4: Run the new verifier tests and B3 regressions**

```bash
/home/elfbob/miniconda3/bin/python -m pytest \
  tests/test_b3_wind_share_capital_postwrite.py \
  tests/test_b3_exposures.py \
  tests/test_b3_portfolios_states.py \
  tests/test_b3_eval.py \
  tests/test_b3_impact_audit.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit downstream evidence tooling**

```bash
git add \
  data_fixes/2026-08-01-b3-wind-share-capital/verify_post_write.py \
  data_fixes/2026-08-01-b3-wind-share-capital/run_guarded_b3.py \
  tests/test_b3_wind_share_capital_postwrite.py
git diff --cached --check
git commit -m "test(b3): verify Wind share-tail closure"
```

Record this commit as `style_input_commit`. It must be the style commit embedded in the proposal manifest.

## Task 8: Generate and review the full 56-ticker proposal (read-only)

**Files:**

- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/wind_monthly_observations.csv`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/proposal_nodes.csv`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/proposal_actions.csv`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/existing_rows_backup.csv`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/rollback_insert_keys.csv`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json`
- Generate/modify: `data_fixes/2026-08-01-b3-wind-share-capital/README.md`

- [ ] **Step 1: Verify committed trees and current quota**

Require clean tracked worktrees. Confirm that the recorded implementation commits are the current commits. The CLI's `/health` and `/quota` calls are read-only and their body hashes go into the proposal.

- [ ] **Step 2: Run the proposal command**

From the committed `stock_selector` implementation worktree, run:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital propose \
  --anchor-dir /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-07-25-share-capital-par \
  --campaign-dir /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital \
  --settings /home/elfbob/claude-code/stock_selector/config/settings.yaml \
  --style-repo /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital
```

The CLI resolves and records the style worktree's current committed HEAD as `style_input_commit`; it refuses tracked changes. Write the resolved command array into `README.md` and the sanitized manifest. This is a read-only Wind/PostgreSQL operation and performs no canonical write.

Expected gates:

```text
target_coordinates=5781
required_coordinates=5445
target_tickers=56
missing_target_wind_rows=0
duplicate_target_wind_rows=0
cross_field_failures=0
round_trip_mismatches=0
block_conflicts=0
```

- [ ] **Step 3: Independently verify the package**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital verify-proposal \
  --manifest /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json
```

Expected: exit `0`; every declared file, row count, column list, and hash matches.

- [ ] **Step 4: Review action counts and changed-share diagnostics**

Inspect the manifest and `proposal_actions.csv`. Confirm there is no `BLOCK_CONFLICT`; all writes are only `INSERT` or `UPGRADE_PAR_UNKNOWN`; `KEEP_IDENTICAL` is unchanged. Review every reported large share-count jump as evidence, not as an automatic rejection.

- [ ] **Step 5: Commit the read-only proposal artifacts in style**

```bash
git add data_fixes/2026-08-01-b3-wind-share-capital
git diff --cached --check
git commit -m "data(b3): propose Wind share-capital tail repair"
```

Do not include B3 runtime outputs or any pre-existing untracked file. The commit message/body cross-references `stock_selector_code_commit`; the proposal manifest already records the pre-proposal `style_input_commit`, avoiding a self-referential commit hash.

## Task 9: Run pre-production regressions and stop at the production gate

**Files:** no production data changes

- [ ] **Step 1: Run the complete upstream suite**

```bash
cd /home/elfbob/claude-code/.worktrees/stock_selector/b3-wind-share-capital
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -q
env STOCK_SELECTOR_INTEGRATION_STRICT=1 \
  /home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -o addopts='' \
  tests/test_b3_wind_share_capital_db.py \
  tests/test_writers_share_capital.py \
  tests/test_pg_reader_derived_mv.py \
  -m integration -q
```

Expected: unit suite passes; selected integration tests pass with no skips.

- [ ] **Step 2: Run the complete downstream suite**

```bash
cd /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 3: Present the production packet and STOP**

Report:

- both code commits and proposal artifact commit;
- proposal manifest SHA-256;
- Wind before/after quota evidence;
- observation, proposal, action, backup, and rollback-key counts;
- maximum relative error;
- exact regression commands and results; and
- exact planned insert/upgrade/keep counts.

Ask for explicit authorization to apply this exact manifest to production. Do not run Task 10 in the same turn unless the user's response explicitly authorizes the production transaction.

## Task 10: Apply the exact proposal to production after explicit approval

**Files:**

- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/apply_receipt.json`
- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/post_write_canonical_verification.json`
- Mutate only: production `stock_selector.stock_share_capital` at declared proposal keys

- [ ] **Step 1: Reverify the approved packet immediately before apply**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital verify-proposal \
  --manifest /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json
```

Expected: exit `0`. Any hash or database-state drift returns to Task 8 and requires a new proposal plus new approval.

- [ ] **Step 2: Apply one guarded production transaction**

Only after explicit user authorization:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital apply \
  --manifest /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json \
  --settings /home/elfbob/claude-code/stock_selector/config/settings.yaml \
  --target production \
  --confirm-production
```

Expected: committed affected rows equal declared `INSERT + UPGRADE_PAR_UNKNOWN`; `KEEP_IDENTICAL` rows are untouched; `apply_receipt.json` is written after commit and binds the manifest hash.

- [ ] **Step 3: Perform exact readback and all-coordinate as-of verification**

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital verify-applied \
  --manifest /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json \
  --settings /home/elfbob/claude-code/stock_selector/config/settings.yaml \
  --target production \
  --output /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/post_write_canonical_verification.json
```

Expected:

```text
proposal_key_mismatches=0
target_coordinate_mismatches=0
target_coordinates_verified=5781
```

If commit status is uncertain, do not retry apply. Run `verify-applied`, reread canonical state, and reclassify.

- [ ] **Step 4: Preserve rollback as a separate future gate**

Never run this command automatically. If rollback is later explicitly approved, the only legal entry is:

```bash
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m scripts.backfill_b3_wind_share_capital rollback \
  --manifest /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json \
  --settings /home/elfbob/claude-code/stock_selector/config/settings.yaml \
  --target production \
  --confirm-rollback
```

Current-state drift aborts rollback.

## Task 11: Rerun unchanged B3 under the 8 GiB guard

**Files:**

- Generate below campaign: `b3_research_output/`
- Generate below campaign: `b3_backtest_output/`
- Generate below campaign: `b3_execution_receipt.json`

- [ ] **Step 1: Run the guarded B3 orchestrator**

From the style worktree:

```bash
/home/elfbob/miniconda3/bin/python \
  data_fixes/2026-08-01-b3-wind-share-capital/run_guarded_b3.py \
  --python /home/elfbob/miniconda3/bin/python \
  --data-end 2023-12-31 \
  --research-output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output \
  --backtest-output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_backtest_output \
  --receipt data_fixes/2026-08-01-b3-wind-share-capital/b3_execution_receipt.json
```

The orchestrator executes these exact payload commands beneath `systemd-run --user --scope -p MemoryMax=8G`:

```text
python -m signals.style_basket.b3_build --stage preflight --data-end 2023-12-31 --output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output
python -m signals.style_basket.b3_build --stage all --data-end 2023-12-31 --output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output
python -m backtest.b3_eval --data-end 2023-12-31 --research-output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output --backtest-output-dir data_fixes/2026-08-01-b3-wind-share-capital/b3_backtest_output
```

Expected: preflight and build exit `0`; evaluation writes complete candidate statistics. Evaluation exit `0` or `2` is interpreted only through the produced run manifest; OOM, exit `3`, missing output, or an exception fails closed.

- [ ] **Step 2: Inspect memory and stage evidence**

Require every recorded peak RSS to be below 8 GiB, preflight `status == "OK"`, all declared stage hashes to verify, and `database_source_evidence` to include `stock_selector.stock_share_capital`.

## Task 12: Produce the machine-readable final verdict and closure evidence

**Files:**

- Generate: `data_fixes/2026-08-01-b3-wind-share-capital/final_verification.json`
- Modify: `data_fixes/2026-08-01-b3-wind-share-capital/README.md`

- [ ] **Step 1: Run the post-write verifier**

```bash
/home/elfbob/miniconda3/bin/python \
  data_fixes/2026-08-01-b3-wind-share-capital/verify_post_write.py \
  --proposal-manifest data_fixes/2026-08-01-b3-wind-share-capital/proposal_manifest.json \
  --apply-receipt data_fixes/2026-08-01-b3-wind-share-capital/apply_receipt.json \
  --canonical-verification data_fixes/2026-08-01-b3-wind-share-capital/post_write_canonical_verification.json \
  --execution-receipt data_fixes/2026-08-01-b3-wind-share-capital/b3_execution_receipt.json \
  --coverage-audit data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output/coverage_audit.csv \
  --preflight-manifest data_fixes/2026-08-01-b3-wind-share-capital/b3_research_output/manifests/preflight.json \
  --run-manifest data_fixes/2026-08-01-b3-wind-share-capital/b3_backtest_output/run_manifest.json \
  --output data_fixes/2026-08-01-b3-wind-share-capital/final_verification.json
```

Expected:

```text
DATA_MISSING_SHARES: 0 all / 0 required
DATA_MISSING_CLOSE: 0 all / 0 required
required formations: 120 per PIT policy
candidate statistical verdicts: nonempty
family statistical verdict: nonempty
final verdict: copied from B3 run_manifest.json
verification: OK
```

The final verdict may be `DATA_BLOCKED` only if the unchanged run manifest says so after statistics exist. Preserve both `family_statistical_verdict` and `final_verdict`; never rewrite the result to `PASS_SHADOW`.

- [ ] **Step 2: Run final verification suites**

Invoke `verification-before-completion`, then run:

```bash
cd /home/elfbob/claude-code/.worktrees/stock_selector/b3-wind-share-capital
/home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -q
env STOCK_SELECTOR_INTEGRATION_STRICT=1 \
  /home/elfbob/claude-code/stock_selector/.venv/bin/python -m pytest -o addopts='' \
  tests/test_b3_wind_share_capital_db.py -m integration -q

cd /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital
/home/elfbob/miniconda3/bin/python -m pytest -q
git diff --check
```

Expected: every suite passes and the integration slice has no skips.

- [ ] **Step 3: Update README and commit final evidence**

Document exact commits, commands, exit codes, peak RSS, quota before/after, proposal/apply/readback counts, all artifact hashes, the two zero-blocker counts, both B3 verdicts, and any remaining run-level blocker.

```bash
git add data_fixes/2026-08-01-b3-wind-share-capital
git diff --cached --check
git commit -m "data(b3): record Wind tail repair verdict"
```

Do not stage the user's pre-existing untracked outputs.

- [ ] **Step 4: Completion audit**

Confirm:

```bash
git status --short --branch
git log --oneline --decorate -8
git diff HEAD^ --check
```

Report the exact machine verdicts and clickable paths to `proposal_manifest.json`, `apply_receipt.json`, `post_write_canonical_verification.json`, `b3_execution_receipt.json`, and `final_verification.json`. Mark the goal complete only after all acceptance gates are satisfied and no required work remains.
