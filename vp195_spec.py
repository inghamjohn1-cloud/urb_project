#!/usr/bin/env python3
"""The 195-minute POC/VAH/VAL plan, implemented to the numbered specification.

Section numbers in the code refer to the sections of the written rules, and
every exit is tagged with the rule that produced it (spec 8).

Unlike vp195.py (a different, earlier rule set that was tested and failed),
this engine keys everything off the REFERENCE BAR'S OWN footprint levels, and
requires three consecutive POC migrations before a name is eligible at all.

Two no-trade conditions in spec 2 cannot be evaluated from price data alone and
are reported as un-applied rather than silently skipped:
  * scheduled earnings (needs a calendar; see --earnings)
  * the Risk Desk "Extreme" volatility regime (external table)
Neither affects R-multiples through position sizing, since R is by definition
normalised by the entry-to-stop distance.

Usage:
    python vp195_spec.py --spec samples/cohort.txt
    python vp195_spec.py --spec samples/cohort.txt --log trades.csv --trades
"""

import argparse
import csv
import datetime as dt
import math
import os
import sys

import vp195 as V

TICK = 0.01
MIGRATIONS_REQUIRED = 3      # spec 3: "POC Up Migration x3"
TRIGGER_WINDOW = 3           # bars after the reference bar a trigger may appear
CHASE_WIDTHS = 1.0           # spec 3: entry within 1 value-area width of refPOC
MAX_STOP_WIDTHS = 2.0        # spec 4
SCALE_FRACTION = 0.5         # spec 5: first scale-out is half
STALL_BARS = 3               # spec 5: time stop
CLIMAX_VOL = 2.0             # spec 2
CLIMAX_RANGE = 2.0           # spec 2
MAX_CONCURRENT = 3           # spec 2, across the universe
EARNINGS_BLACKOUT = 2        # spec 2: bars before and after a release


# ------------------------------------------------------------ calendar

def bar_date(bar):
    return dt.date(*(int(x) for x in bar.key.split("|")[0].split("-")))


def is_opening_bar(bar):
    return bar.key.endswith("|0")


def third_friday(year, month):
    d = dt.date(year, month, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == month
               and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2]


def blackout_opens(bars):
    """Spec 2: the opening 195m bar of monthly opex and quarterly rebalance."""
    out = set()
    for b in bars:
        d = bar_date(b)
        if d == third_friday(d.year, d.month) and is_opening_bar(b):
            out.add(b.key)                       # monthly opex open
    return out


# ------------------------------------------------------- spec 1 primitives

def migrated_up(bars, i):
    return bars[i].poc > bars[i - 1].poc


def migrated_down(bars, i):
    return bars[i].poc < bars[i - 1].poc


def migration_run(bars, i, up, n=None):
    """Spec 1 + 3: n consecutive POC migrations ending at bar i.

    n defaults to the module setting, read at call time - binding it as a
    default argument would freeze it at import and silently ignore any later
    change, which makes the sensitivity sweep a no-op.
    """
    if n is None:
        n = MIGRATIONS_REQUIRED
    if i < n:
        return False
    test = migrated_up if up else migrated_down
    return all(test(bars, k) for k in range(i - n + 1, i + 1))


def failed_auction(bars, k, long):
    """Spec 1/6: pierced the prior value area edge, closed back inside it."""
    p = bars[k - 1]
    if long:
        return bars[k].h > p.vah and p.val <= bars[k].c <= p.vah
    return bars[k].l < p.val and p.val <= bars[k].c <= p.vah


def migration_reversal(bars, k, long):
    """Spec 6: a bar closes with its POC beyond the prior bar's far edge."""
    p = bars[k - 1]
    return bars[k].poc < p.val if long else bars[k].poc > p.vah


# ------------------------------------------------------------- filters

def climax(bars, i):
    """Spec 2: volume > 2x AND range > 2x the 20-bar averages."""
    if i < 20:
        return False
    w = bars[i - 20:i]
    av = sum(b.vol for b in w) / 20.0
    ar = sum(b.rng for b in w) / 20.0
    return bars[i].vol > CLIMAX_VOL * av and bars[i].rng > CLIMAX_RANGE * ar


def near_earnings(bars, i, earn_bars):
    """Spec 2: inside the two bars before or after a release."""
    if not earn_bars:
        return False
    return any(abs(i - e) <= EARNINGS_BLACKOUT for e in earn_bars)


# --------------------------------------------------------------- trade

