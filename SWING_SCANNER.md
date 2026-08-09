# Single-Name Swing Scanner

Unusual Whales (options flow + dark pool) → Massive (data + ranking) → TradingView (confirmation).

Liquid U.S. single names only. Holding period 3–15 sessions. Every rule below is
mechanical, defined in code, and covered by tests (`python test_swing.py`, 46 tests).

> This is a decision-support tool, not a signal service. It ranks what the tape is
> doing; you still take the risk. Nothing here is investment advice.

| File | Role |
|---|---|
| `swing_config.py` | Every threshold and weight. Change the strategy here, not in the logic. |
| `swing_flow.py` | Normalises + aggregates UW flow alerts and dark-pool prints per ticker. |
| `swing_indicators.py` | EMA / RSI / ATR / volume, defined to match TradingView exactly. |
| `swing_score.py` | Hard gates, the 0–100 composite, tiering, risk levels, sizing. |
| `swing_scan.py` | CLI that runs the whole pipeline and prints/exports the ranked list. |
| `massive_client.py` | Massive REST client + offline reader for MCP exports. |
| `uw_client.py` | Unusual Whales REST client (shared with the rotation strategy). |
| `pine/swing_confirm.pine` | Pine v5 chart checklist + risk levels. |
| `test_swing.py` | Offline self-tests. No keys, no network. |

---

## 1. Scan logic and scoring framework

### 1.1 The thesis in one line

Someone with size is paying up for 14–90 day optionality in a liquid name, the
share tape agrees, and the chart is already trending in that direction. You are
not predicting — you are joining a position that is already being built, with a
stop where the thesis breaks.

### 1.2 Three-layer funnel

```
Layer 1  UNIVERSE + FLOW GATES   ~thousands of alerts -> tens of tickers
Layer 2  SCORE 0-100             rank what survived
Layer 3  RISK GATE               drop anything that can't pay 2R
```

### 1.3 Hard gates (binary — fail = dropped, with a stated reason)

Gates are deliberately kept **separate from the score** so a name can never
partially fail its way onto your screen.

**Universe** (`swing_config.UNIVERSE`)

| Rule | Value | Why |
|---|---|---|
| Issue type | Common Stock, ADR | No ETFs/index — those are the rotation strategy's job |
| Price | $10 – $2,000 | Sub-$10 names have unusable option structure |
| Market cap | ≥ $2B | Keeps the tape orderly |
| 30-day ADV | ≥ 1,000,000 shares | You need to get out too |
| Dollar volume | ≥ $25M/day | The real liquidity test |
| Option spread | ≤ 15% of mid | A wide chain eats the edge before you start |
| **Price history** | **≥ 250 bars** | A 200 EMA seeded at bar 200 *is* the 200 SMA; 250 gives a quarter of smoothing past the seed. Without it the trend-conflict veto silently skips |
| **ATR (dollars)** | **≤ $4.00** | Small-account options screen — see below |
| **Price** | **≤ $150** | Small-account options screen — see below |

The last two are **account-size policy, not setup quality**, and apply only when
sizing with defined-risk options (`swing_options.py`). At the $250 concentration
budget the narrowest worthwhile vertical is 0.5 expected moves wide and costs
~40% of its width, so `required ≈ 104 × ATR` — $2.40 of ATR is *guaranteed*
affordable, and up to ~$5.40 works when the 2R cap binds tighter. $4.00 sits
between: a realistic chance, not a certainty. Set both to `None` in
`UNIVERSE` on a larger account and the screen becomes a no-op.

**Flow** (`swing_config.FLOW`)

| Rule | Value |
|---|---|
| Single-alert premium | ≥ **$250,000** |
| Ticker total premium | ≥ **$750,000** across qualifying alerts |
| DTE | **14–90** (no penalty inside 21–60) |
| Multi-leg | Excluded — spreads muddy the directional read |
| Alert age | ≤ 3 days |
| Side | Must be resolvable as call or put |

**Direction / earnings**

