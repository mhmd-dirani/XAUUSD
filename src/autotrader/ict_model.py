"""
ICT-style candidate engine for the replay (30 Oct 2026): liquidity sweep -> market structure shift with
displacement -> entry in the fair value gap of the displacement, in the London / New York killzones.

Written as deterministic checks on CLOSED candles so the replay can test the owner's rule set on two years of
1-minute data before a cent is spent. Nothing here touches the live trader; `replay.py` is the only caller.

The rule set (owner, 30 Oct), as implemented:
  1. HTF bias        Daily and 4H agree (macro BULLISH/BEARISH), or - `bias: "4h"` variants - the 4H trend alone.
  2. Draw on liquidity  an HTF pool in the bias direction (1H/4H swing, PDH/PDL, PWH/PWL, session high/low) at least
                     `min_draw_rr` x risk from the entry; no draw -> no trade.
  3. Sweep first     within the last `lookback` 5M bars price took a pool AGAINST the bias (a 5M/15M swing, a session
                     level, previous day/week): wick beyond, close back within 3 bars; the sweep extreme not violated
                     since.
  4. MSS             the current 5M close is the FIRST close beyond the most recent confirmed opposing 5M swing since
                     the sweep, and the leg from the sweep shows displacement: a body >= `displacement` x the average
                     body of the 20 bars before the sweep AND a fair value gap (3-candle gap) inside the leg.
  5. Entry           limit at the FVG's consequent encroachment (50 %), or its near edge (`entry: "edge"`), or market
                     on the shift close (`entry: "market"`); a limit becomes market if price is already at/through it
                     at the decision close. Limits expire after `expiry_minutes`.
  6. Position        the entry sits in discount (buys) / premium (sells) of the sweep-to-shift leg.
  7. Stop            beyond the sweep wick + spread + 0.05 ATR15.
  8. Targets         fixed 1R / 1.5R / 2R / 3R, nearest opposing liquidity then the draw (partial + breakeven, as the
                     live trader does), or the draw alone - one variant per target rule so the win-rate / expectancy
                     trade-off is measured, not argued.
  9. Timing          London killzone 02:00-05:00 New York time, New York killzone 07:00-10:00 New York time (DST-safe);
                     `killzones: False` variants trade all tradable hours for comparison. No news lock in history.
 10. One trade per killzone per direction per trading day; a sweep that produced a trade is never re-entered.

Everything is computed from bars that CLOSED at or before the decision time (the replay passes windows that end at
the 5M close; the HTF state comes from the previous 15M close).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from gold_smc_engine import _time

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
TRENDING = ("BULLISH", "BEARISH")
HTF_LEVELS = ("PDH", "PDL", "PWH", "PWL", "ASIA_HIGH", "ASIA_LOW", "LONDON_HIGH", "LONDON_LOW",
              "NEW_YORK_HIGH", "NEW_YORK_LOW")

DEFAULTS = {
    "lookback": 18,              # 5M bars (90 min) in which the sweep must have happened
    "reclaim_bars": 3,           # bars allowed between the wick through the pool and the close back
    "displacement": 1.5,         # max body in the leg >= this x average body of the 20 bars before the sweep
    "body_window": 20,
    "swing_left": 2,             # 5M fractal swings: left/right bars
    "swing_right": 2,
    "leg_bars": 30,              # how far before the sweep the opposing swing (the one MSS must break) may sit
    "mss_wait_bars": 2,          # the FVG may complete up to this many bars after the shift candle
    "expiry_minutes": 60,
    "max_hold_hours": 10,
    "spread_stop_fraction": 0.10,  # replay cap for ICT stops (the live gate's 5 % is counted in the funnel)
    "min_risk": 0.5,             # $ - a stop closer than this is noise on gold
}

# name: variant spec. Every variant sees the SAME setups; only bias source, killzone filter, entry and target differ.
VARIANTS = {
    "ICT_2R": dict(bias="macro", killzones=True, entry="ce", target="2R", min_draw_rr=2.0, discount=True),
    "ICT_1R": dict(bias="macro", killzones=True, entry="ce", target="1R", min_draw_rr=2.0, discount=True),
    "ICT_1_5R": dict(bias="macro", killzones=True, entry="ce", target="1.5R", min_draw_rr=2.0, discount=True),
    "ICT_3R": dict(bias="macro", killzones=True, entry="ce", target="3R", min_draw_rr=3.0, discount=True),
    "ICT_liq_then_draw": dict(bias="macro", killzones=True, entry="ce", target="liq_draw", min_draw_rr=2.0, discount=True),
    "ICT_draw": dict(bias="macro", killzones=True, entry="ce", target="draw", min_draw_rr=2.0, discount=True),
    "ICT_2R_4Hbias": dict(bias="4h", killzones=True, entry="ce", target="2R", min_draw_rr=2.0, discount=True),
    "ICT_2R_allHours": dict(bias="macro", killzones=False, entry="ce", target="2R", min_draw_rr=2.0, discount=True),
    "ICT_2R_edgeEntry": dict(bias="macro", killzones=True, entry="edge", target="2R", min_draw_rr=2.0, discount=True),
    "ICT_1R_4Hbias_allHours": dict(bias="4h", killzones=False, entry="ce", target="1R", min_draw_rr=2.0, discount=True),
    # sweeps of HTF pools only (15M+ swings, session levels, PDH/PDL, PWH/PWL) - fewer, "cleaner" sweeps
    "ICT_2R_htfSweep": dict(bias="macro", killzones=True, entry="ce", target="2R", min_draw_rr=2.0, discount=True, pools="htf"),
    "ICT_1R_htfSweep": dict(bias="macro", killzones=True, entry="ce", target="1R", min_draw_rr=2.0, discount=True, pools="htf"),
    "ICT_2R_htfSweep_4Hbias_allHours": dict(bias="4h", killzones=False, entry="ce", target="2R", min_draw_rr=2.0, discount=True, pools="htf"),
    # market entry on the shift close: no retrace needed, every setup becomes a trade (worse price, no "TP1 before fill")
    "ICT_2R_marketEntry": dict(bias="macro", killzones=True, entry="market", target="2R", min_draw_rr=2.0, discount=False),
    "ICT_1R_marketEntry": dict(bias="macro", killzones=True, entry="market", target="1R", min_draw_rr=2.0, discount=False),
    "ICT_2R_marketEntry_4Hbias": dict(bias="4h", killzones=True, entry="market", target="2R", min_draw_rr=2.0, discount=False),
    "ICT_1R_marketEntry_4Hbias_allHours": dict(bias="4h", killzones=False, entry="market", target="1R", min_draw_rr=2.0, discount=False),
}
FIXED_R = {"1R": 1.0, "1.5R": 1.5, "2R": 2.0, "3R": 3.0}


# ---------------------------------------------------------------------- time
def killzone(t: datetime) -> str | None:
    """'LONDON_KZ' (02:00-05:00 NY), 'NY_KZ' (07:00-10:00 NY) or None, for a UTC instant (the 5M close)."""
    ny = t.astimezone(NY)
    local = ny.time()
    if ny.weekday() >= 5:
        return None
    if dtime(2, 0) <= local < dtime(5, 0):
        return "LONDON_KZ"
    if dtime(7, 0) <= local < dtime(10, 0):
        return "NY_KZ"
    return None


def hour_bucket(t: datetime) -> str:
    return f"{t.astimezone(NY).hour:02d}NY"


# ---------------------------------------------------------------------- structure on 5M bars
def swings(bars: list, left: int, right: int) -> list[dict]:
    """Confirmed fractal swings: bar i is a swing HIGH when its high is the strict maximum of [i-left, i+right]
    (mirror for lows). Only swings whose `right` bars have closed are returned -> no look-ahead."""
    out = []
    n = len(bars)
    for i in range(left, n - right):
        hi, lo = bars[i]["high"], bars[i]["low"]
        if all(bars[k]["high"] < hi for k in range(i - left, i + right + 1) if k != i):
            out.append({"i": i, "side": "HIGH", "price": hi, "time": bars[i]["time"]})
        if all(bars[k]["low"] > lo for k in range(i - left, i + right + 1) if k != i):
            out.append({"i": i, "side": "LOW", "price": lo, "time": bars[i]["time"]})
    return out


def pools(bars5: list, sw5: list, state: dict, lv: list) -> dict:
    """Liquidity pools with the time they came into existence: {'HIGH': [(price, label, formed_utc)], 'LOW': [...]}.
    5M swings (formed at the swing bar's open; a bar can only sweep a swing that precedes it), 15M/1H/4H swings from
    the regime frames, completed session levels (PDH/PDL, PWH/PWL, Asia/London/NY highs and lows, opening ranges)."""
    out = {"HIGH": [], "LOW": []}
    for s in sw5:
        out[s["side"]].append((s["price"], f"5m:{s['side']}", _time(bars5[s["i"]]["time"])))
    for tf in ("15min", "1h", "4h"):
        for s in ((state.get("_frames") or {}).get(tf) or {}).get("swings") or []:
            out[s["side"]].append((s["price"], f"{tf}:{s['side']}", _time(s["time"])))
    for l in lv or []:
        formed = l.get("formed_at")
        out[l["side"]].append((l["price"], l["name"], _time(formed) if formed else datetime(2000, 1, 1, tzinfo=UTC)))
    return out


def find_sweep(bars5: list, direction: str, pools_: dict, cfg: dict, pool_filter: str = "all") -> dict | None:
    """The most recent sweep AGAINST the trade direction inside the lookback: for a BUY, a wick below a LOW pool and a
    close back above it within `reclaim_bars`; the sweep low must not have been taken out since. Returns
    {'j': wick bar, 'j2': reclaim bar, 'extreme': price, 'pool': (price, label)} or None."""
    n = len(bars5) - 1
    buy = direction == "BUY"
    side = "LOW" if buy else "HIGH"
    best = None
    for j in range(max(cfg["swing_left"] + cfg["body_window"], n - cfg["lookback"]), n + 1):
        b = bars5[j]
        bj = _time(b["time"])
        for price, label, formed in pools_[side]:
            if formed >= bj:
                continue                                           # the pool did not exist when bar j printed
            if pool_filter == "htf" and label.startswith("5m:"):
                continue                                           # HTF pools only: 15M+ swings and session levels
            through = b["low"] < price if buy else b["high"] > price
            if not through:
                continue
            for j2 in range(j, min(n, j + cfg["reclaim_bars"]) + 1):
                back = bars5[j2]["close"] > price if buy else bars5[j2]["close"] < price
                if back:
                    break
            else:
                continue
            ext = min(x["low"] for x in bars5[j:j2 + 1]) if buy else max(x["high"] for x in bars5[j:j2 + 1])
            if any((x["low"] < ext) if buy else (x["high"] > ext) for x in bars5[j2 + 1:n + 1]):
                continue                                           # the sweep failed: a deeper sweep since
            cand = {"j": j, "j2": j2, "extreme": ext, "pool": (price, label)}
            # prefer the most recent reclaim; for the same reclaim bar the earliest wick bar (the complete sweep,
            # whose extreme is the real stop reference); for the same bars the deepest pool
            key = (j2, -j, (-price) if buy else price)
            if best is None or key > best["_key"]:
                best = dict(cand, _key=key)
    if best:
        best.pop("_key", None)
    return best


def shift(bars5: list, sw5: list, direction: str, sweep: dict, cfg: dict) -> dict | None:
    """Market structure shift: the FIRST 5M close beyond the most recent confirmed opposing swing of the leg into the
    sweep happened on bar m, with m within `mss_wait_bars` of the current bar n (the FVG of the displacement usually
    completes one bar after the shift candle). Displacement: a body in the leg >= `displacement` x the average body of
    the 20 bars before the sweep. An FVG (3-candle gap) must exist inside the leg [j, n] and must be NEW on this bar
    (if it already existed one bar ago the setup fired then). Returns the leg and the FVG, or None."""
    n = len(bars5) - 1
    buy = direction == "BUY"
    j, j2 = sweep["j"], sweep["j2"]
    opp = [s for s in sw5 if s["side"] == ("HIGH" if buy else "LOW") and j - cfg["leg_bars"] <= s["i"] <= n - cfg["swing_right"]]
    if not opp:
        return None
    s = max(opp, key=lambda x: x["i"])
    level = s["price"]
    first_after = max(s["i"], j2)
    m = next((k for k in range(first_after, n + 1) if k >= j2 and ((bars5[k]["close"] > level) if buy else (bars5[k]["close"] < level))), None)
    if m is None or n - m > cfg["mss_wait_bars"]:
        return None
    bodies = [abs(x["close"] - x["open"]) for x in bars5[max(0, j - cfg["body_window"]):j]]
    avg_body = (sum(bodies) / len(bodies)) if bodies else 0.0
    leg = bars5[j:n + 1]
    max_body = max(abs(x["close"] - x["open"]) for x in leg)
    if avg_body <= 0 or max_body < cfg["displacement"] * avg_body:
        return None
    fvg = _fvg(bars5, j, n, buy)
    if fvg is None:
        return None
    if n > m and _fvg(bars5, j, n - 1, buy) is not None:
        return None                                                # already fired on an earlier bar of this leg
    leg_hi = max(x["high"] for x in leg)
    leg_lo = min(x["low"] for x in leg)
    return {"level": level, "swing_i": s["i"], "mss_i": m, "fvg_low": fvg[0], "fvg_high": fvg[1], "fvg_k": fvg[2],
            "leg_low": leg_lo, "leg_high": leg_hi, "max_body": max_body, "avg_body": avg_body}


def _fvg(bars5: list, j: int, n: int, buy: bool):
    """The most recent 3-candle fair value gap fully formed inside bars [j, n]: (low, high, middle index) or None.
    Bullish: bar k+1 low above bar k-1 high. Bearish: bar k+1 high below bar k-1 low."""
    out = None
    for k in range(j + 1, n):
        a, c = bars5[k - 1], bars5[k + 1]
        if buy and c["low"] > a["high"]:
            out = (a["high"], c["low"], k)
        elif not buy and c["high"] < a["low"]:
            out = (c["high"], a["low"], k)
    return out


# ---------------------------------------------------------------------- the setup
def bias_of(state: dict, mode: str) -> str | None:
    """'BUY' / 'SELL' / None from the HTF state. 'macro': Daily and 4H agree. '4h': the 4H trend alone."""
    t = state.get("macro_trend") if mode == "macro" else state.get("trend_4h")
    if t == "BULLISH":
        return "BUY"
    if t == "BEARISH":
        return "SELL"
    return None


def setup(bars5: list, state: dict, lv: list, as_of: datetime, sp: float, atr15: float, cfg: dict,
          drop=lambda why: None, pool_filter: str = "all", pre: dict | None = None) -> dict | None:
    """One ICT setup at the 5M close `as_of` (bars5 = closed 5M bars ending at as_of), or None.
    Direction-agnostic: both biases are evaluated by the caller through `bias_of`; this builds the geometry for
    whichever direction is passed in state['_ict_direction']."""
    direction = state["_ict_direction"]
    buy = direction == "BUY"
    n = len(bars5) - 1
    if n < cfg["body_window"] + cfg["lookback"] + 5:
        return drop("ict: not enough 5M history")
    if pre is None:
        pre = {}
    if "sw5" not in pre:                                           # the caller may share swings/pools across calls
        pre["sw5"] = swings(bars5, cfg["swing_left"], cfg["swing_right"])
        pre["pools"] = pools(bars5, pre["sw5"], state, lv)
    sw5, pl = pre["sw5"], pre["pools"]
    sweep = find_sweep(bars5, direction, pl, cfg, pool_filter)
    if sweep is None:
        return None                                                # the common case: no sweep -> silent
    sh = shift(bars5, sw5, direction, sweep, cfg)
    if sh is None:
        return None
    ce = (sh["fvg_low"] + sh["fvg_high"]) / 2
    edge = sh["fvg_high"] if buy else sh["fvg_low"]
    cur = bars5[n]
    price_now = cur["close"] + (sp if buy else 0.0)
    pad = sp + 0.05 * atr15
    stop = round((sweep["extreme"] - pad) if buy else (sweep["extreme"] + pad), 2)
    mid = (sh["leg_low"] + sh["leg_high"]) / 2
    highs = sorted(p for p, _, _ in pl["HIGH"])
    lows = sorted((p for p, _, _ in pl["LOW"]), reverse=True)
    htf_highs = sorted((p, l) for p, l, _ in pl["HIGH"] if l.split(":")[0] in ("1h", "4h") or l in HTF_LEVELS)
    htf_lows = sorted(((p, l) for p, l, _ in pl["LOW"] if l.split(":")[0] in ("1h", "4h") or l in HTF_LEVELS), reverse=True)
    return {"direction": direction, "created": as_of, "sweep": sweep, "shift": sh, "ce": round(ce, 2),
            "edge": round(edge, 2), "stop": stop, "price": price_now, "spread": sp, "atr15": atr15,
            "leg_mid": mid, "highs": highs, "lows": lows, "htf_highs": htf_highs, "htf_lows": htf_lows,
            "killzone": killzone(as_of), "sweep_time": bars5[sweep["j"]]["time"],
            "pool_filter": pool_filter,
            "cid": f"ICT:{as_of:%Y%m%d%H%M}:{direction}:{pool_filter}:{bars5[sweep['j']]['time']}"}


def plan(st: dict, spec: dict, cfg: dict, drop=lambda why: None) -> dict | None:
    """Setup + variant spec -> order plan {order_type, entry, stop, tp1, tp2, expiry, rr, draw} or None."""
    buy = st["direction"] == "BUY"
    entry = st["price"] if spec["entry"] == "market" else st["ce"] if spec["entry"] == "ce" else st["edge"]
    if spec["entry"] != "market" and spec.get("discount") and ((buy and entry > st["leg_mid"]) or (not buy and entry < st["leg_mid"])):
        return drop("ict: entry not in discount/premium")
    stop = st["stop"]
    risk = (entry - stop) if buy else (stop - entry)
    if risk < max(cfg["min_risk"], 3 * st["spread"]):
        return drop("ict: stop too close")
    if risk > cfg.get("sl_atr_max", 5.0) * st["atr15"]:
        return drop("ict: stop wider than sl_atr_max")
    if st["spread"] > cfg.get("max_spread", 0.5):
        return drop("ict: spread above max_spread")
    if st["spread"] > cfg["spread_stop_fraction"] * risk:
        return drop(f"ict: spread > {int(cfg['spread_stop_fraction'] * 100)}% of the stop")
    need = spec["min_draw_rr"] * risk
    if buy:
        draws = [(p, l) for p, l in st["htf_highs"] if p >= entry + need]
        liq = [p for p in st["highs"] if p >= entry + 1.0 * risk]
    else:
        draws = [(p, l) for p, l in st["htf_lows"] if p <= entry - need]
        liq = [p for p in st["lows"] if p <= entry - 1.0 * risk]
    if not draws:
        return drop(f"ict: no HTF draw >= {spec['min_draw_rr']}R")
    draw, draw_label = draws[0]
    tgt = spec["target"]
    if tgt in FIXED_R:
        tp1 = round(entry + FIXED_R[tgt] * risk if buy else entry - FIXED_R[tgt] * risk, 2)
        tp2 = None
    elif tgt == "draw":
        tp1, tp2 = draw, None
    else:                                                          # liq_draw
        tp1 = liq[0] if liq else draw
        tp2 = draw if abs(draw - tp1) >= 0.5 * risk else None
        if tp1 == draw:
            tp2 = None
    # price already at/through the entry -> market at the decision close; otherwise a limit into the FVG
    at_or_through = (st["price"] <= entry) if buy else (st["price"] >= entry)
    if at_or_through:
        otype, entry_px = "MARKET", st["price"]
        if (buy and entry_px <= stop) or (not buy and entry_px >= stop):
            return drop("ict: price beyond the stop")
        expiry = st["created"] + timedelta(minutes=15)
    else:
        otype, entry_px = ("BUY_LIMIT" if buy else "SELL_LIMIT"), entry
        expiry = st["created"] + timedelta(minutes=cfg["expiry_minutes"])
    rr = abs(tp1 - entry_px) / max(1e-9, abs(entry_px - stop))
    return {"order_type": otype, "entry": entry_px, "stop": stop, "tp1": tp1, "tp2": tp2, "expiry": expiry,
            "rr": round(rr, 2), "draw": draw, "draw_label": draw_label, "risk": round(abs(entry_px - stop), 2),
            "live_gate_spread_ok": st["spread"] <= 0.05 * abs(entry_px - stop)}
