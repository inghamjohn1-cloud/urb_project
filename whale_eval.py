#!/usr/bin/env python3
"""Verdict machine for the whale forward-test journal.

Pre-registered analysis, written BEFORE the data exists, so the evaluation
can't be bent to fit the outcome later. Reads logs/whale_journal.csv (which is
self-contained: every row carries that day's close, so forward returns come
from later rows of the same journal) and answers, per ticker and pooled:

  1. EUPHORIA:     after days flagged flow_state=euphoria (z > +1.5),
                   did the stock under/over-perform its own baseline?
  2. CAPITULATION: same for flow_state=capitulation (z < -1.5).
  3. DP SURGE:     after days when dark-pool block premium ran > 2x the
                   ticker's own median, what happened next?

Edge = mean forward return after events minus the ticker's unconditional mean
forward return over the same horizons (5/10/21 journal rows ~ trading days).

Usage:
    python whale_eval.py                          # verdicts from the journal
    python whale_eval.py --journal path.csv --min-events 5
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys

HORIZONS = [5, 10, 21]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path):
    by_ticker = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            by_ticker.setdefault(row["ticker"], []).append(row)
    for rows in by_ticker.values():
        rows.sort(key=lambda r: r["date"])
    return by_ticker


def fwd_returns(rows):
    closes = [_f(r["close"]) for r in rows]
    out = []
    for i in range(len(rows)):
        fr = {}
        for h in HORIZONS:
            fr[h] = (closes[i + h] / closes[i] - 1) if i + h < len(closes) and closes[i] else None
        out.append(fr)
    return out


def evaluate(rows, min_events):
    fr = fwd_returns(rows)
    n = len(rows)
    base = {h: [fr[i][h] for i in range(n) if fr[i][h] is not None] for h in HORIZONS}

    def events(pred):
        idx = [i for i in range(n) if pred(rows[i])]
        res = {}
        for h in HORIZONS:
            vals = [fr[i][h] for i in idx if fr[i][h] is not None]
            pend = sum(1 for i in idx if fr[i][h] is None)
            res[h] = (vals, pend)
        return len(idx), res

    dp_vals = [_f(r["dp_premium"]) for r in rows if _f(r["dp_premium"])]
    dp_med = statistics.median(dp_vals) if dp_vals else None

    checks = {
        "euphoria": lambda r: r.get("flow_state") == "euphoria",
        "capitulation": lambda r: r.get("flow_state") == "capitulation",
        "dp_surge": lambda r: dp_med and (_f(r.get("dp_premium")) or 0) > 2 * dp_med,
    }

    report = {}
    for name, pred in checks.items():
        total, res = events(pred)
        lines = {}
        for h in HORIZONS:
            vals, pend = res[h]
            if len(vals) >= min_events and len(base[h]) > 1:
                edge = statistics.mean(vals) - statistics.mean(base[h])
                lines[h] = f"{edge*100:+.2f}% (n={len(vals)}, {pend} pending)"
            elif total:
                lines[h] = f"verdict pending ({len(vals)} resolved, {pend} awaiting data, need {min_events})"
            else:
                lines[h] = "no events yet"
        report[name] = (total, lines)
    return report, n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="logs/whale_journal.csv")
    ap.add_argument("--min-events", type=int, default=5,
                    help="events needed before an edge number is printed")
    args = ap.parse_args(argv)

    try:
        by_ticker = load(args.journal)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    days = {t: len(r) for t, r in by_ticker.items()}
    print(f"WHALE FORWARD-TEST VERDICTS   ({sum(days.values())} rows: "
          + ", ".join(f"{t} {d}d" for t, d in sorted(days.items())) + ")")
    print("edge = event forward return minus the ticker's own baseline; "
          "positive after capitulation / negative after euphoria would confirm the fade thesis\n")

    for t in sorted(by_ticker):
        report, n = evaluate(by_ticker[t], args.min_events)
        print(f"── {t} ({n} logged days)")
        for sig, (total, lines) in report.items():
            print(f"   {sig:<13} events={total:<3} "
                  + "  ".join(f"{h}d: {lines[h]}" for h in HORIZONS))
    print("\nRule written in advance: a signal is REAL only if the same-sign edge shows at "
          ">=2 horizons with n>=10 pooled across tickers; otherwise it dies like the others.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
