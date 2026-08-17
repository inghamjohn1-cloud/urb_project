"""MCP-plane capture utility for the UW options evidence layer.

SCOPE / SEPARATION NOTE
-----------------------
This is NOT the Phase-2 REST shadow poller (`uw_poller.py` / `run_shadow.py`),
and it does NOT reimplement the Phase-1 engine (`uw_evidence.py`). Those live
on the authoring machine and remain the reference implementation.

This file exists because the REST plane is unavailable in the environment where
it runs (no valid `UW_API_KEY`). It captures the SAME measurables through the
Claude "Unusual Whales" MCP connector, which is the sanctioned investigation
plane. It is a transcription/persistence tool only:

  - it does not poll (Claude drives each observation),
  - it does not decide anything,
  - it retains raw payloads verbatim alongside every extract.

Labels carried forward from Phase 0 verbatim:
  flow_per_strike  -> LIVE (real-time, per-row timestamps to the pull-second)
  greek_exposure   -> DAILY-STRUCTURAL (static within a session; date-only stamp)
  open_interest    -> OVERNIGHT-CONFIRMED (prior-day / post-OCC)

Usage (called by Claude after each MCP tool result spills to disk):
    python mcp_capture.py flow  <spill_file> <obs_label>
    python mcp_capture.py gex   <spill_file> <obs_label>
    python mcp_capture.py digest
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

# Legacy fixed band. RETAINED so the 2026-08-12..14 series stays continuous and
# comparable; it is no longer the primary selector.
TRACKED_STRIKES = ["325", "327.5", "330", "332.5", "335", "337.5", "340"]

# Dynamic band: strikes within +/- BAND_PCT of contemporaneous spot, chosen from
# the strikes actually present in the payload (so it adapts to strike spacing).
# 0.025 comes from the 2026-08-14 investigation: spot +/-2.5% covered 100% of
# that session's 335.33-351.26 range, against 36% for the fixed band.
BAND_PCT = 0.025


def _dynamic_strikes(rows, spot: float, pct: float = BAND_PCT) -> list:
    """Strikes present in the payload within +/- pct of spot, ascending."""
    lo, hi = spot * (1 - pct), spot * (1 + pct)
    out = []
    for r in rows:
        s = _num(r.get("strike"))
        if s is not None and lo <= s <= hi:
            out.append(_norm(r.get("strike")))
    return sorted(set(out), key=float)

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "shadow_runs")
RAW = os.path.join(RUNS, "raw")
def _jsonl(kind: str, day: str) -> str:
    """Per-trading-day series file. The day comes from the payload, never from
    the wall clock, so a capture can never land in the wrong day's file."""
    return os.path.join(RUNS, f"TSLA_mcp_shadow_{kind}_{day}.jsonl")


def _norm(strike: str) -> str:
    """UW returns '335' and '332.5'; normalise so lookups are stable."""
    try:
        f = float(strike)
    except (TypeError, ValueError):
        return str(strike)
    return str(int(f)) if f == int(f) else str(f)


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _archive(spill: str, label: str, kind: str) -> dict:
    """Retain the raw payload verbatim; record its digest."""
    blob = open(spill, "rb").read()
    dest = os.path.join(RAW, f"TSLA_{kind}_{label}.json")
    shutil.copyfile(spill, dest)
    return {
        "raw_path": os.path.relpath(dest, HERE),
        "raw_bytes": len(blob),
        "raw_sha256": hashlib.sha256(blob).hexdigest(),
    }


def _extract(r: dict) -> dict:
    return {
        "call_premium": _num(r.get("call_premium")),
        "call_premium_ask_side": _num(r.get("call_premium_ask_side")),
        "call_premium_bid_side": _num(r.get("call_premium_bid_side")),
        "call_volume": r.get("call_volume"),
        "call_trades": r.get("call_trades"),
        "put_premium": _num(r.get("put_premium")),
        "put_premium_ask_side": _num(r.get("put_premium_ask_side")),
        "put_premium_bid_side": _num(r.get("put_premium_bid_side")),
        "put_volume": r.get("put_volume"),
        "put_trades": r.get("put_trades"),
        "timestamp": r.get("timestamp"),
    }


