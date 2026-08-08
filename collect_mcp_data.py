#!/usr/bin/env python3
"""Collect Unusual Whales MCP tool-result files into a dashboard data dir.

The automated Routine calls the `get_ticker_ohlc_latest_or_date` MCP tool for
each ticker. Large results are written to per-call files under the session's
`tool-results/` directory. This script scans those files, routes each to
`<dest>/<TICKER>.json` by the `ticker` field inside the data (newest file wins),
and reports what it found — so the Routine just runs this, then render_from_mcp.

Usage:
    python collect_mcp_data.py --dest data
    python collect_mcp_data.py --dest data --search /root/.claude/projects
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

WANT = ["SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
FILE_GLOB = "mcp-Unusual_Whales-get_ticker_ohlc_latest_or_date-*.txt"


def _rows(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        r = payload.get("result") or payload.get("data") or []
        return r if isinstance(r, list) else []
    return payload if isinstance(payload, list) else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Collect MCP OHLC files into a data dir")
    ap.add_argument("--dest", default="data")
    ap.add_argument("--search", default="/root/.claude/projects",
                    help="root to search for tool-results files")
    args = ap.parse_args(argv)

    os.makedirs(args.dest, exist_ok=True)
    files = sorted(glob.glob(os.path.join(args.search, "**", "tool-results", FILE_GLOB),
                             recursive=True))
    # newest filename (later timestamp) wins per ticker
    routed: dict[str, str] = {}
    for f in files:
        rows = _rows(f)
        if not rows:
            continue
        tk = str(rows[0].get("ticker", "")).upper()
        if tk in WANT:
            routed[tk] = f

    written = []
    for tk in WANT:
        if tk in routed:
            with open(routed[tk]) as src, open(os.path.join(args.dest, f"{tk}.json"), "w") as dst:
                dst.write(src.read())
            written.append(tk)

    missing = [t for t in WANT if t not in written]
    print(f"  collected {len(written)}/{len(WANT)} tickers into {args.dest}/")
    if missing:
        print(f"  missing: {', '.join(missing)}", file=sys.stderr)
    return 0 if "SPY" in written else 1


if __name__ == "__main__":
    raise SystemExit(main())
