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

---

## Proposed overlay: dealer gamma (GEX)

Two extensions were proposed: a **directional eligibility gate** (reference-bar
close above the zero-gamma level for longs, below for shorts) and a
**volatility-regime overlay** that flexes the section-4 stop cap.

**Historical GEX is not obtainable here.** Option-chain snapshots from the wired
data source are current-only — there is no `as_of` for a past date — so dealer
gamma cannot be reconstructed across the test window. `RESEARCH.md` conclusion 3
already records this: the curated institutional signals, GEX included, have no
deep history via the API and can only be evaluated forward.

So both proposals were bounded with **oracles** — filters using information no
vendor could sell, because they read the future. If the perfect version doesn't
help, the real, noisy version cannot. Engine: `gex_feasibility.py`.

### The directional gate is bounded at +0.37R, and costs sample size

| | n | Expectancy |
|---|---|---|
| All spec trades | 103 | −0.073R |
| **Perfect direction oracle** (keep only trades where the underlying really did move the trade's way over 5 bars) | 51 | **+0.370R** |

A *perfect* directional filter keeps half the trades and lifts expectancy by
**+0.44R**. Everything a real gate can deliver sits between zero and there,
scaling roughly with how far its accuracy exceeds a coin flip:

> lift ≈ 0.44R × (accuracy − 0.50) / 0.50

`RESEARCH.md` row 11 measured the GEX directional signal at +0.9% over 5 days —
a weak tilt, implying accuracy in the low fifties. That maps to a lift of
**+0.02R to +0.04R**, against a standard error of 0.12R. Undetectable.

Meanwhile the gate makes the statistical problem strictly worse, because
resolving an edge needs trades and a gate removes them:

| Gate passes | Trades per 6 months | Symbol-years to resolve a +0.15R edge |
|---|---|---|
| 80% | 82 | ~12 |
| 60% | 62 | ~16 |
| 50% | 52 | ~20 |
| 35% | 36 | ~28 |

And row 11 flags the GEX *directional* claim as the suspect half of that
finding — clustered into 15–25 episodes, one year, one name carrying much of it,
the same failure mode that killed rows 9 and 10. **The gate would spend sample
size to buy a signal the repo has already flagged as unreplicated.**

### The volatility overlay fails at the channel, not at the input

This is the more promising half of the proposal in principle: row 11's *volatility*
finding is the part that held up (negative gamma → next-day moves 17% larger).
But the proposed channel — flexing the section-4 stop cap — is inert:

| Stop-cap rule | n | Expectancy |
|---|---|---|
| Spec as written (≤ 2.0 VAW) | 103 | −0.073R |
| Tighten in high vol / widen in low | 84 | −0.149R |
| Widen in high vol / tighten in low | 89 | −0.058R |
| **Perfect** vol oracle, cap at realised range ≤ 1.0× | 77 | −0.205R |
| **Perfect** vol oracle, cap at ≤ 1.5× | 95 | −0.156R |
| **Perfect** vol oracle, cap at ≤ 2.0× | 99 | −0.071R |

Even knowing the realised range in advance, conditioning the stop cap on it does
not beat leaving the cap alone. The problem is not the quality of the volatility
forecast — it is that stop width is the wrong lever.

### A volatility effect that looked real and wasn't

Splitting trades by trailing-ATR percentile at the reference bar showed a
0.48R gap: high-vol third **+0.213R** (n=38) against low-vol third **−0.267R**
(n=37). Tempting, and it survived leave-one-out (+0.12R to +0.32R across all
eight symbols).

It does not survive an honest test. The quintiles are not monotonic
(−0.41, −0.16, −0.16, −0.23, +0.37 — one bucket, not a dose-response), and
permuting the volatility label across the same 103 trades 5,000 times gives:

| Test | Real | p |
|---|---|---|
| High-third minus low-third spread | +0.481R | **0.085** |
| Largest \|mean\| among five quintiles (corrects for having looked at five) | 0.409R | **0.480** |

Neither clears 0.05, and the corrected test is not close. The effect is what
looking at five buckets on 103 trades produces by chance.

### Where this leaves overlays generally

Every gate is a trade: it must add more expectancy than the sample size it
destroys. On a system whose entry selection already tests indistinguishable from
random (see the permutation test above) and which needs ~255 trades to resolve,
**adding filters is the wrong direction** — it shrinks n toward the point where
nothing is ever provable. The leverage is in finding an entry premise that
predicts, not in conditioning one that doesn't.

If GEX is pursued anyway, the honest sequence is: collect it forward for a year
across a wide universe, test whether it predicts direction *on its own* before
attaching it to anything, and only then consider it as a gate.

## Reproducing

```bash
python vp195_spec.py --spec samples/cohort.txt              # the table above
python vp195_spec.py --spec samples/cohort.txt --trades     # every trade
python vp195_spec.py --spec samples/cohort.txt --log out.csv  # spec-8 log
python vp195_spec.py --spec samples/cohort.txt --state 6    # live eligibility
python null_test.py                                         # the permutation test
python gex_feasibility.py                                   # the GEX oracle bounds
```

*Research and education only. Not investment advice.*
