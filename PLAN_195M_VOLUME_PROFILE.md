# 195-Minute Swing Plan — POC / VAH / VAL

A fully mechanical swing plan for the **195-minute** chart, driven by volume
profile: **POC** (point of control), **VAH** (value area high), **VAL** (value
area low). Every rule below is testable — nothing says "if it looks strong."

`vp195.py` implements these rules exactly and replays them over real 195m bars.
**Read §10 before trading any of it**: run over 6 months of RGLD, the rules
produced 11 signals and a *negative* expectancy. This is a specification and a
research harness, not a validated edge.

---

## 1. Why 195 minutes

Regular-hours session = 390 minutes. **195m = exactly half a session, so you get
two bars a day**: `09:30–12:45` and `12:45–16:00` ET.

- **Two decision points per day**, at 12:45 and 16:00 ET. No intraday babysitting.
- Bar 1 captures the opening auction + morning trend; bar 2 the afternoon
  auction + close. Each bar is a genuine half-day auction, not an arbitrary slice.
- A 3–10 day swing = **6–20 bars**.

⚠️ **Check this first**: if your feed includes pre/post market, bars will *not*
split at 12:45 and every level below drifts. Set the chart to **RTH only** and
confirm you see exactly two bars per day. (On RGLD, RTH is 98% of volume — the
extended-hours rows add noise and shift every profile.)

---

## 2. The level tiers

| Tier | Lookback | Levels | Role |
|---|---|---|---|
| **Bar profile (BP)** | 1 bar | `bPOC`, `bVAH`, `bVAL` | Confirmation + trailing reference |
| **Swing value (SVA)** | **6 bars** (≈3 sessions) | `sPOC`, `sVAH`, `sVAL` | **Every trigger and stop keys off this** |
| **Composite value (CVA)** | 20 bars (≈2 weeks) | `cPOC`, `cVAH`, `cVAL` | Context, regime, and far targets |
| **Node map (NM)** | 60 bars (≈6 weeks) | HVNs / LVNs | Final target selection |

**Why triggers use the 6-bar swing area and not the 20-bar composite** — this is
the single most important correction the data forced. On RGLD the 20-bar
composite value area runs a **median 7.5% of price wide** (a $17 band on a $233
stock, up to 28% wide after a fast move). In a trend its VAL sits an entire leg
below price: across 232 scanned bars, a pullback in an uptrend *never once* got
within 47% of the value width of `cVAL`. A "buy the pullback to cVAL" rule
written against the composite can never fire. The 6-bar swing area is the value
the **current leg** actually built, and price interacts with it constantly.

The composite still earns its place — it sets the regime, and its far edge gives
the third target — but it is context, not a trigger.

**Working definitions:**

- **Swing width** `SW = sVAH − sVAL`; composite width `VW = cVAH − cVAL`.
- **Value area** = the rows holding **70%** of volume, built the standard way:
  start at the POC row and repeatedly annex whichever neighbouring row holds
  more volume until 70% is enclosed. *Not* a narrowest-window search — on a
  bimodal profile that can return a band which excludes the POC entirely (it did
  on RGLD in April, putting `cPOC` below `cVAL`).
- **Every composite and swing area excludes the current bar.** The level must be
  known *before* the bar that trades through it. Include the current bar and a
  breakout drags the level along with it, so price can never close outside its
  own value — the rule becomes untestable.
- **Acceptance** = **two consecutive closes** on the same side of a level, *and*
  the second bar's `bPOC` on that side. One close is a poke; a bar that closes
  through but leaves its POC behind has not accepted.
- **Value migration** = `cPOC(now) − cPOC(5 bars ago)`.

**HVN vs LVN:** an HVN is price the market agreed on — it attracts and stalls
price, so it's where you take money off. An LVN is price the market rejected —
it travels fast, so never place a target *inside* one; put it on the far side.

---

## 3. Regime gate (checked before every entry)

Computed at the close of each 195m bar.

**Long regime — all three true:**
1. Close > **SMA(20)** on 195m (≈10 sessions).
2. SMA(20) higher than it was **10 bars** ago.
3. **Value migrating up**: `cPOC(now) > cPOC(5 bars ago)`.

**Short regime — the exact mirror.** Neither → **neutral**.

