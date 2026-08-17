"""One-off backfill: add the spot-centred dynamic band to observations captured
before the band switch. Reads each record's retained raw payload, so nothing is
estimated -- the band is recomputed from the same bytes the original capture saw.

Spot comes from UW 15m regular-session candles (bar close of the bar containing
the observation's source timestamp). Post-close observations use the final
19:45 bar. Records are enriched in place; original fields are left untouched and
the pre-backfill state remains in git history.
"""
import json, os, sys
import mcp_capture as mc

BARS = {  # date -> {bar_start_minute_of_day: close}
 "2026-08-12": [329.4185,328.0617,327.5257,325.5925,325.07,324.6,325,324.38,324.57,
   325.98,326.57,326.945,326.728,326.925,326.825,326.29,326.015,326.295,326.46,
   326.5061,326.49,327.08,327.135,327.68,327.33,327.45],
 "2026-08-13": [329.445,332.96,332.6351,332.895,333.705,334.6,333.81,332.955,333.455,
   333.12,333.58,334.5436,335.06,336.525,335.925,336.3501,336.27,336.27,337.865,
   338.4357,338.36,339.535,339.635,338.75,339.38,340.06],
 "2026-08-14": [347.53,349.925,347.05,346.21,342.8018,341.2,338.495,337.3546,337.62,
   338.684,340.595,340.6573,339.685,338.38,338.92,339.315,341.1591,341.52,341.7,
   341.82,341.6301,341.0601,341.36,341.88,341.59,342.32],
 "2026-08-17": [340.72,339.41,339.1699],
}
OPEN_MIN = 13*60 + 30  # 13:30Z

def spot_for(ts: str, day: str):
    bars = BARS.get(day)
    if not bars or not ts:
        return None
    mins = int(ts[11:13])*60 + int(ts[14:16])
    idx = (mins - OPEN_MIN)//15
    if idx < 0:
        idx = 0
    if idx >= len(bars):          # post-close -> final bar
        idx = len(bars)-1
    return bars[idx]

def backfill(day: str) -> None:
    path = mc._jsonl("flow", day)
    if not os.path.exists(path):
        print(f"  {day}: no series file"); return
    recs = [json.loads(l) for l in open(path) if l.strip()]
    done = 0
    for rec in recs:
        if rec.get("dynamic"):
            continue
        raw = os.path.join(mc.HERE, rec["raw_path"])
        rows = json.load(open(raw)).get("data", [])
        spot = spot_for(rec.get("source_ts_max"), day)
        if spot is None or not rows:
            continue
        by = {mc._norm(r.get("strike")): r for r in rows}
        band = mc._dynamic_strikes(rows, spot)
        rec["spot"] = spot
        rec["band_pct"] = mc.BAND_PCT
        rec["dynamic_band"] = band
        rec["dynamic"] = {s: mc._extract(by[s]) for s in band if s in by}
        rec["backfilled"] = True
        done += 1
    open(path, "w").write("".join(json.dumps(r)+"\n" for r in recs))
    print(f"  {day}: {done}/{len(recs)} records enriched")

if __name__ == "__main__":
    for d in (sys.argv[1:] or ["2026-08-12","2026-08-13","2026-08-14","2026-08-17"]):
        backfill(d)