class Trade:
    def __init__(self, sym, side, ref_i, trig_i, entry, stop, vaw, ref):
        self.sym, self.side = sym, side
        self.ref_i, self.entry_i = ref_i, trig_i
        self.entry, self.stop0, self.stop = entry, stop, stop
        self.vaw, self.ref = vaw, ref
        self.risk = abs(entry - stop)
        self.target = entry + side * vaw       # spec 5: measured move
        self.open_frac = 1.0
        self.exits = []                        # (bar_i, price, frac, rule)
        self.exit_i = None
        self.mfe = self.mae = 0.0

    @property
    def r(self):
        return sum(f * self.side * (px - self.entry) / self.risk
                   for _, px, f, _ in self.exits)

    @property
    def rule(self):
        """Spec 8: the single rule number that closed the position."""
        return self.exits[-1][3] if self.exits else "-"

    @property
    def bars_held(self):
        return (self.exit_i - self.entry_i) if self.exit_i is not None else 0


def close_out(t, k, px, rule):
    t.exits.append((k, px, t.open_frac, rule))
    t.open_frac = 0.0
    t.exit_i = k


def manage(bars, t):
    """Spec 5 and 6. Returns the closed trade."""
    side, long = t.side, t.side > 0
    stall = 0
    for k in range(t.entry_i + 1, len(bars)):
        b, prev = bars[k], bars[k - 1]
        adv = side * ((b.h if long else b.l) - t.entry)
        t.mfe = max(t.mfe, adv / t.risk)
        t.mae = min(t.mae, side * ((b.l if long else b.h) - t.entry) / t.risk)

        # spec 6.1 - the trailing stop is a resting order, so it fills intrabar
        if (long and b.l <= t.stop) or (not long and b.h >= t.stop):
            # A gap through the stop fills at the open, not at the stop price.
            # Without this every stop-out is exactly -1R, which flatters the
            # tail and is not what a resting order actually gets.
            gapped = (b.o < t.stop) if long else (b.o > t.stop)
            close_out(t, k, b.o if gapped else t.stop, "6.1 trailing stop")
            return t

        # spec 5.2 - scale half at the measured move (a resting limit)
        if t.open_frac == 1.0 and ((long and b.h >= t.target)
                                   or (not long and b.l <= t.target)):
            t.exits.append((k, t.target, SCALE_FRACTION, "5.2 measured move"))
            t.open_frac -= SCALE_FRACTION

        migrated = migrated_up(bars, k) if long else migrated_down(bars, k)

        # spec 6.2 / 6.3 - close-based signals. Spec 6.4: a signal on the
        # closing bar of the session is executed at the next bar's open.
        rule = None
        if migration_reversal(bars, k, long):
            rule = "6.2 migration reversal"
        elif failed_auction(bars, k, long):
            rule = "6.3 failed auction"

        # spec 5.3 - three consecutive bars with no migration our way
        stall = 0 if migrated else stall + 1
        if rule is None and stall >= STALL_BARS:
            rule = "5.3 time stop"

        if rule:
            if is_opening_bar(b) or k + 1 >= len(bars):
                close_out(t, k, b.c, rule)      # 12:45 close - executable now
            else:
                close_out(t, k + 1, bars[k + 1].o, rule + " (next open)")
            return t

        # spec 5.1 - ratchet the trail under the VAL of any bar that migrated
        if migrated:
            lvl = b.val - TICK if long else b.vah + TICK
            t.stop = max(t.stop, lvl) if long else min(t.stop, lvl)

    close_out(t, len(bars) - 1, bars[-1].c, "open at data end")
    return t


# ---------------------------------------------------------------- scan

