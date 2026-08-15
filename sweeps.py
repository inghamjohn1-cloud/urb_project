#!/usr/bin/env python3
"""Unusual call-sweep scanner (default: NVDA, $1M+).

Finds large, aggressive intermarket sweeps in a ticker's options tape and ranks
them by how *unusual* they are — not merely how big.

Why this is not a one-line premium filter
-----------------------------------------
Two things break the naive "premium > $1,000,000" screen:

1. **A sweep is not one print.** An intermarket sweep is routed to every
   exchange showing size, so one order lands on the tape as several child
   prints. NVDA 2026-08-28 $232.5C printed on MCRY at 18:50:10.250 and on XBOX
   15ms later — one $146K order, two rows. Filtering the API at
   `min_premium=1000000` would have discarded *both* legs. So we pull the tape
   at a low per-leg floor (`--min-leg-premium`) and stitch legs back into orders
   by (contract, side) within a short time window, then apply the $1M threshold
   to the *order*.

2. **Half of all "call sweeps" are bearish.** A call lifted at the ask is
   someone buying; a call hit on the bid is someone *selling* — often closing,
   or writing premium. In a recent 4-day NVDA sample, 27 of 50 call sweeps were
   bid-side. Reporting those as bullish gets the signal exactly backwards, so
   direction is derived per-order from where each leg filled and the two sides
   are totalled separately.

The unusualness score (0-100) blends order premium, sweep breadth (legs and
exchanges hit), size relative to open interest, fill aggression, time to
expiry, and how far out of the money the strike is. See `score_sweep`.

Usage:
    python sweeps.py                              # live: NVDA calls, $1M+
    python sweeps.py --ticker AMD --min-premium 500000
    python sweeps.py --type puts --lookback 3
    python sweeps.py --file tape.json             # score a saved MCP result
    python sweeps.py --csv out.csv --json out.json --html sweeps.html

Live mode needs an Unusual Whales token in UW_TOKEN (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import json
import math
import os
import sys
from dataclasses import dataclass, field

DEFAULT_TICKER = "NVDA"
DEFAULT_MIN_PREMIUM = 1_000_000.0
# Per-leg floor for the tape pull. Child legs of a $1M sweep are routinely
# $25-50K apiece, so this has to sit well below the alert threshold.
DEFAULT_LEG_FLOOR = 25_000.0
# Child prints of one swept order land within milliseconds; a few seconds of
# slack absorbs a router working an order across venues.
DEFAULT_WINDOW_S = 5.0


# -- parsing -----------------------------------------------------------------

def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _ts(v) -> _dt.datetime | None:
    """Parse UW's ISO-8601 `executed_at` (…Z) into an aware datetime."""
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _date(v) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def rows_of(payload) -> list[dict]:
    """Unwrap an MCP/REST option-trades payload into a list of print rows."""
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


@dataclass
class Leg:
    """One execution print — a child leg of a swept order."""
    id: str
    ts: _dt.datetime
    contract: str
    ticker: str
    opt_type: str
    strike: float
    expiry: _dt.date | None
    premium: float
    size: int
    price: float
    exchange: str
    side: str            # ask | bid | mid | unknown
    above_ask: bool
    spot: float
    open_interest: int
    volume: int
    iv: float
    delta: float
    earnings: _dt.date | None
    sweep_flag: bool


