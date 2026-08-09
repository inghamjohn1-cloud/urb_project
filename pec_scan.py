#!/usr/bin/env python3
"""Post-Earnings Capitulation Scanner.

Three-stage pipeline run once per trading day (after close) to find
beaten-down stocks where post-earnings selling may be exhausted and
institutional accumulation is beginning.

Candidates are classified by why the stock is down, since the two cases are
different trades and are tracked separately in the forward-test journal:

  capitulation — the print was genuinely broken (severe EPS miss) and holders
                 are liquidating a real deterioration.
  overreaction — earnings were a small miss or an outright beat, yet the stock
                 fell hard anyway; the price move outruns the result.

  Stage 1 — Earnings screen  : stocks that reported in the last N days,
                                including modest beats (--max-surprise) so
                                overreactions are not screened out.
  Stage 2 — Flow screen      : confirm capitulation options flow and
                                price damage vs the pre-earnings close.
  Stage 3 — Accumulation     : detect large floor/block call buying via
                                flow alert rules.

Outputs
-------
  logs/pec_candidates.csv     — all scored candidates, newest scan on top.
  logs/whale_journal.csv      — stub rows appended for signal tickers
                                (flow_state=capitulation, close populated)
                                so the verdict machine can track forward returns.
  logs/earnings_cache.csv     — updated with today's earnings screen results.

Usage
-----
    python pec_scan.py
    python pec_scan.py --days-back 3 --min-selloff 0.05
    python pec_scan.py --dry-run          # print only, write nothing
    python pec_scan.py --no-journal-stub  # skip whale_journal.csv writes
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, timedelta
from typing import Any

import earnings as earnings_mod
from uw_client import UWClient, UWError
from whale_log import COLUMNS as JOURNAL_COLS

# ── output schema ─────────────────────────────────────────────────────────────

CANDIDATE_COLS = [
    "scan_date", "ticker",
    "report_date", "days_since_earnings",
    "earnings_selloff_pct", "surprise_pct", "reported_eps", "estimated_eps",
    "close", "today_change_pct",
    "net_premium", "put_call_ratio", "iv_rank",
    "floor_alert_count", "repeat_hit_count", "sweep_floor_count",
    "is_sp500", "market_cap", "sector",
    "pec_score", "pec_signal", "pec_type",
]

# surprise_pct is EPS surprise as a percentage of the estimate, so it blows up
# for companies with near-zero estimates (a 2c miss on a 1c estimate is -200%).
# These cutoffs separate a genuinely broken print from a rounding-error miss.
SEVERE_MISS_PCT = -100.0
MODERATE_MISS_PCT = -20.0

# An overreaction is a large price decline that the earnings result does not
# justify: the print was a small miss or an outright beat, and the stock fell
# anyway (guidance cut, multiple compression, forced selling).
OVERREACTION_SELLOFF = -0.10
MILD_OVERREACTION_SELLOFF = -0.05

# Flow alert rules that indicate institutional accumulation.
ACCUM_RULES = [
    "FloorTradeLargeCap",
    "RepeatedHits",
    "RepeatedHitsAscendingFill",
    "SweepsFollowedByFloor",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _f(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _net_premium(fr: dict) -> float | None:
    """Net options premium, directional (positive = call-side demand).

    The stock screener returns net_premium as null and only supplies the two
    legs, so reconstruct it as net_call_premium - net_put_premium. Falling back
    to net_call_premium alone understates the bearish case: on 2026-08-07 AMRZ
    showed -82,635 on the call leg but -179,406 net once puts were counted.
    """
    direct = _f(fr.get("net_premium"))
    if direct is not None:
        return direct
    call_leg = _f(fr.get("net_call_premium"))
    put_leg = _f(fr.get("net_put_premium"))
    if call_leg is None:
        return None
    return call_leg - (put_leg or 0.0)


def _candle_date(c: dict) -> str:
    """Return YYYY-MM-DD from a candle dict (handles ISO strings and unix-ms timestamps)."""
    v = str(c.get("date") or c.get("start_time") or "")
    if len(v) == 13 and v.isdigit():
        from datetime import datetime
        return datetime.utcfromtimestamp(int(v) / 1000).strftime("%Y-%m-%d")
    return v[:10]


# ── PEC classification ────────────────────────────────────────────────────────

def pec_type(c: dict) -> str:
    """Classify why the stock is down: capitulation, overreaction, or mixed.

    capitulation — the print itself was broken (severe EPS miss) and the stock
                   sold off; sellers are liquidating a real deterioration.
    overreaction — earnings were a small miss or a beat, yet the stock still
                   fell hard; the price move is disproportionate to the result.
    mixed        — a moderate miss with a moderate decline, or too little
                   dislocation either way to call it.
    """
    selloff = _f(c.get("earnings_selloff_pct"), 0.0) or 0.0
    surprise = _f(c.get("surprise_pct"))

    if surprise is not None and surprise <= SEVERE_MISS_PCT:
        return "capitulation"
    if (surprise is None or surprise > MODERATE_MISS_PCT) and selloff <= MILD_OVERREACTION_SELLOFF:
        return "overreaction"
    return "mixed"


# ── PEC score (0–10) ──────────────────────────────────────────────────────────

def pec_score(c: dict) -> int:
    """Score one candidate 0–10 across five dimensions (2 pts each)."""
    score = 0

    # 1. Selloff magnitude from pre-earnings close
    selloff = _f(c.get("earnings_selloff_pct"), 0.0)
    if selloff <= -0.10:
        score += 2
    elif selloff <= -0.05:
        score += 1

    # 2. Earnings/price dislocation — either the print was badly broken, or it
    #    was fine and the stock fell anyway. Both are setups; a moderate miss
    #    matched by a moderate decline is the uninteresting middle and scores low.
    surprise = _f(c.get("surprise_pct"))
    if surprise is not None and surprise <= SEVERE_MISS_PCT:
        score += 2
    elif surprise is None or surprise > MODERATE_MISS_PCT:
        if selloff <= OVERREACTION_SELLOFF:
            score += 2
        elif selloff <= MILD_OVERREACTION_SELLOFF:
            score += 1
    else:
        score += 1

    # 3. Capitulation flow (net options premium negative + put/call elevated)
    net_prem = _f(c.get("net_premium"), 0.0)
    pcr = _f(c.get("put_call_ratio"), 0.0)
    if net_prem is not None and net_prem < 0 and pcr is not None and pcr > 1.2:
        score += 2
    elif net_prem is not None and net_prem < 0:
        score += 1

    # 4. Accumulation signal (floor/block/sweep call prints)
    alerts = (int(c.get("floor_alert_count") or 0)
              + int(c.get("repeat_hit_count") or 0)
              + int(c.get("sweep_floor_count") or 0))
    if alerts >= 2:
        score += 2
    elif alerts >= 1:
        score += 1

    # 5. Quality + recency bonus
    if c.get("is_sp500") in (True, "True", "true", "1", 1):
        score += 1
    days = _f(c.get("days_since_earnings"))
    if days is not None and 1 <= days <= 3:
        score += 1

    return min(score, 10)


# ── pipeline stages ───────────────────────────────────────────────────────────

def stage1_earnings(client: UWClient, min_date: str, max_date: str,
                    min_marketcap: int, max_surprise: float) -> list[dict]:
    print(f"  Stage 1  earnings {min_date}→{max_date}, "
          f"mktcap≥{min_marketcap/1e9:.0f}B, surprise≤{max_surprise:.0f}% ... ",
          end="", flush=True)
    try:
        rows = client.earnings_screener(
            min_report_date=min_date,
            max_report_date=max_date,
            max_surprise_pct=max_surprise,
            min_marketcap=min_marketcap,
            limit=100,
        )
        print(f"{len(rows)} result{'s' if len(rows) != 1 else ''}")
        return rows
    except UWError as e:
        print(f"FAILED — {e}")
        return []


def stage1b_pre_closes(
    client: UWClient,
    earnings_rows: list[dict],
    e_cache: list[dict],
    persist: bool = True,
) -> None:
    """Fetch the pre-earnings close for any ticker that lacks it in the cache.

    Calls daily_ohlc() for each ticker missing a pre_close, takes the closing
    price of the last candle before report_date, upserts into earnings_cache.csv,
    and mutates e_cache in place so build_candidates() sees the correct values.
    """
    need = [
        (_ticker(er), str(er.get("report_date") or "")[:10])
        for er in earnings_rows
        if _ticker(er)
    ]
    need = [
        (t, rd) for t, rd in need
        if rd and not any(
            r.get("ticker", "").upper() == t
            and r.get("report_date", "")[:10] == rd
            and _f(r.get("pre_close"))
            for r in e_cache
        )
    ]
    if not need:
        return

    print(f"  Stage 1b pre-close OHLC for {len(need)} ticker"
          f"{'s' if len(need) != 1 else ''} ... ", end="", flush=True)
    updates: list[dict] = []
    fetched = 0
    for t, report_date in need:
        try:
            candles = client.daily_ohlc(t, timeframe="3M")
            before = [c for c in candles if _candle_date(c) < report_date]
            if not before:
                continue
            pre_close = _f(before[-1].get("close") or before[-1].get("c"))
            if pre_close is None:
                continue
            merged = False
            for r in e_cache:
                if (r.get("ticker", "").upper() == t
                        and r.get("report_date", "")[:10] == report_date):
                    r["pre_close"] = str(pre_close)
                    updates.append(dict(r))
                    merged = True
                    break
            if not merged:
                entry = {k: "" for k in earnings_mod.CACHE_COLS}
                entry.update({"ticker": t, "report_date": report_date,
                              "pre_close": str(pre_close)})
                e_cache.append(entry)
                updates.append(entry)
            fetched += 1
        except UWError:
            pass
    print(f"{fetched}/{len(need)} fetched")
    if updates and persist:
        earnings_mod.upsert(updates)


def stage2_flow(client: UWClient, tickers: list[str]) -> dict[str, dict]:
    if not tickers:
        return {}
    print(f"  Stage 2  stock screener for {len(tickers)} tickers ... ",
          end="", flush=True)
    try:
        rows = client.stock_screener(
            tickers=tickers,
            order="net_premium",
            order_direction="asc",
            limit=len(tickers) + 20,
        )
        out = {_ticker(r): r for r in rows if _ticker(r)}
        print(f"{len(out)} matched")
        return out
    except UWError as e:
        print(f"FAILED — {e}")
        return {}


def stage3_accum(client: UWClient, tickers: list[str]) -> dict[str, dict]:
    if not tickers:
        return {}
    print(f"  Stage 3  flow alerts for {len(tickers)} tickers ... ",
          end="", flush=True)
    try:
        alerts = client.flow_alerts(
            tickers=tickers,
            rule_names=ACCUM_RULES,
            is_call=True,
            min_premium=50_000,
            limit=500,
        )
        counts: dict[str, dict] = {}
        for a in alerts:
            t = _ticker(a)
            if not t:
                continue
            # is_call only says the contract was a call, not that anyone bought
            # it. On 2026-08-07 AMLX's sole post-earnings call alert was 100%
            # bid-side — calls being SOLD into the decline, the opposite of
            # accumulation. Require the ask side to dominate before counting it.
            ask = _f(a.get("total_ask_side_prem"), 0.0) or 0.0
            bid = _f(a.get("total_bid_side_prem"), 0.0) or 0.0
            if ask <= bid:
                continue
            rule = str(a.get("rule_name") or a.get("alert_rule") or "")
            rec = counts.setdefault(t, {
                "floor_alert_count": 0,
                "repeat_hit_count": 0,
                "sweep_floor_count": 0,
            })
            if "Floor" in rule:
                rec["floor_alert_count"] += 1
            elif "Repeat" in rule:
                rec["repeat_hit_count"] += 1
            elif "Sweep" in rule:
                rec["sweep_floor_count"] += 1
        print(f"accumulation signals on {len(counts)} ticker"
              f"{'s' if len(counts) != 1 else ''}")
        return counts
    except UWError as e:
        print(f"FAILED — {e}")
        return {}


# ── candidate assembly ────────────────────────────────────────────────────────

def build_candidates(
    earnings_rows: list[dict],
    flow_map: dict[str, dict],
    accum_map: dict[str, dict],
    e_cache: list[dict],
    today: str,
    min_selloff: float,
) -> list[dict]:
    candidates = []
    for er in earnings_rows:
        t = _ticker(er)
        if not t:
            continue
        fr = flow_map.get(t)
        if fr is None:
            continue

        acc = accum_map.get(t, {})
        report_date = str(er.get("report_date") or er.get("date") or "")[:10]
        days_back = earnings_mod.days_since(t, today, e_cache)

        close_raw = (_f(fr.get("close"))
                     or _f(fr.get("underlying_price"))
                     or _f(fr.get("stock_price")))
        today_chg = (_f(fr.get("perc_change"))
                     or _f(fr.get("change"))
                     or _f(fr.get("percent_change"))
                     or 0.0)

        # Cumulative selloff from pre-earnings close (populated by stage1b).
        # Falls back to today's daily change only when OHLC fetch failed —
        # that proxy is wrong for multi-day selloffs; treat such rows with caution.
        sp = earnings_mod.selloff_pct(t, close_raw, today, e_cache) if close_raw else None
        selloff = sp if sp is not None else today_chg

        surprise = (_f(er.get("surprise_percentage"))
                    or _f(er.get("surprise_pct")))

        # Skip if selloff is less than the minimum threshold.
        if selloff > -min_selloff:
            continue

        c: dict[str, Any] = {
            "scan_date": today,
            "ticker": t,
            "report_date": report_date,
            "days_since_earnings": days_back,
            "earnings_selloff_pct": round(selloff, 4),
            "surprise_pct": round(surprise, 2) if surprise is not None else "",
            "reported_eps": _f(er.get("reported_eps")),
            "estimated_eps": _f(er.get("estimated_eps")),
            "close": close_raw,
            "today_change_pct": round(today_chg, 4),
            "net_premium": _net_premium(fr),
            "put_call_ratio": _f(fr.get("put_call_ratio")),
            "iv_rank": _f(fr.get("iv_rank")),
            "floor_alert_count": acc.get("floor_alert_count", 0),
            "repeat_hit_count": acc.get("repeat_hit_count", 0),
            "sweep_floor_count": acc.get("sweep_floor_count", 0),
            "is_sp500": fr.get("is_s_p_500") or fr.get("is_sp500") or False,
            "market_cap": _f(fr.get("marketcap") or fr.get("market_cap")),
            "sector": fr.get("sector") or "",
            "pec_score": 0,
            "pec_signal": False,
            "pec_type": "",
        }
        c["pec_score"] = pec_score(c)
        c["pec_signal"] = c["pec_score"] >= 6
        c["pec_type"] = pec_type(c)
        candidates.append(c)

    candidates.sort(key=lambda c: c["pec_score"], reverse=True)
    return candidates


# ── output ────────────────────────────────────────────────────────────────────

def write_candidates(candidates: list[dict], path: str, today: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prior: list[dict] = []
    if os.path.exists(path):
        with open(path) as fh:
            for row in csv.DictReader(fh):
                if row.get("scan_date") != today:
                    prior.append(row)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CANDIDATE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(candidates + prior)


def write_journal_stubs(candidates: list[dict], journal_path: str) -> int:
    """Append stub rows for signal tickers to whale_journal.csv.

    Stubs carry flow_state=capitulation and the screener close so the
    verdict machine can compute forward returns from future whale_log.py
    runs.  Deduplication on (date, ticker) prevents double-counting.
    """
    signals = [c for c in candidates if c["pec_signal"]]
    if not signals:
        return 0

    os.makedirs(os.path.dirname(journal_path) or ".", exist_ok=True)
    exists = os.path.exists(journal_path)
    seen: set[tuple] = set()
    if exists:
        with open(journal_path) as fh:
            for row in csv.DictReader(fh):
                seen.add((row.get("date", ""), row.get("ticker", "")))

    new_stubs = []
    for c in signals:
        key = (c["scan_date"], c["ticker"])
        if key in seen:
            continue
        stub = {col: "" for col in JOURNAL_COLS}
        stub["date"] = c["scan_date"]
        stub["ticker"] = c["ticker"]
        stub["close"] = c.get("close") or ""
        stub["flow_state"] = "capitulation"
        stub["pec_signal"] = "True"
        stub["pec_type"] = c.get("pec_type", "")
        # net_prem_5d: screener net_premium is a daily aggregate, not a 5-day
        # mean, but it confirms the capitulation direction for the verdict check.
        np = _f(c.get("net_premium"))
        stub["net_prem_5d"] = round(np, 0) if np is not None else ""
        new_stubs.append(stub)

    if not new_stubs:
        return 0

    with open(journal_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_COLS)
        if not exists:
            w.writeheader()
        w.writerows(new_stubs)
    return len(new_stubs)


def update_earnings_cache(earnings_rows: list[dict],
                          cache_path: str = earnings_mod.CACHE_PATH) -> None:
    """Store earnings screener results in the local cache."""
    new_entries = []
    for er in earnings_rows:
        t = _ticker(er)
        if not t:
            continue
        new_entries.append({
            "ticker": t,
            "report_date": str(er.get("report_date") or er.get("date") or "")[:10],
            "pre_close": "",   # populated later via OHLC fetch if needed
            "post_close": "",
            "reported_eps": _f(er.get("reported_eps")) or "",
            "estimated_eps": _f(er.get("estimated_eps")) or "",
            "surprise_pct": _f(er.get("surprise_percentage") or
                                er.get("surprise_pct")) or "",
        })
    if new_entries:
        earnings_mod.upsert(new_entries, cache_path)


# ── display ───────────────────────────────────────────────────────────────────

def print_table(candidates: list[dict]) -> None:
    W = 110
    print(f"\n{'─' * W}")
    print(f"{'POST-EARNINGS CAPITULATION / OVERREACTION CANDIDATES':^{W}}")
    print(f"{'─' * W}")
    hdr = (f"{'TICKER':<8} {'SCR':>4}  {'TYPE':<13}  {'DAYS':>4}  {'SELLOFF':>8}  "
           f"{'SURP%':>8}  {'NET_PREM':>10}  {'PCR':>5}  "
           f"{'ALERTS':>6}  {'SECTOR':<22}")
    print(hdr)
    print("─" * W)

    for c in candidates:
        selloff = (f"{c['earnings_selloff_pct']*100:.1f}%"
                   if c.get("earnings_selloff_pct") != "" else "n/a")
        surp = (f"{c['surprise_pct']:.1f}%"
                if c.get("surprise_pct") != "" else "n/a")
        np = _f(c.get("net_premium"))
        net_p = f"${np/1e6:.1f}M" if np is not None else "n/a"
        pcr = _f(c.get("put_call_ratio"))
        pcr_s = f"{pcr:.2f}" if pcr is not None else "n/a"
        alerts = (c.get("floor_alert_count", 0)
                  + c.get("repeat_hit_count", 0)
                  + c.get("sweep_floor_count", 0))
        sig = " ◆" if c["pec_signal"] else "  "
        days = c.get("days_since_earnings")
        days_s = str(days) if days is not None else "?"
        print(f"{c['ticker']:<8} {c['pec_score']:>3}{sig}  "
              f"{c.get('pec_type',''):<13}  {days_s:>4}  "
              f"{selloff:>8}  {surp:>8}  {net_p:>10}  {pcr_s:>5}  "
              f"{alerts:>6}  {c.get('sector',''):<22}")

    print("─" * W)
    signals = [c for c in candidates if c["pec_signal"]]
    by_type = {t: sum(1 for c in candidates if c.get("pec_type") == t)
               for t in ("capitulation", "overreaction", "mixed")}
    print(f"\n◆ = signal (score ≥ 6)   "
          f"{len(signals)} signal{'s' if len(signals) != 1 else ''} "
          f"of {len(candidates)} candidates   "
          + "  ".join(f"{t}={n}" for t, n in by_type.items() if n))

    if signals:
        print("\nHIGH-CONVICTION SETUPS:")
        for c in signals:
            days = c.get("days_since_earnings", "?")
            sf = (f"{c['earnings_selloff_pct']*100:.1f}%"
                  if c.get("earnings_selloff_pct") != "" else "n/a")
            sp = (f"{c['surprise_pct']:.1f}%"
                  if c.get("surprise_pct") != "" else "n/a")
            verdict = ("price move not justified by the print"
                       if c.get("pec_type") == "overreaction"
                       else "sellers liquidating a broken print")
            print(f"  {c['ticker']:6s} — {days}d post-print  "
                  f"{sf} selloff vs {sp} EPS surprise  score {c['pec_score']}/10  "
                  f"[{c.get('pec_type','')}: {verdict}]")

        print("\nTo log these tickers into the forward-test journal with full flow history,")
        print("fetch each ticker's OHLC (get_ticker_ohlc_latest_or_date, ≥70 rows) then:")
        for c in signals:
            t = c["ticker"]
            print(f"  python whale_log.py --ticker {t} "
                  f"--history data/_log_{t}.json --journal logs/whale_journal.csv")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Post-Earnings Capitulation Scanner")
    ap.add_argument("--days-back", type=int, default=5,
                    help="earnings reported in last N calendar days (default 5)")
    ap.add_argument("--min-selloff", type=float, default=0.05,
                    help="minimum price decline to qualify (default 0.05 = 5%%)")
    ap.add_argument("--min-marketcap", type=int, default=2_000_000_000,
                    help="minimum market cap (default 2 000 000 000 = $2B)")
    ap.add_argument("--max-surprise", type=float, default=15.0,
                    help="max EPS surprise %% to include (default 15; above 0 "
                         "so beats that still sold off are caught)")
    ap.add_argument("--output", default="logs/pec_candidates.csv",
                    help="candidates CSV output path")
    ap.add_argument("--journal", default="logs/whale_journal.csv",
                    help="whale_journal.csv path for stub rows")
    ap.add_argument("--no-journal-stub", action="store_true",
                    help="skip writing stub rows to whale_journal.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="print results but write no files")
    args = ap.parse_args(argv)

    today = date.today().isoformat()
    min_date = (date.today() - timedelta(days=args.days_back)).isoformat()

    print(f"Post-Earnings Capitulation Scanner  {today}")
    print(f"Window {min_date}→{today}  "
          f"min-selloff {args.min_selloff*100:.0f}%  "
          f"min-mktcap ${args.min_marketcap/1e9:.0f}B\n")

    try:
        client = UWClient()
    except UWError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    e_rows = stage1_earnings(client, min_date, today, args.min_marketcap,
                             args.max_surprise)
    if not e_rows:
        print("No earnings data returned — check credentials or date range.")
        return 0

    tickers = sorted({_ticker(r) for r in e_rows if _ticker(r)})
    flow_map = stage2_flow(client, tickers)
    accum_map = stage3_accum(client, list(flow_map.keys()))

    # Load earnings cache (may be empty on first run).
    if not args.dry_run:
        update_earnings_cache(e_rows)
    e_cache = earnings_mod.load()
    stage1b_pre_closes(client, e_rows, e_cache, persist=not args.dry_run)

    candidates = build_candidates(
        e_rows, flow_map, accum_map, e_cache, today, args.min_selloff
    )
    print_table(candidates)

    if args.dry_run:
        print("(dry-run — no files written)")
        return 0

    write_candidates(candidates, args.output, today)
    print(f"candidates  → {args.output}  ({len(candidates)} rows)")

    if not args.no_journal_stub:
        n = write_journal_stubs(candidates, args.journal)
        if n:
            print(f"journal     → {args.journal}  ({n} new stub row{'s' if n != 1 else ''} added)")
        else:
            print(f"journal     → {args.journal}  (no new stubs — all already logged or no signals)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
