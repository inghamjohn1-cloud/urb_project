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

---

# Result (recorded after the test, criteria unchanged)

100 tickers, 770 events, no overlap with the original 119.

|               | 21d edge |   t   |  win  |  n |
|---------------|---------:|------:|------:|---:|
| in-sample     |  +4.26%  | +2.27 | 71.1% | 45 |
| out-of-sample |  +4.09%  | +1.01 | 55.4% | 74 |
| pooled        |  +4.11%  | +1.57 | 61.3% |119 |

Ticker-clustered 95% CI out-of-sample: **[-2.10%, +12.17%]** — crosses zero.

## Verdict: NOT CONFIRMED

Edge landed at +4.09%, above the +2.0% threshold, but the CI includes zero,
and the registered rule required both. No criterion was adjusted after the
fact.

The effect size replicating to within 0.2pp on unseen names is real evidence
that something is there — a spurious pattern does not usually do that. What
did not replicate is the reliability: 71% win becoming 55% means the
in-sample hit rate was luck, and the surviving mean rests on a handful of
large winners inside a near coin-flip distribution.

## Secondary outcomes

1. **The -$1M flow split failed and inverted.** In-sample +5.2% strong vs
   +1.1% weak; out-of-sample +3.4% strong vs +5.2% weak. This was the one
   dimension the code labelled VALIDATED and scored 2 of 5 points for. It
   was overfitting and has been removed from `pec_score`.
2. Shorter horizons stayed flat, consistent with in-sample: 5d +0.28%,
   10d +1.45%.
3. Pooled t=1.57 on n=119 is still short of significance.

## What this changes

The gate stays — it is the only thing that survived contact with new data,
and it survived on effect size. The scanner keeps firing on it and the
forward test keeps logging it. But it is not yet a signal to size on, and
the honest summary is "promising, unproven," not "validated."

## What would settle it

Power is the binding constraint, not method. At the observed dispersion,
separating a ~4% edge from zero needs several hundred events, so roughly
400-600 tickers rather than 219. That is a bigger fetch, not a cleverer
statistic. The forward test contributes ~6 events per earnings season and
will take years to matter on its own.
