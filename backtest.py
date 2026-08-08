#!/usr/bin/env python3
"""Backtester for the sector-rotation swing strategy.

Replays the *exact* signals produced by the scanner (`rotation.py`) over
historical data: on each rebalance date it ranks the 11 SPDR sector ETFs
as-of that date (no lookahead), buys the `ROTATE IN` names equal-weight,
holds to the next rebalance, and parks unfilled slots in cash. Reports
CAGR, max drawdown, Sharpe/Sortino, win rate and exposure versus a SPY
buy-and-hold benchmark.

Usage:
    python backtest.py                         # 5y history, weekly rebalance, top 3
    python backtest.py --rebalance monthly     # monthly rebalance
    python backtest.py --top 4 --cost-bps 5    # top 4, 5bps per-side cost
    python backtest.py --timeframe 8Y --csv equity.csv

Requires an Unusual Whales API token in UW_TOKEN (see .env.example).

Note: the live options-flow overlay (sector tide) is a real-time signal and
is intentionally NOT used here — a backtest runs on price/momentum/trend only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import statistics
import sys
from dataclasses import dataclass

from uw_client import UWClient, UWError
from rotation import (
    SECTOR_ETFS, BENCHMARK, SMA_SLOW,
    build_score, rank_sectors, market_regime,
)

TRADING_DAYS = 252
REBALANCE_PRESETS = {"weekly": 5, "biweekly": 10, "monthly": 21}


@dataclass
class Config:
    top_n: int = 3
    bottom_n: int = 3
    rebalance_days: int = 5
    capital: float = 100_000.0
    cost_bps: float = 2.0          # per-side transaction cost in basis points
    warmup: int = SMA_SLOW         # bars of history required before first trade


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------
def _candle_date(c: dict) -> str:
    d = c.get("date") or c.get("start_time") or ""
    return str(d)[:10]


def fetch_data(client: UWClient, timeframe: str):
    """Return (calendar, dates_by_ticker, candles_by_ticker, close_by_ticker).

    calendar        : sorted list of benchmark trading dates (master clock)
    dates_by_ticker : {ticker: [sorted date strings]}
    candles_by_ticker: {ticker: [candle dicts aligned with dates]}
    close_by_ticker : {ticker: {date: close_float}}
    """
    tickers = [BENCHMARK] + list(SECTOR_ETFS)
    dates_by_ticker: dict[str, list[str]] = {}
    candles_by_ticker: dict[str, list[dict]] = {}
    close_by_ticker: dict[str, dict[str, float]] = {}

    for t in tickers:
        candles = client.daily_ohlc(t, timeframe)  # oldest -> newest
        dated = []
        for c in candles:
            d = _candle_date(c)
            close = c.get("close")
            if not d or close is None:
                continue
            dated.append((d, c, float(close)))
        dated.sort(key=lambda x: x[0])
        dates_by_ticker[t] = [x[0] for x in dated]
        candles_by_ticker[t] = [x[1] for x in dated]
        close_by_ticker[t] = {x[0]: x[2] for x in dated}

    calendar = dates_by_ticker[BENCHMARK]
    return calendar, dates_by_ticker, candles_by_ticker, close_by_ticker


def _asof(dates: list[str], candles: list[dict], d: str) -> list[dict]:
    """Candles with date <= d (no lookahead)."""
    n = bisect.bisect_right(dates, d)
    return candles[:n]


# --------------------------------------------------------------------------
# Signal selection (mirrors the scanner exactly)
# --------------------------------------------------------------------------
def select_holdings(d: str, dates_by_ticker, candles_by_ticker, cfg: Config) -> list[str]:
    bench_asof = _asof(dates_by_ticker[BENCHMARK], candles_by_ticker[BENCHMARK], d)
    bench_closes = [float(c["close"]) for c in bench_asof if c.get("close") is not None]
    regime, _ = market_regime(bench_asof)
    risk_off = regime == "RISK-OFF"

    scores = []
    for t in SECTOR_ETFS:
        c_asof = _asof(dates_by_ticker[t], candles_by_ticker[t], d)
        sc = build_score(t, c_asof, bench_closes)
        if sc:
            scores.append(sc)
    if not scores:
        return []
    rank_sectors(scores, top_n=cfg.top_n, bottom_n=cfg.bottom_n, risk_off=risk_off)
    return [s.ticker for s in scores if s.signal == "ROTATE IN"]


# --------------------------------------------------------------------------
# Simulation core (pure — no network, unit-testable)
# --------------------------------------------------------------------------
def run_backtest(calendar, dates_by_ticker, candles_by_ticker, close_by_ticker, cfg: Config):
    equity = cfg.capital
    cash = cfg.capital
    positions: dict[str, float] = {}          # ticker -> dollar value
    curve: list[tuple[str, float]] = []
    rebal_equity: list[tuple[str, float]] = []  # equity snapshots at rebalances
    trades_log: list[dict] = []
    exposure_samples: list[float] = []
    cost_bps = cfg.cost_bps / 10_000.0

    first_trade_idx = cfg.warmup
    for i, d in enumerate(calendar):
        # 1) mark existing positions to market using this day's return
        if i > 0 and positions:
            prev = calendar[i - 1]
            for t in list(positions):
                p0 = close_by_ticker[t].get(prev)
                p1 = close_by_ticker[t].get(d)
                if p0 and p1:
                    positions[t] *= p1 / p0
        equity = cash + sum(positions.values())

        # 2) rebalance on schedule, once warmed up
        is_rebal = i >= first_trade_idx and ((i - first_trade_idx) % cfg.rebalance_days == 0)
        if is_rebal:
            holdings = select_holdings(d, dates_by_ticker, candles_by_ticker, cfg)
            # equal weight across fixed slots; unfilled slots -> cash (de-risking)
            slot = equity / cfg.top_n
            target = {t: slot for t in holdings[: cfg.top_n]}
            turnover = sum(
                abs(target.get(t, 0.0) - positions.get(t, 0.0))
                for t in set(target) | set(positions)
            )
            cost = turnover * cost_bps
            positions = target
            cash = equity - sum(positions.values()) - cost
            equity = cash + sum(positions.values())
            rebal_equity.append((d, equity))
            trades_log.append({"date": d, "equity": round(equity, 2),
                               "holdings": ",".join(holdings[: cfg.top_n]) or "CASH"})

        if i >= first_trade_idx:
            invested = sum(positions.values())
            exposure_samples.append(invested / equity if equity else 0.0)
            curve.append((d, equity))

    return {
        "curve": curve,
        "rebal_equity": rebal_equity,
        "trades_log": trades_log,
        "avg_exposure": statistics.mean(exposure_samples) if exposure_samples else 0.0,
    }


def benchmark_curve(calendar, close_by_ticker, cfg: Config):
    """SPY buy-and-hold over the same tested window (post-warmup)."""
    dates = [d for d in calendar[cfg.warmup:] if d in close_by_ticker[BENCHMARK]]
    if not dates:
        return []
    start_px = close_by_ticker[BENCHMARK][dates[0]]
    shares = cfg.capital / start_px
    return [(d, shares * close_by_ticker[BENCHMARK][d]) for d in dates]


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def compute_metrics(curve: list[tuple[str, float]]) -> dict:
    if len(curve) < 2:
        return {}
    values = [v for _, v in curve]
    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))
            if values[i - 1] > 0]

    start, end = values[0], values[-1]
    n_days = len(values)
    years = n_days / TRADING_DAYS
    total_return = end / start - 1.0
    cagr = (end / start) ** (1 / years) - 1.0 if years > 0 and start > 0 else 0.0

    mean_d = statistics.mean(rets) if rets else 0.0
    std_d = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    downside = [r for r in rets if r < 0]
    dstd_d = statistics.pstdev(downside) if len(downside) > 1 else 0.0

    ann_ret = mean_d * TRADING_DAYS
    ann_vol = std_d * math.sqrt(TRADING_DAYS)
    sharpe = ann_ret / ann_vol if ann_vol else 0.0
    sortino = ann_ret / (dstd_d * math.sqrt(TRADING_DAYS)) if dstd_d else 0.0

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    calmar = cagr / abs(max_dd) if max_dd else 0.0

    return {
        "start": start, "end": end,
        "total_return": total_return, "cagr": cagr,
        "ann_vol": ann_vol, "sharpe": sharpe, "sortino": sortino,
        "max_dd": max_dd, "calmar": calmar, "years": years,
    }


def period_win_rate(rebal_equity: list[tuple[str, float]]) -> tuple[float, int]:
    if len(rebal_equity) < 2:
        return 0.0, 0
    vals = [v for _, v in rebal_equity]
    periods = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))]
    wins = sum(1 for p in periods if p > 0)
    return (wins / len(periods) * 100.0 if periods else 0.0), len(periods)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _pct(v):
    return f"{v * 100:+.2f}%" if v is not None else "-"


def print_report(strat, bench, res, cfg, first_date, last_date):
    print()
    print("=" * 78)
    print("  SECTOR ROTATION BACKTEST")
    print("=" * 78)
    reb = {v: k for k, v in REBALANCE_PRESETS.items()}.get(cfg.rebalance_days,
                                                           f"{cfg.rebalance_days}d")
    print(f"  window   : {first_date} -> {last_date}  ({strat.get('years', 0):.1f}y)")
    print(f"  rules    : top {cfg.top_n} sectors, rebalance {reb}, "
          f"{cfg.cost_bps:.0f}bps/side, ${cfg.capital:,.0f} start")
    win, n_periods = period_win_rate(res["rebal_equity"])
    print(f"  activity : {len(res['trades_log'])} rebalances, "
          f"{res['avg_exposure'] * 100:.0f}% avg exposure, {win:.0f}% winning periods")
    print("-" * 78)
    print(f"  {'metric':<20}{'STRATEGY':>16}{'SPY BUY & HOLD':>18}{'edge':>14}")
    print("-" * 78)
    rows = [
        ("Total return", "total_return", True),
        ("CAGR", "cagr", True),
        ("Max drawdown", "max_dd", True),
        ("Annual volatility", "ann_vol", True),
        ("Sharpe", "sharpe", False),
        ("Sortino", "sortino", False),
        ("Calmar", "calmar", False),
    ]
    for label, key, is_pct in rows:
        s = strat.get(key)
        b = bench.get(key)
        if is_pct:
            sv, bv = _pct(s), _pct(b)
            edge = _pct(s - b) if (s is not None and b is not None) else "-"
        else:
            sv = f"{s:.2f}" if s is not None else "-"
            bv = f"{b:.2f}" if b is not None else "-"
            edge = f"{s - b:+.2f}" if (s is not None and b is not None) else "-"
        print(f"  {label:<20}{sv:>16}{bv:>18}{edge:>14}")
    print("-" * 78)
    final_s = strat.get("end", cfg.capital)
    final_b = bench.get("end", cfg.capital)
    print(f"  ${cfg.capital:,.0f} -> ${final_s:,.0f} (strategy) "
          f"vs ${final_b:,.0f} (SPY)")
    print("\n  Backtest on price/momentum/trend only; the live flow overlay is not")
    print("  modelled here. Past performance does not guarantee future results.\n")


def write_curves_csv(path, strat_curve, bench_curve):
    bmap = dict(bench_curve)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "strategy_equity", "spy_equity"])
        for d, v in strat_curve:
            w.writerow([d, round(v, 2), round(bmap.get(d, ""), 2) if d in bmap else ""])
    print(f"  wrote {path}")


def write_trades_csv(path, trades_log):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "equity", "holdings"])
        for row in trades_log:
            w.writerow([row["date"], row["equity"], row["holdings"]])
    print(f"  wrote {path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_rebalance(v: str) -> int:
    if v in REBALANCE_PRESETS:
        return REBALANCE_PRESETS[v]
    try:
        n = int(v)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--rebalance must be one of {list(REBALANCE_PRESETS)} or a positive integer")


def main(argv=None) -> int:
    from scan import load_dotenv  # reuse the minimal .env loader
    parser = argparse.ArgumentParser(description="Sector rotation backtester")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--bottom", type=int, default=3)
    parser.add_argument("--rebalance", type=parse_rebalance, default=5,
                        help="weekly|biweekly|monthly or a number of trading days")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cost-bps", type=float, default=2.0)
    parser.add_argument("--timeframe", default="5Y", help="history window, e.g. 3Y, 5Y, 8Y")
    parser.add_argument("--csv", metavar="PATH", help="write equity curves to CSV")
    parser.add_argument("--trades-csv", metavar="PATH", help="write rebalance log to CSV")
    args = parser.parse_args(argv)

    cfg = Config(top_n=args.top, bottom_n=args.bottom,
                 rebalance_days=args.rebalance, capital=args.capital,
                 cost_bps=args.cost_bps)

    load_dotenv()
    try:
        client = UWClient()
    except UWError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        calendar, dates_bt, candles_bt, close_bt = fetch_data(client, args.timeframe)
    except UWError as e:
        print(f"error fetching data: {e}", file=sys.stderr)
        return 1

    if len(calendar) <= cfg.warmup + cfg.rebalance_days:
        print(f"error: not enough history ({len(calendar)} bars) for a "
              f"{cfg.warmup}-bar warmup. Try a longer --timeframe.", file=sys.stderr)
        return 1

    res = run_backtest(calendar, dates_bt, candles_bt, close_bt, cfg)
    if not res["curve"]:
        print("error: backtest produced no equity curve", file=sys.stderr)
        return 1

    bench = benchmark_curve(calendar, close_bt, cfg)
    strat_metrics = compute_metrics(res["curve"])
    bench_metrics = compute_metrics(bench)

    first_date = res["curve"][0][0]
    last_date = res["curve"][-1][0]
    print_report(strat_metrics, bench_metrics, res, cfg, first_date, last_date)

    if args.csv:
        write_curves_csv(args.csv, res["curve"], bench)
    if args.trades_csv:
        write_trades_csv(args.trades_csv, res["trades_log"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