def parse_leg(r: dict) -> Leg | None:
    ts = _ts(r.get("executed_at"))
    contract = str(r.get("option_chain_id") or "")
    if ts is None or not contract or r.get("canceled") is True:
        return None

    tags = [str(t) for t in (r.get("tags") or [])]
    if "ask_side" in tags:
        side = "ask"
    elif "bid_side" in tags:
        side = "bid"
    elif "mid_side" in tags:
        side = "mid"
    else:
        side = "unknown"

    price, ask = _f(r.get("price")), _f(r.get("nbbo_ask"))
    flags = [str(x) for x in (r.get("report_flags") or [])]

    return Leg(
        id=str(r.get("id") or ""),
        ts=ts,
        contract=contract,
        ticker=str(r.get("underlying_symbol") or "").upper(),
        opt_type=str(r.get("option_type") or "").lower(),
        strike=_f(r.get("strike")),
        expiry=_date(r.get("expiry")),
        premium=_f(r.get("premium")),
        size=_i(r.get("size")),
        price=price,
        exchange=str(r.get("exchange") or "?"),
        side=side,
        above_ask=bool(ask) and price >= ask,
        spot=_f(r.get("underlying_price")),
        open_interest=_i(r.get("open_interest")),
        volume=_i(r.get("volume")),
        iv=_f(r.get("implied_volatility")),
        delta=_f(r.get("delta")),
        earnings=_date(r.get("next_earnings_date")),
        sweep_flag=any("sweep" in f for f in flags),
    )


# -- clustering --------------------------------------------------------------

@dataclass
class Sweep:
    """A swept order: the child legs stitched back into one execution."""
    contract: str
    ticker: str
    opt_type: str
    strike: float
    expiry: _dt.date | None
    side: str
    first_ts: _dt.datetime
    last_ts: _dt.datetime
    premium: float
    size: int
    legs: int
    exchanges: list[str]
    avg_price: float
    spot: float
    open_interest: int
    volume: int
    iv: float
    delta: float
    earnings: _dt.date | None
    ask_premium: float          # premium filled at or above the ask
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    repeat: int = 0             # nth same-direction sweep on this contract

    # -- derived ------------------------------------------------------------
    @property
    def direction(self) -> str:
        """Bullish = buying calls / selling puts; bearish = the reverse."""
        if self.side == "ask":
            return "BULLISH" if self.opt_type == "call" else "BEARISH"
        if self.side == "bid":
            return "BEARISH" if self.opt_type == "call" else "BULLISH"
        return "NEUTRAL"

    @property
    def action(self) -> str:
        return {"ask": "BOT", "bid": "SLD", "mid": "MID"}.get(self.side, "?")

    @property
    def dte(self) -> int | None:
        if self.expiry is None:
            return None
        return (self.expiry - self.first_ts.date()).days

    @property
    def otm_pct(self) -> float | None:
        """Strike distance from spot, signed so + is out of the money."""
        if not self.spot:
            return None
        pct = (self.strike - self.spot) / self.spot * 100.0
        return pct if self.opt_type == "call" else -pct

    @property
    def size_oi(self) -> float | None:
        if not self.open_interest:
            return None
        return self.size / self.open_interest

    @property
    def duration_s(self) -> float:
        return (self.last_ts - self.first_ts).total_seconds()

    @property
    def label(self) -> str:
        if self.score >= 70:
            return "EXTREME"
        if self.score >= 55:
            return "HIGH"
        if self.score >= 40:
            return "NOTABLE"
        return "ROUTINE"

    def describe(self) -> str:
        exp = self.expiry.strftime("%b %d %Y") if self.expiry else "?"
        return (f"{self.ticker} {exp} ${self.strike:g}{self.opt_type[:1].upper()} "
                f"{self.action}")


def cluster_sweeps(legs: list[Leg], window_s: float = DEFAULT_WINDOW_S) -> list[Sweep]:
    """Stitch child prints into orders: same contract + same side, close in time.

    Legs are grouped by (contract, side) so an aggressive buyer and a seller
    hitting the same strike seconds apart never merge into one phantom order.
    """
    buckets: dict[tuple[str, str], list[Leg]] = {}
    seen: set[str] = set()
    for lg in legs:
        if lg.id and lg.id in seen:      # the tape can repeat a print
            continue
        if lg.id:
            seen.add(lg.id)
        buckets.setdefault((lg.contract, lg.side), []).append(lg)

    out: list[Sweep] = []
    for group in buckets.values():
        group.sort(key=lambda x: x.ts)
        run: list[Leg] = []
        for lg in group:
            if run and (lg.ts - run[-1].ts).total_seconds() > window_s:
                out.append(_merge(run))
                run = []
            run.append(lg)
        if run:
            out.append(_merge(run))
    return out


