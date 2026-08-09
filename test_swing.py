#!/usr/bin/env python3
"""Self-tests for the single-name swing scanner.

Runs entirely offline on synthetic candles — no API keys, no network. The point
is to prove that (a) the indicators match their TradingView definitions, (b) the
hard gates actually reject, and (c) a clean setup outscores a messy one.

    python test_swing.py
"""

from __future__ import annotations

import datetime as _dt
import math
import unittest

import swing_indicators as ind
import swing_flow as fl
from swing_config import UNIVERSE, TECHNICAL
from swing_score import (
    build_candidate, check_gates, check_options_universe, score_flow,
    score_technical, score_darkpool, assign_tier, rank,
)


TODAY = _dt.date(2026, 8, 10)

# The options-universe ceilings are ACCOUNT-SIZE POLICY, not engine behaviour.
# Every test below exercises the engine, so the policy is switched off for the
# module and switched back on explicitly by TestOptionsUniverse. Without this
# the synthetic fixture (which drifts to ~$156) would be screened out and the
# gate/scoring tests would stop testing what they were written to test.
_CEILINGS = {}


def setUpModule():
    for k in ("options_max_atr_dollars", "options_max_price"):
        _CEILINGS[k] = UNIVERSE.get(k)
        UNIVERSE[k] = None


def tearDownModule():
    UNIVERSE.update(_CEILINGS)


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------
def make_candles(n: int = 300, start: float = 100.0, drift: float = 0.0015,
                 wobble: float = 0.01, volume: float = 5_000_000.0,
                 last_volume_mult: float = 1.0) -> list[dict]:
    """Deterministic pseudo-random walk with a controllable drift."""
    candles = []
    price = start
    for i in range(n):
        # Deterministic "noise" so tests never flake.
        noise = math.sin(i * 1.7) * wobble
        price *= (1.0 + drift + noise * 0.3)
        high = price * (1.0 + abs(noise) + 0.004)
        low = price * (1.0 - abs(noise) - 0.004)
        vol = volume * (1.0 + 0.1 * math.cos(i * 0.9))
        if i == n - 1:
            vol *= last_volume_mult
        candles.append({
            "open": round(price * 0.999, 4), "high": round(high, 4),
            "low": round(low, 4), "close": round(price, 4),
            "volume": round(vol, 0),
        })
    return candles


def make_alert(ticker="AAAA", premium=400_000.0, side="call", dte=35,
               ask_prem=None, bid_prem=0.0, sweep=True, opening=True,
               strike=110.0, rule="RepeatedHitsAscendingFill", day_offset=0,
               marketcap=50e9, avg_vol=5_000_000.0, earnings=None,
               spread=0.04, volume=2000.0, oi=800.0) -> dict:
    """A raw flow-alert row shaped like the UW payload."""
    return {
        "ticker": ticker,
        "total_premium": premium,
        "total_ask_side_prem": premium if ask_prem is None else ask_prem,
        "total_bid_side_prem": bid_prem,
        "type": side,
        "strike": strike,
        "expiry": (TODAY + _dt.timedelta(days=dte)).isoformat(),
        "volume": volume,
        "open_interest": oi,
        "has_sweep": sweep,
        "all_opening_trades": opening,
        "is_multi_leg": False,
        "alert_rule": rule,
        "created_at": (TODAY - _dt.timedelta(days=day_offset)).isoformat(),
        "underlying_price": 100.0,
        "nbbo_bid": 1.0 * (1 - spread / 2),
        "nbbo_ask": 1.0 * (1 + spread / 2),
        "sector": "Technology",
        "marketcap": marketcap,
        "avg30_stock_volume": avg_vol,
        "next_earnings_date": earnings,
    }


def aggregate(rows: list[dict]) -> dict:
    """Normalise + gate + roll up, returning the single ticker record."""
    norm = []
    for r in rows:
        a = fl.normalize_alert(r, TODAY)
        ok, _ = fl.alert_passes(a, TODAY)
        if ok:
            norm.append(a)
    agg = fl.aggregate_alerts(norm)
    return next(iter(agg.values())) if agg else {}


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
class TestIndicators(unittest.TestCase):
    def test_ema_matches_manual_calculation(self):
        vals = [float(x) for x in range(1, 21)]
        got = ind.ema(vals, 5)
        # SMA seed over the first 5, then k = 2/6 smoothing.
        k = 2.0 / 6.0
        prev = sum(vals[:5]) / 5.0
        for v in vals[5:]:
            prev = v * k + prev * (1 - k)
        self.assertAlmostEqual(got, prev, places=9)

    def test_ema_needs_enough_bars(self):
        self.assertIsNone(ind.ema([1.0, 2.0], 50))

    def test_rsi_all_gains_is_100(self):
        self.assertAlmostEqual(ind.rsi([float(i) for i in range(1, 40)], 14), 100.0)

    def test_rsi_all_losses_is_zero(self):
        self.assertAlmostEqual(ind.rsi([float(i) for i in range(40, 1, -1)], 14), 0.0)

    def test_rsi_flat_series_is_neutral_or_undefined(self):
        # A perfectly flat series has zero average gain and loss -> RSI 100 by
        # the standard formula; assert we at least return a finite number.
        r = ind.rsi([50.0] * 40, 14)
        self.assertIsNotNone(r)
        self.assertTrue(0.0 <= r <= 100.0)

    def test_rsi_in_range_for_a_random_walk(self):
        c = make_candles(200)
        r = ind.rsi(ind.closes(c), 14)
        self.assertTrue(0.0 < r < 100.0)

    def test_atr_is_positive_and_wilder_smoothed(self):
        c = make_candles(100)
        a = ind.atr(c, 14)
        self.assertIsNotNone(a)
        self.assertGreater(a, 0.0)
        # ATR should sit inside the range of recent true ranges.
        trs = ind.true_ranges(c)[-14:]
        self.assertGreaterEqual(a, min(trs))
        self.assertLessEqual(a, max(trs))

    def test_breakout_detection(self):
        c = make_candles(60, drift=0.0)
        c[-1]["close"] = max(x["high"] for x in c[-21:-1]) * 1.05
        self.assertTrue(ind.is_breakout(c, 20, "long"))

    def test_volume_ratio_picks_up_a_surge(self):
        c = make_candles(60, last_volume_mult=3.0)
        self.assertGreater(ind.volume_ratio(c, 20), 2.0)

    def test_snapshot_has_all_expected_fields(self):
        s = ind.snapshot(make_candles(300))
        for key in ("last", "ema_fast", "ema_slow", "rsi", "atr",
                    "volume_ratio", "avg_volume", "pct_from_52w_high"):
            self.assertIn(key, s)
            self.assertIsNotNone(s[key], f"{key} should be computed with 300 bars")

    def test_snapshot_degrades_without_history(self):
        s = ind.snapshot(make_candles(60))
        self.assertIsNotNone(s["ema_fast"])
        self.assertIsNone(s["ema_slow"], "200 EMA needs more than 60 bars")


