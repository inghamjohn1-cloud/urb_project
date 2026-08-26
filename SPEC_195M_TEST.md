# The Specified 195m POC/VAH/VAL Rules — Test Results

This tests the **numbered rule specification** (sections 1–8), not the earlier
draft in `PLAN_195M_VOLUME_PROFILE.md`. They are different systems: the earlier
draft keyed triggers off multi-bar composite value areas and returned −0.21R per
trade. **These rules key off the reference bar's own footprint levels, require
three consecutive POC migrations, and add a chase filter and a value-area-width
stop cap.** That is a materially better-specified system and it tests better.

Engine: `vp195_spec.py`. Data: 8 symbols × 6 months of real 195m bars
(`samples/`), built from 1-minute bars, regular hours only.

---

## Headline

| | |
|---|---|
| Bars scanned | 2,096 |
| Trades taken | **102** |
| Pooled expectancy | **−0.08R** (95% CI −0.32 .. +0.15) |
| Hit rate | 44% |
| Long | **+0.15R** (n=54) |
| Short | **−0.34R** (n=48) |
| Leave-one-out | −0.03R to −0.13R — no symbol drives it |

Materially better than the earlier draft (−0.21R). Statistically, still
indistinguishable from zero.

---

## The result that matters: does POC migration ×3 predict anything?

The heart of the spec is section 3's eligibility condition. So it was tested
directly, by permutation: keep the triggers, the filters, the stop rules and the
whole of sections 5 and 6 **exactly as written**, and replace *only* the
reference-bar selection with a random draw matched to the real base rate
(up-×3 fires on 11.7% of bars, down-×3 on 10.8%). 200 resamples.

| | Real | Random-reference null | Percentile |
|---|---|---|---|
| Long | +0.146R | mean −0.025R (5th −0.43, 95th +0.70) | **82nd** |
| Short | −0.314R | mean −0.188R (5th −0.73, 95th +0.29) | **34th** |
| All | −0.073R | mean −0.103R (5th −0.44, 95th +0.37) | **58th** |

**A randomly chosen reference bar performs about as well as one selected by
POC migration ×3.** The long side sits at the 82nd percentile — suggestive, but
short of the 95th you would need, and the short side is *worse* than random.

Confirmed by varying the requirement directly:

| Migrations required | n | Expectancy | SE |
|---|---|---|---|
| ×1 | 298 | −0.057R | 0.094 |
| ×2 | 197 | −0.150R | 0.116 |
| ×3 | 103 | −0.073R | 0.120 |
| ×4 | 51 | −0.195R | 0.189 |
| ×5 | 16 | −0.479R | 0.363 |

Non-monotonic and inside one standard error throughout. The ×1 row is the most
reliable estimate in the table simply because it has three times the sample, and
it is −0.06R ± 0.09. Requiring more migrations does not improve the edge; it
shrinks the sample until noise dominates.

---

## The filters

Neither section 3's chase filter nor section 4's stop cap improved results here,
though none of these differences clears one standard error (≈0.10R at these
sample sizes):

| Configuration | n | Expectancy |
|---|---|---|
| Spec as written (chase 1.0 VAW, stop ≤ 2.0 VAW) | 103 | −0.073R |
| Chase filter off | 104 | −0.050R |
| Stop cap off | 147 | +0.004R |
| Both off | 171 | −0.005R |
| Chase tightened to 0.5 VAW | 77 | −0.066R |
| Stop cap tightened to 1.5 VAW | 65 | −0.127R |

Worth noting: this is *far* better behaved than the earlier draft, where the
filters were actively anti-predictive (they selected trades that did 0.17R
*worse* than the ones they rejected). Here they are roughly neutral.

Trigger timing (the spec says "the subsequent bar"; a window had to be assumed):
1 bar −0.091R (n=82), 2 bars −0.070R, 3 bars −0.073R, 4 bars −0.049R,
6 bars −0.087R. Flat — the assumption does not drive the result.

By trigger type: continuation −0.12R (n=25), pullback −0.07R (n=77).
By exit rule (spec 8): trailing stop −0.23R (n=79), failed auction **+1.23R**
(n=8) and +0.39R (n=4), migration reversal −0.15R (n=7) / −0.19R (n=4). The
section 6.3 failed-auction exit is the standout, on a small sample.

---

## What it would take to settle this

The long side shows +0.146R with sd 1.19R over 54 trades — standard error
0.162R. To call an edge that size real at 95% confidence you need:

> **n ≈ 255 trades**, i.e. roughly **19 symbol-years** at this signal's rate
> (~7 trades per symbol per 6 months).

That is the honest verdict on the long side: **not disproven, but nowhere near
demonstrated, and this dataset cannot resolve it.** Either widen the universe
substantially (40+ names) or extend the history several years before drawing any
conclusion. The short side needs no further testing — it is worse than random in
this sample and directionally wrong across the cohort.

---

## Implementation notes — where the spec was silent

Five decisions had to be made. Each is a place where results could shift, and
each is a one-line change in `vp195_spec.py`:

1. **Trigger window.** The spec says "the subsequent bar". A pullback needs time,
   so a 3-bar window is used. Sensitivity shown above: negligible.
2. **Entry price.** Section 1 says all determinations are made at bar close, so
   entry is the trigger bar's close.
3. **Section 6.4 applied to exits only.** Signals on the 12:45 bar execute at
   that close; signals on the 16:00 bar execute at the next bar's open, as
   section 6 requires. The trailing stop is treated as a resting order and fills
   intrabar.
4. **Gap fills.** A gap through the stop fills at the open, not at the stop
   price. Without this every stop-out is exactly −1.00R, which flatters the tail
   — it changed the pooled result from +0.00R to −0.08R and the worst trade from
   −1.00R to −4.89R.
5. **Pullback level.** "Closes at or above that level" is tested against
   whichever level price actually reached — VAL if it traded to VAL, otherwise POC.

## Not applied

- **2.1 earnings blackout** — needs a scheduled-earnings calendar, not supplied.
  This is the most material omission: the window covers roughly two reporting
  seasons per name.
- **2.4 Risk Desk "Extreme" regime** — external table.
- **2.5 Rotation Discovery Radar universe** — approximated by the 8-symbol
  cohort. The max-3-concurrent cap is applied across it and blocked 1 entry.

Neither 2.1 nor 2.4 affects R-multiples through position sizing, since R is by
definition normalised by the entry-to-stop distance.

## Reproducing

```bash
python vp195_spec.py --spec samples/cohort.txt              # the table above
python vp195_spec.py --spec samples/cohort.txt --trades     # every trade
python vp195_spec.py --spec samples/cohort.txt --log out.csv  # spec-8 log
python vp195_spec.py --spec samples/cohort.txt --state 6    # live eligibility
python null_test.py                                         # the permutation test
```

*Research and education only. Not investment advice.*
