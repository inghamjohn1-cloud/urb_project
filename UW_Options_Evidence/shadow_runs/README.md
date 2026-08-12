# TSLA UW options evidence — live capture, 2026-08-12 (MCP plane)

## What this is

A live intraday capture of the UW options **evidence layer** measurables for TSLA
on 2026-08-12, taken through the **MCP plane** (Claude "Unusual Whales" connector).

This is **not** the Phase-2 REST shadow run. It is a substitute capture made
because the REST plane was unavailable in this environment (see *Why not REST*).

## Separation of concerns — unchanged

- The production **Live Participation Monitor** (`Participation_Engine/`,
  `TSLA_Live_Participation_Monitor/`) is **untouched**. Nothing here imports it.
- Equity participation and options evidence remain **separate** systems.
- Nothing here is wired into the monitor. Phase 3 is **not** started.

## Why not REST

The Phase-2 REST shadow (`uw_poller.py` / `run_shadow.py`) could not run here:

1. **No token.** `UW_API_KEY`, `UW_TOKEN`, and `UNUSUAL_WHALES_TOKEN` are all
   unset in this container, and there is no `.env`. This is a stronger blocker
   than the previously-recorded one (a *present but invalid* 36-char token) —
   here there is no credential at all.
2. **No code.** The Phase-1/Phase-2 package does not exist in this repository;
   it lives only on the authoring machine.

By explicit decision, the local implementation remains the single source of
truth and was **not** recreated here, to avoid a second divergent copy of code
that already passes 14/14 tests.

## Consequence for the Phase-2 report

The MCP plane **cannot** answer the REST-specific Phase-2 report items. Those
remain open and still require a valid REST token:

- polling reliability of the REST poller
- actual REST request rate
- throttling / rate-limit (429, Retry-After) behavior
- best cadence (30 / 45 / 60 s)

What this capture **does** answer is the data-side questions: effective
freshness, live engine measurables, and a long-baseline re-test of the
GEX-is-static finding.

## Files

| File | Contents |
|---|---|
| `TSLA_mcp_shadow_flow_2026-08-12.jsonl` | One record per observation: tracked-strike flow extract + day totals + source timestamps + raw digest |
| `TSLA_mcp_shadow_gex_2026-08-12.jsonl`  | GEX staticness probes (tracked-strike subset), `DAILY-STRUCTURAL` |
| `raw/`                                   | Raw payloads retained verbatim, one file per observation |

Raw is always retained, per the Phase-1 principle.

## Labels carried forward from Phase 0 (unchanged)

- `flow_per_strike` → **LIVE** (real-time; per-row timestamps to the pull-second)
- `greek_exposure`  → **DAILY-STRUCTURAL** (static within a session; date-only stamp)
- `open_interest`   → **OVERNIGHT-CONFIRMED** (prior-day / post-OCC)

Intraday "GEX migration" remains a **misnomer**. Only FLOW and the TAPE move
intraday. GEX is never built into a series here.

## Continuity with Phase 0

This is the **same trading session** as Phase 0 (2026-08-12, T1–T4 ran
15:24→16:42Z), so these observations **extend** that series rather than starting
a new one. Observation labels continue at `T5`.

Cross-check on the Phase-0 continuity anchor — strike 335 call volume:

| Obs | Time (UTC) | 335 call volume |
|---|---|---|
| T1–T4 (Phase 0, local fixtures) | 15:24 → 16:42 | 80,049 → 95,465 |
| T5 (here) | 17:40 | 101,758 |
