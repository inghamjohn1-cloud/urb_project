#!/usr/bin/env python3
"""195-minute volume-profile swing scanner (POC / VAH / VAL).

Implements the rules in PLAN_195M_VOLUME_PROFILE.md against real 195m bars and
replays each signal forward so you can see what the rules would actually have
done. Standard library only.

Input CSV (one row per 195m bar, chronological):

    bar,o,h,l,c,vol,prof
    2026-08-25|1,266.22,267.81,264.99,267.76,170792,1060:7,1061:56,...

`bar` is `YYYY-MM-DD|H` where H is 0 for the 09:30-12:45 ET half-session and 1
for 12:45-16:00. `prof` is the bar's volume profile as `bucket:volume` pairs,
where bucket index * BUCKET_SIZE is the low edge of the price row. Build it from
1-minute bars by bucketing each minute's volume at its VWAP.

Usage:
    python vp195.py bars.csv                # scan, list signals + outcomes
    python vp195.py bars.csv --levels 10    # print levels for the last 10 bars
    python vp195.py bars.csv --equity 50000 --risk 0.01
"""

import argparse
import csv
import sys

BUCKET = 0.25          # price-row size used when the profile was built
VA_FRACTION = 0.70     # value area = 70% of volume
SWING_BARS = 6         # swing value area - the current leg (~3 sessions)
COMPOSITE_BARS = 20    # rolling composite value area (~2 weeks)
NODE_BARS = 60         # HVN/LVN map (~6 weeks)
SMA_BARS = 20          # trend gate (~10 sessions)
ATR_BARS = 14
SLOPE_LOOKBACK = 10    # SMA slope
MIGRATION_LOOKBACK = 5 # value migration
OVERLAP_LOOKBACK = 10  # balanced-market test
MAX_OVERLAP = 0.75
MAX_STOP_ATR = 2.0
MAX_BAR_ATR = 2.5      # climax filter
MIN_VOL_FRAC = 0.60    # thin-profile filter
MIN_PAYOFF_R = 1.5     # the PRIMARY target (T2, or T1 on a single-target
                       # trade) must pay at least this. T1 on a laddered trade
                       # is a de-risking partial, not the payoff, so it carries
                       # no minimum - requiring 1R there vetoes ~90% of signals
                       # because the stop and the next structural level sit on
                       # the same scale.
TIME_STOP_BARS = 8
TIME_STOP_R = 0.5
TRIGGER_OFFSET = 0.05
ORDER_LIFE = 2         # bars a stop-entry order stays live


# ---------------------------------------------------------------- data

class Bar:
    __slots__ = ("key", "o", "h", "l", "c", "vol", "hist",
                 "poc", "vah", "val")

    def __init__(self, key, o, h, l, c, vol, hist):
        self.key, self.o, self.h, self.l, self.c = key, o, h, l, c
        self.vol, self.hist = vol, hist
        self.val, self.poc, self.vah = value_area(hist)

    @property
    def rng(self):
        return self.h - self.l

    def __repr__(self):
        return "<%s c=%.2f>" % (self.key, self.c)


def load(path):
    bars = []
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header[:6] != ["bar", "o", "h", "l", "c", "vol"]:
            sys.exit("unexpected header: %s" % header[:7])
        for row in reader:
            if not row:
                continue
            hist = {}
            for pair in row[6:]:
                bk, _, v = pair.partition(":")
                hist[int(bk)] = hist.get(int(bk), 0) + int(v)
            bars.append(Bar(row[0], float(row[1]), float(row[2]),
                            float(row[3]), float(row[4]), int(row[5]), hist))
    return bars


# ------------------------------------------------------- profile maths

