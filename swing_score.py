"""Composite scoring and risk levels for single-name swing candidates.

Score = flow (40) + dark pool (15) + technical (35) + liquidity (10) -> 0..100.

Two principles run through the whole module:

  1. Gates are separate from scores. A name that fails a hard gate is dropped
     with a stated reason — it never gets a partial score that might tempt you
     into taking it anyway.
  2. Missing data scores 0, never negative. No dark-pool prints means "no
     confirmation", not "distribution". The report labels the difference.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from swing_config import (
    UNIVERSE, FLOW, DARKPOOL, TECHNICAL, WEIGHTS, FLOW_POINTS, TECH_POINTS,
    TIERS, MIN_SCORE, RISK, EARNINGS,
)


@dataclass
class Candidate:
    ticker: str
    direction: str                  # "long" or "short"
    sector: str = ""

    flow: dict = field(default_factory=dict)
    dp: dict = field(default_factory=dict)
    tech: dict = field(default_factory=dict)

    flow_score: float = 0.0
    dp_score: float = 0.0
    tech_score: float = 0.0
    liq_score: float = 0.0
    score: float = 0.0
    tier: str = ""

    entry: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    rr: float | None = None
    shares: int | None = None
    risk_dollars: float | None = None

    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rejected: str = ""


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------
def check_gates(flow: dict, tech: dict, allow_earnings: bool = False,
                today: _dt.date | None = None) -> str:
    """Return "" if the candidate is tradeable, else the rejection reason."""
    today = today or _dt.date.today()

    if flow["total_premium"] < FLOW["min_ticker_premium"]:
        return f"ticker premium ${flow['total_premium']:,.0f} < ${FLOW['min_ticker_premium']:,.0f}"

    price = tech.get("last") or flow.get("underlying_price")
    if price is None:
        return "no price data"
    if price < UNIVERSE["min_price"]:
        return f"price ${price:.2f} below ${UNIVERSE['min_price']:.0f}"
    if price > UNIVERSE["max_price"]:
        return f"price ${price:.2f} above ${UNIVERSE['max_price']:.0f}"

    adv = tech.get("avg_volume") or flow.get("avg30_volume")
    if adv is None:
        return "no volume data"
    if adv < UNIVERSE["min_avg_volume"]:
        return f"ADV {adv:,.0f} below {UNIVERSE['min_avg_volume']:,.0f} shares"
    if adv * price < UNIVERSE["min_dollar_volume"]:
        return f"dollar volume ${adv * price:,.0f} too thin"

    mcap = flow.get("marketcap")
    if mcap is not None and mcap < UNIVERSE["min_marketcap"]:
        return f"marketcap ${mcap:,.0f} below ${UNIVERSE['min_marketcap']:,.0f}"

    # Trend conflict: never fight the 200 EMA on the side the flow is betting.
    ema_slow = tech.get("ema_slow")
    if ema_slow and price:
        if flow["direction"] == "long" and price < ema_slow:
            return "bullish flow but price below 200 EMA"
        if flow["direction"] == "short" and price > ema_slow:
            return "bearish flow but price above 200 EMA"

    # Earnings inside the hold window turn a swing into a coin flip.
    er = flow.get("next_earnings")
    if er and not allow_earnings:
        days = (er - today).days
        if 0 <= days <= EARNINGS["block_if_within_days"]:
            return f"earnings in {days}d"

    return ""


# ---------------------------------------------------------------------------
# Component 1 — options-flow conviction (max 40)
# ---------------------------------------------------------------------------
def score_flow(f: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    p = FLOW_POINTS

    # Premium magnitude, log-scaled between the $250k floor and $10M.
    # $250k -> 0, $1M -> ~6.2, $5M -> ~11.9, $10M+ -> 14.
    lo, hi = FLOW["min_alert_premium"], 10_000_000
    prem = _clamp(f["total_premium"], lo, hi)
    prem_pts = p["premium"] * math.log(prem / lo) / math.log(hi / lo)
    if f["total_premium"] >= 5_000_000:
        notes.append(f"${f['total_premium'] / 1e6:.1f}M premium")

    # Aggression — paying the offer rather than resting on the bid.
    agg = f.get("aggression")
    if agg is None:
        agg_pts = p["aggression"] * 0.4      # unknown: partial credit, not zero
    else:
        # 0.55 (the gate) -> 0 pts, 0.90+ -> full marks.
        agg_pts = p["aggression"] * _clamp((agg - FLOW["min_aggression_pct"]) / 0.35, 0.0, 1.0)
        if agg >= 0.80:
            notes.append(f"{agg:.0%} aggressive fills")

    # Structure — sweeps, floor prints, and the alert rules that fired.
    struct = 0.0
    if f["sweep_share"] >= 0.30:
        struct += 3.0
        notes.append("sweep-dominated")
    elif f["sweep_share"] > 0:
        struct += 1.5
    if f["floor_share"] >= 0.30:
        struct += 2.0
        notes.append("floor prints")
    struct += max((FLOW["rule_bonus"].get(r, 0.0) for r in f["rules"]), default=0.0)
    struct_pts = _clamp(struct, 0.0, p["structure"])

    # Opening interest — new positioning beats closing an old one out.
    open_pts = 0.0
    if f["opening_share"] >= 0.50:
        open_pts += 4.0
        notes.append("opening activity")
    elif f["opening_share"] > 0:
        open_pts += 2.0
    if (f.get("max_volume_oi") or 0) >= FLOW["min_volume_oi_ratio"]:
        open_pts += 2.0
    open_pts = _clamp(open_pts, 0.0, p["opening"])

    # Coherence + repetition — one-sided flow worked across strikes/days.
    coh_pts = p["coherence"] * 0.7 * _clamp(f["coherence"], 0.0, 1.0)
    if f["n_alerts"] >= FLOW["min_alerts_for_cluster"] and f["n_strikes"] >= 2:
        coh_pts += p["coherence"] * 0.15
        notes.append(f"{f['n_alerts']} alerts / {f['n_strikes']} strikes")
    if f["n_days"] >= 2:
        coh_pts += p["coherence"] * 0.15
        notes.append("multi-day accumulation")
    coh_pts = _clamp(coh_pts, 0.0, p["coherence"])

    total = prem_pts + agg_pts + struct_pts + open_pts + coh_pts

    # DTE fit — a haircut, not a gate. The band is already enforced upstream.
    avg_dte = f.get("avg_dte")
    if avg_dte is not None:
        ideal_lo, ideal_hi = FLOW["ideal_dte"]
        if avg_dte < ideal_lo:
            total *= 0.90
            notes.append(f"short-dated ({avg_dte:.0f}d avg)")
        elif avg_dte > ideal_hi:
            total *= 0.95
            notes.append(f"long-dated ({avg_dte:.0f}d avg)")

    return _clamp(total, 0.0, WEIGHTS["flow"]), notes


# ---------------------------------------------------------------------------
# Component 2 — dark-pool confirmation (max 15)
# ---------------------------------------------------------------------------
def score_darkpool(dp: dict, direction: str) -> tuple[float, list[str]]:
    """No data -> 0 points and an explicit "no dark-pool data" note.

    Scored points require the blocks to agree with the flow's direction:
    accumulation confirms longs, distribution confirms shorts.
    """
    if not dp.get("has_data") or not dp.get("n_blocks"):
        return 0.0, ["no dark-pool blocks"]

    notes: list[str] = []
    pts = 0.0

    # Size of the off-exchange footprint relative to average daily volume.
    pct = dp.get("pct_of_adv")
    if pct is not None:
        lo, hi = DARKPOOL["min_pct_of_adv"], DARKPOOL["strong_pct_of_adv"]
        pts += 7.0 * _clamp((pct - lo) / (hi - lo), 0.0, 1.0)
        if pct >= hi:
            notes.append(f"blocks = {pct:.0%} of ADV")

    # Execution lean vs the NBBO midpoint, signed to the trade direction.
    lean = dp.get("lean")
    if lean is not None:
        aligned = lean if direction == "long" else -lean
        pts += 5.0 * _clamp(aligned, 0.0, 1.0)
        if aligned >= 0.3:
            notes.append("blocks above mid (accumulation)" if direction == "long"
                         else "blocks below mid (distribution)")
        elif aligned <= -0.3:
            notes.append("dark-pool lean opposes the options flow")

    # A single outsized print carries information on its own.
    if dp.get("biggest_block", 0) >= DARKPOOL["strong_block_premium"]:
        pts += 3.0
        notes.append(f"${dp['biggest_block'] / 1e6:.0f}M single block")

    return _clamp(pts, 0.0, WEIGHTS["darkpool"]), notes


# ---------------------------------------------------------------------------
# Component 3 — technical confluence (max 35)
# ---------------------------------------------------------------------------
def score_technical(t: dict, direction: str) -> tuple[float, list[str]]:
    """Mirrored for shorts: below the EMAs, RSI 30-55, breakdown structure."""
    if not t or t.get("last") is None:
        return 0.0, ["no chart data"]

    notes: list[str] = []
    pts = TECH_POINTS
    long_side = direction == "long"
    last = t["last"]
    ef, es = t.get("ema_fast"), t.get("ema_slow")

    # --- Trend structure (12) ---
    trend = 0.0
    if ef and es:
        if long_side:
            if last > ef > es:
                trend = pts["trend"]
                notes.append("price > 50 EMA > 200 EMA")
            elif last > es and last > ef:
                trend = pts["trend"] * 0.6
                notes.append("above both EMAs, 50 below 200")
            elif last > es:
                trend = pts["trend"] * 0.35
                notes.append("above 200 EMA, below 50 EMA")
        else:
            if last < ef < es:
                trend = pts["trend"]
                notes.append("price < 50 EMA < 200 EMA")
            elif last < es and last < ef:
                trend = pts["trend"] * 0.6
            elif last < es:
                trend = pts["trend"] * 0.35
        # Slope confirms the structure is live rather than flat.
        slope = t.get("ema_fast_slope_pct")
        if slope is not None:
            if (long_side and slope < 0) or (not long_side and slope > 0):
                trend *= 0.75
                notes.append("50 EMA slope against the trade")

    # --- Extension (5): reward proximity to the 50 EMA, punish the chase ---
    ext = 0.0
    dist = t.get("atr_from_ema_fast")
    if dist is not None:
        d = dist if long_side else -dist
        if d < 0:
            ext = pts["extension"] * 0.5           # pulled back through the EMA
        elif d <= TECHNICAL["max_atr_from_ema_fast"]:
            ext = pts["extension"] * (1.0 - d / TECHNICAL["max_atr_from_ema_fast"] * 0.5)
        else:
            ext = 0.0
            notes.append(f"extended {d:.1f} ATR from 50 EMA")

    # --- RSI behaviour (8) ---
    r = t.get("rsi")
    rsi_pts = 0.0
    if r is not None:
        lo, hi = TECHNICAL["rsi_constructive"]
        rlo, rhi = TECHNICAL["rsi_recovering"]
        if long_side:
            if lo <= r <= hi:
                rsi_pts = pts["rsi"]
            elif rlo <= r < lo:
                rsi_pts = pts["rsi"] * 0.6
                notes.append(f"RSI {r:.0f} recovering")
            elif r > TECHNICAL["rsi_overbought"]:
                rsi_pts = pts["rsi"] * 0.25
                notes.append(f"RSI {r:.0f} overbought — chase risk")
            elif hi < r <= TECHNICAL["rsi_overbought"]:
                rsi_pts = pts["rsi"] * 0.7
        else:
            if (100 - hi) <= r <= (100 - lo):
                rsi_pts = pts["rsi"]
            elif r < 25:
                rsi_pts = pts["rsi"] * 0.25
                notes.append(f"RSI {r:.0f} oversold — chase risk")
            elif r <= (100 - hi):
                rsi_pts = pts["rsi"] * 0.7
        # Momentum agreement over the past week.
        rp = t.get("rsi_prev")
        if rp is not None:
            if (long_side and r > rp) or (not long_side and r < rp):
                rsi_pts = min(pts["rsi"], rsi_pts + 1.0)

    # --- Volume (6) ---
    vr = t.get("volume_ratio")
    vol_pts = 0.0
    if vr is not None:
        if vr >= TECHNICAL["vol_surge_ratio"]:
            vol_pts = pts["volume"]
            notes.append(f"volume {vr:.1f}x average")
        elif vr >= 1.0:
            vol_pts = pts["volume"] * 0.5
        else:
            vol_pts = pts["volume"] * 0.2

    # --- Structure: breakout / proximity to the 52-week extreme (4) ---
    st = 0.0
    if long_side:
        if t.get("breakout_long"):
            st += 2.5
            notes.append("20-day breakout")
        pfh = t.get("pct_from_52w_high")
        if pfh is not None and pfh <= TECHNICAL["near_high_pct"]:
            st += 1.5
            notes.append(f"{pfh:.0%} from 52w high")
    else:
        if t.get("breakout_short"):
            st += 2.5
            notes.append("20-day breakdown")
        pfl = t.get("pct_from_52w_low")
        if pfl is not None and pfl <= TECHNICAL["near_high_pct"]:
            st += 1.5
    st = _clamp(st, 0.0, pts["structure"])

    total = trend + ext + rsi_pts + vol_pts + st
    return _clamp(total, 0.0, WEIGHTS["technical"]), notes


# ---------------------------------------------------------------------------
# Component 4 — liquidity / tradeability (max 10)
# ---------------------------------------------------------------------------
def score_liquidity(f: dict, t: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    pts = 0.0
    price = t.get("last") or f.get("underlying_price") or 0.0
    adv = t.get("avg_volume") or f.get("avg30_volume") or 0.0

    # Dollar volume: $25M (the gate) -> 0, $500M+ -> full 6 points.
    dv = adv * price
    if dv > 0:
        lo, hi = UNIVERSE["min_dollar_volume"], 500_000_000
        pts += 6.0 * _clamp(math.log(max(dv, lo) / lo) / math.log(hi / lo), 0.0, 1.0)

    # Option spread on the alerted contracts — tight chains are tradeable.
    spreads = [a["spread_pct"] for a in f.get("alerts", []) if a.get("spread_pct") is not None]
    if spreads:
        avg_spread = sum(spreads) / len(spreads)
        pts += 4.0 * _clamp(1.0 - avg_spread / UNIVERSE["max_option_spread_pct"], 0.0, 1.0)
        if avg_spread > 0.10:
            notes.append(f"wide option spread ({avg_spread:.0%})")
    else:
        pts += 2.0    # unknown spread: partial credit

    # ATR sanity — a name that moves <1% a day won't pay a swing in two weeks.
    atr_pct = t.get("atr_pct")
    if atr_pct is not None and atr_pct < 1.0:
        pts *= 0.8
        notes.append(f"low volatility (ATR {atr_pct:.1f}%)")

    return _clamp(pts, 0.0, WEIGHTS["liquidity"]), notes


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------
def add_risk_levels(c: Candidate) -> None:
    """ATR-based entry/stop/targets plus a position size at fixed fractional risk."""
    t = c.tech
    price, a = t.get("last"), t.get("atr")
    if not price or not a:
        return

    mult = RISK["stop_atr_multiple"]
    r1, r2 = RISK["target_r_multiples"]
    long_side = c.direction == "long"

    c.entry = round(price, 2)
    if long_side:
        # Prefer the structural stop (below the 50 EMA) when it is tighter
        # than the raw ATR stop — the EMA is where the thesis actually breaks.
        atr_stop = price - mult * a
        ema_stop = (t["ema_fast"] - 0.5 * a) if t.get("ema_fast") else None
        stop = max(atr_stop, ema_stop) if (ema_stop and ema_stop < price) else atr_stop
    else:
        atr_stop = price + mult * a
        ema_stop = (t["ema_fast"] + 0.5 * a) if t.get("ema_fast") else None
        stop = min(atr_stop, ema_stop) if (ema_stop and ema_stop > price) else atr_stop

    c.stop = round(stop, 2)
    risk_per_share = abs(c.entry - c.stop)
    if risk_per_share <= 0:
        c.stop = None
        return

    sign = 1.0 if long_side else -1.0
    c.target1 = round(c.entry + sign * r1 * risk_per_share, 2)
    c.target2 = round(c.entry + sign * r2 * risk_per_share, 2)
    c.rr = round(r2, 1)

    # Fixed-fractional sizing, halved for B-tier ideas.
    risk_budget = RISK["account_size"] * RISK["risk_per_trade_pct"]
    if c.tier == "B":
        risk_budget *= RISK["b_tier_risk_multiplier"]
    elif c.tier == "C":
        risk_budget = 0.0        # watchlist only — no size until it upgrades

    c.risk_dollars = round(risk_budget, 2)
    c.shares = int(risk_budget // risk_per_share) if risk_budget else 0


def assign_tier(score: float) -> str:
    for cutoff, tier in TIERS:
        if score >= cutoff:
            return tier
    return "-"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def build_candidate(flow: dict, tech: dict, dp: dict,
                    allow_earnings: bool = False,
                    today: _dt.date | None = None) -> Candidate:
    """Score one ticker end to end. Rejected candidates carry their reason."""
    c = Candidate(
        ticker=flow["ticker"],
        direction=flow["direction"],
        sector=flow.get("sector") or "",
        flow=flow, dp=dp, tech=tech,
    )

    reason = check_gates(flow, tech, allow_earnings=allow_earnings, today=today)
    if reason:
        c.rejected = reason
        return c

    c.flow_score, fn = score_flow(flow)
    c.dp_score, dn = score_darkpool(dp, flow["direction"])
    c.tech_score, tn = score_technical(tech, flow["direction"])
    c.liq_score, ln = score_liquidity(flow, tech)

    c.score = round(c.flow_score + c.dp_score + c.tech_score + c.liq_score, 1)
    c.notes = fn + dn + tn + ln
    c.tier = assign_tier(c.score)

    if c.score < MIN_SCORE:
        c.rejected = f"score {c.score:.1f} below {MIN_SCORE:.0f}"
        return c

    add_risk_levels(c)

    # A trade that cannot pay the minimum reward:risk isn't worth the slot.
    if c.stop is None:
        c.rejected = "no ATR — cannot define risk"
    elif c.rr is not None and c.rr < RISK["min_reward_risk"]:
        c.rejected = f"reward:risk {c.rr:.1f} below {RISK['min_reward_risk']:.1f}"

    # Advisory flags — these do not reject, they just travel with the idea.
    er = flow.get("next_earnings")
    if er:
        days = (er - (today or _dt.date.today())).days
        if 0 <= days <= 30:
            c.flags.append(f"earnings in {days}d")
    if not dp.get("has_data"):
        c.flags.append("no dark-pool confirmation")
    if flow["coherence"] < 0.3:
        c.flags.append("two-way flow")

    return c


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Sort surviving candidates best-first and apply portfolio-level caps."""
    live = [c for c in candidates if not c.rejected]
    live.sort(key=lambda c: c.score, reverse=True)

    per_sector: dict[str, int] = {}
    for c in live:
        sec = c.sector or "Unknown"
        per_sector[sec] = per_sector.get(sec, 0) + 1
        if per_sector[sec] > RISK["max_per_sector"]:
            c.flags.append(f"over sector cap ({sec})")
    return live
