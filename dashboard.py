#!/usr/bin/env python3
"""Daily swing dashboard for the sector-rotation strategy.

Renders a self-contained, mobile-friendly HTML page from live Unusual Whales
data: market regime, the ranked sector table with momentum sparklines and
options-flow confirmation, and ATR-based swing levels (entry/stop/target) for
the rotate-in leaders. Open it on your phone each morning.

Usage:
    python dashboard.py                       # writes dashboard.html
    python dashboard.py --out ~/swing.html
    python dashboard.py --open                # write + open in a browser
    python dashboard.py --no-flow --top 4

Requires an Unusual Whales API token in UW_TOKEN (see .env.example).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import sys
import webbrowser

from uw_client import UWClient, UWError
from rotation import (
    SECTOR_ETFS, BENCHMARK,
    build_score, rank_sectors, market_regime,
)
from scan import load_dotenv, add_flow_confirmation


# --------------------------------------------------------------------------
# Small SVG sparkline (single series; colored by net change)
# --------------------------------------------------------------------------
def sparkline(closes: list[float], up: str, down: str, w: int = 120, h: int = 30) -> str:
    pts = [c for c in closes if c is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    pad = 3
    n = len(pts)
    coords = []
    for i, v in enumerate(pts):
        x = pad + i / (n - 1) * (w - 2 * pad)
        y = pad + (1 - (v - lo) / rng) * (h - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    color = up if pts[-1] >= pts[0] else down
    path = " ".join(coords)
    last_x, last_y = coords[-1].split(",")
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{path}"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.2" fill="{color}"/>'
        f"</svg>"
    )


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
CSS = """
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --surface-2:#f2f2ee;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --line:#c3c2b7; --border:rgba(11,11,11,.10);
  --good:#0ca30c; --good-ink:#006300; --warn:#b8860b; --crit:#d03b3b;
  --series:#256abf; --series-track:#e6eef9;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --surface-2:#232321;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --line:#383835; --border:rgba(255,255,255,.10);
  --good:#0ca30c; --good-ink:#0ca30c; --warn:#fab219; --crit:#e66767;
  --series:#3987e5; --series-track:#22303f;
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --surface-2:#232321;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --line:#383835; --border:rgba(255,255,255,.10);
  --good:#0ca30c; --good-ink:#0ca30c; --warn:#fab219; --crit:#e66767;
  --series:#3987e5; --series-track:#22303f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:15px;line-height:1.45;-webkit-text-size-adjust:100%}
.wrap{max-width:1040px;margin:0 auto;padding:20px 16px 56px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;
  gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:2px 0 18px}
.toggle{background:var(--surface);border:1px solid var(--border);color:var(--ink-2);
  border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}
