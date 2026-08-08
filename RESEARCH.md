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
| 9 | **Single-name: FADE unusual bullish flow (euphoria)** | **−2.0% avg underperformance in 21d after bullish premium spikes** (4 of 6 names) | ⚠️ real but modest; contrarian signal |

## The three conclusions

1. **Base position: cap-weighted core ≈ SPY.** No sector-level overlay we tested
   (momentum, mean-reversion, flow, insiders, VWAP) reliably beats holding the
   market. A cap-tilted core across all 11 sectors matched SPY while never being
   concentrated — that plus a disciplined buy-low/trim-high band (row 3) is the
   validated strategy.
2. **Whale/options flow is contrarian at the single-name level, not a follow
   signal.** Aggregate bullish premium spikes mark crowd euphoria: stocks
   underperformed their own baseline by ~2% over the following month (row 9).
   The actionable use is a **trim signal on names you already hold**, not a buy
   signal. Caveat: ~30–50 events/name, one regime (2024–26), suggestive not proof.
3. **The curated institutional signals can't be backtested** — flow alerts
   (sweeps, repeated hits), dark-pool blocks, GEX have no deep history via the
   API. They can only be evaluated live/forward. Treat them as discretionary
   context, not proven edge.

## Practical playbook this supports

- Hold a cap-weighted sector core (or just SPY — nearly identical).
- Rebalance monthly with a ±band: trim what's stretched, add what's lagging.
- On individual holdings: when price is stretched AND call premium z-spikes
  (euphoria), that is the moment to trim — the one whale signal that tested out.
- Use VWAP bands, flow, dark pool as discretionary timing context, knowing the
  mechanical versions carry no edge.

## Testing notes

- Event study: 5d mean of net_premium vs trailing 60d distribution; |z| > 1.5
  defines an event; forward returns vs same-window unconditional mean.
- Sector weight tests used static current-cap weights (mild hindsight); a live
  implementation should refresh weights from current market caps.
- Costs modeled at 2bps/side on turnover; results insensitive to 1–5bps.

*Not investment advice. Small samples, one macro regime, past ≠ future.*
