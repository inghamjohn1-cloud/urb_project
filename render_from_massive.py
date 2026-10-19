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
import whale as whale_mod

SOURCE_NOTE = "Prices/live quotes from Massive; whale layer (options flow + accumulation) from Unusual Whales."


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


def watchlist_card(journal_path: str) -> str:
    """Render the whale-watchlist panel from the latest journal row per ticker."""
    import csv as _csv
    import html as _html
    try:
        rows = list(_csv.DictReader(open(journal_path)))
    except OSError:
        return ""
    latest = {}
    for r in rows:
        if r.get("ticker") and (r["ticker"] not in latest or r["date"] >= latest[r["ticker"]]["date"]):
            latest[r["ticker"]] = r
    if not latest:
        return ""

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def state_chip(state, z):
        zs = f"{z:+.2f}" if z is not None else "–"
        if state == "euphoria":
            return f'<span class="wchip wdn">🔥 EUPHORIA {zs}</span>'
        if state == "capitulation":
            return f'<span class="wchip wup">🟢 CAPITULATION {zs}</span>'
        return f'<span class="wchip wna">normal {zs}</span>'

    def warn_chip(days, edate):
        note = f" ({edate})" if edate else ""
        return f'<span class="wchip wwarn">⚠️ EARNINGS IN {int(days)}d{note}</span>'

    def gex_chip(g):
        if g is None:
            return '<span class="wchip wna">n/a</span>'
        return (f'<span class="wchip wdn">▼ NEGATIVE {g/1e6:.2f}M</span>' if g < 0
                else f'<span class="wchip wup">▲ positive +{g/1e6:.2f}M</span>')

    def pct(v):
        return f'<td class="tnum {"pos" if v > 0 else ("neg" if v < 0 else "")}">{v*100:+.1f}%</td>' if v is not None else '<td class="tnum">–</td>'

    body = []
    ordered = sorted(latest.values(), key=lambda r: -(num(r.get("flow_z")) or 0))
    for r in ordered:
        dp = num(r.get("dp_premium"))
        if dp:
            dp_cell = f'<span class="wchip wna">${dp/1e6:.0f}M / {r.get("dp_prints")} prints</span>'
        elif r.get("dp_prints") == "0":
            dp_cell = '<span class="wchip wna">none today</span>'
        else:
            dp_cell = '<span class="wchip wna">awaiting first fetch</span>'

        days_to_e = num(r.get("days_to_earnings"))
        edate = r.get("earnings_date", "")
        if days_to_e is not None and 0 <= days_to_e <= 7:
            flow_cell = warn_chip(days_to_e, edate)
        else:
            flow_cell = state_chip(r.get("flow_state"), num(r.get("flow_z")))

        if days_to_e is not None and days_to_e <= 60:
            earn_cell = f'<span class="wchip wwarn">{int(days_to_e)}d — {edate}</span>' if days_to_e <= 14 else f'<span class="wchip wna">{int(days_to_e)}d</span>'
        else:
            earn_cell = '<span class="wchip wna">—</span>'

        body.append(f"""
        <tr>
          <td class="l"><span class="tk">{_html.escape(r['ticker'])}</span></td>
          <td class="tnum">{num(r.get('close')):.2f}</td>
          {pct(num(r.get('ret_21d')))}{pct(num(r.get('ret_63d')))}
          <td>{flow_cell}</td>
          <td>{gex_chip(num(r.get('gex_net_gamma')))}</td>
          <td class="tnum">{dp_cell}</td>
          <td>{earn_cell}</td>
        </tr>""")
    asof = max(r["date"] for r in latest.values())
    return f"""
  <style>
  .wchip{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:650;
    background:var(--surface-2);border:1px solid var(--border);color:var(--ink-2)}}
  .wchip.wup{{color:var(--good-ink);border-color:var(--good-ink)}}
  .wchip.wdn{{color:var(--crit);border-color:var(--crit)}}
  .wchip.wna{{color:var(--ink-2)}}
  .wchip.wwarn{{color:#b45309;border-color:#b45309}}
  </style>
  <div class="card">
    <h2>Whale watchlist — forward test (as of {_html.escape(asof)})</h2>
    <div class="scroll"><table>
      <thead><tr><th class="l">Ticker</th><th>Close</th><th>21d</th><th>63d</th>
        <th class="l">Options flow</th><th class="l">Dealer gamma</th><th>Dark pool</th><th>Earnings</th></tr></thead>
      <tbody>{"".join(body)}</tbody>
    </table></div>
    <div class="empty">🟢 capitulation = your buy-watch condition · 🔥 euphoria = not a buy day ·
    NEG gamma = dealers amplify moves · ⚠️ earnings &lt;7 days = flow is hedging noise, not signal.
    Signals under forward test — not proven, not advice.</div>
  </div>
"""


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
    ap.add_argument("--whale-file", default=None,
                    help="get_market_sector_etfs JSON for the whale layer (default data/_whale.json)")
    ap.add_argument("--whale-weight", type=float, default=12.0,
                    help="max points the conviction layer adds/removes from a sector's score")
    ap.add_argument("--no-whale", action="store_true", help="skip the whale conviction layer")
    ap.add_argument("--journal", default="logs/whale_journal.csv",
                    help="whale journal CSV for the watchlist panel ('' to skip)")
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

    # whale / smart-money conviction layer (optional)
    use_whale = False
    if not args.no_whale:
        wpath = args.whale_file or _find(args.data_dir, "_whale") or os.path.join(args.data_dir, "_whale.json")
        if os.path.exists(wpath):
            try:
                with open(wpath) as fh:
                    payload = json.load(fh)
                wmap = whale_mod.parse_sector_etfs(payload)
                use_whale = whale_mod.attach(scores, wmap) > 0
            except (json.JSONDecodeError, OSError) as e:
                print(f"  warn: whale layer unavailable ({e})", file=sys.stderr)

    rank_sectors(scores, top_n=args.top, bottom_n=args.bottom, risk_off=risk_off,
                 whale_weight=args.whale_weight if use_whale else 0.0)

    generated = _dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    note = SOURCE_NOTE if use_whale else "Prices and live quotes from Massive; daily OHLC is split-adjusted."
    doc = render_html(scores, regime, detail, use_flow=False,
                      spark_by_ticker=spark_by_ticker, generated=generated,
                      live_mode=use_live, whale_mode=use_whale, source_note=note)
    if args.journal:
        card = watchlist_card(args.journal)
        if card:
            doc = doc.replace("<footer>", card + "\n  <footer>", 1)
    with open(args.out, "w") as fh:
        fh.write(doc)
    print(f"  wrote {args.out}  ({len(scores)} sectors, regime {regime}, "
          f"live={'on' if use_live else 'off'}, whale={'on' if use_whale else 'off'})")

    rotate_in = [s.ticker for s in scores if s.signal == "ROTATE IN"]
    rotate_out = [s.ticker for s in scores if s.signal == "ROTATE OUT"]
    print("SUMMARY: regime=" + regime
          + " | ROTATE IN: " + (", ".join(rotate_in) or "none")
          + " | ROTATE OUT: " + (", ".join(rotate_out) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