- **Direction is set by option type *and* the side of the spread**, because
  buying and selling the same contract are opposite bets:

  | Trade | Reading |
  |---|---|
  | Ask-side call | bullish (paying up for upside) |
  | Bid-side call | bearish (writing/selling calls) |
  | Ask-side put | bearish (paying up for downside) |
  | Bid-side put | **bullish** (writing/selling puts) |

  Premium that printed between the quotes can't be attributed by side, so it
  falls back to option type and is reported as `unsided_share`; above 50% the
  candidate carries a `direction inferred` flag.
- **Aggression is direction-relative.** It measures the share of the premium
  betting the candidate's own way that *crossed the spread* rather than
  printing mid-market — `ask-side calls + bid-side puts` for a long, the mirror
  for a short. Long and short books of equal quality therefore score equally;
  under the earlier ask-side-only definition every short built from sold calls
  scored zero aggression by construction. The denominator is same-direction
  premium, not the whole book, so this stays independent of `coherence` and the
  flow score doesn't count directional agreement twice.
- **Trend conflict veto:** bullish flow is rejected if price is below the 200 EMA;
  bearish flow rejected if above it. You never take the flow's side against the
  primary trend.
- **Earnings veto:** rejected if earnings fall within 12 days (the hold window).
  A swing through earnings is a binary event bet, not a swing trade.
  Override with `--allow-earnings`; the name then carries a visible flag.

### 1.4 The 100-point composite

| Component | Max | What it measures |
|---|---|---|
| **Flow conviction** | 40 | Is the options bet big, aggressive, new, and one-sided? |
| **Dark-pool confirmation** | 15 | Is someone also accumulating the shares? |
| **Technical confluence** | 35 | Does the chart already agree? |
| **Liquidity / tradeability** | 10 | Can you actually get in and out? |

**Flow conviction (40)**

| Sub-score | Max | Rule |
|---|---|---|
| Premium magnitude | 14 | Log-scaled $250k → $10M. $1M ≈ 6.2, $5M ≈ 11.9 |
| Aggression | 8 | Share of *same-direction* premium that crossed the spread. 0.55 → 0 pts, 0.90+ → full |
| Structure | 6 | Sweep-dominated +3, floor prints +2, plus the rule-name bonus |
| Opening | 6 | ≥50% all-opening +4, volume > OI +2 |
| Coherence | 6 | One-sided **bullish-vs-bearish** premium, worked across ≥2 strikes and ≥2 days |