.regime{font-weight:650}
.regime.on{color:var(--good-ink)} .regime.off{color:var(--crit)} .regime.neutral{color:var(--warn)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.kpi .lab{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.kpi .val{font-size:22px;font-weight:680;margin-top:3px}
.kpi .val.sm{font-size:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:4px 4px;margin-bottom:20px;overflow:hidden}
.card h2{font-size:14px;margin:14px 14px 8px;color:var(--ink-2);font-weight:640}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:9px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
td.l,th.l{text-align:left}
tbody tr:last-child td{border-bottom:none}
.tnum{font-variant-numeric:tabular-nums}
.tk{font-weight:680} .nm{color:var(--ink-2);font-size:12.5px}
.pos{color:var(--good-ink)} .neg{color:var(--crit)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;
  font-size:11.5px;font-weight:640;border:1px solid transparent}
.b-in{color:var(--good-ink);background:color-mix(in srgb,var(--good) 14%,transparent);
  border-color:color-mix(in srgb,var(--good) 35%,transparent)}
.b-out{color:var(--crit);background:color-mix(in srgb,var(--crit) 12%,transparent);
  border-color:color-mix(in srgb,var(--crit) 35%,transparent)}
.b-hold{color:var(--ink-2);background:var(--surface-2);border-color:var(--border)}
.chip{font-size:11px;font-weight:600}
.chip.up{color:var(--good-ink)} .chip.dn{color:var(--crit)} .chip.na{color:var(--muted)}
.scorebar{display:flex;align-items:center;gap:7px;justify-content:flex-end}
.track{width:64px;height:7px;border-radius:4px;background:var(--series-track);overflow:hidden}
.fill{height:100%;border-radius:4px;background:var(--series)}
.spark{display:block}
.leaders{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;padding:8px 14px 16px}
.lead{border:1px solid var(--border);border-radius:12px;padding:12px 14px;background:var(--surface-2)}
.lead .ltk{font-weight:720;font-size:16px}
.lead .lnm{color:var(--muted);font-size:12px;margin-bottom:8px}
.lvl{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;font-variant-numeric:tabular-nums}
.lvl .k{color:var(--muted)}
.lvl.t .v{color:var(--good-ink);font-weight:640}
.lvl.s .v{color:var(--crit);font-weight:640}
.tags{margin-top:8px;font-size:11.5px;color:var(--muted)}
.empty{padding:12px 16px;color:var(--muted);font-size:13px}
footer{color:var(--muted);font-size:12px;line-height:1.6;margin-top:8px}
"""

TOGGLE_JS = """
(function(){
  var k='swing-theme';
  try{var s=localStorage.getItem(k); if(s)document.documentElement.setAttribute('data-theme',s);}catch(e){}
  window.__t=function(){
    var d=document.documentElement;
    var cur=d.getAttribute('data-theme');
    var next = cur==='dark'?'light':(cur==='light'?'dark':(matchMedia('(prefers-color-scheme:dark)').matches?'light':'dark'));
    d.setAttribute('data-theme',next);
    try{localStorage.setItem(k,next);}catch(e){}
  };
})();
"""


def _ret_cell(v) -> str:
    if v is None:
        return '<td class="tnum">–</td>'
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<td class="tnum {cls}">{v:+.1f}%</td>'


def _signal_badge(sig: str) -> str:
    if sig == "ROTATE IN":
        return '<span class="badge b-in">▲ Rotate in</span>'
    if sig == "ROTATE OUT":
        return '<span class="badge b-out">▼ Rotate out</span>'
    return '<span class="badge b-hold">— Hold</span>'


def _trend_chip(uptrend) -> str:
    if uptrend is True:
        return '<span class="chip up">▲ up</span>'
    if uptrend is False:
        return '<span class="chip dn">▼ down</span>'
    return '<span class="chip na">·</span>'


def _flow_chip(flow, use_flow) -> str:
    if not use_flow or flow is None:
        return '<span class="chip na">n/a</span>'
    return ('<span class="chip up">▲ bull</span>' if flow
            else '<span class="chip dn">▼ bear</span>')


def _live_chip(chg) -> str:
    if chg is None:
        return '<span class="chip na">–</span>'
    cls = "up" if chg > 0 else ("dn" if chg < 0 else "na")
    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "·")
    return f'<span class="chip {cls}">{arrow} {chg:+.1f}%</span>'


def render_html(scores, regime, detail, use_flow, spark_by_ticker, generated,
                live_mode=False, source_note=None) -> str:
    up_col, dn_col = "var(--good)", "var(--crit)"
    n_up = sum(1 for s in scores if s.above_sma200)
    n_in = sum(1 for s in scores if s.signal == "ROTATE IN")
    leaders = [s for s in scores if s.signal == "ROTATE IN"]
    top = scores[0] if scores else None

    reg_cls = {"RISK-ON": "on", "RISK-OFF": "off"}.get(regime, "neutral")

    rows = []
    for s in scores:
        spark = spark_by_ticker.get(s.ticker, "")
        score_pct = max(0.0, min(100.0, s.score))
        rows.append(f"""
        <tr>
          <td class="tnum">{s.rank}</td>
          <td class="l"><span class="tk">{html.escape(s.ticker)}</span><br>
              <span class="nm">{html.escape(s.name)}</span></td>
          <td class="l">{spark}</td>
          {_ret_cell(s.ret_1m)}{_ret_cell(s.ret_3m)}{_ret_cell(s.ret_6m)}
          {_ret_cell(s.rs_3m)}
          <td>{_trend_chip(s.uptrend)}</td>
          <td>{_live_chip(s.live_chg) if live_mode else _flow_chip(s.flow_bull, use_flow)}</td>
          <td><div class="scorebar"><div class="track"><div class="fill" style="width:{score_pct:.0f}%"></div></div>
              <span class="tnum">{s.score:.0f}</span></div></td>
          <td class="l">{_signal_badge(s.signal)}</td>
        </tr>""")

    if leaders:
        lead_cards = []
        for s in leaders:
            rr = ""
            if s.entry and s.stop and s.target and s.entry != s.stop:
                risk = s.entry - s.stop
                reward = s.target - s.entry
                if risk > 0:
                    rr = f'<div class="lvl"><span class="k">R:R</span><span class="v">{reward/risk:.1f} : 1</span></div>'
            tags = ", ".join(s.notes) if s.notes else ""
            lead_cards.append(f"""
            <div class="lead">
              <div class="ltk">{html.escape(s.ticker)}</div>
              <div class="lnm">{html.escape(s.name)}</div>
              <div class="lvl"><span class="k">Entry</span><span class="v tnum">{_fmt(s.entry)}</span></div>
              <div class="lvl s"><span class="k">Stop (2·ATR)</span><span class="v tnum">{_fmt(s.stop)}</span></div>
              <div class="lvl t"><span class="k">Target (3·ATR)</span><span class="v tnum">{_fmt(s.target)}</span></div>
              {rr}
              <div class="tags">{html.escape(tags)}</div>
            </div>""")
        leaders_html = f'<div class="leaders">{"".join(lead_cards)}</div>'
    else:
        leaders_html = ('<div class="empty">No sectors qualify to rotate into right now — '
                        'the trend filter is keeping you defensive.</div>')

    top_kpi = f'{html.escape(top.ticker)} · {top.score:.0f}' if top else '–'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sector Rotation — Daily Swing Dashboard</title>
<script>{TOGGLE_JS}</script>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Sector Rotation — Daily Swing</h1>
      <div class="sub">Generated {html.escape(generated)}</div>
    </div>
    <button class="toggle" onclick="__t()">◐ Theme</button>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="lab">Market regime</div>
      <div class="val sm regime {reg_cls}">{html.escape(regime)}</div>
      <div class="nm">{html.escape(detail)}</div></div>
    <div class="kpi"><div class="lab">Rotate-in now</div><div class="val">{n_in}</div>
      <div class="nm">of {len(scores)} sectors</div></div>
    <div class="kpi"><div class="lab">Breadth &gt; 200d</div><div class="val">{n_up}/{len(scores)}</div>
      <div class="nm">sectors in uptrend</div></div>
    <div class="kpi"><div class="lab">Top rank</div><div class="val sm">{top_kpi}</div>
      <div class="nm">highest score</div></div>
  </div>

  <div class="card">
    <h2>Sector ranking</h2>
    <div class="scroll">
    <table>
      <thead><tr>
        <th>#</th><th class="l">Sector</th><th class="l">30d</th>
        <th>1M</th><th>3M</th><th>6M</th><th>RS</th>
        <th>Trend</th><th>{"Live" if live_mode else "Flow"}</th><th>Score</th><th class="l">Signal</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <h2>Swing levels — leaders (ATR-based, 2R stop / 3R target)</h2>
    {leaders_html}
  </div>

  <footer>
    Signals are mechanical: momentum + relative strength vs SPY, gated on the 50/200-day trend.
    {html.escape(source_note) if source_note else ("Confirmed with Unusual Whales options flow." if use_flow else "Price-only (flow overlay off).")}
    Sparklines show ~30 trading days of price. <strong>Not investment advice</strong> —
    confirm on your own chart and manage risk.
  </footer>
</div>
</body>
</html>"""


def _fmt(v):
    return f"{v:.2f}" if v is not None else "–"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily sector-rotation swing dashboard")
    parser.add_argument("--out", default="dashboard.html", help="output HTML path")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--bottom", type=int, default=3)
    parser.add_argument("--timeframe", default="1Y")
    parser.add_argument("--no-flow", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the file in a browser")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        client = UWClient()
    except UWError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        bench = client.daily_ohlc(BENCHMARK, args.timeframe)
    except UWError as e:
        print(f"error fetching {BENCHMARK}: {e}", file=sys.stderr)
        return 1
    bench_closes = [float(c["close"]) for c in bench if c.get("close") is not None]
    regime, detail = market_regime(bench)
    risk_off = regime == "RISK-OFF"

    scores = []
    spark_by_ticker: dict[str, str] = {}
    up_col, dn_col = "#0ca30c", "#d03b3b"
    for ticker in SECTOR_ETFS:
        try:
            candles = client.daily_ohlc(ticker, args.timeframe)
        except UWError as e:
            print(f"  warn: {ticker} skipped ({e})", file=sys.stderr)
            continue
        sc = build_score(ticker, candles, bench_closes)
        if not sc:
            continue
        scores.append(sc)
        closes = [float(c["close"]) for c in candles[-30:] if c.get("close") is not None]
        spark_by_ticker[ticker] = sparkline(closes, up_col, dn_col)

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

    generated = _dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    html_doc = render_html(scores, regime, detail, use_flow, spark_by_ticker, generated)

    with open(args.out, "w") as fh:
        fh.write(html_doc)
    print(f"  wrote {args.out}  ({len(scores)} sectors, regime {regime})")

    if args.open:
        webbrowser.open(f"file://{_abspath(args.out)}")
    return 0


def _abspath(p: str) -> str:
    import os
    return os.path.abspath(p)


if __name__ == "__main__":
    raise SystemExit(main())
