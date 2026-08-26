#!/usr/bin/env python3
"""GEX context layer for the 195m plan — the terrain, never the trigger.

Implements the proposed hierarchy:

    195m price structure -> POC/VAH/VAL -> GEX context -> entry/exit

GEX never fires a trade here. It grades one that price and value have already
produced, and it can veto or downgrade. That ordering is deliberate: it stops
"there's a call wall at 270, so price can't go through 270" from becoming a
trading decision.

NOTHING IN THIS MODULE HAS BEEN TESTED. Historical dealer-gamma aggregates are
not available through this repo's data sources (see SPEC_195M_TEST.md), so this
is a specification with a working implementation, waiting on a feed. Point it at
one and `vp195_spec.py --gex FILE` runs the same cohort test with the layer
attached.

Input CSV — one row per symbol per session, from your vendor:

    date,symbol,flip,call_wall,put_wall,net_gex
    2026-08-25,RGLD,259.00,268.00,251.00,1.24e9

`net_gex` is optional; when absent, regime is inferred from price vs flip alone.
Major positive/negative strikes beyond the walls can be appended as
`pos_strikes` / `put_strikes` pipe-separated, and are used for the next-level
lookup when present.
"""

import csv
import os
from collections import namedtuple

GexDay = namedtuple("GexDay", "date symbol flip call_wall put_wall net_gex "
                              "pos_strikes neg_strikes")

NEAR_FLIP_ATR = 0.5      # within this many ATR of the flip = transition zone
WALL_FRICTION_ATR = 0.5  # within this many ATR of a wall = friction zone
MIN_ROOM_R = 1.5         # "meaningful room" before the next wall, in R


def load(path):
    """Return {(symbol, 'YYYY-MM-DD'): GexDay}."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            def num(k):
                v = (row.get(k) or "").strip()
                return float(v) if v else None

            def strikes(k):
                v = (row.get(k) or "").strip()
                return tuple(sorted(float(x) for x in v.split("|") if x)) if v else ()

            sym = row["symbol"].strip().upper()
            out[(sym, row["date"].strip())] = GexDay(
                row["date"].strip(), sym, num("flip"), num("call_wall"),
                num("put_wall"), num("net_gex"),
                strikes("pos_strikes"), strikes("neg_strikes"))
    return out


def regime(price, day):
    """positive | negative | near_flip | unknown."""
    if day is None or day.flip is None:
        return "unknown"
    if day.net_gex is not None:
        return "positive" if day.net_gex > 0 else "negative"
    return "positive" if price > day.flip else "negative"


def context(price, day, atr, risk=None):
    """Everything the decision table needs, from one day's levels."""
    c = {"regime": regime(price, day), "above_flip": None, "near_flip": False,
         "room_to_call_wall": None, "room_r": None, "at_call_wall": False,
         "at_put_wall": False, "flip": None, "call_wall": None,
         "put_wall": None, "flip_delta": None}
    if day is None:
        return c
    c["flip"], c["call_wall"], c["put_wall"] = day.flip, day.call_wall, day.put_wall
    if day.flip is not None:
        c["above_flip"] = price > day.flip
        c["near_flip"] = abs(price - day.flip) <= NEAR_FLIP_ATR * atr
    if day.call_wall is not None:
        gap = day.call_wall - price
        c["room_to_call_wall"] = gap
        c["room_r"] = gap / risk if risk else None
        c["at_call_wall"] = abs(gap) <= WALL_FRICTION_ATR * atr
    if day.put_wall is not None:
        c["at_put_wall"] = abs(price - day.put_wall) <= WALL_FRICTION_ATR * atr
    return c


def next_level_above(price, day):
    """Nearest major positive-GEX strike or call wall above price."""
    if day is None:
        return None
    cands = [s for s in day.pos_strikes if s > price]
    if day.call_wall is not None and day.call_wall > price:
        cands.append(day.call_wall)
    return min(cands) if cands else None


