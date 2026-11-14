"""
1-minute mean-reversion SCALP model for the replay (30 Oct 2026, owner's request: "many trades a day, a couple of
dollars each, against the candle").

Rule (deterministic, closed 1M candles only):
  * MA   = simple moving average of the last `ma` 1M closes;  ATR1 = 14-bar ATR on 1M candles.
  * entry  when the 1M close is stretched more than `k` x ATR1 beyond the MA: SELL above, BUY below - a MARKET order
           filled on the NEXT bar's open (bid for a sell, ask = bid + spread for a buy, plus `slippage`).
  * target the MA value at the decision (a few dollars away); stop at `stop_mult` x that distance beyond the entry;
           if neither is hit within `hold_min` minutes the trade is closed at the bar close (time exit).
  * one trade at a time; `cooldown` bars after an exit; optional session filter; optional "range only" filter
           (macro MIXED/NEUTRAL as read by the replay's regime module at the previous 15M close).
  * costs: the bar's recorded spread, `slippage` on the entry and on stop exits; the same conventions as the main
           replay (a bar that touches stop and target is a LOSS - 1-minute data cannot order them).

Results are reported in DOLLARS PER 0.01 LOT (1 $ of gold = 1 $ at 0.01 lot) as well as in R, because the owner's
question is "a couple of dollars per trade, many times a day" and the spread is the whole story at that scale.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import sessions as SS

UTC = timezone.utc
TRENDING = ("BULLISH", "BEARISH")

DEFAULTS = {"ma": 20, "atr": 14, "slippage": 0.10, "spread": 0.30, "warmup": 60}

# name: (k x ATR1 stretch, stop multiple of the target distance, hold minutes, allowed sessions or None, range-only)
VARIANTS = {
    "SCALP_k1.5_s1.5": (1.5, 1.5, 30, None, False),
    "SCALP_k2.0_s1.5": (2.0, 1.5, 30, None, False),
    "SCALP_k2.0_s2.0": (2.0, 2.0, 30, None, False),
    "SCALP_k3.0_s1.5": (3.0, 1.5, 30, None, False),
    "SCALP_k2.0_s1.5_LDN_NY": (2.0, 1.5, 30, ("LONDON", "LONDON_NY", "NEW_YORK"), False),
    "SCALP_k2.0_s1.5_rangeOnly": (2.0, 1.5, 30, None, True),
    "SCALP_k2.0_s1.5_hold60": (2.0, 1.5, 60, None, False),
    "SCALP_k2.0_s1.0": (2.0, 1.0, 30, None, False),              # 1:1 - the "couple of dollars" shape
    "SCALP_k1.5_s1.0_ASIA": (1.5, 1.0, 30, ("ASIA",), False),    # the quiet session, where ranges live
}


@dataclass
class ScalpTrade:
    variant: str
    direction: str
    created: datetime
    fill: float
    stop: float
    target: float
    exit_time: datetime
    exit_price: float
    outcome: str
    usd: float            # $ per 0.01 lot, costs included
    r: float
    macro: str | None
    session: str
    hold_min: int
    cost: float = 0.0     # $ of spread + slippage paid on this trade (per 0.01 lot)


def indicators(m1: list, ma: int, atr: int) -> tuple[list, list]:
    """Rolling SMA of closes and Wilder-free simple ATR (mean true range) over `atr` bars, aligned to m1."""
    n = len(m1)
    sma = [None] * n
    tr = [0.0] * n
    s = 0.0
    for i, b in enumerate(m1):
        s += b["c"]
        if i >= ma:
            s -= m1[i - ma]["c"]
        if i >= ma - 1:
            sma[i] = s / ma
        pc = m1[i - 1]["c"] if i else b["o"]
        tr[i] = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
    atrs = [None] * n
    s = 0.0
    for i in range(n):
        s += tr[i]
        if i >= atr:
            s -= tr[i - atr]
        if i >= atr - 1:
            atrs[i] = s / atr
    return sma, atrs


def _macro_at(timeline_t: list, timeline_v: list, t: datetime):
    k = bisect.bisect_right(timeline_t, t) - 1
    return timeline_v[k] if k >= 0 else None


def run_variant(name: str, m1: list, sma: list, atrs: list, cfg: dict, macro_timeline: tuple, sessions_cfg) -> list:
    k, stop_mult, hold_min, sessions_ok, range_only = VARIANTS[name]
    slip = cfg["slippage"]
    tl_t, tl_v = macro_timeline
    out: list[ScalpTrade] = []
    i = cfg["warmup"]
    n = len(m1)
    while i < n - 1:
        b = m1[i]
        ma, a = sma[i], atrs[i]
        if ma is None or a is None or a <= 0:
            i += 1
            continue
        stretch = (b["c"] - ma) / a
        if abs(stretch) < k:
            i += 1
            continue
        t = b["t"]
        nxt = m1[i + 1]
        if (nxt["t"] - t) > timedelta(minutes=5):                # a gap (weekend/break): no entry into it
            i += 1
            continue
        if sessions_ok and SS.session_of(t, sessions_cfg) not in sessions_ok:
            i += 1
            continue
        macro = _macro_at(tl_t, tl_v, t) if tl_t else None
        if range_only and macro in TRENDING:
            i += 1
            continue
        if range_only and macro is None:
            i += 1
            continue
        direction = "SELL" if stretch > 0 else "BUY"
        buy = direction == "BUY"
        sp = nxt["s"] if nxt["s"] is not None else cfg["spread"]
        fill = (nxt["o"] + sp + slip) if buy else (nxt["o"] - slip)
        target = ma
        dist = abs(target - fill)
        if dist < 2 * sp:                                          # the target would be inside the spread: skip
            i += 1
            continue
        stop = fill - stop_mult * dist if buy else fill + stop_mult * dist
        deadline = nxt["t"] + timedelta(minutes=hold_min)
        j = i + 1
        exit_price = exit_t = outcome = None
        while j < n:
            x = m1[j]
            spx = x["s"] if x["s"] is not None else cfg["spread"]
            if buy:
                stop_hit, tgt_hit = x["l"] <= stop, x["h"] >= target
            else:
                stop_hit, tgt_hit = x["h"] + spx >= stop, x["l"] + spx <= target   # a short exits on the ask
            if j == i + 1:                                         # the fill bar may stop us, never pay us
                tgt_hit = False
            if stop_hit:
                gap = (x["o"] < stop) if buy else (x["o"] + spx > stop)
                px = (x["o"] if gap else stop) - slip if buy else ((x["o"] + spx) if gap else stop) + slip
                exit_price, exit_t, outcome = px, x["t"], "stop (gap)" if gap else "stop"
                break
            if tgt_hit:
                exit_price, exit_t, outcome = target, x["t"], "target"
                break
            if x["t"] >= deadline or (j + 1 < n and (m1[j + 1]["t"] - x["t"]) > timedelta(minutes=5)):
                px = x["c"] if buy else x["c"] + spx
                exit_price, exit_t, outcome = px, x["t"], "time exit"
                break
            j += 1
        if exit_price is None:
            break                                                  # data ended inside a trade: drop it
        usd = (exit_price - fill) if buy else (fill - exit_price)
        risk = stop_mult * dist
        cost = sp + slip + (slip if outcome.startswith("stop") else 0.0)   # spread once, slippage in and (on stops) out
        out.append(ScalpTrade(name, direction, t, round(fill, 2), round(stop, 2), round(target, 2), exit_t,
                              round(exit_price, 2), outcome, round(usd, 2), round(usd / risk, 3), macro,
                              SS.session_of(t, sessions_cfg), int((exit_t - nxt["t"]).total_seconds() // 60), round(cost, 2)))
        i = j + 1                                                  # one trade at a time; resume after the exit
    return out


def run_all(m1: list, cfg: dict, macro_timeline: tuple, sessions_cfg, progress=None) -> dict:
    c = dict(DEFAULTS, **(cfg or {}))
    sma, atrs = indicators(m1, c["ma"], c["atr"])
    out = {}
    for idx, name in enumerate(VARIANTS):
        if progress:
            progress(idx, len(VARIANTS))
        out[name] = run_variant(name, m1, sma, atrs, c, macro_timeline, sessions_cfg)
    return out


# ---------------------------------------------------------------------- statistics
def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    usd = [t.usd for t in trades]
    wins = [u for u in usd if u > 0]
    losses = [u for u in usd if u < 0]
    eq = peak = dd = 0.0
    worst_day: dict = {}
    for t in sorted(trades, key=lambda x: x.exit_time):
        eq += t.usd
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        d = SS.trading_day(t.created)
        worst_day[d] = worst_day.get(d, 0.0) + t.usd
    days = {SS.trading_day(t.created) for t in trades}
    return {"n": len(trades), "win": len(wins) / len(usd) * 100, "avg_usd": sum(usd) / len(usd), "net_usd": sum(usd),
            "pf": (sum(wins) / -sum(losses)) if losses else float("inf"), "dd_usd": dd,
            "avg_r": sum(t.r for t in trades) / len(trades), "per_day": len(trades) / max(1, len(days)),
            "worst_day": min(worst_day.values()) if worst_day else 0.0,
            "avg_hold": sum(t.hold_min for t in trades) / len(trades),
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0, "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "avg_cost": sum(t.cost for t in trades) / len(trades)}


HEAD = "trades | /day | win % | avg $ | gross $ | cost $ | avg win $ | avg loss $ | net $ | PF | max DD $ | worst day $ | avg R | hold min"


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "   0 |   -  |    -  |     -  |     -  |     -  |     -  |     -  |      -  |   -  |     -  |     -  |    -  |  -"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['n']:>5} | {s['per_day']:>4.1f} | {s['win']:>5.1f}% | {s['avg_usd']:>+6.2f} | "
            f"{s['avg_usd'] + s['avg_cost']:>+6.2f} | {s['avg_cost']:>5.2f} | {s['avg_win']:>+6.2f} | "
            f"{s['avg_loss']:>+6.2f} | {s['net_usd']:>+8.0f} | {pf:>5} | {s['dd_usd']:>7.0f} | {s['worst_day']:>+7.0f} | "
            f"{s['avg_r']:>+5.2f} | {s['avg_hold']:>4.0f}")


def report(results: dict, split: datetime, variants_detail=("SCALP_k2.0_s1.5", "SCALP_k2.0_s1.0", "SCALP_k2.0_s1.5_rangeOnly")) -> list:
    L = ["## Scalp model: 1-minute mean reversion (stretch > k x ATR1 from the 20-bar mean, target the mean)", "",
         "MARKET on the next 1M open, target = the mean, stop = s x the target distance, time exit after the hold; one "
         "trade at a time; the bar's own spread + $0.10 slippage; stop+target in one bar = loss. Dollars are per 0.01 lot.",
         "", "| variant | " + HEAD + " |", "|" + "---|" * 15]
    for v in VARIANTS:
        L.append(f"| {v} | " + fmt(stats(results[v])) + " |")
    L += ["", "### Scalp in-sample vs out-of-sample", "", "| variant | part | " + HEAD + " |", "|" + "---|" * 16]
    for v in VARIANTS:
        for name, sel in (("in", lambda t: t.created < split), ("out", lambda t: t.created >= split)):
            L.append(f"| {v} | {name} | " + fmt(stats([t for t in results[v] if sel(t)])) + " |")
    L.append("")
    for v in variants_detail:
        for label, key in (("year", lambda t: t.created.year), ("macro at the previous 15M close", lambda t: t.macro),
                           ("session", lambda t: t.session), ("direction", lambda t: t.direction),
                           ("outcome", lambda t: t.outcome)):
            groups: dict = {}
            for t in results[v]:
                groups.setdefault(key(t), []).append(t)
            L += [f"### {v} by {label}", "", f"| {label} | " + HEAD + " |", "|" + "---|" * 15]
            for g in sorted(groups, key=str):
                L.append(f"| {g} | " + fmt(stats(groups[g])) + " |")
            L.append("")
    L += ["Reading the scalp table: `avg $` is what one 0.01-lot trade made after spread and slippage, `gross $` the "
          "same trade with the costs added back, `cost $` the spread + slippage paid per trade. When `gross $` is about "
          "zero the candles have no edge to give; when `gross $` is positive but `avg $` is not, the spread took it. "
          "`worst day $` and `max DD $` show what the trending days do to a mean-reversion book.", ""]
    return L


# ====================================================================== 11 Nov: 1-minute TREND PULLBACK (roadmap plan)
# Owner, 10 Nov: "can we make another functionality ... a scalping bot". Two candidates are replayed side by side before
# anything is built: the mean reversion above and this one. Deterministic, closed candles only:
#   * 5M context: M1 aggregated to broker-aligned 5M candles; EMA21 on 5M closes. UP = last closed 5M close > EMA21 and
#     EMA21 above its value 3 candles earlier (DOWN mirrored). No context -> no trade.
#   * 1M: EMA9 and EMA21. In an UP context with EMA9 > EMA21, a pullback is a 1M candle whose low reaches EMA21; the
#     entry signal is the first later candle (within 10) that CLOSES back above EMA9. MARKET on the next 1M open.
#   * stop: the lowest low of the pullback (last 6 candles) - 0.1 x ATR1, at least max(1 x ATR1, 3 x spread) away.
#   * target: `rr` x the stop distance; time exit after `hold` minutes; one trade at a time.
#   * costs and same-bar conventions exactly as the mean-reversion model (spread once, slippage in and on stops,
#     a bar that touches stop and target counts as the stop).
PB_VARIANTS = {
    # name: (rr, hold minutes, sessions or None)
    "PULLBACK_rr1.5": (1.5, 60, None),
    "PULLBACK_rr2.0": (2.0, 60, None),
    "PULLBACK_rr1.5_LDN_NY": (1.5, 60, ("LONDON", "LONDON_NY", "NEW_YORK")),
    "PULLBACK_rr2.0_LDN_NY": (2.0, 60, ("LONDON", "LONDON_NY", "NEW_YORK")),
    "PULLBACK_rr1.0_LDN_NY": (1.0, 30, ("LONDON", "LONDON_NY", "NEW_YORK")),
}


def ema(values: list, n: int) -> list:
    out, k, e = [None] * len(values), 2.0 / (n + 1), None
    for i, v in enumerate(values):
        e = v if e is None else e + k * (v - e)
        out[i] = e if i >= n - 1 else None
    return out


def context_5m(m1: list, n: int = 21, slope: int = 3) -> list:
    """For each M1 index: 'UP' / 'DOWN' / None from the last CLOSED 5M candle before that M1 candle closes."""
    closes, keys, idx_of = [], [], []
    cur = None
    for i, b in enumerate(m1):
        k = int(b["t"].timestamp() // 300)
        if k != cur:
            if cur is not None:
                keys.append(cur)
            closes.append(b["c"])
            cur = k
        else:
            closes[-1] = b["c"]
        idx_of.append(len(closes) - 1)
    e = ema(closes, n)
    out = [None] * len(m1)
    for i, b in enumerate(m1):
        j = idx_of[i] - 1                                        # the 5M candle before the forming one
        if (b["t"].timestamp() + 60) % 300 == 0:
            j = idx_of[i]                                        # this M1 closes its 5M candle
        if j - slope < 0 or e[j] is None or e[j - slope] is None:
            continue
        if closes[j] > e[j] and e[j] > e[j - slope]:
            out[i] = "UP"
        elif closes[j] < e[j] and e[j] < e[j - slope]:
            out[i] = "DOWN"
    return out


def run_pullback(name: str, m1: list, atrs: list, ctx: list, e9: list, e21: list, cfg: dict, sessions_cfg,
                 variants: dict | None = None) -> list:
    rr, hold_min, sessions_ok = (variants or PB_VARIANTS)[name]
    slip = cfg["slippage"]
    out: list[ScalpTrade] = []
    n = len(m1)
    i = cfg["warmup"]
    armed = None                                                 # (direction, index of the pullback touch)
    while i < n - 1:
        b, a, c = m1[i], atrs[i], ctx[i]
        if a is None or a <= 0 or e9[i] is None or e21[i] is None or c is None:
            armed, i = None, i + 1
            continue
        up = c == "UP" and e9[i] > e21[i]
        down = c == "DOWN" and e9[i] < e21[i]
        if up and b["l"] <= e21[i]:
            armed = ("BUY", i)
        elif down and b["h"] >= e21[i]:
            armed = ("SELL", i)
        sig = None
        if armed and i - armed[1] <= 10 and i > armed[1]:
            if armed[0] == "BUY" and up and b["c"] > e9[i]:
                sig = "BUY"
            elif armed[0] == "SELL" and down and b["c"] < e9[i]:
                sig = "SELL"
        elif armed and i - armed[1] > 10:
            armed = None
        if not sig:
            i += 1
            continue
        armed = None
        t, nxt = b["t"], m1[i + 1]
        if (nxt["t"] - t) > timedelta(minutes=5) or (sessions_ok and SS.session_of(t, sessions_cfg) not in sessions_ok):
            i += 1
            continue
        buy = sig == "BUY"
        sp = nxt["s"] if nxt["s"] is not None else cfg["spread"]
        fill = (nxt["o"] + sp + slip) if buy else (nxt["o"] - slip)
        lo = min(x["l"] for x in m1[max(0, i - 5):i + 1])
        hi = max(x["h"] for x in m1[max(0, i - 5):i + 1])
        stop = (lo - 0.1 * a) if buy else (hi + sp + 0.1 * a)
        dist = max(abs(fill - stop), a, 3 * sp)
        stop = fill - dist if buy else fill + dist
        target = fill + rr * dist if buy else fill - rr * dist
        deadline = nxt["t"] + timedelta(minutes=hold_min)
        j = i + 1
        exit_price = exit_t = outcome = None
        while j < n:
            x = m1[j]
            spx = x["s"] if x["s"] is not None else cfg["spread"]
            if buy:
                stop_hit, tgt_hit = x["l"] <= stop, x["h"] >= target
            else:
                stop_hit, tgt_hit = x["h"] + spx >= stop, x["l"] + spx <= target
            if j == i + 1:
                tgt_hit = False
            if stop_hit:
                gap = (x["o"] < stop) if buy else (x["o"] + spx > stop)
                px = (x["o"] if gap else stop) - slip if buy else ((x["o"] + spx) if gap else stop) + slip
                exit_price, exit_t, outcome = px, x["t"], "stop (gap)" if gap else "stop"
                break
            if tgt_hit:
                exit_price, exit_t, outcome = target, x["t"], "target"
                break
            if x["t"] >= deadline or (j + 1 < n and (m1[j + 1]["t"] - x["t"]) > timedelta(minutes=5)):
                px = x["c"] if buy else x["c"] + spx
                exit_price, exit_t, outcome = px, x["t"], "time exit"
                break
            j += 1
        if exit_price is None:
            break
        usd = (exit_price - fill) if buy else (fill - exit_price)
        cost = sp + slip + (slip if outcome.startswith("stop") else 0.0)
        out.append(ScalpTrade(name, sig, t, round(fill, 2), round(stop, 2), round(target, 2), exit_t, round(exit_price, 2),
                              outcome, round(usd, 2), round(usd / dist, 3), c, SS.session_of(t, sessions_cfg),
                              int((exit_t - nxt["t"]).total_seconds() // 60), round(cost, 2)))
        i = j + 1
    return out


def run_pullbacks(m1: list, cfg: dict, sessions_cfg) -> dict:
    c = dict(DEFAULTS, **(cfg or {}))
    _sma, atrs = indicators(m1, c["ma"], c["atr"])
    closes = [b["c"] for b in m1]
    e9, e21, ctx = ema(closes, 9), ema(closes, 21), context_5m(m1)
    return {name: run_pullback(name, m1, atrs, ctx, e9, e21, c, sessions_cfg) for name in PB_VARIANTS}


def report_all(results: dict, split: datetime) -> list:
    """Both scalp families in one table (mean reversion + pullback), with in/out-of-sample and by year/session."""
    L = ["## Scalp candidates: 1M mean reversion vs 1M trend pullback (dollars per 0.01 lot, costs included)", "",
         "| variant | " + HEAD + " |", "|" + "---|" * 15]
    for v, tr in results.items():
        L.append(f"| {v} | " + fmt(stats(tr)) + " |")
    L += ["", "### In-sample vs out-of-sample", "", "| variant | part | " + HEAD + " |", "|" + "---|" * 16]
    for v, tr in results.items():
        for name, sel in (("in", lambda t: t.created < split), ("out", lambda t: t.created >= split)):
            L.append(f"| {v} | {name} | " + fmt(stats([t for t in tr if sel(t)])) + " |")
    for label, key in (("year", lambda t: t.created.year), ("session", lambda t: t.session)):
        L += ["", f"### By {label}", "", f"| variant | {label} | " + HEAD + " |", "|" + "---|" * 16]
        for v, tr in results.items():
            groups: dict = {}
            for t in tr:
                groups.setdefault(key(t), []).append(t)
            for g in sorted(groups, key=str):
                L.append(f"| {v} | {g} | " + fmt(stats(groups[g])) + " |")
    L += ["", "Pass bar before anything is built: net $ > 0 AND PF > 1.2 in BOTH halves, 300+ trades, after costs. "
          "`gross $` near zero = no edge in the candles; `gross $` > 0 but `avg $` < 0 = the spread took it.", ""]
    return L


# ====================================================================== 11 Nov: the same pullback on the 5M chart
# The 1M replay (2 years, 696k bars) showed a small edge before costs (+$0.06..+0.13 per 0.01-lot trade) that the
# $0.22 spread + slippage took. On 5M candles the move per trade is ~3x larger while the cost per trade stays the same,
# so the same rule is replayed one timeframe up: 5M entries (EMA 9/21, pullback to EMA21, close back beyond EMA9),
# 15M context (EMA21 slope), stop behind the last 6 5M candles, exits simulated on the 5M candles (stop before target).
PB5_VARIANTS = {
    "PB5M_rr1.5": (1.5, 180, None),
    "PB5M_rr2.0": (2.0, 240, None),
    "PB5M_rr1.5_LDN_NY": (1.5, 180, ("LONDON", "LONDON_NY", "NEW_YORK")),
    "PB5M_rr2.0_LDN_NY": (2.0, 240, ("LONDON", "LONDON_NY", "NEW_YORK")),
}


def aggregate(m1: list, sec: int) -> list:
    """Broker-aligned candles of `sec` seconds from M1 (the first M1's spread is the candle's spread)."""
    out, cur = [], None
    for b in m1:
        k = int(b["t"].timestamp() // sec)
        if k != cur:
            out.append({"t": datetime.fromtimestamp(k * sec, UTC), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                        "s": b["s"]})
            cur = k
        else:
            x = out[-1]
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return out


def context_htf(bars: list, period: int, block: int, n: int = 21, slope: int = 3) -> list:
    """context_5m for any pair: `bars` of `period` seconds, context from closed candles of `block` seconds."""
    closes, idx_of, cur = [], [], None
    for b in bars:
        k = int(b["t"].timestamp() // block)
        if k != cur:
            closes.append(b["c"])
            cur = k
        else:
            closes[-1] = b["c"]
        idx_of.append(len(closes) - 1)
    e = ema(closes, n)
    out = [None] * len(bars)
    for i, b in enumerate(bars):
        j = idx_of[i] if (b["t"].timestamp() + period) % block == 0 else idx_of[i] - 1
        if j - slope < 0 or e[j] is None or e[j - slope] is None:
            continue
        if closes[j] > e[j] and e[j] > e[j - slope]:
            out[i] = "UP"
        elif closes[j] < e[j] and e[j] < e[j - slope]:
            out[i] = "DOWN"
    return out


def run_pullbacks_5m(m1: list, cfg: dict, sessions_cfg) -> dict:
    c = dict(DEFAULTS, **(cfg or {}))
    m5 = aggregate(m1, 300)
    _sma, atrs = indicators(m5, c["ma"], c["atr"])
    closes = [b["c"] for b in m5]
    e9, e21, ctx = ema(closes, 9), ema(closes, 21), context_htf(m5, 300, 900)
    c5 = dict(c, warmup=max(30, c["warmup"] // 5))
    return {name: run_pullback(name, m5, atrs, ctx, e9, e21, c5, sessions_cfg, PB5_VARIANTS) for name in PB5_VARIANTS}
