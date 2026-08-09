"""Thin REST client for Massive market data (daily bars + snapshots).

Massive is the price/liquidity engine for the swing scanner: it supplies the
adjusted daily OHLCV history that the EMA / RSI / ATR / volume rules are
computed from, and a live snapshot for the intraday price column.

Auth is a bearer token from MASSIVE_API_KEY. Endpoints used:

    GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}   daily bars
    GET /v2/snapshot/locale/us/markets/stocks/tickers      live snapshot

There is also an offline path: `load_candles_dir()` reads the CSV/JSON files
that Massive's MCP `call_api` tool writes, so you can drive the whole scanner
from MCP output without a REST key (same pattern as render_from_massive.py).
"""

from __future__ import annotations

import csv as _csv
import datetime as _dt
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.app")


class MassiveError(RuntimeError):
    """Raised when Massive returns an error or is unreachable."""


class MassiveClient:
    def __init__(self, api_key: str | None = None, timeout: int = 30, max_retries: int = 3):
        self.api_key = api_key or os.environ.get("MASSIVE_API_KEY")
        if not self.api_key:
            raise MassiveError(
                "No Massive API key found. Set MASSIVE_API_KEY in your environment "
                "or a .env file (see .env.example), or run with --data-dir to use "
                "files exported by the Massive MCP tools."
            )
        self.timeout = timeout
        self.max_retries = max_retries

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = BASE_URL + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "urb-swing-scanner/1.0",
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:300]
                if e.code != 429 and 400 <= e.code < 500:
                    raise MassiveError(f"{e.code} on {path}: {detail}") from e
                last_err = MassiveError(f"{e.code} on {path}: {detail}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = MassiveError(f"request to {path} failed: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)

        raise last_err or MassiveError(f"request to {path} failed")

    # -- endpoints ---------------------------------------------------------
    def daily_candles(self, ticker: str, days: int = 400) -> list[dict]:
        """Adjusted daily bars, oldest -> newest, normalised to our schema."""
        end = _dt.date.today()
        # Calendar days, padded for weekends/holidays so we get `days` bars.
        start = end - _dt.timedelta(days=int(days * 1.5) + 10)
        path = (f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/range/1/day/"
                f"{start.isoformat()}/{end.isoformat()}")
        payload = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return normalize_bars(payload.get("results") or [])

    def snapshot(self, tickers: list[str]) -> dict[str, dict]:
        """Live snapshot keyed by ticker (best effort — never fatal)."""
        payload = self._get("/v2/snapshot/locale/us/markets/stocks/tickers",
                            {"tickers": ",".join(tickers)})
        out: dict[str, dict] = {}
        for row in (payload.get("tickers") or []):
            tk = str(row.get("ticker", "")).upper()
            if tk:
                out[tk] = row
        return out


# ---------------------------------------------------------------------------
# Normalisation — Massive's short keys (o/h/l/c/v/t) -> our candle schema
# ---------------------------------------------------------------------------
def normalize_bars(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        try:
            candle = {
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": float(r.get("v") or 0.0),
                "t": r.get("t"),
            }
        except (KeyError, TypeError, ValueError):
            continue
        ts = r.get("t")
        if ts:
            try:
                candle["date"] = _dt.datetime.utcfromtimestamp(
                    float(ts) / 1000.0).date().isoformat()
            except (TypeError, ValueError, OSError):
                candle["date"] = None
        out.append(candle)
    out.sort(key=lambda c: c.get("t") or 0)
    return out


# ---------------------------------------------------------------------------
# Offline path — read files exported by the Massive MCP tools
# ---------------------------------------------------------------------------
def load_candles_file(path: str) -> list[dict]:
    """Parse one ticker's daily bars from a Massive CSV or JSON export."""
    with open(path) as fh:
        raw = fh.read().strip()
    if not raw:
        return []

    if raw[:1] in "[{":
        payload = json.loads(raw)
        if isinstance(payload, dict):
            # call_api sometimes wraps a CSV string in {"result": "..."}
            if isinstance(payload.get("result"), str):
                return _parse_csv(payload["result"])
            rows = payload.get("results") or payload.get("result") or []
        else:
            rows = payload
        return normalize_bars(rows if isinstance(rows, list) else [])

    return _parse_csv(raw)


def _parse_csv(text: str) -> list[dict]:
    rows = list(_csv.DictReader(io.StringIO(text.strip())))
    # Tolerate both short (o/h/l/c/v/t) and long (open/high/...) headers.
    remapped = []
    for r in rows:
        remapped.append({
            "o": r.get("o", r.get("open")),
            "h": r.get("h", r.get("high")),
            "l": r.get("l", r.get("low")),
            "c": r.get("c", r.get("close")),
            "v": r.get("v", r.get("volume")),
            "t": r.get("t", r.get("timestamp")),
        })
    return normalize_bars(remapped)


def load_candles_dir(data_dir: str) -> dict[str, list[dict]]:
    """Load every <TICKER>.csv / <TICKER>.json in a directory."""
    out: dict[str, list[dict]] = {}
    if not os.path.isdir(data_dir):
        return out
    for name in sorted(os.listdir(data_dir)):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".csv", ".json") or stem.startswith("_"):
            continue
        try:
            candles = load_candles_file(os.path.join(data_dir, name))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if candles:
            out[stem.upper()] = candles
    return out