def _merge(run: list[Leg]) -> Sweep:
    prem = sum(x.premium for x in run)
    size = sum(x.size for x in run)
    last = run[-1]
    # exchanges in first-hit order, de-duplicated
    exch: list[str] = []
    for x in run:
        if x.exchange not in exch:
            exch.append(x.exchange)
    return Sweep(
        contract=last.contract,
        ticker=last.ticker,
        opt_type=last.opt_type,
        strike=last.strike,
        expiry=last.expiry,
        side=last.side,
        first_ts=run[0].ts,
        last_ts=last.ts,
        premium=prem,
        size=size,
        legs=len(run),
        exchanges=exch,
        # premium-weighted, so a big leg sets the average
        avg_price=(sum(x.price * x.premium for x in run) / prem) if prem else last.price,
        spot=last.spot,
        open_interest=max(x.open_interest for x in run),
        volume=max(x.volume for x in run),
        iv=last.iv,
        delta=last.delta,
        earnings=last.earnings,
        ask_premium=sum(x.premium for x in run if x.above_ask),
    )


# -- scoring -----------------------------------------------------------------

def score_sweep(s: Sweep, threshold: float = DEFAULT_MIN_PREMIUM) -> None:
    """Rate 0-100 on how unusual the order is; sets `.score` and `.reasons`.

    Weights: premium 30, breadth 15, size-vs-OI 20, aggression 15, expiry 10,
    moneyness 10. Size premium alone tops out at 30 — a $5M deep-ITM single-leg
    print should not outrank a $1.2M sweep that swept four venues for more
    contracts than the strike's entire open interest.
    """
    reasons: list[str] = []

    # 1. Notional (30) — log-scaled, $100K floor to 50x the threshold.
    if s.premium > 0:
        span = math.log10(50.0)
        pts = math.log10(max(s.premium, 1.0) / 100_000.0) / span
        prem_pts = 30.0 * max(0.0, min(1.0, pts))
    else:
        prem_pts = 0.0
    if s.premium >= threshold * 2:
        reasons.append(f"{_money(s.premium)} order")

    # 2. Sweep breadth (15) — venues swept, then leg count.
    n_ex, n_legs = len(s.exchanges), s.legs
    breadth_pts = 10.0 * min(1.0, (n_ex - 1) / 3.0) + 5.0 * min(1.0, (n_legs - 1) / 5.0)
    if n_ex >= 3:
        reasons.append(f"swept {n_ex} venues")
    elif n_legs >= 3:
        reasons.append(f"{n_legs} legs")

    # 3. Size vs open interest (20) — the new-position tell.
    ratio = s.size_oi
    if s.open_interest == 0:
        oi_pts, ratio = 20.0, None
        reasons.append("no prior OI at strike")
    elif ratio is not None:
        oi_pts = 20.0 * min(1.0, ratio)
        if ratio >= 1.0:
            reasons.append(f"size {ratio:.1f}x OI")
    else:
        oi_pts = 0.0

    # 4. Aggression (15) — share of premium paid at or through the offer.
    ask_frac = (s.ask_premium / s.premium) if s.premium else 0.0
    agg_pts = 15.0 * ask_frac
    if ask_frac >= 0.9 and s.side == "ask":
        reasons.append("filled at/through ask")

    # 5. Time to expiry (10) — near-dated buying is urgent money.
    dte = s.dte
    if dte is None:
        exp_pts = 3.0
    elif dte <= 2:
        exp_pts = 10.0
        reasons.append(f"{dte}DTE")
    elif dte <= 7:
        exp_pts = 8.5
        reasons.append(f"{dte}DTE")
    elif dte <= 30:
        exp_pts = 6.0
    elif dte <= 90:
        exp_pts = 4.0
    elif dte <= 365:
        exp_pts = 2.5
    else:
        exp_pts = 2.0
        reasons.append("LEAPS")

    # 6. Moneyness (10) — OTM is a leveraged directional bet; deep ITM is
    #    usually stock replacement or a hedge, so it scores low.
    otm = s.otm_pct
    if otm is None:
        mon_pts = 3.0
    elif otm > 25:
        mon_pts = 6.0                      # far OTM: cheap lottery, discount it
        reasons.append(f"{otm:.0f}% OTM")
    elif otm >= 3:
        mon_pts = 10.0
        reasons.append(f"{otm:.0f}% OTM")
    elif otm >= -2:
        mon_pts = 8.0                      # at the money
    elif otm >= -10:
        mon_pts = 4.0
    else:
        mon_pts = 1.5
        reasons.append("deep ITM")

    score = prem_pts + breadth_pts + oi_pts + agg_pts + exp_pts + mon_pts

    # Context flags — surfaced to the reader, not scored.
    if s.volume and s.open_interest and s.volume > s.open_interest:
        reasons.append("day vol > OI")
    if s.earnings and s.expiry and s.earnings <= s.expiry:
        d = (s.earnings - s.first_ts.date()).days
        if 0 <= d <= 45:
            reasons.append(f"covers earnings ({d}d)")
    if s.repeat > 1:
        reasons.append(f"#{s.repeat} on this contract")

    s.score = round(max(0.0, min(100.0, score)), 1)
    s.reasons = reasons