def capture_flow(spill: str, label: str, spot: float | None = None,
                 pull_ts: str | None = None) -> dict:
    payload = json.load(open(spill))
    rows = payload.get("data", [])
    by_strike = {_norm(r.get("strike")): r for r in rows}

    # legacy fixed band, kept for continuity with 2026-08-12..14
    tracked = {s: (_extract(by_strike[s]) if s in by_strike else None)
               for s in TRACKED_STRIKES}

    # spot-centred band
    band, dynamic = None, None
    if spot is not None:
        band = _dynamic_strikes(rows, spot)
        dynamic = {s: _extract(by_strike[s]) for s in band if s in by_strike}

    stamps = sorted(t for t in (r.get("timestamp") for r in rows) if t)
    rec = {
        "obs": label,
        "kind": "flow_per_strike",
        "plane": "mcp",
        "freshness_label": "LIVE",
        "date": payload.get("date"),
        "strike_rows": len(rows),
        "source_ts_max": stamps[-1] if stamps else None,
        "source_ts_min": stamps[0] if stamps else None,
        "pull_ts": pull_ts,
        "spot": spot,
        "band_pct": BAND_PCT if spot is not None else None,
        "dynamic_band": band,
        "dynamic": dynamic,
        "tracked": tracked,
        "totals": {
            "call_premium": sum(_num(r.get("call_premium")) or 0 for r in rows),
            "put_premium": sum(_num(r.get("put_premium")) or 0 for r in rows),
            "call_volume": sum(r.get("call_volume") or 0 for r in rows),
            "put_volume": sum(r.get("put_volume") or 0 for r in rows),
        },
    }
    rec.update(_archive(spill, label, "flow_per_strike"))
    day = rec["date"] or (stamps[-1][:10] if stamps else "unknown")
    rec["series_day"] = day
    with open(_jsonl("flow", day), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def capture_gex(spill: str, label: str) -> dict:
    """GEX is DAILY-STRUCTURAL. Captured only to re-test staticness, never as a series."""
    payload = json.load(open(spill))
    rows = payload.get("data", payload if isinstance(payload, list) else [])
    blob = json.dumps(rows, sort_keys=True).encode()
    rec = {
        "obs": label,
        "kind": "greek_exposure_by_strike",
        "plane": "mcp",
        "freshness_label": "DAILY-STRUCTURAL",
        "strike_rows": len(rows),
        "payload_sha256": hashlib.sha256(blob).hexdigest(),
        "note": "staticness probe only; GEX can never be made an intraday series",
    }
    rec.update(_archive(spill, label, "greek_exposure_by_strike"))
    day = (rows[0].get("date") if rows else None) or "unknown"
    rec["series_day"] = day
    with open(_jsonl("gex", day), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _read(path: str) -> list:
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def digest(day: str = "2026-08-12") -> None:
    flows = _read(_jsonl("flow", day))
    print(f"flow observations: {len(flows)}")
    for r in flows:
        print(f"  {r['obs']}  rows={r['strike_rows']}  src_ts_max={r['source_ts_max']}")

    if len(flows) >= 2:
        print("\nFLOW pressure migration (net ask-minus-bid call premium, $M), first -> last:")
        a, b = flows[0], flows[-1]
        print(f"  window {a['obs']} -> {b['obs']}")
        for s in TRACKED_STRIKES:
            ra, rb = a["tracked"].get(s), b["tracked"].get(s)
            if not ra or not rb:
                continue
            na = (ra["call_premium_ask_side"] or 0) - (ra["call_premium_bid_side"] or 0)
            nb = (rb["call_premium_ask_side"] or 0) - (rb["call_premium_bid_side"] or 0)
            dv = (rb["call_volume"] or 0) - (ra["call_volume"] or 0)
            print(f"    {s:>6}  net {na/1e6:+7.2f}M -> {nb/1e6:+7.2f}M  "
                  f"(delta {(nb-na)/1e6:+6.2f}M)   call_vol {dv:+d}")

    gex = _read(_jsonl("gex", day))
    if gex:
        print(f"\nGEX staticness probe: {len(gex)} observation(s)")
        digs = {r["payload_sha256"] for r in gex}
        for r in gex:
            print(f"  {r['obs']}  rows={r['strike_rows']}  sha256={r['payload_sha256'][:16]}")
        if len(gex) >= 2:
            print("  VERDICT: " + ("byte-identical across observations -> STATIC within session"
                                   if len(digs) == 1 else
                                   "payload CHANGED across observations -> NOT static"))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "digest":
        digest(sys.argv[2] if len(sys.argv) > 2 else "2026-08-12")
    elif cmd == "flow":
        from datetime import datetime, timezone
        spot = float(sys.argv[4]) if len(sys.argv) > 4 else None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = capture_flow(sys.argv[2], sys.argv[3], spot=spot, pull_ts=now)
        print(f"{r['obs']} rows={r['strike_rows']} src_ts_max={r['source_ts_max']} "
              f"pull_ts={r['pull_ts']} spot={r['spot']}")
        if r["dynamic"]:
            print(f"  DYNAMIC band (spot +/-{BAND_PCT:.1%} = "
                  f"{spot*(1-BAND_PCT):.2f}-{spot*(1+BAND_PCT):.2f}), "
                  f"{len(r['dynamic_band'])} strikes:")
            for s in r["dynamic_band"]:
                t = r["dynamic"][s]
                net = (t["call_premium_ask_side"] or 0) - (t["call_premium_bid_side"] or 0)
                print(f"  {s:>6} call_vol={t['call_volume']:>7} "
                      f"net_call_ask_minus_bid={net/1e6:+7.2f}M")
        print("  legacy fixed band:")
        for s in TRACKED_STRIKES:
            t = r["tracked"].get(s)
            if not t:
                continue
            net = (t["call_premium_ask_side"] or 0) - (t["call_premium_bid_side"] or 0)
            print(f"  {s:>6} call_vol={t['call_volume']:>7} "
                  f"net_call_ask_minus_bid={net/1e6:+7.2f}M")
    elif cmd == "gex":
        print(json.dumps(capture_gex(sys.argv[2], sys.argv[3]), indent=2))
    else:
        raise SystemExit(f"unknown command: {cmd}")
