# Phase 2 — live shadow findings (TSLA, 2026-08-12)

Status: **partially complete.** The data-side questions are answered from a live
session. The REST-transport questions are **not** answered and remain blocked.

Read this together with `shadow_runs/README.md`, which records why the capture
ran on the MCP plane instead of REST.

---

## 1. What actually ran

| | |
|---|---|
| Plane | MCP (Claude "Unusual Whales" connector), **not** REST |
| Window | 2026-08-12, 17:40Z → 20:02Z (2h 22m), regular session + post-close |
| Flow observations | **15** (T5 → T19), ~10 min spacing, 201 strikes each |
| GEX staticness probes | 2 (17:41Z, 19:47Z) |
| Overnight OI context | 1 (startup) |
| Spot over window | ~$325.89 (17:36Z) → **$327.25** (19:43Z), +0.4% |

Labels continue Phase 0's numbering because this is the **same trading session**
(Phase 0 ran T1–T4, 15:24→16:42Z).

Raw payloads for every observation are retained under `shadow_runs/raw/`.

## 2. The blocker, stated precisely

The Phase-2 REST shadow did **not** run. The handoff recorded the blocker as a
*present but invalid* 36-char token. In this environment it is stronger than
that, and it is two independent blockers:

1. **No credential at all.** `UW_API_KEY`, `UW_TOKEN`, `UNUSUAL_WHALES_TOKEN`
   and `UNUSUAL_WHALES_API_KEY` are all unset; `find / -name .env` returns
   nothing.
2. **No code.** `uw_evidence.py`, `uw_poller.py`, `run_shadow.py` and the
   fixtures exist only on the authoring machine, not in this repository.

This container is remote Linux with no access to the authoring Windows machine —
no `/mnt/c`, no OneDrive mount — so neither blocker can be cleared from here.

The account's API subscription is **not** in question (an active key exists in
the UW dashboard, created 2026-07-17, 30k requests/day). This is purely
credential *availability* in the runtime environment.

## 3. Findings

### 3.1 `flow_per_strike` is genuinely live — CONFIRMED, stronger than Phase 0

Every observation carried per-row timestamps to the sub-second, and the maximum
source timestamp tracked each pull:

| Obs | source_ts_max | Obs | source_ts_max |
|---|---|---|---|
| T5 | 17:40:00.755Z | T12 | 18:57:12.821Z |
| T6 | 17:54:00.812Z | T13 | 19:07:16.563Z |
| T7 | 18:06:48.780Z | T14 | 19:17:20.716Z |
| T8 | 18:16:52.697Z | T15 | 19:27:24.815Z |
| T9 | 18:26:56.820Z | T16 | 19:37:28.682Z |
| T10 | 18:37:00.582Z | T17 | 19:46:36.745Z |
| T11 | 18:47:04.803Z | | |

Consecutive source stamps advance by ~10 minutes, matching the pull cadence with
no drift or plateau — the signature of a live aggregate, not a cached snapshot.

**Effective freshness: seconds, not minutes.** Observed lag between pull and
`source_ts_max` ranged from sub-second to ~60s across the run, tightening after
the first two observations. *Caveat:* pull wall-clock time was not written into
each record, so these lag figures were read at capture time and are approximate.
The source timestamps themselves are exact and recorded. **Recommend adding a
`pull_ts` field** so lag becomes an exact stored quantity rather than an
observation.

### 3.2 GEX is static within the session — CONFIRMED over a 2h 06m baseline

The tracked-strike GEX payload at 17:41Z and at 19:47Z is **byte-identical**
(sha256 `cbbd57f456f02709…` both times), across all seven tracked strikes and
all of gamma, delta, charm and vanna.

What makes this a materially stronger test than Phase 0's 78-minute window: over
those same 2h 06m, spot moved **+0.4%** and tracked-strike call volume grew by
**tens of thousands of contracts per strike**. GEX did not move by one digit.

This independently re-confirms, on a longer baseline, that intraday "GEX
migration" is a misnomer. GEX remains **DAILY-STRUCTURAL** and is never built
into a series here.

*Scope note:* this probe compares the seven tracked strikes, not the full
190-strike payload. Phase 0 separately established byte-identity across the full
payload for T1–T4; those fixtures are local and were not re-verified here.

### 3.3 Open interest is overnight — CONFIRMED independently

The OI screener returns `last_date` **2026-08-11** → `curr_date` **2026-08-12**:
a prior-day/post-OCC comparison with no intraday component. The
**OVERNIGHT-CONFIRMED** label holds exactly as recorded.

### 3.4 Intraday FLOW pressure migration is real and measurable

Net ask-minus-bid call premium per tracked strike, T5 (17:40Z) → T17 (19:47Z):

