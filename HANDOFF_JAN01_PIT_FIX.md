# Jan-01 PIT correction handoff

Date: 2026-09-01 (Asia/Shanghai)

## Repository state at handoff

- Worktree: `/tmp/b3-jan1-pit-fix`
- Branch: `fix/b3-jan1-pit-correction`
- HEAD: `3dbec3288217d78ea7ff1f71ec51787bb5d04ae6`
- The worktree was clean before this handoff document was attempted.
- Fresh focused baseline before the edit-channel failure:
  `/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q`
  returned `418 passed in 19.92s`.
- An earlier checkpoint reported `353 tests passed`; the later 418-test run is the
  freshest baseline and includes the current focused test selection.
- No production fix or RED regression test has been written or committed.

## Problem statement

The WSL exposure build covered `629,466 / 629,556` rows, leaving 90 missing
rows. Every missing dependency key had the form
`YYYY-01-01|balance|csmar`.

CSMAR balance-sheet rows whose period end is January 1 and is not a standard
quarter end are being treated as independent disclosure events. They are not
valid standalone balance-sheet events for PIT factor assembly. Allowing them
into the derived event rows can move book equity forward before the genuine
annual disclosure is available.

The audit found that 70 of the 90 missing rows represented genuine look-ahead
exposure, and at least 16 BP inputs would change under the correction. The user
explicitly chose correctness over preserving historical BP/style-score output
and accepted those historical changes.

## Required correction

Before generating derived event rows for snapshot assembly, exclude only facts
that satisfy all of the following:

1. `data_source == "csmar"`
2. `statement_type == "balance"`
3. `end_date` is January 1
4. the date is not a standard quarter end

The filter must be scoped to the balance event stream. Do not filter CSMAR
income or direct-cashflow facts on January 1, because their TTM/event behavior
is separate and must remain unchanged.

Do not fabricate or rewrite disclosure provenance to make the row pass. The
invalid balance event itself must be excluded before derived event rows are
built.

## RED regression tests to add

Target file: `tests/test_b3_exposures.py`.

Add a minimal fixture/helper that supplies:

- a valid CSMAR balance fact for `2020-12-31`, disclosed `2021-04-30`, with
  `equity_parent = 500.0`; and
- a CSMAR balance fact for `2021-01-01`, stored `2021-07-29`, with sentinel or
  missing first-disclosure provenance.

Add the following test contracts:

### `test_snapshot_bp_excludes_csmar_jan_01_balance_event`

- Parameterize both `POLICY_MAIN` and `POLICY_LAG`.
- Parameterize the January 1 equity value as both 500.0 and 900.0, proving the
  result does not depend on duplicate values.
- Patch style scoring so `style_score` exposes `bp` directly.
- Assert ticker A keeps BP/style score `0.5`, using the valid annual equity.
- Assert `true_first_disclosure_verified` remains true.
- Assert `_unverified_dependency_keys == ()`.

### `test_snapshot_jan_01_filter_is_balance_event_scoped`

- Add January 1 CSMAR `income` and `cashflow_direct` facts alongside the invalid
  balance fact.
- Record the facts passed into derived-row generation.
- Assert the January 1 statements still seen downstream are exactly
  `{"income", "cashflow_direct"}`.

Run these tests before the production change and confirm they fail for the
expected reason. Then implement the narrow filter and rerun them to GREEN.

## Verification gates

After the targeted RED-to-GREEN cycle:

1. Run the focused suite:
   `/home/elfbob/miniconda3/bin/python -m pytest tests/test_factors.py tests/test_style_basket.py tests/test_b3_exposures.py -q`
2. Run the repository's full relevant test suite.
3. Rebuild WSL r3 exposures and independently audit dependency coverage.
4. Require exact coverage of `629,556 / 629,556` before continuing.
5. Only after 100% exposure coverage may states, portfolio, or evaluation be
   run.
6. Review the diff for scope and commit only the regression tests, narrow
   production filter, and appropriate documentation.

## Codex execution failure recorded for handoff

The implementation was not stopped voluntarily. All attempted Codex edit and
shell channels began failing before file access with:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

The failure occurred for the primary agent, fresh agents, normal `apply_patch`,
an escalated outer shell invoking `apply_patch`, and a resumed Codex child
process. Launching `codex resume` from inside the existing Codex session merely
created a nested process that inherited the broken sandbox; it did not restart
the host Codex process.

No system packages were installed. Full filesystem access was suggested as a
workaround and explicitly rejected by the user; it is neither required nor
authorized. Do not request Full access and do not change WSL, system packages,
network configuration, or database state to work around this issue.

For a Codex-only retry, the host terminal process—not a nested child—would have
to be restarted. The user is handing implementation to Claude Code instead.

## Preserved investigation source

The prior Codex session ID is
`01a05594-771b-7052-9ae0-1dc889ce2d18`. Its local rollout transcript was found
at:

`/home/elfbob/.codex/sessions/2026/08/31/rollout-2026-08-31T10-09-48-01a05594-771b-7052-9ae0-1dc889ce2d18.jsonl`

That transcript contains the recovered proposed RED patch and detailed audit
context. Treat the contracts above as the authoritative implementation scope.
