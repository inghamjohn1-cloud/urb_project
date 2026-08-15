"""Thin REST client for the Unusual Whales public API.

Only the endpoints needed by the sector-rotation scanner are wrapped here.
Auth uses a bearer token read from the UW_TOKEN environment variable.

Docs: https://api.unusualwhales.com/docs
"""

from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request
import urllib.error
import json
from typing import Any


BASE_URL = "https://api.unusualwhales.com"


class UWError(RuntimeError):
    """Raised when the Unusual Whales API returns an error or is unreachable."""


class UWClient:
    def __init__(self, token: str | None = None, timeout: int = 30, max_retries: int = 3):
        self.token = token or os.environ.get("UW_TOKEN") or os.environ.get("UNUSUAL_WHALES_TOKEN")
        if not self.token:
            raise UWError(
                "No API token found. Set UW_TOKEN in your environment or a .env file "
                "(see .env.example)."
            )
        self.timeout = timeout
        self.max_retries = max_retries

    # -- low level ---------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = BASE_URL + path
        if params:
            # drop None values, keep the rest
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "urb-rotation-scanner/1.0",
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:300]
                # 4xx (except 429) are not worth retrying
                if e.code != 429 and 400 <= e.code < 500:
                    raise UWError(f"{e.code} on {path}: {detail}") from e
                last_err = UWError(f"{e.code} on {path}: {detail}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = UWError(f"request to {path} failed: {e}")

            # exponential backoff before the next attempt
            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)

        raise last_err or UWError(f"request to {path} failed")

    # -- endpoints ---------------------------------------------------------
    def daily_ohlc(self, ticker: str, timeframe: str = "1Y") -> list[dict]:
        """Daily OHLC candles for a ticker (oldest -> newest).

        GET /api/stock/{ticker}/ohlc/1d
        """
        data = self._get(f"/api/stock/{ticker}/ohlc/1d", {"timeframe": timeframe})
        candles = _unwrap(data)
        # UW returns newest-first in some cases; normalise to oldest-first by date/time
        candles = [c for c in candles if c]
        candles.sort(key=lambda c: str(c.get("start_time") or c.get("date") or ""))
        return candles

    def sector_tide(self, sector: str, date: str | None = None) -> list[dict]:
        """Net options premium tide for a GICS-style sector name.

        GET /api/market/{sector}/sector-tide
        """
        data = self._get(f"/api/market/{urllib.parse.quote(sector)}/sector-tide", {"date": date})
        return _unwrap(data)

    def market_tide(self, date: str | None = None, interval_5m: bool = True) -> list[dict]:
        """Market-wide net options premium tide.

        GET /api/market/market-tide
        """
        data = self._get("/api/market/market-tide", {"date": date, "interval_5m": str(interval_5m).lower()})
        return _unwrap(data)

    def option_trades(
        self,
        ticker: str,
        option_type: str | None = None,
        report_flag: str | None = None,
        min_premium: float | None = None,
        min_size: int | None = None,
        newer_than: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Tape-level option prints for a ticker (one row per execution).

        GET /api/option-trades

        `report_flag="sweep"` restricts to intermarket sweeps. Note that a single
        swept order arrives here as several child prints (one per exchange), so
        `min_premium` should stay low — the caller aggregates legs into orders.
        """
        params: dict[str, Any] = {
            "ticker_symbol": ticker,
            "type": option_type,
            "report_flag[]": report_flag,
            "min_premium": min_premium,
            "min_size": min_size,
            "newer_than": newer_than,
            "limit": limit,
        }
        return _unwrap(self._get("/api/option-trades", params))

    def sector_etfs(self) -> list[dict]:
        """Current-day snapshot for SPY and the SPDR sector ETFs.

        GET /api/market/sector-etfs
        Includes call/put premium and bullish/bearish premium per ETF.
        """
        data = self._get("/api/market/sector-etfs")
        return _unwrap(data)


def _unwrap(payload: Any) -> list[dict]:
    """UW responses are usually {"data": [...]} but occasionally a bare list."""
    if isinstance(payload, dict):
        for key in ("data", "chains", "results"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        # some endpoints return {"data": {...}} single object
        if "data" in payload and isinstance(payload["data"], dict):
            return [payload["data"]]
        return []
    if isinstance(payload, list):
        return payload
    return []
