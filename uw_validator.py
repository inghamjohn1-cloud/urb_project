"""Async Unusual Whales validator for webhook-triggered trade setups.

Takes a ticker off the TradingView webhook (see ``server.py``) and asks two
questions before we price a trade:

1. Is there institutional conviction in the options tape in our direction?
   (:meth:`UnusualWhalesValidator.check_options_flow`)
2. Where are the heaviest off-exchange price levels, so we can place short
   credit-spread strikes behind them?
   (:meth:`UnusualWhalesValidator.get_dark_pool_support_resistance`)

This module is deliberately separate from ``uw_client.py``: that one is the
synchronous, standard-library-only client the sector-rotation scanner depends
on, and importing httpx at its top level would add a hard dependency to
``scan.py`` / ``backtest.py`` / ``dashboard.py``.

Auth reads ``UW_API_KEY``, falling back to the ``UW_TOKEN`` name the rest of
this repo already uses.

Endpoints used (https://api.unusualwhales.com/docs):
    GET /api/option-trades/flow-alerts
    GET /api/darkpool/{ticker}/price-levels

Usage:
    async with UnusualWhalesValidator() as uw:
        if await uw.check_options_flow("TSLA", "bullish"):
            levels = await uw.get_dark_pool_support_resistance("TSLA")
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.unusualwhales.com"

Direction = Literal["bullish", "bearish"]

#: Spec defaults for what counts as institutional options flow.
MIN_PREMIUM = 250_000
MIN_DTE = 21
#: Share of directional ask-side premium needed to call the flow confirmed.
MIN_ASK_SIDE_RATIO = 0.60
#: Ignore a verdict built on less qualifying premium than this — a single
#: $250k print is not "institutional conviction".
MIN_QUALIFYING_PREMIUM = 500_000


class UWValidatorError(RuntimeError):
    """Base class for validator failures."""


class UWAuthError(UWValidatorError):
    """The API key is missing, invalid, or lacks entitlement for an endpoint."""


class UWAPIError(UWValidatorError):
    """The API returned an error, or was unreachable after retries."""


@dataclass(frozen=True)
class FlowAnalysis:
    """Breakdown behind a :meth:`check_options_flow` verdict."""

    ticker: str
    direction: Direction
    confirmed: bool
    ratio: float
    """Directional share of ask-side premium, 0..1 (0.5 = evenly split)."""
    call_ask_premium: float
    put_ask_premium: float
    qualifying_premium: float
    """Total premium across every alert that passed the sweep/block filters."""
    alert_count: int
    reason: str

    def __bool__(self) -> bool:
        return self.confirmed


@dataclass(frozen=True)
class DarkPoolLevel:
    """One off-exchange volume shelf."""

    price: float
    dark_pool_volume: float
    regular_volume: float
    share_of_dark_pool: float
    """This level's share of all off-exchange volume in the window, 0..1."""
    sessions: int
    """How many of the sampled sessions contributed volume at this price."""


@dataclass
class _Retry:
    attempts: int = 3
    backoff: float = 1.0


