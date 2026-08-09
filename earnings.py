"""Earnings date cache for post-earnings capitulation context.

Stores recent earnings events (report date, pre-earnings close, EPS
data) in logs/earnings_cache.csv so that pec_scan.py and whale_log.py
can compute days_since_earnings and earnings_selloff_pct without hitting
the API on every log run.

pec_scan.py populates the cache as a side effect of the earnings screen;
whale_log.py reads it to enrich journal rows.
"""

from __future__ import annotations

import csv
import os
from datetime import date

CACHE_PATH = "logs/earnings_cache.csv"
CACHE_COLS = [
    "ticker", "report_date", "pre_close", "post_close",
    "reported_eps", "estimated_eps", "surprise_pct",
]


def load(path: str = CACHE_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return list(csv.DictReader(fh))


def save(rows: list[dict], path: str = CACHE_PATH) -> None:
    """Write rows to cache, deduplicating on (ticker, report_date)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    seen: set[tuple] = set()
    deduped = []
    for r in rows:
        key = (r.get("ticker", ""), r.get("report_date", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    deduped.sort(key=lambda r: (r.get("ticker", ""), r.get("report_date", "")))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CACHE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)


def upsert(new_rows: list[dict], path: str = CACHE_PATH) -> None:
    """Merge new_rows into the existing cache, newer report_date wins."""
    existing = load(path)
    by_key: dict[tuple, dict] = {}
    for r in existing:
        by_key[(r.get("ticker", ""), r.get("report_date", ""))] = r
    for r in new_rows:
        by_key[(r.get("ticker", ""), r.get("report_date", ""))] = r
    save(list(by_key.values()), path)


def days_since(ticker: str, as_of: str, cache: list[dict]) -> int | None:
    """Calendar days from the most recent earnings report to as_of."""
    rows = [r for r in cache
            if r.get("ticker", "").upper() == ticker.upper()
            and r.get("report_date", "") <= as_of]
    if not rows:
        return None
    latest = max(rows, key=lambda r: r.get("report_date", ""))
    try:
        return (date.fromisoformat(as_of[:10])
                - date.fromisoformat(latest["report_date"][:10])).days
    except ValueError:
        return None


def selloff_pct(ticker: str, current_close: float,
                as_of: str, cache: list[dict]) -> float | None:
    """(current_close / pre_earnings_close - 1); negative means decline."""
    rows = [r for r in cache
            if r.get("ticker", "").upper() == ticker.upper()
            and r.get("report_date", "") <= as_of]
    if not rows:
        return None
    latest = max(rows, key=lambda r: r.get("report_date", ""))
    try:
        pre = float(latest["pre_close"])
        return round(current_close / pre - 1, 4) if pre > 0 else None
    except (TypeError, ValueError, KeyError):
        return None
