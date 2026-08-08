"""Whale / smart-money conviction layer for the sector-rotation strategy.

Turns Unusual Whales sector data into a per-sector conviction score that
confirms or vetoes the momentum-based rotation picks. Built from the single
`get_market_sector_etfs` call, which for each sector ETF provides:

  - bullish_premium / bearish_premium  -> net options-flow direction
  - call_premium / put_premium         -> options activity bias
  - in_out_flow (recent daily net share flow) -> accumulation vs distribution

The conviction score is in [-1, +1]: positive = whales confirm the long side
(bullish flow + accumulation), negative = distribution / bearish flow.

Note on scope: individual dark-pool prints and rule-based flow alerts are
dominated by SPY/QQQ/bonds at the market level and are sparse for the sector
ETFs themselves, so the accumulation read here uses the sector ETF's net
in/out share flow (institutional accumulation) rather than per-print tape.
"""

from __future__ import annotations

import math


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_sector_etfs(payload) -> dict[str, dict]:
    """Parse a get_market_sector_etfs response into per-ticker whale metrics."""
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    out: dict[str, dict] = {}
    for r in rows:
        tk = str(r.get("ticker", "")).upper()
        if not tk:
            continue
        bull, bear = _f(r.get("bullish_premium")), _f(r.get("bearish_premium"))
        callp, putp = _f(r.get("call_premium")), _f(r.get("put_premium"))
        flow5d = sum(_f(x.get("change")) for x in (r.get("in_out_flow") or []))
        out[tk] = {
            "bull": bull, "bear": bear,
            "net_prem": bull - bear,
            "call_prem": callp, "put_prem": putp,
            "flow_5d": flow5d,
            "avg_vol": _f(r.get("avg30_stock_volume")),
        }
    return out


def whale_score(m: dict) -> float:
    """Combine net premium, options bias, and accumulation into [-1, +1]."""
    tot = m["bull"] + m["bear"]
    net_norm = (m["bull"] - m["bear"]) / tot if tot else 0.0

    cp = m["call_prem"] + m["put_prem"]
    cp_norm = (m["call_prem"] - m["put_prem"]) / cp if cp else 0.0

    # 5-day net share flow scaled by average daily volume, bounded via tanh
    flow_norm = math.tanh(m["flow_5d"] / m["avg_vol"]) if m["avg_vol"] else 0.0

    score = 0.45 * net_norm + 0.20 * cp_norm + 0.35 * flow_norm
    return max(-1.0, min(1.0, score))


def label(score: float) -> str:
    if score >= 0.33:
        return "strong bullish"
    if score >= 0.10:
        return "bullish"
    if score <= -0.33:
        return "strong bearish"
    if score <= -0.10:
        return "bearish"
    return "neutral"


def attach(scores, whale_by_ticker: dict[str, dict]) -> int:
    """Attach whale metrics + conviction to SectorScore objects. Returns count."""
    n = 0
    for s in scores:
        m = whale_by_ticker.get(s.ticker)
        if not m:
            continue
        s.whale_net_prem = m["net_prem"]
        s.whale_flow_5d = m["flow_5d"]
        s.whale_score = whale_score(m)
        s.whale_label = label(s.whale_score)
        n += 1
    return n