def scan_symbol(path, bucket, sym, earn_bars=()):
    V.BUCKET = bucket
    bars = V.load(path)
    blackout = blackout_opens(bars)
    trades, vetoes = [], {}
    busy_until = -1
    i = 21
    while i < len(bars) - 1:
        if i <= busy_until:
            i += 1
            continue
        for long in (True, False):
            if not migration_run(bars, i, long):
                continue
            ref = bars[i]
            vaw = ref.vah - ref.val
            if vaw <= 0:
                continue
            # ---- spec 2 no-trade conditions, evaluated on the reference bar
            if climax(bars, i):
                vetoes["2.3 climax bar"] = vetoes.get("2.3 climax bar", 0) + 1
                continue
            if ref.key in blackout:
                vetoes["2.2 opex/rebalance open"] = vetoes.get("2.2 opex/rebalance open", 0) + 1
                continue
            if near_earnings(bars, i, earn_bars):
                vetoes["2.1 earnings blackout"] = vetoes.get("2.1 earnings blackout", 0) + 1
                continue
            # ---- spec 3 triggers, within the window
            for k in range(i + 1, min(i + 1 + TRIGGER_WINDOW, len(bars))):
                b = bars[k]
                hit = None
                # Spec 3: "price returns to the reference-bar VAL or POC and
                # the subsequent bar closes at or above that level" - the level
                # tested is whichever one price actually reached.
                if long:
                    if b.c > ref.vah:
                        hit = "continuation"
                    elif b.l <= ref.val and b.c >= ref.val:
                        hit = "pullback"
                    elif b.l <= ref.poc and b.c >= ref.poc:
                        hit = "pullback"
                else:
                    if b.c < ref.val:
                        hit = "continuation"
                    elif b.h >= ref.vah and b.c <= ref.vah:
                        hit = "pullback"
                    elif b.h >= ref.poc and b.c <= ref.poc:
                        hit = "pullback"
                if not hit:
                    continue
                entry = b.c
                side = 1 if long else -1
                # spec 3 stop: one tick beyond ref VAL / lowest low of the pair
                if long:
                    stop = min(ref.val, ref.l, b.l) - TICK
                else:
                    stop = max(ref.vah, ref.h, b.h) + TICK
                # spec 3 chase filter
                if abs(entry - ref.poc) > CHASE_WIDTHS * vaw:
                    vetoes["3.c chase filter"] = vetoes.get("3.c chase filter", 0) + 1
                    break
                # spec 4 maximum stop width
                if abs(entry - stop) > MAX_STOP_WIDTHS * vaw:
                    vetoes["4.m stop too wide"] = vetoes.get("4.m stop too wide", 0) + 1
                    break
                t = Trade(sym, side, i, k, entry, stop, vaw, ref)
                t.trigger = hit
                trades.append(manage(bars, t))
                busy_until = t.exit_i if t.exit_i is not None else k
                break
            break
        i += 1
    return bars, trades, vetoes


# --------------------------------------------------------------- report

def stats(rs):
    n = len(rs)
    if not n:
        return None
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "se": se,
            "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
            "t": mean / se if se else 0.0,
            "hit": 100.0 * len([r for r in rs if r > 0]) / n,
            "total": sum(rs), "worst": min(rs), "best": max(rs)}


def line(label, s, w=8):
    if s is None:
        return "%-*s   no trades" % (w, label)
    return ("%-*s  n=%3d   %+.2fR  [95%% %+.2f .. %+.2f]   hit %3.0f%%   "
            "total %+6.1fR   worst %+.2fR"
            % (w, label, s["n"], s["mean"], s["lo"], s["hi"], s["hit"],
               s["total"], s["worst"]))


def apply_concurrency(trades, cap=MAX_CONCURRENT):
    """Spec 2: no new position while `cap` are already open across the universe."""
    order = sorted(trades, key=lambda t: (t.entry_key, t.sym))
    open_until, kept, dropped = [], [], 0
    for t in order:
        open_until = [x for x in open_until if x > t.entry_key]
        if len(open_until) >= cap:
            dropped += 1
            continue
        kept.append(t)
        open_until.append(t.exit_key)
    return kept, dropped


