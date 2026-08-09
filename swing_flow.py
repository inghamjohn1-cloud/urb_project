"""Aggregate Unusual Whales options-flow alerts and dark-pool prints per ticker.

The flow-alert endpoint returns one row per triggered alert (a contract, a
premium, a rule name). A swing candidate is a *ticker*, not a contract, so this
module rolls the rows up into a single per-ticker conviction picture:

  - how much premium hit, and on which side
  - whether it was aggressive (ask-side calls / bid-side puts) and swept
  - whether it looks like NEW positioning (opening, volume > OI)
  - whether it was one print or a cluster worked across strikes/expiries
  - whether the flow is one-sided or two-way (two-way = someone's hedging)

Field names are read through `_pick()` with several aliases because the REST
payload and the MCP tool payload spell a few fields differently.
"""

from __future__ import annotations

import datetime as _dt

from swing_config import FLOW, DARKPOOL


# ---------------------------------------------------------------------------
# Tolerant field access
# ---------------------------------------------------------------------------
def _pick(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "t", "1", "yes")
    return bool(v)


def _date(v) -> _dt.date | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        return None


def _dte(row: dict, today: _dt.date) -> int | None:
    """Days to expiry — from the field if present, else from the expiry date."""
    direct = _pick(row, "dte", "days_to_expiry")
    if direct is not None:
        try:
            return int(float(direct))
        except (TypeError, ValueError):
            pass
    exp = _date(_pick(row, "expiry", "expiration", "expires_at"))
    return (exp - today).days if exp else None


# ---------------------------------------------------------------------------
# Per-alert normalisation
# ---------------------------------------------------------------------------
def normalize_alert(row: dict, today: _dt.date | None = None) -> dict | None:
    """Flatten one flow-alert row into the fields the scorer needs."""
    today = today or _dt.date.today()
    ticker = str(_pick(row, "ticker", "ticker_symbol", "underlying_symbol", "symbol", default="")).upper()
    if not ticker:
        return None

    premium = _f(_pick(row, "total_premium", "premium"), 0.0) or 0.0
    ask_prem = _f(_pick(row, "total_ask_side_prem", "ask_side_premium"), 0.0) or 0.0
    bid_prem = _f(_pick(row, "total_bid_side_prem", "bid_side_premium"), 0.0) or 0.0

    opt_type = str(_pick(row, "type", "option_type", "put_call", default="")).lower()
    is_call = opt_type.startswith("c") or _b(_pick(row, "is_call", default=False))
    is_put = opt_type.startswith("p") or _b(_pick(row, "is_put", default=False))

    volume = _f(_pick(row, "volume", "total_size", "size"), 0.0) or 0.0
    oi = _f(_pick(row, "open_interest", "oi"), 0.0) or 0.0
    vol_oi = _f(_pick(row, "volume_oi_ratio"))
    if vol_oi is None:
        vol_oi = (volume / oi) if oi else None

    bid = _f(_pick(row, "nbbo_bid", "bid"))
    ask = _f(_pick(row, "nbbo_ask", "ask"))
    spread_pct = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = (ask - bid) / mid

    return {
        "ticker": ticker,
        "premium": premium,
        "ask_prem": ask_prem,
        "bid_prem": bid_prem,
        "side": "call" if is_call else ("put" if is_put else "unknown"),
        "strike": _f(_pick(row, "strike")),
        "expiry": _date(_pick(row, "expiry", "expiration", "expires_at")),
        "dte": _dte(row, today),
        "volume": volume,
        "open_interest": oi,
        "volume_oi_ratio": vol_oi,
        "is_sweep": _b(_pick(row, "has_sweep", "is_sweep", default=False)),
        "is_floor": _b(_pick(row, "has_floor", "is_floor", default=False)),
        "all_opening": _b(_pick(row, "all_opening_trades", "all_opening", default=False)),
        "is_multi_leg": _b(_pick(row, "is_multi_leg", "has_multileg", default=False)),
        "is_otm": _b(_pick(row, "is_otm", default=False)),
        "rule": str(_pick(row, "alert_rule", "rule_name", default="")),
        "created_at": _date(_pick(row, "created_at", "executed_at", "start_time")),
        "underlying_price": _f(_pick(row, "underlying_price", "price")),
        "spread_pct": spread_pct,
        "sector": _pick(row, "sector", default=""),
        "marketcap": _f(_pick(row, "marketcap", "market_cap")),
        "avg30_volume": _f(_pick(row, "avg30_stock_volume", "avg30_volume")),
        "next_earnings": _date(_pick(row, "next_earnings_date", "earnings_date")),
    }


