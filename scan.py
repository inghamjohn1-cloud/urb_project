#!/usr/bin/env python3
"""Sector-rotation swing-strategy scanner.

Ranks the 11 SPDR sector ETFs by momentum + relative strength versus SPY,
gates on trend, optionally confirms with Unusual Whales options-flow (sector
tide), and prints a "rotate in / hold / rotate out" table with ATR-based
entry/stop/target levels for the leaders.

Usage:
    python scan.py                 # console table
    python scan.py --csv out.csv   # also write a CSV
    python scan.py --no-flow       # skip the options-flow confirmation calls
    python scan.py --top 4         # rotate into the top 4 sectors

Requires an Unusual Whales API token in UW_TOKEN (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

from uw_client import UWClient, UWError
from rotation import (
    SECTOR_ETFS, BENCHMARK,
    build_score, rank_sectors, market_regime,
)


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so we don't need python-dotenv."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def add_flow_confirmation(client: UWClient, scores) -> None:
    """Attach options-flow sentiment to each sector from the sector tide."""
    for s in scores:
        sector = SECTOR_ETFS.get(s.ticker, {}).get("uw_sector")
        if not sector:
            continue
        try:
            tide = client.sector_tide(sector)
        except UWError:
            continue
        if not tide:
            continue
        # sum net call/put premium across the day's intervals
        net_call = 0.0
        net_put = 0.0
        for row in tide:
            net_call += _num(row.get("net_call_premium"))
            net_put += _num(row.get("net_put_premium"))
        s.net_call_premium = net_call
        s.flow_bull = net_call > net_put


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt(v, suffix="", nd=1):
    if v is None:
        return "  -  "
    return f"{v:+.{nd}f}{suffix}" if suffix == "%" else f"{v:.{nd}f}{suffix}"


def print_report(regime, detail, scores, use_flow: bool) -> None:
    print()
    print("=" * 96)
    print(f"  SECTOR ROTATION SCANNER            regime: {regime}  ({detail})")
    print("=" * 96)
    header = (
        f"{'#':>2}  {'ETF':<5}{'Sector':<24}{'1M':>7}{'3M':>7}{'6M':>7}"
        f"{'RS':>7}{'Trend':>7}{'Flow':>6}{'Score':>7}  Signal"
    )
    print(header)
    print("-" * 96)
    for s in scores:
        trend = "up" if s.uptrend else ("--" if s.uptrend is False else "?")
        if use_flow:
            flow = "bull" if s.flow_bull else ("bear" if s.flow_bull is False else " - ")
        else:
            flow = " - "
        print(
            f"{s.rank:>2}  {s.ticker:<5}{s.name[:23]:<24}"
            f"{_fmt(s.ret_1m,'%'):>7}{_fmt(s.ret_3m,'%'):>7}{_fmt(s.ret_6m,'%'):>7}"
            f"{_fmt(s.rs_3m,'%'):>7}{trend:>7}{flow:>6}{s.score:>7.1f}  {s.signal}"
        )
    print("-" * 96)

    leaders = [s for s in scores if s.signal == "ROTATE IN"]
    if leaders:
        print("\n  SWING LEVELS (leaders) — ATR(14) based, 2R stop / 3R target:")
        for s in leaders:
            note = f"  [{', '.join(s.notes)}]" if s.notes else ""
            print(
                f"    {s.ticker:<5} entry {_fmt(s.entry,'',2)}   "
                f"stop {_fmt(s.stop,'',2)}   target {_fmt(s.target,'',2)}   "
                f"ATR {_fmt(s.atr,'',2)}{note}"
            )
    outs = [s.ticker for s in scores if s.signal == "ROTATE OUT"]
    if outs:
        print(f"\n  ROTATE OUT / AVOID: {', '.join(outs)}")
    print("\n  Not investment advice. Signals are mechanical; confirm on your own chart.\n")


def write_csv(path: str, scores) -> None:
    cols = ["rank", "ticker", "name", "last", "ret_1m", "ret_3m", "ret_6m", "rs_3m",
            "momentum", "uptrend", "above_sma50", "above_sma200", "flow_bull",
            "net_call_premium", "score", "signal", "entry", "stop", "target"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in scores:
            w.writerow([getattr(s, c) for c in cols])
    print(f"  wrote {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SPDR sector rotation scanner")
    parser.add_argument("--top", type=int, default=3, help="how many sectors to rotate into")
    parser.add_argument("--bottom", type=int, default=3, help="how many to flag rotate-out")
    parser.add_argument("--timeframe", default="1Y", help="OHLC history window (e.g. 1Y, 2Y)")
    parser.add_argument("--no-flow", action="store_true", help="skip options-flow confirmation")
    parser.add_argument("--csv", metavar="PATH", help="write results to a CSV file")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        client = UWClient()
    except UWError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # benchmark first (needed for relative strength + regime)
    try:
        bench = client.daily_ohlc(BENCHMARK, args.timeframe)
    except UWError as e:
        print(f"error fetching {BENCHMARK}: {e}", file=sys.stderr)
        return 1
    bench_closes = [float(c["close"]) for c in bench if c.get("close") is not None]

    regime, detail = market_regime(bench)
    risk_off = regime == "RISK-OFF"

    scores = []
    for ticker in SECTOR_ETFS:
        try:
            candles = client.daily_ohlc(ticker, args.timeframe)
        except UWError as e:
            print(f"  warn: {ticker} skipped ({e})", file=sys.stderr)
            continue
        sc = build_score(ticker, candles, bench_closes)
        if sc:
            scores.append(sc)

    if not scores:
        print("error: no sector data returned", file=sys.stderr)
        return 1

    use_flow = not args.no_flow
    if use_flow:
        try:
            add_flow_confirmation(client, scores)
        except UWError as e:
            print(f"  warn: flow confirmation unavailable ({e})", file=sys.stderr)
            use_flow = False

    rank_sectors(scores, top_n=args.top, bottom_n=args.bottom, risk_off=risk_off)
    print_report(regime, detail, scores, use_flow)

    if args.csv:
        write_csv(args.csv, scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