def value_area(hist, frac=VA_FRACTION):
    """Value area by the standard expand-from-the-POC method.

    Start at the highest-volume row and keep annexing whichever neighbouring
    row holds more volume until `frac` of the total is enclosed. Returns
    (VAL, POC, VAH) as prices: VAL/VAH are the outer edges of the boundary
    rows, POC the midpoint of the peak row. The value area always contains the
    POC, which a plain narrowest-window search does not guarantee on a bimodal
    profile.
    """
    if not hist:
        return (0.0, 0.0, 0.0)
    lo_bk, hi_bk = min(hist), max(hist)
    vols = [hist.get(bk, 0) for bk in range(lo_bk, hi_bk + 1)]
    total = sum(vols)
    target = total * frac
    poc_bk = max(hist, key=lambda bk: (hist[bk], -bk))
    lo = hi = poc_bk - lo_bk
    run = vols[lo]
    while run < target and (lo > 0 or hi < len(vols) - 1):
        below = vols[lo - 1] if lo > 0 else -1
        above = vols[hi + 1] if hi < len(vols) - 1 else -1
        if above >= below:
            hi += 1
            run += vols[hi]
        else:
            lo -= 1
            run += vols[lo]
    return ((lo_bk + lo) * BUCKET,
            (poc_bk + 0.5) * BUCKET,
            (lo_bk + hi + 1) * BUCKET)


def merge(bars):
    hist = {}
    for b in bars:
        for bk, v in b.hist.items():
            hist[bk] = hist.get(bk, 0) + v
    return hist


def nodes(hist, top=4):
    """High-volume nodes: local volume peaks, strongest first."""
    if not hist:
        return []
    lo, hi = min(hist), max(hist)
    peaks = []
    for bk in range(lo, hi + 1):
        v = hist.get(bk, 0)
        if v and v >= hist.get(bk - 1, 0) and v >= hist.get(bk + 1, 0):
            peaks.append(((bk + 0.5) * BUCKET, v))
    peaks.sort(key=lambda p: -p[1])
    return [p[0] for p in peaks[:top]]


def overlap(a, b):
    """Fraction of value area `a` that also lies inside value area `b`."""
    lo = max(a[0], b[0])
    hi = min(a[2], b[2])
    width = a[2] - a[0]
    return max(0.0, hi - lo) / width if width else 1.0


# ------------------------------------------------------------ indicators

def sma(bars, i, n):
    if i + 1 < n:
        return None
    return sum(b.c for b in bars[i - n + 1:i + 1]) / n


def atr(bars, i, n=ATR_BARS):
    if i < n:
        return None
    total = 0.0
    for k in range(i - n + 1, i + 1):
        pc = bars[k - 1].c
        total += max(bars[k].h - bars[k].l,
                     abs(bars[k].h - pc), abs(bars[k].l - pc))
    return total / n


# ------------------------------------------------------------- context

class Ctx:
    """Everything known at the close of bar i, using only completed bars."""

    def __init__(self, bars, i):
        self.i = i
        self.bar = bars[i]
        self.atr = atr(bars, i)
        self.sma = sma(bars, i, SMA_BARS)
        self.sma_prev = sma(bars, i - SLOPE_LOOKBACK, SMA_BARS)
        # Composite value EXCLUDES the current bar: the level has to be known
        # before the bar that trades through it, or a breakout bar drags the
        # level along with it and can never close outside.
        self.cva = value_area(merge(bars[i - COMPOSITE_BARS:i]))
        # The trigger tier: value built by the current leg. The composite is
        # too wide to trade against - in a trend its VAL sits a whole leg
        # below price, so a pullback never reaches it.
        self.sva = value_area(merge(bars[i - SWING_BARS:i]))
        j = i - MIGRATION_LOOKBACK
        self.cva_prev = value_area(merge(bars[j - COMPOSITE_BARS:j]))
        k = i - OVERLAP_LOOKBACK
        self.cva_old = value_area(merge(bars[k - COMPOSITE_BARS:k]))
        self.nodes = nodes(merge(bars[i - NODE_BARS:i])) if i >= NODE_BARS else []
        self.avg_vol = sum(b.vol for b in bars[i - 20:i]) / 20.0

    val = property(lambda s: s.cva[0])
    poc = property(lambda s: s.cva[1])
    vah = property(lambda s: s.cva[2])
    width = property(lambda s: s.cva[2] - s.cva[0])
    sval = property(lambda s: s.sva[0])
    spoc = property(lambda s: s.sva[1])
    svah = property(lambda s: s.sva[2])
    swidth = property(lambda s: s.sva[2] - s.sva[0])
    balanced = property(lambda s: overlap(s.cva, s.cva_old) > MAX_OVERLAP)

    def regime(self):
        if None in (self.sma, self.sma_prev, self.atr):
            return 0
        up = (self.bar.c > self.sma and self.sma > self.sma_prev
              and self.cva[1] > self.cva_prev[1])
        dn = (self.bar.c < self.sma and self.sma < self.sma_prev
              and self.cva[1] < self.cva_prev[1])
        return 1 if up else (-1 if dn else 0)


