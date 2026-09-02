# B3 state/model calendar + equal-weight provenance handoff

**Updated:** 2026-08-11 Asia/Shanghai

**Local integration clone:** `/tmp/b3-equal-weight-merge.8ZS8FO/repo`

**Local branch:** `merge-equal-weight`

**Formal Windows repo:** `D:/style_timing_signal`

**Formal branch:** `fix/b3-wind-share-capital-tail`

## Objective

Deliver the approved **“乙 + downstream verification”** path:

1. Accept the production equal-weight CSV with extra columns but bind the exact
   `date` and `factor_value` columns.
2. Record the equal-weight absolute path, raw-file SHA-256, source kind, date
   column, and value column in the structure manifest.
3. Make eval reload the same file and fail closed on any path/SHA/column mismatch.
4. Treat complete zero-variance state windows as neutral `z=0`, while keeping
   warm-up windows and real data gaps as `NaN`.
5. Keep structural evidence from 2014-10, but freeze model scoring from 2015-01.
6. Complete the formal states → structure → eval run for `data_end=2023-12-31`.

## Frozen semantics

- Structural calendar starts in 2014-10.
- Model calendar starts at the first January 2015 formation (2015-01-30 in the
  formal data); it is never inferred from the first finite feature.
- The formal formation proof ends at 2023-12. With forward returns defined on
  `(formation, next formation]`, the last realized holding is 2023-11 → 2023-12.
- Formal hard-sort evidence therefore has `n=110`; confirmation model evidence
  has `n=35`.
- A midmonth cutoff rejects a formation inside the incomplete cutoff month, but
  daily report-only states/targets/control remain available through `data_end`.
- Equal-weight may omit benchmark history before the model calendar. It must not
  contain off-benchmark dates, and it must exactly cover every model-calendar day.
- Missing `salg_source_end_date` on a latest model row means that observation did
  not consume SalG. Nonmissing dependencies remain strict quarter ends; at least
  one latest-formation SalG dependency is required.

## Commit chain

Base already on the formal branch before this work:

- `1426a073` `fix(b3): bind equal-weight control provenance`

Implemented and reviewed:

- `918fe34` `docs(b3): freeze state feature calendar semantics`
- `0cc8095` `docs(b3): plan state feature calendar fix`
- `f04703c` `fix(b3): keep zero-variance state windows neutral`
- `cebe8a5` `refactor(b3): centralize frozen model windows`
- `71938b1` `fix(b3): split structural and model calendars`
- `4210003` `fix(b3): score eval on the frozen model calendar`
- `e37ee82` `fix(b3): align frozen evidence with cutoff`
- `a0f4841` `fix(b3): validate eval cutoff formations`
- `bf04f4c` `docs(b3): align eval plan with cutoff semantics`
- `14efbfe` `fix(b3): scope control grid to model calendar`

Latest local code commit at the time this record was updated:

- `9a18090` `fix(b3): scope SalG freshness to dependencies`

`9a18090` has fresh GREEN verification and independent spec/quality approval.
The handoff document itself is the following docs-only commit.

## Verification already completed

At `bf04f4c`:

- Directly affected modules: `473 passed, 21 warnings`.
- Full repository: `1134 passed, 25 warnings`.
- Final independent review: APPROVE, no Critical/Important findings.

At `14efbfe`:

- Eval module: `335 passed`.
- Three affected modules: `476 passed, 21 warnings`.
- Equal-weight provenance subset: `8 passed`.
- Independent spec review: PASS.
- Independent quality review: Ready Yes.
- Remote Windows/WSL eval module: `335 passed in 80.23s`.

At `9a18090`:

- Focused SalG tests: `17 passed`.
- Full eval module: `343 passed`.
- Three affected modules: `484 passed, 21 warnings`.
- Independent spec review: PASS.
- Independent quality review: Ready Yes, no Critical/Important/Minor findings.

At the deployed handoff head:

- Full repository: `1145 passed, 25 warnings`.
- Remote Windows/WSL eval module: `343 passed in 81.14s`.
- Formal states → structure → eval completed with audit artifacts.

The warnings are existing pandas FutureWarnings in `b3_build.py` and
`b3_portfolios.py`.

## Formal environment and transport

Use the Windows OpenSSH endpoint and enter WSL explicitly:

```text
ssh -p 2222 ghls@100.120.152.1
wsl -d ubuntu2404 -e ...
```