Rule-name bonuses (UW's alert engine, highest signal first):
`SweepsFollowedByFloor` +5, `RepeatedHitsAscendingFill` +4, `RepeatedHits` +3,
`FloorTradeLargeCap`/`MidCap` +3, `VolumeOverOi` +2, `LowHistoricVolumeFloor` +2.

A DTE haircut applies outside 21–60 days (×0.90 short-dated, ×0.95 long-dated).

**Dark-pool confirmation (15)**

| Sub-score | Max | Rule |
|---|---|---|
| Footprint | 7 | 5-day block shares as % of ADV: 2% → 0, 10%+ → full |
| Lean | 5 | Premium-weighted price vs NBBO mid, signed to the trade direction |
| Single block | 3 | One print ≥ $10M |

**Absence and contradiction score differently — three states, not two.**

| Dark-pool state | Score | Flag |
|---|---|---|
| No blocks | 0 | `no dark-pool confirmation` |
| Blocks aligned with the flow | up to +15 | note names the accumulation/distribution |
| Blocks leaning **against** the flow | down to **−5** | `dark pool opposing (lean …)` |

A quiet off-exchange tape is absence of evidence, not evidence of distribution,
so it stays at 0. Blocks moving *against* the options bet are evidence, so the
lean term is symmetric in [−5, +5] and a contradicted name ranks **below** an
unconfirmed one. The penalty is proportional and deliberately not a veto: a
strong footprint (7) plus a big single block (3) can still carry a fully opposed
lean to a net +5.

**Technical confluence (35)** — long-side; mirrored for shorts

| Sub-score | Max | Rule |
|---|---|---|
| Trend structure | 12 | price > 50 EMA > 200 EMA = full; above 200 only = 6 / 4.2. ×0.75 if the 50 EMA slopes against the trade |
| Extension | 5 | Distance from the 50 EMA in ATRs. Pulled back = 2.5, >4 ATR extended = 0 |
| RSI behaviour | 8 | 45–70 = full; 40–45 recovering = 4.8; >75 overbought = 2 (chase risk); +1 if RSI is moving your way over 5 bars |
| Volume | 6 | ≥1.3× the 20-day average = full; 1.0–1.3× = 3 |
| Structure | 4 | 20-day breakout +2.5, within 10% of the 52-week high +1.5 |

**Liquidity (10)** — log-scaled dollar volume (6), option spread tightness (4),
×0.8 if ATR < 1% of price (a name that doesn't move can't pay a swing).

### 1.5 Tiers and what you do with them

| Tier | Score | Action |
|---|---|---|
| **A** | ≥ 78 | Actionable. Full planned risk (0.75% of account). |
| **B** | 65–77 | Actionable at **half** risk. |
| **C** | 55–64 | Watchlist only. No size until it upgrades. |
| — | < 55 | Dropped. |

### 1.6 Risk model

- **Entry:** last close (your actual trigger is the chart confirmation in §4).
- **Stop:** the *tighter* of 1.5 × ATR(14) and 0.5 ATR beyond the 50 EMA — the
  EMA is where the thesis actually breaks, so if it's closer, use it.
- **Targets:** 2R and 3R off the entry–stop distance.
- **Size:** fixed fractional. `shares = (account × risk%) ÷ (entry − stop)`.
- **Portfolio caps:** max 5 open positions, max 2 per sector (flagged, not
  auto-dropped — you decide which of the two you want).
- Anything that can't pay the configured minimum 2R is rejected outright.

---

## 2. Exact Unusual Whales filter settings

### 2.1 Options flow — saved filter "Swing Flow 14-90"

Page: **Option Flow Alerts** (`/option-flow-alerts`).
Endpoint: `GET /api/option-trades/flow-alerts`.

| Field | Value |
|---|---|
| `min_premium` | `250000` |
| `min_dte` | `14` |
| `max_dte` | `90` |
| `min_volume_oi_ratio` | `1` |
| `min_volume` | `500` |
| `issue_types[]` | `Common Stock`, `ADR` |
| `min_price` | `10` |
| `max_price` | `2000` |
| `min_marketcap` | `2000000000` |
| `is_multi_leg` | `false` |
| `limit` | `500` |

Deep link (paste into the browser, then **Save filter**):

```
https://unusualwhales.com/option-flow-alerts?min_premium=250000&min_dte=14&max_dte=90
&min_volume_oi_ratio=1&min_volume=500&issue_types[]=Common%20Stock&issue_types[]=ADR
&min_price=10&max_price=2000&min_marketcap=2000000000&is_multi_leg=false&limit=500
```

**Two tightening presets** to run when the base filter returns too much:

| Preset | Extra fields | Use when |
|---|---|---|
| *Aggressive opening* | `all_opening=true`, `min_ask_perc=0.6` | You want only new, ask-lifted positions |
| *Sweeps only* | `is_sweep=true` | Choppy tape — you want urgency, not patience |

CLI equivalents: `--opening-only`, `--sweeps-only`.

**Rule-name filter.** Leave `rule_name[]` empty in the saved filter so the scanner
sees everything and applies its own bonuses. If you want a manual high-conviction
view on the site, set `rule_name[]` to:
`SweepsFollowedByFloor`, `RepeatedHitsAscendingFill`, `RepeatedHits`,
`FloorTradeLargeCap`, `FloorTradeMidCap`.

Explicitly **not** used: `OtmEarningsFloor` (earnings-driven — vetoed by the
earnings gate anyway).

### 2.2 Dark pool — saved filter "Swing Blocks"

Page: **Large Trades → Dark Pool** (`/large-trades?tab=dark-pool`).
Endpoint: `GET /api/darkpool/{ticker}` (per candidate).

| Field | Value |
|---|---|
| `min_premium` | `1000000` |
| `newer_than` | today − 5 days |
| `limit` | `200` |
| `issue_type[]` | `Common Stock`, `ADR` |
| `hide_index_etf` | `true` |
| `order` | `prem` |

For the market-wide browse view, add `min_size_avg30d_vol_perc = 0.02` to surface
only blocks that are ≥2% of the name's 30-day average volume — that's the same
footprint threshold the scanner scores on.

### 2.3 Optional context — stock screener

Page: `/stock-screener`. Useful as a sanity cross-check, not part of the score:
`min_marketcap=2000000000`, `min_stock_volume=1000000`,
`issue_types[]=Common Stock,ADR`, `order=net_call_premium`, `order_direction=desc`.

### 2.4 Live alerts (optional)

Set up a UW custom alert on the same criteria so intraday prints reach you
between scans. The WebSocket channel `flow-alerts` streams the same rule engine
if you'd rather consume it programmatically.

---

## 3. Python script (Massive-focused)

Zero third-party dependencies — standard library only, matching the rest of the repo.

### 3.1 Setup

```bash
cp .env.example .env      # add UW_TOKEN and MASSIVE_API_KEY
python test_swing.py      # 46 offline tests, no keys needed
```

### 3.2 Running it

```bash
python swing_scan.py --dry-run                     # show the exact API queries, no calls
python swing_scan.py                               # full scan
python swing_scan.py --direction long --top 10     # longs only
python swing_scan.py --sweeps-only --opening-only  # highest-conviction preset
python swing_scan.py --show-rejects                # why each name was dropped
python swing_scan.py --csv swing.csv --watchlist swing.txt
python swing_scan.py --data-dir data               # offline bars from MCP exports
```

### 3.3 Pipeline

```
[1/4] UW    /api/option-trades/flow-alerts   -> normalise -> per-alert gates -> per-ticker roll-up
[2/4] Massive /v2/aggs/.../range/1/day/...   -> 400 daily bars -> EMA/RSI/ATR/volume snapshot
[3/4] UW    /api/darkpool/{ticker}           -> 5-day blocks -> footprint + lean
[4/4] swing_score                            -> gates -> 0-100 -> tier -> entry/stop/targets/size
```

Massive is the ranking engine for the price half: it supplies adjusted daily
OHLCV, and `swing_indicators.py` derives every technical field from it. The
`--data-dir` path reads the CSV/JSON that Massive's MCP `call_api` tool writes,
so you can drive the whole scan from MCP output without a REST key.

### 3.4 Output

Console: ranked table (score, component breakdown, premium, DTE, entry/stop/T1/T2,
share size) plus a per-name "why it scored" block listing the specific reasons and
any warning flags.

```
 #  TKR   Dir   Tier  Score  Flow   DP  Tech  Liq    Premium  DTE    Entry     Stop       T1       T2    Sh
 1  AAAA  long  A      79.5  36.5  8.6  25.5  8.9      $4.0M   40   156.57   151.77   166.17   170.97   156

    AAAA (long, tier A, 79.5) — Technology
      + 100% aggressive fills; sweep-dominated; opening activity; 2 alerts / 2 strikes;
        multi-day accumulation; blocks above mid (accumulation); price > 50 EMA > 200 EMA;
        volume 1.7x average; 1% from 52w high
      risk $4.80/share  (3.1%)  budget $750
```

`--csv` writes 34 columns (every score component and raw input, so you can audit
or backtest the ranking). `--watchlist` writes a TradingView-importable file with
`###SWING A/B/C` section headers.

### 3.5 Tuning

Everything lives in `swing_config.py`: `UNIVERSE`, `FLOW`, `DARKPOOL`,
`TECHNICAL`, `WEIGHTS`, `TIERS`, `RISK`, `EARNINGS`. Set `RISK["account_size"]`
to your actual account before trusting the share counts. Re-run `test_swing.py`
after any change — the tests assert the relationships (more premium scores
higher, two-way flow scores lower, stops sit on the right side of entry).

---

## 4. TradingView support

### 4.1 Watchlist workflow

1. Run with `--watchlist swing.txt`.
2. TradingView → Watchlist panel → **⋯ → Import list…** → pick `swing.txt`.
3. You get sections `SWING A`, `SWING B`, `SWING C`.
4. Keep three permanent lists: **Active** (open positions), **Triggered**
   (A/B awaiting entry), **Watch** (C tier). Move names between them by hand —
   the friction is the point; it forces a decision per name.

### 4.2 `pine/swing_confirm.pine` (Pine v5)

Paste into Pine Editor → Add to a **daily** chart of any candidate.

It re-computes the scanner's technical rules on the chart with the same
definitions (`ta.ema`, `ta.rsi`, `ta.atr`) and shows:

- A **checklist table**: trend 50/200, EMA slope, extension in ATRs, RSI band,
  volume ratio, structure — each ✔/✘ with its live value.
- **Chart score /35** — should match the scanner's `Tech` column. If it doesn't,
  your chart settings differ (check the session/adjustment settings).
- Typing the scanner's `Flow` + `DP` points into the **"Flow score from scanner"**
  input gives you the full composite and tier on the chart.
- **Entry / stop / T1 / T2** plotted with the same ATR-and-EMA stop logic.
- A background tint on the bar where *every* rule aligns.

Set **Trade direction** to match the scanner's `Dir` column — all rules mirror
for shorts.

**Alerts included:**

| Alert | Fires when |
|---|---|
| Swing setup confirmed | First bar all rules align — use this on C-tier names so you're told when the chart catches up to the flow |
| Long/Short stop breached | Close beyond your stop |
| Target 1 reached | 2R hit — scale |

---

## 5. Daily operating checklist

**Pre-market — 15 minutes (8:30–9:00 ET)**

1. `python swing_scan.py --csv swing.csv --watchlist swing.txt`
2. Read the "why it scored" block for every A and B. Reject anything where the
   reasons are thin even if the number is high — the score is a ranking device,
   not a verdict.
3. Check the flags: `no dark-pool confirmation`, `two-way flow`, `earnings in Nd`,
   `over sector cap`. Two or more flags on one name = demote it.
4. Import `swing.txt` into TradingView.

**Open — do nothing (9:30–10:00 ET)**

5. No entries in the first 30 minutes. The opening auction is noise and it will
   take you out of a good idea at a bad price.

**Entry window (10:00–15:30 ET)**

6. For each A/B name, open the daily chart with `swing_confirm.pine`.
   Confirm the checklist is green — you are verifying, not re-deciding.
7. Enter only on a **confirmation trigger**: price holding above the prior day's
   high (long) / below the prior day's low (short), on volume ≥ the 20-day average.
   No trigger, no trade — it stays on the list for tomorrow.
8. Place the stop from the scanner output *at the same time as the entry*. Never
   "watch it" instead.
9. Size from the `Sh` column, adjusted to your real account size.

**Position management**

10. Scale 1/3–1/2 at **T1 (2R)**, move the stop to breakeven.
11. Trail the rest under the 50 EMA (or your prior swing low) toward **T2 (3R)**.
12. **Time stop:** flat after 15 sessions regardless. The flow thesis had a
    14–90 DTE horizon; if it hasn't worked in three weeks, it isn't working.
13. Exit early if the dark-pool lean flips against you or the next scan drops the
    name below 55.

**End of day — 5 minutes**

14. Re-run the scan. Note any open position that no longer scores.
15. Log every entry and exit: ticker, tier, score, entry, stop, exit, R multiple,
    and the *reason* — reuse `whale_log.py`'s journal pattern.

**Weekly (Friday, 20 minutes)**

16. Review the log by tier. If B-tier is outperforming A-tier over 20+ trades,
    your weights are wrong — adjust `swing_config.WEIGHTS` and re-run the tests.
17. Check hit rate by component: are dark-pool-confirmed names actually better?
    That's the question the CSV columns exist to answer.

### Non-negotiables

- Max 5 open positions, max 2 per sector.
- No entry without a stop already placed.
- No trades through earnings unless you deliberately override *and* halve size.
- A day with no qualifying candidate is a normal day. The scanner printing
  nothing is the system working, not failing.