def mark_repeats(sweeps: list[Sweep]) -> None:
    """Number same-contract, same-direction sweeps in time order (accumulation)."""
    counts: dict[tuple[str, str], int] = {}
    for s in sorted(sweeps, key=lambda x: x.first_ts):
        key = (s.contract, s.side)
        counts[key] = counts.get(key, 0) + 1
        s.repeat = counts[key]


def scan(rows: list[dict], min_premium: float = DEFAULT_MIN_PREMIUM,
         window_s: float = DEFAULT_WINDOW_S, opt_type: str = "call",
         sweeps_only: bool = True) -> tuple[list[Sweep], list[Sweep]]:
    """Tape rows -> (orders clearing the threshold, all orders). Both ranked."""
    legs = [lg for lg in (parse_leg(r) for r in rows) if lg]
    if opt_type in ("call", "put"):
        legs = [lg for lg in legs if lg.opt_type == opt_type]
    if sweeps_only:
        legs = [lg for lg in legs if lg.sweep_flag]

    orders = cluster_sweeps(legs, window_s)
    mark_repeats(orders)
    for s in orders:
        score_sweep(s, min_premium)

    orders.sort(key=lambda s: (-s.premium, -s.score))
    hits = [s for s in orders if s.premium >= min_premium]
    hits.sort(key=lambda s: (-s.score, -s.premium))
    return hits, orders


# -- formatting --------------------------------------------------------------

def _money(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.2f}M"
    if a >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def totals(orders: list[Sweep]) -> dict:
    bull = sum(s.premium for s in orders if s.direction == "BULLISH")
    bear = sum(s.premium for s in orders if s.direction == "BEARISH")
    return {"bullish": bull, "bearish": bear, "net": bull - bear, "count": len(orders)}