# --------------------------------------------------------------- setups

class Signal:
    def __init__(self, ctx, setup, side, entry, stop, targets, note=""):
        self.ctx, self.setup, self.side = ctx, setup, side
        self.entry, self.stop, self.targets = entry, stop, targets
        self.note = note
        self.risk = abs(entry - stop)
        self.vetoes = []
        self.trade = None

    @property
    def bar(self):
        return self.ctx.bar

    def payoff_r(self):
        """R multiple at the primary target: T2 when laddered, else T1."""
        tp = self.targets[1] if len(self.targets) > 1 else self.targets[0]
        return abs(tp - self.entry) / self.risk if self.risk else 0.0

    def check(self):
        c, s = self.ctx, self
        if s.risk <= 0:
            s.vetoes.append("stop on the wrong side of entry")
            return s
        if s.risk > MAX_STOP_ATR * c.atr:
            s.vetoes.append("stop %.2f > 2.0xATR (%.2f)" % (s.risk, MAX_STOP_ATR * c.atr))
        if s.payoff_r() < MIN_PAYOFF_R:
            s.vetoes.append("payoff only %.2fR (<%.1fR)" % (s.payoff_r(), MIN_PAYOFF_R))
        if c.bar.rng > MAX_BAR_ATR * c.atr:
            s.vetoes.append("climax bar: range %.2f > 2.5xATR (%.2f)" % (c.bar.rng, MAX_BAR_ATR * c.atr))
        if c.bar.vol < MIN_VOL_FRAC * c.avg_vol:
            s.vetoes.append("thin: vol %.0f%% of 20-bar avg" % (100.0 * c.bar.vol / c.avg_vol))
        if s.setup in ("A", "B") and c.balanced:
            s.vetoes.append("balanced: value overlaps 10 bars ago by %.0f%%" % (100 * overlap(c.cva, c.cva_old)))
        return s


