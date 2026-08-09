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

    # Affordability pre-filter results — populated even when rejected, so the
    # report can show the shortfall instead of a bare "no".
    status: str = "planned"         # planned | unaffordable | capped | no_size
    min_width: float | None = None      # narrowest width that still pays
    required_budget: float | None = None  # what one contract of it would cost

    @property
    def legs(self) -> str:
        if self.long_strike is None or self.short_strike is None:
            return "-"
        kind = "C" if self.direction == "long" else "P"
        return f"{self.long_strike:g}/{self.short_strike:g}{kind}"


def budget_for(tier: str, account_size: float = ACCOUNT_SIZE) -> float:
    return account_size * OPTIONS["risk_pct_by_tier"].get(tier, 0.0)


def min_practical_width(expected_move: float, increment: float,
                        target_width: float | None = None) -> float:
    """Narrowest width that still produces an acceptable reward:risk.

    Narrowing a vertical toward the money drives debit/width up to 0.50, i.e.
    reward:risk down to 1.0 — affordable but not worth trading. The floor is
    set in expected moves so it scales with the name's volatility, rounded UP
    to a real strike, and never wider than the 2R width we actually want.
    """
    floor = OPTIONS["min_width_expected_moves"] * expected_move
    steps = max(1, math.ceil(floor / increment))
    width = steps * increment
    if target_width is not None and width > target_width:
        # Never demand more width than the trade thesis calls for.
        width = max(increment, round(target_width / increment) * increment)
    return width


def affordability(c, budget: float, expected_move: float, increment: float,
                  target_width: float) -> tuple[bool, float, float]:
    """Can this underlying carry a worthwhile spread inside the budget?

    Returns (affordable, min_width, required_budget_for_one_contract).

    This is the price filter: a $1,200 stock needs a wide spread to pay, and a
    wide spread costs more than a small account can risk — regardless of how
    good the setup scored.
    """
    mult = OPTIONS["contract_multiplier"]
    width = min_practical_width(expected_move, increment, target_width)
    debit = round(width * debit_fraction(width, expected_move), 2)
    required = debit * mult
    return required <= budget, width, required


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
        p.status = "no_size"
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

    # -- affordability pre-filter -----------------------------------------
    # Run BEFORE constructing anything: if this underlying's price means even
    # the narrowest worthwhile spread blows the budget, say so plainly rather
    # than degrading the structure until it technically fits.
    inc = strike_increment(c.entry)
    preferred = abs(c.target1 - c.entry)
    mult = OPTIONS["contract_multiplier"]

    ok, floor_width, required = affordability(c, p.budget, expected_move, inc,
                                              preferred)
    p.min_width, p.required_budget = floor_width, required
    if not ok:
        p.status = "unaffordable"
        p.reason = (f"needs ${required:,.0f} for a ${floor_width:g}-wide spread "
                    f"(min for R:R {OPTIONS['min_reward_risk']:.1f}), "
                    f"budget ${p.budget:,.0f}")
        return p

    # -- width: prefer the 2R target, narrow only down to the floor --------
    steps = max(1, round(preferred / inc))
    floor_steps = max(1, round(floor_width / inc))

    chosen = None
    for s in range(steps, floor_steps - 1, -1):
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
        # Should be unreachable given the pre-filter, but never size blind.
        p.status = "unaffordable"
        p.reason = (f"no width between ${floor_width:g} and ${steps * inc:g} "
                    f"fits a ${p.budget:,.0f} budget")
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
        p.notes.append(f"R:R {p.reward_risk:.2f} below the "
                       f"{OPTIONS['min_reward_risk']:.1f} floor")
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
            p.status = "capped"
            p.reason = f"over the {OPTIONS['max_concurrent']}-position cap"
            p.contracts, p.max_risk, p.max_profit = 0, 0.0, 0.0
            continue
        if running + p.max_risk > cap_total:
            p.tradeable = False
            p.status = "capped"
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
    tag = {"unaffordable": "FILTERED (price)", "capped": "FILTERED (cap)",
           "no_size": "WATCHLIST"}.get(p.status, "SKIPPED")
    return (f"{c.ticker:<6}{c.direction:<6}{c.tier:<5}{c.score:>6.1f}"
            f"{c.flow_score:>6.1f}{c.dp_score:>5.1f}{c.tech_score:>6.1f}"
            f"{c.liq_score:>5.1f}  {tag:<18}{p.reason}")


def render(candidates, plans) -> str:
    """Full report: tradeable spreads first, then everything filtered out."""
    out = [HEADER, "-" * len(HEADER)]
    live = [(c, p) for c, p in zip(candidates, plans) if p.tradeable]
    dead = [(c, p) for c, p in zip(candidates, plans) if not p.tradeable]

    for c, p in live:
        out.append(render_row(c, p))
        for n in p.notes:
            out.append(f"{'':>52}  ! {n}")
    if not live:
        out.append("  (no candidate could carry a worthwhile spread at this "
                   "account size)")

    if dead:
        out.append("")
        out.append("FILTERED OUT — scored fine, cannot be sized:")
        for c, p in dead:
            out.append(render_row(c, p))

    total = sum(p.max_risk for p in plans if p.tradeable)
    out.append("")
    out.append(f"open risk ${total:,.0f} across "
               f"{sum(1 for p in plans if p.tradeable)} position(s)")
    return "\n".join(out)