def print_report(hits: list[Sweep], orders: list[Sweep], ticker: str,
                 min_premium: float, opt_type: str, window_s: float) -> None:
    kind = {"call": "CALL", "put": "PUT"}.get(opt_type, "OPTION")
    print()
    print("=" * 104)
    print(f"  UNUSUAL {kind} SWEEP SCANNER — {ticker}      "
          f"threshold {_money(min_premium)} per order   (legs stitched within {window_s:g}s)")
    print("=" * 104)

    if not orders:
        print(f"\n  No {kind.lower()} sweeps in this tape. Widen with --lookback, switch")
        print("  --type, or drop --min-leg-premium; check the ticker and token.\n")
        return

    t = totals(orders)
    span = f"{min(s.first_ts for s in orders):%Y-%m-%d %H:%M} → {max(s.last_ts for s in orders):%Y-%m-%d %H:%M} UTC"
    print(f"  tape: {t['count']} swept orders   {span}")
    print(f"  {kind.lower()} sweep premium:  bullish {_money(t['bullish'])}   "
          f"bearish {_money(t['bearish'])}   net {_money(t['net'])}")
    print()

    if not hits:
        print(f"  Nothing cleared {_money(min_premium)}. Largest orders on the tape:")
        _table(orders[:8])
        print(f"\n  $1M+ single-ticker sweeps are episodic — often a couple a week, not")
        print(f"  a couple a day. Lower it with --min-premium 250000 to see more.\n")
        return

    print(f"  {len(hits)} order(s) over {_money(min_premium)}, ranked by unusualness:")
    _table(hits)
    print()
    for s in hits:
        why = ("  ·  ".join(s.reasons)) or "size only"
        print(f"    {s.describe():<34} {s.label:<8} {s.score:>5.1f}   {why}")
    print("\n  Sweeps are one input, not a thesis. Direction is inferred from the fill")
    print("  side; sweeps can be hedges, rolls, or legs of a spread. Not investment advice.\n")


def _table(rows: list[Sweep]) -> None:
    print(f"    {'Time (UTC)':<17}{'Expiry':<12}{'Strike':>8}{'Act':>5}{'Dir':>9}"
          f"{'Premium':>10}{'Size':>8}{'OI':>9}{'DTE':>5}{'OTM':>7}{'Lg':>4}{'Vn':>4}{'Score':>7}")
    print("    " + "-" * 96)
    for s in rows:
        exp = s.expiry.strftime("%Y-%m-%d") if s.expiry else "?"
        otm = f"{s.otm_pct:+.1f}%" if s.otm_pct is not None else "  -  "
        dte = str(s.dte) if s.dte is not None else "-"
        oi = f"{s.open_interest:,}" if s.open_interest else "0"
        print(f"    {s.first_ts:%m-%d %H:%M:%S}  {exp:<12}{s.strike:>8g}{s.action:>5}"
              f"{s.direction:>9}{_money(s.premium):>10}{s.size:>8,}{oi:>9}{dte:>5}"
              f"{otm:>7}{s.legs:>4}{len(s.exchanges):>4}{s.score:>7.1f}")


# -- exports -----------------------------------------------------------------

CSV_COLS = ["first_ts", "last_ts", "ticker", "contract", "opt_type", "strike", "expiry",
            "dte", "action", "direction", "premium", "size", "open_interest", "size_oi",
            "volume", "otm_pct", "spot", "avg_price", "iv", "delta", "legs", "exchanges",
            "duration_s", "repeat", "score", "label", "reasons"]


def _row(s: Sweep) -> dict:
    d = {
        "first_ts": s.first_ts.isoformat(), "last_ts": s.last_ts.isoformat(),
        "ticker": s.ticker, "contract": s.contract, "opt_type": s.opt_type,
        "strike": s.strike, "expiry": s.expiry.isoformat() if s.expiry else "",
        "dte": s.dte, "action": s.action, "direction": s.direction,
        "premium": round(s.premium, 2), "size": s.size,
        "open_interest": s.open_interest,
        "size_oi": round(s.size_oi, 3) if s.size_oi is not None else "",
        "volume": s.volume,
        "otm_pct": round(s.otm_pct, 2) if s.otm_pct is not None else "",
        "spot": s.spot, "avg_price": round(s.avg_price, 2),
        "iv": round(s.iv, 4), "delta": round(s.delta, 4),
        "legs": s.legs, "exchanges": "|".join(s.exchanges),
        "duration_s": round(s.duration_s, 3), "repeat": s.repeat,
        "score": s.score, "label": s.label, "reasons": "; ".join(s.reasons),
    }
    return d