# ---------------------------------------------------------------------------
# Flow parsing / aggregation
# ---------------------------------------------------------------------------
class TestFlow(unittest.TestCase):
    def test_alert_below_premium_floor_is_rejected(self):
        a = fl.normalize_alert(make_alert(premium=100_000), TODAY)
        ok, why = fl.alert_passes(a, TODAY)
        self.assertFalse(ok)
        self.assertIn("premium", why)

    def test_dte_outside_band_is_rejected(self):
        for dte in (5, 200):
            a = fl.normalize_alert(make_alert(dte=dte), TODAY)
            ok, why = fl.alert_passes(a, TODAY)
            self.assertFalse(ok, f"dte {dte} should fail")
            self.assertIn("dte", why)

    def test_dte_inside_band_passes(self):
        a = fl.normalize_alert(make_alert(dte=35), TODAY)
        ok, why = fl.alert_passes(a, TODAY)
        self.assertTrue(ok, why)

    def test_multi_leg_is_rejected(self):
        row = make_alert()
        row["is_multi_leg"] = True
        a = fl.normalize_alert(row, TODAY)
        ok, why = fl.alert_passes(a, TODAY)
        self.assertFalse(ok)
        self.assertEqual(why, "multi-leg")

    def test_stale_alert_is_rejected(self):
        a = fl.normalize_alert(make_alert(day_offset=10), TODAY)
        ok, why = fl.alert_passes(a, TODAY)
        self.assertFalse(ok)
        self.assertEqual(why, "stale alert")

    def test_direction_follows_dominant_premium(self):
        bull = aggregate([make_alert(premium=1e6, side="call"),
                          make_alert(premium=2e5 + 1e5, side="put", strike=90.0)])
        self.assertEqual(bull["direction"], "long")
        bear = aggregate([make_alert(premium=1e6, side="put", strike=90.0),
                          make_alert(premium=4e5, side="call")])
        self.assertEqual(bear["direction"], "short")

    def test_coherence_is_one_when_flow_is_one_sided(self):
        agg = aggregate([make_alert(premium=1e6, side="call"),
                         make_alert(premium=8e5, side="call", strike=115.0)])
        self.assertAlmostEqual(agg["coherence"], 1.0, places=6)

    def test_coherence_is_zero_when_flow_is_split(self):
        agg = aggregate([make_alert(premium=1e6, side="call"),
                         make_alert(premium=1e6, side="put", strike=90.0)])
        self.assertAlmostEqual(agg["coherence"], 0.0, places=6)

    def test_cluster_counts_strikes_expiries_and_days(self):
        agg = aggregate([
            make_alert(premium=5e5, strike=110.0, dte=30, day_offset=0),
            make_alert(premium=5e5, strike=115.0, dte=30, day_offset=1),
            make_alert(premium=5e5, strike=120.0, dte=60, day_offset=1),
        ])
        self.assertEqual(agg["n_alerts"], 3)
        self.assertEqual(agg["n_strikes"], 3)
        self.assertEqual(agg["n_expiries"], 2)
        self.assertEqual(agg["n_days"], 2)

    def test_darkpool_aggregation_detects_accumulation(self):
        prints = [{"premium": 5e6, "size": 50_000, "price": 100.9,
                   "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 4
        dp = fl.aggregate_darkpool(prints, avg_volume=5_000_000)
        self.assertTrue(dp["has_data"])
        self.assertGreater(dp["lean"], 0.5, "prints near the ask = accumulation")
        self.assertAlmostEqual(dp["pct_of_adv"], 0.04, places=4)

    def test_darkpool_aggregation_detects_distribution(self):
        prints = [{"premium": 5e6, "size": 50_000, "price": 100.1,
                   "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 4
        dp = fl.aggregate_darkpool(prints, avg_volume=5_000_000)
        self.assertLess(dp["lean"], -0.5)

    def test_darkpool_with_no_prints_is_no_data_not_negative(self):
        dp = fl.aggregate_darkpool([], avg_volume=5_000_000)
        self.assertFalse(dp["has_data"])
        pts, notes = score_darkpool(dp, "long")
        self.assertEqual(pts, 0.0)
        self.assertIn("no dark-pool blocks", notes)


# ---------------------------------------------------------------------------
# Direction classification (option type AND side of the spread)
# ---------------------------------------------------------------------------
def bought(side, premium=2e6, **kw):
    """An alert that lifted the offer — the whole premium on the ask."""
    return make_alert(side=side, premium=premium, ask_prem=premium, bid_prem=0.0, **kw)


def sold(side, premium=2e6, **kw):
    """An alert that hit the bid — the whole premium on the bid side."""
    return make_alert(side=side, premium=premium, ask_prem=0.0, bid_prem=premium, **kw)


class TestDirectionClassification(unittest.TestCase):
    """The four type x side combinations, and that the label propagates."""

    # -- the truth table ---------------------------------------------------
    def test_ask_side_calls_are_bullish(self):
        agg = aggregate([bought("call")])
        self.assertEqual(agg["direction"], "long")
        self.assertEqual(agg["bullish_premium"], 2e6)
        self.assertEqual(agg["bearish_premium"], 0.0)

    def test_bid_side_calls_are_bearish(self):
        """Selling calls is a bearish position, not a bullish one."""
        agg = aggregate([sold("call")])
        self.assertEqual(agg["direction"], "short")
        self.assertEqual(agg["bearish_premium"], 2e6)
        self.assertEqual(agg["bullish_premium"], 0.0)

    def test_ask_side_puts_are_bearish(self):
        agg = aggregate([bought("put", strike=90.0)])
        self.assertEqual(agg["direction"], "short")
        self.assertEqual(agg["bearish_premium"], 2e6)
        self.assertEqual(agg["bullish_premium"], 0.0)

    def test_bid_side_puts_are_bullish(self):
        """Selling puts is a bullish position — the regression this fixes."""
        agg = aggregate([sold("put", strike=90.0)])
        self.assertEqual(agg["direction"], "long")
        self.assertEqual(agg["bullish_premium"], 2e6)
        self.assertEqual(agg["bearish_premium"], 0.0)

    # -- regressions against the old call-vs-put logic ---------------------
    def test_sold_puts_no_longer_read_as_short(self):
        """Old logic keyed on put premium alone and returned 'short' here."""
        agg = aggregate([sold("put", premium=3e6, strike=90.0)])
        self.assertGreater(agg["put_premium"], agg["call_premium"],
                           "put premium still dominates by option type")
        self.assertEqual(agg["direction"], "long",
                         "but the position is bullish, so direction must be long")

    def test_sold_calls_no_longer_read_as_long(self):
        agg = aggregate([sold("call", premium=3e6)])
        self.assertGreater(agg["call_premium"], agg["put_premium"])
        self.assertEqual(agg["direction"], "short")

    def test_mixed_book_nets_by_side_not_by_type(self):
        # All puts by option type, but mostly sold -> net bullish.
        agg = aggregate([sold("put", premium=3e6, strike=90.0),
                         bought("put", premium=1e6, strike=85.0)])
        self.assertEqual(agg["put_premium"], 4e6)
        self.assertEqual(agg["call_premium"], 0.0)
        self.assertEqual(agg["bullish_premium"], 3e6)
        self.assertEqual(agg["bearish_premium"], 1e6)
        self.assertEqual(agg["direction"], "long")

    def test_synthetic_long_from_bought_calls_and_sold_puts(self):
        """Both legs are bullish even though one is a call and one a put."""
        agg = aggregate([bought("call", premium=1.5e6),
                         sold("put", premium=1.5e6, strike=90.0)])
        self.assertEqual(agg["bullish_premium"], 3e6)
        self.assertEqual(agg["bearish_premium"], 0.0)
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["coherence"], 1.0, places=6,
                               msg="one-sided by intent, not by option type")

    # -- mid-market fallback ----------------------------------------------
    def test_mid_market_premium_falls_back_to_option_type(self):
        agg = aggregate([make_alert(side="call", premium=2e6,
                                    ask_prem=0.0, bid_prem=0.0)])
        self.assertEqual(agg["unsided_premium"], 2e6)
        self.assertEqual(agg["bullish_premium"], 2e6)
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["unsided_share"], 1.0, places=6)

    def test_partial_mid_market_is_split_correctly(self):
        # 2M premium, only 1.2M attributable to a side (0.8M printed mid).
        agg = aggregate([make_alert(side="put", premium=2e6, strike=90.0,
                                    ask_prem=0.2e6, bid_prem=1.0e6)])
        self.assertAlmostEqual(agg["unsided_premium"], 0.8e6, places=3)
        # bid-side puts (1.0M) bullish; ask-side puts (0.2M) + mid (0.8M) bearish
        self.assertAlmostEqual(agg["bullish_premium"], 1.0e6, places=3)
        self.assertAlmostEqual(agg["bearish_premium"], 1.0e6, places=3)
        self.assertAlmostEqual(agg["unsided_share"], 0.4, places=6)

    def test_fully_sided_flow_reports_no_unsided_share(self):
        agg = aggregate([bought("call")])
        self.assertEqual(agg["unsided_premium"], 0.0)
        self.assertEqual(agg["unsided_share"], 0.0)

    # -- coherence now measures intent, not option type --------------------
    def test_coherence_uses_bullish_vs_bearish(self):
        # Bought calls + sold calls = genuinely two-way despite one type.
        agg = aggregate([bought("call", premium=2e6),
                         sold("call", premium=2e6, strike=120.0)])
        self.assertAlmostEqual(agg["coherence"], 0.0, places=6)

    # -- the corrected label reaches the gates and the technical rules -----
    def test_corrected_direction_reaches_the_trend_veto(self):
        """Sold puts in a downtrend must now be vetoed as a LONG."""
        downtrend = ind.snapshot(make_candles(300, drift=-0.0015))
        agg = aggregate([sold("put", premium=3e6, strike=90.0)])
        reason = check_gates(agg, downtrend, today=TODAY)
        self.assertIn("bullish flow but price below 200 EMA", reason)

    def test_corrected_direction_passes_the_veto_in_an_uptrend(self):
        uptrend = ind.snapshot(make_candles(300, drift=0.0015))
        agg = aggregate([sold("put", premium=3e6, strike=90.0)])
        self.assertEqual(check_gates(agg, uptrend, today=TODAY), "")

    def test_sold_calls_are_vetoed_in_an_uptrend(self):
        uptrend = ind.snapshot(make_candles(300, drift=0.0015))
        agg = aggregate([sold("call", premium=3e6)])
        self.assertIn("bearish flow but price above 200 EMA",
                      check_gates(agg, uptrend, today=TODAY))

    def test_corrected_direction_drives_mirrored_technical_rules(self):
        """The uptrend chart must be graded long-side for a sold-put name."""
        uptrend = ind.snapshot(make_candles(300, drift=0.0015))
        agg = aggregate([sold("put", premium=3e6, strike=90.0)])
        long_pts = score_technical(uptrend, agg["direction"])[0]
        short_pts = score_technical(uptrend, "short")[0]
        self.assertEqual(agg["direction"], "long")
        self.assertGreater(long_pts, short_pts)

    def test_corrected_direction_drives_darkpool_alignment(self):
        """Accumulation blocks must confirm a sold-put (bullish) name."""
        acc = fl.aggregate_darkpool(
            [{"premium": 5e6, "size": 100_000, "price": 100.9,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5, 5_000_000)
        agg = aggregate([sold("put", premium=3e6, strike=90.0)])
        pts, notes = score_darkpool(acc, agg["direction"])
        self.assertGreater(pts, 0.0)
        self.assertTrue(any("accumulation" in n for n in notes))

    def test_end_to_end_sold_put_is_scored_as_a_long(self):
        uptrend = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        agg = aggregate([sold("put", premium=2.5e6, strike=90.0, dte=35),
                         sold("put", premium=1.5e6, strike=85.0, dte=45, day_offset=1)])
        c = build_candidate(agg, uptrend, {"has_data": False}, today=TODAY)
        self.assertEqual(c.direction, "long")
        self.assertEqual(c.rejected, "", f"unexpected rejection: {c.rejected}")
        self.assertLess(c.stop, c.entry, "long stop must sit below entry")
        self.assertGreater(c.target1, c.entry)

    def test_high_mid_market_share_is_flagged_not_hidden(self):
        uptrend = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        agg = aggregate([make_alert(side="call", premium=3e6,
                                    ask_prem=0.0, bid_prem=0.0)])
        c = build_candidate(agg, uptrend, {"has_data": False}, today=TODAY)
        self.assertTrue(any("direction inferred" in f for f in c.flags),
                        f"expected a mid-market flag, got {c.flags}")


# ---------------------------------------------------------------------------
# Direction-relative aggression
# ---------------------------------------------------------------------------
class TestAggression(unittest.TestCase):
    """Aggression measures same-direction premium that crossed the spread.

        long  -> ask-side calls + bid-side puts
        short -> bid-side calls + ask-side puts

    So a bullish sold-put book and a bearish sold-call book score identically,
    which the old ask-side-only definition could not do.
    """

    # -- all four expressions are fully aggressive in their own direction --
    def test_bought_calls_are_aggressive_for_a_long(self):
        agg = aggregate([bought("call")])
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6)

    def test_sold_puts_are_aggressive_for_a_long(self):
        """The fix: hitting the bid on puts is how you express a long."""
        agg = aggregate([sold("put", strike=90.0)])
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6)

    def test_bought_puts_are_aggressive_for_a_short(self):
        agg = aggregate([bought("put", strike=90.0)])
        self.assertEqual(agg["direction"], "short")
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6)

    def test_sold_calls_are_aggressive_for_a_short(self):
        """The fix: hitting the bid on calls is how you express a short."""
        agg = aggregate([sold("call")])
        self.assertEqual(agg["direction"], "short")
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6)

    # -- the symmetry this change exists to deliver ------------------------
    def test_sold_put_long_and_sold_call_short_are_symmetric(self):
        long_book = aggregate([sold("put", premium=4e6, strike=90.0, dte=35)])
        short_book = aggregate([sold("call", premium=4e6, strike=110.0, dte=35)])

        self.assertEqual(long_book["direction"], "long")
        self.assertEqual(short_book["direction"], "short")
        self.assertAlmostEqual(long_book["aggression"], short_book["aggression"], places=6)
        self.assertAlmostEqual(long_book["coherence"], short_book["coherence"], places=6)

        long_pts, _ = score_flow(long_book)
        short_pts, _ = score_flow(short_book)
        self.assertAlmostEqual(long_pts, short_pts, places=6,
                               msg="identical books of opposite sign must score the same")

    def test_bought_call_long_and_bought_put_short_are_symmetric(self):
        long_book = aggregate([bought("call", premium=4e6, dte=35)])
        short_book = aggregate([bought("put", premium=4e6, strike=90.0, dte=35)])
        self.assertAlmostEqual(score_flow(long_book)[0], score_flow(short_book)[0],
                               places=6)

    def test_short_books_are_no_longer_structurally_penalised(self):
        """Under the old rule a sold-call short scored 0 aggression by design."""
        short_book = aggregate([sold("call", premium=4e6)])
        old_style = 0.0   # ask_prem / (ask_prem + bid_prem) with ask_prem == 0
        self.assertAlmostEqual(old_style, 0.0)
        self.assertAlmostEqual(short_book["aggression"], 1.0, places=6)

    # -- opposing premium is coherence's job, not aggression's -------------
    def test_opposing_premium_does_not_dilute_aggression(self):
        """A two-way book still crossed the spread on the side that won."""
        agg = aggregate([sold("put", premium=3e6, strike=90.0),
                         bought("put", premium=1e6, strike=85.0)])
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6,
                               msg="all 3M of the bullish premium crossed")
        self.assertLess(agg["coherence"], 1.0,
                        msg="the opposing 1M shows up in coherence instead")

    def test_aggression_and_coherence_are_not_the_same_measure(self):
        a = aggregate([bought("call", premium=2e6), sold("call", premium=1e6, strike=120.0)])
        b = aggregate([bought("call", premium=2e6),
                       make_alert(side="call", premium=1e6, strike=120.0,
                                  ask_prem=0.0, bid_prem=0.0)])
        # Same total premium, different composition: b's extra 1M printed mid.
        self.assertAlmostEqual(a["aggression"], 1.0, places=6)
        self.assertAlmostEqual(b["aggression"], 2.0 / 3.0, places=6)
        self.assertGreater(b["coherence"], a["coherence"])

    # -- mid-market handling preserved -------------------------------------
    def test_mid_market_premium_is_not_aggressive(self):
        agg = aggregate([make_alert(side="call", premium=2e6,
                                    ask_prem=0.0, bid_prem=0.0)])
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["unsided_share"], 1.0, places=6)
        self.assertAlmostEqual(agg["aggression"], 0.0, places=6,
                               msg="expresses a view, but nobody paid up for it")

    def test_half_crossed_half_mid_scores_one_half(self):
        agg = aggregate([bought("call", premium=2e6),
                         make_alert(side="call", premium=2e6, strike=120.0,
                                    ask_prem=0.0, bid_prem=0.0)])
        self.assertAlmostEqual(agg["aggression"], 0.5, places=6)
        self.assertAlmostEqual(agg["passive_premium"], 2e6, places=3)

    def test_partial_mid_market_on_a_put_book(self):
        # 1.0M bid-side (bullish, crossed) + 0.2M ask-side (bearish, crossed)
        # + 0.8M mid (bearish by option-type fallback) -> direction long.
        agg = aggregate([make_alert(side="put", premium=2e6, strike=90.0,
                                    ask_prem=0.2e6, bid_prem=1.0e6)])
        self.assertEqual(agg["direction"], "long")
        self.assertAlmostEqual(agg["aggressive_premium"], 1.0e6, places=3)
        self.assertAlmostEqual(agg["aggression"], 1.0, places=6,
                               msg="all bullish premium here crossed the spread")

    # -- the rest of the pipeline is untouched -----------------------------
    def test_other_flow_components_are_unchanged(self):
        agg = aggregate([bought("call", premium=2e6, strike=110.0, dte=35),
                         bought("call", premium=1e6, strike=115.0, dte=45, day_offset=1)])
        self.assertAlmostEqual(agg["total_premium"], 3e6, places=3)
        self.assertAlmostEqual(agg["sweep_share"], 1.0, places=6)
        self.assertAlmostEqual(agg["opening_share"], 1.0, places=6)
        self.assertEqual(agg["n_alerts"], 2)
        self.assertEqual(agg["n_strikes"], 2)
        self.assertEqual(agg["n_days"], 2)
        self.assertAlmostEqual(agg["avg_dte"], 40.0, places=6)

    def test_technical_and_darkpool_scores_are_unaffected(self):
        up = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        dp = fl.aggregate_darkpool(
            [{"premium": 8e6, "size": 80_000, "price": 100.85,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5, up["avg_volume"])
        crossed = aggregate([bought("call", premium=3e6, dte=35)])
        mid = aggregate([make_alert(side="call", premium=3e6, dte=35,
                                    ask_prem=0.0, bid_prem=0.0)])
        a = build_candidate(crossed, up, dp, today=TODAY)
        b = build_candidate(mid, up, dp, today=TODAY)
        # Aggression differs, so flow differs; everything else must not.
        self.assertGreater(a.flow_score, b.flow_score)
        self.assertAlmostEqual(a.tech_score, b.tech_score, places=6)
        self.assertAlmostEqual(a.dp_score, b.dp_score, places=6)
        self.assertAlmostEqual(a.liq_score, b.liq_score, places=6)

    def test_end_to_end_short_candidate_is_actionable(self):
        """A clean sold-call short in a downtrend must now reach a real tier."""
        down = ind.snapshot(make_candles(300, drift=-0.0015, last_volume_mult=1.6))
        agg = aggregate([sold("call", premium=2.5e6, strike=110.0, dte=35),
                         sold("call", premium=1.5e6, strike=115.0, dte=45, day_offset=1)])
        c = build_candidate(agg, down, {"has_data": False}, today=TODAY)
        self.assertEqual(c.direction, "short")
        self.assertEqual(c.rejected, "", f"unexpected rejection: {c.rejected}")
        self.assertIn(c.tier, ("A", "B", "C"))
        self.assertGreater(c.stop, c.entry, "short stop must sit above entry")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
class TestGates(unittest.TestCase):
    def setUp(self):
        self.tech = ind.snapshot(make_candles(300, drift=0.0015))
        self.flow = aggregate([make_alert(premium=1e6)])

    def test_clean_candidate_passes_all_gates(self):
        self.assertEqual(check_gates(self.flow, self.tech, today=TODAY), "")

    def test_ticker_premium_floor_blocks_small_flow(self):
        weak = aggregate([make_alert(premium=300_000)])
        self.assertIn("ticker premium", check_gates(weak, self.tech, today=TODAY))

    def test_bullish_flow_below_200_ema_is_rejected(self):
        downtrend = ind.snapshot(make_candles(300, drift=-0.0015))
        self.assertIn("below 200 EMA", check_gates(self.flow, downtrend, today=TODAY))

    def test_bearish_flow_above_200_ema_is_rejected(self):
        bear_flow = aggregate([make_alert(premium=1e6, side="put", strike=90.0)])
        self.assertIn("above 200 EMA", check_gates(bear_flow, self.tech, today=TODAY))

    def test_thin_liquidity_is_rejected(self):
        thin = dict(self.tech, avg_volume=100_000.0)
        self.assertIn("ADV", check_gates(self.flow, thin, today=TODAY))

    def test_earnings_inside_hold_window_is_rejected(self):
        er = aggregate([make_alert(premium=1e6,
                                   earnings=(TODAY + _dt.timedelta(days=5)).isoformat())])
        self.assertIn("earnings", check_gates(er, self.tech, today=TODAY))

    def test_earnings_override_re_admits_the_name(self):
        er = aggregate([make_alert(premium=1e6,
                                   earnings=(TODAY + _dt.timedelta(days=5)).isoformat())])
        self.assertEqual(check_gates(er, self.tech, allow_earnings=True, today=TODAY), "")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class TestScoring(unittest.TestCase):
    def test_flow_score_rises_with_premium(self):
        small, _ = score_flow(aggregate([make_alert(premium=800_000)]))
        big, _ = score_flow(aggregate([make_alert(premium=8_000_000)]))
        self.assertGreater(big, small)

    def test_flow_score_is_capped_at_the_weight(self):
        huge, _ = score_flow(aggregate([
            make_alert(premium=5e7, strike=110.0),
            make_alert(premium=5e7, strike=115.0, day_offset=1),
        ]))
        self.assertLessEqual(huge, 40.0)

    def test_two_way_flow_scores_below_one_sided_flow(self):
        one_sided, _ = score_flow(aggregate([make_alert(premium=2e6)]))
        two_way, _ = score_flow(aggregate([
            make_alert(premium=2e6),
            make_alert(premium=1.9e6, side="put", strike=90.0),
        ]))
        self.assertGreater(one_sided, two_way)

    def test_passive_flow_scores_below_aggressive_flow(self):
        """Crossing the spread beats printing mid-market.

        Note the contrast is crossed-vs-mid, not ask-vs-bid: since aggression
        became direction-relative, an ask-side call and a bid-side call are
        equally aggressive expressions of opposite views and score the same.
        """
        crossed, _ = score_flow(aggregate([
            make_alert(premium=2e6, ask_prem=2e6, bid_prem=0)]))
        mid_market, _ = score_flow(aggregate([
            make_alert(premium=2e6, ask_prem=0, bid_prem=0)]))
        self.assertGreater(crossed, mid_market)

    def test_ask_side_and_bid_side_books_of_equal_size_score_equally(self):
        """The asymmetry the direction-relative change removed."""
        bull, _ = score_flow(aggregate([make_alert(premium=2e6, ask_prem=2e6, bid_prem=0)]))
        bear, _ = score_flow(aggregate([make_alert(premium=2e6, ask_prem=0, bid_prem=2e6)]))
        self.assertAlmostEqual(bull, bear, places=6)

    def test_technical_score_rewards_a_clean_uptrend(self):
        up = score_technical(ind.snapshot(make_candles(300, drift=0.0015)), "long")[0]
        choppy = score_technical(ind.snapshot(make_candles(300, drift=0.0)), "long")[0]
        self.assertGreater(up, choppy)

    def test_technical_score_is_direction_aware(self):
        up = ind.snapshot(make_candles(300, drift=0.0015))
        self.assertGreater(score_technical(up, "long")[0], score_technical(up, "short")[0])

    def test_technical_score_penalises_extension(self):
        base = make_candles(300, drift=0.0015)
        extended = [dict(c) for c in base]
        # Jam the last close far above the 50 EMA.
        extended[-1]["close"] = extended[-1]["close"] * 1.35
        extended[-1]["high"] = extended[-1]["close"] * 1.01
        s_base = ind.snapshot(base)
        s_ext = ind.snapshot(extended)
        self.assertGreater(s_ext["atr_from_ema_fast"], s_base["atr_from_ema_fast"])

    def test_darkpool_lean_against_the_trade_scores_low(self):
        with_acc = fl.aggregate_darkpool(
            [{"premium": 5e6, "size": 100_000, "price": 100.9,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5, 5_000_000)
        against = fl.aggregate_darkpool(
            [{"premium": 5e6, "size": 100_000, "price": 100.1,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5, 5_000_000)
        self.assertGreater(score_darkpool(with_acc, "long")[0],
                           score_darkpool(against, "long")[0])

    def test_tier_boundaries(self):
        self.assertEqual(assign_tier(90.0), "A")
        self.assertEqual(assign_tier(78.0), "A")
        self.assertEqual(assign_tier(77.9), "B")
        self.assertEqual(assign_tier(65.0), "B")
        self.assertEqual(assign_tier(55.0), "C")
        self.assertEqual(assign_tier(54.9), "-")


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
class TestEndToEnd(unittest.TestCase):
    def _candidate(self, **kw):
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([
            make_alert(premium=2.5e6, strike=110.0, dte=35),
            make_alert(premium=1.5e6, strike=115.0, dte=45, day_offset=1),
        ])
        dp = fl.aggregate_darkpool(
            [{"premium": 8e6, "size": 80_000, "price": 100.85,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5,
            tech.get("avg_volume"))
        return build_candidate(flow, tech, dp, today=TODAY, **kw)

    def test_strong_setup_is_actionable_with_risk_levels(self):
        c = self._candidate()
        self.assertEqual(c.rejected, "", f"unexpected rejection: {c.rejected}")
        self.assertIn(c.tier, ("A", "B"))
        self.assertGreater(c.score, 65.0)
        self.assertIsNotNone(c.entry)
        self.assertIsNotNone(c.stop)
        self.assertLess(c.stop, c.entry, "long stop must sit below entry")
        self.assertGreater(c.target1, c.entry)
        self.assertGreater(c.target2, c.target1)
        self.assertGreater(c.shares, 0)

    def test_reward_risk_matches_the_configured_multiples(self):
        c = self._candidate()
        risk = c.entry - c.stop
        self.assertAlmostEqual((c.target1 - c.entry) / risk, 2.0, places=1)
        self.assertAlmostEqual((c.target2 - c.entry) / risk, 3.0, places=1)

    def test_position_size_respects_the_risk_budget(self):
        c = self._candidate()
        risk_per_share = c.entry - c.stop
        self.assertLessEqual(c.shares * risk_per_share, c.risk_dollars + risk_per_share)

    def test_short_side_levels_are_mirrored(self):
        tech = ind.snapshot(make_candles(300, drift=-0.0015))
        flow = aggregate([make_alert(premium=3e6, side="put", strike=90.0)])
        c = build_candidate(flow, tech, {"has_data": False}, today=TODAY)
        self.assertEqual(c.direction, "short")
        if not c.rejected:
            self.assertGreater(c.stop, c.entry, "short stop must sit above entry")
            self.assertLess(c.target1, c.entry)

    def test_missing_darkpool_only_costs_the_darkpool_points(self):
        strong = self._candidate()
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([
            make_alert(premium=2.5e6, strike=110.0, dte=35),
            make_alert(premium=1.5e6, strike=115.0, dte=45, day_offset=1),
        ])
        without = build_candidate(flow, tech, {"has_data": False}, today=TODAY)
        self.assertEqual(without.dp_score, 0.0)
        self.assertAlmostEqual(strong.score - without.score, strong.dp_score, places=1)
        self.assertIn("no dark-pool confirmation", without.flags)

    def test_rank_orders_by_score_and_flags_sector_concentration(self):
        cands = []
        for i in range(4):
            c = self._candidate()
            c.ticker = f"TK{i}"
            c.score = 90.0 - i          # descending
            cands.append(c)
        ranked = rank(cands)
        self.assertEqual([c.ticker for c in ranked], ["TK0", "TK1", "TK2", "TK3"])
        # All four share the Technology sector; the cap is 2.
        over = [c for c in ranked if any("over sector cap" in f for f in c.flags)]
        self.assertEqual(len(over), 2)

    def test_rejected_candidates_carry_a_reason_and_no_levels(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015))
        weak = aggregate([make_alert(premium=300_000)])
        c = build_candidate(weak, tech, {"has_data": False}, today=TODAY)
        self.assertTrue(c.rejected)
        self.assertIsNone(c.entry)




# ---------------------------------------------------------------------------
# Defined-risk options sizing (swing_options.py)
# ---------------------------------------------------------------------------
import swing_options as opt
from swing_config import OPTIONS


class TestOptionsSizing(unittest.TestCase):
    """The spread builder. Scoring is untouched — these only test sizing."""

    def _cand(self, tier="B", direction="long"):
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([bought("call", premium=2.5e6, strike=110.0, dte=35),
                          bought("call", premium=1.5e6, strike=115.0, dte=45, day_offset=1)])
        c = build_candidate(flow, tech, {"has_data": False}, today=TODAY)
        c.tier, c.direction = tier, direction
        return c

    # -- debit model -------------------------------------------------------
    def test_debit_fraction_limits(self):
        # An infinitely narrow ATM vertical costs half its width (delta 0.5).
        self.assertAlmostEqual(opt.debit_fraction(1e-6, 1.0), 0.5, places=3)
        # One expected move wide is ~0.32; two is ~0.20. Monotonically falling.
        self.assertAlmostEqual(opt.debit_fraction(1.0, 1.0), 0.3156, places=3)
        self.assertAlmostEqual(opt.debit_fraction(2.0, 1.0), 0.1952, places=3)
        self.assertGreater(opt.debit_fraction(0.5, 1.0), opt.debit_fraction(1.5, 1.0))

    def test_debit_fraction_never_exceeds_half_or_goes_negative(self):
        for w in (0.01, 0.1, 1.0, 5.0, 50.0):
            f = opt.debit_fraction(w, 1.0)
            self.assertGreater(f, 0.0)
            self.assertLessEqual(f, 0.5)

    def test_debit_fraction_handles_degenerate_inputs(self):
        self.assertEqual(opt.debit_fraction(0.0, 1.0), 0.5)
        self.assertEqual(opt.debit_fraction(1.0, 0.0), 0.5)

    # -- expiry ------------------------------------------------------------
    def test_expiry_is_a_third_friday_inside_the_window(self):
        d = opt.next_monthly_expiry(_dt.date(2026, 8, 9), 21, 45, 30)
        self.assertEqual(d, _dt.date(2026, 9, 18))
        self.assertEqual(d.weekday(), 4, "must be a Friday")
        self.assertTrue(21 <= (d - _dt.date(2026, 8, 9)).days <= 45)

    def test_expiry_window_can_be_empty(self):
        self.assertIsNone(opt.next_monthly_expiry(_dt.date(2026, 8, 9), 1, 2, 1))

    def test_strike_increments_scale_with_price(self):
        self.assertEqual(opt.strike_increment(20.0), 0.5)
        self.assertEqual(opt.strike_increment(48.0), 1.0)
        self.assertEqual(opt.strike_increment(133.0), 2.5)
        self.assertEqual(opt.strike_increment(427.0), 5.0)
        self.assertEqual(opt.strike_increment(1185.0), 20.0)

    # -- structure ---------------------------------------------------------
    def test_long_candidate_gets_a_bull_call_spread(self):
        p = opt.plan_spread(self._cand(direction="long"), TODAY, 100_000.0)
        self.assertEqual(p.structure, "bull call spread")
        self.assertTrue(p.tradeable, p.reason)
        self.assertGreater(p.short_strike, p.long_strike, "long: sell the higher strike")
        self.assertTrue(p.legs.endswith("C"))

    def test_short_candidate_gets_a_bear_put_spread(self):
        c = self._cand(direction="short")
        c.target1 = c.entry - (c.target1 - c.entry)     # mirror the target
        p = opt.plan_spread(c, TODAY, 100_000.0)
        self.assertEqual(p.structure, "bear put spread")
        self.assertLess(p.short_strike, p.long_strike, "short: sell the lower strike")
        self.assertTrue(p.legs.endswith("P"))

    def test_max_risk_never_exceeds_the_tier_budget(self):
        for acct in (5_000.0, 25_000.0, 100_000.0):
            for tier in ("A", "B"):
                c = self._cand(tier=tier)
                p = opt.plan_spread(c, TODAY, acct)
                if p.tradeable:
                    self.assertLessEqual(p.max_risk, opt.budget_for(tier, acct) + 1e-6)

    def test_max_risk_equals_debit_times_contracts_times_multiplier(self):
        p = opt.plan_spread(self._cand(), TODAY, 100_000.0)
        self.assertAlmostEqual(p.max_risk, p.debit * 100 * p.contracts, places=2)

    def test_max_profit_is_width_minus_debit(self):
        p = opt.plan_spread(self._cand(), TODAY, 100_000.0)
        self.assertAlmostEqual(p.max_profit, (p.width - p.debit) * 100 * p.contracts,
                               places=2)

    def test_concentration_mode_funds_b_tier_at_the_a_tier_rate(self):
        """One position at a time, so a B-tier idea is not under-funded."""
        self.assertTrue(OPTIONS["concentration_mode"])
        self.assertEqual(opt.budget_for("B", 5_000.0), opt.budget_for("A", 5_000.0))
        self.assertEqual(opt.budget_for("A", 5_000.0), 250.0)

    def test_concentration_never_funds_a_watchlist_tier(self):
        """C is zero by configuration and must stay zero, not get promoted."""
        self.assertEqual(opt.budget_for("C", 5_000.0), 0.0)

    def test_graded_mode_restores_the_tier_split(self):
        OPTIONS["concentration_mode"] = False
        try:
            self.assertGreater(opt.budget_for("A", 5_000.0),
                               opt.budget_for("B", 5_000.0))
            self.assertEqual(opt.budget_for("B", 5_000.0), 150.0)
            self.assertEqual(opt.budget_for("C", 5_000.0), 0.0)
        finally:
            OPTIONS["concentration_mode"] = True

    def test_only_one_position_is_ever_funded(self):
        cands = [self._cand() for _ in range(5)]
        plans = opt.plan_all(cands, TODAY, 100_000.0)
        self.assertEqual(sum(1 for p in plans if p.tradeable), 1)

    def test_banner_states_the_concentration_rule(self):
        text = opt.banner(5_000.0)
        self.assertIn("CONCENTRATION MODE", text)
        self.assertIn("ONE position", text)
        self.assertIn("$250", text)

    def test_c_tier_is_watchlist_only(self):
        p = opt.plan_spread(self._cand(tier="C"), TODAY, 5_000.0)
        self.assertFalse(p.tradeable)
        self.assertEqual(p.contracts, 0)
        self.assertIn("watchlist", p.reason)

    def test_unaffordable_spread_reports_a_reason_not_zero_contracts(self):
        """A tiny account must say why, not silently return nothing."""
        p = opt.plan_spread(self._cand(), TODAY, 200.0)
        self.assertFalse(p.tradeable)
        self.assertIn("budget", p.reason)
        self.assertEqual(p.contracts, 0)

    def test_width_narrows_when_the_preferred_width_is_unaffordable(self):
        big = opt.plan_spread(self._cand(), TODAY, 100_000.0)
        small = opt.plan_spread(self._cand(), TODAY, 3_000.0)
        if small.tradeable:
            self.assertLessEqual(small.width, big.width)
            if small.width < big.width:
                self.assertTrue(any("narrowed" in n for n in small.notes))

    # -- portfolio caps ----------------------------------------------------
    def test_position_cap_is_enforced(self):
        cands = [self._cand() for _ in range(6)]
        plans = opt.plan_all(cands, TODAY, 100_000.0)
        self.assertLessEqual(sum(1 for p in plans if p.tradeable),
                             OPTIONS["max_concurrent"])
        blocked = [p for p in plans if not p.tradeable and "cap" in p.reason]
        self.assertTrue(blocked)

    def test_total_risk_cap_is_enforced(self):
        cands = [self._cand() for _ in range(6)]
        plans = opt.plan_all(cands, TODAY, 100_000.0)
        total = sum(p.max_risk for p in plans if p.tradeable)
        self.assertLessEqual(total, 100_000.0 * OPTIONS["max_total_risk_pct"] + 1e-6)

    def test_blocked_positions_carry_zero_risk(self):
        cands = [self._cand() for _ in range(6)]
        for p in opt.plan_all(cands, TODAY, 100_000.0):
            if not p.tradeable:
                self.assertEqual(p.max_risk, 0.0)
                self.assertEqual(p.contracts, 0)
                self.assertTrue(p.reason)

    # -- the scoring engine must be untouched ------------------------------
    def test_planning_does_not_mutate_the_candidate(self):
        c = self._cand()
        before = (c.score, c.flow_score, c.dp_score, c.tech_score, c.liq_score,
                  c.tier, c.direction, c.entry, c.stop, c.target1, c.shares)
        opt.plan_spread(c, TODAY, 5_000.0)
        after = (c.score, c.flow_score, c.dp_score, c.tech_score, c.liq_score,
                 c.tier, c.direction, c.entry, c.stop, c.target1, c.shares)
        self.assertEqual(before, after)


class TestAffordabilityFilter(unittest.TestCase):
    """Price filter: reject names that cannot carry a worthwhile spread."""

    def _cand(self, price, atr, tier="B", direction="long"):
        """Synthetic candidate at a chosen price/ATR, scoring untouched."""
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([bought("call", premium=2.5e6, strike=110.0, dte=35)])
        c = build_candidate(flow, tech, {"has_data": False}, today=TODAY)
        c.tier, c.direction = tier, direction
        c.entry = price
        c.tech = dict(c.tech, atr=atr, last=price)
        r = 1.5 * atr
        c.stop = price - r if direction == "long" else price + r
        c.target1 = price + 2 * r if direction == "long" else price - 2 * r
        return c

    # -- the width floor ---------------------------------------------------
    def test_min_width_scales_with_expected_move(self):
        narrow = opt.min_practical_width(10.0, 1.0, target_width=100.0)
        wide = opt.min_practical_width(50.0, 1.0, target_width=100.0)
        self.assertGreater(wide, narrow, "more volatile name needs more width")

    def test_min_width_never_exceeds_the_2r_target(self):
        w = opt.min_practical_width(100.0, 1.0, target_width=4.0)
        self.assertLessEqual(w, 4.0, "never demand more width than the thesis")

    def test_min_width_is_at_least_one_increment(self):
        self.assertGreaterEqual(opt.min_practical_width(0.01, 2.5, 100.0), 2.5)

    def test_width_floor_delivers_the_configured_reward_risk(self):
        em, inc = 20.0, 0.5
        w = opt.min_practical_width(em, inc, target_width=1e9)
        frac = opt.debit_fraction(w, em)
        rr = (1 - frac) / frac
        self.assertGreaterEqual(rr, OPTIONS["min_reward_risk"] - 0.15)

    # -- the filter itself -------------------------------------------------
    def test_expensive_underlying_is_filtered_on_a_small_account(self):
        p = opt.plan_spread(self._cand(1200.0, 42.0), TODAY, 5_000.0)
        self.assertFalse(p.tradeable)
        self.assertEqual(p.status, "unaffordable")
        self.assertIn("needs $", p.reason)

    def test_cheap_low_volatility_underlying_survives(self):
        p = opt.plan_spread(self._cand(25.0, 0.6), TODAY, 5_000.0)
        self.assertTrue(p.tradeable, p.reason)
        self.assertEqual(p.status, "planned")

    def test_same_name_becomes_affordable_on_a_bigger_account(self):
        small = opt.plan_spread(self._cand(1200.0, 42.0), TODAY, 5_000.0)
        big = opt.plan_spread(self._cand(1200.0, 42.0), TODAY, 500_000.0)
        self.assertFalse(small.tradeable)
        self.assertTrue(big.tradeable, big.reason)

    def test_filter_reports_the_shortfall_not_just_a_refusal(self):
        p = opt.plan_spread(self._cand(1200.0, 42.0), TODAY, 5_000.0)
        self.assertIsNotNone(p.required_budget)
        self.assertIsNotNone(p.min_width)
        self.assertGreater(p.required_budget, p.budget,
                           "required must exceed budget for a rejection")

    def test_surviving_spreads_meet_the_reward_risk_floor(self):
        """The whole point: no more 1.03:1 structures sneaking through."""
        for price, atr in ((25.0, 0.6), (48.0, 1.2), (90.0, 2.0), (140.0, 3.0)):
            for acct in (5_000.0, 25_000.0, 100_000.0):
                p = opt.plan_spread(self._cand(price, atr), TODAY, acct)
                if p.tradeable:
                    self.assertGreaterEqual(
                        p.reward_risk, OPTIONS["min_reward_risk"] - 0.15,
                        f"{price}/{atr}/{acct} gave R:R {p.reward_risk}")

    def test_no_spread_narrower_than_the_floor_is_ever_built(self):
        for price, atr in ((25.0, 0.6), (48.0, 1.2), (140.0, 3.0)):
            p = opt.plan_spread(self._cand(price, atr), TODAY, 8_000.0)
            if p.tradeable:
                self.assertGreaterEqual(p.width, p.min_width - 1e-9)

    def test_filtered_names_carry_no_risk(self):
        p = opt.plan_spread(self._cand(1200.0, 42.0), TODAY, 5_000.0)
        self.assertEqual(p.contracts, 0)
        self.assertEqual(p.max_risk, 0.0)

    # -- reporting ---------------------------------------------------------
    def test_render_labels_filtered_names_visibly(self):
        cands = [self._cand(1200.0, 42.0), self._cand(25.0, 0.6)]
        plans = opt.plan_all(cands, TODAY, 5_000.0)
        text = opt.render(cands, plans)
        self.assertIn("FILTERED OUT", text)
        self.assertIn("FILTERED (price)", text)

    def test_render_handles_nothing_being_tradeable(self):
        cands = [self._cand(1200.0, 42.0)]
        plans = opt.plan_all(cands, TODAY, 5_000.0)
        text = opt.render(cands, plans)
        self.assertIn("no candidate could carry", text)

    # -- scoring untouched -------------------------------------------------
    def test_filtering_does_not_change_the_score(self):
        c = self._cand(1200.0, 42.0)
        before = (c.score, c.tier, c.flow_score, c.dp_score, c.tech_score)
        opt.plan_spread(c, TODAY, 5_000.0)
        self.assertEqual(before, (c.score, c.tier, c.flow_score, c.dp_score,
                                  c.tech_score))


class TestOptionsUniverse(unittest.TestCase):
    """The small-account screen. Policy, not scoring."""

    def setUp(self):
        UNIVERSE["options_max_atr_dollars"] = _CEILINGS["options_max_atr_dollars"]
        UNIVERSE["options_max_price"] = _CEILINGS["options_max_price"]

    def tearDown(self):
        UNIVERSE["options_max_atr_dollars"] = None
        UNIVERSE["options_max_price"] = None

    def test_configured_ceilings(self):
        self.assertEqual(UNIVERSE["options_max_atr_dollars"], 4.00)
        self.assertEqual(UNIVERSE["options_max_price"], 150.0)

    def test_high_atr_name_is_screened_out(self):
        reason = check_options_universe({}, {"last": 90.0, "atr": 10.72})
        self.assertIn("ATR", reason)
        self.assertIn("too wide", reason)

    def test_high_priced_name_is_screened_out(self):
        reason = check_options_universe({}, {"last": 1185.71, "atr": 42.27})
        self.assertIn("price ceiling", reason)

    def test_cheap_low_atr_name_passes(self):
        self.assertEqual(check_options_universe({}, {"last": 48.42, "atr": 3.69}), "")

    def test_price_ok_but_atr_too_high_is_rejected(self):
        """Both ceilings bind independently."""
        reason = check_options_universe({}, {"last": 88.58, "atr": 10.72})
        self.assertTrue(reason)
        self.assertIn("ATR", reason)

    def test_screen_falls_back_to_flow_price_when_no_bars(self):
        reason = check_options_universe({"underlying_price": 1185.0}, {})
        self.assertIn("price ceiling", reason)

    def test_screen_is_a_noop_when_ceilings_are_disabled(self):
        UNIVERSE["options_max_atr_dollars"] = None
        UNIVERSE["options_max_price"] = None
        self.assertEqual(check_options_universe({}, {"last": 5000.0, "atr": 99.0}), "")

    def test_missing_data_does_not_trip_the_screen(self):
        self.assertEqual(check_options_universe({}, {}), "")

    def test_ceiling_admits_what_the_budget_can_realistically_reach(self):
        """At the ceiling a spread is reachable when the 2R cap binds."""
        import swing_options as so
        atr = UNIVERSE["options_max_atr_dollars"]
        em = atr * (39 * 252 / 365) ** 0.5
        # Tight-stop case: 2R about 1 ATR wide.
        w = atr
        required = w * so.debit_fraction(w, em) * 100
        self.assertLess(required, so.budget_for("A", 5_000.0),
                        "ceiling should leave a reachable case")

    def test_gate_rejects_screened_names_before_scoring(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015))
        tech = dict(tech, last=1185.0, atr=42.0)
        flow = aggregate([bought("call", premium=3e6)])
        reason = check_gates(flow, tech, today=TODAY)
        self.assertIn("ceiling", reason)

    def test_screened_candidate_has_no_levels_and_states_why(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015))
        tech = dict(tech, last=1185.0, atr=42.0)
        flow = aggregate([bought("call", premium=3e6)])
        c = build_candidate(flow, tech, {"has_data": False}, today=TODAY)
        self.assertTrue(c.rejected)
        self.assertIn("ceiling", c.rejected)
        self.assertIsNone(c.entry)
        self.assertEqual(c.score, 0.0)


class TestOpposingDarkPoolPenalty(unittest.TestCase):
    """Contradicting blocks subtract; absent blocks stay neutral."""

    def _dp(self, price, bid=100.0, ask=101.0, n=5, prem=5e6, size=100_000):
        return fl.aggregate_darkpool(
            [{"premium": prem, "size": size, "price": price,
              "nbbo_bid": bid, "nbbo_ask": ask}] * n, 5_000_000)

    def test_opposing_lean_now_subtracts(self):
        """Was 0 (neutral); must now be a real penalty."""
        against = self._dp(100.1)              # near the bid = distribution
        pts, notes = score_darkpool(against, "long")
        self.assertLess(pts, 7.0, "footprint alone would have scored 7")
        self.assertTrue(any("OPPOSES" in n for n in notes))

    def test_penalty_is_proportional_to_the_opposition(self):
        mild = score_darkpool(self._dp(100.4), "long")[0]     # slightly below mid
        severe = score_darkpool(self._dp(100.0), "long")[0]   # at the bid
        self.assertGreater(mild, severe)

    def test_penalty_is_symmetric_with_the_reward(self):
        """+/-5 either way: the lean term mirrors."""
        acc = self._dp(101.0)   # at the ask
        dis = self._dp(100.0)   # at the bid
        long_acc = score_darkpool(acc, "long")[0]
        long_dis = score_darkpool(dis, "long")[0]
        self.assertAlmostEqual(long_acc - long_dis, 10.0, places=1)

    def test_aligned_lean_is_unaffected_by_the_change(self):
        """Accumulation into a long still earns the full footprint + lean."""
        pts, notes = score_darkpool(self._dp(100.9), "long")
        self.assertGreater(pts, 7.0)
        self.assertTrue(any("accumulation" in n for n in notes))

    def test_neutral_lean_is_unaffected(self):
        """A print exactly at the mid neither earns nor costs."""
        mid = self._dp(100.5)
        self.assertAlmostEqual(mid["lean"], 0.0, places=6)
        pts, _ = score_darkpool(mid, "long")
        self.assertAlmostEqual(pts, 7.0, places=1)

    def test_absent_data_is_still_zero_not_negative(self):
        """Absence must never be confused with contradiction."""
        pts, notes = score_darkpool({"has_data": False, "n_blocks": 0}, "long")
        self.assertEqual(pts, 0.0)
        self.assertIn("no dark-pool blocks", notes)

    def test_contradicted_ranks_below_unconfirmed(self):
        """The whole point of allowing a negative component."""
        contradicted = score_darkpool(
            self._dp(100.0, prem=2e6, size=10_000), "long")[0]
        unconfirmed = score_darkpool({"has_data": False, "n_blocks": 0}, "long")[0]
        self.assertLess(contradicted, unconfirmed)

    def test_penalty_is_not_a_veto(self):
        """Strong footprint + big block survives a fully opposed lean."""
        strong = fl.aggregate_darkpool(
            [{"premium": 5e7, "size": 500_000, "price": 100.0,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}] * 5, 5_000_000)
        pts, _ = score_darkpool(strong, "long")
        self.assertGreater(pts, 0.0, "footprint and block still count")

    def test_component_floor_is_minus_five(self):
        weak = fl.aggregate_darkpool(
            [{"premium": 1.1e6, "size": 100, "price": 100.0,
              "nbbo_bid": 100.0, "nbbo_ask": 101.0}], 5_000_000)
        pts, _ = score_darkpool(weak, "long")
        self.assertGreaterEqual(pts, -5.0)

    def test_short_side_opposition_is_mirrored(self):
        """Accumulation opposes a SHORT."""
        acc = self._dp(100.9)
        pts, notes = score_darkpool(acc, "short")
        self.assertLess(pts, 7.0)
        self.assertTrue(any("accumulating against bearish flow" in n for n in notes))

    def test_candidate_carries_an_opposing_flag(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([bought("call", premium=3e6, dte=35)])
        c = build_candidate(flow, tech, self._dp(100.0), today=TODAY)
        self.assertTrue(any("opposing" in f for f in c.flags),
                        f"expected an opposing flag, got {c.flags}")

    def test_aligned_candidate_has_no_opposing_flag(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015, last_volume_mult=1.6))
        flow = aggregate([bought("call", premium=3e6, dte=35)])
        c = build_candidate(flow, tech, self._dp(100.9), today=TODAY)
        self.assertFalse(any("opposing" in f for f in c.flags))


class TestMinimumHistoryGate(unittest.TestCase):
    """Names without enough bars to trust the 200 EMA never reach scoring."""

    def _flow(self):
        return aggregate([bought("call", premium=3e6, dte=35)])

    def test_configured_floor(self):
        self.assertEqual(TECHNICAL["min_bars_for_trend"], 250)

    def test_short_history_is_rejected(self):
        tech = ind.snapshot(make_candles(60, drift=0.0015))
        reason = check_gates(self._flow(), tech, today=TODAY)
        self.assertIn("insufficient history", reason)

    def test_rejection_names_the_shortfall(self):
        tech = ind.snapshot(make_candles(60, drift=0.0015))
        reason = check_gates(self._flow(), tech, today=TODAY)
        self.assertIn("60 bars", reason)
        self.assertIn("250", reason)

    def test_just_below_the_floor_is_rejected(self):
        tech = ind.snapshot(make_candles(249, drift=0.0015))
        self.assertIn("insufficient history",
                      check_gates(self._flow(), tech, today=TODAY))

    def test_at_the_floor_is_accepted(self):
        tech = ind.snapshot(make_candles(250, drift=0.0015))
        self.assertEqual(check_gates(self._flow(), tech, today=TODAY), "")

    def test_ample_history_is_accepted(self):
        tech = ind.snapshot(make_candles(300, drift=0.0015))
        self.assertEqual(check_gates(self._flow(), tech, today=TODAY), "")

    def test_missing_200_ema_is_rejected_even_if_bars_unreported(self):
        """The hole this closes: the trend veto silently skips without an EMA."""
        tech = {"last": 100.0, "atr": 2.0, "avg_volume": 5e6, "ema_slow": None}
        reason = check_gates(self._flow(), tech, today=TODAY)
        self.assertIn("insufficient history", reason)
        self.assertIn("trend cannot be verified", reason)

    def test_gate_runs_before_scoring(self):
        tech = ind.snapshot(make_candles(60, drift=0.0015))
        c = build_candidate(self._flow(), tech, {"has_data": False}, today=TODAY)
        self.assertIn("insufficient history", c.rejected)
        self.assertEqual(c.score, 0.0)
        self.assertEqual(c.flow_score, 0.0)
        self.assertIsNone(c.entry)

    def test_short_history_cannot_slip_past_the_trend_veto(self):
        """A 60-bar downtrend with bullish flow must be rejected for history,
        not accidentally admitted because the 200 EMA was unavailable."""
        tech = ind.snapshot(make_candles(60, drift=-0.0015))
        self.assertIsNone(tech["ema_slow"])
        reason = check_gates(self._flow(), tech, today=TODAY)
        self.assertIn("insufficient history", reason)

    def test_empty_tech_is_rejected_for_missing_history(self):
        """Regression: the flow payload carries its own underlying_price, so
        an empty tech dict used to satisfy the price gate and then skip the
        trend veto — priced by the alert, trend-checked by nothing."""
        reason = check_gates(self._flow(), {}, today=TODAY)
        self.assertIn("insufficient history", reason)

    def test_no_chart_data_never_reaches_scoring(self):
        c = build_candidate(self._flow(), {}, {"has_data": False}, today=TODAY)
        self.assertTrue(c.rejected)
        self.assertEqual(c.score, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