def delta(today, yesterday):
    """Day-over-day change in the levels — the migration of the terrain."""
    if today is None or yesterday is None:
        return {}
    out = {}
    for f in ("flip", "call_wall", "put_wall", "net_gex"):
        a, b = getattr(today, f), getattr(yesterday, f)
        out[f] = (a - b) if (a is not None and b is not None) else None
    return out


# ------------------------------------------------------------------ grading

def grade_long(structure, ctx):
    """Grade a long that price and value have already triggered.

    `structure` carries what the 195m chart decided:
        above_vah, poc_rising, reclaimed_poc, val_rejection, payoff_r
    Returns (grade, reasons). Grade is one of A, B, aggressive, downgrade, veto.
    """
    why = []
    above_flip = ctx.get("above_flip")
    room_r = ctx.get("room_r")

    # veto: structural failure below both value and gamma support
    if structure.get("below_val") and ctx.get("regime") == "negative" \
            and above_flip is False:
        return "veto", ["below VAL, below flip, negative gamma — no long"]

    # don't chase into a wall
    if ctx.get("at_call_wall"):
        return "downgrade", ["price at the call wall — friction, don't chase"]
    if room_r is not None and room_r < MIN_ROOM_R:
        why.append("only %.1fR to the call wall" % room_r)
        return "downgrade", why

    if ctx.get("near_flip"):
        why.append("within half an ATR of the gamma flip — regime transition")

    # A: acceptance above value, value migrating, above flip, room to run
    if structure.get("above_vah") and structure.get("poc_rising"):
        if above_flip and (room_r is None or room_r >= MIN_ROOM_R):
            why.append("above VAH, POC rising, above flip, room to the wall")
            if ctx.get("regime") == "negative":
                why.append("negative gamma — expansion favoured")
            return ("A" if not ctx.get("near_flip") else "B"), why
        if above_flip is False:
            why.append("above VAH but still below the gamma flip")
            return "B", why

    # B: POC reclaim. Above the flip it is confirmation; below it, provisional.
    if structure.get("reclaimed_poc"):
        if above_flip:
            why.append("POC reclaim with the flip reclaimed too — confirmation")
            return "B", why
        why.append("POC reclaim still below the flip — provisional")
        return "downgrade", why

    # aggressive: VAL rejection, best with put-wall or flip confluence
    if structure.get("val_rejection"):
        conf = [n for n, ok in (("put wall", ctx.get("at_put_wall")),
                                ("gamma flip", ctx.get("near_flip")),
                                ("positive gamma", ctx.get("regime") == "positive"))
                if ok]
        if conf:
            why.append("VAL rejection with " + " + ".join(conf) + " confluence")
            return "aggressive", why
        why.append("VAL rejection without gamma confluence")
        return "downgrade", why

    return "downgrade", why or ["no graded structure"]


def exit_pressure(structure, ctx):
    """Deterioration checks for an open long. Returns (action, reasons)."""
    why = []
    if structure.get("below_val") and ctx.get("put_wall") is not None \
            and structure.get("price", 0) < ctx["put_wall"]:
        return "exit", ["VAL and the put wall both lost — structural failure"]
    lost = [n for n, ok in (("VAH", structure.get("lost_vah")),
                            ("POC", structure.get("lost_poc")))
            if ok]
    if lost and ctx.get("above_flip") is False:
        return "reduce", ["lost " + " and ".join(lost) + ", and closed below the flip"]
    if structure.get("lost_vah") and ctx.get("above_flip"):
        return "watch", ["VAH lost but the gamma flip still holds"]
    if ctx.get("at_call_wall") and not structure.get("poc_rising"):
        return "reduce", ["stalling at the call wall with POC no longer migrating"]
    if ctx.get("at_call_wall"):
        return "watch", ["at the call wall but POC still migrating — let it work"]
    return "hold", why


def coverage(feed, bars, symbol):
    """How much of a bar series the feed actually covers — check before trusting."""
    days = {b.key.split("|")[0] for b in bars}
    have = sum(1 for d in days if (symbol, d) in feed)
    return have, len(days)
