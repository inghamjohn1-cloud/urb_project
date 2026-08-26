#!/usr/bin/env python3
"""Can a GEX gate or a GEX volatility overlay rescue the 195m spec rules?

Historical dealer-gamma aggregates are not obtainable from the data sources
wired into this repo (option-chain snapshots are current-only; RESEARCH.md
conclusion 3 records the same limitation). So instead of guessing, this bounds
both proposals with ORACLES - filters that use information no vendor could
give you, because they peek at the future.

The logic: if a perfect version of the filter doesn't help, the real, noisy
version cannot. An oracle result is an upper bound, never an edge.

  Proposal 1 - directional eligibility gate (close above zero-gamma for longs).
    Bounded by a perfect direction oracle: take the trade only when the
    underlying actually did move the trade's way.

  Proposal 2 - volatility overlay on the section-4 stop cap.
    Bounded by a perfect volatility oracle: set the cap from realised
    volatility over the holding period, known in advance.
"""

import math
import statistics as st
import sys

import vp195 as V
import vp195_spec as S

SPECS = [l.strip() for l in open("samples/cohort.txt")
         if l.strip() and not l.startswith("#")]


def collect():
    """Every spec trade, tagged with volatility regime and forward direction."""
    out = []
    for spec in SPECS:
        path, _, bk = spec.rpartition(":")
        sym = path.split("/")[-1].split("_")[0].upper()
        bars, ts, _ = S.scan_symbol(path, float(bk), sym)
        for t in ts:
            i = t.ref_i
            a = V.atr(bars, i)
            if a is None:
                continue
            hist = [V.atr(bars, k) / bars[k].c for k in range(max(15, i - 60), i)
                    if V.atr(bars, k)]
            now = a / bars[i].c
            t.vol_pct = (100.0 * sum(1 for h in hist if h < now) / len(hist)) if hist else 50.0
            # forward move of the underlying over a typical holding period
            j = min(i + 5, len(bars) - 1)
            t.fwd = (bars[j].c - bars[t.entry_i].c) * t.side
            # realised range over the holding period, in units of entry ATR
            k = min(t.exit_i if t.exit_i else i + 5, len(bars) - 1)
            seg = bars[t.entry_i:k + 1] or [bars[t.entry_i]]
            t.fwd_range = (max(b.h for b in seg) - min(b.l for b in seg)) / a
            t.atr_at_entry = a
            out.append(t)
    return out


def stat(rs):
    n = len(rs)
    if not n:
        return "     n=  0"
    m = st.mean(rs)
    sd = st.stdev(rs) if n > 1 else 0.0
    return "n=%3d  %+.3fR  (SE %.3f)" % (n, m, sd / math.sqrt(n) if n else 0)


def main():
    ts = collect()
    print("Baseline (spec as written): %s\n" % stat([t.r for t in ts]))

    print("=" * 74)
    print("PROPOSAL 1 - a directional eligibility gate")
    print("=" * 74)
    real = [t.r for t in ts]
    oracle = [t.r for t in ts if t.fwd > 0]
    print("  all trades                        %s" % stat(real))
    print("  PERFECT direction oracle          %s" % stat(oracle))
    print("     (keeps only trades where the underlying really did go the")
    print("      trade's way over the next 5 bars - unobtainable, an upper bound)")
    kept = 100.0 * len(oracle) / len(real)
    print("  the oracle keeps %.0f%% of trades and lifts expectancy by %+.2fR"
          % (kept, st.mean(oracle) - st.mean(real)))

    print("\n  What a REAL gate would do to sample size:")
    sd = st.stdev(real)
    for pass_rate in (0.8, 0.6, 0.5, 0.35):
        n2 = len(real) * pass_rate
        # to declare a +0.15R edge real at 95% you need n = (1.96*sd/0.15)^2
        need = (1.96 * sd / 0.15) ** 2
        yrs = need / (n2 / 8) / 2
        print("     gate passes %2.0f%% -> n=%3.0f per 6 months, so resolving a "
              "+0.15R edge needs ~%2.0f symbol-years" % (pass_rate * 100, n2, yrs))

    print("\n" + "=" * 74)
    print("PROPOSAL 2 - a volatility overlay on the section-4 stop cap")
    print("=" * 74)
    print("  Does volatility regime predict outcome at all?")
    lo = [t.r for t in ts if t.vol_pct < 33]
    mid = [t.r for t in ts if 33 <= t.vol_pct < 67]
    hi = [t.r for t in ts if t.vol_pct >= 67]
    print("     low-vol third   %s" % stat(lo))
    print("     mid             %s" % stat(mid))
    print("     high-vol third  %s" % stat(hi))

    print("\n  Conditioning the stop cap on TRAILING volatility:")
    base = S.MAX_STOP_WIDTHS
    for lab, tight, wide in (("tighten in high vol / widen in low", 1.5, 2.5),
                             ("widen in high vol / tighten in low", 2.5, 1.5)):
        out = []
        for spec in SPECS:
            path, _, bk = spec.rpartition(":")
            V.BUCKET = float(bk)
            bars = V.load(path)
            for t in collect_for(bars, path, float(bk), tight, wide):
                out.append(t)
        print("     %-38s %s" % (lab, stat(out)))
    S.MAX_STOP_WIDTHS = base

    print("\n  PERFECT volatility oracle on the stop cap:")
    for thresh in (1.0, 1.5, 2.0):
        sel = [t.r for t in ts if t.fwd_range <= thresh * 2.0]
        print("     keep only trades whose realised range <= %.1f x the 2-VAW cap  %s"
              % (thresh, stat(sel)))
    print("     (knowing the future range in advance - an upper bound on any")
    print("      volatility overlay, GEX-derived or otherwise)")


def collect_for(bars, path, bucket, tight, wide):
    """Re-run one symbol with a vol-conditioned stop cap."""
    res = []
    V.BUCKET = bucket
    sym = path.split("/")[-1].split("_")[0].upper()
    saved = S.MAX_STOP_WIDTHS
    _, ts, _ = S.scan_symbol(path, bucket, sym)
    for t in ts:
        i = t.ref_i
        a = V.atr(bars, i)
        if a is None:
            continue
        hist = [V.atr(bars, k) / bars[k].c for k in range(max(15, i - 60), i)
                if V.atr(bars, k)]
        now = a / bars[i].c
        pct = (100.0 * sum(1 for h in hist if h < now) / len(hist)) if hist else 50.0
        cap = tight if pct >= 67 else (wide if pct < 33 else saved)
        if abs(t.entry - t.stop0) <= cap * t.vaw:
            res.append(t.r)
    S.MAX_STOP_WIDTHS = saved
    return res


if __name__ == "__main__":
    main()