Runtime Python:

```text
/home/ghls/style_timing_signal/.venv/bin/python
```

Windows checkout mounted in WSL:

```text
/mnt/d/style_timing_signal
```

Do **not** use Linux Git on the NTFS checkout; CRLF makes the entire repository
appear modified. Use Windows Git through WSL:

```text
wsl -d ubuntu2404 -e "/mnt/c/Program Files/Git/cmd/git.exe" \
  -C D:/style_timing_signal ...
```

Formal run roots:

```text
research=/mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/research
backtest=/mnt/d/style_timing_signal/data_fixes/2026-08-01-b3-wind-share-capital/run/backtest
data_end=2023-12-31
```

The only expected Windows Git status entry is:

```text
?? data_fixes/2026-08-01-b3-wind-share-capital/run/
```

## Deployment state

All implementation commits through `9a18090` and this handoff document have been
fast-forwarded to the formal branch. The final docs-only status update follows the
audited `5e7a9025a97674034584923772b8d3f0479bb32c` run. The run manifest correctly
records `5e7a902` as the exact code/working-tree snapshot that generated the audit
artifacts. A later docs-only handoff commit does not change inputs or executable
code and does not require another campaign-stage run; formal `HEAD` may therefore
be one docs-only descendant of `run_manifest.code_commit`.

The old states outputs were copied to:

```text
/mnt/d/deploy_stage/wsl2/b3-state-before-model-calendar-20260811/
```

Backup hashes:

```text
state_components.csv  a84e93c6b74f814e48e6f5714cfbfb4001d31781bf48406434969d2a265e3fc5
states.json           ff8e8ea07e593a7224e55f55d048adb9151bfe3ad1bf68d78905a34beb066399
```

The states stage was rebuilt directly with `run_states_stage`; exposures and
portfolios were not rerun.

Current states evidence:

```text
state_components.csv SHA  77dc3a2f79ed01248ac87e444adbde897e06fc89c3db2d72a8c24aa0107ec1f4
states manifest SHA       e912b596cfe78791ef453c65ce79c873d2491d831bf5673db2a95d49513073a2
rows                      13,392
date range                2014-11-03..2023-12-29
policy/q groups           6, identical 2,232-day grids
duplicate keys            0
first complete features   2015-01-30 for all groups
pre-2015 F_U/F_D/F_X/F_T  all missing
2015-01-30+ nonfinite     0 for all four features
manifest status           OK
```

The formal structure stage completed with exit 0 and status `OK`.

```text
structure_manifest.json SHA   0621d96c4be830a48756627fcc9b65088b7bf1636a434d098cd4b406239917de
structure_coefficients.csv    4bae44445e144dc409fc54a1bb0683f02f92e8b1cd3514abc7d9b8bea160083a
model_comparison.csv          aee6ea2977904e16a6cdaa31dd6be90ea07202c703686fecb189a914fba30b9f
hard_sort_surface.csv         d6b923e3ebd6023b5f84fc45d2d421f53c2e4a20658f30abbc70d9878180acc9
hard_sort                     2 rows, n=110, both pass
2015-2020 M1 discovery        6 rows
```

Equal-weight binding in the structure manifest:

```text
path          /mnt/d/style_timing_signal/output/equal_weight/equal_weight_signal_20d40z.csv
SHA-256       5d3fb8c90c836f40b86918f649b3c8844ec905b8f22bc0c4f5efdc26e25e4f64
source_kind   file
date_column   date
value_column  factor_value
```

The production file has eight columns. The loader intentionally selects the two
named columns; the raw eight-column file SHA is what the manifest binds.

## Formal eval attempts and diagnosis

First attempt (before `14efbfe`) stopped pre-audit with:

```text
equal_weight signal must exactly match the benchmark calendar
```

Diagnosis:

```text
control  2014-01-02..2023-12-29, n=2434
targets  2013-03-07..2023-12-29, n=2633
difference: 199 benchmark-only days, all before 2014; control extra dates=0
```

`14efbfe` fixes this by requiring exact coverage only on the consumed model
calendar, while still rejecting off-benchmark dates and model-period gaps.

Second attempt (after `14efbfe`) passed equal-weight verification, then stopped
pre-audit with:

```text
salg_source_end_date must be a normalized date
```

Diagnosis on the latest formation (`2023-12-29`):

