# Insider buying test — registered before the data was pulled

Second of the "follow the money" leads. Registered the same way as
OOS_PREREGISTRATION.md, and for the same reason: the first study found its
result by searching ten buckets and then could not replicate the details.

## Lead triage (done first, recorded here)

| lead | history | testable |
|---|---|---|
| open interest confirmation | none — endpoint returns current session only | no, live-only |
| dark pool blocks | 90 trading days (earliest 2026-03-30) | no, live-only |
| **insider Form 4** | **full 2 years with filing dates** | **yes — this test** |
| 13F institutional holdings | quarterly, 45-day statutory lag | deferred: too stale for a 21-day horizon |

Two of four leads cannot be backtested on this data tier. They will be wired
into the scanner to accumulate forward evidence instead. No backtest of them
is possible and none will be claimed.

## The look-ahead trap, avoided explicitly

UW returns Form 4 rows with `transaction_date` a year before `filing_date`
(e.g. traded 2024-09-12, filed 2025-09-12). Anchoring on transaction_date
would buy on information the market did not have and manufacture a fake
edge. **Every event keys off `filing_date`, and the entry price is the close
on the filing date.** Forward returns start from that close.

## The rule under test

An **insider cluster buy**: on a given filing date for a given ticker,
open-market purchases (Form 4 transaction code `P`) reported by 2 or more
distinct insiders, or a single purchase of >= $500,000 notional.

Rationale for requiring size or a cluster: lone token purchases are routinely
cosmetic. Neither threshold is tuned — both are set now and will not move.

## Sample

The same 219 tickers already fetched (119 in-sample + 100 out-of-sample), so
no price data is re-pulled and no new universe is selected. Survivorship bias
noted in OOS_PREREGISTRATION.md applies here too and biases results upward.

## Primary outcome

**Mean 21-day forward return after a cluster-buy filing, minus the mean
21-day forward return across all days of the same tickers.**

One number, reported whatever it is.

## Verdicts, fixed in advance

Four leads were triaged, so the significance bar is corrected for the number
of tests actually run on data (2: this one and the flow gate). Bonferroni:
the CI must exclude zero at 97.5% rather than 95%.

- **Confirmed** — edge >= +2.0% and the ticker-clustered 97.5% CI excludes zero.
- **Not confirmed** — positive but CI includes zero.
- **Dead** — edge <= 0%.

## Secondary (reported, non-overriding)

1. 5d and 10d horizons.
2. Officer/director purchases vs 10% owners.
3. Excluding Rule 10b5-1 planned trades.
4. Whether insider buying co-occurring with the PEC gate beats either alone.

## Guard

Baseline comes from the same tickers over the same window, so a rising market
lifts signal and baseline together. If fewer than 30 events are found, the
test is reported as underpowered rather than stretched with looser thresholds.
