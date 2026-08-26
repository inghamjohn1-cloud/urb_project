#!/usr/bin/env python3
"""Does the NON-GEX half of the A/B/aggressive grading discriminate?

The proposed hierarchy is: price -> value -> acceptance -> GEX -> risk, with GEX
as context rather than trigger. That only works if layers 1-3 carry signal on
their own; if they don't, GEX is being asked to supply the entire edge, which
contradicts its stated role.

Everything below is computable from price and volume alone, so it can be tested
now. Two proxies stand in for GEX levels:

  "room to the next major call wall"  ->  distance to the highest high of the
                                          prior 20 bars, in units of R
  "friction at a major level"         ->  entry sitting within 0.25R of it

They are not gamma. They are the price-structure analogue: a known overhead
level with a crowd at it. If distance-to-overhead-resistance carries no signal,
that is evidence about the SHAPE of the "room to the wall" idea, though not a
verdict on gamma itself.
"""

import math
import statistics as st

import vp195 as V
import vp195_spec as S

SPECS = [l.strip() for l in open("samples/cohort.txt")
         if l.strip() and not l.startswith("#")]
LOOKBACK = 20          # bars used for the overhead-resistance proxy


def grade(t, bars):
    """Assign the proposed grade from price and value only."""
    i, k = t.ref_i, t.entry_i
    ref = bars[i]
    long = t.side > 0
    # payoff available to the measured-move target, in R
    payoff = abs(t.target - t.entry) / t.risk if t.risk else 0.0
    if long:
        deep = bars[k].l <= ref.val          # pullback reached VAL, not just POC
    else:
        deep = bars[k].h >= ref.vah
    if t.trigger == "continuation" and payoff >= 2.0:
        return "A"
    if t.trigger == "continuation":
        return "A-minus"
    if deep:
        return "aggressive"
    return "B"


def room(t, bars):
    """Proxy for 'room to the next major call wall', in R."""
    i = t.ref_i
    w = bars[max(0, i - LOOKBACK):i]
    if not w:
        return 0.0
    level = max(b.h for b in w) if t.side > 0 else min(b.l for b in w)
    return (level - t.entry) * t.side / t.risk if t.risk else 0.0


def collect():
    out = []
    for spec in SPECS:
        path, _, bk = spec.rpartition(":")
        sym = path.split("/")[-1].split("_")[0].upper()
        bars, ts, _ = S.scan_symbol(path, float(bk), sym)
        for t in ts:
            t.grade = grade(t, bars)
            t.room = room(t, bars)
            t.payoff = abs(t.target - t.entry) / t.risk if t.risk else 0.0
            out.append(t)
    return out


def line(lab, rs, w=22):
    n = len(rs)
    if n < 2:
        return "  %-*s n=%3d   --" % (w, lab, n)
    m, sd = st.mean(rs), st.stdev(rs)
    se = sd / math.sqrt(n)
    return ("  %-*s n=%3d  %+.3fR  (SE %.3f, t=%+.2f)  hit %2.0f%%"
            % (w, lab, n, m, se, m / se, 100.0 * len([r for r in rs if r > 0]) / n))


def main():
    ts = collect()
    print("Non-GEX components of the proposed grading, on %d spec trades\n" % len(ts))

    print("=== 1. Does the grade ladder rank as claimed? (A best, aggressive worst) ===")
    for g in ("A", "A-minus", "B", "aggressive"):
        print(line(g, [t.r for t in ts if t.grade == g]))
    print(line("ALL", [t.r for t in ts]))

    print("\n=== 2. Long-only (the grades are written for longs) ===")
    for g in ("A", "A-minus", "B", "aggressive"):
        print(line(g, [t.r for t in ts if t.grade == g and t.side > 0]))

    print("\n=== 3. 'Room to the next major level' - does more room pay? ===")
    rs = sorted(ts, key=lambda t: t.room)
    n = len(rs)
    for lab, seg in (("least room (bottom 3rd)", rs[:n // 3]),
                     ("middle third", rs[n // 3:2 * n // 3]),
                     ("most room (top 3rd)", rs[2 * n // 3:])):
        print(line(lab, [t.r for t in seg], 24))
    xs = [t.room for t in ts]
    ys = [t.r for t in ts]
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    r = cov / den if den else 0.0
    print("  correlation between room and outcome: r = %+.3f "
          "(t = %+.2f on %d trades)" % (r, r * math.sqrt((n - 2) / (1 - r * r)), n))

    print("\n=== 4. \"Don't chase\" - entries sitting right under overhead resistance ===")
    near = [t.r for t in ts if 0 <= t.room <= 0.25]
    far = [t.r for t in ts if t.room > 0.25]
    print(line("within 0.25R of it", near, 24))
    print(line("clear of it", far, 24))

    print("\n=== 5. The 2:1 requirement on its own ===")
    for lab, sel in (("payoff >= 2.0R", [t.r for t in ts if t.payoff >= 2.0]),
                     ("payoff <  2.0R", [t.r for t in ts if t.payoff < 2.0])):
        print(line(lab, sel, 24))


if __name__ == "__main__":
    main()