class UnusualWhalesValidator:
    """Async Unusual Whales client for validating a webhook-triggered setup.

    Methods raise :class:`UWValidatorError` rather than returning a falsey
    result when the API is unreachable — a network failure must not be
    mistaken for "the institutions disagree with this trade".
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("UW_API_KEY")
            or os.environ.get("UW_TOKEN")
            or os.environ.get("UNUSUAL_WHALES_TOKEN")
        )
        if not self.api_key:
            raise UWAuthError(
                "No Unusual Whales API key found. Set UW_API_KEY (or UW_TOKEN) "
                "in your environment or .env file."
            )
        self._retry = _Retry(attempts=max_retries)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "urb-trading-scanner/1.0",
            },
        )

    async def __aenter__(self) -> "UnusualWhalesValidator":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client, unless one was injected."""
        if self._owns_client:
            await self._client.aclose()

    # -- transport ---------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with retries on 429/5xx and transport errors."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Exception | None = None

        for attempt in range(1, self._retry.attempts + 1):
            try:
                response = await self._client.get(path, params=clean)
            except httpx.TransportError as exc:
                last_error = UWAPIError(f"GET {path} failed: {exc!r}")
                logger.warning(
                    "UW request error on %s (attempt %d/%d): %r",
                    path, attempt, self._retry.attempts, exc,
                )
            else:
                if response.status_code in (401, 403):
                    raise UWAuthError(
                        f"{response.status_code} on {path}: check UW_API_KEY and "
                        f"that your plan covers this endpoint. "
                        f"{_snippet(response)}"
                    )
                if response.status_code == 404:
                    raise UWAPIError(f"404 on {path}: {_snippet(response)}")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = UWAPIError(
                        f"{response.status_code} on {path}: {_snippet(response)}"
                    )
                    logger.warning(
                        "UW %s on %s (attempt %d/%d)",
                        response.status_code, path, attempt, self._retry.attempts,
                    )
                    if attempt < self._retry.attempts:
                        await self._sleep_before_retry(attempt, response)
                    continue
                if response.status_code >= 400:
                    # Other 4xx are our fault; retrying will not help.
                    raise UWAPIError(
                        f"{response.status_code} on {path}: {_snippet(response)}"
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise UWAPIError(f"non-JSON response from {path}: {exc}") from exc

            if attempt < self._retry.attempts:
                await asyncio.sleep(self._retry.backoff * 2 ** (attempt - 1))

        raise last_error or UWAPIError(f"GET {path} failed")

    async def _sleep_before_retry(self, attempt: int, response: httpx.Response) -> None:
        """Back off, honouring Retry-After when the API sends one."""
        delay = self._retry.backoff * 2 ** (attempt - 1)
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        await asyncio.sleep(delay)

    # -- 1. options flow ---------------------------------------------------
    async def check_options_flow(
        self,
        ticker: str,
        direction: Direction,
        *,
        min_premium: int = MIN_PREMIUM,
        min_dte: int = MIN_DTE,
        min_ratio: float = MIN_ASK_SIDE_RATIO,
        lookback_days: int = 5,
        limit: int = 200,
    ) -> bool:
        """True when institutional options flow agrees with ``direction``.

        Pulls flow alerts for ``ticker``, keeps only sweeps and floor (block)
        trades of at least ``min_premium`` with at least ``min_dte`` days to
        expiry, then measures how much of the ask-side premium is calls versus
        puts. Bullish needs calls lifting the ask; bearish needs puts.

        Use :meth:`analyze_options_flow` if you want the numbers behind the
        verdict rather than just the boolean.
        """
        analysis = await self.analyze_options_flow(
            ticker,
            direction,
            min_premium=min_premium,
            min_dte=min_dte,
            min_ratio=min_ratio,
            lookback_days=lookback_days,
            limit=limit,
        )
        return analysis.confirmed

    async def analyze_options_flow(
        self,
        ticker: str,
        direction: Direction,
        *,
        min_premium: int = MIN_PREMIUM,
        min_dte: int = MIN_DTE,
        min_ratio: float = MIN_ASK_SIDE_RATIO,
        min_qualifying_premium: float = MIN_QUALIFYING_PREMIUM,
        lookback_days: int = 5,
        limit: int = 200,
    ) -> FlowAnalysis:
        """The full :class:`FlowAnalysis` behind :meth:`check_options_flow`."""
        ticker = ticker.strip().upper()
        if direction not in ("bullish", "bearish"):
            raise ValueError(f"direction must be 'bullish' or 'bearish', got {direction!r}")

        params: dict[str, Any] = {
            "ticker_symbol": ticker,
            "min_premium": min_premium,
            "min_dte": min_dte,
            "limit": min(limit, 200),
        }
        if lookback_days:
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            params["newer_than"] = since.date().isoformat()

        payload = await self._get("/api/option-trades/flow-alerts", params)
        alerts = _rows(payload)
        logger.debug("%s: %d flow alerts before local filters", ticker, len(alerts))

        qualifying = [a for a in alerts if _is_institutional(a, min_premium, min_dte)]
        if not qualifying:
            return FlowAnalysis(
                ticker=ticker, direction=direction, confirmed=False, ratio=0.0,
                call_ask_premium=0.0, put_ask_premium=0.0, qualifying_premium=0.0,
                alert_count=0,
                reason=(
                    f"no sweep/block alerts >= ${min_premium:,.0f} with "
                    f">= {min_dte} DTE in the last {lookback_days} days"
                ),
            )

        call_ask = sum(
            _num(a.get("total_ask_side_prem")) for a in qualifying
            if str(a.get("type", "")).lower() == "call"
        )
        put_ask = sum(
            _num(a.get("total_ask_side_prem")) for a in qualifying
            if str(a.get("type", "")).lower() == "put"
        )
        qualifying_premium = sum(_num(a.get("total_premium")) for a in qualifying)

        directional = call_ask + put_ask
        if directional <= 0:
            ratio = 0.0
        elif direction == "bullish":
            ratio = call_ask / directional
        else:
            ratio = put_ask / directional

        confirmed = ratio >= min_ratio and qualifying_premium >= min_qualifying_premium
        if ratio < min_ratio:
            reason = (
                f"{direction} ask-side share {ratio:.0%} below the {min_ratio:.0%} "
                f"threshold ({len(qualifying)} alerts)"
            )
        elif qualifying_premium < min_qualifying_premium:
            reason = (
                f"{direction} ask-side share {ratio:.0%} but only "
                f"${qualifying_premium:,.0f} qualifying premium "
                f"(need ${min_qualifying_premium:,.0f})"
            )
        else:
            reason = (
                f"{direction} ask-side share {ratio:.0%} across {len(qualifying)} "
                f"sweep/block alerts, ${qualifying_premium:,.0f} premium"
            )

        logger.info("%s flow check (%s): %s", ticker, "PASS" if confirmed else "FAIL", reason)
        return FlowAnalysis(
            ticker=ticker,
            direction=direction,
            confirmed=confirmed,
            ratio=ratio,
            call_ask_premium=call_ask,
            put_ask_premium=put_ask,
            qualifying_premium=qualifying_premium,
            alert_count=len(qualifying),
            reason=reason,
        )

    # -- 2. dark pool levels -----------------------------------------------
    async def get_dark_pool_support_resistance(
        self,
        ticker: str,
        *,
        sessions: int = 5,
        top_n: int = 3,
        max_lookback_days: int = 12,
    ) -> list[DarkPoolLevel]:
        """Top ``top_n`` off-exchange volume shelves over the last ``sessions`` days.

        ``/api/darkpool/{ticker}/price-levels`` reports one session per call, so
        this walks back over trading days, merges the per-session buckets by
        price, and returns the heaviest levels — the prices institutions
        actually transacted size at, which is where short credit-spread strikes
        want to sit behind.

        Days that return nothing (holidays, or a ticker with no off-exchange
        prints) are skipped, up to ``max_lookback_days`` calendar days back.
        """
        ticker = ticker.strip().upper()
        volume: dict[float, float] = {}
        regular: dict[float, float] = {}
        seen: dict[float, int] = {}
        collected = 0

        for day in _candidate_sessions(max_lookback_days):
            if collected >= sessions:
                break
            try:
                payload = await self._get(
                    f"/api/darkpool/{ticker}/price-levels", {"date": day.isoformat()}
                )
            except UWAPIError as exc:
                # One bad session should not sink the whole window.
                logger.warning("%s: dark pool levels for %s failed: %s", ticker, day, exc)
                continue

            rows = _price_level_rows(payload)
            if not rows:
                logger.debug("%s: no dark pool levels for %s", ticker, day)
                continue

            collected += 1
            for row in rows:
                price = _num(row.get("price"))
                dp_vol = _num(row.get("dark_pool_volume"))
                if price <= 0 or dp_vol <= 0:
                    continue
                volume[price] = volume.get(price, 0.0) + dp_vol
                regular[price] = regular.get(price, 0.0) + _num(row.get("regular_volume"))
                seen[price] = seen.get(price, 0) + 1

        if not volume:
            logger.warning(
                "%s: no dark pool volume in the last %d sessions", ticker, sessions
            )
            return []

        total = sum(volume.values())
        levels = [
            DarkPoolLevel(
                price=price,
                dark_pool_volume=vol,
                regular_volume=regular.get(price, 0.0),
                share_of_dark_pool=vol / total if total else 0.0,
                sessions=seen.get(price, 0),
            )
            for price, vol in volume.items()
        ]
        levels.sort(key=lambda lvl: lvl.dark_pool_volume, reverse=True)
        top = levels[:top_n]
        logger.info(
            "%s dark pool levels over %d session(s): %s",
            ticker, collected,
            ", ".join(f"{lvl.price:g} ({lvl.dark_pool_volume:,.0f})" for lvl in top),
        )
        return top


# -- helpers ---------------------------------------------------------------
def _snippet(response: httpx.Response, limit: int = 200) -> str:
    """A short, safe excerpt of an error body for log and exception messages."""
    try:
        text = response.text
    except Exception:  # pragma: no cover - body already consumed/undecodable
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _is_institutional(alert: dict, min_premium: float, min_dte: int) -> bool:
    """Sweep or floor (block) print, big enough, far enough out.

    The API exposes execution style as the booleans ``has_sweep`` / ``has_floor``
    rather than a ``trade_type`` string, and gives ``expiry`` rather than a
    days-to-expiration number, so both are derived here. The server-side
    min_premium/min_dte filters are re-checked locally so a change to the
    request cannot silently widen the gate.
    """
    if not (alert.get("has_sweep") or alert.get("has_floor")):
        return False
    if _num(alert.get("total_premium")) < min_premium:
        return False
    dte = _days_to_expiration(alert.get("expiry"))
    return dte is not None and dte >= min_dte


def _days_to_expiration(expiry: Any, today: date | None = None) -> int | None:
    if not expiry:
        return None
    try:
        expiry_date = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        logger.debug("unparseable expiry %r", expiry)
        return None
    return (expiry_date - (today or datetime.now(timezone.utc).date())).days


def _candidate_sessions(max_lookback_days: int) -> Iterable[date]:
    """Weekdays walking back from today (holidays get filtered by empty results)."""
    today = datetime.now(timezone.utc).date()
    for offset in range(max_lookback_days + 1):
        day = today - timedelta(days=offset)
        if day.weekday() < 5:
            yield day


def _num(value: Any) -> float:
    """UW returns numerics as strings on many fields."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rows(payload: Any) -> list[dict]:
    """Unwrap the {"data": [...]} envelope (some endpoints use "result")."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _price_level_rows(payload: Any) -> list[dict]:
    """Pull ``stock_price_vol`` out of a price-levels response.

    The body is ``{"data": {"stock_price_vol": [...], "opt_price_vol": [...]}}``;
    only the stock buckets carry off-exchange share volume.
    """
    body = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(body, list):
        body = body[0] if body and isinstance(body[0], dict) else {}
    if not isinstance(body, dict):
        return []
    rows = body.get("stock_price_vol")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]