def show_state(specfile, n):
    """Spec 1 and 3 evaluated on the most recent completed bars."""
    for spec in [l.strip() for l in open(specfile)
                 if l.strip() and not l.startswith("#")]:
        path, _, bk = spec.rpartition(":")
        sym = os.path.basename(path).split(".")[0].replace("_195m", "").upper()
        V.BUCKET = float(bk)
        bars = V.load(path)
        print("\n%s   last %d completed 195m bars   (row size %s)"
              % (sym, n, bk))
        print("  %-14s %9s %9s %9s %9s %9s   %s"
              % ("bar", "close", "POC", "VAH", "VAL", "VA width", "migration"))
        for i in range(len(bars) - n, len(bars)):
            b = bars[i]
            mig = ("up" if migrated_up(bars, i)
                   else "down" if migrated_down(bars, i) else "flat")
            print("  %-14s %9.2f %9.2f %9.2f %9.2f %9.2f   %s"
                  % (b.key, b.c, b.poc, b.vah, b.val, b.vah - b.val, mig))
        i = len(bars) - 1
        up = migration_run(bars, i, True)
        dn = migration_run(bars, i, False)
        ref = bars[i]
        vaw = ref.vah - ref.val
        if not (up or dn):
            run_up = run_dn = 0
            while migrated_up(bars, i - run_up):
                run_up += 1
            while migrated_down(bars, i - run_dn):
                run_dn += 1
            print("  SPEC 3: not eligible - needs %d consecutive migrations, "
                  "has %d up / %d down" % (MIGRATIONS_REQUIRED, run_up, run_dn))
            continue
        side = "LONG (Up Migration x%d)" % MIGRATIONS_REQUIRED if up else \
               "SHORT (Down Migration x%d)" % MIGRATIONS_REQUIRED
        print("  SPEC 3: ELIGIBLE %s   reference bar %s" % (side, ref.key))
        if up:
            print("     continuation trigger: next bar closes above VAH %.2f" % ref.vah)
            print("     pullback trigger:     price back to POC %.2f or VAL %.2f, "
                  "closing at/above it" % (ref.poc, ref.val))
            print("     chase filter:         no entry above %.2f (POC + 1 VA width)"
                  % (ref.poc + vaw))
            print("     stop would sit below: %.2f (ref VAL/low, less a tick)"
                  % (min(ref.val, ref.l) - TICK))
        else:
            print("     continuation trigger: next bar closes below VAL %.2f" % ref.val)
            print("     pullback trigger:     price back to POC %.2f or VAH %.2f, "
                  "closing at/below it" % (ref.poc, ref.vah))
            print("     chase filter:         no entry below %.2f (POC - 1 VA width)"
                  % (ref.poc - vaw))
            print("     stop would sit above: %.2f (ref VAH/high, plus a tick)"
                  % (max(ref.vah, ref.h) + TICK))
        print("     max stop width:       %.2f (2 VA widths); "
              "measured-move scale-out %.2f from entry" % (2 * vaw, vaw))


def attach_gex(trades, path):
    """Grade already-triggered trades with the GEX context layer (gex.py).

    The layer never creates a trade - price and value do that. It grades,
    downgrades or vetoes what they produced, which is the proposed hierarchy:
    195m structure -> POC/VAH/VAL -> GEX context -> entry/exit.
    """
    import gex as GX
    feed = GX.load(path)
    covered = 0
    for t in trades:
        day = feed.get((t.sym, t.entry_key.split("|")[0]))
        if day is None:
            t.gex_grade, t.gex_why = "no data", []
            continue
        covered += 1
        ctx = GX.context(t.entry, day, t.vaw, t.risk)
        structure = {"above_vah": t.trigger == "continuation" and t.side > 0,
                     "poc_rising": t.side > 0,
                     "reclaimed_poc": t.trigger == "pullback" and t.side > 0,
                     "val_rejection": t.trigger == "pullback" and t.side > 0,
                     "below_val": t.side < 0}
        t.gex_grade, t.gex_why = GX.grade_long(structure, ctx)
    print("GEX feed %s covers %d of %d trade dates\n" % (path, covered, len(trades)))
    if not covered:
        print("  -> no overlapping dates; the layer graded nothing.\n")