```text
latest model rows       7,412
2023-09-30 source end   7,408
2019-12-31 source end   2
missing source end      2 (same ticker 688266.SH under both PIT policies)
```

Ticker `688266.SH` has 84 historical exposure rows and never has a SalG source
date. It was `size_only/MISSING_STYLE_SCORE` through 2023-10 and became model
eligible in 2023-11/12 from other available factors. Therefore its missing SalG
date denotes no SalG dependency, not malformed provenance.

Commit `9a18090` changes `salg_valid_through` to skip missing latest-row
non-dependencies, reject an all-missing latest formation, and keep strict parsing
for every nonmissing dependency. The formal data produced `2020-04-30`, then
correctly emitted a `SALG_FRESHNESS` final blocker rather than a pre-audit failure.

## Completed formal eval result

The third eval attempt completed the full audit and wrote all five frozen outputs.
The WSL process returned the CLI's audited-block status through Windows OpenSSH as
SSH exit 1; the artifacts distinguish this from the earlier pre-audit failures.

```text
family_statistical_verdict  STOP
final_verdict               DATA_BLOCKED
salg_valid_through          2020-04-30
shadow_start_allowed        false
run blockers                SALG_FRESHNESS, TRUE_DISCLOSURE_COVERAGE
true disclosure coverage    0 / 626,732 (0.0)
```

The final row also records `PIT_POLICY_FLIP`; candidate statistical verdicts are
all `STOP`. No carry freshness blocker was emitted because both raw carry series
extend beyond the requested historical cash end.

Every manifest/input hash was recomputed and matched:

```text
preflight manifest          f3e8bfecf268f2bc7ae1008d00dc31d2146959c529054b795fdd4cf4be7aeb82
exposures manifest          efd078f1b6d1ec37ebd21a9810e7f11256d48cf51dd84946c6c4f03eef15bc43
states manifest             e912b596cfe78791ef453c65ce79c873d2491d831bf5673db2a95d49513073a2
structure manifest          0621d96c4be830a48756627fcc9b65088b7bf1636a434d098cd4b406239917de
monthly_exposures.csv.gz    8c3365508abef5cff12230da12ce4b8e1377f4df06c9a18f77b3af76d0c928db
state_components.csv        77dc3a2f79ed01248ac87e444adbde897e06fc89c3db2d72a8c24aa0107ec1f4
model_comparison.csv        aee6ea2977904e16a6cdaa31dd6be90ea07202c703686fecb189a914fba30b9f
structure_coefficients.csv  4bae44445e144dc409fc54a1bb0683f02f92e8b1cd3514abc7d9b8bea160083a
equal-weight source         5d3fb8c90c836f40b86918f649b3c8844ec905b8f22bc0c4f5efdc26e25e4f64
```

Audit output hashes from the completed run at `5e7a902`:

```text
verdicts.csv                1413c5ea3aeff7a0ea649e4a5b142462bc0518fab60b03a2631da257489729b9
production_metrics.csv      709666560e13faea5e5e2677297b97f74dee66f1a9c65ec3b626917f0498410e
yearly_contribution.csv     822a981a3678a986c64c34ab5bfc7e10584b19a0e47c01809a38151e484b006a
bootstrap.csv               92bb98b5e726c570c531863853fc6a0253f6699e00116cd3c19a7dd32db9b051
run_manifest.json           382ec014e2e033ec147452df6b0c832f0e6c72d740191a5b98746df176dcdd56
```

## Remaining work after this handoff

No implementation or campaign-stage rerun remains for this task. The next
substantive work is upstream data remediation:

1. Extend/rebuild the stale SalG dependencies beyond `2020-04-30`.
2. Backfill explicit true-first-disclosure provenance; current verified coverage
   is 0/626,732.
3. Only after those inputs and their manifests are refreshed should the affected
   B3 stages be rerun. Shadow start is not allowed under the current evidence.

For a read-only audit, verify the formal Windows Git `HEAD`, the expected sole
untracked run directory, and the hashes above. Do not repeat the current run unless
an upstream input or code commit changes.

## Safety notes

- Do not rerun `b3_build --stage states`; it cumulatively reruns earlier stages.
  Use direct `run_states_stage` only if states must be rebuilt again.
- Do not regenerate or refresh the equal-weight file between structure and eval.
- Preserve the formal run directory and the states backup.
- Do not remove the local integration clone until deployment and final evidence
  verification are complete.
