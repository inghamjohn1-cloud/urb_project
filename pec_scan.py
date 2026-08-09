#!/usr/bin/env python3
"""Post-Earnings Capitulation Scanner.

Three-stage pipeline run once per trading day (after close) to find stocks
that reported a badly broken quarter and are being sold hard in the options
market — a setup that historically reverts over the following month.

Tuned against pec_study.py (945 events, 119 names) and then tested against
100 names that search never touched (770 events), under a prediction
registered in OOS_PREREGISTRATION.md before the data was pulled.

  gate: EPS miss <= -20% AND negative net premium on the reaction day

              21d edge     t      win
  in-sample     +4.26%   +2.27   71.1%   (n=45)
  OUT-OF-SAMPLE +4.09%   +1.01   55.4%   (n=74)

The effect size replicated; the significance did not. The ticker-clustered
95% CI out-of-sample was [-2.10%, +12.17%], which crosses zero, so the
registered verdict is NOT CONFIRMED. Note the win rate: 71% collapsing to
55% means the in-sample hit rate was luck and the surviving edge lives in a
few large winners, not a reliably favourable distribution. Trade it small
or not at all until the forward test says more.

What the out-of-sample run killed outright: the -$1M flow-magnitude split,
which inverted (strong +3.4%, weak +5.2%) after scoring +5.2% vs +1.1%
in-sample. It no longer contributes to the score.

and what the original study had already ruled out:

  A stock falling further than the options market priced in carries NO
  information (n=172, edge ~0.0% at every horizon). An earlier version of
  this scanner scored that "overreaction" case; the study killed it. Size
  of the selloff is likewise not predictive within the qualifying set
  (-10% or worse: +5.0%, milder: +3.9% — the same trade).

So the two gates below are requirements, not points. Everything the score
adds on top is a tiebreaker, and only the net-premium magnitude tier has
evidence behind it.

  Stage 1 — Earnings screen  : stocks that reported in the last N days with
                                an EPS miss at or beyond --max-surprise.
  Stage 2 — Flow screen      : price damage vs the pre-earnings close, plus
                                the net premium that gates the signal.
  Stage 3 — Accumulation     : large floor/block call buying via flow alert
                                rules. Unvalidated — the alert history does
                                not reach back far enough to test.

Holding period: the edge is a 10-to-21-day effect. There was nothing at 5
days (+0.6%), so this is not a bounce trade.

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
    "pec_score", "pec_signal",
]

# Gate 1: EPS surprise as a percentage of the estimate. -20% is where the
# study's edge appears and it is not a knife edge — the bucket boundary was
# tested, not fitted. Misses milder than this behave like the baseline.
MISS_PCT = -20.0

# Gate 2: net options premium on the reaction day must be negative. Sign
# alone decides the gate; magnitude decides the score. -$1M separated
# t=2.44 from t=0.26, which is the difference between a signal and a coin flip.
STRONG_FLOW = -1_000_000.0

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

def pec_qualifies(c: dict) -> bool:
    """The two gates the historical study actually supports.

    Both must hold. A miss with positive net premium was the single worst
    bucket measured (-3.7% at 21d, 44% win) — worse than no trade at all —
    so this is a conjunction, not a preponderance of evidence.
    """
    surprise = _f(c.get("surprise_pct"))
    net_prem = _f(c.get("net_premium"))
    selloff = _f(c.get("earnings_selloff_pct"))
    return (surprise is not None and surprise <= MISS_PCT
            and net_prem is not None and net_prem < 0
            and selloff is not None and selloff < 0)


# ── PEC score (0–5) ───────────────────────────────────────────────────────────

def pec_score(c: dict) -> int:
    """Rank conviction among candidates that already passed both gates.

    Deliberately small. The old 0–10 score spread five dimensions across ten
    points and implied a precision the data does not support: of those five,
    the study found selloff magnitude and earnings/price dislocation carry no
    signal at all. Only the first point below is evidence-backed.

    Returns 0 for anything that fails the gates, so score and signal cannot
    disagree.
    """
    if not pec_qualifies(c):
        return 0

    score = 1  # cleared both gates

    # FAILED REPLICATION: flow magnitude. In-sample, below -$1M scored +5.2%
    # at 21d against +1.1% above it, and this was worth 2 points as the one
    # "validated" dimension. On 100 unseen tickers the split inverted —
    # strong +3.4%, weak +5.2%. It was overfitting, so it no longer scores.
    # STRONG_FLOW survives only as the label in print_table and the
    # pre-registered whale_eval split, which the forward test still tracks.
    net_prem = _f(c.get("net_premium"), 0.0) or 0.0

    # THIN: elevated IV rank looked strong (+9.9% edge, 86% win) on n=7.
    # One point, purely to break ties and accumulate forward evidence. Given
    # that the far better-powered flow split above still failed to replicate,
    # treat this as unproven rather than merely under-sampled.
    iv = _f(c.get("iv_rank"))
    if iv is not None and iv > 50:
        score += 1

    # UNVALIDATED: call accumulation. The flow-alert history does not reach
    # back far enough to test, so it stays a tiebreaker rather than a gate —
    # it is the leg the original thesis was built on and still unproven.
    alerts = (int(c.get("floor_alert_count") or 0)
              + int(c.get("repeat_hit_count") or 0)
              + int(c.get("sweep_floor_count") or 0))
    if alerts >= 1:
        score += 1

    return min(score, 5)


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
        }
        c["pec_score"] = pec_score(c)
        # The gates decide the signal; the score only ranks what already
        # passed. Keeping the old "score >= threshold" test would let a stack
        # of tiebreakers manufacture a signal the study never supported.
        c["pec_signal"] = pec_qualifies(c)
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
    W = 104
    print(f"\n{'─' * W}")
    print(f"{'POST-EARNINGS CAPITULATION CANDIDATES':^{W}}")
    print(f"{'─' * W}")
    hdr = (f"{'TICKER':<8} {'SCR':>4}  {'GATES':<9}  {'DAYS':>4}  {'SELLOFF':>8}  "
           f"{'SURP%':>8}  {'NET_PREM':>10}  {'IVR':>5}  "
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
        iv = _f(c.get("iv_rank"))
        iv_s = f"{iv:.0f}" if iv is not None else "n/a"
        alerts = (c.get("floor_alert_count", 0)
                  + c.get("repeat_hit_count", 0)
                  + c.get("sweep_floor_count", 0))
        sig = " ◆" if c["pec_signal"] else "  "
        # Show which gate a rejected row failed, so it is diagnosable without
        # re-reading the CSV. Name the gate that failed, not the one that held.
        sp_v = _f(c.get("surprise_pct"))
        if c["pec_signal"]:
            gates = "miss+flow"
        elif sp_v is None or sp_v > MISS_PCT:
            gates = "no miss"
        else:
            gates = "no flow"
        days = c.get("days_since_earnings")
        days_s = str(days) if days is not None else "?"
        print(f"{c['ticker']:<8} {c['pec_score']:>3}{sig}  "
              f"{gates:<9}  {days_s:>4}  "
              f"{selloff:>8}  {surp:>8}  {net_p:>10}  {iv_s:>5}  "
              f"{alerts:>6}  {c.get('sector',''):<22}")

    print("─" * W)
    signals = [c for c in candidates if c["pec_signal"]]
    print(f"\n◆ = both gates passed (EPS miss ≤ {MISS_PCT:.0f}% and negative net premium)")
    print(f"{len(signals)} signal{'s' if len(signals) != 1 else ''} "
          f"of {len(candidates)} candidates")

    if signals:
        print("\nQUALIFYING SETUPS:")
        for c in signals:
            days = c.get("days_since_earnings", "?")
            sf = (f"{c['earnings_selloff_pct']*100:.1f}%"
                  if c.get("earnings_selloff_pct") != "" else "n/a")
            sp = (f"{c['surprise_pct']:.1f}%"
                  if c.get("surprise_pct") != "" else "n/a")
            np_v = _f(c.get("net_premium"), 0.0) or 0.0
            strength = ("flow below -$1M — the tier that carried the edge"
                        if np_v <= STRONG_FLOW
                        else "flow negative but shallow — the weak tier (t=0.26)")
            print(f"  {c['ticker']:6s} — {days}d post-print  "
                  f"{sp} EPS miss, {sf} selloff  score {c['pec_score']}/5")
            print(f"           {strength}")
        print("\nHold to 10–21 days. The study found nothing at 5 days (+0.6%),")
        print("so exiting early gives up the entire measured effect.")

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
    ap.add_argument("--min-selloff", type=float, default=0.0,
                    help="minimum price decline to qualify (default 0.0 = any "
                         "decline; selloff size was not predictive)")
    ap.add_argument("--min-marketcap", type=int, default=2_000_000_000,
                    help="minimum market cap (default 2 000 000 000 = $2B)")
    ap.add_argument("--max-surprise", type=float, default=MISS_PCT,
                    help=f"max EPS surprise %% to include (default {MISS_PCT:.0f}, "
                         "the study's miss threshold)")
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
          f"EPS miss ≤{args.max_surprise:.0f}%  "
          f"min-selloff {args.min_selloff*100:.0f}%  "
          f"min-mktcap ${args.min_marketcap/1e9:.0f}B")
    print("gates: EPS miss + negative net premium (pec_study.py, n=945)\n")

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