def write_csv(path: str, sweeps: list[Sweep]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for s in sweeps:
            w.writerow(_row(s))
    print(f"  wrote {path}")


def write_json(path: str, sweeps: list[Sweep], meta: dict) -> None:
    with open(path, "w") as fh:
        json.dump({"meta": meta, "sweeps": [_row(s) for s in sweeps]}, fh, indent=2)
    print(f"  wrote {path}")


def render_html(hits: list[Sweep], orders: list[Sweep], ticker: str,
                min_premium: float, opt_type: str, generated: str) -> str:
    """Self-contained report in the dashboard's theme."""
    from dashboard import CSS, TOGGLE_JS

    t = totals(orders)
    kind = {"call": "call", "put": "put"}.get(opt_type, "option")
    net_cls = "pos" if t["net"] > 0 else ("neg" if t["net"] < 0 else "")
    show = hits or orders[:10]
    empty_note = "" if hits else (
        f'<p class="empty">Nothing cleared {html.escape(_money(min_premium))} — '
        f'showing the largest orders on the tape instead. $1M+ single-ticker sweeps '
        f'are episodic, often a couple a week.</p>')

    body = []
    for s in show:
        exp = s.expiry.strftime("%b %d %Y") if s.expiry else "?"
        dcls = "pos" if s.direction == "BULLISH" else ("neg" if s.direction == "BEARISH" else "")
        badge = ("b-in" if s.direction == "BULLISH"
                 else "b-out" if s.direction == "BEARISH" else "b-hold")
        otm = f"{s.otm_pct:+.1f}%" if s.otm_pct is not None else "–"
        body.append(
            f'<tr><td class="l tnum">{s.first_ts:%m-%d %H:%M:%S}</td>'
            f'<td class="l"><span class="tk">${s.strike:g}{s.opt_type[:1].upper()}</span>'
            f'<div class="nm">{html.escape(exp)} · {s.dte if s.dte is not None else "?"}d</div></td>'
            f'<td><span class="badge {badge}">{s.action} {html.escape(s.direction.title())}</span></td>'
            f'<td class="tnum">{html.escape(_money(s.premium))}</td>'
            f'<td class="tnum">{s.size:,}</td>'
            f'<td class="tnum">{s.open_interest:,}</td>'
            f'<td class="tnum {dcls}">{otm}</td>'
            f'<td class="tnum">{s.legs}/{len(s.exchanges)}</td>'
            f'<td><div class="scorebar"><div class="track">'
            f'<div class="fill" style="width:{max(0, min(100, s.score)):.0f}%"></div></div>'
            f'<span class="tnum">{s.score:.0f}</span></div></td>'
            f'<td class="l nm">{html.escape("  ·  ".join(s.reasons)) or "–"}</td></tr>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(ticker)} sweep scanner</title>
<style>{CSS}</style><script>{TOGGLE_JS}</script></head><body><div class="wrap">
<header class="top"><div>
  <h1>{html.escape(ticker)} unusual {kind} sweeps</h1>
  <p class="sub">Orders over {html.escape(_money(min_premium))} · child legs stitched into
     orders · {html.escape(generated)}</p>
</div><button class="toggle" onclick="__t()">◐ theme</button></header>
<div class="kpis">
  <div class="kpi"><div class="lab">Orders ≥ threshold</div><div class="val">{len(hits)}</div></div>
  <div class="kpi"><div class="lab">Bullish {kind} premium</div>
      <div class="val sm pos">{html.escape(_money(t['bullish']))}</div></div>
  <div class="kpi"><div class="lab">Bearish {kind} premium</div>
      <div class="val sm neg">{html.escape(_money(t['bearish']))}</div></div>
  <div class="kpi"><div class="lab">Net</div>
      <div class="val sm {net_cls}">{html.escape(_money(t['net']))}</div></div>
</div>
<div class="card"><h2>Ranked by unusualness</h2>{empty_note}<div class="scroll"><table>
<thead><tr><th class="l">Time (UTC)</th><th class="l">Contract</th><th>Side</th>
<th>Premium</th><th>Size</th><th>OI</th><th>OTM</th><th>Lg/Vn</th><th>Score</th>
<th class="l">Why</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div></div>
<footer>Direction is inferred from the fill side: calls lifted at the ask are buying,
calls hit on the bid are selling. Sweeps can be hedges, rolls, or spread legs — this is
one input, not a thesis. Not investment advice.</footer>
</div></body></html>"""


# -- data sources ------------------------------------------------------------

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


def fetch_live(ticker: str, opt_type: str, leg_floor: float, lookback_days: int,
               limit: int) -> list[dict]:
    """Pull the sweep tape from the Unusual Whales REST API."""
    from uw_client import UWClient

    newer = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")
    client = UWClient()
    return client.option_trades(
        ticker=ticker,
        option_type={"call": "Calls", "put": "Puts"}.get(opt_type),
        report_flag="sweep",
        min_premium=leg_floor,
        newer_than=newer,
        limit=limit,
    )


def load_file(path: str) -> list[dict]:
    with open(path) as fh:
        return rows_of(json.load(fh))


# -- cli ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Scan a ticker's options tape for unusual large sweeps.")
    ap.add_argument("--ticker", default=DEFAULT_TICKER, help="underlying (default NVDA)")
    ap.add_argument("--min-premium", type=float, default=DEFAULT_MIN_PREMIUM,
                    help="alert threshold per stitched order (default 1000000)")
    ap.add_argument("--min-leg-premium", type=float, default=DEFAULT_LEG_FLOOR,
                    help="per-print floor for the tape pull (default 25000)")
    ap.add_argument("--type", choices=["calls", "puts", "both"], default="calls")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_S,
                    help="seconds within which prints stitch into one order (default 5)")
    ap.add_argument("--lookback", type=int, default=5, help="days of tape (default 5)")
    ap.add_argument("--limit", type=int, default=1000, help="max prints to pull")
    ap.add_argument("--all-trades", action="store_true",
                    help="include non-sweep prints (blocks, singles) too")
    ap.add_argument("--file", metavar="PATH", help="score a saved tape JSON instead of the API")
    ap.add_argument("--csv", metavar="PATH", help="write ranked orders to CSV")
    ap.add_argument("--json", metavar="PATH", dest="json_path", help="write ranked orders to JSON")
    ap.add_argument("--html", metavar="PATH", help="write an HTML report")
    args = ap.parse_args(argv)

    opt_type = {"calls": "call", "puts": "put", "both": "both"}[args.type]

    if args.file:
        try:
            rows = load_file(args.file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"error reading {args.file}: {e}", file=sys.stderr)
            return 2
        ticker = args.ticker
    else:
        load_dotenv()
        try:
            rows = fetch_live(args.ticker, opt_type, args.min_leg_premium,
                              args.lookback, args.limit)
        except Exception as e:                     # UWError or missing token
            print(f"error: {e}", file=sys.stderr)
            print("  (no token? score a saved tape with --file tape.json)", file=sys.stderr)
            return 2
        ticker = args.ticker

    hits, orders = scan(rows, min_premium=args.min_premium, window_s=args.window,
                        opt_type=opt_type, sweeps_only=not args.all_trades)

    print_report(hits, orders, ticker, args.min_premium, opt_type, args.window)

    ranked = hits or orders
    if args.csv:
        write_csv(args.csv, ranked)
    if args.json_path:
        write_json(args.json_path, ranked, {
            "ticker": ticker, "min_premium": args.min_premium, "type": opt_type,
            "window_s": args.window, "orders_scanned": len(orders),
            "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            **totals(orders),
        })
    if args.html:
        gen = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(args.html, "w") as fh:
            fh.write(render_html(hits, orders, ticker, args.min_premium, opt_type, gen))
        print(f"  wrote {args.html}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
