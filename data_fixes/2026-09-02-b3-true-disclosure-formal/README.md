# B3 true-first-disclosure formal-run evidence archive (r4)

This directory preserves a small, review-oriented core from the B3 formal
rerun executed on WSL2 on 2026-09-02 after the true-first-disclosure
provenance line and the Jan-01 CSMAR balance-event PIT correction were merged
into `main`. It does **not** contain the complete formal-run tree.

## Provenance

- Run code commit: `013f3bc9fde276671e400abc317aa46c47fcb0bc`
  (`fix(b3): exclude CSMAR Jan-01 balance events before derived rows`, an
  ancestor of `main`; `run_manifest.json.code_commit` records the same hash).
- Config hash: `33e7f69ff47e84e9ac92cabee2aa4f2fd1c19850e3d1a27d42a375a63c6b2c61`
  (unchanged from the 2026-08-12 formal run).
- Execution host: WSL2 worktree `/home/ghls/style_timing_signal-jan01-pit`,
  run root `/home/ghls/b3_runs/20260902_jan01_pit_r4`, Python 3.13.9.
- Stage order and receipts:
  - `execution_receipt.txt`: `b3_build --stage states` (preflight, exposures,
    portfolios, states) 15:39-16:06 CST, exit 0, peak RSS 20.5 GB.
  - `execution_receipt_eval.txt`: first eval attempt, exit 2 after 2 s with
    `STRUCTURE_PROVENANCE_MISSING` because the structure stage had not been
    run. Kept as-is; no backtest output was written by this attempt.
  - `execution_receipt_structure_eval.txt`: `b3_structure` (exit 0, 38 s) then
    `b3_eval` (exit 2, 10 s, `DATA_BLOCKED` by `SALG_FRESHNESS`).
- Independent coverage audit (`core/audit/r4_coverage_audit.json`, run on
  `main` `cda5b2b` with `tools.audit_b3_disclosure_coverage`):
  629,556 / 629,556 model rows verified, `coverage_ready=true`.
- Determinism: `monthly_exposures.csv.gz`, `exposure_diagnostics.csv`,
  `coverage_audit.csv`, `preflight.json` and `exposures.json` are byte-identical
  to the r3 rebuild recorded in `data_fixes/2026-09-02-b3-jan01-balance-pit/`.

The `core/` directory contains exactly 31 selected files listed in
`inventory.json` under `core_files` (paths, sizes, SHA-256).
`formal_run_files` separately inventories all 57 files found under the run
root (`formal_run_files.tsv` is the same list as fetched from WSL). The
complete file content belongs in the external tarball described below.

## External complete archive

~~~text
/home/elfbob/claude-code/deploy_backups/2026-09-02-b3-true-disclosure-formal/20260902_jan01_pit_r4.tar.gz
~~~

Member root `20260902_jan01_pit_r4/`. Size 117,843,704 bytes, SHA-256
`6c7619c9e8965fdf43f3fb8f794b76cec543c207384caa26578c809218518286`. The tar
was created on WSL and its digest matched after transfer
(`remote_tar_receipt.txt` next to the tarball). The same path, size, digest
and member root are recorded in `inventory.json.external_archive`.

## Verify the repository core

~~~bash
python3 tools/verify_b3_formal_archive.py \
  --inventory data_fixes/2026-09-02-b3-true-disclosure-formal/inventory.json \
  --root data_fixes/2026-09-02-b3-true-disclosure-formal/core
~~~

Prints `OK` only when the core file set exactly matches the inventory and every
file has the recorded size and digest. To verify or restore the external
tarball, follow the procedure in
`data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/README.md` with the
inventory path above and member root `20260902_jan01_pit_r4`.

## How to read the result

Interpret only `core/backtest/run_manifest.json` and `core/backtest/verdicts.csv`.
The verdict record is `docs/plans/2026-09-02-b3-true-disclosure-formal-verdict.md`.

## Limitations

- The repository core is a selected evidence set, not a complete copy; the
  complete archive is host-local and outside Git.
- `.gitattributes` preserves the core bytes without line-end conversion.
- Hash verification establishes byte identity, not analytical correctness or
  acceptance of the B3 result.
- `SALG_FRESHNESS` remains: 000820.SZ has empty CSMAR income-statement facts
  for 2020-03-31 and 2021-03-31, so `salg_valid_through` stays 2020-04-30.
  This archive does not repair data or change any production signal.
