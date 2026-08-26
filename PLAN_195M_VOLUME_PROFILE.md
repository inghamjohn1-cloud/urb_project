# 195-Minute Swing Plan — POC / VAH / VAL

A fully mechanical swing plan for the **195-minute** chart, driven by volume
profile: **POC** (point of control), **VAH** (value area high), **VAL** (value
area low). Every rule below is testable — nothing says "if it looks strong."

**Status: unvalidated.** No backtest in this repo covers it (see `RESEARCH.md`
for what has and hasn't held up here). Treat it as a hypothesis to forward-test
on the log in §10, not as a proven edge.

---

## 1. Why 195 minutes

Regular-hours session = 390 minutes. **195m = exactly half a session, so you get
two bars a day**: `09:30–12:45` and `12:45–16:00` ET.

That matters for a rules plan:

- **Two decision points per day**, at 12:45 and 16:00 ET. No intraday babysitting.
- Bar 1 captures the opening auction + morning trend; bar 2 captures the
  afternoon auction + close. Each bar's own profile is a genuine half-day
  auction, not an arbitrary slice.
- A 3–10 day swing = **6–20 bars**. Long enough for structure, short enough to
  react before a daily chart would.

⚠️ **Check this first**: if your feed includes pre/post market, bars will *not*
split at 12:45 and every level below drifts. Set the chart to **RTH only** and
confirm you see exactly two bars per day.

---

## 2. The three level sets

| Tier | Lookback | What it gives | Role |
|---|---|---|---|
| **Bar profile (BP)** | 1 bar | `bPOC`, `bVAH`, `bVAL` from the footprint | Trigger + trailing reference |
| **Composite value (CVA)** | rolling **20 bars** (≈2 weeks) | `cPOC`, `cVAH`, `cVAL` | **The swing structure — all entries and stops key off this** |
| **Node map (NM)** | rolling **60 bars** (≈6 weeks) | HVNs (volume peaks), LVNs (volume valleys) | Target selection |

Value area = the price range containing **70%** of the profile's volume, centred
on the POC. Use the same row size everywhere (the screenshot uses Manual 10);
if you change it, re-derive every level.

**Working definitions used throughout:**

- **Value width** `VW = cVAH − cVAL`
- **ATR** = ATR(14) on the 195m chart
- **Acceptance** = **two consecutive 195m closes** on the same side of a level,
  *and* the second bar's `bPOC` on that side too. One close is a poke; a bar
  that closes through but leaves its POC behind has not accepted.
- **Rejection** = a bar trades through a level intrabar but closes back on the
  original side, with `bPOC` on the original side.
- **Value migration** = `cPOC(now) − cPOC(5 bars ago)`. Positive and rising =
  buyers moving value up.

**HVN vs LVN, and why targets sit where they do:** an HVN is price the market
agreed on — it attracts and stalls price, so it's where you take money off. An
LVN is price the market rejected — it travels fast, so never place a target
*inside* one; put it on the far side.

---

## 3. Regime gate (checked before every entry — no exceptions)

Compute on the 195m chart at the close of each bar.

**Long regime — all four true:**
1. Close > **SMA(50)** on 195m (≈25 sessions).
2. SMA(50) higher than it was **10 bars** ago.
3. **Value migrating up**: `cPOC(now) > cPOC(5 bars ago)`.
4. `cVAL(now) ≥ cVAL(5 bars ago)` — the floor of value is not sinking.

**Short regime — the exact mirror** (close below a falling SMA(50), `cPOC` and
`cVAH` both lower than 5 bars ago).

**Neutral / balanced — no trend trades.** If neither set is fully true, or if
the current 20-bar CVA overlaps the CVA from 10 bars ago by **more than 75%**
(`overlap = intersection(VW_now, VW_then) / VW_now`), the market is rotational.
Only Setup C (§4.3) is permitted, at half size.

The screenshot's RGLD is a textbook long regime: price above a rising SMA, and
each bar's profile stacking higher than the last — value migrating up with no
overlap. That stair-step of non-overlapping bar profiles *is* the signal.

---

## 4. The three setups

Only one position per symbol. Never hold Setup A and Setup B in the same name.

### 4.1 Setup A — VAL reclaim (primary; buy the pullback in an uptrend)

The bread-and-butter trade. You are buying a pullback into the bottom of value
that gets rejected — responsive buyers defending the low end of the range.

**Conditions (all, at a 195m bar close):**
1. Long regime per §3.
2. Price traded **at or below `cVAL`** during the current bar or the one before.
3. The current bar **closes back above `cVAL`**.
4. Current bar `bPOC ≥ cVAL` (buyers, not just a wick).
5. Bar range ≤ **2.5 × ATR** (skip climax bars — the stop is too far).

**Entry:** buy-stop at **signal bar high + 0.05**, working for the **next two
bars only**, then cancel. If price gaps above the trigger, take the open only if
the gap is < 0.5 × ATR above it; otherwise stand aside and wait for a new signal.

**Initial stop:** `min(signal bar low, cVAL) − 0.25 × ATR`.

**Invalidation before fill:** cancel the order if any bar **closes below `cVAL`**
before you're filled. The setup is dead — do not re-enter until a fresh signal
bar prints.

**Targets:** T1 `cPOC` · T2 `cVAH` · T3 `cVAH + VW` (§5).

### 4.2 Setup B — acceptance above VAH (continuation breakout)

Value has broken out and been *accepted* above. You're joining an initiative
move, not fading it.

**Conditions (all):**
1. Long regime per §3.
2. **Acceptance above `cVAH`**: two consecutive closes above `cVAH`, second
   bar's `bPOC` above `cVAH` (§2). One-bar pokes don't count.
3. Entry taken **one of two ways** — pick per trade, don't switch mid-trade:
   - **B1 (momentum)**: buy-stop at the acceptance bar's high + 0.05, valid 2 bars.
   - **B2 (retest, preferred)**: resting buy-limit at `cVAH + 0.10`, valid **4
     bars**, cancelled if any bar closes below `cVAH`. Better fill, lower hit rate.
4. Bar range of the acceptance bar ≤ 2.5 × ATR.

**Initial stop:** `cVAH − 0.5 × ATR`, or below the acceptance bar's `bPOC`,
whichever is **lower**. Rationale: if price is back below the POC of the bar
that broke out, the breakout failed.

**Targets:** T1 `cVAH + 0.5 × VW` · T2 `cVAH + 1.0 × VW` · T3 next HVN above
from the 60-bar node map (§5).

### 4.3 Setup C — the 80% rule (rotation, both directions)

The one counter-trend trade, and the only trade allowed in a balanced market.

Classic market-profile logic: when price opens outside value and is then
**accepted back inside**, the auction has failed and price tends to rotate to
the *far* side of the value area.

**Conditions (long version):**
1. Price closed **below `cVAL`** at some point in the last 3 bars.
2. Two consecutive closes **back inside** the value area (between `cVAL` and `cVAH`).
3. Second bar's `bPOC` inside value.

**Entry:** market on the close of that second bar, or next bar's open.
**Stop:** `cVAL − 0.5 × ATR` (below the level you just reclaimed).
**Target:** **single target, `cVAH`.** Full exit. No runner — this is a
rotation, not a trend trade.
**Size:** half normal risk in a balanced market; full risk only if the §3
regime agrees with the trade's direction.

Mirror everything for the short side (accepted back inside from above → target `cVAL`).

---

## 5. Exits — the ladder

Every trade exits in thirds. Define R = entry − initial stop (per share).

| Leg | Action | Then |
|---|---|---|
| **T1** | Sell 1/3 | Move stop on the rest to **breakeven** |
| **T2** | Sell 1/3 | Switch the last third to the trail below |
| **T3** | Sell the last 1/3 at target, or on the trail — whichever comes first | Flat |

**The trail (one rule, mechanical):** after T2, exit the remaining third on the
**first 195m close below the `bVAL` of the prior bar** (long) / above prior
`bVAH` (short). Nothing else — no discretionary "it looks weak."

**Minimum-R filter:** if T1 is closer than **1.0 R**, the trade is not worth
taking. Skip it. This kills entries taken too far from structure.

**Hard stop-out:** initial stop hit before T1 = full exit, −1R, done. No
averaging down, no widening, no "give it one more bar."

**Time stop:** if after **8 bars** (4 sessions) the trade has not reached T1 and
is under **+0.5R**, exit at market on the next bar close. Dead capital is a cost.

**Failed-breakout override (Setup B only):** an *acceptance back below `cVAH`*
(two closes, second bar's POC below) is a full exit at that close, even if the
stop hasn't been touched. That's the structure telling you the breakout failed
before your stop does.

**Event exit:** flatten before any earnings print in the name (§6).

---

## 6. Filters that veto a trade

Run this list before every entry. Any single hit = no trade.

- ❌ **Earnings** inside the expected hold: no new entry within **6 bars
  (3 sessions)** of a confirmed earnings date; flatten existing positions before
  the print. A gap through your stop is not a −1R loss, it's whatever the gap says.
- ❌ **Stop too wide**: structural stop > **2.0 × ATR**. Structure isn't clean
  enough to trade.
- ❌ **T1 < 1.0 R** (§5).
- ❌ **Climax signal bar**: range > 2.5 × ATR.
- ❌ **Gap beyond value**: session opens more than **1.5 × ATR** outside `cVAH`
  /`cVAL`. Wait for the first 195m bar to complete and re-derive levels; never
  chase the open.
- ❌ **Balanced market** (>75% CVA overlap, §3) for Setups A and B. Setup C only.
- ❌ **Thin profile**: signal bar volume < 60% of the 20-bar average volume. A
  profile built on no volume is not information.
- ❌ **Correlation stack**: already holding 2 positions in the same complex
  (e.g. RGLD + NEM + GDX are one bet on gold, not three).

---

## 7. Position sizing

```
shares = floor( account_equity × risk_pct / (entry − stop) )
```

- **risk_pct = 0.5%** per trade default; **1.0%** maximum, and only when the §3
  regime is fully aligned and all of §6 is clean.
- **Max 3 concurrent positions.** Max **2** in correlated names (§6).
- **Portfolio heat cap: 3.0%.** Sum of open risk (using current stops, so
  breakeven stops count as zero) may not exceed 3% of equity. At the cap, no new
  entries regardless of signal quality.
- **Drawdown brake:** after **3 consecutive −1R losses**, halve risk_pct until
  the next winning trade closes.

---

## 8. The daily routine

Twice a day, at the 195m closes — **12:45 ET** and **16:00 ET**:

1. **Update levels.** Re-read `cPOC`/`cVAH`/`cVAL` (20-bar) and the HVN/LVN map
   (60-bar). Write them down; they move every bar.
2. **Manage what's open first.** T1/T2/T3 hit? Trail triggered? Time stop at 8
   bars? Failed-breakout override? Earnings inside 6 bars?
3. **Re-check the regime gate** (§3) per symbol.
4. **Scan for signal bars** — A, then B, then C.
5. **Run the veto list** (§6) on each candidate.
6. **Size it** (§7), place the order with its stop as a single bracket, log it (§10).

Orders go in **at the bar close** with the stop attached in the same bracket. A
position without a resting stop is not part of this plan.

---

## 9. Worked example (structure only — read your own levels)

Numbers below are **illustrative**, not read off a live feed. Take the real
`cPOC`/`cVAH`/`cVAL` from your own footprint before risking anything.

Say RGLD on the 195m shows: `cVAL 258.50`, `cPOC 262.00`, `cVAH 266.50`,
ATR 4.00, account 100,000, risk 0.5%.

`VW = 266.50 − 258.50 = 8.00`

**Setup A fires:** a bar wicks to 257.20, closes 259.80 (above `cVAL`), `bPOC`
260.40 (above `cVAL` ✓), bar range 3.10 (< 2.5 × ATR = 10.00 ✓), volume above
the 20-bar average ✓.

| | Level | Working |
|---|---|---|
| Entry (buy-stop) | **260.95** | signal bar high 260.90 + 0.05 |
| Initial stop | **256.20** | min(257.20, 258.50) − 0.25 × 4.00 |
| **R** | **4.75** | 260.95 − 256.20 |
| T1 | **262.00** | `cPOC` — 1.05 R ✓ clears the 1.0 R filter |
| T2 | **266.50** | `cVAH` — 1.17 R |
| T3 | **274.50** | `cVAH + VW` — 2.85 R |
| Size | **105 shares** | (100,000 × 0.005) / 4.75 = 105.2 → 105 |
| Risk | **$499** | 105 × 4.75 |

Stop distance 4.75 < 2.0 × ATR (8.00) ✓. Trade is valid.

Sell 35 at 262.00 → stop to 260.95. Sell 35 at 266.50 → last 35 trails on
"close below the prior bar's `bVAL`." If 8 bars pass without touching T1 at
262.00 and the position is under +0.5R (0.5 × 4.75 = **+2.38/share**, i.e. price
under 263.33), it's out at market on the next close.

---

## 10. Forward-test log (do this before sizing up)

Nothing here is backtested. Paper or minimum size for **at least 30 closed
trades**, logging one row each:

`date_in, symbol, setup(A/B1/B2/C), regime_flags(4), cPOC, cVAH, cVAL, VW, ATR,
entry, stop, R_per_share, shares, T1/T2/T3, exit_reason(T1|T2|T3|stop|trail|time|event),
bars_held, MFE_R, MAE_R, realised_R`

**Judge it on:**
- **Expectancy in R** — must be > +0.15R/trade net of costs, or the plan is noise.
- **Hit rate by setup** — if B1 (momentum) trails B2 (retest) badly, drop B1.
- **MAE distribution** — if most winners never trade more than −0.5R against
  you, the stops are too wide and every R figure above is inflated.
- **Time-stop cost** — how many time-stopped trades would have worked with more
  patience? If most, lengthen from 8 bars; if few, keep it.
- **Regime attribution** — if trades taken in balanced markets (Setup C) lose
  while trend trades win, cut C entirely.

Kill any rule that doesn't earn its place. `RESEARCH.md` is the standard: most
plausible-sounding ideas in this repo failed their honest test, and this one has
not taken its test yet.

---

## Disclaimer

For research and education only. Not investment advice. These rules are
mechanical and unvalidated; confirm every level on your own chart and manage
your own risk.
