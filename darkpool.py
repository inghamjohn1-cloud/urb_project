#!/usr/bin/env python3
"""Per-ETF dark-pool block-print view for the sector ETFs.

Aggregates recent off-exchange (dark pool / TRF) prints from Unusual Whales
`get_dark_pool_trades` (one call per ETF, ticker_symbol=<ETF>) into a per-sector
summary: total block premium, print count, the largest block, how much of the
day's volume printed off-exchange, and an accumulation-vs-distribution lean
(premium-weighted price vs the NBBO midpoint). Renders a self-contained HTML
report that matches the dashboard's theme.

Data files (as returned by the MCP tool, one per ETF):
    <data-dir>/_dp_SPY.json, _dp_XLK.json, ...   ({"result":[{...prints...}]})

Usage:
    python darkpool.py --data-dir data --out darkpool.html
    python darkpool.py --data-dir data --min-premium 1000000   # only big blocks
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import html
import json
import os
import sys

from rotation import SECTOR_ETFS, BENCHMARK
from dashboard import CSS, TOGGLE_JS, _money

WANT = [BENCHMARK] + list(SECTOR_ETFS)
FILE_GLOB = "mcp-Unusual_Whales-get_dark_pool_trades-*.txt"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prints(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return rows if isinstance(rows, list) else []


def aggregate(prints: list[dict], min_premium: float = 0.0) -> dict | None:
    """Summarise a ticker's dark-pool prints."""
    rows = []
    for p in prints:
        prem = _f(p.get("premium"))
        size = _f(p.get("size"))
        price = _f(p.get("price"))
        if prem is None or size is None or price is None:
            continue
        if prem < min_premium:
            continue
        rows.append(p)
    if not rows:
        return None

    total_prem = sum(_f(p["premium"]) for p in rows)
    total_size = sum(_f(p["size"]) for p in rows)
    day_vol = max((_f(p.get("volume")) or 0) for p in rows)
    avg30 = next((_f(p.get("avg30_volume")) for p in rows if _f(p.get("avg30_volume"))), None)

    # premium-weighted accumulation lean: price vs NBBO midpoint
    lean_num, lean_den = 0.0, 0.0
    for p in rows:
        bid, ask, price, prem = _f(p.get("nbbo_bid")), _f(p.get("nbbo_ask")), _f(p["price"]), _f(p["premium"])
        if bid and ask and ask > bid:
            mid = (bid + ask) / 2.0
            sign = 1.0 if price > mid else (-1.0 if price < mid else 0.0)
            lean_num += prem * sign
            lean_den += prem
    buy_lean = (lean_num / lean_den) if lean_den else 0.0

    biggest = max(rows, key=lambda p: _f(p["premium"]))
    return {
        "count": len(rows),
        "total_prem": total_prem,
        "total_size": total_size,
        "day_vol": day_vol,
        "avg30": avg30,
        "dp_pct_day": (total_size / day_vol) if day_vol else None,
        "buy_lean": buy_lean,
        "largest_prem": _f(biggest["premium"]),
        "largest_size": _f(biggest["size"]),
        "largest_time": str(biggest.get("executed_at", ""))[:16].replace("T", " "),
        "prints": sorted(rows, key=lambda p: _f(p["premium"]), reverse=True),
    }


def collect(data_dir: str, search: str | None) -> dict[str, list[dict]]:
    """Load per-ETF dark-pool files: explicit data/_dp_<T>.json, or route
    auto-saved tool-result files by the ticker field inside each row."""
    out: dict[str, list[dict]] = {}
    for tk in WANT:
        p = os.path.join(data_dir, f"_dp_{tk}.json")
        if os.path.exists(p):
            try:
                out[tk] = _prints(json.load(open(p)))
            except (OSError, json.JSONDecodeError):
                pass
    if search:
        for f in sorted(glob.glob(os.path.join(search, "**", "tool-results", FILE_GLOB), recursive=True)):
            try:
                rows = _prints(json.load(open(f)))
            except (OSError, json.JSONDecodeError):
                continue
            if not rows:
                continue
            tk = str(rows[0].get("ticker", "")).upper()
            if tk in WANT:
                out[tk] = rows  # newest filename wins (sorted asc)
    return out


def _lean_chip(lean: float) -> str:
    if lean >= 0.15:
        return f'<span class="chip up">▲ accumulation</span>'
    if lean <= -0.15:
        return f'<span class="chip dn">▼ distribution</span>'
    return '<span class="chip na">· mixed</span>'


