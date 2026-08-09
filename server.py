#!/usr/bin/env python3
"""FastAPI backend for the local trading scanner.

Exposes a webhook endpoint that TradingView alerts POST into. Incoming
payloads are validated against a Pydantic model and printed to the terminal,
so you can watch alerts land while you wire up the rest of the pipeline.

Expected alert body:
    {"ticker": "TSLA", "condition": "RSI_Oversold", "price": 225.50}

Usage:
    uvicorn server:app --host 127.0.0.1 --port 8000 --reload

Requires fastapi and uvicorn (see requirements.txt).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

app = FastAPI(
    title="Trading Scanner Backend",
    description="Receives TradingView alert webhooks for the local scanner.",
    version="0.1.0",
)


class TradingViewAlert(BaseModel):
    """A single alert fired by a TradingView alert's webhook.

    Extra keys are kept rather than rejected — TradingView alert messages
    commonly carry additional placeholders (``{{interval}}``, ``{{exchange}}``,
    a shared secret, ...) and those are worth seeing in the log.
    """

    model_config = ConfigDict(extra="allow")

    ticker: str
    condition: str
    price: float

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        return ticker

    @field_validator("condition")
    @classmethod
    def _normalize_condition(cls, value: str) -> str:
        condition = value.strip()
        if not condition:
            raise ValueError("condition must not be empty")
        return condition

    @field_validator("price")
    @classmethod
    def _check_price(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("price must be greater than 0")
        return value

    @property
    def extras(self) -> dict[str, Any]:
        """Any fields sent beyond ticker/condition/price."""
        return self.__pydantic_extra__ or {}


def log_alert(alert: TradingViewAlert, received_at: datetime) -> None:
    """Print a parsed alert to the terminal."""
    stamp = received_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print("=" * 60)
    print(f"  TRADINGVIEW ALERT                        {stamp}")
    print("=" * 60)
    print(f"  Ticker    : {alert.ticker}")
    print(f"  Condition : {alert.condition}")
    print(f"  Price     : {alert.price:,.2f}")
    for key, value in alert.extras.items():
        print(f"  {key:<10}: {value}")
    print("=" * 60, flush=True)


async def parse_alert_body(request: Request) -> TradingViewAlert:
    """Read and validate the request body as a TradingView alert.

    The body is decoded by hand instead of via a typed body parameter because
    TradingView sends its webhook with ``Content-Type: text/plain``, which
    FastAPI would not hand to the JSON parser.
    """
    raw = await request.body()
    if not raw.strip():
        raise HTTPException(status_code=400, detail="Request body is empty.")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Body is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Body must be a JSON object, e.g. "
            '{"ticker": "TSLA", "condition": "RSI_Oversold", "price": 225.50}',
        )

    try:
        return TradingViewAlert.model_validate(payload)
    except ValidationError as exc:
        # include_context=False drops the raw ValueError that custom validators
        # put in ``ctx``, which would not survive JSON encoding.
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc


@app.post("/api/tv-webhook")
async def tv_webhook(request: Request) -> dict[str, Any]:
    """Receive a TradingView alert, validate it, and log it."""
    received_at = datetime.now(timezone.utc)
    alert = await parse_alert_body(request)
    log_alert(alert, received_at)
    return {
        "status": "ok",
        "received_at": received_at.isoformat(),
        "alert": alert.model_dump(),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — handy for checking the tunnel from TradingView."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
