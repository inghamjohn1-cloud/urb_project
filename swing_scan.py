#!/usr/bin/env python3
"""Single-name swing scanner: unusual options flow + dark pool + technicals.

Pipeline
--------
    1. Unusual Whales  -> market-wide flow alerts matching the swing filter
                          ($250k+ premium, 14-90 DTE, sweeps/opening, single
                          names only), rolled up per ticker.
    2. Unusual Whales  -> recent dark-pool blocks for each surviving ticker.
    3. Massive         -> 400 days of adjusted daily bars per ticker, from
                          which EMA50/200, RSI(14), ATR(14) and volume are
                          computed (definitions match TradingView).
    4. swing_score     -> hard gates, then a 0-100 composite, tiering, and
                          ATR-based entry/stop/target/size.
    5. Output          -> ranked console table, CSV, and a TradingView
                          watchlist file you can import directly.

Usage
-----
    python swing_scan.py                          # full live scan
    python swing_scan.py --direction long         # long candidates only
    python swing_scan.py --min-premium 500000     # tighter premium floor
    python swing_scan.py --csv swing.csv --watchlist swing.txt
    python swing_scan.py --show-rejects           # why names were dropped
    python swing_scan.py --data-dir data          # offline bars from MCP export
    python swing_scan.py --dry-run                # print the API filters only

Keys (see .env.example):
    UW_TOKEN          Unusual Whales API token
    MASSIVE_API_KEY   Massive API key (not needed with --data-dir)
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys

from uw_client import UWClient, UWError
from massive_client import MassiveClient, MassiveError, load_candles_dir
import swing_flow as flow_mod
import swing_indicators as ind
from swing_score import build_candidate, rank, Candidate
from swing_config import (
    UNIVERSE, FLOW, DARKPOOL, TECHNICAL, RISK, MIN_SCORE, HOLD_PERIOD_DAYS,
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
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


def build_uw_filters(args) -> dict:
    """The exact query the scanner sends to /api/option-trades/flow-alerts.

    This mirrors the saved filter documented in SWING_SCANNER.md section 2 —
    keep the two in sync if you change either.
    """
    f = {
        "min_premium": args.min_premium,
        "min_dte": FLOW["min_dte"],
        "max_dte": FLOW["max_dte"],
        "min_volume_oi_ratio": FLOW["min_volume_oi_ratio"],
        "issue_types": UNIVERSE["issue_types"],
        "min_price": UNIVERSE["min_price"],
        "max_price": UNIVERSE["max_price"],
        "min_marketcap": UNIVERSE["min_marketcap"],
        "min_volume": 500,
        "is_multi_leg": False if FLOW["exclude_multi_leg"] else None,
        "all_opening": True if args.opening_only else None,
        "is_sweep": True if args.sweeps_only else None,
        "limit": args.limit,
    }
    return {k: v for k, v in f.items() if v is not None}


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def fetch_flow(client: UWClient, args, today: _dt.date) -> tuple[dict[str, dict], dict[str, int]]:
    """Pull flow alerts, normalise, gate, and roll up per ticker."""
    raw = client.flow_alerts(**build_uw_filters(args))
    print(f"  fetched {len(raw)} flow alerts")

    passed: list[dict] = []
    dropped: dict[str, int] = {}
    for row in raw:
        a = flow_mod.normalize_alert(row, today)
        if a is None:
            dropped["unparseable"] = dropped.get("unparseable", 0) + 1
            continue
        ok, why = flow_mod.alert_passes(a, today)
        if ok:
            passed.append(a)
        else:
            dropped[why] = dropped.get(why, 0) + 1

    print(f"  {len(passed)} alerts passed the per-alert gates")
    return flow_mod.aggregate_alerts(passed), dropped


def fetch_darkpool(client: UWClient, tickers: list[str],
                   advs: dict[str, float | None], today: _dt.date) -> dict[str, dict]:
    """One dark-pool call per candidate ticker. Failures degrade to no-data."""
    since = (today - _dt.timedelta(days=DARKPOOL["lookback_days"])).isoformat()
    out: dict[str, dict] = {}
    for tk in tickers:
        try:
            prints = client.darkpool_trades(
                tk, limit=200, newer_than=since,
                min_premium=int(DARKPOOL["min_block_premium"]),
            )
        except UWError as e:
            print(f"  warn: dark pool for {tk} unavailable ({e})", file=sys.stderr)
            prints = []
        out[tk] = flow_mod.aggregate_darkpool(prints, advs.get(tk))
    return out


def fetch_technicals(tickers: list[str], args) -> dict[str, dict]:
    """Daily bars from Massive (or an offline export) -> indicator snapshots."""
    candles_by_ticker: dict[str, list[dict]] = {}

    if args.data_dir:
        loaded = load_candles_dir(args.data_dir)
        for tk in tickers:
            if tk in loaded:
                candles_by_ticker[tk] = loaded[tk]
            else:
                print(f"  warn: no bars for {tk} in {args.data_dir}", file=sys.stderr)
    else:
        try:
            massive = MassiveClient()
        except MassiveError as e:
            print(f"error: {e}", file=sys.stderr)
            return {}
        for tk in tickers:
            try:
                candles_by_ticker[tk] = massive.daily_candles(
                    tk, TECHNICAL["history_days"])
            except MassiveError as e:
                print(f"  warn: bars for {tk} unavailable ({e})", file=sys.stderr)

    return {tk: ind.snapshot(c) for tk, c in candles_by_ticker.items() if c}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_report(live: list[Candidate], rejects: list[Candidate],
                 dropped: dict[str, int], args, today: _dt.date) -> None:
    lo, hi = HOLD_PERIOD_DAYS
    print()
    print("=" * 118)
    print(f"  SWING SCANNER — unusual options flow + dark pool + technicals"
          f"          {today.isoformat()}")
    print(f"  premium >= ${args.min_premium:,.0f}   DTE {FLOW['min_dte']}-{FLOW['max_dte']}"
          f"   hold {lo}-{hi} sessions   min score {MIN_SCORE:.0f}")
    print("=" * 118)

    if not live:
        print("\n  No candidates passed the filters today. That is a valid outcome —")
        print("  a day with no qualifying setup is a day you do not need to trade.\n")
    else:
        header = (f"{'#':>2}  {'TKR':<6}{'Dir':<6}{'Tier':<5}{'Score':>6}"
                  f"{'Flow':>6}{'DP':>5}{'Tech':>6}{'Liq':>5}"
                  f"{'Premium':>11}{'DTE':>5}{'Entry':>9}{'Stop':>9}{'T1':>9}{'T2':>9}{'Sh':>6}")
        print(header)
        print("-" * 118)
        for i, c in enumerate(live, 1):
            dte = f"{c.flow['avg_dte']:.0f}" if c.flow.get("avg_dte") else "-"
            print(
                f"{i:>2}  {c.ticker:<6}{c.direction:<6}{c.tier:<5}{c.score:>6.1f}"
                f"{c.flow_score:>6.1f}{c.dp_score:>5.1f}{c.tech_score:>6.1f}{c.liq_score:>5.1f}"
                f"{_money(c.flow['total_premium']):>11}{dte:>5}"
                f"{_p(c.entry):>9}{_p(c.stop):>9}{_p(c.target1):>9}{_p(c.target2):>9}"
                f"{(c.shares if c.shares is not None else '-'):>6}"
            )
        print("-" * 118)

        print("\n  WHY EACH NAME SCORED:")
        for c in live:
            print(f"\n    {c.ticker} ({c.direction}, tier {c.tier}, {c.score:.1f}) "
                  f"— {c.sector or 'sector n/a'}")
            if c.notes:
                print(f"      + {'; '.join(c.notes)}")
            if c.flags:
                print(f"      ! {'; '.join(c.flags)}")
            if c.stop and c.entry:
                risk = abs(c.entry - c.stop)
                print(f"      risk ${risk:.2f}/share"
                      f"  ({risk / c.entry * 100:.1f}%)"
                      f"  budget ${c.risk_dollars:,.0f}")

    if args.show_rejects and rejects:
        print("\n  REJECTED (top 25 by premium):")
        for c in sorted(rejects, key=lambda x: x.flow.get("total_premium", 0),
                        reverse=True)[:25]:
            print(f"    {c.ticker:<6} {_money(c.flow.get('total_premium', 0)):>10}"
                  f"  — {c.rejected}")

    if args.show_rejects and dropped:
        print("\n  ALERTS DROPPED BEFORE SCORING:")
        for why, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>5}  {why}")

    print(f"\n  Position sizing assumes a ${RISK['account_size']:,.0f} account risking "
          f"{RISK['risk_per_trade_pct'] * 100:.2f}% per A-tier idea "
          f"(B = half, C = watchlist only). Edit swing_config.RISK.")
    print("  Mechanical output — confirm every name on your own chart before "
          "committing risk. Not investment advice.\n")


def _money(v: float) -> str:
    if v is None:
        return "-"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _p(v) -> str:
    return f"{v:.2f}" if v is not None else "-"


def write_csv(path: str, live: list[Candidate]) -> None:
    cols = ["ticker", "direction", "tier", "score", "flow_score", "dp_score",
            "tech_score", "liq_score", "sector", "total_premium", "call_premium",
            "put_premium", "bullish_premium", "bearish_premium", "unsided_share",
            "n_alerts", "avg_dte", "aggression", "sweep_share",
            "opening_share", "coherence", "dp_block_premium", "dp_pct_of_adv",
            "dp_lean", "last", "ema_fast", "ema_slow", "rsi", "atr", "volume_ratio",
            "entry", "stop", "target1", "target2", "shares", "notes", "flags"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for c in live:
            w.writerow([
                c.ticker, c.direction, c.tier, c.score, round(c.flow_score, 1),
                round(c.dp_score, 1), round(c.tech_score, 1), round(c.liq_score, 1),
                c.sector,
                round(c.flow.get("total_premium") or 0, 0),
                round(c.flow.get("call_premium") or 0, 0),
                round(c.flow.get("put_premium") or 0, 0),
                round(c.flow.get("bullish_premium") or 0, 0),
                round(c.flow.get("bearish_premium") or 0, 0),
                _r(c.flow.get("unsided_share"), 3),
                c.flow.get("n_alerts"),
                _r(c.flow.get("avg_dte"), 1), _r(c.flow.get("aggression"), 3),
                _r(c.flow.get("sweep_share"), 3), _r(c.flow.get("opening_share"), 3),
                _r(c.flow.get("coherence"), 3),
                round(c.dp.get("block_premium") or 0, 0),
                _r(c.dp.get("pct_of_adv"), 4), _r(c.dp.get("lean"), 3),
                _r(c.tech.get("last"), 2), _r(c.tech.get("ema_fast"), 2),
                _r(c.tech.get("ema_slow"), 2), _r(c.tech.get("rsi"), 1),
                _r(c.tech.get("atr"), 2), _r(c.tech.get("volume_ratio"), 2),
                c.entry, c.stop, c.target1, c.target2, c.shares,
                "; ".join(c.notes), "; ".join(c.flags),
            ])
    print(f"  wrote {path}")


def _r(v, nd):
    return round(v, nd) if isinstance(v, (int, float)) else None


def write_watchlist(path: str, live: list[Candidate]) -> None:
    """TradingView-importable watchlist, sectioned by tier.

    TradingView reads a comma-separated symbol list; lines beginning with ###
    become section headers in the watchlist panel.
    """
    by_tier: dict[str, list[str]] = {}
    for c in live:
        by_tier.setdefault(c.tier, []).append(f"{c.ticker}")

    parts: list[str] = []
    for tier in ("A", "B", "C"):
        syms = by_tier.get(tier)
        if syms:
            parts.append(f"###SWING {tier}")
            parts.extend(syms)

    with open(path, "w") as fh:
        fh.write(",".join(parts) + "\n")
    print(f"  wrote {path}  (TradingView -> Watchlist -> Import list)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Single-name swing scanner (UW flow + dark pool + Massive technicals)")
    ap.add_argument("--min-premium", type=int, default=FLOW["min_alert_premium"],
                    help=f"per-alert premium floor (default {FLOW['min_alert_premium']:,})")
    ap.add_argument("--limit", type=int, default=500,
                    help="max flow alerts to request from UW")
    ap.add_argument("--direction", choices=["long", "short", "both"], default="both",
                    help="which side to report (default both)")
    ap.add_argument("--top", type=int, default=15, help="max candidates to print")
    ap.add_argument("--sweeps-only", action="store_true",
                    help="request only sweep alerts from UW")
    ap.add_argument("--opening-only", action="store_true",
                    help="request only all-opening alerts from UW")
    ap.add_argument("--allow-earnings", action="store_true",
                    help="do not reject names with earnings inside the hold window")
    ap.add_argument("--no-darkpool", action="store_true",
                    help="skip the dark-pool calls (faster, loses 15 score points)")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="read daily bars from Massive MCP exports instead of REST")
    ap.add_argument("--csv", metavar="PATH", help="write the ranked table to CSV")
    ap.add_argument("--watchlist", metavar="PATH",
                    help="write a TradingView-importable watchlist")
    ap.add_argument("--show-rejects", action="store_true",
                    help="explain what was filtered out and why")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the UW filter payload and exit (no API calls)")
    args = ap.parse_args(argv)

    load_dotenv()
    today = _dt.date.today()

    if args.dry_run:
        print("\n  UW /api/option-trades/flow-alerts query:")
        for k, v in sorted(build_uw_filters(args).items()):
            print(f"    {k:<24} {v}")
        print("\n  UW /api/darkpool/{ticker} query (per candidate):")
        print(f"    {'min_premium':<24} {int(DARKPOOL['min_block_premium'])}")
        print(f"    {'newer_than':<24} "
              f"{(today - _dt.timedelta(days=DARKPOOL['lookback_days'])).isoformat()}")
        print(f"    {'limit':<24} 200\n")
        return 0

    try:
        uw = UWClient()
    except UWError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # 1. Options flow -------------------------------------------------------
    print("\n  [1/4] Unusual Whales — options flow alerts")
    try:
        flows, dropped = fetch_flow(uw, args, today)
    except UWError as e:
        print(f"error fetching flow alerts: {e}", file=sys.stderr)
        return 1

    if args.direction != "both":
        flows = {k: v for k, v in flows.items() if v["direction"] == args.direction}

    # Pre-filter on the ticker-level premium gate so we don't spend API calls
    # on names that cannot possibly qualify.
    flows = {k: v for k, v in flows.items()
             if v["total_premium"] >= FLOW["min_ticker_premium"]}
    tickers = sorted(flows, key=lambda t: flows[t]["total_premium"], reverse=True)
    print(f"  {len(tickers)} tickers cleared the ${FLOW['min_ticker_premium']:,.0f} "
          f"ticker-level premium gate")

    if not tickers:
        print_report([], [], dropped, args, today)
        return 0

    # 2. Technicals ---------------------------------------------------------
    print(f"\n  [2/4] Massive — daily bars for {len(tickers)} tickers")
    techs = fetch_technicals(tickers, args)
    print(f"  built indicator snapshots for {len(techs)} tickers")

    # 3. Dark pool ----------------------------------------------------------
    if args.no_darkpool:
        print("\n  [3/4] dark pool — skipped (--no-darkpool)")
        dps = {tk: {"has_data": False, "n_blocks": 0} for tk in tickers}
    else:
        print(f"\n  [3/4] Unusual Whales — dark-pool blocks for {len(tickers)} tickers")
        advs = {tk: (techs.get(tk, {}).get("avg_volume")
                     or flows[tk].get("avg30_volume")) for tk in tickers}
        dps = fetch_darkpool(uw, tickers, advs, today)
        with_blocks = sum(1 for d in dps.values() if d.get("n_blocks"))
        print(f"  {with_blocks} tickers had qualifying blocks")

    # 4. Score --------------------------------------------------------------
    print("\n  [4/4] scoring")
    scored = [
        build_candidate(flows[tk], techs.get(tk, {}), dps.get(tk, {}),
                        allow_earnings=args.allow_earnings, today=today)
        for tk in tickers
    ]
    live = rank(scored)[:args.top]
    rejects = [c for c in scored if c.rejected]

    print_report(live, rejects, dropped, args, today)

    if args.csv:
        write_csv(args.csv, live)
    if args.watchlist:
        write_watchlist(args.watchlist, live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
