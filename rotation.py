"""Momentum / relative-strength scoring for a sector-rotation swing strategy.

Pure-Python indicators (no numpy/pandas) so the only dependency is `requests`
(actually just the stdlib UW client). Everything works on plain lists of floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# The 11 SPDR sector ETFs mapped to the sector names the UW sector-tide
# endpoint expects. SPY is the benchmark (not a rotation candidate).
SECTOR_ETFS: dict[str, dict[str, str]] = {
    "XLK": {"name": "Technology", "uw_sector": "Technology"},
    "XLF": {"name": "Financials", "uw_sector": "Financial Services"},
    "XLE": {"name": "Energy", "uw_sector": "Energy"},
    "XLV": {"name": "Health Care", "uw_sector": "Healthcare"},
    "XLI": {"name": "Industrials", "uw_sector": "Industrials"},
    "XLY": {"name": "Consumer Discretionary", "uw_sector": "Consumer Cyclical"},
    "XLP": {"name": "Consumer Staples", "uw_sector": "Consumer Defensive"},
    "XLU": {"name": "Utilities", "uw_sector": "Utilities"},
    "XLB": {"name": "Materials", "uw_sector": "Basic Materials"},
    "XLRE": {"name": "Real Estate", "uw_sector": "Real Estate"},
    "XLC": {"name": "Communication Services", "uw_sector": "Communication Services"},
}

BENCHMARK = "SPY"

# Defensive sectors favoured when the market regime is risk-off.
DEFENSIVE = {"XLP", "XLU", "XLV"}

# Lookback windows in trading days.
LB_1M = 21
LB_3M = 63
LB_6M = 126
SMA_FAST = 50
SMA_SLOW = 200
ATR_LEN = 14

# Blend weights for the composite momentum figure.
MOM_WEIGHTS = {"1m": 0.30, "3m": 0.40, "6m": 0.30}


def _close(candles: list[dict]) -> list[float]:
    return [float(c["close"]) for c in candles if c.get("close") is not None]


def pct_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    past = closes[-1 - lookback]
    if past == 0:
        return None
    return (closes[-1] / past - 1.0) * 100.0


def sma(closes: list[float], length: int) -> float | None:
    if len(closes) < length:
        return None
    return sum(closes[-length:]) / length


def atr(candles: list[dict], length: int = ATR_LEN) -> float | None:
    """Wilder ATR on daily candles."""
    if len(candles) < length + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        try:
            high = float(candles[i]["high"])
            low = float(candles[i]["low"])
            prev_close = float(candles[i - 1]["close"])
        except (KeyError, TypeError, ValueError):
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < length:
        return None
    return sum(trs[-length:]) / length


@dataclass
class SectorScore:
    ticker: str
    name: str
    last: float
    ret_1m: float | None
    ret_3m: float | None
    ret_6m: float | None
    rs_3m: float | None          # relative strength vs benchmark over 3m
    momentum: float | None       # blended composite return
    above_sma50: bool | None
    above_sma200: bool | None
    uptrend: bool | None         # price>SMA50 and SMA50>SMA200
    atr: float | None
    flow_bull: bool | None = None    # options-flow confirmation (sector tide)
    net_call_premium: float | None = None
    live_price: float | None = None  # real-time price (Massive snapshot)
    live_chg: float | None = None    # today's % change (Massive snapshot)
    whale_score: float | None = None # smart-money conviction [-1, +1]
    whale_net_prem: float | None = None  # net options premium (bull - bear)
    whale_flow_5d: float | None = None   # 5-day net in/out share flow
    whale_label: str | None = None
    score: float = 0.0
    rank: int = 0
    signal: str = ""
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    notes: list[str] = field(default_factory=list)


def build_score(ticker: str, candles: list[dict], bench_closes: list[float]) -> SectorScore | None:
    closes = _close(candles)
    if len(closes) < LB_1M + 1:
        return None

    r1, r3, r6 = pct_return(closes, LB_1M), pct_return(closes, LB_3M), pct_return(closes, LB_6M)
    bench_r3 = pct_return(bench_closes, LB_3M)
    rs_3m = (r3 - bench_r3) if (r3 is not None and bench_r3 is not None) else None

    parts = {"1m": r1, "3m": r3, "6m": r6}
    avail = {k: v for k, v in parts.items() if v is not None}
    if avail:
        wsum = sum(MOM_WEIGHTS[k] for k in avail)
        momentum = sum(MOM_WEIGHTS[k] * v for k, v in avail.items()) / wsum
    else:
        momentum = None

    s50, s200 = sma(closes, SMA_FAST), sma(closes, SMA_SLOW)
    above50 = closes[-1] > s50 if s50 is not None else None
    above200 = closes[-1] > s200 if s200 is not None else None
    uptrend = None
    if s50 is not None and s200 is not None:
        uptrend = closes[-1] > s50 and s50 > s200

    return SectorScore(
        ticker=ticker,
        name=SECTOR_ETFS.get(ticker, {}).get("name", ticker),
        last=closes[-1],
        ret_1m=r1, ret_3m=r3, ret_6m=r6,
        rs_3m=rs_3m, momentum=momentum,
        above_sma50=above50, above_sma200=above200, uptrend=uptrend,
        atr=atr(candles),
    )


def _rank_percentile(values: list[float | None]) -> list[float]:
    """Percentile rank (0..100) of each value; None -> 0."""
    idx = [(i, v) for i, v in enumerate(values) if v is not None]
    out = [0.0] * len(values)
    if not idx:
        return out
    ordered = sorted(idx, key=lambda t: t[1])
    n = len(ordered)
    for rank, (orig_i, _) in enumerate(ordered):
        out[orig_i] = (rank / (n - 1) * 100.0) if n > 1 else 100.0
    return out


def rank_sectors(scores: list[SectorScore], top_n: int = 3, bottom_n: int = 3,
                 risk_off: bool = False, whale_weight: float = 0.0) -> list[SectorScore]:
    """Compute composite scores, rank, and assign rotation signals in place.

    whale_weight: points (per unit of conviction in [-1,1]) added from the
    smart-money layer, so bullish flow/accumulation lifts a sector and
    distribution/bearish flow drags it down. 0 disables the whale layer.
    """
    mom_pct = _rank_percentile([s.momentum for s in scores])
    rs_pct = _rank_percentile([s.rs_3m for s in scores])

    for s, mp, rp in zip(scores, mom_pct, rs_pct):
        # 60% absolute momentum, 40% relative strength vs benchmark
        base = 0.6 * mp + 0.4 * rp
        # trend gate: haircut names not in a clean uptrend
        if s.uptrend is False:
            base *= 0.5
        if s.above_sma200 is False:
            base *= 0.6
        # in risk-off regimes nudge defensives up, offensives down
        if risk_off and s.ticker in DEFENSIVE:
            base += 8
        # smart-money conviction confirms or vetoes the momentum pick
        if whale_weight and s.whale_score is not None:
            base += whale_weight * s.whale_score
        s.score = round(max(0.0, min(100.0, base)), 1)

    scores.sort(key=lambda s: s.score, reverse=True)
    for i, s in enumerate(scores, 1):
        s.rank = i

    for i, s in enumerate(scores):
        _assign_signal(s, i, len(scores), top_n, bottom_n)
    return scores


def _assign_signal(s: SectorScore, idx: int, total: int, top_n: int, bottom_n: int) -> None:
    healthy_trend = s.above_sma50 is not False and s.above_sma200 is not False
    if idx < top_n and healthy_trend and (s.momentum or 0) > 0:
        s.signal = "ROTATE IN"
    elif idx >= total - bottom_n or s.above_sma200 is False:
        s.signal = "ROTATE OUT"
    else:
        s.signal = "HOLD"

    # ATR-based swing levels for the actionable (rotate-in) names
    if s.signal == "ROTATE IN" and s.atr:
        s.entry = round(s.last, 2)
        s.stop = round(s.last - 2.0 * s.atr, 2)
        s.target = round(s.last + 3.0 * s.atr, 2)

    if s.uptrend is False:
        s.notes.append("below 50/200 trend")
    if s.rs_3m is not None and s.rs_3m > 0:
        s.notes.append("outperforming SPY")
    if s.flow_bull is True:
        s.notes.append("bullish flow")
    elif s.flow_bull is False:
        s.notes.append("bearish flow")


def market_regime(bench_candles: list[dict]) -> tuple[str, str]:
    """Classify the overall regime from the benchmark's trend.

    Returns (label, detail). Risk-off when SPY is below its 200-day SMA.
    """
    closes = _close(bench_candles)
    s200 = sma(closes, SMA_SLOW)
    s50 = sma(closes, SMA_FAST)
    if not closes or s200 is None:
        return "UNKNOWN", "not enough benchmark history"
    last = closes[-1]
    if last > s200 and (s50 is None or s50 > s200):
        return "RISK-ON", f"{BENCHMARK} {last:.2f} above 200d SMA {s200:.2f}"
    if last < s200:
        return "RISK-OFF", f"{BENCHMARK} {last:.2f} below 200d SMA {s200:.2f}"
    return "NEUTRAL", f"{BENCHMARK} {last:.2f} near 200d SMA {s200:.2f}"