def classify_premium(a: dict) -> tuple[float, float, float]:
    """Split one alert's premium into (bullish, bearish, unsided).

    Direction is a function of BOTH the option type and the side of the spread
    the trade printed on — buying and selling the same contract are opposite
    bets, so option type alone is not enough:

        ask-side call  -> bullish   (paying up for upside)
        bid-side call  -> bearish   (writing/selling calls)
        ask-side put   -> bearish   (paying up for downside)
        bid-side put   -> bullish   (writing/selling puts)

    Premium that printed between the quotes cannot be attributed by side, so it
    falls back to the option type (call = bullish, put = bearish) and is
    returned separately as `unsided` — the caller exposes it as
    `unsided_share` so a mid-market-dominated name can be discounted.
    """
    is_call = a["side"] == "call"
    ask, bid = a["ask_prem"], a["bid_prem"]

    if is_call:
        bullish, bearish = ask, bid
    else:
        bullish, bearish = bid, ask

    # Whatever the ask/bid split does not account for printed mid-market.
    unsided = max(0.0, a["premium"] - (ask + bid))
    if unsided > 0:
        if is_call:
            bullish += unsided
        else:
            bearish += unsided

    return bullish, bearish, unsided


def alert_passes(a: dict, today: _dt.date | None = None) -> tuple[bool, str]:
    """Apply the per-alert gates. Returns (passed, reason_if_failed)."""
    today = today or _dt.date.today()
    if a["premium"] < FLOW["min_alert_premium"]:
        return False, "premium below floor"
    if FLOW["exclude_multi_leg"] and a["is_multi_leg"]:
        return False, "multi-leg"
    if a["dte"] is None:
        return False, "no expiry"
    if not (FLOW["min_dte"] <= a["dte"] <= FLOW["max_dte"]):
        return False, f"dte {a['dte']} outside {FLOW['min_dte']}-{FLOW['max_dte']}"
    if a["side"] == "unknown":
        return False, "unknown side"
    if a["created_at"] and (today - a["created_at"]).days > FLOW["max_alert_age_days"]:
        return False, "stale alert"
    if a["spread_pct"] is not None and a["spread_pct"] > 0.15:
        return False, "option spread too wide"
    return True, ""


# ---------------------------------------------------------------------------
# Per-ticker roll-up
# ---------------------------------------------------------------------------
def aggregate_alerts(alerts: list[dict]) -> dict[str, dict]:
    """Group normalised, gate-passing alerts into one record per ticker."""
    by_ticker: dict[str, dict] = {}

    for a in alerts:
        t = by_ticker.setdefault(a["ticker"], {
            "ticker": a["ticker"],
            "alerts": [],
            "call_premium": 0.0,
            "put_premium": 0.0,
            "total_premium": 0.0,
            "bullish_premium": 0.0,      # ask-side calls + bid-side puts
            "bearish_premium": 0.0,      # bid-side calls + ask-side puts
            "unsided_premium": 0.0,      # printed mid-market, side unknowable
            "aggressive_premium": 0.0,   # lifted the offer (either type)
            "passive_premium": 0.0,      # rested on / hit the bid
            "opening_premium": 0.0,
            "sweep_premium": 0.0,
            "floor_premium": 0.0,
            "strikes": set(),
            "expiries": set(),
            "days": set(),
            "rules": set(),
            "sector": a.get("sector") or "",
            "marketcap": a.get("marketcap"),
            "avg30_volume": a.get("avg30_volume"),
            "next_earnings": a.get("next_earnings"),
            "underlying_price": a.get("underlying_price"),
        })

        t["alerts"].append(a)
        t["total_premium"] += a["premium"]
        if a["side"] == "call":
            t["call_premium"] += a["premium"]
        else:
            t["put_premium"] += a["premium"]

        # Directional attribution: option type AND side of the spread.
        bull, bear, unsided = classify_premium(a)
        t["bullish_premium"] += bull
        t["bearish_premium"] += bear
        t["unsided_premium"] += unsided

        # Aggression is a separate axis and is unchanged: lifting the offer is
        # aggressive on either option type, resting on the bid is passive.
        t["aggressive_premium"] += a["ask_prem"]
        t["passive_premium"] += a["bid_prem"]

        if a["all_opening"]:
            t["opening_premium"] += a["premium"]
        if a["is_sweep"]:
            t["sweep_premium"] += a["premium"]
        if a["is_floor"]:
            t["floor_premium"] += a["premium"]
        if a["strike"] is not None:
            t["strikes"].add(a["strike"])
        if a["expiry"]:
            t["expiries"].add(a["expiry"])
        if a["created_at"]:
            t["days"].add(a["created_at"])
        if a["rule"]:
            t["rules"].add(a["rule"])
        # Keep the most recent non-null context fields.
        for k in ("marketcap", "avg30_volume", "next_earnings", "underlying_price", "sector"):
            if not t.get(k) and a.get(k):
                t[k] = a[k]

    # Derived per-ticker measures
    for t in by_ticker.values():
        tot = t["total_premium"] or 1.0
        t["n_alerts"] = len(t["alerts"])
        t["call_share"] = t["call_premium"] / tot
        t["put_share"] = t["put_premium"] / tot

        # Direction comes from the BULLISH/BEARISH split, not call vs put:
        # a put sold on the bid is a bullish position, not a bearish one.
        directional = t["bullish_premium"] + t["bearish_premium"]
        t["direction"] = ("long" if t["bullish_premium"] >= t["bearish_premium"]
                          else "short")
        t["bullish_share"] = (t["bullish_premium"] / directional) if directional else 0.0
        # Share of premium that printed mid-market and had to fall back to
        # option type — high values mean the direction read is less reliable.
        t["unsided_share"] = t["unsided_premium"] / tot

        dominant = max(t["bullish_premium"], t["bearish_premium"])
        # Coherence: 1.0 = entirely one-sided, 0.0 = perfectly split.
        t["coherence"] = ((2.0 * dominant / directional) - 1.0) if directional else 0.0
        ag_total = t["aggressive_premium"] + t["passive_premium"]
        t["aggression"] = (t["aggressive_premium"] / ag_total) if ag_total else None
        t["opening_share"] = t["opening_premium"] / tot
        t["sweep_share"] = t["sweep_premium"] / tot
        t["floor_share"] = t["floor_premium"] / tot
        t["n_strikes"] = len(t["strikes"])
        t["n_expiries"] = len(t["expiries"])
        t["n_days"] = len(t["days"])
        dtes = [a["dte"] for a in t["alerts"] if a["dte"] is not None]
        t["avg_dte"] = (sum(dtes) / len(dtes)) if dtes else None
        vois = [a["volume_oi_ratio"] for a in t["alerts"] if a["volume_oi_ratio"] is not None]
        t["max_volume_oi"] = max(vois) if vois else None
        t["biggest_alert"] = max((a["premium"] for a in t["alerts"]), default=0.0)

    return by_ticker


