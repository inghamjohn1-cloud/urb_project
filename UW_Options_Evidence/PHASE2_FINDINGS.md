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
| Window | 2026-08-12, 17:40Z → 19:47Z (2h 07m), regular session |
| Flow observations | **13** (T5 → T17), ~10 min spacing, 201 strikes each |
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
4. Phase 3 wiring stays **not approved** until a clean live REST shadow exists.

## 6. Rules honoured

- Production Live Participation Monitor **untouched**; nothing imports it.
- Equity participation and options evidence remain **separate**.
- GEX treated as daily structural; OI as overnight; live intraday evidence taken
  only from `flow_per_strike` (and the tape for spot).
- No combined score, no dark-pool integration, no Phase 3 wiring.
- The local Phase-1/2 implementation was **not** recreated, so no divergent copy
  exists.
