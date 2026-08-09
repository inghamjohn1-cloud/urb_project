"""Pure-Python technical indicators for the single-name swing scanner.

No numpy/pandas — the whole repo runs on the standard library. Every function
here takes plain lists of floats (or candle dicts) ordered OLDEST -> NEWEST and
returns either a single latest value or a full series.

The definitions deliberately match TradingView's built-ins so a number printed
by the scanner is the same number you read off the chart:
  - ema()  == ta.ema()   (SMA seed, then 2/(n+1) smoothing)
  - rsi()  == ta.rsi()   (Wilder's RMA smoothing)
  - atr()  == ta.atr()   (Wilder's RMA of true range)
"""

from __future__ import annotations

from swing_config import TECHNICAL


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------
def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def closes(candles: list[dict]) -> list[float]:
    return [c["close"] for c in candles if c.get("close") is not None]


def volumes(candles: list[dict]) -> list[float]:
    return [c.get("volume") or 0.0 for c in candles]


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def ema_series(values: list[float], length: int) -> list[float | None]:
    """TradingView-compatible EMA: SMA seed over the first `length` bars."""
    out: list[float | None] = [None] * len(values)
    if len(values) < length or length < 1:
        return out
    k = 2.0 / (length + 1.0)
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def ema(values: list[float], length: int) -> float | None:
    s = ema_series(values, length)
    return s[-1] if s else None


def sma(values: list[float], length: int) -> float | None:
    if len(values) < length or length < 1:
        return None
    return sum(values[-length:]) / length


def rma_series(values: list[float], length: int) -> list[float | None]:
    """Wilder's smoothing (RMA) — the basis of RSI and ATR."""
    out: list[float | None] = [None] * len(values)
    if len(values) < length or length < 1:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Oscillators / volatility
# ---------------------------------------------------------------------------
def rsi_series(values: list[float], length: int = 14) -> list[float | None]:
    """Wilder RSI. Returns a series aligned to `values` (None until seeded)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < length + 1:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = values[i] - values[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)

    # Wilder seeds on the first `length` changes, i.e. indices 1..length.
    avg_gain = sum(gains[1:length + 1]) / length
    avg_loss = sum(losses[1:length + 1]) / length
    out[length] = _rsi_from(avg_gain, avg_loss)

    for i in range(length + 1, n):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(values: list[float], length: int = 14) -> float | None:
    s = rsi_series(values, length)
    return s[-1] if s else None


def true_ranges(candles: list[dict]) -> list[float]:
    trs: list[float] = []
    for i in range(1, len(candles)):
        high, low = _f(candles[i].get("high")), _f(candles[i].get("low"))
        prev_close = _f(candles[i - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return trs


def atr(candles: list[dict], length: int = 14) -> float | None:
    """Wilder ATR — matches TradingView's ta.atr()."""
    trs = true_ranges(candles)
    if len(trs) < length:
        return None
    s = rma_series(trs, length)
    return s[-1]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def pct_from_high(candles: list[dict], lookback: int = 252) -> float | None:
    """How far below the N-day high the last close sits, as a fraction."""
    window = candles[-lookback:]
    highs = [_f(c.get("high")) for c in window]
    highs = [h for h in highs if h is not None]
    last = _f(window[-1].get("close")) if window else None
    if not highs or last is None or max(highs) == 0:
        return None
    return (max(highs) - last) / max(highs)


def pct_from_low(candles: list[dict], lookback: int = 252) -> float | None:
    window = candles[-lookback:]
    lows = [_f(c.get("low")) for c in window]
    lows = [l for l in lows if l is not None]
    last = _f(window[-1].get("close")) if window else None
    if not lows or last is None or last == 0:
        return None
    return (last - min(lows)) / last


def is_breakout(candles: list[dict], lookback: int = 20, side: str = "long") -> bool | None:
    """Last close above the prior N-day high (long) / below the low (short)."""
    if len(candles) < lookback + 1:
        return None
    prior = candles[-lookback - 1:-1]
    last = _f(candles[-1].get("close"))
    if last is None:
        return None
    if side == "long":
        highs = [_f(c.get("high")) for c in prior]
        highs = [h for h in highs if h is not None]
        return bool(highs) and last > max(highs)
    lows = [_f(c.get("low")) for c in prior]
    lows = [l for l in lows if l is not None]
    return bool(lows) and last < min(lows)


def volume_ratio(candles: list[dict], length: int = 20) -> float | None:
    """Latest bar volume divided by the trailing average (excluding today)."""
    vols = [v for v in volumes(candles) if v]
    if len(vols) < length + 1:
        return None
    avg = sum(vols[-length - 1:-1]) / length
    if avg == 0:
        return None
    return vols[-1] / avg


def avg_volume(candles: list[dict], length: int = 30) -> float | None:
    vols = [v for v in volumes(candles) if v]
    if len(vols) < length:
        return sum(vols) / len(vols) if vols else None
    return sum(vols[-length:]) / length


# ---------------------------------------------------------------------------
# Bundled technical snapshot
# ---------------------------------------------------------------------------
def snapshot(candles: list[dict]) -> dict:
    """Compute every technical field the scorer needs from daily candles.

    Returns a dict with None for anything there isn't enough history for, so
    the caller can degrade gracefully instead of raising.
    """
    cs = closes(candles)
    t = TECHNICAL
    if not cs:
        return {}

    ema_f = ema(cs, t["ema_fast"])
    ema_s = ema(cs, t["ema_slow"])
    ema_f_series = ema_series(cs, t["ema_fast"])
    a = atr(candles, t["atr_len"])
    last = cs[-1]

    # Slope of the fast EMA over the past 10 bars, in percent — a cheap read
    # on whether the trend is actually moving or just flat-and-crossed.
    slope = None
    if len(ema_f_series) > 10 and ema_f_series[-11] and ema_f_series[-1]:
        slope = (ema_f_series[-1] / ema_f_series[-11] - 1.0) * 100.0

    # Distance from the fast EMA expressed in ATRs (extension / chase risk).
    atr_from_ema = None
    if a and ema_f:
        atr_from_ema = (last - ema_f) / a

    return {
        "last": last,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "ema_fast_slope_pct": slope,
        "rsi": rsi(cs, t["rsi_len"]),
        "rsi_prev": (rsi_series(cs, t["rsi_len"])[-6]
                     if len(cs) > t["rsi_len"] + 6 else None),
        "atr": a,
        "atr_pct": (a / last * 100.0) if (a and last) else None,
        "atr_from_ema_fast": atr_from_ema,
        "volume_ratio": volume_ratio(candles, t["vol_avg_len"]),
        "avg_volume": avg_volume(candles, 30),
        "pct_from_52w_high": pct_from_high(candles, 252),
        "pct_from_52w_low": pct_from_low(candles, 252),
        "breakout_long": is_breakout(candles, t["breakout_lookback"], "long"),
        "breakout_short": is_breakout(candles, t["breakout_lookback"], "short"),
        "bars": len(candles),
    }