Two calibration notes, both from the data rather than from taste:

- **SMA(20), not SMA(50).** A 50-bar SMA on 195m is 25 sessions; after RGLD's
  20% June–July decline it took weeks to turn up, and the gate admitted only
  **4.3%** of bars — that is a blackout, not a filter. SMA(20) admits 8.6%.
- **A fourth condition was dropped.** Requiring `cVAL(now) ≥ cVAL(5 bars ago)`
  on top of the other three changed the long-regime count by exactly zero. A
  condition that never binds is not a safeguard, it's decoration.

**Neutral is not the same as balanced.** A strict trend gate reports neutral all
the way up a strong advance. Use the explicit test: the market is **balanced**
when the current 20-bar CVA overlaps the CVA from 10 bars ago by **more than
75%** (`overlap = intersection / current width`). Setups A and B are barred in a
balanced market; Setup C *requires* one (§4.3).

---

## 4. The three setups

One position per symbol. Never hold Setup A and Setup B in the same name.

### 4.1 Setup A — swing-VAL reclaim (buy the pullback in an uptrend)

Buying a pullback into the bottom of the current leg's value that gets rejected.

**Conditions (all, at a 195m bar close):**
1. Long regime (§3).
2. Price traded **at or below `sVAL`** during this bar or the one before.
3. This bar **closes back above `sVAL`**.
4. This bar's `bPOC ≥ sVAL` (buyers, not just a wick).

**Entry:** buy-stop at **signal bar high + 0.05**, working the **next two bars
only**, then cancel.

**Stop:** `min(signal bar low, sVAL) − 0.25 × ATR`.

**Cancel before fill** if any bar **closes below `sVAL`**. The setup is dead —
wait for a fresh signal bar.

**Targets:** T1 `sPOC` (or `sVAH` if `sPOC` is already below entry) ·
T2 `sVAH + SW` · T3 `cVAH + VW`.

**Short mirror:** rejection at `sVAH` in a short regime.

### 4.2 Setup B — acceptance outside swing value (continuation)

The current leg's value has broken and been *accepted* outside.

**Conditions:** long regime · **acceptance above `sVAH`** (§2) · entry via
buy-stop at the acceptance bar's high + 0.05, valid 2 bars.

**Stop:** `sVAH − 0.5 × ATR`, or below the acceptance bar's `bPOC`, whichever is
**lower** — if price is back under the POC of the bar that broke out, the
breakout failed.

**Targets:** T1 `sVAH + 0.5 × SW` · T2 `sVAH + SW` · T3 the next HVN above from
the 60-bar node map.

**Failed-breakout override:** acceptance back *inside* swing value is a full exit
at that close, even if the stop hasn't been touched.

### 4.3 Setup C — the 80% rule (rotation)

The one counter-trend trade. When price is accepted back inside a value area it
had left, the auction failed and price tends to rotate to the far side.

**Conditions (long version):**
1. **The market is balanced** (>75% CVA overlap, §3) — *or* the regime agrees
   with the trade's direction. Not merely "the regime isn't against it."
2. Price closed **below `sVAL`** within the last 3 bars.
3. Two consecutive closes **back inside** swing value, second bar's `bPOC` inside.

**Entry:** market, at the next bar's open. **Stop:** `sVAL − 0.5 × ATR`.
**Target:** single — `sVAH`. Full exit, no runner. **Size:** half risk in a
balanced market.

> Condition 1 is the fix for the worst failure in testing. Written as "regime
> ≤ 0", Setup C shorted RGLD at 215, at 214, and at 252 during the strongest
> advance of the year, because a strict trend gate called that advance
> *neutral*. Requiring a genuinely balanced market cut the plan's overall
> expectancy loss by roughly two-thirds. It is still the weakest of the three.

---

## 5. Exits — the ladder

R = |entry − initial stop| per share.

| Leg | Action | Then |
|---|---|---|
| **T1** | Sell 1/3 | Stop on the rest to **breakeven** |
| **T2** | Sell 1/3 | Last third goes to the trail |
| **T3** | Sell the last 1/3 at target or on the trail | Flat |

**The trail:** after T2, exit the last third on the **first 195m close below the
prior bar's `bVAL`** (long) / above prior `bVAH` (short). Nothing discretionary.

