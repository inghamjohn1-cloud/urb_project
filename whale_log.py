#!/usr/bin/env python3
"""Forward-test logger: append today's whale read for a ticker to a journal.

This builds the dataset that cannot be backtested: daily options-flow state
(from Unusual Whales ticker history) plus same-day dark-pool block activity.
One row per ticker per trading day, appended to logs/whale_journal.csv, which
the morning routine commits so the record survives ephemeral containers.

Inputs (raw MCP tool outputs saved to files):
  --history   get_ticker_ohlc_latest_or_date result ({"result":[...]}), >=70 rows
  --darkpool  get_dark_pool_trades result for the ticker (optional)

Usage:
    python whale_log.py --ticker TSLA --history data/_log_TSLA.json \
        --darkpool data/_dp_TSLA.json --journal logs/whale_journal.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import statistics
import sys

COLUMNS = ["date", "ticker", "close", "ret_21d", "ret_63d",
           "net_prem_5d", "flow_z", "flow_state",
           "dp_prints", "dp_premium", "dp_largest", "dp_at_or_above_close",
           "gex_call_gamma", "gex_put_gamma", "gex_net_gamma",
           "days_to_earnings", "earnings_date"]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _rows(path):
    with open(path) as fh:
        payload = json.load(fh)
    r = payload.get("result", payload if isinstance(payload, list) else [])
    return r if isinstance(r, list) else []


def flow_entry(history_rows, win=5, base=60):
    rows = sorted(history_rows, key=lambda r: r["date"])
    close = [_f(r.get("close")) for r in rows]
    net = [_f(r.get("net_premium")) for r in rows]
    n = len(rows)
    if n < base + win + 1:
        raise SystemExit(f"error: need >={base+win+1} history rows, got {n}")
    s5 = statistics.mean(net[-win:])
    hist = [statistics.mean(net[j - win + 1:j + 1]) for j in range(n - base - win, n - win)]
    sd = statistics.pstdev(hist) or 1.0
    z = (s5 - statistics.mean(hist)) / sd
    state = "euphoria" if z > 1.5 else ("capitulation" if z < -1.5 else "normal")
    return {
        "date": str(rows[-1]["date"])[:10],
        "close": round(close[-1], 2),
        "ret_21d": round(close[-1] / close[-22] - 1, 4) if n > 22 else "",
        "ret_63d": round(close[-1] / close[-64] - 1, 4) if n > 64 else "",
        "net_prem_5d": round(s5, 0),
        "flow_z": round(z, 2),
        "flow_state": state,
    }


def darkpool_entry(dp_rows, close, have_data):
    if not have_data:
        # no dark-pool fetch for this day: blank, NOT zero (zero means "fetched, none found")
        return {"dp_prints": "", "dp_premium": "", "dp_largest": "", "dp_at_or_above_close": ""}
    prints = [r for r in dp_rows if _f(r.get("premium")) > 0]
    if not prints:
        return {"dp_prints": 0, "dp_premium": 0, "dp_largest": 0, "dp_at_or_above_close": ""}
    prem = [_f(r["premium"]) for r in prints]
    at_or_above = sum(1 for r in prints if _f(r.get("price")) >= close * 0.999)
    return {
        "dp_prints": len(prints),
        "dp_premium": round(sum(prem), 0),
        "dp_largest": round(max(prem), 0),
        "dp_at_or_above_close": round(at_or_above / len(prints), 2),
    }


def earnings_entry(history_rows, entry_date):
    """Find next earnings date after entry_date from UW history rows."""
    blank = {"days_to_earnings": "", "earnings_date": ""}
    future = []
    for r in history_rows:
        e = r.get("earnings")
        if not e or not isinstance(e, dict):
            continue
        row_date = str(r.get("date", ""))[:10]
        if row_date > entry_date:
            future.append(row_date)
    if not future:
        return blank
    next_date = min(future)
    try:
        delta = (_dt.date.fromisoformat(next_date) - _dt.date.fromisoformat(entry_date)).days
    except ValueError:
        return blank
    return {"days_to_earnings": delta, "earnings_date": next_date}


def gex_entry(gex_rows, entry_date):
    """Dealer gamma exposure for the entry's date (or blank if absent)."""
    blank = {"gex_call_gamma": "", "gex_put_gamma": "", "gex_net_gamma": ""}
    if not gex_rows:
        return blank
    row = None
    for r in sorted(gex_rows, key=lambda r: str(r.get("date", ""))):
        if str(r.get("date", ""))[:10] <= entry_date:
            row = r
    if row is None:
        return blank
    cg, pg = _f(row.get("call_gamma")), _f(row.get("put_gamma"))
    if cg is None or pg is None:
        return blank
    return {"gex_call_gamma": round(cg, 0), "gex_put_gamma": round(pg, 0),
            "gex_net_gamma": round(cg + pg, 0)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--darkpool", default=None)
    ap.add_argument("--gex", default=None,
                    help="get_greek_exposure_by_ticker result JSON (optional)")
    ap.add_argument("--journal", default="logs/whale_journal.csv")
    args = ap.parse_args(argv)

    entry = flow_entry(_rows(args.history))
    entry["ticker"] = args.ticker.upper()

    dp = []
    have_dp = False
    if args.darkpool and os.path.exists(args.darkpool):
        try:
            dp = _rows(args.darkpool)
            have_dp = True
        except (OSError, json.JSONDecodeError):
            pass
    entry.update(darkpool_entry(dp, entry["close"], have_dp))

    gex = []
    if args.gex and os.path.exists(args.gex):
        try:
            gex = _rows(args.gex)
        except (OSError, json.JSONDecodeError):
            pass
    entry.update(gex_entry(gex, entry["date"]))
    entry.update(earnings_entry(_rows(args.history), entry["date"]))

    os.makedirs(os.path.dirname(args.journal) or ".", exist_ok=True)
    exists = os.path.exists(args.journal)
    # dedupe: skip if this (date, ticker) is already logged
    if exists:
        with open(args.journal) as fh:
            for row in csv.DictReader(fh):
                if row.get("date") == entry["date"] and row.get("ticker") == entry["ticker"]:
                    print(f"  {entry['ticker']} {entry['date']} already logged, skipping")
                    return 0
    with open(args.journal, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(entry)
    dp_note = (f"DP ${entry['dp_premium']/1e6:.0f}M in {entry['dp_prints']} prints"
               if entry["dp_prints"] != "" else "DP n/a")
    if entry["gex_net_gamma"] != "":
        gex_note = f"GEX {'+' if entry['gex_net_gamma'] >= 0 else ''}{entry['gex_net_gamma']/1e6:.2f}M ({'positive' if entry['gex_net_gamma'] >= 0 else 'NEGATIVE'} gamma)"
    else:
        gex_note = "GEX n/a"
    print(f"  logged {entry['ticker']} {entry['date']}: close {entry['close']}, "
          f"flow_z {entry['flow_z']} ({entry['flow_state']}), {dp_note}, {gex_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
