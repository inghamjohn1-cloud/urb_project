"""Express a scored swing candidate as a defined-risk vertical debit spread.

This module sits DOWNSTREAM of the scoring engine and never feeds back into it.
It reads a finished `Candidate` (ticker, direction, tier, entry, stop, target1,
ATR) and answers a single question: given a small account, what spread would
express this idea and how much can it lose?

    long candidate  -> bull call spread (buy lower strike, sell higher)
    short candidate -> bear put spread  (buy higher strike, sell lower)

Max loss is the net debit, fixed at entry. That is the whole point for a small
account: no gap risk beyond what you paid.

Debit is ESTIMATED, not quoted. For an at-the-money vertical the debit is the
average option delta across the width, which for a lognormal-ish underlying is

    debit/width = (EM/W) * [ a*N(-a) - phi(a) + phi(0) ],   a = W/EM

where EM is the expected move to expiry (ATR * sqrt(trading days)). The limits
behave correctly: W -> 0 gives 0.50 (ATM delta), W = 1 EM gives ~0.32, W = 2 EM
gives ~0.20. Always replace this with the live chain quote before trading.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from swing_config import (
    ACCOUNT_SIZE, OPTIONS, STRIKE_INCREMENTS, HOLD_PERIOD_DAYS,
)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


PHI0 = _norm_pdf(0.0)   # 0.39894...


def debit_fraction(width: float, expected_move: float) -> float:
    """Estimated debit as a fraction of width for an ATM vertical.

    Mean option delta across [K, K+W], which is what a vertical costs.
    Bounded to (0, 0.5]: you can never pay more than the width, and an
    infinitely narrow ATM spread costs half of it.
    """
    if width <= 0 or expected_move <= 0:
        return 0.5
    a = width / expected_move
    frac = (expected_move / width) * (a * _norm_cdf(-a) - _norm_pdf(a) + PHI0)
    return max(0.01, min(0.5, frac))


def strike_increment(price: float) -> float:
    """Realistic strike spacing for an underlying at this price."""
    for ceiling, inc in STRIKE_INCREMENTS:
        if price < ceiling:
            return inc
    return STRIKE_INCREMENTS[-1][1]


def next_monthly_expiry(today: _dt.date, min_dte: int, max_dte: int,
                        target_dte: int) -> _dt.date | None:
    """Third Friday closest to `target_dte` that falls inside the DTE window."""
    candidates: list[_dt.date] = []
    for months_ahead in range(0, 4):
        y = today.year + (today.month - 1 + months_ahead) // 12
        m = (today.month - 1 + months_ahead) % 12 + 1
        first = _dt.date(y, m, 1)
        # weekday(): Mon=0 .. Fri=4. Days until the first Friday, then +14.
        third_friday = first + _dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
        dte = (third_friday - today).days
        if min_dte <= dte <= max_dte:
            candidates.append(third_friday)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today).days - target_dte))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@dataclass
class OptionPlan:
    ticker: str
    direction: str
    tier: str
    structure: str = ""             # "bull call spread" / "bear put spread"
    expiry: _dt.date | None = None
    dte: int | None = None
    long_strike: float | None = None
    short_strike: float | None = None
    width: float | None = None
    debit: float | None = None      # per share, estimated
    debit_pct_of_width: float | None = None
    contracts: int = 0
    max_risk: float = 0.0           # dollars, = debit * 100 * contracts
    max_profit: float = 0.0
    reward_risk: float | None = None
    budget: float = 0.0
    tradeable: bool = False
    reason: str = ""                # why not tradeable, or how it was adjusted
    notes: list[str] = field(default_factory=list)

    @property
    def legs(self) -> str:
        if self.long_strike is None or self.short_strike is None:
            return "-"
        kind = "C" if self.direction == "long" else "P"
        return f"{self.long_strike:g}/{self.short_strike:g}{kind}"


def budget_for(tier: str, account_size: float = ACCOUNT_SIZE) -> float:
    return account_size * OPTIONS["risk_pct_by_tier"].get(tier, 0.0)


def plan_spread(c, today: _dt.date | None = None,
                account_size: float = ACCOUNT_SIZE) -> OptionPlan:
    """Build a vertical debit spread for one scored candidate.

    `c` is a swing_score.Candidate. Nothing on it is mutated.
    """
    today = today or _dt.date.today()
    p = OptionPlan(ticker=c.ticker, direction=c.direction, tier=c.tier)
    p.structure = ("bull call spread" if c.direction == "long"
                   else "bear put spread")
    p.budget = budget_for(c.tier, account_size)

    if p.budget <= 0:
        p.reason = f"{c.tier}-tier carries no size (watchlist only)"
        return p
    if c.entry is None or c.target1 is None or not c.tech.get("atr"):
        p.reason = "no entry/target/ATR — cannot construct a spread"
        return p

    # -- expiration --------------------------------------------------------
    exp = next_monthly_expiry(today, OPTIONS["min_dte"], OPTIONS["max_dte"],
                              OPTIONS["target_dte"])
    if exp is None:
        p.reason = (f"no monthly expiry between {OPTIONS['min_dte']} and "
                    f"{OPTIONS['max_dte']} DTE")
        return p
    p.expiry, p.dte = exp, (exp - today).days

    # -- expected move to expiry ------------------------------------------
    atr = c.tech["atr"]
    trading_days = max(1.0, p.dte * 252.0 / 365.0)
    expected_move = atr * math.sqrt(trading_days)

    # -- width: prefer the 2R target, narrow only if unaffordable ----------
    inc = strike_increment(c.entry)
    preferred = abs(c.target1 - c.entry)
    steps = max(OPTIONS["min_width_increments"], round(preferred / inc))
    mult = OPTIONS["contract_multiplier"]

    chosen = None
    for s in range(steps, OPTIONS["min_width_increments"] - 1, -1):
        w = s * inc
        frac = debit_fraction(w, expected_move)
        # Round to the cent BEFORE sizing — options quote in pennies, and the
        # printed debit has to reconcile with the printed risk exactly.
        debit = round(w * frac, 2)
        if debit <= 0:
            continue
        contracts = int(p.budget // (debit * mult))
        if contracts >= 1:
            chosen = (w, frac, debit, contracts, s == steps)
            break

    if chosen is None:
        # Even one contract of the narrowest spread costs more than the budget.
        w = OPTIONS["min_width_increments"] * inc
        debit = round(w * debit_fraction(w, expected_move), 2)
        p.width, p.debit = w, debit
        p.reason = (f"narrowest spread (${w:g} wide) costs ~${debit * mult:,.0f} "
                    f"vs ${p.budget:,.0f} budget")
        return p

    width, frac, debit, contracts, at_preferred = chosen

    # -- strikes -----------------------------------------------------------
    # Long leg at the money, short leg one width out in the trade's direction.
    long_strike = round(c.entry / inc) * inc
    short_strike = (long_strike + width if c.direction == "long"
                    else long_strike - width)

    p.long_strike, p.short_strike, p.width = long_strike, short_strike, width
    p.debit = debit
    p.debit_pct_of_width = frac
    p.contracts = contracts
    p.max_risk = round(debit * mult * contracts, 2)
    p.max_profit = round((width - debit) * mult * contracts, 2)
    p.reward_risk = round((width - debit) / debit, 2) if debit else None
    p.tradeable = True

    # -- advisory notes ----------------------------------------------------
    if not at_preferred:
        p.notes.append(f"narrowed from ${steps * inc:g} to fit the budget")
    if p.reward_risk is not None and p.reward_risk < OPTIONS["min_reward_risk"]:
        p.notes.append(f"R:R {p.reward_risk:.2f} below "
                       f"{OPTIONS['min_reward_risk']:.1f} — width too tight")
    if frac > OPTIONS["max_debit_pct_of_width"]:
        p.notes.append(f"debit {frac:.0%} of width — paying up")
    lo, hi = HOLD_PERIOD_DAYS
    if p.dte and p.dte < hi + 7:
        p.notes.append(f"{p.dte}d to expiry vs {hi}-session hold — little slack")
    return p


def plan_all(candidates, today: _dt.date | None = None,
             account_size: float = ACCOUNT_SIZE) -> list[OptionPlan]:
    """Plan every candidate, then apply the portfolio-level risk caps.

    Candidates are assumed already ranked best-first. Positions beyond
    `max_concurrent`, or that would breach `max_total_risk_pct`, are marked
    untradeable with the reason rather than silently dropped.
    """
    plans = [plan_spread(c, today, account_size) for c in candidates]

    cap_total = account_size * OPTIONS["max_total_risk_pct"]
    taken, running = 0, 0.0
    for p in plans:
        if not p.tradeable:
            continue
        if taken >= OPTIONS["max_concurrent"]:
            p.tradeable = False
            p.reason = f"over the {OPTIONS['max_concurrent']}-position cap"
            p.contracts, p.max_risk, p.max_profit = 0, 0.0, 0.0
            continue
        if running + p.max_risk > cap_total:
            p.tradeable = False
            p.reason = (f"would breach the {OPTIONS['max_total_risk_pct']:.0%} "
                        f"total-risk cap (${cap_total:,.0f})")
            p.contracts, p.max_risk, p.max_profit = 0, 0.0, 0.0
            continue
        taken += 1
        running += p.max_risk
    return plans


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
HEADER = (f"{'TKR':<6}{'Dir':<6}{'Tier':<5}{'Score':>6}{'Flow':>6}{'DP':>5}"
          f"{'Tech':>6}{'Liq':>5}  {'Expiry':<11}{'Spread':<14}{'W':>6}"
          f"{'Debit':>7}{'Cts':>5}{'Risk$':>8}{'Max$':>8}{'R:R':>6}")


def render_row(c, p: OptionPlan) -> str:
    if p.tradeable:
        return (f"{c.ticker:<6}{c.direction:<6}{c.tier:<5}{c.score:>6.1f}"
                f"{c.flow_score:>6.1f}{c.dp_score:>5.1f}{c.tech_score:>6.1f}"
                f"{c.liq_score:>5.1f}  {p.expiry.isoformat():<11}{p.legs:<14}"
                f"{p.width:>6g}{p.debit:>7.2f}{p.contracts:>5}"
                f"{p.max_risk:>8.0f}{p.max_profit:>8.0f}{p.reward_risk:>6.2f}")
    return (f"{c.ticker:<6}{c.direction:<6}{c.tier:<5}{c.score:>6.1f}"
            f"{c.flow_score:>6.1f}{c.dp_score:>5.1f}{c.tech_score:>6.1f}"
            f"{c.liq_score:>5.1f}  {'— ' + p.reason:<60}")