**Minimum payoff — on the primary target, not T1.** The trade must offer at
least **1.5R at T2** (or at T1 on a single-target Setup C). T1 carries no
minimum: it is a de-risking partial, not the payoff.

> The first draft of this plan required 1.0R to *T1* and that single line vetoed
> **~90% of all signals**. The reason is structural and worth internalising: on
> a 195m chart a single bar's range is about the same size as the distance to
> the next structural level, so a stop placed beyond the signal bar's low is
> roughly as far away as T1 is. Demanding 1R to the first scale-out is
> arithmetically close to impossible.

**Hard stop-out:** stop hit = full exit, −1R. No averaging down, no widening.

**Time stop:** after **8 bars** (4 sessions), if T1 hasn't been reached and the
position is under **+0.5R**, exit at market on the next bar close.

**Event exit:** flatten before any earnings print.

---

## 6. Filters that veto a trade

Any single hit = no trade.

- ❌ **Earnings** inside the hold: no entry within **6 bars (3 sessions)** of a
  confirmed date; flatten before the print.
- ❌ **Payoff < 1.5R** at the primary target (§5).
- ❌ **Stop > 2.0 × ATR** — structure isn't clean enough.
- ❌ **Climax signal bar**: range > 2.5 × ATR.
- ❌ **Thin profile**: signal bar volume < 60% of the 20-bar average. A profile
  built on no volume is not information.
- ❌ **Balanced market** for Setups A and B; **trending market** for Setup C.
- ❌ **Gap beyond value**: session opens more than 1.5 × ATR outside value — wait
  for the first 195m bar to complete and re-derive levels. Never chase the open.
- ❌ **Correlation stack**: already holding 2 positions in one complex (RGLD +
  NEM + GDX is one bet on gold, not three).

---

## 7. Position sizing

```
shares = floor( equity × risk_pct / (entry − stop) )
```

- **risk_pct = 0.5%** default, **1.0%** maximum and only with the regime fully
  aligned and §6 clean.
- **Max 3 concurrent positions**; max 2 in correlated names.
- **Portfolio heat cap 3.0%** — sum of open risk at current stops (breakeven
  stops count as zero). At the cap, no new entries.
- **Drawdown brake:** after 3 consecutive −1R losses, halve risk_pct until the
  next winning trade closes.

---

## 8. The daily routine

At the 195m closes — **12:45 ET** and **16:00 ET**:

1. **Update levels** — `python vp195.py bars.csv --levels 8` prints the bar,
   swing, and composite tiers plus ATR and the regime flag.
2. **Manage what's open first** — T1/T2/T3, trail, 8-bar time stop,
   failed-breakout override, earnings inside 6 bars.
3. **Re-check the regime gate** per symbol.
4. **Scan for signal bars** — A, then B, then C.
5. **Run the veto list** (§6).
6. **Size it** (§7), place the order with its stop as one bracket, log it (§10).

Orders go in **at the bar close** with the stop attached. A position without a
resting stop is not part of this plan.

---

## 9. Worked example — a real signal

RGLD, **2026-08-14 morning bar** (09:30–12:45 ET). Setup A, produced by
`vp195.py` from 1-minute bars, not by hand.

| | Value |
|---|---|
| Signal bar | o 228.29 · h 233.56 · **l 226.44** · c 230.50 · `bPOC` 230.88 · vol 187,589 |
| Swing value (6 bars) | `sVAL` **228.75** · `sPOC` 233.38 · `sVAH` 238.25 · SW 9.50 |
| Composite (20 bars) | `cVAL` 214.00 · `cPOC` 233.62 · `cVAH` 238.25 · VW 24.25 |
| ATR(14) | 4.64 |

The bar traded to 226.44, **below `sVAL` 228.75**, then closed at 230.50 back
above it, with `bPOC` 230.88 above it too. Long regime confirmed. Condition met.

| | Level | Working |
|---|---|---|
| Entry (buy-stop) | **233.61** | signal bar high 233.56 + 0.05 |
| Initial stop | **225.28** | min(226.44, 228.75) − 0.25 × 4.64 |
| **R** | **8.33** | 1.79 × ATR — under the 2.0 cap ✓ |
| T1 | 238.25 | `sVAH` (0.56R) |
| T2 | **247.75** | `sVAH + SW` — **1.70R**, clears the 1.5R payoff filter ✓ |
| T3 | 262.50 | `cVAH + VW` (3.47R) |
| Size | **60 sh** | (100,000 × 0.005) / 8.33 |
| Risk | **$500** | |