def scan_bar(bars, i):
    """Return every setup that fires at the close of bar i."""
    c = Ctx(bars, i)
    b, prev = bars[i], bars[i - 1]
    out = []
    reg = c.regime()
    a = c.atr

    # --- Setup A: swing-VAL reclaim (and swing-VAH rejection short).
    # Trigger off swing value; take profit at composite structure.
    if reg == 1 and min(b.l, prev.l) <= c.sval < b.c and b.poc >= c.sval:
        entry = b.h + TRIGGER_OFFSET
        stop = min(b.l, c.sval) - 0.25 * a
        t1 = c.spoc if c.spoc > entry else c.svah
        out.append(Signal(c, "A", 1, entry, stop,
                          [t1, c.svah + c.swidth, c.vah + c.width], "reclaimed sVAL"))
    if reg == -1 and max(b.h, prev.h) >= c.svah > b.c and b.poc <= c.svah:
        entry = b.l - TRIGGER_OFFSET
        stop = max(b.h, c.svah) + 0.25 * a
        t1 = c.spoc if c.spoc < entry else c.sval
        out.append(Signal(c, "A", -1, entry, stop,
                          [t1, c.sval - c.swidth, c.val - c.width], "rejected sVAH"))

    # --- Setup B: acceptance outside swing value
    prev_c = Ctx(bars, i - 1)
    if reg == 1 and prev.c > prev_c.svah and b.c > c.svah and b.poc > c.svah:
        entry = b.h + TRIGGER_OFFSET
        stop = min(c.svah - 0.5 * a, b.poc - 0.01)
        t3 = next((n for n in sorted(c.nodes) if n > c.svah + c.swidth), c.svah + 2.0 * c.swidth)
        out.append(Signal(c, "B", 1, entry, stop,
                          [c.svah + 0.5 * c.swidth, c.svah + c.swidth, t3], "accepted above sVAH"))
    if reg == -1 and prev.c < prev_c.sval and b.c < c.sval and b.poc < c.sval:
        entry = b.l - TRIGGER_OFFSET
        stop = max(c.sval + 0.5 * a, b.poc + 0.01)
        t3 = next((n for n in sorted(c.nodes, reverse=True) if n < c.sval - c.swidth), c.sval - 2.0 * c.swidth)
        out.append(Signal(c, "B", -1, entry, stop,
                          [c.sval - 0.5 * c.swidth, c.sval - c.swidth, t3], "accepted below sVAL"))

    # --- Setup C: the 80% rule (rotation back through swing value)
    inside = lambda x: c.sval <= x <= c.svah
    if inside(b.c) and inside(prev.c) and inside(b.poc):
        was_below = any(bars[k].c < Ctx(bars, k).sval for k in range(i - 4, i - 1))
        was_above = any(bars[k].c > Ctx(bars, k).svah for k in range(i - 4, i - 1))
        # C is a ROTATION trade: it needs a market that is actually balanced,
        # not merely one that isn't trending against it. "Regime != -1" is not
        # the same as balanced - a strict trend gate reports neutral all the
        # way up a strong advance, and C then shorts every new high.
        if c.balanced or reg != 0:
            if was_below and reg >= 0:
                out.append(Signal(c, "C", 1, b.c, c.sval - 0.5 * a, [c.svah], "80% rule from below"))
            elif was_above and reg <= 0:
                out.append(Signal(c, "C", -1, b.c, c.svah + 0.5 * a, [c.sval], "80% rule from above"))

    return [s.check() for s in out]


# ------------------------------------------------------------ replay

class Trade:
    def __init__(self, sig, fill_i, fill):
        self.sig, self.fill_i, self.fill = sig, fill_i, fill
        self.exits = []          # (bar_index, price, fraction, reason)
        self.mfe_r = 0.0
        self.mae_r = 0.0
        self.exit_i = None

    @property
    def realised_r(self):
        r = self.sig.risk
        return sum(f * self.sig.side * (px - self.fill) / r for _, px, f, _ in self.exits)

    @property
    def bars_held(self):
        return (self.exit_i - self.fill_i) if self.exit_i is not None else 0

    @property
    def reasons(self):
        seen = []
        for _, _, _, why in self.exits:
            if why not in seen:
                seen.append(why)
        return "+".join(seen)


