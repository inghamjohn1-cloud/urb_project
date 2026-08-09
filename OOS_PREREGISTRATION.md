# Out-of-sample test — registered before the data was pulled

The 21-day edge of +4.26% (t=2.27) came from a search over roughly ten
bucket definitions across 119 tickers. Corrected for that search the result
was p=0.153, so it is a hypothesis, not a finding. This document fixes the
test that decides it, written before any new ticker was fetched.

## The rule under test

Frozen as committed in `pec_scan.py` at a32c962 — `pec_qualifies()`:

    EPS surprise <= -20%   AND   net premium < 0   AND   stock down vs pre-earnings close

No parameter may be adjusted after seeing the new data. If the rule needs
changing, the result of this test is void and a fresh sample is required.

## Sample

Tickers with liquid options and >= $2B market cap that were NOT among the
119 in the original study. Same 500-row history window, same event
extraction (`pec_study.py`), same forward-return convention.

## The single primary outcome

**Mean 21-day forward return of qualifying events, minus the mean 21-day
forward return of all events in the new sample.**

One number. Reported whatever it is.

## What counts as what — decided in advance

- **Confirmed** — edge >= +2.0% and the ticker-clustered 95% CI excludes
  zero. The multiple-comparisons objection dies: the rule was fixed before
  these events were seen.
- **Not confirmed** — edge between 0% and +2.0%, or the CI crosses zero.
  The pattern is real but too small or too noisy to size on.
- **Dead** — edge <= 0%. The original result was a product of the search
  and the scanner goes back to having no validated signal.

## Secondary checks (reported, but they do not override the primary)

1. The -$1M strong/weak flow split, same direction as in-sample.
2. 5d and 10d horizons.
3. Pooled result across both samples.

## Pre-committed guard

The in-sample events are not reused. The new sample's baseline is computed
from the new sample only, so nothing from the original study can leak into
the comparison.
