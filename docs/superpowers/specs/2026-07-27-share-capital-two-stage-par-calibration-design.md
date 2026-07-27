# Share-Capital Two-Stage Par Calibration Design

**Status:** Approved by the user on 2026-07-27.

## Context

`stock_selector` commit `c31104e` moved the par-calibration overlap from the
latest market window to `[CSMAR_END, CSMAR_END + 180 days)`. The first
production rerun was safe but under-recovered:

- B3 gap tickers: `696 -> 282`;
- recovered: `414` rather than the expected approximately `639`;
- newly introduced B3 gaps: `0`;
- valued-to-unvalued ticker regressions: `0`.

The original `639/696` probe used each ticker's earliest post-`CSMAR_END`
observation. The implementation instead takes the median over all observations
in a 180-day window. Share changes inside that window therefore contaminate the
median for many tickers.

Read-only production simulations gave:

| Calibration estimator | Original 696 recovered | Residual tail | Existing par calibrations lost |
| --- | ---: | ---: | ---: |
| 180-day median | about 415 | about 281 | 0 (current state) |
| 90-day median | 602 | 94 | 1 |
| 30-day median | 640 | 56 | 3 |
| Earliest post-anchor row | 639 | 57 | 10 |
| 30-day median, then 180-day fallback | about 640 | about 56 | 0 |

The three 30-day-only losses are `000657.SZ`, `300286.SZ`, and `688568.SH`.
All have B3-relevant history, so a 30-day-only implementation is not acceptable.

## Requirements

1. Prefer observations tightly contemporaneous with `CSMAR_END`.
2. Preserve every calibration that the current 180-day estimator can make.
3. Do not widen `_STANDARD_PARS` or `_PAR_TOLERANCE`.
4. Do not change `indicator_implied` node generation.
5. Keep the overlap read bounded and OOM-safe.
6. Fail verification if the rerun introduces any new B3 historical gap, even
   when the ticker still has a valued post-2025 node.

## Design

Keep `_read_overlap` bounded to the existing 180-day forward window. Change only
the pure par estimator:

1. Compute the median implied par from rows in
   `[CSMAR_END, CSMAR_END + 30 days)`.
2. Attempt to snap that median to the existing standard denominations.
3. If it cannot snap, compute the median over the full supplied 180-day overlap
   and attempt the same snap.
4. Return `par_unknown` only when both stages fail.

The primary 30-day median is more robust than a single earliest observation and
avoids later share events. The fallback handles delayed alignment between the
CSMAR numerator and market-side share count. Because fallback behavior is the
current estimator, the change is monotonic: it can add calibrated tickers but
cannot remove a calibration the current 180-day estimator produces.

`verify_par_recovery.py --phase after` will add a second regression set:

```text
new_historical_gaps = gap_after - gap_before
```

The command must return nonzero if either `new_historical_gaps` or the existing
valued-to-unvalued regression set is nonempty.

## Testing

Add focused pure-function tests proving:

1. a later share event can corrupt the 180-day median while the 30-day primary
   still recovers par `1.0`;
2. an unsnappable 30-day primary falls back to a snappable 180-day median;
3. a ticker unsnappable in both stages remains `par_unknown`;
4. existing par `1.0`, par `0.1`, tolerance, and indicator-implied behavior are
   unchanged.

Add verification-script tests for a new historical gap that still retains a
post-2025 valued node. Run targeted tests, the full `stock_selector` suite, the
test-schema backfill, one idempotent production rerun, final before/after
verification, and then guarded B3 evaluation.

## Operational Gates

- Preserve the existing `gap_before.csv` and `valued_tickers_before.csv`.
- Production rerun remains a single idempotent UPSERT invocation.
- Final acceptance requires zero new historical gaps and zero
  valued-to-unvalued regressions.
- Run real-data B3 only under `systemd-run --user --scope -p MemoryMax=8G`.
- Do not automatically resolve the final non-standard-par tail.
