#!/usr/bin/env python3
"""Render the daily swing dashboard from Unusual Whales MCP data files.

This is the token-free path used by the automated morning Routine. Instead of
calling the REST API (which needs UW_TOKEN), it reads per-ticker JSON files
saved from the `get_ticker_ohlc_latest_or_date` MCP tool — each file has daily
OHLC *and* per-day options-flow (bullish/bearish premium) — and renders the
same HTML dashboard as `dashboard.py`.

Expected layout:
    <data-dir>/SPY.json
    <data-dir>/XLK.json
    ... one file per ticker (SPY + the 11 SPDR sector ETFs) ...

Each file is the raw MCP response: {"result": [ {date, open, high, low, close,
bullish_premium, bearish_premium, ...}, ... ]} (newest-first or oldest-first;
this script sorts by date).

Usage:
    python render_from_mcp.py --data-dir data --out dashboard.html
    python render_from_mcp.py --data-dir data --top 4 --no-flow
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

from rotation import (
    SECTOR_ETFS, BENCHMARK,
    build_score, rank_sectors, market_regime,
)
from dashboard import render_html, sparkline


def _rows_from_file(path: str) -> list[dict]:
    with open(path) as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return rows if isinstance(rows, list) else []


def _to_candles(rows: list[dict]) -> list[dict]:
    """Normalise MCP rows -> candle dicts sorted oldest->newest."""
    candles = []
    for r in rows:
        try:
            candles.append({
                "date": str(r["date"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "bullish_premium": _num(r.get("bullish_premium")),
                "bearish_premium": _num(r.get("bearish_premium")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["date"])
    return candles


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _flow_from_candles(candles: list[dict], window: int = 5) -> tuple[bool | None, float]:
    """Bullish if summed bullish premium > bearish over the recent window."""
    recent = candles[-window:]
    bull = sum(c.get("bullish_premium", 0.0) for c in recent)
    bear = sum(c.get("bearish_premium", 0.0) for c in recent)
    if bull == 0.0 and bear == 0.0:
        return None, 0.0
    return bull > bear, bull - bear


def _find_file(data_dir: str, ticker: str) -> str | None:
    for name in (f"{ticker}.json", f"{ticker.lower()}.json", f"{ticker.upper()}.json"):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            return p
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render dashboard from MCP data files")
    parser.add_argument("--data-dir", default="data", help="dir with <TICKER>.json files")
    parser.add_argument("--out", default="dashboard.html")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--bottom", type=int, default=3)
    parser.add_argument("--no-flow", action="store_true")
    args = parser.parse_args(argv)

    bench_path = _find_file(args.data_dir, BENCHMARK)
    if not bench_path:
        print(f"error: missing {BENCHMARK}.json in {args.data_dir}", file=sys.stderr)
        return 1
    bench_candles = _to_candles(_rows_from_file(bench_path))
    if not bench_candles:
        print(f"error: no usable rows in {bench_path}", file=sys.stderr)
        return 1
    bench_closes = [c["close"] for c in bench_candles]
    regime, detail = market_regime(bench_candles)
    risk_off = regime == "RISK-OFF"

    use_flow = not args.no_flow
    scores = []
    spark_by_ticker: dict[str, str] = {}
    up_col, dn_col = "#0ca30c", "#d03b3b"
    missing = []
    for ticker in SECTOR_ETFS:
        path = _find_file(args.data_dir, ticker)
        if not path:
            missing.append(ticker)
            continue
        candles = _to_candles(_rows_from_file(path))
        sc = build_score(ticker, candles, bench_closes)
        if not sc:
            continue
        if use_flow:
            sc.flow_bull, sc.net_call_premium = _flow_from_candles(candles)
        scores.append(sc)
        closes = [c["close"] for c in candles[-30:]]
        spark_by_ticker[ticker] = sparkline(closes, up_col, dn_col)

    if missing:
        print(f"  warn: no data file for {', '.join(missing)}", file=sys.stderr)
    if not scores:
        print("error: no sector data could be rendered", file=sys.stderr)
        return 1

    rank_sectors(scores, top_n=args.top, bottom_n=args.bottom, risk_off=risk_off)

    generated = _dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    doc = render_html(scores, regime, detail, use_flow, spark_by_ticker, generated)
    with open(args.out, "w") as fh:
        fh.write(doc)
    print(f"  wrote {args.out}  ({len(scores)} sectors, regime {regime})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