# ---------------------------------------------------------------------------
# Dark pool
# ---------------------------------------------------------------------------
def aggregate_darkpool(prints: list[dict], avg_volume: float | None = None) -> dict:
    """Summarise recent off-exchange prints for one ticker.

    `lean` is premium-weighted execution price versus the NBBO midpoint:
    consistently above the mid means buyers were paying up (accumulation),
    below means sellers were hitting bids (distribution).
    """
    rows = []
    for p in prints:
        prem = _f(_pick(p, "premium"))
        size = _f(_pick(p, "size"))
        price = _f(_pick(p, "price"))
        if prem is None and price is not None and size is not None:
            prem = price * size
        if prem is None or size is None or price is None:
            continue
        rows.append({
            "premium": prem,
            "size": size,
            "price": price,
            "bid": _f(_pick(p, "nbbo_bid", "bid")),
            "ask": _f(_pick(p, "nbbo_ask", "ask")),
            "executed_at": _date(_pick(p, "executed_at", "trf_executed_at")),
        })

    if not rows:
        return {"has_data": False, "n_prints": 0, "total_premium": 0.0,
                "block_premium": 0.0, "biggest_block": 0.0,
                "block_shares": 0.0, "pct_of_adv": None, "lean": None}

    blocks = [r for r in rows if r["premium"] >= DARKPOOL["min_block_premium"]]
    block_shares = sum(r["size"] for r in blocks)

    # Premium-weighted position within the NBBO spread, mapped to [-1, +1].
    num = den = 0.0
    for r in blocks or rows:
        if r["bid"] is None or r["ask"] is None or r["ask"] <= r["bid"]:
            continue
        mid = (r["bid"] + r["ask"]) / 2.0
        half = (r["ask"] - r["bid"]) / 2.0
        pos = max(-1.0, min(1.0, (r["price"] - mid) / half)) if half else 0.0
        num += pos * r["premium"]
        den += r["premium"]
    lean = (num / den) if den else None

    pct_adv = (block_shares / avg_volume) if (avg_volume and block_shares) else None

    return {
        "has_data": True,
        "n_prints": len(rows),
        "n_blocks": len(blocks),
        "total_premium": sum(r["premium"] for r in rows),
        "block_premium": sum(r["premium"] for r in blocks),
        "biggest_block": max((r["premium"] for r in rows), default=0.0),
        "block_shares": block_shares,
        "pct_of_adv": pct_adv,
        "lean": lean,
    }