| Strike | T5 | T17 | Δ | call vol Δ |
|---|---|---|---|---|
| 325 | +2.22M | **+4.30M** | +2.08M | +30,838 |
| 327.5 | +2.37M | +3.54M | +1.16M | +77,795 |
| 330 | −2.57M | −2.96M | −0.38M | +46,276 |
| 332.5 | −0.31M | +0.32M | +0.63M | +12,287 |
| 335 | −0.69M | +0.23M | +0.92M | +14,422 |
| 337.5 | −0.57M | −0.59M | −0.03M | +3,066 |
| 340 | −3.71M | −3.08M | +0.63M | +8,201 |

Three strikes (332.5, 335, 340) changed sign or direction mid-window, with the
transition clustering around T10 (18:37Z). This is FLOW pressure only. It
carries **no** gamma interpretation and no verdict.

### 3.5 `flow_per_strike` ask/bid components update NON-ATOMICALLY — new, and it matters most

The single most operationally important finding of the run, caught only because
the capture continued past the close.

At T18 (19:58Z) strike 340 showed net ask-minus-bid of **−0.70M**, against
−3.08M ten minutes earlier. Taken alone that reads as the largest single-interval
move of the session. It was not a move. It was an artifact, and it reverted:

| Obs | ask_side | bid_side | net | call_vol |
|---|---|---|---|---|
| T17 19:47 | 5.71M | 8.78M | −3.08M | 68,730 |
| T18 19:58 | **8.17M** (+2.46) | 8.87M (+0.09) | **−0.70M** | 70,216 |
| T19 20:02 | 8.18M (+0.01) | **11.31M** (+2.44) | −3.13M | 70,856 |

The ask-side counter took on ~+2.46M while the bid-side counter had not yet
moved; by the next pull the bid side took on a near-identical ~+2.44M and net
returned to its prior level. **The two sides of a single strike are not
guaranteed mutually consistent at read time.**

Why this is serious: `flow_pressure_migration` is computed from exactly these two
fields. A single observation can therefore produce a large, entirely spurious
pressure excursion. Nothing about the payload flags the inconsistency — the row
carries a normal timestamp and a plausible volume.

**Implication for the evidence layer.** A single-observation net reading is not
trustworthy on its own. Any excursion should be corroborated against the
following observation before it is treated as real, and a same-magnitude,
opposite-side revert on the next pull should be classified as a partial update
rather than flow. This is a detection rule, not an interpretation rule — it adds
no verdict.

This also argues the finding generalises beyond the close: nothing observed ties
the behaviour specifically to end-of-session, it is simply where a large enough
print made it visible.

## 4. What is still open

These require a valid REST token **and** the poller, on a machine that has both.
Nothing in this capture substitutes for them:

- polling reliability of `uw_poller.py` under a real run
- actual REST request rate against the 30k/day allowance
- throttling behavior: 429 handling, `Retry-After` honouring, rate-limit headers
- **best cadence (30 / 45 / 60 s)** — genuinely unanswerable here, because MCP
  calls were driven at ~10 min spacing, two orders of magnitude coarser

One observation that *does* transfer: since `flow_per_strike` is fresh to within
seconds and the day-aggregate advances continuously, a 30–60s cadence is not
oversampling. The payload is ~116 KB per pull, so at 45s a full session is
roughly 520 pulls / ~60 MB — well inside a 30k/day allowance. That bounds the
cost question but does not settle cadence, which needs the live throttling data.

## 5. Recommendation

1. Restore `UW_API_KEY` on the authoring machine and confirm a single request
   returns HTTP 200 — **without** starting the shadow.
2. Then run `run_shadow.py --cadence 45 --minutes 12` in a live session and
   complete section 4 from the telemetry.
3. Add `pull_ts` to observation records first (§3.1) so freshness is stored
   rather than observed.
4. Add the §3.5 corroboration rule before any pressure excursion is surfaced
   anywhere. On the evidence of this run, a single-pull net reading can be wrong
   by ~2.4M on one strike. At a 45s cadence the partial-update window will be
   crossed *more* often than at 10 min, not less — so this needs handling before
   the REST shadow, not after.
5. Phase 3 wiring stays **not approved** until a clean live REST shadow exists.

## 6. Rules honoured

- Production Live Participation Monitor **untouched**; nothing imports it.
- Equity participation and options evidence remain **separate**.
- GEX treated as daily structural; OI as overnight; live intraday evidence taken
  only from `flow_per_strike` (and the tape for spot).
- No combined score, no dark-pool integration, no Phase 3 wiring.
- The local Phase-1/2 implementation was **not** recreated, so no divergent copy
  exists.

---

# Addendum — second session captured (TSLA, 2026-08-13)