def report_gex(trades):
    by = {}
    for t in trades:
        by.setdefault(getattr(t, "gex_grade", "no data"), []).append(t.r)
    if set(by) == {"no data"}:
        return
    print("\nBY GEX GRADE (gex.py context layer)")
    for k in sorted(by, key=lambda k: -len(by[k])):
        print("  " + line(k, stats(by[k]), 12))
    kept = [t.r for t in trades
            if getattr(t, "gex_grade", "") in ("A", "B", "aggressive")]
    if kept:
        print("  " + line("A+B+aggr", stats(kept), 12) + "   <- what the layer would keep")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="file of path:bucket lines")
    ap.add_argument("--log", help="write the spec-8 trade log to this CSV")
    ap.add_argument("--trades", action="store_true")
    ap.add_argument("--equity", type=float, default=100000.0)
    ap.add_argument("--risk", type=float, default=0.005)
    ap.add_argument("--no-concurrency-cap", action="store_true")
    ap.add_argument("--gex", metavar="FILE",
                    help="CSV of daily GEX levels; attaches the gex.py context "
                         "layer and reports results by grade")
    ap.add_argument("--state", type=int, default=0, metavar="N",
                    help="print the last N bars with POC/VAH/VAL and the live "
                         "spec-3 setup status for each symbol, then exit")
    args = ap.parse_args()

    if args.state:
        show_state(args.spec, args.state)
        return

    specs = [l.strip() for l in open(args.spec)
             if l.strip() and not l.startswith("#")]
    all_t, vetoes, scanned = [], {}, 0
    for spec in specs:
        path, _, bk = spec.rpartition(":")
        sym = os.path.basename(path).split(".")[0].replace("_195m", "").upper()
        bars, ts, vt = scan_symbol(path, float(bk), sym)
        scanned += len(bars) - 22
        for k, v in vt.items():
            vetoes[k] = vetoes.get(k, 0) + v
        for t in ts:
            t.entry_key = bars[t.entry_i].key
            t.exit_key = bars[t.exit_i].key if t.exit_i is not None else "open"
            t.shares = int(args.equity * args.risk / t.risk) if t.risk else 0
            t.pnl = t.r * args.equity * args.risk
        all_t += ts

    if args.gex:
        attach_gex(all_t, args.gex)

    dropped = 0
    if not args.no_concurrency_cap:
        all_t, dropped = apply_concurrency(all_t)

    print("SPEC RUN: %d symbols, %d bars scanned, %d trades taken"
          % (len(specs), scanned, len(all_t)))
    print("no-trade vetoes: " + (", ".join("%s x%d" % (k, v)
          for k, v in sorted(vetoes.items())) or "none"))
    if dropped:
        print("spec 2 concurrency cap (max %d open) blocked %d further entries"
              % (MAX_CONCURRENT, dropped))
    print("NOT APPLIED: 2.1 earnings blackout (no calendar supplied), "
          "2.4 Risk Desk Extreme regime (external table)\n")

    print("PER SYMBOL")
    per = {}
    for t in all_t:
        per.setdefault(t.sym, []).append(t.r)
    for sym in sorted(per, key=lambda s: -len(per[s])):
        print("  " + line(sym, stats(per[sym])))

    print("\nBY DIRECTION")
    for lab, sel in (("long", 1), ("short", -1)):
        print("  " + line(lab, stats([t.r for t in all_t if t.side == sel])))

    print("\nBY TRIGGER (spec 3)")
    for lab in ("continuation", "pullback"):
        print("  " + line(lab, stats([t.r for t in all_t if t.trigger == lab]), 13))

    print("\nBY EXIT RULE (spec 8)")
    by = {}
    for t in all_t:
        by.setdefault(t.rule, []).append(t.r)
    for k in sorted(by, key=lambda k: -len(by[k])):
        s = stats(by[k])
        print("  %-28s n=%3d   avg %+.2fR" % (k, s["n"], s["mean"]))

    s = stats([t.r for t in all_t])
    print("\n" + "=" * 78)
    print("POOLED  " + line("", s, 0).strip())
    print("        sd %.2fR   t = %+.2f   -> mean is %s from zero at 95%%"
          % (s["sd"], s["t"],
             "DISTINGUISHABLE" if abs(s["t"]) > 1.96 else "not distinguishable"))
    print("\nLEAVE-ONE-OUT")
    for sym in sorted(per):
        rest = [t.r for t in all_t if t.sym != sym]
        print("  without %-5s n=%3d   %+.2fR" % (sym, len(rest), stats(rest)["mean"]))

    if args.gex:
        report_gex(all_t)

    if args.trades:
        print("\nTRADES")
        for t in sorted(all_t, key=lambda t: t.entry_key):
            print("  %-5s %-14s -> %-14s %-5s %5d sh  %8.2f -> %8.2f  "
                  "%+6.2fR  %-28s" % (t.sym, t.entry_key, t.exit_key,
                  "long" if t.side > 0 else "short", t.shares, t.entry,
                  t.exits[-1][1], t.r, t.rule))

    if args.log:
        with open(args.log, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["entry_bar", "exit_bar", "symbol", "direction", "size",
                        "entry_price", "exit_price", "exit_rule", "pnl_dollars",
                        "pnl_r"])
            for t in sorted(all_t, key=lambda t: t.entry_key):
                w.writerow([t.entry_key, t.exit_key, t.sym,
                            "long" if t.side > 0 else "short", t.shares,
                            "%.2f" % t.entry, "%.2f" % t.exits[-1][1], t.rule,
                            "%.2f" % t.pnl, "%.3f" % t.r])
        print("\nspec-8 trade log written to %s" % args.log)


if __name__ == "__main__":
    main()