def render(rows: list[tuple[str, dict]], generated: str) -> str:
    body = []
    for tk, a in rows:
        name = SECTOR_ETFS.get(tk, {}).get("name", "Benchmark" if tk == BENCHMARK else tk)
        dp_pct = f'{a["dp_pct_day"]*100:.0f}%' if a["dp_pct_day"] is not None else "–"
        body.append(f"""
        <tr>
          <td class="l"><span class="tk">{html.escape(tk)}</span><br><span class="nm">{html.escape(name)}</span></td>
          <td class="tnum">{a['count']}</td>
          <td class="tnum">{_money(a['total_prem'])}</td>
          <td class="tnum">{_money(a['largest_prem'])}<br><span class="nm">{a['largest_size']:,.0f} sh</span></td>
          <td class="tnum">{dp_pct}</td>
          <td>{_lean_chip(a['buy_lean'])}</td>
        </tr>""")

    # notable individual blocks across all sectors
    all_prints = []
    for tk, a in rows:
        for p in a["prints"][:3]:
            all_prints.append((tk, p))
    all_prints.sort(key=lambda t: _f(t[1]["premium"]), reverse=True)
    notable = []
    for tk, p in all_prints[:12]:
        bid, ask, price = _f(p.get("nbbo_bid")), _f(p.get("nbbo_ask")), _f(p["price"])
        lean = ""
        if bid and ask and ask > bid:
            mid = (bid + ask) / 2
            lean = '<span class="chip up">↑ above mid</span>' if price > mid else (
                '<span class="chip dn">↓ below mid</span>' if price < mid else '<span class="chip na">· at mid</span>')
        when = str(p.get("executed_at", ""))[:16].replace("T", " ")
        notable.append(f"""
        <tr><td class="l tk">{html.escape(tk)}</td>
        <td class="tnum">{_money(_f(p['premium']))}</td>
        <td class="tnum">{_f(p['size']):,.0f}</td>
        <td class="tnum">{_f(p['price']):.2f}</td>
        <td>{lean}</td>
        <td class="l nm">{html.escape(when)}</td></tr>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sector ETF Dark-Pool Blocks</title>
<script>{TOGGLE_JS}</script>
<style>{CSS}
.chip.up{{color:var(--good-ink)}} .chip.dn{{color:var(--crit)}} .chip.na{{color:var(--muted)}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div><h1>Sector ETF — Dark-Pool Blocks</h1>
      <div class="sub">Off-exchange (TRF) prints · generated {html.escape(generated)}</div></div>
    <button class="toggle" onclick="__t()">◐ Theme</button>
  </header>

  <div class="card">
    <h2>By sector ETF (sorted by dark-pool premium)</h2>
    <div class="scroll"><table>
      <thead><tr><th class="l">ETF</th><th>Prints</th><th>DP premium</th>
        <th>Largest block</th><th>% day vol</th><th class="l">Lean</th></tr></thead>
      <tbody>{"".join(body)}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>Notable blocks</h2>
    <div class="scroll"><table>
      <thead><tr><th class="l">ETF</th><th>Premium</th><th>Size</th><th>Price</th>
        <th class="l">vs NBBO mid</th><th class="l">Executed</th></tr></thead>
      <tbody>{"".join(notable)}</tbody>
    </table></div>
  </div>

  <footer>
    Dark-pool / TRF block prints from Unusual Whales. "Lean" is premium-weighted price vs the
    NBBO midpoint — a rough accumulation (above mid) vs distribution (below mid) read; many blocks
    print at mid. <strong>Not investment advice.</strong>
  </footer>
</div>
</body>
</html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sector ETF dark-pool block view")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="darkpool.html")
    ap.add_argument("--min-premium", type=float, default=0.0,
                    help="ignore prints below this dollar premium")
    ap.add_argument("--search", default="/root/.claude/projects",
                    help="also route auto-saved tool-result files from here")
    args = ap.parse_args(argv)

    raw = collect(args.data_dir, args.search)
    aggregated = []
    for tk in WANT:
        if tk not in raw:
            continue
        a = aggregate(raw[tk], args.min_premium)
        if a:
            aggregated.append((tk, a))
    if not aggregated:
        print("error: no dark-pool data found", file=sys.stderr)
        return 1

    aggregated.sort(key=lambda t: t[1]["total_prem"], reverse=True)
    generated = _dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    with open(args.out, "w") as fh:
        fh.write(render(aggregated, generated))
    print(f"  wrote {args.out}  ({len(aggregated)} ETFs with dark-pool prints)")
    top = ", ".join(f"{tk} {_money(a['total_prem'])}" for tk, a in aggregated[:5])
    print(f"SUMMARY: top DP premium — {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
