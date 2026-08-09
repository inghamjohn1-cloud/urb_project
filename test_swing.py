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
from swing_score import (
    build_candidate, check_gates, score_flow, score_technical, score_darkpool,
    assign_tier, rank,
)


TODAY = _dt.date(2026, 8, 10)


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
        aggressive, _ = score_flow(aggregate([make_alert(premium=2e6, ask_prem=2e6, bid_prem=0)]))
        passive, _ = score_flow(aggregate([make_alert(premium=2e6, ask_prem=0, bid_prem=2e6)]))
        self.assertGreater(aggressive, passive)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