def replay(bars, sig):
    """Work the entry order, then manage the position to a flat close."""
    c, side, i = sig.ctx, sig.side, sig.ctx.i
    trig, stop, tgts = sig.entry, sig.stop, sig.targets
    fill = fill_i = None
    if sig.setup == "C":                           # market entry, next open
        if i + 1 >= len(bars):
            return None
        fill, fill_i = bars[i + 1].o, i + 1
    for k in range(i + 1, min(i + 1 + ORDER_LIFE, len(bars))):
        if fill is not None:
            break
        b = bars[k]
        hit = b.h >= trig if side > 0 else b.l <= trig
        if hit:
            fill = max(trig, b.o) if side > 0 else min(trig, b.o)
            fill_i = k
            break
        if sig.setup == "A":                       # invalidation before fill
            if (side > 0 and b.c < c.sval) or (side < 0 and b.c > c.svah):
                return None
    if fill is None:
        return None

    t = Trade(sig, fill_i, fill)
    legs = [1 / 3.0, 1 / 3.0, 1 / 3.0] if len(tgts) == 3 else [1.0]
    left, leg, cur_stop = 1.0, 0, stop
    for k in range(fill_i, len(bars)):
        b = bars[k]
        held = k - fill_i
        t.mfe_r = max(t.mfe_r, side * (b.h if side > 0 else b.l) - side * fill)
        t.mae_r = min(t.mae_r, side * (b.l if side > 0 else b.h) - side * fill)
        # stop first — if both the stop and a target are inside the bar, assume
        # the stop went first. Bar data can't tell you, so take the bad fill.
        if (side > 0 and b.l <= cur_stop) or (side < 0 and b.h >= cur_stop):
            why = "stop" if cur_stop == stop else "breakeven"
            t.exits.append((k, cur_stop, left, why))
            t.exit_i = k
            break
        while leg < len(tgts) and left > 1e-9:
            tp = tgts[leg]
            if (side > 0 and b.h >= tp) or (side < 0 and b.l <= tp):
                f = min(legs[leg], left)
                t.exits.append((k, tp, f, "T%d" % (leg + 1)))
                left -= f
                leg += 1
                if leg == 1:
                    cur_stop = fill              # breakeven after T1
            else:
                break
        if left <= 1e-9:
            t.exit_i = k
            break
        # failed breakout: accepted back inside value kills a Setup B
        if sig.setup == "B" and k > fill_i:
            cc, pc = Ctx(bars, k), Ctx(bars, k - 1)
            back = ((side > 0 and b.c < cc.svah and bars[k - 1].c < pc.svah and b.poc < cc.svah)
                    or (side < 0 and b.c > cc.sval and bars[k - 1].c > pc.sval and b.poc > cc.sval))
            if back:
                t.exits.append((k, b.c, left, "failed-breakout"))
                t.exit_i = k
                break
        if leg >= 2:                              # trail the last third
            p = bars[k - 1]
            if (side > 0 and b.c < p.val) or (side < 0 and b.c > p.vah):
                t.exits.append((k, b.c, left, "trail"))
                t.exit_i = k
                break
        if held >= TIME_STOP_BARS and leg == 0:
            if side * (b.c - fill) < TIME_STOP_R * sig.risk:
                t.exits.append((k, b.c, left, "time"))
                t.exit_i = k
                break
    if left > 1e-9 and t.exit_i is None:          # ran out of data
        t.exits.append((len(bars) - 1, bars[-1].c, left, "open"))
        t.exit_i = len(bars) - 1
    t.mfe_r /= sig.risk
    t.mae_r /= sig.risk
    return t


# ------------------------------------------------------------- reporting

def fmt_levels(c):
    return ("swing  sVAL %7.2f  sPOC %7.2f  sVAH %7.2f  (w %5.2f)\n"
            "  compos cVAL %7.2f  cPOC %7.2f  cVAH %7.2f  (w %5.2f)   ATR %5.2f"
            % (c.sval, c.spoc, c.svah, c.swidth,
               c.val, c.poc, c.vah, c.width, c.atr))