**What happened:** filled 233.61 on the 08-17 morning bar. T1 and T2 both hit on
08-19 (the 241→249 gap), T3 on 08-24 at 262.50. **+1.91R over 10 bars**, MFE
+3.53R, MAE −0.45R. That is the August advance visible on the right of the chart.

**And a signal the rules refused.** At the chart's right edge — 2026-08-25,
close 267.76 — Setup B fires long: acceptance above `sVAH` 263.25. But the stop
(`sVAH − 0.5 ATR` = 260.43) gives R = 7.43, while T2 (`sVAH + SW` = 270.25) is
only **0.32R** away. Payoff 0.32R against a 1.5R minimum → **no trade**. Price
is extended far above the value it came from; the structure to lean on is 7
points below while the next objective is 2 points above. The veto list exists
for exactly that geometry.

---

## 10. What happened when this was actually run

`vp195.py` replayed every rule over **RGLD, 2026-02-02 → 2026-08-25**, 284
bars of 195m built from 1-minute bars (volume bucketed at each minute's VWAP,
$0.25 rows, RTH only). 252 bars scanned after warmup.

| | |
|---|---|
| Signals passing every filter | **11** (59 vetoed) |
| Triggered | 9 |
| Expectancy | **−0.12R** per trade |
| Hit rate | 44% |
| Setup A | n=2, +0.91R avg |
| Setup B | n=3, −0.11R avg |
| Setup C | n=4, −0.63R avg |

**Read that honestly.** Nine trades on one symbol over six months is not a
backtest — it cannot distinguish a −0.12R edge from a +0.3R one, and the window
is dominated by a single 20% decline followed by a single 40% rally. What the
run *does* establish, because these are structural rather than statistical
findings, is the four corrections already folded into the rules above:

1. Triggers cannot key off the 20-bar composite — its VAL is a whole leg below
   price in a trend, so Setup A fired **zero** times in 232 bars (§2).
2. A 1.0R-to-T1 filter vetoes ~90% of signals for arithmetic reasons (§5).
3. An SMA(50) trend gate admits 4% of bars — a blackout, not a filter (§3).
4. "Regime not against me" is not "balanced", and conflating them made Setup C
   short a runaway uptrend three times (§4.3).

**Before risking real size**, paper or minimum-size **at least 30 closed
trades across several symbols**, logging:

`date_in, symbol, setup, regime, sVAL/sPOC/sVAH, cVAL/cPOC/cVAH, SW, ATR, entry,
stop, R_per_share, shares, T1/T2/T3, exit_reason, bars_held, MFE_R, MAE_R, realised_R`

Judge it on **expectancy in R** (> +0.15R net of costs or it's noise), **hit
rate by setup** (Setup C is the one to cut first), **MAE distribution** (if
winners rarely trade more than −0.5R against you, the stops are too wide and
every R figure here is flattering), and **time-stop cost**.

Kill any rule that doesn't earn its place. `RESEARCH.md` is the standard: most
plausible-sounding ideas in this repo failed their honest test.

---

## Running it

```bash
python vp195.py bars.csv                 # scan, list signals + replayed outcomes
python vp195.py bars.csv --levels 8      # current level tiers + regime flag
python vp195.py bars.csv --show-vetoed   # every rejected signal and why
python vp195.py bars.csv --equity 50000 --risk 0.01
python vp195.py bars.csv --composite 14  # sensitivity-test the composite window
```

Input is one row per 195m bar: `bar,o,h,l,c,vol,prof`, where `bar` is
`YYYY-MM-DD|0` / `|1` for the two half-sessions and `prof` is the bar's profile
as `bucket:volume` pairs (bucket index × 0.25 = the row's low edge). Build it
from 1-minute bars by bucketing each minute's volume at its VWAP.

## Disclaimer

For research and education only. Not investment advice. These rules are
mechanical and **unvalidated** — §10 is the whole of the evidence. Confirm every
level on your own chart and manage your own risk.