A second full session was captured on the same MCP plane, from the open to
post-close. It confirms the day-1 findings and adds two that day 1 could not
produce.

## A1. What ran

| | |
|---|---|
| Window | 2026-08-13, 13:38Z → 20:04Z (full session + post-close) |
| Flow observations | **39** (D2_T1 → D2_T39), ~10 min spacing |
| Day-over-day GEX | 1 snapshot (first ever taken) |
| Overnight OI | 1 snapshot, closing the day-1 confirmation loop |

Combined across both sessions: **54 flow observations**, all raw payloads retained.

## A2. The overnight confirmation loop closed — NEW

The 2026-08-13 OI update (`last_date` 08-12 → `curr_date` 08-13) reflects the
session captured on day 1. The two largest OI builds were calls at **325
(+7,841)** and **327.5 (+6,793)** — the same two strikes that carried the
strongest and fastest-growing net ask-side call flow pressure through day 1.

Day-1 intraday flow became real open positioning overnight. This is the first
time the evidence layer's flow → OI loop has been closed with live data.

**Limit:** strike-level only. `flow_per_strike` aggregates across all expiries,
so day-1 flow cannot be attributed to the 08-14 contracts that built the OI.

## A3. Day-over-day GEX measured — NEW

Net GEX (call+put) change, 08-12 → 08-13, concentrated at the low end of the band:

| Strike | 08-12 | 08-13 | Δ |
|---|---|---|---|
| 330 | −15,982 | +29,927 | **+45,910** |
| 327.5 | +5,430 | +42,689 | **+37,260** |
| 325 | +5,527 | +39,440 | **+33,912** |
| 340 | +47,295 | +33,240 | −14,055 |

The largest GEX increases land on the same three strikes as the overnight OI
build and day-1 flow pressure.

**Confound:** the 08-12 0DTE contracts expired at day-1's close, so an unknown
share of this change is expiry roll-off rather than new positioning. Three
co-located signals are not evidence that one explains another.

## A4. Section 3.5 rule — exercised, and corrected

The partial-update rule fired on three day-2 candidates. **All three resolved as
real flow.** Only the original day-1 case (340) has ever actually reverted.

| Case | Move | Resolved |
|---|---|---|
| D1 340 | +2.38M on +1,486 contracts | **artifact** (reverted) |
| D2 325 (T15) | +0.87M | real |
| D2 330 (T26) | +2.03M on +1,238 contracts | real |
| D2 325 (T36) | −2.25M on +1,410 contracts, **bid**-side | real |

Two corrections to how §3.5 was originally stated:

1. **A large net move on small volume is NOT sufficient to identify an artifact.**
   The day-1 340 case and the day-2 330 case share that profile almost exactly
   and resolve oppositely. The only reliable discriminator is whether the lagging
   component takes on a matching amount at the *next* pull.
2. **The effect is not ask-side specific.** The day-2 325 case was a bid-side
   one-sided jump; it behaves identically.

Practical consequence: **a flag is not evidence of an artifact.** The flag rate
(4 in 54 observations) is far higher than the artifact rate (1 in 54). The rule
is correctly conservative, but a downstream consumer must not treat "flagged" as
"bad data" — it means "unresolved until the next observation."

## A5. Day-2 flow migration, full session

Net ask-minus-bid call premium, D2_T1 (13:38Z) → post-close:

| Strike | open | close | Δ | call vol Δ |
|---|---|---|---|---|
| 330 | +0.53M | **+10.08M** | +9.55M | +86,225 |
| 332.5 | +0.19M | +3.89M | +3.70M | +77,397 |
| 340 | +0.02M | +2.93M | +2.91M | +228,723 |
| 337.5 | +0.00M | +1.49M | +1.49M | +120,413 |
| 325 | −0.13M | +1.33M | +1.45M | +18,505 |
| 327.5 | +0.80M | +2.18M | +1.38M | +17,869 |
| 335 | −0.03M | +0.81M | +0.84M | +161,193 |

Structurally different from day 1, where 330 sat persistently negative
(−2.5 to −3.1M) and 325 led. On day 2, 330 led decisively and every tracked
strike closed positive.

335 is the clearest volume/pressure divergence: **+161,193 contracts** for only
+0.84M of net pressure, and it held negative for 28 consecutive observations
before flipping at 18:48Z.

## A6. What is still open — unchanged

Day 2 changes nothing about the REST-transport items. Request rate, 429 /
`Retry-After` behavior, poller reliability and **best cadence** still require a
valid REST token and the poller. The ~10 min MCP spacing cannot answer a 30–60s
cadence question.

Recommendation §5 item 4 is reinforced: at 45s the partial-update window is
crossed more often than at 10 min, so the corroboration rule must exist before
the REST shadow runs.
