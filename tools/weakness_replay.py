r"""
weakness_replay.py - replay of the SELL_4H_WEAKNESS strategy (and its BUY mirror)
on XAUUSD 1-minute history. Research tool only: no AI, no orders, no account access.

Rules implemented (spec agreed 5 Nov 2026):
  1. 4H resistance (revised 5 Nov): >= min_run (3) closed 4H candles within
     touch_window (8) whose highs lie on the same line (within equal_tol_atr
     0.25 x ATR(4H,14)); wick size is irrelevant; no close above the line and no
     trade through it in the window; the line is a local high (>= the highest
     high of the prior 20 4H candles).  Zone = [highest body top of the
     touching candles, line].
  2. Zone: a bearish order block (bullish candle whose displacement leaves a
     bearish FVG: low[i] > high[i+2]) searched on 1H, 30M, 15M, 5M, in that
     order, formed after the run started; its 50% level must lie inside the 4H
     wick zone (zone bottom - zone_slack_atr x ATR .. zone top).
  3. Entry level (revised 5 Nov): 50% of the area of interest - the last
     candle of the swing (OB), a bearish breaker, or an unfilled bearish FVG.
     Priority: timeframe 1H -> 30M -> 15M -> 5M, then OB -> BREAKER -> FVG.
     --zones OB,FVG etc. restricts the types.  Limit sits at the 50%.
  4. Trigger: when price reaches the level, read the last closed 5M candle's
     structure (fractal swings, last break).  Bearish -> filled at the level.
     Bullish -> wait; the first later 5M close that is bearish AND below the
     level is a market entry at that close.
  5. A 15M close above the level = wait (no fills) until a 15M close back below.
     A 4H close above the level cancels.  Price trading through the stop before
     entry cancels.  A setup older than max_setup_4h (12) 4H candles expires.
  6. Stop = run high + max(stop_atr (0.1) x ATR4H, 2 x spread).  TP1 = first
     1H swing low below entry.  Skip if TP1 < min_rr (2.0) R.  Half off at TP1,
     stop to entry, rest to TP2 = next lower 1H swing low (else 2 x TP1
     distance).  Same-bar stop/target = loss.  Max hold max_hold_days (5).
  7. News lock is NOT modelled (no calendar in history).  Sessions and the
     Daily/4H macro are tagged so the report can split the results.

Usage (PowerShell, from Forex_Bot_Trader\):
  .\.venv\Scripts\python.exe tools\weakness_replay.py --self-test
  .\.venv\Scripts\python.exe tools\weakness_replay.py --days 720
  .\.venv\Scripts\python.exe tools\weakness_replay.py --csv replay_data\m1.csv
Outputs: replay_data\weakness_report.md and replay_data\weakness_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

VERSION = "0.4.0"

# ----------------------------------------------------------------------------- data


@dataclass
class Bars:
    """Parallel lists of candles. t = candle OPEN time, UTC, as epoch seconds."""
    t: list
    o: list
    h: list
    l: list
    c: list
    spread: list = field(default_factory=list)  # price units, M1 only

    def __len__(self):
        return len(self.t)


def aggregate(m1: Bars, minutes: int) -> Bars:
    """Aggregate M1 into fixed UTC-aligned buckets. Returns closed candles only
    for a full file; the caller treats the last bucket as closed."""
    sec = minutes * 60
    t, o, h, l, c = [], [], [], [], []
    cur = None
    for i in range(len(m1)):
        b = (m1.t[i] // sec) * sec
        if b != cur:
            cur = b
            t.append(b); o.append(m1.o[i]); h.append(m1.h[i]); l.append(m1.l[i]); c.append(m1.c[i])
        else:
            if m1.h[i] > h[-1]: h[-1] = m1.h[i]
            if m1.l[i] < l[-1]: l[-1] = m1.l[i]
            c[-1] = m1.c[i]
    return Bars(t, o, h, l, c)


def index_before(bars: Bars, ts: int) -> int:
    """Index of the last candle whose CLOSE time <= ts (candle fully closed), or -1."""
    # bars are sorted; binary search on open time, then check closedness
    lo, hi = 0, len(bars.t)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars.t[mid] <= ts:
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    return idx


# ----------------------------------------------------------------------------- indicators


def atr(bars: Bars, idx: int, n: int = 14) -> float:
    """Simple ATR over the n candles ending at idx (inclusive)."""
    if idx < 1:
        return 0.0
    start = max(1, idx - n + 1)
    trs = []
    for i in range(start, idx + 1):
        tr = max(bars.h[i] - bars.l[i], abs(bars.h[i] - bars.c[i - 1]), abs(bars.l[i] - bars.c[i - 1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def structure_trend(bars: Bars, end: int, lookback: int = 60, k: int = 2) -> str:
    """'BULLISH' if the most recent close broke a fractal swing high, 'BEARISH'
    if it broke a swing low, 'NONE' if no break in the window.  Uses candles
    [end-lookback+1 .. end] inclusive; swings need k candles each side."""
    start = max(0, end - lookback + 1)
    last_sh = None
    last_sl = None
    state = "NONE"
    for i in range(start, end + 1):
        # update swings confirmed at i (pivot at i-k)
        p = i - k
        if p - k >= start:
            is_high = all(bars.h[p] > bars.h[j] for j in range(p - k, p + k + 1) if j != p)
            is_low = all(bars.l[p] < bars.l[j] for j in range(p - k, p + k + 1) if j != p)
            if is_high:
                last_sh = bars.h[p]
            if is_low:
                last_sl = bars.l[p]
        if last_sh is not None and bars.c[i] > last_sh:
            state = "BULLISH"
            last_sh = None
        elif last_sl is not None and bars.c[i] < last_sl:
            state = "BEARISH"
            last_sl = None
    return state


def swing_lows(bars: Bars, end: int, lookback: int = 200, k: int = 3) -> list:
    """Confirmed fractal swing lows (price) in the window ending at `end`, oldest first."""
    out = []
    start = max(0, end - lookback + 1)
    for p in range(start + k, end - k + 1):
        if all(bars.l[p] < bars.l[j] for j in range(p - k, p + k + 1) if j != p):
            out.append(bars.l[p])
    return out


# ----------------------------------------------------------------------------- rules


@dataclass
class Params:
    min_run: int = 3
    touch_window: int = 8      # 4H candles (32 h) in which the touches must occur
    consecutive: bool = False  # True = touches must be back-to-back candles
    equal_tol_atr: float = 0.25
    zone_slack_atr: float = 0.25
    stop_atr: float = 0.1
    min_rr: float = 2.0
    max_setup_4h: int = 12
    max_hold_days: int = 5
    local_high_lookback: int = 20
    spread_default: float = 0.30
    ob_tfs: tuple = (60, 30, 15, 5)
    zone_types: tuple = ("OB", "BREAKER", "FVG")  # areas of interest, in priority order
    both_sides: bool = True


def weakness_run(h4: Bars, end: int, p: Params):
    """Resistance line on the 4H (rule revised 5 Nov 2026): at least min_run (3)
    closed 4H candles whose highs (wick tips) sit on the same line, i.e. within
    equal_tol_atr x ATR(4H) of the newest touch.  Wick size versus body does NOT
    matter.  Inside the touch window no candle may close above the line or trade
    clearly through it (high > line + tolerance), otherwise it is not a
    resistance.  The touches need not be consecutive unless p.consecutive.
    The signal fires on the candle that makes the min_run-th touch (the newest
    candle must be a touch).  Returns (first_touch_idx, line_high, zone_bottom,
    atr_value) or None.  Sell side only; the buy side mirrors the data."""
    if end < 15:
        return None
    a = atr(h4, end)
    if a <= 0:
        return None
    tol = p.equal_tol_atr * a
    ref = h4.h[end]
    w0 = max(0, end - p.touch_window + 1)
    touches = []
    for j in range(end, w0 - 1, -1):
        if h4.h[j] > ref + tol:
            break  # traded through the line: the line starts after this candle
        if abs(h4.h[j] - ref) <= tol:
            touches.append(j)
        elif p.consecutive:
            break
    touches.sort()
    if len(touches) < p.min_run:
        return None
    first = touches[0]
    line = max(h4.h[j] for j in touches)
    if any(h4.c[j] > line for j in range(first, end + 1)):
        return None
    prior = h4.h[max(0, first - p.local_high_lookback):first]
    if prior and line < max(prior):
        return None
    zone_bottom = max(max(h4.c[j], h4.o[j]) for j in touches)
    return first, line, zone_bottom, a


def find_ob(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    """Most recent bearish OB on this frame whose FVG candle closed by until_ts
    and whose 50% lies in [zone_lo, zone_hi].  Returns (level, ob_time) or None."""
    last = index_before(tf, until_ts)
    # candle i must be closed, i+1 (displacement) and i+2 (FVG) too
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    best = None
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.t[i + 2] + period > until_ts:
            i -= 1
            continue
        bullish = tf.c[i] > tf.o[i]
        displaced = tf.c[i + 1] < tf.l[i]
        fvg = tf.l[i] > tf.h[i + 2]
        if bullish and displaced and fvg:
            level = (tf.h[i] + tf.l[i]) / 2.0
            if zone_lo <= level <= zone_hi:
                best = (level, tf.t[i])
                break
        i -= 1
    return best


def find_breaker(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    """Bearish breaker: a bearish candle i (a demand OB) followed by a bullish
    displacement close above its high, later broken by a close below its low;
    the broken block is now resistance.  Level = 50% of candle i.  Every candle
    used must be closed by until_ts.  Returns (level, time) or None."""
    last = index_before(tf, until_ts)
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    if last >= 0 and tf.t[last] + period > until_ts:
        last -= 1
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.c[i] < tf.o[i] and tf.c[i + 1] > tf.h[i]:
            for j in range(i + 2, last + 1):
                if tf.c[j] < tf.l[i]:
                    level = (tf.h[i] + tf.l[i]) / 2.0
                    if zone_lo <= level <= zone_hi:
                        return level, tf.t[i]
                    break
        i -= 1
    return None


def find_fvg(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    """Bearish fair value gap (low[i] > high[i+2]), not yet filled by a close
    above low[i].  Level = 50% of the gap.  Returns (level, time) or None."""
    last = index_before(tf, until_ts)
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    if last >= 0 and tf.t[last] + period > until_ts:
        last -= 1
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.l[i] > tf.h[i + 2]:
            filled = any(tf.c[j] > tf.l[i] for j in range(i + 3, last + 1))
            level = (tf.l[i] + tf.h[i + 2]) / 2.0
            if not filled and zone_lo <= level <= zone_hi:
                return level, tf.t[i]
        i -= 1
    return None


ZONE_FINDERS = {"OB": find_ob, "BREAKER": find_breaker, "FVG": find_fvg}


def find_zone(frames: dict, p: "Params", since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    """Area of interest for the entry, in priority order: timeframe first
    (1H -> 30M -> 15M -> 5M), then type (p.zone_types, default OB, BREAKER, FVG).
    Returns (tf_minutes, zone_type, level) or None."""
    for tf in p.ob_tfs:
        for kind in p.zone_types:
            z = ZONE_FINDERS[kind](frames[tf], since_ts, until_ts, zone_lo, zone_hi)
            if z:
                return tf, kind, z[0]
    return None


def session_of(ts: int) -> str:
    hr = (ts // 3600) % 24
    if 0 <= hr < 7:
        return "ASIA"
    if 7 <= hr < 12:
        return "LONDON"
    if 12 <= hr < 16:
        return "LONDON_NY"
    if 16 <= hr < 21:
        return "NEW_YORK"
    return "OFF"


# ----------------------------------------------------------------------------- engine


@dataclass
class Trade:
    side: str
    signal_time: int
    entry_time: int
    entry: float
    stop: float
    tp1: float
    tp2: float
    ob_tf: int
    zone_type: str
    entry_kind: str
    rr1: float
    macro: str
    session: str
    exit_time: int = 0
    r: float = 0.0
    outcome: str = ""


FUNNEL: dict = {}


def mirror(b: Bars) -> Bars:
    return Bars(b.t, [-x for x in b.o], [-x for x in b.l], [-x for x in b.h], [-x for x in b.c], b.spread)


def run_engine(m1: Bars, p: Params, side: str = "SELL", frames=None) -> list:
    """Run the sell logic on `m1` (already mirrored for the buy side)."""
    if frames is None:
        frames = {m: aggregate(m1, m) for m in (5, 15, 60, 240, 1440)}
        for tf in p.ob_tfs:
            frames.setdefault(tf, aggregate(m1, tf))
    h4 = frames[240]
    m15 = frames[15]
    m5 = frames[5]
    h1 = frames[60]
    d1 = frames[1440]
    trades = []
    setup = None
    open_trade = None
    last_h4_checked = -1
    fun = FUNNEL.setdefault(side, dict(weakness=0, ob_found=0, cancel_4h_close=0, cancel_stop=0,
                                        expired=0, touched=0, skipped_rr=0, no_support=0, entered=0))
    # per-M1 loop; 4H decisions happen on the first M1 bar after a 4H close
    for i in range(len(m1)):
        ts = m1.t[i]
        sp = m1.spread[i] if m1.spread else p.spread_default
        hi, lo, cl = m1.h[i], m1.l[i], m1.c[i]
        bar_close_ts = ts + 60

        # ---- manage open trade on this bar
        if open_trade is not None:
            tr, st = open_trade
            # ask price for buy-backs = bid + spread
            stop_hit = hi + sp >= st["stop"]
            tgt = st["target"]
            tgt_hit = lo + sp <= tgt
            if stop_hit:  # worst case first
                if st["half_done"]:
                    tr.r = 0.5 * tr.rr1 + 0.5 * ((tr.entry - st["stop"]) / st["risk"])
                    tr.outcome = "TP1_THEN_STOP"
                else:
                    tr.r = -1.0
                    tr.outcome = "STOP"
                tr.exit_time = bar_close_ts
                trades.append(tr)
                open_trade = None
            elif tgt_hit:
                if not st["half_done"]:
                    st["half_done"] = True
                    st["stop"] = tr.entry + sp  # breakeven incl. spread
                    st["target"] = tr.tp2
                else:
                    tr.r = 0.5 * tr.rr1 + 0.5 * ((tr.entry - tr.tp2) / st["risk"])
                    tr.outcome = "TP2"
                    tr.exit_time = bar_close_ts
                    trades.append(tr)
                    open_trade = None
            elif bar_close_ts - tr.entry_time >= p.max_hold_days * 86400:
                r_rest = (tr.entry - (cl + sp)) / st["risk"]
                tr.r = (0.5 * tr.rr1 + 0.5 * r_rest) if st["half_done"] else r_rest
                tr.outcome = "TIME"
                tr.exit_time = bar_close_ts
                trades.append(tr)
                open_trade = None

        # ---- 4H boundary: weakness detection / cancel checks
        h4_idx = index_before(h4, ts) if ts % 14400 == 0 else -1
        if ts % 14400 == 0:
            closed = index_before(h4, ts - 1)  # last fully closed 4H candle
            if closed > last_h4_checked:
                last_h4_checked = closed
                if setup is not None:
                    if h4.c[closed] > setup["level"]:
                        setup = None; fun["cancel_4h_close"] += 1
                    elif closed - setup["signal_idx"] >= p.max_setup_4h:
                        setup = None; fun["expired"] += 1
                if setup is None and open_trade is None:
                    w = weakness_run(h4, closed, p)
                    if w is not None:
                        fun["weakness"] += 1
                        start, run_high, zone_bottom, a = w
                        zone_lo = zone_bottom - p.zone_slack_atr * a
                        found = find_zone(frames, p, h4.t[start], ts, zone_lo, run_high)
                        if found:
                            fun["ob_found"] += 1
                            tf, zkind, level = found
                            stop = run_high + max(p.stop_atr * a, 2 * sp)
                            setup = dict(level=level, stop=stop, signal_idx=closed, signal_ts=ts,
                                         ob_tf=tf, zone_type=zkind, waiting=False, touched=False, atr=a)

        if setup is None or open_trade is not None:
            continue

        # ---- stop traded before entry -> cancel
        if hi >= setup["stop"]:
            setup = None; fun["cancel_stop"] += 1
            continue

        # ---- 15M close above / below the level (wait / re-arm)
        if bar_close_ts % 900 == 0:
            k = index_before(m15, bar_close_ts - 1)
            if k >= 0 and m15.t[k] + 900 == bar_close_ts:
                if m15.c[k] > setup["level"]:
                    setup["waiting"] = True
                elif m15.c[k] < setup["level"] and setup["waiting"]:
                    setup["waiting"] = False
                    setup["touched"] = False

        if setup["waiting"]:
            continue

        entry = None
        kind = ""
        # ---- touch of the 50% level: limit fill if the 5M is already bearish
        if hi >= setup["level"] and not setup["touched"]:
            setup["touched"] = True; fun["touched"] += 1
            k5 = index_before(m5, ts - 1)
            if k5 >= 0 and structure_trend(m5, k5) == "BEARISH":
                entry, kind = setup["level"], "LIMIT"
        # ---- after a touch: first bearish 5M close below the level = market entry
        elif setup["touched"] and bar_close_ts % 300 == 0:
            k5 = index_before(m5, bar_close_ts - 1)
            if k5 >= 0 and m5.t[k5] + 300 == bar_close_ts and m5.c[k5] < setup["level"] \
                    and structure_trend(m5, k5) == "BEARISH":
                entry, kind = cl, "MARKET"

        if entry is None:
            continue

        risk = setup["stop"] - entry
        if risk <= 0:
            setup = None
            continue
        k1 = index_before(h1, ts - 1)
        lows = [x for x in swing_lows(h1, k1) if x < entry - 2 * sp]
        if not lows:
            setup = None; fun["no_support"] += 1
            continue
        tp1 = max(lows)
        rr1 = (entry - tp1) / risk
        if rr1 < p.min_rr:
            setup = None; fun["skipped_rr"] += 1  # the user's rule: first support; if < 2R, no trade
            continue
        lower = [x for x in lows if x < tp1]
        tp2 = max(lower) if lower else entry - 2 * (entry - tp1)
        kd = index_before(d1, ts - 1)
        k4 = index_before(h4, ts - 1)
        dtr = structure_trend(d1, kd, 100) if kd >= 0 else "NONE"
        htr = structure_trend(h4, k4, 100) if k4 >= 0 else "NONE"
        if dtr == htr and dtr != "NONE":
            macro = dtr
        else:
            macro = "MIXED"
        tr = Trade(side, setup["signal_ts"], ts, entry, setup["stop"], tp1, tp2, setup["ob_tf"], setup["zone_type"], kind,
                   rr1, macro, session_of(ts))
        open_trade = (tr, dict(stop=setup["stop"], target=tp1, half_done=False, risk=risk))
        setup = None; fun["entered"] += 1
    return trades


def unmirror(trades: list) -> list:
    flip = {"BULLISH": "BEARISH", "BEARISH": "BULLISH"}
    for tr in trades:
        tr.entry, tr.stop, tr.tp1, tr.tp2 = -tr.entry, -tr.stop, -tr.tp1, -tr.tp2
        tr.macro = flip.get(tr.macro, tr.macro)
    return trades


def build_frames(m1: Bars, p: Params) -> dict:
    frames = {m: aggregate(m1, m) for m in (5, 15, 60, 240, 1440)}
    for tf in p.ob_tfs:
        frames.setdefault(tf, aggregate(m1, tf))
    return frames


def run_all(m1: Bars, p: Params, cache: dict = None) -> list:
    """cache (optional) holds aggregated frames so several variants share one
    aggregation: keys 'm1r' (mirrored M1), 'f' and 'fr' (frames)."""
    FUNNEL.clear()
    if cache is None:
        cache = {}
    if "f" not in cache:
        cache["f"] = build_frames(m1, p)
    trades = run_engine(m1, p, "SELL", cache["f"])
    if p.both_sides:
        if "m1r" not in cache:
            cache["m1r"] = mirror(m1)
            cache["fr"] = build_frames(cache["m1r"], p)
        trades += unmirror(run_engine(cache["m1r"], p, "BUY", cache["fr"]))
    trades.sort(key=lambda x: x.entry_time)
    return trades


# Variants fixed BEFORE seeing their results (agreed 5 Nov 2026, after the 19-trade run).
VARIANTS = [
    ("BASE",       dict()),
    ("WIDE_STOP",  dict(stop_atr=0.5)),
    ("LOOSE",      dict(local_high_lookback=0, touch_window=12)),
    ("WIDE_LOOSE", dict(stop_atr=0.5, local_high_lookback=0, touch_window=12)),
]


def run_variants(m1: Bars, base: Params, out_dir: str) -> str:
    import dataclasses
    cache: dict = {}
    t0, t1 = m1.t[0], m1.t[-1]
    mid = t0 + (t1 - t0) // 2
    split = t0 + int(0.7 * (t1 - t0))
    years = (t1 - t0) / (365.25 * 86400)
    hdr = ("| variant | trades | /month | win % | avg R | net R | PF | max DD R | 1st half avg R (n) "
           "| 2nd half avg R (n) | last 30% avg R (n) | SELL avg R (n) | BUY avg R (n) | stopped before entry |\n"
           "|" + "---|" * 14)
    lines = [f"# SELL_4H_WEAKNESS variants (weakness_replay {VERSION})\n",
             f"M1 bars {len(m1)}; {datetime.fromtimestamp(t0, timezone.utc):%Y-%m-%d} -> "
             f"{datetime.fromtimestamp(t1, timezone.utc):%Y-%m-%d} UTC; base {base}\n",
             "Pass bar (decided before the run): >= 100 trades, avg R > 0 in BOTH halves, "
             "last-30% avg R > 0 with >= 30 trades, PF > 1.2.\n", hdr]
    details = []
    for name, over in VARIANTS:
        p = dataclasses.replace(base, **over)
        trades = run_all(m1, p, cache)
        s = stats(trades)
        def g(sel):
            x = stats(sel)
            return f"{x['avg']:+.2f} ({x['n']})"
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        stopped = sum(f["cancel_stop"] for f in FUNNEL.values())
        lines.append(
            f"| {name} | {s['n']} | {s['n'] / (years * 12):.1f} | {s['win']:.1f} | {s['avg']:+.2f} | "
            f"{s['net']:+.1f} | {pf} | {s['dd']:.1f} | {g([t for t in trades if t.entry_time < mid])} | "
            f"{g([t for t in trades if t.entry_time >= mid])} | {g([t for t in trades if t.entry_time >= split])} | "
            f"{g([t for t in trades if t.side == 'SELL'])} | {g([t for t in trades if t.side == 'BUY'])} | {stopped} |")
        sub = write_report(trades, m1, p, os.path.join(out_dir, f"variant_{name}"), f"variant {name}")
        details.append(f"- {name}: {over or 'spec as agreed'} -> {sub}")
        print(f"{name}: {s['n']} trades, avg R {s['avg']:+.2f}")
    lines.append("\n## Variant settings and full reports\n" + "\n".join(details))
    lines.append("\nRule: a variant that only works in one half, or with < 30 trades in the last 30%, is noise. "
                 "No further variants are fitted to this data; the next test is a new period or live logging.")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "weakness_variants.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ----------------------------------------------------------------------------- report


def stats(trs: list) -> dict:
    n = len(trs)
    if n == 0:
        return dict(n=0, win=0.0, avg=0.0, net=0.0, pf=0.0, dd=0.0)
    rs = [t.r for t in trs]
    wins = sum(1 for r in rs if r > 0)
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dict(n=n, win=100.0 * wins / n, avg=sum(rs) / n, net=sum(rs),
                pf=(gp / gl) if gl > 0 else float("inf"), dd=dd)


def row(label: str, s: dict) -> str:
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"| {label} | {s['n']} | {s['win']:.1f} | {s['avg']:+.2f} | {s['net']:+.1f} | {pf} | {s['dd']:.1f} |"


def write_report(trades: list, m1: Bars, p: Params, out_dir: str, title: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    md = []
    hdr = "| group | trades | win % | avg R | net R | PF | max DD R |\n|---|---|---|---|---|---|---|"
    t0 = datetime.fromtimestamp(m1.t[0], timezone.utc)
    t1 = datetime.fromtimestamp(m1.t[-1], timezone.utc)
    md.append(f"# {title}\n")
    md.append(f"weakness_replay {VERSION}; M1 bars {len(m1)}; period {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} UTC; "
              f"params {p}\n")
    md.append("## Overall\n" + hdr)
    md.append(row("ALL", stats(trades)))
    for side in ("SELL", "BUY"):
        md.append(row(side, stats([t for t in trades if t.side == side])))
    # in / out of sample: last 30% of the period
    split = m1.t[0] + int(0.7 * (m1.t[-1] - m1.t[0]))
    md.append("\n## In-sample (first 70%) vs out-of-sample (last 30%)\n" + hdr)
    md.append(row("in-sample", stats([t for t in trades if t.entry_time < split])))
    md.append(row("out-of-sample", stats([t for t in trades if t.entry_time >= split])))

    def group(name, key):
        md.append(f"\n## By {name}\n" + hdr)
        for v in sorted(set(key(t) for t in trades), key=str):
            md.append(row(str(v), stats([t for t in trades if key(t) == v])))

    group("Daily/4H macro at entry (BEARISH = with-trend for a SELL)", lambda t: f"{t.side} in {t.macro}")
    group("zone timeframe (minutes)", lambda t: t.ob_tf)
    group("area of interest (zone type)", lambda t: t.zone_type)
    group("zone type x timeframe", lambda t: f"{t.zone_type} {t.ob_tf}m")
    group("entry kind", lambda t: t.entry_kind)
    group("session (UTC)", lambda t: t.session)
    group("outcome", lambda t: t.outcome)
    md.append("\n## Funnel (why setups did or did not become trades)\n| side | " + " | ".join(
        next(iter(FUNNEL.values())).keys()) + " |\n|---|" + "---|" * len(next(iter(FUNNEL.values()))))
    for sd, f in FUNNEL.items():
        md.append(f"| {sd} | " + " | ".join(str(v) for v in f.values()) + " |")
    md.append("\n## Caveats\n"
              "- No news lock in history: trades inside USD release windows are included.\n"
              "- Areas of interest: OB with FVG, breaker, unfilled FVG (50% of each); other zone types are not modelled.\n"
              "- The 5M trend read is mechanical (fractal structure, last break), not your eye.\n"
              "- Candles are UTC-aligned; broker 4H candles are server-time aligned (UTC+2/+3).\n"
              "- Same-minute stop and target counts as a loss. Spread from the M1 feed if present.\n"
              "- A result on one feed and one period is a hypothesis, not proof.\n")
    path = os.path.join(out_dir, "weakness_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(out_dir, "weakness_trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["side", "signal_utc", "entry_utc", "exit_utc", "entry", "stop", "tp1", "tp2", "ob_tf", "zone_type",
                    "entry_kind", "rr1", "macro", "session", "outcome", "r"])
        for t in trades:
            w.writerow([t.side, datetime.fromtimestamp(t.signal_time, timezone.utc).isoformat(),
                        datetime.fromtimestamp(t.entry_time, timezone.utc).isoformat(),
                        datetime.fromtimestamp(t.exit_time, timezone.utc).isoformat(),
                        f"{t.entry:.2f}", f"{t.stop:.2f}", f"{t.tp1:.2f}", f"{t.tp2:.2f}", t.ob_tf, t.zone_type,
                        t.entry_kind, f"{t.rr1:.2f}", t.macro, t.session, t.outcome, f"{t.r:.2f}"])
    return path


# ----------------------------------------------------------------------------- data sources


def load_csv(path: str) -> Bars:
    """Columns (any order, case-insensitive): time, open, high, low, close, [spread].
    time = ISO-8601 UTC or epoch seconds. spread in price units or points (<1 => price)."""
    t, o, h, l, c, s = [], [], [], [], [], []
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        cols = {k.lower(): k for k in rd.fieldnames}
        for r in rd:
            tv = r[cols["time"]]
            if tv.replace(".", "", 1).isdigit():
                ts = int(float(tv))
            else:
                dt = datetime.fromisoformat(tv.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
            t.append(ts); o.append(float(r[cols["open"]])); h.append(float(r[cols["high"]]))
            l.append(float(r[cols["low"]])); c.append(float(r[cols["close"]]))
            if "spread" in cols and r[cols["spread"]] != "":
                s.append(float(r[cols["spread"]]))
    if s and max(s) > 5:  # points, not price: XAUUSD point = 0.01
        s = [x * 0.01 for x in s]
    return Bars(t, o, h, l, c, s)


def server_to_utc(server_ts: int, rule: str) -> int:
    """Convert a broker server timestamp to UTC. rule 'ny+7' = New York local + 7h
    (UTC+3 in summer, UTC+2 in winter); 'utc+N' = fixed offset."""
    if rule.startswith("ny+"):
        if ZoneInfo is None:
            return server_ts - 3 * 3600
        hours = int(rule[3:])
        naive = datetime.fromtimestamp(server_ts, timezone.utc).replace(tzinfo=None)
        ny = naive - timedelta(hours=hours)  # this is NY wall-clock time
        off = ZoneInfo("America/New_York").utcoffset(ny)
        return int((ny - off).replace(tzinfo=timezone.utc).timestamp())
    if rule.startswith("utc"):
        return server_ts - int(float(rule[3:])) * 3600
    raise ValueError(rule)


def fetch_mt5(symbol: str, days: int, rule: str) -> Bars:
    import MetaTrader5 as mt5  # type: ignore
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise SystemExit(f"symbol {symbol} not found")
        point = info.point
        want = days * 1440
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, want)
        if rates is None or len(rates) == 0:
            raise SystemExit(f"copy_rates_from_pos returned nothing: {mt5.last_error()}")
    finally:
        mt5.shutdown()
    t, o, h, l, c, s = [], [], [], [], [], []
    for r in rates:
        t.append(server_to_utc(int(r["time"]), rule)); o.append(float(r["open"])); h.append(float(r["high"]))
        l.append(float(r["low"])); c.append(float(r["close"])); s.append(float(r["spread"]) * point)
    bars = Bars(t, o, h, l, c, s)
    print(f"MT5: {len(bars)} M1 bars from {datetime.fromtimestamp(t[0], timezone.utc):%Y-%m-%d} "
          f"(asked for {want}); raise 'Max bars in chart' if fewer than expected")
    os.makedirs("replay_data", exist_ok=True)
    with open(os.path.join("replay_data", "weakness_m1.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "spread"])
        for i in range(len(bars)):
            w.writerow([t[i], o[i], h[i], l[i], c[i], f"{s[i]:.3f}"])
    return bars


def synthetic(days: int = 120, seed: int = 7) -> Bars:
    """Deterministic M1 random walk with session volatility and weekend gaps."""
    rnd = random.Random(seed)
    start = int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp())
    t, o, h, l, c, s = [], [], [], [], [], []
    price = 2600.0
    drift = 0.0
    ts = start
    end = start + days * 86400
    while ts < end:
        wd = datetime.fromtimestamp(ts, timezone.utc).weekday()
        if wd >= 5:
            ts += 86400
            continue
        if ts % (4 * 86400) == 0:
            drift = rnd.choice([-0.02, 0.0, 0.02])
        hr = (ts // 3600) % 24
        vol = 0.6 if 7 <= hr < 20 else 0.25
        op = price
        cl = op + drift + rnd.gauss(0, vol)
        hi = max(op, cl) + abs(rnd.gauss(0, vol * 0.6))
        lo = min(op, cl) - abs(rnd.gauss(0, vol * 0.6))
        t.append(ts); o.append(op); h.append(hi); l.append(lo); c.append(cl); s.append(0.3)
        price = cl
        ts += 60
    return Bars(t, o, h, l, c, s)


# ----------------------------------------------------------------------------- self-test


def self_test() -> int:
    import unittest

    class T(unittest.TestCase):
        def test_aggregate(self):
            m = synthetic(3)
            h = aggregate(m, 60)
            self.assertEqual(h.t[0] % 3600, 0)
            self.assertGreaterEqual(max(h.h), max(m.h) - 1e-9)

        def _h4(self, tail):
            """20 rising candles then `tail` = list of (open, high, low, close)."""
            t, o, h, l, c = [], [], [], [], []
            px = 100.0
            for i in range(20):
                t.append(i * 14400); o.append(px); c.append(px + 1); h.append(px + 1.2); l.append(px - 0.5)
                px += 1
            for k, (op, hi, lo, cl) in enumerate(tail):
                t.append((20 + k) * 14400); o.append(op); h.append(hi); l.append(lo); c.append(cl)
            return Bars(t, o, h, l, c)

        def test_three_highs_on_a_line_fire_whatever_the_wick_size(self):
            # big bodies, tiny wicks: still a resistance (rule revised 5 Nov)
            b = self._h4([(119, 123.0, 118.5, 122.8), (122.8, 123.1, 120, 120.5), (120.5, 122.9, 119.5, 122.7)])
            w = weakness_run(b, len(b) - 1, Params())
            self.assertIsNotNone(w)
            self.assertEqual(w[0], 20)
            self.assertAlmostEqual(w[1], 123.1)

        def test_touches_need_not_be_consecutive(self):
            tail = [(119, 123.0, 118.5, 121), (121, 121.5, 117, 118), (118, 123.05, 117.5, 120), (120, 123.0, 119, 121)]
            b = self._h4(tail)
            self.assertIsNotNone(weakness_run(b, len(b) - 1, Params()))
            self.assertIsNone(weakness_run(b, len(b) - 1, Params(consecutive=True)))

        def test_close_above_or_trade_through_is_not_a_resistance(self):
            above = self._h4([(119, 123.0, 118.5, 121), (121, 123.1, 120, 123.4), (121, 123.0, 119, 121)])
            self.assertIsNone(weakness_run(above, len(above) - 1, Params()))
            through = self._h4([(119, 123.0, 118.5, 121), (121, 126.0, 120, 121), (121, 123.0, 119, 121)])
            self.assertIsNone(weakness_run(through, len(through) - 1, Params()))
            two = self._h4([(119, 123.0, 118.5, 121), (121, 123.0, 119, 121)])
            self.assertIsNone(weakness_run(two, len(two) - 1, Params()))

        def test_find_ob_requires_fvg_inside_zone(self):
            t = [i * 900 for i in range(6)]
            o = [10, 10, 11.5, 10.0, 9.0, 8.5]
            c = [10.5, 11.5, 10.0, 9.0, 8.5, 8.2]
            h = [10.6, 11.6, 11.6, 10.1, 9.1, 8.6]
            l = [9.9, 9.9, 9.8, 8.9, 8.4, 8.0]
            tf = Bars(t, o, h, l, c)
            # candle 1 is bullish (9.9..11.6), candle 2 displaces below its low? 10.0 > 9.9 -> no
            # candle 2 ... make candle 3 close below low of candle 2? candle2 bearish. Use candle 1:
            l[2] = 9.8; c[2] = 9.7  # displacement below 9.9
            h[3] = 9.5  # FVG: low[1]=9.9 > high[3]=9.5
            ob = find_ob(Bars(t, o, h, l, c), 0, 6 * 900, 9.0, 12.0)
            self.assertIsNotNone(ob)
            self.assertAlmostEqual(ob[0], (11.6 + 9.9) / 2)
            self.assertIsNone(find_ob(Bars(t, o, h, l, c), 0, 6 * 900, 12.0, 13.0))
            # not closed yet -> not usable
            self.assertIsNone(find_ob(Bars(t, o, h, l, c), 0, 3 * 900 + 60, 9.0, 12.0))

        def test_breaker_and_fvg_levels(self):
            # bearish candle 0 (demand OB 10..11), bullish displacement 1, rally, then close below 10
            t = [i * 900 for i in range(7)]
            o = [11.0, 10.2, 12.0, 12.5, 11.8, 10.4, 9.6]
            c = [10.2, 12.0, 12.5, 11.8, 10.4, 9.6, 9.4]
            h = [11.0, 12.1, 12.7, 12.6, 11.9, 10.5, 9.7]
            l = [10.0, 10.1, 11.9, 11.7, 10.3, 9.5, 9.3]
            b = Bars(t, o, h, l, c)
            z = find_breaker(b, 0, 7 * 900, 9.0, 12.0)
            self.assertIsNotNone(z)
            self.assertAlmostEqual(z[0], 10.5)
            self.assertIsNone(find_breaker(b, 0, 7 * 900, 11.0, 12.0))  # 50% outside the zone
            # two unfilled bearish FVGs: newest low[4]=10.3 > high[6]=9.7 -> 10.0;
            # older low[3]=11.7 > high[5]=10.5 -> 11.1 (found when the zone excludes 10.0)
            f = find_fvg(b, 0, 7 * 900, 9.0, 13.0)
            self.assertAlmostEqual(f[0], (10.3 + 9.7) / 2)
            f = find_fvg(b, 0, 7 * 900, 10.6, 13.0)
            self.assertAlmostEqual(f[0], (11.7 + 10.5) / 2)

        def test_zone_priority_and_filter(self):
            t = [i * 900 for i in range(7)]
            o = [11.0, 10.2, 12.0, 12.5, 11.8, 10.4, 9.6]
            c = [10.2, 12.0, 12.5, 11.8, 10.4, 9.6, 9.4]
            h = [11.0, 12.1, 12.7, 12.6, 11.9, 10.5, 9.7]
            l = [10.0, 10.1, 11.9, 11.7, 10.3, 9.5, 9.3]
            frames = {15: Bars(t, o, h, l, c)}
            p = Params(ob_tfs=(15,), zone_types=("BREAKER", "FVG"))
            self.assertEqual(find_zone(frames, p, 0, 7 * 900, 9.0, 13.0)[1], "BREAKER")
            p = Params(ob_tfs=(15,), zone_types=("FVG",))
            self.assertEqual(find_zone(frames, p, 0, 7 * 900, 9.0, 13.0)[1], "FVG")

        def test_local_high_off_and_variants_run(self):
            b = self._h4([(119, 123.0, 118.5, 122.8), (122.8, 123.1, 120, 120.5), (120.5, 122.9, 119.5, 122.7)])
            b.h[5] = 200.0  # an older, much higher high: blocks the line unless the check is off
            self.assertIsNone(weakness_run(b, len(b) - 1, Params()))
            self.assertIsNotNone(weakness_run(b, len(b) - 1, Params(local_high_lookback=0)))
            import tempfile
            m = synthetic(60)
            with tempfile.TemporaryDirectory() as d:
                path = run_variants(m, Params(), d)
                txt = open(path, encoding="utf-8").read()
                for name, _ in VARIANTS:
                    self.assertIn(f"| {name} |", txt)

        def test_wider_stop_is_further_away(self):
            m = synthetic(90)
            a = run_all(m, Params(stop_atr=0.1))
            b = run_all(m, Params(stop_atr=0.5))
            ra = [abs(t.stop - t.entry) for t in a]
            rb = [abs(t.stop - t.entry) for t in b]
            if ra and rb:
                self.assertGreater(sum(rb) / len(rb), sum(ra) / len(ra))

        def test_structure_trend(self):
            # rising then a break down
            o = []; h = []; l = []; c = []
            seq = [1, 2, 3, 2, 1, 2, 4, 5, 4, 3, 2, 1, 0]
            for v in seq:
                o.append(v); c.append(v); h.append(v + 0.4); l.append(v - 0.4)
            b = Bars([i * 300 for i in range(len(seq))], o, h, l, c)
            self.assertEqual(structure_trend(b, 6), "BULLISH")
            self.assertEqual(structure_trend(b, len(seq) - 1), "BEARISH")

        def test_no_look_ahead_and_runs(self):
            m = synthetic(90)
            trades = run_all(m, Params())
            for tr in trades:
                self.assertGreater(tr.entry_time, tr.signal_time - 1)
                self.assertGreater(tr.exit_time, tr.entry_time)
                self.assertGreaterEqual(tr.rr1, 2.0)
                if tr.side == "SELL":
                    self.assertGreater(tr.stop, tr.entry)
                    self.assertLess(tr.tp1, tr.entry)
                else:
                    self.assertLess(tr.stop, tr.entry)
                    self.assertGreater(tr.tp1, tr.entry)
            self.assertEqual(trades, sorted(trades, key=lambda x: x.entry_time))

        def test_server_to_utc(self):
            summer = int(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())
            winter = int(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp())
            self.assertEqual(server_to_utc(summer, "ny+7"), summer - 3 * 3600)
            self.assertEqual(server_to_utc(winter, "ny+7"), winter - 2 * 3600)
            self.assertEqual(server_to_utc(winter, "utc+3"), winter - 3 * 3600)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if res.wasSuccessful() else 1


# ----------------------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--days", type=int, default=720)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--cached", action="store_true", help="reuse replay_data\\weakness_m1.csv (no MT5)")
    ap.add_argument("--csv", help="replay this M1 CSV instead of MT5 (time,open,high,low,close[,spread])")
    ap.add_argument("--synthetic", action="store_true", help="run on synthetic data (smoke test only)")
    ap.add_argument("--offset-rule", default="ny+7", help="server->UTC rule: ny+7 (default) or utc+3")
    ap.add_argument("--sell-only", action="store_true")
    ap.add_argument("--min-run", type=int, default=3)
    ap.add_argument("--min-rr", type=float, default=2.0)
    ap.add_argument("--touch-window", type=int, default=8, help="4H candles in which the 3 touches must occur")
    ap.add_argument("--stop-atr", type=float, default=0.1, help="stop buffer above the line, x 4H ATR")
    ap.add_argument("--local-high", type=int, default=20,
                    help="line must be >= the highest high of this many prior 4H candles; 0 = off")
    ap.add_argument("--variants", action="store_true", help="run BASE, WIDE_STOP, LOOSE, WIDE_LOOSE and compare")
    ap.add_argument("--zones", default="OB,BREAKER,FVG", help="areas of interest in priority order")
    ap.add_argument("--consecutive", action="store_true", help="touches must be back-to-back 4H candles")
    ap.add_argument("--out", default="replay_data")
    ap.add_argument("--title", default="SELL_4H_WEAKNESS replay")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    p = Params(min_run=a.min_run, min_rr=a.min_rr, both_sides=not a.sell_only,
               touch_window=a.touch_window, consecutive=a.consecutive,
               zone_types=tuple(z.strip().upper() for z in a.zones.split(",") if z.strip()),
               stop_atr=a.stop_atr, local_high_lookback=a.local_high)
    if a.cached:
        m1 = load_csv(os.path.join("replay_data", "weakness_m1.csv"))
    elif a.csv:
        m1 = load_csv(a.csv)
    elif a.synthetic:
        m1 = synthetic(a.days)
    else:
        m1 = fetch_mt5(a.symbol, a.days, a.offset_rule)
    if len(m1) < 10000:
        print(f"only {len(m1)} M1 bars - too little history to say anything")
    if a.variants:
        path = run_variants(m1, p, a.out)
        with open(path, encoding="utf-8") as f:
            print(f.read())
        print(f"\nwritten: {path}")
        return 0
    trades = run_all(m1, p)
    path = write_report(trades, m1, p, a.out, a.title)
    with open(path, encoding="utf-8") as f:
        print(f.read())
    print(f"\nwritten: {path} and {os.path.join(a.out, 'weakness_trades.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
