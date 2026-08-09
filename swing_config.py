"""Tunable configuration for the single-name swing scanner.

Every threshold the scanner uses lives here so the rules stay auditable and
you can tighten/loosen the scan without touching the scoring code. Nothing in
this module does I/O — it is pure declaration.

Scope reminder: this scanner trades LIQUID U.S. SINGLE NAMES ONLY (common
stock + ADRs). Index and sector ETFs are handled by the separate rotation
strategy in scan.py / rotation.py and are excluded here on purpose.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Universe gates — a name must pass ALL of these to be scoreable at all.
#    These are hard filters, not score inputs: they define "liquid enough to
#    swing trade with defined risk".
# ---------------------------------------------------------------------------
UNIVERSE = {
    "issue_types": ["Common Stock", "ADR"],  # no ETFs, no index products
    "min_price": 10.0,             # sub-$10 names have poor option structure
    "max_price": 2000.0,           # avoids the handful of untradeable prints
    "min_marketcap": 2_000_000_000,        # $2B — keeps the tape orderly
    "min_avg_volume": 1_000_000,           # 30d ADV in shares
    "min_dollar_volume": 25_000_000,       # ADV * price — real liquidity test
    "min_option_open_interest": 5_000,     # ticker-level OI; thin chains fail
    "max_option_spread_pct": 0.15,         # (ask-bid)/mid on the alerted contract
}

# ---------------------------------------------------------------------------
# 2. Options-flow gates — what counts as "unusual activity worth a swing".
#    Premium target is $250k+ on a single print; we also accept a cluster of
#    smaller prints that aggregate to the cluster threshold, because repeated
#    hits are often a single order being worked in pieces.
# ---------------------------------------------------------------------------
FLOW = {
    "min_alert_premium": 250_000,       # single-alert premium floor
    "min_ticker_premium": 750_000,      # total across all alerts for the name
    "min_dte": 14,                      # below 14d is gamma/event trading
    "max_dte": 90,                      # above 90d the thesis is too slow
    "ideal_dte": (21, 60),              # no DTE penalty inside this band
    "min_volume_oi_ratio": 1.0,         # volume > OI hints at new positioning
    "min_aggression_pct": 0.55,         # share of same-direction premium that crossed
    "min_alerts_for_cluster": 2,        # distinct alerts to call it repeated
    "exclude_multi_leg": True,          # spreads muddy the directional read
    "max_alert_age_days": 3,            # act on fresh flow, not last week's

    # Rule names from the Unusual Whales flow-alert engine that carry the most
    # signal for multi-day swings. Value = bonus points (capped in scoring).
    "rule_bonus": {
        "SweepsFollowedByFloor": 5.0,   # retail-visible sweep + institutional floor
        "RepeatedHitsAscendingFill": 4.0,  # buyer lifting through the offer
        "RepeatedHits": 3.0,
        "FloorTradeLargeCap": 3.0,
        "FloorTradeMidCap": 3.0,
        "VolumeOverOi": 2.0,
        "RepeatedHitsDescendingFill": 1.0,  # weaker: seller-ish fill pattern
        "LowHistoricVolumeFloor": 2.0,
        "FloorTradeSmallCap": 1.0,
        "OtmEarningsFloor": 0.0,        # earnings-driven; excluded by default
    },
}

# ---------------------------------------------------------------------------
# 3. Dark-pool context. Off-exchange blocks confirm that someone is building a
#    position in the SHARES, not just renting leverage in the options.
#    Absence of dark-pool data is scored as NEUTRAL (0), never as a negative —
#    a quiet tape is not evidence of distribution.
# ---------------------------------------------------------------------------
DARKPOOL = {
    "lookback_days": 5,
    "min_block_premium": 1_000_000,     # a "block" worth counting
    "strong_block_premium": 10_000_000, # a single print that matters on its own
    "min_pct_of_adv": 0.02,             # 5d block volume >= 2% of ADV to score
    "strong_pct_of_adv": 0.10,
    "above_mid_is_accumulation": True,  # premium-weighted price vs NBBO mid
}

# ---------------------------------------------------------------------------
# 4. Technical confluence — all verifiable on a TradingView daily chart.
#    Long-side definitions; the scanner mirrors them for short candidates.
# ---------------------------------------------------------------------------
TECHNICAL = {
    "ema_fast": 50,
    "ema_slow": 200,
    "rsi_len": 14,
    "atr_len": 14,
    "vol_avg_len": 20,
    "rsi_constructive": (45, 70),    # healthy trend continuation band
    "rsi_recovering": (40, 45),      # pullback that is turning up
    "rsi_overbought": 75,            # chase risk — score it down, don't ban it
    "max_atr_from_ema_fast": 4.0,    # >4 ATR above EMA50 = extended
    "vol_surge_ratio": 1.3,          # today vs 20d average volume
    "near_high_pct": 0.10,           # within 10% of the 52-week high
    "breakout_lookback": 20,         # 20-day high/low structure break
    "history_days": 400,             # bars to request (need 200 EMA + buffer)
}

# ---------------------------------------------------------------------------
# 5. Scoring weights. Components are each computed on their own 0..max scale
#    then summed to a 0..100 composite.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "flow": 40.0,        # conviction in the options tape
    "darkpool": 15.0,    # share-side confirmation
    "technical": 35.0,   # chart agrees with the flow
    "liquidity": 10.0,   # can you actually get in and out
}

# Sub-component caps inside the 40-point flow bucket.
FLOW_POINTS = {
    "premium": 14.0,
    "aggression": 8.0,
    "structure": 6.0,     # sweep / floor / rule bonuses
    "opening": 6.0,       # opening position vs closing one out
    "coherence": 6.0,     # one-sided vs two-way flow
}

# Sub-component caps inside the 35-point technical bucket.
TECH_POINTS = {
    "trend": 12.0,
    "extension": 5.0,
    "rsi": 8.0,
    "volume": 6.0,
    "structure": 4.0,
}

# ---------------------------------------------------------------------------
# 6. Tiering and risk. Tiers drive whether a name is actionable, a watch, or
#    discarded. Risk numbers drive the printed entry/stop/target/size.
# ---------------------------------------------------------------------------
TIERS = [
    (78.0, "A"),   # actionable — full planned risk
    (65.0, "B"),   # actionable — half risk
    (55.0, "C"),   # watchlist only, needs another day of confirmation
]
MIN_SCORE = 55.0   # below this the candidate is dropped entirely

# ---------------------------------------------------------------------------
# >>> YOUR ACCOUNT SIZE LIVES ON THE NEXT LINE <<<
#
# This single number drives every "Sh" (share count) and "budget" figure the
# scanner prints. Entry, stop and target prices do NOT depend on it — only the
# size shown next to them. Edit this one line and nothing else; the RISK block
# below reads from it.
#
#     ACCOUNT_SIZE = 5_000.0        <- small account
#     ACCOUNT_SIZE = 250_000.0      <- larger account
ACCOUNT_SIZE = 5_000.0          # <-- YOUR REAL ACCOUNT SIZE GOES HERE
ACCOUNT_SIZE_IS_PLACEHOLDER = False  # <-- True while the line above is still a guess
# ---------------------------------------------------------------------------

RISK = {
    "account_size": ACCOUNT_SIZE,   # used only to size the printed suggestion
    "risk_per_trade_pct": 0.0075,   # 0.75% of account risked per A-tier idea
    "b_tier_risk_multiplier": 0.5,
    "stop_atr_multiple": 1.5,       # initial stop distance
    "target_r_multiples": (2.0, 3.0),
    "max_open_positions": 5,
    "max_per_sector": 2,
    "min_reward_risk": 2.0,         # skip anything that can't pay 2R
}

# ---------------------------------------------------------------------------
# 7. Defined-risk options sizing (swing_options.py).
#
#    An ALTERNATIVE to the share sizing in RISK above — nothing here feeds the
#    scoring engine. Scores, gates, tiers and direction are computed exactly as
#    before; this block only decides how a scored candidate is expressed as a
#    vertical debit spread.
#
#    Long candidate  -> bull call spread (buy lower strike, sell higher)
#    Short candidate -> bear put spread  (buy higher strike, sell lower)
#
#    Max loss is the net debit, known at entry, so the risk percentages are
#    higher than the stop-based share model: a stop can gap, a debit cannot.
# ---------------------------------------------------------------------------
OPTIONS = {
    "enabled": True,
    "structure": "vertical_debit",

    # Risk per idea as a fraction of ACCOUNT_SIZE. Max loss = net debit.
    "risk_pct_by_tier": {"A": 0.05, "B": 0.03, "C": 0.0},

    # CONCENTRATION MODE — one position at a time, funded at the A-tier rate
    # whatever the tier. A small account cannot spread 3% across several names
    # and still buy a spread wide enough to pay; pooling the risk into a single
    # idea is the only way the width floor is reachable. C-tier is unaffected:
    # zero stays zero, so a watchlist name never gets funded by concentration.
    # Set False to restore the graded A/B budgets above.
    "concentration_mode": True,
    "max_concurrent": 1,            # concentration: one open position
    "max_total_risk_pct": 0.05,     # with one position this equals the A-tier %

    # Expiration: nearest monthly inside this window (monthlies = liquidity).
    "target_dte": 30,
    "min_dte": 21,
    "max_dte": 45,

    # Width is anchored to the scanner's own 2R target so max profit lands
    # where the share model would take its first scale.
    "width_basis": "target1",
    "min_width_increments": 1,      # never narrower than one strike increment
    "trading_days_per_year": 252,

    # Quality floors for the constructed spread. These are GATES, not notes:
    # a spread narrow enough to be affordable but too narrow to pay is not a
    # trade, it is a fee. R:R = (1 - debit/width) / (debit/width), and
    # debit/width falls as width grows, so the floor below is really a floor
    # on width — expressed in expected moves so it scales with volatility.
    #
    #   0.50 expected moves -> debit ~40% of width -> R:R ~1.5
    #   0.25 expected moves -> debit ~45% of width -> R:R ~1.2
    #   0.00 (ATM, minimum) -> debit  50% of width -> R:R  1.0
    "min_reward_risk": 1.5,
    "min_width_expected_moves": 0.5,
    "max_debit_pct_of_width": 0.60, # above this you are paying too much
    "contract_multiplier": 100,

    # Chain liquidity rules — verify on the live chain before entering.
    "min_open_interest": 500,
    "max_bid_ask_pct": 0.10,
}

# Strike increments by underlying price — used to round the spread width to
# strikes that actually exist. Adjust if your broker's chain differs.
STRIKE_INCREMENTS = [
    (25.0, 0.5),
    (50.0, 1.0),
    (200.0, 2.5),
    (500.0, 5.0),
    (1000.0, 10.0),
    (float("inf"), 20.0),
]

# Earnings inside the hold window turn a swing into a binary event bet.
EARNINGS = {
    "block_if_within_days": 12,     # roughly the expected hold period
    "allow_override": True,         # --allow-earnings re-admits them, flagged
}

# Holding-period expectation the whole system is tuned around.
HOLD_PERIOD_DAYS = (3, 15)
