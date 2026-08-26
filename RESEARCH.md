# Research Findings — What Actually Works (and Doesn't)

Honest results from backtests on real data (Massive daily OHLC ~10y for the 11
SPDR sector ETFs + SPY; Unusual Whales daily options-flow ~2y for sectors and
~2y for 6 liquid single names). All tests net of 2bps/side costs, no lookahead.
Every number below was produced by code in this repo against saved raw data.

## The scorecard

| # | Idea | Result | Verdict |
|---|------|--------|---------|
| 1 | Momentum sector rotation (top-3 by momentum/RS, trend-gated) | 5.4% CAGR vs SPY 14.1% (10y) | ❌ badly loses; whipsawed at V-bottoms |
| 2 | Equal-weight sector basket, rebalanced | 10.9% vs SPY 14.4% (7y) | ❌ structural drag vs cap-weight |
| 3 | **Cap-weighted core + price buy-low/trim-high tilt (±50% band, monthly)** | **15.1% vs SPY 14.4%, Sharpe 0.80 vs 0.78** | ✅ ties/edges SPY, fully diversified |
| 4 | Sector tilt by options flow (follow institutions in) | 16.9% vs 17.9% for price tilt (1.5y) | ❌ worse than price alone |
| 5 | Insider sector flow as a signal | ~95% sells, ~50d history, bad rows | ❌ unusable |
| 6 | VWAP-band mean reversion, in/out of market | 0/11 sectors beat buy&hold (10y) | ❌ misses trends; only works in ranges |
| 7 | VWAP tilt around a held core (always invested) | 13.5% vs 13.7% core-only (8y) | ➖ no added edge |
| 8 | Single-name: FOLLOW unusual bullish options flow | +0.0%/1d, +0.1%/5d edge | ❌ no follow edge |
| 9 | Single-name: FADE unusual bullish flow (euphoria) | −2.0% 21d edge in tech cohort (NVDA/TSLA/AMD/AAPL/META/PLTR)… | ⚠️ see row 10 |
| 10 | **Row 9 robustness check** — same test on a diverse cohort (JPM/XOM/UNH/WMT/CAT/DIS) | **−0.25% 21d edge — the effect disappears out-of-sample** | ❌ does not generalize; at best a hot-momentum-name/regime artifact |
| 11 | **Dealer gamma (GEX), 1y history, 5 watchlist names** — days with net gamma < 0 | Vol: next-day \|move\| +17% larger (2.13% vs 1.83%, mechanism confirmed). Direction: +0.9%/5d, +1.6%/10d, +3.6%/21d pooled (n≈125 days) | ⚠️ vol effect credible; directional edge promising but suspect — events cluster into ~15–25 episodes, single year, NVDA's number is essentially one episode (April bottom). Same regime-risk that killed rows 9. Now under forward test (gex_negative in whale_eval) |
| 12 | **195m volume-profile swing rules (POC/VAH/VAL)** — 8-symbol cohort, 6 months, 2,016 bars scanned, 69 trades | **−0.21R/trade** (95% CI −0.43..+0.01), hit rate 41%. Negative on 6 of 8 names; leave-one-out stable (−0.15 to −0.31). Every parameter setting tested lands at or below zero. **The veto filters are anti-predictive**: signals taken −0.21R vs signals rejected −0.04R | ❌ no edge; filters actively select worse trades |
| 13 | **GEX overlay on the 195m spec rules** — directional gate + volatility stop-width overlay, bounded by oracles (historical GEX unobtainable, see conclusion 3) | Perfect direction oracle caps the gain at **+0.37R** (keeps 50% of trades); row 11's measured GEX tilt implies only **+0.02..+0.04R** against SE 0.12. Stop-width channel inert even with perfect volatility foresight (−0.21R to −0.07R vs −0.07R baseline). An apparent high-vs-low-vol spread of +0.48R dies under permutation (p=0.085 raw, **p=0.48** corrected for five buckets) | ❌ gate costs more sample size than it can buy; vol overlay wrong channel |

## The three conclusions

1. **Base position: cap-weighted core ≈ SPY.** No sector-level overlay we tested
   (momentum, mean-reversion, flow, insiders, VWAP) reliably beats holding the
   market. A cap-tilted core across all 11 sectors matched SPY while never being
   concentrated — that plus a disciplined buy-low/trim-high band (row 3) is the
   validated strategy.
2. **Whale/options flow carries no generalizable signal at the single-name
   level, in either direction.** Following bullish spikes: no edge (row 8).
   Fading them looked promising in the tech cohort (−2% over 21d, row 9) but
   **failed the out-of-sample robustness check** on a sector-diverse cohort
   (row 10). The euphoria-fade survives only as a *hypothesis about crowded,
   retail-heavy momentum names* — untestable further with available history.
3. **The curated institutional signals can't be backtested** — flow alerts
   (sweeps, repeated hits), dark-pool blocks, GEX have no deep history via the
   API. They can only be evaluated live/forward. Treat them as discretionary
   context, not proven edge.

4. **A rules-based volume-profile swing plan on 195m bars showed no edge, and
   its filters made things worse.** Full write-up in
   `PLAN_195M_VOLUME_PROFILE.md` §10; harness in `vp195.py` / `vp195_cohort.py`.
   Two things are worth carrying forward regardless of the verdict. First, the
   failure is at *entry*, not exit: stopped-out trades had a median MFE of only
   +0.44R and 53% never reached +0.5R, so the pattern simply doesn't precede
   directional movement — and changing the exit rules (breakeven after T1 vs T2
   vs never) moved the pooled mean by 0.03R, i.e. nothing. Second, **raising the
   reward:risk threshold made results monotonically worse** (−0.02R at 1.0R,
   −0.26R at 2.0R, −0.68R at 2.5R). A real edge improves when you demand better
   payoff; this one degrades, which is the signature of a selection rule picking
   up noise. Even granting every ambiguous intrabar fill to the target rather
   than the stop, the pooled mean only reaches −0.10R, gross of costs.

5. **Overlays cannot rescue a signal that does not predict.** Adding a gate to
   the 195m rules costs sample size that must be paid back in expectancy, and on
   a system already indistinguishable from random entry that trade never clears.
   A *perfect* directional oracle lifts the spec rules only to +0.37R; the GEX
   tilt measured in row 11 implies a hundredth of that. Full working in
   `SPEC_195M_TEST.md`; harness in `gex_feasibility.py`. The generalisable
   lesson is the oracle technique itself — bound a proposed filter with a
   version that reads the future before paying for the data to build the real
   one.

## Practical playbook this supports

- Hold a cap-weighted sector core (or just SPY — nearly identical).
- Rebalance monthly with a ±band: trim what's stretched, add what's lagging.
  This is the only tested approach that kept pace with SPY while diversified.
- Use VWAP bands, flow, dark pool as discretionary timing context only —
  every mechanical version tested (follow AND fade) carried no generalizable
  edge. In crowded momentum names, extreme call-premium spikes *may* mark
  euphoria tops (unproven; failed to replicate outside the tech cohort).

## Testing notes

- Event study: 5d mean of net_premium vs trailing 60d distribution; |z| > 1.5
  defines an event; forward returns vs same-window unconditional mean.
- Sector weight tests used static current-cap weights (mild hindsight); a live
  implementation should refresh weights from current market caps.
- Costs modeled at 2bps/side on turnover; results insensitive to 1–5bps.

*Not investment advice. Small samples, one macro regime, past ≠ future.*
