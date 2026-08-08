#!/usr/bin/env python3
"""Render the daily swing dashboard from Massive market-data files.

Massive is the price/liquidity engine: adjusted daily OHLC from the aggregates
endpoint drives momentum / relative strength / trend / ATR, and the live
snapshot adds a real-time "Live" column (today's % change) — deeper and more
live than a prior-close-only view.

Expected files in <data-dir> (as returned by Massive's call_api, which emits
CSV):
    <data-dir>/SPY.csv, XLK.csv, ... one daily-aggs file per ticker
        columns include: o, h, l, c, t (ms epoch)  [v, vw, n optional]
    <data-dir>/_snapshot.csv   (optional) live snapshot for all tickers
        columns include: ticker, session_price, session_change_percent,
        session_early_trading_change_percent, session_regular_trading_change_percent

Usage:
    python render_from_massive.py --data-dir data --out dashboard.html
    python render_from_massive.py --data-dir data --no-live
"""

from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import io
import json
import os
import sys

from rotation import (
    SECTOR_ETFS, BENCHMARK,
    build_score, rank_sectors, market_regime,
)
from dashboard import render_html, sparkline

SOURCE_NOTE = "Prices and live quotes from Massive; daily OHLC is split-adjusted."


# --------------------------------------------------------------------------
# Parsing (Massive call_api returns CSV; also tolerate JSON shapes)
# --------------------------------------------------------------------------
def _load_text(path: str) -> str:
    with open(path) as fh:
        raw = fh.read()
    s = raw.strip()
    if s[:1] in "{[":
        try:
            payload = json.loads(s)
        except json.JSONDecodeError:
            return raw
        if isinstance(payload, dict):
            if isinstance(payload.get("result"), str):
                return payload["result"]
            if isinstance(payload.get("results"), list):
                return json.dumps(payload["results"])
            if isinstance(payload.get("result"), list):
                return json.dumps(payload["result"])
        return raw
    return raw


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_aggs(path: str) -> list[dict]:
    """Return candles [{date, open, high, low, close}] sorted oldest->newest."""
    text = _load_text(path).strip()
    rows: list[dict] = []
    if text[:1] == "[":
        for r in json.loads(text):
            rows.append({"o": r.get("o"), "h": r.get("h"), "l": r.get("l"),
                         "c": r.get("c"), "t": r.get("t")})
    else:
        reader = _csv.DictReader(io.StringIO(text))
        for r in reader:
            rows.append(r)

    candles = []
    for r in rows:
        o, h, l, c, t = _num(r.get("o")), _num(r.get("h")), _num(r.get("l")), _num(r.get("c")), _num(r.get("t"))
        if None in (o, h, l, c, t):
            continue
        date = _dt.datetime.utcfromtimestamp(t / 1000.0).strftime("%Y-%m-%d")
        candles.append({"date": date, "open": o, "high": h, "low": l, "close": c})
    candles.sort(key=lambda x: x["date"])
    return candles


def parse_snapshot(path: str) -> dict[str, dict]:
    """Return {ticker: {price, chg}} from a Massive snapshot file."""
    if not os.path.exists(path):
        return {}
    text = _load_text(path).strip()
    out: dict[str, dict] = {}
    records: list[dict]
    if text[:1] == "[":
        records = json.loads(text)
        # flatten nested session.* if present
        flat = []
        for r in records:
            sess = r.get("session", {}) if isinstance(r.get("session"), dict) else {}
            flat.append({
                "ticker": r.get("ticker"),
                "session_price": sess.get("price"),
                "session_change_percent": sess.get("change_percent"),
                "session_regular_trading_change_percent": sess.get("regular_trading_change_percent"),
                "session_early_trading_change_percent": sess.get("early_trading_change_percent"),
                "session_late_trading_change_percent": sess.get("late_trading_change_percent"),
            })
        records = flat
    else:
        records = list(_csv.DictReader(io.StringIO(text)))

    for r in records:
        tk = str(r.get("ticker", "")).upper()
        if not tk:
            continue
        # prefer regular change, else pre/post-market, else the rollup
        chg = None
        for key in ("session_regular_trading_change_percent",
                    "session_change_percent",
                    "session_early_trading_change_percent",
                    "session_late_trading_change_percent"):
            v = _num(r.get(key))
            if v not in (None, 0.0):
                chg = v
                break
        if chg is None:
            chg = _num(r.get("session_change_percent"))
        out[tk] = {"price": _num(r.get("session_price")), "chg": chg}
    return out


def _find(data_dir: str, ticker: str) -> str | None:
    for ext in ("csv", "json"):
        for name in (f"{ticker}.{ext}", f"{ticker.upper()}.{ext}", f"{ticker.lower()}.{ext}"):
            p = os.path.join(data_dir, name)
            if os.path.exists(p):
                return p
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render dashboard from Massive data files")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--bottom", type=int, default=3)
    ap.add_argument("--no-live", action="store_true", help="skip the live snapshot column")
    args = ap.parse_args(argv)

    bench_path = _find(args.data_dir, BENCHMARK)
    if not bench_path:
        print(f"error: missing {BENCHMARK} data in {args.data_dir}", file=sys.stderr)
        return 1
    bench_candles = parse_aggs(bench_path)
    if not bench_candles:
        print(f"error: no usable rows in {bench_path}", file=sys.stderr)
        return 1
    bench_closes = [c["close"] for c in bench_candles]
    regime, detail = market_regime(bench_candles)
    risk_off = regime == "RISK-OFF"

    live = {} if args.no_live else parse_snapshot(
        _find(args.data_dir, "_snapshot") or os.path.join(args.data_dir, "_snapshot.csv"))
    use_live = bool(live)

    scores = []
    spark_by_ticker: dict[str, str] = {}
    up_col, dn_col = "#0ca30c", "#d03b3b"
    missing = []
    for ticker in SECTOR_ETFS:
        path = _find(args.data_dir, ticker)
        if not path:
            missing.append(ticker)
            continue
        candles = parse_aggs(path)
        sc = build_score(ticker, candles, bench_closes)
        if not sc:
            continue
        if ticker in live:
            sc.live_price = live[ticker].get("price")
            sc.live_chg = live[ticker].get("chg")
        scores.append(sc)
        spark_by_ticker[ticker] = sparkline([c["close"] for c in candles[-30:]], up_col, dn_col)

    if missing:
        print(f"  warn: no data file for {', '.join(missing)}", file=sys.stderr)
    if not scores:
        print("error: no sector data could be rendered", file=sys.stderr)
        return 1

    rank_sectors(scores, top_n=args.top, bottom_n=args.bottom, risk_off=risk_off)

    generated = _dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    doc = render_html(scores, regime, detail, use_flow=False,
                      spark_by_ticker=spark_by_ticker, generated=generated,
                      live_mode=use_live, source_note=SOURCE_NOTE)
    with open(args.out, "w") as fh:
        fh.write(doc)
    print(f"  wrote {args.out}  ({len(scores)} sectors, regime {regime}, live={'on' if use_live else 'off'})")

    rotate_in = [s.ticker for s in scores if s.signal == "ROTATE IN"]
    rotate_out = [s.ticker for s in scores if s.signal == "ROTATE OUT"]
    print("SUMMARY: regime=" + regime
          + " | ROTATE IN: " + (", ".join(rotate_in) or "none")
          + " | ROTATE OUT: " + (", ".join(rotate_out) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
