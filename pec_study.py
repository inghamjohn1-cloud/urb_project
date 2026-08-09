#!/usr/bin/env python3
"""Historical study: do post-earnings overreactions revert?

Reconstructs earnings events from Unusual Whales daily OHLC history. Each
daily row carries an embedded `earnings` object on report days (actual EPS,
street estimate, and the option market's pre-print implied move) plus that
day's net_premium, so a single 500-row fetch per ticker yields ~8 events with
the price path and flow context around each.

The point of the implied move is that it defines "overreaction" without an
arbitrary threshold. A stock falling 9% sounds dramatic, but if the options
market priced an 8% move it was merely in line. Overreaction is the move
divided by what was expected.

Event timing follows report_time: a premarket print reacts the same session,
a postmarket print reacts the next one.

Usage:
    python pec_study.py --dir data/study/raw --out logs/pec_study.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics

HORIZONS = [5, 10, 21]


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_ticker(path: str) -> tuple[str, list[dict]]:
    with open(path) as fh:
        payload = json.load(fh)
    rows = payload.get("result", payload if isinstance(payload, list) else [])
    if not isinstance(rows, list) or not rows:
        return "", []
    rows = [r for r in rows if r.get("date") and _f(r.get("close"))]
    rows.sort(key=lambda r: r["date"])
    return str(rows[0].get("ticker") or ""), rows


def events(ticker: str, rows: list[dict]) -> list[dict]:
    """Extract one record per earnings print with forward returns attached."""
    out = []
    closes = [_f(r["close"]) for r in rows]
    for i, r in enumerate(rows):
        e = r.get("earnings")
        if not e:
            continue
        actual, est = _f(e.get("actual_eps")), _f(e.get("street_mean_est"))
        implied = _f(e.get("expected_move_perc"))
        if actual is None or est is None or not implied:
            continue

        # premarket prints react the same session; postmarket prints react the next
        if str(e.get("report_time")) == "postmarket":
            pre_i, post_i = i, i + 1
        else:
            pre_i, post_i = i - 1, i
        if pre_i < 0 or post_i >= len(rows):
            continue

        pre, post = closes[pre_i], closes[post_i]
        if not pre or not post:
            continue
        move = post / pre - 1

        # Surprise relative to the magnitude of the estimate. A sign flip
        # (loss where a profit was expected) has no meaningful percentage, so
        # those are recorded but excluded from the "decent print" band below.
        surprise = ((actual - est) / abs(est) * 100) if est else None
        sign_flip = (actual < 0) != (est < 0)

        rec = {
            "ticker": ticker,
            "report_date": str(r["date"])[:10],
            "report_time": e.get("report_time") or "",
            "reported_eps": actual,
            "estimated_eps": est,
            "surprise_pct": round(surprise, 2) if surprise is not None else "",
            "sign_flip": sign_flip,
            "pre_close": round(pre, 2),
            "post_close": round(post, 2),
            "actual_move": round(move, 4),
            "implied_move": round(implied, 4),
            # >1 means the stock moved further than the options market priced
            "move_vs_implied": round(abs(move) / implied, 2) if implied else "",
            "net_premium": _f(rows[post_i].get("net_premium")),
            "iv_rank": _f(rows[post_i].get("iv_rank")),
        }
        for h in HORIZONS:
            j = post_i + h
            rec[f"fwd_{h}d"] = round(closes[j] / post - 1, 4) if j < len(closes) else ""
        out.append(rec)
    return out


def classify(rec: dict, band: float, ratio: float) -> str:
    """overreaction = fell further than priced, on a print that wasn't bad."""
    move = _f(rec.get("actual_move"), 0.0)
    mvi = _f(rec.get("move_vs_implied"), 0.0)
    sp = _f(rec.get("surprise_pct"))
    if move >= 0:
        return "up"
    if rec.get("sign_flip") or sp is None:
        return "other"
    if sp <= -band:
        return "capitulation" if mvi >= ratio else "miss"
    return "overreaction" if mvi >= ratio else "orderly"


def summarize(recs: list[dict], label: str, baseline: dict) -> None:
    print(f"\n  {label}  (n={len(recs)})")
    if not recs:
        return
    for h in HORIZONS:
        vals = [_f(r[f"fwd_{h}d"]) for r in recs if r.get(f"fwd_{h}d") != ""]
        vals = [v for v in vals if v is not None]
        if len(vals) < 5:
            print(f"    {h:>2}d  n={len(vals):<4} too few to report")
            continue
        m = statistics.mean(vals)
        base = baseline.get(h)
        win = sum(1 for v in vals if v > 0) / len(vals) * 100
        edge = f"  edge {(m-base)*100:+.2f}%" if base is not None else ""
        # standard error, so a mean can be read against its own noise
        se = statistics.pstdev(vals) / (len(vals) ** 0.5)
        print(f"    {h:>2}d  n={len(vals):<4} mean {m*100:+6.2f}%  "
              f"win {win:4.1f}%  se {se*100:4.2f}%{edge}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/study/raw")
    ap.add_argument("--out", default="logs/pec_study.csv")
    ap.add_argument("--band", type=float, default=20.0,
                    help="EPS surprise worse than -band%% counts as a real miss")
    ap.add_argument("--ratio", type=float, default=1.0,
                    help="move/implied_move above this counts as disproportionate")
    args = ap.parse_args(argv)

    recs: list[dict] = []
    files = sorted(glob.glob(os.path.join(args.dir, "*.json")))
    for p in files:
        t, rows = load_ticker(p)
        if t:
            recs.extend(events(t, rows))
    if not recs:
        print(f"no events found in {args.dir}")
        return 1

    for r in recs:
        r["bucket"] = classify(r, args.band, args.ratio)

    tickers = {r["ticker"] for r in recs}
    dates = sorted(r["report_date"] for r in recs)
    print(f"PEC HISTORICAL STUDY   {len(recs)} earnings events across "
          f"{len(tickers)} tickers, {dates[0]} → {dates[-1]}")

    # Baseline: every event in the sample, so "edge" means excess over simply
    # holding a random one of these names through any earnings print.
    baseline = {}
    for h in HORIZONS:
        vals = [_f(r[f"fwd_{h}d"]) for r in recs if r.get(f"fwd_{h}d") != ""]
        vals = [v for v in vals if v is not None]
        if vals:
            baseline[h] = statistics.mean(vals)
    print(f"baseline (all events): "
          + "  ".join(f"{h}d {baseline[h]*100:+.2f}%" for h in sorted(baseline)))

    order = ["overreaction", "orderly", "capitulation", "miss", "up", "other"]
    counts = {b: [r for r in recs if r["bucket"] == b] for b in order}
    for b in order:
        if counts[b]:
            summarize(counts[b], b.upper(), baseline)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    cols = list(recs[0].keys())
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(recs, key=lambda r: (r["report_date"], r["ticker"])))
    print(f"\nwrote {len(recs)} events → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
