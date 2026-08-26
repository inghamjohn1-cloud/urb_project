#!/usr/bin/env python3
"""Run the 195m volume-profile rules across a cohort and pool the results.

A single symbol cannot tell you whether these rules work - `vp195.py` on RGLD
alone produced nine trades, which is noise. This runs the identical rules over
several symbols and reports the pooled numbers with an honest error bar.

Usage:
    python vp195_cohort.py DIR/rgld.csv:0.25 DIR/jpm.csv:0.30 ...
    python vp195_cohort.py --spec cohort.txt        # one "path:bucket" per line
"""

import argparse
import math
import os
import sys

import vp195 as V


def run_symbol(path, bucket, composite=None):
    """Replay every valid signal for one symbol. Returns (label, [trades])."""
    V.BUCKET = bucket
    if composite:
        V.COMPOSITE_BARS = composite
    bars = V.load(path)
    start = max(V.SMA_BARS, V.COMPOSITE_BARS + V.OVERLAP_LOOKBACK, V.ATR_BARS) + 1
    rows, scanned = [], 0
    for i in range(start, len(bars) - 1):
        scanned += 1
        for sig in V.scan_bar(bars, i):
            if sig.vetoes:
                continue
            t = V.replay(bars, sig)
            if t is None:
                continue
            rows.append({
                "bar": sig.bar.key, "setup": sig.setup,
                "side": "long" if sig.side > 0 else "short",
                "r": t.realised_r, "bars": t.bars_held,
                "mfe": t.mfe_r, "mae": t.mae_r, "why": t.reasons,
            })
    label = os.path.basename(path).split(".")[0]
    if label.endswith("_195m"):
        label = label[:-5]
    label = label.upper()
    return label, rows, scanned


def stats(rs):
    n = len(rs)
    if n == 0:
        return None
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n) if n else 0.0
    wins = [r for r in rs if r > 0]
    return {
        "n": n, "mean": mean, "sd": sd, "se": se,
        "t": mean / se if se else 0.0,
        "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
        "hit": 100.0 * len(wins) / n,
        "total": sum(rs),
        "best": max(rs), "worst": min(rs),
    }


def line(label, s, width=8):
    if s is None:
        return "%-*s      no trades" % (width, label)
    return ("%-*s  n=%3d   %+.2fR  [95%% %+.2f .. %+.2f]   hit %3.0f%%   "
            "total %+6.1fR   worst %+.2fR"
            % (width, label, s["n"], s["mean"], s["lo"], s["hi"],
               s["hit"], s["total"], s["worst"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="*", help="path.csv:bucket_size")
    ap.add_argument("--spec", help="file with one path:bucket per line")
    ap.add_argument("--composite", type=int, default=None)
    ap.add_argument("--trades", action="store_true", help="print every trade")
    args = ap.parse_args()

    specs = list(args.specs)
    if args.spec:
        specs += [l.strip() for l in open(args.spec) if l.strip()
                  and not l.startswith("#")]
    if not specs:
        sys.exit("no symbols given")

    allrows, per, total_scanned = [], [], 0
    for spec in specs:
        path, _, bk = spec.rpartition(":")
        label, rows, scanned = run_symbol(path, float(bk), args.composite)
        total_scanned += scanned
        for r in rows:
            r["sym"] = label
        allrows += rows
        per.append((label, rows))

    print("COHORT: %d symbols, %d bars scanned, %d trades taken\n"
          % (len(per), total_scanned, len(allrows)))

    print("PER SYMBOL")
    for label, rows in sorted(per, key=lambda p: -len(p[1])):
        print("  " + line(label, stats([r["r"] for r in rows])))

    print("\nBY SETUP")
    for k in ("A", "B", "C"):
        print("  " + line(k, stats([r["r"] for r in allrows if r["setup"] == k])))

    print("\nBY SIDE")
    for k in ("long", "short"):
        print("  " + line(k, stats([r["r"] for r in allrows if r["side"] == k])))

    print("\nBY EXIT REASON")
    reasons = {}
    for r in allrows:
        reasons.setdefault(r["why"], []).append(r["r"])
    for k in sorted(reasons, key=lambda k: -len(reasons[k])):
        s = stats(reasons[k])
        print("  %-22s n=%3d  avg %+.2fR" % (k, s["n"], s["mean"]))

    s = stats([r["r"] for r in allrows])
    print("\n" + "=" * 78)
    print("POOLED   " + line("", s, 0).strip())
    print("         sd %.2fR   t = %+.2f" % (s["sd"], s["t"]))
    verdict = ("distinguishable from zero" if abs(s["t"]) > 1.96
               else "NOT distinguishable from zero")
    print("         the mean is %s at 95%% confidence" % verdict)

    # Drop-one-symbol robustness: is any single name carrying the result?
    print("\nLEAVE-ONE-OUT (pooled mean with each symbol removed)")
    for label, _ in per:
        rest = [r["r"] for r in allrows if r["sym"] != label]
        print("  without %-6s n=%3d   %+.2fR" % (label, len(rest),
                                                 stats(rest)["mean"]))

    if args.trades:
        print("\nTRADES")
        print("  %-6s %-14s %-2s %-5s %7s %5s %7s %7s  %s"
              % ("sym", "bar", "s", "side", "R", "bars", "MFE", "MAE", "exit"))
        for r in sorted(allrows, key=lambda r: r["bar"]):
            print("  %-6s %-14s %-2s %-5s %+7.2f %5d %+7.2f %+7.2f  %s"
                  % (r["sym"], r["bar"], r["setup"], r["side"], r["r"],
                     r["bars"], r["mfe"], r["mae"], r["why"]))


if __name__ == "__main__":
    main()