def main():
    global COMPOSITE_BARS
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--equity", type=float, default=100000.0)
    ap.add_argument("--risk", type=float, default=0.005)
    ap.add_argument("--levels", type=int, default=0,
                    help="print the level table for the last N bars and exit")
    ap.add_argument("--show-vetoed", action="store_true")
    ap.add_argument("--composite", type=int, default=COMPOSITE_BARS,
                    help="bars in the rolling composite value area")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    COMPOSITE_BARS = args.composite
    bars = load(args.csv)
    start = max(SMA_BARS, COMPOSITE_BARS + OVERLAP_LOOKBACK, ATR_BARS) + 1

    if args.levels:
        print("%-14s %7s %7s | %7s %7s %7s | %7s %7s %7s | %6s %3s" %
              ("bar", "close", "bPOC", "sVAL", "sPOC", "sVAH",
               "cVAL", "cPOC", "cVAH", "ATR", "reg"))
        for i in range(max(start, len(bars) - args.levels), len(bars)):
            c = Ctx(bars, i)
            print("%-14s %7.2f %7.2f | %7.2f %7.2f %7.2f | %7.2f %7.2f %7.2f | %6.2f %3s" %
                  (bars[i].key, bars[i].c, bars[i].poc, c.sval, c.spoc, c.svah,
                   c.val, c.poc, c.vah, c.atr,
                   {1: "up", -1: "dn", 0: "--"}[c.regime()]))
        return

    taken, vetoed = [], []
    for i in range(start, len(bars) - 1):
        for sig in scan_bar(bars, i):
            (vetoed if sig.vetoes else taken).append(sig)

    print("Scanned %d bars (%s .. %s), %d valid signals, %d vetoed\n"
          % (len(bars) - start - 1, bars[start].key, bars[-1].key,
             len(taken), len(vetoed)))

    for sig in taken:
        c = sig.ctx
        t = replay(bars, sig)
        shares = int(args.equity * args.risk / sig.risk)
        side = "LONG " if sig.side > 0 else "SHORT"
        if args.quiet:
            sig.trade = t
            continue
        print("=" * 78)
        print("%s  Setup %s  %s   (%s)" % (sig.bar.key, sig.setup, side, sig.note))
        print("  signal bar   o %.2f  h %.2f  l %.2f  c %.2f   bPOC %.2f  vol %s"
              % (sig.bar.o, sig.bar.h, sig.bar.l, sig.bar.c, sig.bar.poc,
                 format(sig.bar.vol, ",")))
        print("  %s" % fmt_levels(c))
        print("  entry %.2f   stop %.2f   R %.2f (%.2f x ATR)   %d sh, risk $%.0f"
              % (sig.entry, sig.stop, sig.risk, sig.risk / c.atr, shares, shares * sig.risk))
        print("  targets  " + "  ".join(
            "T%d %.2f (%.2fR)" % (n + 1, tp, abs(tp - sig.entry) / sig.risk)
            for n, tp in enumerate(sig.targets)))
        if t is None:
            print("  -> never triggered")
            continue
        print("  filled %.2f on %s" % (t.fill, bars[t.fill_i].key))
        for k, px, f, why in t.exits:
            print("     %-14s %-14s %5.0f%% @ %.2f  (%+.2fR)"
                  % (bars[k].key, why, f * 100, px, f * sig.side * (px - t.fill) / sig.risk))
        print("  -> %+.2fR in %d bars   MFE %+.2fR  MAE %+.2fR"
              % (t.realised_r, t.bars_held, t.mfe_r, t.mae_r))
        sig.trade = t

    done = [s for s in taken if s.trade]
    if done:
        rs = [s.trade.realised_r for s in done]
        wins = [r for r in rs if r > 0]
        print("\n" + "=" * 78)
        print("%d triggered / %d signals   expectancy %+.2fR   hit rate %.0f%%"
              % (len(done), len(taken), sum(rs) / len(rs), 100.0 * len(wins) / len(rs)))
        print("total %+.2fR   best %+.2fR   worst %+.2fR"
              % (sum(rs), max(rs), min(rs)))
        by = {}
        for s in done:
            by.setdefault(s.setup, []).append(s.trade.realised_r)
        for k in sorted(by):
            print("  setup %s: n=%d  expectancy %+.2fR  total %+.2fR"
                  % (k, len(by[k]), sum(by[k]) / len(by[k]), sum(by[k])))

    if args.show_vetoed:
        print("\n" + "=" * 78)
        print("VETOED (%d)" % len(vetoed))
        for sig in vetoed:
            print("  %-14s %s %-5s  entry %.2f stop %.2f  ->  %s"
                  % (sig.bar.key, sig.setup, "long" if sig.side > 0 else "short",
                     sig.entry, sig.stop, "; ".join(sig.vetoes)))


if __name__ == "__main__":
    main()
