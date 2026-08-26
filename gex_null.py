#!/usr/bin/env python3
"""Pre-register the bar a real GEX feed has to clear.

The gex.py context layer sorts trades into A / B / aggressive / downgrade /
veto. Any feed produces *some* separation between those buckets - including a
feed of pure noise, because the layer is slicing 103 trades several ways.

So: build many synthetic feeds with realistic geometry but no information
(flip near price, walls a few percent out, random sign on net gamma), run the
layer on each, and record what the kept bucket returns. That distribution is
what a real feed must beat to be evidence of anything.

Run this BEFORE buying data, and write the threshold down.
"""

import random
import statistics as st
import sys

import gex as GX
import vp195 as V
import vp195_spec as S

SPECS = [l.strip() for l in open("samples/cohort.txt")
         if l.strip() and not l.startswith("#")]
N_FEEDS = 200


def trades_once():
    out = []
    for spec in SPECS:
        path, _, bk = spec.rpartition(":")
        sym = path.split("/")[-1].split("_")[0].upper()
        bars, ts, _ = S.scan_symbol(path, float(bk), sym)
        px = {b.key.split("|")[0]: b.c for b in bars}
        for t in ts:
            t.day = t.entry_key.split("|")[0] if hasattr(t, "entry_key") else None
        for t in ts:
            t.sym = sym
            t.day = bars[t.entry_i].key.split("|")[0]
            t.ref_close = bars[t.ref_i].c
            out.append(t)
    return out


def grade_all(ts, rng, flip_sd=0.01, wall=0.03):
    kept, buckets = [], {}
    for t in ts:
        p = t.ref_close
        day = GX.GexDay(t.day, t.sym,
                        p * (1 + rng.gauss(0, flip_sd)),
                        p * (1 + wall), p * (1 - wall),
                        rng.choice([1, -1]) * 1e9, (), ())
        ctx = GX.context(t.entry, day, t.vaw, t.risk)
        structure = {"above_vah": t.trigger == "continuation" and t.side > 0,
                     "poc_rising": t.side > 0,
                     "reclaimed_poc": t.trigger == "pullback" and t.side > 0,
                     "val_rejection": t.trigger == "pullback" and t.side > 0,
                     "below_val": t.side < 0}
        g, _ = GX.grade_long(structure, ctx)
        buckets.setdefault(g, []).append(t.r)
        if g in ("A", "B", "aggressive"):
            kept.append(t.r)
    return kept, buckets


def main():
    ts = trades_once()
    base = st.mean(t.r for t in ts)
    print("Baseline: %d spec trades, %+.3fR ungraded\n" % (len(ts), base))
    print("Running %d synthetic GEX feeds with NO information...\n" % N_FEEDS)

    kept_means, kept_ns, seps = [], [], []
    for s in range(N_FEEDS):
        rng = random.Random(9000 + s)
        kept, buckets = grade_all(ts, rng)
        if len(kept) < 5:
            continue
        kept_means.append(st.mean(kept))
        kept_ns.append(len(kept))
        down = buckets.get("downgrade", []) + buckets.get("veto", [])
        if down:
            seps.append(st.mean(kept) - st.mean(down))

    km = sorted(kept_means)
    sp = sorted(seps)
    n = len(km)
    print("What a NOISE feed produces for the kept (A+B+aggressive) bucket:")
    print("   median  %+.3fR      mean n kept  %.0f of %d trades"
          % (km[n // 2], st.mean(kept_ns), len(ts)))
    print("   5th pct %+.3fR      95th pct %+.3fR" % (km[int(.05 * n)], km[int(.95 * n)]))
    print("   best of %d noise feeds: %+.3fR" % (n, km[-1]))
    print("\nSeparation between kept and downgraded/vetoed, from noise alone:")
    m = len(sp)
    print("   median %+.3fR   95th pct %+.3fR   best %+.3fR"
          % (sp[m // 2], sp[int(.95 * m)], sp[-1]))

    print("\n" + "=" * 72)
    print("PRE-REGISTERED THRESHOLDS — write these down before you buy data")
    print("=" * 72)
    print("  A real GEX feed is evidence of something only if, on this cohort:")
    print("    * the kept bucket returns more than %+.3fR      (95th pct of noise)"
          % km[int(.95 * n)])
    print("    * kept-minus-downgraded exceeds %+.3fR          (95th pct of noise)"
          % sp[int(.95 * m)])
    print("  Anything below those numbers is what random gamma levels already do.")
    print("\n  Note how large they are. Slicing ~100 trades into five buckets")
    print("  manufactures separation on its own; that is the cost of a layer with")
    print("  this many branches, and it is why the layer must be tested, not")
    print("  reasoned about.")


if __name__ == "__main__":
    main()
