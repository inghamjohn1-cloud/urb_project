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
            clean = {k: v for k, v in params.items() if v is not None}
            qs = _build_qs(clean)
            if qs:
                url += "?" + qs

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

    def sector_etfs(self) -> list[dict]:
        """Current-day snapshot for SPY and the SPDR sector ETFs.

        GET /api/market/sector-etfs
        Includes call/put premium and bullish/bearish premium per ETF.
        """
        data = self._get("/api/market/sector-etfs")
        return _unwrap(data)

    def earnings_screener(
        self,
        min_report_date: str,
        max_report_date: str,
        max_surprise_pct: float | None = None,
        min_marketcap: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Earnings results with EPS vs estimate data ordered by worst miss first.

        GET /api/earnings/screener
        """
        data = self._get("/api/earnings/screener", {
            "min_report_date": min_report_date,
            "max_report_date": max_report_date,
            "max_surprise_percentage": max_surprise_pct,
            "min_marketcap": min_marketcap,
            "order_by": "surprise_percentage",
            "order_direction": "asc",
            "limit": limit,
        })
        return _unwrap(data)

    def stock_screener(
        self,
        tickers: list[str] | None = None,
        max_change: float | None = None,
        max_net_premium: int | None = None,
        min_marketcap: int | None = None,
        is_sp500: bool | None = None,
        order: str = "net_premium",
        order_direction: str = "asc",
        limit: int = 100,
    ) -> list[dict]:
        """Stock screener ranked by options-flow metrics.

        GET /api/stock-screener
        """
        params: dict[str, Any] = {
            "order": order,
            "order_direction": order_direction,
            "limit": limit,
            "hide_index_etf": "true",
            "issue_types[]": ["Common Stock"],
        }
        if tickers:
            params["ticker"] = ",".join(tickers)
        if max_change is not None:
            params["max_change"] = max_change
        if max_net_premium is not None:
            params["max_net_premium"] = max_net_premium
        if min_marketcap is not None:
            params["min_marketcap"] = min_marketcap
        if is_sp500 is not None:
            params["is_s_p_500"] = str(is_sp500).lower()
        data = self._get("/api/stock-screener", params)
        return _unwrap(data)

    def flow_alerts(
        self,
        tickers: list[str] | None = None,
        rule_names: list[str] | None = None,
        is_call: bool | None = None,
        min_premium: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Option flow alert hits for large block and unusual activity rules.

        GET /api/option-flow-alerts
        """
        params: dict[str, Any] = {"limit": limit}
        if tickers:
            params["ticker_symbol"] = ",".join(tickers)
        if rule_names:
            params["rule_name"] = rule_names  # list → bracket params via _build_qs
        if is_call is not None:
            params["is_call"] = str(is_call).lower()
        if min_premium is not None:
            params["min_premium"] = min_premium
        data = self._get("/api/option-flow-alerts", params)
        return _unwrap(data)


def _build_qs(params: dict) -> str:
    """URL-encode params; list values become repeated bracket params (key[]=v)."""
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, list):
            for item in v:
                parts.append((f"{k}[]", str(item)))
        else:
            parts.append((k, str(v)))
    return urllib.parse.urlencode(parts)


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
